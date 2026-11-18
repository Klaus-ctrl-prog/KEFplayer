"""E1 Radio: URL-Pruefung, Maskierung, Codec-Richtlinie, Favoriten, Probe (nur lokaler Fake-Sender)."""
import json

import pytest

from kefplayer import radio
from radio_helpers import FakeStation


@pytest.mark.parametrize("url", [
    "", "   ", "ftp://x.de/a", "file:///etc/passwd", "javascript:alert(1)", "http://", "http:///x",
    "http://user:pw@x.de/a", "http://user@x.de/a", "http://x.de:99999/a", "http://x.de/a b", "http://x.de/a\\b",
    "http://x.de/a\nb", None, 5,
])
def test_invalid_urls_rejected(url):
    with pytest.raises(radio.RadioError) as e:
        radio.validate_url(url)
    assert e.value.category == "stream_url_invalid"


@pytest.mark.parametrize("url", ["http://x.de/a", "https://x.de:8443/live?x=1", "http://127.0.0.1:8000/s"])
def test_valid_urls_accepted(url):
    assert radio.validate_url(url) == url


def test_mask_url_hides_path_query_and_userinfo():
    m = radio.mask_url("https://user:pw@radio.example:8443/live/TOKEN123?key=SECRET#frag")
    assert m == "https://radio.example:8443/..."
    for s in ("TOKEN123", "SECRET", "pw", "user", "frag"):
        assert s not in m


def test_mask_url_never_raises():
    assert radio.mask_url("http://x.de:99999/a") == "(ungueltige URL)"
    assert isinstance(radio.mask_url(None), str)


@pytest.mark.parametrize("ct,ext", [("audio/aac", ".aac"), ("AUDIO/AACP; charset=x", ".aac"),
                                    ("audio/flac", ".flac"), ("audio/mp4", ".m4a")])
def test_allowed_codecs(ct, ext):
    assert radio.check_codec(ct)[1] == ext


@pytest.mark.parametrize("ct", ["audio/mpeg", "audio/MPEG; x=1", "audio/mp3", "audio/x-mpeg", "audio/mpeg3"])
def test_mp3_is_rejected_by_policy(ct):
    with pytest.raises(radio.RadioError) as e:
        radio.check_codec(ct)
    assert e.value.category == "stream_format_unsupported" and "MP3" in str(e.value)


@pytest.mark.parametrize("ct", [None, "", "text/html", "audio/x-unknown", "video/mp4",
                                "audio/ogg", "application/ogg", "audio/opus",  # E-6: nur AAC und FLAC
                                "application/vnd.apple.mpegurl", "audio/x-mpegurl"])
def test_unknown_or_missing_content_type_rejected(ct):
    with pytest.raises(radio.RadioError) as e:
        radio.check_codec(ct)
    assert e.value.category == "stream_format_unsupported"


def test_probe_ok_closes_connection_and_sends_no_icy_header():
    with FakeStation(content_type="audio/aac", endless_chunk=b"x" * 100) as st:
        info = radio.probe(st.url)
        assert info.mime == "audio/aac" and info.ext == ".aac"
        assert "Icy-Metadata" not in st.requests[0]["headers"] and "icy-metadata" not in {
            k.lower() for k in st.requests[0]["headers"]}
        assert st.requests[0]["headers"]["User-Agent"] == radio.UA


def test_probe_mp3_station_rejected():
    with FakeStation(content_type="audio/mpeg", body=b"x") as st:
        with pytest.raises(radio.RadioError) as e:
            radio.probe(st.url)
    assert e.value.category == "stream_format_unsupported"


def test_probe_http_error_and_masked_message():
    with FakeStation(status=404, content_type="text/plain") as st:
        with pytest.raises(radio.RadioError) as e:
            radio.probe(st.url)
    assert e.value.category == "stream_http_error"
    assert "GEHEIM123" not in str(e.value) and "/live/stream" not in str(e.value)


def test_probe_unreachable_masked():
    with pytest.raises(radio.RadioError) as e:
        radio.probe("http://127.0.0.1:9/geheim/pfad?key=ABC", timeout=1.0)
    assert e.value.category == "stream_unreachable" and "ABC" not in str(e.value) and "geheim" not in str(e.value)


def test_open_stream_bad_status_line_is_unreachable():
    """Shoutcast v1 antwortet mit 'ICY 200 OK': urllib scheitert, das ist ein klarer Fehler und kein Absturz."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(b"ICY 200 OK\r\nicy-name: x\r\n\r\n")
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    try:
        with pytest.raises(radio.RadioError) as e:
            radio.open_stream(f"http://127.0.0.1:{port}/x", timeout=2.0)
    finally:
        srv.close()
    assert e.value.category == "stream_unreachable"


def test_favorites_roundtrip_and_resolution(tmp_path):
    f = tmp_path / "fav.json"
    f.write_text(json.dumps([{"name": "Jazz", "url": "http://a.de/j"}, {"name": "Klassik", "url": "https://b.de/k"}]),
                 encoding="utf-8")
    assert [x["name"] for x in radio.load_favorites(f)] == ["Jazz", "Klassik"]
    assert radio.resolve_target("jazz", f) == ("Jazz", "http://a.de/j")
    assert radio.resolve_target("http://c.de/x", None)[1] == "http://c.de/x"


@pytest.mark.parametrize("content", [
    "kein json", "{}", "[1]", '[{"url": "http://a.de"}]', '[{"name": " ", "url": "http://a.de"}]',
    '[{"name": "A", "url": "ftp://a.de"}]', '[{"name": "A", "url": "http://a.de"}, {"name": "a", "url": "http://b.de"}]',
    '[{"name": "A", "url": "http://u:p@a.de"}]',
])
def test_invalid_favorites_rejected(tmp_path, content):
    f = tmp_path / "fav.json"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(radio.RadioError):
        radio.load_favorites(f)


def test_missing_favorites_file_and_unknown_name(tmp_path):
    with pytest.raises(radio.RadioError):
        radio.load_favorites(tmp_path / "nope.json")
    f = tmp_path / "fav.json"
    f.write_text("[]", encoding="utf-8")
    with pytest.raises(radio.RadioError):
        radio.resolve_target("Unbekannt", f)
    with pytest.raises(radio.RadioError):
        radio.resolve_target("Name", None)
