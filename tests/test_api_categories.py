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
        assert x["scope"] == "queue"
        assert x["ts"] and x["ts_source"] in ("event", "evaluation")


def test_process_findings_keep_process_scope_before_first_queue():
    c = attached()
    feed(c, ev("sink_unavailable", error="pre-queue failure"),
         ev("renderer_retry", action="play", attempt=1, of=2, category="renderer_timeout", error="t", wait_s=0.5))
    app = api.StatusApp(c)
    findings = app.findings(c.snapshot(), {
        "enabled": True, "ok": False, "error": "probe failed",
    })
    by_code = {item["code"]: item for item in findings}

    assert by_code["sink_unavailable"]["scope"] == "process"
    assert by_code["renderer_retries"]["scope"] == "process"
    assert by_code["live_probe_failed"]["scope"] == "process"

    summary = body(c, "/api/v1/diagnostics")["data"]["summary"]
    assert summary["findings_by_scope"]["process"] >= 2


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



def test_live_probe_error_is_scrubbed_in_findings():
    c = attached()
    app = api.StatusApp(c)
    live = {
        "enabled": True,
        "ok": False,
        "error": "Connection failed to 192.168.1.77 at C:\\\\Users\\\\User\\\\private.flac "
                 "https://media.local/audio.flac?token=secret",
    }

    finding = next(x for x in app.findings(c.snapshot(), live) if x["code"] == "live_probe_failed")

    assert "192.168.1.77" not in finding["evidence"]["error"]
    assert "C:\\\\Users" not in finding["evidence"]["error"]
    assert "token=secret" not in finding["evidence"]["error"]
    assert "<ip>" in finding["evidence"]["error"]
    assert "<url>" in finding["evidence"]["error"]


def test_many_track_errors_keep_findings_and_bound_repeated_diagnosis_evidence():
    c = attached()
    feed(c, ev("queue_start", tracks=[f"{i}.flac" for i in range(400)], profile={"name": "p"},
               profile_neutral=False, renderer="r", poll_s=0.5))
    for i in range(400):
        feed(c, ev("track_end", index=i, path=f"{i}.flac", end_reason="error", error="renderer timeout",
                   error_category="renderer_timeout", requests=[]))

    status, raw = api.respond(c, "/api/v1/diagnostics", "events=0")
    assert status == 200
    data = json.loads(raw)["data"]
    track_findings = [x for x in data["findings"] if x["code"] == "track_problem"]
    track_diagnoses = [x for x in data["diagnoses"] if x["symptom"] == "track_problem"]

    assert len(track_findings) == 400  # die Primaerbefunde bleiben vollstaendig erhalten
    assert len(track_diagnoses) == 1
    assert len(track_diagnoses[0]["evidence"]) == 25
    assert track_diagnoses[0]["evidence_omitted"] == 375
    assert len(raw) < api.MAX_RESPONSE_BYTES


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

    # Die API-Kette muss unabhängige Befundcodes getrennt an D0 weitergeben.
    diagnosis_by_symptom = {item["symptom"]: item for item in data["diagnoses"]}
    finding_codes = {item["code"] for item in data["findings"]}
    assert set(diagnosis_by_symptom) == finding_codes

    sink = diagnosis_by_symptom["sink_unavailable"]
    cleanup = diagnosis_by_symptom["cleanup_failed"]
    assert sink["component"] == "upnp"
    assert sink["decision"] == "shadow_propose"
    assert cleanup["component"] == "cache"
    assert cleanup["decision"] == "suppress"
    assert sink["symptom"] != cleanup["symptom"]

def test_operational_findings_do_not_leak_across_playback_queues():
    c = attached()
    feed(c,
         ev("queue_start", tracks=["a"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("renderer_retry", action="play", attempt=1, of=2, category="renderer_timeout", error="t", wait_s=0.5),
         ev("renderer_recovered", action="play", attempt=2, of=2, category="renderer_timeout", error="t"),
         ev("stop_failed", error="stop"),
         ev("cleanup_failed", error="cleanup", path="temp"))
    first = body(c, "/api/v1/diagnostics")["data"]
    first_by_code = {item["code"]: item for item in first["findings"]}
    assert first_by_code["renderer_retries"]["evidence"]["retries"] == 1
    assert first_by_code["renderer_retries"]["evidence"]["recovered"] == 1
    assert first_by_code["stop_failed"]["evidence"]["count"] == 1
    assert first_by_code["cleanup_failed"]["evidence"]["count"] == 1
    assert all(item["evidence"]["queue_started_at"] for item in first["findings"]
               if item["code"] in {"renderer_retries", "stop_failed", "cleanup_failed"})

    feed(c, ev("queue_end", played=1, reasons=[]),
         ev("queue_start", tracks=["b"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5))
    second = body(c, "/api/v1/diagnostics")["data"]
    second_codes = {item["code"] for item in second["findings"]}
    assert not ({"renderer_retries", "stop_failed", "cleanup_failed"} & second_codes)


def test_retry_recovery_counts_are_not_combined_across_playback_queues():
    c = attached()
    feed(c,
         ev("queue_start", tracks=["a"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("renderer_retry", action="play", attempt=1, of=2, category="renderer_timeout", error="t", wait_s=0.5),
         ev("renderer_recovered", action="play", attempt=2, of=2, category="renderer_timeout", error="t"),
         ev("queue_end", played=1, reasons=[]),
         ev("queue_start", tracks=["b"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("renderer_retry", action="play", attempt=1, of=2, category="renderer_timeout", error="t", wait_s=0.5))
    data = body(c, "/api/v1/diagnostics")["data"]
    retry = next(item for item in data["findings"] if item["code"] == "renderer_retries")
    assert retry["evidence"]["retries"] == 1
    assert retry["evidence"]["recovered"] == 0
    diagnosis = next(item for item in data["diagnoses"] if item["symptom"] == "renderer_retries")
    assert diagnosis["cause_hypotheses"][0]["cause"] == "renderer_command_retry_without_recorded_recovery"

def test_sink_failure_from_previous_queue_is_not_reused_in_new_queue():
    c = attached()
    feed(c,
         ev("queue_start", tracks=["a"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("sink_unavailable", error="old failure"))
    first = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert any(item["code"] == "sink_unavailable" for item in first)

    feed(c, ev("queue_end", played=0, reasons=[]),
         ev("queue_start", tracks=["b"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5))
    second = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert not any(item["code"] == "sink_unavailable" for item in second)

