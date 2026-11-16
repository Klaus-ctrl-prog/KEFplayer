"""Tests der reinen Anzeigelogik (ohne tkinter)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kefplayer.gui import controller as ctl
from kefplayer.gui import viewmodel as vm


def test_fmt_hms():
    assert vm.fmt_hms(None) == "--:--" and vm.fmt_hms(-1) == "--:--"
    assert vm.fmt_hms(0) == "00:00" and vm.fmt_hms(72.9) == "01:12" and vm.fmt_hms(3725) == "1:02:05"


def test_wall_since():
    now = datetime(2026, 11, 15, 12, 0, 10, tzinfo=timezone.utc)
    assert vm.wall_since("2026-11-15T12:00:00.000+00:00", now) == 10.0
    assert vm.wall_since("2026-11-15T12:00:20+00:00", now) == 0.0  # nie negativ
    assert vm.wall_since("kein datum", now) is None and vm.wall_since(None, now) is None
    naive = (now - timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    assert vm.wall_since(naive, now) == 5.0  # ohne Zeitzone: als UTC gelesen


def test_format_text_and_item_row():
    info = {"format": "FLAC", "samplerate": 44100, "bits": 16}
    assert vm.format_text(info) == "FLAC 44.1 kHz 16 Bit"
    assert vm.format_text({"format": "WAV", "samplerate": 96000, "bits": None}) == "WAV 96 kHz Bittiefe unbekannt"
    assert vm.format_text(None) == "unbekannt" and vm.format_text({}) == "unbekannt"
    assert vm.item_row(0, ctl.Item(Path("x/a.flac"), True, info)) == ("001", "a.flac", "FLAC 44.1 kHz 16 Bit", "")
    nr, name, fmt, status = vm.item_row(11, ctl.Item(Path("b.flac"), False, None, "kaputt"))
    assert (nr, name, fmt) == ("012", "b.flac", "nicht lesbar") and "kaputt" in status


def test_end_text():
    assert vm.end_text("ended") == "gespielt" and vm.end_text("never_started") == "startete nie"
    assert vm.end_text("neu") == "neu" and vm.end_text(None) == "unbekannt"


def test_now_text_states():
    assert vm.now_text(None, None, pause_requested=False) == "Kein Titel aktiv."
    assert vm.now_text({"index": None, "total": 3, "name": None}, None, pause_requested=False) == "Kein Titel aktiv."
    now = datetime(2026, 11, 15, 12, 1, 12, tzinfo=timezone.utc)
    t = vm.now_text({"index": 1, "total": 12, "name": "b.flac"}, "2026-11-15T12:00:00+00:00", pause_requested=False, now_utc=now)
    assert t == "Titel 2 von 12: b.flac | Wandzeit seit Titelstart 01:12"
    t = vm.now_text({"index": 0, "total": 1, "name": "a"}, None, pause_requested=True, now_utc=now)
    assert "--:--" in t and "nicht bestaetigt" in t  # Pause wird nie als bestaetigt dargestellt


def test_log_lines():
    L = lambda k, **d: vm.log_line(ctl.Msg(k, d))  # noqa: E731
    assert L("warning", text="x") == "Hinweis: x" and L("error", text="y") == "FEHLER: y"
    assert L("devices", devices=[], hint="Kein Renderer gefunden. Tipps") .startswith("Kein Renderer")
    assert L("devices", devices=[{}, {}], hint=None) == "2 Geraet(e) gefunden."
    assert "KEF" in L("device", name="KEF", host="1.2.3.4")
    assert L("profile", name="p", neutral=True) == "Profil: p (neutral)"
    assert L("queue_changed", count=3) is None and L("state", state="idle") is None
    assert L("playback_done", played=2, problems=1, log="l.jsonl").startswith("Fertig: 2 Titel, 1 mit Problemen")
    start = {"event": "track_start", "index": 1, "path": "/m/b.flac", "mime": "audio/flac", "mime_in_sink": None,
             "processed": False, "served_format": {"format": "FLAC", "samplerate": 44100, "bits": 16}}
    s = L("player_event", event=start)
    assert s.startswith("> 002 b.flac") and "Sink unbekannt" in s and "unveraendert" in s
    end = {"event": "track_end", "end_reason": "ended", "startup_delay_s": 0.12, "gap_before_s": 0.08, "ended_early": True}
    e = L("player_event", event=end)
    assert "gespielt" in e and "Start nach 0.12 s" in e and "Luecke" in e and "frueher" in e
    assert L("player_event", event={"event": "queue_end"}) is None
    assert L("player_event", event={"event": "etwas_neues"}) is None


@pytest.mark.parametrize("state,dev,n,expected_on", [
    (ctl.STATE_IDLE, False, 0, {"search", "connect", "queue_edit", "profile"}),
    (ctl.STATE_IDLE, True, 0, {"search", "connect", "queue_edit", "profile"}),
    (ctl.STATE_IDLE, False, 3, {"search", "connect", "queue_edit", "profile"}),
    (ctl.STATE_IDLE, True, 3, {"search", "connect", "queue_edit", "profile", "play"}),
    (ctl.STATE_PLAYING, True, 3, {"pause", "skip", "stop"}),
    (ctl.STATE_STOPPING, True, 3, set()),
    (ctl.STATE_BUSY, True, 3, set()),
    (ctl.STATE_PROCESSING, True, 3, set()),
])
def test_button_states(state, dev, n, expected_on):
    st = vm.button_states(state, has_device=dev, n_playable=n)
    assert {k for k, v in st.items() if v} == expected_on
    assert set(st) == {"search", "connect", "queue_edit", "profile", "play", "pause", "skip", "stop"}


def test_window_size_scales_and_clamps():
    # 100 %: mindestens 900 breit, angeforderte Hoehe, Mindestgroesse 780 / 75 %
    assert vm.window_size(850, 700, 1.0, 1920, 1080) == (900, 700, 780, 525)
    # 200 % auf 2880x1800: Pixelwerte wachsen mit, Inhalt bestimmt die Hoehe
    assert vm.window_size(1500, 1400, 2.0, 2880, 1800) == (1800, 1400, 1560, 1050)
    # Inhalt groesser als der Bildschirm: auf 90 % begrenzt, Mindestgroesse nie ueber der Startgroesse
    w, h, minw, minh = vm.window_size(2000, 2000, 2.0, 1280, 1024)
    assert (w, h) == (1152, 921) and minw <= w and minh <= h
