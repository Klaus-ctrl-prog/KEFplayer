import numpy as np
import pytest

from kefplayer import dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile
from kefplayer.streaming_dsp import StreamingProfile

FS = 48000


def _noise(n=20000, ch=2, seed=1):
    return np.random.default_rng(seed).uniform(-0.5, 0.5, (n, ch))


def _profile():
    return Profile(
        name="t", preamp_db=None,
        filters=[Filter("peq", 2000, -2.0, 2.0), Filter("lowshelf", 120, 3.0, 0.7)],
        channels={1: ChannelSpec(gain_db=-1.0, delay_ms=0.5, filters=[Filter("peq", 800, -1.5, 3.0)])},
    )


def _run_blocks(sp, x, sizes):
    outs, i, k = [], 0, 0
    while i < len(x):
        s = sizes[k % len(sizes)]
        outs.append(sp.process(x[i:i + s]))
        i += s
        k += 1
    return np.vstack(outs)


@pytest.mark.parametrize("sizes", [[4096], [1], [7, 333, 1024], [5000, 3]])
def test_blockweise_gleich_ganzes_stueck(sizes):
    x = _noise()
    ref, info = dsp.apply_profile(x, FS, _profile())
    sp = StreamingProfile(_profile(), FS, 2)
    out = _run_blocks(sp, x, sizes)
    assert out.shape == x.shape
    assert sp.preamp_db == info["preamp_db"]
    assert sp.delay_samples == info["delay_samples"]
    np.testing.assert_allclose(out, ref[: len(x)], atol=1e-12)


def test_neutral_unveraendert_und_eingabe_unberuehrt():
    x = _noise(1000)
    x0 = x.copy()
    sp = StreamingProfile(Profile(), FS, 2)
    out = sp.process(x)
    np.testing.assert_array_equal(out, x0)
    np.testing.assert_array_equal(x, x0)


def test_ohne_zustand_waere_es_anders():
    # Sabotage-Probe: Zustand je Block zuruecksetzen -> weicht messbar ab
    x = _noise(8000)
    ref, _ = dsp.apply_profile(x, FS, _profile())
    sp = StreamingProfile(_profile(), FS, 2)
    outs = []
    for i in range(0, len(x), 1000):
        sp.reset()
        outs.append(sp.process(x[i:i + 1000]))
    assert np.max(np.abs(np.vstack(outs) - ref[: len(x)])) > 1e-4


def test_reset_loescht_nachklingen():
    sp = StreamingProfile(_profile(), FS, 2)
    sp.process(_noise(5000))
    sp.reset()
    out = sp.process(np.zeros((2000, 2)))
    assert np.max(np.abs(out)) == 0.0


def test_falsche_form_und_kanalzahl():
    sp = StreamingProfile(_profile(), FS, 2)
    with pytest.raises(ValueError):
        sp.process(np.zeros((10, 3)))
    with pytest.raises(ValueError):
        sp.process(np.zeros(10))
    with pytest.raises(ValueError):
        StreamingProfile(_profile(), FS, 0)


def test_manueller_preamp_warnung_und_ueberzaehliger_kanal():
    p = Profile(preamp_db=6.0, filters=[Filter("peq", 2000, 3.0, 2.0)], channels={5: ChannelSpec(gain_db=-1.0)})
    sp = StreamingProfile(p, FS, 2)
    assert any("Clipping moeglich" in w for w in sp.warnings)
    assert any("Kanal 5" in w for w in sp.warnings)


def test_mono_und_delay_blockgrenze_kleiner_als_delay():
    p = Profile(channels={0: ChannelSpec(delay_ms=2.0)})
    x = _noise(3000, 1)
    ref, _ = dsp.apply_profile(x, FS, p)
    sp = StreamingProfile(p, FS, 1)
    out = _run_blocks(sp, x, [10, 50])  # Delay 96 Samples > Block
    np.testing.assert_allclose(out, ref[: len(x)], atol=1e-12)
