"""Regressionstests zu den Befunden der Code-Pruefung (14.11.2026)."""

import io
import json
import pathlib
import shutil
import subprocess
import sys

import pytest
import soundfile as sf

from kefplayer import cli, dsp, upnp
from kefplayer.dsp import Filter, Profile
from mock_renderer import MockRenderer


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]


# ---- Option --bits/--format bei neutralem Profil wurde frueher stillschweigend ignoriert ----

def test_neutral_with_explicit_bits_converts(rig, tone_factory):
    src = tone_factory("b", seconds=0.8, fs=48000, subtype="PCM_24")
    mock, server, player = rig(bits=16)
    res = player.play_queue([src])
    assert res[0]["processed"] is True and res[0]["served_format"]["bits"] == 16
    assert sf.info(io.BytesIO(mock.fetched[0]["data"])).subtype == "PCM_16"


def test_neutral_with_explicit_format_converts(rig, tone_factory):
    src = tone_factory("f", seconds=0.8, fs=44100, subtype="PCM_16")
    mock, server, player = rig(fmt="WAV")
    res = player.play_queue([src])
    assert res[0]["processed"] is True
    assert res[0]["served_format"]["format"] == "WAV" and res[0]["mime"] == "audio/wav"
    assert mock.fetched[0]["uri"].endswith(".wav")


def test_neutral_without_options_still_serves_original(rig, tone_factory):
    src = tone_factory("o", seconds=0.8)
    mock, server, player = rig()
    res = player.play_queue([src])
    assert res[0]["processed"] is False and mock.fetched[0]["data"] == src.read_bytes()


# ---- Windows: Loeschen einer temporaeren Datei schlaegt fehl ----

def test_cleanup_failure_does_not_abort_queue(rig, tone_factory, monkeypatch, tmp_path):
    a = tone_factory("a", seconds=0.8)
    b = tone_factory("b", seconds=0.8)
    orig = pathlib.Path.unlink

    def failing(self, *args, **kw):
        if self.name.startswith("000_"):
            raise PermissionError(13, "gesperrt (WinError 32 simuliert)", str(self))
        return orig(self, *args, **kw)

    monkeypatch.setattr(pathlib.Path, "unlink", failing)
    mock, server, player = rig(Profile(filters=[Filter("peq", 2000, -1.0, 1.0)]))
    res = player.play_queue([a, b])
    assert [r["end_reason"] for r in res] == ["ended", "ended"]
    assert any(e["event"] == "cleanup_failed" for e in read_log(tmp_path))


# ---- MIME-Typ nach Inhalt statt nach Endung ----

def test_pick_mime_prefers_content_over_extension():
    assert upnp.pick_mime(".wav", None, "FLAC") == ("audio/flac", None)
    assert upnp.pick_mime(".flac", ["audio/x-wav", "audio/wav"], "WAV") == ("audio/wav", True)
    assert upnp.pick_mime(".xyz", None, "RF64")[0] == "application/octet-stream"  # unbekanntes Format: Endung
    assert upnp.pick_mime(".flac", None, None) == ("audio/flac", None)
    assert upnp.ext_for_format("flac") == ".flac" and upnp.ext_for_format("RF64") is None


def test_misnamed_file_gets_mime_and_url_extension_of_its_content(rig, tone_factory, tmp_path):
    real = tone_factory("real", seconds=0.8)
    fake = tmp_path / "falsch.wav"
    shutil.copy(real, fake)
    mock, server, player = rig()
    res = player.play_queue([fake])
    assert res[0]["mime"] == "audio/flac"
    assert mock.fetched[0]["uri"].endswith(".flac") and mock.fetched[0]["content_type"] == "audio/flac"


# ---- Lueckenanteil des Players: nicht mehr ein ganzes Abfrageintervall ----

def test_gap_is_not_dominated_by_poll_interval(rig, tone_factory):
    files = [tone_factory(f"p{i}", seconds=2.0, fs=8000) for i in range(2)]
    mock, server, player = rig(mock_kw={"speedup": 1.0, "start_delay": 0.05}, poll_s=0.5)
    res = player.play_queue(files)
    assert [r["end_reason"] for r in res] == ["ended", "ended"]
    starts = [t for k, t in mock.timeline if k == "start"]
    ends = [t for k, t in mock.timeline if k == "end"]
    true_gap = starts[1] - ends[0]
    assert true_gap < 0.3, true_gap  # vorher ca. 0,5 s (= Abfrageintervall)
    assert abs(res[1]["gap_before_s"] - true_gap) < 0.15
    assert res[0]["startup_delay_s"] < 0.3


def test_fast_poll_never_slower_than_poll():
    p = Player_for_attrs(poll_s=0.02, fast_poll_s=0.05)
    assert p.fast_poll_s == 0.02


def Player_for_attrs(**kw):
    from kefplayer.player import Player
    return Player(None, None, None, **kw)


# ---- APO-Import: Shelf-Varianten ----

def test_apo_shelf_slope_forms_are_rejected():
    for bad in ["Filter 1: ON LS 6dB Fc 100 Hz Gain 3 dB", "Filter 1: ON LSC 10.8 dB Fc 100 Hz Gain 3 dB",
                "Filter 1: ON HS 12 dB Fc 8000 Hz Gain -2 dB"]:
        with pytest.raises(ValueError, match="dB/Oktave"):
            dsp.parse_apo(bad)


def test_apo_shelf_q_form_is_accepted_with_unverified_note():
    p = dsp.parse_apo("Filter 1: ON LSC Fc 300 Hz Gain 5.0 dB Q 0.6473\nFilter 2: ON PK Fc 1000 Hz Gain -2,5 dB Q 2")
    assert [f.type for f in p.filters] == ["lowshelf", "peq"] and p.filters[0].q == 0.6473
    assert any("nicht verifiziert" in n for n in p.notes)
    assert p.filters[1].gain_db == -2.5  # Gain mit Dezimalkomma wird nicht als Steilheit missverstanden


# ---- Kommandozeilenpfad der Umwandlung und weitere Formate ----

def test_play_cli_bits_option_converts_neutral_and_is_labelled_correctly(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=1.0, fs=48000, subtype="PCM_24")
    with MockRenderer() as mock:
        rc = cli.main(["play", str(a), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
                       "--port", "0", "--poll", "0.02", "--bits", "16", "--log", str(tmp_path / "r.jsonl")])
        assert rc == 0
        assert sf.info(io.BytesIO(mock.fetched[0]["data"])).subtype == "PCM_16"
    out = capsys.readouterr().out
    assert "FLAC 48000 Hz 16 Bit" in out
    assert "verarbeitet" in out and "gefiltert" not in out       # reine Umwandlung ist kein Filtern
    assert "(neutral, Dateien unveraendert)" not in out           # Kopfzeile darf nichts Falsches behaupten


def test_play_cli_without_options_banner_says_unchanged(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=1.0, fs=44100, subtype="PCM_16")
    with MockRenderer() as mock:
        rc = cli.main(["play", str(a), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
                       "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl")])
        assert rc == 0 and mock.fetched[0]["data"] == a.read_bytes()
    assert "(neutral, Dateien unveraendert)" in capsys.readouterr().out


def test_mime_and_extension_mapping_for_other_formats():
    assert upnp.pick_mime(".dat", None, "OGG")[0] == "audio/ogg" and upnp.ext_for_format("OGG") == ".ogg"
    assert upnp.pick_mime(".dat", None, "MPEG")[0] == "audio/mpeg" and upnp.ext_for_format("MPEG") == ".mp3"
    assert upnp.pick_mime(".dat", None, "AIFF")[0] == "audio/aiff" and upnp.ext_for_format("AIFF") == ".aif"
    assert upnp.pick_mime(".dat", ["audio/x-aiff"], "AIFF") == ("audio/x-aiff", True)


# ---- Start per python -m ----

def test_python_dash_m_entry_point():
    root = pathlib.Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "kefplayer", "--version"], cwd=root, capture_output=True, text=True, timeout=60)
    from kefplayer import __version__
    assert r.returncode == 0 and f"kefplayer {__version__}" in r.stdout
