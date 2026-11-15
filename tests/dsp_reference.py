"""Unabhaengige Referenzrechnung fuer die DSP-Tests (T5, T9, T10).

Nichts hier ist aus kefplayer abgeleitet. Die Biquad-Koeffizienten werden NICHT mit den RBJ-Formeln aus dsp.py
berechnet, sondern aus dem analogen Prototyp (Audio EQ Cookbook, Mittenfrequenz w0 = 1 rad/s) und
scipy.signal.bilinear mit Vorverzerrung bei fc. Beide Wege sind mathematisch gleichwertig, der Rechenweg ist ein
anderer. Gemessene Uebereinstimmung mit Filter.sos im moderaten Bereich: 1,1e-14 (siehe test_numeric_stability.py).
"""
import math

import numpy as np
from scipy import signal


def ref_sos(ftype, fc, gain_db, q, fs):
    """Biquad-Zeile [b0 b1 b2 1 a1 a2] aus analogem Prototyp plus Bilinear-Transformation."""
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * fc / fs
    if ftype == "peq":
        b, a = [1, A / q, 1], [1, 1 / (A * q), 1]
    elif ftype == "lowpass":
        b, a = [0, 0, 1], [1, 1 / q, 1]
    elif ftype == "highpass":
        b, a = [1, 0, 0], [1, 1 / q, 1]
    elif ftype == "lowshelf":
        r = math.sqrt(A) / q
        b, a = [A, A * r, A * A], [A, r, 1]
    elif ftype == "highshelf":
        r = math.sqrt(A) / q
        b, a = [A * A, A * r, A], [1, r, A]
    else:
        raise AssertionError(f"unbekannter Filtertyp {ftype!r}")
    bz, az = signal.bilinear(b, a, fs=0.5 / math.tan(w0 / 2))
    return np.concatenate([bz / az[0], az / az[0]])


def chain_sos(filters, fs):
    """Kette aus Filter-Dicts ({'type', 'fc', 'gain_db' (optional), 'q'}) als SOS-Matrix; None bei leerer Kette."""
    if not filters:
        return None
    return np.vstack([ref_sos(f["type"], f["fc"], f.get("gain_db", 0.0), f["q"], fs) for f in filters])


def chain_spec(todict, ch):
    """Filterliste (Dicts) und Pegel (dB) eines Kanals (0 = L, 1 = R) aus einem Profile.to_dict()."""
    filters = list(todict.get("filters", []))
    spec = todict.get("channels", {}).get({0: "L", 1: "R"}[ch], {})
    return filters + list(spec.get("filters", [])), float(spec.get("gain_db", 0.0))
