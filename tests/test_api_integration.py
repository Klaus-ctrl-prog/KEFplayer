"""Status-API zusammen mit echtem Player, Medienserver und simuliertem Renderer.

Belegt wird: Die API beobachtet nur, veraendert die Wiedergabe nicht, belastet den Renderer ohne --api-live nicht und
zeigt nichts, was nicht gemessen wurde. Das Verhalten einer echten KEF ist damit NICHT belegt."""

import json
import threading
import time

import pytest
import soundfile as sf

from kefplayer import api, upnp
from mock_renderer import MockRenderer
from api_helpers import all_endpoint_paths, call, ev, feed, get_json, wait_for


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]


def wire(player, server, collector):
    collector.attach(player=player, renderer=player.renderer, server=server, profile=player.profile)
    return collector


# ---- Der Sammler spiegelt, was der Player tut ----------------------------------------------------------------

def test_collector_mirrors_player_results_and_log_file(rig, tone_factory, tmp_path):
    files = [tone_factory(f"t{i}", seconds=1.0, fs=8000) for i in range(3)]
    c = api.StatusCollector()
    mock, server, player = rig(mock_kw={"speedup": 1.0, "start_delay": 0.05}, poll_s=0.1, on_event=c.on_event)
    wire(player, server, c)
    res = player.play_queue(files)
    log = read_log(tmp_path)
    d = api.respond(c, "/api/v1/diagnostics", "events=300")
    d = json.loads(d[1])["data"]
    assert [e["event"] for e in d["events"]] == [l["event"] for l in log]  # Ereignis fuer Ereignis dasselbe
    assert [t["end_reason"] for t in d["tracks"]] == [r["end_reason"] for r in res] == ["ended"] * 3
    assert [t["gap_before_s"] for t in d["tracks"]] == [r.get("gap_before_s") for r in res]
    assert [t["startup_delay_s"] for t in d["tracks"]] == [r["startup_delay_s"] for r in res]
    assert [t["name"] for t in d["tracks"]] == [f"t{i}.flac" for i in range(3)]
    assert d["summary"]["phase"] == "queue_ended" and d["summary"]["end_reasons"] == {"ended": 3}
    assert d["renderer"]["identity"]["model_name"] == "MockRenderer"
    assert d["renderer"]["capabilities"]["flac_in_sink"] is True
    assert d["tracks"][0]["requests"]["methods"] == {"HEAD": 1, "GET": 1} or d["tracks"][0]["requests"]["count"] >= 1
    assert d["findings"] == [] or all(f["level"] == "info" for f in d["findings"])
    assert str(tmp_path) not in json.dumps(d)  # keine privaten Verzeichnisse


def test_processed_playback_reports_dsp_and_source_vs_target(rig, tone_factory):
    from kefplayer.dsp import Filter, Profile

    src = tone_factory("hi", seconds=0.8, fs=48000, subtype="PCM_24")
    c = api.StatusCollector()
    prof = Profile(name="mess", filters=[Filter("peq", 2000, -1.0, 1.0)])
    mock, server, player = rig(prof, bits=16, on_event=c.on_event)
    wire(player, server, c)
    player.play_queue([src])
    dsp = json.loads(api.respond(c, "/api/v1/dsp")[1])["data"]
    assert dsp["profile"]["name"] == "mess" and dsp["profile"]["is_neutral"] is False
    assert dsp["applied_per_track"][0]["preamp_db"] <= 0 and dsp["output"]["bits"] == 16
    au = json.loads(api.respond(c, "/api/v1/audio")[1])["data"]["recent"][0]
    assert au["source_format"]["value"]["bits"] == 24 and au["target_format"]["bits"] == 16
    assert au["processing_path"]["mode"] == "dsp" and au["differs"]["bits"] is True and au["differs"]["samplerate"] is False
    # gemessene Wahrheit: Renderer hat wirklich 16 Bit bekommen
    assert sf.info(__import__("io").BytesIO(mock.fetched[0]["data"])).subtype == "PCM_16"


def test_collector_cost_per_event_is_small():
    c = api.StatusCollector()
    feed(c, ev("queue_start", tracks=["x"] * 1000, profile={"name": "p"}, profile_neutral=True, renderer="r", poll_s=0.5))
    recs = [ev("track_end", index=i, path=f"/m/{i}.flac", end_reason="ended", startup_delay_s=0.2, wall_s=1.0,
               last_position_s=1.0, ended_early=False, gap_before_s=0.1,
               requests=[{"client": "x", "method": "GET", "range": None, "status": 200, "bytes_sent": 5}] * 4) for i in range(1000)]
    t0 = time.perf_counter()
    for r in recs:
        c.on_event(r)
    per = (time.perf_counter() - t0) / len(recs)
    print(f"Sammler: {per * 1e6:.0f} us je Ereignis")
    assert per < 0.005, per


# ---- Die API veraendert die Wiedergabe nicht -------------------------------------------------------------------------------

def _run(rig, files, with_api, hammer=4):
    kw = {"mock_kw": {"speedup": 1.0, "start_delay": 0.05}, "poll_s": 0.1}
    c = srv = None
    if with_api:
        c = api.StatusCollector()
        kw["on_event"] = c.on_event
    mock, server, player = rig(**kw)
    stop = threading.Event()
    threads, errors = [], []
    if with_api:
        wire(player, server, c)
        srv = api.StatusApiServer(c, port=0).start()

        def worker():
            try:
                while not stop.is_set():
                    for p in all_endpoint_paths():
                        st, _, _ = call(srv.port, p)
                        assert st == 200
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(hammer)]
        for t in threads:
            t.start()
    try:
        res = player.play_queue(files)
    finally:
        stop.set()
        for t in threads:
            t.join(10)
        if srv:
            srv.stop()
    assert errors == []
    starts = [t for k, t in mock.timeline if k == "start"]
    ends = [t for k, t in mock.timeline if k == "end"]
    return mock, res, [starts[i + 1] - ends[i] for i in range(len(ends) - 1)]


def test_api_traffic_does_not_change_playback(rig, tone_factory):
    files = [tone_factory(f"p{i}", seconds=1.5, fs=8000) for i in range(3)]
    base_mock, base_res, base_gaps = _run(rig, files, with_api=False)
    api_mock, api_res, api_gaps = _run(rig, files, with_api=True)

    def non_poll(m):
        return [a for a, _ in m.actions if not a.startswith("Get") or a == "GetProtocolInfo"]

    assert non_poll(api_mock) == non_poll(base_mock) == ["GetProtocolInfo"] + ["SetAVTransportURI", "Play"] * 3
    assert {a for a, _ in api_mock.actions} <= {a for a, _ in base_mock.actions}  # nichts Neues vom Renderer verlangt
    assert [r["end_reason"] for r in api_res] == [r["end_reason"] for r in base_res] == ["ended"] * 3
    assert [f["data"] for f in api_mock.fetched] == [f["data"] for f in base_mock.fetched] == [f.read_bytes() for f in files]
    assert max(api_gaps) < 0.3 and max(base_gaps) < 0.3, (api_gaps, base_gaps)  # Luecke nicht durch die API vergroessert
    assert not any(r.get("ended_early") for r in api_res)


def test_without_live_the_api_sends_nothing_to_the_renderer(rig, tone_factory):
    c = api.StatusCollector()
    mock, server, player = rig(on_event=c.on_event)
    wire(player, server, c)
    player.play_queue([tone_factory("a", seconds=0.6, fs=8000)])
    n = len(mock.actions)
    with api.StatusApiServer(c, port=0) as srv:
        for _ in range(30):
            for p in all_endpoint_paths():
                assert call(srv.port, p)[0] == 200
        time.sleep(0.3)
    assert len(mock.actions) == n


def test_hook_failure_does_not_disturb_playback(rig, tone_factory, monkeypatch):
    seen = []
    c = api.StatusCollector()
    monkeypatch.setattr(api.StatusCollector, "_make_entry", staticmethod(lambda *a, **k: 1 / 0))
    mock, server, player = rig(on_event=api.compose_hooks(c.on_event, seen.append))
    wire(player, server, c)
    res = player.play_queue([tone_factory("a", seconds=0.6, fs=8000)])
    assert res[0]["end_reason"] == "ended" and c.snapshot()["counters"]["hook_errors"] > 0
    assert len(seen) >= 5  # der zweite Rueckruf (Konsolenausgabe) laeuft trotzdem weiter


# ---- Live-Abfrage ---------------------------------------------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_live_probe_sends_only_get_actions_and_is_throttled(rig, tone_factory):
    mock, server, player = rig()
    clock = FakeClock()
    probe = api.LiveProbe(player.renderer.info, interval_s=1.0, timeout_s=2.0, clock=clock)
    n0 = len(mock.actions)
    r = probe.get()
    assert r["ok"] is True and r["transport_state"] == "NO_MEDIA_PRESENT" and r["position_s"] == 0.0
    assert [a for a, _ in mock.actions[n0:]] == ["GetTransportInfo", "GetPositionInfo"]
    for _ in range(10):
        probe.get()
    assert len(mock.actions) == n0 + 2  # gedrosselt: keine weitere Abfrage innerhalb des Intervalls
    assert probe.peek()["ok"] is True and len(mock.actions) == n0 + 2
    clock.t += 1.01
    probe.get()
    assert len(mock.actions) == n0 + 4
    assert set(probe.calls) <= set(api.READ_ONLY_ACTIONS)


def test_live_probe_interval_has_a_floor():
    info = fake_info()
    assert api.LiveProbe(info, interval_s=0.01).interval_s == api.MIN_LIVE_INTERVAL_S


def fake_info():
    from api_helpers import fake_renderer

    return fake_renderer().info


def test_live_probe_refuses_anything_but_get(rig):
    mock, server, player = rig()
    probe = api.LiveProbe(player.renderer.info)
    for bad in ("Play", "Pause", "Stop", "SetAVTransportURI", "Seek", "SetVolume"):
        with pytest.raises(RuntimeError):
            probe._soap_get(bad)
    assert mock.actions == []


def test_api_requests_trigger_at_most_one_refresh_per_interval(rig):
    mock, server, player = rig()
    clock = FakeClock()
    c = api.StatusCollector()
    wire(player, server, c)
    c.set_live(api.LiveProbe(player.renderer.info, interval_s=1.0, clock=clock))
    n0 = len(mock.actions)
    with api.StatusApiServer(c, port=0) as srv:
        for _ in range(10):
            assert call(srv.port, "/api/v1/status")[0] == 200
            assert call(srv.port, "/api/v1/renderer")[0] == 200
            assert call(srv.port, "/api/v1/diagnostics")[0] == 200
        assert len(mock.actions) == n0 + 2
        for p in ("/api/v1/dsp", "/api/v1/audio", "/api/v1/logs"):  # diese Endpunkte fragen den Renderer nie
            clock.t += 5
            call(srv.port, p)
        assert len(mock.actions) == n0 + 2
        d = get_json(srv.port, "/api/v1/renderer")
        assert d["data"]["live"]["enabled"] is True and d["data"]["live"]["actions"] == ["GetTransportInfo", "GetPositionInfo"]
        assert d["data"]["connection"]["reachable"] is True and d["data"]["connection"]["source"] == "live"


def test_live_position_during_real_playback(rig, tone_factory):
    c = api.StatusCollector()
    mock, server, player = rig(mock_kw={"speedup": 1.0, "start_delay": 0.05}, poll_s=0.1, on_event=c.on_event)
    wire(player, server, c)
    c.set_live(api.LiveProbe(player.renderer.info, interval_s=0.5, timeout_s=1.0))
    f = tone_factory("lang", seconds=3.0, fs=8000)
    result = {}
    with api.StatusApiServer(c, port=0) as srv:
        t = threading.Thread(target=lambda: result.update(res=player.play_queue([f])))
        t.start()

        def playing():
            try:
                d = get_json(srv.port, "/api/v1/status")["data"]
            except OSError:
                return None
            return d if d["track"] and d["track"]["position_s"] is not None and d["track"]["transport_state"] == "PLAYING" else None

        d = wait_for(playing, timeout=12)
        t.join(30)
    assert d, "keine Live-Position gesehen"
    tr = d["track"]
    assert tr["name"] == "lang.flac" and tr["paused"] is False and tr["duration_s"] == 3.0
    assert 0.0 <= tr["position_s"] <= 3.5 and d["live"]["available"] is True and isinstance(d["live"]["latency_ms"], float)
    assert result["res"][0]["end_reason"] == "ended" and not result["res"][0]["ended_early"]


def test_live_pause_is_visible_only_via_live(rig, tone_factory):
    c = api.StatusCollector()
    mock, server, player = rig(mock_kw={"speedup": 1.0, "start_delay": 0.05}, poll_s=0.1, on_event=c.on_event)
    wire(player, server, c)
    f = tone_factory("p", seconds=3.0, fs=8000)
    t = threading.Thread(target=lambda: player.play_queue([f]))
    t.start()
    try:
        assert wait_for(lambda: mock.state == "PLAYING", 8)
        player.toggle_pause()
        assert wait_for(lambda: mock.state == "PAUSED_PLAYBACK", 8)
        st = json.loads(api.respond(c, "/api/v1/status")[1])
        assert st["data"]["phase"] == "track_active" and st["data"]["track"]["transport_state"] is None
        assert "track.paused" in st["unknown"] and "Pause" in st["unknown"]["track.paused"]  # ohne Live: ehrlich unbekannt
        c.set_live(api.LiveProbe(player.renderer.info, interval_s=0.5, timeout_s=1.0))
        st = json.loads(api.respond(c, "/api/v1/status")[1])
        assert st["data"]["track"]["transport_state"] == "PAUSED_PLAYBACK" and st["data"]["track"]["paused"] is True
    finally:
        player.stop()
        t.join(20)


def test_unreachable_renderer_is_unknown_not_an_error_and_not_a_hang(rig):
    mock, server, player = rig()
    c = api.StatusCollector()
    wire(player, server, c)
    feed(c, ev("queue_start", tracks=["a.flac"], profile={"name": "neutral"}, profile_neutral=True, renderer="r", poll_s=0.5),
         ev("track_start", index=0, path="a.flac", served="a.flac", processed=False, mime="audio/flac", mime_in_sink=True,
            served_format={"duration_s": 10.0}))
    c.set_live(api.LiveProbe(player.renderer.info, interval_s=0.5, timeout_s=0.5))
    mock.stop()
    t0 = time.monotonic()
    status, raw = api.respond(c, "/api/v1/status")
    assert status == 200 and time.monotonic() - t0 < 3.0
    d = json.loads(raw)
    assert d["data"]["live"]["available"] is False and d["data"]["live"]["error"]
    assert d["data"]["track"]["position_s"] is None and "track.position_s" in d["unknown"]
    text = raw.decode()
    assert "127.0.0.1" not in text  # Adresse aus der Fehlermeldung entfernt
    rn = json.loads(api.respond(c, "/api/v1/renderer")[1])
    assert rn["data"]["connection"]["reachable"] is False
    f = json.loads(api.respond(c, "/api/v1/diagnostics")[1])["data"]["findings"]
    assert any(x["code"] == "live_probe_failed" for x in f)


def test_stale_live_result_is_not_presented_as_current(rig):
    """Beim Export am Ende (ohne neue Abfrage) darf ein altes Live-Ergebnis nicht als aktuell gelten."""
    mock, server, player = rig()
    clock = FakeClock()
    c = api.StatusCollector()
    wire(player, server, c)
    probe = api.LiveProbe(player.renderer.info, interval_s=1.0, clock=clock)
    c.set_live(probe)
    feed(c, ev("queue_start", tracks=["a.flac"], profile={"name": "neutral"}, profile_neutral=True, renderer="r", poll_s=0.5),
         ev("track_start", index=0, path="a.flac", served="a.flac", processed=False, mime="audio/flac", mime_in_sink=True,
            served_format={"duration_s": 10.0}))
    fresh = json.loads(api.respond(c, "/api/v1/renderer")[1])
    assert fresh["data"]["connection"]["reachable"] is True and fresh["data"]["live"]["stale"] is False
    clock.t += 10.0  # 10 s spaeter, ohne neue Abfrage (final=True wie beim --diag-file)
    status, raw = api.respond(c, "/api/v1/renderer", final=True)
    d = json.loads(raw)
    assert d["data"]["live"]["stale"] is True and d["data"]["live"]["available"] is False
    assert d["data"]["connection"]["reachable"] is None and "zu alt" in d["data"]["live"]["error"]
    assert "connection.reachable" in d["unknown"]
    st = json.loads(api.respond(c, "/api/v1/status", final=True)[1])
    assert st["data"]["track"]["position_s"] is None and st["data"]["track"]["transport_state"] is None
    assert "track.position_s" in st["unknown"]
    n = len(mock.actions)
    api.respond(c, "/api/v1/diagnostics", final=True)
    assert len(mock.actions) == n  # Export am Ende fragt den Renderer nicht mehr
