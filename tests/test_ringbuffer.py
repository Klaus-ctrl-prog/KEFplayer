import threading
import time

import pytest

from kefplayer.ringbuffer import ByteRing


def test_reihenfolge_und_teilweises_lesen():
    r = ByteRing(100)
    r.put(b"abc")
    r.put(b"defg")
    assert r.get(2) == b"ab"
    assert r.get(100) == b"cdefg"
    assert len(r) == 0


def test_ueberlauf_verwirft_aelteste_und_zaehlt():
    r = ByteRing(10)
    r.put(b"0123456789")
    r.put(b"AB")
    assert r.overrun_bytes == 2
    assert r.get(100) == b"23456789AB"


def test_uebergrosses_put_behaelt_neueste():
    r = ByteRing(4)
    r.put(b"xy")
    r.put(b"0123456789")
    assert r.get(100) == b"6789"
    assert r.overrun_bytes == 2 + 10 - 4


def test_timeout_und_geschlossen():
    r = ByteRing(10)
    t0 = time.monotonic()
    assert r.get(5, timeout=0.1) == b""
    assert 0.08 < time.monotonic() - t0 < 1.0
    r.put(b"ab")
    r.close()
    r.put(b"zz")  # nach close ignoriert
    assert r.get(5) == b"ab"  # Rest wird noch geliefert
    assert r.get(5) == b"" and r.closed


def test_blockierendes_get_wird_von_put_und_close_geweckt():
    r = ByteRing(10)
    res = []
    th = threading.Thread(target=lambda: res.append(r.get(5)))
    th.start()
    time.sleep(0.2)
    assert th.is_alive()
    r.put(b"hi")
    th.join(2)
    assert res == [b"hi"]
    th2 = threading.Thread(target=lambda: res.append(r.get(5)))
    th2.start()
    time.sleep(0.2)
    r.close()
    th2.join(2)
    assert res[-1] == b"" and not th2.is_alive()


def test_erzeuger_verbraucher_integritaet():
    r = ByteRing(1 << 22)
    data = bytes(range(256)) * 4096  # 1 MiB
    got = bytearray()

    def prod():
        for i in range(0, len(data), 1000):
            r.put(data[i:i + 1000])
        r.close()

    th = threading.Thread(target=prod)
    th.start()
    while True:
        c = r.get(777, timeout=2)
        if not c:
            break
        got += c
    th.join()
    assert bytes(got) == data and r.overrun_bytes == 0


def test_ungueltige_kapazitaet():
    with pytest.raises(ValueError):
        ByteRing(0)
