import math

import numpy as np
import pytest

from kefplayer import dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile


def resp_db(flt, fs, f):
    return float(Profile(filters=[flt]).response_db(0, fs, [f])[0])


# ---- Filtereigenschaften (bekannte Eigenschaften der Filtertypen, kein Selbstvergleich) ----

@pytest.mark.parametrize("fs", [44100, 48000, 96000])
@pytest.mark.parametrize("gain", [-9.0, -3.0, 6.0])
def test_peq_gain_at_center(fs, gain):
    assert resp_db(Filter("peq", 1000, gain, 2.0), fs, 1000) == pytest.approx(gain, abs=1e-6)


def test_peq_far_away_is_flat():
    flt = Filter("peq", 1000, 6.0, 5.0)
    assert abs(resp_db(flt, 48000, 30)) < 0.05
    assert abs(resp_db(flt, 48000, 15000)) < 0.05


@pytest.mark.parametrize("kind", ["lowpass", "highpass"])
def test_butterworth_minus3db_at_fc(kind):
    assert resp_db(Filter(kind, 500), 48000, 500) == pytest.approx(-3.0103, abs=1e-3)


def test_lowpass_highpass_slopes():
    # 2. Ordnung: etwa 12 dB pro Oktave weit ausserhalb des Durchlassbereichs
    lp = Filter("lowpass", 200)
    d = resp_db(lp, 96000, 3200) - resp_db(lp, 96000, 6400)
    assert d == pytest.approx(12.0, abs=0.6)
    hp = Filter("highpass", 5000)
    d = resp_db(hp, 96000, 250) - resp_db(hp, 96000, 125)
    assert d == pytest.approx(12.0, abs=0.6)


def test_shelves_reach_gain_far_from_corner():
    ls = Filter("lowshelf", 200, 6.0, 0.7071)
    assert resp_db(ls, 48000, 2) == pytest.approx(6.0, abs=0.05)
    assert abs(resp_db(ls, 48000, 10000)) < 0.05
    hs = Filter("highshelf", 2000, -4.0, 0.7071)
    assert resp_db(hs, 96000, 30000) == pytest.approx(-4.0, abs=0.1)
    assert abs(resp_db(hs, 96000, 50)) < 0.05


def test_time_domain_matches_frequency_response():
    fs = 48000
    prof = Profile(preamp_db=0.0, filters=[Filter("peq", 1000, -6.0, 2.0)])
    t = np.arange(fs * 2) / fs
    # Zeitbereich (sosfilt) gegen Frequenzbereich (sosfreqz): zwei getrennte Rechenwege.
    # Bei 1 kHz ist der Sollwert zusaetzlich bekannt (= gain_db), bei 200 Hz liegt er wegen
    # der breiten Flanken (Q 2) leicht unter 0 dB und kommt aus dem Betragsgang.
    for f, expected in [(1000, -6.0), (200, float(prof.response_db(0, fs, [200])[0]))]:
        x = (0.1 * np.sin(2 * np.pi * f * t))[:, None]
        y, _ = dsp.apply_profile(x, fs, prof)
        a = np.sqrt(np.mean(x[fs:, 0] ** 2))
        b = np.sqrt(np.mean(y[fs:, 0] ** 2))
        assert 20 * math.log10(b / a) == pytest.approx(expected, abs=0.01)
    assert -0.1 < expected < 0.0


# ---- Validierung ----

def test_filter_validation():
    with pytest.raises(ValueError):
        Filter("peq", -5, 1, 1)
    with pytest.raises(ValueError):
        Filter("peq", 100, 1, 0)
    with pytest.raises(ValueError):
        Filter("peq", 100, 40, 1)
    with pytest.raises(ValueError):
        Filter("lowpass", 100, 3)
    with pytest.raises(ValueError):
        Filter("bandpass", 100)
    with pytest.raises(ValueError):
        Filter("peq", float("nan"), 1, 1)


def test_fc_above_nyquist_rejected():
    with pytest.raises(ValueError, match="Nyquist"):
        Filter("peq", 30000, 1, 1).sos(44100)


# ---- Neutralitaet, Headroom ----

def test_default_profile_is_neutral_and_identity():
    p = Profile()
    assert p.is_neutral()
    x = np.random.default_rng(1).uniform(-1, 1, (1000, 2))
    y, info = dsp.apply_profile(x, 44100, p, ceiling=0.5)
    assert np.array_equal(x, y)
    assert info["preamp_db"] == 0.0 and info["guard_gain_db"] == 0.0


def test_cuts_only_need_no_headroom():
    p = Profile(filters=[Filter("peq", 3000, -6.0, 2.0)])
    assert not p.is_neutral()
    assert p.auto_preamp_db(2, 44100) == 0.0


@pytest.mark.parametrize("fs", [44100, 96000])
def test_auto_headroom_brings_peak_to_zero_or_below(fs):
    p = Profile(filters=[Filter("peq", 3000, 6.0, 2.0), Filter("lowshelf", 120, 4.0, 0.7), Filter("peq", 8000, -3.0, 1.0)])
    pre = p.auto_preamp_db(2, fs)
    assert pre < 0
    peak = p.peak_gain_db(0, fs)
    assert peak + pre <= 1e-9
    assert peak + pre > -0.05  # nicht uebermaessig konservativ


def test_auto_headroom_is_common_for_both_channels():
    p = Profile(
        filters=[],
        channels={0: ChannelSpec(filters=[Filter("peq", 2000, 3.0, 2.0)]), 1: ChannelSpec(gain_db=-2.0)},
    )
    pre = p.auto_preamp_db(2, 48000)
    assert pre == pytest.approx(-3.0, abs=0.02)
    x = np.full((4800, 2), 0.1)
    y, info = dsp.apply_profile(x, 48000, p)
    # rechter Kanal: nur -2 dB Kanalpegel plus gemeinsamer Preamp
    assert 20 * math.log10(y[-1, 1] / 0.1) == pytest.approx(pre - 2.0, abs=0.01)


def test_manual_preamp_too_high_warns_and_guard_scales():
    p = Profile(preamp_db=6.0, filters=[Filter("peq", 1000, 3.0, 1.0)])
    x = np.full((4800, 1), 0.9)
    y, info = dsp.apply_profile(x, 48000, p, ceiling=0.99)
    assert any("Headroom" in w for w in info["warnings"])
    assert np.max(np.abs(y)) == pytest.approx(0.99)
    assert info["guard_gain_db"] < 0


def test_delay_shifts_one_channel_and_pads_other():
    fs = 48000
    p = Profile(channels={1: ChannelSpec(delay_ms=1.0)})
    x = np.zeros((1000, 2))
    x[100, :] = 0.5
    y, info = dsp.apply_profile(x, fs, p)
    assert info["delay_samples"] == [0, 48]
    assert y.shape == (1048, 2)
    assert np.argmax(np.abs(y[:, 0])) == 100
    assert np.argmax(np.abs(y[:, 1])) == 148


def test_profile_channel_beyond_file_channels_warns():
    p = Profile(channels={1: ChannelSpec(gain_db=-1.0)})
    _, info = dsp.apply_profile(np.zeros((100, 1)), 44100, p)
    assert any("Kanal 1" in w for w in info["warnings"])


# ---- Profile laden, Projektregel-Hinweise ----

def test_profile_dict_roundtrip_and_strictness():
    d = {
        "name": "t",
        "preamp_db": "auto",
        "filters": [{"type": "pk", "fc": 1000, "gain_db": -2, "q": 2}],
        "channels": {"R": {"gain_db": -1.0, "delay_ms": 0.5, "filters": [{"type": "lp", "fc": 15000}]}},
    }
    p = Profile.from_dict(d)
    assert p.filters[0].type == "peq"
    assert p.channels[1].delay_ms == 0.5
    p2 = Profile.from_dict(p.to_dict())
    assert p2.to_dict() == p.to_dict()
    with pytest.raises(ValueError):
        Profile.from_dict({"filterz": []})
    with pytest.raises(ValueError):
        Profile.from_dict({"filters": [{"type": "peq", "fc": 100, "gane_db": 1}]})
    with pytest.raises(ValueError):
        Profile.from_dict({"channels": {"C": {}}})
    with pytest.raises(ValueError):
        Profile.from_dict({"preamp_db": "loud"})


def test_load_json_and_apo(tmp_path):
    j = tmp_path / "p.json"
    j.write_text('{"filters": [{"type": "peq", "fc": 2000, "gain_db": -1, "q": 1}]}', encoding="utf-8")
    assert Profile.load(j).name == "p"
    a = tmp_path / "q.txt"
    a.write_text("Preamp: -2 dB\nFilter 1: ON PK Fc 2000 Hz Gain -1 dB Q 1\n", encoding="utf-8")
    prof = Profile.load(a)
    assert prof.preamp_db == -2.0 and prof.filters[0].fc == 2000


def test_parse_apo_channels_delay_and_unsupported():
    text = """
# Kommentar
Preamp: -3.5 dB
Filter 1: ON PK Fc 1000 Hz Gain -2,5 dB Q 2.0
Filter 2: OFF PK Fc 2000 Hz Gain 3 dB Q 2
Channel: L
Filter 3: ON HP Fc 80 Hz
Delay: 0.3 ms
Channel: R
Filter 4: ON HSC Fc 8000 Hz Gain -1 dB
Channel: L R
Filter 5: ON LP Fc 18000 Hz
"""
    p = dsp.parse_apo(text)
    assert p.preamp_db == -3.5
    assert [f.fc for f in p.filters] == [1000, 18000]
    assert p.filters[0].gain_db == -2.5
    assert [f.type for f in p.channels[0].filters] == ["highpass"]
    assert p.channels[0].delay_ms == 0.3
    assert p.channels[1].filters[0].type == "highshelf"
    assert any("Q fehlt" in n for n in p.notes)
    for bad in ["Include: x.txt", "Filter 1: ON PK Fc 100 Hz Gain 1 dB", "Filter 1: ON PK Fc 100 Hz Gain 1 dB BW Oct 1",
                "Channel: C", "Delay: 20 samples", "Filter 1: ON NO Fc 100 Hz"]:
        with pytest.raises(ValueError):
            dsp.parse_apo(bad)


def test_example_profiles_load_and_behave():
    from pathlib import Path
    ex = Path(__file__).resolve().parent.parent / "examples"
    neutral = Profile.load(ex / "neutral.json")
    assert neutral.is_neutral() and neutral.validate() == []
    lp = Profile.load(ex / "test_tiefpass_500.json")
    assert not lp.is_neutral() and lp.validate() == []  # kein Bass-, Boost- oder Mittelungshinweis noetig
    assert float(lp.response_db(0, 44100, [500])[0]) == pytest.approx(-3.0103, abs=1e-3)
    assert lp.auto_preamp_db(2, 44100) == 0.0


def test_validate_warnings_follow_project_rules():
    assert Profile().validate() == []
    bass = Profile(filters=[Filter("peq", 60, -3.0, 5.0)])
    assert any("Bassbereich" in w for w in bass.validate())
    boost = Profile(filters=[Filter("peq", 3000, 2.0, 2.0)])
    w = boost.validate()
    assert any("Boost" in x and "Ausloeschung" in x for x in w)
    assert any("gemittelt" in x for x in w)
    ch = Profile(channels={0: ChannelSpec(gain_db=-1.0)})
    assert any("nur auf diesen Signalweg" in x for x in ch.validate())
