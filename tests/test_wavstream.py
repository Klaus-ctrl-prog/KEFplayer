import struct

import numpy as np
import pytest

from kefplayer.wavstream import pack_s24le, wav_header_24


def _decode(raw, ch):
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    v = np.where(v >= 1 << 23, v - (1 << 24), v)
    return (v / 8388608.0).reshape(-1, ch)


def test_header_felder():
    h = wav_header_24(44100, 2)
    assert len(h) == 44 and h[:4] == b"RIFF" and h[8:12] == b"WAVE" and h[36:40] == b"data"
    assert struct.unpack("<I", h[4:8])[0] == 0xFFFFFFFF and struct.unpack("<I", h[40:44])[0] == 0xFFFFFFFF
    fmt, ch, rate, byterate, align, bits = struct.unpack("<HHIIHH", h[20:36])
    assert (fmt, ch, rate, byterate, align, bits) == (1, 2, 44100, 44100 * 6, 6, 24)
    with pytest.raises(ValueError):
        wav_header_24(44100, 6)
    with pytest.raises(ValueError):
        wav_header_24(100, 2)


def test_bekannte_werte_und_reihenfolge():
    raw, c, b = pack_s24le(np.array([[0.5, -0.5], [0.0, -1.0]]))
    assert raw == bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0xC0, 0, 0, 0, 0x00, 0x00, 0x80])
    assert (c, b) == (0, 0)  # -1.0 ist exakt MIN_INT, kein Clipping


def test_clipping_wird_gezaehlt_und_begrenzt():
    raw, c, _ = pack_s24le(np.array([[1.0, 1.5], [-1.2, 0.1]]))
    d = _decode(raw, 2)
    assert c == 3
    assert d[0, 0] == pytest.approx(8388607 / 8388608) and d[0, 1] == pytest.approx(8388607 / 8388608)
    assert d[1, 0] == -1.0


def test_nan_inf_zu_null_und_gezaehlt():
    raw, c, b = pack_s24le(np.array([[np.nan, np.inf], [-np.inf, 0.25]]))
    assert b == 3 and c == 0
    assert _decode(raw, 2).tolist() == [[0.0, 0.0], [0.0, 0.25]]


def test_rundung_max_halbes_lsb():
    x = np.random.default_rng(3).uniform(-0.9, 0.9, (5000, 2))
    raw, _, _ = pack_s24le(x)
    assert len(raw) == 5000 * 2 * 3
    assert np.max(np.abs(_decode(raw, 2) - x)) <= 0.5 / 8388608 + 1e-12
