"""Absicherung der vorhandenen, unabhaengigen L/R-Funktionen (Stand 0.2.0): EQ, Pegel, Delay.

Alle Tests beschreiben das aktuelle Verhalten, ohne Produktivcode zu aendern. Die Signale sind synthetisch
und deterministisch (Impulse, Sinus, Rauschen mit festem Seed, L und R getrennt). Die Sollwerte stehen fest im
Test (Filtereigenschaften, dB-Definition, Samplezahlen) und werden nicht aus dem getesteten Code abgeleitet.

Gemeinsamer Preamp: Der automatische Preamp senkt bei einem Boost beide Kanaele gleich ab. Tests, die einen
einzelnen Kanal beurteilen, nutzen deshalb entweder nur Absenkungen (Preamp 0 dB), einen festen Preamp von 0 dB
oder rechnen den Preamp ausdruecklich in den Sollwert ein.
"""
import numpy as np
import pytest
import soundfile as sf

from kefplayer import audio, dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile

FS = 48000
N_IR = 2 ** 15  # Laenge der Impulsantwort, weit laenger als das Ausschwingen der Testfilter


def tone(freq=1000.0, seconds=1.0, fs=FS, amp=0.2):
    t = np.arange(int(seconds * fs)) / fs
    return amp * np.sin(2 * np.pi * freq * t)


def level_db(y, x, fs=FS):
    """Pegelaenderung y gegenueber x in dB, ohne die ersten 0,25 s (Einschwingen)."""
    k = fs // 4
    return 20 * np.log10(np.sqrt(np.mean(y[k:] ** 2)) / np.sqrt(np.mean(x[k:] ** 2)))


def impulse_pair():
    """Einheitsimpuls auf L und R."""
    x = np.zeros((N_IR, 2))
    x[0, :] = 1.0
    return x


def mag_db(y, f, fs=FS):
    """Betrag der Impulsantwort y bei der Frequenz f in dB (direkte DFT, ohne Gitterfehler)."""
    n = np.arange(y.size)
    return float(20 * np.log10(abs(np.sum(y * np.exp(-2j * np.pi * f * n / fs)))))


# ---- L/R-EQ: getrennte Ketten, unterschiedliche Einstellungen, Kanaltrennung -----------------

@pytest.mark.parametrize("ch", [0, 1])
def test_eq_channel_filter_changes_only_its_own_channel(ch):
    rng = np.random.default_rng(1)
    x = rng.uniform(-0.3, 0.3, (FS, 2))
    p = Profile(channels={ch: ChannelSpec(filters=[Filter("peq", 2000, -3.0, 2.0)])})
    y, info = dsp.apply_profile(x, FS, p)
    other = 1 - ch
    assert info["preamp_db"] == 0.0  # nur Absenkung: kein Headroom noetig
    assert y.shape == x.shape
    assert np.array_equal(y[:, other], x[:, other])  # bitgleich, kein Uebersprechen
    assert not np.allclose(y[:, ch], x[:, ch])


@pytest.mark.parametrize("ch", [0, 1])
def test_eq_channel_filter_has_its_nominal_gain_on_that_channel(ch):
    x = np.stack([tone(2000.0), tone(2000.0)], axis=1)
    p = Profile(channels={ch: ChannelSpec(filters=[Filter("peq", 2000, -3.0, 2.0)])})
    y, _ = dsp.apply_profile(x, FS, p)
    assert level_db(y[:, ch], x[:, ch]) == pytest.approx(-3.0, abs=0.05)
    assert level_db(y[:, 1 - ch], x[:, 1 - ch]) == pytest.approx(0.0, abs=1e-9)


def test_eq_impulse_response_follows_each_channels_own_chain():
    p = Profile(
        channels={
            0: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)]),
            1: ChannelSpec(filters=[Filter("peq", 3000, -3.0, 2.0)]),
        }
    )
    y, info = dsp.apply_profile(impulse_pair(), FS, p)
    assert info["preamp_db"] == 0.0
    assert mag_db(y[:, 0], 1000) == pytest.approx(-6.0, abs=0.01)
    assert mag_db(y[:, 1], 3000) == pytest.approx(-3.0, abs=0.01)
    # Die Kette des anderen Kanals wirkt nicht: dort bleibt nur der Auslaeufer des eigenen Filters (unter 0,5 dB).
    # Vertauschte Ketten gaeben hier -3 dB bzw. -6 dB.
    assert mag_db(y[:, 0], 3000) > -0.5
    assert mag_db(y[:, 1], 1000) > -0.5


def test_eq_different_filter_types_per_channel():
    # 2. Ordnung Butterworth bei 2 kHz: -3,01 dB bei fc, 12 dB je Oktave. L Tiefpass, R Hochpass.
    p = Profile(
        channels={
            0: ChannelSpec(filters=[Filter("lowpass", 2000)]),
            1: ChannelSpec(filters=[Filter("highpass", 2000)]),
        }
    )
    y, _ = dsp.apply_profile(impulse_pair(), FS, p)
    assert mag_db(y[:, 0], 500) > -0.1
    assert mag_db(y[:, 0], 2000) == pytest.approx(-3.01, abs=0.05)
    assert mag_db(y[:, 0], 8000) < -20.0
    assert mag_db(y[:, 1], 500) < -20.0
    assert mag_db(y[:, 1], 2000) == pytest.approx(-3.01, abs=0.05)
    assert mag_db(y[:, 1], 8000) > -0.1


def test_eq_each_channel_uses_its_own_chain_and_common_filters_apply_to_both():
    # Gemeinsam -2 dB bei 1 kHz, nur L zusaetzlich -4 dB: L = -6 dB, R = -2 dB. Vertauschte Ketten ergaeben andere Werte.
    x = np.stack([tone(1000.0), tone(1000.0)], axis=1)
    p = Profile(
        filters=[Filter("peq", 1000, -2.0, 2.0)],
        channels={0: ChannelSpec(filters=[Filter("peq", 1000, -4.0, 2.0)])},
    )
    y, info = dsp.apply_profile(x, FS, p)
    assert info["preamp_db"] == 0.0
    assert level_db(y[:, 0], x[:, 0]) == pytest.approx(-6.0, abs=0.05)
    assert level_db(y[:, 1], x[:, 1]) == pytest.approx(-2.0, abs=0.05)


@pytest.mark.parametrize("ch", [0, 1])
def test_eq_silent_channel_stays_exactly_silent(ch):
    x = np.zeros((FS, 2))
    x[:, ch] = tone(1000.0)
    p = Profile(channels={ch: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)])})
    y, _ = dsp.apply_profile(x, FS, p)
    assert np.all(y[:, 1 - ch] == 0.0)
    assert level_db(y[:, ch], x[:, ch]) == pytest.approx(-6.0, abs=0.05)


@pytest.mark.parametrize("ch", [0, 1])
def test_eq_filter_on_the_silent_channel_does_not_touch_the_signal_channel(ch):
    x = np.zeros((FS, 2))
    x[:, ch] = tone(1000.0)
    p = Profile(channels={1 - ch: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)])})
    y, _ = dsp.apply_profile(x, FS, p)
    assert np.array_equal(y[:, ch], x[:, ch])
    assert np.all(y[:, 1 - ch] == 0.0)


def test_eq_boost_on_one_channel_is_compensated_by_the_shared_preamp():
    # +6 dB nur auf L: Auto-Preamp -6 dB fuer beide Kanaele. L netto 0 dB, R netto -6 dB, Abstand bleibt +6 dB.
    p = Profile(channels={0: ChannelSpec(filters=[Filter("peq", 1000, 6.0, 2.0)])})
    y, info = dsp.apply_profile(impulse_pair(), FS, p)
    pre = info["preamp_db"]
    assert -6.01 <= pre <= -6.0
    assert mag_db(y[:, 0], 1000) == pytest.approx(0.0, abs=0.011)
    for f in (100, 1000, 10000):  # R hat keine Kette: flach, nur Preamp
        assert mag_db(y[:, 1], f) == pytest.approx(pre, abs=1e-6)
    assert mag_db(y[:, 0], 1000) - mag_db(y[:, 1], 1000) == pytest.approx(6.0, abs=0.011)


def test_eq_boost_with_manual_preamp_leaves_the_other_channel_bit_identical():
    p = Profile(preamp_db=0.0, channels={0: ChannelSpec(filters=[Filter("peq", 1000, 6.0, 2.0)])})
    x = impulse_pair()
    y, info = dsp.apply_profile(x, FS, p)
    assert info["preamp_db"] == 0.0
    assert mag_db(y[:, 0], 1000) == pytest.approx(6.0, abs=0.01)
    assert np.array_equal(y[:, 1], x[:, 1])


def test_eq_channel_filters_reach_the_written_file(tone_factory, tmp_path):
    # Ueber audio.process_file bis in die Datei, 24 Bit (kein Dither): R bleibt im Rahmen der Quantisierung gleich.
    src = tone_factory("lr_filter", freq=2000, seconds=1.0, fs=FS, subtype="PCM_24")
    prof = Profile(channels={0: ChannelSpec(filters=[Filter("peq", 2000, -3.0, 2.0)])})
    rep = audio.process_file(src, tmp_path / "o", prof)
    a, _, _ = audio.read_audio(src)
    b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert level_db(b[:, 0], a[:, 0]) == pytest.approx(-3.0, abs=0.05)
    assert np.max(np.abs(b[:, 1] - a[:, 1])) <= 2.0 ** -23


# ---- L/R-Pegel: unabhaengige Verstaerkung, +-12-dB-Grenzen, tatsaechliche Amplituden ----------

@pytest.mark.parametrize(
    "gl, gr",
    [(6.0, -3.0), (12.0, -12.0), (-12.0, 12.0), (0.0, 12.0), (-12.0, 0.0), (-6.0, -6.0)],
)
def test_level_output_amplitudes_match_independent_channel_gains(gl, gr):
    # Fester Preamp 0 dB, damit der Headroom die Sollwerte nicht veraendert. Getrennte Impulse auf L und R.
    x = np.zeros((400, 2))
    x[100, 0] = 0.1
    x[300, 1] = 0.05
    p = Profile(preamp_db=0.0, channels={0: ChannelSpec(gain_db=gl), 1: ChannelSpec(gain_db=gr)})
    y, info = dsp.apply_profile(x, FS, p)
    assert info["preamp_db"] == 0.0
    assert y.shape == x.shape
    assert y[100, 0] == pytest.approx(0.1 * 10 ** (gl / 20), rel=1e-12)
    assert y[300, 1] == pytest.approx(0.05 * 10 ** (gr / 20), rel=1e-12)
    rest = np.ones(y.shape, dtype=bool)
    rest[100, 0] = rest[300, 1] = False
    assert np.all(y[rest] == 0.0)  # kein Uebersprechen, keine weiteren Samples


@pytest.mark.parametrize("g", [12.0, -12.0])
def test_level_limit_of_12_db_is_accepted(g):
    assert ChannelSpec(gain_db=g).gain_db == g
    p = Profile.from_dict({"channels": {"L": {"gain_db": g}, "R": {"gain_db": g}}})
    assert p.channel_gain_db(0) == g and p.channel_gain_db(1) == g


@pytest.mark.parametrize("g", [12.01, -12.01])
def test_level_beyond_12_db_is_rejected(g):
    with pytest.raises(ValueError, match="gain_db"):
        ChannelSpec(gain_db=g)
    for key in ("L", "R"):
        with pytest.raises(ValueError, match="gain_db"):
            Profile.from_dict({"channels": {key: {"gain_db": g}}})


def test_level_auto_headroom_is_shared_and_keeps_the_balance():
    # L +6 dB, R -3 dB: Auto-Preamp -6 dB fuer beide. Netto L 0 dB, R -9 dB (je +-0,01 dB durch Rundung des Preamps).
    x = np.zeros((400, 2))
    x[100, 0] = 0.1
    x[300, 1] = 0.1
    p = Profile(channels={0: ChannelSpec(gain_db=6.0), 1: ChannelSpec(gain_db=-3.0)})
    y, info = dsp.apply_profile(x, FS, p)
    assert -6.01 <= info["preamp_db"] <= -6.0
    assert 20 * np.log10(y[100, 0] / 0.1) == pytest.approx(0.0, abs=0.011)
    assert 20 * np.log10(y[300, 1] / 0.1) == pytest.approx(-9.0, abs=0.011)
    assert 20 * np.log10(y[100, 0] / y[300, 1]) == pytest.approx(9.0, abs=1e-9)  # Abstand unabhaengig vom Preamp


def test_level_reaches_the_written_file(tone_factory, tmp_path):
    src = tone_factory("lr_level", freq=1000, seconds=1.0, fs=FS, subtype="PCM_24")
    prof = Profile(preamp_db=0.0, channels={0: ChannelSpec(gain_db=-6.0), 1: ChannelSpec(gain_db=3.0)})
    rep = audio.process_file(src, tmp_path / "o", prof)
    a, _, _ = audio.read_audio(src)
    b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert level_db(b[:, 0], a[:, 0]) == pytest.approx(-6.0, abs=0.01)
    assert level_db(b[:, 1], a[:, 1]) == pytest.approx(3.0, abs=0.01)


# ---- L/R-Delay: unabhaengig, Sample-Rundung, Grenzen 0-50 ms, Dateilaenge --------------------

@pytest.mark.parametrize("ch", [0, 1])
@pytest.mark.parametrize(
    "fs, ms, samples",
    [
        (44100, 0.0, 0),
        (44100, 0.011, 0),     # 0,48 Samples -> 0
        (44100, 0.1, 4),       # 4,41
        (44100, 0.2, 9),       # 8,82 -> aufgerundet
        (44100, 0.5, 22),      # 22,05
        (48000, 0.25, 12),
        (48000, 1.0, 48),
        (96000, 0.1, 10),      # 9,6 -> aufgerundet
        (192000, 50.0, 9600),  # Obergrenze
    ],
)
def test_delay_is_independent_and_rounded_to_whole_samples(ch, fs, ms, samples):
    x = np.zeros((600, 2))
    x[100, 0] = 0.5   # getrennte L/R-Impulse mit verschiedener Amplitude
    x[300, 1] = 0.25
    y, info = dsp.apply_profile(x, fs, Profile(channels={ch: ChannelSpec(delay_ms=ms)}))
    shift = [0, 0]
    shift[ch] = samples
    assert info["delay_samples"] == shift
    assert y.shape[0] == 600 + samples
    for c, pos, amp in ((0, 100, 0.5), (1, 300, 0.25)):
        p = pos + shift[c]
        assert int(np.argmax(np.abs(y[:, c]))) == p
        assert y[p, c] == pytest.approx(amp)
        assert np.count_nonzero(y[:, c]) == 1  # Impuls unveraendert, sonst Stille


def test_delay_limits_zero_and_fifty_ms_accepted_outside_rejected():
    assert ChannelSpec(delay_ms=0.0).delay_ms == 0.0
    assert ChannelSpec(delay_ms=50.0).delay_ms == 50.0
    for key in ("L", "R"):
        assert Profile.from_dict({"channels": {key: {"delay_ms": 50.0}}}).channel_delay_ms(0 if key == "L" else 1) == 50.0
    for bad in (-0.01, 50.01):
        with pytest.raises(ValueError, match="delay_ms"):
            ChannelSpec(delay_ms=bad)
        for key in ("L", "R"):
            with pytest.raises(ValueError, match="delay_ms"):
                Profile.from_dict({"channels": {key: {"delay_ms": bad}}})


def test_delay_both_channels_keeps_content_and_pads_to_common_length():
    rng = np.random.default_rng(2)
    x = rng.uniform(-0.3, 0.3, (2000, 2))
    p = Profile(channels={0: ChannelSpec(delay_ms=0.5), 1: ChannelSpec(delay_ms=1.0)})
    y, info = dsp.apply_profile(x, FS, p)
    assert info["delay_samples"] == [24, 48]
    assert y.shape == (2000 + 48, 2)  # Laenge = Eingang + groesstes Delay
    for ch, d in ((0, 24), (1, 48)):
        assert np.all(y[:d, ch] == 0.0)
        assert np.array_equal(y[d:d + 2000, ch], x[:, ch])  # Inhalt unveraendert, nur verschoben
        assert np.all(y[d + 2000:, ch] == 0.0)


@pytest.mark.parametrize(
    "fs, ms_l, ms_r, d_l, d_r",
    [
        (44100, 0.0, 50.0, 0, 2205),   # Obergrenze nur R
        (48000, 0.5, 1.0, 24, 48),     # beide Kanaele
        (96000, 50.0, 0.0, 4800, 0),   # Obergrenze nur L
    ],
)
def test_delay_resulting_file_length_and_content(tone_factory, tmp_path, fs, ms_l, ms_r, d_l, d_r):
    src = tone_factory(f"lr_delay_{fs}", seconds=0.5, fs=fs, subtype="PCM_24")
    prof = Profile(channels={0: ChannelSpec(delay_ms=ms_l), 1: ChannelSpec(delay_ms=ms_r)})
    rep = audio.process_file(src, tmp_path / "o", prof)
    a, _, _ = audio.read_audio(src)
    b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    n = a.shape[0]
    assert b.shape[0] == n + max(d_l, d_r)
    for ch, d in ((0, d_l), (1, d_r)):
        assert np.all(b[:d, ch] == 0.0)
        assert np.max(np.abs(b[d:d + n, ch] - a[:, ch])) <= 2.0 ** -23
        assert np.all(b[d + n:, ch] == 0.0)
