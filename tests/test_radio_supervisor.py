import threading
import time
from pathlib import Path

import numpy as np
import pytest

from kefplayer import radio_supervisor, upnp
from kefplayer.dsp import Profile
from kefplayer.quality import Verdict
from kefplayer.radio import RadioError
from kefplayer.radio_pipeline import RadioPipeline
from kefplayer.radio_supervisor import RadioSupervisor, ReconnectBudget
from kefplayer.wavstream import pack_s24le

RATE = 48000
URL = "https://sender.example.invalid:8443/pfad/TOKEN123?key=SECRET"
OK = Verdict("full", None, [])


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeDec:
    def __init__(self, frames=0, value=0.25, hold=False):
        self.data = np.full((frames, 2), value, dtype="<f4").tobytes()
        self.pos, self.hold = 0, hold
        self.stopped = threading.Event()

    def read(self, n):
        if self.pos < len(self.data):
            c = self.data[self.pos:self.pos + min(n, 4096)]
            self.pos += len(c)
            return c
        if self.hold:
            self.stopped.wait(30)
        return b""

    def stop(self, grace_s=0):
        self.stopped.set()


def build(decoders, gates=None, *, budget=None, reconnect=True, stall_s=5.0, retry_initial=False):
    events = []
    made = []
    gate_calls = []
    decs = list(decoders)
    gts = list(gates) if gates else None

    def gate(url):
        gate_calls.append(url)
        if gts:
            g = gts.pop(0) if len(gts) > 1 else gts[0]
            if isinstance(g, Exception):
                raise g
            return g
        return OK

    def make(url):
        d = decs.pop(0)
        if isinstance(d, Exception):
            raise d
        made.append(d)
        return d

    pipe = RadioPipeline(None, Profile(), rate=RATE, supervised=True, block_frames=100)
    sup = RadioSupervisor(URL, pipeline=pipe, gate=gate, make_decoder=make,
                          budget=budget or ReconnectBudget(backoff=(0.05,), stable_s=999),
                          emit=lambda ev, **kw: events.append((ev, kw)), reconnect=reconnect,
                          stall_s=stall_s, retry_initial=retry_initial, poll_s=0.02)
    return pipe, sup, events, made, gate_calls


def until(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------- Budget (reine Logik) ----------------
def test_budget_backoff_fenster_gesamt_und_stabil():
    c = FakeClock()
    b = ReconnectBudget(clock=c, backoff=(2, 5, 10), window_s=100, max_per_window=3, max_total=5, stable_s=60)
    got = []
    for _ in range(3):
        d, why = b.next_delay()
        got.append(d)
        b.record_attempt()
        c.t += 1
    assert got == [2, 5, 10]
    assert b.next_delay() == (None, "window_budget_exhausted")
    c.t += 200  # Fenster abgelaufen
    assert b.next_delay() == (10, None)  # Backoff bleibt auf dem letzten Wert
    b.record_stable(10)  # zu kurz: kein Reset
    assert b.consecutive == 3
    b.record_stable(60)
    assert b.consecutive == 0 and b.next_delay() == (2, None)
    b.record_attempt()
    b.record_attempt()
    assert b.next_delay() == (None, "total_budget_exhausted")


# ---------------- Supervisor ----------------
def test_luecke_reconnect_stille_und_kein_eof():
    d1, d2 = FakeDec(500, 0.25), FakeDec(500, -0.25, hold=True)
    pipe, sup, events, made, gate_calls = build([d1, d2])
    pipe.start()
    sup.start()
    assert sup.wait_ready(5)
    src = pipe.open_source()
    src.read1()
    got = bytearray()
    saw_silence_in_gap = False
    for _ in range(400):
        c = src.read1(8192)
        assert c != b""  # Strom bleibt waehrend der Luecke offen
        got += c
        if sup.stats().reconnects >= 1 and len(got) >= 2 * 500 * 6 and pipe.stats().underruns:
            saw_silence_in_gap = True
        if len(got) >= 1000 * 6 + 3000 and sup.stats().state == "streaming" and sup.stats().reconnects == 1:
            break
    assert sup.stats().reconnects == 1 and len(gate_calls) == 2
    assert pipe.stats().gaps == 1 and saw_silence_in_gap
    one = pack_s24le(np.array([[0.25, -0.25]]))[0]
    neg = pack_s24le(np.array([[-0.25, 0.25]]))[0]  # zweiter Decoder: -0.25 auf beiden Kanaelen
    assert pack_s24le(np.full((1, 2), 0.25))[0] in bytes(got)
    assert pack_s24le(np.full((1, 2), -0.25))[0] in bytes(got)
    names = [e for e, _ in events]
    assert names[:2] == ["radio_connect", "radio_stream_up"] and "radio_gap" in names and "radio_reconnect_wait" in names
    sup.stop()
    pipe.stop()


def test_qualitaetspruefung_vor_jedem_versuch_und_ablehnung_beendet():
    bad = Verdict("rejected", "stream_quality_too_low", ["unter 128"])
    pipe, sup, events, made, gate_calls = build([FakeDec(200), FakeDec(200)], gates=[OK, bad])
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().state == "failed")
    s = sup.stats()
    assert s.last_category == "stream_quality_too_low" and len(gate_calls) == 2
    assert len(made) == 1  # zweiter Decoder wurde nie gestartet
    src = pipe.open_source()
    src.read1()
    assert until(lambda: pipe.stats().state == "ended")
    pipe.stop()


def test_budget_erschoepft_mit_gate_fehlern():
    unreachable = RadioError("stream_unreachable", "weg")
    b = ReconnectBudget(backoff=(0.02,), max_per_window=3, max_total=10, stable_s=999)
    pipe, sup, events, made, gate_calls = build([FakeDec(100)], gates=[OK, unreachable], budget=b)
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().state == "failed")
    s = sup.stats()
    assert s.last_category == "stream_reconnect_exhausted"
    assert len(gate_calls) == 4  # 1 Start + 3 Versuche, dann Fenster erschoepft
    assert b.total == 3
    assert any(e == "radio_failed" and kw["category"] == "stream_reconnect_exhausted" for e, kw in events)
    pipe.stop()


def test_erster_versuch_scheitert_ohne_retry_standard():
    pipe, sup, events, made, gate_calls = build([FakeDec(1)], gates=[RadioError("stream_unreachable", "x")])
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().state == "failed")
    assert sup.stats().last_category == "stream_unreachable" and len(gate_calls) == 1
    assert not sup.wait_ready(0.1)
    pipe.stop()


def test_erster_versuch_mit_retry_initial():
    pipe, sup, events, made, gate_calls = build([FakeDec(100, hold=True)],
                                                gates=[RadioError("stream_unreachable", "x"), OK], retry_initial=True)
    pipe.start()
    sup.start()
    assert sup.wait_ready(5) and len(gate_calls) == 2
    sup.stop()
    pipe.stop()


def test_haengender_decoder_wird_erkannt_und_ersetzt():
    d1, d2 = FakeDec(0, hold=True), FakeDec(100, hold=True)
    pipe, sup, events, made, gate_calls = build([d1, d2], stall_s=0.3)
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().reconnects == 1 and len(made) == 2, 8)
    assert d1.stopped.is_set()
    assert any(e == "radio_gap" and kw["reason"] == "stalled" for e, kw in events)
    sup.stop()
    pipe.stop()


def test_reconnect_abgeschaltet():
    pipe, sup, events, made, gate_calls = build([FakeDec(100), FakeDec(100)], reconnect=False)
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().state == "failed")
    assert len(made) == 1 and len(gate_calls) == 1 and sup.stats().last_category == "stream_ended"
    pipe.stop()


def test_stop_waehrend_wartezeit_ist_schnell():
    b = ReconnectBudget(backoff=(30.0,), stable_s=999)
    pipe, sup, events, made, gate_calls = build([FakeDec(100), FakeDec(100)], budget=b)
    pipe.start()
    sup.start()
    assert until(lambda: sup.stats().state == "reconnecting")
    t0 = time.time()
    sup.stop()
    pipe.stop()
    assert time.time() - t0 < 3 and len(made) == 1


def test_kein_zugriff_auf_renderer_und_lock_blockiert_nicht():
    import ast
    tree = ast.parse(Path(radio_supervisor.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "upnp" not in (node.module or "") and all(a.name != "upnp" for a in node.names)
        elif isinstance(node, ast.Import):
            assert all("upnp" not in a.name for a in node.names)
        elif isinstance(node, ast.Name):
            assert node.id != "RENDERER_LOCK"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "RENDERER_LOCK"
    with upnp.RENDERER_LOCK:  # Test haelt den Lock; der Supervisor darf trotzdem arbeiten
        pipe, sup, events, made, gate_calls = build([FakeDec(100), FakeDec(100, hold=True)])
        pipe.start()
        sup.start()
        assert until(lambda: sup.stats().reconnects == 1 and len(made) == 2, 6)
        sup.stop()
        pipe.stop()


def test_ereignisse_ohne_volle_url():
    pipe, sup, events, made, gate_calls = build([FakeDec(100), FakeDec(100, hold=True)])
    pipe.start()
    sup.start()
    assert until(lambda: len(made) == 2, 6)
    sup.stop()
    pipe.stop()
    text = repr(events)
    assert "TOKEN123" not in text and "SECRET" not in text and "https://sender.example.invalid:8443/..." in text
