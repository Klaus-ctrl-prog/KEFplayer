import json

import pytest
import soundfile as sf

from kefplayer import audio, cli
from kefplayer.mediaserver import local_ip_for
from kefplayer.player import collect_files
from mock_renderer import MockRenderer


def write_profile(tmp_path, d=None):
    p = tmp_path / "prof.json"
    p.write_text(json.dumps(d or {"filters": [{"type": "peq", "fc": 2000, "gain_db": -3, "q": 2}]}), encoding="utf-8")
    return p


def test_collect_files_recursive_sorted_and_filtered(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "02.flac").write_bytes(b"x")
    (tmp_path / "b" / "01.FLAC").write_bytes(b"x")
    (tmp_path / "a.wav").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    names = [p.name for p in collect_files([tmp_path])]
    assert names == ["a.wav", "01.FLAC", "02.flac"]  # nach vollem Pfad sortiert: a.wav vor b/...
    assert "cover.jpg" not in names
    with pytest.raises(FileNotFoundError):
        collect_files([tmp_path / "missing"])


def test_local_ip_for_loopback():
    assert local_ip_for("127.0.0.1") == "127.0.0.1"


def test_process_command(tone_factory, tmp_path, capsys):
    src = tone_factory("x", seconds=0.5, fs=48000, subtype="PCM_24")
    prof = write_profile(tmp_path)
    log = tmp_path / "p.jsonl"
    rc = cli.main(["process", str(src), "--out", str(tmp_path / "out"), "--profile", str(prof), "--log", str(log)])
    assert rc == 0
    out = capsys.readouterr()
    assert "FLAC 48000 Hz 24 Bit" in out.out
    assert (tmp_path / "out" / "x.flac").is_file()
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["event"] == "process"
    # Projektregel: auch ein Cut oberhalb 150 Hz bekommt den Hinweis auf Mittelung, aber keinen Boost-Hinweis
    assert "gemittelt" in out.err
    assert "Boost" not in out.err


def test_process_neutral_without_profile_is_bit_exact(tone_factory, tmp_path):
    src = tone_factory("y", seconds=0.5, subtype="PCM_16")
    assert cli.main(["process", str(src), "--out", str(tmp_path / "o")]) == 0
    a, _ = sf.read(str(src), dtype="int32")
    b, _ = sf.read(str(tmp_path / "o" / "y.flac"), dtype="int32")
    assert (a == b).all()


def test_process_bad_profile_returns_2(tone_factory, tmp_path, capsys):
    src = tone_factory("z", seconds=0.3)
    bad = tmp_path / "bad.json"
    bad.write_text('{"filters": [{"type": "peq", "fc": -1, "gain_db": 1}]}', encoding="utf-8")
    assert cli.main(["process", str(src), "--out", str(tmp_path / "o"), "--profile", str(bad)]) == 2
    assert "Fehler" in capsys.readouterr().err



def test_process_corrupt_audio_returns_2(tmp_path, capsys):
    src = tmp_path / "broken.flac"
    src.write_bytes(b"not a valid audio file")

    rc = cli.main(["process", str(src), "--out", str(tmp_path / "out")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Fehler" in err
    assert "Traceback" not in err
def test_response_command(tmp_path, capsys):
    prof = write_profile(tmp_path, {"filters": [{"type": "peq", "fc": 2000, "gain_db": 4, "q": 2}]})
    assert cli.main(["response", "--profile", str(prof), "--fs", "48000"]) == 0
    out = capsys.readouterr().out
    assert "Preamp -4.0" in out and "auto" in out
    import re
    m = re.search(r"Spitze alle: ([+-]\d+\.\d+) dB", out)
    assert m and -0.05 <= float(m.group(1)) <= 0.0  # Preamp bringt die Spitze auf hoechstens 0 dB
    line = [l for l in out.splitlines() if l.strip().startswith("2000")][0]
    assert float(line.split()[1]) == pytest.approx(0.0, abs=0.01)  # +4 dB Boost minus 4 dB Preamp


def test_response_shows_both_channels(tmp_path, capsys):
    prof = write_profile(tmp_path, {"channels": {"R": {"gain_db": -2.0}}})
    assert cli.main(["response", "--profile", str(prof)]) == 0
    out = capsys.readouterr().out
    header = [l for l in out.splitlines() if "f/Hz" in l][0]
    assert header.split() == ["f/Hz", "L", "R"]
    row = [l for l in out.splitlines() if l.strip().startswith("1000")][0].split()
    assert float(row[1]) == pytest.approx(0.0, abs=0.01) and float(row[2]) == pytest.approx(-2.0, abs=0.01)


def test_probe_and_discover_against_mock(capsys):
    with MockRenderer() as mock:
        assert cli.main(["probe", "--renderer", mock.location]) == 0
        out = capsys.readouterr().out
        assert "Mock KEF" in out and "audio/flac" in out and "kein Wiedergabetest" in out
        host, port = mock.ssdp_addr
        assert cli.main(["discover", "--ssdp-addr", f"{host}:{port}", "--timeout", "0.8"]) == 0
        assert mock.location in capsys.readouterr().out


def test_discover_nothing_found(capsys):
    with MockRenderer(ssdp=False) as mock:
        assert cli.main(["discover", "--ssdp-addr", f"127.0.0.1:{mock.port}", "--timeout", "0.4"]) == 1


def test_resolve_renderer_nothing_found_message():
    with MockRenderer(ssdp=False) as mock:
        with pytest.raises(SystemExit) as ei:
            cli.main(["probe", "--ssdp-addr", f"127.0.0.1:{mock.port}", "--discover-timeout", "0.4"])
        assert "Kein Renderer gefunden" in str(ei.value)


def test_play_command_end_to_end(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=1.0, fs=44100, subtype="PCM_16")
    b = tone_factory("b", seconds=1.0, fs=48000, subtype="PCM_24")
    log = tmp_path / "run.jsonl"
    with MockRenderer() as mock:
        rc = cli.main(["play", str(a), str(b), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
                       "--port", "0", "--poll", "0.02", "--log", str(log)])
        assert rc == 0
        assert len(mock.fetched) == 2 and mock.fetched[0]["data"] == a.read_bytes()
    out = capsys.readouterr().out
    assert "FLAC 44100 Hz 16 Bit" in out and "FLAC 48000 Hz 24 Bit" in out
    assert "unveraendert" in out and "Lautstaerke des Renderers wird nicht veraendert" in out
    assert "Fertig: 2 Titel, 0 mit Problemen" in out
    events = [json.loads(l)["event"] for l in log.read_text(encoding="utf-8").splitlines()]
    assert events.count("track_end") == 2


def test_play_command_reports_problems_with_exit_code(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=0.8)
    with MockRenderer(reject_mimes=["audio/flac"]) as mock:
        rc = cli.main(["play", str(a), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
                       "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl")])
    assert rc == 1
    assert "1 mit Problemen" in capsys.readouterr().out


def test_play_unknown_file_returns_2(tmp_path, capsys):
    assert cli.main(["play", str(tmp_path / "nope.flac")]) == 2
    assert "Fehler" in capsys.readouterr().err
