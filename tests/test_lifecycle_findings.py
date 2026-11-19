"""Lifecycle-Untersuchung Player / UPnP / MediaServer / Radio (Stand main 857ef60, Simulator, kein echtes KEF/ffmpeg).

Jeder Test beschreibt ein SOLL-Verhalten und ist xfail(strict=True), solange der Befund offen ist. Wird ein Befund
behoben, schlaegt der Test als XPASS an: dann die Markierung entfernen. Die Befund-Nummern (L-1 ...) stehen im Bericht.
"""
import http.client
import threading
import time

import pytest
from test_radio_supervisor import FakeDec, build

from kefplayer import upnp


class _Resp:
    def __init__(self, data=b"", exc=None):
        self.data, self.exc = data, exc

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def read(self):
        if self.exc:
            raise self.exc
        return self.data


def _patch_transport_info(monkeypatch, make, from_call=2):
    """Ab dem from_call-ten GetTransportInfo antwortet 'der Renderer' mit make() (HTTP 200, aber kaputt)."""
    real, cnt = upnp._OPENER.open, {"n": 0}

    def fake(req, timeout=None):
        if getattr(req, "data", None) and b"GetTransportInfo" in req.data:
            cnt["n"] += 1
            if cnt["n"] >= from_call:
                return make()
        return real(req, timeout=timeout)

    monkeypatch.setattr(upnp._OPENER, "open", fake)


BAD_BODIES = [b"<html><body>busy</body>", b"", b"kein xml"]


# ---- L-1: kaputte SOAP-Antworten muessen als UpnpError ankommen (classify_error kennt 'renderer_bad_response') ----

@pytest.mark.xfail(strict=True, reason="L-1: parse_response wirft ParseError statt UpnpError")
@pytest.mark.parametrize("body", BAD_BODIES)
def test_l1_parse_response_kaputtes_xml_ist_upnperror(body):
    with pytest.raises(upnp.UpnpError):
        upnp.parse_response(body)


@pytest.mark.xfail(strict=True, reason="L-1: parse_description wirft ParseError statt UpnpError")
def test_l1_parse_description_kaputtes_xml_ist_upnperror():
    with pytest.raises(upnp.UpnpError):
        upnp.parse_description(b"<html><body>busy</body>", "http://127.0.0.1:1/description.xml")


@pytest.mark.xfail(strict=True, reason="L-1: abgeschnittene Antwort (IncompleteRead) ist weder UpnpError noch OSError")
def test_l1_abgeschnittene_antwort_ist_upnperror_oder_oserror(rig, monkeypatch):
    mock, server, player = rig()
    monkeypatch.setattr(upnp._OPENER, "open", lambda req, timeout=None: _Resp(exc=http.client.IncompleteRead(b"<s:Env", 100)))
    with pytest.raises((upnp.UpnpError, OSError)):
        player.renderer.transport_state()


@pytest.mark.xfail(strict=True, reason="L-1: kaputte SOAP-Antwort bricht die ganze Queue per Ausnahme ab (kein Stop, kein Ergebnis)")
@pytest.mark.parametrize("body", BAD_BODIES)
def test_l1_kaputte_antwort_bricht_queue_nicht_ab(rig, tone_factory, monkeypatch, body):
    mock, server, player = rig()
    a = tone_factory("a", seconds=20.0, fs=8000, subtype="PCM_16")
    _patch_transport_info(monkeypatch, lambda: _Resp(body))
    res = player.play_queue([a])  # darf nicht werfen
    assert [r["end_reason"] for r in res] == ["error"]
    assert res[0]["error_category"] == "renderer_bad_response"
    assert any(act == "Stop" for act, _ in mock.actions), "Renderer wurde nicht gestoppt"


# ---- L-2: unerwartete Ausnahme -> Aufraeumen (Token abmelden, Renderer stoppen), egal ob sie weiterlaeuft ----

@pytest.mark.xfail(strict=True, reason="L-2: Aufraeumen von play_queue/_play_one liegt nicht in finally")
def test_l2_queue_raeumt_bei_unerwarteter_ausnahme_auf(rig, tone_factory, monkeypatch):
    mock, server, player = rig()
    a = tone_factory("a", seconds=20.0, fs=8000, subtype="PCM_16")
    real, n = player.renderer.transport_state, {"c": 0}

    def bad():
        n["c"] += 1
        if n["c"] >= 3:
            raise RuntimeError("unerwartet (simuliert)")
        return real()

    monkeypatch.setattr(player.renderer, "transport_state", bad)
    try:
        player.play_queue([a])
    except Exception:  # noqa: BLE001 - ob sie weiterlaeuft, legt dieser Test nicht fest
        pass
    assert not player.track_active.is_set()
    assert server.entries == {}, "Datei bleibt per Token abrufbar"
    assert any(act == "Stop" for act, _ in mock.actions), "Renderer spielt weiter"


@pytest.mark.xfail(strict=True, reason="L-2: Aufraeumen von play_stream liegt nicht in finally")
def test_l2_stream_raeumt_bei_unerwarteter_ausnahme_auf(rig, monkeypatch):
    mock, server, player = rig()
    real, n = player.renderer.transport_state, {"c": 0}

    def bad():
        n["c"] += 1
        if n["c"] >= 3:
            raise RuntimeError("unerwartet (simuliert)")
        return real()

    monkeypatch.setattr(player.renderer, "transport_state", bad)

    class Src:
        def read1(self, n=8192):
            time.sleep(0.05)
            return b"x" * 10

        def close(self):
            pass

    try:
        player.play_stream(Src, "audio/wav", ".wav", "T", "quelle")
    except Exception:  # noqa: BLE001
        pass
    assert not player.track_active.is_set()
    assert server.streams == {}, "Stream-Token bleibt registriert"
    assert any(act == "Stop" for act, _ in mock.actions), "Renderer spielt weiter"


# ---- L-3: attach_decoder nach stop() darf keinen Decoder unverwaltet laufen lassen ----

@pytest.mark.xfail(strict=True, reason="L-3: Supervisor haengt nach pipeline.stop() noch einen Decoder an, niemand stoppt ihn")
def test_l3_decoder_nach_pipeline_stop_wird_gestoppt():
    pipe, sup, events, made, gate_calls = build([FakeDec(5000, hold=True)])
    pipe.start()
    pipe.stop()  # wie cmd_radio: sup.stop() hat nach 10 s aufgegeben, pipe.stop() lief, der Supervisor-Thread ist noch im Gate
    err = sup._connect_once()  # liefert danach den Decoder (wie make_decoder nach dem langen Gate)
    assert made, "Decoder wurde nicht erzeugt (Annahme des Tests verletzt)"
    assert err is not None or made[0].stopped.is_set()
    assert made[0].stopped.is_set(), "Decoder laeuft weiter, obwohl die Pipeline beendet ist"


# ---- L-5: cmd_radio muss enden, wenn die Quelle endgueltig beendet ist, auch wenn der Renderer PLAYING meldet ----

@pytest.mark.xfail(strict=True, reason="L-5: kein Signal Supervisor -> Player; haengt bei Renderer, der nach EOF PLAYING bleibt")
def test_l5_cmd_radio_endet_wenn_quelle_beendet_ist_auch_bei_haengendem_renderer(tmp_path, monkeypatch):
    from conftest import make_tone
    from mock_renderer import MockRenderer
    from radio_helpers import FakeStation
    from test_cli_radio_e1b import _args, _fake_ffmpeg

    from kefplayer import cli

    class Sticky(MockRenderer):
        """Holt den Strom bis EOF und bleibt danach PLAYING (Modell eines Renderers, der EOF nicht als Ende meldet)."""

        def _run(self, gen, uri):
            time.sleep(self.start_delay)
            try:
                self._http("HEAD", uri)
                self._http("GET", uri)
            except Exception:  # noqa: BLE001
                self._set_if_gen(gen, "STOPPED")
                return
            with self._lock:
                if gen != self._gen:
                    return
                self._duration, self._pos, self._t_ref, self._state = 0.0, 0.0, time.perf_counter(), "PLAYING"
            while not self._closed:
                time.sleep(0.01)
                with self._lock:
                    if gen != self._gen:
                        return

    exe, _ = _fake_ffmpeg(monkeypatch, tmp_path, frames=96000)
    body = make_tone(tmp_path / "s.flac", seconds=1.0, fs=8000).read_bytes()
    out = {}
    with FakeStation(body=body, content_type="audio/flac") as st, Sticky(speedup=5.0) as mock:
        th = threading.Thread(
            target=lambda: out.update(rc=cli.main(_args(tmp_path, mock, st.url, ["--ffmpeg", exe, "--no-reconnect"]))),
            daemon=True)
        th.start()
        th.join(5.0)
        hing = th.is_alive()
        mock.stop()  # Aufraeumen: Renderer unerreichbar -> _wait endet nach 5 Fehlern
        th.join(30.0)
    assert not hing, "cmd_radio laeuft weiter, obwohl der Radio-Strom endgueltig beendet ist"
