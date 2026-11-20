"""D2 Reparaturausfuehrung (opt-in). Einzige Aktion: R5, Diagnose-Monitor neu starten.

R5 ist harmlos und umkehrbar: Der Monitor liest nur. Es wird nie am Renderer
gearbeitet, nie RENDERER_LOCK erworben und nie waehrend einer Wiedergabesitzung
eingegriffen. Ein Versuch pro Aufruf, keine Schleife. Abhaengigkeiten werden
injiziert, damit die Logik ohne Netzwerk testbar ist.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

from .repair_budget import RepairBudget

ACTION_R5 = "R5_restart_monitor"
TRIGGER_R5 = "monitor_stalled"


def should_restart_monitor(snapshot: Any) -> tuple[bool, str]:
    """Ausloeser fuer R5. Nur ein Monitor, der nachweislich nicht mehr arbeitet."""
    if not isinstance(snapshot, dict):
        return False, "snapshot_unknown"
    if snapshot.get("state") == "stalled":
        return True, TRIGGER_R5
    return False, "not_triggered"


def restart_monitor(
    old: Any,
    make_monitor: Callable[[], Any],
    *,
    busy: Callable[[], bool],
    budget: Any,
    emit: Callable[..., None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    verify_timeout_s: float = 3.0,
) -> tuple[Any, str]:
    """Gibt (aktiver_monitor, ergebnis) zurueck. Ergebnis: success | failed | blocked | busy | not_triggered.

    ``busy`` meldet eine aktive Wiedergabesitzung (Player.track_active). Dann
    passiert nichts: kein Budgetverbrauch, kein Ereignis, kein Eingriff.
    """
    try:
        if busy():
            return old, "busy"
    except Exception:  # noqa: BLE001 - unbekannt gilt als beschaeftigt, nie als frei
        return old, "busy"
    trigger_ok, trigger = should_restart_monitor(old.snapshot() if old is not None else None)
    if not trigger_ok:
        return old, "not_triggered"

    allowed, reason = budget.check(ACTION_R5)
    if not allowed:
        emit("repair_blocked", action=ACTION_R5, reason=reason)
        return old, "blocked"

    emit("repair_start", action=ACTION_R5, trigger=trigger)
    # F-11: Der alte Monitor wird erst gestoppt, wenn der Ersatz gestartet UND verifiziert ist. Scheitert der Neustart,
    # bleibt der alte Monitor unberuehrt aktiv (nie ein gestoppter Monitor als "aktiv"). Die Ueberlappung ist auf die
    # Verifikationszeit begrenzt; beide Monitore lesen nur.
    new = None
    try:
        new = make_monitor().start()
    except Exception as exc:  # noqa: BLE001
        budget.record_attempt(ACTION_R5, succeeded=False)
        emit("repair_result", action=ACTION_R5, result="failed", reason="start_error", error=str(exc))
        return old, "failed"

    deadline = clock() + verify_timeout_s
    verified = False
    while True:
        snap = new.snapshot()
        if snap.get("ticks", 0) >= 1 and snap.get("state") == "running":
            verified = True
            break
        if clock() >= deadline:
            break
        sleep(0.05)

    budget.record_attempt(ACTION_R5, succeeded=verified, trigger=trigger)
    if verified:
        old_stopped = _stop_confirmed(old)
        if old_stopped:
            emit("repair_result", action=ACTION_R5, result="success")
        else:  # alter Monitor haengt noch in einem blockierenden Aufruf: Stop-Flag ist gesetzt, Thread endet danach von selbst
            emit("repair_result", action=ACTION_R5, result="success", old_monitor_stopped=False)
        return new, "success"

    new_stopped = _stop_confirmed(new)
    emit("repair_result", action=ACTION_R5, result="failed", reason="not_verified",
         **({} if new_stopped else {"new_monitor_stopped": False}))
    return old, "failed"


def _stop_confirmed(mon: Any) -> bool:
    """stop() aufrufen und pruefen, ob der Monitor tatsaechlich gestoppt ist (Fehler und Haenger werden nicht verschluckt)."""
    try:
        mon.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        return mon.snapshot().get("state") == "stopped"
    except Exception:  # noqa: BLE001
        return False


class RepairSupervisor:
    """Eigener Thread (der Monitor selbst kann haengen). Haelt den aktiven Monitor.

    ``on_swap(neuer_monitor)`` wird nach erfolgreichem Neustart aufgerufen, damit die
    Status-API den neuen Monitor sieht. Ohne Opt-in wird diese Klasse nie erzeugt.
    """

    def __init__(self, monitor: Any, make_monitor: Callable[[], Any], *, busy: Callable[[], bool],
                 emit: Callable[..., None], on_swap: Callable[[Any], None] | None = None,
                 budget: RepairBudget | None = None, interval_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.monitor = monitor
        self._make, self._busy, self._emit, self._on_swap = make_monitor, busy, emit, on_swap
        self.budget = budget or RepairBudget(clock=clock)
        self.interval_s, self._clock, self._sleep = interval_s, clock, sleep
        self._history: deque = deque(maxlen=20)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _record(self, name: str, **kw) -> None:
        self._history.append({"event": name, **kw})
        self._emit(name, **kw)

    def tick(self) -> str:
        self.monitor, result = restart_monitor(
            self.monitor, self._make, busy=self._busy, budget=self.budget, emit=self._record,
            clock=self._clock, sleep=self._sleep)
        if result == "success" and self._on_swap is not None:
            try:
                self._on_swap(self.monitor)
            except Exception:  # noqa: BLE001 - API-Anbindung darf die Reparatur nie kippen
                pass
        return result

    def start(self) -> "RepairSupervisor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="repair-supervisor", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - Supervisor darf nie die Wiedergabe beeinflussen
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def status(self) -> dict:
        return {"enabled": True, "actions": [ACTION_R5], "recent": list(self._history)}
