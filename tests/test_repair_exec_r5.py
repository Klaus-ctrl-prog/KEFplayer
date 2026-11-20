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
    active, result = X.restart_monitor(old, lambda: new, busy=lambda: False, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "success" and active is new
    assert old.stopped == 1 and new.started == 1
    assert ("repair_result",) == (events[-1][0][0],)
    assert events[-1][1]["result"] == "success"
    assert budget.last_success.get(X.ACTION_R5) is not None


def test_running_monitor_is_not_touched():
    c, budget, events, emit = _env()
    old = FakeMon("running", ticks=5)
    active, result = X.restart_monitor(old, lambda: FakeMon(), busy=lambda: False, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "not_triggered" and active is old and old.stopped == 0
    assert events == []


def test_start_error_keeps_old_and_records_failure():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    def boom():
        return FakeMon(start_error=RuntimeError("port belegt"))
    active, result = X.restart_monitor(old, boom, busy=lambda: False, budget=budget, emit=emit, clock=c, sleep=c.sleep)
    assert result == "failed" and active is old
    assert events[-1][1]["reason"] == "start_error"
    assert len(budget.attempts[X.ACTION_R5]) == 1


def test_unverified_new_monitor_is_stopped_again_and_marked_failed():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    new = FakeMon()
    new._auto_ticks = 0  # startet, macht aber nie einen Takt
    active, result = X.restart_monitor(old, lambda: new, busy=lambda: False, budget=budget, emit=emit,
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
                                       busy=lambda: False, budget=budget, emit=emit, clock=c, sleep=c.sleep)
    assert result == "blocked" and active is old
    assert old.stopped == 0 and created == []
    assert events[-1][0][0] == "repair_blocked" and events[-1][1]["reason"] == "cooldown"


def test_stop_error_on_old_does_not_prevent_restart():
    c, budget, events, emit = _env()
    old = FakeMon("stalled", stop_error=RuntimeError("haengt"))
    new = FakeMon()
    new._auto_ticks = 1
    active, result = X.restart_monitor(old, lambda: new, busy=lambda: False, budget=budget, emit=emit,
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
    X.restart_monitor(old, make, busy=lambda: False, budget=budget, emit=emit, clock=c, sleep=c.sleep, verify_timeout_s=0.5)
    assert len(calls) == 1


# ---- D2 opt-in: Busy-Guard, Sperrfreiheit, Supervisor ----------------------------------

import threading
from kefplayer import upnp


def test_busy_playback_means_no_action_no_budget_no_event():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    active, result = X.restart_monitor(old, lambda: FakeMon(), busy=lambda: True, budget=budget,
                                       emit=emit, clock=c, sleep=c.sleep)
    assert result == "busy" and active is old
    assert old.stopped == 0 and events == [] and not budget.attempts.get(X.ACTION_R5)


def test_unknown_busy_state_counts_as_busy():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")

    def boom():
        raise RuntimeError("x")
    _, result = X.restart_monitor(old, lambda: FakeMon(), busy=boom, budget=budget, emit=emit,
                                  clock=c, sleep=c.sleep)
    assert result == "busy" and old.stopped == 0


def test_repair_never_blocks_or_takes_renderer_lock():
    """Der Lock wird von einem anderen Thread gehalten (Wiedergabe); R5 darf nicht warten."""
    c, budget, events, emit = _env()
    held, release = threading.Event(), threading.Event()

    def holder():
        with upnp.RENDERER_LOCK:
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(2)
    old, new = FakeMon("stalled"), FakeMon()
    done = []
    th = threading.Thread(target=lambda: done.append(X.restart_monitor(
        old, lambda: new, busy=lambda: False, budget=budget, emit=emit, clock=c, sleep=c.sleep)), daemon=True)
    th.start()
    th.join(2)
    release.set()
    t.join(2)
    assert done and done[0][1] == "success"


def test_supervisor_swaps_monitor_and_notifies_api_and_history():
    c, budget, events, emit = _env()
    old, new = FakeMon("stalled"), FakeMon()
    swapped = []
    sup = X.RepairSupervisor(old, lambda: new, busy=lambda: False, emit=emit, on_swap=swapped.append,
                             budget=budget, clock=c, sleep=c.sleep)
    assert sup.tick() == "success"
    assert sup.monitor is new and swapped == [new]
    names = [h["event"] for h in sup.status()["recent"]]
    assert names == ["repair_start", "repair_result"]
    assert sup.status()["actions"] == [X.ACTION_R5]


def test_supervisor_thread_starts_and_stops_and_respects_busy():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    sup = X.RepairSupervisor(old, lambda: FakeMon(), busy=lambda: True, emit=emit, budget=budget,
                             interval_s=0.01).start()
    import time
    time.sleep(0.1)
    sup.stop()
    assert old.stopped == 0 and sup.monitor is old and events == []


# ---- F-11: echte HealthMonitor-Threads, nicht nur Objektidentitaet ----

import threading
import time

from kefplayer.health import HealthMonitor


class _StalledView(HealthMonitor):
    """Echter, laufender Monitor, der sich als 'stalled' meldet, solange er nicht gestoppt wurde (Ausloeser fuer R5)."""

    def snapshot(self):
        snap = super().snapshot()
        if snap["state"] == "running":
            snap["state"] = "stalled"
        return snap


class _NeverTicks(HealthMonitor):
    """Ersatz, der startet, aber nie einen Takt macht (Verifikation scheitert)."""

    def tick(self):
        return None


def _health_threads():
    return [t for t in threading.enumerate() if t.name == "kefplayer-health" and t.is_alive()]


def _real_env():
    return B.RepairBudget(clock=time.monotonic), []


def test_f11_fehlgeschlagener_neustart_laesst_alten_monitor_lebendig_und_funktionsfaehig():
    budget, events = _real_env()
    old = _StalledView(lambda *a, **k: None, interval_s=15.0).start()
    time.sleep(0.2)
    old.tick()  # funktionsfaehig: Takte werden gezaehlt
    ticks_before = old.snapshot()["ticks"]
    new_holder = []
    try:
        def make():
            m = _NeverTicks(lambda *a, **k: None, interval_s=15.0)
            new_holder.append(m)
            return m
        active, result = X.restart_monitor(old, make, busy=lambda: False, budget=budget,
                                           emit=lambda *a, **k: events.append((a, k)),
                                           clock=time.monotonic, sleep=time.sleep, verify_timeout_s=0.3)
        assert result == "failed" and active is old
        assert old._thread.is_alive() and not old._stop.is_set(), "alter Monitor wurde trotz fehlgeschlagenem Neustart gestoppt"
        assert old.snapshot()["state"] != "stopped"
        old.tick()
        assert old.snapshot()["ticks"] == ticks_before + 1
        assert not new_holder[0]._thread.is_alive(), "Ersatz-Monitor laeuft nach Fehlschlag weiter"
        assert len(_health_threads()) == 1
    finally:
        old.stop()
        for m in new_holder:
            m.stop()


def test_f11_erfolgreicher_neustart_genau_ein_lebender_monitor_thread():
    budget, events = _real_env()
    old = _StalledView(lambda *a, **k: None, interval_s=15.0).start()
    time.sleep(0.2)
    new = None
    try:
        def make():
            return HealthMonitor(lambda *a, **k: None, interval_s=15.0)
        active, result = X.restart_monitor(old, make, busy=lambda: False, budget=budget,
                                           emit=lambda *a, **k: events.append((a, k)),
                                           clock=time.monotonic, sleep=time.sleep, verify_timeout_s=3.0)
        new = active
        assert result == "success" and active is not old
        assert active._thread.is_alive() and active.snapshot()["state"] == "running" and active.snapshot()["ticks"] >= 1
        assert not old._thread.is_alive() and old.snapshot()["state"] == "stopped"
        assert len(_health_threads()) == 1
        assert events[-1][1]["result"] == "success" and "old_monitor_stopped" not in events[-1][1]
    finally:
        old.stop()
        if new is not None:
            new.stop()


def test_f11_start_fehler_beruehrt_den_alten_monitor_nicht():
    c, budget, events, emit = _env()
    old = FakeMon("stalled")
    active, result = X.restart_monitor(old, lambda: FakeMon(start_error=RuntimeError("x")), busy=lambda: False,
                                       budget=budget, emit=emit, clock=c, sleep=c.sleep)
    assert result == "failed" and active is old and old.stopped == 0


def test_f11_alter_monitor_haengt_wird_gemeldet_nicht_verschwiegen():
    c, budget, events, emit = _env()
    old = FakeMon("stalled", stop_error=RuntimeError("haengt"))
    new = FakeMon()
    active, result = X.restart_monitor(old, lambda: new, busy=lambda: False, budget=budget, emit=emit,
                                       clock=c, sleep=c.sleep)
    assert result == "success" and active is new
    assert events[-1][1].get("old_monitor_stopped") is False
