import pytest
from kefplayer import repair_budget as B
from kefplayer import repair_exec as X


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeMon:
    def __init__(self, state="stalled", ticks=0, start_error=None, stop_error=None):
        self._state, self._ticks = state, ticks
        self.start_error, self.stop_error = start_error, stop_error
        self.started = self.stopped = 0

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started += 1
        self._state = "running"
        self._ticks = getattr(self, "_auto_ticks", 1)
        return self

    def stop(self):
        self.stopped += 1
        if self.stop_error:
            raise self.stop_error
        self._state = "stopped"

    def snapshot(self):
        return {"state": self._state, "ticks": self._ticks}


def _env():
    c = Clock()
    events = []
    budget = B.RepairBudget(clock=c)
    return c, budget, events, lambda *a, **k: events.append((a, k))


@pytest.mark.parametrize("snap,expected", [
    ({"state": "stalled"}, True),
    ({"state": "running"}, False),
    ({"state": "stopping"}, False),
    ({"state": "not_started"}, False),
    ({"state": "stopped"}, False),
    (None, False),
    ("x", False),
])
def test_trigger_table(snap, expected):
    assert X.should_restart_monitor(snap)[0] is expected


def test_success_replaces_monitor_and_records_success():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    new = FakeMon()
    new._auto_ticks = 1
    active, result = X.restart_monitor(old, lambda: new, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "success" and active is new
    assert old.stopped == 1 and new.started == 1
    assert ("repair_result",) == (events[-1][0][0],)
    assert events[-1][1]["result"] == "success"
    assert budget.last_success.get(X.ACTION_R5) is not None


def test_running_monitor_is_not_touched():
    c, budget, events, emit = _env()
    old = FakeMon("running", ticks=5)
    active, result = X.restart_monitor(old, lambda: FakeMon(), budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "not_triggered" and active is old and old.stopped == 0
    assert events == []


def test_start_error_keeps_old_and_records_failure():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    def boom():
        return FakeMon(start_error=RuntimeError("port belegt"))
    active, result = X.restart_monitor(old, boom, budget=budget, emit=emit, clock=c, sleep=c.sleep)
    assert result == "failed" and active is old
    assert events[-1][1]["reason"] == "start_error"
    assert len(budget.attempts[X.ACTION_R5]) == 1


def test_unverified_new_monitor_is_stopped_again_and_marked_failed():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    new = FakeMon()
    new._auto_ticks = 0  # startet, macht aber nie einen Takt
    active, result = X.restart_monitor(old, lambda: new, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep, verify_timeout_s=1.0)
    assert result == "failed" and active is old
    assert new.stopped == 1
    assert events[-1][1]["reason"] == "not_verified"


def test_budget_blocks_without_stopping_or_starting():
    c, budget, events, emit = _env()
    budget.record_attempt(X.ACTION_R5, succeeded=False)  # Cooldown aktiv
    old = FakeMon("stalled")
    created = []
    active, result = X.restart_monitor(old, lambda: created.append(1) or FakeMon(),
                                       budget=budget, emit=emit, clock=c, sleep=c.sleep)
    assert result == "blocked" and active is old
    assert old.stopped == 0 and created == []
    assert events[-1][0][0] == "repair_blocked" and events[-1][1]["reason"] == "cooldown"


def test_stop_error_on_old_does_not_prevent_restart():
    c, budget, events, emit = _env()
    old = FakeMon("stalled", stop_error=RuntimeError("haengt"))
    new = FakeMon()
    new._auto_ticks = 1
    active, result = X.restart_monitor(old, lambda: new, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "success" and active is new


def test_one_attempt_per_call_no_retry_loop():
    c, budget, events, emit = _env()
    calls = []
    old = FakeMon("stalled")
    def make():
        calls.append(1)
        m = FakeMon()
        m._auto_ticks = 0
        return m
    X.restart_monitor(old, make, budget=budget, emit=emit, clock=c, sleep=c.sleep, verify_timeout_s=0.5)
    assert len(calls) == 1
