"""Datei-Kopier-/Verschiebe-Operationen: Fortschrittsanzeige, sicheres Verschieben,
Speicherplatz-Check und die Sicherungsordner-Logik für die Original-MKV."""

import queue
import shutil
from pathlib import Path

from dv_remux.konstanten import LOKALE_KOPIE_PUFFER
from dv_remux.sprache import t, _bereinige_log


def genug_speicherplatz(ordner: Path, benoetigte_bytes: int,
                         puffer: float = LOKALE_KOPIE_PUFFER) -> bool:
    """Prüft ob im Zielordner genug freier Speicher für den Kopiervorgang ist."""
    try:
        frei = shutil.disk_usage(ordner).free
    except OSError:
        return False
    return frei >= benoetigte_bytes * puffer


def kopiere_mit_fortschritt(quelle: Path, ziel: Path, beschreibung: str,
                             log_q: queue.Queue, task_q: queue.Queue,
                             simulation: bool, log_zeilen: list,
                             stopp_event=None) -> bool:
    """
    Kopiert quelle -> ziel in 64-MB-Blöcken mit Fortschrittsanzeige (task_q sub_prog).
    Abbrechbar über stopp_event; räumt bei Abbruch/Fehler die (unvollständige)
    Zieldatei auf.
    """
    if simulation:
        text = t("copy.sim", beschreibung=beschreibung, quelle=quelle.name, ziel=ziel)
        log_q.put(("SIM", text))
        log_zeilen.append(_bereinige_log(text))
        task_q.put({"schritt": beschreibung, "sub_prog": 100})
        return True

    BUF = 64 * 1024 * 1024  # 64 MB
    text = t("copy.running", beschreibung=beschreibung)
    log_q.put(("INFO", text))
    log_zeilen.append(_bereinige_log(text))

    try:
        gesamt = quelle.stat().st_size
        kopiert = 0
        ziel.parent.mkdir(parents=True, exist_ok=True)
        with open(quelle, "rb") as fin, open(ziel, "wb") as fout:
            while True:
                if stopp_event and stopp_event.is_set():
                    raise InterruptedError(t("copy.abgebrochen"))
                chunk = fin.read(BUF)
                if not chunk:
                    break
                fout.write(chunk)
                kopiert += len(chunk)
                task_q.put({
                    "schritt":  beschreibung,
                    "sub_prog": int(kopiert / gesamt * 100) if gesamt else 100
                })
        task_q.put({"sub_prog": 100})
        return True
    except Exception as e:
        if ziel.exists():
            ziel.unlink(missing_ok=True)
        typ = "WARN" if isinstance(e, InterruptedError) else "ERR"
        text = t("copy.failed", beschreibung=beschreibung, fehler=e)
        log_q.put((typ, text))
        log_zeilen.append(_bereinige_log(text))
        return False


def verschiebe_sicher(quelle: Path, ziel: Path, beschreibung: str,
                       log_q: queue.Queue, task_q: queue.Queue,
                       simulation: bool, log_zeilen: list,
                       stopp_event=None) -> bool:
    """
    Verschiebt quelle -> ziel SICHER: kopiert vollständig (mit Fortschritt,
    abbrechbar über stopp_event), verifiziert per Dateigröße dass die Kopie
    vollständig ist, und löscht die Quelle NUR nach erfolgreicher Verifikation.
    Schlägt die Kopie oder die Prüfung fehl, bleibt die Quelle unangetastet.
    """
    if simulation:
        text = t("copy.sim", beschreibung=beschreibung, quelle=quelle.name, ziel=ziel)
        log_q.put(("SIM", text))
        log_zeilen.append(_bereinige_log(text))
        task_q.put({"schritt": beschreibung, "sub_prog": 100})
        return True

    quelle_groesse = quelle.stat().st_size
    if not kopiere_mit_fortschritt(quelle, ziel, beschreibung, log_q, task_q,
                                    simulation, log_zeilen, stopp_event=stopp_event):
        return False

    if not ziel.exists() or ziel.stat().st_size != quelle_groesse:
        text = t("move.verify_failed", quelle=quelle.name)
        log_q.put(("ERR", text))
        log_zeilen.append(_bereinige_log(text))
        ziel.unlink(missing_ok=True)
        return False

    try:
        quelle.unlink()
    except OSError as e:
        text = t("move.delete_failed", fehler=e)
        log_q.put(("WARN", text))
        log_zeilen.append(_bereinige_log(text))
        # Kopie ist verifiziert vollständig vorhanden -> trotzdem als Erfolg werten,
        # Original bleibt dann halt als (harmlose) Dublette liegen.

    return True


def verschiebe_oder_loesche_mkv(mkv_pfad: Path, original_behalten: bool,
                                simulation: bool, log_func,
                                undo_log: list = None,
                                old_mkv_global_pfad: Path = None,
                                aktueller_pfad: Path = None) -> None:
    """
    MKV nach erfolgreichem Remux verschieben oder löschen.
    aktueller_pfad: wo die Datei GERADE liegt (z. B. lokaler Arbeitsordner bei
    aktivierter lokaler Kopie); Default = mkv_pfad (unverändertes Verhalten).
    mkv_pfad bleibt in jedem Fall die NAS-Referenz für Zielordner-Namen und
    Undo-Ursprungspfad.
    """
    quelle = aktueller_pfad or mkv_pfad
    if original_behalten:
        if old_mkv_global_pfad:
            ziel_ordner = old_mkv_global_pfad
            ziel_label  = str(old_mkv_global_pfad / mkv_pfad.name)
        else:
            ziel_ordner = mkv_pfad.parent / "old MKV"
            ziel_label  = f"old MKV/{mkv_pfad.name}"
        ziel_pfad = ziel_ordner / mkv_pfad.name
        if simulation:
            log_func("SIM", t("mkvmove.sim_move", ziel=ziel_label))
        elif ziel_pfad.exists():
            log_func("WARN", t("mkvmove.already_exists", name=mkv_pfad.name))
        else:
            try:
                ziel_ordner.mkdir(parents=True, exist_ok=True)
                shutil.move(str(quelle), str(ziel_pfad))
                log_func("OK", t("mkvmove.moved", ziel=ziel_label))
                if undo_log is not None:
                    undo_log.append({"typ": "mkv_move",
                                     "von": ziel_pfad, "nach": mkv_pfad})
            except Exception as e:
                log_func("ERR", t("mkvmove.move_failed", fehler=e))
    elif simulation:
        log_func("SIM", t("mkvmove.sim_delete", name=mkv_pfad.name))
    else:
        try:
            quelle.unlink()
            log_func("INFO", t("mkvmove.deleted", name=mkv_pfad.name))
            if undo_log is not None:
                undo_log.append({"typ": "mkv_del", "pfad": mkv_pfad})
        except Exception as e:
            log_func("ERR", t("mkvmove.delete_failed", fehler=e))
