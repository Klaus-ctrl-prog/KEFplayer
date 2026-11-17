"""D1 shadow-only repair decision policy.

Decisions are observational only: this module performs no I/O, executes no
repair, and does not mutate D0 findings or diagnoses.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

_CATEGORY_COMPONENTS = {
    "renderer": "renderer",
    "upnp": "upnp",
    "media_server": "media_server",
    "audio": "audio",
    "dsp": "dsp",
    "cache": "cache",
    "diagnostics": "diagnostics",
}

# Only causes with a direct relationship to a health check are gated by it.
# A missing/unrelated component must not block an unrelated source/audio cause.
_CAUSE_HEALTH_COMPONENTS = {
    "renderer_or_network_path_unavailable": ("renderer_http",),
    "renderer_upnp_control_path_unavailable": ("renderer_upnp",),
    "renderer_command_failure_with_recorded_recovery": ("renderer_upnp",),
    "renderer_command_retry_without_recorded_recovery": ("renderer_upnp",),
    "renderer_stop_command_failed": ("renderer_upnp",),
    "renderer_sink_capability_query_failed": ("renderer_upnp",),
    "local_media_server_unavailable_or_port_conflict": ("media_server",),
    "cache_write_or_storage_capacity_problem": ("cache",),
    "cache_write_or_storage_problem": ("cache",),
}

# D0 deliberately uses one broad delivery-path hypothesis for several error
# categories. Resolve the corresponding health component from the original
# error_category rather than requiring unrelated renderer checks.
_DELIVERY_ERROR_COMPONENT = {
    "renderer_timeout": "renderer_http",
    "renderer_unreachable": "renderer_http",
    "renderer_fetched_no_play": "renderer_http",
    "playback_timeout": "renderer_http",
    "renderer_soap_fault": "renderer_upnp",
    "renderer_http_error": "renderer_upnp",
    "renderer_bad_response": "renderer_upnp",
    "renderer_never_fetched": "media_server",
}


def _freeze(value: Any) -> tuple:
    """Create a deterministic, type-preserving representation of JSON-like evidence."""
    if isinstance(value, dict):
        return ("dict", tuple(sorted((str(k), _freeze(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_freeze(v) for v in value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return (type(value).__name__, value)
    return (type(value).__name__, repr(value))


def _evidence_signature(item: dict[str, Any]) -> tuple:
    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    return (
        _freeze(item.get("code")),
        _freeze(item.get("level")),
        _freeze(item.get("category")),
        _freeze(item.get("ts")),
        _freeze(evidence),
    )


def _groups(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Reproduce D0 grouping so the original findings can be checked exactly."""
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = finding.get("code")
        if not isinstance(code, str) or not code:
            continue
        track_category = None
        if code == "track_problem":
            ev = finding.get("evidence")
            raw = ev.get("error_category") if isinstance(ev, dict) else None
            if isinstance(raw, str) and raw:
                track_category = raw
        grouped[(code, track_category)].append(finding)

    result: dict[str, list[dict[str, Any]]] = {}
    for (code, track_category), items in grouped.items():
        components = {
            _CATEGORY_COMPONENTS.get(category, "process") if isinstance(category, str) else "process"
            for category in (item.get("category") for item in items)
        }
        component = next(iter(components)) if len(components) == 1 else "playback"
        diagnosis_id = (
            f"{component}:{code}:{track_category}"
            if code == "track_problem" and track_category is not None
            else f"{component}:{code}"
        )
        result[diagnosis_id] = items
    return result


def _correlate_evidence(
    diagnosis: dict[str, Any], matched: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """Verify every original finding is represented by the complete D0 evidence."""
    omitted = diagnosis.get("evidence_omitted")
    if not isinstance(omitted, int) or isinstance(omitted, bool) or omitted < 0:
        return None, "finding_evidence_invalid"
    if omitted:
        return None, "finding_evidence_truncated"

    d0_evidence = diagnosis.get("evidence")
    if not isinstance(d0_evidence, list) or len(d0_evidence) != len(matched):
        return None, "finding_evidence_mismatch"
    if any(not isinstance(item, dict) for item in d0_evidence):
        return None, "finding_evidence_invalid"

    expected = Counter(_evidence_signature(item) for item in matched)
    observed = Counter(_evidence_signature(item) for item in d0_evidence)
    if expected != observed:
        return None, "finding_evidence_mismatch"

    raw_scopes = [item.get("scope") for item in matched]
    if any(not isinstance(scope, str) for scope in raw_scopes):
        return None, "finding_scope_ambiguous"
    scopes = set(raw_scopes)
    if len(scopes) != 1 or not scopes <= {"queue", "process"}:
        return None, "finding_scope_ambiguous"
    return next(iter(scopes)), None


def _required_health_components(
    causes: list[str], diagnosis: dict[str, Any]
) -> tuple[set[str], str | None]:
    required: set[str] = set()
    for cause in causes:
        required.update(_CAUSE_HEALTH_COMPONENTS.get(cause, ()))
        if cause == "renderer_or_delivery_path_playback_failure":
            evidence = diagnosis.get("evidence")
            categories = set()
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict) and isinstance(item.get("evidence"), dict):
                        category = item["evidence"].get("error_category")
                        if isinstance(category, str) and category:
                            categories.add(category)
            if not categories:
                return set(), "health_mapping_unresolved"
            mapped = {_DELIVERY_ERROR_COMPONENT.get(category) for category in categories}
            if None in mapped:
                return set(), "health_mapping_unresolved"
            required.update(mapped)
    return required, None


def _health_reason(
    causes: list[str], diagnosis: dict[str, Any], health: dict[str, Any] | None
) -> str | None:
    required, mapping_error = _required_health_components(causes, diagnosis)
    if mapping_error:
        return mapping_error
    if not required:
        return None

    if not isinstance(health, dict) or health.get("enabled") is not True:
        return "health_unavailable"
    monitor = health.get("monitor")
    if not isinstance(monitor, dict):
        return "health_state_unknown"
    if monitor.get("state") != "running":
        return "health_monitor_not_running"

    renderer_components = {"renderer_http", "renderer_upnp"}
    if required & renderer_components and monitor.get("renderer_checks_paused") is True:
        return "renderer_health_checks_paused"

    components = health.get("components")
    if not isinstance(components, dict):
        return "health_components_unknown"
    for name in sorted(required):
        item = components.get(name)
        if not isinstance(item, dict) or item.get("status") in (None, "unknown"):
            return "required_health_unknown"
        if item.get("status") != "ok":
            return "required_health_not_ok"
    return None


def decide_repairs(
    diagnoses: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    health: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return deterministic D1 shadow decisions; no cause is repair-eligible yet."""
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

        scope, correlation_error = _correlate_evidence(diagnosis, matched)
        if correlation_error:
            decisions.append({**base, "status": "deferred",
                              "reason": correlation_error, "scope": "unknown",
                              "finding_count": len(matched)})
            continue

        decisions.append({
            **base,
            "status": "deferred",
            "reason": _health_reason(causes, diagnosis, health) or "no_repair_action_approved_in_d1",
            "scope": scope,
            "finding_count": len(matched),
        })
    return decisions
