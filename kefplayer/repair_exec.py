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


# --------------------------------------------------------------------------
# R1: Medienserver am selben Port neu starten
# --------------------------------------------------------------------------
import threading  # noqa: E402

ACTION_R1 = "R1_restart_media_server"
TRIGGER_R1 = "media_server_error"


def should_restart_server(statuses: Any) -> tuple[bool, str]:
    """Ausloeser fuer R1: die letzten zwei Gesundheitsrunden 'media_server' waren 'error'."""
    if not isinstance(statuses, (list, tuple)) or len(statuses) < 2:
        return False, "not_triggered"
    if all(s == "error" for s in list(statuses)[-2:]):
        return True, TRIGGER_R1
    return False, "not_triggered"


def _stop_with_timeout(server: Any, timeout_s: float) -> bool:
    """server.stop() in einem Hilfsthread. False, wenn es nicht rechtzeitig zurueckkehrt."""
    err: list[BaseException] = []

    def run() -> None:
        try:
            server.stop()
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    return not t.is_alive()


def restart_media_server(
    server: Any,
    statuses: Any,
    *,
    busy: Callable[[], bool],
    budget: Any,
    emit: Callable[..., None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    verify: Callable[[Any], dict],
    stop_timeout_s: float = 5.0,
    verify_timeout_s: float = 3.0,
) -> str:
    """Startet den Medienserver an demselben Port neu. Ergebnis: success | failed | blocked | not_triggered.

    Nur bei festem Port (requested_port != 0): sonst aendern sich bereits ausgegebene URLs.
    Nie waehrend eines laufenden Titels. Bei Fehlschlag bleibt der Server gegebenenfalls gestoppt;
    ein sicherer Zustand ist Sache des Aufrufers. Ein Versuch pro Aufruf.
    """
    triggered, trigger = should_restart_server(statuses)
    if not triggered:
        return "not_triggered"

    if busy():
        emit("repair_blocked", action=ACTION_R1, reason="track_active")
        return "blocked"
    if not getattr(server, "requested_port", 0):
        emit("repair_blocked", action=ACTION_R1, reason="dynamic_port")
        return "blocked"
    allowed, reason = budget.check(ACTION_R1)
    if not allowed:
        emit("repair_blocked", action=ACTION_R1, reason=reason)
        return "blocked"

    port_before = server.port
    entries_before = set(server.entries)
    emit("repair_start", action=ACTION_R1, trigger=trigger, port=port_before, entries=len(entries_before))

    def fail(why: str, **extra: Any) -> str:
        budget.record_attempt(ACTION_R1, succeeded=False)
        emit("repair_result", action=ACTION_R1, result="failed", reason=why, **extra)
        return "failed"

    if not _stop_with_timeout(server, stop_timeout_s):
        return fail("stop_timeout")
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        return fail("start_error", error=str(exc))

    deadline = clock() + verify_timeout_s
    verified = False
    while True:
        res = verify(server)
        if (isinstance(res, dict) and res.get("status") == "ok"
                and server.port == port_before and set(server.entries) == entries_before):
            verified = True
            break
        if clock() >= deadline:
            break
        sleep(0.05)

    if not verified:
        _stop_with_timeout(server, stop_timeout_s)
        return fail("not_verified")

    budget.record_attempt(ACTION_R1, succeeded=True, trigger=trigger)
    emit("repair_result", action=ACTION_R1, result="success", port=port_before)
    return "success"
