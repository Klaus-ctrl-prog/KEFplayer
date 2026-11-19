"""KEP-013: Nach einem Verarbeitungsfehler kann die RadioPipeline beendet sein, obwohl der Supervisor weiter verbindet.

Beobachtet (main 857ef60, Simulator): Die Pipeline geht auf 'failed', ihr Thread endet, der Ringpuffer ist
geschlossen. Der Supervisor sieht 'failed', behandelt es wie eine Luecke und verbindet neu: Er startet einen neuen
Decoder, ruft attach_decoder auf (ohne Wirkung, da kein Thread mehr liest) und meldet 'radio_stream_up', obwohl nichts
mehr fliesst. Wiederholt sich bis das Budget erschoepft ist. Die Decoder-Prozesse der fehlgeschlagenen Laeufe werden
dabei NICHT beendet (pipeline.stop() stoppt nur den zuletzt angehaengten).

Die Tests unten beschreiben das SOLL-Verhalten (Invarianten). KEP-013 ist behoben (Pipeline-Ende ist endgueltig, Supervisor gibt auf, Decoder werden gestoppt).
Der Test 'voraussetzung' sichert, dass die Fehlereinspeisung die Pipeline wirklich beendet.
"""
import time

import pytest
from test_radio_supervisor import FakeDec, build, until

from kefplayer.radio_supervisor import ReconnectBudget

class _BoomRead(FakeDec):
    """Decoder, dessen read() beim zweiten Aufruf eine Ausnahme wirft (Fehler im Pipeline-Thread)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads = 0

    def read(self, n):
        self.reads += 1
        if self.reads == 2:
            raise RuntimeError("Lesefehler (simuliert)")
        return super().read(n)


def _setup(mode):
    decs = [_BoomRead(5000, hold=True)] if mode == "read" else [FakeDec(5000, hold=True)]
    decs += [FakeDec(5000, hold=True) for _ in range(6)]
    b = ReconnectBudget(backoff=(0.05,), stable_s=999, max_per_window=4, max_total=10)
    pipe, sup, events, made, gate_calls = build(decs, budget=b)
    if mode == "dsp":
        real, calls = pipe.dsp.process, {"n": 0}

        def boom(block):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("Verarbeitungsfehler (simuliert)")
            return real(block)

        pipe.dsp.process = boom
    pipe.start()
    sup.start()
    return pipe, sup, events, made, gate_calls


def _finish(pipe, sup):
    sup.stop()
    pipe.stop()


@pytest.mark.parametrize("mode", ["dsp", "read"])
def test_voraussetzung_fehler_beendet_die_pipeline(mode):
    pipe, sup, events, made, gate_calls = _setup(mode)
    try:
        assert until(lambda: pipe.stats().state == "failed", 5)
        pipe._thread.join(2)
        assert not pipe._thread.is_alive() and pipe.ring.closed
    finally:
        _finish(pipe, sup)


@pytest.mark.parametrize("mode", ["dsp", "read"])
def test_kein_stream_up_bei_toter_pipeline(mode):
    """I1: 'radio_stream_up' darf nach dem Ausfall der Pipeline nicht erneut gemeldet werden."""
    pipe, sup, events, made, gate_calls = _setup(mode)
    try:
        assert until(lambda: pipe.stats().state == "failed", 5)
        time.sleep(1.0)  # Zeit fuer (unerwuenschte) Reconnects
        ups = [e for e, _ in events if e == "radio_stream_up"]
        assert len(ups) == 1, f"{len(ups)}x radio_stream_up, Pipeline ist tot"
    finally:
        _finish(pipe, sup)


@pytest.mark.parametrize("mode", ["dsp", "read"])
def test_keine_weiteren_verbindungsversuche_bei_toter_pipeline(mode):
    """I5: Gate und Decoder-Start laufen nicht weiter, wenn niemand mehr liest (Budget wird nicht verbrannt)."""
    pipe, sup, events, made, gate_calls = _setup(mode)
    try:
        assert until(lambda: pipe.stats().state == "failed", 5)
        time.sleep(1.0)
        assert len(made) == 1 and len(gate_calls) == 1, f"{len(made)} Decoder, {len(gate_calls)} Pruefungen"
    finally:
        _finish(pipe, sup)


@pytest.mark.parametrize("mode", ["dsp", "read"])
def test_kein_decoder_leck(mode):
    """I2/I3: Nach Stopp sind ALLE gestarteten Decoder beendet (sonst bleiben ffmpeg-Prozesse uebrig)."""
    pipe, sup, events, made, gate_calls = _setup(mode)
    try:
        assert until(lambda: pipe.stats().state == "failed", 5)
        time.sleep(1.0)
    finally:
        _finish(pipe, sup)
    assert all(d.stopped.is_set() for d in made), [d.stopped.is_set() for d in made]


@pytest.mark.parametrize("mode", ["dsp", "read"])
def test_entweder_erholung_oder_klares_ende(mode):
    """I4: Entweder liest die Pipeline wieder (state streaming) oder der Supervisor beendet mit klarer Kategorie.
    Verboten: Supervisor 'streaming'/'reconnecting', Pipeline tot."""
    pipe, sup, events, made, gate_calls = _setup(mode)
    try:
        assert until(lambda: pipe.stats().state == "failed", 5)
        time.sleep(1.0)
        s, p = sup.stats(), pipe.stats()
        recovered = p.state == "streaming" and pipe._thread.is_alive()
        ended_clearly = s.state == "failed" and s.last_category not in (None, "stream_reconnect_exhausted")
        assert recovered or ended_clearly, f"Supervisor={s.state}/{s.last_category}, Pipeline={p.state}"
    finally:
        _finish(pipe, sup)


# ---- Beleg mit ECHTEN Prozessen (fake_decoder.py als ffmpeg-Ersatz): bleiben Decoder-Prozesse zurueck? ----

def test_keine_prozesse_bleiben_zurueck_echte_decoder():
    """I2/I3 mit echten Subprozessen. Zeigt, dass das Leck mehr ist als ein Flag im Simulator:
    jeder uebersehene Decoder waere ein laufender ffmpeg mit offener Senderverbindung."""
    import sys
    from pathlib import Path

    from kefplayer.decoder import Decoder, FfmpegInfo

    fake = str(Path(__file__).with_name("fake_decoder.py"))
    info = FfmpegInfo(path="x", version="fake", sha256="00" * 32,
                      argv_prefix=(sys.executable, fake, "--mode", "sine", "--frames", "5000000"))
    decs = []
    for _ in range(6):
        d = Decoder(info, "http://sender.invalid/s", rate=48000, channels=2)
        d.start()
        decs.append(d)
    b = ReconnectBudget(backoff=(0.05,), stable_s=999, max_per_window=4, max_total=10)
    pipe, sup, events, made, gate_calls = build(decs, budget=b)
    real, calls = pipe.dsp.process, {"n": 0}

    def boom(block):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("Verarbeitungsfehler (simuliert)")
        return real(block)

    pipe.dsp.process = boom
    try:
        pipe.start()
        sup.start()
        assert until(lambda: pipe.stats().state == "failed", 8)
        time.sleep(1.0)
        sup.stop()
        pipe.stop()
        time.sleep(0.3)
        alive = [d._proc.pid for d in made if d._proc.poll() is None]
        assert not alive, f"{len(alive)} von {len(made)} benutzten Decoder-Prozessen laufen weiter: {alive}"
    finally:
        for d in decs:
            d.stop(0.3)
