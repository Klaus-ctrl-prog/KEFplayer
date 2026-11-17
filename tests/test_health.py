"""Phase C1: Gesundheitspruefungen (simulierter Renderer, keine echte KEF)."""

import collections
import json
import socket

import pytest

from kefplayer import cli, health, upnp
from kefplayer.mediaserver import MediaServer
from mock_renderer import MockRenderer

Usage = collections.namedtuple("Usage", "total used free")


@pytest.fixture
def env(tmp_path):
    mock = MockRenderer().start()
    server = MediaServer(bind="127.0.0.1", port=0)
    server.start()
    info = upnp.Renderer.from_location(mock.location).info
    yield mock, server, info, tmp_path
    mock.stop()
    server.stop()


def _by(checks):
    return {c["component"]: c for c in checks}


def test_all_ok(env):
    mock, server, info, tmp = env
    checks = health.run_checks(info, server, tmp)
    by = _by(checks)
    assert [c["status"] for c in checks] == ["ok"] * 4
    assert by["renderer_upnp"]["evidence"]["transport_state"]
    assert health.overall(checks) == "ok"
    for c in checks:  # Pflichtfelder laut Phase C
        assert c["ts"] and c["category"] and c["message"] and c["status"]


def test_renderer_down_http_error_and_upnp_skipped(env):
    mock, server, info, tmp = env
    mock.stop()
    by = _by(health.run_checks(info, server, tmp, timeout=1.0))
    assert by["renderer_http"]["status"] == "error"
    assert by["renderer_http"]["category"] in ("renderer_unreachable", "renderer_timeout")
    assert by["renderer_upnp"]["status"] == "unknown"
    assert health.overall(list(by.values())) == "error"


def test_upnp_fault_is_classified_with_b1_categories(env, monkeypatch):
    mock, server, info, tmp = env

    def boom(self):
        raise upnp.UpnpError(701, "x")

    monkeypatch.setattr(upnp.Renderer, "transport_info", boom)
    by = _by(health.run_checks(info, server, tmp))
    assert by["renderer_http"]["status"] == "ok"
    assert by["renderer_upnp"]["status"] == "error" and by["renderer_upnp"]["category"] == "renderer_soap_fault"


def test_no_renderer_is_unknown(tmp_path):
    by = _by(health.run_checks(None, None, tmp_path, port=0))
    assert by["renderer_http"]["status"] == "unknown" and by["renderer_upnp"]["status"] == "unknown"
    assert health.overall(list(by.values())) == "unknown"


def test_media_server_stopped(env):
    mock, server, info, tmp = env
    server.stop()
    assert health.check_media_server(server)["status"] == "error"


def test_media_server_port_free_and_busy():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        busy = health.check_media_server(None, "127.0.0.1", port)
        assert busy["status"] == "warn" and busy["category"] == "media_server"
    finally:
        s.close()
    assert health.check_media_server(None, "127.0.0.1", 0)["status"] == "ok"


def test_cache_not_writable(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise PermissionError("nein")

    monkeypatch.setattr(health.tempfile, "mkstemp", deny)
    c = health.check_cache(tmp_path)
    assert c["status"] == "error" and c["category"] == "cache_io"


@pytest.mark.parametrize("free_mb, expected", [(10, "error"), (100, "warn"), (5000, "ok")])
def test_cache_free_space_thresholds(tmp_path, monkeypatch, free_mb, expected):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(10**12, 0, int(free_mb * 1e6)))
    assert health.check_cache(tmp_path)["status"] == expected


def test_cache_probe_file_is_removed(tmp_path):
    health.check_cache(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_overall_picks_worst():
    mk = lambda s: {"status": s}  # noqa: E731
    assert health.overall([mk("ok"), mk("warn")]) == "warn"
    assert health.overall([mk("warn"), mk("error"), mk("ok")]) == "error"
    assert health.overall([mk("ok"), mk("unknown")]) == "unknown"
    assert health.overall([]) == "unknown"


def test_health_monitor_does_not_retain_stale_renderer_errors_while_paused(tmp_path, monkeypatch):
    events = []
    monitor = health.HealthMonitor(lambda event, **kw: events.append({"event": event, **kw}), cache_dir=tmp_path, busy=lambda: True)

    for component, category in (("renderer_http", "renderer"), ("renderer_upnp", "upnp")):
        monitor._update({
            "component": component,
            "status": "error",
            "category": category,
            "message": "alter Fehler",
            "ts": "2026-11-17T10:00:00+00:00",
            "evidence": {},
            "latency_ms": 1.0,
        })

    monkeypatch.setattr(
        health, "check_media_server",
        lambda server: health._result("media_server", "ok", "media_server", "ok"),
    )
    monkeypatch.setattr(
        health, "check_cache",
        lambda cache_dir: health._result("cache", "ok", "cache_io", "ok"),
    )

    monitor.tick()
    snapshot = monitor.snapshot()

    assert snapshot["renderer_checks_paused"] is True
    assert snapshot["components"]["renderer_http"]["status"] == "unknown"
    assert snapshot["components"]["renderer_upnp"]["status"] == "unknown"
    assert snapshot["overall"] == "unknown"
    assert sum(event["event"] == "health_change" for event in events) == 4


# ---- CLI ----


def test_cli_health_ok_text_and_json(env, capsys):
    mock, server, info, tmp = env
    assert cli.main(["health", "--renderer", mock.location, "--port", "0", "--bind", "127.0.0.1", "--cache-dir", str(tmp)]) == 0
    out = capsys.readouterr().out
    assert "Gesamt: ok" in out and "renderer_upnp" in out
    assert cli.main(["health", "--renderer", mock.location, "--port", "0", "--bind", "127.0.0.1", "--cache-dir", str(tmp), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"] == "ok" and len(data["checks"]) == 4


def test_cli_health_renderer_down_exit_1(env, capsys):
    mock, server, info, tmp = env
    loc = mock.location
    mock.stop()
    rc = cli.main(["health", "--renderer", loc, "--port", "0", "--bind", "127.0.0.1", "--cache-dir", str(tmp), "--timeout", "1"])
    assert rc == 1
    assert "Gesamt: error" in capsys.readouterr().out
