import pytest
from kefplayer import repair_budget as B


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _b():
    c = Clock()
    return c, B.RepairBudget(clock=c)


def test_first_attempt_is_allowed():
    _, b = _b()
    assert b.check("r1") == (True, None)


def test_cooldown_blocks_until_elapsed():
    c, b = _b()
    b.record_attempt("r1", succeeded=False)
    assert b.check("r1") == (False, "cooldown")
    c.advance(B.COOLDOWN_S - 1)
    assert b.check("r1") == (False, "cooldown")
    c.advance(1)
    assert b.check("r1") == (True, None)


def test_action_window_limit():
    c, b = _b()
    for _ in range(B.MAX_PER_WINDOW):
        b.record_attempt("r1", succeeded=False)
        c.advance(B.COOLDOWN_S)
    assert b.check("r1") == (False, "action_window_limit")
    c.advance(B.WINDOW_S)
    assert b.check("r1") == (True, None)


def test_global_budget_across_actions():
    c, b = _b()
    actions = ["r1", "r3", "r5"]
    n = 0
    while n < B.GLOBAL_MAX:
        a = actions[n % len(actions)]
        ok, _ = b.check(a)
        if not ok:
            c.advance(B.COOLDOWN_S)
            continue
        b.record_attempt(a, succeeded=False)
        n += 1
        c.advance(B.COOLDOWN_S)
    assert b.check("r5")[1] in {"global_budget_exhausted", "cooldown", "action_window_limit"}
    c.advance(B.GLOBAL_WINDOW_S)
    assert b.check("r5") == (True, None)


def test_blocked_check_records_nothing():
    c, b = _b()
    b.record_attempt("r1", succeeded=False)
    before = list(b.attempts["r1"])
    b.check("r1")
    b.check("r1")
    assert list(b.attempts["r1"]) == before


def test_flapping_after_repeated_success_locks_and_blocks_everything():
    c, b = _b()
    trig = "media_server_error"
    reasons = []
    for _ in range(B.FLAP_MAX + 1):
        reasons.append(b.record_attempt("r1", succeeded=True, trigger=trig))
        c.advance(B.COOLDOWN_S)
    assert reasons[-1] == "flapping:r1"
    assert b.check("r3") == (False, "locked:flapping:r1")


def test_failures_do_not_trigger_flapping_lock():
    c, b = _b()
    for _ in range(B.FLAP_MAX + 2):
        assert b.record_attempt("r1", succeeded=False, trigger="x") is None
        c.advance(B.COOLDOWN_S)
    assert b.locked_reason is None


def test_lock_reset_requires_explicit_confirmation():
    c, b = _b()
    b.locked_reason = "flapping:r1"
    assert b.reset_lock(operator_confirmed=False) is False
    assert b.check("r3")[0] is False
    assert b.reset_lock(operator_confirmed=True) is True
    assert b.check("r3") == (True, None)


def test_old_flap_recurrences_expire():
    c, b = _b()
    b.record_attempt("r1", succeeded=True, trigger="t")
    c.advance(B.FLAP_WINDOW_S + 1)
    b.record_attempt("r1", succeeded=True, trigger="t")
    c.advance(B.FLAP_WINDOW_S + 1)
    assert b.record_attempt("r1", succeeded=True, trigger="t") is None
