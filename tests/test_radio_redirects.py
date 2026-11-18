import http.server
import threading
import urllib.request

import pytest

from kefplayer import radio
from kefplayer.radio import RadioError, _SafeRedirect


class _Srv:
    def __init__(self, location):
        outer = self
        outer.location = location
        outer.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", outer.location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/aac")
                    self.send_header("Content-Length", "3")
                    self.end_headers()
                    self.wfile.write(b"abc")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    made = []

    def make(loc):
        s = _Srv(loc)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_erlaubte_weiterleitung_http_nach_http(srv):
    s = srv("/ziel")
    r = radio.open_stream(f"http://127.0.0.1:{s.port}/start")
    assert r.read() == b"abc"
    r.close()
    assert s.hits == ["/start", "/ziel"]


@pytest.mark.parametrize("loc", ["ftp://127.0.0.1:1/x", "file:///etc/passwd", "http://user:pw@127.0.0.1:1/x", "gopher://h/x"])
def test_unzulaessige_ziele_werden_abgelehnt(srv, loc):
    s = srv(loc)
    with pytest.raises(RadioError) as e:
        radio.open_stream(f"http://127.0.0.1:{s.port}/start")
    # file:/gopher: blockt schon urllib selbst (stream_http_error); ftp:, Zugangsdaten und https->http blockt _SafeRedirect
    assert e.value.category in ("stream_url_invalid", "stream_http_error")
    assert s.hits == ["/start"]  # Ziel wurde nie angefragt
    assert "pw" not in str(e.value)


def test_https_nach_http_wird_abgelehnt():
    h = _SafeRedirect()
    req = urllib.request.Request("https://sender.invalid/start")
    with pytest.raises(RadioError) as e:
        h.redirect_request(req, None, 302, "Found", {}, "http://sender.invalid/ziel")
    assert "https auf http" in str(e.value)
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://sender.invalid/ziel")
    assert ok.full_url == "https://sender.invalid/ziel"


def test_weiterleitungsschleife_endet(srv):
    s = srv("/start")
    with pytest.raises(RadioError):
        radio.open_stream(f"http://127.0.0.1:{s.port}/start")
    assert len(s.hits) <= 7
