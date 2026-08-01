"""Mehrsprachigkeit: JSON-Sprachdateien laden und Texte auflösen."""

import json

from dv_remux.konstanten import LANG_ORDNER, SPRACHEN, SPRACHE_DEFAULT

_sprach_cache: dict = {}
_aktuelle_sprache = SPRACHE_DEFAULT


def lade_sprache(code: str) -> dict:
    """Lädt (und cached) die Übersetzungstabelle für einen Sprachcode."""
    if code not in _sprach_cache:
        pfad = LANG_ORDNER / f"{code}.json"
        try:
            _sprach_cache[code] = json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            _sprach_cache[code] = {}
    return _sprach_cache[code]


def sprachen_konsistenz_pruefen() -> None:
    """Warnt (nur im Log via print), falls de.json/en.json unterschiedliche Keys haben."""
    keys = {code: set(lade_sprache(code).keys()) for code in SPRACHEN}
    alle = set().union(*keys.values()) if keys else set()
    for code, vorhanden in keys.items():
        fehlend = alle - vorhanden
        if fehlend:
            print(f"[Sprache] WARNUNG: {code}.json fehlen {len(fehlend)} Key(s): "
                  f"{sorted(fehlend)[:10]}{' …' if len(fehlend) > 10 else ''}")


def setze_sprache(code: str) -> None:
    """Setzt die aktuell aktive Sprache (modulweit, wirkt sofort für alle t()-Aufrufe)."""
    global _aktuelle_sprache
    if code in SPRACHEN:
        _aktuelle_sprache = code


def t(key: str, **kwargs) -> str:
    """
    Übersetzten String für key in der aktuellen Sprache liefern.
    Fallback-Kette: aktuelle Sprache -> Deutsch -> der Key selbst.
    Fehlen Format-Platzhalter, wird der unformatierte Text zurückgegeben
    statt zu crashen.
    """
    vorlage = lade_sprache(_aktuelle_sprache).get(key)
    if vorlage is None:
        vorlage = lade_sprache(SPRACHE_DEFAULT).get(key, key)
    try:
        return vorlage.format(**kwargs)
    except (KeyError, IndexError):
        return vorlage


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
                .replace("⏭","SKIP:").replace("🎬","")
                .replace("→","->"))
