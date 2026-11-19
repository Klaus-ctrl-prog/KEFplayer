"""Radio-Pipeline E1b Schritt 3: Decoder (PCM f32le) -> StreamingProfile -> WAV 24 Bit -> Ringpuffer -> Quelle fuer den MediaServer.

Ohne Supervisor (``supervised=False``): Endet der Decoder, endet der Strom.
Mit Supervisor (E1b Schritt 4): Endet der Decoder, bleibt der Strom offen (Zustand ``gap``, Verbraucher bekommt
Stille) bis ``attach_decoder`` einen neuen Decoder liefert oder ``stop``/``finish`` aufgerufen wird.
Beruehrt weder Renderer noch RENDERER_LOCK.
Unterlauf: Der Verbraucher bekommt Stille (ANNAHME: die KEF Gen1 bleibt dann verbunden; ungeprueft).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .dsp import Profile
from .ringbuffer import ByteRing
from .streaming_dsp import StreamingProfile
from .wavstream import pack_s24le, wav_header_24

BLOCK_FRAMES = 1024
SILENCE_MS = 20
UNDERRUN_WAIT_S = 0.2


STANDARD_RATES = (32000, 44100, 48000, 88200, 96000, 176400, 192000)
FALLBACK_RATE = 48000


def choose_rate(sender_rate: int | None, override: int | None = None) -> int:
    """E-17: Senderrate ohne unnoetiges Resampling; Rueckfall 48 kHz.

    Raten unter 32 kHz oder unbekannte Raten gelten als nicht belastbar (bei HE-AAC meldet der ADTS-Kopf nur die
    halbe Kernrate; ein Resampling dorthin wuerde die Hoehen abschneiden) -> Rueckfall. ``override`` gewinnt immer.
    """
    if override is not None:
        return int(override)
    if sender_rate in STANDARD_RATES:
        return int(sender_rate)
    return FALLBACK_RATE


class _DecoderLike(Protocol):
    def read(self, n: int) -> bytes: ...
    def stop(self, grace_s: float = ...) -> int | None: ...


@dataclass
class PipelineStats:
    state: str = "idle"           # idle | streaming | gap | ended | failed | stopped
    gaps: int = 0
    frames_in: int = 0
    clipped_samples: int = 0
    invalid_samples: int = 0
    underruns: int = 0
    overrun_bytes: int = 0
    buffered_bytes: int = 0
    error_type: str | None = None  # nur Klassenname der Ausnahme bei state=failed (keine Meldung, keine URL)
    warnings: list[str] = field(default_factory=list)


class RadioPipeline:
    def __init__(self, decoder: _DecoderLike | None, profile: Profile, *, rate: int = 48000, channels: int = 2,
                 buffer_seconds: float = 2.0, block_frames: int = BLOCK_FRAMES, supervised: bool = False):
        self.decoder = decoder
        self.supervised = supervised
        self.last_data = time.monotonic()
        self.gap_reason: str | None = None
        self._attach = threading.Event()
        if decoder is not None:
            self._attach.set()
        self.rate = rate
        self.channels = channels
        self.block_frames = block_frames
        self.dsp = StreamingProfile(profile, rate, channels)
        self.ring = ByteRing(max(1, int(buffer_seconds * rate * channels * 3)))
        self._stats = PipelineStats(warnings=list(self.dsp.warnings))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._header = wav_header_24(rate, channels)

    # ---- Betrieb ----
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Pipeline laeuft bereits")
        with self._lock:
            self._stats.state = "streaming" if self.decoder is not None else "gap"
        self._thread = threading.Thread(target=self._run, name="radio-reader", daemon=True)
        self._thread.start()

    def attach_decoder(self, decoder: _DecoderLike) -> None:
        """Neuen (bereits gestarteten) Decoder uebernehmen. Filterzustand wird geloescht (kein Nachklingen)."""
        with self._lock:
            if self._stats.state in ("failed", "ended", "stopped"):
                raise RuntimeError("Pipeline beendet")
            self.decoder = decoder
            self.dsp.reset()
            self.last_data = time.monotonic()
            self.gap_reason = None
            if self._stats.state == "gap":
                self._stats.state = "streaming"
        self._attach.set()

    def finish(self) -> None:
        """Strom regulaer beenden (z. B. Supervisor gibt auf): Verbraucher bekommt nach dem Rest EOF."""
        with self._lock:
            if self._stats.state in ("streaming", "gap"):
                self._stats.state = "ended"
        self._stop.set()
        self._attach.set()
        self.ring.close()

    def _pump(self, dec) -> str:
        """Liest, bis der Decoder endet. Rueckgabe: 'eof' | 'stop'."""
        frame_bytes = 4 * self.channels
        want = self.block_frames * frame_bytes
        while not self._stop.is_set():
            data = dec.read(want)
            if not data:
                return "eof"
            while len(data) < want:  # volle Bloecke sammeln (Pipe liefert ggf. weniger)
                more = dec.read(want - len(data))
                if not more:
                    break
                data += more
            n_frames = len(data) // frame_bytes  # angefangenes Frame am Ende verwerfen
            if n_frames == 0:
                return "eof"
            block = np.frombuffer(data[: n_frames * frame_bytes], dtype="<f4").reshape(n_frames, self.channels)
            out = self.dsp.process(block.astype(np.float64))
            raw, clipped, bad = pack_s24le(out)
            self.ring.put(raw)
            with self._lock:
                self.last_data = time.monotonic()
                self._stats.frames_in += n_frames
                self._stats.clipped_samples += clipped
                self._stats.invalid_samples += bad
            if n_frames * frame_bytes < want:
                return "eof"
        return "stop"

    def _run(self) -> None:
        last = None
        try:
            while not self._stop.is_set():
                if not self._attach.wait(0.1):
                    continue
                self._attach.clear()  # erst loeschen, dann lesen: ein gleichzeitiges attach geht nicht verloren
                with self._lock:
                    dec = self.decoder
                if dec is None or dec is last:
                    continue
                last = dec
                if self._pump(dec) == "stop":
                    break
                if not self.supervised:
                    break
                with self._lock:  # Luecke: Strom bleibt offen, Verbraucher bekommt Stille
                    if self._stats.state == "streaming":
                        self._stats.state = "gap"
                        self._stats.gaps += 1
                    self.gap_reason = "decoder_ended"
            with self._lock:
                if self._stats.state in ("streaming", "gap"):
                    self._stats.state = "stopped" if self._stop.is_set() else "ended"
        except Exception as e:  # noqa: BLE001 - Verarbeitungsfehler: Strom beenden, Zustand melden
            with self._lock:
                self._stats.state = "failed"
                self._stats.error_type = type(e).__name__
                dec = self.decoder
            try:
                if dec is not None:
                    dec.stop(0.5)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.ring.close()

    def stop(self) -> None:
        self._stop.set()
        self._attach.set()
        with self._lock:
            if self._stats.state in ("streaming", "gap"):
                self._stats.state = "stopped"
            dec = self.decoder
        self.ring.close()
        try:
            if dec is not None:
                dec.stop()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=5.0)

    def stats(self) -> PipelineStats:
        with self._lock:
            s = PipelineStats(**{**self._stats.__dict__, "warnings": list(self._stats.warnings)})
        s.overrun_bytes = self.ring.overrun_bytes
        s.buffered_bytes = len(self.ring)
        return s

    # ---- Quelle fuer MediaServer.register_stream(opener, ...) ----
    def open_source(self) -> "_Source":
        return _Source(self)


class _Source:
    """Dateiaehnlich: erst WAV-Kopf, dann Ringpuffer; bei Unterlauf Stille; endet mit dem Strom."""

    def __init__(self, pipe: RadioPipeline):
        self._pipe = pipe
        self._sent_header = False
        self._silence = bytes(int(pipe.rate * SILENCE_MS / 1000) * pipe.channels * 3)

    def read1(self, n: int = 8192) -> bytes:
        p = self._pipe
        if not self._sent_header:
            self._sent_header = True
            return p._header
        data = p.ring.get(n, timeout=UNDERRUN_WAIT_S)
        if data:
            return data
        if p.ring.closed:
            return b""
        with p._lock:
            p._stats.underruns += 1
        return self._silence

    read = read1

    def close(self) -> None:
        pass
