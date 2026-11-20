"""Steuerlogik der GUI, ohne GUI-Toolkit (headless testbar).

Die View liest nur Meldungen aus ``Controller.messages`` (``drain()``) und ruft die Befehle auf. Nichts hier
importiert tkinter. Der Kern (Player, DSP, Audio, UPnP, Medienserver) wird unveraendert benutzt.

Threads: Suche, Verbinden, Wiedergabe und Verarbeiten laufen in eigenen Threads. Meldungen gehen ausschliesslich
ueber die Queue; die View darf nie aus anderen Threads auf ihre Widgets zugreifen.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import audio, dsp, upnp
from ..mediaserver import MediaServer, local_ip_for
from ..player import AUDIO_EXTS, Player
from ..runlog import RunLog
from . import texts

STATE_IDLE = "idle"
STATE_BUSY = "busy"  # Suche/Verbinden/Pruefen
STATE_PLAYING = "playing"
STATE_STOPPING = "stopping"
STATE_PROCESSING = "processing"

FAIL_REASONS = ("error", "never_started", "timeout", "prepare_error")


@dataclass
class Msg:
    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class Item:
    path: Path
    ok: bool
    info: dict | None = None
    error: str | None = None


@dataclass
class Settings:
    """Standardwerte wie in der Kommandozeile."""

    fmt: str | None = None  # None = nicht umwandeln (nur bei neutralem Profil relevant)
    bits: int | None = None
    dither: bool = True
    always_process: bool = False
    cache_dir: str | None = None
    log_path: str | None = "kefplayer_log.jsonl"
    poll_s: float = 0.5
    bind: str = "0.0.0.0"
    port: int = 8765
    host_ip: str | None = None
    plain_protocolinfo: bool = False
    mime: str | None = None
    bind_ip: str | None = None  # eigene IP fuer die Suche
    discover_timeout: float = 3.0
    player_extra: dict = field(default_factory=dict)  # nur fuer Tests (z. B. end_tolerance_s)


class Controller:
    def __init__(self, settings: Settings | None = None, *, ssdp_search=None):
        self.settings = settings or Settings()
        self._ssdp = ssdp_search or upnp.ssdp_search
        self.messages: queue.Queue[Msg] = queue.Queue()
        self._lock = threading.RLock()
        self._state = STATE_IDLE
        self._threads: list[threading.Thread] = []
        self.items: list[Item] = []
        self.profile = dsp.Profile()
        self.profile_path: str | None = None
        self.profile_notes: list[str] = []
        self.profile_warnings: list[str] = []
        self.devices: dict[str, upnp.Renderer] = {}
        self.renderer: upnp.Renderer | None = None
        self._player: Player | None = None
        self._stop_requested = False
        self._cancel_process = False
        self.now: dict | None = None
        self.results: list[dict] = []

    # ---- Meldungen -----------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        self.messages.put(Msg(kind, data))

    def drain(self) -> list[Msg]:
        out: list[Msg] = []
        while True:
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                return out

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, state: str) -> None:
        with self._lock:  # Zustand und Meldung unter demselben Schloss: Reihenfolge in der Queue = Reihenfolge der Wechsel
            if self._state == state:
                return
            self._state = state
            self._emit("state", state=state)

    def _begin(self, state: str) -> bool:
        """Zustand nur aus 'idle' heraus wechseln; sonst Fehlermeldung."""
        with self._lock:
            if self._state != STATE_IDLE:
                self._emit("error", text=texts.ERR_BUSY.format(state=self._state))
                return False
            self._state = state
            self._emit("state", state=state)
            return True

    def _spawn(self, name: str, target, *args) -> None:
        def run():
            try:
                target(*args)
            except Exception as e:  # noqa: BLE001 - jede Ursache melden, nie ein Traceback in der GUI
                self._emit("error", text=f"{type(e).__name__}: {e}")
            finally:
                self._set_state(STATE_IDLE)

        t = threading.Thread(target=run, name=f"kefplayer-gui-{name}", daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    # ---- Warteschlange -------------------------------------------------------
    def add_paths(self, paths) -> int:
        """Dateien/Ordner anhaengen. Rueckgabe: Anzahl neu hinzugefuegter Eintraege. Fehler als Meldung 'warning'."""
        added = 0
        for p in paths:
            p = Path(p)
            if p.is_dir():
                cand = sorted((f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
                              key=lambda f: str(f).lower())
            elif p.is_file():
                if p.suffix.lower() not in AUDIO_EXTS:
                    self._emit("warning", text=texts.ERR_NOT_AUDIO.format(name=p.name))
                    continue
                cand = [p]
            else:
                self._emit("warning", text=f"Datei oder Ordner nicht gefunden: {p}")
                continue
            for f in cand:
                try:
                    item = Item(f, True, audio.probe(f).to_dict())
                except Exception as e:  # noqa: BLE001
                    item = Item(f, False, None, str(e))
                    self._emit("warning", text=texts.ERR_UNREADABLE.format(name=f.name, error=e))
                with self._lock:
                    self.items.append(item)
                added += 1
        self._emit("queue_changed", count=len(self.items))
        return added

    def remove(self, index: int) -> None:
        with self._lock:
            if self._state == STATE_PLAYING:
                self._emit("error", text="Die Warteschlange ist waehrend der Wiedergabe gesperrt.")
                return
            if 0 <= index < len(self.items):
                del self.items[index]
        self._emit("queue_changed", count=len(self.items))

    def move(self, index: int, delta: int) -> None:
        with self._lock:
            if self._state == STATE_PLAYING:
                self._emit("error", text="Die Warteschlange ist waehrend der Wiedergabe gesperrt.")
                return
            j = index + delta
            if 0 <= index < len(self.items) and 0 <= j < len(self.items):
                self.items[index], self.items[j] = self.items[j], self.items[index]
        self._emit("queue_changed", count=len(self.items))

    def clear(self) -> None:
        with self._lock:
            if self._state == STATE_PLAYING:
                self._emit("error", text="Die Warteschlange ist waehrend der Wiedergabe gesperrt.")
                return
            self.items.clear()
        self._emit("queue_changed", count=0)

    def playable(self) -> list[Path]:
        with self._lock:
            return [i.path for i in self.items if i.ok]

    # ---- Profil -----------------------------------------------------------------
    def load_profile(self, path: str) -> bool:
        """Profil laden. Bei Fehler bleibt das bisherige Profil erhalten."""
        try:
            prof = dsp.Profile.load(path)
        except Exception as e:  # noqa: BLE001 - ValueError, OSError, JSON-Fehler
            self._emit("error", text=f"Profil nicht geladen: {e}")
            return False
        with self._lock:
            self.profile, self.profile_path = prof, str(path)
            self.profile_notes = list(prof.notes)
            self.profile_warnings = prof.validate()
        self._emit("profile", **self.profile_info())
        return True

    def set_neutral(self) -> None:
        with self._lock:
            self.profile, self.profile_path = dsp.Profile(), None
            self.profile_notes, self.profile_warnings = [], []
        self._emit("profile", **self.profile_info())

    def profile_info(self) -> dict:
        with self._lock:
            p = self.profile
            return {
                "name": p.name,
                "path": self.profile_path,
                "neutral": p.is_neutral(),
                "preamp": "auto" if p.preamp_db is None else p.preamp_db,
                "filters": [f.to_dict() for f in p.filters],
                "channels": {("L" if k == 0 else "R"): {"gain_db": c.gain_db, "delay_ms": c.delay_ms,
                                                       "filters": [f.to_dict() for f in c.filters]}
                             for k, c in p.channels.items()},
                "notes": list(self.profile_notes),
                "warnings": list(self.profile_warnings),
                "signalpath_hint": texts.SIGNALPATH_HINT,
                "summary": self.summary(),
            }

    def summary(self) -> str:
        s = self.settings
        with self._lock:
            p = self.profile
            if p.is_neutral():
                if s.always_process or s.fmt is not None or s.bits is not None:
                    what = ", ".join(x for x in (
                        f"Format {s.fmt}" if s.fmt else None, f"{s.bits} Bit" if s.bits else None,
                        "immer neu schreiben" if s.always_process else None) if x)
                    return texts.NEUTRAL_CONVERT_SUMMARY.format(what=what)
                return texts.NEUTRAL_SUMMARY
            return texts.PROFILE_SUMMARY.format(
                name=p.name, preamp="auto" if p.preamp_db is None else f"{p.preamp_db:+g} dB")

    # ---- Geraet -------------------------------------------------------------------
    def search(self) -> bool:
        if not self._begin(STATE_BUSY):
            return False
        self._spawn("search", self._search_worker)
        return True

    def _search_worker(self) -> None:
        s = self.settings
        found = self._ssdp(timeout=s.discover_timeout, bind_ip=s.bind_ip)
        devices: dict[str, upnp.Renderer] = {}
        rows = []
        for f in found:
            loc = f["location"]
            try:
                r = upnp.Renderer.from_location(loc)
                name = r.info.friendly_name or r.info.host
                devices[loc] = r
            except Exception as e:  # noqa: BLE001 - Geraet melden, aber nicht verbinden
                name = f"{f.get('_from', loc)} (Beschreibung nicht lesbar: {e})"
            rows.append({"location": loc, "from": f.get("_from"), "name": name, "usable": loc in devices})
        with self._lock:
            self.devices = devices
        self._emit("devices", devices=rows, hint=None if rows else texts.NO_RENDERER_HINT)

    def connect(self, location: str) -> bool:
        """Geraet waehlen: Adresse aus der Suche oder direkt eingegebene Beschreibungs-URL (http://...)."""
        if not self._begin(STATE_BUSY):
            return False
        self._spawn("connect", self._connect_worker, location)
        return True

    def _connect_worker(self, location: str) -> None:
        with self._lock:
            r = self.devices.get(location)
        if r is None:
            r = upnp.Renderer.from_location(location)
        with self._lock:
            self.renderer = r
        self._emit("device", location=location, name=r.info.friendly_name, host=r.info.host,
                   manufacturer=r.info.manufacturer, model=r.info.model_name)

    def probe(self) -> bool:
        """Nur lesend: gemeldete Formate und Transportstatus (Eigenangabe des Geraets, kein Wiedergabetest)."""
        if self.renderer is None:
            self._emit("error", text=texts.ERR_NO_DEVICE)
            return False
        if not self._begin(STATE_BUSY):
            return False
        self._spawn("probe", self._probe_worker, self.renderer)
        return True

    def _probe_worker(self, r: upnp.Renderer) -> None:
        try:
            mimes = r.sink_mimes()
            err = None
        except Exception as e:  # noqa: BLE001
            mimes, err = [], str(e)
        self._emit("probe", mimes=mimes, error=err, transport_state=r.transport_state() if err is None else None)

    # ---- Wiedergabe ----------------------------------------------------------------
    def start(self) -> bool:
        if self.renderer is None:
            self._emit("error", text=texts.ERR_NO_DEVICE)
            return False
        paths = self.playable()
        if not paths:
            self._emit("error", text=texts.ERR_EMPTY_QUEUE)
            return False
        if not self._begin(STATE_PLAYING):
            return False
        with self._lock:
            self._stop_requested = False
            self.results = []
            self.now = None
            profile = self.profile
        self._spawn("play", self._play_worker, paths, profile, self.renderer)
        return True

    def _on_player_event(self, rec: dict) -> None:
        ev = rec.get("event")
        with self._lock:
            if ev == "queue_start":
                self.now = {"total": len(rec.get("tracks", [])), "index": None, "name": None, "started": None}
            elif ev == "track_start" and self.now is not None:
                self.now.update(index=rec["index"], name=Path(rec["path"]).name, started=time.monotonic())
            elif ev == "track_end":
                self.results.append({k: rec.get(k) for k in (
                    "index", "path", "end_reason", "startup_delay_s", "gap_before_s", "ended_early", "error")})
        self._emit("player_event", event=rec)
        if ev == "track_end":
            hint = self._hint_for(rec)
            if hint:
                self._emit("warning", text=hint)

    @staticmethod
    def _hint_for(rec: dict) -> str | None:
        reason = rec.get("end_reason")
        if reason == "never_started" and not rec.get("requests"):
            return texts.FIREWALL_HINT
        if reason == "error":
            return texts.REJECTED_HINT
        return None

    def _play_worker(self, paths: list[Path], profile: dsp.Profile, renderer: upnp.Renderer) -> None:
        s = self.settings
        host_ip = s.host_ip or local_ip_for(renderer.info.host)
        server = MediaServer(bind=s.bind, port=s.port, advertise_host=host_ip)
        try:
            server.start()
        except OSError as e:
            self._emit("error", text=texts.ERR_SERVER.format(error=e))
            return
        try:
            player = Player(
                renderer, server, profile,
                fmt=s.fmt, bits=s.bits, dither=s.dither, cache_dir=s.cache_dir, log=RunLog(s.log_path),
                on_event=self._on_player_event, poll_s=s.poll_s, serve_original_if_neutral=not s.always_process,
                plain_protocolinfo=s.plain_protocolinfo, mime_override=s.mime, **s.player_extra,
            )
            with self._lock:
                self._player = player
                stop_now = self._stop_requested
            if stop_now:
                player.stop()
            results = player.play_queue(paths)
        finally:
            with self._lock:
                self._player = None
            server.stop()
        bad = [r for r in results if r.get("end_reason") not in ("ended", "skipped", "stopped")]
        self._emit("playback_done", played=len(results), problems=len(bad),
                   reasons=[r.get("end_reason") for r in results], log=s.log_path)

    def skip(self) -> None:
        with self._lock:
            p = self._player
        if p is not None:
            p.skip()

    def toggle_pause(self) -> None:
        """Sendet nur die Anforderung. Ob der Renderer wirklich pausiert, meldet der Player nicht (siehe Konzept K2)."""
        with self._lock:
            p = self._player
        if p is not None:
            p.toggle_pause()
            self._emit("pause_requested")

    def stop(self) -> None:
        with self._lock:
            p = self._player
            self._stop_requested = True
            self._cancel_process = True
            switched = self._state == STATE_PLAYING
            if switched:  # unter dem Schloss, damit ein gleichzeitig endender Thread 'idle' nicht ueberschreibt
                self._state = STATE_STOPPING
                self._emit("state", state=STATE_STOPPING)
        if p is not None:
            p.stop()

    # ---- Verarbeiten (process) ------------------------------------------------------
    def process(self, out_dir: str, *, fmt: str = "flac", bits: int | None = None, dither: bool = True,
                numbered: bool = False, overwrite: bool = False) -> bool:
        paths = self.playable()
        if not paths:
            self._emit("error", text=texts.ERR_EMPTY_QUEUE)
            return False
        # Wie die CLI: Vorabpruefung (doppelte Zielnamen, Quelle == Ziel, vorhandene Dateien), bevor etwas geschrieben wird
        targets = [audio.output_path(f, out_dir, fmt, f"{n:03d}_{f.stem}" if numbered else None) for n, f in enumerate(paths)]
        problems = audio.process_target_problems(paths, targets, overwrite=overwrite)
        if problems:
            self._emit("error", text=texts.ERR_PROCESS_TARGETS + "\n" + "\n".join(f"  {p}" for p in problems))
            return False
        if not self._begin(STATE_PROCESSING):
            return False
        with self._lock:
            self._cancel_process = False
            profile = self.profile
        self._spawn("process", self._process_worker, paths, Path(out_dir), profile, fmt, bits, dither, numbered, overwrite)
        return True

    def _process_worker(self, paths, out_dir, profile, fmt, bits, dither, numbered, overwrite=False) -> None:
        ok = failed = 0
        for n, f in enumerate(paths):
            if self._cancel_process:
                break
            try:
                rep = audio.process_file(f, out_dir, profile, fmt=fmt, bits=bits, dither=dither,
                                         stem=f"{n:03d}_{f.stem}" if numbered else None, overwrite=overwrite)
            except Exception as e:  # noqa: BLE001 - je Datei melden, weiter mit der naechsten (Konzept G6)
                failed += 1
                self._emit("process_item", index=n, src=str(f), ok=False, error=str(e))
                continue
            ok += 1
            self._emit("process_item", index=n, src=str(f), ok=True, report=rep)
        self._emit("process_done", ok=ok, failed=failed, total=len(paths), cancelled=self._cancel_process)

    # ---- Beenden ------------------------------------------------------------------------
    def shutdown(self, timeout: float = 10.0) -> bool:
        """Laufende Vorgaenge beenden und auf die Threads warten. Rueckgabe: True, wenn alles beendet ist."""
        self.stop()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads)
