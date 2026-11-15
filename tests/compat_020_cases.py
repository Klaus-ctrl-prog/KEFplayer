"""Gemeinsame Hilfen fuer die Rueckwaertskompatibilitaetstests gegen 0.2.0 (T5).

Dieses Modul beschreibt WAS geprueft wird (Profildateien, Anwendungsfaelle, Eingangssignale, fehlerhafte
Profile) und wie daraus ein "Schnappschuss" des Verhaltens entsteht. Es enthaelt keine Sollwerte.

Die Sollwerte stehen in data/compat_020/expected.json. Diese Datei wurde mit dem UNVERAENDERTEN Stand 0.2.0
(Tag v0.2.0, Commit 8788254) erzeugt, und zwar mit data/compat_020/make_expected.py. Der Test vergleicht den
Schnappschuss des aktuellen Codes mit diesen gespeicherten 0.2.0-Werten.

Eingangssignale sind Summen von Sinustoenen (keine Zufallszahlen), damit sie ohne Abhaengigkeit von einer
numpy-Version reproduzierbar sind.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from kefplayer import dsp
from kefplayer.dsp import Profile

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "compat_020"
EXPECTED = DATA / "expected.json"
EXAMPLES = HERE.parent / "examples"

FREQS = [20, 50, 100, 500, 1000, 3000, 8000, 16000]
# Name -> (Abtastrate, Kanalzahl)
APPLY_CASES = {"stereo_44100": (44100, 2), "stereo_96000": (96000, 2), "mono_48000": (48000, 1)}
CEILING = 1.0 - 2.0 ** -23  # Clipping-Schutz wie bei 24-Bit-Ausgabe


def profile_paths() -> dict[str, Path]:
    """Profildateien: Testdaten (JSON und Equalizer-APO-Text) und die mitgelieferten Beispielprofile."""
    out: dict[str, Path] = {}
    for p in sorted(DATA.glob("j*.json")) + sorted(DATA.glob("a*.txt")):
        out[p.name] = p
    for p in sorted(EXAMPLES.glob("*.json")):
        out[f"examples/{p.name}"] = p
    return out


def make_signal(fs: int, nch: int) -> np.ndarray:
    """0,25 s, Spitze unter 0,6. Kanal 0 und 1 mit verschiedenen Frequenzen und Phasen."""
    n = fs // 4
    t = np.arange(n) / fs
    two_pi = 2.0 * math.pi
    left = 0.30 * np.sin(two_pi * 97 * t) + 0.20 * np.sin(two_pi * 1013 * t + 0.3) + 0.10 * np.sin(two_pi * 7919 * t + 1.1)
    right = 0.25 * np.sin(two_pi * 131 * t + 0.7) + 0.20 * np.sin(two_pi * 2003 * t) + 0.10 * np.sin(two_pi * 11003 * t + 2.0)
    return np.stack([left, right], axis=1)[:, :nch]


def _f(x) -> float:
    return float(x)


def snapshot(path: Path) -> dict:
    """Verhalten einer Profildatei: Serialisierung, Hinweise, Frequenzgang, Anwendung auf feste Signale."""
    prof = Profile.load(path)
    snap: dict = {
        "to_dict": prof.to_dict(),
        "is_neutral": bool(prof.is_neutral()),
        "notes": list(prof.notes),
        "validate": list(prof.validate()),
        "response_db_48000": {
            label: [_f(v) for v in prof.response_db(ch, 48000, FREQS)] for ch, label in ((0, "L"), (1, "R"))
        },
        "apply": {},
    }
    for case, (fs, nch) in APPLY_CASES.items():
        data = make_signal(fs, nch)
        out, info = dsp.apply_profile(data, fs, prof, ceiling=CEILING)
        snap["apply"][case] = {
            "shape": list(out.shape),
            "preamp_db": _f(info["preamp_db"]),
            "guard_gain_db": _f(info["guard_gain_db"]),
            "delay_samples": [int(d) for d in info["delay_samples"]],
            "peak_in": _f(info["peak_in"]),
            "peak_out": _f(info["peak_out"]),
            "warnings": list(info["warnings"]),
            "rms": [_f(np.sqrt(np.mean(out[:, c] ** 2))) for c in range(nch)],
            "peak": [_f(np.max(np.abs(out[:, c]))) for c in range(nch)],
        }
    return snap


# ---- fehlerhafte Profile: Fehlermeldung muss gleich bleiben -------------------------------------------
# (Schluessel, Art, Text, Stufe, Abtastrate). Stufe "load": Fehler beim Einlesen, "apply": erst beim Anwenden.
ERROR_CASES: list[tuple[str, str, str, str, int]] = [
    ("json_unknown_field", "json", '{"filters": [], "foo": 1}', "load", 48000),
    ("json_unknown_channel", "json", '{"channels": {"C": {}}}', "load", 48000),
    ("json_unknown_channel_field", "json", '{"channels": {"L": {"volume": 1}}}', "load", 48000),
    ("json_unknown_filter_field", "json", '{"filters": [{"type": "peq", "fc": 100, "gain_db": 1, "q": 1, "x": 2}]}', "load", 48000),
    ("json_filter_without_fc", "json", '{"filters": [{"type": "peq"}]}', "load", 48000),
    ("json_unknown_filter_type", "json", '{"filters": [{"type": "notch", "fc": 100}]}', "load", 48000),
    ("json_gain_beyond_24_db", "json", '{"filters": [{"type": "peq", "fc": 100, "gain_db": 25, "q": 1}]}', "load", 48000),
    ("json_q_zero", "json", '{"filters": [{"type": "peq", "fc": 100, "gain_db": 1, "q": 0}]}', "load", 48000),
    ("json_negative_fc", "json", '{"filters": [{"type": "peq", "fc": -1, "gain_db": 1}]}', "load", 48000),
    ("json_gain_on_lowpass", "json", '{"filters": [{"type": "lowpass", "fc": 100, "gain_db": 1}]}', "load", 48000),
    ("json_preamp_text", "json", '{"preamp_db": "laut"}', "load", 48000),
    ("json_channel_gain_beyond_12_db", "json", '{"channels": {"L": {"gain_db": 13}}}', "load", 48000),
    ("json_channel_delay_beyond_50_ms", "json", '{"channels": {"R": {"delay_ms": 51}}}', "load", 48000),
    ("json_fc_above_nyquist", "json", '{"filters": [{"type": "peq", "fc": 15000, "gain_db": -1, "q": 1}]}', "apply", 22050),
    ("apo_include", "apo", "Include: andere.txt\n", "load", 48000),
    ("apo_convolution", "apo", "Convolution: hall.wav\n", "load", 48000),
    ("apo_delay_in_samples", "apo", "Delay: 10 samples\n", "load", 48000),
    ("apo_bw_oct", "apo", "Filter 1: ON PK Fc 1000 Hz Gain -3 dB BW Oct 0.5\n", "load", 48000),
    ("apo_pk_without_q", "apo", "Filter 1: ON PK Fc 1000 Hz Gain -3 dB\n", "load", 48000),
    ("apo_shelf_db_per_octave", "apo", "Filter 1: ON LS 6dB Fc 100 Hz Gain 3 dB\n", "load", 48000),
    ("apo_unknown_channel", "apo", "Channel: C\n", "load", 48000),
    ("apo_unreadable_filter_line", "apo", "Filter 1: ON\n", "load", 48000),
    ("apo_filter_without_fc", "apo", "Filter 1: ON PK Gain -3 dB Q 2\n", "load", 48000),
    ("apo_gain_on_lowpass", "apo", "Filter 1: ON LP Fc 1000 Hz Gain 3 dB\n", "load", 48000),
    ("apo_copy_line", "apo", "Copy: L=R\n", "load", 48000),
    ("apo_preamp_without_number", "apo", "Preamp: dB\n", "load", 48000),
    ("apo_delay_without_number", "apo", "Delay: ms\n", "load", 48000),
    ("apo_fc_above_nyquist", "apo", "Filter 1: ON PK Fc 15000 Hz Gain -1 dB Q 1\n", "apply", 22050),
]


def run_error_case(kind: str, text: str, stage: str, fs: int) -> dict:
    """Fuehrt einen fehlerhaften Fall aus. Rueckgabe: Ausnahmetyp und Meldung (None, None, falls kein Fehler)."""
    try:
        prof = Profile.from_dict(json.loads(text)) if kind == "json" else dsp.parse_apo(text)
        if stage == "apply":
            dsp.apply_profile(np.zeros((100, 2)), fs, prof)
    except Exception as e:  # noqa: BLE001 - genau die Ausnahme interessiert
        return {"type": type(e).__name__, "message": str(e)}
    return {"type": None, "message": None}


def build_expected() -> dict:
    return {
        "profiles": {name: snapshot(path) for name, path in profile_paths().items()},
        "errors": {key: run_error_case(kind, text, stage, fs) for key, kind, text, stage, fs in ERROR_CASES},
    }
