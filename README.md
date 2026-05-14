# DV Remux Tool

GUI-Tool für **Batch-Remux von Dolby-Vision-MKV-Dateien zu MP4** — ohne Re-Encoding, optimiert für **Jellyfin** und **LG TV**.

![DV Remux Tool — Screenshot](docs/screenshot.png)

---

## Inhalt

- [Funktionen](#funktionen)
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- [Erster Start](#erster-start)
- [Bedienungsanleitung](#bedienungsanleitung)
  - [1. ffmpeg-Ordner setzen](#1-ffmpeg-ordner-setzen)
  - [2. Modus wählen: Filme / Serien / Ordner](#2-modus-wählen-filme--serien--ordner)
  - [3. Optionen umschalten](#3-optionen-umschalten)
  - [4. Konvertierung starten oder simulieren](#4-konvertierung-starten-oder-simulieren)
  - [5. Abbrechen & Rückgängig](#5-abbrechen--rückgängig)
  - [6. Logs](#6-logs)
- [Erwartete Ordner-Struktur](#erwartete-ordner-struktur)
- [Konfigurationsdatei](#konfigurationsdatei)
- [Wie funktioniert das Remux?](#wie-funktioniert-das-remux)
- [Hinweise zu Dolby Vision](#hinweise-zu-dolby-vision)
- [Troubleshooting](#troubleshooting)
- [Changelog](#changelog)
- [Drittanbieter-Komponenten](#drittanbieter-komponenten)
- [Lizenz](#lizenz)

---

## Funktionen

- **Verlustfreies Remux** von MKV → MP4 (`-c copy`, kein Re-Encoding, kein Qualitätsverlust)
- HEVC-Tag-Korrektur auf `hvc1` für **LG TV-Kompatibilität**
- **dvcC-Box Injektion** — Dolby Vision Configuration Record wird direkt per Python-Binärpatch in die MP4 geschrieben; ohne diese Box erkennt Jellyfin / LG TV kein DV im MP4-Container
- **faststart + mp42** — `moov`-Atom vor `mdat` verschieben und `major_brand` auf `mp42` setzen für optimale Player-Kompatibilität
- **DV-Profil-5 → Profil-8.1-Konvertierung** (Blaustich-Fix) via `dovi_tool -m 3 convert` — 5-Schritt-Pipeline ohne Re-Encoding
- **Drei Modi:**
  - **Filme** — verarbeitet alle Unterordner eines Root-Verzeichnisses (ein MKV pro Ordner)
  - **Serien** — rekursive Verarbeitung von Staffel/Episode-Strukturen
  - **Ordner** — verarbeitet genau einen einzelnen Film-Ordner (Direktauswahl)
- **Untertitel-Extraktion** als externe `.srt`-Dateien (subrip, ass, ssa, webvtt, mov_text, srt)
- **Untertitel einbetten** als `mov_text` direkt in die MP4
- **NFO-Aktualisierung** (tinyMediaManager-kompatibel) inklusive Backup als `movie.nfo.bak`
- **Simulationsmodus** — komplette Vorschau ohne Dateien anzufassen
- **Rollback / Undo** — bei Abbruch oder Fehler werden erstellte Dateien automatisch entfernt
- **Live-Fortschritt**, farbiger Log, separater Schritt-Fortschritt
- **Dark Theme** (GitHub-Farbschema)

---

## Voraussetzungen

| Komponente | Version | Hinweis |
|---|---|---|
| Python | 3.8+ | `tkinter` ist im Lieferumfang |
| ffmpeg | aktuell | nur im Echtlauf nötig |
| ffprobe | aktuell | nur im Echtlauf nötig |
| dovi_tool | aktuell | **optional** — nur für DV-Profil-5-Konvertierung |

Im **Simulationsmodus** sind weder ffmpeg noch ffprobe erforderlich — ideal zum Testen der Pipeline.

ffmpeg-Download: <https://ffmpeg.org/download.html>

dovi_tool-Download: <https://github.com/quietvoid/dovi_tool/releases>
→ `dovi_tool.exe` in den Ordner `tools/` legen. Die GUI zeigt den Status automatisch an.

---

## Installation

```bash
git clone https://github.com/Hero9774/DolbyVision-Remux.git
cd DolbyVision-Remux
python dv_remux_gui.py
```

Optional: Beispiel-Konfig kopieren:

```bash
cp dv_remux_config.example.json dv_remux_config.json
```

---

## Erster Start

Beim allerersten Start ist die GUI leer. Du musst lediglich:

1. den **ffmpeg-Ordner** angeben (siehe unten),
2. einen **Root-Ordner** oder **Film-Ordner** wählen,
3. den passenden **Modus** auswählen.

Alle Einstellungen werden beim Beenden automatisch in `dv_remux_config.json` gespeichert.

---

## Bedienungsanleitung

### 1. ffmpeg-Ordner setzen

Im Feld **„ffmpeg Ordner"** den Pfad zum `bin/`-Verzeichnis deiner ffmpeg-Installation eintragen oder über das Ordner-Symbol auswählen.

Beispiel: `C:/Program Files/FFMPG/ffmpeg-8.1-full_build/bin`

Die GUI zeigt durch grüne Häkchen, ob `ffmpeg` und `ffprobe` gefunden wurden:

- ✅ ffmpeg
- ✅ ffprobe

Sind beide gefunden, kannst du im Echtlauf starten. Fehlt eines, erscheint eine Warnung — der Simulationsmodus läuft trotzdem.

### 2. Modus wählen: Filme / Serien / Ordner

| Modus | Verhalten | Eingabefeld |
|---|---|---|
| **Filme** | Verarbeitet **alle Unterordner** des angegebenen Root. Erwartet eine MKV pro Unterordner. | Root-Ordner (z. B. `Y:/Shared Movies`) |
| **Serien** | Geht **rekursiv** durch Staffel-/Episoden-Struktur. `trickplay`-Ordner werden übersprungen. | Root-Ordner (z. B. `Y:/Shared Series`) |
| **Ordner** | Verarbeitet **genau einen** Film-Ordner, der direkt eine MKV enthält. | Film-Ordner (direkt) |

Der gewählte Modus blendet automatisch das passende Eingabefeld ein.

### 3. Optionen umschalten

Vier Toggle-Buttons (`[ON]` / `[OFF]`):

| Option | Beschreibung |
|---|---|
| **MKV verschieben** | Original-MKV wird **behalten** (verschoben/umbenannt) statt gelöscht. Empfohlen, bis du das Ergebnis geprüft hast. |
| **Untertitel .srt** | Text-basierte Untertitel werden als externe `.srt`-Datei neben der MP4 abgelegt. |
| **Subs einbetten** | Extrahierte Untertitel werden als `mov_text`-Stream in die MP4 eingebettet (kann mit `Untertitel .srt` kombiniert werden). |
| **NFO aktualisieren** | `movie.nfo` wird aktualisiert: `original_filename` `.mkv` → `.mp4`, `<subtitle>`-Einträge in `<streamdetails>` neu gesetzt. Vor jeder Änderung wird `movie.nfo.bak` angelegt. |

Nur Text-Codecs werden für SRT-Extraktion akzeptiert: `subrip`, `ass`, `ssa`, `webvtt`, `mov_text`, `text`, `srt`. Bild-basierte Untertitel (PGS / VobSub) werden übersprungen.

### 4. Konvertierung starten oder simulieren

| Button | Aktion |
|---|---|
| **▶ Konvertierung starten** | Startet den **Echtlauf** — ruft ffmpeg auf, schreibt MP4-Dateien, ändert NFO. |
| **🔬 Simulation** | Startet den **Simulationsmodus** — keine Datei wird angefasst. Stattdessen wird jede geplante Aktion als `[SIM]` ins Log geschrieben. Untertitel-Streams werden aus der NFO gelesen. |

Beide Modi laufen in einem **Background-Thread**, die GUI bleibt während des Laufs reaktionsfähig.

Während der Konvertierung zeigt die GUI:

- **FILM** — aktueller Filmtitel
- **SCHRITT** — aktuelle Aktion (Analyse, Remux, SRT, NFO …)
- **STATUS** — `Bereit` / `Läuft` / `Fertig` / `Fehler`
- **SCHRITT-FORTSCHRITT** — Prozent-Balken für den aktuellen Schritt
- Großer Gesamt-Fortschrittsbalken oben

### 5. Abbrechen & Rückgängig

**⏹ Abbrechen & Rückgängig** stoppt den laufenden Prozess sauber:

1. ffmpeg-Subprozess wird beendet (`process.terminate()`).
2. Der bisher geführte **Undo-Log** wird abgearbeitet:
   - erzeugte `.mp4` werden gelöscht,
   - extrahierte `.srt` werden gelöscht,
   - geänderte `.nfo` werden aus dem `.bak` wiederhergestellt.

Das Tool versucht damit, einen sauberen Vorzustand wiederherzustellen.

### 6. Logs

- **📄 Log öffnen** — öffnet die zuletzt erzeugte Log-Datei im Standard-Editor.
- **🗑 Log leeren** — leert das Log-Fenster (nicht die Datei).

Log-Dateien liegen in `logs/`:

- Echtlauf: `dv_remux_RUN_YYYYMMDD_HHMMSS.log`
- Simulation: `dv_remux_SIM_YYYYMMDD_HHMMSS.log`

Im Log-Fenster sind die Einträge **farbig markiert**: `OK` (grün), `ERR` (rot), `SIM` (blau), `SKIP` (grau), `PROG`, `HEAD`.

---

## Erwartete Ordner-Struktur

### Modus „Filme"

```
Y:/Shared Movies/
├── Film A (2023)/
│   ├── Film A.mkv
│   └── movie.nfo          ← muss <hdrtype>dolbyvision</hdrtype> enthalten
├── Film B (2024)/
│   ├── Film B.mkv
│   └── movie.nfo
└── …
```

### Modus „Serien"

```
Y:/Shared Series/
└── Serie X/
    ├── Season 01/
    │   ├── Serie X - S01E01.mkv
    │   ├── Serie X - S01E01.nfo
    │   └── …
    └── Season 02/
        └── …
```

`trickplay`-Unterordner werden automatisch übersprungen.

### Modus „Ordner"

```
D:/Downloads/Film C (2025)/
├── Film C.mkv
└── movie.nfo
```

→ direkt diesen Ordner im Feld **„Film-Ordner (direkt)"** auswählen.

---

## Konfigurationsdatei

Beim Schließen wird `config/dv_remux_config.json` automatisch gespeichert (der Ordner `config/` wird beim ersten Start automatisch angelegt):

```json
{
  "ffbin":      "C:/Program Files/FFMPG/ffmpeg-8.1-full_build/bin",
  "root":       "Y:/Shared Movies",
  "behalten":   true,
  "subs":       true,
  "nfo":        true,
  "modus":      "filme",
  "embed_subs": true
}
```

| Schlüssel | Bedeutung |
|---|---|
| `ffbin` | Ordner mit `ffmpeg.exe` und `ffprobe.exe` |
| `root` | Root-Verzeichnis (Filme/Serien) bzw. Einzel-Ordner |
| `behalten` | Original-MKV nach Remux behalten |
| `subs` | Untertitel als externe `.srt` extrahieren |
| `nfo` | `movie.nfo` aktualisieren |
| `modus` | `"filme"` \| `"serien"` \| `"ordner"` |
| `embed_subs` | Untertitel in MP4 einbetten |

Eine Beispieldatei findest du in [`dv_remux_config.example.json`](dv_remux_config.example.json).

---

## Wie funktioniert das Remux?

### Normaler DV-Pfad (Profil 7 / 8)

Der Remux-Prozess läuft in **drei Phasen**:

**Phase 1 — ffmpeg (ohne faststart):**
```bash
ffmpeg -i "input.mkv" -c copy -tag:v hvc1 -map 0:v -map 0:a "output.mp4"
```

| Flag | Wirkung |
|---|---|
| `-c copy` | Streams 1:1 kopieren — kein Re-Encoding, keine Qualitätsverluste |
| `-tag:v hvc1` | HEVC-Codec-Tag auf `hvc1` setzen (LG TV erwartet das, statt `hev1`) |
| `-map 0:v -map 0:a` | Nur Video- und Audio-Streams übernehmen |

**Phase 2 — dvcC-Box injizieren** (Python-Binärpatch, kein externes Tool):

Das Tool navigiert im `moov`-Atom den Pfad `trak → mdia → minf → stbl → stsd → hvc1/dvh1` und fügt eine 16-Byte-`dvcC`-Box direkt nach der `hvcC`-Box ein. Ohne diese Box erkennt Jellyfin und LG TV kein Dolby Vision im MP4-Container. Ist die Box bereits vorhanden, wird sie übersprungen.

**Phase 3 — faststart + mp42** (Python, kein ffmpeg):

`moov` wird vor `mdat` verschoben und `major_brand` auf `mp42` gesetzt. Die `stco`/`co64`-Chunk-Offsets werden entsprechend korrigiert.

Untertitel-Streams werden separat behandelt:

- Bei **„Untertitel .srt"** über `ffmpeg -map 0:s:N -c:s srt` als externe Datei extrahiert.
- Bei **„Subs einbetten"** als `-c:s mov_text` direkt in die MP4 gemappt.

---

## Hinweise zu Dolby Vision

Das Tool prüft, ob ein Film tatsächlich Dolby Vision ist — Filme **ohne** DV werden übersprungen.

Die Erkennung läuft zweistufig:

1. **Primär:** Aus `movie.nfo` — Tag `<hdrtype>dolbyvision</hdrtype>` (tinyMediaManager-Standard).
2. **Fallback:** ffprobe-Side-Data des Video-Streams (`DOVI configuration record`).

Wenn keine NFO existiert oder kein DV erkannt wird, wird der Film im Log als `[SKIP]` markiert.

### DV-Profil-5-Konvertierung (Blaustich-Fix)

**Dolby Vision Profil 5** (typisch bei WEB-DL-Releases, erkennbar an `dvhe.05` / `IPT-PQ-C2` in MediaInfo) verwendet den **ICtCp-Farbraum** statt Standard-YUV. Geräte ohne nativen DV-Decoder interpretieren diese Daten als YUV — das Ergebnis ist ein starker **Farb-/Blaustich**.

Wenn `dovi_tool.exe` in `tools/` vorhanden ist, läuft eine **5-Schritt-Pipeline** vollautomatisch:

| Schritt | Aktion |
|---|---|
| **[1/5] HEVC extrahieren** | `ffmpeg -c:v copy -an -sn` → `%TEMP%\_dv_remux_*.hevc` |
| **[2/5] RPU P5 → P8.1** | `dovi_tool -m 3 convert` — Modus 3 ist explizit für Profil 5 → 8.1 |
| **[3/5] MP4 zusammensetzen** | P8-HEVC + Audio aus Original-MKV, `-tag:v dvh1`, kein faststart |
| **[4/5] dvcC injizieren** | Dolby Vision Configuration Record (Profil 8.1, Level auto, compat_id=1) |
| **[5/5] faststart + mp42** | `moov` vor `mdat`, `major_brand = mp42` |

Der DV-Level in der dvcC-Box wird automatisch aus Auflösung und Bildrate berechnet. Temp-Dateien in `%TEMP%` werden in jedem Fall bereinigt. Fehlt `dovi_tool.exe`, läuft der normale Remux durch — mit Warnung im Log.

**Erkennung von Profil 5** (dreistufig, ohne NFO):
1. `dv_profile` direkt aus `ffprobe`-Stream-Side-Data
2. `dv_bl_signal_compatibility_id == 0` → typisch für Profil 5 (kein HDR10-Fallback)
3. Frame-Level-Fallback via `ffprobe -read_intervals %+#1 -show_frames`

---

## Troubleshooting

| Symptom | Ursache / Lösung |
|---|---|
| **„ffmpeg nicht gefunden"** | `ffbin`-Pfad zeigt nicht auf den Ordner mit `ffmpeg.exe`. Vollen Pfad zum `bin/`-Verzeichnis setzen. |
| **„Keine MKV gefunden"** | Im Modus „Ordner" enthält der gewählte Ordner keine `.mkv`-Datei. |
| **Film wird mit `[SKIP]` markiert** | Kein `<hdrtype>dolbyvision</hdrtype>` in `movie.nfo` und ffprobe findet keine DOVI-Side-Data. |
| **LG TV spielt MP4 nicht ab** | Ab v5.3 wird automatisch eine dvcC-Box injiziert und mp42 gesetzt. Bei älteren Outputs: MP4 erneut verarbeiten. |
| **NFO-Backup wiederherstellen** | `movie.nfo.bak` einfach zurück nach `movie.nfo` kopieren. |
| **MP4 hat keine Untertitel im Player** | „Subs einbetten" muss aktiv sein **und** der Quell-Subtitle-Codec muss text-basiert sein (subrip/ass/ssa/webvtt/mov_text/srt). PGS/VobSub werden übersprungen. |
| **Tool friert kurz ein** | Beim Start eines neuen Films läuft `ffprobe` — kann je nach Datei einige Sekunden dauern. |

Bei Fehlern lohnt sich immer ein Blick ins Log unter `logs/`.

---

## Changelog

### v5.5.0
- **dvcC-Box** wird jetzt auch im normalen DV-Pfad (Profil 7/8) automatisch geprüft und injiziert
- **faststart + mp42** für alle Ausgabedateien — reiner Python-Binärpatch, kein ffmpeg-Aufruf
- Konfigurationsdatei in Unterordner `config/` verschoben (wird beim Start automatisch angelegt)
- `stco`/`co64`-Chunk-Offsets werden nach moov-Verschiebung korrekt aktualisiert

### v5.3.0
- **dvcC-Box Injektion** via Python-Binärpatch (`struct`): Dolby Vision Configuration Record direkt in die MP4 schreiben
- **5-Schritt-Pipeline** für P5→P8: HEVC → dovi_tool → MP4 (kein faststart) → dvcC → faststart+mp42
- **DV-Level-Berechnung** aus Auflösung + Bildrate für korrekte dvcC-Metadaten
- **Duplikat-Untertitel-Erkennung** — gleiche Sprache mehrfach vorhanden: nur erste Spur extrahiert, Rest als `[SKIP]` geloggt

### v5.2.0
- **DV-Profil-5-Erkennung** + automatische P5→P8.1-Konvertierung via `dowi_tool -m 3 convert` (Blaustich-Fix für ICtCp-WEB-DL-Releases)
- Dreistufige Profil-5-Erkennung: Stream-Side-Data → `dv_bl_signal_compatibility_id` → Frame-Level-Fallback

### v5.1.0
- MKV-Zielordner wählbar: „Im Filmordner" oder globaler Ordner

### v5.0.x
- `v5.0.3`: Info-Button (ℹ), ✕-Button oben rechts, Buttons vereinheitlicht
- `v5.0.2`: Schließen-Schutz + Autoscroll-Toggle
- `v5.0.1`: TrueHD-Fallback (TrueHD-Spur automatisch ausgelassen, EAC3 bleibt erhalten)
- `v5.0.0`: Initiales Release — Filme/Serien/Ordner-Modi, Simulationsmodus, Rollback, NFO-Update

---

## Drittanbieter-Komponenten

| Komponente | Autor | Lizenz | Verwendung |
|---|---|---|---|
| [dovi_tool](https://github.com/quietvoid/dovi_tool) | quietvoid | GPL v3.0 or later | DV-Profil-5 → Profil-8-Konvertierung |

dovi_tool ist **nicht im Repository enthalten**. Download: <https://github.com/quietvoid/dovi_tool/releases>

---

## Lizenz

[MIT](LICENSE) © 2026 Hero9774
