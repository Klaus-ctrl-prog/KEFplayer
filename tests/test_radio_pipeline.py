import http.client
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from kefplayer import dsp
from kefplayer.decoder import Decoder, FfmpegInfo
from kefplayer.dsp import Filter, Profile
from kefplayer.mediaserver import MediaServer
from kefplayer.radio_pipeline import RadioPipeline
from kefplayer.wavstream import pack_s24le

FAKE = str(Path(__file__).with_name("fake_decoder.py"))
RATE = 48000


class FakeDec:
    """Duck-typed Decoder: liefert vorgegebene Bytes, blockiert danach optional bis stop()."""

    def __init__(self, data: bytes, chunk=3000, hold=False):
        self.data, self.chunk, self.hold = data, chunk, hold
        self.pos = 0
        self.stopped = threading.Event()

    def read(self, n):
        if self.pos < len(self.data):
            c = self.data[self.pos:self.pos + min(n, self.chunk)]
            self.pos += len(c)
            return c
        if self.hold:
            self.stopped.wait(30)
        return b""

    def stop(self, grace_s=0):
        self.stopped.set()


def f32(x):
    return np.asarray(x, dtype="<f4").tobytes()


def drain(pipe, limit=10_000_000):
    src = pipe.open_source()
    hdr = src.read1()
    body = bytearray()
    while len(body) < limit:
        c = src.read1(8192)
        if not c:
            break
        body += c
    return hdr, bytes(body)


def test_neutral_ende_zu_ende_mit_kopf_und_stats():
    x = np.random.default_rng(5).uniform(-0.8, 0.8, (5000, 2)).astype("<f4")
    pipe = RadioPipeline(FakeDec(f32(x)), Profile(), rate=RATE)
    pipe.start()
    hdr, body = drain(pipe)
    assert hdr[:4] == b"RIFF" and len(hdr) == 44
    ref, _, _ = pack_s24le(x.astype(np.float64))
    assert body == ref
    pipe.stop()
    s = pipe.stats()
    assert s.frames_in == 5000 and s.state in ("ended", "stopped") and s.clipped_samples == 0


def test_raum_eq_ende_zu_ende_gleich_offline():
    x = np.random.default_rng(6).uniform(-0.5, 0.5, (9000, 2)).astype("<f4")
    prof = Profile(preamp_db=None, filters=[Filter("peq", 2000, -3.0, 2.0), Filter("lowshelf", 100, 2.0, 0.7)])
    pipe = RadioPipeline(FakeDec(f32(x), chunk=1777), prof, rate=RATE, block_frames=500)
    pipe.start()
    _, body = drain(pipe)
    ref, _ = dsp.apply_profile(x.astype(np.float64), RATE, prof)
    exp, _, _ = pack_s24le(ref[:9000])
    a = np.frombuffer(body, np.uint8).astype(np.int32)
    b = np.frombuffer(exp, np.uint8).astype(np.int32)
    assert len(a) == len(b)
    assert np.max(np.abs(a - b)) <= 1  # Rundung an Byte-Grenzen
    pipe.stop()


def test_uebersteuerung_wird_gezaehlt():
    x = np.full((1000, 2), 1.5, dtype="<f4")
    pipe = RadioPipeline(FakeDec(f32(x)), Profile(), rate=RATE)
    pipe.start()
    drain(pipe)
    assert pipe.stats().clipped_samples == 2000
    pipe.stop()


def test_angefangenes_frame_am_ende_wird_verworfen():
    raw = f32(np.full((100, 2), 0.1)) + b"\x00\x00\x80"
    pipe = RadioPipeline(FakeDec(raw), Profile(), rate=RATE)
    pipe.start()
    _, body = drain(pipe)
    assert len(body) == 100 * 2 * 3 and pipe.stats().frames_in == 100
    pipe.stop()


def test_unterlauf_liefert_stille_und_zaehlt():
    pipe = RadioPipeline(FakeDec(f32(np.full((10, 2), 0.5)), hold=True), Profile(), rate=RATE, block_frames=10)
    pipe.start()
    src = pipe.open_source()
    src.read1()
    first = src.read1(8192)
    assert first == pack_s24le(np.full((10, 2), 0.5))[0]
    sil = src.read1(8192)  # nichts mehr da: Stille
    assert sil == bytes(int(RATE * 0.02) * 2 * 3)
    assert pipe.stats().underruns >= 1
    pipe.stop()
    assert src.read1() == b""  # nach stop endet der Strom


def test_ueberlauf_bei_langsamem_verbraucher():
    x = np.full((20000, 2), 0.1, dtype="<f4")
    pipe = RadioPipeline(FakeDec(f32(x)), Profile(), rate=RATE, buffer_seconds=0.05)
    pipe.start()
    for _ in range(100):
        if pipe.stats().state != "streaming":
            break
        time.sleep(0.02)
    s = pipe.stats()
    assert s.overrun_bytes > 0 and s.buffered_bytes <= int(0.05 * RATE * 6)
    pipe.stop()


def test_verarbeitungsfehler_ergibt_failed():
    class Boom(FakeDec):
        def read(self, n):
            raise RuntimeError("kaputt")

    pipe = RadioPipeline(Boom(b""), Profile(), rate=RATE)
    pipe.start()
    pipe._thread.join(3)
    assert pipe.stats().state == "failed"
    pipe.stop()


def test_stop_beendet_haengenden_decoder_und_wartenden_verbraucher():
    dec = FakeDec(b"", hold=True)
    pipe = RadioPipeline(dec, Profile(), rate=RATE)
    pipe.start()
    done = []
    src = pipe.open_source()
    src.read1()

    def consume():
        while src.read1(8192):
            pass
        done.append(1)

    th = threading.Thread(target=consume)
    th.start()
    time.sleep(0.4)
    assert th.is_alive()
    pipe.stop()
    th.join(3)
    assert done and dec.stopped.is_set()


def test_doppelstart_verboten():
    pipe = RadioPipeline(FakeDec(b""), Profile(), rate=RATE)
    pipe.start()
    with pytest.raises(RuntimeError):
        pipe.start()
    pipe.stop()


def test_mediaserver_mit_echtem_decoder_prozess_fake():
    info = FfmpegInfo(path=sys.executable, version="fake", sha256="0" * 64,
                      argv_prefix=(sys.executable, FAKE, "--mode", "sine", "--frames", "2400"))
    dec = Decoder(info, "http://sender.invalid/x", rate=RATE)
    dec.start()
    pipe = RadioPipeline(dec, Profile(), rate=RATE)
    pipe.start()
    server = MediaServer(bind="127.0.0.1", port=0)
    server.start()
    try:
        url, token = server.register_stream(pipe.open_source, "audio/wav", name="radio.wav")
        host, rest = url[len("http://"):].split("/", 1)
        c = http.client.HTTPConnection(host, timeout=10)
        c.request("GET", "/" + rest)
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type") == "audio/wav"
        assert r.getheader("Content-Length") is None
        body = r.read()
        c.close()
        assert body[:4] == b"RIFF"
        pcm = body[44:]
        assert len(pcm) == 2400 * 2 * 3
        assert pcm[:6] == pack_s24le(np.array([[0.25, -0.25]]))[0]
    finally:
        pipe.stop()
        server.stop() if hasattr(server, "stop") else None
