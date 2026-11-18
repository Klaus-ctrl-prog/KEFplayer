"""ffmpeg als externer Decoder (Phase E1b, Schritt 2). Nur Prozessfuehrung, keine DSP-/Netzlogik.

Regeln (docs/radio-e1b-design.md, Abschnitt 4):
- ffmpeg kommt nur vom Nutzer: ABSOLUTER Pfad, keine PATH-Suche, kein Download.
- Beim Start werden Version und SHA-256 der Datei ermittelt; optional Pin (Abweichung = Abbruch).
- ``shell=False``, Argumentliste, ``stdin=DEVNULL``, ``-nostdin``, Protokoll-Whitelist.
- Kein ``-reconnect*``: Wiederverbinden macht der Supervisor (geprueft und gezaehlt).
- stderr wird dauernd geleert (kein Pipe-Stau) und vor dem Speichern von URLs bereinigt.
- Windows Application Control wird nicht umgangen: Startfehler -> ``decoder_unavailable``.
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .radio import RadioError, mask_url

PROTOCOL_WHITELIST = "http,https,tcp,tls,crypto"
STDERR_KEEP_LINES = 20
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")


@dataclass(frozen=True)
class FfmpegInfo:
    path: str
    version: str
    sha256: str
    # Nur fuer Tests: Startpraefix statt [path] (z. B. [python, fake.py, ...]). Produktiv immer [path].
    argv_prefix: tuple[str, ...] = field(default=(), compare=False)

    def prefix(self) -> list[str]:
        return list(self.argv_prefix) if self.argv_prefix else [self.path]


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_ffmpeg(path: str, *, pin_sha256: str | None = None, timeout_s: float = 10.0,
                 runner: Callable = subprocess.run) -> FfmpegInfo:
    """Prueft den vom Nutzer angegebenen ffmpeg. Wirft RadioError('decoder_unavailable', ...)."""
    if not path or not os.path.isabs(path):
        raise RadioError("decoder_unavailable", "ffmpeg-Pfad muss absolut sein (keine PATH-Suche, kein relativer Pfad).")
    p = Path(path)
    if not p.is_file():
        raise RadioError("decoder_unavailable", "ffmpeg-Datei nicht gefunden oder keine Datei.")
    try:
        digest = sha256_file(p)
    except OSError as e:
        raise RadioError("decoder_unavailable", f"ffmpeg nicht lesbar ({type(e).__name__}).") from None
    if pin_sha256 is not None and digest.lower() != pin_sha256.strip().lower():
        raise RadioError("decoder_unavailable", "ffmpeg-Pruefsumme weicht vom angegebenen Pin ab: nicht gestartet.")
    try:
        out = runner([str(p), "-hide_banner", "-version"], capture_output=True, text=True,
                     timeout=timeout_s, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RadioError("decoder_unavailable",
                         f"ffmpeg nicht startbar ({type(e).__name__}); von Windows Application Control blockiert oder defekt?") from None
    first = (out.stdout or "").strip().splitlines()[:1]
    if out.returncode != 0 or not first or not first[0].lower().startswith("ffmpeg version"):
        raise RadioError("decoder_unavailable", "Datei meldet sich nicht als ffmpeg (-version).")
    return FfmpegInfo(path=str(p), version=first[0].strip(), sha256=digest)


def scrub(text: str) -> str:
    """URLs durch Schema://Host:Port/... ersetzen (Pfad/Query koennen Token enthalten)."""
    return _URL_RE.sub(lambda m: mask_url(m.group(0)), text)


def build_args(url: str, *, rate: int = 48000, channels: int = 2) -> list[str]:
    if not isinstance(rate, int) or not 8000 <= rate <= 192000:
        raise ValueError("rate ausserhalb 8000..192000")
    if channels not in (1, 2):
        raise ValueError("channels muss 1 oder 2 sein")
    return ["-nostdin", "-hide_banner", "-loglevel", "warning",
            "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-rw_timeout", "10000000",
            "-i", url, "-vn", "-sn", "-dn",
            "-f", "f32le", "-ac", str(channels), "-ar", str(rate), "pipe:1"]


class Decoder:
    """Ein ffmpeg-Prozess. read() liefert PCM f32le; stop() beendet sanft, dann hart."""

    def __init__(self, info: FfmpegInfo, url: str, *, rate: int = 48000, channels: int = 2):
        self.info = info
        self.rate = rate
        self.channels = channels
        self._url = url
        self._args = build_args(url, rate=rate, channels=channels)
        self._proc: subprocess.Popen | None = None
        self._err_thread: threading.Thread | None = None
        self._err_lines: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._lock = threading.Lock()
        self.url_masked = mask_url(url)

    # ---- Lebenszyklus ----
    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("Decoder laeuft bereits")
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(self.info.prefix() + self._args, shell=False,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, bufsize=0, **kw)
        except OSError as e:
            raise RadioError("decoder_unavailable", f"ffmpeg nicht startbar ({type(e).__name__}).") from None
        self._err_thread = threading.Thread(target=self._drain_stderr, name="decoder-stderr", daemon=True)
        self._err_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = scrub(raw.decode("utf-8", "replace").rstrip())
                if line:
                    with self._lock:
                        self._err_lines.append(line[:300])
        except (OSError, ValueError):
            pass

    def read(self, n: int) -> bytes:
        """Blockierend, bis n Bytes da sind oder der Strom endet (dann kuerzer/leer)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return proc.stdout.read(n) or b""
        except (OSError, ValueError):
            return b""

    def poll(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def stderr_tail(self) -> list[str]:
        with self._lock:
            return list(self._err_lines)

    def stop(self, grace_s: float = 2.0) -> int | None:
        """Sanft (SIGTERM/CTRL_BREAK), nach grace_s hart. Idempotent. Gibt den Exitcode zurueck."""
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            self._signal(proc, hard=False)
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self._signal(proc, hard=True)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except (OSError, ValueError):
                pass
        if self._err_thread is not None:
            self._err_thread.join(timeout=2.0)
        return proc.poll()

    @staticmethod
    def _signal(proc: subprocess.Popen, *, hard: bool) -> None:
        try:
            if os.name == "nt":
                if hard:
                    proc.kill()
                else:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def __enter__(self) -> "Decoder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
