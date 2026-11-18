import hashlib
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kefplayer import decoder
from kefplayer.decoder import Decoder, FfmpegInfo, build_args, check_ffmpeg, scrub
from kefplayer.radio import RadioError

FAKE = str(Path(__file__).with_name("fake_decoder.py"))
URL = "https://geheim.example.invalid:8443/pfad/TOKEN123?key=SECRET"


def info(mode, **kw):
    return FfmpegInfo(path=sys.executable, version="fake", sha256="0" * 64,
                      argv_prefix=(sys.executable, FAKE, "--mode", mode, *[x for k, v in kw.items() for x in (f"--{k}", str(v))]))


def _fake_run(stdout, rc=0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
    return run


# ---------------- Pfadpruefung ----------------
def test_relativer_pfad_abgelehnt():
    with pytest.raises(RadioError) as e:
        check_ffmpeg("ffmpeg")
    assert e.value.category == "decoder_unavailable"


def test_fehlende_datei_und_verzeichnis(tmp_path):
    for p in (str(tmp_path / "nix"), str(tmp_path)):
        with pytest.raises(RadioError) as e:
            check_ffmpeg(p)
        assert e.value.category == "decoder_unavailable"


def test_pin_abweichung_startet_nicht(tmp_path):
    f = tmp_path / "ff"
    f.write_bytes(b"abc")
    called = []
    with pytest.raises(RadioError) as e:
        check_ffmpeg(str(f), pin_sha256="0" * 64, runner=lambda *a, **k: called.append(1))
    assert "Pruefsumme" in str(e.value)
    assert not called  # Datei wurde NICHT ausgefuehrt


def test_pin_passt_und_version_wird_gelesen(tmp_path):
    f = tmp_path / "ff"
    f.write_bytes(b"abc")
    d = hashlib.sha256(b"abc").hexdigest()
    got = check_ffmpeg(str(f), pin_sha256=d.upper(), runner=_fake_run("ffmpeg version 7.1 Copyright\nweitere Zeile\n"))
    assert got.sha256 == d and got.version.startswith("ffmpeg version 7.1") and got.path == str(f)


def test_kein_ffmpeg_oder_fehlercode_abgelehnt(tmp_path):
    f = tmp_path / "ff"
    f.write_bytes(b"x")
    for out, rc in (("Python 3.12\n", 0), ("", 0), ("ffmpeg version 7\n", 1)):
        with pytest.raises(RadioError):
            check_ffmpeg(str(f), runner=_fake_run(out, rc))


def test_nicht_startbar_ist_decoder_unavailable(tmp_path):
    f = tmp_path / "ff"
    f.write_bytes(b"x")

    def run(*a, **k):
        raise PermissionError("blockiert")
    with pytest.raises(RadioError) as e:
        check_ffmpeg(str(f), runner=run)
    assert e.value.category == "decoder_unavailable" and "Application Control" in str(e.value)


def test_echte_ausfuehrung_python_ist_kein_ffmpeg():
    with pytest.raises(RadioError):
        check_ffmpeg(sys.executable)


# ---------------- Aufruf ----------------
def test_build_args_sicherheitsoptionen():
    a = build_args(URL, rate=48000)
    assert "-nostdin" in a and a[a.index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"
    assert not any(x.startswith("-reconnect") for x in a)
    assert a[a.index("-i") + 1] == URL and a[-1] == "pipe:1"
    assert a[a.index("-f") + 1] == "f32le" and a[a.index("-ar") + 1] == "48000"
    with pytest.raises(ValueError):
        build_args(URL, rate=5)
    with pytest.raises(ValueError):
        build_args(URL, channels=6)


def test_url_nur_als_argument_ohne_shell():
    evil = "http://h.invalid/a;rm -rf x&&y$(z)"
    with Decoder(info("args"), evil) as d:
        d.read(1)
        time.sleep(0.3)
        d.stop()
    tail = " ".join(d.stderr_tail())
    assert "rm -rf" not in tail  # bereinigt
    assert d.url_masked == "http://h.invalid/..."


# ---------------- Betrieb ----------------
def test_pcm_wird_geliefert_und_exitcode_0():
    with Decoder(info("sine", frames=1000), URL) as d:
        data = b""
        while len(data) < 8000:
            c = d.read(8000 - len(data))
            if not c:
                break
            data += c
        assert len(data) == 8000
        assert struct.unpack("<2f", data[:8]) == (0.25, -0.25)
        assert d.read(10) == b""
        assert d.stop() == 0


def test_fehler_exitcode_und_stderr_ohne_url():
    with Decoder(info("fail"), URL) as d:
        assert d.read(100) == b""
        for _ in range(50):
            if d.poll() is not None:
                break
            time.sleep(0.05)
        assert d.poll() == 3
        d.stop()
    tail = "\n".join(d.stderr_tail())
    assert "404" in tail
    assert "TOKEN123" not in tail and "SECRET" not in tail
    assert "https://geheim.example.invalid:8443/..." in tail


def test_stderr_flut_blockiert_nicht_und_ist_begrenzt():
    with Decoder(info("flood"), URL) as d:
        wd = threading.Timer(15, d.stop)  # Notbremse: ohne Drain-Thread wuerde der Test sonst haengen
        wd.start()
        t0 = time.time()
        got = d.read(8)
        d.stop()
        wd.cancel()
        assert time.time() - t0 < 10
        assert got == b"\x00" * 8
    tail = d.stderr_tail()
    assert 0 < len(tail) <= decoder.STDERR_KEEP_LINES
    assert all("TOKEN123" not in x for x in tail)


def test_stop_beendet_haengenden_prozess_und_entblockt_read():
    d = Decoder(info("hang"), URL)
    d.start()
    res = []
    th = threading.Thread(target=lambda: res.append(d.read(100)))
    th.start()
    time.sleep(0.5)
    assert th.is_alive() and d.poll() is None
    t0 = time.time()
    rc = d.stop(grace_s=1.0)
    th.join(5)
    assert not th.is_alive() and res == [b""]
    assert rc is not None and d.poll() is not None  # kein Zombie
    assert time.time() - t0 < 10
    assert d.stop() == rc  # idempotent


def test_doppelstart_verboten_und_nicht_startbar(tmp_path):
    d = Decoder(info("sine"), URL)
    d.start()
    with pytest.raises(RuntimeError):
        d.start()
    d.stop()
    bad = FfmpegInfo(path="x", version="v", sha256="0", argv_prefix=(str(tmp_path / "gibtsnicht"),))
    with pytest.raises(RadioError) as e:
        Decoder(bad, URL).start()
    assert e.value.category == "decoder_unavailable"


def test_scrub_mehrere_urls_und_ohne_url():
    assert scrub("a http://x.invalid/p?t=1 b https://y.invalid:1/q") == "a http://x.invalid/... b https://y.invalid:1/..."
    assert scrub("nichts") == "nichts"
