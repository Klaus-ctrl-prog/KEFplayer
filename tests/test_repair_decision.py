from __future__ import annotations

import copy
import pytest

from kefplayer.diagnosis import diagnose
from kefplayer.repair_decision import decide_repairs


def finding(code, category="renderer", scope="process", **evidence):
    return {"code": code, "category": category, "level": "warn", "scope": scope,
            "ts": "2026-11-17T10:00:00+00:00", "evidence": evidence}


def healthy():
    return {
        "enabled": True,
        "monitor": {"state": "running", "renderer_checks_paused": False},
        "components": {name: {"status": "ok"} for name in
                       ("renderer_http", "renderer_upnp", "media_server", "cache")},
    }


def test_supported_hypothesis_is_deferred_without_executing_repair():
    findings = [finding("health_renderer_http")]
    decision = decide_repairs(diagnose(findings), findings, healthy())[0]
    assert decision["status"] == "deferred"
    assert decision["reason"] == "no_repair_action_approved_in_d1"
    assert decision["scope"] == "process"
    assert decision["simulated"] is True and decision["repair_executed"] is False


def test_unknown_finding_is_suppressed():
    findings = [finding("future_unknown_code", "new_area")]
    decision = decide_repairs(diagnose(findings), findings, healthy())[0]
    assert decision["status"] == "suppressed"
    assert decision["reason"] == "no_supported_d0_hypothesis"


def test_queue_and_process_scope_are_recovered_per_d0_group():
    findings = [
        finding("track_problem", "audio", "queue", error_category="source_file_access"),
        finding("track_problem", "renderer", "process", error_category="renderer_timeout"),
    ]
    decisions = decide_repairs(diagnose(findings), findings, healthy())
    assert {x["diagnosis_id"]: x["scope"] for x in decisions} == {
        "audio:track_problem:source_file_access": "queue",
        "renderer:track_problem:renderer_timeout": "process",
    }


def test_mixed_scopes_are_ambiguous():
    findings = [finding("health_renderer_http", scope="queue"),
                finding("health_renderer_http", scope="process")]
    decision = decide_repairs(diagnose(findings), findings, healthy())[0]
    assert decision["status"] == "deferred"
    assert decision["reason"] == "finding_scope_ambiguous"
    assert decision["scope"] == "unknown"


@pytest.mark.parametrize("health,reason", [
    (None, "health_unavailable"),
    ({"enabled": False}, "health_unavailable"),
    ({"enabled": True}, "health_state_unknown"),
    ({"enabled": True, "monitor": {"state": "stalled"}, "components": {}},
     "health_monitor_not_running"),
    ({"enabled": True, "monitor": {"state": "running", "renderer_checks_paused": True},
      "components": {"renderer_http": {"status": "unknown"}}},
     "renderer_health_checks_paused"),
])
def test_uncertain_renderer_health_is_deferred(health, reason):
    findings = [finding("health_renderer_http")]
    decision = decide_repairs(diagnose(findings), findings, health)[0]
    assert decision["status"] == "deferred" and decision["reason"] == reason


def test_missing_health_component_is_not_assumed_healthy():
    health = healthy()
    del health["components"]["renderer_http"]
    findings = [finding("health_renderer_http")]
    decision = decide_repairs(diagnose(findings), findings, health)[0]
    assert decision["status"] == "deferred"
    assert decision["reason"] == "required_health_unknown"


def test_missing_finding_match_does_not_guess_scope():
    findings = [finding("health_renderer_http")]
    decision = decide_repairs(diagnose(findings), [], healthy())[0]
    assert decision["status"] == "deferred"
    assert decision["reason"] == "finding_scope_unresolved"
    assert decision["scope"] == "unknown"


def test_inputs_are_not_mutated():
    findings = [finding("health_renderer_http")]
    diagnoses = diagnose(findings)
    before = copy.deepcopy((findings, diagnoses))
    decide_repairs(diagnoses, findings, healthy())
    assert (findings, diagnoses) == before


def test_invalid_input_types_raise():
    with pytest.raises(TypeError, match="diagnoses"):
        decide_repairs({}, [], healthy())
    with pytest.raises(TypeError, match="findings"):
        decide_repairs([], {}, healthy())
