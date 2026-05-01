# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Starten

```bash
python dv_remux_gui.py
```

**Voraussetzungen:** Python 3.8+, ffmpeg + ffprobe (nur im Echtlauf; im Simulationsmodus nicht nötig).

## Zweck

`dv_remux_gui.py` (aktuell **v5.0**, Docstring v5) ist ein GUI-Tool zum Batch-Remux von **Dolby Vision MKV → MP4** (ohne Re-Encoding) für Jellyfin / LG TV. Es verarbeitet automatisch alle Unterordner eines Root-Verzeichnisses.

Zwei Betriebsmodi (Radiobutton in der GUI, Config-Key `"modus"`):
- **`"filme"`** – ein MKV pro Unterordner, steuert `verarbeite_sammlung()`
- **`"serien"`** – rekursive Staffel/Episode-Struktur, steuert `verarbeite_serien()`; überspringt Trickplay-Ordner (`trickplay` im Pfad)

## Verarbeitungs-Pipeline

Der Worker `verarbeite_sammlung()` läuft in einem eigenen Thread und durchläuft für jeden Unterordner:

1. `movie.nfo` einlesen → HDR-Typ prüfen (`hdrtype = "dolbyvision"`)
2. `.mkv`-Datei im Ordner finden
3. **Remux** via ffmpeg: `ffmpeg -i input.mkv -c copy -tag:v hvc1 -map 0:v -map 0:a -movflags +faststart output.mp4`
4. Optional: **Untertitel** als `.srt` extrahieren (nur Text-Codecs: subrip, ass, ssa, webvtt, mov_text, text, srt)
5. Optional: **Untertitel einbetten** (`embed_subs`) – SRT-Streams als `-c:s mov_text` in die MP4 mappen
6. Optional: **`movie.nfo` aktualisieren** – `original_filename` (.mkv → .mp4) und `<subtitle>`-Einträge in `<streamdetails>` neu schreiben; Backup als `movie.nfo.bak`

HDR-Typ-Prüfung: primär aus `movie.nfo` (`lese_hdrtype_aus_nfo()`), alternativ direkt aus MKV-Metadaten via ffprobe DOVI side-data (`ermittle_hdrtype_aus_mkv()`).

## Threading-Modell

4 Queues für die GUI-Kommunikation:
- `log_queue` – farbige Log-Einträge (`"OK"`, `"ERR"`, `"SIM"`, `"SKIP"`, `"PROG"`, `"HEAD"`)
- `task_queue` – aktueller Film/Schritt/Sub-Fortschritt (`{"film": ..., "schritt": ..., "sub_prog": ...}`)
- `fort_queue` – Gesamt-Fortschritt 0–100
- `done_queue` – Ergebnis-Stats + Log-Pfad am Ende

Die GUI polt alle Queues via `self.after()` (`_poll()`-Methode) – kein direkter GUI-Zugriff aus dem Worker-Thread.

## Simulationsmodus

Im Simulationsmodus (`var_sim = True`):
- ffmpeg/ffprobe werden nicht aufgerufen
- Untertitel-Streams werden aus der NFO gelesen (`simuliere_streams_aus_nfo()`)
- Alle Aktionen werden als `[SIM]`-Einträge geloggt
- Log-Dateien bekommen das Suffix `_SIM_`

## Konfiguration

`dv_remux_config.json` – wird beim Start geladen und beim Schließen gespeichert:
```json
{
  "ffbin":      "C:/ffmpeg/bin",   // Ordner mit ffmpeg.exe + ffprobe.exe
  "root":       "Y:/Shared Movies",// Root-Verzeichnis
  "sim":        true,              // Simulationsmodus
  "behalten":   true,              // Original-MKV nach Remux behalten
  "subs":       false,             // Untertitel als externe .srt extrahieren
  "nfo":        true,              // movie.nfo aktualisieren
  "modus":      "filme",           // "filme" | "serien"
  "embed_subs": false              // Untertitel in MP4 einbetten
}
```

## Ordner-Struktur (erwartet)

```
Root/
  Film A (2023)/
    Film A.mkv
    movie.nfo          ← muss <hdrtype>dolbyvision</hdrtype> enthalten
  Film B (2024)/
    Film B.mkv
    movie.nfo
```

## NFO-Aktualisierung

`aktualisiere_nfo()` verwendet `xml.etree.ElementTree` und schreibt das XML manuell zurück, um die `<?xml ...>`-Deklaration und tinyMediaManager-Kommentare (`<!-- ... -->`) zu erhalten. `ET.indent()` wird genutzt (Python 3.9+), mit Fallback für ältere Versionen.

## Rollback / Undo

Jeder Worker baut eine `undo_log`-Liste auf (Einträge mit `{"typ": "mp4"|"srt"|"nfo", "pfad": ...}`). Bei Abbruch oder Fehler ruft `rollback_session()` die Liste ab und löscht/stellt erstellte Dateien wieder her.

## GUI-Architektur

Klasse `App(tk.Tk)` mit Dark-Theme (GitHub-Farbschema: `#0d1117` BG, `#58a6ff` Accent). Stile via `ttk.Style` mit dem `"clam"`-Theme als Basis.

Kernmethoden: `_starten()` startet den Worker-Thread, `_poll()` liest alle Queues via `self.after()`, `_modus_update()` schaltet zwischen Filme/Serien-UI um.
