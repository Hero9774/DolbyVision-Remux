"""
dv_remux_gui.py  v5.5.0
=======================
GUI-Tool: Dolby Vision MKV → MP4 Remux + SRT Untertitel-Extraktion
Für Jellyfin / LG TV

Neu in v3:
  • NFO-Aktualisierung: original_filename .mkv→.mp4, Untertitel-Einträge
    werden durch die tatsächlich extrahierten SRT-Dateien ersetzt
  • Backup der originalen NFO als movie.nfo.bak vor jeder Änderung
  • XML-Struktur, Kommentare und tinyMediaManager-Metadaten bleiben erhalten

Neu in v5.0.1:
  • TrueHD-Fallback: MKVs mit TrueHD-Atmos-Track (nicht MP4-kompatibel)
    werden automatisch ohne den TrueHD-Stream wiederholt – der EAC3-Track
    bleibt erhalten. Kein manuelles Eingreifen nötig.

Neu in v5.0.2:
  • Schließen-Button (✖) + X-Button mit Sicherheitsabfrage wenn ein
    Prozess läuft; Config wird beim Beenden gespeichert.
  • Autoscroll-Toggle im Log-Bereich: deaktivierbar um während eines
    laufenden Prozesses im Log zu scrollen.

Neu in v5.0.3:
  • ✕-Button oben rechts in der Titelzeile (Schließen-Schutz).
  • Info-Button (ℹ) mit GitHub-Link und Kontaktadresse.
  • Hint-Texte anonymisiert (kein echter Filmname als Beispiel).
  • Sekundär-Buttons einheitlich größer.

Neu in v5.3.0:
  • dvcC-Box (Dolby Vision Configuration Record) direkt in die MP4 injiziert:
    Ohne dvcC erkennt Jellyfin / LG TV kein Dolby Vision im MP4-Container.
    Die Box wird nach dem Mux per Python-Binärpatch eingefügt (keine faststart-
    Verschiebung nötig, da moov am Dateiende liegt). ffprobe zeigt danach:
    dv_profile=8, dv_level=<auto>, dv_bl_signal_compatibility_id=1
  • 5-Schritt-Pipeline für P5→P8: [1/5] HEVC, [2/5] dovi_tool P5→P8,
    [3/5] MP4 (kein faststart), [4/5] dvcC injizieren, [5/5] faststart + mp42
  • Duplikate bei Untertiteln werden übersprungen (SKIP-Eintrag im Log)

Neu in v5.2.0:
  • DV Profil 5 → Profil 8 Konvertierung via dovi_tool (tools/dovi_tool.exe):
    Profil-5-MKVs (ICtCp/IPT-PQ-C2, typisch bei WEB-DL-Releases) verursachen
    Blaustich auf Geräten ohne nativen DV-Decoder. Das Tool wandelt das RPU
    automatisch zu Profil 8 (HDR10-kompatibel) um – 3-Schritt-Pipeline:
      1. HEVC extrahieren  2. dovi_tool P5→P8  3. MP4 zusammensetzen
    Wenn dovi_tool fehlt: Warnung im Log, normaler Remux läuft weiter.

Neu in v5.1.0:
  • old-MKV-Ziel wählbar: "Im Filmordner" (wie bisher) oder "Globaler Ordner"
    – bei globalem Ordner landen alle alten MKVs direkt im gewählten Pfad,
      kein "old MKV"-Unterordner mehr im Quellverzeichnis.

Voraussetzungen:
  - Python 3.8+  (tkinter ist im Lieferumfang von Python enthalten)
  - ffmpeg + ffprobe (https://ffmpeg.org/download.html)
    Im Simulationsmodus werden ffmpeg/ffprobe NICHT benötigt.
"""

import os
import sys
import json
import time
import queue
import shutil
import struct
import tempfile
import threading
import subprocess
import xml.etree.ElementTree as ET
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
#  KONSTANTEN
# ═══════════════════════════════════════════════════════════════════════════════

VERSION      = "5.5.0"
CONFIG_ORDNER = Path(__file__).parent / "config"
CONFIG_DATEI  = CONFIG_ORDNER / "dv_remux_config.json"
LOG_ORDNER    = Path(__file__).parent / "logs"
TEXT_CODECS   = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "srt"}
DOVI_TOOL     = Path(__file__).parent / "tools" / "dovi_tool.exe"

# Ordnerstruktur beim Start sicherstellen
for _d in (CONFIG_ORDNER, LOG_ORDNER, DOVI_TOOL.parent):
    _d.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  EINSTELLUNGEN
# ═══════════════════════════════════════════════════════════════════════════════

def config_laden() -> dict:
    if CONFIG_DATEI.exists():
        try:
            return json.loads(CONFIG_DATEI.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def config_speichern(daten: dict):
    try:
        CONFIG_DATEI.write_text(
            json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
#  KERN-LOGIK
# ═══════════════════════════════════════════════════════════════════════════════

def _bereinige_log(text: str) -> str:
    """Emojis/Sonderzeichen für Log-Datei bereinigen."""
    return (text.replace("✅","OK").replace("❌","FEHLER")
                .replace("⚠️","WARNUNG").replace("⚠","WARNUNG")
                .replace("📁","").replace("📺","")
                .replace("▶",">>").replace("📝","SRT:")
                .replace("🗑️","LOESCHEN:").replace("ℹ️","INFO:")
                .replace("🔍","ANALYSE:").replace("📋","")
                .replace("🔵","DV:").replace("📂","")
                .replace("📥","EMBED:").replace("📦","MOVE:")
                .replace("💾","BACKUP:").replace("🔬","SIM:")
                .replace("⏮","ROLLBACK:").replace("↩","UNDO:")
                .replace("→","->"))

def lese_hdrtype_aus_nfo(nfo_pfad: Path):
    """HDR-Typ aus movie.nfo lesen. Gibt z.B. 'dolbyvision' zurück."""
    try:
        wurzel = ET.parse(nfo_pfad).getroot()
        el = wurzel.find("./fileinfo/streamdetails/video/hdrtype")
        if el is not None and el.text:
            # Leerzeichen entfernen: "Dolby Vision" → "dolbyvision"
            return el.text.strip().lower().replace(" ", "")
    except ET.ParseError:
        pass
    return None

def finde_mkv(ordner: Path):
    """Erste .mkv-Datei im Ordner zurückgeben."""
    dateien = list(ordner.glob("*.mkv"))
    return dateien[0] if dateien else None

def ermittle_audio_streams(ffprobe: Path, mkv_pfad: Path) -> list:
    """Audio-Streams analysieren – gibt Liste mit index + codec_name zurück.
    Wird für den TrueHD-Fallback in remux_zu_mp4 benötigt: TrueHD ist im
    MP4-Container nicht erlaubt; mit den zurückgegebenen Indizes können
    inkompatible Tracks gezielt ausgelassen werden.
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
            {"index": s.get("index", "?"), "codec": s.get("codec_name", "unbekannt")}
            for s in daten.get("streams", [])
        ]
    except Exception:
        return []

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
            })
        return spuren
    except Exception:
        return []

def ermittle_hdrtype_aus_mkv(ffprobe: Path, mkv_pfad: Path):
    """HDR-Typ direkt aus der MKV-Datei lesen (via ffprobe).
    Gibt 'dolbyvision' zurück wenn Dolby Vision erkannt, sonst None.

    Erkennungs-Strategie (in Reihenfolge):
      1. side_data_list im Stream: DOVI- oder DOLBY-Eintrag
      2. side_data enthält dv_profile-Schlüssel (ältere ffprobe)
      3. Fallback: Frame-Level-Analyse des ersten Frames (-read_intervals)
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

    def _parse_dovi_entry(entry: dict) -> int | None:
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


def _berechne_dv_level(breite: int, hoehe: int, fps: float) -> int:
    """Bestimmt den DV-Level aus Videoauflösung und Bildrate (für dvcC-Box)."""
    px = breite * hoehe
    if px <= 1280 * 720:
        return 2 if fps > 24.5 else 1
    if px <= 1920 * 1080:
        if fps <= 24.5: return 3
        if fps <= 30.5: return 5
        return 6
    if px <= 2048 * 1080:
        return 7 if fps > 24.5 else 4
    if px <= 3840 * 2160:
        if fps <= 24.5: return 6
        if fps <= 30.5: return 8
        return 9
    return 9


def injiziere_dvcc_box(mp4_pfad: Path, dv_profil: int = 8,
                        dv_level: int = 6, compat_id: int = 1) -> bool:
    """
    Injiziert eine dvcC-Box (Dolby Vision Configuration Record) in eine MP4-Datei.
    Voraussetzung: moov am Ende der Datei (kein faststart-Modus).
    Navigiert moov→trak→mdia→minf→stbl→stsd→(dvh1/hvc1) und fügt dvcC nach
    hvcC ein. Aktualisiert nur die betroffenen Parent-Box-Größen; stco/co64
    bleiben unberührt (mdat liegt vor moov).
    """
    # 16-Byte dvcC aufbauen
    # Bit-Layout (48 Bit): profile(7)+level(6)+rpu(1)+el(1)+bl(1)+compat(4)+reserved(28)
    bits = dv_profil & 0x7F
    bits = (bits << 6) | (dv_level & 0x3F)
    bits = (bits << 3) | 0b101          # rpu=1, el=0, bl=1
    bits = (bits << 4) | (compat_id & 0xF)
    bits <<= 28                          # 28 reservierte Bits → 48 Bit gesamt
    dvcc = struct.pack(">I4sBB", 16, b"dvcC", 1, 0) + bits.to_bytes(6, "big")

    try:
        with open(mp4_pfad, "r+b") as f:
            datei_sz = f.seek(0, 2)
            f.seek(0)

            moov_off = moov_sz = None
            pos = 0
            while pos < datei_sz:
                f.seek(pos)
                kopf = f.read(8)
                if len(kopf) < 8:
                    break
                sz = struct.unpack(">I", kopf[:4])[0]
                typ = kopf[4:8]
                if sz == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    sz = struct.unpack(">Q", ext)[0]
                elif sz == 0:
                    sz = datei_sz - pos
                if sz < 8:
                    break
                if typ == b"moov":
                    moov_off, moov_sz = pos, sz
                pos += sz

            if moov_off is None:
                return False

            f.seek(moov_off)
            moov = bytearray(f.read(moov_sz))

        CONTAINER = {b"trak", b"mdia", b"minf", b"stbl", b"stsd"}
        HEVC_ENTRY = {b"dvh1", b"hvc1", b"dvhe", b"hev1"}
        n = len(moov)

        def _box(data, pos, ende):
            if pos + 8 > ende:
                return None
            sz = struct.unpack_from(">I", data, pos)[0]
            hdr = 8
            if sz == 1:
                if pos + 16 > ende:
                    return None
                sz = struct.unpack_from(">Q", data, pos + 8)[0]
                hdr = 16
            elif sz == 0:
                sz = ende - pos
            return pos, sz, bytes(data[pos + 4:pos + 8]), pos + hdr

        def _kinder(data, start, ende):
            p = start
            while p + 8 <= ende:
                b = _box(data, p, ende)
                if not b or b[1] < 8:
                    break
                yield b
                p += b[1]

        def _suche(data, start, ende):
            for off, sz, typ, ds in _kinder(data, start, ende):
                if typ in CONTAINER:
                    # stsd ist eine FullBox: 8 Byte extra (version+flags+entry_count)
                    kind_start = ds + (8 if typ == b"stsd" else 0)
                    pos_ins, eltern = _suche(data, kind_start, off + sz)
                    if pos_ins is not None:
                        return pos_ins, [off] + eltern
                elif typ in HEVC_ENTRY:
                    # hvcC per Signatur finden (VisualSampleEntry-Länge
                    # variiert je nach ffmpeg-Version → kein fixer Offset)
                    eintrag = bytes(data[ds:off + sz])
                    hvcc_rel = eintrag.find(b"hvcC")
                    if hvcc_rel >= 4:
                        hvcc_abs = ds + hvcc_rel - 4
                        hvcc_sz  = struct.unpack_from(">I", data, hvcc_abs)[0]
                        hat_dvcc = b"dvcC" in eintrag[hvcc_rel + hvcc_sz - 4:]
                        if 8 <= hvcc_sz <= (off + sz - hvcc_abs) and not hat_dvcc:
                            return hvcc_abs + hvcc_sz, [off]
            return None, []

        einfuege_pos, groessen_offs = _suche(moov, 8, n)
        if einfuege_pos is None:
            return False

        moov[einfuege_pos:einfuege_pos] = dvcc

        for soff in [0] + groessen_offs:
            alt = struct.unpack_from(">I", moov, soff)[0]
            struct.pack_into(">I", moov, soff, alt + 16)

        with open(mp4_pfad, "r+b") as f:
            f.seek(moov_off)
            f.write(moov)
            f.truncate(moov_off + len(moov))

        return True
    except Exception:
        return False


def _update_stco_co64(moov: bytearray, delta: int) -> None:
    """Addiert delta zu allen stco/co64-Chunk-Offsets im moov-Bytearray."""
    CONTAINER = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
    n = len(moov)

    def _visit(start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            bsz = struct.unpack_from(">I", moov, pos)[0]
            btyp = bytes(moov[pos + 4:pos + 8])
            hdr = 8
            if bsz == 1:
                if pos + 16 > end:
                    break
                bsz = struct.unpack_from(">Q", moov, pos + 8)[0]
                hdr = 16
            elif bsz == 0:
                bsz = end - pos
            if bsz < 8 or pos + bsz > end:
                break
            if btyp in CONTAINER:
                _visit(pos + hdr, pos + bsz)
            elif btyp == b"stco":
                # FullBox: version(1)+flags(3)+count(4) = 8 Byte vor Offsets
                count = struct.unpack_from(">I", moov, pos + hdr + 4)[0]
                off = pos + hdr + 8
                for _ in range(count):
                    old = struct.unpack_from(">I", moov, off)[0]
                    struct.pack_into(">I", moov, off, old + delta)
                    off += 4
            elif btyp == b"co64":
                count = struct.unpack_from(">I", moov, pos + hdr + 4)[0]
                off = pos + hdr + 8
                for _ in range(count):
                    old = struct.unpack_from(">Q", moov, off)[0]
                    struct.pack_into(">Q", moov, off, old + delta)
                    off += 8
            pos += bsz

    _visit(0, n)


def mache_faststart_und_ftyp(mp4_pfad: Path) -> bool:
    """
    Verschiebt moov vor mdat (faststart) und setzt major_brand auf mp42.
    Schreibt eine neue Datei (30-GB-Copy) — benötigt freien Speicherplatz.
    Gibt True zurück wenn erfolgreich oder moov bereits an erster Stelle.
    """
    BUF = 64 * 1024 * 1024  # 64 MB Lesepuffer

    try:
        with open(mp4_pfad, "rb") as f:
            total_sz = f.seek(0, 2)
            f.seek(0)

            # Top-Level-Boxen ermitteln
            ftyp_off = ftyp_sz = None
            mdat_off = mdat_sz = mdat_hdr = None
            moov_off = moov_sz = None
            pos = 0
            while pos < total_sz:
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8:
                    break
                bsz = struct.unpack(">I", h[:4])[0]
                btyp = h[4:8]
                hdr = 8
                if bsz == 1:
                    ext = f.read(8)
                    bsz = struct.unpack(">Q", ext)[0]
                    hdr = 16
                elif bsz == 0:
                    bsz = total_sz - pos
                if bsz < 8:
                    break
                if btyp == b"ftyp":
                    ftyp_off, ftyp_sz = pos, bsz
                elif btyp == b"mdat":
                    mdat_off, mdat_sz, mdat_hdr = pos, bsz, hdr
                elif btyp == b"moov":
                    moov_off, moov_sz = pos, bsz
                pos += bsz

            if None in (ftyp_off, mdat_off, moov_off):
                return False
            if moov_off < mdat_off:
                return True  # Bereits faststart

            # ftyp laden und major_brand auf mp42 patchen
            f.seek(ftyp_off)
            ftyp_data = bytearray(f.read(ftyp_sz))
            ftyp_data[8:12] = b"mp42"
            compat = [bytes(ftyp_data[i:i + 4]) for i in range(16, ftyp_sz, 4)]
            if b"mp42" not in compat:
                ftyp_data += b"mp42"
                struct.pack_into(">I", ftyp_data, 0, ftyp_sz + 4)
                new_ftyp_sz = ftyp_sz + 4
            else:
                new_ftyp_sz = ftyp_sz

            # moov laden
            f.seek(moov_off)
            moov_data = bytearray(f.read(moov_sz))

        # stco/co64 anpassen:
        # Alt: mdat-Daten bei mdat_off + mdat_hdr
        # Neu: mdat-Daten bei new_ftyp_sz + moov_sz + mdat_hdr
        old_data_off = mdat_off + mdat_hdr
        new_data_off = new_ftyp_sz + moov_sz + mdat_hdr
        _update_stco_co64(moov_data, new_data_off - old_data_off)

        tmp_pfad = mp4_pfad.with_name(mp4_pfad.stem + "._fstmp.mp4")
        try:
            with open(mp4_pfad, "rb") as fin, open(tmp_pfad, "wb") as fout:
                fout.write(ftyp_data)
                fout.write(moov_data)
                fin.seek(mdat_off)
                verbleibend = mdat_sz
                while verbleibend > 0:
                    chunk = fin.read(min(verbleibend, BUF))
                    if not chunk:
                        break
                    fout.write(chunk)
                    verbleibend -= len(chunk)

            mp4_pfad.unlink()
            tmp_pfad.rename(mp4_pfad)
            return True
        except Exception:
            if tmp_pfad.exists():
                tmp_pfad.unlink(missing_ok=True)
            raise

    except Exception:
        return False


def konvertiere_dv_p5_zu_p8(
        ffmpeg: Path, dovi_tool: Path, ffprobe: Path,
        mkv_pfad: Path, mp4_pfad: Path,
        log_q: queue.Queue, task_q: queue.Queue,
        simulation: bool, log_zeilen: list,
        stopp_event=None) -> bool:
    """
    DV Profil 5 (ICtCp) → Profil 8.1 (HDR10-kompatibel) ohne Re-Encoding:
      Schritt 1: HEVC-Stream extrahieren (ffmpeg -c:v copy)
      Schritt 2: RPU konvertieren P5→P8.1 (dovi_tool -m 3 convert)
      Schritt 3: MP4 zusammensetzen (P8-HEVC + Audio aus Original-MKV, kein faststart)
      Schritt 4: dvcC-Box injizieren (Dolby Vision Configuration Record)
      Schritt 5: faststart – moov vor mdat schieben, major_brand mp42
    Temp-Dateien werden in jedem Fall bereinigt (auch bei Fehler).
    """
    if simulation:
        msg = (f"  [SIM] P5->P8-Konvertierung wuerde stattfinden:\n"
               f"    {mkv_pfad.name}\n    -> {mp4_pfad.name}")
        log_q.put(("SIM",
            f"  [SIM] P5→P8-Konvertierung würde stattfinden:\n"
            f"    {mkv_pfad.name}\n    → {mp4_pfad.name}"))
        log_zeilen.append(msg)
        for p in range(0, 101, 5):
            task_q.put({"sub_prog": p})
            time.sleep(0.04)
        return True

    tmp_dir     = Path(tempfile.gettempdir())
    tmp_hevc    = tmp_dir / f"_dv_remux_{mp4_pfad.stem}.hevc"
    tmp_hevc_p8 = tmp_dir / f"_dv_remux_{mp4_pfad.stem}_p8.hevc"

    def cleanup():
        for tmp in (tmp_hevc, tmp_hevc_p8):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    proc = None
    try:
        # ── Schritt 1: HEVC extrahieren ────────────────────────────────────
        task_q.put({"schritt": "P5→P8 [1/5]: HEVC extrahieren …", "sub_prog": 0})
        log_q.put(("INFO", "  🔬  [1/5] HEVC-Stream extrahieren …"))
        log_zeilen.append("  [1/5] HEVC extrahieren")

        befehl1 = [str(ffmpeg), "-i", str(mkv_pfad),
                   "-c:v", "copy", "-an", "-sn", "-y", str(tmp_hevc)]
        proc = subprocess.Popen(befehl1, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        dauer_sek = None
        for zeile in proc.stderr:
            if stopp_event and stopp_event.is_set():
                proc.terminate(); proc.wait(); cleanup()
                log_q.put(("WARN", "  ⚠  P5→P8 abgebrochen."))
                log_zeilen.append("  ABGEBROCHEN: P5->P8")
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
            log_q.put(("ERR", "  ❌ [1/5] HEVC-Extraktion fehlgeschlagen."))
            log_zeilen.append("  FEHLER: HEVC-Extraktion")
            cleanup(); return False
        task_q.put({"sub_prog": 25})
        log_q.put(("OK", "     ✅ HEVC extrahiert"))
        log_zeilen.append("     OK: HEVC extrahiert")

        # ── Schritt 2: RPU P5 → P8.1 konvertieren ─────────────────────────
        task_q.put({"schritt": "P5→P8 [2/5]: RPU konvertieren …", "sub_prog": 25})
        log_q.put(("INFO", "  🔬  [2/5] RPU Profil 5 → 8.1 (dovi_tool) …"))
        log_zeilen.append("  [2/5] dovi_tool P5->P8")

        befehl2 = [str(dovi_tool), "-m", "3", "convert",
                   str(tmp_hevc), "-o", str(tmp_hevc_p8)]
        erg2 = subprocess.run(befehl2, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        if erg2.returncode != 0:
            fehler = (erg2.stderr or erg2.stdout or "").strip()[-300:]
            log_q.put(("ERR", f"  ❌ dovi_tool Fehler (Code {erg2.returncode}):\n     {fehler}"))
            log_zeilen.append(f"  FEHLER: dovi_tool Code {erg2.returncode}: {fehler}")
            cleanup(); return False
        task_q.put({"sub_prog": 50})
        log_q.put(("OK", "     ✅ RPU zu Profil 8.1 konvertiert"))
        log_zeilen.append("     OK: RPU Profil 8")

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
        task_q.put({"schritt": "P5→P8 [3/5]: MP4 zusammensetzen …", "sub_prog": 50})
        log_q.put(("INFO", f"  🔬  [3/5] MP4 zusammensetzen → {mp4_pfad.name}"))
        log_zeilen.append(f"  [3/5] MP4 zusammensetzen: {mp4_pfad.name}")

        # Audio: alle kompatiblen Spuren aus Original-MKV (TrueHD ausschließen)
        alle_audio = ermittle_audio_streams(ffprobe, mkv_pfad)
        kompatible = [s["index"] for s in alle_audio if s["codec"] != "truehd"]
        audio_indizes = kompatible if kompatible else [s["index"] for s in alle_audio]
        if len(kompatible) < len(alle_audio):
            ausgelassen = len(alle_audio) - len(kompatible)
            log_q.put(("WARN", f"  ⚠  {ausgelassen} TrueHD-Spur(en) ausgelassen (nicht MP4-kompatibel)"))
            log_zeilen.append(f"  WARNUNG: {ausgelassen} TrueHD-Spur(en) ausgelassen")

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
                log_q.put(("WARN", "  ⚠  P5→P8 abgebrochen."))
                log_zeilen.append("  ABGEBROCHEN: P5->P8 Schritt 3")
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
            log_q.put(("ERR", "  ❌ [3/5] MP4-Zusammensetzen fehlgeschlagen."))
            log_zeilen.append("  FEHLER: MP4-Zusammensetzen")
            return False

        # ── Schritt 4: dvcC-Box injizieren ────────────────────────────────
        task_q.put({"schritt": "P5→P8 [4/5]: dvcC-Box injizieren …", "sub_prog": 93})
        log_q.put(("INFO", f"  🔬  [4/5] dvcC-Box (Profil 8.1, Level {dv_level}) injizieren …"))
        log_zeilen.append("  [4/5] dvcC-Box injizieren")
        if not injiziere_dvcc_box(mp4_pfad, dv_profil=8, dv_level=dv_level, compat_id=1):
            log_q.put(("ERR", "  ❌ [4/5] dvcC-Box konnte nicht injiziert werden."))
            log_zeilen.append("  FEHLER: dvcC-Injektion")
            return False
        log_q.put(("OK", "     ✅ dvcC-Box gesetzt (DV Profil 8.1, HDR10-kompatibel)"))
        log_zeilen.append("     OK: dvcC gesetzt")

        # ── Schritt 5: faststart – moov vor mdat schieben ─────────────────
        task_q.put({"schritt": "P5→P8 [5/5]: faststart (moov → Dateianfang) …", "sub_prog": 96})
        log_q.put(("INFO", "  🔬  [5/5] faststart: moov vor mdat verschieben …"))
        log_zeilen.append("  [5/5] faststart")
        if mache_faststart_und_ftyp(mp4_pfad):
            task_q.put({"sub_prog": 100})
            log_q.put(("OK", "     ✅ MP4 fertig (DV P8.1, dvcC, faststart, mp42)"))
            log_zeilen.append("     OK: MP4 fertig (DV Profil 8)")
            return True
        else:
            # faststart-Fehler ist nicht fatal — dvcC ist bereits gesetzt
            log_q.put(("WARN", "  ⚠  [5/5] faststart fehlgeschlagen (kein freier Speicher?). MP4 trotzdem nutzbar."))
            log_zeilen.append("  WARNUNG: faststart fehlgeschlagen")
            return True

    except Exception as e:
        log_q.put(("ERR", f"  ❌ P5→P8-Fehler: {e}"))
        log_zeilen.append(f"  FEHLER P5->P8: {e}")
        return False
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate(); proc.wait()
        cleanup()


def extrahiere_untertitel(ffmpeg: Path, mkv_pfad: Path, streams: list,
                           log_q: queue.Queue, task_q: queue.Queue,
                           simulation: bool, log_zeilen: list,
                           undo_log: list = None) -> list:
    """
    Text-Untertitelspuren als .srt extrahieren.
    Gibt Liste der erfolgreich erstellten/vorhandenen SRT-Pfade zurück
    (wird von aktualisiere_nfo() benötigt).
    """
    basis = mkv_pfad.with_suffix("")
    sprachzähler = {}
    text_streams   = [s for s in streams if s["codec"] in TEXT_CODECS]
    bitmap_streams = [s for s in streams if s["codec"] not in TEXT_CODECS]
    erstellte_srts = []   # ← Rückgabe-Liste

    for s in bitmap_streams:
        msg = (f"  ⚠  Untertitel #{s['index']} [{s['language']}] "
               f"ist {s['codec'].upper()} (Bitmap) -> kein SRT moeglich")
        log_q.put(("SKIP", msg.replace("->","→")))
        log_zeilen.append(msg)

    total = len(text_streams)
    for i, stream in enumerate(text_streams):
        sprache = stream["language"]
        idx     = stream["index"]
        sprachzähler[sprache] = sprachzähler.get(sprache, 0) + 1
        if sprachzähler[sprache] > 1:
            log_q.put(("SKIP", f"  ⚠  Duplikat [{sprache}] Spur #{idx} übersprungen"))
            log_zeilen.append(f"  SKIP: Duplikat [{sprache}] Spur #{idx} uebersprungen")
            continue
        srt_pfad = basis.parent / (basis.name + f".{sprache}.srt")

        task_q.put({
            "schritt":  f"SRT: {srt_pfad.name}",
            "sub_prog": int(i / total * 100) if total else 0
        })

        if srt_pfad.exists():
            log_q.put(("INFO", f"  ℹ️  SRT vorhanden: {srt_pfad.name}"))
            log_zeilen.append(f"  INFO: SRT vorhanden: {srt_pfad.name}")
            erstellte_srts.append(srt_pfad)
            continue

        if simulation:
            log_q.put(("SIM", f"  [SIM] SRT würde erstellt: {srt_pfad.name}"))
            log_zeilen.append(f"  [SIM] SRT wuerde erstellt: {srt_pfad.name}")
            erstellte_srts.append(srt_pfad)  # auch im Sim merken für NFO-Update
            continue

        log_q.put(("INFO", f"  📝  {srt_pfad.name}"))
        log_zeilen.append(f"  SRT: {srt_pfad.name}")
        befehl = [
            str(ffmpeg), "-i", str(mkv_pfad),
            "-map", f"0:{idx}", "-c:s", "srt", "-y", str(srt_pfad)
        ]
        try:
            subprocess.run(befehl, capture_output=True, check=True)
            log_q.put(("OK",  f"     ✅ {srt_pfad.name}"))
            log_zeilen.append(f"     OK: {srt_pfad.name}")
            erstellte_srts.append(srt_pfad)
            if undo_log is not None:
                undo_log.append({"typ": "srt", "pfad": srt_pfad})
        except subprocess.CalledProcessError:
            log_q.put(("ERR", f"     ❌ Fehler: {srt_pfad.name}"))
            log_zeilen.append(f"     FEHLER: {srt_pfad.name}")

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
    task_q.put({"schritt": "NFO wird aktualisiert …", "sub_prog": None})
    log_q.put(("INFO", "  📝  NFO-Update …"))

    # ── Backup erstellen ──────────────────────────────────────────────────
    bak_pfad = nfo_pfad.with_suffix(".nfo.bak")
    if not bak_pfad.exists():
        if simulation:
            log_q.put(("SIM", f"  [SIM] Backup würde erstellt: {bak_pfad.name}"))
            log_zeilen.append(f"  [SIM] Backup: {bak_pfad.name}")
        else:
            try:
                shutil.copy2(nfo_pfad, bak_pfad)
                log_q.put(("OK", f"     💾 Backup: {bak_pfad.name}"))
                log_zeilen.append(f"     Backup: {bak_pfad.name}")
            except Exception as e:
                log_q.put(("ERR", f"     ❌ Backup fehlgeschlagen – NFO-Update abgebrochen: {e}"))
                log_zeilen.append(f"     FEHLER Backup: {e}")
                task_q.put({"schritt": "NFO-Update abgebrochen", "sub_prog": 100})
                return
    else:
        log_q.put(("INFO", f"     ℹ️  Backup bereits vorhanden: {bak_pfad.name}"))
        log_zeilen.append(f"     Backup vorhanden: {bak_pfad.name}")

    # ── XML parsen ────────────────────────────────────────────────────────
    try:
        # Rohtext aufbewahren um Kommentare später wieder einzufügen
        rohtext = nfo_pfad.read_text(encoding="utf-8")

        # ET-Parser ohne Namespace-Probleme
        baum = ET.parse(nfo_pfad)
        wurzel = baum.getroot()
    except Exception as e:
        log_q.put(("ERR", f"     ❌ NFO kann nicht geparst werden: {e}"))
        log_zeilen.append(f"     FEHLER NFO-Parse: {e}")
        return

    aenderungen = []

    # ── 1. original_filename aktualisieren ────────────────────────────────
    el_fn = wurzel.find("original_filename")
    if el_fn is not None and el_fn.text:
        alt = el_fn.text.strip()
        neu = mp4_pfad.name
        if alt != neu:
            el_fn.text = neu
            aenderungen.append(f"original_filename: {alt} → {neu}")
            log_q.put(("OK",
                f"     ✅ original_filename: {alt} → {neu}"))
            log_zeilen.append(
                f"     original_filename: {alt} -> {neu}")

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
            f"subtitle-Einträge: {anzahl_alt} (alt) → {neue_subs_eingefuegt} SRT (neu)")
        log_q.put(("OK",
            f"     ✅ Untertitel: {anzahl_alt} Spuren → "
            f"{neue_subs_eingefuegt} SRT-Einträge"))
        log_zeilen.append(
            f"     Subtitle: {anzahl_alt} -> {neue_subs_eingefuegt} SRT")

    elif streamdetails is not None and not srt_dateien:
        log_q.put(("INFO", "     ℹ️  Keine SRT-Dateien → Untertitel-Einträge unverändert"))

    # ── Änderungen zusammenfassen ─────────────────────────────────────────
    if not aenderungen:
        log_q.put(("INFO", "     ℹ️  NFO bereits aktuell – keine Änderungen nötig."))
        log_zeilen.append("     NFO bereits aktuell.")
        task_q.put({"schritt": "NFO aktuell", "sub_prog": 100})
        return

    # ── XML zurückschreiben ───────────────────────────────────────────────
    if simulation:
        for a in aenderungen:
            log_q.put(("SIM", f"  [SIM] NFO-Änderung: {a}"))
            log_zeilen.append(f"  [SIM] NFO: {a}")
        task_q.put({"schritt": "NFO (Simulation)", "sub_prog": 100})
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

        log_q.put(("OK", f"     ✅ NFO gespeichert: {nfo_pfad.name}"))
        log_zeilen.append(f"     NFO gespeichert: {nfo_pfad.name}")

    except AttributeError:
        # ET.indent nicht verfügbar (Python < 3.9) → ohne Einrückung
        xml_inhalt = ET.tostring(wurzel, encoding="unicode", xml_declaration=False)
        gesamt = deklaration + tmm_kommentar + xml_inhalt + "\n"
        nfo_pfad.write_text(gesamt, encoding="utf-8")
        if undo_log is not None:
            undo_log.append({"typ": "nfo", "nfo": nfo_pfad, "bak": bak_pfad})
        log_q.put(("OK", f"     ✅ NFO gespeichert (ohne Einrückung): {nfo_pfad.name}"))
        log_zeilen.append(f"     NFO gespeichert: {nfo_pfad.name}")

    except Exception as e:
        log_q.put(("ERR", f"     ❌ NFO-Schreib-Fehler: {e}"))
        log_zeilen.append(f"     FEHLER NFO-Schreiben: {e}")

    task_q.put({"schritt": "NFO aktualisiert", "sub_prog": 100})

def _dvcc_vorhanden(mp4_pfad: Path) -> bool:
    """Prüft ob eine dvcC-Box in der MP4-Datei vorhanden ist (schnelle Byte-Suche im moov)."""
    try:
        with open(mp4_pfad, "rb") as f:
            total = f.seek(0, 2); f.seek(0)
            pos = 0
            while pos < total:
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8: break
                bsz = struct.unpack(">I", h[:4])[0]
                btyp = h[4:8]
                hdr = 8
                if bsz == 1:
                    ext = f.read(8); bsz = struct.unpack(">Q", ext)[0]; hdr = 16
                elif bsz == 0: bsz = total - pos
                if bsz < 8: break
                if btyp == b"moov":
                    daten = f.read(bsz - hdr)
                    return b"dvcC" in daten
                pos += bsz
    except Exception:
        pass
    return False


def nachbearbeite_dv_mp4(mp4_pfad: Path, log_q: queue.Queue,
                          log_zeilen: list, simulation: bool) -> None:
    """
    Nach normalem DV-Remux (ohne faststart): dvcC prüfen/injizieren,
    dann moov nach vorne schieben (faststart) und ftyp auf mp42 setzen.
    """
    if simulation:
        return
    hat_dvcc = _dvcc_vorhanden(mp4_pfad)
    if not hat_dvcc:
        log_q.put(("INFO", "  🔬  dvcC-Box fehlt – wird injiziert …"))
        log_zeilen.append("  dvcC-Box injizieren")
        if injiziere_dvcc_box(mp4_pfad):
            log_q.put(("OK", "     ✅ dvcC-Box gesetzt"))
            log_zeilen.append("     OK: dvcC gesetzt")
        else:
            log_q.put(("WARN", "  ⚠  dvcC-Injektion fehlgeschlagen"))
            log_zeilen.append("  WARNUNG: dvcC-Injektion fehlgeschlagen")
    else:
        log_q.put(("INFO", "  ✅ dvcC-Box bereits vorhanden"))
        log_zeilen.append("  dvcC: bereits vorhanden")

    log_q.put(("INFO", "  🔬  faststart: moov → Dateianfang, mp42 …"))
    log_zeilen.append("  faststart + mp42")
    if mache_faststart_und_ftyp(mp4_pfad):
        log_q.put(("OK", "     ✅ faststart OK (mp42)"))
        log_zeilen.append("     OK: faststart mp42")
    else:
        log_q.put(("WARN", "  ⚠  faststart fehlgeschlagen (MP4 trotzdem nutzbar)"))
        log_zeilen.append("  WARNUNG: faststart fehlgeschlagen")


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
        embed_info = (f" + {len(text_sub_indices)} Sub(s)" if text_sub_indices else "")
        msg = (f"  [SIM] Remux wuerde stattfinden:\n"
               f"    {mkv_pfad.name}\n    -> {mp4_pfad.name}{embed_info}")
        log_q.put(("SIM",
            f"  [SIM] Remux würde stattfinden:\n"
            f"    {mkv_pfad.name}\n    → {mp4_pfad.name}{embed_info}"))
        log_zeilen.append(msg)
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

    msg = f"  Remux: {mkv_pfad.name} -> {mp4_pfad.name}"
    log_q.put(("INFO", f"  ▶  Remux: {mkv_pfad.name} → {mp4_pfad.name}"))
    log_zeilen.append(msg)

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
                log_q.put(("WARN", "  ⚠  Remux abgebrochen."))
                log_zeilen.append("  ABGEBROCHEN: Remux")
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
            log_q.put(("ERR", "  ── ffmpeg stderr (letzte Zeilen) ──"))
            for z in stderr_zeilen[-15:]:
                if z.strip():
                    log_q.put(("ERR", f"     {z}"))
                    log_zeilen.append(f"     {z}")

            if text_sub_indices:
                # Sub-Einbettung fehlgeschlagen → Wiederholung ohne Untertitel
                log_q.put(("WARN",
                    "  ⚠  Sub-Einbettung fehlgeschlagen – Wiederholung ohne eingebettete Untertitel …"))
                log_zeilen.append("  WARNUNG: Sub-Einbettung fehlgeschlagen, Retry ohne Subs")
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
                    log_q.put(("WARN",
                        "  ⚠  TrueHD-Spur ist nicht MP4-kompatibel – "
                        f"Wiederholung ohne TrueHD ({len(alle_audio) - len(kompatibel)} Spur(en) ausgelassen) …"))
                    log_zeilen.append("  WARNUNG: TrueHD ausgelassen, Retry ohne TrueHD")
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
            log_q.put(("ERR", f"  ❌ ffmpeg Fehler (Code {rc})"))
            log_zeilen.append(f"  FEHLER: ffmpeg Code {rc}")
            return False
    except FileNotFoundError:
        log_q.put(("ERR", "  ❌ ffmpeg nicht gefunden!"))
        log_zeilen.append("  FEHLER: ffmpeg nicht gefunden")
        return False
    except Exception as e:
        log_q.put(("ERR", f"  ❌ Unerwarteter Fehler: {e}"))
        log_zeilen.append(f"  FEHLER: {e}")
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
        kopf  = [
            f"DV Remux Tool v{VERSION}",
            f"Datum:  {ts.strftime('%d.%m.%Y %H:%M:%S')}",
            f"Modus:  {'SIMULATION' if simulation else 'ECHTLAUF'}",
            "=" * 55, ""
        ]
        pfad.write_text("\n".join(kopf + log_zeilen), encoding="utf-8")
        return pfad
    except Exception as e:
        # Fallback: Log-Pfad trotzdem zurückgeben, damit GUI nicht crasht
        fallback = LOG_ORDNER / "dv_remux_error.log"
        try:
            fallback.write_text(f"Log konnte nicht gespeichert werden: {e}", encoding="utf-8")
        except Exception:
            pass
        return fallback

def verschiebe_oder_loesche_mkv(mkv_pfad: Path, original_behalten: bool,
                                simulation: bool, log_func,
                                undo_log: list = None,
                                old_mkv_global_pfad: Path = None) -> None:
    """MKV nach erfolgreichem Remux verschieben oder löschen."""
    if original_behalten:
        if old_mkv_global_pfad:
            ziel_ordner = old_mkv_global_pfad
            ziel_label  = str(old_mkv_global_pfad / mkv_pfad.name)
        else:
            ziel_ordner = mkv_pfad.parent / "old MKV"
            ziel_label  = f"old MKV/{mkv_pfad.name}"
        ziel_pfad = ziel_ordner / mkv_pfad.name
        if simulation:
            log_func("SIM", f"  [SIM] Würde verschieben → {ziel_label}")
        elif ziel_pfad.exists():
            log_func("WARN", f"  ⚠  {mkv_pfad.name} bereits vorhanden – übersprungen.")
        else:
            try:
                ziel_ordner.mkdir(parents=True, exist_ok=True)
                shutil.move(str(mkv_pfad), str(ziel_pfad))
                log_func("OK", f"  📦  Verschoben → {ziel_label}")
                if undo_log is not None:
                    undo_log.append({"typ": "mkv_move",
                                     "von": ziel_pfad, "nach": mkv_pfad})
            except Exception as e:
                log_func("ERR", f"  ❌ Verschieben fehlgeschlagen: {e}")
    elif simulation:
        log_func("SIM", f"  [SIM] Würde gelöscht: {mkv_pfad.name}")
    else:
        try:
            mkv_pfad.unlink()
            log_func("INFO", f"  🗑️  Gelöscht: {mkv_pfad.name}")
            if undo_log is not None:
                undo_log.append({"typ": "mkv_del", "pfad": mkv_pfad})
        except Exception as e:
            log_func("ERR", f"  ❌ Löschen fehlgeschlagen: {e}")


def rollback_session(undo_log: list, log_func, task_q: queue.Queue):
    """Alle protokollierten Operationen der Session rückgängig machen (LIFO)."""
    if not undo_log:
        log_func("INFO", "\n  ℹ️  Rollback: Nichts zu rückgängig zu machen.")
        return

    log_func("WARN", "\n⏮  Rollback wird durchgeführt …")
    task_q.put({"schritt": "Rollback läuft …", "sub_prog": None})

    for eintrag in reversed(undo_log):
        typ = eintrag["typ"]

        if typ == "mp4":
            pfad = eintrag["pfad"]
            try:
                if pfad.exists():
                    pfad.unlink()
                    log_func("OK", f"  ↩  MP4 gelöscht: {pfad.name}")
                else:
                    log_func("INFO", f"  ℹ️  MP4 nicht mehr vorhanden: {pfad.name}")
            except Exception as e:
                log_func("ERR", f"  ❌ MP4-Löschen fehlgeschlagen: {e}")

        elif typ == "mkv_move":
            von  = eintrag["von"]   # aktueller Pfad (in "old MKV")
            nach = eintrag["nach"]  # ursprünglicher Pfad
            try:
                if von.exists():
                    shutil.move(str(von), str(nach))
                    log_func("OK", f"  ↩  MKV zurückbewegt: {nach.name}")
                else:
                    log_func("WARN", f"  ⚠  MKV nicht mehr in old MKV: {von.name}")
            except Exception as e:
                log_func("ERR", f"  ❌ MKV-Rückbewegung fehlgeschlagen: {e}")

        elif typ == "mkv_del":
            pfad = eintrag["pfad"]
            log_func("WARN", f"  ⚠  Gelöschte MKV nicht wiederherstellbar: {pfad.name}")

        elif typ == "nfo":
            nfo = eintrag["nfo"]
            bak = eintrag["bak"]
            try:
                if bak.exists():
                    shutil.copy2(str(bak), str(nfo))
                    bak.unlink()
                    log_func("OK", f"  ↩  NFO wiederhergestellt: {nfo.name}")
                else:
                    log_func("WARN", f"  ⚠  NFO-Backup nicht vorhanden: {bak.name}")
            except Exception as e:
                log_func("ERR", f"  ❌ NFO-Wiederherstellung fehlgeschlagen: {e}")

        elif typ == "srt":
            pfad = eintrag["pfad"]
            try:
                if pfad.exists():
                    pfad.unlink()
                    log_func("OK", f"  ↩  SRT gelöscht: {pfad.name}")
            except Exception as e:
                log_func("ERR", f"  ❌ SRT-Löschen fehlgeschlagen: {e}")

    log_func("OK", "\n  ✅ Rollback abgeschlossen.")
    task_q.put({"schritt": "Rollback abgeschlossen", "sub_prog": 100})


def verarbeite_serien(
        ffmpeg_pfad: str, ffprobe_pfad: str, root_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None):
    """Serien-Worker: root → Show-Ordner → Staffel-Ordner → episode.mkv"""

    ffmpeg     = Path(ffmpeg_pfad)
    ffprobe    = Path(ffprobe_pfad)
    root       = Path(root_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = "SIMULATION" if simulation else "ECHTLAUF"
    log("HEAD", f"{'='*55}")
    log("HEAD", f"  DV Remux Tool v{VERSION}  -  {modus_text}  [SERIEN]")
    log("HEAD", f"  Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    log("HEAD", f"  Root:  {root}")
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
            log("WARN", "\n⚠  Verarbeitung vom Benutzer abgebrochen.")
            break

        fort_q.put(int(i / gesamt * 100) if gesamt else 0)
        log("FOLDER", f"\n📺  {show_ordner.name}")

        staffeln = sorted([p for p in show_ordner.iterdir() if ist_kein_trickplay(p)])
        if not staffeln:
            staffeln = [show_ordner]

        for staffel in staffeln:
            if stopp_event and stopp_event.is_set():
                break
            if staffel != show_ordner:
                log("INFO", f"  📂  {staffel.name}")

            for mkv_pfad in sorted(staffel.glob("*.mkv")):
                if stopp_event and stopp_event.is_set():
                    break

                anzeige = (f"{show_ordner.name}  /  {staffel.name}  /  {mkv_pfad.stem}"
                           if staffel != show_ordner
                           else f"{show_ordner.name}  /  {mkv_pfad.stem}")
                task_q.put({"film": anzeige, "schritt": "HDR-Typ wird ermittelt …",
                            "sub_prog": None})

                nfo_pfad = mkv_pfad.with_suffix(".nfo")
                mp4_pfad = mkv_pfad.with_suffix(".mp4")

                # HDR-Typ ermitteln: NFO zuerst, ffprobe als Fallback
                hdrtype = lese_hdrtype_aus_nfo(nfo_pfad) if nfo_pfad.exists() else None
                if hdrtype:
                    log("SIM" if simulation else "INFO",
                        f"    {'[SIM] ' if simulation else ''}HDR-Typ (NFO): {hdrtype}")
                else:
                    hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
                    log("SIM" if simulation else "INFO",
                        f"    {'[SIM] ' if simulation else ''}HDR-Typ (ffprobe): {hdrtype or '(nicht erkannt)'}")
                if hdrtype != "dolbyvision":
                    log("SKIP", f"    ℹ️  {mkv_pfad.name}: kein DV – übersprungen.")
                    stats["uebersprungen"] += 1
                    task_q.put({"schritt": "Übersprungen (kein DV)", "sub_prog": 100})
                    continue

                stats["gefunden"] += 1
                log("INFO", f"    🔵  {mkv_pfad.name}  [Dolby Vision]")

                # DV-Profil-Prüfung: Profil 5 (ICtCp) → P5→P8-Konvertierung oder Warnung
                braucht_p5_fix = False
                dv_profil, farb_matrix = ermittle_dv_profil(ffprobe, mkv_pfad)
                _ictcp = farb_matrix and any(kw in str(farb_matrix).lower()
                                    for kw in ("ictcp", "ipt-pq", "ipt_pq"))
                if dv_profil == 5 or _ictcp:
                    braucht_p5_fix = True
                    if DOVI_TOOL.exists():
                        log("WARN", (f"    ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                                      "– wird zu Profil 8 konvertiert (dovi_tool) …"))
                    else:
                        log("WARN", (f"    ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                                      "– dovi_tool nicht gefunden, Farbfehler im Output möglich!"))
                        log("WARN",  "        dovi_tool.exe in tools/ ablegen um die Konvertierung zu aktivieren.")

                # Untertitel-Streams ermitteln (vor Remux)
                streams = []
                if embed_subs or untertitel:
                    task_q.put({"schritt": "Untertitel analysieren …", "sub_prog": None})
                    log("INFO", "    🔍 Analysiere Untertitel-Streams …")
                    if simulation:
                        streams = simuliere_streams_aus_nfo(nfo_pfad)
                        log("SIM", f"    [SIM] {len(streams)} Spur(en) laut NFO")
                    else:
                        streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
                    if streams:
                        log("INFO", f"    📋 {len(streams)} Spur(en) gefunden")

                text_sub_indices = None
                if embed_subs and streams:
                    eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
                    bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
                    if eng_subs:
                        text_sub_indices = [s["index"] for s in eng_subs]
                        log("INFO", f"    📥  {len(eng_subs)} Englischer Untertitel wird eingebettet")
                    for s in bitmap_subs:
                        log("SKIP", f"    ⚠  Bitmap-Sub #{s['index']} [{s['language']}] "
                                    f"({s['codec'].upper()}) kann nicht eingebettet werden")

                # Remux (normaler Pfad) oder P5→P8-Konvertierung
                neu_remuxed = False
                if mp4_pfad.exists():
                    log("SKIP", "    ✅ MP4 bereits vorhanden – übersprungen.")
                    stats["uebersprungen"] += 1
                    task_q.put({"schritt": "MP4 bereits vorhanden", "sub_prog": 100})
                else:
                    task_q.put({"schritt": "Remux läuft …", "sub_prog": None})
                    if braucht_p5_fix and DOVI_TOOL.exists():
                        task_q.put({"schritt": "P5→P8-Konvertierung läuft …", "sub_prog": None})
                        erfolg = konvertiere_dv_p5_zu_p8(
                            ffmpeg, DOVI_TOOL, ffprobe,
                            mkv_pfad, mp4_pfad,
                            log_q, task_q, simulation, log_zeilen,
                            stopp_event=stopp_event)
                    else:
                        erfolg = remux_zu_mp4(
                            ffmpeg, mkv_pfad, mp4_pfad,
                            log_q, task_q, simulation, log_zeilen,
                            stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                            ffprobe_pfad=ffprobe, kein_faststart=True)
                        if erfolg:
                            nachbearbeite_dv_mp4(mp4_pfad, log_q, log_zeilen, simulation)
                    if erfolg:
                        stats["remuxed"] += 1
                        neu_remuxed = True
                        log("OK", "    ✅ Remux erfolgreich!")
                        task_q.put({"schritt": "Remux abgeschlossen", "sub_prog": 100})
                        if not simulation:
                            undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
                    else:
                        stats["fehler"] += 1
                        if mp4_pfad.exists():
                            mp4_pfad.unlink()
                        task_q.put({"schritt": "Fehler beim Remux", "sub_prog": 0})
                        continue

                # SRT extrahieren (vor dem Verschieben der MKV!)
                erstellte_srts = []
                if streams and (untertitel or embed_subs):
                    srt_streams = ([s for s in streams if s["language"] != "eng"]
                                   if embed_subs else streams)
                    if srt_streams:
                        erstellte_srts = extrahiere_untertitel(
                            ffmpeg, mkv_pfad, srt_streams, log_q, task_q, simulation, log_zeilen,
                            undo_log=undo_log if not simulation else None)
                    elif untertitel:
                        log("INFO", "    ℹ️  Keine nicht-englischen Untertitel-Spuren für SRT.")
                        task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})
                elif untertitel:
                    log("INFO", "    ℹ️  Keine Untertitel-Spuren.")
                    task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})

                # MKV verschieben / löschen (nach SRT-Extraktion!)
                if neu_remuxed:
                    verschiebe_oder_loesche_mkv(
                        mkv_pfad, original_behalten, simulation, log,
                        undo_log=undo_log,
                        old_mkv_global_pfad=old_mkv_global_pfad)

                # NFO aktualisieren
                if nfo_update and nfo_pfad.exists():
                    aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                                     simulation, log_zeilen, undo_log=undo_log)
                elif nfo_update:
                    log("INFO", "    ℹ️  Keine NFO vorhanden – Update übersprungen.")

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": "Verarbeitung abgeschlossen", "schritt": "", "sub_prog": 100})

    log("HEAD", f"\n{'='*55}")
    log("HEAD",   "  ZUSAMMENFASSUNG  [SERIEN]")
    log("HEAD", f"{'='*55}")
    log("OK",   f"  Dolby Vision gefunden:  {stats['gefunden']}")
    log("OK",   f"  Erfolgreich remuxed:    {stats['remuxed']}")
    log("SKIP", f"  Uebersprungen:          {stats['uebersprungen']}")
    log("ERR",  f"  Fehler:                 {stats['fehler']}")
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", "\n  [SIM] SIMULATION - es wurden KEINE Dateien veraendert.")

    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", f"\n  Log gespeichert: {log_pfad.name}")
    log("HEAD", f"  Speicherort:     {log_pfad.parent}")
    done_q.put((stats, log_pfad))


def verarbeite_sammlung(
        ffmpeg_pfad: str, ffprobe_pfad: str, root_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None):
    """Haupt-Worker (eigener Thread)."""

    ffmpeg  = Path(ffmpeg_pfad)
    ffprobe = Path(ffprobe_pfad)
    root    = Path(root_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = "SIMULATION" if simulation else "ECHTLAUF"
    log("HEAD", f"{'='*55}")
    log("HEAD", f"  DV Remux Tool v{VERSION}  -  {modus_text}")
    log("HEAD", f"  Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    log("HEAD", f"  Root:  {root}")
    log("HEAD", f"{'='*55}")

    ordner_liste = sorted([p for p in root.iterdir() if p.is_dir()])
    gesamt   = len(ordner_liste)
    stats    = {"gefunden": 0, "remuxed": 0, "uebersprungen": 0, "fehler": 0}
    undo_log = []

    for i, ordner in enumerate(ordner_liste):
        if stopp_event and stopp_event.is_set():
            log("WARN", "\n⚠  Verarbeitung vom Benutzer abgebrochen.")
            break

        fort_q.put(int(i / gesamt * 100) if gesamt else 0)
        task_q.put({"film": ordner.name, "schritt": "MKV wird gesucht …", "sub_prog": None})
        log("FOLDER", f"\n📁  {ordner.name}")

        # 1. MKV-Datei suchen
        mkv_pfad = finde_mkv(ordner)
        if mkv_pfad is None:
            log("SKIP", "  ℹ️  Keine MKV-Datei – übersprungen.")
            stats["uebersprungen"] += 1
            task_q.put({"schritt": "Übersprungen (keine MKV)", "sub_prog": 100})
            continue

        # 2. HDR-Typ ermitteln (im Sim-Modus aus NFO, sonst direkt aus MKV)
        task_q.put({"schritt": "HDR-Typ wird ermittelt …", "sub_prog": None})
        nfo_pfad_sim = ordner / "movie.nfo"
        hdrtype = lese_hdrtype_aus_nfo(nfo_pfad_sim) if nfo_pfad_sim.exists() else None
        if hdrtype:
            log("SIM" if simulation else "INFO",
                f"  {'[SIM] ' if simulation else ''}HDR-Typ (NFO): {hdrtype}")
        else:
            hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
            log("SIM" if simulation else "INFO",
                f"  {'[SIM] ' if simulation else ''}HDR-Typ (ffprobe): {hdrtype or '(nicht erkannt)'}")
        if hdrtype != "dolbyvision":
            log("SKIP", "  ℹ️  Kein Dolby Vision – übersprungen.")
            stats["uebersprungen"] += 1
            task_q.put({"schritt": "Übersprungen (kein DV)", "sub_prog": 100})
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
                log("WARN", (f"  ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                              "– wird zu Profil 8 konvertiert (dovi_tool) …"))
            else:
                log("WARN", (f"  ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                              "– dovi_tool nicht gefunden, Farbfehler im Output möglich!"))
                log("WARN",  "      dovi_tool.exe in tools/ ablegen um die Konvertierung zu aktivieren.")

        # 3. Untertitel-Streams ermitteln (vor Remux, für Einbettung + SRT)
        streams = []
        if embed_subs or untertitel:
            task_q.put({"schritt": "Untertitel analysieren …", "sub_prog": None})
            log("INFO", "  🔍 Analysiere Untertitel-Streams …")
            if simulation:
                streams = simuliere_streams_aus_nfo(nfo_pfad)
                log("SIM", f"  [SIM] {len(streams)} Spur(en) laut NFO")
            else:
                streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
            if streams:
                log("INFO", f"  📋 {len(streams)} Spur(en) gefunden")

        text_sub_indices = None
        if embed_subs and streams:
            eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
            bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
            if eng_subs:
                text_sub_indices = [s["index"] for s in eng_subs]
                log("INFO", f"  📥  {len(eng_subs)} Englischer Untertitel wird eingebettet")
            for s in bitmap_subs:
                log("SKIP", f"  ⚠  Bitmap-Sub #{s['index']} [{s['language']}] "
                            f"({s['codec'].upper()}) kann nicht eingebettet werden")

        # 4. MP4 bereits vorhanden?
        neu_remuxed = False
        if mp4_pfad.exists():
            log("SKIP", "  ✅ MP4 existiert bereits – übersprungen.")
            stats["uebersprungen"] += 1
            task_q.put({"schritt": "MP4 bereits vorhanden", "sub_prog": 100})
        else:
            # 5. Remux (normaler Pfad) oder P5→P8-Konvertierung
            task_q.put({"schritt": "Remux läuft …", "sub_prog": None})
            if braucht_p5_fix and DOVI_TOOL.exists():
                task_q.put({"schritt": "P5→P8-Konvertierung läuft …", "sub_prog": None})
                erfolg = konvertiere_dv_p5_zu_p8(
                    ffmpeg, DOVI_TOOL, ffprobe,
                    mkv_pfad, mp4_pfad,
                    log_q, task_q, simulation, log_zeilen,
                    stopp_event=stopp_event)
            else:
                erfolg = remux_zu_mp4(
                    ffmpeg, mkv_pfad, mp4_pfad,
                    log_q, task_q, simulation, log_zeilen,
                    stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                    ffprobe_pfad=ffprobe, kein_faststart=True
                )
                if erfolg:
                    nachbearbeite_dv_mp4(mp4_pfad, log_q, log_zeilen, simulation)
            if erfolg:
                stats["remuxed"] += 1
                neu_remuxed = True
                log("OK", "  ✅ Remux erfolgreich!")
                task_q.put({"schritt": "Remux abgeschlossen", "sub_prog": 100})
                if not simulation:
                    undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
            else:
                stats["fehler"] += 1
                if mp4_pfad.exists():
                    mp4_pfad.unlink()
                task_q.put({"schritt": "Fehler beim Remux", "sub_prog": 0})
                continue

        # 6. Untertitel als SRT extrahieren (vor dem Verschieben der MKV!)
        erstellte_srts = []
        if streams and (untertitel or embed_subs):
            srt_streams = ([s for s in streams if s["language"] != "eng"]
                           if embed_subs else streams)
            if srt_streams:
                erstellte_srts = extrahiere_untertitel(
                    ffmpeg, mkv_pfad, srt_streams, log_q, task_q, simulation, log_zeilen,
                    undo_log=undo_log if not simulation else None)
            elif untertitel:
                log("INFO", "  ℹ️  Keine nicht-englischen Untertitel-Spuren für SRT.")
                task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})
        elif untertitel:
            log("INFO", "  ℹ️  Keine Untertitel-Spuren gefunden.")
            task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})

        # 7. MKV verschieben / löschen (nach SRT-Extraktion!)
        if neu_remuxed:
            verschiebe_oder_loesche_mkv(
                mkv_pfad, original_behalten, simulation, log,
                undo_log=undo_log,
                old_mkv_global_pfad=old_mkv_global_pfad)

        # 8. NFO aktualisieren
        if nfo_update and nfo_pfad.exists():
            aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                             simulation, log_zeilen, undo_log=undo_log)
        elif nfo_update:
            log("INFO", "  ℹ️  Keine movie.nfo vorhanden – NFO-Update übersprungen.")

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": "Verarbeitung abgeschlossen", "schritt": "", "sub_prog": 100})

    # 9. Zusammenfassung
    log("HEAD", f"\n{'='*55}")
    log("HEAD",   "  ZUSAMMENFASSUNG")
    log("HEAD", f"{'='*55}")
    log("OK",   f"  Dolby Vision gefunden:  {stats['gefunden']}")
    log("OK",   f"  Erfolgreich remuxed:    {stats['remuxed']}")
    log("SKIP", f"  Uebersprungen:          {stats['uebersprungen']}")
    log("ERR",  f"  Fehler:                 {stats['fehler']}")
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", "\n  [SIM] SIMULATION - es wurden KEINE Dateien veraendert.")

    # 10. Log-Datei schreiben
    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", f"\n  Log gespeichert: {log_pfad.name}")
    log("HEAD", f"  Speicherort:     {log_pfad.parent}")

    done_q.put((stats, log_pfad))


# ═══════════════════════════════════════════════════════════════════════════════
#  EINZELORDNER-WORKER
# ═══════════════════════════════════════════════════════════════════════════════

def verarbeite_einzelordner(
        ffmpeg_pfad: str, ffprobe_pfad: str, ordner_pfad: str,
        simulation: bool, original_behalten: bool,
        untertitel: bool, nfo_update: bool, embed_subs: bool,
        log_q: queue.Queue, task_q: queue.Queue,
        fort_q: queue.Queue, done_q: queue.Queue,
        stopp_event=None, old_mkv_global_pfad: Path = None):
    """Einzelordner-Worker: verarbeitet genau einen Film-Ordner (direkt MKV darin)."""

    ffmpeg  = Path(ffmpeg_pfad)
    ffprobe = Path(ffprobe_pfad)
    ordner  = Path(ordner_pfad)
    log_zeilen = []

    def log(typ: str, text: str):
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))

    modus_text = "SIMULATION" if simulation else "ECHTLAUF"
    log("HEAD", f"{'='*55}")
    log("HEAD", f"  DV Remux Tool v{VERSION}  -  {modus_text}  [EINZELORDNER]")
    log("HEAD", f"  Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    log("HEAD", f"  Ordner: {ordner}")
    log("HEAD", f"{'='*55}")

    stats    = {"gefunden": 0, "remuxed": 0, "uebersprungen": 0, "fehler": 0}
    undo_log = []

    fort_q.put(0)
    task_q.put({"film": ordner.name, "schritt": "MKV wird gesucht …", "sub_prog": None})
    log("FOLDER", f"\n📁  {ordner.name}")

    mkv_pfad = finde_mkv(ordner)
    if mkv_pfad is None:
        log("SKIP", "  ℹ️  Keine MKV-Datei im Ordner gefunden.")
        fort_q.put(100)
        task_q.put({"film": ordner.name, "schritt": "Keine MKV gefunden", "sub_prog": 100})
        done_q.put((stats, None))
        return

    # HDR-Typ ermitteln
    task_q.put({"schritt": "HDR-Typ wird ermittelt …", "sub_prog": None})
    nfo_pfad = ordner / "movie.nfo"
    hdrtype = lese_hdrtype_aus_nfo(nfo_pfad) if nfo_pfad.exists() else None
    if hdrtype:
        log("SIM" if simulation else "INFO",
            f"  {'[SIM] ' if simulation else ''}HDR-Typ (NFO): {hdrtype}")
    else:
        hdrtype = ermittle_hdrtype_aus_mkv(ffprobe, mkv_pfad)
        log("SIM" if simulation else "INFO",
            f"  {'[SIM] ' if simulation else ''}HDR-Typ (ffprobe): {hdrtype or '(nicht erkannt)'}")

    if hdrtype != "dolbyvision":
        log("SKIP", "  ℹ️  Kein Dolby Vision – abgebrochen.")
        fort_q.put(100)
        task_q.put({"film": ordner.name, "schritt": "Kein Dolby Vision", "sub_prog": 100})
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
            log("WARN", (f"  ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                          "– wird zu Profil 8 konvertiert (dovi_tool) …"))
        else:
            log("WARN", (f"  ⚠️  DV-PROFIL {dv_profil or '?'} (ICtCp) erkannt "
                          "– dovi_tool nicht gefunden, Farbfehler im Output möglich!"))
            log("WARN",  "      dovi_tool.exe in tools/ ablegen um die Konvertierung zu aktivieren.")

    # Untertitel-Streams ermitteln
    streams = []
    if embed_subs or untertitel:
        task_q.put({"schritt": "Untertitel analysieren …", "sub_prog": None})
        log("INFO", "  🔍 Analysiere Untertitel-Streams …")
        if simulation:
            streams = simuliere_streams_aus_nfo(nfo_pfad) if nfo_pfad.exists() else []
            log("SIM", f"  [SIM] {len(streams)} Spur(en) laut NFO")
        else:
            streams = ermittle_untertitel_streams(ffprobe, mkv_pfad)
        if streams:
            log("INFO", f"  📋 {len(streams)} Spur(en) gefunden")

    text_sub_indices = None
    if embed_subs and streams:
        eng_subs    = [s for s in streams if s["codec"] in TEXT_CODECS and s["language"] == "eng"]
        bitmap_subs = [s for s in streams if s["codec"] not in TEXT_CODECS]
        if eng_subs:
            text_sub_indices = [s["index"] for s in eng_subs]
            log("INFO", f"  📥  {len(eng_subs)} Englischer Untertitel wird eingebettet")
        for s in bitmap_subs:
            log("SKIP", f"  ⚠  Bitmap-Sub #{s['index']} [{s['language']}] "
                        f"({s['codec'].upper()}) kann nicht eingebettet werden")

    fort_q.put(10)

    # Remux (normaler Pfad) oder P5→P8-Konvertierung
    neu_remuxed = False
    if mp4_pfad.exists():
        log("SKIP", "  ✅ MP4 existiert bereits – übersprungen.")
        stats["uebersprungen"] += 1
        task_q.put({"schritt": "MP4 bereits vorhanden", "sub_prog": 100})
    else:
        task_q.put({"schritt": "Remux läuft …", "sub_prog": None})
        if braucht_p5_fix and DOVI_TOOL.exists():
            task_q.put({"schritt": "P5→P8-Konvertierung läuft …", "sub_prog": None})
            erfolg = konvertiere_dv_p5_zu_p8(
                ffmpeg, DOVI_TOOL, ffprobe,
                mkv_pfad, mp4_pfad,
                log_q, task_q, simulation, log_zeilen,
                stopp_event=stopp_event)
        else:
            erfolg = remux_zu_mp4(
                ffmpeg, mkv_pfad, mp4_pfad,
                log_q, task_q, simulation, log_zeilen,
                stopp_event=stopp_event, text_sub_indices=text_sub_indices,
                ffprobe_pfad=ffprobe, kein_faststart=True)
            if erfolg:
                nachbearbeite_dv_mp4(mp4_pfad, log_q, log_zeilen, simulation)
        if erfolg:
            stats["remuxed"] += 1
            neu_remuxed = True
            log("OK", "  ✅ Remux erfolgreich!")
            task_q.put({"schritt": "Remux abgeschlossen", "sub_prog": 100})
            if not simulation:
                undo_log.append({"typ": "mp4", "pfad": mp4_pfad})
        else:
            stats["fehler"] += 1
            if mp4_pfad.exists():
                mp4_pfad.unlink()
            task_q.put({"schritt": "Fehler beim Remux", "sub_prog": 0})
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
                ffmpeg, mkv_pfad, srt_streams, log_q, task_q, simulation, log_zeilen,
                undo_log=undo_log if not simulation else None)
        elif untertitel:
            log("INFO", "  ℹ️  Keine nicht-englischen Untertitel-Spuren für SRT.")
            task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})
    elif untertitel:
        log("INFO", "  ℹ️  Keine Untertitel-Spuren gefunden.")
        task_q.put({"schritt": "Keine Untertitel", "sub_prog": 100})

    fort_q.put(80)

    # MKV verschieben / löschen
    if neu_remuxed:
        verschiebe_oder_loesche_mkv(
            mkv_pfad, original_behalten, simulation, log, undo_log=undo_log,
            old_mkv_global_pfad=old_mkv_global_pfad)

    # NFO aktualisieren
    if nfo_update and nfo_pfad.exists():
        aktualisiere_nfo(nfo_pfad, mp4_pfad, erstellte_srts, log_q, task_q,
                         simulation, log_zeilen, undo_log=undo_log)
    elif nfo_update:
        log("INFO", "  ℹ️  Keine movie.nfo vorhanden – NFO-Update übersprungen.")

    if stopp_event and stopp_event.is_set() and not simulation:
        rollback_session(undo_log, log, task_q)

    fort_q.put(100)
    task_q.put({"film": "Verarbeitung abgeschlossen", "schritt": "", "sub_prog": 100})

    log("HEAD", f"\n{'='*55}")
    log("HEAD",   "  ZUSAMMENFASSUNG  [EINZELORDNER]")
    log("HEAD", f"{'='*55}")
    log("OK",   f"  Dolby Vision gefunden:  {stats['gefunden']}")
    log("OK",   f"  Erfolgreich remuxed:    {stats['remuxed']}")
    log("SKIP", f"  Uebersprungen:          {stats['uebersprungen']}")
    log("ERR",  f"  Fehler:                 {stats['fehler']}")
    log("HEAD", f"{'='*55}")
    if simulation:
        log("SIM", "\n  [SIM] SIMULATION - es wurden KEINE Dateien veraendert.")

    log_pfad = schreibe_log_datei(log_zeilen, simulation)
    log("HEAD", f"\n  Log gespeichert: {log_pfad.name}")
    log("HEAD", f"  Speicherort:     {log_pfad.parent}")
    done_q.put((stats, log_pfad))


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):

    BG      = "#0d1117"
    PANEL   = "#161b22"
    PANEL2  = "#1c2128"
    BORDER  = "#30363d"
    ACCENT  = "#58a6ff"
    ACCENT2 = "#f78166"
    GREEN   = "#3fb950"
    YELLOW  = "#d29922"
    RED     = "#f85149"
    MUTED   = "#8b949e"
    TEXT    = "#e6edf3"

    def __init__(self):
        super().__init__()
        self.title(f"DV Remux Tool  v{VERSION}  •  Jellyfin / LG TV")
        self.geometry("980x860")
        self.minsize(800, 660)
        self.configure(bg=self.BG)

        self.läuft             = False
        self.log_queue         = queue.Queue()
        self.task_queue        = queue.Queue()
        self.fort_queue        = queue.Queue()
        self.done_queue        = queue.Queue()
        self.letzter_log_pfad  = None
        self.cfg               = config_laden()

        self.var_ffbin    = tk.StringVar(value=self.cfg.get("ffbin",    self._auto_ffbin()))
        self.var_root     = tk.StringVar(value=self.cfg.get("root",     ""))
        self.var_behalten = tk.BooleanVar(value=self.cfg.get("behalten",True))
        self.var_subs     = tk.BooleanVar(value=self.cfg.get("subs",    True))
        self.var_nfo      = tk.BooleanVar(value=self.cfg.get("nfo",     True))
        self.var_modus          = tk.StringVar(value=self.cfg.get("modus",          "filme"))
        self.var_embed_subs     = tk.BooleanVar(value=self.cfg.get("embed_subs",     False))
        self.var_old_mkv_modus  = tk.StringVar(value=self.cfg.get("old_mkv_modus",  "lokal"))
        self.var_old_mkv_pfad   = tk.StringVar(value=self.cfg.get("old_mkv_pfad",   ""))
        self.var_autoscroll     = tk.BooleanVar(value=True)
        self.stopp_event    = threading.Event()

        self._stil()
        self._gui()
        self._modus_update()
        self._toggle_styles_update()
        self._ffbin_status_update()
        self._dovi_status_update()
        self.var_modus.trace_add("write", lambda *_: self._modus_update())
        self.var_ffbin.trace_add("write", self._ffbin_status_update)
        self.protocol("WM_DELETE_WINDOW", self._schliessen)
        self._poll()

    def _auto_ffbin(self) -> str:
        """
        ffmpeg-Ordner automatisch ermitteln.
        Sucht ffmpeg im PATH und gibt den übergeordneten Ordner zurück.
        Beispiel: /usr/bin/ffmpeg  →  /usr/bin
                  C:/ffmpeg/bin/ffmpeg.exe  →  C:/ffmpeg/bin
        """
        pfad = shutil.which("ffmpeg")
        if pfad:
            return str(Path(pfad).parent)
        return ""

    def _ffmpeg_pfad(self) -> str:
        """Vollständigen ffmpeg-Pfad aus Ordner ableiten."""
        ordner = Path(self.var_ffbin.get())
        name   = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        return str(ordner / name)

    def _ffprobe_pfad(self) -> str:
        """Vollständigen ffprobe-Pfad aus Ordner ableiten."""
        ordner = Path(self.var_ffbin.get())
        name   = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        return str(ordner / name)

    # ─── Stile ───────────────────────────────────────────────────────────────
    def _stil(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        bg, panel, panel2, border = self.BG, self.PANEL, self.PANEL2, self.BORDER
        text, muted = self.TEXT, self.MUTED
        acc, acc2   = self.ACCENT, self.ACCENT2

        s.configure("TFrame",        background=bg)
        s.configure("Panel.TFrame",  background=panel)
        s.configure("Panel2.TFrame", background=panel2)

        s.configure("TLabel",        background=bg,     foreground=text,  font=("Consolas",10))
        s.configure("Muted.TLabel",  background=panel,  foreground=muted, font=("Consolas",9))
        s.configure("MutedBG.TLabel",background=bg,     foreground=muted, font=("Consolas",9))
        s.configure("Task.TLabel",   background=panel2, foreground=text,  font=("Consolas",10))
        s.configure("TaskH.TLabel",  background=panel2, foreground=muted, font=("Consolas",8))
        s.configure("TaskV.TLabel",  background=panel2, foreground=acc,   font=("Consolas",10,"bold"))
        s.configure("TaskOK.TLabel", background=panel2, foreground=self.GREEN, font=("Consolas",9))

        s.configure("TEntry", fieldbackground=panel, foreground=text,
                    bordercolor=border, relief="flat",
                    insertcolor=text, font=("Consolas",9))

        s.configure("TCheckbutton", background=bg, foreground=text, font=("Consolas",10))
        s.map("TCheckbutton",
              background=[("active", bg)],
              foreground=[("active", acc)])

        s.configure("TRadiobutton", background=bg, foreground=text, font=("Consolas",10))
        s.map("TRadiobutton",
              background=[("active", bg)],
              foreground=[("active", acc)])

        s.configure("Sim.TCheckbutton", background=bg, foreground=acc2,
                    font=("Consolas",10,"bold"))
        s.map("Sim.TCheckbutton", background=[("active", bg)])

        # Toggle-Buttons: inaktiv = weiß auf dunkel, aktiv = grün auf dunkel
        s.configure("Toggle.TButton",
            background=panel, foreground=text,
            font=("Consolas", 10), borderwidth=1, relief="flat",
            padding=(11, 6))
        s.map("Toggle.TButton",
              background=[("active", panel2)],
              foreground=[("active", text)])

        s.configure("ToggleOn.TButton",
            background=panel2, foreground=self.GREEN,
            font=("Consolas", 10, "bold"), borderwidth=1, relief="flat",
            padding=(11, 6))
        s.map("ToggleOn.TButton",
              background=[("active", panel2)],
              foreground=[("active", self.GREEN)])

        for name, bg_col, fg_col in [
            ("Run",    acc,    "#0d1117"),
            ("SimRun", acc2,   "#0d1117"),
        ]:
            s.configure(f"{name}.TButton",
                background=bg_col, foreground=fg_col,
                font=("Consolas",11,"bold"),
                borderwidth=0, relief="flat", padding=(18,7))
        s.map("Run.TButton",    background=[("active","#1f6feb"),("disabled",border)],
                                foreground=[("disabled",muted)])
        s.map("SimRun.TButton", background=[("active","#c0392b"),("disabled",border)])

        s.configure("Browse.TButton", background=border, foreground=text,
                    font=("Consolas",9), borderwidth=0, relief="flat", padding=(7,3))
        s.map("Browse.TButton", background=[("active","#3a3d50")])

        s.configure("Log.TButton", background=panel2, foreground=muted,
                    font=("Consolas",10), borderwidth=0, relief="flat", padding=(11,6))
        s.map("Log.TButton", background=[("active","#2d333b")])

        s.configure("Close.TButton", background=panel2, foreground=self.RED,
                    font=("Consolas",13,"bold"), borderwidth=0, relief="flat", padding=(8,2))
        s.map("Close.TButton", background=[("active","#3a1010")],
                               foreground=[("active",self.RED)])

        s.configure("Info.TButton", background=panel2, foreground=muted,
                    font=("Consolas",11), borderwidth=0, relief="flat", padding=(8,2))
        s.map("Info.TButton", background=[("active","#2d333b")],
                              foreground=[("active",self.ACCENT)])

        s.configure("Main.Horizontal.TProgressbar",
                    troughcolor=panel, background=acc,
                    bordercolor=panel, thickness=8)
        s.configure("Sub.Horizontal.TProgressbar",
                    troughcolor=panel, background=self.GREEN,
                    bordercolor=panel, thickness=5)

    # ─── GUI aufbauen ─────────────────────────────────────────────────────────
    def _gui(self):
        # Titelzeile
        t = ttk.Frame(self)
        t.pack(fill="x", padx=20, pady=(16,4))
        # ✕-Schließen-Button oben rechts (vor den linken Labels packen, damit er Platz bekommt)
        ttk.Button(t, text="✕", style="Close.TButton",
                   command=self._schliessen).pack(side="right", padx=(4,0))
        ttk.Button(t, text="ℹ", style="Info.TButton",
                   command=self._info_dialog).pack(side="right", padx=(4,0))
        tk.Label(t, text="DV", fg=self.ACCENT2, bg=self.BG,
                 font=("Consolas",16,"bold")).pack(side="left")
        tk.Label(t, text=" Remux Tool", fg=self.TEXT, bg=self.BG,
                 font=("Consolas",16,"bold")).pack(side="left")
        tk.Label(t, text=f"  v{VERSION}  •  Jellyfin / LG TV",
                 fg=self.MUTED, bg=self.BG,
                 font=("Consolas",9)).pack(side="left", pady=4)
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=(2,10))

        # ── Einstellungs-Panel ────────────────────────────────────────────
        panel = ttk.Frame(self, style="Panel.TFrame")
        panel.pack(fill="x", padx=20, pady=(0,8))
        panel.columnconfigure(1, weight=1)

        # ── Zeile 0: ffmpeg-Ordner ────────────────────────────────────────
        ttk.Label(panel, text="ffmpeg  Ordner", style="Muted.TLabel").grid(
            row=0, column=0, padx=(12,8), pady=(9,0), sticky="w")
        ttk.Entry(panel, textvariable=self.var_ffbin).grid(
            row=0, column=1, padx=(0,6), pady=(9,0), sticky="ew", ipady=4)

        def wähle_ffbin():
            p = filedialog.askdirectory(
                title="ffmpeg-Ordner wählen (Ordner der ffmpeg.exe / ffmpeg enthält)",
                initialdir=self.var_ffbin.get() or "/")
            if p:
                self.var_ffbin.set(p)
                self._ffbin_status_update()
        ttk.Button(panel, text="📂", style="Browse.TButton",
                   command=wähle_ffbin, width=4).grid(
            row=0, column=2, padx=(0,12), pady=(9,0))

        # Status-Frame row=1: ffmpeg/ffprobe + dovi_tool (zwei Zeilen)
        _status_frame = tk.Frame(panel, bg=self.PANEL)
        _status_frame.grid(row=1, column=1, padx=(0,6), pady=(2,4), sticky="w")

        self.ffbin_status = tk.Label(
            _status_frame, text="", bg=self.PANEL, font=("Consolas", 8))
        self.ffbin_status.pack(anchor="w")

        self.dovi_status = tk.Label(
            _status_frame, text="", bg=self.PANEL, font=("Consolas", 8))
        self.dovi_status.pack(anchor="w")

        # ── Zeile 2: Quell-Ordner ─────────────────────────────────────────
        self.root_lbl = ttk.Label(panel, text="Film-Ordner (Root)", style="Muted.TLabel")
        self.root_lbl.grid(row=2, column=0, padx=(12,8), pady=(9,0), sticky="w")
        ttk.Entry(panel, textvariable=self.var_root).grid(
            row=2, column=1, padx=(0,6), pady=(9,0), sticky="ew", ipady=4)
        def wähle_root():
            p = filedialog.askdirectory(
                title=self.root_lbl.cget("text"),
                initialdir=self.var_root.get() or "/")
            if p:
                self.var_root.set(p)
        ttk.Button(panel, text="📂", style="Browse.TButton",
                   command=wähle_root, width=4).grid(
            row=2, column=2, padx=(0,12), pady=(9,0))

        # Hinweis-Label unter dem Quell-Ordner
        self.root_hint = tk.Label(
            panel,
            text='  z.B. "Y:\\Shared Movies\\Filmname (Jahr)\\"  –  alle Unterordner werden durchsucht',
            bg=self.PANEL, fg=self.MUTED, font=("Consolas", 8))
        self.root_hint.grid(row=3, column=1, padx=(0,6), pady=(2,0), sticky="w")

        # ── Zeile 4a: Modus-Auswahl (Toggle-Buttons) ─────────────────────
        modus_frame = ttk.Frame(panel, style="Panel.TFrame")
        modus_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(10,2))
        tk.Label(modus_frame, text="Modus:", bg=self.PANEL, fg=self.MUTED,
                 font=("Consolas", 9)).pack(side="left", padx=(0,10))

        self._modus_btns = {}
        for wert, label in [("filme", "Filme"), ("serien", "Serien"), ("ordner", "Ordner")]:
            btn = ttk.Button(modus_frame, text=label,
                             command=lambda v=wert: self._set_modus(v))
            btn.pack(side="left", padx=(0,4))
            self._modus_btns[wert] = btn

        # ── Zeile 4b: Optionen (Toggle-Buttons) ──────────────────────────
        opt = ttk.Frame(panel, style="Panel.TFrame")
        opt.grid(row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(4,10))

        self._opt_btns = {}
        opt_defs = [
            (self.var_behalten,   "MKV verschieben"),
            (self.var_subs,       "Untertitel .srt"),
            (self.var_embed_subs, "Subs einbetten"),
            (self.var_nfo,        "NFO aktualisieren"),
        ]
        for var, label in opt_defs:
            btn = ttk.Button(opt, text=label,
                             command=lambda v=var: self._toggle_opt(v))
            btn.pack(side="left", padx=(0,6))
            self._opt_btns[id(var)] = (btn, var, label)

        # ── Zeile 6: old-MKV-Ziel ────────────────────────────────────────
        old_ziel_frame = ttk.Frame(panel, style="Panel.TFrame")
        old_ziel_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=12, pady=(2,2))
        tk.Label(old_ziel_frame, text="MKV-Ziel:", bg=self.PANEL, fg=self.MUTED,
                 font=("Consolas", 9)).pack(side="left", padx=(0,10))
        self._old_mkv_ziel_btns = {}
        for wert, label in [("lokal", "Im Filmordner"), ("global", "Globaler Ordner")]:
            btn = ttk.Button(old_ziel_frame, text=label,
                             command=lambda v=wert: self._set_old_mkv_modus(v))
            btn.pack(side="left", padx=(0,4))
            self._old_mkv_ziel_btns[wert] = btn

        # ── Zeile 7: Pfad für globalen Ordner ────────────────────────────
        old_pfad_frame = ttk.Frame(panel, style="Panel.TFrame")
        old_pfad_frame.grid(row=7, column=0, columnspan=3, sticky="ew", padx=12, pady=(0,8))
        old_pfad_frame.columnconfigure(1, weight=1)
        ttk.Label(old_pfad_frame, text="Ziel-Ordner", style="Muted.TLabel").grid(
            row=0, column=0, padx=(0,8), pady=(4,0), sticky="w")
        self.old_mkv_pfad_entry = ttk.Entry(old_pfad_frame, textvariable=self.var_old_mkv_pfad)
        self.old_mkv_pfad_entry.grid(row=0, column=1, padx=(0,6), pady=(4,0), sticky="ew", ipady=4)
        def wähle_old_mkv_pfad():
            p = filedialog.askdirectory(
                title="Globalen Zielordner für old MKV wählen",
                initialdir=self.var_old_mkv_pfad.get() or "/")
            if p:
                self.var_old_mkv_pfad.set(p)
        self.old_mkv_pfad_btn = ttk.Button(
            old_pfad_frame, text="📂", style="Browse.TButton",
            command=wähle_old_mkv_pfad, width=4)
        self.old_mkv_pfad_btn.grid(row=0, column=2, padx=(0,0), pady=(4,0))

        # ── Button-Leiste ─────────────────────────────────────────────────
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=20, pady=(0,8))
        self.btn_start = ttk.Button(
            bf, text="▶  Konvertierung starten", style="Run.TButton",
            command=lambda: self._starten(simulation=False))
        self.btn_start.pack(side="left")
        self.btn_sim = ttk.Button(
            bf, text="🔬  Simulation", style="SimRun.TButton",
            command=lambda: self._starten(simulation=True))
        self.btn_sim.pack(side="left", padx=(8,0))
        self.btn_stopp = ttk.Button(
            bf, text="⏹  Abbrechen & Rückgängig", style="Log.TButton",
            command=self._abbrechen, state="disabled")
        self.btn_stopp.pack(side="left", padx=(8,0))
        self.btn_log = ttk.Button(
            bf, text="📄  Log öffnen", style="Log.TButton",
            command=self._log_oeffnen, state="disabled")
        self.btn_log.pack(side="left", padx=(8,0))
        ttk.Button(bf, text="🗑  Log leeren", style="Log.TButton",
                   command=self._log_leeren).pack(side="left", padx=(8,0))
        self.btn_autoscroll = ttk.Button(
            bf, text="[ON]  Autoscroll", style="ToggleOn.TButton",
            command=self._toggle_autoscroll)
        self.btn_autoscroll.pack(side="left", padx=(8,0))

        # ── Status-Zeile (eigene Reihe, damit sie nicht von Buttons überlagert wird)
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=20, pady=(0,4))
        self.status_lbl = tk.Label(
            status_frame, text="", fg=self.MUTED, bg=self.BG,
            font=("Consolas",10), anchor="w")
        self.status_lbl.pack(side="left")

        # ── Gesamt-Fortschrittsbalken ─────────────────────────────────────
        self.prog_main = ttk.Progressbar(
            self, style="Main.Horizontal.TProgressbar",
            mode="determinate", maximum=100)
        self.prog_main.pack(fill="x", padx=20, pady=(0,6))

        # ── Task-Status-Fenster ───────────────────────────────────────────
        task_border = tk.Frame(self, bg=self.BORDER)
        task_border.pack(fill="x", padx=20, pady=(0,8))
        task_inner = tk.Frame(task_border, bg=self.PANEL2)
        task_inner.pack(fill="x", padx=1, pady=1)

        # Linke Info-Spalte
        left = tk.Frame(task_inner, bg=self.PANEL2)
        left.pack(side="left", fill="both", expand=True, padx=(12,8), pady=10)

        def task_zeile(parent, label):
            r = tk.Frame(parent, bg=self.PANEL2)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, fg=self.MUTED, bg=self.PANEL2,
                     font=("Consolas",8), width=8, anchor="w").pack(side="left")
            val = tk.Label(r, text="—", fg=self.TEXT, bg=self.PANEL2,
                           font=("Consolas",10), anchor="w")
            val.pack(side="left", fill="x", expand=True)
            return val

        self.task_film    = task_zeile(left, "FILM")
        self.task_schritt = task_zeile(left, "SCHRITT")
        self.task_status  = task_zeile(left, "STATUS")
        self.task_status.configure(fg=self.GREEN, text="Bereit")

        # Trennlinie
        tk.Frame(task_inner, bg=self.BORDER, width=1).pack(
            side="left", fill="y", pady=6)

        # Rechte Fortschritts-Spalte
        right = tk.Frame(task_inner, bg=self.PANEL2)
        right.pack(side="left", padx=(12,14), pady=10)

        tk.Label(right, text="SCHRITT-FORTSCHRITT",
                 fg=self.MUTED, bg=self.PANEL2,
                 font=("Consolas",8)).pack(anchor="w")

        self.prog_sub = ttk.Progressbar(
            right, style="Sub.Horizontal.TProgressbar",
            mode="determinate", maximum=100, length=220)
        self.prog_sub.pack(fill="x", pady=(4,2))

        self.pct_lbl = tk.Label(
            right, text="0 %", fg=self.MUTED, bg=self.PANEL2,
            font=("Consolas",9))
        self.pct_lbl.pack(anchor="e")

        # Trennlinie Sim-Indikator
        self.sim_banner = tk.Label(
            right, text="", fg=self.ACCENT2, bg=self.PANEL2,
            font=("Consolas",8,"bold"))
        self.sim_banner.pack(anchor="w", pady=(4,0))

        # ── Log-Textfenster ───────────────────────────────────────────────
        log_outer = tk.Frame(self, bg=self.BORDER)
        log_outer.pack(fill="both", expand=True, padx=20, pady=(0,16))

        self.log_widget = scrolledtext.ScrolledText(
            log_outer,
            bg="#090d13", fg=self.TEXT,
            font=("Consolas",9), wrap="word",
            borderwidth=0, relief="flat",
            state="disabled",
            selectbackground=self.ACCENT
        )
        self.log_widget.pack(fill="both", expand=True, padx=1, pady=1)

        self.log_widget.tag_configure("HEAD",   foreground=self.ACCENT)
        self.log_widget.tag_configure("OK",     foreground=self.GREEN)
        self.log_widget.tag_configure("ERR",    foreground=self.RED)
        self.log_widget.tag_configure("SKIP",   foreground=self.MUTED)
        self.log_widget.tag_configure("INFO",   foreground=self.TEXT)
        self.log_widget.tag_configure("WARN",   foreground=self.YELLOW)
        self.log_widget.tag_configure("SIM",    foreground=self.ACCENT2,
                                                font=("Consolas",9,"bold"))
        self.log_widget.tag_configure("PROG",   foreground="#4a505a")
        self.log_widget.tag_configure("FOLDER", foreground="#e6b450",
                                                font=("Consolas",9,"bold"))

    # ─── Abbrechen ────────────────────────────────────────────────────────────
    def _abbrechen(self):
        self.stopp_event.set()
        self.btn_stopp.configure(state="disabled")
        self.status_lbl.configure(text="⏳ Abbrechen …", fg=self.YELLOW)

    # ─── Schließen (X-Button + Schließen-Button) ──────────────────────────────
    def _schliessen(self):
        if self.läuft:
            antwort = messagebox.askyesno(
                "Prozess läuft noch",
                "Ein Remux-Prozess ist aktiv.\n\n"
                "Jetzt wirklich beenden?\n"
                "Der laufende Vorgang wird abgebrochen.",
                icon="warning",
                default="no"
            )
            if not antwort:
                return
            self.stopp_event.set()
        config_speichern({
            "ffbin":          self.var_ffbin.get(),
            "root":           self.var_root.get(),
            "behalten":       self.var_behalten.get(),
            "subs":           self.var_subs.get(),
            "nfo":            self.var_nfo.get(),
            "modus":          self.var_modus.get(),
            "embed_subs":     self.var_embed_subs.get(),
            "old_mkv_modus":  self.var_old_mkv_modus.get(),
            "old_mkv_pfad":   self.var_old_mkv_pfad.get(),
        })
        self.destroy()

    # ─── Autoscroll-Toggle ────────────────────────────────────────────────────
    def _toggle_autoscroll(self):
        val = not self.var_autoscroll.get()
        self.var_autoscroll.set(val)
        if val:
            self.btn_autoscroll.configure(style="ToggleOn.TButton", text="[ON]  Autoscroll")
            self.log_widget.see("end")
        else:
            self.btn_autoscroll.configure(style="Toggle.TButton",   text="[OFF] Autoscroll")

    # ─── Info-Dialog ──────────────────────────────────────────────────────────
    def _info_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Über DV Remux Tool")
        dlg.configure(bg=self.BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="DV Remux Tool", fg=self.ACCENT2, bg=self.BG,
                 font=("Consolas",14,"bold")).pack(pady=(20,2))
        tk.Label(dlg, text=f"Version  {VERSION}", fg=self.MUTED, bg=self.BG,
                 font=("Consolas",10)).pack()
        tk.Label(dlg, text="Dolby Vision MKV → MP4  •  Jellyfin / LG TV",
                 fg=self.TEXT, bg=self.BG, font=("Consolas",9)).pack(pady=(4,16))

        tk.Frame(dlg, bg=self.BORDER, height=1).pack(fill="x", padx=20)

        # Anklickbarer GitHub-Link
        REPO = "https://github.com/Hero9774/DolbyVision-Remux"
        lnk = tk.Label(dlg, text=REPO, fg=self.ACCENT, bg=self.BG,
                       font=("Consolas",9,"underline"), cursor="hand2")
        lnk.pack(pady=(14,2))
        lnk.bind("<Button-1>", lambda _: webbrowser.open(REPO))

        tk.Label(dlg, text="hero.ommen@posteo.de", fg=self.MUTED, bg=self.BG,
                 font=("Consolas",9)).pack(pady=(0,12))

        # Drittanbieter
        tk.Frame(dlg, bg=self.BORDER, height=1).pack(fill="x", padx=20)
        tk.Label(dlg, text="Drittanbieter-Komponenten",
                 fg=self.MUTED, bg=self.BG,
                 font=("Consolas",8,"bold")).pack(pady=(10,2))

        DOVI_URL = "https://github.com/quietvoid/dovi_tool"
        dovi_lnk = tk.Label(dlg,
                             text="dovi_tool  (quietvoid)  –  GPL v3.0 or later",
                             fg=self.ACCENT, bg=self.BG,
                             font=("Consolas",8,"underline"), cursor="hand2")
        dovi_lnk.pack()
        dovi_lnk.bind("<Button-1>", lambda _: webbrowser.open(DOVI_URL))

        tk.Label(dlg,
                 text="Wird verwendet für DV-Profil-5 → Profil-8-Konvertierung.",
                 fg=self.MUTED, bg=self.BG, font=("Consolas",8)).pack(pady=(0,14))

        ttk.Button(dlg, text="Schließen", style="Log.TButton",
                   command=dlg.destroy).pack(pady=(0,16))

    # ─── Modus-Toggle ─────────────────────────────────────────────────────────
    def _set_modus(self, wert):
        self.var_modus.set(wert)

    def _toggle_opt(self, var):
        var.set(not var.get())
        self._toggle_styles_update()

    def _set_old_mkv_modus(self, wert):
        self.var_old_mkv_modus.set(wert)
        self._toggle_styles_update()

    def _toggle_styles_update(self):
        """Alle Toggle-Buttons aktualisieren: aktiv = grün + [ON], inaktiv = weiß + [OFF]."""
        modus = self.var_modus.get()
        for wert, btn in self._modus_btns.items():
            if wert == modus:
                btn.configure(style="ToggleOn.TButton")
            else:
                btn.configure(style="Toggle.TButton")

        for key, (btn, var, label) in self._opt_btns.items():
            if var.get():
                btn.configure(style="ToggleOn.TButton", text=f"[ON]  {label}")
            else:
                btn.configure(style="Toggle.TButton", text=f"[OFF] {label}")

        # MKV-Ziel: nur aktiv wenn "MKV verschieben" an
        behalten      = self.var_behalten.get()
        old_mkv_modus = self.var_old_mkv_modus.get()
        if hasattr(self, "_old_mkv_ziel_btns"):
            for wert, btn in self._old_mkv_ziel_btns.items():
                if not behalten:
                    btn.configure(style="Toggle.TButton", state="disabled")
                elif wert == old_mkv_modus:
                    btn.configure(style="ToggleOn.TButton", state="normal")
                else:
                    btn.configure(style="Toggle.TButton", state="normal")
        if hasattr(self, "old_mkv_pfad_entry"):
            ist_global = behalten and old_mkv_modus == "global"
            state = "normal" if ist_global else "disabled"
            self.old_mkv_pfad_entry.configure(state=state)
            self.old_mkv_pfad_btn.configure(state=state)

    # ─── Modus-Label aktualisieren ────────────────────────────────────────────
    def _modus_update(self):
        modus = self.var_modus.get()
        self._toggle_styles_update()
        if modus == "serien":
            self.root_lbl.configure(text="Serien-Ordner (Root)")
            self.root_hint.configure(
                text='  z.B. "Y:\\Shared TV Shows\\Serienname (Jahr)\\Season 01\\"  –  alle Shows/Staffeln werden durchsucht')
        elif modus == "ordner":
            self.root_lbl.configure(text="Film-Ordner (direkt)")
            self.root_hint.configure(
                text='  z.B. "D:\\Downloads\\Filmname (Jahr)\\"  –  der Ordner enthält direkt die .mkv-Datei')
        else:
            self.root_lbl.configure(text="Film-Ordner (Root)")
            self.root_hint.configure(
                text='  z.B. "Y:\\Shared Movies\\Filmname (Jahr)\\"  –  alle Unterordner werden durchsucht')

    def _ffbin_status_update(self, *_):
        """
        Prüft ob ffmpeg und ffprobe im gewählten Ordner liegen
        und aktualisiert das Status-Label darunter in Echtzeit.
        """
        ordner = Path(self.var_ffbin.get()) if self.var_ffbin.get() else None
        if ordner is None or not ordner.is_dir():
            self.ffbin_status.configure(
                text="  ⚠  Ordner nicht gefunden", fg=self.RED)
            return

        ext       = ".exe" if sys.platform == "win32" else ""
        ffmpeg_ok  = (ordner / f"ffmpeg{ext}").is_file()
        ffprobe_ok = (ordner / f"ffprobe{ext}").is_file()

        teile = []
        if ffmpeg_ok:
            teile.append("✅ ffmpeg")
        else:
            teile.append("❌ ffmpeg fehlt")
        if ffprobe_ok:
            teile.append("✅ ffprobe")
        else:
            teile.append("❌ ffprobe fehlt")

        farbe = self.GREEN if (ffmpeg_ok and ffprobe_ok) else self.RED
        self.ffbin_status.configure(
            text="  " + "   ".join(teile), fg=farbe)

    def _dovi_status_update(self):
        """Prüft ob dovi_tool.exe in tools/ vorhanden ist und zeigt Status."""
        if DOVI_TOOL.exists():
            self.dovi_status.configure(
                text="  ✅ dovi_tool", fg=self.GREEN, cursor="")
            self.dovi_status.unbind("<Button-1>")
        else:
            self.dovi_status.configure(
                text=("  ⚠  dovi_tool fehlt  "
                      "(für DV-Profil-5-Konvertierung)  "
                      "→ Download ↗  [→ in tools/ ablegen]"),
                fg=self.YELLOW, cursor="hand2")
            self.dovi_status.bind(
                "<Button-1>",
                lambda e: webbrowser.open(
                    "https://github.com/quietvoid/dovi_tool/releases"))

    # ─── Log ──────────────────────────────────────────────────────────────────
    def _log(self, typ: str, text: str):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text + "\n", typ)
        self.log_widget.configure(state="disabled")
        if self.var_autoscroll.get():
            self.log_widget.see("end")

    def _log_leeren(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _log_oeffnen(self):
        if self.letzter_log_pfad and self.letzter_log_pfad.exists():
            if sys.platform == "win32":
                os.startfile(self.letzter_log_pfad)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(self.letzter_log_pfad)])
            else:
                subprocess.run(["xdg-open", str(self.letzter_log_pfad)])
        else:
            messagebox.showinfo("Log", "Noch keine Log-Datei vorhanden.")

    # ─── Start ────────────────────────────────────────────────────────────────
    def _starten(self, simulation: bool):
        if self.läuft:
            return
        ist_sim = simulation
        fehler  = []

        # ffmpeg-Ordner und abgeleitete Pfade prüfen
        if not ist_sim:
            ffmpeg_pfad  = self._ffmpeg_pfad()
            ffprobe_pfad = self._ffprobe_pfad()
            if not self.var_ffbin.get() or not Path(self.var_ffbin.get()).is_dir():
                fehler.append("ffmpeg-Ordner ungültig oder nicht gefunden.")
            else:
                if not Path(ffmpeg_pfad).is_file():
                    fehler.append(
                        f"ffmpeg nicht im Ordner gefunden: {Path(ffmpeg_pfad).name}")
                if not Path(ffprobe_pfad).is_file():
                    fehler.append(
                        f"ffprobe nicht im Ordner gefunden: {Path(ffprobe_pfad).name}")
        else:
            ffmpeg_pfad  = self._ffmpeg_pfad()
            ffprobe_pfad = self._ffprobe_pfad()

        if not self.var_root.get() or not Path(self.var_root.get()).is_dir():
            fehler.append("Quell-Ordner ungültig.")

        if fehler:
            for f in fehler:
                self._log("ERR", f"❌  {f}")
            return

        config_speichern({
            "ffbin":          self.var_ffbin.get(),
            "root":           self.var_root.get(),
            "behalten":       self.var_behalten.get(),
            "subs":           self.var_subs.get(),
            "nfo":            self.var_nfo.get(),
            "modus":          self.var_modus.get(),
            "embed_subs":     self.var_embed_subs.get(),
            "old_mkv_modus":  self.var_old_mkv_modus.get(),
            "old_mkv_pfad":   self.var_old_mkv_pfad.get(),
        })

        self.stopp_event.clear()
        self.läuft = True
        self.btn_start.configure(state="disabled")
        self.btn_sim.configure(state="disabled")
        self.btn_stopp.configure(state="normal")
        self.btn_log.configure(state="disabled")
        self.status_lbl.configure(text="⏳ Läuft …", fg=self.MUTED)
        self.prog_main["value"] = 0
        self.prog_sub["value"]  = 0
        self.pct_lbl.configure(text="0 %")
        self.task_film.configure(text="—")
        self.task_schritt.configure(text="—")
        self.task_status.configure(text="Läuft …", fg=self.YELLOW)
        self._log_leeren()

        if ist_sim:
            self.sim_banner.configure(text="🔬 SIMULATIONSMODUS AKTIV")
            self.configure(bg="#110d0a")
            self._log("SIM",
                "🔬 SIMULATIONSMODUS – keine Dateien werden verändert!\n")
        else:
            self.sim_banner.configure(text="")
            self.configure(bg=self.BG)

        modus  = self.var_modus.get()
        worker = (verarbeite_serien       if modus == "serien"
                  else verarbeite_einzelordner if modus == "ordner"
                  else verarbeite_sammlung)

        old_mkv_global = None
        if self.var_behalten.get() and self.var_old_mkv_modus.get() == "global":
            p = self.var_old_mkv_pfad.get().strip()
            if p:
                old_mkv_global = Path(p)

        threading.Thread(
            target=worker,
            args=(
                ffmpeg_pfad,
                ffprobe_pfad,
                self.var_root.get(),
                ist_sim,
                self.var_behalten.get(),
                self.var_subs.get(),
                self.var_nfo.get(),
                self.var_embed_subs.get(),
                self.log_queue,
                self.task_queue,
                self.fort_queue,
                self.done_queue,
                self.stopp_event,
                old_mkv_global,
            ),
            daemon=True
        ).start()

    # ─── Poll-Loop (alle 80 ms) ───────────────────────────────────────────────
    def _poll(self):
        # Gesamt-Fortschritt
        try:
            while True:
                self.prog_main["value"] = self.fort_queue.get_nowait()
        except queue.Empty:
            pass

        # Task-Updates
        try:
            while True:
                info = self.task_queue.get_nowait()
                if "film" in info:
                    self.task_film.configure(text=info["film"])
                if "schritt" in info:
                    self.task_schritt.configure(text=info["schritt"])
                if "sub_prog" in info:
                    pct = info["sub_prog"]
                    if pct is None:
                        self.prog_sub.stop()
                        self.prog_sub.configure(mode="indeterminate")
                        self.prog_sub.start(12)
                        self.pct_lbl.configure(text="…")
                    else:
                        self.prog_sub.stop()
                        self.prog_sub.configure(mode="determinate")
                        self.prog_sub["value"] = pct
                        self.pct_lbl.configure(text=f"{pct} %")
        except queue.Empty:
            pass

        # Log-Nachrichten
        try:
            while True:
                typ, text = self.log_queue.get_nowait()
                self._log(typ, text)
        except queue.Empty:
            pass

        # Fertig-Signal
        try:
            stats, log_pfad = self.done_queue.get_nowait()
            self.läuft = False
            self.btn_start.configure(state="normal")
            self.btn_sim.configure(state="normal")
            self.btn_stopp.configure(state="disabled")
            self.sim_banner.configure(text="")
            self.configure(bg=self.BG)
            self.letzter_log_pfad = log_pfad
            self.btn_log.configure(state="normal" if log_pfad else "disabled")
            ok  = stats["remuxed"]
            err = stats["fehler"]
            farbe = self.GREEN if err == 0 else self.YELLOW
            self.status_lbl.configure(
                text=f"✅ {ok} remuxed  ❌ {err} Fehler", fg=farbe)
            log_name = log_pfad.name if log_pfad else "—"
            self.task_status.configure(
                text=f"Fertig – {log_name}", fg=self.GREEN)
        except queue.Empty:
            pass

        self.after(80, self._poll)


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = App()
    app.mainloop()
