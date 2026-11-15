"""HTTP-Schicht der Status-API: nur lokal, nur lesend, robust gegen Fehlbedienung."""

import json
import socket
import threading
import time

import pytest

from kefplayer import api
from api_helpers import (all_endpoint_paths, call, ev, fake_renderer, feed, free_port, get_json, one_track_run, wait_for)


@pytest.fixture
def srv():
    c = api.StatusCollector()
    c.attach(renderer=fake_renderer())
    s = api.StatusApiServer(c, port=0).start()
    yield s
    s.stop()


# ---- Bindung ------------------------------------------------------------------------------------------

def test_binds_to_loopback_only(srv):
    assert srv._httpd.socket.getsockname()[0] == "127.0.0.1"
    assert srv.url.startswith("http://127.0.0.1:") and srv.port > 0


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::1", "localhost", "192.168.1.5", ""])
def test_refuses_any_other_bind_address(host):
    with pytest.raises(ValueError):
        api.StatusApiServer(api.StatusCollector(), port=0, host=host)


@pytest.mark.parametrize("port", [-1, 65536, "8766", None])
def test_refuses_bad_port(port):
    with pytest.raises(ValueError):
        api.StatusApiServer(api.StatusCollector(), port=port)


def test_not_reachable_via_other_local_address(srv):
    # Auf einer anderen lokalen Adresse (z. B. 127.0.0.2) darf nichts lauschen.
    s = socket.socket()
    s.settimeout(1.0)
    try:
        assert s.connect_ex(("127.0.0.2", srv.port)) != 0
    finally:
        s.close()


# ---- Antworten und Kopfzeilen -------------------------------------------------------------------------

@pytest.mark.parametrize("path", all_endpoint_paths())
def test_endpoints_ok_json_and_safe_headers(srv, path):
    st, hd, data = call(srv.port, path)
    assert st == 200 and hd["content-type"] == "application/json; charset=utf-8"
    assert hd["cache-control"] == "no-store" and hd["x-content-type-options"] == "nosniff"
    assert hd["content-length"] == str(len(data)) and hd["connection"] == "close"
    assert hd["server"] == "kefplayer-status-api" and "python" not in hd["server"].lower()
    assert not any(k.startswith("access-control-") for k in hd)  # keine CORS-Freigabe
    json.loads(data)


def test_head_returns_headers_without_body(srv):
    st, hd, data = call(srv.port, "/api/v1/status", method="HEAD")
    st2, hd2, data2 = call(srv.port, "/api/v1/status")
    assert st == 200 and data == b"" and int(hd["content-length"]) > 0 and hd["content-type"] == hd2["content-type"]


def test_unknown_path_and_bad_query_are_json_errors(srv):
    st, hd, data = call(srv.port, "/api/v1/steuern")
    assert st == 404 and json.loads(data)["error"]["status"] == 404
    for q in ("?foo=1", "/logs?limit=0", "/logs?limit=abc", "/logs?level=x"):
        path = "/api/v1/status" + q if q.startswith("?") else "/api/v1" + q
        st, hd, data = call(srv.port, path)
        assert st == 400 and hd["content-type"].startswith("application/json") and "error" in json.loads(data)
    st, _, data = call(srv.port, "/api/v1/status?pretty=1")
    assert st == 200 and b"\n" in data


def test_http_response_is_ascii_even_with_umlaut_titles(srv):
    one_track_run(srv.collector, name="Süße Töne.flac")
    st, _, data = call(srv.port, "/api/v1/diagnostics")
    data.decode("ascii")
    assert json.loads(data)["data"]["tracks"][0]["name"] == "Süße Töne.flac"


# ---- Nur lesend ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "PROPFIND"])
def test_every_non_read_method_is_405_and_changes_nothing(srv, method):
    before = srv.collector.snapshot()["seq"]
    st, hd, data = call(srv.port, "/api/v1/status", method=method, body=b'{"cmd":"stop"}' if method in ("POST", "PUT", "PATCH") else None)
    assert st == 405 and hd["allow"] == "GET, HEAD" and json.loads(data)["error"]["status"] == 405
    assert srv.collector.snapshot()["seq"] == before
    assert "access-control-allow-methods" not in hd


def test_no_endpoint_offers_control_actions(srv):
    text = json.dumps([get_json(srv.port, p) for p in ("/", "/api/v1/status", "/api/v1/diagnostics")]).lower()
    for word in ("/stop", "/pause", "/skip", "/play", "/next", "/volume"):
        assert word not in text


# ---- Schutz gegen Browser-/Rebinding-Zugriffe ------------------------------------------------------------

def test_foreign_host_header_is_rejected(srv):
    p = srv.port
    for host in ("evil.example.com", f"evil.example.com:{p}", "127.0.0.1.evil.com", f"127.0.0.1:{p + 1}", "192.168.1.5",
                 f"[::1]:{p}", ""):
        st, _, data = call(p, "/api/v1/status", host=host)
        assert st == 403, host
        assert b"nicht erlaubt" in data
    for host in ("127.0.0.1", f"127.0.0.1:{p}", "localhost", f"localhost:{p}", f"LOCALHOST:{p}"):
        assert call(p, "/api/v1/status", host=host)[0] == 200, host
    assert srv.collector.api_requests["rejected_host"] >= 7


def test_request_without_host_header_is_rejected(srv):
    assert call(srv.port, "/api/v1/status", host="")[0] == 403


def test_cross_site_browser_requests_are_rejected(srv):
    p = srv.port
    assert call(p, "/api/v1/status", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(p, "/api/v1/status", headers={"Origin": "http://evil.example"})[0] == 403
    assert call(p, "/api/v1/status", headers={"Origin": "null"})[0] == 403
    assert call(p, "/api/v1/status", headers={"Origin": f"http://127.0.0.1:{p}", "Sec-Fetch-Site": "same-origin"})[0] == 200
    st, hd, _ = call(p, "/api/v1/status", headers={"Origin": f"http://localhost:{p}"})
    assert st == 200 and "access-control-allow-origin" not in hd


def test_absolute_uri_request_target_is_rejected(srv):
    assert call(srv.port, "http://evil.example/api/v1/status")[0] == 400


# ---- Fehlbedienung / Missbrauch ----------------------------------------------------------------------------

def _raw(port, payload, timeout=3.0, read=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(payload)
        if not read:
            return b""
        s.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            try:
                d = s.recv(65536)
            except OSError:
                break
            if not d:
                break
            out += d
        return out
    finally:
        s.close()


def test_garbage_and_oversize_requests_get_json_errors_and_server_survives(srv):
    for payload in (b"GARBAGE\r\n\r\n", b"\x00\x01\x02\xff\r\n\r\n", b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
                    b"GET / HTTP/1.1\r\n" + b"X-A: b\r\n" * 150 + b"\r\n", b"GET /api/v1/status HTTP/9.9\r\n\r\n"):
        out = _raw(srv.port, payload)
        assert b"Traceback" not in out and b"File \"" not in out
        if out:
            assert out.startswith(b"HTTP/1.") and b"text/html" not in out
    assert call(srv.port, "/api/v1/status")[0] == 200


def test_slow_clients_do_not_block_others(srv, monkeypatch):
    monkeypatch.setattr(api._Handler, "timeout", 0.5)
    stalled = [socket.create_connection(("127.0.0.1", srv.port)) for _ in range(5)]
    try:
        for s in stalled:
            s.sendall(b"GET /api/v1/status HTT")  # unvollstaendig
        t0 = time.monotonic()
        assert call(srv.port, "/api/v1/status")[0] == 200
        assert time.monotonic() - t0 < 1.0
        # Der Server kappt die haengenden Verbindungen selbst.
        for s in stalled:
            s.settimeout(3.0)
            data = s.recv(4096)  # wirft socket.timeout, wenn der Server die Verbindung NICHT selbst kappt
            assert data == b"" or data.startswith(b"HTTP/1.")
    finally:
        for s in stalled:
            s.close()


def test_connection_limit_answers_503_and_recovers(monkeypatch):
    monkeypatch.setattr(api, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(api._Handler, "timeout", 3.0)
    s = api.StatusApiServer(api.StatusCollector(), port=0).start()
    held = []
    try:
        for _ in range(2):
            c = socket.create_connection(("127.0.0.1", s.port))
            c.sendall(b"GET /api/v1/status HTT")
            held.append(c)
        time.sleep(0.2)
        out = _raw(s.port, b"GET /api/v1/status HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % s.port)
        assert out.startswith(b"HTTP/1.0 503")
        for c in held:
            c.close()
        assert wait_for(lambda: call(s.port, "/api/v1/status")[0] == 200, timeout=5)
    finally:
        for c in held:
            c.close()
        s.stop()


def test_concurrent_clients_and_concurrent_event_feed(srv):
    stop = threading.Event()
    errors = []

    def feeder():
        i = 0
        while not stop.is_set():
            feed(srv.collector, ev("track_end", index=i % 50, path=f"C:\\x\\{i}.flac", end_reason="ended", startup_delay_s=0.1,
                                   wall_s=1.0, last_position_s=1.0, ended_early=False, gap_before_s=0.1, requests=[]))
            i += 1
            time.sleep(0.001)

    def client(n):
        try:
            for k in range(12):
                for p in all_endpoint_paths():
                    st, _, data = call(srv.port, p)
                    assert st == 200
                    json.loads(data)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ft = threading.Thread(target=feeder)
    ft.start()
    ts = [threading.Thread(target=client, args=(n,)) for n in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    stop.set()
    ft.join(5)
    assert errors == [] and srv.collector.api_errors == 0


def test_internal_error_returns_generic_500_without_details(srv, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(r"geheim C:\Users\Klaus\x.flac 192.168.1.50")

    monkeypatch.setattr(api, "respond", boom)
    st, hd, data = call(srv.port, "/api/v1/status")
    assert st == 500 and b"geheim" not in data and b"Klaus" not in data and b"192.168" not in data
    assert srv.collector.api_errors == 1 and "RuntimeError" in srv.collector.api_last_error
    monkeypatch.undo()
    assert call(srv.port, "/api/v1/status")[0] == 200


def test_server_is_silent_on_stdout_and_stderr(srv, capfd):
    call(srv.port, "/api/v1/status")
    call(srv.port, "/api/v1/steuern")
    call(srv.port, "/api/v1/status", method="POST")
    call(srv.port, "/api/v1/status", host="evil.example")
    _raw(srv.port, b"GARBAGE\r\n\r\n")
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_client_disconnect_mid_request_is_harmless(srv):
    for _ in range(20):
        s = socket.create_connection(("127.0.0.1", srv.port))
        s.sendall(b"GET /api/v1/diagnostics HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % srv.port)
        s.close()  # sofort weg, bevor die Antwort gelesen wird
    assert call(srv.port, "/api/v1/status")[0] == 200 and srv.collector.api_errors == 0


# ---- Lebenszyklus ------------------------------------------------------------------------------------------

def test_stop_is_idempotent_closes_port_and_stops_thread():
    s = api.StatusApiServer(api.StatusCollector(), port=0).start()
    port, thread = s.port, s._thread
    assert call(port, "/api/v1/status")[0] == 200
    s.stop()
    s.stop()  # zweimal ist ok
    assert not thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_stop_does_not_hang_with_open_stalled_connection(monkeypatch):
    monkeypatch.setattr(api._Handler, "timeout", 5.0)
    s = api.StatusApiServer(api.StatusCollector(), port=0).start()
    c = socket.create_connection(("127.0.0.1", s.port))
    c.sendall(b"GET /api/v1/stat")
    time.sleep(0.1)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 2.5
    c.close()


def test_restart_on_same_port_works():
    s1 = api.StatusApiServer(api.StatusCollector(), port=0).start()
    port = s1.port
    call(port, "/api/v1/status")
    s1.stop()
    s2 = api.StatusApiServer(api.StatusCollector(), port=port).start()
    try:
        assert call(port, "/api/v1/status")[0] == 200
    finally:
        s2.stop()


def test_busy_port_raises_oserror_and_does_not_touch_the_occupant():
    occupant = socket.socket()
    occupant.bind(("127.0.0.1", 0))
    occupant.listen(1)
    port = occupant.getsockname()[1]
    try:
        with pytest.raises(OSError):
            api.StatusApiServer(api.StatusCollector(), port=port).start()
        assert occupant.fileno() != -1  # Belegender Socket unberuehrt
    finally:
        occupant.close()


def test_stop_without_start_and_context_manager():
    api.StatusApiServer(api.StatusCollector(), port=0).stop()
    with api.StatusApiServer(api.StatusCollector(), port=0) as s:
        assert call(s.port, "/")[0] == 200


def test_daemon_threads_never_block_interpreter_exit():
    # Ein Prozess, der den Server startet und ohne stop() endet, darf nicht haengen.
    import subprocess
    import sys

    code = ("import socket,sys;from kefplayer import api;"
            "s=api.StatusApiServer(api.StatusCollector(),port=0).start();"
            "c=socket.create_connection(('127.0.0.1',s.port));c.sendall(b'GET /api/v1/st');print('ok',flush=True)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout.strip() == "ok"


def test_allow_reuse_address_policy():
    import os

    assert api._Server.allow_reuse_address == (os.name != "nt")


def test_response_does_not_depend_on_dns(srv, monkeypatch):
    # HTTPServer.server_bind ruft normalerweise getfqdn() (unter Windows teils langsam). Hier darf nie DNS gefragt werden.
    def forbidden(*a, **k):
        raise AssertionError("DNS-Abfrage")

    monkeypatch.setattr(socket, "getfqdn", forbidden)
    monkeypatch.setattr(socket, "gethostbyaddr", forbidden)
    s = api.StatusApiServer(api.StatusCollector(), port=0).start()
    try:
        assert call(s.port, "/api/v1/status")[0] == 200
    finally:
        s.stop()
