"""D2 Reparaturausfuehrung. R5: Diagnose-Monitor neu starten.

Ein Versuch pro Aufruf, keine Schleife. Alle Abhaengigkeiten werden injiziert,
damit die Logik ohne Netzwerk und ohne echte Threads testbar ist.
"""
from __future__ import annotations

from typing import Any, Callable

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
    budget: Any,
    emit: Callable[..., None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    verify_timeout_s: float = 3.0,
) -> tuple[Any, str]:
    """Gibt (aktiver_monitor, ergebnis) zurueck. Ergebnis: success | failed | blocked | not_triggered."""
    trigger_ok, trigger = should_restart_monitor(old.snapshot() if old is not None else None)
    if not trigger_ok:
        return old, "not_triggered"

    allowed, reason = budget.check(ACTION_R5)
    if not allowed:
        emit("repair_blocked", action=ACTION_R5, reason=reason)
        return old, "blocked"

    emit("repair_start", action=ACTION_R5, trigger=trigger)
    try:
        old.stop()
    except Exception:  # noqa: BLE001 - alter Monitor ist ohnehin gestoert; Neustart trotzdem versuchen
        pass

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
        emit("repair_result", action=ACTION_R5, result="success")
        return new, "success"

    try:
        new.stop()
    except Exception:  # noqa: BLE001
        pass
    emit("repair_result", action=ACTION_R5, result="failed", reason="not_verified")
    return old, "failed"
