import io
import json
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from kefplayer import audio, upnp
from kefplayer.dsp import ChannelSpec, Filter, Profile
from kefplayer.mediaserver import MediaServer
from kefplayer.player import Player
from kefplayer.runlog import RunLog
from mock_renderer import MockRenderer


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def decode(data):
    return sf.read(io.BytesIO(data), dtype="float64", always_2d=True)


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]


# ---- neutraler Weg: Original unveraendert ----

def test_neutral_serves_original_bytes(rig, tone_factory, tmp_path):
    src = tone_factory("a", seconds=1.0, fs=44100, subtype="PCM_16")
    mock, server, player = rig()
    res = player.play_queue([src])
    assert [r["end_reason"] for r in res] == ["ended"]
    assert mock.fetched[0]["data"] == src.read_bytes()
    assert mock.fetched[0]["content_type"] == "audio/flac"
    r = res[0]
    assert r["processed"] is False and r["mime"] == "audio/flac" and r["mime_in_sink"] is True
    assert r["served_format"]["samplerate"] == 44100 and r["served_format"]["bits"] == 16 and r["served_format"]["channels"] == 2
    assert r["startup_delay_s"] >= 0.02 and r["ended_early"] is False
    # Renderer hat HEAD und GET gestellt, vollstaendig uebertragen
    assert mock.head_count == 1
    methods = [(q["method"], q["status"]) for q in r["requests"]]
    assert ("HEAD", 200) in methods and ("GET", 200) in methods
    assert max(q["bytes_sent"] for q in r["requests"]) == src.stat().st_size
    # Metadaten, die der Renderer bekam
    md = mock.metadata[0]
    assert md["mime"] == "audio/flac" and md["res"]["sampleFrequency"] == "44100" and md["res"]["bitsPerSample"] == "16"
    assert md["res"]["nrAudioChannels"] == "2" and md["url"].startswith("http://127.0.0.1:")
    # Lautstaerke wird nie angefasst
    assert not any("Volume" in a for a, _ in mock.actions)


def test_action_order_per_track(rig, tone_factory):
    src = [tone_factory(f"t{i}", seconds=0.8) for i in range(2)]
    mock, server, player = rig()
    player.play_queue(src)
    names = [a for a, _ in mock.actions if a in ("SetAVTransportURI", "Play")]
    assert names == ["SetAVTransportURI", "Play", "SetAVTransportURI", "Play"]
    assert mock.actions[0][0] == "GetProtocolInfo"


def test_queue_order_gaps_and_log(rig, tone_factory, tmp_path):
    files = [tone_factory(f"q{i}", seconds=0.8, freq=500 + 100 * i) for i in range(3)]
    mock, server, player = rig()
    res = player.play_queue(files)
    assert [r["end_reason"] for r in res] == ["ended"] * 3
    assert "gap_before_s" not in res[0]
    for r in res[1:]:
        assert 0 <= r["gap_before_s"] < 3
    assert [f["uri"].rsplit("/", 1)[-1][:3] for f in mock.fetched] == ["000", "001", "002"]
    events = [e["event"] for e in read_log(tmp_path)]
    assert events[0] == "queue_start" and events[-1] == "queue_end"
    assert events.count("track_start") == 3 and events.count("track_end") == 3
    assert "sink" in events
    start = read_log(tmp_path)[0]
    assert start["profile_neutral"] is True and start["renderer"] == "Mock KEF"
    # beendete Titel sind nicht mehr abrufbar
    assert server.entries == {}


# ---- gefilterter Weg ----

def test_filtered_track_arrives_filtered(rig, tone_factory):
    src = tone_factory("f", freq=1000, seconds=2.0, fs=48000, subtype="PCM_24")
    prof = Profile(filters=[Filter("peq", 1000, -6.0, 2.0)])
    mock, server, player = rig(prof)
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "ended" and res[0]["processed"] is True
    got, fs = decode(mock.fetched[0]["data"])
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    assert fs == 48000 and got.shape == ref.shape
    assert 20 * np.log10(rms(got[fs:, 0]) / rms(ref[fs:, 0])) == pytest.approx(-6.0, abs=0.15)
    assert res[0]["served_format"]["bits"] == 24 and res[0]["served_format"]["samplerate"] == 48000
    assert mock.metadata[0]["res"]["bitsPerSample"] == "24"


def test_boost_track_has_headroom_in_delivered_data(rig, tone_factory):
    src = tone_factory("b", freq=3000, seconds=1.0, fs=48000, subtype="PCM_16", amp=0.95)
    prof = Profile(filters=[Filter("peq", 3000, 6.0, 2.0)])
    mock, server, player = rig(prof)
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "ended"
    got, _ = sf.read(io.BytesIO(mock.fetched[0]["data"]), dtype="int16", always_2d=True)
    assert np.max(np.abs(got.astype(int))) < 32767


def test_left_right_difference_reaches_renderer(rig, tone_factory):
    src = tone_factory("lr", freq=1000, seconds=1.0, fs=44100, subtype="PCM_24")
    prof = Profile(channels={0: ChannelSpec(gain_db=-2.0)})
    mock, server, player = rig(prof)
    player.play_queue([src])
    got, _ = decode(mock.fetched[0]["data"])
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    assert 20 * np.log10(rms(got[:, 0]) / rms(ref[:, 0])) == pytest.approx(-2.0, abs=0.02)
    assert 20 * np.log10(rms(got[:, 1]) / rms(ref[:, 1])) == pytest.approx(0.0, abs=0.02)


def test_always_process_neutral_is_bit_exact(rig, tone_factory):
    src = tone_factory("n", seconds=0.8, subtype="PCM_24", fs=48000)
    mock, server, player = rig(serve_original_if_neutral=False)
    res = player.play_queue([src])
    assert res[0]["processed"] is True
    got, _ = sf.read(io.BytesIO(mock.fetched[0]["data"]), dtype="int32", always_2d=True)
    ref, _ = sf.read(str(src), dtype="int32", always_2d=True)
    assert np.array_equal(got, ref)


def test_temp_files_are_cleaned_up(rig, tone_factory):
    src = tone_factory("c", seconds=0.8)
    mock, server, player = rig(Profile(filters=[Filter("peq", 2000, -1.0, 1.0)]))
    res = player.play_queue([src])
    from pathlib import Path
    assert not Path(res[0]["served"]).exists()
    assert src.exists()


def test_cache_dir_is_kept(rig, tone_factory, tmp_path):
    src = tone_factory("k", seconds=0.8)
    cache = tmp_path / "cache"
    mock, server, player = rig(Profile(filters=[Filter("peq", 2000, -1.0, 1.0)]), cache_dir=cache)
    res = player.play_queue([src])
    from pathlib import Path
    assert Path(res[0]["served"]).exists() and Path(res[0]["served"]).parent == cache


# ---- Fehlerfaelle ----

def test_renderer_rejects_mime_queue_continues(rig, tone_factory, tmp_path):
    a = tone_factory("a", seconds=0.8)  # FLAC wird abgelehnt
    b = tone_factory("b", seconds=0.8, fmt="WAV")
    mock, server, player = rig(mock_kw={"reject_mimes": ["audio/flac"]})
    res = player.play_queue([a, b])
    assert res[0]["end_reason"] == "error" and "714" in res[0]["error"]
    assert res[1]["end_reason"] == "ended" and res[1]["mime"] == "audio/wav"
    assert server.entries == {}


def test_mime_not_in_sink_is_reported_but_tried(rig, tone_factory):
    src = tone_factory("m", seconds=0.8)
    mock, server, player = rig(mock_kw={"sink": ["http-get:*:audio/mpeg:*"]})
    res = player.play_queue([src])
    assert res[0]["mime_in_sink"] is False and res[0]["end_reason"] == "ended"


def test_mime_override(rig, tone_factory):
    src = tone_factory("o", seconds=0.8)
    mock, server, player = rig(mime_override="audio/x-flac")
    res = player.play_queue([src])
    assert res[0]["mime"] == "audio/x-flac" and res[0]["mime_in_sink"] is None
    assert mock.fetched[0]["content_type"] == "audio/x-flac"
    assert mock.metadata[0]["mime"] == "audio/x-flac"


def test_renderer_never_starts(rig, tone_factory):
    src = tone_factory("s", seconds=0.8)
    mock, server, player = rig(mock_kw={"stall": True}, start_timeout_s=0.4)
    t0 = time.monotonic()
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "never_started" and res[0]["startup_delay_s"] is None
    assert time.monotonic() - t0 < 5
    assert any(a == "Stop" for a, _ in mock.actions)


def test_early_end_is_flagged(rig, tone_factory):
    src = tone_factory("e", seconds=6.0)
    mock, server, player = rig(mock_kw={"cut_fraction": 0.3})
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "ended" and res[0]["ended_early"] is True
    assert 0 < res[0]["last_position_s"] < 4


def test_unreadable_file_with_filter_is_prepare_error_and_queue_continues(rig, tone_factory, tmp_path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"this is not a flac file" * 10)
    good = tone_factory("g", seconds=0.8)
    mock, server, player = rig(Profile(filters=[Filter("peq", 2000, -1.0, 1.0)]))
    res = player.play_queue([bad, good])
    assert res[0]["end_reason"] == "prepare_error" and res[0]["error"]
    assert res[1]["end_reason"] == "ended"


def test_unreachable_renderer_gives_error_not_hang(rig, tone_factory):
    src = tone_factory("u", seconds=0.8)
    mock, server, player = rig()
    mock.stop()  # Geraet verschwindet, bevor gespielt wird
    t0 = time.monotonic()
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "error"
    assert time.monotonic() - t0 < 15


class _ScriptedRenderer:
    """Liefert Antworten nach Aufrufnummer, um ein Rennen zwischen Status- und Positionsabfrage nachzustellen:
    Ab Aufruf 6 ist der Titel zu Ende (Status STOPPED, Position auf 0 zurueckgesetzt)."""

    def __init__(self):
        self.k = 0

    def position_s(self):
        self.k += 1
        if self.k <= 2:
            return 1.0
        if self.k <= 4:
            return 2.0
        return 2.9 if self.k == 5 else 0.0

    def transport_state(self):
        self.k += 1
        return "PLAYING" if self.k <= 5 else "STOPPED"

    def stop(self):
        pass


def test_track_end_between_state_and_position_is_not_reported_as_early():
    p = Player(_ScriptedRenderer(), None, poll_s=0.001, end_tolerance_s=1.0)
    out = p._wait(time.perf_counter(), 3.0)
    assert out["end_reason"] == "ended"
    assert out["last_position_s"] == 2.0  # die zurueckgesetzte 0 wird verworfen
    assert out["ended_early"] is False


# ---- Steuerung ----

def test_skip_goes_to_next_track(rig, tone_factory):
    a = tone_factory("a", seconds=20.0, fs=8000, subtype="PCM_16")
    b = tone_factory("b", seconds=0.8)
    mock, server, player = rig()

    def later():
        for _ in range(500):
            if mock.state == "PLAYING":
                break
            time.sleep(0.01)
        player.skip()

    threading.Thread(target=later, daemon=True).start()
    res = player.play_queue([a, b])
    assert [r["end_reason"] for r in res] == ["skipped", "ended"]
    assert any(act == "Stop" for act, _ in mock.actions)
    assert len(mock.fetched) == 2


def test_stop_ends_queue(rig, tone_factory):
    a = tone_factory("a", seconds=20.0, fs=8000, subtype="PCM_16")
    b = tone_factory("b", seconds=0.8)
    mock, server, player = rig()

    def later():
        for _ in range(500):
            if mock.state == "PLAYING":
                break
            time.sleep(0.01)
        player.stop()

    threading.Thread(target=later, daemon=True).start()
    res = player.play_queue([a, b])
    assert [r["end_reason"] for r in res] == ["stopped"]
    assert len(mock.fetched) == 1


def test_pause_and_resume(rig, tone_factory):
    a = tone_factory("a", seconds=10.0, fs=8000, subtype="PCM_16")
    mock, server, player = rig()

    def later():
        for _ in range(500):
            if mock.state == "PLAYING":
                break
            time.sleep(0.01)
        player.toggle_pause()
        for _ in range(500):
            if mock.state == "PAUSED_PLAYBACK":
                break
            time.sleep(0.01)
        time.sleep(0.1)
        player.toggle_pause()

    threading.Thread(target=later, daemon=True).start()
    res = player.play_queue([a])
    names = [act for act, _ in mock.actions]
    assert "Pause" in names and names.count("Play") == 2
    assert res[0]["end_reason"] == "ended" and res[0]["ended_early"] is False


# ---- Fehlerklassifikation (Phase B1) ----------------------------------------------------------------


def test_classify_error_categories():
    import socket
    import urllib.error

    from kefplayer.player import classify_error, classify_prepare_error

    assert classify_error(upnp.UpnpError(701, "x")) == "renderer_soap_fault"
    assert classify_error(upnp.UpnpError(None, "HTTP 500: kaputt")) == "renderer_http_error"
    assert classify_error(upnp.UpnpError(None, "SOAP-Antwort ohne Body")) == "renderer_bad_response"
    assert classify_error(socket.timeout("t")) == "renderer_timeout"
    assert classify_error(urllib.error.URLError(socket.timeout("t"))) == "renderer_timeout"
    assert classify_error(ConnectionRefusedError()) == "renderer_unreachable"
    assert classify_error(urllib.error.URLError("no route")) == "renderer_unreachable"
    assert classify_error(ValueError("x")) == "unknown"
    assert classify_prepare_error(FileNotFoundError()) == "source_file_access"
    assert classify_prepare_error(PermissionError()) == "source_file_access"
    assert classify_prepare_error(OSError("disk")) == "file_io"
    assert classify_prepare_error(audio.AudioProcessError("cache_io", "disk")) == "cache_io"
    assert classify_prepare_error(ValueError("x")) == "processing_error"


def test_prepare_error_has_category(rig, tone_factory, tmp_path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"this is not a flac file" * 10)
    mock, server, player = rig(Profile(filters=[Filter("peq", 2000, -1.0, 1.0)]))
    res = player.play_queue([bad])
    assert res[0]["end_reason"] == "prepare_error" and res[0]["error_category"] == "source_format"


def test_unreachable_renderer_has_category(rig, tone_factory):
    src = tone_factory("uc", seconds=0.8)
    mock, server, player = rig()
    mock.stop()
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "error"
    assert res[0]["error_category"] in ("renderer_unreachable", "renderer_timeout")


def test_never_started_category_follows_media_server_requests(rig, tone_factory):
    src = tone_factory("ns", seconds=0.8)
    mock, server, player = rig(mock_kw={"stall": True}, start_timeout_s=0.4)
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "never_started"
    expected = "renderer_fetched_no_play" if res[0]["requests"] else "renderer_never_fetched"
    assert res[0]["error_category"] == expected


def test_ok_track_has_no_error_category(rig, tone_factory):
    src = tone_factory("ok", seconds=0.8)
    mock, server, player = rig()
    res = player.play_queue([src])
    assert res[0]["end_reason"] == "ended" and "error_category" not in res[0]


# ---- Wiederholung bei voruebergehenden Fehlern (Phase B2) -------------------------------------------


class _Flaky:
    """Renderer-Attrappe: set_uri/play/stop scheitern n-mal mit dem angegebenen Fehler, dann gelingt der Aufruf."""

    def __init__(self, fails, exc):
        self.fails, self.exc, self.calls = fails, exc, 0

    def _go(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return "ok"

    set_uri = play = stop = lambda self, *a: self._go()


def _retry_player(fails, exc, **kw):
    ev = []
    r = _Flaky(fails, exc)
    p = Player(r, None, poll_s=0.001, retry_backoff_s=0.001, on_event=lambda rec: ev.append(rec), **kw)
    return p, r, ev


def test_retry_recovers_after_transient_timeout():
    import socket

    p, r, ev = _retry_player(2, socket.timeout("t"))
    assert p._call("play", r.play) == "ok" and r.calls == 3
    assert [e["event"] for e in ev] == ["renderer_retry", "renderer_retry", "renderer_recovered"]
    assert ev[0]["category"] == "renderer_timeout" and ev[0]["attempt"] == 1


def test_retry_is_limited_and_reraises_last_error():
    p, r, ev = _retry_player(99, ConnectionRefusedError())
    with pytest.raises(ConnectionRefusedError):
        p._call("play", r.play)
    assert r.calls == 3 and sum(e["event"] == "renderer_retry" for e in ev) == 2


def test_soap_fault_is_not_retried():
    p, r, ev = _retry_player(99, upnp.UpnpError(701, "x"))
    with pytest.raises(upnp.UpnpError):
        p._call("play", r.play)
    assert r.calls == 1 and not ev


def test_no_retry_while_stop_requested():
    p, r, ev = _retry_player(99, ConnectionRefusedError())
    p._stop.set()
    with pytest.raises(ConnectionRefusedError):
        p._call("set_uri", r.set_uri)
    assert r.calls == 1 and not ev


def test_stop_interrupts_retry_backoff():
    entered_backoff = threading.Event()
    ev = []
    r = _Flaky(99, ConnectionRefusedError())
    p = Player(
        r,
        None,
        retry_backoff_s=30.0,
        on_event=lambda rec: (ev.append(rec), entered_backoff.set()),
    )
    outcome = []

    def call():
        try:
            p._call("play", r.play)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert entered_backoff.wait(2.0)
    p.stop()
    worker.join(1.0)

    assert not worker.is_alive()
    assert r.calls == 1
    assert len(outcome) == 1 and isinstance(outcome[0], ConnectionRefusedError)
    assert [e["event"] for e in ev] == ["renderer_retry"]


def test_stop_uses_at_most_two_attempts_and_reports_stop_failed():
    p, r, ev = _retry_player(99, ConnectionRefusedError())
    p.renderer = r
    p._safe_stop()
    assert r.calls == 2 and ev[-1]["event"] == "stop_failed"


def test_retry_attempts_one_disables_retry():
    p, r, ev = _retry_player(99, ConnectionRefusedError(), retry_attempts=1)
    with pytest.raises(ConnectionRefusedError):
        p._call("play", r.play)
    assert r.calls == 1


def test_retries_show_up_in_status_findings():
    from kefplayer import api

    c = api.StatusCollector()
    c.on_event({"ts": "t", "event": "renderer_retry", "action": "play", "attempt": 1, "category": "renderer_timeout", "error": "x"})
    c.on_event({"ts": "t", "event": "renderer_recovered", "action": "play", "attempts": 2})
    app = api.StatusApp(c)
    snap = c.snapshot()
    f = [x for x in app.findings(snap, app._live(False)) if x["code"] == "renderer_retries"]
    assert f and f[0]["evidence"] == {"retries": 1, "recovered": 1}
