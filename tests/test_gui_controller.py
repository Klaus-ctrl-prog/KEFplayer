"""Headless-Tests des GUI-Controllers (ohne tkinter) gegen den simulierten Renderer."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import soundfile as sf

from kefplayer import audio
from kefplayer.gui import controller as ctl
from kefplayer.gui.controller import Controller, Settings
from mock_renderer import MockRenderer


# ---- Hilfen ---------------------------------------------------------------------------------

def wait_for(pred, timeout=20.0, step=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


class Collector:
    """Sammelt alle Meldungen eines Controllers (drain) fuer Auswertung und Warten."""

    def __init__(self, c):
        self.c, self.msgs = c, []

    def pump(self):
        self.msgs += self.c.drain()

    def kinds(self):
        self.pump()
        return [m.kind for m in self.msgs]

    def of(self, kind):
        self.pump()
        return [m.data for m in self.msgs if m.kind == kind]

    def wait_kind(self, kind, n=1, timeout=20.0):
        return wait_for(lambda: len(self.of(kind)) >= n, timeout)

    def track_events(self, event):
        return [d["event"] for d in self.of("player_event") if d["event"]["event"] == event]


@pytest.fixture
def env(tmp_path):
    made = []

    def build(mock_kw=None, **settings_kw):
        mock = MockRenderer(**(mock_kw or {})).start()
        settings_kw.setdefault("bind", "127.0.0.1")
        settings_kw.setdefault("port", 0)
        settings_kw.setdefault("poll_s", 0.02)
        settings_kw.setdefault("log_path", str(tmp_path / "gui_log.jsonl"))
        settings_kw.setdefault("player_extra", {"end_tolerance_s": 1.0})
        c = Controller(Settings(**settings_kw), ssdp_search=lambda **kw: [{"location": mock.location, "_from": "127.0.0.1"}])
        col = Collector(c)
        made.append((mock, c))
        return mock, c, col

    yield build
    for mock, c in made:
        c.shutdown(5.0)
        mock.stop()


def connect(c, col, mock):
    assert c.connect(mock.location)
    assert col.wait_kind("device")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)


# ---- Warteschlange ----------------------------------------------------------------------------

def test_add_files_and_folder_filters_and_marks_invalid(env, tone_factory, tmp_path):
    _, c, col = env()
    a = tone_factory("a")
    sub = tmp_path / "album"
    sub.mkdir()
    b = tone_factory("b")
    b.rename(sub / "b.flac")
    (sub / "notiz.txt").write_text("x")
    bad = sub / "kaputt.flac"
    bad.write_bytes(b"kein flac")
    n = c.add_paths([a, sub])
    assert n == 3  # a.flac, b.flac, kaputt.flac (notiz.txt wird im Ordner ignoriert)
    assert [i.path.name for i in c.items] == ["a.flac", "b.flac", "kaputt.flac"]
    assert [i.ok for i in c.items] == [True, True, False]
    assert c.items[0].info["samplerate"] == 44100
    assert [p.name for p in c.playable()] == ["a.flac", "b.flac"]
    warn = col.of("warning")
    assert any("kaputt.flac" in w["text"] for w in warn)
    assert col.of("queue_changed")[-1]["count"] == 3


def test_add_non_audio_file_and_missing_path_warn(env, tmp_path):
    _, c, col = env()
    txt = tmp_path / "x.txt"
    txt.write_text("x")
    assert c.add_paths([txt, tmp_path / "gibt_es_nicht"]) == 0
    texts_ = [w["text"] for w in col.of("warning")]
    assert any("x.txt" in t for t in texts_) and any("nicht gefunden" in t for t in texts_)
    assert c.items == []


def test_remove_move_clear(env, tone_factory):
    _, c, _ = env()
    files = [tone_factory(n) for n in "abc"]
    c.add_paths(files)
    c.move(0, 1)
    assert [i.path.stem for i in c.items] == ["b", "a", "c"]
    c.move(0, -1)  # ausserhalb: keine Aenderung
    c.move(2, 1)
    assert [i.path.stem for i in c.items] == ["b", "a", "c"]
    c.remove(1)
    assert [i.path.stem for i in c.items] == ["b", "c"]
    c.remove(9)
    assert len(c.items) == 2
    c.clear()
    assert c.items == []


# ---- Profil -------------------------------------------------------------------------------------

def test_summary_neutral_and_convert(env):
    _, c, _ = env()
    assert c.summary() == "Profil: neutral. Dateien werden unveraendert gesendet."
    c.settings.bits = 16
    assert "umgewandelt" in c.summary() and "16 Bit" in c.summary()
    c.settings.bits, c.settings.always_process = None, True
    assert "immer neu schreiben" in c.summary()


def test_load_profile_shows_warnings_not_blocking(env, tmp_path):
    _, c, col = env()
    p = tmp_path / "boost.json"
    p.write_text(json.dumps({"name": "boost", "preamp_db": "auto",
                             "filters": [{"type": "peq", "fc": 3000, "gain_db": 3.0, "q": 2.0}],
                             "channels": {"R": {"gain_db": -0.5}}}))
    assert c.load_profile(str(p))
    info = col.of("profile")[-1]
    assert info["name"] == "boost" and info["neutral"] is False and info["preamp"] == "auto"
    assert any("Boost" in w for w in info["warnings"])
    assert any("Kanalunterschiede" in w for w in info["warnings"])
    assert "Equalizer APO" in info["signalpath_hint"]
    assert "boost" in info["summary"] and info["channels"]["R"]["gain_db"] == -0.5
    c.set_neutral()
    assert col.of("profile")[-1]["neutral"] is True and c.profile_warnings == []


def test_load_profile_error_keeps_previous(env, tmp_path):
    _, c, col = env()
    good = tmp_path / "ok.json"
    good.write_text(json.dumps({"name": "gut", "filters": [{"type": "peq", "fc": 3000, "gain_db": -2.0, "q": 3.0}]}))
    bad = tmp_path / "bad.json"
    bad.write_text("{kein json")
    assert c.load_profile(str(good))
    assert not c.load_profile(str(bad))
    assert not c.load_profile(str(tmp_path / "fehlt.json"))
    assert c.profile.name == "gut" and c.profile_path == str(good)
    errs = col.of("error")
    assert len(errs) == 2 and all("Profil nicht geladen" in e["text"] for e in errs)


def test_load_apo_profile_notes_and_delay_limit(env, tmp_path):
    _, c, col = env()
    p = tmp_path / "apo.txt"
    p.write_text("Preamp: -3 dB\nFilter 1: ON PK Fc 100 Hz Gain -2 dB Q 2\nDelay: 100 ms\n")
    assert not c.load_profile(str(p))  # Delay ausserhalb 0..50 ms wird abgelehnt (wie im Kern seit 0.2.1)
    assert "Delay" in col.of("error")[-1]["text"]


# ---- Geraet -----------------------------------------------------------------------------------------

def test_search_connect_probe(env):
    mock, c, col = env()
    assert c.search()
    assert col.wait_kind("devices")
    dev = col.of("devices")[-1]
    assert dev["hint"] is None and len(dev["devices"]) == 1 and dev["devices"][0]["usable"] is True
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    assert c.connect(dev["devices"][0]["location"])
    assert col.wait_kind("device")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    assert c.renderer is not None
    assert c.probe()
    assert col.wait_kind("probe")
    pr = col.of("probe")[-1]
    assert pr["error"] is None and "audio/flac" in " ".join(pr["mimes"]).lower()
    assert not any("Volume" in a for a, _ in mock.actions)  # probe liest nur


def test_search_without_result_gives_hint(tmp_path):
    c = Controller(Settings(), ssdp_search=lambda **kw: [])
    col = Collector(c)
    assert c.search()
    assert col.wait_kind("devices")
    d = col.of("devices")[-1]
    assert d["devices"] == [] and "Kein Renderer gefunden" in d["hint"]
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)


def test_connect_unreachable_reports_error_and_returns_to_idle(env):
    _, c, col = env()
    assert c.connect("http://127.0.0.1:9/desc.xml")
    assert col.wait_kind("error")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    assert c.renderer is None


# ---- Start-Voraussetzungen -----------------------------------------------------------------------------

def test_start_requires_device_and_playable_files(env, tone_factory, tmp_path):
    mock, c, col = env()
    assert not c.start()
    assert "Kein Geraet" in col.of("error")[-1]["text"]
    connect(c, col, mock)
    assert not c.start()
    assert "keine abspielbaren" in col.of("error")[-1]["text"]
    bad = tmp_path / "kaputt.flac"
    bad.write_bytes(b"x")
    c.add_paths([bad])
    assert not c.start()  # nur ungueltige Eintraege
    assert c.state == ctl.STATE_IDLE and mock.actions == [] or all(a != "Play" for a, _ in mock.actions)


# ---- Wiedergabe -----------------------------------------------------------------------------------------

def test_play_neutral_sends_original_and_returns_idle(env, tone_factory, tmp_path):
    mock, c, col = env()
    src = [tone_factory("a", seconds=1.0), tone_factory("b", seconds=1.0, subtype="PCM_24", fs=48000)]
    connect(c, col, mock)
    c.add_paths(src)
    assert c.start()
    assert col.wait_kind("playback_done")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    done = col.of("playback_done")[-1]
    assert done["played"] == 2 and done["problems"] == 0 and done["reasons"] == ["ended", "ended"]
    assert [d["data"] for d in mock.fetched] == [p.read_bytes() for p in src]
    assert not any("Volume" in a for a, _ in mock.actions)
    assert [r["end_reason"] for r in c.results] == ["ended", "ended"]
    kinds = col.kinds()
    assert kinds.index("state") < kinds.index("player_event") < kinds.index("playback_done")
    states = [d["state"] for d in col.of("state")]
    assert states[states.index(ctl.STATE_PLAYING):] == [ctl.STATE_PLAYING, ctl.STATE_IDLE]
    # Protokolldatei der GUI-Einstellung
    lines = [json.loads(l) for l in Path(c.settings.log_path).read_text(encoding="utf-8").splitlines()]
    assert [x["event"] for x in lines if x["event"] in ("queue_start", "queue_end")] == ["queue_start", "queue_end"]
    assert c.now["total"] == 2 and c.now["name"] == "b.flac"


def test_play_with_profile_processes_and_matches_cli_function(env, tone_factory, tmp_path):
    mock, c, col = env()
    src = tone_factory("a", seconds=1.0)
    p = tmp_path / "tief.json"
    p.write_text(json.dumps({"name": "tief", "filters": [{"type": "lowpass", "fc": 500, "gain_db": 0, "q": 0.7071}]}))
    connect(c, col, mock)
    assert c.load_profile(str(p))
    c.add_paths([src])
    assert c.start() and col.wait_kind("playback_done")
    assert col.of("playback_done")[-1]["reasons"] == ["ended"]
    assert mock.fetched[0]["data"] != src.read_bytes()  # gefilterte Datei wurde gesendet
    started = col.track_events("track_start")[0]
    assert started["processed"] is True


def test_skip_then_ended(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0), tone_factory("b", seconds=0.6)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    c.skip()
    assert col.wait_kind("playback_done")
    assert col.of("playback_done")[-1]["reasons"] == ["skipped", "ended"]


def test_stop_ends_cleanly_and_stops_server(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0), tone_factory("b", seconds=4.0)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    c.stop()
    assert col.wait_kind("playback_done")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    done = col.of("playback_done")[-1]
    assert done["reasons"] == ["stopped"]
    assert any(a == "Stop" for a, _ in mock.actions)
    states = [d["state"] for d in col.of("state")]
    assert ctl.STATE_STOPPING in states and states[-1] == ctl.STATE_IDLE
    assert c._player is None


def test_pause_request_calls_renderer_pause_and_play(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    assert wait_for(lambda: mock.state == "PLAYING")
    c.toggle_pause()
    assert wait_for(lambda: mock.state == "PAUSED_PLAYBACK")
    assert col.of("pause_requested")
    c.toggle_pause()
    assert wait_for(lambda: mock.state == "PLAYING")
    c.stop()
    assert col.wait_kind("playback_done")


def test_second_start_while_playing_is_refused(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    assert not c.start()
    assert not c.search()
    errs = [e["text"] for e in col.of("error")]
    assert sum("laeuft bereits" in e for e in errs) == 2
    c.stop()
    assert col.wait_kind("playback_done")


def test_queue_locked_while_playing(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0), tone_factory("b", seconds=1.0)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    c.remove(1)
    c.move(0, 1)
    c.clear()
    assert len(c.items) == 2
    assert sum("gesperrt" in e["text"] for e in col.of("error")) == 3
    c.stop()
    assert col.wait_kind("playback_done")


def test_port_in_use_reports_error_and_returns_idle(env, tone_factory):
    mock, c, col = env()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        c.settings.port = blocker.getsockname()[1]
        connect(c, col, mock)
        c.add_paths([tone_factory("a")])
        assert c.start()
        assert col.wait_kind("error")
        assert wait_for(lambda: c.state == ctl.STATE_IDLE)
        assert "Medienserver konnte nicht starten" in col.of("error")[-1]["text"]
        assert all(a != "SetAVTransportURI" for a, _ in mock.actions)
    finally:
        blocker.close()


def test_rejected_track_gives_hint_and_queue_continues(env, tone_factory):
    mock, c, col = env(mock_kw={"reject_mimes": ["audio/flac"]})
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=0.6)])
    assert c.start() and col.wait_kind("playback_done")
    done = col.of("playback_done")[-1]
    assert done["problems"] == 1
    assert any("nicht gespielt" in w["text"] or "protocolInfo" in w["text"] for w in col.of("warning"))


def test_never_started_without_requests_hints_firewall(env, tone_factory):
    mock, c, col = env(mock_kw={"stall": True}, player_extra={"end_tolerance_s": 1.0, "start_timeout_s": 0.5})
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=0.6)])
    assert c.start() and col.wait_kind("playback_done", timeout=30.0)
    reasons = col.of("playback_done")[-1]["reasons"]
    assert reasons == ["never_started"]


def test_shutdown_while_playing_stops_in_time(env, tone_factory):
    mock, c, col = env()
    connect(c, col, mock)
    c.add_paths([tone_factory("a", seconds=4.0)])
    assert c.start()
    assert wait_for(lambda: len(col.track_events("track_start")) >= 1)
    assert c.shutdown(10.0) is True
    assert any(a == "Stop" for a, _ in mock.actions)
    assert c.state == ctl.STATE_IDLE


# ---- Verarbeiten --------------------------------------------------------------------------------------------

def test_process_per_file_continues_after_error_and_matches_core(env, tone_factory, tmp_path):
    _, c, col = env()
    a, b = tone_factory("a", seconds=0.5), tone_factory("b", seconds=0.5, fs=48000, subtype="PCM_24")
    c.add_paths([a, b])
    # Datei nach dem Hinzufuegen beschaedigen: Vorab-Pruefung war ok, die Verarbeitung scheitert
    bad = tone_factory("c", seconds=0.5)
    c.add_paths([bad])
    bad.write_bytes(b"zerstoert")
    p = tmp_path / "tief.json"
    p.write_text(json.dumps({"name": "tief", "filters": [{"type": "lowpass", "fc": 500, "gain_db": 0, "q": 0.7071}]}))
    assert c.load_profile(str(p))
    out = tmp_path / "out"
    assert c.process(str(out), fmt="wav", bits=24, numbered=True)
    assert col.wait_kind("process_done")
    assert wait_for(lambda: c.state == ctl.STATE_IDLE)
    done = col.of("process_done")[-1]
    assert (done["ok"], done["failed"], done["total"], done["cancelled"]) == (2, 1, 3, False)
    items = col.of("process_item")
    assert [i["ok"] for i in items] == [True, True, False] and items[2]["error"]
    # Gleiches Ergebnis wie direkter Aufruf der Kernfunktion (WAV ist bytegleich)
    ref = tmp_path / "ref"
    for n, f in enumerate([a, b]):
        audio.process_file(f, ref, c.profile, fmt="wav", bits=24, stem=f"{n:03d}_{f.stem}")
    for name in ("000_a.wav", "001_b.wav"):
        assert (out / name).read_bytes() == (ref / name).read_bytes()
    assert not (out / "002_c.wav").exists()


def test_process_without_items_or_while_busy_refused(env, tone_factory, tmp_path):
    mock, c, col = env()
    assert not c.process(str(tmp_path / "o"))
    assert "keine abspielbaren" in col.of("error")[-1]["text"]


# ---- Abgrenzung ---------------------------------------------------------------------------------------------------

def test_controller_does_not_import_tkinter_and_cli_does_not_import_gui():
    root = str(Path(__file__).resolve().parents[1])
    pre = f"import sys; sys.path.insert(0, {root!r}); "
    code1 = (pre + "import kefplayer.gui.controller; "
             "sys.exit(1 if any(m == 'tkinter' or m == '_tkinter' for m in sys.modules) else 0)")
    code2 = (pre + "import kefplayer.cli; "
             "sys.exit(1 if any(m.startswith('kefplayer.gui') for m in sys.modules) else 0)")
    for code in (code1, code2):
        r = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ---- F-07 (GUI): wie die CLI keine stillen Ueberschreibungen ----

def test_process_gleiche_basisnamen_werden_vorab_abgelehnt(env, tone_factory, tmp_path):
    import shutil
    _, c, col = env()
    src = tone_factory("x", seconds=0.2)
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        shutil.copy(src, tmp_path / d / "song.flac")
    c.add_paths([tmp_path / "a" / "song.flac", tmp_path / "b" / "song.flac"])
    out = tmp_path / "out"
    assert not c.process(str(out))
    assert "gleicher Zielname" in col.of("error")[-1]["text"]
    assert not out.exists() or not any(out.iterdir())
    assert c.state == ctl.STATE_IDLE
    assert c.process(str(out), numbered=True)  # mit Nummerierung eindeutig
    assert col.wait_kind("process_done")
    assert sorted(p.name for p in out.iterdir()) == ["000_song.flac", "001_song.flac"]


def test_process_vorhandene_zieldatei_bleibt_ohne_overwrite(env, tone_factory, tmp_path):
    _, c, col = env()
    src = tone_factory("y", seconds=0.2)
    c.add_paths([src])
    out = tmp_path / "out"
    out.mkdir()
    (out / f"{src.stem}.flac").write_bytes(b"ALT")
    assert not c.process(str(out))
    assert "existiert bereits" in col.of("error")[-1]["text"]
    assert (out / f"{src.stem}.flac").read_bytes() == b"ALT"
    assert c.process(str(out), overwrite=True)
    assert col.wait_kind("process_done")
    assert (out / f"{src.stem}.flac").read_bytes() != b"ALT"


def test_process_quelle_gleich_ziel_wird_abgelehnt(env, tone_factory, tmp_path):
    _, c, col = env()
    src = tone_factory("z", seconds=0.2)
    before = src.read_bytes()
    c.add_paths([src])
    assert not c.process(str(src.parent), overwrite=True)
    assert "Quelldatei selbst" in col.of("error")[-1]["text"]
    assert src.read_bytes() == before
