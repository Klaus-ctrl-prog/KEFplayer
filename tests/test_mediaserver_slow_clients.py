"""MediaServer: unvollstaendige/langsame HTTP-Anfragen duerfen Handler-Threads nicht dauerhaft binden."""
import http.client
import socket
import time

import pytest

import kefplayer.mediaserver as M
from test_radio_supervisor import until


@pytest.fixture
def server():
    s = M.MediaServer(bind="127.0.0.1", port=0, advertise_host="127.0.0.1")
    s.start()
    yield s
    s.stop()


def _partial(port, n, raw=b"GET /x HTTP/1.1\r\nHost: a\r\nX-"):
    socks = []
    for _ in range(n):
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(raw)
        socks.append(c)
    return socks


def _status(port, path="/gibtsnicht"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_unvollstaendige_anfrage_wird_nach_frist_beendet(server, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 0.5)
    socks = _partial(server.port, 6)
    try:
        assert until(lambda: len(server._httpd._handlers) == 6, 3)
        assert until(lambda: len(server._httpd._handlers) == 0, 5), "Handler bleiben trotz Frist gebunden"
        assert _status(server.port) == 404
    finally:
        for c in socks:
            c.close()


def test_handlerzahl_ist_begrenzt_und_normale_anfrage_wird_kontrolliert_bedient_oder_abgewiesen(server, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 30.0)
    monkeypatch.setattr(M, "MAX_HANDLERS", 5)
    socks = _partial(server.port, 9)
    try:
        assert until(lambda: len(server._httpd._handlers) == 5, 3)
        time.sleep(0.3)
        assert len(server._httpd._handlers) == 5, "Limit wird ueberschritten"
        assert _status(server.port) == 503  # kontrolliert abgewiesen
    finally:
        for c in socks:
            c.close()
    assert until(lambda: len(server._httpd._handlers) == 0, 5), "Handler werden nach Abbruch der Clients nicht frei"
    assert _status(server.port) == 404  # danach wieder normal


def test_stop_funktioniert_bei_offenen_langsamen_verbindungen(monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 30.0)
    s = M.MediaServer(bind="127.0.0.1", port=0, advertise_host="127.0.0.1")
    s.start()
    socks = _partial(s.port, 4)
    try:
        assert until(lambda: len(s._httpd._handlers) == 4, 3)
        t = time.monotonic()
        s.stop()
        assert time.monotonic() - t < 5.0
    finally:
        for c in socks:
            c.close()


def test_laufender_stream_wird_von_der_anfragefrist_nicht_beruehrt(server, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 0.5)

    class Src:
        def read1(self, n=8192):
            time.sleep(0.2)
            return b"x" * 100

        def close(self):
            pass

    url, token = server.register_stream(lambda: Src(), "audio/wav", "t.wav")
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    c.request("GET", f"/s/{token}/t.wav")
    r = c.getresponse()
    got, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < 1.6:  # laenger als die Anfragefrist
        got += len(r.read(100))
    c.close()
    assert r.status == 200 and got >= 600


def test_tropf_client_wird_per_gesamtfrist_getrennt(server, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 2.0)  # je recv laenger als das Tropf-Intervall: nur die Gesamtfrist kann den Client trennen
    monkeypatch.setattr(M, "REQUEST_TOTAL_S", 1.0)
    c = socket.create_connection(("127.0.0.1", server.port))
    try:
        c.sendall(b"GET /x HTTP/1.1\r\nHost: a\r\nX-")
        closed = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4:
            try:
                c.sendall(b"a")
                c.settimeout(0.2)
                if c.recv(1) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        assert closed, "Tropf-Client wird nicht getrennt"
        assert until(lambda: len(server._httpd._handlers) == 0, 6)  # <= TOTAL + TIMEOUT_S (+ Reserve)
    finally:
        c.close()


def test_hohe_last_503_und_danach_alle_slots_frei(server, monkeypatch):
    monkeypatch.setattr(M, "REQUEST_TIMEOUT_S", 30.0)
    monkeypatch.setattr(M, "MAX_HANDLERS", 5)
    socks = _partial(server.port, 150)
    try:
        assert until(lambda: len(server._httpd._handlers) == 5, 3)
        assert len(server._httpd._handlers) <= 5
    finally:
        for c in socks:
            c.close()
    assert until(lambda: len(server._httpd._handlers) == 0, 5), "Zaehler bleibt nach Last belegt"
    assert [_status(server.port) for _ in range(10)] == [404] * 10
