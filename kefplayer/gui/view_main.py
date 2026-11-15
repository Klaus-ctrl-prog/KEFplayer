"""tkinter/ttk-Hauptfenster. Reine View: alle Befehle gehen an den Controller, Meldungen kommen per Queue (after-Polling).

Tk-Objekte werden nur im Hauptthread benutzt. Dialoge sind austauschbar (``Dialogs``), damit Tests ohne Benutzer laufen.
"""

from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from . import controller as ctl
from . import texts, viewmodel as vm

POLL_MS = 100
LOG_MAX_LINES = 2000
AUDIO_PATTERN = "*.flac *.wav *.ogg *.aif *.aiff *.mp3"


class Dialogs:
    """Echte Dialoge. Tests ersetzen diese Klasse durch eine Attrappe."""

    def open_files(self, parent):
        return filedialog.askopenfilenames(parent=parent, title="Audiodateien waehlen",
                                           filetypes=[("Audiodateien", AUDIO_PATTERN), ("Alle Dateien", "*.*")])

    def open_folder(self, parent):
        return filedialog.askdirectory(parent=parent, title="Ordner waehlen", mustexist=True)

    def open_profile(self, parent):
        return filedialog.askopenfilename(parent=parent, title="Profil laden",
                                          filetypes=[("Profil (JSON oder Equalizer-APO/REW-Text)", "*.json *.txt"),
                                                     ("Alle Dateien", "*.*")])

    def confirm_quit(self, parent) -> bool:
        return messagebox.askokcancel("KEFplayer", "Die Wiedergabe laeuft noch. Stoppen und beenden?", parent=parent)


class MainWindow:
    def __init__(self, root: tk.Tk, controller: ctl.Controller, dialogs: Dialogs | None = None):
        self.root, self.c = root, controller
        self.dialogs = dialogs or Dialogs()
        self.devices: list[dict] = []
        self._device_name = None
        self._now: dict | None = None
        self._started_ts: str | None = None
        self._pause_requested = False
        self._play_map: list[int] = []
        self._row_status: dict[int, str] = {}
        self._closing = False
        self.shutdown_ok: bool | None = None
        self._after_id = None
        root.title(f"KEFplayer GUI (Entwurf, Kern {__version__})")
        # Skalierung relativ zu 96 dpi (1.0 bei 100 %, 2.0 bei 200 %): Pixelwerte muessen mitwachsen
        self.ui_scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        self._wrap = int(720 * self.ui_scale)
        self._build()
        root.update_idletasks()
        w, h, minw, minh = vm.window_size(root.winfo_reqwidth(), root.winfo_reqheight(), self.ui_scale,
                                          root.winfo_screenwidth(), root.winfo_screenheight())
        root.geometry(f"{w}x{h}")
        root.minsize(minw, minh)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._log("Protokolldatei der Wiedergabe: " + os.path.abspath(self.c.settings.log_path or "(keine)"))
        self.refresh_profile()
        self.refresh_queue()
        self.refresh_buttons()
        self._after_id = root.after(POLL_MS, self._poll)

    # ---- Aufbau -----------------------------------------------------------------------------------
    def _build(self) -> None:
        r = self.root
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1, minsize=int(130 * self.ui_scale))  # nie auf null schrumpfen
        r.rowconfigure(3, weight=1, minsize=int(150 * self.ui_scale))
        pad = {"padx": 8, "pady": 4}

        # Geraet
        dev = ttk.LabelFrame(r, text="Geraet")
        dev.grid(row=0, column=0, sticky="ew", **pad)
        dev.columnconfigure(1, weight=1)
        self.btn_search = ttk.Button(dev, text="Suchen", command=self.on_search)
        self.btn_search.grid(row=0, column=0, padx=4, pady=4)
        self.combo = ttk.Combobox(dev, state="readonly", values=[])
        self.combo.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_connect = ttk.Button(dev, text="Verbinden", command=self.on_connect)
        self.btn_connect.grid(row=0, column=2, padx=4)
        ttk.Label(dev, text="oder Adresse:").grid(row=1, column=0, sticky="e", padx=4)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(dev, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 4))
        self.btn_url = ttk.Button(dev, text="Adresse verbinden", command=self.on_connect_url)
        self.btn_url.grid(row=1, column=2, padx=4, pady=(0, 4))
        self.device_var = tk.StringVar(value="Kein Geraet verbunden.")
        ttk.Label(dev, textvariable=self.device_var).grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 4))

        # Warteschlange
        q = ttk.LabelFrame(r, text="Warteschlange")
        q.grid(row=1, column=0, sticky="nsew", **pad)
        q.columnconfigure(0, weight=1)
        q.rowconfigure(0, weight=1)
        # Zeilenhoehe der Tabelle folgt der Schrift (Tk setzt sie fest in Pixeln, bei 200 % wuerden Zeilen abgeschnitten)
        ttk.Style(r).configure("Treeview", rowheight=tkfont.nametofont("TkDefaultFont").metrics("linespace")
                               + int(6 * self.ui_scale))
        cols = ("nr", "file", "fmt", "status")
        self.tree = ttk.Treeview(q, columns=cols, show="headings", selectmode="browse", height=7)
        for key, title, w, anchor in (("nr", "Nr.", 50, "e"), ("file", "Datei", 300, "w"),
                                       ("fmt", "Format", 170, "w"), ("status", "Status", 190, "w")):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=int(w * self.ui_scale), anchor=anchor, stretch=(key == "file"))
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        sb = ttk.Scrollbar(q, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=4)
        self.tree.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(q)
        bf.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4))
        self.queue_buttons = []
        for text, cmd in (("Dateien hinzufuegen", self.on_add_files), ("Ordner hinzufuegen", self.on_add_folder),
                          ("Entfernen", self.on_remove), ("Nach oben", lambda: self.on_move(-1)),
                          ("Nach unten", lambda: self.on_move(1)), ("Leeren", self.on_clear)):
            b = ttk.Button(bf, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.queue_buttons.append(b)

        # Profil
        p = ttk.LabelFrame(r, text="Verarbeitung (DSP-Profil)")
        p.grid(row=2, column=0, sticky="ew", **pad)
        p.columnconfigure(0, weight=1)
        self.profile_var = tk.StringVar()
        ttk.Label(p, textvariable=self.profile_var, wraplength=self._wrap, justify="left").grid(
            row=0, column=0, sticky="w", padx=4, pady=(4, 0))
        self.profile_detail_var = tk.StringVar()
        ttk.Label(p, textvariable=self.profile_detail_var, wraplength=self._wrap, justify="left").grid(
            row=1, column=0, sticky="w", padx=4)
        pbf = ttk.Frame(p)
        pbf.grid(row=0, column=1, rowspan=2, sticky="ne", padx=4, pady=4)
        self.btn_profile = ttk.Button(pbf, text="Profil laden ...", command=self.on_load_profile)
        self.btn_profile.pack(fill="x", pady=2)
        self.btn_neutral = ttk.Button(pbf, text="Neutral", command=self.on_neutral)
        self.btn_neutral.pack(fill="x", pady=2)
        self.warn_text = tk.Text(p, height=5, wrap="word", state="disabled", relief="flat",
                                 font="TkDefaultFont", background=ttk.Style().lookup("TFrame", "background") or "#ececec")
        self.warn_text.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
        ttk.Label(p, text=texts.SIGNALPATH_HINT, wraplength=self._wrap, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4))

        # Wiedergabe + Protokoll
        w = ttk.LabelFrame(r, text="Wiedergabe")
        w.grid(row=3, column=0, sticky="nsew", **pad)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(2, weight=1)
        tf = ttk.Frame(w)
        tf.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.btn_play = ttk.Button(tf, text="Abspielen", command=self.on_play)
        self.btn_pause = ttk.Button(tf, text="Pause / Weiter", command=self.on_pause)
        self.btn_skip = ttk.Button(tf, text="Naechster Titel", command=self.on_skip)
        self.btn_stop = ttk.Button(tf, text="Stopp", command=self.on_stop)
        for b in (self.btn_play, self.btn_pause, self.btn_skip, self.btn_stop):
            b.pack(side="left", padx=3)
        self.now_var = tk.StringVar(value=vm.now_text(None, None, pause_requested=False))
        ttk.Label(w, textvariable=self.now_var, wraplength=self._wrap, justify="left").grid(row=1, column=0, sticky="w", padx=4)
        lf = ttk.Frame(w)
        lf.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(lf, height=9, wrap="word", state="disabled", font="TkFixedFont")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set)

        # Statuszeile
        sf = ttk.Frame(r)
        sf.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 6))
        sf.columnconfigure(1, weight=1)
        self.state_var = tk.StringVar(value=vm.STATE_TEXT[ctl.STATE_IDLE])
        ttk.Label(sf, textvariable=self.state_var, width=18).grid(row=0, column=0, sticky="w")
        ttk.Label(sf, text=texts.VOLUME_HINT).grid(row=0, column=1, sticky="e")

    # ---- Protokoll -------------------------------------------------------------------------------------
    def _log(self, line: str) -> None:
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", f"{datetime.now():%H:%M:%S}  {line}\n")
        extra = int(t.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if extra > 0:
            t.delete("1.0", f"{extra + 1}.0")
        t.see("end")
        t.configure(state="disabled")

    def log_contents(self) -> str:
        return self.log_text.get("1.0", "end-1c")

    # ---- Aktualisieren -----------------------------------------------------------------------------------
    def refresh_queue(self) -> None:
        sel = self.selected_index()
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.c.items):
            nr, name, fmt, status = vm.item_row(i, item)
            status = self._row_status.get(i, status)
            self.tree.insert("", "end", iid=str(i), values=(nr, name, fmt, status))
        if sel is not None and sel < len(self.c.items):
            self.tree.selection_set(str(sel))

    def selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def refresh_profile(self, info: dict | None = None) -> None:
        info = info or self.c.profile_info()
        self.profile_var.set(info["summary"] + (f"  Datei: {info['path']}" if info.get("path") else ""))
        n_f = len(info["filters"]) + sum(len(c["filters"]) for c in info["channels"].values())
        self.profile_detail_var.set(
            f"Preamp: {info['preamp'] if info['preamp'] == 'auto' else format(info['preamp'], '+g') + ' dB'}"
            f" | Filter: {n_f} | Kanaele mit eigenen Werten: {', '.join(info['channels']) or 'keine'}")
        lines = ["Hinweis (Import): " + n for n in info["notes"]] + ["Warnung: " + w for w in info["warnings"]]
        w = self.warn_text
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", "\n".join(lines) if lines else "Keine Warnungen.")
        w.configure(state="disabled")

    def refresh_buttons(self) -> None:
        st = vm.button_states(self.c.state, has_device=self.c.renderer is not None, n_playable=len(self.c.playable()))
        on = lambda b, flag: b.state(["!disabled"] if flag else ["disabled"])  # noqa: E731
        on(self.btn_search, st["search"])
        on(self.btn_connect, st["connect"])
        on(self.btn_url, st["connect"])
        on(self.btn_profile, st["profile"])
        on(self.btn_neutral, st["profile"])
        for b in self.queue_buttons:
            on(b, st["queue_edit"])
        on(self.btn_play, st["play"])
        on(self.btn_pause, st["pause"])
        on(self.btn_skip, st["skip"])
        on(self.btn_stop, st["stop"])
        self.state_var.set(vm.STATE_TEXT.get(self.c.state, self.c.state))

    # ---- Meldungen -----------------------------------------------------------------------------------------
    def _poll(self) -> None:
        self._after_id = None
        if self._closing:
            return
        self.pump()
        self._after_id = self.root.after(POLL_MS, self._poll)

    def pump(self) -> None:
        for m in self.c.drain():
            self._handle(m)
        self.now_var.set(vm.now_text(self._now, self._started_ts, pause_requested=self._pause_requested))
        self.refresh_buttons()

    def _handle(self, m: ctl.Msg) -> None:
        line = vm.log_line(m)
        if line:
            self._log(line)
        k, d = m.kind, m.data
        if k == "queue_changed":
            self.refresh_queue()
        elif k == "devices":
            self.devices = [x for x in d["devices"] if x["usable"]]
            self.combo.configure(values=[x["name"] for x in d["devices"] if x["usable"]])
            if self.devices:
                self.combo.current(0)
        elif k == "device":
            self.device_var.set(f"Verbunden: {d.get('name') or d.get('host')} ({d.get('host')})")
        elif k == "profile":
            self.refresh_profile(d)
        elif k == "pause_requested":
            self._pause_requested = True
        elif k == "player_event":
            self._on_player_event(d["event"])

    def _on_player_event(self, r: dict) -> None:
        ev = r.get("event")
        if ev == "queue_start":
            self._now = {"index": None, "total": len(r.get("tracks", [])), "name": None}
            self._row_status = {}
            self._started_ts = None
        elif ev == "track_start":
            self._now.update(index=r["index"], name=os.path.basename(r["path"]))
            self._started_ts, self._pause_requested = r.get("ts"), False
            self._set_row(r["index"], "laeuft")
        elif ev == "track_end":
            self._set_row(r["index"], vm.end_text(r.get("end_reason")))

    def _set_row(self, play_index: int, text: str) -> None:
        if 0 <= play_index < len(self._play_map):
            i = self._play_map[play_index]
            self._row_status[i] = text
            if self.tree.exists(str(i)):
                self.tree.set(str(i), "status", text)

    # ---- Befehle ------------------------------------------------------------------------------------------------
    def on_search(self) -> None:
        self._log("Suche laeuft ...")
        self.c.search()

    def on_connect(self) -> None:
        idx = self.combo.current()
        if idx < 0 or idx >= len(self.devices):
            self._log("FEHLER: Kein Geraet in der Liste gewaehlt. Zuerst suchen.")
            return
        self.c.connect(self.devices[idx]["location"])

    def on_connect_url(self) -> None:
        url = self.url_var.get().strip()
        if not url.lower().startswith("http"):
            self._log("FEHLER: Adresse muss mit http:// beginnen (Geraetebeschreibung).")
            return
        self.c.connect(url)

    def on_add_files(self) -> None:
        paths = self.dialogs.open_files(self.root)
        if paths:
            self.c.add_paths(list(paths))

    def on_add_folder(self) -> None:
        path = self.dialogs.open_folder(self.root)
        if path:
            self.c.add_paths([path])

    def on_remove(self) -> None:
        i = self.selected_index()
        if i is not None:
            self.c.remove(i)

    def on_move(self, delta: int) -> None:
        i = self.selected_index()
        if i is None:
            return
        self.c.move(i, delta)
        j = min(max(i + delta, 0), len(self.c.items) - 1)
        self.refresh_queue()
        if self.tree.exists(str(j)):
            self.tree.selection_set(str(j))

    def on_clear(self) -> None:
        self.c.clear()

    def on_load_profile(self) -> None:
        path = self.dialogs.open_profile(self.root)
        if path:
            self.c.load_profile(path)

    def on_neutral(self) -> None:
        self.c.set_neutral()

    def on_play(self) -> None:
        self._play_map = [i for i, it in enumerate(self.c.items) if it.ok]
        self._row_status = {}
        self.refresh_queue()
        self.c.start()

    def on_pause(self) -> None:
        self.c.toggle_pause()

    def on_skip(self) -> None:
        self.c.skip()

    def on_stop(self) -> None:
        self.c.stop()

    # ---- Beenden ----------------------------------------------------------------------------------------------------
    def on_close(self) -> None:
        if self._closing:
            return
        if self.c.state == ctl.STATE_PLAYING and not self.dialogs.confirm_quit(self.root):
            return
        self._closing = True
        self.state_var.set("Wird beendet ...")
        self.root.update_idletasks()
        self.shutdown_ok = self.c.shutdown(10.0)
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()
