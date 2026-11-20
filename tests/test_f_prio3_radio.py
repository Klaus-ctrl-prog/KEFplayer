"""Konsolidierter Fehlerbehebungsauftrag, Prioritaet 3 (Radio, HLS, Speicher): F-03 ... F-16."""
import pytest
from test_radio_supervisor import until

from kefplayer.ringbuffer import ByteRing

FRAME = 6  # Stereo, PCM 24 Bit


def _frames(start, count):
    """Frame i = sechs gleiche Bytes (i % 256): ein verschobenes Frame faellt sofort auf."""
    return b"".join(bytes([(start + i) % 256]) * FRAME for i in range(count))


def _assert_aligned_frames(data):
    assert len(data) % FRAME == 0
    for i in range(0, len(data), FRAME):
        assert len(set(data[i:i + FRAME])) == 1, f"Frame bei Byte {i} ist angeschnitten/verschoben"


def test_f03_kapazitaet_wird_auf_ganze_frames_abgerundet():
    r = ByteRing(100, frame_bytes=FRAME)  # 100 ist nicht durch 6 teilbar
    assert r.capacity == 96
    assert ByteRing(3, frame_bytes=FRAME).capacity == FRAME  # mindestens ein Frame


def test_f03_ueberlauf_verwirft_nur_ganze_frames():
    r = ByteRing(100, frame_bytes=FRAME)
    for i in range(0, 60, 7):  # Bloecke zu 7 Frames: Ueberlauf endet nie auf einer Framegrenze des Chunks
        r.put(_frames(i, 7))
    assert r.overrun_bytes > 0 and r.overrun_bytes % FRAME == 0
    assert len(r) % FRAME == 0 and len(r) <= r.capacity
    out = r.get(10_000)
    _assert_aligned_frames(out)
    assert out[:FRAME] != b""  # Rest ist die juengste Seite des Stroms
    last = (56 + 6) % 256
    assert out[-1] == last  # juengstes Frame bleibt erhalten


def test_f03_einzelner_zu_grosser_block_bleibt_frame_ausgerichtet():
    r = ByteRing(100, frame_bytes=FRAME)
    r.put(_frames(0, 40))  # 240 Byte > Kapazitaet 96
    out = r.get(10_000)
    _assert_aligned_frames(out)
    assert len(out) == 96 and r.overrun_bytes == 240 - 96


def test_f03_get_liefert_nur_ganze_frames_auch_bei_unpassender_anfragegroesse():
    r = ByteRing(600, frame_bytes=FRAME)
    r.put(_frames(0, 50))
    got = b""
    while len(got) < 300:
        part = r.get(8192 if len(got) == 0 else 7, timeout=0.1)  # 8192 und 7 sind keine Vielfachen von 6
        assert part and len(part) % FRAME == 0
        got += part
    _assert_aligned_frames(got)


def test_f03_angefangenes_frame_wird_mit_den_naechsten_daten_vervollstaendigt():
    r = ByteRing(600, frame_bytes=FRAME)
    data = _frames(0, 10)
    r.put(data[:23])  # 3 Frames + 5 Byte
    assert len(r) == 18
    r.put(data[23:])
    assert r.get(10_000) == data


def test_f03_pipeline_ring_ist_frame_ausgerichtet_bei_krummer_kapazitaet():
    from kefplayer.dsp import Profile
    from kefplayer.radio_pipeline import RadioPipeline
    p = RadioPipeline(None, Profile(), rate=44100, channels=2, buffer_seconds=0.0007, supervised=True)
    assert p.ring.frame_bytes == 6 and p.ring.capacity % 6 == 0
    p.ring.put(_frames(0, 100))
    _assert_aligned_frames(p.ring.get(10_000))


# ---- F-04 / F-05: Qualitaetsmessung (quality.native_probe) ----

import random
import sys
import threading
import time

from kefplayer import quality as Q
from radio_helpers import adts_stream


def test_f04_messung_exakt_bei_variierenden_blockgroessen_und_kurzem_fenster():
    """Nicht reproduzierbarer Befund: der erste Block nach der Aufwaermphase wird bewusst verworfen, die Zeitspanne
    beginnt mit seinem Eintreffen. Das ist rechnerisch exakt; dieser Test haelt das fest."""
    rnd = random.Random(1)
    head = adts_stream(n_frames=20)
    for _ in range(60):
        kbps = rnd.choice([96, 128, 192, 256])
        bps = kbps * 125.0
        warm, win = rnd.choice([(0.5, 1.0), (1.0, 2.0), (0.2, 0.6)])
        clk = type("C", (), {"t": 0.0, "__call__": lambda self: self.t})()

        class S:
            first = True

            def read1(self, n):
                if self.first:
                    self.first = False
                    return head[:4096]
                k = max(1, min(rnd.choice([n, n // 2, 1000, 64, 3000, 8000]), n))
                clk.t += k / bps
                return bytes(k)

            def close(self):
                pass

        rep = Q.native_probe(lambda: S(), clock=clk, warmup_s=warm, window_s=win)
        assert rep.measured_kbps == pytest.approx(kbps, rel=0.001)


class _Blocking:
    """Liefert den ADTS-Kopf, dann blockiert read1 (bis release) - Modell eines Senders, der verstummt."""

    def __init__(self, head_first=True):
        self.release = threading.Event()
        self.closed = threading.Event()
        self._first = head_first
        self.head = adts_stream(n_frames=20)

    def read1(self, n=4096):
        if self._first:
            self._first = False
            return self.head[:4096]
        self.release.wait(10.0)
        return b""

    def close(self):
        self.closed.set()


def test_f05_blockierender_read_ueberschreitet_die_gesamtfrist_nicht():
    src = _Blocking()
    t0 = time.monotonic()
    rep = Q.native_probe(lambda: src, warmup_s=0.2, window_s=0.4)
    took = time.monotonic() - t0
    try:
        assert took < 0.6 + 1.0, f"Messung dauerte {took:.2f} s (Gesamtfrist 0,6 s + Reserve)"
        assert rep.codec == "aac" and rep.measured_kbps is None
        assert any("Gesamtfrist" in n for n in rep.notes)
    finally:
        src.release.set()
    assert src.closed.wait(3.0), "Quelle wird nach dem Ende des Lesevorgangs nicht geschlossen"


@pytest.mark.parametrize("sizes", [(1,), (1, 7, 3), (5,)])
def test_f05_tropfende_quelle_endet_in_der_gesamtfrist(sizes):
    head = adts_stream(n_frames=20)

    class Drip:
        def __init__(self):
            self.i = 0
            self.closed = threading.Event()

        def read1(self, n=4096):
            self.i += 1
            if self.i == 1:
                return head[:4096]
            time.sleep(0.12)  # jede einzelne Leseoperation ist kurz, die Summe nicht
            return bytes(sizes[self.i % len(sizes)])

        def close(self):
            self.closed.set()

    src = Drip()
    t0 = time.monotonic()
    rep = Q.native_probe(lambda: src, warmup_s=0.2, window_s=0.5)
    assert time.monotonic() - t0 < 0.7 + 1.0
    assert rep.codec == "aac"
    assert src.closed.wait(3.0)


def test_f05_blockierendes_oeffnen_ueberschreitet_die_gesamtfrist_nicht():
    gate = threading.Event()

    def slow_open():
        gate.wait(10.0)
        raise RuntimeError("zu spaet")

    t0 = time.monotonic()
    rep = Q.native_probe(slow_open, warmup_s=0.2, window_s=0.3)
    gate.set()
    assert time.monotonic() - t0 < 0.5 + 1.0
    assert rep.codec == "unknown" and rep.measured_kbps is None and any("Gesamtfrist" in n for n in rep.notes)


def test_f05_oeffnungsfehler_wird_weiterhin_durchgereicht():
    def bad():
        raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        Q.native_probe(bad, warmup_s=0.1, window_s=0.2)


def test_f05_timeout_waehrend_der_messung_behaelt_gemessenes():
    """Rate bekannt (16000 B/s = 128 kbit/s), danach verstummt der Sender: bis zum Abbruch Gemessenes bleibt."""
    head = adts_stream(n_frames=20)

    class Then:
        def __init__(self):
            self.i = 0
            self.release = threading.Event()

        def read1(self, n=4096):
            self.i += 1
            if self.i == 1:
                return head[:4096]
            if self.i <= 14:
                time.sleep(0.05)
                return bytes(800)  # 800 B / 0,05 s = 16000 B/s
            self.release.wait(10.0)
            return b""

        def close(self):
            pass

    src = Then()
    rep = Q.native_probe(lambda: src, warmup_s=0.1, window_s=0.8)
    src.release.set()
    assert any("Gesamtfrist" in n for n in rep.notes)
    if rep.measured_kbps is not None:
        assert 90 < rep.measured_kbps < 140


# ---- F-12 / F-13: HLS ----

import http.server
import socketserver

from kefplayer import hls
from kefplayer.radio import RadioError

BASE = "http://h.example/live/media.m3u8"


def _media(*lines):
    return "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:4\n" + "\n".join(lines) + "\n"


def _parse(*lines):
    kind, pl = hls.parse_playlist(_media(*lines), BASE)
    assert kind == "media"
    return pl


def test_f12_expliziter_offset():
    pl = _parse("#EXTINF:2.0,", "#EXT-X-BYTERANGE:1000@5000", "seg.ts")
    assert pl.segments == [("http://h.example/live/seg.ts", 2.0)] and pl.ranges == [(5000, 1000)]


def test_f12_impliziter_folgeoffset_derselben_ressource():
    pl = _parse("#EXTINF:2,", "#EXT-X-BYTERANGE:1000@0", "a.ts",
                "#EXTINF:2,", "#EXT-X-BYTERANGE:1000", "a.ts",
                "#EXTINF:2,", "#EXT-X-BYTERANGE:500", "a.ts")
    assert pl.ranges == [(0, 1000), (1000, 1000), (2000, 500)]


def test_f12_mehrere_ressourcen_haben_eigene_offsets():
    pl = _parse("#EXTINF:2,", "#EXT-X-BYTERANGE:100@0", "a.ts",
                "#EXTINF:2,", "#EXT-X-BYTERANGE:200@50", "b.ts",
                "#EXTINF:2,", "#EXT-X-BYTERANGE:100", "a.ts",
                "#EXTINF:2,", "#EXT-X-BYTERANGE:100", "b.ts",
                "#EXTINF:2,", "plain.ts")
    assert pl.ranges == [(0, 100), (50, 200), (100, 100), (250, 100), None]


@pytest.mark.parametrize("bad", ["0", "abc", "-5@1", "10@x", "", "1@2@3", "99999999999999999999"])
def test_f12_ungueltige_bereiche_werden_nicht_geladen(bad):
    pl = _parse("#EXTINF:2,", f"#EXT-X-BYTERANGE:{bad}", "a.ts", "#EXTINF:2,", "ok.ts")
    assert [u.rsplit("/", 1)[1] for u, _ in pl.segments] == ["ok.ts"] and pl.invalid_ranges == 1


def test_f12_implizit_ohne_vorgaenger_ist_ungueltig():
    pl = _parse("#EXTINF:2,", "#EXT-X-BYTERANGE:500", "a.ts")
    assert pl.segments == [] and pl.invalid_ranges == 1


def test_f12_byterange_gilt_nur_fuer_das_naechste_segment():
    pl = _parse("#EXTINF:2,", "#EXT-X-BYTERANGE:10@0", "a.ts", "#EXTINF:2,", "a.ts")
    assert pl.ranges == [(0, 10), None]


def test_f12_map_byterange_ist_eigenstaendig_und_beeinflusst_segment_offsets_nicht():
    pl = _parse('#EXT-X-MAP:URI="f.mp4",BYTERANGE="720@0"',
                "#EXTINF:2,", "#EXT-X-BYTERANGE:500", "f.mp4")  # implizit ohne Segment-Vorgaenger: ungueltig
    assert pl.map_range == (0, 720) and not pl.map_bad
    assert pl.segments == [] and pl.invalid_ranges == 1
    pl = _parse('#EXT-X-MAP:URI="f.mp4",BYTERANGE="720@0"', "#EXTINF:2,", "#EXT-X-BYTERANGE:500@720", "f.mp4")
    assert pl.ranges == [(720, 500)]
    assert _parse('#EXT-X-MAP:URI="f.mp4",BYTERANGE="720"').map_bad  # Offset ist bei MAP Pflicht
    assert _parse('#EXT-X-MAP:URI="f.mp4",BYTERANGE="0@5"').map_bad


def test_f12_check_variant_ruft_fetch_mit_dem_bereich_und_nie_mit_der_ganzen_ressource():
    calls = []
    seg = adts_stream(n_frames=100)
    playlist = _media("#EXTINF:2,", "#EXT-X-BYTERANGE:1000@0", "a.aac",
                      "#EXTINF:2,", "#EXT-X-BYTERANGE:1000", "a.aac",
                      "#EXTINF:2,", "#EXT-X-BYTERANGE:1000", "a.aac").encode()

    def fetch(url, max_bytes, **kw):
        calls.append((url.rsplit("/", 1)[1], kw.get("byte_range")))
        return (playlist if url.endswith(".m3u8") else seg), url

    hls.check_variant(BASE, window_s=6.0, fetch=fetch)
    segs = [c for c in calls if c[0] == "a.aac"]
    assert segs == [("a.aac", (0, 1000)), ("a.aac", (1000, 1000)), ("a.aac", (2000, 1000))]


class _RangeServer:
    """Ressource 0..255 wiederholt; Range wahlweise unterstuetzt (206) oder ignoriert (200)."""

    def __init__(self, size=300_000, honor=True, bad_content_range=False):
        outer = self
        self.data = bytes(i % 251 for i in range(size))
        self.requests = []
        self.sent = 0

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                outer.requests.append(rng)
                body, status = outer.data, 200
                headers = {}
                if rng and honor:
                    a, b = rng.split("=")[1].split("-")
                    a, b = int(a), min(int(b), len(outer.data) - 1)
                    body, status = outer.data[a:b + 1], 206
                    start = a + 1 if bad_content_range else a
                    headers["Content-Range"] = f"bytes {start}-{b}/{len(outer.data)}"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                    outer.sent += len(body)
                except OSError:
                    pass

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/r.ts"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.mark.parametrize("honor", [True, False])
def test_f12_http_fetch_holt_nur_den_angegebenen_bereich(honor):
    s = _RangeServer(honor=honor)
    try:
        data, _ = hls.http_fetch(s.url, hls.MAX_SEGMENT_BYTES, byte_range=(5000, 1000))
        assert data == s.data[5000:6000]
        assert s.requests == ["bytes=5000-5999"]
        if honor:
            assert until(lambda: s.sent == 1000, 3), "bei 206 darf nur der Bereich uebertragen werden, nicht die ganze Ressource"
            time.sleep(0.1)
            assert s.sent == 1000
    finally:
        s.close()


def test_f12_http_fetch_lehnt_falschen_content_range_ab():
    s = _RangeServer(bad_content_range=True)
    try:
        with pytest.raises(RadioError):
            hls.http_fetch(s.url, hls.MAX_SEGMENT_BYTES, byte_range=(5000, 1000))
    finally:
        s.close()


def test_f12_http_fetch_bereich_ueber_das_ende_und_zu_gross():
    s = _RangeServer(size=10_000, honor=False)
    try:
        with pytest.raises(RadioError):  # Ressource kuerzer als Offset+Laenge
            hls.http_fetch(s.url, hls.MAX_SEGMENT_BYTES, byte_range=(9_500, 1000))
        with pytest.raises(RadioError):  # Bereich groesser als erlaubt
            hls.http_fetch(s.url, 100, byte_range=(0, 1000))
        with pytest.raises(RadioError):  # Server ignoriert Range und Offset waere riesig
            hls.http_fetch(s.url, hls.MAX_SEGMENT_BYTES, byte_range=(hls.MAX_RANGE_SKIP_BYTES + 1, 10))
    finally:
        s.close()


def test_f12_http_fetch_ohne_bereich_bleibt_unveraendert():
    s = _RangeServer(size=20_000)
    try:
        data, _ = hls.http_fetch(s.url, hls.MAX_SEGMENT_BYTES)
        assert data == s.data and s.requests == [None]
    finally:
        s.close()


@pytest.mark.parametrize("v", ["1e999", "-1e999", "NaN", "nan", "Infinity", "-Infinity", "inf", "abc", "", None, "-5",
                               "1e30", "9" * 400, "0x10", "1,5"])
def test_f13_to_int_lehnt_ungueltiges_kontrolliert_ab(v):
    assert hls._to_int(v) is None


def test_f13_to_int_gueltige_werte():
    assert hls._to_int("128000") == 128000 and hls._to_int("1.5e5") == 150000 and hls._to_int("0") == 0
    assert hls._to_int(str(1 << 53)) == 1 << 53


def test_f13_master_playlist_mit_ueberlaufwerten_wirft_nicht():
    text = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1e999,AVERAGE-BANDWIDTH=Infinity,CODECS="mp4a.40.2"\nlow.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=NaN\nhigh.m3u8\n')
    kind, variants = hls.parse_playlist(text, "http://h.example/m.m3u8")
    assert kind == "master" and len(variants) == 2
    assert all(v.bandwidth is None and v.avg_bandwidth is None for v in variants)
    assert hls.select_variant(variants) is not None


# ---- F-16: MediaServer-Anfrageprotokoll ----

import http.client

import kefplayer.mediaserver as M


def _get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


@pytest.fixture
def media(tmp_path):
    s = M.MediaServer(bind="127.0.0.1", port=0, advertise_host="127.0.0.1")
    s.start()
    f = tmp_path / "a.bin"
    f.write_bytes(bytes(1000))
    yield s, f
    s.stop()


def test_f16_viele_requests_auf_einen_token_sind_begrenzt(media, monkeypatch):
    s, f = media
    monkeypatch.setattr(M, "MAX_LOG_PER_TOKEN", 20)
    url, token = s.register(f, "application/octet-stream")
    path = url.split(str(s.port), 1)[1]
    for i in range(60):
        _get(s.port, path, {"Range": f"bytes={i}-{i + 5}"})
    assert until(lambda: (s.requests_for(token) or [{}])[-1].get("range") == "bytes=59-64", 3)  # Handler protokolliert nach der Antwort
    recs = s.requests_for(token)
    assert len(recs) == 20
    assert recs[-1]["range"] == "bytes=59-64" and recs[0]["range"] == "bytes=40-45"  # juengste bleiben, Reihenfolge stimmt


def test_f16_viele_nacheinander_registrierte_und_entfernte_tokens(media, monkeypatch):
    s, f = media
    monkeypatch.setattr(M, "MAX_LOG_TOKENS", 10)
    for _ in range(60):
        url, token = s.register(f, "application/octet-stream")
        _get(s.port, url.split(str(s.port), 1)[1])
        assert until(lambda: s.requests_for(token), 3), "Auswertung muss vor dem Entfernen moeglich sein"
        s.unregister(token)
    assert len(s._log) <= 10
    assert len(s.request_log) <= 10 * M.MAX_LOG_PER_TOKEN


def test_f16_erfundene_tokens_belegen_keinen_unbegrenzten_speicher_und_aktive_bleiben(media, monkeypatch):
    s, f = media
    monkeypatch.setattr(M, "MAX_LOG_TOKENS", 8)
    url, token = s.register(f, "application/octet-stream")
    path = url.split(str(s.port), 1)[1]
    _get(s.port, path)
    assert until(lambda: s.requests_for(token), 3)
    for i in range(100):
        assert _get(s.port, f"/m/fake{i}/x.bin") == 404
    assert len(s._log) <= 8
    assert [r["method"] for r in s.requests_for(token)] == ["GET"], "Protokoll eines aktiven Tokens ging verloren"


def test_f16_player_wertet_head_get_und_range_weiterhin_aus(rig, tone_factory):
    mock, server, player = rig()
    a = tone_factory("a", seconds=1.0, fs=8000)
    res = player.play_queue([a])
    reqs = res[0]["requests"]
    assert reqs and {r["method"] for r in reqs} <= {"HEAD", "GET"} and any(r["method"] == "GET" for r in reqs)
    assert all(set(r) == {"client", "method", "range", "status", "bytes_sent"} for r in reqs)


# ---- Prioritaet 5: F-08, F-09, F-10 (reproduziert, dann klein behoben) ----

import json

from kefplayer import audio, radio
from kefplayer.dsp import Filter, Profile


def test_f08_stopp_waehrend_der_vorbereitung_startet_den_renderer_nicht(rig, tone_factory, monkeypatch):
    mock, server, player = rig(profile=Profile(filters=[Filter("peaking", 1000, -3, 1)]))
    a = tone_factory("a", seconds=0.5, fs=8000)
    real = audio.process_file
    started = threading.Event()

    def slow(*args, **kw):
        started.set()
        time.sleep(1.0)  # lange Datei: DSP-Vorbereitung dauert
        return real(*args, **kw)

    monkeypatch.setattr(audio, "process_file", slow)
    out = {}
    th = threading.Thread(target=lambda: out.update(r=player.play_queue([a])), daemon=True)
    th.start()
    assert started.wait(3)
    player.stop()
    th.join(10)
    assert not th.is_alive()
    names = [x[0] for x in mock.actions]
    assert "SetAVTransportURI" not in names and "Play" not in names, f"Renderer wurde nach Stopp noch gestartet: {names}"
    assert out["r"] == [] and server.entries == {}


def _radio_args(tmp_path, mock, url, extra=()):
    return ["radio", url, "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl"), *extra]


def test_f09_abbruch_im_qualitaetscheck_ohne_traceback_und_ohne_senden(tmp_path, monkeypatch, capsys):
    from kefplayer import cli
    from mock_renderer import MockRenderer

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(radio, "quality_gate", interrupted)
    with MockRenderer() as mock:
        rc = cli.main(_radio_args(tmp_path, mock, "http://127.0.0.1:9/s"))
        assert [a[0] for a in mock.actions] == []  # nichts an den Renderer gesendet
    out = capsys.readouterr()
    assert rc == 130 and "Abgebrochen" in out.out and "Traceback" not in out.err


@pytest.mark.parametrize("extra", [
    {"expected_codec": 5}, {"expected_codec": ["aac"]}, {"min_bitrate_kbps": "abc"}, {"min_bitrate_kbps": True},
    {"min_bitrate_kbps": -5}, {"min_bitrate_kbps": 0}, {"min_bitrate_kbps": [1]}, {"min_bitrate_kbps": 1e9},
    {"genre": 5}, {"notes": {"a": 1}}, {"status": 1}, {"verified_at": 20260101},
])
def test_f10_falsche_typen_in_favoriten_werden_abgelehnt(tmp_path, extra):
    p = tmp_path / "f.json"
    p.write_text(json.dumps([{"name": "x", "url": "http://127.0.0.1:9/s", **extra}]))
    with pytest.raises(radio.RadioError) as e:
        radio.load_favorites(p)
    assert e.value.category == "favorites_invalid"


@pytest.mark.parametrize("lit", ["NaN", "Infinity", "-Infinity"])
def test_f10_nan_und_inf_als_mindestbitrate_werden_abgelehnt(tmp_path, lit):
    p = tmp_path / "f.json"
    p.write_text('[{"name":"x","url":"http://127.0.0.1:9/s","min_bitrate_kbps": %s}]' % lit)
    with pytest.raises(radio.RadioError):
        radio.load_favorites(p)


def test_f10_gueltige_felder_und_null_bleiben_erlaubt(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps([{"name": "x", "url": "http://127.0.0.1:9/s", "genre": "Jazz", "expected_codec": "aac",
                              "min_bitrate_kbps": 192, "status": None, "verified_at": None, "notes": "ok"}]))
    fav = radio.load_favorites(p)[0]
    assert fav["min_bitrate_kbps"] == 192 and fav["expected_codec"] == "aac" and fav["status"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "abc", True, [1], None, -5])
def test_f10_effective_floor_nur_endliche_zahlen_heben_an(bad):
    assert Q.effective_floor(bad) == 128.0
    assert Q.effective_floor(192) == 192.0 and Q.effective_floor(64) == 128.0


def test_f10_quality_gate_mit_nicht_text_codec_wirft_keinen_attributeerror():
    from radio_helpers import FakeStation
    with FakeStation(body=adts_stream(n_frames=400), content_type="audio/aac") as st:
        info, rep, verdict = radio.quality_gate(st.url, favorite={"name": "x", "expected_codec": 5}, warmup_s=0.1, window_s=0.3)
    assert verdict is not None
