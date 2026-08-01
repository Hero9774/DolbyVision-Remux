"""Modulweite Konstanten und Ordnerstruktur."""

import sys
from pathlib import Path

# Dieses Modul liegt in dv_remux/ – der Projekt-Root ist eine Ebene höher.
PROJEKT_ROOT = Path(__file__).resolve().parent.parent

VERSION      = "5.9.1"
CONFIG_ORDNER = PROJEKT_ROOT / "config"
CONFIG_DATEI  = CONFIG_ORDNER / "dv_remux_config.json"
LOG_ORDNER    = PROJEKT_ROOT / "logs"
LANG_ORDNER   = PROJEKT_ROOT / "lang"
TEXT_CODECS   = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "srt"}
# Auf Windows heißt das Tool dovi_tool.exe, sonst dovi_tool. Ohne diese
# Unterscheidung wäre DOVI_TOOL.exists() außerhalb von Windows immer False und
# die P5→P8-Konvertierung würde stumm ausfallen.
DOVI_TOOL     = PROJEKT_ROOT / "tools" / (
    "dovi_tool.exe" if sys.platform == "win32" else "dovi_tool")
LOKALE_KOPIE_PUFFER = 2.3   # Sicherheitsfaktor für Platzbedarf bei lokaler Arbeitskopie
SPRACHEN      = {"de": "Deutsch", "en": "English"}
SPRACHE_DEFAULT = "en"


def stelle_ordner_sicher() -> None:
    """
    Legt die Projektordner an. Bewusst KEIN Import-Nebeneffekt: läge das
    Projekt auf einem schreibgeschützten Pfad, würde sonst schon der Import
    mit einem nackten Traceback scheitern statt mit einer Meldung.
    Wird beim Start aus dv_remux_gui.py aufgerufen.
    """
    for ordner in (CONFIG_ORDNER, LOG_ORDNER, LANG_ORDNER, DOVI_TOOL.parent):
        try:
            ordner.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[Setup] Ordner {ordner} konnte nicht angelegt werden: {e}")
