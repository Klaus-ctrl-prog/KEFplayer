import numpy as np
import pytest
import soundfile as sf

from kefplayer import upnp
from kefplayer.mediaserver import MediaServer
from kefplayer.player import Player
from kefplayer.runlog import RunLog
from mock_renderer import MockRenderer


@pytest.fixture
def rig(tmp_path):
    """Simulierter Renderer + Medienserver + Player. Protokoll: tmp_path/log.jsonl."""
    made = []

    def build(profile=None, mock_kw=None, **player_kw):
        mock = MockRenderer(**(mock_kw or {})).start()
        server = MediaServer(bind="127.0.0.1", port=0)
        server.start()
        renderer = upnp.Renderer.from_location(mock.location)
        player_kw.setdefault("poll_s", 0.02)
        player_kw.setdefault("end_tolerance_s", 1.0)
        player = Player(renderer, server, profile, log=RunLog(tmp_path / "log.jsonl"), **player_kw)
        made.append((mock, server))
        return mock, server, player

    yield build
    for mock, server in made:
        mock.stop()
        server.stop()


def make_tone(path, freq=1000.0, seconds=1.0, fs=44100, subtype="PCM_16", fmt="FLAC", channels=2, amp=0.2, seed=0):
    """Synthetische Testdatei (Sinus plus leises Rauschen, kanalweise leicht verschieden)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    cols = []
    for ch in range(channels):
        x = amp * np.sin(2 * np.pi * freq * t + ch) + 0.001 * rng.standard_normal(t.size)
        cols.append(x)
    data = np.stack(cols, axis=1)
    sf.write(str(path), data, fs, subtype=subtype, format=fmt)
    return path


@pytest.fixture
def tone_factory(tmp_path):
    def _make(name, **kw):
        fmt = kw.pop("fmt", "FLAC")
        return make_tone(tmp_path / f"{name}.{fmt.lower()}", fmt=fmt, **kw)

    return _make
