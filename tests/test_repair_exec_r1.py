import threading
import urllib.request

import pytest
from kefplayer import health
from kefplayer import repair_budget as B
from kefplayer import repair_exec as X
from kefplayer.mediaserver import MediaServer

from api_helpers import free_port


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeServer:
    def __init__(self, requested_port=8765, start_error=None, block_stop=None, port_after=None):
        self.requested_port = requested_port
        self.port = requested_port
        self.entries = {"tok": object()}
        self.start_error, self.block_stop, self.port_after = start_error, block_stop, port_after
        self.stops = self.starts = 0

    def stop(self):
        self.stops += 1
        if self.block_stop is not None:
            self.block_stop.wait(5)

    def start(self):
        self.starts += 1
        if self.start_error:
            raise self.start_error
        if self.port_after is not None:
            self.port = self.port_after


OK = lambda s: {"status": "ok"}
BAD = lambda s: {"status": "error"}
ERR2 = ["ok", "error", "error"]


def _env():
    c = Clock()
    events = []
    return c, B.RepairBudget(clock=c), events, lambda *a, **k: events.append((a[0], k))


def _run(server, statuses=ERR2, *, busy=False, verify=OK, budget=None, env=None, stop_timeout_s=0.3):
    c, b, events, emit = env or _env()
    res = X.restart_media_server(server, statuses, busy=lambda: busy, budget=budget or b, emit=emit,
                                 clock=c, sleep=c.sleep, verify=verify, stop_timeout_s=stop_timeout_s)
    return res, events, b


@pytest.mark.parametrize("statuses,expected", [
    (["error", "error"], True), (["ok", "error", "error"], True),
    (["error"], False), (["error", "ok"], False), ([], False), (None, False), (["ok", "ok"], False),
])
def test_trigger_needs_two_consecutive_errors(statuses, expected):
    assert X.should_restart_server(statuses)[0] is expected


def test_success_restarts_same_port_and_records():
    s = FakeServer()
    res, events, b = _run(s)
    assert res == "success" and s.stops == 1 and s.starts == 1
    assert events[-1][1]["result"] == "success"
    assert b.last_success.get(X.ACTION_R1) is not None


def test_not_triggered_touches_nothing():
    s = FakeServer()
    res, events, _ = _run(s, statuses=["ok", "ok"])
    assert res == "not_triggered" and s.stops == 0 and s.starts == 0 and events == []


def test_blocked_while_track_active():
    s = FakeServer()
    res, events, _ = _run(s, busy=True)
    assert res == "blocked" and s.stops == 0 and events[-1][1]["reason"] == "track_active"


def test_blocked_for_dynamic_port():
    s = FakeServer(requested_port=0)
    res, events, _ = _run(s)
    assert res == "blocked" and s.stops == 0 and events[-1][1]["reason"] == "dynamic_port"


def test_blocked_by_budget_cooldown():
    env = _env()
    env[1].record_attempt(X.ACTION_R1, succeeded=False)
    s = FakeServer()
    res, events, _ = _run(s, env=env)
    assert res == "blocked" and s.stops == 0 and events[-1][1]["reason"] == "cooldown"


def test_start_error_is_failure_and_counted_once():
    s = FakeServer(start_error=OSError("Address already in use"))
    res, events, b = _run(s)
    assert res == "failed" and s.starts == 1
    assert events[-1][1]["reason"] == "start_error"
    assert len(b.attempts[X.ACTION_R1]) == 1


def test_hanging_stop_times_out_and_does_not_start_second_server():
    gate = threading.Event()
    s = FakeServer(block_stop=gate)
    try:
        res, events, _ = _run(s, stop_timeout_s=0.2)
    finally:
        gate.set()
    assert res == "failed" and s.starts == 0
    assert events[-1][1]["reason"] == "stop_timeout"


def test_unhealthy_after_start_is_failure_and_server_is_stopped_again():
    s = FakeServer()
    res, events, _ = _run(s, verify=BAD)
    assert res == "failed" and s.stops == 2
    assert events[-1][1]["reason"] == "not_verified"


def test_changed_port_fails_verification():
    s = FakeServer(port_after=9999)
    res, events, _ = _run(s)
    assert res == "failed" and events[-1][1]["reason"] == "not_verified"


def test_real_server_url_stays_valid_after_restart(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    server = MediaServer(bind="127.0.0.1", port=free_port(), advertise_host="127.0.0.1")
    server.start()
    try:
        url, _ = server.register(f, "application/octet-stream")
        assert urllib.request.urlopen(url, timeout=3).read() == b"hello"
        server._httpd.shutdown()  # Serverthread beendet sich, Objekt bleibt: Health meldet 'error'
        assert health.check_media_server(server)["status"] == "error"
        c, b, events, emit = _env()
        res = X.restart_media_server(server, ["error", "error"], busy=lambda: False, budget=b, emit=emit,
                                     clock=c, sleep=c.sleep, verify=health.check_media_server)
        assert res == "success"
        assert urllib.request.urlopen(url, timeout=3).read() == b"hello"  # dieselbe URL, derselbe Port
        assert health.check_media_server(server)["status"] == "ok"
    finally:
        server.stop()
