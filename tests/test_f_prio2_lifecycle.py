"""Konsolidierter Fehlerbehebungsauftrag, Prioritaet 2: F-01, F-02 (spaeter F-11, F-14)."""
import pytest

from kefplayer import upnp
from kefplayer.mediaserver import MediaServer


class Src:
    def read1(self, n=8192):
        return b""

    def close(self):
        pass


def _boom_on(event):
    def cb(rec):
        if rec.get("event") == event:
            raise RuntimeError("Rueckruf wirft (simuliert)")
    return cb


def test_f01_start_callback_wirft_registrierung_wird_entfernt_titel(rig, tone_factory):
    mock, server, player = rig()
    player.on_event = _boom_on("track_start")
    a = tone_factory("a", seconds=0.5)
    with pytest.raises(Exception):
        player.play_queue([a])
    assert server.entries == {}


def test_f01_didl_erzeugung_scheitert_registrierung_wird_entfernt_titel(rig, tone_factory, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(upnp, "build_didl", lambda *a, **k: (_ for _ in ()).throw(ValueError("DIDL kaputt")))
    a = tone_factory("a", seconds=0.5)
    with pytest.raises(Exception):
        player.play_queue([a])
    assert server.entries == {}


def test_f01_start_callback_wirft_registrierung_wird_entfernt_stream(rig):
    mock, server, player = rig()
    player.on_event = _boom_on("stream_start")
    with pytest.raises(RuntimeError):
        player.play_stream(lambda: Src(), "audio/mpeg", ".mp3", "Sender", "http://x:1")
    assert server.streams == {}
    assert not player.track_active.is_set()


def test_f01_didl_erzeugung_scheitert_registrierung_wird_entfernt_stream(rig, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(upnp, "build_didl", lambda *a, **k: (_ for _ in ()).throw(ValueError("DIDL kaputt")))
    with pytest.raises(ValueError):
        player.play_stream(lambda: Src(), "audio/mpeg", ".mp3", "Sender", "http://x:1")
    assert server.streams == {}


def test_f02_register_ohne_advertise_host_hinterlaesst_keinen_eintrag(tmp_path):
    s = MediaServer(bind="0.0.0.0", port=0)  # kein advertise_host
    f = tmp_path / "a.flac"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        s.register(f, "audio/flac")
    with pytest.raises(ValueError):
        s.register_stream(lambda: Src(), "audio/mpeg", "s.mp3")
    assert s.entries == {} and s.streams == {}


# ---- F-14: Radioquelle beendet, Renderer meldet weiter PLAYING ----

import threading
import time

from kefplayer import player as player_mod


def _sticky(player, monkeypatch):
    stops = []
    monkeypatch.setattr(player.renderer, "transport_state", lambda: "PLAYING")
    monkeypatch.setattr(player.renderer, "position_s", lambda: 0.0)
    real_stop = player.renderer.stop
    monkeypatch.setattr(player.renderer, "stop", lambda: (stops.append(1), real_stop())[1])
    return stops


def test_f14_quelle_beendet_renderer_bleibt_playing_endet_in_frist(rig, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(player_mod, "EOF_GRACE_S", 0.4)
    stops = _sticky(player, monkeypatch)
    t0 = time.monotonic()
    res = player.play_stream(lambda: Src(), "audio/mpeg", ".mp3", "Sender", "http://x:1", source_done=lambda: True)
    assert res["end_reason"] == "source_ended"
    assert time.monotonic() - t0 < 5.0
    assert stops, "Renderer wurde nicht gestoppt"
    assert server.streams == {} and not player.track_active.is_set()


def test_f14_ohne_quellenende_laeuft_der_stream_weiter_bis_stop(rig, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(player_mod, "EOF_GRACE_S", 0.2)
    _sticky(player, monkeypatch)
    threading.Timer(1.0, player.stop).start()
    res = player.play_stream(lambda: Src(), "audio/mpeg", ".mp3", "Sender", "http://x:1", source_done=lambda: False)
    assert res["end_reason"] == "stopped"


def test_f14_quellenende_nur_voruebergehend_kein_abbruch(rig, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(player_mod, "EOF_GRACE_S", 0.6)
    _sticky(player, monkeypatch)
    t0 = time.monotonic()
    flag = lambda: time.monotonic() - t0 > 0.5 and time.monotonic() - t0 < 0.9  # kurzes "Ende", dann wieder Daten
    threading.Timer(1.8, player.stop).start()
    res = player.play_stream(lambda: Src(), "audio/mpeg", ".mp3", "Sender", "http://x:1", source_done=flag)
    assert res["end_reason"] == "stopped"
