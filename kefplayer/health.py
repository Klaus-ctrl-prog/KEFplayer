"""Gesundheitspruefungen (Phase C): Renderer, Medienserver, Cache. Rein funktional, keine eigenen Threads.

Jede Pruefung liefert ein Dict: ts, component, status (ok|warn|error|unknown), category, message, evidence,
latency_ms. Renderer-Fehler werden mit ``player.classify_error`` eingeordnet (keine zweite Klassifikation).
Es werden nur lesende UPnP-Aktionen gesendet (GetTransportInfo). Grenze: Eine Windows-Firewall, die eingehende
Verbindungen vom Renderer blockiert, ist von hier aus nicht erkennbar (lokale Verbindungen umgehen sie).
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import upnp
from .player import classify_error

MIN_FREE_WARN_MB = 500.0
MIN_FREE_ERROR_MB = 50.0
_RANK = {"ok": 0, "unknown": 1, "warn": 2, "error": 3}
MIN_INTERVAL_S = 15.0      # Untergrenze: nie haeufiger als alle 15 s (Schutz der KEF vor Dauerabfragen)
DEFAULT_INTERVAL_S = 30.0


def _result(component: str, status: str, category: str, message: str, t0: float | None = None, **evidence) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "component": component, "status": status, "category": category, "message": message,
        "evidence": evidence,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1) if t0 is not None else None,
    }


def check_renderer_http(info: upnp.DeviceInfo | None, timeout: float = 3.0) -> dict:
    """Antwortet das Geraet auf HTTP (Gerätebeschreibung)? Sagt nichts ueber den UPnP-Dienst."""
    if info is None:
        return _result("renderer_http", "unknown", "renderer", "Kein Renderer bekannt.")
    t0 = time.perf_counter()
    try:
        upnp.http_get(info.location, timeout)
    except Exception as e:  # noqa: BLE001 - jede Ursache wird gemeldet
        return _result("renderer_http", "error", classify_error(e), f"Renderer nicht per HTTP erreichbar: {e}", t0,
                       host=info.host)
    return _result("renderer_http", "ok", "renderer", "Renderer antwortet auf HTTP.", t0, host=info.host)


def check_renderer_upnp(info: upnp.DeviceInfo | None, timeout: float = 3.0) -> dict:
    """Antwortet der AVTransport-Dienst auf GetTransportInfo (nur lesend)?"""
    if info is None:
        return _result("renderer_upnp", "unknown", "upnp", "Kein Renderer bekannt.")
    t0 = time.perf_counter()
    try:
        ti = upnp.Renderer(info, timeout=timeout).transport_info()
    except Exception as e:  # noqa: BLE001
        return _result("renderer_upnp", "error", classify_error(e), f"UPnP-Abfrage fehlgeschlagen: {e}", t0)
    state = ti.get("CurrentTransportState") or None
    return _result("renderer_upnp", "ok", "upnp", "UPnP-Dienst antwortet.", t0, transport_state=state)


def check_media_server(server=None, bind: str = "0.0.0.0", port: int = 8765, timeout: float = 2.0) -> dict:
    """Mit Server: laeuft der Thread, nimmt der Port Verbindungen an? Ohne Server: ist der Port frei?"""
    t0 = time.perf_counter()
    if server is not None:
        thread = getattr(server, "_thread", None)
        if getattr(server, "_httpd", None) is None or thread is None or not thread.is_alive():
            return _result("media_server", "error", "media_server", "Medienserver laeuft nicht.", t0)
        host = server.advertise_host or ("127.0.0.1" if server.bind in ("0.0.0.0", "") else server.bind)
        try:
            with socket.create_connection((host, server.port), timeout=timeout):
                pass
        except OSError as e:
            return _result("media_server", "error", "media_server", f"Medienserver nimmt keine Verbindung an: {e}", t0,
                           port=server.port)
        return _result("media_server", "ok", "media_server", "Medienserver nimmt Verbindungen an.", t0, port=server.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((bind, port))
    except OSError as e:
        return _result("media_server", "warn", "media_server",
                       f"Port {port} ist belegt oder nicht nutzbar (anderer Prozess oder laufender Player?): {e}", t0, port=port)
    finally:
        s.close()
    return _result("media_server", "ok", "media_server", f"Port {port} ist frei.", t0, port=port)


def check_cache(cache_dir=None, min_free_warn_mb: float = MIN_FREE_WARN_MB, min_free_error_mb: float = MIN_FREE_ERROR_MB) -> dict:
    """Ist das Cache-Verzeichnis beschreibbar und der freie Platz ausreichend? Ohne Angabe: Temp-Verzeichnis."""
    t0 = time.perf_counter()
    d = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=d, prefix=".kefplayer_health_")
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
            os.unlink(name)
        free_mb = shutil.disk_usage(d).free / 1e6
    except OSError as e:
        return _result("cache", "error", "cache_io", f"Cache-Verzeichnis nicht beschreibbar: {e}", t0, dir=str(d))
    free_mb = round(free_mb, 1)
    if free_mb < min_free_error_mb:
        return _result("cache", "error", "cache_io", f"Kaum freier Platz im Cache ({free_mb} MB).", t0, dir=str(d), free_mb=free_mb)
    if free_mb < min_free_warn_mb:
        return _result("cache", "warn", "cache_io", f"Wenig freier Platz im Cache ({free_mb} MB).", t0, dir=str(d), free_mb=free_mb)
    return _result("cache", "ok", "cache_io", "Cache beschreibbar, Platz ausreichend.", t0, dir=str(d), free_mb=free_mb)


def run_checks(info: upnp.DeviceInfo | None = None, server=None, cache_dir=None, *, bind: str = "0.0.0.0",
               port: int = 8765, timeout: float = 3.0) -> list[dict]:
    """Alle Pruefungen. Ist der Renderer per HTTP nicht erreichbar, wird die UPnP-Abfrage uebersprungen (begrenzte Dauer)."""
    http = check_renderer_http(info, timeout)
    if http["status"] == "error":
        up = _result("renderer_upnp", "unknown", "upnp", "Uebersprungen: Renderer per HTTP nicht erreichbar.")
    else:
        up = check_renderer_upnp(info, timeout)
    return [http, up, check_media_server(server, bind, port), check_cache(cache_dir)]


def overall(checks: list[dict]) -> str:
    return max((c["status"] for c in checks), key=lambda s: _RANK.get(s, 1), default="unknown")


class HealthMonitor:
    """Periodische Gesundheitspruefung im Hintergrund (ein Daemon-Thread), auch ohne laufende Wiedergabe.

    * Meldet nur Zustandswechsel als Ereignis ``health_change`` (kein Dauerrauschen im Protokoll).
    * Waehrend ein Titel spielt (``busy()`` ist wahr), gibt es KEINE Renderer-Abfragen; der Player fragt dann selbst.
      Lokale Pruefungen (Medienserver, Cache) laufen weiter.
    * Intervall mindestens ``MIN_INTERVAL_S``. Pruefungen sind nur lesend (GetTransportInfo, HTTP-GET).
    * Ein Fehler in einer Runde beendet den Thread nicht; er wird gezaehlt (``errors``) und im Snapshot gemeldet.
    """

    def __init__(self, emit, *, info=None, server=None, cache_dir=None, busy=None, interval_s: float = DEFAULT_INTERVAL_S,
                 timeout_s: float = 3.0, clock=time.monotonic):
        self._emit = emit
        self.info, self.server, self.cache_dir = info, server, cache_dir
        self._busy = busy or (lambda: False)
        self.interval_s = max(float(interval_s), MIN_INTERVAL_S)
        self.timeout_s = float(timeout_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._started_mono: float | None = None
        self._last_tick_mono: float | None = None
        self._last_tick_iso: str | None = None
        self._ticks = 0
        self._errors = 0
        self._last_error: str | None = None
        self._renderer_paused = False
        self._state: dict[str, dict] = {}

    # -- eine Runde (auch direkt aufrufbar, z. B. in Tests) --
    def tick(self) -> None:
        results: list[dict] = []
        paused = bool(self._busy())
        # Nicht warten: waehrend einer Wiedergabesitzung Renderer-Pruefungen auslassen,
        # lokale Pruefungen aber weiter ausfuehren. Das erfasst auch den Start vor dem
        # ersten Track und faengt eine bereits laufende Monitor-Runde sicher ab.
        renderer_lock = upnp.RENDERER_LOCK
        if not paused and renderer_lock.acquire(blocking=False):
            try:
                http = check_renderer_http(self.info, self.timeout_s)
                if http["status"] == "error":
                    up = _result("renderer_upnp", "unknown", "upnp", "Uebersprungen: Renderer per HTTP nicht erreichbar.")
                else:
                    up = check_renderer_upnp(self.info, self.timeout_s)
                results += [http, up]
            finally:
                renderer_lock.release()
        else:
            paused = True
        results.append(check_media_server(self.server))
        results.append(check_cache(self.cache_dir))
        for r in results:
            self._update(r)
        with self._lock:
            self._ticks += 1
            self._last_tick_mono = self._clock()
            self._last_tick_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._renderer_paused = paused

    def _update(self, r: dict) -> None:
        comp = r["component"]
        with self._lock:
            prev = self._state.get(comp)
            changed = prev is None or prev["status"] != r["status"]
            self._state[comp] = {**r, "since": r["ts"] if changed else prev["since"]}
        if changed:
            try:
                self._emit("health_change", component=comp, status=r["status"], previous=prev["status"] if prev else None,
                           category=r["category"], message=r["message"], evidence=r["evidence"], latency_ms=r["latency_ms"])
            except Exception as e:  # noqa: BLE001 - ein defekter Empfaenger darf die Ueberwachung nie beenden
                with self._lock:
                    self._errors += 1
                    self._last_error = f"emit: {type(e).__name__}: {e}"

    # -- Thread --
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._errors += 1
                    self._last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval_s)

    def start(self) -> "HealthMonitor":
        if self._thread is None:
            self._started_mono = self._clock()
            self._thread = threading.Thread(target=self._run, name="kefplayer-health", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(self.timeout_s * 2 + 2.0)
        self._stopped = True

    def snapshot(self) -> dict:
        with self._lock:
            comps = {k: dict(v) for k, v in self._state.items()}
            last_mono, ticks, errors, last_err = self._last_tick_mono, self._ticks, self._errors, self._last_error
            tick_iso, paused = self._last_tick_iso, self._renderer_paused
        now = self._clock()
        age = (now - last_mono) if last_mono is not None else None
        limit = 3 * self.interval_s + self.timeout_s * 2
        if self._stopped:
            state = "stopped"
        elif self._thread is None:
            state = "not_started"
        else:
            ref = last_mono if last_mono is not None else self._started_mono
            state = "running" if (self._thread.is_alive() and ref is not None and now - ref <= limit) else "stalled"
        return {"state": state, "interval_s": self.interval_s, "ticks": ticks, "errors": errors, "last_error": last_err,
                "last_tick_at": tick_iso, "last_tick_age_s": round(age, 3) if age is not None else None,
                "renderer_checks_paused": paused, "overall": overall(list(comps.values())) if comps else "unknown",
                "components": comps}
