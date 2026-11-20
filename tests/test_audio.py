import json

import numpy as np
import pytest
import soundfile as sf

from kefplayer import audio, dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile
from kefplayer.runlog import RunLog


@pytest.mark.parametrize("fs", [44100, 48000, 96000])
@pytest.mark.parametrize("subtype", ["PCM_16", "PCM_24"])
def test_neutral_roundtrip_is_bit_exact(tone_factory, tmp_path, fs, subtype):
    src = tone_factory("n", fs=fs, subtype=subtype, seconds=0.5)
    rep = audio.process_file(src, tmp_path / "out", Profile())
    a, _ = sf.read(str(src), dtype="int32", always_2d=True)
    b, fs2 = sf.read(rep["dst"], dtype="int32", always_2d=True)
    assert fs2 == fs
    assert np.array_equal(a, b)
    assert rep["neutral"] and not rep["dither"]
    assert rep["out"]["subtype"] == subtype and rep["out"]["samplerate"] == fs


def test_format_choice_and_conversion(tone_factory, tmp_path):
    src = tone_factory("w24", subtype="PCM_24", fmt="WAV", seconds=0.3)
    info = audio.probe(src)
    assert audio.choose_output(info, "FLAC") == "PCM_24"
    assert audio.choose_output(info, "WAV") == "PCM_24"
    assert audio.choose_output(info, "FLAC", bits=16) == "PCM_16"
    with pytest.raises(ValueError):
        audio.choose_output(info, "FLAC", bits=32)
    with pytest.raises(ValueError):
        audio.choose_output(info, "MP3")
    rep = audio.process_file(src, tmp_path / "o", Profile(), fmt="FLAC", bits=16)
    assert rep["out"]["format"] == "FLAC" and rep["out"]["subtype"] == "PCM_16"
    assert rep["dither"]  # 24 -> 16 Bit: neu quantisiert, also Dither
    f32 = tone_factory("f32", subtype="FLOAT", fmt="WAV", seconds=0.3)
    assert audio.choose_output(audio.probe(f32), "WAV") == "FLOAT"
    assert audio.choose_output(audio.probe(f32), "FLAC") == "PCM_24"


def test_filter_cuts_target_band_only(tone_factory, tmp_path):
    prof = Profile(filters=[Filter("peq", 1000, -6.0, 2.0)])
    for freq, expected in [(1000, -6.0), (100, 0.0)]:
        src = tone_factory(f"t{freq}", freq=freq, seconds=2.0, fs=48000, subtype="PCM_24")
        rep = audio.process_file(src, tmp_path / "o", prof)
        a, _ = audio.read_audio(src)[:2]
        b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
        ra = np.sqrt(np.mean(a[48000:, 0] ** 2))
        rb = np.sqrt(np.mean(b[48000:, 0] ** 2))
        assert 20 * np.log10(rb / ra) == pytest.approx(expected, abs=0.1)
        assert rep["preamp_db"] == 0.0 and not rep["neutral"]


def test_boost_gets_headroom_and_never_clips(tone_factory, tmp_path):
    # Nahe Vollaussteuerung plus Boost: Auto-Preamp verhindert Clipping
    src = tone_factory("loud", freq=3000, seconds=1.0, fs=48000, subtype="PCM_16", amp=0.95)
    prof = Profile(filters=[Filter("peq", 3000, 6.0, 2.0)])
    rep = audio.process_file(src, tmp_path / "o", prof)
    assert rep["preamp_db"] <= -5.99
    assert rep["peak_out_dbfs"] <= 0.0
    assert rep["guard_gain_db"] == 0.0
    out, _ = sf.read(rep["dst"], dtype="int16")
    assert np.max(np.abs(out.astype(int))) < 32767


def test_guard_scales_instead_of_clipping(tone_factory, tmp_path):
    src = tone_factory("loud2", freq=3000, seconds=1.0, fs=48000, subtype="PCM_16", amp=0.95)
    prof = Profile(preamp_db=0.0, filters=[Filter("peq", 3000, 6.0, 2.0)])  # bewusst zu wenig Headroom
    rep = audio.process_file(src, tmp_path / "o", prof)
    assert rep["guard_gain_db"] < -5.0
    assert any("Clipping-Schutz" in w for w in rep["warnings"])
    out, _ = sf.read(rep["dst"], dtype="int16")
    m = np.max(np.abs(out.astype(int)))
    assert 32000 < m <= 32767
    # kein Clipping: die Spitzen sind keine abgeflachten Plateaus
    assert np.sum(np.abs(out.astype(int)) >= 32766) < 20


def test_channel_difference_applied_per_side(tone_factory, tmp_path):
    src = tone_factory("lr", freq=1000, seconds=1.0, fs=48000, subtype="PCM_24")
    prof = Profile(channels={1: ChannelSpec(gain_db=-3.0)})
    rep = audio.process_file(src, tmp_path / "o", prof)
    a, _ = audio.read_audio(src)[:2]
    b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    r = lambda x: np.sqrt(np.mean(x ** 2))
    assert 20 * np.log10(r(b[:, 0]) / r(a[:, 0])) == pytest.approx(0.0, abs=0.01)
    assert 20 * np.log10(r(b[:, 1]) / r(a[:, 1])) == pytest.approx(-3.0, abs=0.01)


def test_mono_file_works(tone_factory, tmp_path):
    src = tone_factory("mono", channels=1, seconds=0.5, subtype="PCM_16")
    rep = audio.process_file(src, tmp_path / "o", Profile(filters=[Filter("peq", 2000, -2.0, 1.0)]))
    assert rep["out"]["channels"] == 1


def test_report_contains_formats_and_warnings(tone_factory, tmp_path):
    src = tone_factory("r", seconds=0.3, fs=96000, subtype="PCM_24")
    rep = audio.process_file(src, tmp_path / "o", Profile(filters=[Filter("peq", 60, 3.0, 5.0)]))
    assert rep["in"]["samplerate"] == 96000 and rep["in"]["bits"] == 24 and rep["in"]["channels"] == 2
    assert rep["out"]["frames"] == rep["in"]["frames"]
    assert any("Bassbereich" in w for w in rep["warnings"])
    assert any("Boost" in w for w in rep["warnings"])
    json.dumps(rep)  # muss serialisierbar sein


def test_process_file_classifies_source_read_oserror(monkeypatch, tmp_path):
    def fail_read(_path):
        raise OSError("source device unavailable")

    monkeypatch.setattr(audio, "read_audio", fail_read)
    with pytest.raises(audio.AudioProcessError) as err:
        audio.process_file("source.flac", tmp_path / "cache", Profile(), classify_errors=True)
    assert err.value.category == "source_file_access"
    assert isinstance(err.value.__cause__, OSError)


def test_process_file_classifies_cache_write_oserror(monkeypatch, tone_factory, tmp_path):
    src = tone_factory("cache-write", seconds=0.2)

    def fail_write(*_args, **_kwargs):
        raise OSError("cache device full")

    monkeypatch.setattr(audio, "_write", fail_write)
    with pytest.raises(audio.AudioProcessError) as err:
        audio.process_file(src, tmp_path / "cache", Profile(filters=[dsp.Filter("peq", 1000, -1.0, 1.0)]), classify_errors=True)
    assert err.value.category == "cache_io"
    assert isinstance(err.value.__cause__, OSError)


def test_runlog_writes_jsonl(tmp_path):
    log = RunLog(tmp_path / "sub" / "run.jsonl")
    log.write("a", x=1)
    log.write("b", path=tmp_path)
    lines = (tmp_path / "sub" / "run.jsonl").read_text(encoding="utf-8").splitlines()
    recs = [json.loads(l) for l in lines]
    assert [r["event"] for r in recs] == ["a", "b"]
    assert recs[0]["x"] == 1 and "ts" in recs[0]
    RunLog(None).write("ignored")  # ohne Datei kein Fehler


# ---- F-07: keine stillen Ueberschreibungen, atomisches Schreiben ----

import shutil
from pathlib import Path

from kefplayer import cli


def _two_same_names(tone_factory, tmp_path):
    src = tone_factory("x", seconds=0.2)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    return shutil.copy(src, a / "song.flac"), shutil.copy(src, b / "song.flac")


def test_f07_cli_gleiche_basisnamen_aus_zwei_ordnern_bricht_vorab_ab(tone_factory, tmp_path, capsys):
    fa, fb = _two_same_names(tone_factory, tmp_path)
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        cli.main(["process", str(fa), str(fb), "--out", str(out)])
    assert "gleicher Zielname" in str(e.value.code)
    assert not out.exists() or not any(out.iterdir())  # nichts geschrieben


def test_f07_cli_mit_number_eindeutig_und_zuordnung_sichtbar(tone_factory, tmp_path, capsys):
    fa, fb = _two_same_names(tone_factory, tmp_path)
    out = tmp_path / "out"
    assert cli.main(["process", str(fa), str(fb), "--out", str(out), "--number"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["000_song.flac", "001_song.flac"]
    text = capsys.readouterr().out
    assert f"{fa} -> 000_song.flac" in text and f"{fb} -> 001_song.flac" in text


def test_f07_cli_vorhandene_zieldatei_bleibt_ohne_overwrite(tone_factory, tmp_path):
    src = tone_factory("y", seconds=0.2)
    out = tmp_path / "out"
    out.mkdir()
    (out / f"{src.stem}.flac").write_bytes(b"ALT")
    with pytest.raises(SystemExit) as e:
        cli.main(["process", str(src), "--out", str(out)])
    assert "existiert bereits" in str(e.value.code)
    assert (out / f"{src.stem}.flac").read_bytes() == b"ALT"
    assert cli.main(["process", str(src), "--out", str(out), "--overwrite"]) == 0
    assert audio.probe(out / f"{src.stem}.flac").format == "FLAC"


def test_f07_cli_quelle_gleich_ziel(tone_factory, tmp_path):
    src = Path(shutil.copy(tone_factory("z", seconds=0.2), tmp_path / "q.flac"))
    before = src.read_bytes()
    with pytest.raises(SystemExit) as e:
        cli.main(["process", str(src), "--out", str(tmp_path), "--overwrite"])
    assert "Quelldatei selbst" in str(e.value.code)
    assert src.read_bytes() == before


def test_f07_process_file_quelle_gleich_ziel_wird_immer_abgelehnt(tone_factory, tmp_path):
    src = Path(shutil.copy(tone_factory("s", seconds=0.2), tmp_path / "q.flac"))
    before = src.read_bytes()
    with pytest.raises(FileExistsError):
        audio.process_file(src, tmp_path, Profile(), fmt="FLAC")  # auch mit overwrite=True (Standard)
    with pytest.raises(audio.AudioProcessError) as e:
        audio.process_file(src, tmp_path, Profile(), fmt="FLAC", classify_errors=True)
    assert e.value.category == "cache_io"
    assert src.read_bytes() == before


def test_f07_process_file_ohne_overwrite_laesst_vorhandene_datei(tone_factory, tmp_path):
    src = tone_factory("o", seconds=0.2)
    out = tmp_path / "out"
    out.mkdir()
    (out / f"{src.stem}.flac").write_bytes(b"ALT")
    with pytest.raises(FileExistsError):
        audio.process_file(src, out, Profile(), overwrite=False)
    assert (out / f"{src.stem}.flac").read_bytes() == b"ALT"


def test_f07_schreibfehler_hinterlaesst_keine_halbe_datei(tone_factory, tmp_path, monkeypatch):
    src = tone_factory("e", seconds=0.2)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / f"{src.stem}.flac"
    dst.write_bytes(b"ALT")

    def broken(path, *a, **k):
        Path(path).write_bytes(b"halb")
        raise OSError("Datentraeger voll")

    monkeypatch.setattr(audio, "_write", broken)
    with pytest.raises(OSError):
        audio.process_file(src, out, Profile())
    assert dst.read_bytes() == b"ALT"  # alte Datei unversehrt
    assert sorted(p.name for p in out.iterdir()) == [dst.name]  # keine Temp-Reste
