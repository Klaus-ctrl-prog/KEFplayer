"""Lokale, ausschliesslich lesende Status-API (HTTP + JSON) fuer den laufenden kefplayer.

Grundsaetze
-----------
* Nur lesen: GET/HEAD. Alles andere wird mit 405 abgelehnt. Es gibt keinen Steuerbefehl.
* Nur lokal: Bindung ausschliesslich an 127.0.0.1, Host-Header-Pruefung (Schutz gegen DNS-Rebinding),
  keine CORS-Freigabe, Cross-Site-Anfragen von Browsern werden abgelehnt.
* Eigenstaendig: Das Modul importiert nichts aus der Wiedergabe-Logik ausser (lazy) upnp/audio fuer die
  optionale Live-Abfrage bzw. die Quelldatei-Pruefung. Die Wiedergabe kennt die API nicht; sie reicht ihre
  Ereignisse nur an einen Rueckruf (``on_event``), den der Sammler bedient.
* Keine erfundenen Werte: Alle Werte stammen aus Ereignissen DES LAUFENDEN Prozesses oder (nur mit
  ``--api-live``) aus einer echten, gedrosselten Renderer-Abfrage. Alles andere steht als ``null`` und ist in
  ``unknown`` mit Grund aufgefuehrt. Aus alten Protokolldateien wird nichts gelesen.
* Datenschutz: Standardmaessig keine Verzeichnispfade, keine Renderer-IP/UDN/Geraetename, keine
  Medienserver-Adresse, keine URL-Token. Dateinamen bleiben sichtbar (Titel = Dateiname).
* Blockiert nie die Wiedergabe: Der Sammler haelt ein Schloss nur fuer Mikrosekunden, die HTTP-Threads arbeiten
  auf Kopien, der Server laeuft in eigenen Daemon-Threads.
"""

from __future__ import annotations

import collections
import http.server
import json
import math
import os
import platform
import re
import socket
import socketserver
import statistics
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus

from . import __version__
from .diagnosis import diagnose
from .repair_decision import decide_repairs

API_VERSION = 1
DEFAULT_PORT = 8766            # 8765 ist der Standardport des Medienservers
EVENT_BUFFER = 300
MAX_LOG_LIMIT = 300
MAX_TRACKS_IN_DIAG = 300
MAX_TRACKS_KEPT = 2000
MAX_RESPONSE_BYTES = 1_500_000  # Windows PowerShell 5.1 liest JSON nur bis ca. 2 MB
MAX_CONNECTIONS = 16
REJECT_LINGER_S = 2.0       # so lange darf ein abgewiesener Client seine Anfrage noch loswerden und selbst schliessen
REJECT_DRAIN_MAX = 65536    # so viele Bytes werden dabei hoechstens gelesen und verworfen
MAX_REJECT_THREADS = 8      # gleichzeitig wartende Abweisungen; darueber wird sofort hart geschlossen
MIN_LIVE_INTERVAL_S = 0.5
READ_ONLY_ACTIONS = ("GetTransportInfo", "GetPositionInfo")
ENDPOINTS = ("status", "renderer", "dsp", "audio", "logs", "diagnostics", "health")

_OK_END = {"ended", "skipped", "stopped"}

# Kategorien (Phase C): in welchem Teil des Systems liegt der Befund bzw. das Ereignis?
CATEGORIES = ("renderer", "upnp", "media_server", "audio", "dsp", "cache", "process", "diagnostics")
_EVENT_CATEGORY = {
    "queue_start": "process", "queue_end": "process", "sink": "upnp", "sink_unavailable": "upnp",
    "track_processed": "dsp", "track_start": "audio", "stop_failed": "renderer", "cleanup_failed": "cache",
    "renderer_retry": "renderer", "renderer_recovered": "renderer",
}
# Einordnung der error_category (player.classify_error u. a.) in Systemteile
_ERROR_CATEGORY = {
    "renderer_timeout": "renderer", "renderer_unreachable": "renderer", "renderer_fetched_no_play": "renderer",
    "playback_timeout": "renderer", "renderer_soap_fault": "upnp", "renderer_http_error": "upnp",
    "renderer_bad_response": "upnp", "renderer_never_fetched": "media_server", "source_file_access": "audio",
    "source_format": "audio", "processing_error": "dsp", "cache_io": "cache", "file_io": "audio",
}
_LEVELS = {"info": 0, "warn": 1, "error": 2}
_COMPONENT_CATEGORY = {"renderer_http": "renderer", "renderer_upnp": "upnp", "media_server": "media_server", "cache": "cache"}
_HEALTH_PUBLIC_EVIDENCE = ("transport_state", "port", "free_mb")  # host/dir nur im Detailmodus


# ---- kleine Helfer -----------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _round(v, n: int = 3):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, n) if math.isfinite(f) else None


def _is_index(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _basename(p) -> str | None:
    if p is None:
        return None
    return re.split(r"[\\/]", str(p).rstrip("\\/"))[-1] or None


_RE_URL = re.compile(r"https?://[^\s'\"<>)\]]+", re.I)
_RE_QUOTED_PATH = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|/|\\\\)[^'"]*\1""")
_RE_WIN_PATH = re.compile(r"""(?<![A-Za-z0-9])[A-Za-z]:[\\/]+[^\s'"<>|]*|\\\\[^\s'"<>|]+""")
_RE_POSIX_PATH = re.compile(r"""(?<![\w:.\-])/[^\s'"<>|:]+""")
_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scrub(text, details: bool = False, limit: int = 500) -> str:
    """Freitext (Fehlermeldungen) bereinigen. Best Effort, keine Garantie: vor dem Weitergeben pruefen.

    URLs enthalten das Zugriffs-Token des Medienservers und werden immer gekuerzt. Ohne ``details`` werden
    ausserdem Pfade und IPv4-Adressen entfernt."""
    s = str(text)

    def url(m: re.Match) -> str:
        if not details:
            return "<url>"
        parts = urllib.parse.urlsplit(m.group(0))
        return f"{parts.scheme}://{parts.netloc}/<pfad entfernt>"

    s = _RE_URL.sub(url, s)
    if not details:
        s = _RE_QUOTED_PATH.sub("<pfad>", s)
        s = _RE_WIN_PATH.sub("<pfad>", s)
        s = _RE_POSIX_PATH.sub("<pfad>", s)
        s = _RE_IPV4.sub("<ip>", s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def clean(obj, _depth: int = 0):
    """In JSON-taugliche Struktur ueberfuehren: NaN/Inf -> None, Schluessel -> str, unbekannte Typen -> str."""
    if _depth > 14:
        return None
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, str):
        return obj if len(obj) <= 2000 else obj[:1999] + "…"
    if isinstance(obj, dict):
        return {str(k): clean(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset, collections.deque)):
        seq = sorted(obj, key=str) if isinstance(obj, (set, frozenset)) else obj
        return [clean(v, _depth + 1) for v in seq]
    return clean(str(obj), _depth + 1)


def encode_json(obj, pretty: bool = False) -> bytes:
    """ASCII-JSON (Umlaute als \\uXXXX): keine Kodierungsprobleme in Windows PowerShell 5.1."""
    return json.dumps(
        clean(obj), ensure_ascii=True, allow_nan=False,
        indent=2 if pretty else None, separators=None if pretty else (",", ":"),
    ).encode("ascii")


def _stats(values) -> dict:
    v = sorted(float(x) for x in values if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x))
    if not v:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    return {"n": len(v), "min": _round(v[0]), "median": _round(statistics.median(v)),
            "mean": _round(statistics.fmean(v)), "max": _round(v[-1])}


class _Unknown:
    """Sammelt 'Feld -> Grund' fuer nicht verfuegbare Werte."""

    def __init__(self):
        self.map: dict[str, str] = {}

    def add(self, path: str, reason: str) -> None:
        self.map[path] = reason

    def merge(self, other: dict, prefix: str) -> None:
        for k, v in other.items():
            self.map[f"{prefix}.{k}"] = v


# ---- Live-Abfrage (optional, gedrosselt, nur Get*) -------------------------------------------------


class LiveProbe:
    """Fragt den Renderer ausschliesslich mit GetTransportInfo/GetPositionInfo ab.

    * Nur auf Anfrage (kein Hintergrund-Polling): Ohne API-Aufruf entsteht kein Netzverkehr.
    * Hoechstens eine Abfrage-Runde je ``interval_s`` (mind. 0.5 s), egal wie viele Clients fragen.
    * Eigene Renderer-Instanz mit eigenem Timeout; fragt nur bei freier gemeinsamer Renderer-Sperre ab.
    * Laeuft im aufrufenden HTTP-Thread, nie im Player-Thread; blockiert die Wiedergabe nicht.
    Beobachtereffekt: Jede Abfrage ist zusaetzlicher Verkehr zur KEF (der Player fragt selbst 2x je poll_s)."""

    def __init__(self, renderer_info, interval_s: float = 1.0, timeout_s: float = 2.0, clock=time.monotonic):
        from . import upnp  # lazy: nur wenn Live aktiv ist

        self._upnp = upnp
        self.interval_s = max(MIN_LIVE_INTERVAL_S, float(interval_s))
        self.timeout_s = float(timeout_s)
        self._r = upnp.Renderer(renderer_info, timeout=self.timeout_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._last: dict | None = None
        self.calls: collections.deque = collections.deque(maxlen=1000)  # Aktionsnamen (fuer Tests/Diagnose)

    def _soap_get(self, action: str) -> dict:
        if action not in READ_ONLY_ACTIONS:  # Absicherung: nie etwas anderes senden
            raise RuntimeError(f"Aktion nicht erlaubt: {action}")
        self.calls.append(action)
        return self._r.soap(self._upnp.SVC_AVT, action, {"InstanceID": "0"})

    def _query(self) -> dict:
        t0 = self._clock()
        res = {"queried_at": _now_iso(), "ok": False, "transport_state": None, "transport_status": None,
               "position_s": None, "duration_s": None, "latency_ms": None, "error": None}
        try:
            ti = self._soap_get("GetTransportInfo")
            res["transport_state"] = ti.get("CurrentTransportState") or None
            res["transport_status"] = ti.get("CurrentTransportStatus") or None
            pi = self._soap_get("GetPositionInfo")
            res["position_s"] = _round(self._upnp.parse_hms(pi.get("RelTime", "")))
            res["duration_s"] = _round(self._upnp.parse_hms(pi.get("TrackDuration", "")))
            res["ok"] = True
        except Exception as e:  # noqa: BLE001 - jede Ursache wird gemeldet, nie geworfen
            res["error"] = f"{type(e).__name__}: {e}"
        t1 = self._clock()
        res["latency_ms"] = round((t1 - t0) * 1000.0, 1)
        res["mono"] = t1
        return res

    def get(self, refresh: bool = True) -> dict | None:
        now = self._clock()
        with self._lock:
            last = self._last
        if refresh and (last is None or now - last["mono"] >= self.interval_s):
            if self._busy.acquire(blocking=False):  # nur ein Abfrager gleichzeitig; die anderen nehmen den Cache
                try:
                    # Nur abfragen, wenn der Renderer gerade frei ist: Live darf den Player nicht aufhalten.
                    if self._upnp.RENDERER_LOCK.acquire(blocking=False):
                        try:
                            res = self._query()
                            with self._lock:
                                self._last = last = res
                        finally:
                            self._upnp.RENDERER_LOCK.release()
                finally:
                    self._busy.release()
        return dict(last) if last else None

    def peek(self) -> dict | None:
        return self.get(refresh=False)

    def age_s(self, res: dict) -> float:
        return self._clock() - res["mono"]


# ---- Sammler (wird vom Player-Rueckruf gefuettert) ---------------------------------------------------


class StatusCollector:
    """Haelt den passiv aus Ereignissen abgeleiteten Laufzeitzustand. ``on_event`` wirft nie."""

    def __init__(self, details: bool = False, buffer: int = EVENT_BUFFER, clock=time.monotonic):
        self.details = bool(details)
        self._clock = clock
        self._t0 = clock()
        self._started_at = _now_iso()
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=buffer)
        self._seq = 0
        self._dropped = 0
        self._cfg: dict = {}
        self._live: LiveProbe | None = None
        self._health = None
        self._phase = "no_queue"
        self._queue: dict | None = None
        self._queue_end: dict | None = None
        self._queues_seen = 0
        self._cur: dict | None = None
        self._results: collections.OrderedDict = collections.OrderedDict()
        self._proc: dict[int, dict] = {}
        self._sink: dict = {"state": "not_read"}
        self._counters = collections.Counter()  # Prozessweite Summen, bestehendes API-Verhalten
        self._queue_counters = collections.Counter()  # Fehlerzaehler der aktuellen Wiedergabesitzung
        self._last_hook_error: str | None = None
        self._source_cache: dict[str, dict] = {}
        self._source_lock = threading.Lock()
        self.api_requests = collections.Counter()
        self.api_errors = 0
        self.api_last_error: str | None = None

    # ---- Konfiguration (einmaliger Schnappschuss, danach kein Zugriff mehr auf Player/Renderer) ----
    def set_live(self, probe: LiveProbe | None) -> None:
        self._live = probe

    @property
    def live(self) -> LiveProbe | None:
        return self._live

    def set_health(self, monitor) -> None:
        self._health = monitor

    def set_repair(self, supervisor) -> None:
        self._repair = supervisor

    @property
    def repair(self):
        return getattr(self, "_repair", None)

    @property
    def health(self):
        return self._health

    def attach(self, *, player=None, renderer=None, server=None, profile=None) -> None:
        cfg: dict = {}
        if player is not None:
            for name in ("poll_s", "fast_poll_s", "end_window_s", "start_timeout_s", "end_tolerance_s", "fmt", "bits",
                         "dither", "serve_original", "plain", "mime_override", "convert_requested"):
                if hasattr(player, name):
                    cfg[name] = getattr(player, name)
            profile = profile if profile is not None else getattr(player, "profile", None)
        if profile is not None:
            try:
                cfg["profile"] = profile.to_dict()
                cfg["profile_neutral"] = bool(profile.is_neutral())
                cfg["profile_warnings"] = list(profile.validate())
            except Exception as e:  # noqa: BLE001
                cfg["profile_error"] = f"{type(e).__name__}: {e}"
        if renderer is not None:
            info = getattr(renderer, "info", None)
            if info is not None:
                cfg["renderer"] = {
                    "manufacturer": getattr(info, "manufacturer", "") or None,
                    "model_name": getattr(info, "model_name", "") or None,
                    "device_type": getattr(info, "device_type", "") or None,
                    "services": sorted(getattr(info, "services", {}) or {}),
                    "_friendly_name": getattr(info, "friendly_name", "") or None,
                    "_host": getattr(info, "host", "") or None,
                    "_udn": getattr(info, "udn", "") or None,
                    "_location": getattr(info, "location", "") or None,
                }
        if server is not None:
            cfg["media_server"] = {"port": getattr(server, "port", None), "bind": getattr(server, "bind", None),
                                   "_advertise_host": getattr(server, "advertise_host", None)}
        with self._lock:
            self._cfg = cfg

    # ---- Ereigniseingang (laeuft im Player-Thread: kurz halten, nie werfen) ----
    def on_event(self, rec: dict) -> None:
        try:
            self._ingest(rec)
        except Exception as e:  # noqa: BLE001 - ein Fehler hier darf die Wiedergabe nie stoeren
            self._counters["hook_errors"] += 1
            self._last_hook_error = f"{type(e).__name__}: {e}"

    def _ingest(self, rec: dict) -> None:
        ev = str(rec.get("event"))
        now = self._clock()
        with self._lock:
            entry = self._make_entry(self._seq + 1, ev, rec, now)
            self._seq += 1
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(entry)
            self._counters["events_total"] += 1
            self._update_state(ev, rec, now)

    @staticmethod
    def _make_entry(seq: int, ev: str, rec: dict, now: float) -> dict:
        g = rec.get
        e: dict = {"seq": seq, "ts": g("ts"), "event": ev, "mono": now}
        if ev == "queue_start":
            prof = g("profile") or {}
            e.update(n_tracks=len(g("tracks") or []), profile_name=prof.get("name") if isinstance(prof, dict) else None,
                     profile_neutral=g("profile_neutral"), poll_s=g("poll_s"), _renderer=g("renderer"))
        elif ev == "sink":
            e.update(n_protocol_infos=len(g("protocol_infos") or []), mimes=list(g("mimes") or []))
        elif ev == "sink_unavailable":
            e.update(error=g("error"))
        elif ev == "track_processed":
            e.update(index=g("index"), name=_basename(g("src")), _path=g("src"), preamp_db=g("preamp_db"),
                     peak_in_dbfs=g("peak_in_dbfs"), peak_out_dbfs=g("peak_out_dbfs"),
                     guard_gain_db=g("guard_gain_db"), warnings=list(g("warnings") or []), elapsed_s=g("elapsed_s"))
        elif ev == "track_start":
            e.update(index=g("index"), name=_basename(g("path")), _path=g("path"), processed=g("processed"),
                     mime=g("mime"), mime_in_sink=g("mime_in_sink"),
                     served_format=dict(g("served_format") or {}) or None)
        elif ev == "track_end":
            e.update(index=g("index"), name=_basename(g("path")), _path=g("path"), end_reason=g("end_reason"),
                     startup_delay_s=g("startup_delay_s"), wall_s=g("wall_s"), last_position_s=g("last_position_s"),
                     ended_early=g("ended_early"), gap_before_s=g("gap_before_s"), error=g("error"),
                     error_category=g("error_category"), n_requests=len(g("requests") or []))
        elif ev in ("stop_failed", "cleanup_failed"):
            e.update(error=g("error"), name=_basename(g("path")), _path=g("path"))
        elif ev == "health_change":
            bad = g("status") in ("warn", "error")
            e.update(component=g("component"), status=g("status"), previous=g("previous"), error_category=g("category"),
                     error=g("message") if bad else None)
        elif ev in ("renderer_retry", "renderer_recovered"):
            e.update(action=g("action"), attempt=g("attempt"), error_category=g("category"), error=g("error"))
        elif ev == "queue_end":
            e.update(played=g("played"), reasons=list(g("reasons") or []))
        else:  # unbekanntes Ereignis: nur die Feldnamen, nie die Werte (koennten Privates enthalten)
            e.update(fields=sorted(k for k in rec if k not in ("ts", "event"))[:30])
        e["category"] = _category_of(e)
        return e

    def _update_state(self, ev: str, rec: dict, now: float) -> None:
        g = rec.get
        if ev == "queue_start":
            self._queues_seen += 1
            self._queue = {"started_at": g("ts"), "mono": now, "n_tracks": len(g("tracks") or []),
                           "profile": g("profile") if isinstance(g("profile"), dict) else None,
                           "profile_neutral": g("profile_neutral"), "poll_s": g("poll_s")}
            self._queue_counters = collections.Counter()
            self._sink = {"state": "not_read"}
            self._queue_end = None
            self._cur = None
            self._results = collections.OrderedDict()
            self._proc = {}
            self._phase = "preparing"
        elif ev == "sink":
            self._sink = {"state": "read", "at": g("ts"), "mimes": list(g("mimes") or []),
                          "protocol_infos": [str(x) for x in (g("protocol_infos") or [])][:100],
                          "n_protocol_infos": len(g("protocol_infos") or [])}
        elif ev == "sink_unavailable":
            self._sink = {"state": "unavailable", "at": g("ts"), "error": str(g("error"))[:1000]}
            self._counters["sink_unavailable"] += 1
            if self._queue is not None:
                self._queue_counters["sink_unavailable"] += 1
        elif ev == "track_processed":
            idx = g("index")
            if not _is_index(idx):
                self._counters["malformed_events"] += 1
                return
            self._proc[idx] = {"preamp_db": g("preamp_db"), "peak_in_dbfs": g("peak_in_dbfs"),
                               "peak_out_dbfs": g("peak_out_dbfs"), "guard_gain_db": g("guard_gain_db"),
                               "warnings": [str(w) for w in (g("warnings") or [])][:20], "elapsed_s": g("elapsed_s"),
                               "_src": g("src")}
            while len(self._proc) > MAX_TRACKS_KEPT:
                self._proc.pop(next(iter(self._proc)))
        elif ev == "track_start":
            if not _is_index(g("index")):
                self._counters["malformed_events"] += 1
                return
            self._counters["tracks_started"] += 1
            self._cur = {"index": g("index"), "path": g("path"), "served": g("served"), "processed": g("processed"),
                         "mime": g("mime"), "mime_in_sink": g("mime_in_sink"),
                         "served_format": dict(g("served_format") or {}) or None,
                         "started_ts": g("ts"), "started_mono": now}
            self._phase = "track_active"
        elif ev == "track_end":
            idx = g("index")
            if not _is_index(idx):
                self._counters["malformed_events"] += 1
                return
            self._counters["tracks_ended"] += 1
            cur =self._cur if (self._cur and self._cur.get("index") == idx) else {}
            reqs = [r for r in (g("requests") or []) if isinstance(r, dict)]
            methods = collections.Counter(str(r.get("method")) for r in reqs)
            statuses = collections.Counter(str(r.get("status")) for r in reqs)
            sent = [r.get("bytes_sent") for r in reqs if isinstance(r.get("bytes_sent"), (int, float))]
            res = {
                "index": idx, "path": g("path") or cur.get("path"), "processed": cur.get("processed"),
                "mime": cur.get("mime"), "mime_in_sink": cur.get("mime_in_sink"),
                "served_format": cur.get("served_format"), "end_reason": g("end_reason"),
                "startup_delay_s": g("startup_delay_s"), "wall_s": g("wall_s"), "last_position_s": g("last_position_s"),
                "ended_early": g("ended_early"), "gap_before_s": g("gap_before_s"),
                "error": str(g("error"))[:1000] if g("error") else None,
                "error_category": g("error_category"),
                "requests": {"count": len(reqs), "methods": dict(methods), "statuses": dict(statuses),
                             "bytes_sent": int(sum(sent)) if sent else None,
                             "range_requests": sum(1 for r in reqs if r.get("range"))},
                "ended_at": g("ts"),
            }
            self._results[idx] = res
            while len(self._results) > MAX_TRACKS_KEPT:
                self._results.popitem(last=False)
            self._cur = None
            self._phase = "between_tracks"
        elif ev == "queue_end":
            self._queue_end = {"ended_at": g("ts"), "played": g("played"), "reasons": list(g("reasons") or [])}
            self._cur = None
            self._phase = "queue_ended"
        elif ev == "stop_failed":
            self._counters["stop_failed"] += 1
            if self._queue is not None:
                self._queue_counters["stop_failed"] += 1
        elif ev == "cleanup_failed":
            self._counters["cleanup_failed"] += 1
            if self._queue is not None:
                self._queue_counters["cleanup_failed"] += 1
        elif ev == "renderer_retry":
            self._counters["renderer_retry"] += 1
            if self._queue is not None:
                self._queue_counters["renderer_retry"] += 1
        elif ev == "renderer_recovered":
            self._counters["renderer_recovered"] += 1
            if self._queue is not None:
                self._queue_counters["renderer_recovered"] += 1

    def count_request(self, key: str) -> None:
        with self._lock:
            self.api_requests[key] += 1

    def note_api_error(self, text: str) -> None:
        with self._lock:
            self.api_errors += 1
            self.api_last_error = text

    # ---- Lesen ---------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "now_mono": self._clock(),
                "now_iso": _now_iso(),
                "started_at": self._started_at,
                "uptime_s": self._clock() - self._t0,
                "phase": self._phase,
                "queue": dict(self._queue) if self._queue else None,
                "queue_end": dict(self._queue_end) if self._queue_end else None,
                "queues_seen": self._queues_seen,
                "cur": dict(self._cur) if self._cur else None,
                "results": [dict(v) for v in self._results.values()],
                "proc": {k: dict(v) for k, v in self._proc.items()},
                "events": list(self._events),
                "dropped": self._dropped,
                "seq": self._seq,
                "sink": dict(self._sink),
                "cfg": dict(self._cfg),
                "counters": dict(self._counters),
                "queue_counters": dict(self._queue_counters),
                "hook_error": self._last_hook_error,
                "api_errors": self.api_errors,
                "api_last_error": self.api_last_error,
                "api_requests": dict(self.api_requests),
            }

    def source_info(self, path) -> tuple[dict | None, str | None]:
        """Kopfdaten der QUELLDATEI (nur lesend, gecacht). Laeuft im HTTP-Thread, nie im Player."""
        key = str(path)
        with self._source_lock:
            if key in self._source_cache:
                c = self._source_cache[key]
                return c.get("info"), c.get("error")
        try:
            from . import audio

            info, err = audio.probe(key).to_dict(), None
        except Exception as e:  # noqa: BLE001
            info, err = None, f"{type(e).__name__}: {e}"
        with self._source_lock:
            if len(self._source_cache) >= 200:
                self._source_cache.pop(next(iter(self._source_cache)))
            self._source_cache[key] = {"info": info, "error": err}
        return info, err


# ---- Darstellung der Endpunkte ----------------------------------------------------------------------------


def _category_of(e: dict) -> str:
    ev = e.get("event")
    if ev == "track_end":
        ec = e.get("error_category")
        if ec:
            return _ERROR_CATEGORY.get(ec, "process")
        return "audio" if e.get("end_reason") in _OK_END else "renderer"
    if ev == "health_change":
        return _COMPONENT_CATEGORY.get(e.get("component"), "process")
    return _EVENT_CATEGORY.get(ev, "process")


def _level_of(e: dict) -> str:
    ev = e.get("event")
    if ev == "track_end":
        reason = e.get("end_reason")
        if reason not in _OK_END:
            return "error"
        return "warn" if e.get("ended_early") else "info"
    if ev == "track_start":
        return "warn" if e.get("mime_in_sink") is False else "info"
    if ev == "health_change":
        return {"error": "error", "warn": "warn"}.get(e.get("status"), "info")
    if ev in ("sink_unavailable", "stop_failed", "cleanup_failed", "renderer_retry"):
        return "warn"
    return "info"


class StatusApp:
    """Erzeugt die JSON-Antworten aus Sammler (und optionaler Live-Abfrage). Kennt weder HTTP noch Player."""

    def __init__(self, collector: StatusCollector):
        self.c = collector

    @property
    def details(self) -> bool:
        return self.c.details

    # -- Bausteine --
    def _ev(self, e: dict) -> dict:
        d = {k: v for k, v in e.items() if k != "mono" and (self.details or not k.startswith("_"))}
        if self.details:
            d = {(k[1:] if k.startswith("_") else k): v for k, v in d.items()}
        for k in ("error",):
            if d.get(k):
                d[k] = scrub(d[k], self.details)
        if d.get("warnings"):
            d["warnings"] = [scrub(w, self.details, 300) for w in d["warnings"]]
        d["level"] = _level_of(e)
        d["category"] = e.get("category") or _category_of(e)
        return d

    def _track_summary(self, r: dict, snap: dict, proc: bool = True) -> dict:
        out = {
            "index": r["index"], "number": (r["index"] + 1) if isinstance(r.get("index"), int) else None,
            "name": _basename(r.get("path")), "processed": r.get("processed"), "mime": r.get("mime"),
            "mime_in_sink": r.get("mime_in_sink"), "served_format": r.get("served_format"),
            "end_reason": r.get("end_reason"), "startup_delay_s": r.get("startup_delay_s"),
            "gap_before_s": r.get("gap_before_s"), "wall_s": r.get("wall_s"),
            "last_position_s": r.get("last_position_s"), "ended_early": r.get("ended_early"),
            "error": scrub(r["error"], self.details) if r.get("error") else None,
            "error_category": r.get("error_category"), "requests": r.get("requests"),
        }
        if self.details:
            out["path"] = r.get("path")
        if proc and r.get("index") in snap["proc"]:
            p = snap["proc"][r["index"]]
            out["dsp"] = {k: p[k] for k in ("preamp_db", "peak_in_dbfs", "peak_out_dbfs", "guard_gain_db", "elapsed_s")}
        return out

    def _live(self, refresh: bool) -> dict:
        probe = self.c.live
        if probe is None:
            return {"enabled": False, "reason": "Live-Abfrage aus (Standard). Start mit --api-live, um Position und "
                                                "Transportstatus vom Renderer zu lesen; das erzeugt zusaetzlichen Verkehr zur KEF."}
        res = probe.get(refresh=refresh)
        out = {"enabled": True, "interval_s": probe.interval_s, "timeout_s": probe.timeout_s,
               "actions": list(READ_ONLY_ACTIONS), "available": False, "age_s": None, "queried_at": None,
               "ok": None, "error": None, "latency_ms": None}
        if res is None:
            out["error"] = "noch keine Abfrage erfolgt"
            return out
        age = probe.age_s(res)
        stale = age > max(3.0 * probe.interval_s, 3.0)  # zu alt, um als aktueller Zustand zu gelten (z. B. Export am Ende)
        err = scrub(res["error"], self.details) if res["error"] else None
        if stale and res["ok"]:
            err = f"letzte erfolgreiche Abfrage vor {age:.1f} s, zu alt fuer einen aktuellen Wert"
        out.update(available=bool(res["ok"]) and not stale, stale=stale, queried_at=res["queried_at"],
                   ok=None if stale else res["ok"], latency_ms=res["latency_ms"], age_s=_round(age), error=err,
                   _res=res)
        return out

    # -- status --
    def status(self, snap: dict, live: dict) -> tuple[dict, dict]:
        u = _Unknown()
        now = snap["now_mono"]
        cur = snap["cur"]
        lv = live.get("_res") if live.get("available") else None
        track = None
        if cur:
            idx = cur["index"]
            title = _basename(cur["path"])
            title = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", title) if title else None
            sf = cur.get("served_format") or {}
            track = {
                "index": idx, "number": (idx + 1) if isinstance(idx, int) else None,
                "name": _basename(cur["path"]),
                "title": {"value": title, "source": "filename"},
                "processed": cur["processed"], "mime": cur["mime"], "mime_in_sink": cur["mime_in_sink"],
                "duration_s": _round(sf.get("duration_s")),
                "event_started_at": cur["started_ts"],
                "wall_since_start_s": _round(now - cur["started_mono"]),
                "position_s": lv["position_s"] if lv else None,
                "transport_state": lv["transport_state"] if lv else None,
            }
            if self.details:
                track["path"] = cur["path"]
            u.add("track.title.tags", "Titel/Interpret aus den Datei-Tags liefert der Player nicht an die API; "
                                      "als Titel dient der Dateiname.")
            if not lv:
                why = ("Live-Abfrage aus (--api-live)" if not live["enabled"]
                       else f"Live-Abfrage ohne Ergebnis: {live.get('error') or 'keine Daten'}")
                u.add("track.position_s", why)
                u.add("track.transport_state", why)
                u.add("track.paused", why + "; Pause ist ohne Renderer-Abfrage nicht erkennbar.")
            else:
                track["paused"] = (lv["transport_state"] == "PAUSED_PLAYBACK") if lv["transport_state"] else None
                if lv["transport_state"] is None:
                    u.add("track.paused", "Renderer meldete keinen Transportstatus")
                if lv["position_s"] is None:
                    u.add("track.position_s", "Renderer meldete keine lesbare Position (RelTime)")
            if track["duration_s"] is None:
                u.add("track.duration_s", "Dauer der gesendeten Datei nicht ermittelt")
        else:
            u.add("track", {"no_queue": "Es lief noch keine Warteschlange in diesem Prozess.",
                            "preparing": "Warteschlange wird vorbereitet, noch kein Titel gestartet.",
                            "between_tracks": "Zwischen zwei Titeln.",
                            "queue_ended": "Warteschlange beendet."}.get(snap["phase"], "kein aktiver Titel"))
        last_ev = snap["events"][-1] if snap["events"] else None
        last_done = snap["results"][-1] if snap["results"] else None
        q = snap["queue"]
        data = {
            "phase": snap["phase"],
            "phase_meaning": {
                "no_queue": "noch keine Warteschlange gestartet",
                "preparing": "Warteschlange laeuft, noch kein Titel gestartet",
                "track_active": "Player fuehrt einen Titel (Start-Wartezeit, Wiedergabe oder Pause; Details nur mit Live-Abfrage)",
                "between_tracks": "Titel beendet, naechster noch nicht gestartet",
                "queue_ended": "Warteschlange beendet",
            }[snap["phase"]],
            "queue": ({"n_tracks": q["n_tracks"], "tracks_finished": len(snap["results"]),
                       "started_at": q["started_at"], "ended_at": (snap["queue_end"] or {}).get("ended_at")}
                      if q else None),
            "track": track,
            "last_finished_track": ({k: self._track_summary(last_done, snap, proc=False)[k]
                                     for k in ("index", "number", "name", "end_reason", "startup_delay_s",
                                               "gap_before_s", "ended_early")} if last_done else None),
            "last_event": ({"seq": last_ev["seq"], "event": last_ev["event"], "ts": last_ev["ts"],
                            "age_s": _round(now - last_ev["mono"])} if last_ev else None),
            "live": {k: v for k, v in live.items() if k != "_res"},
        }
        if not q:
            u.add("queue", "keine Warteschlange gestartet")
        return data, u.map

    # -- renderer --
    def renderer(self, snap: dict, live: dict) -> tuple[dict, dict]:
        u = _Unknown()
        cfg = snap["cfg"]
        r = cfg.get("renderer")
        sink = snap["sink"]
        data: dict = {}
        if r:
            ident = {"manufacturer": r["manufacturer"], "model_name": r["model_name"], "device_type": r["device_type"]}
            if self.details:
                ident.update(friendly_name=r["_friendly_name"], host=r["_host"], udn=r["_udn"], location=r["_location"])
            else:
                for k in ("friendly_name", "host", "udn", "location"):
                    ident[k] = "redacted"
            data["identity"] = ident
            data["services"] = [":".join(s.split(":")[-2:]) for s in r["services"]]
        else:
            data["identity"] = None
            data["services"] = None
            u.add("identity", "kein Renderer an den Sammler gemeldet")
        mimes = sink.get("mimes")
        data["sink"] = {k: v for k, v in sink.items() if k != "error"}
        if sink.get("error"):
            data["sink"]["error"] = scrub(sink["error"], self.details)
        if sink["state"] == "not_read":
            u.add("sink", "Sink-Formate wurden noch nicht gelesen (kommt beim Start der Warteschlange)")
        elif sink["state"] == "unavailable":
            u.add("sink.mimes", "Renderer lieferte die Sink-Formate nicht")
        def has(*names):
            return None if mimes is None else any(m.lower() in names for m in mimes)
        data["capabilities"] = {
            "flac_in_sink": has("audio/flac", "audio/x-flac"),
            "wav_in_sink": has("audio/wav", "audio/x-wav", "audio/vnd.wave"),
            "mp3_in_sink": has("audio/mpeg"),
            "note": "Eigenangabe des Geraets (GetProtocolInfo), kein Wiedergabetest.",
        }
        u.add("capabilities.gapless", "nicht abgefragt (SCPD/SetNextAVTransportURI wird nicht gelesen)")
        lv = live.get("_res") if live.get("available") else None
        if lv:
            reach = True
        elif live["enabled"] and live.get("ok") is False and not live.get("stale"):
            reach = False
        else:
            reach = None  # ohne Live, ohne Ergebnis oder mit zu altem Ergebnis: nicht behaupten
        data["connection"] = {"reachable": reach, "source": "live" if live["enabled"] else None,
                              "last_check_ms": live.get("latency_ms"), "last_check_age_s": live.get("age_s"),
                              "evidence": {"sink_state": sink["state"], "sink_at": sink.get("at"),
                                           "tracks_with_error": sum(1 for x in snap["results"]
                                                                    if x.get("end_reason") not in _OK_END)}}
        if reach is None:
            u.add("connection.reachable", "nicht ermittelbar: ohne Live-Abfrage (--api-live), ohne Ergebnis oder die letzte "
                                          "Abfrage ist zu alt; Hinweise nur unter evidence")
        data["live"] = {k: v for k, v in live.items() if k != "_res"}
        ms = cfg.get("media_server")
        if ms:
            bind = ms.get("bind")
            data["media_server"] = {"port": ms.get("port"),
                                    "bind": bind if (self.details or bind in ("0.0.0.0", "127.0.0.1", "::", None)) else "redacted"}
            if self.details:
                data["media_server"]["advertise_host"] = ms.get("_advertise_host")
            else:
                data["media_server"]["advertise_host"] = "redacted"
        return data, u.map

    # -- dsp --
    def dsp(self, snap: dict) -> tuple[dict, dict]:
        u = _Unknown()
        cfg = snap["cfg"]
        q = snap["queue"]
        prof = (q or {}).get("profile") or cfg.get("profile")
        source = "queue_start-Ereignis" if (q and q.get("profile")) else ("Player-Konfiguration" if prof else None)
        data: dict = {"profile": None, "source": source}
        if prof:
            ch = prof.get("channels") or {}
            channels = {}
            for key in ["L", "R"] + [k for k in ch if k not in ("L", "R")]:
                if key in ch:
                    channels[key] = dict(ch[key], defined_in_profile=True)
                else:
                    channels[key] = {"gain_db": 0.0, "delay_ms": 0.0, "filters": [], "defined_in_profile": False}
            data["profile"] = {
                "name": prof.get("name"),
                "is_neutral": (q or {}).get("profile_neutral", cfg.get("profile_neutral")),
                "preamp_setting_db": prof.get("preamp_db"),
                "filters_all_channels": prof.get("filters") or [],
                "channels": channels,
                "warnings": [scrub(w, self.details, 400) for w in cfg.get("profile_warnings", [])],
            }
            data["channel_delays_ms"] = {k: v["delay_ms"] for k, v in channels.items()}
        else:
            u.add("profile", "kein Profil gemeldet (Sammler nicht an einen Player angeschlossen)")
        if cfg.get("profile_error"):
            u.add("profile.warnings", scrub(cfg["profile_error"], self.details))
        applied = []
        for r in snap["results"][-MAX_TRACKS_IN_DIAG:]:
            p = snap["proc"].get(r["index"])
            if p:
                applied.append({"index": r["index"], "name": _basename(r.get("path")),
                                **{k: p[k] for k in ("preamp_db", "peak_in_dbfs", "peak_out_dbfs", "guard_gain_db",
                                                     "elapsed_s")}})
        cur = snap["cur"]
        if cur and cur["index"] in snap["proc"]:
            p = snap["proc"][cur["index"]]
            applied.append({"index": cur["index"], "name": _basename(cur["path"]),
                            **{k: p[k] for k in ("preamp_db", "peak_in_dbfs", "peak_out_dbfs", "guard_gain_db", "elapsed_s")},
                            "current": True})
        data["applied_per_track"] = applied[-50:]
        data["output"] = {k: cfg.get(k) for k in ("fmt", "bits", "dither") if k in cfg}
        if "bits" in cfg and cfg["bits"] is None:
            data["output"]["bits"] = "wie Quelle"
        if not applied:
            u.add("applied_per_track", "noch kein Titel verarbeitet (bei neutralem Profil wird das Original gesendet)")
        return data, u.map

    # -- audio --
    def _audio_track(self, r: dict, snap: dict, current: bool) -> tuple[dict, dict]:
        u = _Unknown()
        served = r.get("served_format")
        processed = r.get("processed")
        neutral = (snap["queue"] or {}).get("profile_neutral")
        out = {"index": r.get("index"), "name": _basename(r.get("path")), "current": current,
               "mime": r.get("mime"), "mime_in_sink": r.get("mime_in_sink"), "target_format": served}
        src = None
        if processed is False:
            src = served
            out["source_format"] = {"value": served, "origin": "gesendete Datei ist das Original"}
        elif processed is True and r.get("path"):
            src, err = self.c.source_info(r["path"])
            out["source_format"] = {"value": src, "origin": "Kopfdaten der Quelldatei (von der API gelesen)"}
            if err:
                u.add(f"source_format.{r.get('index')}", scrub(err, self.details))
        else:
            out["source_format"] = {"value": None, "origin": None}
            u.add(f"source_format.{r.get('index')}", "unbekannt (Titel nicht gestartet oder Pfad fehlt)")
        if processed is None:
            path = {"mode": None, "description": None}
            u.add(f"processing_path.{r.get('index')}", "unbekannt")
        elif processed is False:
            path = {"mode": "original",
                    "description": "Originaldatei unveraendert (bitgenau) ueber den Medienserver an den Renderer"}
        else:
            path = {"mode": "dsp" if neutral is False else "convert",
                    "description": ("Dekodieren, Filter/Pegel/Delay, Kodieren und Senden" if neutral is False
                                    else "Dekodieren, Umwandeln (Format/Bittiefe), Kodieren und Senden; kein Filter")
                    if neutral is not None else "verarbeitet (Profilzustand unbekannt)"}
        out["processing_path"] = path
        if src and served:
            out["differs"] = {k: (src.get(k) != served.get(k)) for k in ("format", "samplerate", "channels", "bits")}
        if self.details and r.get("path"):
            out["path"] = r["path"]
        return out, u.map

    def audio(self, snap: dict) -> tuple[dict, dict]:
        u = _Unknown()
        cfg = snap["cfg"]
        data: dict = {
            "serving": {k: cfg.get(k) for k in ("serve_original", "convert_requested", "fmt", "bits", "dither", "plain",
                                                "mime_override") if k in cfg},
            "current": None, "recent": [],
        }
        cur = snap["cur"]
        if cur:
            a, um = self._audio_track(cur, snap, True)
            data["current"] = a
            u.merge(um, "current")
        else:
            u.add("current", "kein aktiver Titel (Phase: %s)" % snap["phase"])
        for r in snap["results"][-10:]:
            a, um = self._audio_track(r, snap, False)
            data["recent"].append(a)
            u.merge(um, "recent")
        return data, u.map

    # -- health --
    def _repair_status(self) -> dict:
        sup = self.c.repair
        if sup is None:
            return {"enabled": False, "reason": "D2 nicht aktiviert (--enable-repair); nur D1-Schattenentscheidungen."}
        try:
            return sup.status()
        except Exception:  # noqa: BLE001
            return {"enabled": True, "status": "unknown"}

    def health_status(self, snap: dict) -> tuple[dict, dict]:
        u = _Unknown()
        mon = self.c.health
        if mon is None:
            u.add("monitor", "Gesundheitsueberwachung nicht aktiv (Standard bei 'play'; --no-health schaltet sie ab)")
            return {"enabled": False, "reason": "Gesundheitsueberwachung nicht aktiv."}, u.map
        h = mon.snapshot()
        comps = {}
        for name, r in h["components"].items():
            ev = {k: v for k, v in (r.get("evidence") or {}).items() if self.details or k in _HEALTH_PUBLIC_EVIDENCE}
            comps[name] = {"status": r["status"], "category": _COMPONENT_CATEGORY.get(name, "process"),
                           "error_category": r.get("category"), "message": scrub(r["message"], self.details),
                           "latency_ms": r.get("latency_ms"), "last_checked_at": r.get("ts"), "since": r.get("since"),
                           "evidence": ev}
        data = {"enabled": True, "overall": h["overall"], "monitor": {k: h[k] for k in (
            "state", "interval_s", "ticks", "errors", "last_tick_at", "last_tick_age_s", "renderer_checks_paused")},
            "components": comps,
            "repair": self._repair_status(),
            "notes": ["Nur lesende Abfragen (HTTP-GET der Geraetebeschreibung, GetTransportInfo).",
                      "Waehrend ein Titel spielt, werden keine Renderer-Abfragen gesendet (renderer_checks_paused).",
                      "Eine blockierende Windows-Firewall ist von hier aus nicht erkennbar."]}
        if h["last_error"]:
            data["monitor"]["last_error"] = scrub(h["last_error"], self.details)
        return data, u.map

    # -- logs --
    def logs(self, snap: dict, limit: int, since: int, level: str, category: str | None = None) -> tuple[dict, dict]:
        u = _Unknown()
        floor = _LEVELS[level]
        sel = [e for e in snap["events"] if e["seq"] > since and _LEVELS[_level_of(e)] >= floor
               and (category is None or e.get("category") == category)]
        sel = sel[-limit:]
        evs = snap["events"]
        data = {
            "events": [self._ev(e) for e in sel], "returned": len(sel), "limit": limit, "since": since, "level": level,
            "category": category,
            "newest_seq": snap["seq"], "oldest_seq_in_buffer": evs[0]["seq"] if evs else None,
            "buffer_size": EVENT_BUFFER, "dropped_total": snap["dropped"],
            "scope": "nur Ereignisse dieses Prozesses seit Start; keine Zeilen aus alten Protokolldateien",
        }
        if not evs:
            u.add("events", "noch keine Ereignisse")
        return data, u.map

    # -- diagnostics --
    def timing(self, snap: dict) -> dict:
        rs = snap["results"]
        cfg = snap["cfg"]
        return {
            "gap_before_s": _stats(r.get("gap_before_s") for r in rs),
            "startup_delay_s": _stats(r.get("startup_delay_s") for r in rs),
            "resolution_hint": {
                "fast_poll_s": cfg.get("fast_poll_s"), "poll_s": cfg.get("poll_s"),
                "note": "Zeiten sind Abfragezeitpunkte des Players (Auflösung ca. fast_poll_s kurz vor Titelende und "
                        "beim Start, sonst poll_s) plus SOAP-Latenz. Modellangabe, nicht gemessen.",
            },
        }

    def findings(self, snap: dict, live: dict) -> list[dict]:
        out: list[dict] = []

        def add(level, code, msg, category, ts=None, **ev):
            # ts: Zeitpunkt des zugrunde liegenden Ereignisses, wenn bekannt; sonst Zeitpunkt der Auswertung
            queue_scoped = {"track_problem", "ended_early", "mime_not_in_sink", "sink_unavailable",
                            "stop_failed", "cleanup_failed", "renderer_retries", "clipping_guard"}
            scope = "queue" if code in queue_scoped and snap.get("queue") is not None else "process"
            if scope == "queue":
                ev.setdefault("queue_started_at", snap["queue"].get("started_at"))
            out.append({"level": level, "code": code, "category": category, "scope": scope, "message": msg,
                        "ts": ts or snap["now_iso"], "ts_source": "event" if ts else "evaluation", "evidence": ev})

        for r in snap["results"]:
            name = _basename(r.get("path"))
            if r.get("end_reason") not in _OK_END:
                add("error", "track_problem", f"Titel {r['index'] + 1} endete mit '{r.get('end_reason')}'.",
                    _ERROR_CATEGORY.get(r.get("error_category"), "renderer"), r.get("ended_at"), index=r["index"], name=name, end_reason=r.get("end_reason"), error_category=r.get("error_category"),
                    error=scrub(r["error"], self.details) if r.get("error") else None)
            elif r.get("ended_early"):
                add("warn", "ended_early", f"Titel {r['index'] + 1} endete frueher als die Dauer.",
                    "renderer", r.get("ended_at"), index=r["index"], name=name, last_position_s=r.get("last_position_s"))
            if r.get("mime_in_sink") is False:
                add("warn", "mime_not_in_sink", f"MIME-Typ {r.get('mime')} steht nicht in den Sink-Formaten des Renderers.",
                    "upnp", r.get("ended_at"), index=r["index"], name=name, mime=r.get("mime"))
        if snap["sink"]["state"] == "unavailable":
            add("warn", "sink_unavailable", "Sink-Formate des Renderers waren nicht lesbar.", "upnp", snap["sink"].get("at"),
                error=scrub(snap["sink"].get("error", ""), self.details))
        # Vor queue_start eingehende Ereignisse behalten die historische Prozess-Sicht;
        # sobald eine Queue existiert, werden nur noch deren Zaehler diagnostiziert.
        counters = snap.get("queue_counters", {}) if snap.get("queue") is not None else snap["counters"]
        for key, code, txt, cat in (("stop_failed", "stop_failed", "Stop am Renderer schlug fehl", "renderer"),
                                    ("cleanup_failed", "cleanup_failed", "Temporaere Datei konnte nicht geloescht werden", "cache")):
            n = counters.get(key, 0)
            if n:
                add("warn", code, f"{txt} ({n}x).", cat, count=n)
        n_retry = counters.get("renderer_retry", 0)
        if n_retry:
            recovered = counters.get("renderer_recovered", 0)
            add("warn", "renderer_retries", f"Renderer-Aufrufe wurden {n_retry}x wiederholt "
                f"({recovered}x danach erfolgreich).", "renderer",
                retries=n_retry, recovered=recovered)
        h = self.c.health.snapshot() if self.c.health is not None else None
        if h is not None:
            for comp, r in h["components"].items():
                if r["status"] in ("warn", "error"):
                    add(r["status"], f"health_{comp}", scrub(r["message"], self.details), _COMPONENT_CATEGORY.get(comp, "process"),
                        r.get("since"), error_category=r.get("category"), latency_ms=r.get("latency_ms"))
            if h["state"] == "stalled":
                add("warn", "health_monitor_stalled", "Die Gesundheitsueberwachung liefert keine aktuellen Werte.",
                    "diagnostics", last_tick_age_s=h["last_tick_age_s"], errors=h["errors"],
                    last_error=scrub(h["last_error"] or "", self.details))
            if h["errors"]:
                add("warn", "health_monitor_errors",
                    f"Die Gesundheitsueberwachung hat {h['errors']} interne Fehler registriert.",
                    "diagnostics", count=h["errors"],
                    last_error=scrub(h["last_error"] or "", self.details),
                    monitor_state=h["state"])
        guard = [i for i, p in snap["proc"].items() if p.get("guard_gain_db")]
        if guard:
            add("info", "clipping_guard", f"Clipping-Schutz war bei {len(guard)} Titel(n) aktiv.", "dsp", count=len(guard))
        if snap["dropped"]:
            add("info", "event_buffer_overflow",
                f"{snap['dropped']} aeltere Ereignisse wurden aus dem Puffer ({EVENT_BUFFER}) verdraengt.", "diagnostics", dropped=snap["dropped"])
        if snap["counters"].get("malformed_events"):
            add("warn", "malformed_events", "Ereignisse ohne gueltigen Titelindex wurden im Zustand nicht beruecksichtigt "
                "(sie stehen weiter unter /logs).", "diagnostics", count=snap["counters"]["malformed_events"])
        if snap["counters"].get("hook_errors"):
            add("warn", "collector_error", "Der Sammler konnte Ereignisse nicht verarbeiten.", "diagnostics",
                count=snap["counters"]["hook_errors"], last=scrub(snap["hook_error"] or "", self.details))
        if snap["api_errors"]:
            add("warn", "api_error", "Interne Fehler beim Beantworten von API-Anfragen.", "diagnostics",
                count=snap["api_errors"], last=scrub(snap["api_last_error"] or "", self.details))
        if live["enabled"] and live.get("ok") is False:
            add("warn", "live_probe_failed", "Die letzte Live-Abfrage des Renderers schlug fehl.", "renderer",
                error=scrub(live.get("error") or "", self.details))
        return out

    def diagnostics(self, snap: dict, live: dict, n_events: int = 100, final: bool = False) -> tuple[dict, dict]:
        u = _Unknown()
        st, us = self.status(snap, live)
        rn, ur = self.renderer(snap, live)
        ds, ud = self.dsp(snap)
        au, ua = self.audio(snap)
        for name, um in (("status", us), ("renderer", ur), ("dsp", ud), ("audio", ua)):
            u.merge(um, name)
        rs = snap["results"]
        reasons = collections.Counter(str(r.get("end_reason")) for r in rs)
        findings = self.findings(snap, live)
        diagnoses = diagnose(findings, mode="shadow")
        health_data, _ = self.health_status(snap)
        repair_decisions = decide_repairs(diagnoses, findings, health_data)
        counts = collections.Counter(f["level"] for f in findings)
        tracks = [self._track_summary(r, snap) for r in rs[-MAX_TRACKS_IN_DIAG:]]
        evs = [self._ev(e) for e in snap["events"][-n_events:]] if n_events > 0 else []
        data = {
            "final": final,
            "summary": {
                "phase": snap["phase"], "n_tracks_queue": (snap["queue"] or {}).get("n_tracks"),
                "tracks_finished": len(rs), "end_reasons": dict(reasons),
                "processed_tracks": sum(1 for r in rs if r.get("processed")),
                "findings": {"error": counts.get("error", 0), "warn": counts.get("warn", 0), "info": counts.get("info", 0)},
                "findings_by_category": dict(collections.Counter(f["category"] for f in findings)),
                "findings_by_scope": dict(collections.Counter(f["scope"] for f in findings)),
                "profile": (ds.get("profile") or {}).get("name"),
                "renderer_model": ((rn.get("identity") or {}).get("model_name")),
            },
            "findings": findings,
            "diagnoses": diagnoses,
            "repair_decisions": repair_decisions,
            "health": health_data,
            "timing": self.timing(snap),
            "status": st, "renderer": rn, "dsp": ds, "audio": au,
            "tracks": tracks, "tracks_omitted": max(0, len(rs) - len(tracks)),
            "events": evs,
            "environment": _environment(),
            "api": {"live_enabled": live["enabled"], "details_mode": self.details, "event_buffer": EVENT_BUFFER,
                    "events_total": snap["seq"], "events_dropped": snap["dropped"], "queues_seen": snap["queues_seen"],
                    "requests": snap["api_requests"], "started_at": snap["started_at"]},
            "notes": [
                "Alle Werte stammen aus Ereignissen dieses laufenden Prozesses; nichts aus alten Protokolldateien.",
                "Position, Pause und Transportstatus sind nur mit --api-live bekannt (sonst null, siehe unknown).",
                "Titel = Dateiname; Tags werden von der API nicht gelesen.",
                "Zeitwerte haben die Aufloesung der Player-Abfragen (timing.resolution_hint).",
                "Protokolltexte sind bereinigt (Pfade, IPs, URLs); Best Effort, vor dem Weitergeben pruefen.",
            ],
        }
        return data, u.map


def _environment() -> dict:
    env = {"python": platform.python_version(), "implementation": platform.python_implementation(),
           "os": f"{platform.system()} {platform.release()}", "machine": platform.machine()}
    try:
        import numpy

        env["numpy"] = numpy.__version__
    except Exception:  # noqa: BLE001
        pass
    try:
        import scipy

        env["scipy"] = scipy.__version__
    except Exception:  # noqa: BLE001
        pass
    try:
        import soundfile

        env["soundfile"] = soundfile.__version__
        env["libsndfile"] = soundfile.__libsndfile_version__
    except Exception:  # noqa: BLE001
        pass
    return env


# ---- Anfragen auswerten (ohne HTTP-Details) --------------------------------------------------------------------


class QueryError(ValueError):
    pass


def _parse_query(endpoint: str, qs: str) -> dict:
    allowed = {"pretty"}
    if endpoint == "logs":
        allowed |= {"limit", "since", "level", "category"}
    if endpoint == "diagnostics":
        allowed |= {"events"}
    try:
        parsed = urllib.parse.parse_qs(qs, keep_blank_values=True, max_num_fields=10)
    except ValueError:
        raise QueryError("zu viele oder ungueltige Parameter") from None
    for k, vs in parsed.items():
        if k not in allowed:
            raise QueryError(f"Unbekannter Parameter: {k[:40]}")
        if len(vs) != 1:
            raise QueryError(f"Parameter {k} mehrfach angegeben")

    def one(k, default):
        return parsed[k][0] if k in parsed else default

    def integer(k, default, lo, hi):
        raw = one(k, None)
        if raw is None:
            return default
        if not re.fullmatch(r"[0-9]{1,9}", raw):
            raise QueryError(f"{k} muss eine ganze Zahl sein")
        v = int(raw)
        if not lo <= v <= hi:
            raise QueryError(f"{k} muss zwischen {lo} und {hi} liegen")
        return v

    pretty = one("pretty", "0").lower()
    if pretty not in ("0", "1", "true", "false"):
        raise QueryError("pretty muss 0 oder 1 sein")
    q = {"pretty": pretty in ("1", "true")}
    if endpoint == "logs":
        level = one("level", "info")
        if level not in _LEVELS:
            raise QueryError("level muss info, warn oder error sein")
        category = one("category", None)
        if category is not None and category not in CATEGORIES:
            raise QueryError("category muss eine von " + ", ".join(CATEGORIES) + " sein")
        q.update(limit=integer("limit", 50, 1, MAX_LOG_LIMIT), since=integer("since", 0, 0, 10**9), level=level,
                 category=category)
    if endpoint == "diagnostics":
        q["events"] = integer("events", 100, 0, MAX_LOG_LIMIT)
    return q


_SHRINK_KEYS = ("tracks", "events", "recent", "applied_per_track")


def _shrink(obj, pretty: bool) -> bytes:
    """Antwort unter MAX_RESPONSE_BYTES halten (PowerShell 5.1). Kuerzt grosse Listen und kennzeichnet es."""
    body = encode_json(obj, pretty)
    for _ in range(12):
        if len(body) <= MAX_RESPONSE_BYTES:
            return body
        obj["truncated"] = True
        data = obj.get("data") or {}
        for k in _SHRINK_KEYS:
            if isinstance(data.get(k), list) and data[k]:
                data[k] = data[k][len(data[k]) // 2:]
        body = encode_json(obj, pretty)
    return body


def _envelope(endpoint: str, data: dict, unknown: dict, snap: dict, details: bool) -> dict:
    return {
        "api_version": API_VERSION,
        "kefplayer_version": __version__,
        "endpoint": endpoint,
        "generated_at": snap["now_iso"],
        "uptime_s": _round(snap["uptime_s"]),
        "read_only": True,
        "privacy": {
            "mode": "details" if details else "standard",
            "redacted": [] if details else ["Verzeichnispfade", "Renderer-Name/IP/UDN/URL", "Medienserver-Adresse",
                                            "IP-Adressen und Pfade in Fehlertexten"],
            "always_removed": ["URL-Token des Medienservers"],
        },
        "data": data,
        "unknown": unknown,
    }


INDEX_TEXT = {
    "status": "Wiedergabestatus: Phase, aktueller Titel, Dauer, Position (nur live)",
    "renderer": "Renderer: Modell, Verbindung, Dienste, Sink-Formate",
    "dsp": "DSP: Profil, Filter, Preamp, Kanalverzoegerungen, angewendete Werte je Titel",
    "audio": "Audio: Quellformat, Zielformat, Verarbeitungspfad",
    "logs": "Letzte Ereignisse und Fehler (limit, since, level)",
    "diagnostics": "Zusammenfassung fuer die technische Analyse (alle Bereiche, Befunde, Zeitstatistik)",
    "health": "Gesundheit: Renderer (HTTP/UPnP), Medienserver, Cache; Zustand und Ueberwachung",
}


def respond(collector: StatusCollector, path: str, query: str = "", final: bool = False) -> tuple[int, bytes]:
    """Antwort fuer einen Pfad bestimmen. Rueckgabe (HTTP-Status, JSON-Bytes). Wirft nur QueryError."""
    app = StatusApp(collector)
    norm = path.rstrip("/") or "/"
    if norm in ("/", "/api", "/api/v1"):
        endpoint = "index"
    elif norm.startswith("/api/v1/") and norm[len("/api/v1/"):] in ENDPOINTS:
        endpoint = norm[len("/api/v1/"):]
    else:
        return 404, encode_json({"api_version": API_VERSION, "error": {"status": 404, "message": "Unbekannter Pfad"},
                                 "endpoints": [f"/api/v1/{e}" for e in ENDPOINTS]})
    q = _parse_query(endpoint, query)
    pretty = q["pretty"]
    live = app._live(not final) if endpoint in ("status", "renderer", "diagnostics") else {"enabled": collector.live is not None}
    snap = collector.snapshot()  # nach der Live-Abfrage: ein Schnappschuss je Antwort
    if endpoint == "index":
        data = {"endpoints": {f"/api/v1/{e}": INDEX_TEXT[e] for e in ENDPOINTS},
                "methods": ["GET", "HEAD"], "port_note": "nur 127.0.0.1; ohne Steuerbefehle"}
        return 200, _shrink(_envelope("index", data, {}, snap, collector.details), pretty)
    if endpoint == "status":
        data, unk = app.status(snap, live)
    elif endpoint == "renderer":
        data, unk = app.renderer(snap, live)
    elif endpoint == "dsp":
        data, unk = app.dsp(snap)
    elif endpoint == "audio":
        data, unk = app.audio(snap)
    elif endpoint == "logs":
        data, unk = app.logs(snap, q["limit"], q["since"], q["level"], q["category"])
    elif endpoint == "health":
        data, unk = app.health_status(snap)
    else:
        data, unk = app.diagnostics(snap, live, q["events"], final=final)
    return 200, _shrink(_envelope(endpoint, data, unk, snap, collector.details), pretty)


def diagnostics_json(collector: StatusCollector, final: bool = True) -> bytes:
    """Diagnoseexport als gut lesbares JSON (ohne neue Renderer-Abfrage, wenn ``final``)."""
    return respond(collector, "/api/v1/diagnostics", "pretty=1&events=%d" % MAX_LOG_LIMIT, final=final)[1]


def write_diag_file(collector: StatusCollector, path) -> str:
    """Diagnoseexport atomar schreiben (temporaere Datei, dann ersetzen). Wirft OSError bei Schreibproblemen."""
    path = os.fspath(path)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(diagnostics_json(collector, final=True))
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return path


# ---- HTTP-Server -------------------------------------------------------------------------------------------------


def _build_503() -> bytes:
    body = encode_json({"api_version": API_VERSION,
                        "error": {"status": 503, "message": "Zu viele gleichzeitige Verbindungen"}})
    head = ("HTTP/1.0 503 Service Unavailable\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Retry-After: 1\r\n"
            "Connection: close\r\n\r\n")
    return head.encode("ascii") + body


_RESPONSE_503 = _build_503()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # Windows: SO_REUSEADDR erlaubt Doppelbelegung/Hijacking des Ports
    request_queue_size = 16

    def __init__(self, addr, handler, collector: StatusCollector):
        self.collector = collector
        self._sem = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._reject_sem = threading.BoundedSemaphore(MAX_REJECT_THREADS)
        super().__init__(addr, handler)

    def server_bind(self):  # HTTPServer.server_bind ruft getfqdn() auf: unter Windows teils traege, hier unnoetig
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):  # zu viele gleichzeitige Verbindungen
            self._reject(request)
            return
        super().process_request(request, client_address)

    def _reject(self, request) -> None:
        """Abweisen ohne den Annahme-Thread zu blockieren: 503 in einem kurzlebigen Hilfs-Thread senden.

        Der Annahme-Thread (serve_forever) tut hier nichts Blockierendes. Sind auch alle Hilfs-Plaetze belegt
        (MAX_REJECT_THREADS), wird die Verbindung sofort hart geschlossen: der Dienst bleibt nutzbar, ein Ansturm
        kann weder Threads noch Zeit des Servers binden."""
        if not self._reject_sem.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            threading.Thread(target=self._reject_worker, args=(request,), daemon=True,
                             name="kefplayer-status-api-reject").start()
        except Exception:  # noqa: BLE001 - z. B. keine Threads mehr: dann hart schliessen, nie haengen
            self._reject_sem.release()
            self.shutdown_request(request)

    def _reject_worker(self, request) -> None:
        """503 senden und die Verbindung *geordnet* beenden (Lingering Close), zeitlich und mengenmaessig begrenzt.

        Schliesst ein Server einen Socket, waehrend ungelesene Anfragedaten anstehen, oder bevor die Anfrage beim
        Server angekommen ist, antwortet sein TCP mit einem Reset (RST). Unter Windows verwirft der Client dann
        die bereits empfangene Antwort und meldet WSAECONNABORTED/WSAECONNRESET (10053/10054), schon beim Senden
        oder Lesen. Daher: Antwort senden, Schreibseite per FIN schliessen (der Client sieht das Antwort-Ende) und
        bis zu REJECT_LINGER_S weiterlesen, bis der Client selbst schliesst, hoechstens REJECT_DRAIN_MAX Bytes.
        Danach wird geschlossen, auch wenn der Client sich nicht kooperativ verhaelt."""
        try:
            deadline = time.monotonic() + REJECT_LINGER_S
            request.settimeout(REJECT_LINGER_S)
            request.sendall(_RESPONSE_503)
            request.shutdown(socket.SHUT_WR)
            drained = 0
            while drained < REJECT_DRAIN_MAX:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                request.settimeout(left)
                chunk = request.recv(4096)
                if not chunk:  # Client hat selbst geschlossen: sauberes Ende ohne Reset
                    break
                drained += len(chunk)
        except OSError:
            pass  # Client weg, Zeit abgelaufen oder Verbindung schon zurueckgesetzt: nichts mehr zu retten
        finally:
            try:
                self.close_request(request)
            finally:
                self._reject_sem.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()

    def handle_error(self, request, client_address):  # keine Tracebacks auf die Konsole des Players
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # eine Antwort je Verbindung: kein Festhalten durch traege Clients
    server_version = "kefplayer-status-api"
    sys_version = ""
    timeout = 5.0

    def version_string(self):
        return self.server_version

    def log_message(self, *args):  # still: nichts auf stdout/stderr des Players
        pass

    # -- Antworten --
    def _write(self, status: int, body: bytes, extra: dict | None = None) -> None:
        # Nach einer fehlerhaften Request-Zeile bleibt request_version in Python < 3.13 auf dem Vorgabewert
        # "HTTP/0.9" stehen. http.server unterdrueckt dann Statuszeile und Kopfzeilen und sendet nur den Body.
        # Diese API antwortet immer mit einer vollstaendigen HTTP/1.0-Antwort (auch auf echte 0.9-Anfragen).
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass  # Client weg
        self.close_connection = True

    def _error(self, status: int, message: str, extra: dict | None = None) -> None:
        body = encode_json({"api_version": API_VERSION, "error": {"status": status, "message": message}})
        self._write(status, body, extra)

    def send_error(self, code, message=None, explain=None):  # JSON statt HTML; unbekannte Methoden -> 405
        if code == 501:
            return self._error(405, "Nur GET und HEAD erlaubt (nur lesende API)", {"Allow": "GET, HEAD"})
        try:
            short = HTTPStatus(code).phrase
        except ValueError:
            short = "Fehler"
        self._error(int(code), short)

    def _method_not_allowed(self):
        self.server.collector.count_request("rejected_method")
        self._error(405, "Nur GET und HEAD erlaubt (nur lesende API)", {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    # -- Pruefungen --
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in ("127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            h = urllib.parse.urlsplit(origin).hostname
        except ValueError:
            return False
        return h in ("127.0.0.1", "localhost")

    def _handle(self):
        c: StatusCollector = self.server.collector
        c.count_request("total")
        try:
            if not self._host_ok():
                c.count_request("rejected_host")
                return self._error(403, "Host-Header nicht erlaubt (nur 127.0.0.1/localhost)")
            if not self._origin_ok():
                c.count_request("rejected_origin")
                return self._error(403, "Cross-Site-Anfrage abgelehnt")
            if not self.path.startswith("/"):
                return self._error(400, "Ungueltiger Pfad")
            parts = urllib.parse.urlsplit(self.path)
            status, body = respond(c, parts.path, parts.query)
            self._write(status, body)
        except QueryError as e:
            c.count_request("bad_query")
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 - nie einen Traceback oder Interna ausliefern
            c.note_api_error(f"{type(e).__name__}: {e}")
            self._error(500, "Interner Fehler")


class StatusApiServer:
    """Startet/stoppt den lokalen Server. Nur 127.0.0.1. ``start()`` wirft OSError, wenn der Port belegt ist."""

    def __init__(self, collector: StatusCollector, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
        if host != "127.0.0.1":
            raise ValueError("Die Status-API bindet ausschliesslich an 127.0.0.1")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Port muss 0..65535 sein")
        self.collector = collector
        self.host = host
        self._port = port
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self._port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1/diagnostics"

    def start(self) -> "StatusApiServer":
        if self._httpd is not None:
            return self
        self._httpd = _Server((self.host, self._port), _Handler, self.collector)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True, name="kefplayer-status-api")
        self._thread.start()
        return self

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def compose_hooks(*hooks):
    """Mehrere Ereignis-Rueckrufe hintereinander. Der Sammler steht zuerst und wirft nie."""
    hooks = [h for h in hooks if h is not None]

    def run(rec):
        for h in hooks:
            h(rec)

    return run
