"""Deterministic, side-effect-free D0 diagnosis over existing Phase B/C findings.

This module evaluates evidence already collected by the player and health monitor.
It performs no I/O, does not infer hardware state, and never executes repairs.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _hypothesis(cause: str, confidence: str, rationale: str) -> dict[str, str]:
    return {"cause": cause, "confidence": confidence, "rationale": rationale}


def diagnose(findings: list[dict[str, Any]], *, mode: str = "shadow") -> list[dict[str, Any]]:
    """Convert existing findings into conservative, correlated D0 diagnosis records.

    mode is intentionally limited to off and shadow. Both modes are observational:
    no action is ever executed. off emits observe records; shadow emits
    shadow_propose only when a known evidence rule matches.
    """
    if mode not in {"off", "shadow"}:
        raise ValueError("mode must be 'off' or 'shadow'")
    if not isinstance(findings, list):
        raise TypeError("findings must be a list")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = finding.get("code")
        if not isinstance(code, str) or not code:
            continue
        grouped[code].append(finding)

    category_components = {
        "renderer": "renderer",
        "upnp": "upnp",
        "media_server": "media_server",
        "audio": "audio",
        "dsp": "dsp",
        "cache": "cache",
        "diagnostics": "diagnostics",
    }
    records: list[dict[str, Any]] = []
    for code, items in sorted(grouped.items()):
        components = {category_components.get(item.get("category"), "process") for item in items}
        component = next(iter(components)) if len(components) == 1 else "playback"
        evidence = []
        for item in items:
            evidence.append({
                "code": code,
                "level": item.get("level"),
                "category": item.get("category"),
                "ts": item.get("ts"),
                "evidence": dict(item.get("evidence") or {}) if isinstance(item.get("evidence"), dict) else {},
            })

        hypotheses: list[dict[str, str]] = []
        suppression_reason = "no_supported_rule"

        if code == "health_renderer_http":
            hypotheses = [_hypothesis(
                "renderer_or_network_path_unavailable", "low",
                "HTTP health check failed; this does not distinguish speaker, network, or host-side causes.",
            )]
        elif code == "health_renderer_upnp":
            hypotheses = [_hypothesis(
                "renderer_upnp_control_path_unavailable", "low",
                "The UPnP transport query failed; HTTP reachability alone cannot establish device health.",
            )]
        elif code == "health_media_server":
            hypotheses = [_hypothesis(
                "local_media_server_unavailable_or_port_conflict", "low",
                "The media-server check failed; the finding does not identify the conflicting process or cause.",
            )]
        elif code == "health_cache":
            hypotheses = [_hypothesis(
                "cache_write_or_storage_capacity_problem", "low",
                "The cache check failed; inspect the recorded I/O error and free-space evidence.",
            )]
        elif code == "health_monitor_stalled":
            hypotheses = [_hypothesis(
                "health_monitor_not_progressing", "medium",
                "The monitor snapshot reports stale ticks; this is a monitor symptom, not a renderer diagnosis.",
            )]
        elif code == "health_monitor_errors":
            hypotheses = [_hypothesis(
                "health_monitor_internal_error", "medium",
                "The health monitor recorded internal errors; its last error is supporting evidence.",
            )]
        elif code == "renderer_retries":
            retry_evidence = [x.get("evidence", {}) for x in evidence]
            recovered = [x.get("recovered") for x in retry_evidence]
            if recovered and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in recovered):
                hypotheses = [_hypothesis(
                    "renderer_command_failure_with_recorded_recovery", "low",
                    "At least one retry sequence has a recorded recovery; this supports a transient failure but does not identify its cause.",
                )]
            elif recovered and all(n == 0 for n in recovered):
                hypotheses = [_hypothesis(
                    "renderer_command_retry_without_recorded_recovery", "low",
                    "Retries were recorded without a successful recovery; this does not prove the renderer is offline or identify the cause.",
                )]
        elif code == "stop_failed":
            hypotheses = [_hypothesis(
                "renderer_stop_command_failed", "low",
                "The stop operation failed; the finding does not prove the renderer is offline.",
            )]
        elif code == "sink_unavailable":
            hypotheses = [_hypothesis(
                "renderer_sink_capability_query_failed", "low",
                "Sink-format information could not be read; playback and renderer reachability are separate signals.",
            )]
        elif code == "track_problem":
            categories = {x.get("evidence", {}).get("error_category") for x in evidence}
            if categories and categories <= {"source_file_access", "file_io"}:
                hypotheses = [_hypothesis(
                    "source_file_unavailable_or_inaccessible", "medium",
                    "The playback finding classifies the failure as source-file or file I/O related.",
                )]
            elif categories == {"source_format"}:
                hypotheses = [_hypothesis(
                    "source_format_not_supported_or_not_decodable", "medium",
                    "The playback finding classifies the failure as a source-format problem.",
                )]
            elif categories == {"processing_error"}:
                hypotheses = [_hypothesis(
                    "audio_processing_failure", "medium",
                    "The playback finding classifies the failure in the audio-processing path.",
                )]
            elif categories == {"cache_io"}:
                hypotheses = [_hypothesis(
                    "cache_write_or_storage_problem", "medium",
                    "The playback finding classifies the failure as cache I/O related.",
                )]
            elif categories and categories <= {
                "renderer_timeout", "renderer_unreachable", "renderer_fetched_no_play",
                "playback_timeout", "renderer_soap_fault", "renderer_http_error",
                "renderer_bad_response", "renderer_never_fetched",
            }:
                hypotheses = [_hypothesis(
                    "renderer_or_delivery_path_playback_failure", "low",
                    "The playback error is renderer/delivery related, but the evidence does not isolate its root cause.",
                )]
        elif code == "ended_early":
            hypotheses = [_hypothesis(
                "playback_ended_before_expected_duration", "low",
                "The track ended early; this symptom alone does not identify why.",
            )]

        decision = "observe" if mode == "off" else ("shadow_propose" if hypotheses else "suppress")
        if hypotheses:
            suppression_reason = None

        records.append({
            "diagnosis_id": f"{component}:{code}",
            "component": component,
            "symptom": code,
            "cause_hypotheses": hypotheses,
            "confidence": max(
                (h["confidence"] for h in hypotheses),
                key={"low": 0, "medium": 1, "high": 2}.get,
                default="unknown",
            ),
            "evidence": evidence,
            "proposed_action": None,
            "decision": decision,
            "suppression_reason": suppression_reason,
            "verification": "not_applicable_d0_no_action",
        })

    return records
