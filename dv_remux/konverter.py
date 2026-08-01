"""
Video-Konverter (Unterprogramm)
===============================
Scannt einen Ordner nach Videodateien, ermittelt deren Auflösung/Bildformat
und wandelt alles, was noch kein MP4 ist, in ein LG-TV-taugliches MP4 um –
mit AMD-Hardwarebeschleunigung (AMF), sofern verfügbar.

Kernpunkte für die LG-/Jellyfin-Kompatibilität:
  • H.264 High (bis 1080p) bzw. HEVC Main + Tag hvc1 (darüber), yuv420p
  • Audio nach AAC (oder Copy, wenn die Quelle schon AAC ist)
  • -movflags +faststart  → Vorspulen/Seek funktioniert sofort
  • festes Keyframe-Intervall (2 s) → flüssiges Spulen statt Ruckeln
  • Auflösung der Quelle bleibt erhalten; anamorphe Quellen (SAR ≠ 1:1,
    typisch bei MPEG/DVD/WMV) werden auf quadratische Pixel gerechnet,
    sonst zeigt der TV das Bild verzerrt
  • interlaced Quellen werden deinterlaced (AMF encodiert nur progressiv)

Das Modul ist bewusst eigenständig: es benutzt nur sprache/konstanten und
kommuniziert – wie die Remux-Worker – ausschließlich über Queues mit der GUI.
"""

import json
import queue
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from dv_remux.dateioperationen import genug_speicherplatz, verschiebe_sicher
from dv_remux.konstanten import LOG_ORDNER, TEXT_CODECS, VERSION
from dv_remux.sprache import t, _bereinige_log

# ── Dateitypen ───────────────────────────────────────────────────────────────

# Quellformate, die konvertiert werden sollen.
# .mkv fehlt bewusst: MKVs gehören zum Remux-Teil des Hauptfensters und werden
# hier nur gemeldet, nicht angefasst.
VIDEO_ENDUNGEN = {
    ".avi", ".wmv", ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".mod",
    ".mov", ".qt", ".m4v", ".flv", ".f4v", ".vob", ".divx", ".xvid",
    ".ts", ".m2ts", ".mts", ".tp", ".asf", ".rm", ".rmvb", ".ogv", ".ogm",
    ".webm", ".3gp", ".3g2", ".dv", ".mxf", ".nsv", ".amv",
}

# Wird gescannt, aber nie konvertiert (eigene SKIP-Meldung)
UEBERSPRUNGENE_ENDUNGEN = {".mkv"}

# Begleitdateien, die stillschweigend ignoriert werden (kein SKIP-Log-Spam)
IGNORIERTE_ENDUNGEN = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tbn",
    ".nfo", ".xml", ".txt", ".log", ".md", ".json", ".ini", ".db",
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup",
    ".url", ".lnk", ".bak", ".part", ".tmp", ".sfv", ".par2",
}

ZIEL_ENDUNG = ".mp4"
SICHERUNGS_ORDNER = "old video"
ARBEITS_UNTERORDNER = "dv_konverter"   # Unterordner im Temp-/Arbeitsordner

# ── Qualitätsstufen ──────────────────────────────────────────────────────────
# Zielbitrate für HEVC bei 1080p/25 fps; kleinere/größere Auflösungen werden
# unterlinear skaliert (siehe berechne_bitrate). Bitrate statt konstantem
# Quantizer, weil verrauschte alte Quellen bei CQP größer werden können als
# das Original – mit einer Zielbitrate bleibt die Dateigröße vorhersehbar.
BITRATE_BASIS = {
    "hoch":   6_000_000,
    "mittel": 4_000_000,
    "klein":  2_500_000,
}
QUALITAET_DEFAULT = "mittel"

BITRATE_MIN = 900_000
BITRATE_MAX = 25_000_000
H264_AUFSCHLAG = 1.6      # H.264 braucht für dieselbe Qualität mehr Bitrate
REFERENZ_PIXEL_PRO_SEK = 1920 * 1080 * 25

# HEVC ab dieser Höhe (H.264 über 1080p wird von vielen TVs nicht dekodiert)
HEVC_AB_HOEHE = 1080

# Encoder-Kandidaten je Ziel-Codec: (Name, ist_hardware)
ENCODER_KANDIDATEN = {
    "h264": [("h264_amf", True), ("libx264", False)],
    "hevc": [("hevc_amf", True), ("libx265", False)],
}

_encoder_cache: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# Encoder-Ermittlung
# ─────────────────────────────────────────────────────────────────────────────

def _encoder_kompiliert(ffmpeg: Path, name: str) -> bool:
    """Prüft, ob ffmpeg den Encoder überhaupt kennt (Compile-Zeit-Support)."""
    try:
        erg = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return f" {name} " in erg.stdout
    except Exception:
        return False


def _encoder_funktioniert(ffmpeg: Path, name: str) -> bool:
    """
    Echter Mini-Testencode (1 s Testbild ins Nichts).
    Nötig, weil '-encoders' nur den Compile-Zeit-Support beweist – ob zur
    Laufzeit auch Treiber und GPU-Kontext bereitstehen, sagt es nicht.
    """
    befehl = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "1",
        "-c:v", name, "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=90)
        return erg.returncode == 0
    except Exception:
        return False


def ermittle_encoder(ffmpeg: Path, ziel_codec: str, amd_nutzen: bool) -> dict:
    """
    Besten verfügbaren Encoder für ziel_codec ('h264'|'hevc') bestimmen.
    Rückgabe: {"name": str, "hw": bool, "codec": str} – oder name=None,
    wenn gar kein Encoder läuft.
    Das Ergebnis wird gecacht (der Testencode kostet ~1 s pro Encoder).
    """
    schluessel = (str(ffmpeg), ziel_codec, amd_nutzen)
    if schluessel in _encoder_cache:
        return _encoder_cache[schluessel]

    ergebnis = {"name": None, "hw": False, "codec": ziel_codec}
    for name, ist_hw in ENCODER_KANDIDATEN.get(ziel_codec, []):
        if ist_hw and not amd_nutzen:
            continue
        if not _encoder_kompiliert(ffmpeg, name):
            continue
        if ist_hw and not _encoder_funktioniert(ffmpeg, name):
            continue
        ergebnis = {"name": name, "hw": ist_hw, "codec": ziel_codec}
        break

    _encoder_cache[schluessel] = ergebnis
    return ergebnis


# ─────────────────────────────────────────────────────────────────────────────
# Analyse
# ─────────────────────────────────────────────────────────────────────────────

def _bruch(text: str, standard=(1, 1)) -> tuple:
    """'16:15' oder '16/15' → (16, 15). Ungültiges/0 → standard."""
    if not text:
        return standard
    for trenner in (":", "/"):
        if trenner in text:
            a, _, b = text.partition(trenner)
            try:
                za, ne = int(a), int(b)
                if za > 0 and ne > 0:
                    return (za, ne)
            except ValueError:
                pass
            break
    return standard


def _fps(text: str) -> float:
    """ffprobe-Framerate ('25/1', '30000/1001') → float. Fallback 25.0."""
    try:
        if "/" in text:
            z, n = text.split("/")
            return float(z) / float(n) if float(n) else 25.0
        return float(text)
    except (ValueError, TypeError, ZeroDivisionError):
        return 25.0


def analysiere_video(ffprobe: Path, pfad: Path) -> dict:
    """
    Auflösung und alle für die Konvertierung nötigen Eigenschaften ermitteln.
    Gibt None zurück, wenn die Datei kein lesbares Video enthält.
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format",
        "-analyzeduration", "100M", "-probesize", "100M",
        str(pfad),
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
        if erg.returncode != 0:
            return None
        daten = json.loads(erg.stdout)
    except Exception:
        return None

    streams = daten.get("streams", [])
    video = next((s for s in streams
                  if s.get("codec_type") == "video"
                  # Cover-Art (Standbild) ist kein echter Videostream
                  and s.get("disposition", {}).get("attached_pic", 0) != 1), None)
    if not video:
        return None

    breite = int(video.get("width") or 0)
    hoehe  = int(video.get("height") or 0)
    if breite <= 0 or hoehe <= 0:
        return None

    sar = _bruch(video.get("sample_aspect_ratio", ""), (1, 1))
    dar = _bruch(video.get("display_aspect_ratio", ""), (0, 0))
    feld = (video.get("field_order") or "progressive").lower()

    audio = [{"index": s.get("index"),
              "codec": s.get("codec_name", "?"),
              "kanaele": int(s.get("channels") or 2)}
             for s in streams if s.get("codec_type") == "audio"]

    subs = [{"index": s.get("index"),
             "codec": s.get("codec_name", "?")}
            for s in streams if s.get("codec_type") == "subtitle"]

    try:
        dauer = float(daten.get("format", {}).get("duration") or 0)
    except (ValueError, TypeError):
        dauer = 0.0

    return {
        "breite":     breite,
        "hoehe":      hoehe,
        "sar":        sar,
        "dar":        dar,
        "feld":       feld,
        "interlaced": feld not in ("progressive", "unknown", ""),
        "fps":        _fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or ""),
        "codec":      video.get("codec_name", "?"),
        "dauer":      dauer,
        "audio":      audio,
        "text_subs":  [s["index"] for s in subs if s["codec"] in TEXT_CODECS],
        "groesse":    pfad.stat().st_size if pfad.exists() else 0,
    }


def ziel_aufloesung(info: dict, sar_korrigieren: bool) -> tuple:
    """
    Ausgabe-Auflösung bestimmen: die Quellauflösung bleibt erhalten, nur bei
    anamorphem Material (SAR ≠ 1:1) wird die Breite auf quadratische Pixel
    hochgerechnet – sonst zeigt der TV das Bild gestaucht/gestreckt.
    H.264/HEVC brauchen zudem gerade Kantenlängen.
    """
    breite, hoehe = info["breite"], info["hoehe"]
    sar_z, sar_n = info["sar"]

    if sar_korrigieren and sar_z != sar_n and sar_z > 0 and sar_n > 0:
        breite = int(round(breite * sar_z / sar_n))

    breite = max(2, breite - (breite % 2))
    hoehe  = max(2, hoehe  - (hoehe  % 2))
    return breite, hoehe


def berechne_bitrate(breite: int, hoehe: int, fps: float,
                     qualitaet: str, codec: str) -> int:
    """
    Zielbitrate aus Auflösung, Bildrate und Qualitätsstufe ableiten.
    Referenz ist 1080p/25 (Vorgabe „mittel" = 4 Mbit/s für HEVC); kleinere
    Auflösungen werden unterlinear skaliert, weil sie relativ mehr Bitrate pro
    Pixel brauchen. H.264 bekommt einen Aufschlag.
    """
    basis  = BITRATE_BASIS.get(qualitaet, BITRATE_BASIS[QUALITAET_DEFAULT])
    faktor = (breite * hoehe * max(fps, 1.0)) / REFERENZ_PIXEL_PRO_SEK
    bitrate = basis * (faktor ** 0.75)
    if codec == "h264":
        bitrate *= H264_AUFSCHLAG
    return int(max(BITRATE_MIN, min(BITRATE_MAX, bitrate)))


def waehle_ziel_codec(info: dict, wunsch: str) -> str:
    """'auto' → H.264 bis 1080p, darüber HEVC. Sonst der gewünschte Codec."""
    if wunsch in ("h264", "hevc"):
        return wunsch
    return "hevc" if info["hoehe"] > HEVC_AB_HOEHE or info["breite"] > 1920 else "h264"


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg-Kommando
# ─────────────────────────────────────────────────────────────────────────────

def baue_befehl(ffmpeg: Path, quelle: Path, ziel: Path, info: dict,
                encoder: dict, qualitaet: str, sar_korrigieren: bool,
                mit_subs: bool) -> list:
    """Vollständiges ffmpeg-Kommando für eine Datei zusammenbauen."""
    breite, hoehe = ziel_aufloesung(info, sar_korrigieren)
    fps     = info["fps"] if info["fps"] > 0 else 25.0
    gop     = max(12, int(round(fps * 2)))   # Keyframe alle 2 s → sauberes Spulen
    bitrate = berechne_bitrate(breite, hoehe, fps, qualitaet, encoder["codec"])
    maxrate = int(bitrate * 1.5)

    # Videofilter: erst deinterlacen, dann auf Zielgröße + quadratische Pixel
    filter_kette = []
    if info["interlaced"]:
        filter_kette.append("yadif=mode=0:parity=-1:deint=0")
    if (breite, hoehe) != (info["breite"], info["hoehe"]):
        filter_kette.append(f"scale={breite}:{hoehe}:flags=lanczos")
    filter_kette.append("setsar=1")

    befehl = [
        str(ffmpeg), "-hide_banner",
        "-fflags", "+genpts",                       # kaputte Timestamps alter Container
        "-analyzeduration", "100M", "-probesize", "100M",
        "-i", str(quelle),
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if mit_subs and info["text_subs"]:
        for idx in info["text_subs"]:
            befehl += ["-map", f"0:{idx}"]

    befehl += ["-vf", ",".join(filter_kette), "-pix_fmt", "yuv420p"]
    befehl += ["-c:v", encoder["name"]]

    if encoder["hw"]:
        # AMF: Peak-VBR auf die Zielbitrate, Qualitäts-Preset, Transcoding-Usage
        befehl += ["-usage", "transcoding", "-quality", "quality",
                   "-rc", "vbr_peak", "-b:v", str(bitrate),
                   "-maxrate", str(maxrate)]
        if encoder["name"] == "h264_amf":
            befehl += ["-profile:v", "high", "-coder", "cabac"]
        else:
            befehl += ["-profile:v", "main"]
    else:
        befehl += ["-preset", "medium", "-b:v", str(bitrate),
                   "-maxrate", str(maxrate), "-bufsize", str(bitrate * 3)]
        if encoder["name"] == "libx264":
            befehl += ["-profile:v", "high"]

    if encoder["codec"] == "hevc":
        befehl += ["-tag:v", "hvc1"]        # ohne hvc1 spielt LG/Apple kein HEVC im MP4

    befehl += ["-g", str(gop), "-keyint_min", str(gop // 2)]

    # Audio: schon AAC → verlustfrei durchreichen, sonst nach AAC wandeln
    alle_aac = bool(info["audio"]) and all(a["codec"] == "aac" for a in info["audio"])
    if not info["audio"]:
        pass
    elif alle_aac:
        befehl += ["-c:a", "copy"]
    else:
        max_kanaele = max(a["kanaele"] for a in info["audio"])
        befehl += ["-c:a", "aac", "-b:a", "192k" if max_kanaele <= 2 else "384k"]
        if max_kanaele > 6:
            befehl += ["-ac", "6"]

    if mit_subs and info["text_subs"]:
        befehl += ["-c:s", "mov_text"]

    befehl += ["-movflags", "+faststart", "-y", str(ziel)]
    return befehl


# ─────────────────────────────────────────────────────────────────────────────
# Konvertierung einer Datei
# ─────────────────────────────────────────────────────────────────────────────

def konvertiere_datei(ffmpeg: Path, quelle: Path, ziel: Path, info: dict,
                      encoder: dict, qualitaet: str, sar_korrigieren: bool,
                      log_q: queue.Queue, task_q: queue.Queue,
                      log_zeilen: list, stopp_event=None,
                      mit_subs: bool = True, sw_fallback_erlaubt: bool = True,
                      ffmpeg_sw_encoder: dict = None) -> bool:
    """
    Eine Datei konvertieren, mit Fortschritt und abgestufter Fehlerbehandlung:
    Untertitel raus → Software-Encoder → aufgeben.
    """
    befehl = baue_befehl(ffmpeg, quelle, ziel, info, encoder,
                         qualitaet, sar_korrigieren, mit_subs)

    def _log(typ, text):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    zb, zh = ziel_aufloesung(info, sar_korrigieren)
    mbit = berechne_bitrate(zb, zh, info["fps"] or 25.0, qualitaet,
                            encoder["codec"]) / 1_000_000
    _log("INFO", t("konv.encoding", name=quelle.name, encoder=encoder["name"],
                   bitrate=round(mbit, 1)))

    proc = None
    try:
        proc = subprocess.Popen(befehl, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace")
        dauer_sek = info["dauer"] or None
        stderr_zeilen = []
        for zeile in proc.stderr:
            if stopp_event and stopp_event.is_set():
                proc.terminate()
                proc.wait()
                ziel.unlink(missing_ok=True)
                _log("WARN", t("konv.abgebrochen"))
                return False
            zeile = zeile.rstrip()
            stderr_zeilen.append(zeile)
            if "Duration:" in zeile and not dauer_sek:
                try:
                    teil = zeile.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = teil.split(":")
                    dauer_sek = int(h) * 3600 + int(m) * 60 + float(s)
                except Exception:
                    pass
            if zeile.startswith("frame=") and "time=" in zeile:
                try:
                    zeit_str = zeile.split("time=")[1].split()[0]
                    h, m, s = zeit_str.split(":")
                    vergangen = int(h) * 3600 + int(m) * 60 + float(s)
                    if dauer_sek and dauer_sek > 0:
                        task_q.put({"sub_prog": min(int(vergangen / dauer_sek * 100), 99)})
                except Exception:
                    pass
                log_q.put(("PROG", f"     {zeile}"))
        proc.wait()

        if proc.returncode == 0:
            task_q.put({"sub_prog": 100})
            return True

        ziel.unlink(missing_ok=True)
        _log("ERR", t("konv.ffmpeg_stderr"))
        for z in stderr_zeilen[-12:]:
            if z.strip():
                log_q.put(("ERR", f"     {z}"))
                log_zeilen.append(f"     {z}")

        # Stufe 1: Untertitel weglassen (mov_text verträgt nicht jede Quelle)
        if mit_subs and info["text_subs"]:
            _log("WARN", t("konv.retry_ohne_subs"))
            return konvertiere_datei(ffmpeg, quelle, ziel, info, encoder,
                                     qualitaet, sar_korrigieren, log_q, task_q,
                                     log_zeilen, stopp_event,
                                     mit_subs=False,
                                     sw_fallback_erlaubt=sw_fallback_erlaubt,
                                     ffmpeg_sw_encoder=ffmpeg_sw_encoder)

        # Stufe 2: Hardware-Encoder scheitert an dieser Datei → Software
        if encoder["hw"] and sw_fallback_erlaubt and ffmpeg_sw_encoder \
                and ffmpeg_sw_encoder.get("name"):
            _log("WARN", t("konv.retry_software", encoder=ffmpeg_sw_encoder["name"]))
            return konvertiere_datei(ffmpeg, quelle, ziel, info,
                                     ffmpeg_sw_encoder, qualitaet,
                                     sar_korrigieren, log_q, task_q, log_zeilen,
                                     stopp_event, mit_subs=False,
                                     sw_fallback_erlaubt=False,
                                     ffmpeg_sw_encoder=None)

        rc = proc.returncode
        if rc > 0x7FFFFFFF:
            rc = rc - 0x100000000
        _log("ERR", t("konv.ffmpeg_error", code=rc))
        return False

    except FileNotFoundError:
        _log("ERR", t("konv.ffmpeg_not_found"))
        return False
    except Exception as e:
        ziel.unlink(missing_ok=True)
        _log("ERR", t("konv.unexpected_error", fehler=e))
        return False
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait()


# ─────────────────────────────────────────────────────────────────────────────
# Log-Datei
# ─────────────────────────────────────────────────────────────────────────────

def schreibe_konverter_log(log_zeilen: list, simulation: bool) -> Path:
    """Log-Datei des Konverters im logs/-Ordner ablegen."""
    try:
        LOG_ORDNER.mkdir(exist_ok=True)
        ts    = datetime.now()
        modus = "SIM" if simulation else "RUN"
        pfad  = LOG_ORDNER / f"konverter_{modus}_{ts.strftime('%Y%m%d_%H%M%S')}.log"
        kopf  = [
            f"Video-Konverter (DV Remux Tool v{VERSION})",
            t("logheader.date_label", datum=ts.strftime('%d.%m.%Y %H:%M:%S')),
            t("logheader.mode_label",
              modus=t("logheader.sim") if simulation else t("logheader.run")),
            "=" * 55, "",
        ]
        pfad.write_text("\n".join(kopf + log_zeilen), encoding="utf-8")
        return pfad
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Worker
# ─────────────────────────────────────────────────────────────────────────────

def sammle_dateien(ordner: Path, rekursiv: bool) -> tuple:
    """
    Ordnerinhalt sortieren in (kandidaten, schon_mp4, mkvs, unbekannt).
    kandidaten = Videodateien mit bekannter Nicht-MP4-Endung.
    Der Arbeits-/Sicherungsordner wird dabei ausgelassen.
    """
    muster = ordner.rglob("*") if rekursiv else ordner.glob("*")
    kandidaten, schon_mp4, mkvs, unbekannt = [], [], [], []
    for p in sorted(muster):
        if not p.is_file():
            continue
        if SICHERUNGS_ORDNER in p.parts or ARBEITS_UNTERORDNER in p.parts:
            continue
        endung = p.suffix.lower()
        if endung == ZIEL_ENDUNG:
            schon_mp4.append(p)
        elif endung in UEBERSPRUNGENE_ENDUNGEN:
            mkvs.append(p)
        elif endung in VIDEO_ENDUNGEN:
            kandidaten.append(p)
        elif endung in IGNORIERTE_ENDUNGEN:
            continue
        else:
            unbekannt.append(p)
    return kandidaten, schon_mp4, mkvs, unbekannt


def sichere_original(quelle: Path, basis_ordner: Path, behalten: bool,
                     simulation: bool, log_func, sicherungs_pfad: Path = None,
                     aktueller_pfad: Path = None) -> None:
    """
    Original nach erfolgreicher Konvertierung sichern oder löschen.
    aktueller_pfad: wo die Datei GERADE liegt (lokaler Arbeitsordner);
    quelle bleibt die Referenz für Dateiname und Sicherungsordner.
    """
    liegt_bei = aktueller_pfad or quelle
    if behalten:
        ziel_ordner = sicherungs_pfad or (quelle.parent / SICHERUNGS_ORDNER)
        ziel = ziel_ordner / quelle.name
        if simulation:
            log_func("SIM", t("konv.sim_original_move", ziel=str(ziel)))
            return
        if ziel.exists():
            log_func("WARN", t("konv.original_exists", name=quelle.name))
            return
        try:
            ziel_ordner.mkdir(parents=True, exist_ok=True)
            shutil.move(str(liegt_bei), str(ziel))
            log_func("OK", t("konv.original_moved", ziel=str(ziel)))
        except Exception as e:
            log_func("ERR", t("konv.original_move_failed", fehler=e))
    else:
        if simulation:
            log_func("SIM", t("konv.sim_original_delete", name=quelle.name))
            return
        try:
            liegt_bei.unlink()
            log_func("INFO", t("konv.original_deleted", name=quelle.name))
        except Exception as e:
            log_func("ERR", t("konv.original_delete_failed", fehler=e))


def verarbeite_videoordner(ffmpeg: str, ffprobe: str, ordner: str,
                           simulation: bool, rekursiv: bool,
                           original_behalten: bool, ziel_codec_wunsch: str,
                           qualitaet: str, amd_nutzen: bool,
                           sar_korrigieren: bool,
                           log_q: queue.Queue, task_q: queue.Queue,
                           fort_q: queue.Queue, done_q: queue.Queue,
                           stopp_event, sicherungs_pfad=None,
                           lokal_verarbeiten: bool = False,
                           arbeits_pfad=None):
    """
    Worker-Thread: Ordner scannen → Auflösungen ermitteln → konvertieren.
    Kommuniziert ausschließlich über die vier Queues mit der GUI.

    lokal_verarbeiten: Original wird zuerst in den lokalen Arbeitsordner
    verschoben, dort konvertiert, danach wandert nur das fertige MP4 zurück –
    das entlastet das NAS und verhindert MKV/MP4-Dubletten im Quellordner.
    Schlägt irgendein Schritt fehl, wird das Original an seinen Ursprungsort
    zurückgeschoben.
    """
    ffmpeg_p  = Path(ffmpeg)
    ffprobe_p = Path(ffprobe)
    wurzel    = Path(ordner)
    log_zeilen: list = []
    stats = {"konvertiert": 0, "fehler": 0, "uebersprungen": 0, "gefunden": 0}

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    try:
        log("HEAD", t("konv.header", ordner=str(wurzel)))
        log("INFO", t("konv.mode_line",
                      rekursiv=t("konv.ja") if rekursiv else t("konv.nein"),
                      qualitaet=t(f"konvgui.quality_{qualitaet}"),
                      codec=t(f"konvgui.codec_{ziel_codec_wunsch}"),
                      lokal=t("konv.ja") if lokal_verarbeiten else t("konv.nein")))

        # ── Phase 1: Scannen ────────────────────────────────────────────────
        task_q.put({"film": wurzel.name, "schritt": t("konv.step_scan"),
                    "sub_prog": None})
        kandidaten, schon_mp4, mkvs, unbekannt = sammle_dateien(wurzel, rekursiv)
        stats["gefunden"] = len(kandidaten)

        for p in schon_mp4:
            log("SKIP", t("konv.skip_ist_mp4", name=p.name))
            stats["uebersprungen"] += 1
        for p in mkvs:
            log("SKIP", t("konv.skip_mkv", name=p.name))
            stats["uebersprungen"] += 1
        for p in unbekannt:
            log("SKIP", t("konv.skip_unbekannt", name=p.name))
            stats["uebersprungen"] += 1

        if not kandidaten:
            log("WARN", t("konv.nichts_zu_tun"))
            task_q.put({"schritt": t("konv.step_done"), "sub_prog": 100})
            fort_q.put(100)
            done_q.put((stats, schreibe_konverter_log(log_zeilen, simulation)))
            return

        # Auflösungen ermitteln und als Übersicht ausgeben
        log("HEAD", t("konv.scan_header", anzahl=len(kandidaten)))
        infos = {}
        for i, p in enumerate(kandidaten, 1):
            if stopp_event.is_set():
                break
            task_q.put({"film": p.name, "schritt": t("konv.step_analyse"),
                        "sub_prog": int(i / len(kandidaten) * 100)})
            info = analysiere_video(ffprobe_p, p)
            if not info:
                log("ERR", t("konv.analyse_failed", name=p.name))
                stats["fehler"] += 1
                continue
            infos[p] = info
            zb, zh = ziel_aufloesung(info, sar_korrigieren)
            sar_txt = f"{info['sar'][0]}:{info['sar'][1]}"
            ziel_txt = (t("konv.ziel_gleich") if (zb, zh) == (info["breite"], info["hoehe"])
                        else t("konv.ziel_anders", breite=zb, hoehe=zh))
            log("INFO", t("konv.scan_zeile",
                          name=p.name, breite=info["breite"], hoehe=info["hoehe"],
                          sar=sar_txt, fps=round(info["fps"], 3),
                          codec=info["codec"],
                          scan=(t("konv.interlaced") if info["interlaced"]
                                else t("konv.progressiv")),
                          ziel=ziel_txt))

        if stopp_event.is_set():
            log("WARN", t("konv.abgebrochen"))
            done_q.put((stats, schreibe_konverter_log(log_zeilen, simulation)))
            return

        # ── Encoder bestimmen ───────────────────────────────────────────────
        task_q.put({"schritt": t("konv.step_encoder"), "sub_prog": None})
        benoetigte_codecs = {waehle_ziel_codec(i, ziel_codec_wunsch)
                             for i in infos.values()}
        encoder_map, sw_map = {}, {}
        for codec in benoetigte_codecs:
            encoder_map[codec] = ermittle_encoder(ffmpeg_p, codec, amd_nutzen)
            sw_map[codec]      = ermittle_encoder(ffmpeg_p, codec, amd_nutzen=False)
            enc = encoder_map[codec]
            if not enc["name"]:
                log("ERR", t("konv.kein_encoder", codec=codec))
            elif enc["hw"]:
                log("OK", t("konv.encoder_hw", codec=codec, encoder=enc["name"]))
            else:
                log("WARN", t("konv.encoder_sw", codec=codec, encoder=enc["name"]))

        # ── Arbeitsordner für die lokale Verarbeitung ───────────────────────
        arbeitsordner = None
        if lokal_verarbeiten and arbeits_pfad:
            arbeitsordner = Path(arbeits_pfad) / ARBEITS_UNTERORDNER
            if not simulation:
                try:
                    arbeitsordner.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    log("WARN", t("konv.arbeitsordner_fehler", fehler=e))
                    arbeitsordner = None
            if arbeitsordner:
                log("INFO", t("konv.lokal_aktiv", ordner=str(arbeitsordner)))

        # ── Phase 2: Konvertieren ───────────────────────────────────────────
        gesamt = len(infos)
        for nr, (quelle, info) in enumerate(infos.items(), 1):
            if stopp_event.is_set():
                log("WARN", t("konv.abgebrochen"))
                break

            fort_q.put(int((nr - 1) / gesamt * 100))
            task_q.put({"film": f"[{nr}/{gesamt}] {quelle.name}",
                        "schritt": t("konv.step_convert"), "sub_prog": 0})

            ziel = quelle.with_suffix(ZIEL_ENDUNG)
            if ziel.exists():
                log("SKIP", t("konv.skip_ziel_existiert", name=ziel.name))
                stats["uebersprungen"] += 1
                continue

            codec = waehle_ziel_codec(info, ziel_codec_wunsch)
            enc   = encoder_map.get(codec, {"name": None})
            if not enc["name"]:
                log("ERR", t("konv.kein_encoder", codec=codec))
                stats["fehler"] += 1
                continue

            if simulation:
                zb, zh = ziel_aufloesung(info, sar_korrigieren)
                if arbeitsordner:
                    log("SIM", t("konv.sim_lokal", name=quelle.name,
                                 ordner=str(arbeitsordner)))
                log("SIM", t("konv.sim_convert", quelle=quelle.name,
                             ziel=ziel.name, breite=zb, hoehe=zh,
                             encoder=enc["name"]))
                sichere_original(quelle, wurzel, original_behalten, True, log,
                                 sicherungs_pfad)
                for p in range(0, 101, 20):
                    task_q.put({"sub_prog": p})
                    time.sleep(0.03)
                stats["konvertiert"] += 1
                continue

            # ── Lokale Verarbeitung: Original in den Arbeitsordner holen ────
            lokal_aktiv   = False
            arbeits_quelle = quelle
            arbeits_ziel   = ziel
            if arbeitsordner:
                if not genug_speicherplatz(arbeitsordner, info["groesse"]):
                    log("WARN", t("konv.zu_wenig_platz", name=quelle.name))
                else:
                    arbeits_quelle = arbeitsordner / quelle.name
                    arbeits_ziel   = arbeitsordner / ziel.name
                    arbeits_quelle.unlink(missing_ok=True)
                    arbeits_ziel.unlink(missing_ok=True)
                    if verschiebe_sicher(quelle, arbeits_quelle,
                                         t("konv.step_move_local"),
                                         log_q, task_q, False, log_zeilen,
                                         stopp_event=stopp_event):
                        lokal_aktiv = True
                    else:
                        # Verschieben fehlgeschlagen → am Ursprungsort weitermachen
                        arbeits_quelle, arbeits_ziel = quelle, ziel
                        if stopp_event.is_set():
                            break

            def zurueck_an_ursprung():
                """Original nach einem Fehler an seinen Ursprungsort zurückholen."""
                if not lokal_aktiv or not arbeits_quelle.exists():
                    return
                if verschiebe_sicher(arbeits_quelle, quelle,
                                     t("konv.step_restore"), log_q, task_q,
                                     False, log_zeilen):
                    log("OK", t("konv.original_zurueck", name=quelle.name))
                else:
                    log("ERR", t("konv.original_zurueck_failed",
                                 pfad=str(arbeits_quelle)))

            task_q.put({"schritt": t("konv.step_convert"), "sub_prog": 0})
            erfolg = konvertiere_datei(
                ffmpeg_p, arbeits_quelle, arbeits_ziel, info, enc, qualitaet,
                sar_korrigieren, log_q, task_q, log_zeilen, stopp_event,
                mit_subs=True, sw_fallback_erlaubt=True,
                ffmpeg_sw_encoder=sw_map.get(codec))

            if not (erfolg and arbeits_ziel.exists() and arbeits_ziel.stat().st_size > 0):
                arbeits_ziel.unlink(missing_ok=True)
                zurueck_an_ursprung()
                if not stopp_event.is_set():
                    log("ERR", t("konv.fehlgeschlagen", name=quelle.name))
                    stats["fehler"] += 1
                continue

            # ── Fertiges MP4 zurück an den Ursprungsort ─────────────────────
            if lokal_aktiv:
                if not verschiebe_sicher(arbeits_ziel, ziel,
                                         t("konv.step_move_back"),
                                         log_q, task_q, False, log_zeilen):
                    # MP4 bleibt lokal erhalten, Original geht zurück
                    log("ERR", t("konv.rueckkopie_failed", pfad=str(arbeits_ziel)))
                    zurueck_an_ursprung()
                    stats["fehler"] += 1
                    continue

            alt_mb = info["groesse"] / 1024 / 1024
            neu_mb = ziel.stat().st_size / 1024 / 1024
            log("OK", t("konv.fertig", name=ziel.name,
                        alt=round(alt_mb, 1), neu=round(neu_mb, 1)))
            stats["konvertiert"] += 1
            # Original löschen bzw. sichern – bei lokaler Verarbeitung liegt es
            # bereits im Arbeitsordner, sonst noch am Ursprungsort.
            sichere_original(quelle, wurzel, original_behalten, False, log,
                             sicherungs_pfad,
                             aktueller_pfad=arbeits_quelle if lokal_aktiv else None)

        fort_q.put(100)
        task_q.put({"schritt": t("konv.step_done"), "sub_prog": 100})
        log("HEAD", t("konv.summary",
                      ok=stats["konvertiert"], err=stats["fehler"],
                      skip=stats["uebersprungen"]))

    except Exception as e:
        log("ERR", t("konv.worker_error", fehler=e))
        stats["fehler"] += 1
    finally:
        done_q.put((stats, schreibe_konverter_log(log_zeilen, simulation)))
