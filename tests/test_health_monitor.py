"""Phase C3: HealthMonitor (Leerlaufueberwachung), Anbindung an Sammler/API/CLI. Simulierter Renderer."""

import json
import threading
import time

import pytest

from kefplayer import api, cli, health, upnp
from kefplayer.mediaserver import MediaServer
from kefplayer.player import Player
from kefplayer.runlog import RunLog
from api_helpers import wait_for
from mock_renderer import MockRenderer


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "MIN_INTERVAL_S", 0.05)  # nur fuer Tests; Standard 15 s
    mock = MockRenderer().start()
    server = MediaServer(bind="127.0.0.1", port=0)
    server.start()
    info = upnp.Renderer.from_location(mock.location).info
    events = []

    def emit(name, **kw):
        events.append({"event": name, **kw})

    yield mock, server, info, tmp_path, events, emit
    mock.stop()
    server.stop()


def _mon(env, **kw):
    mock, server, info, tmp, events, emit = env
    kw.setdefault("interval_s", 0.05)
    kw.setdefault("timeout_s", 1.0)
    return health.HealthMonitor(emit, info=info, server=server, cache_dir=tmp, **kw)


def _changes(events):
    return [e for e in events if e["event"] == "health_change"]


# ---- Zustandswechsel ----

def test_first_tick_reports_each_component_once_then_silence(env):
    m = _mon(env)
    m.tick()
    ch = _changes(env[4])
    assert sorted(c["component"] for c in ch) == ["cache", "media_server", "renderer_http", "renderer_upnp"]
    assert all(c["previous"] is None and c["status"] == "ok" for c in ch)
    m.tick()
    m.tick()
    assert len(_changes(env[4])) == 4  # unveraenderter Zustand: kein weiteres Ereignis


def test_renderer_loss_is_reported_with_previous_status(env):
    m = _mon(env)
    m.tick()
    env[4].clear()
    env[0].stop()
    m.tick()
    ch = {c["component"]: c for c in _changes(env[4])}
    assert ch["renderer_http"]["previous"] == "ok" and ch["renderer_http"]["status"] == "error"
    assert ch["renderer_http"]["category"] in ("renderer_unreachable", "renderer_timeout")
    assert ch["renderer_upnp"]["status"] == "unknown"
    assert "media_server" not in ch and "cache" not in ch
    assert m.snapshot()["overall"] == "error"


def test_local_failure_is_reported(env):
    m = _mon(env)
    m.tick()
    env[4].clear()
    env[1].stop()
    m.tick()
    ch = _changes(env[4])
    assert [c["component"] for c in ch] == ["media_server"] and ch[0]["status"] == "error"


# ---- keine Renderer-Abfragen waehrend der Wiedergabe ----

def test_busy_pauses_renderer_checks_but_not_local_checks(env, monkeypatch):
    calls = []
    monkeypatch.setattr(health, "check_renderer_http", lambda *a, **k: calls.append("http") or {})
    monkeypatch.setattr(health, "check_renderer_upnp", lambda *a, **k: calls.append("upnp") or {})
    m = _mon(env, busy=lambda: True)
    m.tick()
    assert calls == []
    assert env[0].actions == []
    snap = m.snapshot()
    assert snap["renderer_checks_paused"] is True
    assert sorted(snap["components"]) == ["cache", "media_server", "renderer_http", "renderer_upnp"]
    assert snap["components"]["renderer_http"]["status"] == "unknown"
    assert snap["components"]["renderer_upnp"]["status"] == "unknown"


def test_renderer_state_becomes_unknown_while_busy(env):
    busy = {"v": False}
    m = _mon(env, busy=lambda: busy["v"])
    m.tick()
    busy["v"] = True
    env[4].clear()
    n = len(env[0].actions)
    m.tick()
    assert len(env[0].actions) == n
    assert [e["component"] for e in env[4]] == ["renderer_http", "renderer_upnp"]
    assert m.snapshot()["components"]["renderer_http"]["status"] == "unknown"
    assert m.snapshot()["components"]["renderer_upnp"]["status"] == "unknown"


def test_no_monitor_requests_to_renderer_during_track(env, tmp_path, tone_factory, monkeypatch):
    mock, server, info, tmp, events, emit = env
    stamps = []
    real = health.check_renderer_http
    monkeypatch.setattr(health, "check_renderer_http", lambda *a, **k: (stamps.append(time.perf_counter()), real(*a, **k))[1])
    player = Player(upnp.Renderer.from_location(mock.location), server, log=RunLog(None), poll_s=0.02, end_tolerance_s=1.0)
    span = {}

    def hook(rec):
        if rec["event"] == "track_start":
            span["a"] = time.perf_counter()
        elif rec["event"] == "track_end":
            span["b"] = time.perf_counter()

    player.on_event = hook
    m = health.HealthMonitor(emit, info=info, server=server, cache_dir=tmp, busy=player.track_active.is_set,
                             interval_s=0.05, timeout_s=1.0)
    m.start()
    wait_for(lambda: m.snapshot()["ticks"] >= 2)
    mock.speedup = 1.0
    src = tone_factory("long", seconds=1.5)
    player.play_queue([src])
    m.stop()
    assert "a" in span and "b" in span
    during = [t for t in stamps if span["a"] + 0.15 < t < span["b"]]
    assert span["b"] - span["a"] >= 0.5  # der Titel lief lange genug, um viele Runden (Intervall 0,05 s) zu umfassen
    assert during == []
    assert len(stamps) >= 2  # vor dem Titel wurde geprueft
    n = len(stamps)
    m.tick()  # nach dem Titel ist die Renderer-Pruefung wieder aktiv
    assert len(stamps) == n + 1


def test_renderer_soap_calls_share_one_lock(env, monkeypatch):
    # Zwei voneinander unabhaengige Renderer-Objekte muessen dieselbe Sperre nutzen.
    entered = threading.Event()
    release = threading.Event()
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            calls.append(threading.current_thread().name)
            if threading.current_thread().name == "soap-first":
                entered.set()
                assert release.wait(2)
            return (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><CurrentTransportState>STOPPED</CurrentTransportState></u:GetTransportInfoResponse></s:Body></s:Envelope>')
    class Opener:
        def open(self, *args, **kwargs): return Response()
    monkeypatch.setattr(upnp, "_OPENER", Opener())
    r1 = upnp.Renderer(env[2])
    r2 = upnp.Renderer(env[2])
    first = threading.Thread(target=lambda: r1.transport_info(), name="soap-first")
    second = threading.Thread(target=lambda: r2.transport_info(), name="soap-second")
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.05)
    assert calls == ["soap-first"]
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert calls == ["soap-first", "soap-second"]


def test_health_skips_renderer_when_shared_lock_is_held(env):
    m = _mon(env)
    locked = threading.Event()
    release = threading.Event()

    def hold_lock():
        with upnp.RENDERER_LOCK:
            locked.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert locked.wait(2)
    try:
        m.tick()
        snap = m.snapshot()
        assert snap["renderer_checks_paused"] is True
        assert sorted(snap["components"]) == ["cache", "media_server", "renderer_http", "renderer_upnp"]
        assert snap["components"]["renderer_http"]["status"] == "unknown"
        assert snap["components"]["renderer_upnp"]["status"] == "unknown"
    finally:
        release.set()
        holder.join(2)
    assert not holder.is_alive()
    m.tick()
    assert "renderer_upnp" in m.snapshot()["components"]


def test_player_start_and_health_check_transition_are_serialized(env, tmp_path, tone_factory, monkeypatch):
    mock, server, info, tmp, events, emit = env
    entered = threading.Event()
    release = threading.Event()
    sink_entered = threading.Event()
    sink_release = threading.Event()
    real_http = health.check_renderer_http
    stamps = []

    def blocked_http(*args, **kwargs):
        stamps.append(time.perf_counter())
        entered.set()
        assert release.wait(2)
        return real_http(*args, **kwargs)

    player = Player(upnp.Renderer(info), server, log=RunLog(None), poll_s=0.02)
    player._load_sink = lambda: (sink_entered.set(), sink_release.wait(2))
    monitor = _mon(env, busy=player.track_active.is_set)
    monkeypatch.setattr(health, "check_renderer_http", blocked_http)
    monitor_thread = threading.Thread(target=monitor.tick)
    monitor_thread.start()
    assert entered.wait(2)

    source = tone_factory("serialized-start", seconds=0.5)
    result = []
    player_thread = threading.Thread(target=lambda: result.extend(player.play_queue([source])))
    player_thread.start()

    # Solange Health die Sperre haelt, darf der Player die Sitzung noch nicht
    # als aktiv markieren. Nach Freigabe muss Health fertig sein, bevor der
    # Player den aktiven Zustand setzt.
    assert not player.track_active.is_set()
    release.set()
    monitor_thread.join(2)
    assert not monitor_thread.is_alive()
    assert sink_entered.wait(2)
    assert player.track_active.is_set()

    before = len(stamps)
    monitor.tick()
    assert len(stamps) == before
    sink_release.set()
    player_thread.join(5)
    assert not player_thread.is_alive()
    assert len(result) == 1 and not player.track_active.is_set()

def test_player_waits_for_inflight_health_check_before_sink_protocol_info(env, tmp_path, tone_factory, monkeypatch):
    mock, server, info, tmp, events, emit = env
    entered = threading.Event()
    release = threading.Event()
    player_started = threading.Event()
    real_http = health.check_renderer_http

    def blocked_http(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_http(*args, **kwargs)

    monkeypatch.setattr(health, "check_renderer_http", blocked_http)
    monitor = _mon(env)
    monitor_thread = threading.Thread(target=monitor.tick)
    monitor_thread.start()

    player = Player(upnp.Renderer(info), server, log=RunLog(None), poll_s=0.02)
    source = tone_factory("startup-lock", seconds=0.5)
    result = []

    def play():
        player_started.set()
        result.extend(player.play_queue([source]))

    player_thread = threading.Thread(target=play)
    try:
        assert entered.wait(2)
        player_thread.start()
        assert player_started.wait(2)
        # Health haelt RENDERER_LOCK: Der Player darf weder die Sitzung
        # aktivieren noch GetProtocolInfo senden, bevor Health fertig ist.
        time.sleep(0.05)
        assert not player.track_active.is_set()
        assert mock.actions == []

        release.set()
        monitor_thread.join(2)
        assert not monitor_thread.is_alive()
        assert wait_for(lambda: player.track_active.is_set())
        assert wait_for(lambda: any(a[0] == "GetProtocolInfo" for a in mock.actions))
        player_thread.join(5)
        assert not player_thread.is_alive()
        assert len(result) == 1 and not player.track_active.is_set()
    finally:
        release.set()
        if player_thread.ident is not None:
            player_thread.join(5)
        monitor_thread.join(2)
def test_player_track_active_flag(rig, tone_factory):
    mock, server, player = rig()
    seen = []
    player.on_event = lambda rec: seen.append((rec["event"], player.track_active.is_set()))
    player.play_queue([tone_factory("f", seconds=0.5)])
    assert ("track_start", True) in seen
    assert not player.track_active.is_set()


# ---- Thread, Intervall, Robustheit ----

def test_interval_defaults_are_30_s_with_15_s_minimum():
    assert health.DEFAULT_INTERVAL_S == 30.0 and health.MIN_INTERVAL_S == 15.0
    assert health.HealthMonitor(lambda *a, **k: None).interval_s == 30.0
    assert health.HealthMonitor(lambda *a, **k: None, interval_s=5).interval_s == 15.0
    assert cli.build_parser().parse_args(["play", "x.flac"]).health_interval == health.DEFAULT_INTERVAL_S


def test_interval_has_minimum_by_default(tmp_path):
    importlib_reload_free = health.HealthMonitor(lambda *a, **k: None, interval_s=0.1)
    assert importlib_reload_free.interval_s == health.MIN_INTERVAL_S


def test_thread_runs_periodically_and_stops(env):
    m = _mon(env).start()
    assert wait_for(lambda: m.snapshot()["ticks"] >= 3)
    assert m.snapshot()["state"] == "running"
    m.stop()
    n = m.snapshot()["ticks"]
    time.sleep(0.25)
    snap = m.snapshot()
    assert snap["state"] == "stopped" and snap["ticks"] == n
    assert not [t for t in threading.enumerate() if t.name == "kefplayer-health"]


def test_round_error_is_counted_and_thread_survives(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(health, "check_cache", boom)
    m = _mon(env).start()
    assert wait_for(lambda: m.snapshot()["errors"] >= 2)
    snap = m.snapshot()
    assert snap["state"] == "running" and "RuntimeError" in snap["last_error"]
    m.stop()


def test_emit_failure_does_not_stop_monitor(env):
    def bad_emit(*a, **k):
        raise ValueError("empfaenger kaputt")

    m = health.HealthMonitor(bad_emit, info=env[2], server=env[1], cache_dir=env[3], interval_s=0.05, timeout_s=1.0)
    m.tick()
    snap = m.snapshot()
    assert snap["errors"] >= 1 and snap["ticks"] == 1 and "emit" in snap["last_error"]


def test_stalled_state_when_ticks_stop_arriving(env):
    now = {"t": 0.0}
    m = health.HealthMonitor(lambda *a, **k: None, info=env[2], server=env[1], cache_dir=env[3], interval_s=0.05,
                             timeout_s=1.0, clock=lambda: now["t"])
    m._thread = threading.current_thread()  # lebender Thread, der keine Runden mehr liefert
    m._started_mono = 0.0
    now["t"] = 1.0
    assert m.snapshot()["state"] == "running"
    now["t"] = 1000.0
    assert m.snapshot()["state"] == "stalled"


# ---- Sammler / API ----

def _collector(env, details=False, run=True):
    c = api.StatusCollector(details=details)
    m = _mon(env)
    if run:
        m.tick()
    c.set_health(m)
    return c, m


def body(c, path, query=""):
    status, raw = api.respond(c, path, query)
    assert status == 200
    return json.loads(raw)


def test_health_endpoint_listed_and_disabled_without_monitor():
    c = api.StatusCollector()
    assert "health" in api.ENDPOINTS
    d = body(c, "/api/v1/health")["data"]
    assert d["enabled"] is False
    assert "/api/v1/health" in body(c, "/")["data"]["endpoints"]


def test_health_endpoint_with_monitor_and_privacy(env):
    c, m = _collector(env)
    d = body(c, "/api/v1/health")["data"]
    assert d["enabled"] is True and d["overall"] == "ok"
    comps = d["components"]
    assert sorted(comps) == ["cache", "media_server", "renderer_http", "renderer_upnp"]
    assert comps["renderer_upnp"]["category"] == "upnp" and comps["renderer_upnp"]["evidence"]["transport_state"]
    assert "host" not in comps["renderer_http"]["evidence"] and "dir" not in comps["cache"]["evidence"]
    assert d["monitor"]["state"] in ("running", "not_started") and d["monitor"]["ticks"] == 1
    c2, _ = _collector(env, details=True)
    d2 = body(c2, "/api/v1/health")["data"]["components"]
    assert "host" in d2["renderer_http"]["evidence"] and "dir" in d2["cache"]["evidence"]


def test_findings_from_health_error_and_event_timestamp(env):
    c, m = _collector(env)
    env[0].stop()
    m.tick()
    f = [x for x in body(c, "/api/v1/diagnostics")["data"]["findings"] if x["code"].startswith("health_")]
    assert [x["code"] for x in f] == ["health_renderer_http"]
    assert f[0]["level"] == "error" and f[0]["category"] == "renderer" and f[0]["ts_source"] == "event"
    assert "health" in body(c, "/api/v1/diagnostics")["data"]


def test_stalled_monitor_is_a_finding_but_stopped_is_not(env):
    c, m = _collector(env)
    m._thread = threading.current_thread()
    m._started_mono = -1000.0
    m._last_tick_mono = -1000.0
    codes = {x["code"] for x in body(c, "/api/v1/diagnostics")["data"]["findings"]}
    assert "health_monitor_stalled" in codes
    m._stopped = True
    codes = {x["code"] for x in body(c, "/api/v1/diagnostics")["data"]["findings"]}
    assert "health_monitor_stalled" not in codes

def test_monitor_internal_errors_are_diagnostic_findings(env):
    c, m = _collector(env)
    m._errors = 2
    m._last_error = "emit: ValueError: Empfaenger ausgefallen"
    findings = body(c, "/api/v1/diagnostics")["data"]["findings"]
    f = next(x for x in findings if x["code"] == "health_monitor_errors")
    assert f["level"] == "warn"
    assert f["category"] == "diagnostics"
    assert f["evidence"]["count"] == 2
    assert "ValueError" in f["evidence"]["last_error"]
    assert f["evidence"]["monitor_state"] in ("running", "not_started")


def test_health_change_events_in_logs_with_category_level_and_scrubbed_text(env):
    c, m = _collector(env, run=False)
    m._emit = c.on_event_from = None  # Ereignisse gehen unten ueber den Sammler
    m._emit = lambda name, **kw: c.on_event({"ts": "t", "event": name, **kw})
    m.tick()
    env[0].stop()
    m.tick()
    evs = body(c, "/api/v1/logs", "limit=300&category=renderer")["data"]["events"]
    bad = [e for e in evs if e["event"] == "health_change" and e["status"] == "error"]
    assert bad and bad[0]["level"] == "error" and bad[0]["error"]
    ok = [e for e in body(c, "/api/v1/logs", "limit=300")["data"]["events"] if e["event"] == "health_change" and e["status"] == "ok"]
    assert ok and all(e["error"] is None for e in ok)
    assert "evidence" not in bad[0]  # Host/Pfade nie im Protokoll der API


# ---- CLI ----

def test_cli_parser_options():
    a = cli.build_parser().parse_args(["play", "x.flac", "--no-health", "--health-interval", "30"])
    assert a.no_health is True and a.health_interval == 30.0
    assert cli.build_parser().parse_args(["play", "x.flac"]).no_health is False


def _run_play(tone_factory, tmp_path, extra):
    a = tone_factory("a", seconds=1.0)
    log = tmp_path / "run.jsonl"
    with MockRenderer() as mock:
        rc = cli.main(["play", str(a), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1", "--port", "0",
                       "--poll", "0.02", "--log", str(log), "--no-api", "--cache-dir", str(tmp_path / "c"), *extra])
    assert rc == 0
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]


def test_cli_play_runs_monitor_by_default(tone_factory, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(health, "MIN_INTERVAL_S", 0.05)
    recs = _run_play(tone_factory, tmp_path, ["--health-interval", "0.05"])
    ch = [r for r in recs if r["event"] == "health_change"]
    assert {r["component"] for r in ch} >= {"renderer_http", "renderer_upnp", "media_server", "cache"}
    assert "Gesundheit:" not in capsys.readouterr().out  # nur Probleme werden auf der Konsole gemeldet


def test_cli_play_no_health_has_no_health_events(tone_factory, tmp_path):
    recs = _run_play(tone_factory, tmp_path, ["--no-health"])
    assert not [r for r in recs if r["event"] == "health_change"]
