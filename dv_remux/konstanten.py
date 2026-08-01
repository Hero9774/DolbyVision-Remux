"""Modulweite Konstanten und Ordnerstruktur."""

from pathlib import Path

# Dieses Modul liegt in dv_remux/ – der Projekt-Root ist eine Ebene höher.
PROJEKT_ROOT = Path(__file__).resolve().parent.parent

VERSION      = "5.9.0"
CONFIG_ORDNER = PROJEKT_ROOT / "config"
CONFIG_DATEI  = CONFIG_ORDNER / "dv_remux_config.json"
LOG_ORDNER    = PROJEKT_ROOT / "logs"
LANG_ORDNER   = PROJEKT_ROOT / "lang"
TEXT_CODECS   = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "srt"}
DOVI_TOOL     = PROJEKT_ROOT / "tools" / "dovi_tool.exe"
LOKALE_KOPIE_PUFFER = 2.3   # Sicherheitsfaktor für Platzbedarf bei lokaler Arbeitskopie
SPRACHEN      = {"de": "Deutsch", "en": "English"}
SPRACHE_DEFAULT = "en"

# Ordnerstruktur beim Start sicherstellen
for _d in (CONFIG_ORDNER, LOG_ORDNER, LANG_ORDNER, DOVI_TOOL.parent):
    _d.mkdir(parents=True, exist_ok=True)
