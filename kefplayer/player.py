"""Wiedergabe-Warteschlange: vorbereiten (optional filtern), bereitstellen, Renderer steuern, protokollieren.

Gemessen und protokolliert wird pro Titel: gesendetes Format, Startverzoegerung (Play -> PLAYING),
Luecke zum Vorgaenger (Ende erkannt -> naechster PLAYING), Anfragen des Renderers an den Medienserver.
Aufloesung dieser Zeiten = Abfrageintervall (poll_s).
"""

from __future__ import annotations

import re
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from . import audio, dsp, upnp
from .mediaserver import MediaServer
from .runlog import RunLog

# Hochaufloesende, monotone Uhr fuer alle Laufzeitmessungen dieses Moduls (t_play, t_playing, now, t_end).
# time.monotonic() hat unter Windows mit CPython <= 3.12 nur 15,6 ms Aufloesung (GetTickCount64).
_clock = time.perf_counter

AUDIO_EXTS = {".flac", ".wav", ".ogg", ".aif", ".aiff", ".mp3"}


def classify_error(exc: BaseException) -> str:
    """Grobe Fehlerkategorie fuer Renderer-Kommunikation (nur Einordnung des Symptoms, keine Ursachenbehauptung)."""
    if isinstance(exc, upnp.UpnpError):
        if exc.code is not None:
            return "renderer_soap_fault"
        if exc.description.startswith("HTTP "):
            return "renderer_http_error"
        return "renderer_bad_response"
    reason = getattr(exc, "reason", None)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return "renderer_timeout"
    if isinstance(exc, OSError):
        return "renderer_unreachable"
    return "unknown"


TRANSIENT_CATEGORIES = ("renderer_timeout", "renderer_unreachable")


def classify_prepare_error(exc: BaseException) -> str:
    """Fehlerkategorie fuer das Vorbereiten einer Datei (Lesen, Filtern, Schreiben)."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return "source_file_access"
    if isinstance(exc, sf.LibsndfileError):
        return "source_format"
    if isinstance(exc, OSError):
        return "cache_io"
    return "processing_error"


def collect_files(paths) -> list[Path]:
    """Dateien und Ordner (rekursiv, nach Pfad sortiert) einsammeln."""
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += sorted((f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS), key=lambda f: str(f).lower())
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(f"Datei oder Ordner nicht gefunden: {p}")
    return out


def _safe(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:60] or "track"


@dataclass
class Track:
    path: Path
    title: str
    artist: str | None = None


def read_track(path: Path) -> Track:
    title, artist = path.stem, None
    try:
        with sf.SoundFile(str(path)) as f:
            title = f.title or title
            artist = f.artist or None
    except Exception:
        pass
    return Track(path, title, artist)


@dataclass
class Prepared:
    path: Path
    mime: str
    in_sink: bool | None
    info: audio.AudioInfo | None
    report: dict | None


class Player:
    def __init__(
        self,
        renderer: upnp.Renderer,
        server: MediaServer,
        profile: dsp.Profile | None = None,
        *,
        fmt: str | None = None,
        bits: int | None = None,
        dither: bool = True,
        cache_dir: str | Path | None = None,
        log: RunLog | None = None,
        on_event=None,
        poll_s: float = 0.5,
        start_timeout_s: float = 20.0,
        serve_original_if_neutral: bool = True,
        plain_protocolinfo: bool = False,
        mime_override: str | None = None,
        end_tolerance_s: float = 2.0,
        fast_poll_s: float = 0.05,
        end_window_s: float = 3.0,
        retry_attempts: int = 3,
        retry_backoff_s: float = 0.5,
    ):
        self.renderer = renderer
        self.server = server
        self.profile = profile or dsp.Profile()
        self.fmt = fmt or "FLAC"
        self.bits = bits
        # Wer Format oder Bittiefe ausdruecklich verlangt, will eine Umwandlung, auch bei neutralem Profil
        self.convert_requested = fmt is not None or bits is not None
        self.dither = dither
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.log = log or RunLog(None)
        self.on_event = on_event
        self.poll_s = poll_s
        self.start_timeout_s = start_timeout_s
        self.serve_original = serve_original_if_neutral
        self.plain = plain_protocolinfo
        self.mime_override = mime_override
        self.end_tolerance_s = end_tolerance_s
        # Schnell abfragen, solange auf den Start gewartet wird und kurz vor dem Titelende: kleinere Luecke,
        # genauere Messwerte. Sonst genuegt das langsamere poll_s.
        self.fast_poll_s = min(fast_poll_s, poll_s)
        self.end_window_s = end_window_s
        # Begrenzte Wiederholung nur bei voruebergehenden Verbindungsfehlern (Timeout, nicht erreichbar)
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.sink_mimes: list[str] = []
        self._stop = threading.Event()
        self.track_active = threading.Event()  # gesetzt waehrend der gesamten Renderer-Wiedergabesitzung
        self._skip = threading.Event()
        self._pause = threading.Event()
        self._cache: Path | None = None
        self._own_cache = False

    # ---- Steuerung (aus anderen Threads aufrufbar) -------------------------
    def skip(self) -> None:
        self._skip.set()

    def stop(self) -> None:
        self._stop.set()

    def toggle_pause(self) -> None:
        self._pause.set()

    def _call(self, action: str, fn, *args, attempts: int | None = None):
        """Renderer-Aufruf mit begrenzter Wiederholung (Backoff 1x, 2x, 4x ...) nur bei Timeout/Nichterreichbarkeit.
        SOAP-Fehler und HTTP-Fehler des Renderers werden nicht wiederholt (deterministisch). Waehrend ein Stopp
        angefordert ist, wird nicht wiederholt (ausser beim Stop selbst). Der letzte Fehler wird unveraendert gereicht."""
        n = max(1, attempts if attempts is not None else self.retry_attempts)
        for k in range(1, n + 1):
            try:
                result = fn(*args)
            except (upnp.UpnpError, OSError) as e:
                cat = classify_error(e)
                if k >= n or cat not in TRANSIENT_CATEGORIES or (self._stop.is_set() and action != "stop"):
                    raise
                wait = self.retry_backoff_s * 2 ** (k - 1)
                self._emit("renderer_retry", action=action, attempt=k, of=n, category=cat, error=str(e), wait_s=wait)
                time.sleep(wait)
                continue
            if k > 1:
                self._emit("renderer_recovered", action=action, attempts=k)
            return result

    def emit(self, event: str, **kw) -> None:
        """Ereignis ueber denselben Weg senden wie der Player selbst (RunLog und Rueckruf)."""
        self._emit(event, **kw)

    def _emit(self, event: str, **kw) -> None:
        rec = self.log.write(event, **kw)
        if self.on_event:
            self.on_event(rec)

    # ---- Ablauf -------------------------------------------------------------
    def play_queue(self, paths) -> list[dict]:
        tracks = [read_track(Path(p)) for p in paths]
        results: list[dict] = []
        tmp = None
        if self.cache_dir is None:
            tmp = tempfile.TemporaryDirectory(prefix="kefplayer_", ignore_cleanup_errors=True)
            self._cache, self._own_cache = Path(tmp.name), True
        else:
            self._cache, self._own_cache = self.cache_dir, False
        self.track_active.set()
        upnp.RENDERER_LOCK.acquire()
        try:
            self._emit(
                "queue_start",
                tracks=[str(t.path) for t in tracks],
                profile=self.profile.to_dict(),
                profile_neutral=self.profile.is_neutral(),
                renderer=self.renderer.info.friendly_name,
                poll_s=self.poll_s,
            )
            self._load_sink()
            with ThreadPoolExecutor(max_workers=1) as pool:
                futs: dict[int, object] = {}

                def submit(k: int) -> None:
                    if k < len(tracks) and k not in futs:
                        futs[k] = pool.submit(self._prepare, k, tracks[k])

                submit(0)
                prev_end = None
                for i, tr in enumerate(tracks):
                    if self._stop.is_set():
                        break
                    submit(i + 1)  # naechsten Titel waehrend der Wiedergabe vorbereiten
                    try:
                        prep = futs[i].result()
                    except Exception as e:  # noqa: BLE001 - jede Ursache protokollieren, Warteschlange laeuft weiter
                        res = {"index": i, "path": str(tr.path), "end_reason": "prepare_error", "error": str(e),
                               "error_category": classify_prepare_error(e)}
                        self._emit("track_end", **res)
                        results.append(res)
                        continue
                    res = self._play_one(i, tr, prep, prev_end)
                    prev_end = res.pop("_t_end", None)
                    results.append(res)
                    if self._own_cache and prep.report is not None:
                        self._remove(prep.path)
        finally:
            try:
                self._emit("queue_end", played=len(results), reasons=[r.get("end_reason") for r in results])
                if tmp:
                    tmp.cleanup()
            finally:
                upnp.RENDERER_LOCK.release()
                self.track_active.clear()
        return results

    def _load_sink(self) -> None:
        try:
            infos = self.renderer.sink_protocol_infos()
        except (upnp.UpnpError, OSError) as e:
            self._emit("sink_unavailable", error=str(e))
            return
        mimes: list[str] = []
        for entry in infos:
            parts = entry.split(":")
            if len(parts) >= 3 and parts[2] not in mimes:
                mimes.append(parts[2])
        self.sink_mimes = mimes
        self._emit("sink", protocol_infos=infos, mimes=mimes)

    def _prepare(self, i: int, track: Track) -> Prepared:
        if self.profile.is_neutral() and self.serve_original and not self.convert_requested:
            path, report = track.path, None
        else:
            report = audio.process_file(
                track.path, self._cache, self.profile,
                fmt=self.fmt, bits=self.bits, dither=self.dither, stem=f"{i:03d}_{_safe(track.path.stem)}",
            )
            path = Path(report["dst"])
            self._emit("track_processed", index=i, **{k: report[k] for k in ("src", "preamp_db", "peak_in_dbfs", "peak_out_dbfs", "guard_gain_db", "warnings", "elapsed_s")})
        try:
            info = audio.probe(path)
        except Exception:  # noqa: BLE001 - z. B. Format, das libsndfile nicht liest; wird dann unveraendert gereicht
            info = None
        if self.mime_override:
            mime, in_sink = self.mime_override, None
        else:
            mime, in_sink = upnp.pick_mime(path.suffix, self.sink_mimes, info.format if info else None)
        return Prepared(path, mime, in_sink, info, report)

    def _remove(self, path: Path) -> None:
        """Temporaere Datei loeschen. Schlaegt das fehl (unter Windows z. B. durch Virenscanner oder noch
        offene Verbindung), bleibt die Datei bis zum Aufraeumen am Ende liegen und die Warteschlange laeuft weiter."""
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            self._emit("cleanup_failed", path=str(path), error=str(e))

    def _safe_stop(self) -> None:
        try:
            self._call("stop", self.renderer.stop, attempts=min(2, self.retry_attempts))
        except (upnp.UpnpError, OSError) as e:
            self._emit("stop_failed", error=str(e))

    def _play_one(self, i: int, track: Track, prep: Prepared, prev_end: float | None) -> dict:
        info = prep.info
        duration = info.duration_s if info else None
        ext = (upnp.ext_for_format(info.format) if info else None) or prep.path.suffix.lower()
        name = f"{i:03d}_{_safe(track.path.stem)}{ext}"
        url, token = self.server.register(prep.path, prep.mime, name=name)
        didl = upnp.build_didl(
            track.title, url, prep.mime,
            artist=track.artist,
            duration_s=duration,
            samplerate=info.samplerate if info else None,
            channels=info.channels if info else None,
            bits=info.bits if info else None,
            size=prep.path.stat().st_size,
            plain=self.plain,
        )
        res: dict = {
            "index": i,
            "path": str(track.path),
            "served": str(prep.path),
            "processed": prep.report is not None,
            "mime": prep.mime,
            "mime_in_sink": prep.in_sink,
            "served_format": info.to_dict() if info else None,
        }
        self._emit("track_start", **res)
        try:
            self._call("set_uri", self.renderer.set_uri, url, didl)
            t_play = _clock()
            self._call("play", self.renderer.play)
            res.update(self._wait(t_play, duration))
        except (upnp.UpnpError, OSError) as e:
            res.update({"end_reason": "error", "error": str(e), "error_category": classify_error(e)})
            self._safe_stop()
        t_playing = res.pop("_t_playing", None)
        if t_playing is not None and prev_end is not None:
            res["gap_before_s"] = round(t_playing - prev_end, 3)
        res["requests"] = [
            {k: r[k] for k in ("client", "method", "range", "status", "bytes_sent")} for r in self.server.requests_for(token)
        ]
        self.server.unregister(token)
        if res.get("end_reason") == "never_started":
            # Einordnung aus vorhandenen Daten: hat der Renderer die Datei beim Medienserver ueberhaupt angefragt?
            res["error_category"] = "renderer_fetched_no_play" if res["requests"] else "renderer_never_fetched"
        elif res.get("end_reason") == "timeout":
            res["error_category"] = "playback_timeout"
        self._emit("track_end", **{k: v for k, v in res.items() if k != "_t_end"})
        return res

    def _wait(self, t_play: float, duration: float | None) -> dict:
        r = self.renderer
        t_playing: float | None = None
        last_pos: float | None = None
        paused_total = 0.0
        paused_since: float | None = None
        errors = 0
        max_wall = duration * 2 + 60 if duration else 3600.0

        def finish(reason: str, **extra) -> dict:
            t_end = _clock()
            out = {
                "end_reason": reason,
                "startup_delay_s": round(t_playing - t_play, 3) if t_playing is not None else None,
                "wall_s": round(t_end - t_play, 3),
                "last_position_s": last_pos,
                "ended_early": bool(
                    reason == "ended" and duration and last_pos is not None
                    and last_pos < duration - max(self.end_tolerance_s, 3 * self.poll_s)
                ),
                "_t_playing": t_playing,
                "_t_end": t_end,
            }
            out.update(extra)
            return out

        while True:
            now = _clock()
            if self._stop.is_set():
                self._safe_stop()
                return finish("stopped")
            if self._skip.is_set():
                self._skip.clear()
                self._safe_stop()
                return finish("skipped")
            try:
                if self._pause.is_set():
                    self._pause.clear()
                    state = r.transport_state()
                    if state == "PLAYING":
                        r.pause()
                        paused_since = now
                    elif state == "PAUSED_PLAYBACK":
                        r.play()
                        if paused_since is not None:
                            paused_total += now - paused_since
                        paused_since = None
                # Erst Position, dann Status: Nur wenn der Status danach noch PLAYING ist, war die Position
                # gueltig. Umgekehrt liefert ein Renderer, der zwischen den zwei Abfragen stoppt, Position 0
                # und das Titelende wuerde faelschlich als "zu frueh" gemeldet.
                try:
                    pos = r.position_s()
                except upnp.UpnpError:
                    pos = None  # manche Renderer melden ausserhalb von PLAYING einen Fehler
                state = r.transport_state()
                if state == "PLAYING":
                    if t_playing is None:
                        t_playing = now
                    if pos is not None:
                        last_pos = pos
                elif state in ("STOPPED", "NO_MEDIA_PRESENT") and t_playing is not None:
                    return finish("ended")
                errors = 0
            except (upnp.UpnpError, OSError) as e:
                errors += 1
                if errors >= 5:
                    self._safe_stop()
                    return finish("error", error=str(e), error_category=classify_error(e))
            elapsed = now - t_play - paused_total - ((now - paused_since) if paused_since is not None else 0.0)
            if t_playing is None and elapsed > self.start_timeout_s:
                self._safe_stop()
                return finish("never_started")
            if elapsed > max_wall:
                self._safe_stop()
                return finish("timeout")
            near_end = duration is not None and last_pos is not None and last_pos >= duration - self.end_window_s
            time.sleep(self.fast_poll_s if (t_playing is None or near_end) else self.poll_s)
