"""D2 reparatur budget: rate limits, cooldowns, global lock and flapping guard.

Pure logic: no I/O, no threads, injectable clock. A repair is recorded only
when it is actually executed; blocked or deferred decisions are never recorded.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable

WINDOW_S = 600.0            # gleitendes Fenster je Aktion
MAX_PER_WINDOW = 3          # hoechstens 3 Versuche je Aktion und Fenster
COOLDOWN_S = 30.0           # Mindestabstand zwischen zwei Versuchen derselben Aktion
GLOBAL_WINDOW_S = 3600.0    # gleitendes Fenster fuer alle Aktionen
GLOBAL_MAX = 6              # hoechstens 6 Reparaturen je Stunde insgesamt
FLAP_WINDOW_S = 300.0       # Wiederkehr desselben Ausloesers innerhalb dieser Zeit
FLAP_MAX = 2                # zweite Wiederkehr nach Erfolg gilt als Flattern


@dataclass
class RepairBudget:
    clock: Callable[[], float]
    attempts: dict = field(default_factory=lambda: defaultdict(deque))
    global_attempts: deque = field(default_factory=deque)
    last_success: dict = field(default_factory=dict)
    recurrences: dict = field(default_factory=lambda: defaultdict(deque))
    locked_reason: str | None = None

    def _prune(self, q: deque, now: float, window: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def check(self, action: str) -> tuple[bool, str | None]:
        """Darf die Aktion jetzt ausgefuehrt werden? Schreibt nichts."""
        now = self.clock()
        if self.locked_reason:
            return False, f"locked:{self.locked_reason}"
        q = self.attempts[action]
        self._prune(q, now, WINDOW_S)
        self._prune(self.global_attempts, now, GLOBAL_WINDOW_S)
        if q and now - q[-1] < COOLDOWN_S:
            return False, "cooldown"
        if len(q) >= MAX_PER_WINDOW:
            return False, "action_window_limit"
        if len(self.global_attempts) >= GLOBAL_MAX:
            return False, "global_budget_exhausted"
        return True, None

    def record_attempt(self, action: str, *, succeeded: bool, trigger: str | None = None) -> str | None:
        """Nur aufrufen, wenn die Aktion tatsaechlich ausgefuehrt wurde.

        Gibt einen Sperrgrund zurueck, falls durch diesen Versuch gesperrt wird.
        """
        now = self.clock()
        self.attempts[action].append(now)
        self.global_attempts.append(now)
        if trigger is not None:
            rq = self.recurrences[(action, trigger)]
            self._prune(rq, now, FLAP_WINDOW_S)
            rq.append(now)
            if succeeded and len(rq) > FLAP_MAX:
                self.locked_reason = f"flapping:{action}"
                return self.locked_reason
        if succeeded:
            self.last_success[action] = now
        return None

    def reset_lock(self, operator_confirmed: bool) -> bool:
        """Sperre nur durch ausdrueckliche Bestaetigung aufheben."""
        if not operator_confirmed:
            return False
        self.locked_reason = None
        return True
