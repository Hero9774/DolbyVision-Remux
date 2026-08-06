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
from dv_remux.mkv_analyse import (ermittle_audio_streams, ermittle_video_codec,
                                  ermittle_video_geometrie)
from dv_remux.mp4_binary import (_berechne_dv_level, _dvcc_vorhanden,
                                 injiziere_dvcc_box, mache_faststart_und_ftyp)

# Namensbestandteile, die HINTER dem Sprachcode einer SRT stehen können.
# Werden beim Auslesen der Sprache für die NFO abgeschnitten.
SRT_MARKER = {"forced", "erzwungen", "sdh", "cc", "hi"}


def konvertiere_dv_p5_zu_p8(
        ffmpeg: Path, dovi_tool: Path, ffprobe: Path,
        mkv_pfad: Path, mp4_pfad: Path,
        log_q: queue.Queue, task_q: queue.Queue,
        simulation: bool, log_zeilen: list,
        stopp_event=None, dts_zu_eac3: bool = False) -> bool:
    """
    !!! WIRD SEIT v5.9.2 NICHT MEHR AUFGERUFEN – NICHT REAKTIVIEREN !!!

    Die Funktion erzeugt KEIN gültiges Profil 8.1. dovi_tool schreibt nur die
    RPU um; die Bilddaten des Basislayers bleiben in IPT-PQ-C2. Das Ergebnis
    behauptet im dvcC-Record einen HDR10-kompatiblen Basislayer und liefert
    IPT-Pixel – gemessen an SNW S04E02/E03: video_full_range_flag=1 und
    colour_primaries/transfer/matrix jeweils 2 (unspecified), während eine
    gesunde Datei 0 / 9 / 16 / 9 zeigt. LG-Player und Jellyfin verweigern
    solche Dateien.

    Eine echte Konvertierung braucht zwischen Schritt 1 und 2 zusätzlich ein
    Re-Encode des Basislayers (libplacebo: IPT-PQ-C2 → BT.2020/PQ) – also
    verlustbehaftet und um Größenordnungen langsamer. Wer das will, baut es
    hier ein; der bestehende Ablauf allein genügt nicht.

    Ursprüngliche Beschreibung:
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
                mp4_pfad.unlink(missing_ok=True)
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
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30).stdout
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
        if kompatible:
            audio_indizes = kompatible
            if len(kompatible) < len(alle_audio):
                text = t("p5p8.truehd_excluded",
                         anzahl=len(alle_audio) - len(kompatible))
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))
        else:
            # Es gibt gar keine MP4-taugliche Spur -> alle mappen und warnen.
            # Vorher wurde hier "n Spuren ausgelassen" gemeldet, obwohl keine
            # ausgelassen wurde – die Meldung führte bei der Fehlersuche in die Irre.
            audio_indizes = [s["index"] for s in alle_audio]
            if alle_audio:
                text = t("p5p8.truehd_only")
                log_q.put(("WARN", text))
                log_zeilen.append(_bereinige_log(text))

        audio_maps = []
        for idx in audio_indizes:
            audio_maps += ["-map", f"1:{idx}"]

        # DTS ist im MP4-Container nur als mp4a/esds darstellbar; Hardware-Player
        # verweigern dann die GANZE Datei. Derselbe Fix wie in remux_zu_mp4().
        audio_codec_args3 = []
        if dts_zu_eac3:
            gemappt3 = [s for s in alle_audio if s["index"] in audio_indizes]
            audio_codec_args3, anzahl_dts3 = dts_audio_argumente(gemappt3)
            if anzahl_dts3:
                text = t("remux.dts_zu_eac3", anzahl=anzahl_dts3, bitrate=EAC3_BITRATE)
                log_q.put(("INFO", text))
                log_zeilen.append(_bereinige_log(text))

        # Anamorphe Quelle auch hier normalisieren (siehe anamorph_argumente)
        geometrie3 = ermittle_video_geometrie(ffprobe, mkv_pfad)
        anamorph3, status3 = anamorph_argumente(geometrie3)
        if status3:
            # Auch "zu_stark" melden – sonst bleibt die MP4 anamorph, Jellyfin
            # verweigert Direct Play und im Log steht kein Hinweis darauf.
            schluessel3 = {"fix": "p5p8.anamorph_fix",
                           "zu_stark": "p5p8.anamorph_zu_stark"}[status3]
            sar3 = geometrie3.get("sar", (1, 1))
            text = t(schluessel3, sar=f"{sar3[0]}:{sar3[1]}",
                     breite=geometrie3.get("breite", 0),
                     hoehe=geometrie3.get("hoehe", 0))
            log_q.put(("INFO" if status3 == "fix" else "WARN", text))
            log_zeilen.append(_bereinige_log(text))

        # Kein -movflags +faststart: moov landet am Ende → dvcC-Injektion
        # (Schritt 4) kann Box-Offsets unverändert lassen
        befehl3 = ([str(ffmpeg),
                    "-i", str(tmp_hevc_p8),
                    "-i", str(mkv_pfad)]
                   + ["-map", "0:v:0"] + audio_maps
                   + ["-c", "copy", "-strict", "unofficial",
                      "-tag:v", "dvh1"] + audio_codec_args3 + anamorph3
                   + ["-y", str(mp4_pfad)])

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
                mp4_pfad.unlink(missing_ok=True)
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
            mp4_pfad.unlink(missing_ok=True)   # halbfertige Datei nicht liegen lassen
            return False

        # ── Schritt 4: dvcC-Box injizieren ────────────────────────────────
        task_q.put({"schritt": t("p5p8.taskstep4"), "sub_prog": 93})
        text = t("p5p8.step4_start", level=dv_level)
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        # Neuere ffmpeg-Versionen schreiben den DV-Record beim Muxen selbst –
        # dann ist nichts zu injizieren und das ist kein Fehler.
        if _dvcc_vorhanden(mp4_pfad):
            text = t("p5p8.step4_bereits")
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))
        elif not injiziere_dvcc_box(mp4_pfad, dv_profil=8, dv_level=dv_level, compat_id=1):
            text = t("p5p8.step4_failed")
            log_q.put(("ERR", text))
            log_zeilen.append(_bereinige_log(text))
            mp4_pfad.unlink(missing_ok=True)
            return False
        else:
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
                           ziel_ordner: Path = None,
                           stopp_event=None) -> list:
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
    sprachzaehler = {}
    text_streams   = [s for s in streams if s["codec"] in TEXT_CODECS]
    bitmap_streams = [s for s in streams if s["codec"] not in TEXT_CODECS]
    erstellte_srts = []   # ← Rückgabe-Liste

    for s in bitmap_streams:
        text = t("srt.bitmap_skip", index=s['index'], sprache=s['language'], codec=s['codec'].upper())
        log_q.put(("SKIP", text))
        log_zeilen.append(_bereinige_log(text))

    total = len(text_streams)
    for i, stream in enumerate(text_streams):
        # Stopp-Button auch hier auswerten – vorher lief die Schleife nach
        # einem Abbruch stumpf über alle Spuren weiter.
        if stopp_event and stopp_event.is_set():
            text = t("srt.abgebrochen")
            log_q.put(("WARN", text))
            log_zeilen.append(_bereinige_log(text))
            break
        sprache = stream["language"]
        idx     = stream["index"]
        # Forced- und Vollspur derselben Sprache werden getrennt gezählt: sie
        # bekommen unterschiedliche Dateinamen und dürfen sich deshalb nicht
        # gegenseitig als Duplikat verdrängen. '.forced.srt' ist die Endung,
        # an der Jellyfin eine Forced-Spur erkennt.
        forced  = bool(stream.get("forced"))
        zaehler_key = (sprache, forced)
        sprachzaehler[zaehler_key] = sprachzaehler.get(zaehler_key, 0) + 1
        if sprachzaehler[zaehler_key] > 1:
            text = t("srt.duplicate_skip", sprache=sprache, idx=idx)
            log_q.put(("SKIP", text))
            log_zeilen.append(_bereinige_log(text))
            continue
        suffix = f".{sprache}.forced.srt" if forced else f".{sprache}.srt"
        srt_pfad = ziel_ordner / (basis.name + suffix)

        task_q.put({
            "schritt":  f"SRT: {srt_pfad.name}",
            "sub_prog": int(i / total * 100)
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
            # timeout: ein hängender ffmpeg (defekter Stream, NAS-Timeout) würde
            # den Worker-Thread sonst dauerhaft blockieren.
            subprocess.run(befehl, capture_output=True, check=True, timeout=900)
            text = t("srt.ok", name=srt_pfad.name)
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))
            erstellte_srts.append(srt_pfad)
            if undo_log is not None:
                undo_log.append({"typ": "srt", "pfad": srt_pfad})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            srt_pfad.unlink(missing_ok=True)   # Torso nicht liegen lassen
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
           Film.deu.srt        → <language>deu</language>
           Film.deu.2.srt      → <language>deu</language>
           Film.deu.forced.srt → <language>deu</language>
         Alle anderen Stream-Details (Video, Audio) bleiben unverändert.

    tinyMediaManager-Kommentar und XML-Deklaration bleiben erhalten,
    da wir die Datei als Text lesen und gezielt Bereiche ersetzen.
    """
    task_q.put({"schritt": t("nfo.taskstep_updating"), "sub_prog": None})
    text = t("nfo.updating")
    log_q.put(("INFO", text))
    log_zeilen.append(_bereinige_log(text))

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
        task_q.put({"schritt": t("nfo.taskstep_failed"), "sub_prog": 100})
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
            # Nachgestellte Kennzeichnungen abschneiden, sonst wäre bei
            # "Film.ger.forced.srt" der Sprachcode "forced" – der fällt durch
            # die Längenprüfung und die Spur landete als <language>und</language>
            # in der NFO.
            while len(teile) >= 2 and teile[-1].lower() in SRT_MARKER:
                teile.pop()
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
        text = t("nfo.subtitle_unchanged")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))

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


def _ggt(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


# Zielbitrate für die DTS→E-AC3-Wandlung (5.1 in Referenzqualität)
EAC3_BITRATE = "640k"

# Bis zu dieser SAR-Abweichung wird auf quadratische Pixel normalisiert.
# 1 % ist unsichtbar; darüber müsste man skalieren – und das ginge nur mit
# Neucodierung, die beim Dolby-Vision-Remux gerade nicht stattfinden soll.
ANAMORPH_TOLERANZ = 0.01


def anamorph_argumente(geometrie: dict) -> tuple:
    """
    ffmpeg-Argumente, die eine leicht anamorphe Quelle auf quadratische Pixel
    normalisieren – ohne Neucodierung und ohne den Bitstream anzufassen.

    Nötig, weil Jellyfin für Videos mit SAR ≠ 1:1 die direkte Wiedergabe
    ablehnt ("anamorphes Video wird nicht unterstützt"). `-aspect` allein
    genügt: die pasp-Box im MP4-Container hat für ffprobe (und damit für
    Jellyfin) Vorrang vor der SAR im HEVC-VUI.

    WICHTIG – nicht auf `-bsf:v hevc_metadata=sample_aspect_ratio=1/1`
    umstellen: der Filter serialisiert VPS/SPS/PPS neu, und bei Dolby-Vision-
    Material passt die RPU danach nicht mehr zu den Parametersätzen. Das Bild
    zerfällt auf dem TV in Farbschlieren, obwohl ffmpeg die Datei anstandslos
    dekodiert (am LG G4 verifiziert). Der Container-Weg lässt VPS/SPS/PPS und
    alle RPU-NALs nachweislich unverändert.

    Rückgabe: (argumente, status) mit status ∈
      None       – Quelle ist bereits quadratisch, nichts zu tun
      "fix"      – wird korrigiert (Abweichung ≤ ANAMORPH_TOLERANZ)
      "zu_stark" – echtes anamorphes Bild (z. B. PAL SAR 16:15): NICHT
                   angefasst, weil das Bild sonst spürbar verzerrt wäre
    """
    sar_z, sar_n = geometrie.get("sar", (1, 1))
    breite = geometrie.get("breite", 0)
    hoehe  = geometrie.get("hoehe", 0)
    if sar_z == sar_n or breite <= 0 or hoehe <= 0:
        return [], None

    if abs(sar_z / sar_n - 1.0) > ANAMORPH_TOLERANZ:
        return [], "zu_stark"

    teiler = _ggt(breite, hoehe)
    return ["-aspect", f"{breite // teiler}:{hoehe // teiler}"], "fix"


def dts_audio_argumente(audio_streams: list) -> tuple:
    """
    Pro-Spur-Audioargumente, die DTS nach E-AC3 wandeln und alles andere
    unverändert kopieren.

    Hintergrund: ffmpeg kann DTS im MP4-Container ausschließlich als
    mp4a-Sample-Entry mit esds (objectTypeIndication 0xA9) schreiben – ein
    eigener dtsc-Entry wird abgelehnt ("codec not currently supported in
    container"). Hardware-Player erkennen diese Konstruktion nicht und
    verweigern die ganze Datei, selbst wenn sie DTS eigentlich beherrschen
    (am LG G4 verifiziert). E-AC3 640k ist der verlustarme Ausweg, der überall
    läuft; das Video bleibt dabei unangetastet.

    audio_streams: Liste in der Reihenfolge, in der die Spuren gemappt werden.
    Rückgabe: (argumente, anzahl_gewandelter_spuren).
    """
    if not any(s.get("codec") == "dts" for s in audio_streams):
        return [], 0

    argumente, gewandelt = [], 0
    for n, s in enumerate(audio_streams):
        if s.get("codec") == "dts":
            argumente += [f"-c:a:{n}", "eac3", f"-b:a:{n}", EAC3_BITRATE]
            # E-AC3 kann höchstens 5.1 – 7.1-Quellen heruntermischen
            if int(s.get("kanaele") or 2) > 6:
                argumente += [f"-ac:a:{n}", "6"]
            gewandelt += 1
        else:
            argumente += [f"-c:a:{n}", "copy"]
    return argumente, gewandelt


def remux_zu_mp4(ffmpeg: Path, mkv_pfad: Path, mp4_pfad: Path,
                 log_q: queue.Queue, task_q: queue.Queue,
                 simulation: bool, log_zeilen: list,
                 stopp_event=None, text_sub_indices=None,
                 ffprobe_pfad: Path = None,
                 audio_indices: list = None,
                 kein_faststart: bool = False,
                 dts_zu_eac3: bool = False,
                 dv_profil: int = None) -> bool:
    """Remux MKV -> MP4 ohne Re-Encoding (Video immer -c copy).
    text_sub_indices: Stream-Indizes für Text-Untertitel → mov_text einbetten.
    audio_indices: Explizite Audio-Stream-Indizes (None = alle via -map 0:a).
    ffprobe_pfad: Wird für TrueHD-Retry benötigt.
    kein_faststart: True = kein -movflags +faststart (für nachgelagerte dvcC-Injektion).
    dts_zu_eac3: DTS-Tonspuren nach E-AC3 wandeln (siehe dts_audio_argumente);
                 alle übrigen Spuren werden weiterhin nur kopiert.
    dv_profil: Dolby-Vision-Profil der Quelle. Nur 5 wird gesondert behandelt
               (Sample-Entry dvh1 statt hvc1) – siehe Kommentar bei video_tag.
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

    # DTS ist im MP4-Container nur als mp4a/esds darstellbar und wird von
    # Hardware-Playern nicht erkannt → auf Wunsch nach E-AC3 wandeln.
    audio_codec_args = []
    if dts_zu_eac3 and ffprobe_pfad:
        alle_audio = ermittle_audio_streams(ffprobe_pfad, mkv_pfad)
        gemappt = ([s for s in alle_audio if s["index"] in audio_indices]
                   if audio_indices is not None else alle_audio)
        audio_codec_args, anzahl_dts = dts_audio_argumente(gemappt)
        if anzahl_dts:
            text = t("remux.dts_zu_eac3", anzahl=anzahl_dts, bitrate=EAC3_BITRATE)
            log_q.put(("INFO", text))
            log_zeilen.append(_bereinige_log(text))

    # Video-Tag codec-abhängig wählen: 'hvc1' gilt nur für HEVC (LG-TV-Kompatibilität).
    # Bei AV1 (Dolby Vision Profil 10) ist hvc1 inkompatibel → ffmpeg bricht ab
    # ("Tag hvc1 incompatible with output codec id 'av01'"). Dort keinen Tag setzen –
    # ffmpeg vergibt den korrekten 'av01'-Tag automatisch.
    geometrie   = ermittle_video_geometrie(ffprobe_pfad, mkv_pfad) if ffprobe_pfad else {}
    video_codec = geometrie.get("codec") or (
        ermittle_video_codec(ffprobe_pfad, mkv_pfad) if ffprobe_pfad else None)
    # Profil 5 ist die Ausnahme: sein Basislayer liegt in IPT-PQ-C2, ist also
    # KEIN gültiges HDR10. 'hvc1' + dvvC würde dem Player Cross-Kompatibilität
    # versprechen, die die Datei nicht einlösen kann – LG-Player und Jellyfin
    # verweigern sie dann (am G4 verifiziert). Korrekt ist hier 'dvh1' + dvcC
    # mit compat_id 0. Wer echtes 8.1 will, kommt um ein Re-Encode des
    # Basislayers (IPT → BT.2020/PQ) nicht herum; dovi_tool allein reicht
    # nicht, es fasst ausschließlich die RPU an.
    if video_codec == "av1":
        video_tag = []
        text = t("remux.av1_detected")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
    elif dv_profil == 5:
        video_tag = ["-tag:v", "dvh1"]
        text = t("remux.p5_nativ")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
    else:
        video_tag = ["-tag:v", "hvc1"]

    # Anamorphe Quelle (SAR ≠ 1:1) auf quadratische Pixel normalisieren –
    # sonst lehnt Jellyfin die direkte Wiedergabe ab.
    anamorph, anamorph_status = anamorph_argumente(geometrie)
    if anamorph_status:
        sar_z, sar_n = geometrie["sar"]
        schluessel = {"fix": "remux.anamorph_fix",
                      "zu_stark": "remux.anamorph_zu_stark"}[anamorph_status]
        text = t(schluessel, sar=f"{sar_z}:{sar_n}",
                 breite=geometrie["breite"], hoehe=geometrie["hoehe"])
        log_q.put(("INFO" if anamorph_status == "fix" else "WARN", text))
        log_zeilen.append(_bereinige_log(text))

    # Kommando aufbauen – 0:v:0 = nur Haupt-Videostream (kein MJPEG-Cover)
    fs_flags = [] if kein_faststart else ["-movflags", "+faststart"]
    if text_sub_indices:
        # Video + Audio + ausgewählte Text-Untertitel (→ mov_text)
        befehl = [str(ffmpeg), "-i", str(mkv_pfad), "-map", "0:v:0"] + audio_maps
        for idx in text_sub_indices:
            befehl += ["-map", f"0:{idx}"]
        befehl += (["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                    "-strict", "unofficial"] + audio_codec_args
                   + video_tag + anamorph
                   + fs_flags + ["-y", str(mp4_pfad)])
    else:
        # Nur Haupt-Video + Audio mappen
        befehl = ([str(ffmpeg), "-i", str(mkv_pfad), "-map", "0:v:0"]
                  + audio_maps
                  + ["-c", "copy", "-strict", "unofficial"] + audio_codec_args
                  + video_tag + anamorph
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
            log_zeilen.append(_bereinige_log(text))
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
                                    kein_faststart=kein_faststart,
                                    dts_zu_eac3=dts_zu_eac3)

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
                                        kein_faststart=kein_faststart,
                                        dts_zu_eac3=dts_zu_eac3)

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
