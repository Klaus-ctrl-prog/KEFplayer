import copy
import json
from kefplayer import diagnosis as D
from kefplayer import repair_decision as R

ALLOWED_D1_STATUS = {"deferred", "suppressed"}
HEALTH_OK = {"enabled": True, "monitor": {"state": "running", "renderer_checks_paused": False},
             "components": {c: {"status": "ok"} for c in
                            ("renderer_http", "renderer_upnp", "media_server", "cache")}}


def _f(code, category, evidence, scope="queue", ts="2026-11-17T00:00:00Z", queue="q1"):
    ev = dict(evidence)
    if scope == "queue":
        ev.setdefault("queue_started_at", queue)
    return {"level": "error", "code": code, "category": category, "scope": scope,
            "message": "m", "ts": ts, "ts_source": "event", "evidence": ev}


SCENARIOS = {
    "unsupported": [_f("unknown_code", "renderer", {})],
    "source_file": [_f("track_problem", "source", {"error_category": "source_file_access"})],
    "renderer_http": [_f("health_renderer_http", "renderer", {}, scope="process")],
    "retries": [_f("renderer_retries", "renderer", {"recovered": 1}, scope="queue")],
    "collision": [_f("track_problem:source_file_access", "renderer", {}),
                  _f("track_problem", "renderer", {"error_category": "source_file_access"})],
}


def test_d1_statuses_are_only_deferred_or_suppressed_and_never_executed():
    for name, findings in SCENARIOS.items():
        for health in (None, HEALTH_OK):
            diagnoses = D.diagnose(findings, mode="shadow")
            decisions = R.decide_repairs(diagnoses, findings, health)
            for d in decisions:
                assert d["status"] in ALLOWED_D1_STATUS, (name, d)
                assert d["repair_executed"] is False, (name, d)
                assert d["simulated"] is True, (name, d)


def test_eligible_never_appears_anywhere_in_d1_output():
    for name, findings in SCENARIOS.items():
        diagnoses = D.diagnose(findings, mode="shadow")
        text = json.dumps(R.decide_repairs(diagnoses, findings, HEALTH_OK), ensure_ascii=False)
        assert "eligible" not in text.lower(), name


def test_stale_diagnosis_is_not_applied_to_a_new_queue():
    old = [_f("track_problem", "renderer", {"error_category": "source_file_access"}, queue="q1")]
    diagnoses = D.diagnose(old, mode="shadow")
    new = [_f("track_problem", "renderer", {"error_category": "source_file_access"}, queue="q2")]
    decisions = R.decide_repairs(diagnoses, new, HEALTH_OK)
    assert len(decisions) == 1
    assert decisions[0]["status"] == "deferred"
    assert decisions[0]["reason"] == "finding_evidence_mismatch"


def test_identical_input_gives_identical_d0_and_d1_output():
    findings = SCENARIOS["collision"] + SCENARIOS["retries"]
    first_d = D.diagnose(copy.deepcopy(findings), mode="shadow")
    second_d = D.diagnose(copy.deepcopy(findings), mode="shadow")
    assert first_d == second_d
    first_r = R.decide_repairs(first_d, findings, HEALTH_OK)
    second_r = R.decide_repairs(second_d, findings, HEALTH_OK)
    assert first_r == second_r
