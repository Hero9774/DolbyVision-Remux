"""Die tkinter-GUI (App-Klasse)."""

import os
import sys
import queue
import shutil
import subprocess
import tempfile
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path

from dv_remux.konstanten import VERSION, DOVI_TOOL, SPRACHE_DEFAULT, SPRACHEN
from dv_remux.config import config_laden, config_speichern
from dv_remux.sprache import t, setze_sprache
from dv_remux.worker import verarbeite_sammlung, verarbeite_serien, verarbeite_einzelordner


class _Tooltip:
    """Einfacher Hover-Tooltip für ein Widget.
    text_func wird beim Anzeigen aufgerufen (statt festem Text), damit der
    Tooltip immer die aktuell gewählte Sprache verwendet."""

    def __init__(self, widget, text_func, *, bg="#1c2128", fg="#e6edf3",
                 border="#30363d", verzoegerung=450, breite=380):
        self.widget = widget
        self.text_func = text_func
        self.bg, self.fg, self.border = bg, fg, border
        self.verzoegerung = verzoegerung
        self.breite = breite
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._geplant_zeigen, add="+")
        widget.bind("<Leave>", self._verstecken, add="+")
        widget.bind("<ButtonPress>", self._verstecken, add="+")

    def _geplant_zeigen(self, _event=None):
        self._after_id = self.widget.after(self.verzoegerung, self._zeigen)

    def _zeigen(self):
        if self._tip is not None:
            return
        try:
            text = self.text_func()
        except Exception:
            return
        if not text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.configure(bg=self.border)
        tk.Label(self._tip, text=text, justify="left",
                 bg=self.bg, fg=self.fg, font=("Consolas", 9),
                 wraplength=self.breite, padx=8, pady=6,
                 bd=0).pack(padx=1, pady=1)

    def _verstecken(self, _event=None):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class App(tk.Tk):

    BG      = "#0d1117"
    PANEL   = "#161b22"
    PANEL2  = "#1c2128"
    BORDER  = "#30363d"
    ACCENT  = "#58a6ff"
    ACCENT2 = "#f78166"
    GREEN   = "#3fb950"
    YELLOW  = "#d29922"
    RED     = "#f85149"
    MUTED   = "#8b949e"
    TEXT    = "#e6edf3"

    def __init__(self):
        super().__init__()
        self.title(f"DV Remux Tool  v{VERSION}  •  Jellyfin / LG TV")
        self.geometry("980x860")
        self.minsize(800, 660)
        self.configure(bg=self.BG)

        self.läuft             = False
        self.log_queue         = queue.Queue()
        self.task_queue        = queue.Queue()
        self.fort_queue        = queue.Queue()
        self.done_queue        = queue.Queue()
        self.letzter_log_pfad  = None
        self.cfg               = config_laden()

        self.var_sprache = tk.StringVar(value=self.cfg.get("sprache") or SPRACHE_DEFAULT)
        setze_sprache(self.var_sprache.get())

        self.var_ffbin    = tk.StringVar(value=self.cfg.get("ffbin",    self._auto_ffbin()))
        self.var_root     = tk.StringVar(value=self.cfg.get("root",     ""))
        self.var_behalten = tk.BooleanVar(value=self.cfg.get("behalten",True))
        self.var_subs     = tk.BooleanVar(value=self.cfg.get("subs",    True))
        self.var_nfo      = tk.BooleanVar(value=self.cfg.get("nfo",     True))
        self.var_modus          = tk.StringVar(value=self.cfg.get("modus",          "filme"))
        self.var_embed_subs     = tk.BooleanVar(value=self.cfg.get("embed_subs",     False))
        self.var_old_mkv_modus  = tk.StringVar(value=self.cfg.get("old_mkv_modus",  "lokal"))
        self.var_old_mkv_pfad   = tk.StringVar(value=self.cfg.get("old_mkv_pfad",   ""))
        self.var_lokale_kopie      = tk.BooleanVar(value=self.cfg.get("lokale_kopie",      False))
        self.var_lokale_kopie_pfad = tk.StringVar(
            value=self.cfg.get("lokale_kopie_pfad") or tempfile.gettempdir())
        self.var_autoscroll     = tk.BooleanVar(value=True)
        self.stopp_event    = threading.Event()

        self._stil()
        self._gui()
        self._modus_update()
        self._toggle_styles_update()
        self._ffbin_status_update()
        self._dovi_status_update()
        self.var_modus.trace_add("write", lambda *_: self._modus_update())
        self.var_ffbin.trace_add("write", self._ffbin_status_update)
        self.protocol("WM_DELETE_WINDOW", self._schliessen)
        self._sprache_anwenden()
        self._poll()

    # ─── Sprache ──────────────────────────────────────────────────────────────
    def _baue_sprach_dropdown(self, parent):
        anzeige_zu_code = {name: code for code, name in SPRACHEN.items()}
        var_anzeige = tk.StringVar(value=SPRACHEN.get(self.var_sprache.get(), "Deutsch"))

        def _on_wahl(_event=None):
            code = anzeige_zu_code.get(var_anzeige.get())
            if code and code != self.var_sprache.get():
                self.var_sprache.set(code)
                setze_sprache(code)
                self._sprache_anwenden()
                config_speichern(self._config_dict())

        box = ttk.Combobox(
            parent, textvariable=var_anzeige, values=list(SPRACHEN.values()),
            state="readonly", width=9, font=("Consolas", 9))
        box.bind("<<ComboboxSelected>>", _on_wahl)
        return box

    def _sprache_anwenden(self):
        """Rendert alle statischen Texte in der aktuell gewählten Sprache neu."""
        self.ffbin_lbl.configure(text=t("gui.ffmpeg_folder"))
        self.modus_lbl.configure(text=t("gui.mode_label"))
        self.backup_lbl.configure(text=t("gui.backup_folder_label"))
        self.backup_entry_lbl.configure(text=t("gui.backup_folder_entry_label"))
        self.workdir_entry_lbl.configure(text=t("gui.workdir_label"))
        self.btn_start.configure(text=t("gui.btn_start"))
        self.btn_sim.configure(text=t("gui.btn_sim"))
        self.btn_stopp.configure(text=t("gui.btn_stopp"))
        self.btn_log.configure(text=t("gui.btn_log_open"))
        self.btn_log_leeren.configure(text=t("gui.btn_log_clear"))
        self.task_film_lbl.configure(text=t("gui.task_film"))
        self.task_schritt_lbl.configure(text=t("gui.task_schritt"))
        self.task_status_lbl.configure(text=t("gui.task_status"))
        self.progress_lbl.configure(text=t("gui.progress_label"))

        for wert, btn in self._modus_btns.items():
            btn.configure(text=t(f"gui.mode_{wert}"))
        for wert, btn in self._old_mkv_ziel_btns.items():
            btn.configure(text=t(f"gui.backup_{wert}"))

        autoscroll_label = t("gui.autoscroll")
        prefix = t("gui.on_prefix") if self.var_autoscroll.get() else t("gui.off_prefix")
        self.btn_autoscroll.configure(text=f"{prefix}{autoscroll_label}")

        self._toggle_styles_update()
        self._modus_update()
        self._ffbin_status_update()
        self._dovi_status_update()

    def _auto_ffbin(self) -> str:
        """
        ffmpeg-Ordner automatisch ermitteln.
        Sucht ffmpeg im PATH und gibt den übergeordneten Ordner zurück.
        Beispiel: /usr/bin/ffmpeg  →  /usr/bin
                  C:/ffmpeg/bin/ffmpeg.exe  →  C:/ffmpeg/bin
        """
        pfad = shutil.which("ffmpeg")
        if pfad:
            return str(Path(pfad).parent)
        return ""

    def _ffmpeg_pfad(self) -> str:
        """Vollständigen ffmpeg-Pfad aus Ordner ableiten."""
        ordner = Path(self.var_ffbin.get())
        name   = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        return str(ordner / name)

    def _ffprobe_pfad(self) -> str:
        """Vollständigen ffprobe-Pfad aus Ordner ableiten."""
        ordner = Path(self.var_ffbin.get())
        name   = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        return str(ordner / name)

    # ─── Stile ───────────────────────────────────────────────────────────────
    def _stil(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        bg, panel, panel2, border = self.BG, self.PANEL, self.PANEL2, self.BORDER
        text, muted = self.TEXT, self.MUTED
        acc, acc2   = self.ACCENT, self.ACCENT2

        s.configure("TFrame",        background=bg)
        s.configure("Panel.TFrame",  background=panel)
        s.configure("Panel2.TFrame", background=panel2)

        s.configure("TLabel",        background=bg,     foreground=text,  font=("Consolas",10))
        s.configure("Muted.TLabel",  background=panel,  foreground=muted, font=("Consolas",9))
        s.configure("MutedBG.TLabel",background=bg,     foreground=muted, font=("Consolas",9))
        s.configure("Task.TLabel",   background=panel2, foreground=text,  font=("Consolas",10))
        s.configure("TaskH.TLabel",  background=panel2, foreground=muted, font=("Consolas",8))
        s.configure("TaskV.TLabel",  background=panel2, foreground=acc,   font=("Consolas",10,"bold"))
        s.configure("TaskOK.TLabel", background=panel2, foreground=self.GREEN, font=("Consolas",9))

        s.configure("TEntry", fieldbackground=panel, foreground=text,
                    bordercolor=border, relief="flat",
                    insertcolor=text, font=("Consolas",9))

        s.configure("TCheckbutton", background=bg, foreground=text, font=("Consolas",10))
        s.map("TCheckbutton",
              background=[("active", bg)],
              foreground=[("active", acc)])

        s.configure("TRadiobutton", background=bg, foreground=text, font=("Consolas",10))
        s.map("TRadiobutton",
              background=[("active", bg)],
              foreground=[("active", acc)])

        s.configure("Sim.TCheckbutton", background=bg, foreground=acc2,
                    font=("Consolas",10,"bold"))
        s.map("Sim.TCheckbutton", background=[("active", bg)])

        # Toggle-Buttons: inaktiv = weiß auf dunkel, aktiv = grün auf dunkel
        s.configure("Toggle.TButton",
            background=panel, foreground=text,
            font=("Consolas", 10), borderwidth=1, relief="flat",
            padding=(11, 6))
        s.map("Toggle.TButton",
              background=[("active", panel2)],
              foreground=[("active", text)])

        s.configure("ToggleOn.TButton",
            background=panel2, foreground=self.GREEN,
            font=("Consolas", 10, "bold"), borderwidth=1, relief="flat",
            padding=(11, 6))
        s.map("ToggleOn.TButton",
              background=[("active", panel2)],
              foreground=[("active", self.GREEN)])

        for name, bg_col, fg_col in [
            ("Run",    acc,    "#0d1117"),
            ("SimRun", acc2,   "#0d1117"),
        ]:
            s.configure(f"{name}.TButton",
                background=bg_col, foreground=fg_col,
                font=("Consolas",11,"bold"),
                borderwidth=0, relief="flat", padding=(18,7))
        s.map("Run.TButton",    background=[("active","#1f6feb"),("disabled",border)],
                                foreground=[("disabled",muted)])
        s.map("SimRun.TButton", background=[("active","#c0392b"),("disabled",border)])

        s.configure("Browse.TButton", background=border, foreground=text,
                    font=("Consolas",9), borderwidth=0, relief="flat", padding=(7,3))
        s.map("Browse.TButton", background=[("active","#3a3d50")])

        s.configure("Log.TButton", background=panel2, foreground=muted,
                    font=("Consolas",10), borderwidth=0, relief="flat", padding=(11,6))
        s.map("Log.TButton", background=[("active","#2d333b")])

        s.configure("Close.TButton", background=panel2, foreground=self.RED,
                    font=("Consolas",13,"bold"), borderwidth=0, relief="flat", padding=(8,2))
        s.map("Close.TButton", background=[("active","#3a1010")],
                               foreground=[("active",self.RED)])

        s.configure("Info.TButton", background=panel2, foreground=muted,
                    font=("Consolas",11), borderwidth=0, relief="flat", padding=(8,2))
        s.map("Info.TButton", background=[("active","#2d333b")],
                              foreground=[("active",self.ACCENT)])

        s.configure("Main.Horizontal.TProgressbar",
                    troughcolor=panel, background=acc,
                    bordercolor=panel, thickness=8)
        s.configure("Sub.Horizontal.TProgressbar",
                    troughcolor=panel, background=self.GREEN,
                    bordercolor=panel, thickness=5)

    # ─── GUI aufbauen ─────────────────────────────────────────────────────────
    def _gui(self):
        # Titelzeile
        titelzeile = ttk.Frame(self)
        titelzeile.pack(fill="x", padx=20, pady=(16,4))
        # ✕-Schließen-Button oben rechts (vor den linken Labels packen, damit er Platz bekommt)
        ttk.Button(titelzeile, text="✕", style="Close.TButton",
                   command=self._schliessen).pack(side="right", padx=(4,0))
        ttk.Button(titelzeile, text="ℹ", style="Info.TButton",
                   command=self._info_dialog).pack(side="right", padx=(4,0))
        self.sprache_dropdown = self._baue_sprach_dropdown(titelzeile)
        self.sprache_dropdown.pack(side="right", padx=(10,4))
        tk.Label(titelzeile, text="DV", fg=self.ACCENT2, bg=self.BG,
                 font=("Consolas",16,"bold")).pack(side="left")
        tk.Label(titelzeile, text=" Remux Tool", fg=self.TEXT, bg=self.BG,
                 font=("Consolas",16,"bold")).pack(side="left")
        tk.Label(titelzeile, text=f"  v{VERSION}  •  Jellyfin / LG TV",
                 fg=self.MUTED, bg=self.BG,
                 font=("Consolas",9)).pack(side="left", pady=4)
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=(2,10))

        # ── Einstellungs-Panel ────────────────────────────────────────────
        panel = ttk.Frame(self, style="Panel.TFrame")
        panel.pack(fill="x", padx=20, pady=(0,8))
        panel.columnconfigure(1, weight=1)

        # ── Zeile 0: ffmpeg-Ordner ────────────────────────────────────────
        self.ffbin_lbl = ttk.Label(panel, text=t("gui.ffmpeg_folder"), style="Muted.TLabel")
        self.ffbin_lbl.grid(row=0, column=0, padx=(12,8), pady=(9,0), sticky="w")
        ttk.Entry(panel, textvariable=self.var_ffbin).grid(
            row=0, column=1, padx=(0,6), pady=(9,0), sticky="ew", ipady=4)

        def wähle_ffbin():
            p = filedialog.askdirectory(
                title=t("gui.ffmpeg_dialog_title"),
                initialdir=self.var_ffbin.get() or "/")
            if p:
                self.var_ffbin.set(p)
                self._ffbin_status_update()
        ttk.Button(panel, text="📂", style="Browse.TButton",
                   command=wähle_ffbin, width=4).grid(
            row=0, column=2, padx=(0,12), pady=(9,0))

        # Status-Frame row=1: ffmpeg/ffprobe + dovi_tool (zwei Zeilen)
        _status_frame = tk.Frame(panel, bg=self.PANEL)
        _status_frame.grid(row=1, column=1, padx=(0,6), pady=(2,4), sticky="w")

        self.ffbin_status = tk.Label(
            _status_frame, text="", bg=self.PANEL, font=("Consolas", 8))
        self.ffbin_status.pack(anchor="w")

        self.dovi_status = tk.Label(
            _status_frame, text="", bg=self.PANEL, font=("Consolas", 8))
        self.dovi_status.pack(anchor="w")

        # ── Zeile 2: Quell-Ordner ─────────────────────────────────────────
        self.root_entry_lbl = ttk.Label(panel, text=t("gui.root_folder"), style="Muted.TLabel")
        self.root_entry_lbl.grid(row=2, column=0, padx=(12,8), pady=(9,0), sticky="w")
        # self.root_lbl bleibt der Modus-abhängige Hinweistext (siehe _modus_update);
        # self.root_entry_lbl ist das feste Feld-Label links davon.
        self.root_lbl = self.root_entry_lbl
        ttk.Entry(panel, textvariable=self.var_root).grid(
            row=2, column=1, padx=(0,6), pady=(9,0), sticky="ew", ipady=4)
        def wähle_root():
            p = filedialog.askdirectory(
                title=t("gui.root_dialog_title"),
                initialdir=self.var_root.get() or "/")
            if p:
                self.var_root.set(p)
        ttk.Button(panel, text="📂", style="Browse.TButton",
                   command=wähle_root, width=4).grid(
            row=2, column=2, padx=(0,12), pady=(9,0))

        # Hinweis-Label unter dem Quell-Ordner
        self.root_hint = tk.Label(
            panel, text="", bg=self.PANEL, fg=self.MUTED, font=("Consolas", 8))
        self.root_hint.grid(row=3, column=1, padx=(0,6), pady=(2,0), sticky="w")

        # ── Zeile 4a: Modus-Auswahl (Toggle-Buttons) ─────────────────────
        modus_frame = ttk.Frame(panel, style="Panel.TFrame")
        modus_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(10,2))
        self.modus_lbl = tk.Label(modus_frame, text=t("gui.mode_label"), bg=self.PANEL, fg=self.MUTED,
                 font=("Consolas", 9))
        self.modus_lbl.pack(side="left", padx=(0,10))

        self._modus_btns = {}
        for wert in ("filme", "serien", "ordner"):
            btn = ttk.Button(modus_frame, text=t(f"gui.mode_{wert}"),
                             command=lambda v=wert: self._set_modus(v))
            btn.pack(side="left", padx=(0,4))
            self._modus_btns[wert] = btn

        # ── Zeile 4b: Optionen (Toggle-Buttons) ──────────────────────────
        opt = ttk.Frame(panel, style="Panel.TFrame")
        opt.grid(row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(4,10))

        self._opt_btns = {}
        opt_defs = [
            (self.var_behalten,     "gui.opt_backup"),
            (self.var_subs,         "gui.opt_subs_srt"),
            (self.var_embed_subs,   "gui.opt_subs_embed"),
            (self.var_nfo,          "gui.opt_nfo_update"),
            (self.var_lokale_kopie, "gui.opt_local_copy"),
        ]
        for var, label_key in opt_defs:
            btn = ttk.Button(opt, text=t(label_key),
                             command=lambda v=var: self._toggle_opt(v))
            btn.pack(side="left", padx=(0,6))
            self._opt_btns[id(var)] = (btn, var, label_key)
            if var is self.var_lokale_kopie:
                _Tooltip(btn, lambda: t("gui.opt_local_copy_tip"),
                         bg=self.PANEL2, fg=self.TEXT, border=self.BORDER)

        # ── Zeile 6: Sicherungsordner ─────────────────────────────────────
        old_ziel_frame = ttk.Frame(panel, style="Panel.TFrame")
        old_ziel_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=12, pady=(2,2))
        self.backup_lbl = tk.Label(old_ziel_frame, text=t("gui.backup_folder_label"), bg=self.PANEL, fg=self.MUTED,
                 font=("Consolas", 9))
        self.backup_lbl.pack(side="left", padx=(0,10))
        self._old_mkv_ziel_btns = {}
        for wert in ("lokal", "global"):
            btn = ttk.Button(old_ziel_frame, text=t(f"gui.backup_{wert}"),
                             command=lambda v=wert: self._set_old_mkv_modus(v))
            btn.pack(side="left", padx=(0,4))
            self._old_mkv_ziel_btns[wert] = btn

        # ── Zeile 7: Pfad für globalen Ordner ────────────────────────────
        old_pfad_frame = ttk.Frame(panel, style="Panel.TFrame")
        old_pfad_frame.grid(row=7, column=0, columnspan=3, sticky="ew", padx=12, pady=(0,8))
        old_pfad_frame.columnconfigure(1, weight=1)
        self.backup_entry_lbl = ttk.Label(old_pfad_frame, text=t("gui.backup_folder_entry_label"), style="Muted.TLabel")
        self.backup_entry_lbl.grid(row=0, column=0, padx=(0,8), pady=(4,0), sticky="w")
        self.old_mkv_pfad_entry = ttk.Entry(old_pfad_frame, textvariable=self.var_old_mkv_pfad)
        self.old_mkv_pfad_entry.grid(row=0, column=1, padx=(0,6), pady=(4,0), sticky="ew", ipady=4)
        def wähle_old_mkv_pfad():
            p = filedialog.askdirectory(
                title=t("gui.choose_backup_folder"),
                initialdir=self.var_old_mkv_pfad.get() or "/")
            if p:
                self.var_old_mkv_pfad.set(p)
        self.old_mkv_pfad_btn = ttk.Button(
            old_pfad_frame, text="📂", style="Browse.TButton",
            command=wähle_old_mkv_pfad, width=4)
        self.old_mkv_pfad_btn.grid(row=0, column=2, padx=(0,0), pady=(4,0))

        # ── Zeile 8: Pfad für lokale Arbeitskopie ────────────────────────
        lokale_kopie_frame = ttk.Frame(panel, style="Panel.TFrame")
        lokale_kopie_frame.grid(row=8, column=0, columnspan=3, sticky="ew", padx=12, pady=(0,8))
        lokale_kopie_frame.columnconfigure(1, weight=1)
        self.workdir_entry_lbl = ttk.Label(lokale_kopie_frame, text=t("gui.workdir_label"), style="Muted.TLabel")
        self.workdir_entry_lbl.grid(row=0, column=0, padx=(0,8), pady=(4,0), sticky="w")
        self.lokale_kopie_pfad_entry = ttk.Entry(lokale_kopie_frame, textvariable=self.var_lokale_kopie_pfad)
        self.lokale_kopie_pfad_entry.grid(row=0, column=1, padx=(0,6), pady=(4,0), sticky="ew", ipady=4)
        def wähle_lokale_kopie_pfad():
            p = filedialog.askdirectory(
                title=t("gui.choose_workdir"),
                initialdir=self.var_lokale_kopie_pfad.get() or "/")
            if p:
                self.var_lokale_kopie_pfad.set(p)
        self.lokale_kopie_pfad_btn = ttk.Button(
            lokale_kopie_frame, text="📂", style="Browse.TButton",
            command=wähle_lokale_kopie_pfad, width=4)
        self.lokale_kopie_pfad_btn.grid(row=0, column=2, padx=(0,0), pady=(4,0))

        # ── Button-Leiste ─────────────────────────────────────────────────
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=20, pady=(0,8))
        self.btn_start = ttk.Button(
            bf, text=t("gui.btn_start"), style="Run.TButton",
            command=lambda: self._starten(simulation=False))
        self.btn_start.pack(side="left")
        self.btn_sim = ttk.Button(
            bf, text=t("gui.btn_sim"), style="SimRun.TButton",
            command=lambda: self._starten(simulation=True))
        self.btn_sim.pack(side="left", padx=(8,0))
        self.btn_stopp = ttk.Button(
            bf, text=t("gui.btn_stopp"), style="Log.TButton",
            command=self._abbrechen, state="disabled")
        self.btn_stopp.pack(side="left", padx=(8,0))
        self.btn_log = ttk.Button(
            bf, text=t("gui.btn_log_open"), style="Log.TButton",
            command=self._log_oeffnen, state="disabled")
        self.btn_log.pack(side="left", padx=(8,0))
        self.btn_log_leeren = ttk.Button(bf, text=t("gui.btn_log_clear"), style="Log.TButton",
                   command=self._log_leeren)
        self.btn_log_leeren.pack(side="left", padx=(8,0))
        self.btn_autoscroll = ttk.Button(
            bf, text=f"{t('gui.on_prefix')}{t('gui.autoscroll')}", style="ToggleOn.TButton",
            command=self._toggle_autoscroll)
        self.btn_autoscroll.pack(side="left", padx=(8,0))

        # ── Status-Zeile (eigene Reihe, damit sie nicht von Buttons überlagert wird)
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=20, pady=(0,4))
        self.status_lbl = tk.Label(
            status_frame, text="", fg=self.MUTED, bg=self.BG,
            font=("Consolas",10), anchor="w")
        self.status_lbl.pack(side="left")

        # ── Gesamt-Fortschrittsbalken ─────────────────────────────────────
        self.prog_main = ttk.Progressbar(
            self, style="Main.Horizontal.TProgressbar",
            mode="determinate", maximum=100)
        self.prog_main.pack(fill="x", padx=20, pady=(0,6))

        # ── Task-Status-Fenster ───────────────────────────────────────────
        task_border = tk.Frame(self, bg=self.BORDER)
        task_border.pack(fill="x", padx=20, pady=(0,8))
        task_inner = tk.Frame(task_border, bg=self.PANEL2)
        task_inner.pack(fill="x", padx=1, pady=1)

        # Linke Info-Spalte
        left = tk.Frame(task_inner, bg=self.PANEL2)
        left.pack(side="left", fill="both", expand=True, padx=(12,8), pady=10)

        def task_zeile(parent, label_text):
            r = tk.Frame(parent, bg=self.PANEL2)
            r.pack(fill="x", pady=1)
            lbl = tk.Label(r, text=label_text, fg=self.MUTED, bg=self.PANEL2,
                     font=("Consolas",8), width=8, anchor="w")
            lbl.pack(side="left")
            val = tk.Label(r, text="—", fg=self.TEXT, bg=self.PANEL2,
                           font=("Consolas",10), anchor="w")
            val.pack(side="left", fill="x", expand=True)
            return lbl, val

        self.task_film_lbl,    self.task_film    = task_zeile(left, t("gui.task_film"))
        self.task_schritt_lbl, self.task_schritt = task_zeile(left, t("gui.task_schritt"))
        self.task_status_lbl,  self.task_status  = task_zeile(left, t("gui.task_status"))
        self.task_status.configure(fg=self.GREEN, text=t("gui.status_ready"))

        # Trennlinie
        tk.Frame(task_inner, bg=self.BORDER, width=1).pack(
            side="left", fill="y", pady=6)

        # Rechte Fortschritts-Spalte
        right = tk.Frame(task_inner, bg=self.PANEL2)
        right.pack(side="left", padx=(12,14), pady=10)

        self.progress_lbl = tk.Label(right, text=t("gui.progress_label"),
                 fg=self.MUTED, bg=self.PANEL2,
                 font=("Consolas",8))
        self.progress_lbl.pack(anchor="w")

        self.prog_sub = ttk.Progressbar(
            right, style="Sub.Horizontal.TProgressbar",
            mode="determinate", maximum=100, length=220)
        self.prog_sub.pack(fill="x", pady=(4,2))

        self.pct_lbl = tk.Label(
            right, text="0 %", fg=self.MUTED, bg=self.PANEL2,
            font=("Consolas",9))
        self.pct_lbl.pack(anchor="e")

        # Trennlinie Sim-Indikator
        self.sim_banner = tk.Label(
            right, text="", fg=self.ACCENT2, bg=self.PANEL2,
            font=("Consolas",8,"bold"))
        self.sim_banner.pack(anchor="w", pady=(4,0))

        # ── Log-Textfenster ───────────────────────────────────────────────
        log_outer = tk.Frame(self, bg=self.BORDER)
        log_outer.pack(fill="both", expand=True, padx=20, pady=(0,16))

        self.log_widget = scrolledtext.ScrolledText(
            log_outer,
            bg="#090d13", fg=self.TEXT,
            font=("Consolas",9), wrap="word",
            borderwidth=0, relief="flat",
            state="disabled",
            selectbackground=self.ACCENT
        )
        self.log_widget.pack(fill="both", expand=True, padx=1, pady=1)

        self.log_widget.tag_configure("HEAD",   foreground=self.ACCENT)
        self.log_widget.tag_configure("OK",     foreground=self.GREEN)
        self.log_widget.tag_configure("ERR",    foreground=self.RED)
        self.log_widget.tag_configure("SKIP",   foreground=self.MUTED)
        self.log_widget.tag_configure("INFO",   foreground=self.TEXT)
        self.log_widget.tag_configure("WARN",   foreground=self.YELLOW)
        self.log_widget.tag_configure("SIM",    foreground=self.ACCENT2,
                                                font=("Consolas",9,"bold"))
        self.log_widget.tag_configure("PROG",   foreground="#4a505a")
        self.log_widget.tag_configure("FOLDER", foreground="#e6b450",
                                                font=("Consolas",9,"bold"))

    # ─── Abbrechen ────────────────────────────────────────────────────────────
    def _abbrechen(self):
        self.stopp_event.set()
        self.btn_stopp.configure(state="disabled")
        self.status_lbl.configure(text=t("gui.status_cancelling"), fg=self.YELLOW)

    # ─── Config als dict (für config_speichern) ────────────────────────────────
    def _config_dict(self) -> dict:
        return {
            "ffbin":          self.var_ffbin.get(),
            "root":           self.var_root.get(),
            "behalten":       self.var_behalten.get(),
            "subs":           self.var_subs.get(),
            "nfo":            self.var_nfo.get(),
            "modus":          self.var_modus.get(),
            "embed_subs":     self.var_embed_subs.get(),
            "old_mkv_modus":  self.var_old_mkv_modus.get(),
            "old_mkv_pfad":   self.var_old_mkv_pfad.get(),
            "lokale_kopie":      self.var_lokale_kopie.get(),
            "lokale_kopie_pfad": self.var_lokale_kopie_pfad.get(),
            "sprache":        self.var_sprache.get(),
        }

    # ─── Schließen (X-Button + Schließen-Button) ──────────────────────────────
    def _schliessen(self):
        if self.läuft:
            antwort = messagebox.askyesno(
                t("gui.close_confirm_title"),
                t("gui.close_confirm_message"),
                icon="warning",
                default="no"
            )
            if not antwort:
                return
            self.stopp_event.set()
        config_speichern(self._config_dict())
        self.destroy()

    # ─── Autoscroll-Toggle ────────────────────────────────────────────────────
    def _toggle_autoscroll(self):
        val = not self.var_autoscroll.get()
        self.var_autoscroll.set(val)
        if val:
            self.btn_autoscroll.configure(style="ToggleOn.TButton",
                                           text=f"{t('gui.on_prefix')}{t('gui.autoscroll')}")
            self.log_widget.see("end")
        else:
            self.btn_autoscroll.configure(style="Toggle.TButton",
                                           text=f"{t('gui.off_prefix')}{t('gui.autoscroll')}")

    # ─── Info-Dialog ──────────────────────────────────────────────────────────
    def _info_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title(t("gui.about_title"))
        dlg.configure(bg=self.BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="DV Remux Tool", fg=self.ACCENT2, bg=self.BG,
                 font=("Consolas",14,"bold")).pack(pady=(20,2))
        tk.Label(dlg, text=t("gui.about_version", version=VERSION), fg=self.MUTED, bg=self.BG,
                 font=("Consolas",10)).pack()
        tk.Label(dlg, text=t("gui.about_subtitle"),
                 fg=self.TEXT, bg=self.BG, font=("Consolas",9)).pack(pady=(4,16))

        tk.Frame(dlg, bg=self.BORDER, height=1).pack(fill="x", padx=20)

        # Anklickbarer GitHub-Link
        REPO = "https://github.com/Hero9774/DolbyVision-Remux"
        lnk = tk.Label(dlg, text=REPO, fg=self.ACCENT, bg=self.BG,
                       font=("Consolas",9,"underline"), cursor="hand2")
        lnk.pack(pady=(14,2))
        lnk.bind("<Button-1>", lambda _: webbrowser.open(REPO))

        tk.Label(dlg, text="hero.ommen@posteo.de", fg=self.MUTED, bg=self.BG,
                 font=("Consolas",9)).pack(pady=(0,12))

        # Drittanbieter
        tk.Frame(dlg, bg=self.BORDER, height=1).pack(fill="x", padx=20)
        tk.Label(dlg, text=t("gui.about_third_party"),
                 fg=self.MUTED, bg=self.BG,
                 font=("Consolas",8,"bold")).pack(pady=(10,2))

        DOVI_URL = "https://github.com/quietvoid/dovi_tool"
        dovi_lnk = tk.Label(dlg,
                             text="dovi_tool  (quietvoid)  –  GPL v3.0 or later",
                             fg=self.ACCENT, bg=self.BG,
                             font=("Consolas",8,"underline"), cursor="hand2")
        dovi_lnk.pack()
        dovi_lnk.bind("<Button-1>", lambda _: webbrowser.open(DOVI_URL))

        tk.Label(dlg,
                 text=t("gui.about_dovi_usage"),
                 fg=self.MUTED, bg=self.BG, font=("Consolas",8)).pack(pady=(0,14))

        ttk.Button(dlg, text=t("gui.close"), style="Log.TButton",
                   command=dlg.destroy).pack(pady=(0,16))

    # ─── Modus-Toggle ─────────────────────────────────────────────────────────
    def _set_modus(self, wert):
        self.var_modus.set(wert)

    def _toggle_opt(self, var):
        var.set(not var.get())
        self._toggle_styles_update()

    def _set_old_mkv_modus(self, wert):
        self.var_old_mkv_modus.set(wert)
        self._toggle_styles_update()

    def _toggle_styles_update(self):
        """Alle Toggle-Buttons aktualisieren: aktiv = grün + [ON], inaktiv = weiß + [OFF]."""
        modus = self.var_modus.get()
        for wert, btn in self._modus_btns.items():
            if wert == modus:
                btn.configure(style="ToggleOn.TButton")
            else:
                btn.configure(style="Toggle.TButton")

        for key, (btn, var, label_key) in self._opt_btns.items():
            label = t(label_key)
            if var.get():
                btn.configure(style="ToggleOn.TButton", text=f"{t('gui.on_prefix')}{label}")
            else:
                btn.configure(style="Toggle.TButton", text=f"{t('gui.off_prefix')}{label}")

        # Sicherungsordner: nur aktiv wenn "Sicherungskopie erstellen" an
        behalten      = self.var_behalten.get()
        old_mkv_modus = self.var_old_mkv_modus.get()
        if hasattr(self, "_old_mkv_ziel_btns"):
            for wert, btn in self._old_mkv_ziel_btns.items():
                if not behalten:
                    btn.configure(style="Toggle.TButton", state="disabled")
                elif wert == old_mkv_modus:
                    btn.configure(style="ToggleOn.TButton", state="normal")
                else:
                    btn.configure(style="Toggle.TButton", state="normal")
        if hasattr(self, "old_mkv_pfad_entry"):
            ist_global = behalten and old_mkv_modus == "global"
            state = "normal" if ist_global else "disabled"
            self.old_mkv_pfad_entry.configure(state=state)
            self.old_mkv_pfad_btn.configure(state=state)

        # Arbeitsordner-Pfad: nur aktiv wenn "Lokale Arbeitskopie" an
        if hasattr(self, "lokale_kopie_pfad_entry"):
            state = "normal" if self.var_lokale_kopie.get() else "disabled"
            self.lokale_kopie_pfad_entry.configure(state=state)
            self.lokale_kopie_pfad_btn.configure(state=state)

    # ─── Modus-Label aktualisieren ────────────────────────────────────────────
    def _modus_update(self):
        modus = self.var_modus.get()
        self._toggle_styles_update()
        self.root_lbl.configure(text=t(f"gui.root_label.{modus}"))
        self.root_hint.configure(text=t(f"gui.root_hint.{modus}"))

    def _ffbin_status_update(self, *_):
        """
        Prüft ob ffmpeg und ffprobe im gewählten Ordner liegen
        und aktualisiert das Status-Label darunter in Echtzeit.
        """
        ordner = Path(self.var_ffbin.get()) if self.var_ffbin.get() else None
        if ordner is None or not ordner.is_dir():
            self.ffbin_status.configure(
                text="  " + t("gui.folder_not_found"), fg=self.RED)
            return

        ext       = ".exe" if sys.platform == "win32" else ""
        ffmpeg_ok  = (ordner / f"ffmpeg{ext}").is_file()
        ffprobe_ok = (ordner / f"ffprobe{ext}").is_file()

        teile = []
        teile.append(t("gui.ffmpeg_ok") if ffmpeg_ok else t("gui.ffmpeg_missing"))
        teile.append(t("gui.ffprobe_ok") if ffprobe_ok else t("gui.ffprobe_missing"))

        farbe = self.GREEN if (ffmpeg_ok and ffprobe_ok) else self.RED
        self.ffbin_status.configure(
            text="  " + "   ".join(teile), fg=farbe)

    def _dovi_status_update(self):
        """Prüft ob dovi_tool.exe in tools/ vorhanden ist und zeigt Status."""
        if DOVI_TOOL.exists():
            self.dovi_status.configure(
                text="  " + t("gui.dovi_tool_ok"), fg=self.GREEN, cursor="")
            self.dovi_status.unbind("<Button-1>")
        else:
            self.dovi_status.configure(
                text="  " + t("gui.dovi_tool_missing"),
                fg=self.YELLOW, cursor="hand2")
            self.dovi_status.bind(
                "<Button-1>",
                lambda e: webbrowser.open(
                    "https://github.com/quietvoid/dovi_tool/releases"))

    # ─── Log ──────────────────────────────────────────────────────────────────
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
        if self.letzter_log_pfad and self.letzter_log_pfad.exists():
            if sys.platform == "win32":
                os.startfile(self.letzter_log_pfad)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(self.letzter_log_pfad)])
            else:
                subprocess.run(["xdg-open", str(self.letzter_log_pfad)])
        else:
            messagebox.showinfo(t("gui.log_dialog_title"), t("gui.log_not_yet_available"))

    # ─── Start ────────────────────────────────────────────────────────────────
    def _starten(self, simulation: bool):
        if self.läuft:
            return
        ist_sim = simulation
        fehler  = []

        # ffmpeg-Ordner und abgeleitete Pfade prüfen
        if not ist_sim:
            ffmpeg_pfad  = self._ffmpeg_pfad()
            ffprobe_pfad = self._ffprobe_pfad()
            if not self.var_ffbin.get() or not Path(self.var_ffbin.get()).is_dir():
                fehler.append(t("gui.err_ffmpeg_folder_invalid"))
            else:
                if not Path(ffmpeg_pfad).is_file():
                    fehler.append(
                        t("gui.err_ffmpeg_not_found", name=Path(ffmpeg_pfad).name))
                if not Path(ffprobe_pfad).is_file():
                    fehler.append(
                        t("gui.err_ffprobe_not_found", name=Path(ffprobe_pfad).name))
        else:
            ffmpeg_pfad  = self._ffmpeg_pfad()
            ffprobe_pfad = self._ffprobe_pfad()

        if not self.var_root.get() or not Path(self.var_root.get()).is_dir():
            fehler.append(t("gui.err_root_invalid"))

        lokale_kopie_pfad_obj = None
        if self.var_lokale_kopie.get():
            p = self.var_lokale_kopie_pfad.get().strip()
            if not p:
                fehler.append(t("gui.err_workdir_not_set"))
            else:
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    lokale_kopie_pfad_obj = Path(p)
                except OSError as e:
                    fehler.append(t("gui.err_workdir_unreachable", fehler=e))

        if fehler:
            for f in fehler:
                self._log("ERR", f"❌  {f}")
            return

        config_speichern(self._config_dict())

        self.stopp_event.clear()
        self.läuft = True
        self.btn_start.configure(state="disabled")
        self.btn_sim.configure(state="disabled")
        self.btn_stopp.configure(state="normal")
        self.btn_log.configure(state="disabled")
        self.status_lbl.configure(text=t("gui.status_running"), fg=self.MUTED)
        self.prog_main["value"] = 0
        self.prog_sub["value"]  = 0
        self.pct_lbl.configure(text="0 %")
        self.task_film.configure(text="—")
        self.task_schritt.configure(text="—")
        self.task_status.configure(text=t("gui.task_status_running"), fg=self.YELLOW)
        self._log_leeren()

        if ist_sim:
            self.sim_banner.configure(text=t("gui.sim_banner"))
            self.configure(bg="#110d0a")
            self._log("SIM", t("gui.sim_log_notice"))
        else:
            self.sim_banner.configure(text="")
            self.configure(bg=self.BG)

        modus  = self.var_modus.get()
        worker = (verarbeite_serien       if modus == "serien"
                  else verarbeite_einzelordner if modus == "ordner"
                  else verarbeite_sammlung)

        old_mkv_global = None
        if self.var_behalten.get() and self.var_old_mkv_modus.get() == "global":
            p = self.var_old_mkv_pfad.get().strip()
            if p:
                old_mkv_global = Path(p)

        threading.Thread(
            target=worker,
            args=(
                ffmpeg_pfad,
                ffprobe_pfad,
                self.var_root.get(),
                ist_sim,
                self.var_behalten.get(),
                self.var_subs.get(),
                self.var_nfo.get(),
                self.var_embed_subs.get(),
                self.log_queue,
                self.task_queue,
                self.fort_queue,
                self.done_queue,
                self.stopp_event,
                old_mkv_global,
                self.var_lokale_kopie.get(),
                lokale_kopie_pfad_obj,
            ),
            daemon=True
        ).start()

    # ─── Poll-Loop (alle 80 ms) ───────────────────────────────────────────────
    def _poll(self):
        # Gesamt-Fortschritt
        try:
            while True:
                self.prog_main["value"] = self.fort_queue.get_nowait()
        except queue.Empty:
            pass

        # Task-Updates
        try:
            while True:
                info = self.task_queue.get_nowait()
                if "film" in info:
                    self.task_film.configure(text=info["film"])
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

        # Log-Nachrichten
        try:
            while True:
                typ, text = self.log_queue.get_nowait()
                self._log(typ, text)
        except queue.Empty:
            pass

        # Fertig-Signal
        try:
            stats, log_pfad = self.done_queue.get_nowait()
            self.läuft = False
            self.btn_start.configure(state="normal")
            self.btn_sim.configure(state="normal")
            self.btn_stopp.configure(state="disabled")
            self.sim_banner.configure(text="")
            self.configure(bg=self.BG)
            self.letzter_log_pfad = log_pfad
            self.btn_log.configure(state="normal" if log_pfad else "disabled")
            ok  = stats["remuxed"]
            err = stats["fehler"]
            farbe = self.GREEN if err == 0 else self.YELLOW
            self.status_lbl.configure(
                text=t("gui.status_done", ok=ok, err=err), fg=farbe)
            log_name = log_pfad.name if log_pfad else "—"
            self.task_status.configure(
                text=t("gui.task_status_done", name=log_name), fg=self.GREEN)
        except queue.Empty:
            pass

        self.after(80, self._poll)
