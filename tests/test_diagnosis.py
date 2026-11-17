from __future__ import annotations

import pytest

from kefplayer.diagnosis import diagnose


def finding(code, category="renderer", level="error", **evidence):
    return {
        "code": code,
        "category": category,
        "level": level,
        "ts": "2026-11-17T10:00:00+00:00",
        "evidence": evidence,
    }


def test_renderer_http_failure_is_low_confidence_and_never_executes_action():
    result = diagnose([finding("health_renderer_http")])[0]

    assert result["decision"] == "shadow_propose"
    assert result["confidence"] == "low"
    assert result["cause_hypotheses"][0]["cause"] == "renderer_or_network_path_unavailable"
    assert result["proposed_action"] is None
    assert result["verification"] == "not_applicable_d0_no_action"


def test_track_problem_uses_existing_error_classification():
    source = diagnose([finding("track_problem", "audio", error_category="source_file_access")])[0]
    renderer = diagnose([finding("track_problem", "renderer", error_category="renderer_timeout")])[0]

    assert source["cause_hypotheses"][0]["cause"] == "source_file_unavailable_or_inaccessible"
    assert source["confidence"] == "medium"
    assert renderer["cause_hypotheses"][0]["cause"] == "renderer_or_delivery_path_playback_failure"
    assert renderer["confidence"] == "low"


def test_ambiguous_track_problem_is_suppressed():
    result = diagnose([
        finding("track_problem", "audio", error_category="source_file_access"),
        finding("track_problem", "renderer", error_category="renderer_timeout"),
    ])[0]

    assert result["decision"] == "suppress"
    assert result["cause_hypotheses"] == []
    assert result["suppression_reason"] == "no_supported_rule"
    assert len(result["evidence"]) == 2


def test_unknown_finding_is_preserved_but_not_guessed():
    result = diagnose([finding("new_unclassified_code", "new_area")])[0]

    assert result["component"] == "process"
    assert result["decision"] == "suppress"
    assert result["cause_hypotheses"] == []


def test_off_mode_is_observational_even_for_known_findings():
    result = diagnose([finding("health_cache", "cache")], mode="off")[0]

    assert result["decision"] == "observe"
    assert result["proposed_action"] is None


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        diagnose([], mode="on")


def test_input_is_not_mutated():
    source = [finding("health_monitor_stalled", "diagnostics")]
    before = repr(source)

    diagnose(source)

    assert repr(source) == before
