"""T9: numerische Stabilitaet der Biquad-Filter (float64, scipy.signal.sosfilt) ueber ein Raster extremer Parameter.

Frage: Bleiben die Filter stabil, endlich und genau genug, auch bei tiefer Frequenz, hoher Guete, hoher Abtastrate
und +-24 dB? Die Tests sagen nichts ueber Klang oder KEF-Hardware.

Raster (384 Faelle, Konstanten GRID unten)
    Abtastrate  44100, 96000, 192000 Hz
    Frequenz    20, 63, 1000, 20000 Hz
    Guete q     0,5; 5; 100; 1000
    Typ/Gain    peq, lowshelf, highshelf je -24 und +24 dB; lowpass und highpass (ohne Gain)

Pruefungen
    1. Stabilitaet: Pole liegen im Einheitskreis (Schur-Cohn-Dreieck auf den float64-Koeffizienten:
       |a2| < 1 und |a1| < 1 + a2). Der groesste Polradius wird zusaetzlich aus den Nullstellen des Nenners bestimmt.
    2. Endlichkeit: keine NaN/Inf bei 1 s Rauschen und bei einem Einheitsimpuls.
    3. Genauigkeit gegen erweiterte Praezision (Referenzberechnung, siehe unten): groesste Abweichung
       max |y_float64 - y_referenz| ueber 1 s Rauschen muss unter THRESHOLD = 1e-9 liegen (absolut, freigegeben).
    3b. Zusaetzlich Resonanzanregung (Sinus 0,3 bei fc und bei 2 fc): Hier waechst die Ausgangsspitze in einzelnen
       Faellen auf ein Vielfaches der Vollaussteuerung (gemessen bis 1253); die Abweichung wird deshalb auf die Ausgangsspitze bezogen und muss
       unter THRESHOLD liegen (ergaenzend, siehe Befunde unten).
    4. Ausschwingen: Wo der Polradius ein Abklingen innerhalb der halben Signaldauer erzwingt, ist die zweite
       Haelfte der Impulsantwort vernachlaessigbar.
    5. Mehrere Filter hintereinander (Kette aus zehn Abschnitten) und die Kanalketten von apply_profile.
    6. Koeffizienten gegen eine unabhaengige Herleitung (dsp_reference.py: analoger Prototyp plus
       scipy.signal.bilinear), im moderaten Bereich.
    7. Auto-Preamp bei sehr schmalen Spitzen (hohes q), die zwischen den Punkten des Suchgitters liegen. Sollwert
       analytisch: Ein Peaking-Filter hat bei fc genau seinen Gain.

Referenzberechnung (Punkt 3 und 5)
    Gleiche float64-Koeffizienten wie das Produkt (Filter.sos), aber das Signal wird in np.longdouble gefiltert:
    je Abschnitt scipy.signal.lfilter mit longdouble-Koeffizienten und longdouble-Signal, hintereinander, ohne
    Rundung auf float64 zwischen den Abschnitten. Auf x86-64 mit Linux ist longdouble die 80-Bit-Erweiterung
    (Maschinengenauigkeit 1,08e-19 statt 2,2e-16). Die Referenz wird in test_reference_matches_a_hand_written_longdouble_loop
    gegen eine von Hand geschriebene Schleife (Transposed Direct Form II) geprueft. Gemessen wird also der
    Rechenfehler der float64-Filterung, nicht der Fehler durch die Rundung der Koeffizienten selbst. Auf Plattformen,
    auf denen longdouble nicht genauer als float64 ist (z. B. Windows mit MSVC), oder bei einem scipy, dessen lfilter
    longdouble nicht mehr mit erweiterter Praezision rechnet, werden die Tests mit Erweiterungsreferenz
    uebersprungen (Grund steht im Testbericht, die Verfuegbarkeit wird beim Import mit einem Sondierungsaufruf
    geprueft). Die Punkte 1, 2, 4 und 6 laufen dort trotzdem. Die Referenz haengt von dem Verhalten von lfilter mit
    longdouble ab, das scipy 1.15 als nicht unterstuetzt meldet (DeprecationWarning, im Test unterdrueckt). Sie ist
    durch test_reference_matches_a_hand_written_longdouble_loop abgesichert.

Eingangssignal: np.random.default_rng(1234).uniform(-0.3, 0.3, fs), also Aussteuerung 0,3. Die Schwelle ist
eine absolute Abweichung (Vollaussteuerung 1,0). Sie ist ein vorlaeufiger Akzeptanzwert (freigegeben als 1e-9) und
kein aus Hoerbarkeit hergeleiteter Grenzwert.

Gemessene Maximalabweichung (Referenzberechnung oben, Linux x86-64, alle 384 Faelle): 1,41e-10 bei fs = 192000 Hz,
fc = 20 Hz, q = 100, highshelf, +24 dB (groesste Ausgangsspitze dort 5,9). Der Wert war in allen vier geprueften
Umgebungen gleich: CPython 3.10.20 (numpy 2.2.6, scipy 1.15.3), 3.11.18 (2.4.6, 1.17.1), 3.12.3 und 3.13.18
(je 2.5.4, 1.18.1). Groesster Polradius: 0,99999991798 (192000 Hz, 20 Hz, q = 1000, peq, +24 dB). Kein Fall instabil,
kein NaN/Inf, 180 Faelle mit erzwungenem Ausschwingen. Die Schwelle hat damit nur etwa 7-fache Reserve zur groessten
gemessenen Abweichung (nicht zu den Ausgangsspitzen: bei grossen Spitzen waechst die absolute Abweichung mit).
Windows ist nicht geprueft; dort ist longdouble mit MSVC nicht genauer als float64, die Pruefung gegen die
Erweiterungsreferenz wuerde uebersprungen.

Befunde der abschliessenden Pruefung (gemessen, nicht aus der Schwelle abgeleitet)
    a) Resonanzanregung, absolute Abweichung: Sinus bei fc ueberschreitet 1e-9 in 3 von 384 Faellen (groesster Wert
       4,2e-9 bei 192 kHz, 20 Hz, q = 100, lowpass, Ausgangsspitze 14; zwei weitere lowpass mit q = 1000, Spitzen
       287 und 18), Sinus bei 2 fc in 11 von 384 (groesster Wert 1,7e-8, Ausgangsspitze 513; zehn davon
       Hochregal +24 dB, eines Tiefregal -24 dB). Die Ausgangsspitzen aller 14 Ueberschreitungen liegen zwischen
       14 und 1253. Bezogen auf die Ausgangsspitze liegt die Abweichung in allen 768 Faellen unter 4,3e-10
       (gemessen 4,2e-10). Die absolute Schwelle gilt unveraendert nur fuer die freigegebene Rauschanregung; bei
       Spitzen weit ueber Vollaussteuerung ist sie nicht aussagekraeftig.
    b) Koeffizientenrundung: Die Referenz verwendet die float64-Koeffizienten des Produkts. Gegen Koeffizienten, die
       in longdouble berechnet wurden (gleiche Cookbook-Formeln), betraegt die groesste Abweichung bei Rauschanregung
       4,7e-8 (192 kHz, 20 Hz, q = 1000, highshelf, +24 dB); 19 von 384 Faellen liegen ueber 1e-9, keiner ueber 1e-6.
       Das ist die Rundung der Biquad-Koeffizienten auf float64 und kein Rechenfehler der Filterung. Dafuer gibt es
       keinen Test mit Schwelle, weil keine Schwelle freigegeben ist.
    c) q hat im Produkt keine Obergrenze (q = 1000 und mehr werden angenommen).

Neu erzeugen:
    PYTHONPATH=. python -B tests/test_numeric_stability.py
"""
import functools
import itertools
import warnings

import numpy as np
import pytest
from scipy import signal

from dsp_reference import ref_sos
from kefplayer import dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile

THRESHOLD = 1e-9
FS = (44100, 96000, 192000)
FC = (20.0, 63.0, 1000.0, 20000.0)
Q = (0.5, 5.0, 100.0, 1000.0)
TYPES = [(t, g) for t in ("peq", "lowshelf", "highshelf") for g in (-24.0, 24.0)] + [("lowpass", 0.0), ("highpass", 0.0)]
GRID = list(itertools.product(FS, FC, Q, TYPES))

pytestmark = pytest.mark.filterwarnings("ignore:dtype=float(96|128) is not supported by lfilter:DeprecationWarning")


def _extended_precision_reference():
    """(verfuegbar, Grund). Die Referenz braucht (1) ein longdouble, das genauer als float64 ist, und (2) ein
    scipy.signal.lfilter, das in longdouble wirklich mit erweiterter Praezision rechnet. scipy 1.15 meldet dafuer
    eine DeprecationWarning ('not supported ... will raise an error in SciPy 1.17.0'); in 1.17.1 und 1.18.1 laeuft
    es ohne Fehler (gemessen). Gepruft wird das hier selbst, nicht angenommen."""
    if not np.finfo(np.longdouble).eps < np.finfo(np.float64).eps / 100:
        return False, "np.longdouble ist auf dieser Plattform nicht genauer als float64 (z. B. Windows/MSVC)"
    one, tiny = np.longdouble(1), np.longdouble(1e-18)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            y = signal.lfilter(np.array([one]), np.array([one]), np.array([one + tiny]))
    except (TypeError, ValueError) as e:
        return False, f"scipy.signal.lfilter akzeptiert longdouble nicht ({type(e).__name__}: {e})"
    if float(y[0] - one) == 0.0:
        return False, "scipy.signal.lfilter rechnet longdouble nur mit float64-Genauigkeit"
    return True, ""


EXTENDED, _WHY_NOT = _extended_precision_reference()
needs_extended = pytest.mark.skipif(not EXTENDED, reason=_WHY_NOT or "Referenz in erweiterter Praezision nicht verfuegbar")


def noise(fs, seed=1234):
    return np.random.default_rng(seed).uniform(-0.3, 0.3, fs)


def reference_longdouble(sos, x):
    """Abschnitte hintereinander in erweiterter Praezision, ohne Rundung dazwischen."""
    y = np.asarray(x).astype(np.longdouble)
    for row in sos:
        y = signal.lfilter(row[:3].astype(np.longdouble), row[3:].astype(np.longdouble), y)
    return y


def deviation(sos, x):
    y = signal.sosfilt(sos, x)
    ref = reference_longdouble(sos, x)
    return float(np.max(np.abs(y.astype(np.longdouble) - ref))), float(np.max(np.abs(ref)))


def stable(sos):
    a1, a2 = sos[:, 4], sos[:, 5]
    return bool(np.all((np.abs(a2) < 1) & (np.abs(a1) < 1 + a2)))


def radius(sos):
    return float(max(np.max(np.abs(np.roots([1.0, row[4], row[5]]))) for row in sos))


@functools.lru_cache(maxsize=None)
def grid_results():
    """Je Fall: Abweichung, Ausgangsspitze (Referenz), Polradius, stabil, endlich, Ausschwingen."""
    rows = []
    for fs, fc, q, (ftype, gain) in GRID:
        sos = Filter(ftype, fc, gain, q).sos(fs)
        x = noise(fs)
        y = signal.sosfilt(sos, x)
        imp = np.zeros(fs)
        imp[0] = 1.0
        h = signal.sosfilt(sos, imp)
        r = radius(sos)
        dev, ymax = deviation(sos, x) if EXTENDED else (float("nan"), float(np.max(np.abs(y))))
        half = fs // 2
        decays = r ** half < 1e-12
        rows.append({
            "case": (fs, fc, q, ftype, gain), "dev": dev, "ymax": ymax, "radius": r, "stable": stable(sos),
            "finite": bool(np.all(np.isfinite(y)) and np.all(np.isfinite(h))),
            "decays": decays, "tail": float(np.max(np.abs(h[half:]))), "head": float(np.max(np.abs(h[:half]))),
        })
    return rows


def describe(row):
    fs, fc, q, ftype, gain = row["case"]
    return f"fs={fs} fc={fc:g} q={q:g} {ftype} {gain:+g} dB"


def test_grid_has_the_documented_size_and_covers_the_extremes():
    assert len(GRID) == 384
    assert {c[0] for c in GRID} == set(FS) and {c[1] for c in GRID} == set(FC) and {c[2] for c in GRID} == set(Q)
    kinds = {(c[3][0], c[3][1] > 0, c[3][1] < 0) for c in GRID}
    assert len(kinds) == len(TYPES)
    assert (192000, 20.0, 1000.0, ("peq", 24.0)) in GRID


def test_all_poles_are_inside_the_unit_circle():
    rows = grid_results()
    bad = [describe(r) for r in rows if not r["stable"]]
    assert not bad, bad
    assert max(r["radius"] for r in rows) < 1.0


def test_outputs_are_finite_for_noise_and_impulse():
    bad = [describe(r) for r in grid_results() if not r["finite"]]
    assert not bad, bad


def test_impulse_response_rings_out_where_the_poles_demand_it():
    rows = [r for r in grid_results() if r["decays"]]
    assert len(rows) > 100  # die Pruefung ist nicht leer
    bad = [(describe(r), r["tail"], r["head"]) for r in rows if r["tail"] > 1e-6 * max(1.0, r["head"])]
    assert not bad, bad[:5]


@needs_extended
def test_float64_filtering_deviates_from_extended_precision_by_less_than_threshold():
    rows = grid_results()
    worst = sorted(rows, key=lambda r: r["dev"], reverse=True)[:5]
    msg = "; ".join(f"{describe(r)}: {r['dev']:.2e} (max|y| {r['ymax']:.3g})" for r in worst)
    assert worst[0]["dev"] > 0, "Referenz ist nicht genauer als float64"
    assert worst[0]["dev"] < THRESHOLD, msg


@needs_extended
def test_reference_matches_a_hand_written_longdouble_loop():
    for fs, fc, q, ftype, gain in [(192000, 20.0, 1000.0, "peq", 24.0), (44100, 63.0, 5.0, "lowshelf", 12.0)]:
        sos = Filter(ftype, fc, gain, q).sos(fs)
        x = noise(fs)[:4000]
        b0, b1, b2, _, a1, a2 = [np.longdouble(v) for v in sos[0]]
        z1 = z2 = np.longdouble(0)
        loop = np.empty(x.size, dtype=np.longdouble)
        for i in range(x.size):
            xi = np.longdouble(x[i])
            yo = b0 * xi + z1
            z1 = b1 * xi - a1 * yo + z2
            z2 = b2 * xi - a2 * yo
            loop[i] = yo
        assert float(np.max(np.abs(loop - reference_longdouble(sos, x)))) < 1e-14


CHAINS = {
    "zehn_Abschnitte_gemischt": [
        ("peq", 25.0, 6.0, 8.0), ("peq", 63.0, -6.0, 5.0), ("lowshelf", 100.0, 3.0, 0.7), ("peq", 250.0, -4.0, 3.0),
        ("peq", 800.0, 5.0, 2.0), ("peq", 2500.0, -3.0, 4.0), ("highshelf", 6000.0, -2.0, 0.7), ("peq", 9000.0, 2.0, 6.0),
        ("highpass", 20.0, 0.0, 0.7071), ("lowpass", 18000.0, 0.0, 0.7071),
    ],
    "zehn_Abschnitte_extrem": [
        ("peq", 20.0, 24.0, 100.0), ("peq", 20.0, -24.0, 100.0), ("lowshelf", 20.0, 24.0, 5.0), ("highshelf", 20.0, -24.0, 5.0),
        ("peq", 63.0, 12.0, 1000.0), ("peq", 63.0, -12.0, 1000.0), ("peq", 1000.0, 24.0, 50.0), ("peq", 1000.0, -24.0, 50.0),
        ("highpass", 20.0, 0.0, 5.0), ("lowpass", 20000.0, 0.0, 5.0),
    ],
}


@pytest.mark.parametrize("fs", FS)
@pytest.mark.parametrize("chain", sorted(CHAINS))
def test_cascade_of_ten_sections_is_stable_and_finite(chain, fs):
    sos = np.vstack([Filter(t, fc, g, q).sos(fs) for t, fc, g, q in CHAINS[chain]])
    assert stable(sos)
    assert np.all(np.isfinite(signal.sosfilt(sos, noise(fs))))


@needs_extended
@pytest.mark.parametrize("fs", FS)
@pytest.mark.parametrize("chain", sorted(CHAINS))
def test_cascade_of_ten_sections_stays_below_threshold(chain, fs):
    sos = np.vstack([Filter(t, fc, g, q).sos(fs) for t, fc, g, q in CHAINS[chain]])
    dev, ymax = deviation(sos, noise(fs))
    assert dev < THRESHOLD, f"{chain} @ {fs} Hz: {dev:.2e} (max|y| {ymax:.3g})"


def extreme_profile():
    left = [Filter(t, fc, g, q) for t, fc, g, q in CHAINS["zehn_Abschnitte_extrem"]]
    right = [Filter("peq", 1000.0, -3.0, 2.0)]
    return left, right, Profile(preamp_db=-12.0, channels={0: ChannelSpec(filters=left), 1: ChannelSpec(filters=right)})


@pytest.mark.parametrize("fs", FS)
def test_apply_profile_per_channel_chains_are_finite_and_independent(fs):
    left, right, prof = extreme_profile()
    data = np.stack([noise(fs, 1), noise(fs, 2)], axis=1)
    out, _ = dsp.apply_profile(data, fs, prof)
    assert np.all(np.isfinite(out)) and out.shape == data.shape
    # R wird nur von der eigenen Kette und dem gemeinsamen Preamp bestimmt, unabhaengig davon, wie extrem L ist
    only_r, _ = dsp.apply_profile(data, fs, Profile(preamp_db=-12.0, channels={1: ChannelSpec(filters=right)}))
    assert np.array_equal(out[:, 1], only_r[:, 1])


@needs_extended
@pytest.mark.parametrize("fs", FS)
def test_apply_profile_extreme_left_chain_stays_below_threshold(fs):
    left, _, prof = extreme_profile()
    data = np.stack([noise(fs, 1), noise(fs, 2)], axis=1)
    out, _ = dsp.apply_profile(data, fs, prof)
    sos_l = np.vstack([f.sos(fs) for f in left])
    ref = reference_longdouble(sos_l, data[:, 0]) * np.longdouble(10.0 ** (-12.0 / 20.0))
    assert float(np.max(np.abs(out[:, 0].astype(np.longdouble) - ref))) < THRESHOLD


def sine(fs, f, amp=0.3):
    return amp * np.sin(2 * np.pi * f * np.arange(fs) / fs)


@functools.lru_cache(maxsize=None)
def resonant_results():
    """Abweichung bei Resonanzanregung: Sinus bei fc und bei 2 fc (hoechstens 0,45 fs)."""
    rows = []
    for fs, fc, q, (ftype, gain) in GRID:
        sos = Filter(ftype, fc, gain, q).sos(fs)
        for label, f in (("bei fc", fc), ("bei 2 fc", min(2 * fc, 0.45 * fs))):
            dev, ymax = deviation(sos, sine(fs, f))
            rows.append({"case": (fs, fc, q, ftype, gain), "stimulus": label, "dev": dev, "ymax": ymax, "rel": dev / ymax})
    return rows


@needs_extended
def test_float64_deviation_for_resonant_sine_is_below_threshold_relative_to_the_output_peak():
    """Ergaenzend zur freigegebenen absoluten Schwelle (siehe Kopf, Befund a): bezogen auf die Ausgangsspitze."""
    rows = resonant_results()
    assert len(rows) == 768
    worst = sorted(rows, key=lambda r: r["rel"], reverse=True)[:5]
    msg = "; ".join(f"{describe(r)} {r['stimulus']}: {r['rel']:.2e} relativ, {r['dev']:.2e} absolut (max|y| {r['ymax']:.3g})" for r in worst)
    assert worst[0]["rel"] > 0, "Referenz ist nicht genauer als float64"
    assert worst[0]["rel"] < THRESHOLD, msg


@pytest.mark.parametrize("q", [50.0, 200.0, 1000.0])
@pytest.mark.parametrize("fc", [123.456, 1234.5678, 9876.54321])
def test_auto_preamp_finds_a_narrow_boost_that_falls_between_search_grid_points(fc, q):
    """Ein Peaking-Filter hat bei seiner Mittenfrequenz genau seinen Gain (+12 dB), also muss der Auto-Preamp -12,00 dB
    sein (auf 0,01 dB aufgerundet ist -12,01 dB wegen Rechenrauschen zulaessig). Das Suchgitter (8000 logarithmische
    Punkte, Schritt rund 0,1 %) trifft fc bei hohem Q nicht; erst die zusaetzlich geprueften Mittenfrequenzen finden die Spitze."""
    p = Profile(filters=[Filter("peq", fc, 12.0, q)]).auto_preamp_db(2, 48000)
    assert -12.01 - 1e-9 <= p <= -12.0 + 1e-9, f"fc {fc} q {q}: {p}"


def test_coefficients_match_an_independent_derivation_in_the_moderate_range():
    """Raster ohne Extreme (fc >= 63 Hz, q <= 5, fs <= 96 kHz). Gemessene Maximalabweichung 1,1e-14."""
    n = 0
    worst = 0.0
    for fs, fc, q, (ftype, gain) in itertools.product((44100, 48000, 96000), (63.0, 1000.0, 8000.0), (0.5, 0.7071, 2.0, 5.0), TYPES):
        got = Filter(ftype, fc, gain, q).sos(fs)[0]
        worst = max(worst, float(np.max(np.abs(got - ref_sos(ftype, fc, gain, q, fs)))))
        n += 1
    assert n == 288
    assert worst < 1e-11, worst


if __name__ == "__main__":  # Tabelle der Abweichungen ausgeben (siehe Kopfkommentar)
    warnings.filterwarnings("ignore", message="dtype=float(96|128) is not supported by lfilter", category=DeprecationWarning)
    res = grid_results()
    print(f"extended precision: {EXTENDED}, Faelle: {len(res)}, Schwelle {THRESHOLD:g}")
    for r in sorted(res, key=lambda r: r["dev"] if r["dev"] == r["dev"] else 0.0, reverse=True)[:15]:
        print(f"{describe(r):48s} dev={r['dev']:.3e} max|y|={r['ymax']:.3g} Polradius={r['radius']:.12f}")
    print(f"groesste Abweichung: {max(r['dev'] for r in res):.3e}; groesster Polradius: {max(r['radius'] for r in res):.12f}; "
          f"nicht stabil: {sum(not r['stable'] for r in res)}; nicht endlich: {sum(not r['finite'] for r in res)}; "
          f"Faelle mit erzwungenem Ausschwingen: {sum(r['decays'] for r in res)}")
