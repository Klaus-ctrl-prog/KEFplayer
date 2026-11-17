"""Phase C2: Kategorie und Zeitstempel an Befunden und Ereignissen, Kategorie-Filter bei /logs."""

import json

import pytest

from kefplayer import api
from api_helpers import SERVED, FakeServer, ev, fake_renderer, feed


def body(collector, path, query=""):
    status, raw = api.respond(collector, path, query)
    assert status == 200, raw
    return json.loads(raw)


def attached():
    from kefplayer.dsp import Profile

    c = api.StatusCollector()
    c.attach(player=None, renderer=fake_renderer(), server=FakeServer(), profile=Profile())
    return c


def _run_with_problems():
    c = attached()
    feed(c, ev("queue_start", tracks=["a", "b", "c", "d"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("sink_unavailable", error="boom"),
         ev("track_start", index=0, path="a.flac", served="a.flac", processed=False, mime="audio/x-weird", mime_in_sink=False,
            served_format=dict(SERVED)),
         ev("track_end", index=0, path="a.flac", end_reason="ended", ended_early=True, last_position_s=3.0, requests=[]),
         ev("track_end", index=1, path="b.flac", end_reason="never_started", error="kein Start", requests=[],
            error_category="renderer_never_fetched"),
         ev("track_end", index=2, path="c.flac", end_reason="prepare_error", error="kaputt", error_category="source_format"),
         ev("track_end", index=3, path="d.flac", end_reason="error", error="weg", error_category="renderer_unreachable"),
         ev("renderer_retry", action="play", attempt=1, of=3, category="renderer_timeout", error="t", wait_s=0.5),
         ev("stop_failed", error="x"), ev("cleanup_failed", error="y", path="t"))
    return c


def test_every_finding_has_category_ts_and_valid_values():
    c = _run_with_problems()
    f = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert f
    for x in f:
        assert x["category"] in api.CATEGORIES
        assert x["ts"] and x["ts_source"] in ("event", "evaluation")


def test_track_findings_use_error_category_and_event_time():
    c = _run_with_problems()
    f = body(c, "/api/v1/diagnostics")["data"]["findings"]
    tp = {x["evidence"]["end_reason"]: x for x in f if x["code"] == "track_problem"}
    assert tp["never_started"]["category"] == "media_server"
    assert tp["prepare_error"]["category"] == "audio"
    assert tp["error"]["category"] == "renderer"
    assert all(x["ts_source"] == "event" for x in tp.values())


def test_counter_findings_are_evaluation_time_and_categorised():
    c = _run_with_problems()
    by = {x["code"]: x for x in body(c, "/api/v1/diagnostics")["data"]["findings"]}
    assert by["stop_failed"]["category"] == "renderer" and by["stop_failed"]["ts_source"] == "evaluation"
    assert by["cleanup_failed"]["category"] == "cache"
    assert by["renderer_retries"]["category"] == "renderer"
    assert by["sink_unavailable"]["category"] == "upnp"
    assert by["mime_not_in_sink"]["category"] == "upnp"


def test_summary_counts_findings_by_category():
    c = _run_with_problems()
    s = body(c, "/api/v1/diagnostics")["data"]["summary"]
    f = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert sum(s["findings_by_category"].values()) == len(f)


def test_events_carry_category():
    c = _run_with_problems()
    evs = body(c, "/api/v1/logs", "limit=300")["data"]["events"]
    cat = {(e["event"], e.get("end_reason")): e["category"] for e in evs}
    assert cat[("queue_start", None)] == "process"
    assert cat[("sink_unavailable", None)] == "upnp"
    assert cat[("track_end", "ended")] == "audio"
    assert cat[("track_end", "never_started")] == "media_server"
    assert cat[("track_end", "prepare_error")] == "audio"
    assert cat[("track_end", "error")] == "renderer"
    assert cat[("cleanup_failed", None)] == "cache"
    retry = [e for e in evs if e["event"] == "renderer_retry"][0]
    assert retry["category"] == "renderer" and retry["error_category"] == "renderer_timeout"


def test_logs_category_filter():
    c = _run_with_problems()
    d = body(c, "/api/v1/logs", "category=media_server&limit=300")["data"]
    assert d["category"] == "media_server"
    assert d["events"] and all(e["category"] == "media_server" for e in d["events"])
    allv = body(c, "/api/v1/logs", "limit=300")["data"]
    assert allv["category"] is None and len(allv["events"]) > len(d["events"])
    both = body(c, "/api/v1/logs", "category=renderer&level=warn&limit=300")["data"]["events"]
    assert both and all(e["category"] == "renderer" and e["level"] in ("warn", "error") for e in both)


def test_logs_unknown_category_is_rejected():
    c = _run_with_problems()
    with pytest.raises(api.QueryError, match="category"):
        api.respond(c, "/api/v1/logs", "category=bogus")


def test_unknown_event_gets_default_category():
    c = attached()
    feed(c, ev("something_new", x=1))
    e = body(c, "/api/v1/logs")["data"]["events"][0]
    assert e["category"] == "process"



def test_diagnostics_exposes_shadow_diagnoses_without_changing_findings():
    c = _run_with_problems()
    data = body(c, "/api/v1/diagnostics")["data"]

    assert data["diagnoses"]
    app = api.StatusApp(c)
    expected_findings = app.findings(c.snapshot(), app._live(True))
    stable = lambda items: [
        {key: value for key, value in item.items() if key not in ("ts", "ts_source")}
        for item in items
    ]
    assert stable(data["findings"]) == stable(expected_findings)
    track = [d for d in data["diagnoses"] if d["symptom"] == "track_problem"]
    assert len(track) == 1
    assert track[0]["decision"] == "suppress"
    assert track[0]["cause_hypotheses"] == []
    assert track[0]["suppression_reason"] == "no_supported_rule"
    assert track[0]["proposed_action"] is None
    assert all(d["proposed_action"] is None for d in data["diagnoses"])
