import copy
from kefplayer import diagnosis as D
from kefplayer import repair_decision as R


def _f(code, category, evidence, scope="queue", ts="2026-11-17T00:00:00Z"):
    return {"level": "error", "code": code, "category": category, "scope": scope,
            "message": "m", "ts": ts, "ts_source": "event", "evidence": evidence}


def test_befund1_group_collision_is_not_silent():
    # Code "track_problem:source_file_access" ohne error_category bildet dieselbe
    # diagnosis_id wie track_problem mit error_category source_file_access.
    findings = [
        _f("track_problem:source_file_access", "renderer", {}),
        _f("track_problem", "renderer", {"error_category": "source_file_access"}),
    ]
    diagnoses = D.diagnose(findings, mode="shadow")
    supported = [d for d in diagnoses if d["cause_hypotheses"]]
    assert len(supported) == 1
    decisions = R.decide_repairs(diagnoses, findings, None)
    supported_decision = [d for d in decisions if d["diagnosis_id"] == supported[0]["diagnosis_id"]][0]
    assert supported_decision["status"] == "deferred"
    assert supported_decision["reason"] == "finding_group_collision"
    assert all(d["repair_executed"] is False for d in decisions)


def test_befund1_collision_never_yields_eligible_or_executed_repair():
    findings = [
        _f("track_problem:source_file_access", "renderer", {}),
        _f("track_problem", "renderer", {"error_category": "source_file_access"}),
    ]
    diagnoses = D.diagnose(findings, mode="shadow")
    for d in R.decide_repairs(diagnoses, findings, None):
        assert d["status"] in {"deferred", "suppressed"}
        assert d["repair_executed"] is False


def test_befund3_output_does_not_alias_input_findings():
    src = [_f("sink_unavailable", "renderer", {"nested": {"a": [1]}}, scope="process")]
    snapshot = copy.deepcopy(src)
    out = D.diagnose(src, mode="shadow")
    out[0]["evidence"][0]["evidence"]["nested"]["a"].append(99)
    assert src == snapshot
