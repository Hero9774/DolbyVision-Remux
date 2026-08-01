"""dvcC-Box-Injektion, faststart/mp42-Patch und DV-Nachbearbeitung auf MP4-Binärebene."""

import queue
import struct
from pathlib import Path

from dv_remux.sprache import t, _bereinige_log


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


# Beide Namen des Dolby Vision Configuration Records. Welcher gilt, hängt vom
# Sample-Entry ab (Dolby Vision Streams Within the ISO Base Media File Format):
#   dvh1/dvhe (nicht rückwärtskompatible Profile ≤ 7) → dvcC
#   hvc1/hev1 (cross-kompatible Profile 8.x)          → dvvC
DV_BOX_TYPEN = (b"dvcC", b"dvvC")


def _baue_dv_record(dv_profil: int, dv_level: int, compat_id: int) -> bytes:
    """
    DoViDecoderConfigurationRecord (24 Byte Payload) aufbauen.
    Layout: version_major(8) version_minor(8) profile(7) level(6) rpu(1)
            el(1) bl(1) compat_id(4) reserved(28) + 4×32 Bit reserved.
    Die vier Reserved-Words gehören dazu – ohne sie ist die Box zu kurz und
    Parser lesen über ihr Ende hinaus.
    """
    bits = dv_profil & 0x7F
    bits = (bits << 6) | (dv_level & 0x3F)
    bits = (bits << 3) | 0b101          # rpu=1, el=0, bl=1
    bits = (bits << 4) | (compat_id & 0xF)
    bits <<= 28                          # 28 reservierte Bits → 48 Bit gesamt
    return struct.pack(">BB", 1, 0) + bits.to_bytes(6, "big") + b"\x00" * 16


def _dv_box_typ(entry_typ: bytes, dv_profil: int) -> bytes:
    """Passenden Boxnamen für den Sample-Entry wählen (siehe DV_BOX_TYPEN)."""
    if entry_typ in (b"dvh1", b"dvhe"):
        return b"dvcC"
    return b"dvvC" if dv_profil >= 8 else b"dvcC"


def injiziere_dvcc_box(mp4_pfad: Path, dv_profil: int = 8,
                        dv_level: int = 6, compat_id: int = 1) -> bool:
    """
    Injiziert den Dolby Vision Configuration Record in eine MP4-Datei.
    Voraussetzung: moov am Ende der Datei (kein faststart-Modus).
    Navigiert moov→trak→mdia→minf→stbl→stsd→(dvh1/hvc1) und fügt die Box nach
    hvcC ein – als dvvC bei cross-kompatiblen Profilen im hvc1-Entry, sonst als
    dvcC. Ist bereits eine der beiden Boxen vorhanden (neuere ffmpeg-Versionen
    schreiben dvvC selbst), wird nichts eingefügt. Aktualisiert nur die
    betroffenen Parent-Box-Größen; stco/co64 bleiben unberührt (mdat vor moov).
    """
    record = _baue_dv_record(dv_profil, dv_level, compat_id)

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
                    pos_ins, eltern, e_typ = _suche(data, kind_start, off + sz)
                    if pos_ins is not None:
                        return pos_ins, [off] + eltern, e_typ
                elif typ in HEVC_ENTRY:
                    # hvcC per Signatur finden (VisualSampleEntry-Länge
                    # variiert je nach ffmpeg-Version → kein fixer Offset)
                    eintrag = bytes(data[ds:off + sz])
                    hvcc_rel = eintrag.find(b"hvcC")
                    if hvcc_rel >= 4:
                        hvcc_abs = ds + hvcc_rel - 4
                        hvcc_sz  = struct.unpack_from(">I", data, hvcc_abs)[0]
                        # Eine DV-Box zählt egal ob dvcC oder dvvC – sonst
                        # entsteht eine zweite Konfiguration im selben Entry.
                        rest = eintrag[hvcc_rel + hvcc_sz - 4:]
                        hat_dv = any(name in rest for name in DV_BOX_TYPEN)
                        if 8 <= hvcc_sz <= (off + sz - hvcc_abs) and not hat_dv:
                            return hvcc_abs + hvcc_sz, [off], typ
            return None, [], None

        einfuege_pos, groessen_offs, entry_typ = _suche(moov, 8, n)
        if einfuege_pos is None:
            return False

        box_typ = _dv_box_typ(entry_typ, dv_profil)
        dv_box  = struct.pack(">I4s", 8 + len(record), box_typ) + record

        moov[einfuege_pos:einfuege_pos] = dv_box

        for soff in [0] + groessen_offs:
            alt = struct.unpack_from(">I", moov, soff)[0]
            struct.pack_into(">I", moov, soff, alt + len(dv_box))

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


def _moov_enthaelt(mp4_pfad: Path, signatur: bytes) -> bool:
    """Prüft ob eine Byte-Signatur im moov-Atom der MP4 vorkommt (schnelle Suche)."""
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
                    return signatur in daten
                pos += bsz
    except Exception:
        pass
    return False


def _dvcc_vorhanden(mp4_pfad: Path) -> bool:
    """
    Prüft ob eine Dolby-Vision-Konfigurationsbox in der MP4 vorhanden ist
    (schnelle Byte-Suche im moov). Es zählen beide Boxnamen: ffmpeg schreibt
    bei HEVC-DV-Profil 8 im hvc1-Entry selbst eine dvvC-Box – wird nur auf
    dvcC geprüft, landet eine überflüssige zweite Box in der Datei.
    """
    return any(_moov_enthaelt(mp4_pfad, name) for name in DV_BOX_TYPEN)


def nachbearbeite_dv_mp4(mp4_pfad: Path, log_q: queue.Queue,
                          log_zeilen: list, simulation: bool) -> None:
    """
    Nach normalem DV-Remux (ohne faststart): dvcC prüfen/injizieren,
    dann moov nach vorne schieben (faststart) und ftyp auf mp42 setzen.
    """
    if simulation:
        return
    # AV1-Video (DV Profil 10): die HEVC-dvcC-Injektion ist nicht anwendbar
    # (av01-Entry statt hvc1/dvh1). ffmpeg schreibt die DV-Signalisierung bereits
    # als dvvC-Box beim -c copy-Remux – nur prüfen, nicht selbst injizieren.
    if _moov_enthaelt(mp4_pfad, b"av01"):
        if _moov_enthaelt(mp4_pfad, b"dvvC"):
            text = t("postproc.dvvc_present")
            log_q.put(("OK", text))
        else:
            text = t("postproc.dvvc_missing")
            log_q.put(("WARN", text))
        log_zeilen.append(_bereinige_log(text))
    else:
        hat_dvcc = _dvcc_vorhanden(mp4_pfad)
        _dvcc_schritt(mp4_pfad, hat_dvcc, log_q, log_zeilen)

    text = t("postproc.faststart_running")
    log_q.put(("INFO", text))
    log_zeilen.append(_bereinige_log(text))
    if mache_faststart_und_ftyp(mp4_pfad):
        text = t("postproc.faststart_ok")
        log_q.put(("OK", text))
        log_zeilen.append(_bereinige_log(text))
    else:
        text = t("postproc.faststart_failed")
        log_q.put(("WARN", text))
        log_zeilen.append(_bereinige_log(text))


def _dvcc_schritt(mp4_pfad: Path, hat_dvcc: bool, log_q: queue.Queue,
                  log_zeilen: list) -> None:
    """HEVC-dvcC-Box prüfen/injizieren (aus nachbearbeite_dv_mp4 ausgelagert)."""
    if not hat_dvcc:
        text = t("postproc.dvcc_missing")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
        if injiziere_dvcc_box(mp4_pfad):
            text = t("postproc.dvcc_ok")
            log_q.put(("OK", text))
            log_zeilen.append(_bereinige_log(text))
        else:
            text = t("postproc.dvcc_failed")
            log_q.put(("WARN", text))
            log_zeilen.append(_bereinige_log(text))
    else:
        text = t("postproc.dvcc_already")
        log_q.put(("INFO", text))
        log_zeilen.append(_bereinige_log(text))
