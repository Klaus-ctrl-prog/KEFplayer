"""CLI Radio E1b (--ffmpeg): Anbindung im Simulator. Kein echtes ffmpeg: check_ffmpeg wird ersetzt, der Decoder ist ein Fake-Skript."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from kefplayer import cli, decoder, radio_supervisor
from kefplayer.decoder import FfmpegInfo
from conftest import make_tone
from mock_renderer import MockRenderer
from radio_helpers import FakeStation
from kefplayer.wavstream import pack_s24le

FAKE = str(Path(__file__).with_name("fake_decoder.py"))


def _flac(tmp_path):
    return make_tone(tmp_path / "s.flac", seconds=1.0, fs=8000).read_bytes()


def _fake_ffmpeg(monkeypatch, tmp_path, frames=2400, mode="sine"):
    exe = tmp_path / "ffmpeg-fake"
    exe.write_bytes(b"x")
    seen = {}

    def fake_check(path, *, pin_sha256=None, **kw):
        seen["path"], seen["pin"] = path, pin_sha256
        return FfmpegInfo(path=path, version="ffmpeg version fake-1.0", sha256="ab" * 32,
                          argv_prefix=(sys.executable, FAKE, "--mode", mode, "--frames", str(frames)))

    monkeypatch.setattr(decoder, "check_ffmpeg", fake_check)
    return str(exe), seen


def _args(tmp_path, mock, target, extra=()):
    return ["radio", target, "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl"), *extra]


def test_e1b_ende_zu_ende_wav_mit_eq_und_pin(tmp_path, monkeypatch, capsys):
    exe, seen = _fake_ffmpeg(monkeypatch, tmp_path, frames=96000)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"name": "t", "preamp_db": -6.0, "filters": []}), encoding="utf-8")
    with FakeStation(body=_flac(tmp_path), content_type="audio/flac") as st, MockRenderer(speedup=5.0) as mock:
        rc = cli.main(_args(tmp_path, mock, st.url, ["--ffmpeg", exe, "--ffmpeg-sha256", "ab" * 32,
                                                      "--profile", str(prof), "--no-reconnect"]))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert seen == {"path": exe, "pin": "ab" * 32}
    assert "ffmpeg version fake-1.0" in out and "SHA-256" in out and "Reconnect: aus" in out
    assert "48000 Hz" in out and "WAV PCM 24 Bit" in out
    assert "GEHEIM123" not in out
    data = mock.fetched[0]["data"]
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    pcm = data[44:]
    assert len(pcm) == 96000 * 2 * 3
    # Preamp -6,0 dB wirkt: 0.25 * 10**(-6/20), auf 24 Bit gerundet (nicht der Rohwert 0.25)
    assert pcm[:3] == pack_s24le(np.array([[0.25 * 10 ** (-6 / 20)]]))[0]


def test_e1b_optionen_ohne_ffmpeg_abgelehnt(tmp_path, capsys):
    with MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, "http://127.0.0.1:9/x", ["--radio-rate", "44100"]))
    assert rc == 2 and "brauchen --ffmpeg" in capsys.readouterr().err
    assert mock.fetched == []


def test_e1b_relativer_ffmpeg_pfad_vor_jedem_renderer_kontakt(tmp_path, capsys):
    with MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, "http://127.0.0.1:9/x", ["--ffmpeg", "ffmpeg"]))
    assert rc == 2 and "decoder_unavailable" in capsys.readouterr().err
    assert mock.fetched == []


def test_e1b_mp3_wird_vor_dem_decoder_abgelehnt(tmp_path, monkeypatch, capsys):
    exe, _ = _fake_ffmpeg(monkeypatch, tmp_path)
    with FakeStation(body=b"\xff\xfb" * 200, content_type="audio/mpeg") as st, MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, st.url, ["--ffmpeg", exe]))
    assert rc == 2 and mock.fetched == []


def test_e1b_reconnect_standard_an_und_budget_erschoepft(tmp_path, monkeypatch, capsys):
    exe, _ = _fake_ffmpeg(monkeypatch, tmp_path, frames=96000)
    real = radio_supervisor.ReconnectBudget
    monkeypatch.setattr(radio_supervisor, "ReconnectBudget", lambda: real(backoff=(0.05,), max_total=1, stable_s=999))
    with FakeStation(body=_flac(tmp_path), content_type="audio/flac") as st, MockRenderer(speedup=5.0) as mock:
        rc = cli.main(_args(tmp_path, mock, st.url, ["--ffmpeg", exe]))
    cap = capsys.readouterr()
    assert rc == 1
    assert "stream_reconnect_exhausted" in cap.err + cap.out
    assert "Reconnects 1" in cap.out and "Reconnect: an" in cap.out
    log = [json.loads(x)["event"] for x in open(tmp_path / "r.jsonl", encoding="utf-8")]
    assert "radio_gap" in log and "radio_reconnect_wait" in log and "radio_failed" in log
    assert "GEHEIM123" not in cap.out + cap.err + (tmp_path / "r.jsonl").read_text(encoding="utf-8")


def test_e1b_decoder_startet_nicht(tmp_path, monkeypatch, capsys):
    exe = tmp_path / "ff"
    exe.write_bytes(b"x")
    monkeypatch.setattr(decoder, "check_ffmpeg", lambda p, **k: FfmpegInfo(
        path=p, version="ffmpeg version fake", sha256="0" * 64, argv_prefix=(str(tmp_path / "gibtsnicht"),)))
    with FakeStation(body=_flac(tmp_path), content_type="audio/flac") as st, MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, st.url, ["--ffmpeg", str(exe)]))
    assert rc == 2 and "decoder_unavailable" in capsys.readouterr().err
