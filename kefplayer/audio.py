"""Dateiverarbeitung: lesen, Profil anwenden, als FLAC/WAV schreiben, Format protokollieren.

Quantisierung erfolgt hier selbst (nicht ueber libsndfile-Gleitkomma-Konvertierung), damit ein
neutrales Profil bitgenau durchlaeuft. Ganze Datei liegt als float64 im Speicher.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from . import dsp

class AudioProcessError(Exception):
    """Verarbeitungsfehler mit am Entstehungsort bestimmter Fehlerkategorie."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32, "DOUBLE": 64}


@dataclass(frozen=True)
class AudioInfo:
    format: str
    subtype: str
    bits: int | None
    samplerate: int
    channels: int
    frames: int
    duration_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def probe(path: str | Path) -> AudioInfo:
    i = sf.info(str(path))
    return AudioInfo(
        format=i.format,
        subtype=i.subtype,
        bits=_BITS.get(i.subtype),
        samplerate=int(i.samplerate),
        channels=int(i.channels),
        frames=int(i.frames),
        duration_s=float(i.duration),
    )


def read_audio(path: str | Path):
    """Rueckgabe: (data float64 (frames, channels), samplerate, AudioInfo). PCM wird exakt skaliert."""
    info = probe(path)
    if info.subtype.startswith("PCM"):
        raw, fs = sf.read(str(path), dtype="int32", always_2d=True)
        data = raw.astype(np.float64) / 2147483648.0
    else:
        data, fs = sf.read(str(path), dtype="float64", always_2d=True)
    return data, int(fs), info


def choose_output(info: AudioInfo, fmt: str, bits: int | None = None) -> str:
    """Ausgabe-Subtype. Standard: Bittiefe der Quelle (FLAC max. 24 Bit)."""
    fmt = fmt.upper()
    if fmt not in ("FLAC", "WAV"):
        raise ValueError(f"Format muss FLAC oder WAV sein (war {fmt!r})")
    if bits is not None and bits not in (16, 24, 32):
        raise ValueError(f"bits muss 16, 24 oder 32 sein (war {bits})")
    src_bits = info.bits or 24
    if fmt == "FLAC":
        if bits == 32:
            raise ValueError("FLAC unterstuetzt hoechstens 24 Bit")
        want = bits or (16 if src_bits <= 16 else 24)
        return "PCM_16" if want == 16 else "PCM_24"
    if bits is None and info.subtype in ("FLOAT", "DOUBLE"):
        return "FLOAT"
    want = bits or (16 if src_bits <= 16 else min(src_bits, 32))
    return {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}[want]


def _write(path: Path, data: np.ndarray, fs: int, subtype: str, fmt: str, dither: bool, seed: int):
    if subtype == "FLOAT":
        sf.write(str(path), data.astype(np.float32), fs, subtype="FLOAT", format=fmt)
        return
    bits = _BITS[subtype]
    full = 2.0 ** (bits - 1)
    x = data * full
    if dither:  # TPDF, +-1 LSB
        rng = np.random.default_rng(seed)
        x = x + (rng.random(x.shape) - rng.random(x.shape))
    q = np.clip(np.rint(x), -full, full - 1)
    if bits == 16:
        arr = q.astype(np.int16)
    elif bits == 24:
        arr = (q.astype(np.int64) << 8).astype(np.int32)  # libsndfile erwartet 32-Bit-Skalierung
    else:
        arr = q.astype(np.int32)
    sf.write(str(path), arr, fs, subtype=subtype, format=fmt)


def process_file(
    src: str | Path,
    dst_dir: str | Path,
    profile: dsp.Profile,
    *,
    fmt: str = "FLAC",
    bits: int | None = None,
    dither: bool = True,
    seed: int = 0,
    stem: str | None = None,
    classify_errors: bool = False,
) -> dict:
    """Filtert eine Datei und schreibt sie nach dst_dir. Rueckgabe: Protokoll-Dict."""
    t0 = time.perf_counter()
    src = Path(src)
    dst_dir = Path(dst_dir)

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if classify_errors:
            raise AudioProcessError("cache_io", str(exc)) from exc
        raise

    try:
        data, fs, info = read_audio(src)
    except sf.LibsndfileError as exc:
        if classify_errors:
            raise AudioProcessError("source_format", str(exc)) from exc
        raise
    except OSError as exc:
        if classify_errors:
            raise AudioProcessError("source_file_access", str(exc)) from exc
        raise
    except Exception as exc:  # noqa: BLE001 - Fehler beim Lesen der Quelldatei
        if classify_errors:
            raise AudioProcessError("processing_error", str(exc)) from exc
        raise

    try:
        subtype = choose_output(info, fmt, bits)
        obits = _BITS[subtype]
        neutral = profile.is_neutral()
        ceiling = None if (subtype == "FLOAT" or neutral) else 1.0 - 2.0 ** -(obits - 1)
        out, pinfo = dsp.apply_profile(data, fs, profile, ceiling=ceiling)
    except Exception as exc:  # noqa: BLE001 - Konfiguration oder Signalverarbeitung
        if classify_errors:
            raise AudioProcessError("processing_error", str(exc)) from exc
        raise

    # Dither nur bei 16 Bit und nur, wenn tatsaechlich neu quantisiert wird
    use_dither = dither and subtype == "PCM_16" and not (neutral and info.bits == 16)
    dst = dst_dir / f"{stem or src.stem}.{fmt.lower()}"
    try:
        _write(dst, out, fs, subtype, fmt.upper(), use_dither, seed)
        out_info = probe(dst)
    except (OSError, sf.LibsndfileError) as exc:
        if classify_errors:
            raise AudioProcessError("cache_io", str(exc)) from exc
        raise
    except Exception as exc:  # noqa: BLE001 - unerwarteter Fehler beim Schreiben/Pruefen der Cache-Datei
        if classify_errors:
            raise AudioProcessError("processing_error", str(exc)) from exc
        raise

    return {
        "src": str(src),
        "dst": str(dst),
        "neutral": neutral,
        "profile": profile.name,
        "in": info.to_dict(),
        "out": out_info.to_dict(),
        "preamp_db": pinfo["preamp_db"],
        "peak_in_dbfs": dsp.lin_to_db(pinfo["peak_in"]),
        "peak_out_dbfs": dsp.lin_to_db(pinfo["peak_out"]),
        "guard_gain_db": pinfo["guard_gain_db"],
        "delay_samples": pinfo["delay_samples"],
        "dither": use_dither,
        "warnings": profile.validate() + pinfo["warnings"],
        "elapsed_s": round(time.perf_counter() - t0, 3),
    }
