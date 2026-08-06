"""MKV-/NFO-Analyse: HDR-Typ, DV-Profil, Audio-/Untertitel-Streams (via ffprobe)."""

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def lese_hdrtype_aus_nfo(nfo_pfad: Path):
    """HDR-Typ aus movie.nfo lesen. Gibt z.B. 'dolbyvision' zurück."""
    try:
        wurzel = ET.parse(nfo_pfad).getroot()
        el = wurzel.find("./fileinfo/streamdetails/video/hdrtype")
        if el is not None and el.text:
            # Leerzeichen entfernen: "Dolby Vision" → "dolbyvision"
            return el.text.strip().lower().replace(" ", "")
    except Exception:
        # Absichtlich breit: FileNotFoundError (NFO zwischen Scan und Lesen
        # verschwunden), PermissionError und LookupError (unbekanntes encoding=
        # in der XML-Deklaration) dürfen den Lauf nicht abbrechen.
        pass
    return None

def finde_mkv(ordner: Path):
    """Erste .mkv-Datei im Ordner zurückgeben."""
    dateien = list(ordner.glob("*.mkv"))
    return dateien[0] if dateien else None

def ermittle_video_codec(ffprobe: Path, mkv_pfad: Path):
    """Codec des Haupt-Videostreams ermitteln (via ffprobe).
    Gibt den ffprobe-codec_name zurück (z.B. 'hevc', 'av1', 'h264') oder None.
    Wird benötigt, um den MP4-Video-Tag korrekt zu wählen: 'hvc1' gilt nur für
    HEVC – bei AV1 (Dolby Vision Profil 10) ist er inkompatibel.
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", str(mkv_pfad)
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        streams = json.loads(erg.stdout).get("streams", [])
        if streams:
            return streams[0].get("codec_name")
    except Exception:
        pass
    return None

def ermittle_video_geometrie(ffprobe: Path, pfad: Path) -> dict:
    """Codec, Auflösung und Pixel-Seitenverhältnis des Haupt-Videostreams.

    Gibt {'codec', 'breite', 'hoehe', 'sar': (z, n)} zurück (leeres dict bei
    Fehler). Wird für die Anamorph-Korrektur gebraucht: Quellen mit SAR ≠ 1:1
    (z. B. 3840x2080 mit SAR 481:480) landen sonst als anamorphe MP4 im
    Container, und Jellyfin verweigert dafür die direkte Wiedergabe
    ("anamorphic video is not supported").
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", str(pfad)
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        streams = json.loads(erg.stdout).get("streams", [])
        if not streams:
            return {}
        s = streams[0]
        sar = (1, 1)
        roh = s.get("sample_aspect_ratio", "")
        if roh and ":" in roh:
            try:
                z, n = (int(x) for x in roh.split(":"))
                if z > 0 and n > 0:
                    sar = (z, n)
            except ValueError:
                pass
        return {
            "codec":  s.get("codec_name"),
            "breite": int(s.get("width") or 0),
            "hoehe":  int(s.get("height") or 0),
            "sar":    sar,
        }
    except Exception:
        return {}


def ermittle_audio_streams(ffprobe: Path, mkv_pfad: Path) -> list:
    """Audio-Streams analysieren – Liste mit index, codec_name und Kanalzahl.
    Wird für den TrueHD-Fallback in remux_zu_mp4 benötigt: TrueHD ist im
    MP4-Container nicht erlaubt; mit den zurückgegebenen Indizes können
    inkompatible Tracks gezielt ausgelassen werden. Die Kanalzahl braucht die
    DTS→E-AC3-Wandlung (E-AC3 kann höchstens 5.1).
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "a", str(mkv_pfad)
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        daten = json.loads(erg.stdout)
        return [
            {"index":   s.get("index", "?"),
             "codec":   s.get("codec_name", "unbekannt"),
             "kanaele": int(s.get("channels") or 2)}
            for s in daten.get("streams", [])
        ]
    except Exception:
        return []

def _ist_forced(stream: dict) -> bool:
    """
    Forced-Spur erkennen. Primär über das Disposition-Flag, ersatzweise über
    den Titel – manche Releases setzen das Flag nicht und schreiben die
    Kennzeichnung nur in den Namen ("German Forced", "Erzwungen").
    """
    if stream.get("disposition", {}).get("forced"):
        return True
    titel = (stream.get("tags", {}).get("title") or "").lower()
    return any(wort in titel for wort in ("forced", "erzwungen"))


def ermittle_untertitel_streams(ffprobe: Path, mkv_pfad: Path) -> list:
    """Alle Untertitel-Streams analysieren (braucht ffprobe)."""
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "s", str(mkv_pfad)
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        daten = json.loads(erg.stdout)
        return [
            {
                "index":    s.get("index", "?"),
                "codec":    s.get("codec_name", "unbekannt"),
                "language": s.get("tags", {}).get("language", "und"),
                "title":    s.get("tags", {}).get("title", ""),
                # Forced-Spuren zählen NICHT als Duplikat der Vollspur derselben
                # Sprache – sonst gewinnt die zuerst gelistete. Bei SNW S04E02
                # stand "German Forced" (472 Byte) vor "German" (48 KB) und
                # verdrängte die vollständige Spur komplett.
                "forced":   _ist_forced(s),
            }
            for s in daten.get("streams", [])
        ]
    except Exception:
        return []

def simuliere_streams_aus_nfo(nfo_pfad: Path) -> list:
    """
    Untertitelspuren aus NFO lesen (Simulationsmodus, kein ffprobe nötig).
    Codec wird als 'subrip' angenommen, da NFO keinen Codec speichert.
    """
    try:
        wurzel = ET.parse(nfo_pfad).getroot()
        spuren = []
        for sub in wurzel.findall("./fileinfo/streamdetails/subtitle"):
            lang = sub.findtext("language") or "und"
            spuren.append({
                "index":    len(spuren),
                "codec":    "subrip",
                "language": lang,
                "title":    "",
                # Die NFO speichert kein Forced-Flag – in der Simulation gilt
                # jede Spur als normal. Der Echtlauf liest es aus der MKV.
                "forced":   False,
            })
        return spuren
    except Exception:
        return []

def ermittle_hdrtype_aus_mkv(ffprobe: Path, mkv_pfad: Path):
    """HDR-Typ direkt aus der MKV-Datei lesen (via ffprobe).
    Gibt 'dolbyvision' zurück wenn Dolby Vision erkannt, sonst None.

    Erkennungs-Strategie (in Reihenfolge):
      Prüfung 1: side_data_list im Stream – DOVI/DOLBY-Eintrag oder
                 dv_profile-Schlüssel (ältere ffprobe liefern ihn direkt)
      Prüfung 2: Fallback über die Frame-Level-Analyse des ersten Frames
                 (-read_intervals, kein vollständiger Dekode)
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", str(mkv_pfad)
    ]
    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        daten = json.loads(erg.stdout)
        streams = daten.get("streams", [])
        if not streams:
            return None
        stream = streams[0]

        # Prüfung 1: side_data_list im Stream-Objekt
        for entry in stream.get("side_data_list", []):
            typ = str(entry.get("side_data_type", "")).upper()
            if "DOVI" in typ or "DOLBY" in typ:
                return "dolbyvision"
            # ältere ffprobe-Versionen liefern dv_profile direkt im Entry
            if "dv_profile" in entry:
                return "dolbyvision"

        # Prüfung 2: Fallback via Frame-Analyse (erstes Frame, kein vollständiger Dekode)
        befehl_frame = [
            str(ffprobe), "-v", "quiet", "-print_format", "json",
            "-read_intervals", "%+#1",
            "-show_frames", "-select_streams", "v:0", str(mkv_pfad)
        ]
        erg2 = subprocess.run(befehl_frame, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=30)
        if erg2.returncode == 0:
            frames = json.loads(erg2.stdout).get("frames", [])
            for frame in frames:
                for entry in frame.get("side_data_list", []):
                    typ = str(entry.get("side_data_type", "")).upper()
                    if "DOVI" in typ or "DOLBY" in typ:
                        return "dolbyvision"
                    if "dv_profile" in entry:
                        return "dolbyvision"
    except Exception:
        pass
    return None


def ermittle_dv_profil(ffprobe: Path, mkv_pfad: Path) -> tuple:
    """DV-Profil und Farbmatrix aus MKV ermitteln (via ffprobe).
    Gibt (dv_profil: int|None, farb_matrix: str|None) zurück.
    DV Profil 5 + ICtCp (IPT-PQ-C2) verursacht auf Geräten ohne nativen
    Dolby-Vision-Decoder einen typischen Farb-/Blaustich-Fehler.

    Erkennungs-Strategie (in Reihenfolge):
      1. Stream-Level side_data: dv_profile direkt
      2. Stream-Level side_data: dv_bl_signal_compatibility_id == 0 → Profil 5
         (compatibility_id 0 = kein HDR10-Fallback = typisch Profil 5 / ICtCp)
      3. Frame-Level-Fallback (wie ermittle_hdrtype_aus_mkv) für ältere ffprobe
    """
    befehl = [
        str(ffprobe), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", str(mkv_pfad)
    ]

    def _parse_dovi_entry(entry: dict) -> "int | None":
        """Gibt dv_profil aus einem DOVI-side_data-Eintrag zurück oder None."""
        if "dv_profile" in entry:
            try:
                return int(entry["dv_profile"])
            except (ValueError, TypeError):
                pass
        # dv_bl_signal_compatibility_id 0 = kein BL-Signal-Compatibility
        # → typisch für DV Profil 5 (ICtCp, kein HDR10-Fallback)
        if "dv_bl_signal_compatibility_id" in entry:
            try:
                if int(entry["dv_bl_signal_compatibility_id"]) == 0:
                    return 5
            except (ValueError, TypeError):
                pass
        return None

    try:
        erg = subprocess.run(befehl, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             check=True, timeout=60)
        daten = json.loads(erg.stdout)
        streams = daten.get("streams", [])
        if not streams:
            return None, None
        stream = streams[0]

        # Schritt 1+2: Stream-Level side_data
        dv_profil = None
        for entry in stream.get("side_data_list", []):
            typ = str(entry.get("side_data_type", "")).upper()
            if "DOVI" not in typ and "DOLBY" not in typ:
                continue
            dv_profil = _parse_dovi_entry(entry)
            if dv_profil is not None:
                break

        # Schritt 3: Frame-Level-Fallback wenn Stream-Level kein Ergebnis
        if dv_profil is None:
            befehl_f = [
                str(ffprobe), "-v", "quiet", "-print_format", "json",
                "-read_intervals", "%+#1",
                "-show_frames", "-select_streams", "v:0", str(mkv_pfad)
            ]
            erg_f = subprocess.run(befehl_f, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30)
            if erg_f.returncode == 0:
                for frame in json.loads(erg_f.stdout).get("frames", []):
                    for entry in frame.get("side_data_list", []):
                        typ = str(entry.get("side_data_type", "")).upper()
                        if "DOVI" not in typ and "DOLBY" not in typ:
                            continue
                        dv_profil = _parse_dovi_entry(entry)
                        if dv_profil is not None:
                            break
                    if dv_profil is not None:
                        break

        # Farbmatrix: alle relevanten Stream-Felder abfragen
        farb_matrix = (stream.get("color_space")
                       or stream.get("color_primaries")
                       or stream.get("color_transfer"))
        return dv_profil, farb_matrix
    except Exception:
        return None, None
