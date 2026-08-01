# Bug- und Tippfehler-Bericht — dv_remux v5.9.0

Stand: 01.08.2026 · geprüft: alle Python-Module, `lang/de.json`, `lang/en.json`, `CLAUDE.md`

Statische Prüfung (`compileall`, `pyflakes`) ist **sauber** — keine Syntaxfehler, keine
ungenutzten Importe, keine undefinierten Namen. Alle 306 Übersetzungs-Keys existieren in
beiden Sprachdateien, Platzhalter stimmen überein. Die folgenden Befunde stammen aus
manueller Code-Analyse; die als *verifiziert* markierten wurden durch Ausführen nachgestellt.

---

## 🔴 Kritisch — Datenverlust möglich

### K1 · `verschiebe_sicher()` löscht die Datei, wenn Quelle == Ziel
`dv_remux/dateioperationen.py:47` (in `kopiere_mit_fortschritt`) — **verifiziert**

```python
with open(quelle, "rb") as fin, open(ziel, "wb") as fout:
```

`open(ziel, "wb")` trunkiert das Ziel, *bevor* gelesen wird. Sind beide Pfade identisch,
ist die Datei sofort 0 Byte; danach schlägt die Größenprüfung fehl und
`dateioperationen.py:98` `ziel.unlink(missing_ok=True)` löscht sie ganz.
**Die MKV ist weg.**

Erreichbar: Nutzer trägt als „Lokale Arbeitskopie"-Ordner denselben Ordner ein, in dem
der Film liegt. `gui.py:862-872` prüft nur `mkdir`, nicht ob der Pfad ≠ `root` ist.

**Fix** — am Anfang beider Funktionen:
```python
try:
    if quelle.resolve() == ziel.resolve():
        return True
except OSError:
    pass
```
Zusätzlich in `App._starten()` prüfen, dass der Arbeitsordner nicht innerhalb von `root` liegt.

---

### K2 · `injiziere_dvcc_box()` zerstört die Datei, wenn `moov` nicht am Ende liegt — und meldet `True`
`dv_remux/mp4_binary.py:168-173` — **verifiziert** (Test-MP4: 5459 → 515 Byte, Rückgabe `True`)

```python
with open(mp4_pfad, "r+b") as f:
    f.seek(moov_off)
    f.write(moov)
    f.truncate(moov_off + len(moov))
return True
```

Der Docstring fordert „moov am Ende der Datei", die Funktion prüft das aber nie. Bei einer
Faststart-MP4 (moov vorne) überschreibt der Write den `mdat`-Anfang und `truncate()` wirft
den Rest weg — das ganze Video. Zurzeit rufen alle Worker-Pfade mit `kein_faststart=True`
auf, die Bedingung hält also *zufällig*. Ein Aufruf auf eine bereits fertige MP4
(z. B. Reparatur) vernichtet sie.

**Fix** — vor dem Schreiben:
```python
if moov_off is None or moov_off + moov_sz < datei_sz:
    return False   # moov nicht letzte Box – Truncate wäre destruktiv
```

---

### K3 · `mache_faststart_und_ftyp()`: fehlgeschlagenes `rename` löscht die einzige Kopie
`dv_remux/mp4_binary.py:304-310`

```python
mp4_pfad.unlink()
tmp_pfad.rename(mp4_pfad)
return True
except Exception:
    if tmp_pfad.exists():
        tmp_pfad.unlink(missing_ok=True)
    raise
```

Zwischen `unlink()` und `rename()` existiert die Datei nur als `*._fstmp.mp4`. Schlägt das
Rename fehl (unter Windows realistisch: Virenscanner, Jellyfin-Scan, Explorer halten die
frische Datei kurz offen → `WinError 32`), löscht der `except`-Block genau diese Kopie.
Verschärfend: der äußere Handler macht daraus ein stilles `return False`, und beide
Aufrufer werten das nur als Warnung — `pipeline.py:286-291` gibt sogar `True` zurück.
Bei „Original nicht behalten" wird die MKV danach gelöscht.

**Fix** — `os.replace(tmp_pfad, mp4_pfad)` (atomar, ohne vorheriges `unlink`); im `except`
die Temp-Datei nur löschen, wenn das Original nachweislich noch existiert.

---

## 🟠 Hoch — GUI hängt / Original bleibt liegen

### H1 · Remux-Worker ohne Top-Level-`try/except` → GUI friert dauerhaft ein
`dv_remux/worker.py:25` (`verarbeite_serien`), `:304` (`verarbeite_sammlung`),
`:572` (`verarbeite_einzelordner`) — **verifiziert**

Keiner der drei Worker ist gekapselt (`verarbeite_einzelordner` hat ab `:685` nur ein
`try/finally` ohne `except`). Fliegt eine Exception — NAS-Freigabe weg, `PermissionError`
bei `iterdir()`, `stat()` auf eine gerade gelöschte Datei — stirbt der Thread und
**`done_q` bekommt nie ein Signal**. In `gui.py:980-999` bleibt `self.läuft = True`:
Start/Simulation bleiben deaktiviert, Stopp bewirkt nichts, nur Neustart hilft.

`konverter.py:807-811` macht es richtig (globales `except Exception` + `finally: done_q.put(...)`).

**Fix** — jeden Worker-Körper kapseln:
```python
try:
    ...
except Exception as e:
    log("ERR", ...); stats["fehler"] += 1
finally:
    done_q.put((stats, schreibe_log_datei(log_zeilen, simulation)))
```
und die verstreuten `done_q.put`-Aufrufe (`:611, :631, :700, :711, :754, :832`) entfernen.
Löst zugleich H3.

### H2 · Beenden während eines Laufs killt den Worker — Rollback/Restore läuft nie
`dv_remux/gui.py:636-652`

`stopp_event.set()` wird gesetzt, danach sofort `self.destroy()` — kein `join`, kein
verzögertes Schließen (`grep join` findet nichts). Die Threads sind `daemon=True`
(`gui.py:936`), werden also hart abgebrochen. Der `finally`-Block in `worker.py:793ff`,
der die Original-MKV vom Arbeitsordner zurückschiebt, läuft nicht zu Ende — bei aktivem
„Lokal verarbeiten" liegt das Original dann im Temp-Ordner, während es am Quellort schon
gelöscht ist. Der Dialogtext verspricht das Gegenteil.

**Fix** — Thread als Attribut merken und per `self.after(200, self._warte_auf_ende)`
pollen, bis `not thread.is_alive()`, erst dann `destroy()`.

### H3 · Log-Datei wird geschrieben, bevor die Restore-Meldungen entstehen
`dv_remux/worker.py:700, :711, :754`

Diese `return`s liegen im `try:`; der `finally`-Block ab `:793` erzeugt danach erst die
wichtigsten Meldungen (`worker.restore_done`, `restore_failed`, `restore_target_occupied` —
also „wo liegt meine Original-MKV jetzt?"). Die landen im GUI-Log, aber **nicht in der
Log-Datei**. → mit H1 zusammen lösen.

### H4 · Konverter sendet zwei `done`-Signale und schreibt die Log-Datei doppelt
`dv_remux/konverter.py:626` und `:657` gegen `finally:` in `:810-811` — **verifiziert**

Beide `return` liegen im `try:`, dessen `finally` erneut `done_q.put((stats,
schreibe_konverter_log(...)))` ausführt. `schreibe_konverter_log()` läuft zweimal, und
`KonverterFenster._poll()` verarbeitet ein zweites „Fertig".

**Fix** — Zeilen 626 und 657 streichen, nur `return` stehen lassen.

---

## 🟡 Mittel

| # | Datei:Zeile | Problem | Fix |
|---|---|---|---|
| M1 | `mkv_analyse.py:227` | `def _parse_dovi_entry(entry: dict) -> int \| None:` — PEP-604-Syntax gibt es erst ab Python **3.10**, kein `from __future__ import annotations`. Dokumentiert ist 3.8+. Der `def` steht *vor* dem `try:` (`:244`) → `TypeError` propagiert in den Worker-Thread. | `-> "int \| None"` oder `Optional[int]` |
| M2 | `pipeline.py:202-208` | P5→P8-Pfad wandelt **DTS nicht** nach E-AC3: `dts_audio_argumente()` wird dort nicht benutzt, `worker.py` reicht `dts_zu_eac3` gar nicht durch. Laut CLAUDE.md verweigert der Player dann die *ganze* Datei — obwohl der Nutzer die Option aktiviert hat. | `dts_zu_eac3` durchreichen, `dts_audio_argumente()` in `befehl3` einhängen |
| M3 | `pipeline.py:180-186` | Sind **alle** Spuren TrueHD, ist `kompatible` leer → Fallback auf alle Spuren, aber es wird trotzdem „⚠ n TrueHD-Spur(en) ausgelassen" geloggt. Falsche Meldung, ffmpeg scheitert danach. | Meldung an `if kompatible and len(...) < len(...)` binden, leeren Fall eigens behandeln |
| M4 | `pipeline.py:243-251, :268` | Fehlgeschlagener Schritt 3/4 lässt eine halbfertige `.mp4` liegen (`cleanup()` räumt nur TEMP). `remux_zu_mp4()` macht es korrekt. | `mp4_pfad.unlink(missing_ok=True)` vor `return False` |
| M5 | `pipeline.py:159-162` | Einziger `subprocess.run(..., text=True)` **ohne** `encoding="utf-8"`. Auf deutschem Windows dekodiert Python mit cp1252, ffprobe liefert UTF-8 → `UnicodeDecodeError` bei Umlaut im Pfad, verschluckt von `except Exception: pass`; `dv_level` bleibt still auf 6 (falsch für 4K/50p und 60p). | `encoding="utf-8", errors="replace"` ergänzen |
| M6 | `pipeline.py:330-378` | SRT-Extraktion bekommt kein `stopp_event` und hat kein `timeout=`. Stopp-Button wirkungslos; hängendes ffmpeg blockiert den Thread dauerhaft. | `stopp_event` durchreichen + prüfen, `timeout=` setzen |
| M7 | `worker.py:75-78` | Serien-Modus: `show_ordner` selbst kommt nur in die Liste, wenn er *gar keine* Unterordner hat. Lose Episoden neben `Season 1/` werden nie verarbeitet. `:58` verschärft das: Root mit MKVs *und* Show-Ordnern → beides fällt durch. | `staffeln = ([show_ordner] if any(show_ordner.glob("*.mkv")) else []) + unterordner` |
| M8 | `konverter.py:548-550` | `sichere_original()`: existiert im Sicherungsordner schon eine gleichnamige Datei, wird nur gewarnt — das Original bleibt im temporären `dv_konverter`-Ordner verwaist und wird beim nächsten Lauf (`:739`) gelöscht. | eindeutigen Namen wählen oder Datei per `verschiebe_sicher()` zurückschieben |
| M9 | `konverter.py:517` | Ausgeschlossen wird nur nach den fest verdrahteten Namen `"old video"`/`"dv_konverter"`; der echte `sicherungs_pfad` kommt aus dem Hauptfenster. Liegt er im gescannten Baum, werden gesicherte Originale erneut konvertiert. | `sicherungs_pfad`/`arbeits_pfad` an `sammle_dateien()` durchreichen, `p.is_relative_to(...)` |
| M10 | `konstanten.py:14` | `DOVI_TOOL` fest auf `.exe` — auf Linux/macOS nie gefunden, P5→P8 schaltet sich stumm ab. `gui.py:784` macht es für ffmpeg plattformabhängig. | `("dovi_tool.exe" if sys.platform == "win32" else "dovi_tool")` |
| M11 | `dateioperationen.py:141` | `shutil.move()` über Laufwerksgrenzen (lokal → NAS) fällt auf `copy2 + unlink` zurück: stundenlang, ohne Fortschritt, **nicht abbrechbar**, kein `undo_log`-Eintrag. Überall sonst wird `verschiebe_sicher()` benutzt. | `verschiebe_sicher()` verwenden |
| M12 | `config.py:9-22` | Beschädigte Config wird still verworfen; beim Schließen überschreibt `config_speichern()` die noch reparierbare Datei. `write_text` ist nicht atomar. | bei Parse-Fehler nach `.bak` umbenennen + melden; beim Speichern Temp-Datei + `os.replace()` |
| M13 | `gui.py:843-857` | Im Simulationsmodus wird **ffprobe nicht geprüft**, aber `worker.py:106/360/623` ruft `ermittle_hdrtype_aus_mkv()` ohne Simulations-Guard auf. Fehlt ffprobe → jeder Film „Kein Dolby Vision, übersprungen", 0 Treffer, kein Fehler. `konverter_gui.py:426-429` macht es richtig. | ffprobe-Prüfung aus dem `if not ist_sim`-Block herausziehen |
| M14 | `gui.py:862-872` | Simulation legt trotzdem Verzeichnisse an (`mkdir`), obwohl `gui.sim_log_notice` „keine Dateien werden verändert" verspricht. `konverter_gui.py:418-425` löst es korrekt. | `mkdir` nur bei `not ist_sim` |
| M15 | `konverter_gui.py:380-394` + `gui.py:651` | `konv_*`-Einstellungen werden nur in `_config_uebernehmen()` gespiegelt; schließt man das **Hauptfenster** mit ✕, stirbt das Toplevel als Kind ohne eigenen Handler → Änderungen verworfen. | in `App._schliessen()` vor `config_speichern` `konv._config_uebernehmen()` aufrufen |
| M16 | `mp4_binary.py:164-166` | `_box()` parst den 64-Bit-Fall (`size == 1`) korrekt, das Zurückschreiben nimmt aber bedingungslos 32 Bit an → aus dem Marker `1` wird eine 33-Byte-Box, Box-Baum zerstört. Praktisch tritt das im moov nicht auf. | Header-Breite mitführen, ggf. `>Q` an `soff + 8` |
| M17 | `mkv_analyse.py:11-19` | `lese_hdrtype_aus_nfo()` fängt nur `ET.ParseError`; `FileNotFoundError`, `PermissionError`, `LookupError` (unbekanntes `encoding=` in der XML-Deklaration) reißen den ganzen Batch ab statt nur einen Ordner zu überspringen. | `except Exception: pass` (wie im Rest des Moduls) |
| M18 | `pipeline.py:193-198` | Im P5→P8-Pfad fehlt der `status3 == "zu_stark"`-Zweig ganz. Bei echtem anamorphem Material bleibt die MP4 anamorph, Jellyfin verweigert Direct Play — **ohne jeden Log-Hinweis**. `remux_zu_mp4:727-731` macht es richtig. | denselben `schluessel`-Dispatch verwenden |

---

## 🔵 Kosmetisch / Konsistenz

**Code**

- `konverter.py:79` — `HEVC_AB_HOEHE = 1080`, aber benutzt als `hoehe > HEVC_AB_HOEHE` (`:287`). Bei exakt 1080p wird H.264 gewählt. Entweder `>=` oder Konstante in `HEVC_UEBER_HOEHE` umbenennen.
- `gui.py:360-364` — Kommentar behauptet zwei Labels, es ist dasselbe Objekt (`self.root_lbl = self.root_entry_lbl`); `_modus_update()` überschreibt den Text sofort → Key `gui.root_folder` ist **nie sichtbar** (einziger toter Key).
- `gui.py:843-857` — `else:`-Block wiederholt die Zeilen davor wörtlich; `ist_sim = simulation` ist ein reiner Alias.
- `gui.py:166` / `konverter_gui.py:299` — Sprachumschaltung aktualisiert `task_status_lbl`, aber nicht `self.task_status` → dort steht weiter „Bereit" statt „Ready".
- `gui.py:135` — ungültiger Sprachcode in der Config: Oberfläche läuft auf Englisch, Dropdown zeigt „Deutsch", der ungültige Code wird zurückgeschrieben.
- `gui.py:638-645` — Abbruchdialog sagt immer „Ein Remux-Prozess ist aktiv.", auch wenn nur der Konverter läuft.
- `gui.py:824-831` / `konverter_gui.py:367-374` — `_log_oeffnen()` ohne `try/except`: fehlt `xdg-open`, fliegt ein `FileNotFoundError` in den Tk-Callback.
- `sprache.py:62` — `_bereinige_log()` ersetzt nur `ℹ️` (mit Variation Selector), nicht das nackte `ℹ` (U+2139). Betroffen u. a. `p5p8.anamorph_fix`, `remux.anamorph_fix`, `remux.dts_zu_eac3`, `remux.av1_detected` → landen roh in der Log-Datei.
- `sprache.py:44` / `dv_remux_gui.py:73` — Docstring sagt Fallback „→ Deutsch", tatsächlich `SPRACHE_DEFAULT = "en"`.
- `konstanten.py:20-21` — legt beim **Import** vier Ordner an; auf schreibgeschütztem Pfad scheitert schon der Import mit nacktem Traceback. `_d` bleibt als Modulattribut zurück.
- `pipeline.py:416, :511, :803` — `log_q.put(...)` ohne das sonst gepaarte `log_zeilen.append(...)` → diese Zeilen fehlen in der Log-Datei.
- `pipeline.py:450-454` — Early-Return ohne `task_q.put`, Statusanzeige bleibt auf „NFO wird aktualisiert …" stehen.
- `pipeline.py:344` — `if total else 0` ist innerhalb des `enumerate`-Loops unerreichbar.
- `pipeline.py:481-497` — behandelt `Film.deu.2.srt`, aber `extrahiere_untertitel()` erzeugt Zweitspuren derselben Sprache nie.
- `pipeline.py:319/333/334` — `sprachzähler`: einziger Umlaut-Identifier im Projekt.
- `mp4_binary.py:390` und Keys `p5p8.step4_*` sprechen von „dvcC-Box", geschrieben wird bei Profil 8 aber **dvvC** (`_dv_box_typ`). Führt bei der Fehlersuche mit ffprobe in die Irre.
- `konverter.py:534` `basis_ordner` wird nie benutzt · `:374` `ffmpeg_sw_encoder` enthält ein Encoder-Dict, kein ffmpeg · `:216` `dar` wird berechnet, nie gelesen.
- Statistik: `worker.py:371/424` zählt denselben Film als `gefunden` **und** `uebersprungen`; `konverter.py:610` setzt `gefunden` vor der Analyse, fehlgeschlagene zählen doppelt — und `gefunden` wird in der GUI gar nicht angezeigt.
- `worker.py:611/631` — `done_q.put((stats, None))` ohne Log-Datei: im Einzelordner-Modus bleibt „Log öffnen" bei „keine MKV"/„kein DV" deaktiviert.

**Tippfehler in den Sprachdateien**

| Datei:Zeile | Ist | Soll |
|---|---|---|
| `de.json:91` | „Rollback: Nichts **zu** rückgängig zu machen." | „Rollback: Nichts rückgängig zu machen." (doppeltes „zu") |
| `de.json:131` | „{anzahl} **Englischer** Untertitel **wird** eingebettet" | Numerus + Großschreibung falsch → „{anzahl} englische Untertitelspur(en) werden eingebettet" |
| `de.json:355` | `„global"` / `„old video"` | gemischte Anführungszeichen: deutsches `„` mit ASCII-`"` → `„global“` / `„old video“` |
| `de.json:153,155` | „Uebersprungen", „veraendert" | ASCII-Transliteration, obwohl sonst überall „übersprungen" steht → vereinheitlichen |
| `en.json` | `normalising` (25,71), `recognised` (63), `recognise` (212) vs. `signaling` (62), `Analyzing` (127,128), `color` (125) | gemischtes en-GB/en-US → eine Variante wählen |
| `en.json:157` | `"  Location:        {pfad}"` | Spaltenausrichtung passt nicht zu `worker.summary_log_saved` → `"  Location:  {pfad}"` |
| `en.json:199,201` vs `218,220` | „ffmpeg  Folder", „Source Folder" vs. „Backup folder", „Work folder" | uneinheitliche Groß-/Kleinschreibung (und doppeltes Leerzeichen in `ffmpeg  Folder`) |

**CLAUDE.md ist veraltet**

- Z. 15: „aktuell **v5.0**" → tatsächlich `VERSION = "5.9.0"`
- Z. 17: „Zwei Betriebsmodi" → es sind drei (`filme` / `serien` / `ordner`)
- Z. 113-125: Pfad `dv_remux_config.json` → tatsächlich `config/dv_remux_config.json`; der Key `"sim"` existiert nicht mehr (Simulation ist ein Button-Parameter)
- Z. 96: Log-Tags ohne `"INFO"`, `"WARN"`, `"FOLDER"` (in `gui.py:593-598` konfiguriert)
- Z. 145: Rollback „bei Abbruch **oder Fehler**" → `rollback_session()` wird nur bei gesetztem `stopp_event` gerufen (`worker.py:281, 546, 812`)
- Z. 65: „Ziel-Codec-Vorgabe ist HEVC" → `waehle_ziel_codec()` wählt bei „auto" bis 1080p H.264

---

## Empfohlene Reihenfolge

1. **K1** (Quelle == Ziel) — einzeiliger Guard, verhindert Totalverlust
2. **H1 + H3** (Worker kapseln) — eine Änderung, löst zwei Befunde
3. **K3** (`os.replace`) und **K2** (moov-Position prüfen)
4. **M1** (PEP-604), falls Python < 3.10 unterstützt bleiben soll
5. **H2/H4**, dann der Rest nach Bedarf
