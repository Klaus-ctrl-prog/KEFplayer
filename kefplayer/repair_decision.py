"""D1 shadow-only repair decision policy.

This module makes deterministic, observational decisions only. It never
executes repairs, performs I/O, or mutates D0 findings/diagnoses.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

_CATEGORY_COMPONENTS = {
    "renderer": "renderer", "upnp": "upnp", "media_server": "media_server",
    "audio": "audio", "dsp": "dsp", "cache": "cache", "diagnostics": "diagnostics",
}


def _groups(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = finding.get("code")
        if not isinstance(code, str) or not code:
            continue
        category = None
        if code == "track_problem":
            ev = finding.get("evidence")
            raw = ev.get("error_category") if isinstance(ev, dict) else None
            if isinstance(raw, str) and raw:
                category = raw
        grouped[(code, category)].append(finding)

    result: dict[str, list[dict[str, Any]]] = {}
    for (code, track_category), items in grouped.items():
        components = {_CATEGORY_COMPONENTS.get(x.get("category"), "process") for x in items}
        component = next(iter(components)) if len(components) == 1 else "playback"
        diagnosis_id = (
            f"{component}:{code}:{track_category}"
            if code == "track_problem" and track_category is not None
            else f"{component}:{code}"
        )
        result[diagnosis_id] = items
    return result


def _health_reason(causes: list[str], health: dict[str, Any] | None) -> str | None:
    if not isinstance(health, dict) or health.get("enabled") is not True:
        return "health_unavailable"
    monitor = health.get("monitor")
    if not isinstance(monitor, dict):
        return "health_state_unknown"
    if monitor.get("state") != "running":
        return "health_monitor_not_running"

    renderer_causes = {
        "renderer_or_network_path_unavailable", "renderer_upnp_control_path_unavailable",
        "renderer_command_failure_with_recorded_recovery",
        "renderer_command_retry_without_recorded_recovery", "renderer_stop_command_failed",
        "renderer_sink_capability_query_failed", "renderer_or_delivery_path_playback_failure",
    }
    if renderer_causes.intersection(causes) and monitor.get("renderer_checks_paused") is True:
        return "renderer_health_checks_paused"

    required = {
        "renderer_or_network_path_unavailable": ("renderer_http",),
        "renderer_upnp_control_path_unavailable": ("renderer_upnp",),
        "renderer_command_failure_with_recorded_recovery": ("renderer_http", "renderer_upnp"),
        "renderer_command_retry_without_recorded_recovery": ("renderer_http", "renderer_upnp"),
        "renderer_stop_command_failed": ("renderer_upnp",),
        "renderer_sink_capability_query_failed": ("renderer_upnp",),
        "renderer_or_delivery_path_playback_failure": ("renderer_http", "renderer_upnp"),
        "local_media_server_unavailable_or_port_conflict": ("media_server",),
        "cache_write_or_storage_capacity_problem": ("cache",),
        "cache_write_or_storage_problem": ("cache",),
    }
    components = health.get("components")
    if not isinstance(components, dict):
        return "health_components_unknown"
    for cause in causes:
        for name in required.get(cause, ()):
            item = components.get(name)
            if not isinstance(item, dict) or item.get("status") in (None, "unknown"):
                return "required_health_unknown"
    return None


def decide_repairs(
    diagnoses: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    health: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return D1 shadow decisions; no current cause is repair-eligible."""
    if not isinstance(diagnoses, list):
        raise TypeError("diagnoses must be a list")
    if not isinstance(findings, list):
        raise TypeError("findings must be a list")

    grouped = _groups(findings)
    decisions = []
    for diagnosis in diagnoses:
        if not isinstance(diagnosis, dict):
            continue
        diagnosis_id = diagnosis.get("diagnosis_id")
        hypotheses = diagnosis.get("cause_hypotheses")
        causes = [
            h.get("cause") for h in hypotheses
            if isinstance(h, dict) and isinstance(h.get("cause"), str) and h.get("cause")
        ] if isinstance(hypotheses, list) else []
        base = {"diagnosis_id": diagnosis_id, "simulated": True, "repair_executed": False}

        if not causes:
            decisions.append({**base, "status": "suppressed",
                              "reason": "no_supported_d0_hypothesis", "scope": "unknown",
                              "finding_count": 0})
            continue

        matched = grouped.get(diagnosis_id) if isinstance(diagnosis_id, str) else None
        if not matched:
            decisions.append({**base, "status": "deferred",
                              "reason": "finding_scope_unresolved", "scope": "unknown",
                              "finding_count": 0})
            continue

        scopes = {x.get("scope") for x in matched}
        if len(scopes) != 1 or not scopes <= {"queue", "process"}:
            decisions.append({**base, "status": "deferred",
                              "reason": "finding_scope_ambiguous", "scope": "unknown",
                              "finding_count": len(matched)})
            continue

        decisions.append({
            **base,
            "status": "deferred",
            "reason": _health_reason(causes, health) or "no_repair_action_approved_in_d1",
            "scope": next(iter(scopes)),
            "finding_count": len(matched),
        })
    return decisions
