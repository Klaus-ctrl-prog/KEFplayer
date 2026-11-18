"""WAV-PCM-24-Bit-Verpackung fuer Live-Streams (Phase E1b, E-16). Reiner Python/NumPy-Code, kein ffmpeg.

ANNAHME (an echter KEF Gen1 ungeprueft): ein WAV mit Groessenfeldern 0xFFFFFFFF wird als Endlosstrom
akzeptiert. Kein Dither; Spitzen ueber Vollaussteuerung werden begrenzt UND gezaehlt (clip counter).
"""
from __future__ import annotations

import struct

import numpy as np

BITS = 24
FULL_SCALE = 8388608.0  # 2**23
MAX_INT = 8388607
MIN_INT = -8388608
UNKNOWN_SIZE = 0xFFFFFFFF


def wav_header_24(rate: int, channels: int) -> bytes:
    """44-Byte-Kopf (PCM, 24 Bit, little endian) mit unbekannter Laenge."""
    if channels not in (1, 2) or not 8000 <= rate <= 192000:
        raise ValueError("rate/channels ausserhalb des unterstuetzten Bereichs")
    block_align = channels * 3
    return (b"RIFF" + struct.pack("<I", UNKNOWN_SIZE) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, BITS)
            + b"data" + struct.pack("<I", UNKNOWN_SIZE))


def pack_s24le(block: np.ndarray) -> tuple[bytes, int, int]:
    """block: (frames, channels) float, Vollaussteuerung = +-1.0 -> (bytes, begrenzte_samples, ungueltige_samples).

    Ungueltige Werte (NaN/Inf) werden zu 0 und gezaehlt. Rundung auf den naechsten Wert (kein Dither).
    """
    x = np.asarray(block, dtype=np.float64)
    bad = ~np.isfinite(x)
    n_bad = int(bad.sum())
    if n_bad:
        x = np.where(bad, 0.0, x)
    v = np.rint(x * FULL_SCALE)
    clipped = int(((v > MAX_INT) | (v < MIN_INT)).sum())
    v = np.clip(v, MIN_INT, MAX_INT).astype("<i4")
    raw = v.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3]
    return raw.tobytes(), clipped, n_bad
