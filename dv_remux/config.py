"""Laden/Speichern der GUI-Konfiguration (dv_remux_config.json)."""

import json
import os

from dv_remux.konstanten import CONFIG_DATEI


def config_laden() -> dict:
    """
    Config einlesen. Ist die Datei beschädigt, wird sie nach .bak umbenannt
    statt still verworfen – sonst überschreibt config_speichern() beim Beenden
    die noch reparierbare Datei endgültig.
    """
    if CONFIG_DATEI.exists():
        try:
            daten = json.loads(CONFIG_DATEI.read_text(encoding="utf-8"))
            if isinstance(daten, dict):
                return daten
            raise ValueError("Config ist kein JSON-Objekt")
        except Exception as e:
            print(f"[Config] Datei beschädigt ({e}) – wird als .bak gesichert.")
            try:
                sicherung = CONFIG_DATEI.with_suffix(CONFIG_DATEI.suffix + ".bak")
                os.replace(str(CONFIG_DATEI), str(sicherung))
                print(f"[Config] Sicherung: {sicherung}")
            except OSError:
                pass
    return {}


def config_speichern(daten: dict) -> bool:
    """
    Config atomar schreiben: erst in eine Temp-Datei, dann per os.replace()
    ersetzen. Ein Absturz mitten im Schreiben kann die bestehende Config so
    nicht mehr zerstören. Gibt True bei Erfolg zurück.
    """
    tmp = CONFIG_DATEI.with_suffix(CONFIG_DATEI.suffix + ".tmp")
    try:
        CONFIG_DATEI.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(CONFIG_DATEI))
        return True
    except Exception as e:
        print(f"[Config] Speichern fehlgeschlagen: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
