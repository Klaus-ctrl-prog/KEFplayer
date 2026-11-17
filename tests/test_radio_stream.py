"""E1 Radio: Stream-Handler im Medienserver, Player.play_stream, CLI (Simulator, lokaler Fake-Sender)."""
import http.client
import json
import threading
import time

import pytest

from kefplayer import cli, radio, upnp
from kefplayer import player as player_mod
from kefplayer.mediaserver import MediaServer
from api_helpers import wait_for
from conftest import make_tone
from mock_renderer import MockRenderer
from radio_helpers import FakeStation


def _server():
    s = MediaServer(bind="127.0.0.1", port=0)
    s.start()
    return s


def _get(url, method="GET", read=None, timeout=5):
    host, rest = url[len("http://"):].split("/", 1)
    c = http.client.HTTPConnection(host, timeout=timeout)
    c.request(method, "/" + rest)
    r = c.getresponse()
    body = r.read(read) if read is not None else r.read()
    return r, body, c


def test_stream_handler_headers_no_length_no_seek():
    chunks = [b"abc", b"def"]

    class Src:
        def __init__(self):
            self.i = 0
            self.closed = False

        def read1(self, n):
            if self.i >= len(chunks):
                return b""
            self.i += 1
            return chunks[self.i - 1]

        def close(self):
            self.closed = True

    src = Src()
    s = _server()
    try:
        url, token = s.register_stream(lambda: src, "audio/aac", "x.aac")
        r, body, c = _get(url)
        assert r.status == 200 and body == b"abcdef"
        h = {k.lower(): v for k, v in r.getheaders()}
        assert h["content-type"] == "audio/aac"
        assert "content-length" not in h and "accept-ranges" not in h and "content-range" not in h
        assert h["transfermode.dlna.org"] == "Streaming"
        assert "DLNA.ORG_OP=00" in h["contentfeatures.dlna.org"]
        assert h["connection"] == "close"
        assert wait_for(lambda: src.closed, 2)
        recs = s.requests_for(token)
        assert recs and recs[-1]["status"] == 200 and recs[-1]["bytes_sent"] == 6
    finally:
        s.stop()


def test_head_does_not_open_upstream():
    opened = []
    s = _server()
    try:
        url, _ = s.register_stream(lambda: opened.append(1) or None, "audio/aac", "x.aac")
        r, body, c = _get(url, method="HEAD")
        assert r.status == 200 and body == b"" and opened == []
    finally:
        s.stop()


def test_unreachable_upstream_gives_502_not_hang():
    def boom():
        raise radio.RadioError("stream_unreachable", "x")
    s = _server()
    try:
        url, token = s.register_stream(boom, "audio/aac", "x.aac")
        r, body, c = _get(url)
        assert r.status == 502
        assert s.requests_for(token)[-1]["status"] == 502
    finally:
        s.stop()


def test_unknown_stream_token_is_404():
    s = _server()
    try:
        url, token = s.register_stream(lambda: None, "audio/aac", "x.aac")
        s.unregister_stream(token)
        r, _, _ = _get(url)
        assert r.status == 404
    finally:
        s.stop()


def test_unregister_ends_running_stream_and_closes_source():
    with FakeStation(endless_chunk=b"z" * 512, chunk_delay=0.005) as st:
        s = _server()
        try:
            url, token = s.register_stream(lambda: radio.open_stream(st.url), "audio/aac", "x.aac")
            host, rest = url[len("http://"):].split("/", 1)
            c = http.client.HTTPConnection(host, timeout=5)
            c.request("GET", "/" + rest)
            r = c.getresponse()
            assert len(r.read(2048)) == 2048
            s.unregister_stream(token)
            assert wait_for(lambda: st.open_streams == 0, 3)
        finally:
            s.stop()


def test_renderer_disconnect_closes_upstream():
    with FakeStation(endless_chunk=b"z" * 512, chunk_delay=0.005) as st:
        s = _server()
        try:
            url, token = s.register_stream(lambda: radio.open_stream(st.url), "audio/aac", "x.aac")
            r, data, c = _get(url, read=1024)
            r.close()
            c.close()
            assert wait_for(lambda: st.open_streams == 0, 3)
        finally:
            s.stop()


def test_no_icy_metadata_requested_through_proxy():
    with FakeStation(body=b"x" * 10) as st:
        s = _server()
        try:
            url, _ = s.register_stream(lambda: radio.open_stream(st.url), "audio/aac", "x.aac")
            _get(url)
            assert all(k.lower() != "icy-metadata" for k in st.requests[0]["headers"])
        finally:
            s.stop()


def test_didl_live_class_and_no_duration():
    d = upnp.build_didl("Sender", "http://h/s/t/x.aac", "audio/aac", live=True)
    assert "object.item.audioItem.audioBroadcast" in d and "musicTrack" not in d
    assert "duration=" not in d and "size=" not in d and "DLNA.ORG_OP=00" in d
    d2 = upnp.build_didl("T", "http://h/m/t/x.flac", "audio/flac", duration_s=3.0, size=5)
    assert "musicTrack" in d2 and "DLNA.ORG_OP=01" in d2


def test_wait_live_has_no_time_limit_but_non_live_has(rig, monkeypatch):
    """Mit gefaelschter Uhr (+2 h je Aufruf) laeuft ein Live-Stream weiter, eine Datei ohne Dauer nicht."""
    mock, server, player = rig(mock_kw={"speedup": 1.0})
    real = time.perf_counter
    offset = {"v": 0.0}

    def fake_clock():
        offset["v"] += 7200.0 if offset.get("on") else 0.0
        return real() + offset["v"]

    # Renderer spielt lange; erst danach die Uhr springen lassen.
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = make_tone(tmp / "lang.flac", seconds=30.0, fs=8000)
    url, token = server.register(f, "audio/flac", name="lang.flac")
    player.renderer.set_uri(url, "")
    player.renderer.play()
    assert wait_for(lambda: mock.state == "PLAYING", 5)
    monkeypatch.setattr(player_mod, "_clock", fake_clock)
    offset["on"] = True
    out = {}
    t = threading.Thread(target=lambda: out.update(r=player._wait(fake_clock(), None, live=True)), daemon=True)
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "Live-Wartezeit darf nicht an einer Zeitgrenze enden"
    player.stop()
    t.join(5)
    assert out["r"]["end_reason"] == "stopped"
    player._stop.clear()
    player.start_timeout_s = float("inf")  # Kontrolle: nur die Gesamtgrenze ohne Dauer (3600 s) soll greifen
    out2 = {}
    t2 = threading.Thread(target=lambda: out2.update(r=player._wait(fake_clock(), None)), daemon=True)
    t2.start()
    t2.join(5)
    assert out2["r"]["end_reason"] == "timeout"


def _flac_bytes(tmp_path, seconds=1.0):
    p = make_tone(tmp_path / "s.flac", seconds=seconds, fs=8000)
    return p.read_bytes()


def test_play_stream_end_to_end_simulated(rig, tmp_path):
    body = _flac_bytes(tmp_path)
    mock, server, player = rig()
    events = []
    player.on_event = events.append
    with FakeStation(body=body, content_type="audio/flac") as st:
        res = player.play_stream(lambda: radio.open_stream(st.url), "audio/flac", ".flac", "Test FM", radio.mask_url(st.url))
    assert res["end_reason"] == "ended" and res["live"] is True
    assert mock.fetched and mock.fetched[0]["data"] == body, "Durchleitung muss byteidentisch sein"
    assert mock.fetched[0]["content_type"] == "audio/flac"
    assert "DLNA.ORG_OP=00" in mock.metadata[0]["res"]["protocolInfo"]
    assert not player.track_active.is_set()
    assert server.streams == {}
    names = [e["event"] for e in events]
    assert names == ["stream_start", "stream_end"]
    dump = json.dumps(events)
    assert "GEHEIM123" not in dump and "/live/stream" not in dump  # nur maskierte Quelle im Protokoll
    log = [json.loads(x) for x in open(player.log.path, encoding="utf-8")]
    assert "GEHEIM123" not in json.dumps(log)


def test_play_stream_never_holds_renderer_lock_after_and_clears_active_on_error(rig):
    mock, server, player = rig(mock_kw={"reject_mimes": ("audio/aac",)})
    with FakeStation(body=b"x" * 100, content_type="audio/aac") as st:
        res = player.play_stream(lambda: radio.open_stream(st.url), "audio/aac", ".aac", "X", radio.mask_url(st.url))
    assert res["end_reason"] in ("error", "never_started")
    assert not player.track_active.is_set() and server.streams == {}
    assert upnp.RENDERER_LOCK.acquire(blocking=False)
    upnp.RENDERER_LOCK.release()


def test_play_stream_stop_while_endless_stream(rig):
    mock, server, player = rig(mock_kw={"speedup": 1.0})
    with FakeStation(endless_chunk=b"\0" * 1024, content_type="audio/aac", chunk_delay=0.005) as st:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=player.play_stream(
            lambda: radio.open_stream(st.url), "audio/aac", ".aac", "Endlos", "x")), daemon=True)
        t.start()
        assert wait_for(lambda: st.open_streams >= 1 or out, 5)
        player.stop()
        t.join(10)
        assert not t.is_alive()
        assert wait_for(lambda: st.open_streams == 0, 5)
    assert out["r"]["end_reason"] in ("stopped", "never_started")
    assert not player.track_active.is_set()


def _radio_args(tmp_path, mock, target, extra=()):
    return ["radio", target, "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl"), *extra]


def test_cli_radio_end_to_end(tmp_path, capsys):
    body = _flac_bytes(tmp_path)
    with FakeStation(body=body, content_type="audio/flac") as st, MockRenderer(speedup=5.0) as mock:
        rc = cli.main(_radio_args(tmp_path, mock, st.url))
    out = capsys.readouterr().out
    assert rc == 0 and "Radio:" in out and "Fertig: ended" in out
    assert "GEHEIM123" not in out and "/live/stream" not in out
    assert mock.fetched[0]["data"] == body


def test_cli_radio_favorite_by_name(tmp_path, capsys):
    body = _flac_bytes(tmp_path)
    with FakeStation(body=body, content_type="audio/flac") as st, MockRenderer(speedup=5.0) as mock:
        fav = tmp_path / "fav.json"
        fav.write_text(json.dumps([{"name": "Mein Sender", "url": st.url}]), encoding="utf-8")
        rc = cli.main(_radio_args(tmp_path, mock, "mein sender", ["--favorites", str(fav)]))
    assert rc == 0 and "Mein Sender" in capsys.readouterr().out


def test_cli_radio_mp3_rejected_before_any_renderer_contact(tmp_path, capsys):
    with FakeStation(body=b"x", content_type="audio/mpeg") as st, MockRenderer() as mock:
        rc = cli.main(_radio_args(tmp_path, mock, st.url))
        assert mock.actions == []
    err = capsys.readouterr().err
    assert rc == 2 and "stream_format_unsupported" in err and "MP3" in err and "GEHEIM123" not in err


@pytest.mark.parametrize("target", ["ftp://x.de/a", "http://u:p@x.de/a"])
def test_cli_radio_invalid_url_exit_2(tmp_path, capsys, target):
    with MockRenderer() as mock:
        rc = cli.main(_radio_args(tmp_path, mock, target))
        assert mock.actions == []
    assert rc == 2 and "stream_url_invalid" in capsys.readouterr().err


def test_cli_radio_unreachable_station_exit_2_masked(tmp_path, capsys):
    with MockRenderer() as mock:
        rc = cli.main(_radio_args(tmp_path, mock, "http://127.0.0.1:9/geheim?key=ABC"))
        assert mock.actions == []
    err = capsys.readouterr().err
    assert rc == 2 and "stream_unreachable" in err and "ABC" not in err and "geheim" not in err
