"""Tk-Rauch- und Ablauftests des Hauptfensters gegen den simulierten Renderer.

Laufen nur, wenn tkinter importierbar ist und ein Display zur Verfuegung steht (Linux: ``xvfb-run -a pytest ...``);
sonst werden sie uebersprungen.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from kefplayer.gui import controller as ctl  # noqa: E402
from kefplayer.gui.controller import Controller, Settings  # noqa: E402
from kefplayer.gui.view_main import MainWindow  # noqa: E402
from mock_renderer import MockRenderer  # noqa: E402


class FakeDialogs:
    def __init__(self):
        self.files, self.folder, self.profile, self.quit_ok, self.quit_asked = [], "", "", True, 0

    def open_files(self, parent):
        return self.files

    def open_folder(self, parent):
        return self.folder

    def open_profile(self, parent):
        return self.profile

    def confirm_quit(self, parent):
        self.quit_asked += 1
        return self.quit_ok


def pump_until(root, win, pred, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        root.update()
        win.pump()
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def gui(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as e:  # kein Display
        pytest.skip(f"kein Display: {e}")
    root.withdraw()
    made = {}

    def build(**settings_kw):
        mock = MockRenderer().start()
        settings_kw.update(bind="127.0.0.1", port=0, poll_s=0.02, log_path=str(tmp_path / "gui_log.jsonl"),
                           player_extra={"end_tolerance_s": 1.0})
        c = Controller(Settings(**settings_kw), ssdp_search=lambda **kw: [{"location": mock.location, "_from": "127.0.0.1"}])
        dlg = FakeDialogs()
        win = MainWindow(root, c, dlg)
        made.update(mock=mock, c=c, win=win, dlg=dlg)
        return root, win, c, mock, dlg

    yield build
    if "c" in made:
        made["c"].shutdown(5.0)
        made["mock"].stop()
    try:
        root.destroy()
    except tk.TclError:
        pass


def ttk_style_lookup(root, style, option):
    from tkinter import ttk
    return ttk.Style(root).lookup(style, option) or 0


def widgets(w):
    yield w
    for ch in w.winfo_children():
        yield from widgets(ch)


def settle(root, win):
    """Wie ein Poll-Takt: Meldungen verarbeiten und Tk-Ereignisse abarbeiten."""
    root.update()
    win.pump()
    root.update()


def connect_first(root, win):
    win.on_search()
    assert pump_until(root, win, lambda: len(win.devices) == 1)
    win.combo.current(0)
    win.on_connect()
    assert pump_until(root, win, lambda: win.device_var.get().startswith("Verbunden"))


def test_initial_state_and_forbidden_controls(gui):
    root, win, c, mock, dlg = gui()
    root.update()
    assert "KEFplayer GUI" in root.title()
    for b in (win.btn_play, win.btn_pause, win.btn_skip, win.btn_stop):
        assert "disabled" in b.state()
    assert "!disabled" in win.btn_search.state() or "disabled" not in win.btn_search.state()
    assert win.profile_var.get().startswith("Profil: neutral. Dateien werden unveraendert gesendet.")
    assert win.warn_text.get("1.0", "end-1c") == "Keine Warnungen."
    assert "Protokolldatei" in win.log_contents()
    kinds = {type(w).__name__ for w in widgets(root)}
    assert not kinds & {"Scale", "Spinbox"}  # keine Lautstaerke-/Pegelregler
    all_text = " ".join(str(w.cget("text")) for w in widgets(root) if "text" in w.keys()).lower()
    assert "lautstaerke der kef wird nicht veraendert" in all_text
    assert "bass" not in all_text and "preset" not in all_text
    assert "Equalizer APO" in " ".join(str(w.cget("text")) for w in widgets(root) if "text" in w.keys())


def test_full_flow_search_connect_queue_play(gui, tone_factory):
    root, win, c, mock, dlg = gui()
    connect_first(root, win)
    dlg.files = [str(tone_factory("a", seconds=1.0)), str(tone_factory("b", seconds=1.0))]
    win.on_add_files()
    settle(root, win)
    assert len(win.tree.get_children()) == 2
    assert win.tree.item("0", "values")[1] == "a.flac" and "FLAC 44.1 kHz 16 Bit" in win.tree.item("0", "values")[2]
    assert "disabled" not in win.btn_play.state()
    seen_now = []
    win.on_play()
    assert pump_until(root, win, lambda: (seen_now.append(win.now_var.get()) or True) and
                      win.tree.item("1", "values")[3] == "gespielt" and win.tree.item("0", "values")[3] == "gespielt")
    assert any(s.startswith("Titel 1 von 2: a.flac | Wandzeit seit Titelstart") for s in seen_now)
    assert pump_until(root, win, lambda: win.state_var.get() == "Bereit")
    # playback_done wird ueber die GUI-Nachrichtenqueue zugestellt; nicht
    # voraussetzen, dass die Abschlussmeldung im selben Tk-Pump-Zyklus erscheint.
    assert pump_until(root, win, lambda: "Fertig: 2 Titel, 0 mit Problemen" in win.log_contents(), timeout=5.0)
    assert "Fertig: 2 Titel, 0 mit Problemen" in win.log_contents()
    assert "> 001 a.flac" in win.log_contents() and "Ende: gespielt" in win.log_contents()
    assert [d["data"] for d in mock.fetched] == [Path(f).read_bytes() for f in dlg.files]
    assert not any("Volume" in a for a, _ in mock.actions)
    assert "disabled" in win.btn_stop.state()


def test_queue_buttons_move_remove_clear_and_lock(gui, tone_factory):
    root, win, c, mock, dlg = gui()
    dlg.files = [str(tone_factory(n, seconds=3.0)) for n in "abc"]
    win.on_add_files()
    settle(root, win)
    win.tree.selection_set("0")
    win.on_move(1)
    assert [win.tree.item(i, "values")[1] for i in win.tree.get_children()] == ["b.flac", "a.flac", "c.flac"]
    assert win.selected_index() == 1
    win.on_remove()
    settle(root, win)
    assert [win.tree.item(i, "values")[1] for i in win.tree.get_children()] == ["b.flac", "c.flac"]
    # waehrend der Wiedergabe sind die Bearbeitungsschalter gesperrt
    connect_first(root, win)
    win.on_play()
    assert pump_until(root, win, lambda: "gespielt" not in win.now_var.get() and win.now_var.get().startswith("Titel 1"))
    assert all("disabled" in b.state() for b in win.queue_buttons)
    assert "disabled" in win.btn_play.state() and "disabled" not in win.btn_stop.state()
    win.on_stop()
    assert pump_until(root, win, lambda: win.state_var.get() == "Bereit")
    assert "gestoppt" in win.tree.item("0", "values")[3]
    win.on_clear()
    settle(root, win)
    assert win.tree.get_children() == ()


def test_pause_skip_stop_buttons(gui, tone_factory):
    root, win, c, mock, dlg = gui()
    connect_first(root, win)
    dlg.files = [str(tone_factory("a", seconds=4.0)), str(tone_factory("b", seconds=4.0))]
    win.on_add_files()
    settle(root, win)
    win.on_play()
    assert pump_until(root, win, lambda: mock.state == "PLAYING")
    win.on_pause()
    assert pump_until(root, win, lambda: mock.state == "PAUSED_PLAYBACK")
    assert "Pause/Weiter angefordert (Zustand nicht bestaetigt)" in win.now_var.get()
    win.on_pause()
    assert pump_until(root, win, lambda: mock.state == "PLAYING")
    win.on_skip()
    assert pump_until(root, win, lambda: win.tree.item("0", "values")[3] == "uebersprungen")
    assert pump_until(root, win, lambda: win.now_var.get().startswith("Titel 2 von 2"))
    assert "angefordert" not in win.now_var.get()  # Anzeige nach neuem Titel zurueckgesetzt
    win.on_stop()
    assert pump_until(root, win, lambda: win.state_var.get() == "Bereit")
    assert win.tree.item("1", "values")[3] == "gestoppt"


def test_profile_load_shows_warnings_and_neutral_resets(gui, tmp_path):
    root, win, c, mock, dlg = gui()
    p = tmp_path / "boost.json"
    p.write_text(json.dumps({"name": "boost", "preamp_db": "auto",
                             "filters": [{"type": "peq", "fc": 3000, "gain_db": 3.0, "q": 2.0}],
                             "channels": {"R": {"gain_db": -0.5}}}))
    dlg.profile = str(p)
    win.on_load_profile()
    assert pump_until(root, win, lambda: win.profile_var.get().startswith("Profil: boost"))
    w = win.warn_text.get("1.0", "end-1c")
    assert "Warnung: Boost +3 dB bei 3000 Hz" in w and "Kanalunterschiede" in w
    assert "Preamp: auto" in win.profile_detail_var.get() and "Filter: 1" in win.profile_detail_var.get()
    win.on_neutral()
    assert pump_until(root, win, lambda: win.profile_var.get().startswith("Profil: neutral"))
    assert win.warn_text.get("1.0", "end-1c") == "Keine Warnungen."
    # defektes Profil: Fehler im Protokoll, Anzeige bleibt
    bad = tmp_path / "bad.json"
    bad.write_text("{x")
    dlg.profile = str(bad)
    win.on_load_profile()
    assert pump_until(root, win, lambda: "Profil nicht geladen" in win.log_contents())
    assert win.profile_var.get().startswith("Profil: neutral")


def test_url_connect_validation(gui):
    root, win, c, mock, dlg = gui()
    win.url_var.set("192.168.1.5")
    win.on_connect_url()
    assert "Adresse muss mit http://" in win.log_contents()
    win.on_connect()  # ohne Suche, nichts in der Liste
    assert "Kein Geraet in der Liste" in win.log_contents()
    win.url_var.set(mock.location)
    win.on_connect_url()
    assert pump_until(root, win, lambda: win.device_var.get().startswith("Verbunden"))


def test_close_during_playback_asks_then_shuts_down(gui, tone_factory):
    root, win, c, mock, dlg = gui()
    connect_first(root, win)
    dlg.files = [str(tone_factory("a", seconds=5.0))]
    win.on_add_files()
    settle(root, win)
    win.on_play()
    assert pump_until(root, win, lambda: mock.state == "PLAYING")
    dlg.quit_ok = False
    win.on_close()  # Nutzer lehnt ab: alles laeuft weiter
    assert dlg.quit_asked == 1 and win.shutdown_ok is None and c.state == ctl.STATE_PLAYING
    assert root.winfo_exists()
    dlg.quit_ok = True
    win.on_close()
    assert dlg.quit_asked == 2 and win.shutdown_ok is True
    assert any(a == "Stop" for a, _ in mock.actions)
    assert c.state == ctl.STATE_IDLE
    with pytest.raises(tk.TclError):  # Fenster ist zerstoert
        root.title()


@pytest.mark.parametrize("scaling", [1.333, 2.667])
def test_layout_keeps_all_sections_visible_at_100_and_200_percent(gui, scaling):
    """Regression (ASUS, 200 %): Warteschlange und Protokoll verschwanden bei fester Pixelgroesse."""
    import tkinter as tk_
    try:
        root_probe = tk_.Tk()
    except tk_.TclError as exc:
        pytest.skip(f"Tcl/Tk runtime unavailable: {exc}")
    root_probe.withdraw()
    root_probe.tk.call("tk", "scaling", scaling)
    root_probe.destroy()
    root, win, c, mock, dlg = gui()
    root.tk.call("tk", "scaling", scaling)
    win2 = None
    try:
        # neues Fenster im selben Tk bei geaenderter Skalierung aufbauen
        for ch in list(root.winfo_children()):
            ch.destroy()
        win2 = MainWindow(root, c, dlg)
        root.deiconify()
        root.update()
        assert win2.ui_scale == pytest.approx(scaling / (96 / 72), rel=0.01)
        for name in ("tree", "log_text", "warn_text", "btn_play", "btn_stop"):
            w = getattr(win2, name)
            assert w.winfo_height() > 0 and w.winfo_ismapped(), name
        import tkinter.font as tkfont_
        rowh = int(ttk_style_lookup(root, "Treeview", "rowheight"))
        assert rowh >= tkfont_.nametofont("TkDefaultFont").metrics("linespace"), rowh  # Zeilen nicht abgeschnitten
        assert win2.tree.winfo_height() >= 60 and win2.log_text.winfo_height() >= 60
        # Fenster passt auf den Bildschirm
        assert root.winfo_width() <= root.winfo_screenwidth() and root.winfo_height() <= root.winfo_screenheight()
    finally:
        if win2 is not None:
            win2.shutdown_ok = None
            if win2._after_id:
                root.after_cancel(win2._after_id)
        root.withdraw()


def test_close_when_idle_does_not_ask(gui):
    root, win, c, mock, dlg = gui()
    win.on_close()
    assert dlg.quit_asked == 0 and win.shutdown_ok is True


def test_selftest_option_runs_and_prints_tk_data():
    root = str(Path(__file__).resolve().parents[1])
    code = f"import sys; sys.path.insert(0, {root!r}); from kefplayer.gui.__main__ import main; sys.exit(main(['--selftest', '--no-dpi']))"
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and "display" in r.stderr:
        pytest.skip("kein Display")
    assert r.returncode == 0, r.stderr
    assert "Tk 8." in r.stdout and "tk scaling" in r.stdout and "DPI-Aufruf: uebersprungen" in r.stdout
