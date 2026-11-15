"""T5: Rueckwaertskompatibilitaet gegen 0.2.0.

Geprueft wird, dass Profile, die unter 0.2.0 funktionierten (mitgelieferte Beispiele, JSON mit gemeinsamen
Filtern, Kanalketten, Delay, manuellem Preamp, Aliasen und BOM, Equalizer-APO-Text mit Channel:/Delay:), im
aktuellen Stand dasselbe ergeben:

(a) Profile.to_dict() ist identisch zu 0.2.0, ebenso Neutralitaet, Import-Hinweise und validate()-Hinweise.
(b) Die Ausgabe von apply_profile entspricht dem Ergebnis von 0.2.0. Dafuer gibt es zwei voneinander unabhaengige
    Pruefungen: Kennzahlen (Effektivwert, Spitze, Laenge, Preamp, Delay) aus den gespeicherten 0.2.0-Werten, und
    ein Vergleich mit einer Referenzrechnung (dsp_reference.py), die die Biquad-Koeffizienten selbst herleitet
    (analoger Prototyp aus dem Audio EQ Cookbook plus scipy.signal.bilinear, nicht die Formeln aus dsp.py). Auch der
    Frequenzgang (kefplayer response) und die Auto-Preamp-Regel werden gegen diese Referenz geprueft.
(c) Warnungen und Fehlermeldungen (Wortlaut) sind identisch zu 0.2.0.

Die gespeicherten 0.2.0-Werte stehen in data/compat_020/expected.json (erzeugt mit data/compat_020/make_expected.py
aus dem unveraenderten Tag v0.2.0, Commit 8788254). Gespeichert sind absichtlich keine Rohdaten der Ausgabe,
sondern Kennzahlen mit Toleranz, weil Rohdaten zwischen scipy-Versionen in den letzten Stellen abweichen koennen.

Bewusste Abweichung gegenueber 0.2.0, hier NICHT als Kompatibilitaet geprueft: Equalizer-APO-"Delay:" ausserhalb
von 0 bis 50 ms wird seit der Korrektur nach 0.2.0 beim Import abgelehnt (test_dsp.py).

Aufbau der Faelle: compat_020_cases.py.
"""
import functools
import json
import math

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

import compat_020_cases as cc
from dsp_reference import chain_sos, chain_spec
from kefplayer import audio, dsp
from kefplayer.dsp import Profile

EXPECTED = json.loads(cc.EXPECTED.read_text(encoding="utf-8"))
PROFILES = cc.profile_paths()
NAMES = sorted(PROFILES)
CASES = sorted(cc.APPLY_CASES)
PROFILE_CASES = [(n, c) for n in NAMES for c in CASES]
LSB24 = 2.0 ** -23


@functools.lru_cache(maxsize=None)
def current(name):
    return cc.snapshot(PROFILES[name])


def pid(val):
    return str(val)


# ---- unabhaengige Referenzrechnung ---------------------------------------------------------------

def ref_apply(todict, data, fs, preamp_db, delays, guard_db):
    """Referenzausgabe: Kette je Kanal, gemeinsamer Preamp, Kanalpegel, ganze Samples Verzoegerung, Clipping-Schutz."""
    n, nch = data.shape
    out = np.zeros((n + max(delays), nch))
    for ch in range(nch):
        filters, gain_db = chain_spec(todict, ch)
        sos = chain_sos(filters, fs)
        x = signal.sosfilt(sos, data[:, ch]) if sos is not None else data[:, ch]
        out[delays[ch]:delays[ch] + n, ch] = x * 10 ** ((preamp_db + gain_db) / 20)
    return out * 10 ** (guard_db / 20)


# ---- Herkunft der gespeicherten Werte ---------------------------------------------------------------

def test_expected_file_is_from_020_and_covers_all_cases():
    meta = EXPECTED["_meta"]
    assert meta["erzeugt_mit"] == "kefplayer 0.2.0"
    assert meta["quell_commit"].startswith("8788254")
    assert meta["quell_arbeitsverzeichnis_veraendert"] is False
    assert set(EXPECTED["profiles"]) == set(PROFILES)
    assert set(EXPECTED["errors"]) == {key for key, *_ in cc.ERROR_CASES}
    for snap in EXPECTED["profiles"].values():
        assert set(snap["apply"]) == set(cc.APPLY_CASES)


def test_case_set_contains_the_features_it_claims_to_cover():
    """Absicherung gegen leere Abdeckung: Beispiele, JSON/APO, Kanalketten, Delay, manueller Preamp, Schutz, BOM."""
    assert {"examples/neutral.json", "examples/test_tiefpass_500.json"} <= set(PROFILES)
    assert any(n.endswith(".json") for n in NAMES) and any(n.endswith(".txt") for n in NAMES)
    snaps = EXPECTED["profiles"]
    assert any("channels" in s["to_dict"] for s in snaps.values())
    assert any(max(s["apply"]["stereo_44100"]["delay_samples"]) > 0 for s in snaps.values())
    assert any(s["to_dict"]["preamp_db"] != "auto" for s in snaps.values())
    assert any(s["apply"]["stereo_44100"]["guard_gain_db"] != 0 for s in snaps.values())
    # Auto-Preamp mit positiver Spitze auf BEIDEN Kanaelen (der lautere Kanal bestimmt den gemeinsamen Preamp)
    both = snaps["j5_boost_mixed.json"]
    assert both["apply"]["stereo_44100"]["preamp_db"] < -1.0 and both["response_db_48000"]["R"][3] != both["response_db_48000"]["L"][3]
    assert any(s["notes"] for s in snaps.values())
    assert PROFILES["j4_aliases_bom.json"].read_bytes().startswith(b"\xef\xbb\xbf")


# ---- (a) Serialisierung, Neutralitaet, Hinweise ----------------------------------------------------

@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_to_dict_is_identical_to_020(name):
    assert Profile.load(PROFILES[name]).to_dict() == EXPECTED["profiles"][name]["to_dict"]


@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_to_dict_roundtrip_is_stable(name):
    prof = Profile.load(PROFILES[name])
    again = Profile.from_dict(json.loads(json.dumps(prof.to_dict())))
    assert again.to_dict() == prof.to_dict()
    assert again.is_neutral() == prof.is_neutral()


@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_neutrality_import_notes_and_hints_are_identical_to_020(name):
    snap, exp = current(name), EXPECTED["profiles"][name]
    assert snap["is_neutral"] == exp["is_neutral"]
    assert snap["notes"] == exp["notes"]
    assert snap["validate"] == exp["validate"]


@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_response_db_matches_020(name):
    snap, exp = current(name), EXPECTED["profiles"][name]
    for label in ("L", "R"):
        assert snap["response_db_48000"][label] == pytest.approx(exp["response_db_48000"][label], abs=1e-9)


@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_response_db_matches_independent_reference(name):
    """Frequenzgang je Kanal (Werte aus 0.2.0) gegen die unabhaengige Referenz: Kette aus Prototyp und
    Bilinear-Transformation, scipy.signal.sosfreqz, Kanalpegel addiert."""
    exp = EXPECTED["profiles"][name]
    for ch, label in ((0, "L"), (1, "R")):
        filters, gain_db = chain_spec(exp["to_dict"], ch)
        sos = chain_sos(filters, 48000)
        mag = np.zeros(len(cc.FREQS)) if sos is None else 20 * np.log10(np.abs(signal.sosfreqz(sos, worN=cc.FREQS, fs=48000)[1]))
        assert exp["response_db_48000"][label] == pytest.approx(mag + gain_db, abs=1e-8)


# ---- (b) Anwendung auf feste Signale -----------------------------------------------------------------

@pytest.mark.parametrize("name,case", PROFILE_CASES, ids=pid)
def test_apply_info_is_identical_to_020(name, case):
    snap, exp = current(name)["apply"][case], EXPECTED["profiles"][name]["apply"][case]
    assert snap["shape"] == exp["shape"]
    assert snap["preamp_db"] == exp["preamp_db"]
    assert snap["delay_samples"] == exp["delay_samples"]
    assert snap["warnings"] == exp["warnings"]  # (c) Wortlaut der Warnungen
    for key in ("guard_gain_db", "peak_in", "peak_out"):
        assert snap[key] == pytest.approx(exp[key], abs=1e-9)


@pytest.mark.parametrize("name,case", PROFILE_CASES, ids=pid)
def test_apply_output_statistics_match_020(name, case):
    snap, exp = current(name)["apply"][case], EXPECTED["profiles"][name]["apply"][case]
    assert snap["rms"] == pytest.approx(exp["rms"], rel=1e-9, abs=1e-12)
    assert snap["peak"] == pytest.approx(exp["peak"], rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("name,case", PROFILE_CASES, ids=pid)
def test_apply_output_matches_independent_reference(name, case):
    """Ausgabe gegen eine im Test selbst gerechnete Kette. Preamp, Delays und Schutz stammen aus 0.2.0."""
    fs, nch = cc.APPLY_CASES[case]
    exp = EXPECTED["profiles"][name]
    ex = exp["apply"][case]
    data = cc.make_signal(fs, nch)
    out, _ = dsp.apply_profile(data, fs, Profile.load(PROFILES[name]), ceiling=cc.CEILING)
    ref = ref_apply(exp["to_dict"], data, fs, ex["preamp_db"] if not exp["is_neutral"] else 0.0, ex["delay_samples"], ex["guard_gain_db"])
    assert out.shape == ref.shape
    assert float(np.max(np.abs(out - ref))) < 1e-9
    if exp["is_neutral"]:
        assert np.array_equal(out, data)


@pytest.mark.parametrize("name", [n for n in NAMES if EXPECTED["profiles"][n]["to_dict"]["preamp_db"] == "auto"], ids=pid)
def test_auto_preamp_equals_independent_peak_rule(name):
    """Auto-Preamp = -aufgerundet(Spitze des hoechsten Kanals auf 0,01 dB), nur bei Spitze > 0 (sonst 0 dB)."""
    exp = EXPECTED["profiles"][name]
    for case, (fs, nch) in cc.APPLY_CASES.items():
        freqs = np.union1d(np.geomspace(5.0, fs / 2 * 0.9999, 20000), [f["fc"] for ch in range(nch) for f in chain_spec(exp["to_dict"], ch)[0] if f["fc"] < fs / 2])
        peak = -1e9
        for ch in range(nch):
            filters, gain_db = chain_spec(exp["to_dict"], ch)
            sos = chain_sos(filters, fs)
            mag = np.zeros_like(freqs) if sos is None else 20 * np.log10(np.abs(signal.sosfreqz(sos, worN=freqs, fs=fs)[1]))
            peak = max(peak, float(np.max(mag)) + gain_db)
        want = 0.0 if peak <= 0 else -math.ceil(peak * 100) / 100
        assert exp["apply"][case]["preamp_db"] == pytest.approx(want, abs=1e-9)


@pytest.mark.parametrize("name", NAMES, ids=pid)
def test_process_file_24bit_matches_020(name, tmp_path):
    """Dateiweg (24 Bit, ohne Dither): Bericht und decodierte Ausgabe gegen die 0.2.0-Werte."""
    fs, nch = cc.APPLY_CASES["stereo_44100"]
    exp = EXPECTED["profiles"][name]
    ex = exp["apply"]["stereo_44100"]
    src = tmp_path / "in.wav"
    sf.write(str(src), cc.make_signal(fs, nch), fs, subtype="PCM_24", format="WAV")
    rep = audio.process_file(src, tmp_path / "out", Profile.load(PROFILES[name]), fmt="WAV", dither=False)
    out, ofs = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert ofs == fs and list(out.shape) == ex["shape"]
    assert rep["preamp_db"] == ex["preamp_db"] and rep["delay_samples"] == ex["delay_samples"]
    assert rep["warnings"] == exp["validate"] + ex["warnings"]
    assert rep["neutral"] == exp["is_neutral"]
    for c in range(nch):
        assert float(np.sqrt(np.mean(out[:, c] ** 2))) == pytest.approx(ex["rms"][c], abs=3 * LSB24)
        assert float(np.max(np.abs(out[:, c]))) == pytest.approx(ex["peak"][c], abs=3 * LSB24)


def test_neutral_profile_file_roundtrip_is_bit_exact_like_020(tmp_path):
    fs, nch = cc.APPLY_CASES["stereo_44100"]
    src = tmp_path / "in.wav"
    sf.write(str(src), cc.make_signal(fs, nch), fs, subtype="PCM_24", format="WAV")
    rep = audio.process_file(src, tmp_path / "out", Profile.load(PROFILES["examples/neutral.json"]), fmt="WAV")
    a, _ = sf.read(str(src), dtype="int32", always_2d=True)
    b, _ = sf.read(rep["dst"], dtype="int32", always_2d=True)
    assert np.array_equal(a, b) and rep["neutral"] is True and rep["dither"] is False


# ---- (c) Fehlermeldungen --------------------------------------------------------------------------------

@pytest.mark.parametrize("key,kind,text,stage,fs", cc.ERROR_CASES, ids=[c[0] for c in cc.ERROR_CASES])
def test_error_messages_are_identical_to_020(key, kind, text, stage, fs):
    got = cc.run_error_case(kind, text, stage, fs)
    assert got == EXPECTED["errors"][key]
    assert got["type"] == "ValueError" and got["message"]
