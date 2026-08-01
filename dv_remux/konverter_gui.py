"""
Fenster des Video-Konverters (Unterprogramm des DV Remux Tools).

Eigenständiges Toplevel mit eigenen Queues, eigenem Stopp-Event und eigenem
Poll-Loop – das Hauptfenster bleibt davon unberührt und kann parallel laufen.
Die ttk-Stile werden vom Hauptfenster geerbt (App._stil()).
"""

import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path

from dv_remux.config import config_speichern
from dv_remux.konstanten import VERSION
from dv_remux.konverter import QUALITAET_DEFAULT, verarbeite_videoordner
from dv_remux.sprache import t


class KonverterFenster(tk.Toplevel):
    """Ordner scannen → Auflösung ermitteln → alles Nicht-MP4 nach MP4 wandeln."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app

        # Farben vom Hauptfenster übernehmen
        self.BG, self.PANEL, self.PANEL2 = app.BG, app.PANEL, app.PANEL2
        self.BORDER, self.ACCENT, self.ACCENT2 = app.BORDER, app.ACCENT, app.ACCENT2
        self.GREEN, self.YELLOW, self.RED = app.GREEN, app.YELLOW, app.RED
        self.MUTED, self.TEXT = app.MUTED, app.TEXT

        self.title(t("konvgui.window_title"))
        self.geometry("940x780")
        self.minsize(780, 620)
        self.configure(bg=self.BG)
        self.transient(app)

        cfg = app.cfg
        # Alle Pfade kommen aus dem Hauptfenster: Quellordner, Arbeitsordner und
        # Sicherungsordner sind dieselben tk-Variablen – eine Änderung hier wirkt
        # dort und umgekehrt, es gibt keine zweite Pfadverwaltung.
        self.var_ordner     = app.var_root
        self.var_lokal_pfad = app.var_lokale_kopie_pfad
        self.var_rekursiv   = tk.BooleanVar(value=cfg.get("konv_rekursiv", False))
        # Vorgabe: Original nach erfolgreicher Konvertierung löschen
        self.var_behalten   = tk.BooleanVar(value=cfg.get("konv_behalten", False))
        self.var_amd        = tk.BooleanVar(value=cfg.get("konv_amd", True))
        self.var_sar        = tk.BooleanVar(value=cfg.get("konv_sar", True))
        # HEVC/H.265 ist die bevorzugte Vorgabe (kleinere Dateien bei gleicher Qualität)
        self.var_codec      = tk.StringVar(value=cfg.get("konv_codec", "hevc"))
        self.var_qualitaet  = tk.StringVar(value=cfg.get("konv_qualitaet", QUALITAET_DEFAULT))
        self.var_lokal      = tk.BooleanVar(value=cfg.get("konv_lokal", True))
        if not self.var_lokal_pfad.get().strip():
            self.var_lokal_pfad.set(tempfile.gettempdir())
        self.var_autoscroll = tk.BooleanVar(value=True)

        self.laeuft            = False
        self.log_queue         = queue.Queue()
        self.task_queue        = queue.Queue()
        self.fort_queue        = queue.Queue()
        self.done_queue        = queue.Queue()
        self.stopp_event       = threading.Event()
        self.letzter_log_pfad  = None
        self._aktiv            = True

        self._gui()
        self._toggle_styles_update()
        self.protocol("WM_DELETE_WINDOW", self._schliessen)
        self._poll()

    # ─── Aufbau ──────────────────────────────────────────────────────────────
    def _gui(self):
        # Titelzeile
        kopf = ttk.Frame(self)
        kopf.pack(fill="x", padx=20, pady=(16, 4))
        ttk.Button(kopf, text="✕", style="Close.TButton",
                   command=self._schliessen).pack(side="right", padx=(4, 0))
        tk.Label(kopf, text="🎬", fg=self.ACCENT2, bg=self.BG,
                 font=("Consolas", 16, "bold")).pack(side="left")
        self.titel_lbl = tk.Label(kopf, text=" " + t("konvgui.title"),
                                  fg=self.TEXT, bg=self.BG,
                                  font=("Consolas", 16, "bold"))
        self.titel_lbl.pack(side="left")
        self.untertitel_lbl = tk.Label(
            kopf, text=f"  v{VERSION}  •  {t('konvgui.subtitle')}",
            fg=self.MUTED, bg=self.BG, font=("Consolas", 9))
        self.untertitel_lbl.pack(side="left", pady=4)
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=(2, 10))

        # Einstellungs-Panel
        panel = ttk.Frame(self, style="Panel.TFrame")
        panel.pack(fill="x", padx=20, pady=(0, 8))
        panel.columnconfigure(1, weight=1)

        self.ordner_lbl = ttk.Label(panel, text=t("konvgui.folder_label"),
                                    style="Muted.TLabel")
        self.ordner_lbl.grid(row=0, column=0, padx=(12, 8), pady=(10, 0), sticky="w")
        ttk.Entry(panel, textvariable=self.var_ordner).grid(
            row=0, column=1, padx=(0, 6), pady=(10, 0), sticky="ew", ipady=4)

        def waehle_ordner():
            p = filedialog.askdirectory(title=t("konvgui.folder_dialog"),
                                        initialdir=self.var_ordner.get() or "/")
            if p:
                self.var_ordner.set(p)
        ttk.Button(panel, text="📂", style="Browse.TButton",
                   command=waehle_ordner, width=4).grid(
            row=0, column=2, padx=(0, 12), pady=(10, 0))

        self.ordner_hint = tk.Label(panel, text=t("konvgui.folder_hint"),
                                    bg=self.PANEL, fg=self.MUTED,
                                    font=("Consolas", 8))
        self.ordner_hint.grid(row=1, column=1, padx=(0, 6), pady=(2, 0), sticky="w")

        # Codec-Auswahl
        codec_frame = ttk.Frame(panel, style="Panel.TFrame")
        codec_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 2))
        self.codec_lbl = tk.Label(codec_frame, text=t("konvgui.codec_label"),
                                  bg=self.PANEL, fg=self.MUTED,
                                  font=("Consolas", 9))
        self.codec_lbl.pack(side="left", padx=(0, 10))
        self._codec_btns = {}
        for wert in ("auto", "h264", "hevc"):
            btn = ttk.Button(codec_frame, text=t(f"konvgui.codec_{wert}"),
                             command=lambda v=wert: self._set_codec(v))
            btn.pack(side="left", padx=(0, 4))
            self._codec_btns[wert] = btn

        # Qualitätsstufe
        qual_frame = ttk.Frame(panel, style="Panel.TFrame")
        qual_frame.grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 2))
        self.qual_lbl = tk.Label(qual_frame, text=t("konvgui.quality_label"),
                                 bg=self.PANEL, fg=self.MUTED,
                                 font=("Consolas", 9))
        self.qual_lbl.pack(side="left", padx=(0, 10))
        self._qual_btns = {}
        for wert in ("hoch", "mittel", "klein"):
            btn = ttk.Button(qual_frame, text=t(f"konvgui.quality_{wert}"),
                             command=lambda v=wert: self._set_qualitaet(v))
            btn.pack(side="left", padx=(0, 4))
            self._qual_btns[wert] = btn

        # Optionen
        opt = ttk.Frame(panel, style="Panel.TFrame")
        opt.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 10))
        self._opt_btns = {}
        for var, key, tipp in (
            (self.var_amd,      "konvgui.opt_amd",       "konvgui.opt_amd_tip"),
            (self.var_sar,      "konvgui.opt_sar",       "konvgui.opt_sar_tip"),
            (self.var_lokal,    "konvgui.opt_local",     "konvgui.opt_local_tip"),
            (self.var_rekursiv, "konvgui.opt_recursive", "konvgui.opt_recursive_tip"),
            (self.var_behalten, "konvgui.opt_keep",      "konvgui.opt_keep_tip"),
        ):
            btn = ttk.Button(opt, text=t(key),
                             command=lambda v=var: self._toggle_opt(v))
            btn.pack(side="left", padx=(0, 6))
            self._opt_btns[id(var)] = (btn, var, key)
            from dv_remux.gui import _Tooltip
            _Tooltip(btn, lambda k=tipp: t(k),
                     bg=self.PANEL2, fg=self.TEXT, border=self.BORDER)

        # Arbeitsordner für die lokale Verarbeitung
        lokal_frame = ttk.Frame(panel, style="Panel.TFrame")
        lokal_frame.grid(row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 10))
        lokal_frame.columnconfigure(1, weight=1)
        self.lokal_lbl = ttk.Label(lokal_frame, text=t("konvgui.workdir_label"),
                                   style="Muted.TLabel")
        self.lokal_lbl.grid(row=0, column=0, padx=(0, 8), pady=(4, 0), sticky="w")
        self.lokal_entry = ttk.Entry(lokal_frame, textvariable=self.var_lokal_pfad)
        self.lokal_entry.grid(row=0, column=1, padx=(0, 6), pady=(4, 0),
                              sticky="ew", ipady=4)

        def waehle_arbeitsordner():
            p = filedialog.askdirectory(title=t("konvgui.workdir_dialog"),
                                        initialdir=self.var_lokal_pfad.get() or "/")
            if p:
                self.var_lokal_pfad.set(p)
        self.lokal_btn = ttk.Button(lokal_frame, text="📂", style="Browse.TButton",
                                    command=waehle_arbeitsordner, width=4)
        self.lokal_btn.grid(row=0, column=2, pady=(4, 0))

        # Button-Leiste
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=20, pady=(0, 8))
        self.btn_start = ttk.Button(bf, text=t("konvgui.btn_start"), style="Run.TButton",
                                    command=lambda: self._starten(False))
        self.btn_start.pack(side="left")
        self.btn_sim = ttk.Button(bf, text=t("konvgui.btn_sim"), style="SimRun.TButton",
                                  command=lambda: self._starten(True))
        self.btn_sim.pack(side="left", padx=(8, 0))
        self.btn_stopp = ttk.Button(bf, text=t("konvgui.btn_stop"), style="Log.TButton",
                                    command=self._abbrechen, state="disabled")
        self.btn_stopp.pack(side="left", padx=(8, 0))
        self.btn_log = ttk.Button(bf, text=t("konvgui.btn_log"), style="Log.TButton",
                                  command=self._log_oeffnen, state="disabled")
        self.btn_log.pack(side="left", padx=(8, 0))
        self.btn_log_leeren = ttk.Button(bf, text=t("konvgui.btn_log_clear"),
                                         style="Log.TButton", command=self._log_leeren)
        self.btn_log_leeren.pack(side="left", padx=(8, 0))
        self.btn_autoscroll = ttk.Button(
            bf, text=f"{t('gui.on_prefix')}{t('gui.autoscroll')}",
            style="ToggleOn.TButton", command=self._toggle_autoscroll)
        self.btn_autoscroll.pack(side="left", padx=(8, 0))

        # Status + Fortschritt
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=20, pady=(0, 4))
        self.status_lbl = tk.Label(status_frame, text=t("konvgui.status_ready"),
                                   fg=self.MUTED, bg=self.BG,
                                   font=("Consolas", 10), anchor="w")
        self.status_lbl.pack(side="left")

        self.prog_main = ttk.Progressbar(self, style="Main.Horizontal.TProgressbar",
                                         mode="determinate", maximum=100)
        self.prog_main.pack(fill="x", padx=20, pady=(0, 6))

        task_border = tk.Frame(self, bg=self.BORDER)
        task_border.pack(fill="x", padx=20, pady=(0, 8))
        task_inner = tk.Frame(task_border, bg=self.PANEL2)
        task_inner.pack(fill="x", padx=1, pady=1)

        left = tk.Frame(task_inner, bg=self.PANEL2)
        left.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=10)

        def zeile(label_text):
            r = tk.Frame(left, bg=self.PANEL2)
            r.pack(fill="x", pady=1)
            lbl = tk.Label(r, text=label_text, fg=self.MUTED, bg=self.PANEL2,
                           font=("Consolas", 8), width=8, anchor="w")
            lbl.pack(side="left")
            val = tk.Label(r, text="—", fg=self.TEXT, bg=self.PANEL2,
                           font=("Consolas", 10), anchor="w")
            val.pack(side="left", fill="x", expand=True)
            return lbl, val

        self.task_datei_lbl,   self.task_datei   = zeile(t("konvgui.task_file"))
        self.task_schritt_lbl, self.task_schritt = zeile(t("konvgui.task_step"))
        self.task_status_lbl,  self.task_status  = zeile(t("gui.task_status"))
        self.task_status.configure(fg=self.GREEN, text=t("konvgui.status_ready"))

        tk.Frame(task_inner, bg=self.BORDER, width=1).pack(side="left", fill="y", pady=6)

        right = tk.Frame(task_inner, bg=self.PANEL2)
        right.pack(side="left", padx=(12, 14), pady=10)
        self.progress_lbl = tk.Label(right, text=t("gui.progress_label"),
                                     fg=self.MUTED, bg=self.PANEL2,
                                     font=("Consolas", 8))
        self.progress_lbl.pack(anchor="w")
        self.prog_sub = ttk.Progressbar(right, style="Sub.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100, length=220)
        self.prog_sub.pack(fill="x", pady=(4, 2))
        self.pct_lbl = tk.Label(right, text="0 %", fg=self.MUTED, bg=self.PANEL2,
                                font=("Consolas", 9))
        self.pct_lbl.pack(anchor="e")
        self.sim_banner = tk.Label(right, text="", fg=self.ACCENT2, bg=self.PANEL2,
                                   font=("Consolas", 8, "bold"))
        self.sim_banner.pack(anchor="w", pady=(4, 0))

        # Log
        log_outer = tk.Frame(self, bg=self.BORDER)
        log_outer.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.log_widget = scrolledtext.ScrolledText(
            log_outer, bg="#090d13", fg=self.TEXT, font=("Consolas", 9),
            wrap="word", borderwidth=0, relief="flat", state="disabled",
            selectbackground=self.ACCENT)
        self.log_widget.pack(fill="both", expand=True, padx=1, pady=1)
        for tag, farbe in (("HEAD", self.ACCENT), ("OK", self.GREEN),
                           ("ERR", self.RED), ("SKIP", self.MUTED),
                           ("INFO", self.TEXT), ("WARN", self.YELLOW),
                           ("PROG", "#4a505a")):
            self.log_widget.tag_configure(tag, foreground=farbe)
        self.log_widget.tag_configure("SIM", foreground=self.ACCENT2,
                                      font=("Consolas", 9, "bold"))

    # ─── Sprache ─────────────────────────────────────────────────────────────
    def _sprache_anwenden(self):
        """Statische Texte neu rendern (wird vom Hauptfenster aufgerufen)."""
        self.title(t("konvgui.window_title"))
        self.titel_lbl.configure(text=" " + t("konvgui.title"))
        self.untertitel_lbl.configure(text=f"  v{VERSION}  •  {t('konvgui.subtitle')}")
        self.ordner_lbl.configure(text=t("konvgui.folder_label"))
        self.ordner_hint.configure(text=t("konvgui.folder_hint"))
        self.codec_lbl.configure(text=t("konvgui.codec_label"))
        self.qual_lbl.configure(text=t("konvgui.quality_label"))
        self.lokal_lbl.configure(text=t("konvgui.workdir_label"))
        self.btn_start.configure(text=t("konvgui.btn_start"))
        self.btn_sim.configure(text=t("konvgui.btn_sim"))
        self.btn_stopp.configure(text=t("konvgui.btn_stop"))
        self.btn_log.configure(text=t("konvgui.btn_log"))
        self.btn_log_leeren.configure(text=t("konvgui.btn_log_clear"))
        self.task_datei_lbl.configure(text=t("konvgui.task_file"))
        self.task_schritt_lbl.configure(text=t("konvgui.task_step"))
        self.task_status_lbl.configure(text=t("gui.task_status"))
        self.progress_lbl.configure(text=t("gui.progress_label"))
        for wert, btn in self._codec_btns.items():
            btn.configure(text=t(f"konvgui.codec_{wert}"))
        for wert, btn in self._qual_btns.items():
            btn.configure(text=t(f"konvgui.quality_{wert}"))
        prefix = t("gui.on_prefix") if self.var_autoscroll.get() else t("gui.off_prefix")
        self.btn_autoscroll.configure(text=f"{prefix}{t('gui.autoscroll')}")
        if not self.laeuft:
            self.status_lbl.configure(text=t("konvgui.status_ready"))
        self._toggle_styles_update()

    # ─── Toggles ─────────────────────────────────────────────────────────────
    def _set_codec(self, wert):
        self.var_codec.set(wert)
        self._toggle_styles_update()

    def _set_qualitaet(self, wert):
        self.var_qualitaet.set(wert)
        self._toggle_styles_update()

    def _toggle_opt(self, var):
        var.set(not var.get())
        self._toggle_styles_update()

    def _toggle_styles_update(self):
        for wert, btn in self._codec_btns.items():
            btn.configure(style="ToggleOn.TButton" if wert == self.var_codec.get()
                          else "Toggle.TButton")
        for wert, btn in self._qual_btns.items():
            btn.configure(style="ToggleOn.TButton" if wert == self.var_qualitaet.get()
                          else "Toggle.TButton")
        for btn, var, key in self._opt_btns.values():
            label = t(key)
            if var.get():
                btn.configure(style="ToggleOn.TButton",
                              text=f"{t('gui.on_prefix')}{label}")
            else:
                btn.configure(style="Toggle.TButton",
                              text=f"{t('gui.off_prefix')}{label}")
        # Arbeitsordner nur bedienbar, wenn lokal verarbeitet wird
        zustand = "normal" if self.var_lokal.get() else "disabled"
        self.lokal_entry.configure(state=zustand)
        self.lokal_btn.configure(state=zustand)

    def _toggle_autoscroll(self):
        val = not self.var_autoscroll.get()
        self.var_autoscroll.set(val)
        prefix = t("gui.on_prefix") if val else t("gui.off_prefix")
        self.btn_autoscroll.configure(
            style="ToggleOn.TButton" if val else "Toggle.TButton",
            text=f"{prefix}{t('gui.autoscroll')}")
        if val:
            self.log_widget.see("end")

    # ─── Log ─────────────────────────────────────────────────────────────────
    def _log(self, typ: str, text: str):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text + "\n", typ)
        self.log_widget.configure(state="disabled")
        if self.var_autoscroll.get():
            self.log_widget.see("end")

    def _log_leeren(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _log_oeffnen(self):
        if self.letzter_log_pfad and Path(self.letzter_log_pfad).exists():
            if sys.platform == "win32":
                os.startfile(self.letzter_log_pfad)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(self.letzter_log_pfad)])
            else:
                subprocess.run(["xdg-open", str(self.letzter_log_pfad)])
        else:
            messagebox.showinfo(t("gui.log_dialog_title"),
                                t("gui.log_not_yet_available"), parent=self)

    # ─── Config ──────────────────────────────────────────────────────────────
    def _config_uebernehmen(self):
        """Eigene Keys in die App-Config spiegeln und speichern.
        App._config_dict() baut auf app.cfg auf, daher bleiben sie erhalten."""
        # Die Pfade gehören dem Hauptfenster und werden dort gespeichert
        # (root / lokale_kopie_pfad / old_mkv_pfad) – hier nur die eigenen Optionen.
        self.app.cfg.update({
            "konv_rekursiv":  self.var_rekursiv.get(),
            "konv_behalten":  self.var_behalten.get(),
            "konv_amd":       self.var_amd.get(),
            "konv_sar":       self.var_sar.get(),
            "konv_codec":     self.var_codec.get(),
            "konv_qualitaet": self.var_qualitaet.get(),
            "konv_lokal":     self.var_lokal.get(),
        })
        config_speichern(self.app._config_dict())

    # ─── Start / Stopp / Schließen ───────────────────────────────────────────
    def _starten(self, simulation: bool):
        if self.laeuft:
            return

        fehler = []
        ffmpeg  = self.app._ffmpeg_pfad()
        ffprobe = self.app._ffprobe_pfad()
        if not simulation:
            if not Path(ffmpeg).is_file():
                fehler.append(t("gui.err_ffmpeg_not_found", name=Path(ffmpeg).name))
            if not Path(ffprobe).is_file():
                fehler.append(t("gui.err_ffprobe_not_found", name=Path(ffprobe).name))
        ordner = self.var_ordner.get().strip()
        if not ordner or not Path(ordner).is_dir():
            fehler.append(t("konvgui.err_folder"))

        arbeits_pfad = None
        if self.var_lokal.get():
            p = self.var_lokal_pfad.get().strip()
            if not p:
                fehler.append(t("konvgui.err_workdir_missing"))
            elif not simulation:
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    arbeits_pfad = Path(p)
                except OSError as e:
                    fehler.append(t("gui.err_workdir_unreachable", fehler=e))
            else:
                arbeits_pfad = Path(p)
        # ffprobe wird auch in der Simulation gebraucht: die Auflösung soll
        # auch dort real ermittelt werden (es wird nur nichts geschrieben).
        if simulation and not Path(ffprobe).is_file():
            fehler.append(t("gui.err_ffprobe_not_found", name=Path(ffprobe).name))

        # Sicherungsordner ebenfalls aus dem Hauptfenster: „Backup-Ordner global"
        # → dorthin, sonst in den Unterordner „old video" beim Original.
        sicherungs_pfad = None
        if self.var_behalten.get() and self.app.var_old_mkv_modus.get() == "global":
            p = self.app.var_old_mkv_pfad.get().strip()
            if p:
                sicherungs_pfad = Path(p)

        if fehler:
            for f in fehler:
                self._log("ERR", f"❌  {f}")
            return

        self._config_uebernehmen()
        self.stopp_event.clear()
        self.laeuft = True
        self.btn_start.configure(state="disabled")
        self.btn_sim.configure(state="disabled")
        self.btn_stopp.configure(state="normal")
        self.btn_log.configure(state="disabled")
        self.status_lbl.configure(text=t("gui.status_running"), fg=self.MUTED)
        self.prog_main["value"] = 0
        self.prog_sub["value"] = 0
        self.pct_lbl.configure(text="0 %")
        self.task_status.configure(text=t("gui.task_status_running"), fg=self.YELLOW)
        self._log_leeren()
        self.sim_banner.configure(text=t("gui.sim_banner") if simulation else "")
        if simulation:
            self._log("SIM", t("gui.sim_log_notice"))

        threading.Thread(
            target=verarbeite_videoordner,
            args=(ffmpeg, ffprobe, ordner, simulation,
                  self.var_rekursiv.get(), self.var_behalten.get(),
                  self.var_codec.get(), self.var_qualitaet.get(),
                  self.var_amd.get(), self.var_sar.get(),
                  self.log_queue, self.task_queue, self.fort_queue,
                  self.done_queue, self.stopp_event, sicherungs_pfad,
                  self.var_lokal.get(), arbeits_pfad),
            daemon=True
        ).start()

    def _abbrechen(self):
        self.stopp_event.set()
        self.btn_stopp.configure(state="disabled")
        self.status_lbl.configure(text=t("gui.status_cancelling"), fg=self.YELLOW)

    def _schliessen(self):
        if self.laeuft:
            if not messagebox.askyesno(t("gui.close_confirm_title"),
                                       t("konvgui.close_confirm"),
                                       icon="warning", default="no", parent=self):
                return
            self.stopp_event.set()
        self._config_uebernehmen()
        self._aktiv = False
        self.app.konverter_fenster = None
        self.destroy()

    # ─── Poll-Loop ───────────────────────────────────────────────────────────
    def _poll(self):
        if not self._aktiv:
            return
        try:
            while True:
                self.prog_main["value"] = self.fort_queue.get_nowait()
        except queue.Empty:
            pass

        try:
            while True:
                info = self.task_queue.get_nowait()
                if "film" in info:
                    self.task_datei.configure(text=info["film"])
                if "schritt" in info:
                    self.task_schritt.configure(text=info["schritt"])
                if "sub_prog" in info:
                    pct = info["sub_prog"]
                    if pct is None:
                        self.prog_sub.stop()
                        self.prog_sub.configure(mode="indeterminate")
                        self.prog_sub.start(12)
                        self.pct_lbl.configure(text="…")
                    else:
                        self.prog_sub.stop()
                        self.prog_sub.configure(mode="determinate")
                        self.prog_sub["value"] = pct
                        self.pct_lbl.configure(text=f"{pct} %")
        except queue.Empty:
            pass

        try:
            while True:
                typ, text = self.log_queue.get_nowait()
                self._log(typ, text)
        except queue.Empty:
            pass

        try:
            stats, log_pfad = self.done_queue.get_nowait()
            self.laeuft = False
            self.btn_start.configure(state="normal")
            self.btn_sim.configure(state="normal")
            self.btn_stopp.configure(state="disabled")
            self.sim_banner.configure(text="")
            self.letzter_log_pfad = log_pfad
            self.btn_log.configure(state="normal" if log_pfad else "disabled")
            farbe = self.GREEN if stats["fehler"] == 0 else self.YELLOW
            self.status_lbl.configure(
                text=t("konvgui.status_done", ok=stats["konvertiert"],
                       err=stats["fehler"], skip=stats["uebersprungen"]),
                fg=farbe)
            self.task_status.configure(
                text=t("gui.task_status_done",
                       name=Path(log_pfad).name if log_pfad else "—"),
                fg=self.GREEN)
        except queue.Empty:
            pass

        self.after(80, self._poll)


def oeffne_konverter(app):
    """Konverter-Fenster öffnen (oder ein bereits offenes nach vorn holen)."""
    vorhanden = getattr(app, "konverter_fenster", None)
    if vorhanden is not None and vorhanden.winfo_exists():
        vorhanden.deiconify()
        vorhanden.lift()
        vorhanden.focus_force()
        return vorhanden
    fenster = KonverterFenster(app)
    app.konverter_fenster = fenster
    return fenster
