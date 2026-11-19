"""Reconnect-Supervisor fuer Radio (E1b Schritt 4, E-11: Standard an, nur Upstream).

Nur Logik: Zustand, Budget, Backoff, Qualitaetspruefung vor JEDEM Versuch, Ereignisse. Beruehrt weder den
Renderer noch RENDERER_LOCK. Zahlenwerte sind ARBEITSWERTE, nicht gemessen (docs/radio-e1b-design.md, Abschnitt 8).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .radio import RadioError, mask_url

BACKOFF_S = (2.0, 5.0, 10.0, 30.0, 60.0)   # Arbeitswert; danach bleibt es bei 60 s
WINDOW_S = 600.0                            # gleitendes Fenster
MAX_PER_WINDOW = 5                          # Arbeitswert: Versuche je Fenster
MAX_TOTAL = 30                              # Arbeitswert: Versuche je Sitzung
STALL_S = 10.0                              # Arbeitswert: so lange ohne PCM gilt der Strom als haengend
STABLE_S = 60.0                             # so lange ohne Abbruch setzt den Backoff zurueck


@dataclass
class ReconnectBudget:
    clock: Callable[[], float] = time.monotonic
    backoff: tuple[float, ...] = BACKOFF_S
    window_s: float = WINDOW_S
    max_per_window: int = MAX_PER_WINDOW
    max_total: int = MAX_TOTAL
    stable_s: float = STABLE_S
    _times: deque = field(default_factory=deque)
    total: int = 0
    consecutive: int = 0

    def next_delay(self) -> tuple[float | None, str | None]:
        """(Wartezeit, None) oder (None, Grund), wenn kein weiterer Versuch erlaubt ist. Schreibt nichts."""
        now = self.clock()
        while self._times and now - self._times[0] > self.window_s:
            self._times.popleft()
        if self.total >= self.max_total:
            return None, "total_budget_exhausted"
        if len(self._times) >= self.max_per_window:
            return None, "window_budget_exhausted"
        i = min(self.consecutive, len(self.backoff) - 1)
        return self.backoff[i], None

    def record_attempt(self) -> None:
        self._times.append(self.clock())
        self.total += 1
        self.consecutive += 1

    def record_stable(self, uptime_s: float) -> None:
        if uptime_s >= self.stable_s:
            self.consecutive = 0


@dataclass
class SupervisorStats:
    state: str = "idle"      # idle | connecting | streaming | reconnecting | failed | stopped
    reconnects: int = 0
    attempts: int = 0
    last_category: str | None = None
    last_error: str | None = None


class RadioSupervisor:
    def __init__(self, url: str, *, pipeline, gate: Callable[[str], object], make_decoder: Callable[[str], object],
                 budget: ReconnectBudget | None = None, emit: Callable[..., None] | None = None,
                 reconnect: bool = True, stall_s: float = STALL_S, retry_initial: bool = False,
                 poll_s: float = 0.1, clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.url_masked = mask_url(url)
        self.pipeline = pipeline
        self.gate = gate                    # url -> Verdict (quality_gate-Ergebnis, bereits ausgewertet); RadioError moeglich
        self.make_decoder = make_decoder    # url -> gestarteter Decoder; RadioError moeglich
        self.budget = budget or ReconnectBudget()
        self.emit = emit or (lambda *a, **k: None)
        self.reconnect = reconnect
        self.stall_s = stall_s
        self.retry_initial = retry_initial
        self.poll_s = poll_s
        self.clock = clock
        self._stats = SupervisorStats()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- Steuerung ----
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Supervisor laeuft bereits")
        self._thread = threading.Thread(target=self._run, name="radio-supervisor", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        """True, sobald der erste Decoder laeuft; False bei Zeitablauf oder endgueltigem Fehler vorher."""
        self._ready.wait(timeout)
        return self._ready.is_set() and self.stats().state in ("streaming", "reconnecting", "connecting")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._stats.state not in ("failed",):
                self._stats.state = "stopped"
        self._ready.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)

    def stats(self) -> SupervisorStats:
        with self._lock:
            return SupervisorStats(**self._stats.__dict__)

    def _set(self, **kw) -> None:
        with self._lock:
            if self._stats.state == "stopped" and kw.get("state") not in (None, "stopped"):
                return
            for k, v in kw.items():
                setattr(self._stats, k, v)

    # ---- Ablauf ----
    def _fail(self, category: str, message: str) -> None:
        self._set(state="failed", last_category=category, last_error=message)
        self.emit("radio_failed", category=category, reason=message, source=self.url_masked)
        self._ready.set()
        self.pipeline.finish()

    def _connect_once(self) -> str | None:
        """Ein Versuch: Qualitaetspruefung (E-6) -> Decoder. None = laeuft, sonst Fehlerkategorie (und Meldung in last_error)."""
        self._set(state="connecting")
        try:
            verdict = self.gate(self.url)
        except RadioError as e:
            self._set(last_category=e.category, last_error=str(e))
            return e.category
        if self._stop.is_set():
            return "stopped"  # Stopp kam waehrend der (nicht abbrechbaren) Pruefung: keinen Decoder mehr starten
        if not verdict.ok:
            # Qualitaet ungenuegend: kein Wiederholen aendert das, daher endgueltig
            self._set(last_category=verdict.category, last_error="; ".join(verdict.reasons))
            return "rejected:" + (verdict.category or "stream_quality_unverified")
        try:
            dec = self.make_decoder(self.url)
        except RadioError as e:
            self._set(last_category=e.category, last_error=str(e))
            return e.category
        try:
            self.pipeline.attach_decoder(dec)
        except RuntimeError:
            try:
                dec.stop(0.5)
            except Exception:  # noqa: BLE001
                pass
            self._set(last_category="decoder_failed", last_error="Pipeline beendet.")
            return "decoder_failed"
        return None

    def _run(self) -> None:
        first = True
        try:
            while not self._stop.is_set():
                self.emit("radio_connect", source=self.url_masked, attempt=self._stats.attempts + 1, initial=first)
                self._set(attempts=self._stats.attempts + 1)
                err = self._connect_once()
                if self._stop.is_set():
                    return
                if err and err.startswith("rejected:"):
                    cat = err.split(":", 1)[1]
                    self._fail(cat, self.stats().last_error or cat)
                    return
                if err is None:
                    t_up = self.clock()
                    self._set(state="streaming")
                    self.emit("radio_stream_up", source=self.url_masked, initial=first)
                    self._ready.set()
                    reason = self._watch()
                    if self._stop.is_set():
                        return
                    self.budget.record_stable(self.clock() - t_up)
                    if reason == "failed":
                        self._fail("decoder_failed", f"Verarbeitungsfehler in der Pipeline ({self.pipeline.stats().error_type or 'unbekannt'}).")
                        return
                    rc, tail = self._decoder_diag()
                    if rc not in (None, 0):
                        self._set(last_category="decoder_failed",
                                  last_error=f"Decoder beendet mit Exitcode {rc}" + (f": {tail}" if tail else ""))
                    self.emit("radio_gap", source=self.url_masked, reason=reason, decoder_exit=rc, decoder_stderr=tail)
                elif first and not self.retry_initial:
                    self._fail(err, self.stats().last_error or err)
                    return
                first = False
                if not self.reconnect:
                    cat = self.stats().last_category
                    self._fail(cat if cat in ("stream_stalled", "decoder_failed") else "stream_ended",
                               "Strom beendet, Reconnect ist abgeschaltet.")
                    return
                delay, why = self.budget.next_delay()
                if delay is None:
                    self._fail("stream_reconnect_exhausted", f"Reconnect-Budget erschoepft ({why}).")
                    return
                self.budget.record_attempt()
                self._set(state="reconnecting", reconnects=self._stats.reconnects + 1)
                self.emit("radio_reconnect_wait", source=self.url_masked, delay_s=delay, attempt=self.budget.total)
                if self._stop.wait(delay):
                    return
        except Exception as e:  # noqa: BLE001 - Supervisor darf nie unbemerkt sterben
            self._fail("decoder_failed", f"Supervisor-Fehler: {type(e).__name__}")

    def _decoder_diag(self) -> tuple[int | None, str]:
        """Exitcode und letzte stderr-Zeile (bereits URL-bereinigt) des zuletzt angehaengten Decoders."""
        dec = self.pipeline.decoder
        if dec is None:
            return None, ""
        try:
            tail = dec.stderr_tail()
            return dec.poll(), (tail[-1] if tail else "")
        except Exception:  # noqa: BLE001 - Diagnose darf den Ablauf nie stoeren
            return None, ""

    def _watch(self) -> str:
        """Wartet, bis der Strom endet oder haengt. Beendet haengende Decoder. Rueckgabe: Grund."""
        p = self.pipeline
        while not self._stop.is_set():
            st = p.stats().state
            if st == "gap":
                return "decoder_ended"
            if st in ("failed", "stopped", "ended"):
                return st
            if self.clock() - p.last_data > self.stall_s:
                self._set(last_category="stream_stalled", last_error="Kein PCM-Datenfluss.")
                dec = p.decoder
                if dec is not None:
                    dec.stop(0.5)  # entblockt read(); die Pipeline meldet dann 'gap'
                for _ in range(100):
                    if p.stats().state == "gap" or self._stop.is_set():
                        break
                    time.sleep(self.poll_s / 10)
                return "stalled"
            time.sleep(self.poll_s)
        return "stop"
