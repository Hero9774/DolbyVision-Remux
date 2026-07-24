"""Kernpipeline: P5→P8-Konvertierung, Untertitel-Extraktion, NFO-Update,
normaler Remux, Log-Datei-Schreiben und Session-Rollback."""

import json
import queue
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from dv_remux.konstanten import LOG_ORDNER, TEXT_CODECS, VERSION
from dv_remux.sprache import t, _bereinige_log
from dv_remux.mkv_analyse import ermittle_audio_streams
from dv_remux.mp4_binary import _berechne_dv_level, injiziere_dvcc_box, mache_faststart_und_ftyp


def konvertiere_dv_p5_zu_p8(
        ffmpeg: Path, dovi_tool: Path, ffprobe: Path,
        mkv_pfad: Path, mp4_pfad: Path,
        log_q: queue.Queue, task_q: queue.Queue,
        simulation: bool, log_zeilen: list,
        stopp_event=None) -> bool:
    """
    DV Profil 5 (ICtCp) → Profil 8.1 (HDR10-kompatibel) ohne Re-Encoding:
      Schritt 1: HEVC-Stream extrahieren (ffmpeg -c:v copy)
      Schritt 2: RPU konvertieren P5→P8.1 + CMv4.0-Metadaten
                 (dovi_tool --edit-config add_cmv4_default_metadata=true -m 3 convert)
      Schritt 3: MP4 zusammensetzen (P8-HEVC + Audio aus Original-MKV, kein faststart)
      Schritt 4: dvcC-Box injizieren (Dolby Vision Configuration Record)
      Schritt 5: faststart – moov vor mdat schieben, major_brand mp42
    Temp-Dateien werden in jedem Fall bereinigt (auch bei Fehler).
    """
    if simulation:
        text = t("p5p8.sim", mkv=mkv_pfad.name, mp4=mp4_pfad.name)
        log_q.put(("SIM", text))
        log_zeilen.append(_bereinige_log(text))
        for p in range(0, 101, 5):
            task_q.put({"sub_prog": p})
            time.sleep(0.04)
        return True

    tmp_dir        = Path(tempfile.gettempdir())
    tmp_hevc       = tmp_dir / f"_dv_remux_{mp4_pfad.stem}.hevc"
    tmp_hevc_p8    = tmp_dir / f"_dv_remux_{mp4_pfad.stem}_p8.hevc"
    tmp_editor_cfg = tmp_dir / f"_dv_remux_{mp4_pfad.stem}_editor.json"

    def cleanup():
        for tmp in (tmp_hevc, tmp_hevc_p8, tmp_editor_cfg):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    proc = None
    try:
        # ── Schritt 1: HEVC extrahieren ────────────────────────────────────
        task_q.put({"schritt": t("p5p8.taskstep1"), "sub_prog": 0})
        text = t("p5p8.step1_start")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))

        befehl1 = [str(ffmpeg), "-i", str(mkv_pfad),
                   "-c:v", "copy", "-an", "-sn", "-y", str(tmp_hevc)]
        proc = subprocess.Popen(befehl1, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        dauer_sek = None
        for zeile in proc.stderr:
            if stopp_event and stopp_event.is_set():
                proc.terminate(); proc.wait(); cleanup()
                text = t("p5p8.abgebrochen")
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))
                return False
            zeile = zeile.rstrip()
            if "Duration:" in zeile and dauer_sek is None:
                try:
                    teil = zeile.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = teil.split(":")
                    dauer_sek = int(h)*3600 + int(m)*60 + float(s)
                except Exception:
                    pass
            if zeile.startswith("frame=") and "time=" in zeile:
                try:
                    zeit_str = zeile.split("time=")[1].split()[0]
                    h, m, s = zeit_str.split(":")
                    vergangen = int(h)*3600 + int(m)*60 + float(s)
                    if dauer_sek and dauer_sek > 0:
                        task_q.put({"sub_prog": min(int(vergangen / dauer_sek * 33), 32)})
                except Exception:
                    pass
        proc.wait()
        if proc.returncode != 0:
            text = t("p5p8.step1_failed")
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            cleanup(); return False
        task_q.put({"sub_prog": 25})
        text = t("p5p8.step1_ok")
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

        # ── Schritt 2: RPU P5 → P8.1 konvertieren + CMv4.0-Metadaten ────────
        task_q.put({"schritt": t("p5p8.taskstep2"), "sub_prog": 25})
        text = t("p5p8.step2_start")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))

        # CMv4.0-Editor-Config schreiben (dovi_tool ≥ 2.3.3); ältere Versionen
        # ignorieren unbekannte JSON-Keys ohne Fehler.
        cmv4_aktiv = False
        try:
            tmp_editor_cfg.write_text('{"add_cmv4_default_metadata": true}',
                                      encoding="utf-8")
            befehl2 = [str(dovi_tool),
                       "--edit-config", str(tmp_editor_cfg),
                       "-m", "3", "convert",
                       str(tmp_hevc), "-o", str(tmp_hevc_p8)]
            cmv4_aktiv = True
        except Exception:
            befehl2 = [str(dovi_tool), "-m", "3", "convert",
                       str(tmp_hevc), "-o", str(tmp_hevc_p8)]

        erg2 = subprocess.run(befehl2, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        if erg2.returncode != 0 and cmv4_aktiv:
            # Fallback: ohne --edit-config (sehr alte dovi_tool-Version)
            text = t("p5p8.cmv4_fallback")
            log_q.put(("WARN", text))
            log_zeilen.append(_bereinige_log(text))
            befehl2 = [str(dovi_tool), "-m", "3", "convert",
                       str(tmp_hevc), "-o", str(tmp_hevc_p8)]
            erg2 = subprocess.run(befehl2, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
            cmv4_aktiv = False

        if erg2.returncode != 0:
            fehler = (erg2.stderr or erg2.stdout or "").strip()[-300:]
            text = t("p5p8.dovi_tool_error", code=erg2.returncode, fehler=fehler)
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            cleanup(); return False

        task_q.put({"sub_prog": 50})
        cmv4_suffix = t("p5p8.cmv4_suffix") if cmv4_aktiv else ""
        text = t("p5p8.step2_ok", cmv4_suffix=cmv4_suffix)
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

        # DV-Level für dvcC-Box aus Streaminfo bestimmen
        dv_level = 6
        try:
            s_info = json.loads(subprocess.run(
                [str(ffprobe), "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0", str(mkv_pfad)],
                capture_output=True, text=True, timeout=30).stdout
            ).get("streams", [{}])[0]
            breite  = int(s_info.get("width",  3840))
            hoehe   = int(s_info.get("height", 2160))
            fps_raw = s_info.get("r_frame_rate", "24/1")
            fps_n, fps_d = (int(x) for x in fps_raw.split("/")) if "/" in fps_raw else (24, 1)
            dv_level = _berechne_dv_level(breite, hoehe, fps_n / max(fps_d, 1))
        except Exception:
            pass

        # ── Schritt 3: MP4 zusammensetzen (ohne faststart) ────────────────
        task_q.put({"schritt": t("p5p8.taskstep3"), "sub_prog": 50})
        text = t("p5p8.step3_start", mp4=mp4_pfad.name)
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))

        # Audio: alle kompatiblen Spuren aus Original-MKV (TrueHD ausschließen)
        alle_audio = ermittle_audio_streams(ffprobe, mkv_pfad)
        kompatible = [s["index"] for s in alle_audio if s["codec"] != "truehd"]
        audio_indizes = kompatible if kompatible else [s["index"] for s in alle_audio]
        if len(kompatible) < len(alle_audio):
            ausgelassen = len(alle_audio) - len(kompatible)
            text = t("p5p8.truehd_excluded", anzahl=ausgelassen)
            log_q.put(("WARN", text))
            log_zeilen.append(_bereinige_log(text))

        audio_maps = []
        for idx in audio_indizes:
            audio_maps += ["-map", f"1:{idx}"]

        # Kein -movflags +faststart: moov landet am Ende → dvcC-Injektion
        # (Schritt 4) kann Box-Offsets unverändert lassen
        befehl3 = ([str(ffmpeg),
                    "-i", str(tmp_hevc_p8),
                    "-i", str(mkv_pfad)]
                   + ["-map", "0:v:0"] + audio_maps
                   + ["-c", "copy", "-strict", "unofficial",
                      "-tag:v", "dvh1",
                      "-y", str(mp4_pfad)])

        proc = subprocess.Popen(befehl3, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        dauer_sek = None
        stderr_z3 = []
        for zeile in proc.stderr:
            if stopp_event and stopp_event.is_set():
                proc.terminate(); proc.wait(); cleanup()
                text = t("p5p8.abgebrochen")
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))
                return False
            zeile = zeile.rstrip()
            stderr_z3.append(zeile)
            if "Duration:" in zeile and dauer_sek is None:
                try:
                    teil = zeile.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = teil.split(":")
                    dauer_sek = int(h)*3600 + int(m)*60 + float(s)
                except Exception:
                    pass
            if zeile.startswith("frame=") and "time=" in zeile:
                try:
                    zeit_str = zeile.split("time=")[1].split()[0]
                    h, m, s = zeit_str.split(":")
                    vergangen = int(h)*3600 + int(m)*60 + float(s)
                    if dauer_sek and dauer_sek > 0:
                        pct = 50 + min(int(vergangen / dauer_sek * 45), 44)
                        task_q.put({"sub_prog": pct})
                except Exception:
                    pass
                log_q.put(("PROG", f"     {zeile}"))
        proc.wait()
        if proc.returncode != 0:
            for z in stderr_z3[-15:]:
                if z.strip():
                    log_q.put(("ERR", f"     {z}"))
                    log_zeilen.append(f"     {z}")
            text = t("p5p8.step3_failed")
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            return False

        # ── Schritt 4: dvcC-Box injizieren ────────────────────────────────
        task_q.put({"schritt": t("p5p8.taskstep4"), "sub_prog": 93})
        text = t("p5p8.step4_start", level=dv_level)
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        if not injiziere_dvcc_box(mp4_pfad, dv_profil=8, dv_level=dv_level, compat_id=1):
            text = t("p5p8.step4_failed")
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            return False
        text = t("p5p8.step4_ok")
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

        # ── Schritt 5: faststart – moov vor mdat schieben ─────────────────
        task_q.put({"schritt": t("p5p8.taskstep5"), "sub_prog": 96})
        text = t("p5p8.step5_start")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        if mache_faststart_und_ftyp(mp4_pfad):
            task_q.put({"sub_prog": 100})
            cmv4_info = t("p5p8.cmv4_info_suffix") if cmv4_aktiv else ""
            text = t("p5p8.step5_ok", cmv4_info=cmv4_info)
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))
            return True
        else:
            # faststart-Fehler ist nicht fatal — dvcC ist bereits gesetzt
            text = t("p5p8.step5_warn")
            log_q.put(("WARN", text))
            log_zeilen.append(_bereinige_log(text))
            return True

    except Exception as e:
        text = t("p5p8.exception", fehler=e)
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))
        return False
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate(); proc.wait()
        cleanup()


def extrahiere_untertitel(ffmpeg: Path, mkv_pfad: Path, streams: list,
                           log_q: queue.Queue, task_q: queue.Queue,
                           simulation: bool, log_zeilen: list,
                           undo_log: list = None,
                           ziel_ordner: Path = None) -> list:
    """
    Text-Untertitelspuren als .srt extrahieren.
    Gibt Liste der erfolgreich erstellten/vorhandenen SRT-Pfade zurück
    (wird von aktualisiere_nfo() benötigt).
    ziel_ordner: Ordner in den die SRTs geschrieben werden (Default: mkv_pfad.parent,
    relevant wenn mkv_pfad eine lokale Arbeitskopie ist und die SRTs trotzdem
    direkt im NAS-Zielordner landen sollen).
    """
    basis = mkv_pfad.with_suffix("")
    ziel_ordner = ziel_ordner or basis.parent
    sprachzähler = {}
    text_streams   = [s for s in streams if s["codec"] in TEXT_CODECS]
    bitmap_streams = [s for s in streams if s["codec"] not in TEXT_CODECS]
    erstellte_srts = []   # ← Rückgabe-Liste

    for s in bitmap_streams:
        text = t("srt.bitmap_skip", index=s['index'], sprache=s['language'], codec=s['codec'].upper())
        log_q.put(("SKIP", text))
        log_zeilen.append(_bereinige_log(text))

    total = len(text_streams)
    for i, stream in enumerate(text_streams):
        sprache = stream["language"]
        idx     = stream["index"]
        sprachzähler[sprache] = sprachzähler.get(sprache, 0) + 1
        if sprachzähler[sprache] > 1:
            text = t("srt.duplicate_skip", sprache=sprache, idx=idx)
            log_q.put(("SKIP", text))
            log_zeilen.append(_bereinige_log(text))
            continue
        srt_pfad = ziel_ordner / (basis.name + f".{sprache}.srt")

        task_q.put({
            "schritt":  f"SRT: {srt_pfad.name}",
            "sub_prog": int(i / total * 100) if total else 0
        })

        if srt_pfad.exists():
            text = t("srt.already_exists", name=srt_pfad.name)
            log_q.put(("INFO", text))
            log_zeilen.append(_bereinige_log(text))
            erstellte_srts.append(srt_pfad)
            continue

        if simulation:
            text = t("srt.sim", name=srt_pfad.name)
            log_q.put(("SIM", text))
            log_zeilen.append(_bereinige_log(text))
            erstellte_srts.append(srt_pfad)  # auch im Sim merken für NFO-Update
            continue

        text = t("srt.running", name=srt_pfad.name)
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        befehl = [
            str(ffmpeg), "-i", str(mkv_pfad),
            "-map", f"0:{idx}", "-c:s", "srt", "-y", str(srt_pfad)
        ]
        try:
            subprocess.run(befehl, capture_output=True, check=True)
            text = t("srt.ok", name=srt_pfad.name)
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))
            erstellte_srts.append(srt_pfad)
            if undo_log is not None:
                undo_log.append({"typ": "srt", "pfad": srt_pfad})
        except subprocess.CalledProcessError:
            text = t("srt.failed", name=srt_pfad.name)
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))

    task_q.put({"sub_prog": 100})
    return erstellte_srts


# ─────────────────────────────────────────────────────────────────────────────
# NFO-AKTUALISIERUNG
# ─────────────────────────────────────────────────────────────────────────────

def aktualisiere_nfo(
        nfo_pfad: Path,
        mp4_pfad: Path,
        srt_dateien: list,          # Liste der Path-Objekte zu .srt-Dateien
        log_q: queue.Queue,
        task_q: queue.Queue,
        simulation: bool,
        log_zeilen: list,
        undo_log: list = None):
    """
    Aktualisiert die movie.nfo für Jellyfin nach dem Remux.

    Änderungen:
      1. Backup: movie.nfo  →  movie.nfo.bak
         (vorhandenes Backup wird NICHT überschrieben – Sicherheit geht vor)
      2. <original_filename>  .mkv → .mp4
      3. <fileinfo><streamdetails>: alle <subtitle>-Einträge werden durch
         die tatsächlich vorhandenen SRT-Dateien ersetzt.
         Die Sprachcodes werden aus dem Dateinamen extrahiert:
           Film.deu.srt      → <language>deu</language>
           Film.deu.2.srt    → <language>deu</language>
         Alle anderen Stream-Details (Video, Audio) bleiben unverändert.

    tinyMediaManager-Kommentar und XML-Deklaration bleiben erhalten,
    da wir die Datei als Text lesen und gezielt Bereiche ersetzen.
    """
    task_q.put({"schritt": t("nfo.taskstep_updating"), "sub_prog": None})
    text = t("nfo.updating")
    log_q.put(("INFO", text))

    # ── Backup erstellen ──────────────────────────────────────────────────
    bak_pfad = nfo_pfad.with_suffix(".nfo.bak")
    if not bak_pfad.exists():
        if simulation:
            text = t("nfo.backup_sim", name=bak_pfad.name)
            log_q.put(("SIM", text))
            log_zeilen.append(_bereinige_log(text))
        else:
            try:
                shutil.copy2(nfo_pfad, bak_pfad)
                text = t("nfo.backup_ok", name=bak_pfad.name)
                log_q.put(("OK", text))
                log_zeilen.append(_bereinige_log(text))
            except Exception as e:
                text = t("nfo.backup_failed", fehler=e)
                log_q.put(("ERR", text))
                log_zeilen.append(_bereinige_log(text))
                task_q.put({"schritt": t("nfo.taskstep_aborted"), "sub_prog": 100})
                return
    else:
        text = t("nfo.backup_exists", name=bak_pfad.name)
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))

    # ── XML parsen ────────────────────────────────────────────────────────
    try:
        # Rohtext aufbewahren um Kommentare später wieder einzufügen
        rohtext = nfo_pfad.read_text(encoding="utf-8")

        # ET-Parser ohne Namespace-Probleme
        baum = ET.parse(nfo_pfad)
        wurzel = baum.getroot()
    except Exception as e:
        text = t("nfo.parse_failed", fehler=e)
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))
        return

    aenderungen = []

    # ── 1. original_filename aktualisieren ────────────────────────────────
    el_fn = wurzel.find("original_filename")
    if el_fn is not None and el_fn.text:
        alt = el_fn.text.strip()
        neu = mp4_pfad.name
        if alt != neu:
            el_fn.text = neu
            aenderungen.append(t("nfo.filename_change_summary", alt=alt, neu=neu))
            text = t("nfo.filename_change_ok", alt=alt, neu=neu)
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))

    # ── 2. Subtitle-Einträge in streamdetails ersetzen ────────────────────
    streamdetails = wurzel.find("./fileinfo/streamdetails")
    if streamdetails is not None and srt_dateien:

        # Alle alten <subtitle>-Elemente entfernen
        alte_subs = streamdetails.findall("subtitle")
        anzahl_alt = len(alte_subs)
        for sub in alte_subs:
            streamdetails.remove(sub)

        # Neue <subtitle>-Einträge aus SRT-Dateinamen erzeugen
        # Dateiname-Muster: FilmName.LANG.srt oder FilmName.LANG.N.srt
        # Sprachcode ist der vorletzte Punkt-Abschnitt vor .srt
        neue_subs_eingefuegt = 0
        for srt_pfad in sorted(srt_dateien):
            # Sprache aus Dateinamen extrahieren
            # z.B. "War Machine 2026.deu.srt" → "deu"
            #      "War Machine 2026.deu.2.srt" → "deu"
            teile = srt_pfad.stem.split(".")   # stem = ohne .srt
            sprache = "und"
            if len(teile) >= 2:
                # letzter Teil könnte eine Zahl sein (deu.2) → dann vorletzter
                kandidat = teile[-1]
                if kandidat.isdigit() and len(teile) >= 3:
                    kandidat = teile[-2]
                # Sprachcodes sind 2-4 Buchstaben
                if 2 <= len(kandidat) <= 4 and kandidat.isalpha():
                    sprache = kandidat

            sub_el = ET.SubElement(streamdetails, "subtitle")
            lang_el = ET.SubElement(sub_el, "language")
            lang_el.text = sprache
            neue_subs_eingefuegt += 1

        aenderungen.append(
            t("nfo.subtitle_change_summary", alt=anzahl_alt, neu=neue_subs_eingefuegt))
        text = t("nfo.subtitle_change_ok", alt=anzahl_alt, neu=neue_subs_eingefuegt)
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

    elif streamdetails is not None and not srt_dateien:
        log_q.put(("INFO", t("nfo.subtitle_unchanged")))

    # ── Änderungen zusammenfassen ─────────────────────────────────────────
    if not aenderungen:
        text = t("nfo.already_current")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        task_q.put({"schritt": t("nfo.taskstep_current"), "sub_prog": 100})
        return

    # ── XML zurückschreiben ───────────────────────────────────────────────
    if simulation:
        for a in aenderungen:
            text = t("nfo.change_sim", aenderung=a)
            log_q.put(("SIM", text))
            log_zeilen.append(_bereinige_log(text))
        task_q.put({"schritt": t("nfo.taskstep_sim"), "sub_prog": 100})
        return

    try:
        # XML-Deklaration und tinyMediaManager-Kommentar manuell vorhalten,
        # da ET sie beim Schreiben nicht automatisch beibehält.
        deklaration   = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        tmm_kommentar = ""
        for zeile in rohtext.splitlines():
            if zeile.strip().startswith("<!--"):
                tmm_kommentar = zeile + "\n"
                break

        # ET-Baum als String (ohne eigene XML-Deklaration)
        ET.indent(baum, space="  ")   # Einrückung (Python 3.9+)
        xml_inhalt = ET.tostring(
            wurzel,
            encoding="unicode",
            xml_declaration=False
        )

        # Alles zusammensetzen
        gesamt = deklaration + tmm_kommentar + xml_inhalt + "\n"
        nfo_pfad.write_text(gesamt, encoding="utf-8")
        if undo_log is not None:
            undo_log.append({"typ": "nfo", "nfo": nfo_pfad, "bak": bak_pfad})

        text = t("nfo.saved", name=nfo_pfad.name)
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

    except AttributeError:
        # ET.indent nicht verfügbar (Python < 3.9) → ohne Einrückung
        xml_inhalt = ET.tostring(wurzel, encoding="unicode", xml_declaration=False)
        gesamt = deklaration + tmm_kommentar + xml_inhalt + "\n"
        nfo_pfad.write_text(gesamt, encoding="utf-8")
        if undo_log is not None:
            undo_log.append({"typ": "nfo", "nfo": nfo_pfad, "bak": bak_pfad})
        text = t("nfo.saved_no_indent", name=nfo_pfad.name)
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))

    except Exception as e:
        text = t("nfo.write_failed", fehler=e)
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))

    task_q.put({"schritt": t("nfo.taskstep_done"), "sub_prog": 100})


def remux_zu_mp4(ffmpeg: Path, mkv_pfad: Path, mp4_pfad: Path,
                 log_q: queue.Queue, task_q: queue.Queue,
                 simulation: bool, log_zeilen: list,
                 stopp_event=None, text_sub_indices=None,
                 ffprobe_pfad: Path = None,
                 audio_indices: list = None,
                 kein_faststart: bool = False) -> bool:
    """Remux MKV -> MP4 ohne Re-Encoding.
    text_sub_indices: Stream-Indizes für Text-Untertitel → mov_text einbetten.
    audio_indices: Explizite Audio-Stream-Indizes (None = alle via -map 0:a).
    ffprobe_pfad: Wird für TrueHD-Retry benötigt.
    kein_faststart: True = kein -movflags +faststart (für nachgelagerte dvcC-Injektion).
    """
    if simulation:
        embed_info = t("remux.sim_embed_info", anzahl=len(text_sub_indices)) if text_sub_indices else ""
        text = t("remux.sim", mkv=mkv_pfad.name, mp4=mp4_pfad.name, embed_info=embed_info)
        log_q.put(("SIM", text))
        log_zeilen.append(_bereinige_log(text))
        for p in range(0, 101, 5):
            task_q.put({"sub_prog": p})
            time.sleep(0.04)
        return True

    # Audio-Maps aufbauen: explizite Indizes (TrueHD-Retry) oder alle Streams
    if audio_indices is not None:
        audio_maps = []
        for idx in audio_indices:
            audio_maps += ["-map", f"0:{idx}"]
    else:
        audio_maps = ["-map", "0:a"]

    # Kommando aufbauen – 0:v:0 = nur Haupt-Videostream (kein MJPEG-Cover)
    fs_flags = [] if kein_faststart else ["-movflags", "+faststart"]
    if text_sub_indices:
        # Video + Audio + ausgewählte Text-Untertitel (→ mov_text)
        befehl = [str(ffmpeg), "-i", str(mkv_pfad), "-map", "0:v:0"] + audio_maps
        for idx in text_sub_indices:
            befehl += ["-map", f"0:{idx}"]
        befehl += (["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                    "-strict", "unofficial", "-tag:v", "hvc1"]
                   + fs_flags + ["-y", str(mp4_pfad)])
    else:
        # Nur Haupt-Video + Audio mappen
        befehl = ([str(ffmpeg), "-i", str(mkv_pfad), "-map", "0:v:0"]
                  + audio_maps
                  + ["-c", "copy", "-strict", "unofficial", "-tag:v", "hvc1"]
                  + fs_flags + ["-y", str(mp4_pfad)])

    text = t("remux.running", mkv=mkv_pfad.name, mp4=mp4_pfad.name)
    log_q.put(("INFO", text))
    log_zeilen.append(_bereinige_log(text))

    proc = None
    try:
        proc = subprocess.Popen(
            befehl, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace"
        )
        dauer_sek = None
        stderr_zeilen = []   # Alle stderr-Zeilen sammeln für Fehlerdiagnose
        for zeile in proc.stderr:
            # Stop-Anfrage: ffmpeg-Prozess beenden
            if stopp_event and stopp_event.is_set():
                proc.terminate()
                proc.wait()
                text = t("remux.abgebrochen")
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))
                return False
            zeile = zeile.rstrip()
            stderr_zeilen.append(zeile)
            if "Duration:" in zeile and dauer_sek is None:
                try:
                    teil = zeile.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = teil.split(":")
                    dauer_sek = int(h)*3600 + int(m)*60 + float(s)
                except Exception:
                    pass
            if zeile.startswith("frame=") and "time=" in zeile:
                try:
                    zeit_str = zeile.split("time=")[1].split()[0]
                    h, m, s = zeit_str.split(":")
                    vergangen = int(h)*3600 + int(m)*60 + float(s)
                    if dauer_sek and dauer_sek > 0:
                        pct = min(int(vergangen / dauer_sek * 100), 99)
                        task_q.put({"sub_prog": pct})
                except Exception:
                    pass
                log_q.put(("PROG", f"     {zeile}"))
            elif "Error" in zeile or "error" in zeile:
                log_q.put(("ERR", f"     {zeile}"))
                log_zeilen.append(f"     {zeile}")
        proc.wait()
        if proc.returncode == 0:
            task_q.put({"sub_prog": 100})
            return True
        else:
            # Letzte stderr-Zeilen ausgeben für Fehlerdiagnose
            text = t("remux.stderr_header")
            log_q.put(("ERR", text))
            for z in stderr_zeilen[-15:]:
                if z.strip():
                    log_q.put(("ERR", f"     {z}"))
                    log_zeilen.append(f"     {z}")

            if text_sub_indices:
                # Sub-Einbettung fehlgeschlagen → Wiederholung ohne Untertitel
                text = t("remux.sub_embed_failed")
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))
                if mp4_pfad.exists():
                    mp4_pfad.unlink()
                return remux_zu_mp4(ffmpeg, mkv_pfad, mp4_pfad, log_q, task_q,
                                    simulation, log_zeilen,
                                    stopp_event=stopp_event,
                                    text_sub_indices=None,
                                    ffprobe_pfad=ffprobe_pfad,
                                    audio_indices=audio_indices,
                                    kein_faststart=kein_faststart)

            # TrueHD ist im MP4-Container experimentell und von LG TV / Jellyfin
            # nicht unterstützt. Bei entsprechendem ffmpeg-Fehler: Audio-Streams
            # per ffprobe ermitteln, TrueHD-Tracks herausfiltern und neu versuchen.
            truehd_fehler = any("truehd" in z.lower() for z in stderr_zeilen)
            if truehd_fehler and ffprobe_pfad and audio_indices is None:
                alle_audio = ermittle_audio_streams(ffprobe_pfad, mkv_pfad)
                kompatibel = [s["index"] for s in alle_audio if s["codec"] != "truehd"]
                if kompatibel:
                    text = t("remux.truehd_failed", anzahl=len(alle_audio) - len(kompatibel))
                    log_q.put(("WARN", text))
                    log_zeilen.append(_bereinige_log(text))
                    if mp4_pfad.exists():
                        mp4_pfad.unlink()
                    return remux_zu_mp4(ffmpeg, mkv_pfad, mp4_pfad, log_q, task_q,
                                        simulation, log_zeilen,
                                        stopp_event=stopp_event,
                                        text_sub_indices=text_sub_indices,
                                        ffprobe_pfad=ffprobe_pfad,
                                        audio_indices=kompatibel,
                                        kein_faststart=kein_faststart)

            # Return-Code: unsigned → signed für lesbare Anzeige (Windows)
            rc = proc.returncode
            if rc > 0x7FFFFFFF:
                rc = rc - 0x100000000
            text = t("remux.ffmpeg_error", code=rc)
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            return False
    except FileNotFoundError:
        text = t("remux.not_found")
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))
        return False
    except Exception as e:
        text = t("remux.unexpected_error", fehler=e)
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))
        return False
    finally:
        # Prozess sicherstellen – falls Exception den Cleanup übersprungen hat
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait()

def schreibe_log_datei(log_zeilen: list, simulation: bool) -> Path:
    """Log-Datei im logs/-Ordner speichern."""
    try:
        LOG_ORDNER.mkdir(exist_ok=True)
        ts    = datetime.now()
        modus = "SIM" if simulation else "RUN"
        pfad  = LOG_ORDNER / f"dv_remux_{modus}_{ts.strftime('%Y%m%d_%H%M%S')}.log"
        modus_text = t("logheader.sim") if simulation else t("logheader.run")
        kopf  = [
            f"DV Remux Tool v{VERSION}",
            t("logheader.date_label", datum=ts.strftime('%d.%m.%Y %H:%M:%S')),
            t("logheader.mode_label", modus=modus_text),
            "=" * 55, ""
        ]
        pfad.write_text("\n".join(kopf + log_zeilen), encoding="utf-8")
        return pfad
    except Exception as e:
        # Fallback: Log-Pfad trotzdem zurückgeben, damit GUI nicht crasht
        fallback = LOG_ORDNER / "dv_remux_error.log"
        try:
            fallback.write_text(t("logheader.save_failed", fehler=e), encoding="utf-8")
        except Exception:
            pass
        return fallback


def rollback_session(undo_log: list, log_func, task_q: queue.Queue):
    """Alle protokollierten Operationen der Session rückgängig machen (LIFO)."""
    if not undo_log:
        log_func("INFO", t("rollback.nothing"))
        return

    log_func("WARN", t("rollback.running"))
    task_q.put({"schritt": t("rollback.step_running"), "sub_prog": None})

    for eintrag in reversed(undo_log):
        typ = eintrag["typ"]

        if typ == "mp4":
            pfad = eintrag["pfad"]
            try:
                if pfad.exists():
                    pfad.unlink()
                    log_func("OK", t("rollback.mp4_deleted", name=pfad.name))
                else:
                    log_func("INFO", t("rollback.mp4_gone", name=pfad.name))
            except Exception as e:
                log_func("ERR", t("rollback.mp4_delete_failed", fehler=e))

        elif typ == "mkv_move":
            von  = eintrag["von"]   # aktueller Pfad (in "old MKV")
            nach = eintrag["nach"]  # ursprünglicher Pfad
            try:
                if von.exists():
                    shutil.move(str(von), str(nach))
                    log_func("OK", t("rollback.mkv_restored", name=nach.name))
                else:
                    log_func("WARN", t("rollback.mkv_gone", name=von.name))
            except Exception as e:
                log_func("ERR", t("rollback.mkv_restore_failed", fehler=e))

        elif typ == "mkv_del":
            pfad = eintrag["pfad"]
            log_func("WARN", t("rollback.mkv_del_unrecoverable", name=pfad.name))

        elif typ == "nfo":
            nfo = eintrag["nfo"]
            bak = eintrag["bak"]
            try:
                if bak.exists():
                    shutil.copy2(str(bak), str(nfo))
                    bak.unlink()
                    log_func("OK", t("rollback.nfo_restored", name=nfo.name))
                else:
                    log_func("WARN", t("rollback.nfo_backup_missing", name=bak.name))
            except Exception as e:
                log_func("ERR", t("rollback.nfo_restore_failed", fehler=e))

        elif typ == "srt":
            pfad = eintrag["pfad"]
            try:
                if pfad.exists():
                    pfad.unlink()
                    log_func("OK", t("rollback.srt_deleted", name=pfad.name))
            except Exception as e:
                log_func("ERR", t("rollback.srt_delete_failed", fehler=e))

    log_func("OK", t("rollback.done"))
    task_q.put({"schritt": t("rollback.step_done"), "sub_prog": 100})
