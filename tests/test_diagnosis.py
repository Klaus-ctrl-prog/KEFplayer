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


def test_track_problem_categories_are_diagnosed_independently():
    results = diagnose([
        finding("track_problem", "audio", error_category="source_file_access"),
        finding("track_problem", "renderer", error_category="renderer_timeout"),
    ])

    by_id = {item["diagnosis_id"]: item for item in results}
    source = by_id["audio:track_problem:source_file_access"]
    renderer = by_id["renderer:track_problem:renderer_timeout"]
    assert source["decision"] == renderer["decision"] == "shadow_propose"
    assert source["cause_hypotheses"][0]["cause"] == "source_file_unavailable_or_inaccessible"
    assert renderer["cause_hypotheses"][0]["cause"] == "renderer_or_delivery_path_playback_failure"
    assert source["proposed_action"] is renderer["proposed_action"] is None


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




def test_renderer_retry_diagnosis_distinguishes_recovery_from_unrecovered_retries():
    recovered = diagnose([
        finding("renderer_retries", retries=2, recovered=1)
    ])[0]
    unrecovered = diagnose([
        finding("renderer_retries", retries=2, recovered=0)
    ])[0]
    missing = diagnose([
        finding("renderer_retries", retries=2)
    ])[0]

    assert recovered["decision"] == "shadow_propose"
    assert recovered["cause_hypotheses"][0]["cause"] == "renderer_command_failure_with_recorded_recovery"
    assert unrecovered["decision"] == "shadow_propose"
    assert unrecovered["cause_hypotheses"][0]["cause"] == "renderer_command_retry_without_recorded_recovery"
    assert missing["decision"] == "suppress"
    assert missing["cause_hypotheses"] == []
    assert all(item["proposed_action"] is None for item in (recovered, unrecovered, missing))


def test_diagnosis_bounds_repeated_evidence_and_reports_omitted_count():
    findings = [
        finding("track_problem", "renderer", error_category="renderer_timeout", index=i)
        for i in range(40)
    ]

    result = diagnose(findings)[0]

    assert result["decision"] == "shadow_propose"
    assert result["cause_hypotheses"][0]["cause"] == "renderer_or_delivery_path_playback_failure"
    assert len(result["evidence"]) == 25
    assert result["evidence_omitted"] == 15


def test_track_problem_classifies_cache_io_without_proposing_a_repair():
    result = diagnose([
        finding("track_problem", "cache", error_category="cache_io")
    ])[0]

    assert result["decision"] == "shadow_propose"
    assert result["cause_hypotheses"][0]["cause"] == "cache_write_or_storage_problem"
    assert result["confidence"] == "medium"
    assert result["proposed_action"] is None

def test_mixed_retry_recovery_evidence_is_suppressed():
    result = diagnose([
        finding("renderer_retries", retries=3, recovered=1),
        finding("renderer_retries", retries=2, recovered=0),
    ])[0]

    assert result["decision"] == "suppress"
    assert result["cause_hypotheses"] == []
    assert result["suppression_reason"] == "no_supported_rule"

def test_boolean_retry_recovery_evidence_is_suppressed():
    result = diagnose([
        finding("renderer_retries", retries=2, recovered=False)
    ])[0]

    assert result["decision"] == "suppress"
    assert result["cause_hypotheses"] == []
    assert result["suppression_reason"] == "no_supported_rule"



def test_simultaneous_renderer_and_media_server_findings_stay_separate():
    result = diagnose([
        finding("health_renderer_http", "renderer"),
        finding("health_media_server", "media_server"),
    ])

    by_code = {item["symptom"]: item for item in result}
    assert set(by_code) == {"health_renderer_http", "health_media_server"}
    assert by_code["health_renderer_http"]["component"] == "renderer"
    assert by_code["health_media_server"]["component"] == "media_server"
    assert all(item["decision"] == "shadow_propose" for item in result)
    assert all(item["proposed_action"] is None for item in result)


def test_track_problem_unknown_category_is_separately_suppressed():
    results = diagnose([
        finding("track_problem", "audio", error_category="source_file_access"),
        finding("track_problem", "renderer"),
        finding("track_problem", "audio", error_category="future_unknown_category"),
    ])

    assert len(results) == 3
    supported = next(x for x in results if x["diagnosis_id"].endswith(":source_file_access"))
    unknown = [x for x in results if x["decision"] == "suppress"]
    assert supported["decision"] == "shadow_propose"
    assert len(unknown) == 2
    assert all(x["cause_hypotheses"] == [] for x in unknown)
    assert all(x["proposed_action"] is None for x in results)


def test_track_problem_mixed_categories_are_suppressed_independent_of_order():
    items = [
        finding("track_problem", "audio", error_category="source_format"),
        finding("track_problem", "renderer", error_category="renderer_timeout"),
    ]

    forward = diagnose(items)[0]
    reverse = diagnose(list(reversed(items)))[0]

    assert len(forward) == len(reverse) == 2
    assert {x["diagnosis_id"] for x in forward} == {x["diagnosis_id"] for x in reverse}
    assert all(x["decision"] == "shadow_propose" for x in forward + reverse)
    assert all(x["proposed_action"] is None for x in forward + reverse)


def test_same_supported_track_problem_category_remains_diagnosable_when_repeated():
    result = diagnose([
        finding("track_problem", "renderer", error_category="renderer_timeout", index=1),
        finding("track_problem", "renderer", error_category="renderer_timeout", index=2),
        finding("track_problem", "renderer", error_category="renderer_timeout", index=3),
    ])[0]

    assert result["decision"] == "shadow_propose"
    assert result["cause_hypotheses"][0]["cause"] == "renderer_or_delivery_path_playback_failure"
    assert len(result["evidence"]) == 3
    assert result["evidence_omitted"] == 0

