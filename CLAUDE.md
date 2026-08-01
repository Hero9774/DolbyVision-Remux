# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Starten

```bash
python dv_remux_gui.py
```

**Voraussetzungen:** Python 3.8+, ffmpeg + ffprobe (nur im Echtlauf; im Simulationsmodus nicht nötig).

## Zweck

`dv_remux_gui.py` ist nur der Einstiegspunkt; die Logik liegt im Paket `dv_remux/`.
Die Version steht an **einer** Stelle: `konstanten.py` → `VERSION` (aktuell **5.9.1**).

Drei Betriebsmodi (Toggle-Buttons in der GUI, Config-Key `"modus"`):
- **`"filme"`** – ein MKV pro Unterordner, steuert `verarbeite_sammlung()`
- **`"serien"`** – Show/Staffel/Episode-Struktur, steuert `verarbeite_serien()`; überspringt Trickplay-Ordner (`trickplay` im Pfad). Ein Ordner zählt auch dann als Staffel, wenn er selbst MKVs enthält
- **`"ordner"`** – genau ein Film-Ordner, steuert `verarbeite_einzelordner()`

## Dolby-Vision-Box und anamorphe Quellen (Stolperfallen)

- **Es gibt zwei Boxnamen für denselben Record.** Nach der Dolby-ISOBMFF-Spec
  gehört `dvvC` in einen `hvc1`/`hev1`-Entry (cross-kompatible Profile 8.x) und
  `dvcC` in einen `dvh1`/`dvhe`-Entry. Aktuelle ffmpeg-Versionen schreiben `dvvC`
  beim Muxen bereits selbst – prüft man nur auf `dvcC`, landet eine zweite Box
  in derselben Sample-Entry. `_dvcc_vorhanden()` prüft deshalb `DV_BOX_TYPEN`.
- **Der Record ist 24 Byte lang** (Box also 32 Byte), inklusive der vier
  Reserved-Words. Eine kürzere Box ist defekt, auch wenn ffprobe die ersten
  Felder noch korrekt anzeigt.
- **Anamorphe Quellen** (SAR ≠ 1:1) lehnt Jellyfin für Direct Play ab
  („anamorphic video is not supported"). `anamorph_argumente()` in pipeline.py
  korrigiert bis 1 % Abweichung mit **`-aspect W:H` allein** – die `pasp`-Box im
  Container hat für ffprobe Vorrang vor der VUI. Über der Toleranz (echtes
  anamorphes Material wie PAL SAR 16:15) wird nicht gepatcht, sonst wäre das
  Bild sichtbar verzerrt; dort hilft nur Skalieren = Neucodierung.
- **Niemals `-bsf:v hevc_metadata` auf Dolby-Vision-Material anwenden.** Der
  Filter serialisiert VPS/SPS/PPS neu (im Test 12 → 6 Parametersätze), danach
  passt die RPU nicht mehr dazu: ffmpeg dekodiert die Datei klaglos, auf dem TV
  zerfällt das Bild in Farbschlieren. Am LG G4 verifiziert. Prüfmethode: NAL-Typen
  im Elementarstrom zählen (Typ 32/33/34 = VPS/SPS/PPS, Typ 62 = RPU) und mit der
  Quelle vergleichen.
- **DTS in MP4 ist eine Sackgasse.** ffmpeg schreibt es nur als `mp4a`+esds
  (OTI 0xA9); `-tag:a dtsc` lehnt der Muxer ab. Hardware-Player verweigern dann
  die **ganze Datei**, auch wenn sie DTS können. Deshalb `dts_audio_argumente()`
  → E-AC3 640k pro betroffener Spur, alle anderen bleiben `copy`.
- Ein MP4→MP4-Remux verliert die DV-Signalisierung: Reparaturen immer aus der
  Original-MKV fahren.
- **`injiziere_dvcc_box()` setzt moov am Dateiende voraus** und prüft das seit
  v5.9.1 auch. Sie schreibt moov an seine alte Position und ruft `truncate()` –
  auf einer bereits gefaststarteten MP4 wäre das Video danach weg. Reihenfolge
  ist also immer: muxen ohne `+faststart` → injizieren → `mache_faststart_und_ftyp()`.
- **Dateien niemals mit `unlink()` + `rename()` ersetzen**, immer `os.replace()`.
  Unter Windows schlägt das Rename regelmäßig fehl (Virenscanner/Explorer halten
  die frische Datei kurz offen, `WinError 32`); zwischen beiden Aufrufen existiert
  sonst nur die Temp-Kopie, die der Fehlerpfad dann mitlöscht.
- **`kopiere_mit_fortschritt()` / `verschiebe_sicher()` prüfen auf Quelle == Ziel.**
  `open(ziel, "wb")` trunkiert vor dem Lesen; ohne die Prüfung wäre die Datei
  bei identischen Pfaden weg.

## Unterprogramm: Video-Konverter

Zweites, unabhängiges Fenster (Button „🎬 Video-Konverter" in der Button-Leiste,
`App._konverter_oeffnen()` → `dv_remux/konverter_gui.py`). Es hat **eigene** Queues,
ein eigenes `stopp_event` und einen eigenen Poll-Loop – die `done_queue`-Stats des
Hauptfensters (`remuxed`/`fehler`) und die des Konverters
(`konvertiert`/`fehler`/`uebersprungen`/`gefunden`) dürfen deshalb nicht vermischt werden.

- Logik in `dv_remux/konverter.py`, Worker `verarbeite_videoordner()`
- Phase 1 Scan (`analysiere_video()` via ffprobe: Auflösung, SAR, field_order, fps, Audio),
  Phase 2 Re-Encode aller Nicht-MP4-Dateien. `.mkv` steht bewusst **nicht** in
  `VIDEO_ENDUNGEN` (gehört zum Remux-Teil) und wird als SKIP gemeldet
- Encoder-Wahl: `ermittle_encoder()` prüft AMF **mit echtem 1-Sekunden-Testencode**
  (`-encoders` beweist nur Compile-Zeit-Support) und cached das Ergebnis;
  Fallback-Kette pro Datei: HW mit Subs → HW ohne Subs → `libx264`/`libx265`
- Zielcodec: nur **`h264`** oder **`hevc`** (`CODEC_WERTE` in konverter.py),
  Vorgabe `hevc`. `waehle_ziel_codec()` entscheidet nur noch dann nach Auflösung,
  wenn in der Config ein unbekannter Wert steht (Migration von altem `"auto"`)
- Info-Button (ℹ) im Konverter-Fenster: der Text steht als Zeilenliste in den
  Sprachdateien unter `konvgui.info_1`, `konvgui.info_2` … – `_info_dialog()`
  liest hoch, bis ein Key fehlt. Zeilen mit `# ` am Anfang werden Überschriften.
  **Beide Sprachdateien müssen gleich viele info_-Keys haben** (Konsistenzcheck)
- Ratenkontrolle über Zielbitrate
  (`berechne_bitrate()`, Peak-VBR) statt CQP – bei konstantem Quantizer werden
  verrauschte alte Quellen sonst größer als das Original
- Ausgabe: HEVC + `hvc1` bzw. H.264 High, `yuv420p`, AAC (Copy wenn Quelle schon AAC),
  `-movflags +faststart`, GOP = 2 s (Vorspulen), `yadif` bei interlaced,
  SAR ≠ 1:1 → auf quadratische Pixel skalieren, sonst Auflösung unverändert
- **Lokale Verarbeitung** (Standard an): Original per `verschiebe_sicher()` in
  `<Arbeitsordner>/dv_konverter`, dort konvertieren, MP4 zurück, Original löschen.
  Jeder Fehlerpfad (Encode, Rücktransfer, Abbruch) ruft `zurueck_an_ursprung()`
- **Alle Pfade kommen aus dem Hauptfenster**: das Konverter-Fenster bindet direkt
  an `app.var_root` / `app.var_lokale_kopie_pfad` und liest `old_mkv_pfad` +
  `ffbin` – keine eigenen Pfad-Config-Keys anlegen
- Eigene Config-Keys nur mit Präfix `konv_`; `App._config_dict()` baut auf
  `self.cfg` auf, damit die Keys des jeweils anderen Fensters erhalten bleiben

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

**Alle drei Remux-Worker laufen durch `_worker_rahmen()`** (worker.py). Der Rahmen
legt `log_zeilen`/`stats`/`log()` an, ruft die `_..._impl()`-Funktion und garantiert
im `finally` genau **ein** `done_q`-Signal – auch bei unerwarteter Exception. Ohne das
stirbt der Thread stumm und die GUI wartet ewig (Start-Button bleibt deaktiviert).
Deshalb: in den Impl-Funktionen **niemals** selbst `done_q.put()` aufrufen oder
`schreibe_log_datei()` starten; ein einfaches `return` genügt. Der Konverter macht
dasselbe mit einem eigenen `try/except/finally` in `verarbeite_videoordner()`.

4 Queues für die GUI-Kommunikation:
- `log_queue` – farbige Log-Einträge (`"OK"`, `"ERR"`, `"SIM"`, `"SKIP"`, `"PROG"`, `"HEAD"`, `"INFO"`, `"WARN"`, `"FOLDER"`)
- `task_queue` – aktueller Film/Schritt/Sub-Fortschritt (`{"film": ..., "schritt": ..., "sub_prog": ...}`)
- `fort_queue` – Gesamt-Fortschritt 0–100
- `done_queue` – Ergebnis-Stats + Log-Pfad am Ende

Die GUI polt alle Queues via `self.after()` (`_poll()`-Methode) – kein direkter GUI-Zugriff aus dem Worker-Thread.

## Simulationsmodus

Simulation ist **kein** Config-Key, sondern ein Parameter der Start-Buttons
(`App._starten(simulation=True)`).

- ffmpeg wird nicht aufgerufen. **ffprobe schon** – die HDR-Erkennung läuft auch
  hier, deshalb prüft `_starten()` die Existenz von ffprobe auch in der Simulation.
  Fehlt sie, gilt jeder Film als "kein Dolby Vision" und der Lauf meldet 0 Treffer
- Es wird **keine** Datei und **kein** Ordner geschrieben (auch der Arbeitsordner
  wird nicht angelegt)
- Untertitel-Streams werden aus der NFO gelesen (`simuliere_streams_aus_nfo()`)
- Alle Aktionen werden als `[SIM]`-Einträge geloggt
- Log-Dateien bekommen das Suffix `_SIM_`

## Konfiguration

`config/dv_remux_config.json` (Pfad aus `konstanten.CONFIG_DATEI`) – wird beim Start
geladen und beim Schließen gespeichert. `config_speichern()` schreibt **atomar**
(Temp-Datei + `os.replace()`); eine beschädigte Datei wird beim Laden nach `.bak`
umbenannt statt still verworfen.

```json
{
  "ffbin":      "C:/ffmpeg/bin",   // Ordner mit ffmpeg.exe + ffprobe.exe
  "root":       "Y:/Shared Movies",// Root-Verzeichnis
  "behalten":   true,              // Original-MKV nach Remux sichern
  "subs":       false,             // Untertitel als externe .srt extrahieren
  "nfo":        true,              // movie.nfo aktualisieren
  "modus":      "filme",           // "filme" | "serien" | "ordner"
  "embed_subs": false,             // Untertitel in MP4 einbetten
  "sprache":    "de"               // "de" | "en"
}
```
Vollständiges Beispiel inkl. der `konv_*`-Keys: `dv_remux_config.example.json`.

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

Jeder Worker baut eine `undo_log`-Liste auf (Einträge mit `{"typ": "mp4"|"srt"|"nfo", "pfad": ...}`). `rollback_session()` wird **nur bei gesetztem `stopp_event`** aufgerufen (Benutzer-Abbruch), nicht bei einem Fehler in einer einzelnen Datei – dort räumt der jeweilige `finally`-Block auf und schiebt die Original-MKV aus dem Arbeitsordner an ihren Ursprungsort zurück.

Deshalb wartet `App._schliessen()` per `_warte_auf_ende()` auf das Ende des Threads, statt sofort `destroy()` zu rufen: die Worker sind `daemon=True` und würden sonst mitten im Rollback hart abgebrochen.

## GUI-Architektur

Klasse `App(tk.Tk)` mit Dark-Theme (GitHub-Farbschema: `#0d1117` BG, `#58a6ff` Accent). Stile via `ttk.Style` mit dem `"clam"`-Theme als Basis.

Kernmethoden: `_starten()` startet den Worker-Thread, `_poll()` liest alle Queues via `self.after()`, `_modus_update()` schaltet zwischen Filme/Serien-UI um.
