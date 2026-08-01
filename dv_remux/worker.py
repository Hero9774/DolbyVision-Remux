"""Die drei Haupt-Worker (je ein eigener Thread): Filme, Serien, Einzelordner."""

import queue
import shutil
from datetime import datetime
from pathlib import Path

from dv_remux.konstanten import VERSION, DOVI_TOOL, TEXT_CODECS
from dv_remux.sprache import t, _bereinige_log
from dv_remux.mkv_analyse import (
    finde_mkv, lese_hdrtype_aus_nfo, ermittle_hdrtype_aus_mkv,
    ermittle_dv_profil, simuliere_streams_aus_nfo, ermittle_untertitel_streams,
)
from dv_remux.mp4_binary import nachbearbeite_dv_mp4
from dv_remux.dateioperationen import (
    genug_speicherplatz, verschiebe_sicher, kopiere_mit_fortschritt,
    verschiebe_oder_loesche_mkv,
)
from dv_remux.pipeline import (
    konvertiere_dv_p5_zu_p8, remux_zu_mp4, extrahiere_untertitel,
    aktualisiere_nfo, rollback_session, schreibe_log_datei,
)


def verarbeite_serien(
        ffmpeg_pfad: str, ffprobe_pfad: str, root_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None,
        lokale_kopie: bool = False, lokale_kopie_pfad: Path = None,
        dts_zu_eac3: bool = False):
    """Serien-Worker: root → Show-Ordner → Staffel-Ordner → episode.mkv"""

    ffmpeg     = Path(ffmpeg_pfad)
    ffprobe    = Path(ffprobe_pfad)
    root       = Path(root_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = t("logheader.sim") if simulation else t("logheader.run")
    log("HEAD", f"{'='*55}")
    log("HEAD", t("worker.header_title", version=VERSION, modus=modus_text, suffix="  [SERIEN]"))
    log("HEAD", t("worker.header_start", datum=datetime.now().strftime('%d.%m.%Y %H:%M:%S')))
    log("HEAD", t("worker.header_root", root=root))
    log("HEAD", f"{'='*55}")

    # Trickplay-Ordner auf allen Ebenen ignorieren
    # (Jellyfin: "trickplay" oder ".trickplay" als Ordnername)
    def ist_kein_trickplay(p: Path) -> bool:
        return p.is_dir() and "trickplay" not in p.name.lower()

    # Wenn root selbst MKV-Dateien enthält → Einzelserie direkt im Root-Ordner
    if any(root.glob("*.mkv")):
        show_liste = [root]
    else:
        show_liste = sorted([p for p in root.iterdir() if ist_kein_trickplay(p)])

    gesamt   = len(show_liste)
    stats    = {"gefunden": 0, "remuxed": 0, "uebersprungen": 0, "fehler": 0}
    undo_log = []

    for i, show_ordner in enumerate(show_liste):
        if stopp_event and stopp_event.is_set():
            log("WARN", t("worker.user_abort"))
            break

        fort_q.put(int(i / gesamt * 100) if gesamt else 0)
        log("FOLDER", t("worker.serien.show_folder", name=show_ordner.name))

        staffeln = sorted([p for p in show_ordner.iterdir() if ist_kein_trickplay(p)])
        if not staffeln:
            staffeln = [show_ordner]

        for staffel in staffeln:
            if stopp_event and stopp_event.is_set():
                break
            if staffel != show_ordner:
                log("INFO", t("worker.serien.season_folder", name=staffel.name))

            for mkv_pfad in sorted(staffel.glob("*.mkv")):
                if stopp_event and stopp_event.is_set():
                    break

                anzeige = (f"{show_ordner.name}  /  {staffel.name}  /  {mkv_pfad.stem}"
                           if staffel != show_ordner
                           else f"{show_ordner.name}  /  {mkv_pfad.stem}")
                task_q.put({"film": anzeige, "schritt": t("worker.step_hdr_detect"),
                            "sub_prog": None})

                nfo_pfad = mkv_pfad.with_suffix(".nfo")
                mp4_pfad = mkv_pfad.with_suffix(".mp4")

                # HDR-Typ ermitteln: NFO zuerst, ffprobe als Fallback
                hdrtype = lese_hdrtype_aus_nfo(nfo_pfad) if nfo_pfad.exists() else None
                if hdrtype:
                    key = "worker.hdr_type_nfo_sim" if simulation else "worker.hdr_type_nfo"
                    log("SIM" if simulation else "INFO", "  " + t(key, hdrtype=hdrtype))
                else:
                    hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
                    anzeige_hdrtype = hdrtype or t("worker.hdr_not_detected")
                    key = "worker.hdr_type_ffprobe_sim" if simulation else "worker.hdr_type_ffprobe"
                    log("SIM" if simulation else "INFO", "  " + t(key, hdrtype=anzeige_hdrtype))
                if hdrtype != "dolbyvision":
                    log("SKIP", t("worker.serien.not_dv", name=mkv_pfad.name))
                    stats["uebersprungen"] += 1
                    task_q.put({"schritt": t("worker.step_skipped_no_dv"), "sub_prog": 100})
                    continue

                stats["gefunden"] += 1
                log("INFO", t("worker.serien.dv_found", name=mkv_pfad.name))

                # DV-Profil-Prüfung: Profil 5 (ICtCp) → P5→P8-Konvertierung oder Warnung
                braucht_p5_fix = False
                dv_profil, farb_matrix = ermittle_dv_profil(ffprobe, mkv_pfad)
                _ictcp = farb_matrix and any(kw in str(farb_matrix).lower()
                                    for kw in ("ictcp", "ipt-pq", "ipt_pq"))
                if dv_profil == 5 or _ictcp:
                    braucht_p5_fix = True
                    if DOVI_TOOL.exists():
                        log("WARN", "  " + t("worker.p5_detected_converting", profil=dv_profil or "?"))
                    else:
                        log("WARN", "  " + t("worker.p5_detected_no_tool", profil=dv_profil or "?"))
                        log("WARN", "      " + t("worker.p5_tool_hint"))

                # Untertitel-Streams ermitteln (vor Remux)
                streams = []
                if embed_subs or untertitel:
                    task_q.put({"schritt": t("worker.step_subs_analyze"), "sub_prog": None})
                    log("INFO", "  " + t("worker.subs_analyzing"))
                    if simulation:
                        streams = simuliere_streams_aus_nfo(nfo_pfad)
                        log("SIM", "  " + t("worker.subs_found_sim", anzahl=len(streams)))
                    else:
                        streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
                    if streams:
                        log("INFO", "  " + t("worker.subs_found", anzahl=len(streams)))

                text_sub_indices = None
                if embed_subs and streams:
                    eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
                    bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
                    if eng_subs:
                        text_sub_indices = [s["index"] for s in eng_subs]
                        log("INFO", "  " + t("worker.subs_embed_count", anzahl=len(eng_subs)))
                    for s in bitmap_subs:
                        log("SKIP", "  " + t("worker.subs_bitmap_skip", index=s['index'],
                                              sprache=s['language'], codec=s['codec'].upper()))

                # Remux (normaler Pfad) oder P5→P8-Konvertierung, SRT-Extraktion,
                # MKV-Verschieben und NFO-Update – alles in einem try/finally,
                # damit eine evtl. lokale Arbeitskopie immer aufgeräumt wird.
                neu_remuxed = False
                arbeits_mkv, arbeits_mp4 = mkv_pfad, mp4_pfad
                lokal_aktiv = False
                mkv_verschoben = False
                remux_ok = False
                erfolg = False
                try:
                    if mp4_pfad.exists():
                        log("SKIP", "  " + t("worker.serien.mp4_exists"))
                        stats["uebersprungen"] += 1
                        task_q.put({"schritt": t("worker.step_mp4_exists"), "sub_prog": 100})
                    else:
                        # Optional: MKV in lokalen Arbeitsordner verschieben (NAS-Entlastung;
                        # verhindert außerdem dass Jellyfin während der Verarbeitung MKV+MP4
                        # als Dubletten im selben NAS-Ordner sieht)
                        if lokale_kopie:
                            if not simulation and not genug_speicherplatz(lokale_kopie_pfad, mkv_pfad.stat().st_size):
                                log("WARN", "  " + t("worker.no_space_skip"))
                                stats["uebersprungen"] += 1
                                task_q.put({"schritt": t("worker.step_skipped_space"), "sub_prog": 100})
                                continue
                            arbeits_mkv = lokale_kopie_pfad / mkv_pfad.name
                            arbeits_mp4 = lokale_kopie_pfad / mp4_pfad.name
                            lokal_aktiv = True
                            if not verschiebe_sicher(
                                    mkv_pfad, arbeits_mkv, t("worker.desc_move_to_workdir"),
                                    log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event):
                                stats["fehler"] += 1
                                task_q.put({"schritt": t("worker.step_move_failed"), "sub_prog": 0})
                                continue
                            mkv_verschoben = True

                        task_q.put({"schritt": t("worker.step_remux_running"), "sub_prog": None})
                        if braucht_p5_fix and DOVI_TOOL.exists():
                            task_q.put({"schritt": t("worker.step_p5p8_running"), "sub_prog": None})
                            remux_ok = konvertiere_dv_p5_zu_p8(
                                ffmpeg, DOVI_TOOL, ffprobe,
                                arbeits_mkv, arbeits_mp4,
                                log_q, task_q, simulation, log_zeilen,
                                stopp_event=stopp_event)
                        else:
                            remux_ok = remux_zu_mp4(
                                ffmpeg, arbeits_mkv, arbeits_mp4,
                                log_q, task_q, simulation, log_zeilen,
                                stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                                ffprobe_pfad=ffprobe, kein_faststart=True,
                                dts_zu_eac3=dts_zu_eac3)
                            if remux_ok:
                                nachbearbeite_dv_mp4(arbeits_mp4, log_q, log_zeilen, simulation)

                        erfolg = remux_ok
                        if remux_ok and lokal_aktiv:
                            erfolg = kopiere_mit_fortschritt(
                                arbeits_mp4, mp4_pfad, t("worker.desc_move_to_nas"),
                                log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event)
                            if not erfolg:
                                log("ERR", "  " + t("worker.mp4_transfer_failed", pfad=arbeits_mp4))

                        if erfolg:
                            stats["remuxed"] += 1
                            neu_remuxed = True
                            log("OK", "  " + t("worker.remux_success"))
                            task_q.put({"schritt": t("worker.step_remux_done"), "sub_prog": 100})
                            if not simulation:
                                undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
                        else:
                            stats["fehler"] += 1
                            if mp4_pfad.exists():
                                mp4_pfad.unlink()
                            task_q.put({"schritt": t("worker.step_remux_failed"), "sub_prog": 0})
                            continue

                    # SRT extrahieren (vor dem Verschieben der MKV!)
                    erstellte_srts = []
                    if streams and (untertitel or embed_subs):
                        srt_streams = ([s for s in streams if s["language"] != "eng"]
                                       if embed_subs else streams)
                        if srt_streams:
                            erstellte_srts = extrahiere_untertitel(
                                ffmpeg, arbeits_mkv, srt_streams, log_q, task_q, simulation, log_zeilen,
                                undo_log=undo_log if not simulation else None,
                                ziel_ordner=mkv_pfad.parent)
                        elif untertitel:
                            log("INFO", "  " + t("worker.no_foreign_subs"))
                            task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})
                    elif untertitel:
                        log("INFO", "  " + t("worker.serien.no_subs"))
                        task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})

                    # MKV verschieben / löschen (nach SRT-Extraktion!)
                    if neu_remuxed:
                        verschiebe_oder_loesche_mkv(
                            mkv_pfad, original_behalten, simulation, log,
                            undo_log=undo_log,
                            old_mkv_global_pfad=old_mkv_global_pfad,
                            aktueller_pfad=arbeits_mkv if lokal_aktiv else None)
                        if lokal_aktiv and not simulation:
                            mkv_verschoben = arbeits_mkv.exists()

                    # NFO aktualisieren
                    if nfo_update and nfo_pfad.exists():
                        aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                                         simulation, log_zeilen, undo_log=undo_log)
                    elif nfo_update:
                        log("INFO", "  " + t("worker.serien.no_nfo"))
                finally:
                    if lokal_aktiv and not simulation:
                        if arbeits_mkv.exists():
                            if mkv_verschoben:
                                try:
                                    mkv_pfad.parent.mkdir(parents=True, exist_ok=True)
                                    if mkv_pfad.exists():
                                        log("ERR", "  " + t("worker.restore_target_occupied", pfad=arbeits_mkv))
                                    else:
                                        shutil.move(str(arbeits_mkv), str(mkv_pfad))
                                        log("WARN", "  " + t("worker.restore_done", name=mkv_pfad.name))
                                except Exception as e:
                                    log("ERR", "  " + t("worker.restore_failed", fehler=e, pfad=arbeits_mkv))
                            else:
                                arbeits_mkv.unlink(missing_ok=True)
                        ruecktransfer_fehlgeschlagen = remux_ok and not erfolg
                        if arbeits_mp4.exists() and not ruecktransfer_fehlgeschlagen:
                            arbeits_mp4.unlink(missing_ok=True)

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": t("worker.done"), "schritt": "", "sub_prog": 100})

    log("HEAD", f"\n{'='*55}")
    log("HEAD",   t("worker.summary_title", suffix="  [SERIEN]"))
    log("HEAD", f"{'='*55}")
    log("OK",   t("worker.summary_found", anzahl=stats['gefunden']))
    log("OK",   t("worker.summary_remuxed", anzahl=stats['remuxed']))
    log("SKIP", t("worker.summary_skipped", anzahl=stats['uebersprungen']))
    log("ERR",  t("worker.summary_errors", anzahl=stats['fehler']))
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", t("worker.summary_sim_note"))

    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", t("worker.summary_log_saved", name=log_pfad.name))
    log("HEAD", t("worker.summary_log_location", pfad=log_pfad.parent))
    done_q.put((stats, log_pfad))


def verarbeite_sammlung(
        ffmpeg_pfad: str, ffprobe_pfad: str, root_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None,
        lokale_kopie: bool = False, lokale_kopie_pfad: Path = None,
        dts_zu_eac3: bool = False):
    """Haupt-Worker (eigener Thread)."""

    ffmpeg  = Path(ffmpeg_pfad)
    ffprobe = Path(ffprobe_pfad)
    root    = Path(root_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = t("logheader.sim") if simulation else t("logheader.run")
    log("HEAD", f"{'='*55}")
    log("HEAD", t("worker.header_title", version=VERSION, modus=modus_text, suffix=""))
    log("HEAD", t("worker.header_start", datum=datetime.now().strftime('%d.%m.%Y %H:%M:%S')))
    log("HEAD", t("worker.header_root", root=root))
    log("HEAD", f"{'='*55}")

    ordner_liste = sorted([p for p in root.iterdir() if p.is_dir()])
    gesamt   = len(ordner_liste)
    stats    = {"gefunden": 0, "remuxed": 0, "uebersprungen": 0, "fehler": 0}
    undo_log = []

    for i, ordner in enumerate(ordner_liste):
        if stopp_event and stopp_event.is_set():
            log("WARN", t("worker.user_abort"))
            break

        fort_q.put(int(i / gesamt * 100) if gesamt else 0)
        task_q.put({"film": ordner.name, "schritt": t("worker.step_finding_mkv"), "sub_prog": None})
        log("FOLDER", t("worker.folder_movie", name=ordner.name))

        # 1. MKV-Datei suchen
        mkv_pfad = finde_mkv(ordner)
        if mkv_pfad is None:
            log("SKIP", t("worker.sammlung.no_mkv"))
            stats["uebersprungen"] += 1
            task_q.put({"schritt": t("worker.step_no_mkv"), "sub_prog": 100})
            continue

        # 2. HDR-Typ ermitteln (im Sim-Modus aus NFO, sonst direkt aus MKV)
        task_q.put({"schritt": t("worker.step_hdr_detect"), "sub_prog": None})
        nfo_pfad_sim = ordner / "movie.nfo"
        hdrtype = lese_hdrtype_aus_nfo(nfo_pfad_sim) if nfo_pfad_sim.exists() else None
        if hdrtype:
            key = "worker.hdr_type_nfo_sim" if simulation else "worker.hdr_type_nfo"
            log("SIM" if simulation else "INFO", t(key, hdrtype=hdrtype))
        else:
            hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
            anzeige_hdrtype = hdrtype or t("worker.hdr_not_detected")
            key = "worker.hdr_type_ffprobe_sim" if simulation else "worker.hdr_type_ffprobe"
            log("SIM" if simulation else "INFO", t(key, hdrtype=anzeige_hdrtype))
        if hdrtype != "dolbyvision":
            log("SKIP", t("worker.no_dv_skip"))
            stats["uebersprungen"] += 1
            task_q.put({"schritt": t("worker.step_skipped_no_dv"), "sub_prog": 100})
            continue

        stats["gefunden"] += 1
        nfo_pfad = ordner / "movie.nfo"
        mp4_pfad = mkv_pfad.with_suffix(".mp4")

        # DV-Profil-Prüfung: Profil 5 (ICtCp) → P5→P8-Konvertierung oder Warnung
        braucht_p5_fix = False
        dv_profil, farb_matrix = ermittle_dv_profil(ffprobe, mkv_pfad)
        _ictcp = farb_matrix and any(kw in str(farb_matrix).lower()
                                    for kw in ("ictcp", "ipt-pq", "ipt_pq"))
        if dv_profil == 5 or _ictcp:
            braucht_p5_fix = True
            if DOVI_TOOL.exists():
                log("WARN", t("worker.p5_detected_converting", profil=dv_profil or "?"))
            else:
                log("WARN", t("worker.p5_detected_no_tool", profil=dv_profil or "?"))
                log("WARN", t("worker.p5_tool_hint"))

        # 3. Untertitel-Streams ermitteln (vor Remux, für Einbettung + SRT)
        streams = []
        if embed_subs or untertitel:
            task_q.put({"schritt": t("worker.step_subs_analyze"), "sub_prog": None})
            log("INFO", t("worker.subs_analyzing"))
            if simulation:
                streams = simuliere_streams_aus_nfo(nfo_pfad)
                log("SIM", t("worker.subs_found_sim", anzahl=len(streams)))
            else:
                streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
            if streams:
                log("INFO", t("worker.subs_found", anzahl=len(streams)))

        text_sub_indices = None
        if embed_subs and streams:
            eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
            bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
            if eng_subs:
                text_sub_indices = [s["index"] for s in eng_subs]
                log("INFO", t("worker.subs_embed_count", anzahl=len(eng_subs)))
            for s in bitmap_subs:
                log("SKIP", t("worker.subs_bitmap_skip", index=s['index'],
                               sprache=s['language'], codec=s['codec'].upper()))

        # 4. MP4 bereits vorhanden? / Remux / SRT / MKV-Verschieben / NFO
        # (alles in einem try/finally, damit eine evtl. lokale Arbeitskopie
        # in jedem Fall am Ende aufgeräumt wird – auch bei "continue")
        neu_remuxed = False
        arbeits_mkv, arbeits_mp4 = mkv_pfad, mp4_pfad
        lokal_aktiv = False
        mkv_verschoben = False
        remux_ok = False
        erfolg = False
        try:
            if mp4_pfad.exists():
                log("SKIP", t("worker.mp4_exists_skip"))
                stats["uebersprungen"] += 1
                task_q.put({"schritt": t("worker.step_mp4_exists"), "sub_prog": 100})
            else:
                # 5. Optional: MKV in lokalen Arbeitsordner verschieben (NAS-Entlastung;
                # verhindert außerdem dass Jellyfin während der Verarbeitung MKV+MP4
                # als Dubletten im selben NAS-Ordner sieht)
                if lokale_kopie:
                    if not simulation and not genug_speicherplatz(lokale_kopie_pfad, mkv_pfad.stat().st_size):
                        log("WARN", t("worker.no_space_skip"))
                        stats["uebersprungen"] += 1
                        task_q.put({"schritt": t("worker.step_skipped_space"), "sub_prog": 100})
                        continue
                    arbeits_mkv = lokale_kopie_pfad / mkv_pfad.name
                    arbeits_mp4 = lokale_kopie_pfad / mp4_pfad.name
                    lokal_aktiv = True
                    if not verschiebe_sicher(
                            mkv_pfad, arbeits_mkv, t("worker.desc_move_to_workdir"),
                            log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event):
                        stats["fehler"] += 1
                        task_q.put({"schritt": t("worker.step_move_failed"), "sub_prog": 0})
                        continue
                    mkv_verschoben = True

                # 6. Remux (normaler Pfad) oder P5→P8-Konvertierung
                task_q.put({"schritt": t("worker.step_remux_running"), "sub_prog": None})
                if braucht_p5_fix and DOVI_TOOL.exists():
                    task_q.put({"schritt": t("worker.step_p5p8_running"), "sub_prog": None})
                    remux_ok = konvertiere_dv_p5_zu_p8(
                        ffmpeg, DOVI_TOOL, ffprobe,
                        arbeits_mkv, arbeits_mp4,
                        log_q, task_q, simulation, log_zeilen,
                        stopp_event=stopp_event)
                else:
                    remux_ok = remux_zu_mp4(
                        ffmpeg, arbeits_mkv, arbeits_mp4,
                        log_q, task_q, simulation, log_zeilen,
                        stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                        ffprobe_pfad=ffprobe, kein_faststart=True,
                        dts_zu_eac3=dts_zu_eac3
                    )
                    if remux_ok:
                        nachbearbeite_dv_mp4(arbeits_mp4, log_q, log_zeilen, simulation)

                erfolg = remux_ok
                if remux_ok and lokal_aktiv:
                    erfolg = kopiere_mit_fortschritt(
                        arbeits_mp4, mp4_pfad, t("worker.desc_move_to_nas"),
                        log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event)
                    if not erfolg:
                        log("ERR", t("worker.mp4_transfer_failed", pfad=arbeits_mp4))

                if erfolg:
                    stats["remuxed"] += 1
                    neu_remuxed = True
                    log("OK", t("worker.remux_success"))
                    task_q.put({"schritt": t("worker.step_remux_done"), "sub_prog": 100})
                    if not simulation:
                        undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
                else:
                    stats["fehler"] += 1
                    if mp4_pfad.exists():
                        mp4_pfad.unlink()
                    task_q.put({"schritt": t("worker.step_remux_failed"), "sub_prog": 0})
                    continue

            # 7. Untertitel als SRT extrahieren (vor dem Verschieben der MKV!)
            erstellte_srts = []
            if streams and (untertitel or embed_subs):
                srt_streams = ([s for s in streams if s["language"] != "eng"]
                               if embed_subs else streams)
                if srt_streams:
                    erstellte_srts = extrahiere_untertitel(
                        ffmpeg, arbeits_mkv, srt_streams, log_q, task_q, simulation, log_zeilen,
                        undo_log=undo_log if not simulation else None,
                        ziel_ordner=ordner)
                elif untertitel:
                    log("INFO", t("worker.no_foreign_subs"))
                    task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})
            elif untertitel:
                log("INFO", t("worker.no_subs_found"))
                task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})

            # 8. MKV verschieben / löschen (nach SRT-Extraktion!)
            if neu_remuxed:
                verschiebe_oder_loesche_mkv(
                    mkv_pfad, original_behalten, simulation, log,
                    undo_log=undo_log,
                    old_mkv_global_pfad=old_mkv_global_pfad,
                    aktueller_pfad=arbeits_mkv if lokal_aktiv else None)
                if lokal_aktiv and not simulation:
                    mkv_verschoben = arbeits_mkv.exists()

            # 9. NFO aktualisieren
            if nfo_update and nfo_pfad.exists():
                aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                                 simulation, log_zeilen, undo_log=undo_log)
            elif nfo_update:
                log("INFO", t("worker.no_nfo_update"))
        finally:
            if lokal_aktiv and not simulation:
                if arbeits_mkv.exists():
                    if mkv_verschoben:
                        # Original wurde bereits vom NAS wegverschoben, hat aber sein
                        # endgueltiges Ziel (Sicherungsordner/Loeschung) nicht erreicht
                        # -> zurueck zum NAS-Ursprungsort, um Datenverlust zu vermeiden.
                        try:
                            mkv_pfad.parent.mkdir(parents=True, exist_ok=True)
                            if mkv_pfad.exists():
                                log("ERR", t("worker.restore_target_occupied", pfad=arbeits_mkv))
                            else:
                                shutil.move(str(arbeits_mkv), str(mkv_pfad))
                                log("WARN", t("worker.restore_done", name=mkv_pfad.name))
                        except Exception as e:
                            log("ERR", t("worker.restore_failed", fehler=e, pfad=arbeits_mkv))
                    else:
                        arbeits_mkv.unlink(missing_ok=True)
                # Ausnahme: Remux lief lokal durch, aber der Rücktransfer zum NAS
                # ist fehlgeschlagen -> fertige Datei bleibt lokal zur manuellen Abholung.
                ruecktransfer_fehlgeschlagen = remux_ok and not erfolg
                if arbeits_mp4.exists() and not ruecktransfer_fehlgeschlagen:
                    arbeits_mp4.unlink(missing_ok=True)

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": t("worker.done"), "schritt": "", "sub_prog": 100})

    # 9. Zusammenfassung
    log("HEAD", f"\n{'='*55}")
    log("HEAD",   t("worker.summary_title", suffix=""))
    log("HEAD", f"{'='*55}")
    log("OK",   t("worker.summary_found", anzahl=stats['gefunden']))
    log("OK",   t("worker.summary_remuxed", anzahl=stats['remuxed']))
    log("SKIP", t("worker.summary_skipped", anzahl=stats['uebersprungen']))
    log("ERR",  t("worker.summary_errors", anzahl=stats['fehler']))
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", t("worker.summary_sim_note"))

    # 10. Log-Datei schreiben
    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", t("worker.summary_log_saved", name=log_pfad.name))
    log("HEAD", t("worker.summary_log_location", pfad=log_pfad.parent))

    done_q.put((stats, log_pfad))


def verarbeite_einzelordner(
        ffmpeg_pfad: str, ffprobe_pfad: str, ordner_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None,
        lokale_kopie: bool = False, lokale_kopie_pfad: Path = None,
        dts_zu_eac3: bool = False):
    """Einzelordner-Worker: verarbeitet genau einen Film-Ordner (direkt MKV darin)."""

    ffmpeg  = Path(ffmpeg_pfad)
    ffprobe = Path(ffprobe_pfad)
    ordner  = Path(ordner_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = t("logheader.sim") if simulation else t("logheader.run")
    log("HEAD", f"{'='*55}")
    log("HEAD", t("worker.header_title", version=VERSION, modus=modus_text, suffix="  [EINZELORDNER]"))
    log("HEAD", t("worker.header_start", datum=datetime.now().strftime('%d.%m.%Y %H:%M:%S')))
    log("HEAD", t("worker.einzel.header_folder", ordner=ordner))
    log("HEAD", f"{'='*55}")

    stats    = {"gefunden": 0, "remuxed": 0, "uebersprungen": 0, "fehler": 0}
    undo_log = []

    fort_q.put(0)
    task_q.put({"film": ordner.name, "schritt": t("worker.step_finding_mkv"), "sub_prog": None})
    log("FOLDER", t("worker.folder_movie", name=ordner.name))

    mkv_pfad = finde_mkv(ordner)
    if mkv_pfad is None:
        log("SKIP", t("worker.einzel.no_mkv"))
        fort_q.put(100)
        task_q.put({"film": ordner.name, "schritt": t("worker.einzel.step_no_mkv"), "sub_prog": 100})
        done_q.put((stats, None))
        return

    # HDR-Typ ermitteln
    task_q.put({"schritt": t("worker.step_hdr_detect"), "sub_prog": None})
    nfo_pfad = ordner / "movie.nfo"
    hdrtype = lese_hdrtype_aus_nfo(nfo_pfad) if nfo_pfad.exists() else None
    if hdrtype:
        key = "worker.hdr_type_nfo_sim" if simulation else "worker.hdr_type_nfo"
        log("SIM" if simulation else "INFO", t(key, hdrtype=hdrtype))
    else:
        hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
        anzeige_hdrtype = hdrtype or t("worker.hdr_not_detected")
        key = "worker.hdr_type_ffprobe_sim" if simulation else "worker.hdr_type_ffprobe"
        log("SIM" if simulation else "INFO", t(key, hdrtype=anzeige_hdrtype))

    if hdrtype != "dolbyvision":
        log("SKIP", t("worker.einzel.no_dv"))
        fort_q.put(100)
        task_q.put({"film": ordner.name, "schritt": t("worker.einzel.step_no_dv"), "sub_prog": 100})
        done_q.put((stats, None))
        return

    stats["gefunden"] += 1
    mp4_pfad = mkv_pfad.with_suffix(".mp4")

    # DV-Profil-Prüfung: Profil 5 (ICtCp) → P5→P8-Konvertierung oder Warnung
    braucht_p5_fix = False
    dv_profil, farb_matrix = ermittle_dv_profil(ffprobe, mkv_pfad)
    _ictcp = farb_matrix and any(kw in str(farb_matrix).lower()
                                    for kw in ("ictcp", "ipt-pq", "ipt_pq"))
    if dv_profil == 5 or _ictcp:
        braucht_p5_fix = True
        if DOVI_TOOL.exists():
            log("WARN", t("worker.p5_detected_converting", profil=dv_profil or "?"))
        else:
            log("WARN", t("worker.p5_detected_no_tool", profil=dv_profil or "?"))
            log("WARN", t("worker.p5_tool_hint"))

    # Untertitel-Streams ermitteln
    streams = []
    if embed_subs or untertitel:
        task_q.put({"schritt": t("worker.step_subs_analyze"), "sub_prog": None})
        log("INFO", t("worker.subs_analyzing"))
        if simulation:
            streams = simuliere_streams_aus_nfo(nfo_pfad) if nfo_pfad.exists() else []
            log("SIM", t("worker.subs_found_sim", anzahl=len(streams)))
        else:
            streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
        if streams:
            log("INFO", t("worker.subs_found", anzahl=len(streams)))

    text_sub_indices = None
    if embed_subs and streams:
        eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
        bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
        if eng_subs:
            text_sub_indices = [s["index"] for s in eng_subs]
            log("INFO", t("worker.subs_embed_count", anzahl=len(eng_subs)))
        for s in bitmap_subs:
            log("SKIP", t("worker.subs_bitmap_skip", index=s['index'],
                           sprache=s['language'], codec=s['codec'].upper()))

    fort_q.put(10)

    # Remux (normaler Pfad) oder P5→P8-Konvertierung, SRT-Extraktion,
    # MKV-Verschieben und NFO-Update – alles in einem try/finally, damit eine
    # evtl. lokale Arbeitskopie in jedem Fall aufgeräumt wird (auch bei return).
    neu_remuxed = False
    arbeits_mkv, arbeits_mp4 = mkv_pfad, mp4_pfad
    lokal_aktiv = False
    mkv_verschoben = False
    remux_ok = False
    erfolg = False
    try:
        if mp4_pfad.exists():
            log("SKIP", t("worker.mp4_exists_skip"))
            stats["uebersprungen"] += 1
            task_q.put({"schritt": t("worker.step_mp4_exists"), "sub_prog": 100})
        else:
            # Optional: MKV in lokalen Arbeitsordner verschieben (NAS-Entlastung;
            # verhindert außerdem dass Jellyfin während der Verarbeitung MKV+MP4
            # als Dubletten im selben NAS-Ordner sieht)
            if lokale_kopie:
                if not simulation and not genug_speicherplatz(lokale_kopie_pfad, mkv_pfad.stat().st_size):
                    log("WARN", t("worker.einzel.no_space_abort"))
                    stats["uebersprungen"] += 1
                    fort_q.put(100)
                    task_q.put({"schritt": t("worker.step_skipped_space"), "sub_prog": 100})
                    done_q.put((stats, schreibe_log_datei(log_zeilen, simulation)))
                    return
                arbeits_mkv = lokale_kopie_pfad / mkv_pfad.name
                arbeits_mp4 = lokale_kopie_pfad / mp4_pfad.name
                lokal_aktiv = True
                if not verschiebe_sicher(
                        mkv_pfad, arbeits_mkv, t("worker.desc_move_to_workdir"),
                        log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event):
                    stats["fehler"] += 1
                    fort_q.put(100)
                    task_q.put({"schritt": t("worker.step_move_failed"), "sub_prog": 0})
                    done_q.put((stats, schreibe_log_datei(log_zeilen, simulation)))
                    return
                mkv_verschoben = True

            task_q.put({"schritt": t("worker.step_remux_running"), "sub_prog": None})
            if braucht_p5_fix and DOVI_TOOL.exists():
                task_q.put({"schritt": t("worker.step_p5p8_running"), "sub_prog": None})
                remux_ok = konvertiere_dv_p5_zu_p8(
                    ffmpeg, DOVI_TOOL, ffprobe,
                    arbeits_mkv, arbeits_mp4,
                    log_q, task_q, simulation, log_zeilen,
                    stopp_event=stopp_event)
            else:
                remux_ok = remux_zu_mp4(
                    ffmpeg, arbeits_mkv, arbeits_mp4,
                    log_q, task_q, simulation, log_zeilen,
                    stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                    ffprobe_pfad=ffprobe, kein_faststart=True,
                    dts_zu_eac3=dts_zu_eac3)
                if remux_ok:
                    nachbearbeite_dv_mp4(arbeits_mp4, log_q, log_zeilen, simulation)

            erfolg = remux_ok
            if remux_ok and lokal_aktiv:
                erfolg = kopiere_mit_fortschritt(
                    arbeits_mp4, mp4_pfad, t("worker.desc_move_to_nas"),
                    log_q, task_q, simulation, log_zeilen, stopp_event=stopp_event)
                if not erfolg:
                    log("ERR", t("worker.mp4_transfer_failed", pfad=arbeits_mp4))

            if erfolg:
                stats["remuxed"] += 1
                neu_remuxed = True
                log("OK", t("worker.remux_success"))
                task_q.put({"schritt": t("worker.step_remux_done"), "sub_prog": 100})
                if not simulation:
                    undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
            else:
                stats["fehler"] += 1
                if mp4_pfad.exists():
                    mp4_pfad.unlink()
                task_q.put({"schritt": t("worker.step_remux_failed"), "sub_prog": 0})
                fort_q.put(100)
                done_q.put((stats, schreibe_log_datei(log_zeilen, simulation)))
                return

        fort_q.put(60)

        # SRT extrahieren (vor dem Verschieben der MKV!)
        erstellte_srts = []
        if streams and (untertitel or embed_subs):
            srt_streams = ([s for s in streams if s["language"] != "eng"]
                           if embed_subs else streams)
            if srt_streams:
                erstellte_srts = extrahiere_untertitel(
                    ffmpeg, arbeits_mkv, srt_streams, log_q, task_q, simulation, log_zeilen,
                    undo_log=undo_log if not simulation else None,
                    ziel_ordner=ordner)
            elif untertitel:
                log("INFO", t("worker.no_foreign_subs"))
                task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})
        elif untertitel:
            log("INFO", t("worker.no_subs_found"))
            task_q.put({"schritt": t("worker.step_no_subs"), "sub_prog": 100})

        fort_q.put(80)

        # MKV verschieben / löschen
        if neu_remuxed:
            verschiebe_oder_loesche_mkv(
                mkv_pfad, original_behalten, simulation, log, undo_log=undo_log,
                old_mkv_global_pfad=old_mkv_global_pfad,
                aktueller_pfad=arbeits_mkv if lokal_aktiv else None)
            if lokal_aktiv and not simulation:
                mkv_verschoben = arbeits_mkv.exists()

        # NFO aktualisieren
        if nfo_update and nfo_pfad.exists():
            aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                             simulation, log_zeilen, undo_log=undo_log)
        elif nfo_update:
            log("INFO", t("worker.no_nfo_update"))
    finally:
        if lokal_aktiv and not simulation:
            if arbeits_mkv.exists():
                if mkv_verschoben:
                    try:
                        mkv_pfad.parent.mkdir(parents=True, exist_ok=True)
                        if mkv_pfad.exists():
                            log("ERR", t("worker.restore_target_occupied", pfad=arbeits_mkv))
                        else:
                            shutil.move(str(arbeits_mkv), str(mkv_pfad))
                            log("WARN", t("worker.restore_done", name=mkv_pfad.name))
                    except Exception as e:
                        log("ERR", t("worker.restore_failed", fehler=e, pfad=arbeits_mkv))
                else:
                    arbeits_mkv.unlink(missing_ok=True)
            ruecktransfer_fehlgeschlagen = remux_ok and not erfolg
            if arbeits_mp4.exists() and not ruecktransfer_fehlgeschlagen:
                arbeits_mp4.unlink(missing_ok=True)

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": t("worker.done"), "schritt": "", "sub_prog": 100})

    log("HEAD", f"\n{'='*55}")
    log("HEAD",   t("worker.summary_title", suffix="  [EINZELORDNER]"))
    log("HEAD", f"{'='*55}")
    log("OK",   t("worker.summary_found", anzahl=stats['gefunden']))
    log("OK",   t("worker.summary_remuxed", anzahl=stats['remuxed']))
    log("SKIP", t("worker.summary_skipped", anzahl=stats['uebersprungen']))
    log("ERR",  t("worker.summary_errors", anzahl=stats['fehler']))
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", t("worker.summary_sim_note"))

    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", t("worker.summary_log_saved", name=log_pfad.name))
    log("HEAD", t("worker.summary_log_location", pfad=log_pfad.parent))
    done_q.put((stats, log_pfad))
