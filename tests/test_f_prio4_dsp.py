"""Konsolidierter Fehlerbehebungsauftrag, Prioritaet 4: F-18 (NaN/Inf) und F-19 (JSON-Struktur)."""
import json
import math

import pytest

from kefplayer import cli, dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile

BAD_NUMBERS = [math.nan, math.inf, -math.inf]


# ---- F-18 ----

@pytest.mark.parametrize("v", BAD_NUMBERS)
def test_f18_channelspec_gain_und_delay_endlich(v):
    with pytest.raises(ValueError):
        ChannelSpec(gain_db=v)
    with pytest.raises(ValueError):
        ChannelSpec(delay_ms=v)


@pytest.mark.parametrize("v", BAD_NUMBERS)
def test_f18_profile_preamp_direkt_endlich(v):
    with pytest.raises(ValueError):
        Profile(preamp_db=v)


@pytest.mark.parametrize("v", BAD_NUMBERS)
def test_f18_json_import_nan_inf(tmp_path, v):
    lit = {math.inf: "Infinity", -math.inf: "-Infinity"}.get(v, "NaN")  # Python-json akzeptiert diese Literale
    for body in (f'{{"preamp_db": {lit}}}',
                 f'{{"channels": {{"L": {{"gain_db": {lit}}}}}}}',
                 f'{{"channels": {{"L": {{"delay_ms": {lit}}}}}}}',
                 f'{{"filters": [{{"type": "peaking", "fc": 100, "gain_db": {lit}, "q": 1}}]}}'):
        p = tmp_path / "p.json"
        p.write_text(body)
        with pytest.raises(ValueError):
            Profile.load(p)


def test_f18_apo_ueberlange_zahl_ist_nicht_endlich(tmp_path):
    p = tmp_path / "p.txt"
    p.write_text("Preamp: -" + "9" * 400 + " dB\n")
    with pytest.raises(ValueError):
        Profile.load(p)


def test_f18_grenzwerte_und_gueltige_profile():
    assert ChannelSpec(gain_db=dsp.MAX_CHANNEL_GAIN_DB).gain_db == dsp.MAX_CHANNEL_GAIN_DB
    with pytest.raises(ValueError):
        ChannelSpec(gain_db=dsp.MAX_CHANNEL_GAIN_DB + 0.01)
    with pytest.raises(ValueError):
        ChannelSpec(delay_ms=dsp.MAX_DELAY_MS + 0.01)
    assert ChannelSpec(delay_ms=0.0).delay_ms == 0.0 and ChannelSpec(delay_ms=dsp.MAX_DELAY_MS).delay_ms == dsp.MAX_DELAY_MS
    assert Profile().preamp_db is None and Profile(preamp_db=-3.5).preamp_db == -3.5
    p = Profile.from_dict({"name": "ok", "preamp_db": -2, "filters": [{"type": "peaking", "fc": 100, "gain_db": -3, "q": 2}],
                           "channels": {"L": {"gain_db": 1.0, "delay_ms": 0.5}}})
    assert p.preamp_db == -2.0 and p.channels[0].delay_ms == 0.5


# ---- F-19 ----

@pytest.mark.parametrize("root", ["[]", "null", "42", '"text"', "true"])
def test_f19_wurzeltyp(tmp_path, root):
    p = tmp_path / "p.json"
    p.write_text(root)
    with pytest.raises(ValueError, match="JSON-Objekt"):
        Profile.load(p)
    with pytest.raises(ValueError, match="JSON-Objekt"):
        Profile.from_dict(json.loads(root))


@pytest.mark.parametrize("body", [
    {"channels": []}, {"channels": [1]}, {"channels": "L"}, {"channels": 0}, {"channels": {"L": []}},
    {"channels": {"L": 5}}, {"channels": {"L": {"filters": "x"}}}, {"channels": {"L": {"filters": [1]}}},
    {"filters": "x"}, {"filters": 0}, {"filters": {"type": "peaking"}}, {"filters": [1]}, {"filters": ["peaking"]},
    {"filters": [None]}, {"filters": [{"type": 5, "fc": 100}]}, {"filters": [{"type": "peaking", "fc": None}]},
    {"filters": [{"type": "peaking", "fc": [1]}]}, {"filters": [{"type": "peaking", "fc": "abc"}]},
    {"filters": [{"type": "peaking", "fc": True}]}, {"preamp_db": [1]}, {"preamp_db": True},
    {"channels": {"L": {"gain_db": None}}}, {"channels": {"L": {"delay_ms": "x"}}},
])
def test_f19_falsche_typen_ergeben_valueerror(body):
    with pytest.raises(ValueError):
        Profile.from_dict(body)


@pytest.mark.parametrize("root", ["[]", "null", "42", '"text"', '{"filters": "x"}', '{"preamp_db": NaN}'])
def test_f19_cli_kontrollierter_fehlercode_ohne_traceback(tmp_path, root, capsys):
    p = tmp_path / "p.json"
    p.write_text(root)
    rc = cli.main(["response", "--profile", str(p)])
    assert rc not in (0, None)
    assert "Traceback" not in capsys.readouterr().err
