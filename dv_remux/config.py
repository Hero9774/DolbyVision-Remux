"""Laden/Speichern der GUI-Konfiguration (dv_remux_config.json)."""

import json

from dv_remux.konstanten import CONFIG_DATEI


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
