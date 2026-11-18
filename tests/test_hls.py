"""HLS (.m3u8) fuer E1b: Variantenwahl, Messung aus den Nutzdaten, Urteil E-6, Sicherheit, CLI. Kein Internet, kein ffmpeg."""
import json
import sys
from pathlib import Path

import pytest

from kefplayer import cli, decoder, hls, quality, radio, radio_supervisor
from kefplayer.decoder import FfmpegInfo
from kefplayer.radio import RadioError
from hls_helpers import (FakeHls, MPEGURL, adts_es, master_playlist, media_playlist, mp4_init, mp4_segment,
                         standard_routes, ts_segment)
from mock_renderer import MockRenderer

FAKE = str(Path(__file__).with_name("fake_decoder.py"))
SECRET = "GEHEIM123"


def _gate(routes, path="/live/master.m3u8", **kw):
    with FakeHls(routes) as srv:
        return hls.hls_gate(srv.url(path), window_s=10.0, **kw), srv


# ------------------------------------------------------------------ Playlist-Auswertung

def test_variantenwahl_hoechste_stufe():
    txt = master_playlist([("lo.m3u8", 70000, "mp4a.40.5"), ("hi.m3u8", 210000, "mp4a.40.2"), ("mid.m3u8", 140000, "mp4a.40.2")])
    kind, vs = hls.parse_playlist(txt, "http://h.example/a/master.m3u8")
    assert kind == "master" and len(vs) == 3
    best = hls.select_variant(vs)
    assert best.url == "http://h.example/a/hi.m3u8"      # relative URI korrekt aufgeloest


def test_variantenwahl_schliesst_mp3_video_und_fremdcodecs_aus():
    txt = "\n".join(["#EXTM3U",
                     '#EXT-X-STREAM-INF:BANDWIDTH=999000,CODECS="mp4a.40.34"', "mp3.m3u8",
                     '#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="mp4a.69"', "mp3b.m3u8",
                     '#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=640x360', "video.m3u8",
                     '#EXT-X-STREAM-INF:BANDWIDTH=700000,CODECS="ec-3"', "eac3.m3u8",
                     '#EXT-X-STREAM-INF:BANDWIDTH=130000,CODECS="mp4a.40.2"', "aac.m3u8"])
    _, vs = hls.parse_playlist(txt, "http://h.example/m.m3u8")
    assert hls.select_variant(vs).url.endswith("aac.m3u8")
    with pytest.raises(RadioError) as ei:
        hls.select_variant([v for v in vs if not v.url.endswith("aac.m3u8")])
    assert ei.value.category == "stream_format_unsupported"


def test_variante_ohne_codecs_bleibt_kandidat():
    _, vs = hls.parse_playlist(master_playlist([("a.m3u8", 200000, None)]), "http://h.example/m.m3u8")
    assert hls.select_variant(vs).url.endswith("a.m3u8")


def test_keine_playlist_wird_abgelehnt():
    with pytest.raises(RadioError):
        hls.parse_playlist("<html>nein</html>", "http://h.example/x.m3u8")


@pytest.mark.parametrize("ref", ["file:///etc/passwd", "ftp://h.example/a.ts", "javascript:alert(1)",
                                 "http://user:pw@h.example/a.ts", "http://h.example:99999/a.ts"])
def test_playlist_urls_nur_http_ohne_zugangsdaten(ref):
    txt = media_playlist([ref])
    with pytest.raises(RadioError) as ei:
        hls.parse_playlist(txt, "http://h.example/m.m3u8")
    assert ei.value.category == "stream_url_invalid"


def test_https_playlist_darf_nicht_auf_http_verweisen():
    with pytest.raises(RadioError) as ei:
        hls.parse_playlist(media_playlist(["http://h.example/a.ts"]), "https://h.example/m.m3u8")
    assert "unverschluesselte" in str(ei.value)
    hls.parse_playlist(media_playlist(["https://h.example/a.ts"]), "http://h.example/m.m3u8")   # http -> https ist ok


def test_segment_analyse_erkennt_container():
    es = adts_es(192, 6)
    ts = hls.analyze_segment(ts_segment(es))
    assert (ts.codec, ts.profile, ts.sample_rate) == ("aac", "LC", 44100) and ts.audio_s == pytest.approx(6.0, abs=0.03)
    assert hls.analyze_segment(es).codec == "aac"                                  # nacktes ADTS
    id3 = b"ID3\x03\x00\x00\x00\x00\x00\x05" + bytes(5)
    assert hls.analyze_segment(id3 + es).codec == "aac"                            # gepacktes Audio mit ID3
    assert hls.analyze_segment(ts_segment(es, 0x03)).codec == "mp3"
    assert hls.analyze_segment(ts_segment(es, 0x81)).codec == "other"
    assert hls.analyze_segment(mp4_segment(192, 6), mp4_init()).codec == "aac"
    assert hls.analyze_segment(mp4_segment(192, 6), mp4_init(oti=0x6B)).codec == "mp3"
    assert hls.analyze_segment(mp4_segment(192, 6), mp4_init("flac")).codec == "flac"
    assert hls.analyze_segment(mp4_segment(192, 6)).codec == "unknown"              # ohne Init: nie als gut werten
    assert hls.analyze_segment(b"\x00" * 5000).codec == "unknown"
    assert hls.analyze_segment(b"").codec == "unknown"


def test_fmp4_he_aac_aus_esds():
    assert hls.analyze_segment(mp4_segment(64, 6), mp4_init(aot=5)).profile == "HE-AAC"


# ------------------------------------------------------------------ Urteil nach E-6

@pytest.mark.parametrize("container", ["ts", "fmp4"])
def test_192_wird_voll_und_hoechste_variante_gewaehlt(container):
    routes = standard_routes({"lo": 64, "mid": 128, "hi": 192}, container=container)
    (info, rep, verdict), srv = _gate(routes)
    assert verdict.level == "full" and verdict.ok
    assert info.url.split("?")[0].endswith("/hi.m3u8") and info.hls_master.endswith("master.m3u8?token=" + SECRET)
    assert info.mime == hls.HLS_MIME and info.ext == ".m3u8"
    assert rep.codec == "aac" and rep.measured_kbps == pytest.approx(192, rel=0.03)
    fetched = [p.split("?")[0] for p in srv.requests]
    assert not any(p.endswith(("lo_0.ts", "mid_0.ts", "lo_0.m4s", "mid_0.m4s")) for p in fetched)   # nur die gewaehlte Variante gemessen


def test_128_ist_reduziert():
    (_, rep, verdict), _ = _gate(standard_routes({"only": 128}))
    assert verdict.level == "reduced" and verdict.ok


def test_deklarierte_bandbreite_luegt_gemessene_bitrate_entscheidet():
    # Master behauptet 220 kbit/s, die Segmente liefern tatsaechlich 96 kbit/s
    routes = standard_routes({"hi": 96}, labels={"hi": 220000})
    (_, rep, verdict), _ = _gate(routes)
    assert not verdict.ok and verdict.category == "stream_quality_too_low"
    assert rep.measured_kbps == pytest.approx(96, rel=0.03)


def test_favorit_kann_untergrenze_auf_192_anheben():
    routes = standard_routes({"hi": 160})
    (_, _, v_default), _ = _gate(routes)
    assert v_default.level == "reduced"
    (_, _, v_strict), _ = _gate(routes, favorite={"min_bitrate_kbps": 192})
    assert v_strict.category == "stream_quality_too_low"


def test_mp3_segmente_trotz_aac_codecs_angabe_abgelehnt():
    routes = standard_routes({"hi": 192})
    es = adts_es(192, 6)
    for n in range(3):
        routes[f"/live/hi_{n}.ts"] = ("video/mp2t", ts_segment(es, 0x03))
    (_, rep, verdict), _ = _gate(routes)
    assert not verdict.ok and rep.codec == "unknown" and any("widerspricht" in n for n in rep.notes)


def test_codecs_angabe_allein_genuegt_nie():
    routes = standard_routes({"hi": 192})
    for n in range(3):
        routes[f"/live/hi_{n}.ts"] = ("video/mp2t", b"\x00" * 4000)      # Muell statt Audio
    (_, rep, verdict), _ = _gate(routes)
    assert not verdict.ok and rep.codec == "unknown"


def test_fmp4_ohne_init_ist_unverifiziert():
    routes = standard_routes({"hi": 192}, container="fmp4")
    routes["/live/hi.m3u8"] = (MPEGURL, media_playlist([f"hi_{i}.m4s" for i in range(3)], 6.0).encode())   # EXT-X-MAP entfernt
    (_, _, verdict), _ = _gate(routes)
    assert not verdict.ok


def test_flac_in_fmp4_ist_voll():
    routes = standard_routes({"hi": 800}, container="fmp4", codecs="flac")
    routes["/live/hi_init.mp4"] = ("audio/mp4", mp4_init("flac"))
    (_, rep, verdict), _ = _gate(routes)
    assert rep.codec == "flac" and verdict.level == "full"


def test_he_aac_laut_codecs_wird_gekennzeichnet_und_bei_strict_lc_abgelehnt():
    routes = standard_routes({"hi": 192}, codecs="mp4a.40.5")
    (_, rep, verdict), _ = _gate(routes)
    assert rep.profile == "HE-AAC" and "he_aac" in verdict.flags and verdict.ok
    (_, _, v2), _ = _gate(routes, strict_lc=True)
    assert not v2.ok


def test_verschluesselt_wird_nicht_geprueft_und_abgelehnt():
    routes = standard_routes({"hi": 192})
    routes["/live/hi.m3u8"] = (MPEGURL, media_playlist([f"hi_{i}.ts" for i in range(3)], 6.0,
                                                       extra='#EXT-X-KEY:METHOD=AES-128,URI="key.bin"').encode())
    (_, _, verdict), srv = _gate(routes)
    assert not verdict.ok and verdict.category == "stream_quality_unverified"
    assert not any(p.split("?")[0].endswith((".ts", "key.bin")) for p in srv.requests)   # nichts geladen


def test_media_playlist_direkt_ohne_master():
    routes = standard_routes({"hi": 192})
    (info, _, verdict), _ = _gate(routes, path="/live/hi.m3u8")
    assert verdict.level == "full" and info.url.split("?")[0].endswith("hi.m3u8")


def test_erwarteter_codec_wird_geprueft():
    (_, _, verdict), _ = _gate(standard_routes({"hi": 192}), favorite={"expected_codec": "flac"})
    assert not verdict.ok and verdict.category == "stream_format_unsupported"


def test_ffprobe_deklariert_weniger_kleinerer_wert_zaehlt(monkeypatch):
    def fake_probe(url, cmd, **kw):
        return quality.StreamReport(codec="aac", profile="LC", declared_kbps=96.0, source="ffprobe")
    monkeypatch.setattr(quality, "ffprobe_probe", fake_probe)
    (_, rep, verdict), _ = _gate(standard_routes({"hi": 192}), ffprobe="C:/x/ffprobe")
    assert verdict.category == "stream_quality_too_low" and rep.source == "ffprobe+hls"


# ------------------------------------------------------------------ Fehler, Grenzen, Maskierung

def test_zu_grosse_playlist_wird_begrenzt():
    big = "#EXTM3U\n" + "#" * (hls.MAX_PLAYLIST_BYTES + 10)
    with FakeHls({"/m.m3u8": (MPEGURL, big.encode())}) as srv:
        with pytest.raises(RadioError) as ei:
            hls.hls_gate(srv.url("/m.m3u8"))
    assert ei.value.category == "stream_format_unsupported" and SECRET not in str(ei.value)


def test_zu_grosses_segment_abgelehnt(monkeypatch):
    monkeypatch.setattr(hls, "MAX_SEGMENT_BYTES", 1000)
    with pytest.raises(RadioError) as ei:
        _gate(standard_routes({"hi": 192}))
    assert "zu gross" in str(ei.value) and SECRET not in str(ei.value)


def test_segmentzahl_ist_begrenzt():
    routes = standard_routes({"hi": 192}, seconds=0.5, nseg=40)
    (_, _, _), srv = _gate(routes)
    n_seg = sum(1 for p in srv.requests if p.split("?")[0].endswith(".ts"))
    assert n_seg <= hls.MAX_SEGMENTS


def test_fehlende_segmente_http_404_ohne_url_in_meldung():
    routes = standard_routes({"hi": 192})
    del routes["/live/hi_2.ts"]
    with pytest.raises(RadioError) as ei:
        _gate(routes)
    assert ei.value.category == "stream_http_error" and SECRET not in str(ei.value)


def test_nicht_erreichbar_maskiert():
    with pytest.raises(RadioError) as ei:
        hls.hls_gate(f"http://127.0.0.1:9/live/master.m3u8?token={SECRET}")
    assert SECRET not in str(ei.value) and "master.m3u8" not in str(ei.value)


def test_verschachtelte_master_playlist_nicht_unterstuetzt():
    routes = standard_routes({"hi": 192})
    routes["/live/hi.m3u8"] = routes["/live/master.m3u8"]
    (_, rep, verdict), _ = _gate(routes)
    assert not verdict.ok


# ------------------------------------------------------------------ quality_gate-Einbindung (E1 unveraendert)

def test_ohne_hls_ok_bleibt_hls_abgelehnt_e1():
    with FakeHls(standard_routes({"hi": 192})) as srv:
        with pytest.raises(RadioError) as ei:
            radio.quality_gate(srv.url(), warmup_s=0.1, window_s=0.2)
        assert ei.value.category == "stream_format_unsupported"
        assert not any(p.split("?")[0].endswith(".ts") for p in srv.requests)


def test_mit_hls_ok_endung_und_inhaltstyp_erkannt():
    routes = standard_routes({"hi": 192})
    routes["/live/stream"] = routes["/live/master.m3u8"]          # ohne .m3u8, nur am Inhaltstyp erkennbar
    with FakeHls(routes) as srv:
        for path in ("/live/master.m3u8", "/live/stream"):
            info, rep, verdict = radio.quality_gate(srv.url(path), hls_ok=True, window_s=10.0)
            assert verdict.level == "full" and info.url.split("?")[0].endswith("hi.m3u8")


def test_mit_hls_ok_bleiben_normale_streams_normale_streams():
    from radio_helpers import FakeStation, adts_stream
    with FakeStation(body=adts_stream(n_frames=40), content_type="audio/aac") as st:
        info, rep, verdict = radio.quality_gate(st.url, hls_ok=True, warmup_s=0.0, window_s=0.2)
    assert info.hls_master is None and rep.source == "native"


# ------------------------------------------------------------------ CLI / ffmpeg-Stufe

def _fake_ffmpeg(monkeypatch, tmp_path, frames=9600):
    exe = tmp_path / "ffmpeg-fake"
    exe.write_bytes(b"x")
    monkeypatch.setattr(decoder, "check_ffmpeg", lambda path, **kw: FfmpegInfo(
        path=path, version="ffmpeg version fake-1.0", sha256="ab" * 32,
        argv_prefix=(sys.executable, FAKE, "--mode", "sine", "--frames", str(frames))))
    urls = []
    real = decoder.Decoder

    class Rec(real):
        def __init__(self, info, url, **kw):
            urls.append((url, decoder.build_args(url)))
            super().__init__(info, url, **kw)

    monkeypatch.setattr(decoder, "Decoder", Rec)
    return str(exe), urls


def _args(tmp_path, mock, target, extra=()):
    return ["radio", target, "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl"), "--quality-window", "10", *extra]


def test_cli_ffmpeg_bekommt_gepruefte_variante_nicht_master(tmp_path, monkeypatch, capsys):
    exe, urls = _fake_ffmpeg(monkeypatch, tmp_path)
    with FakeHls(standard_routes({"lo": 64, "hi": 192})) as srv, MockRenderer(speedup=5.0) as mock:
        rc = cli.main(_args(tmp_path, mock, srv.url(), ["--ffmpeg", exe, "--no-reconnect"]))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert len(urls) == 1 and urls[0][0].split("?")[0].endswith("/hi.m3u8")
    assert "volle Qualitaet" in out and "HLS:" in out
    assert SECRET not in out + (tmp_path / "r.jsonl").read_text(encoding="utf-8")
    assert "-protocol_whitelist" in urls[0][1] and "http,https,tcp,tls,crypto" in urls[0][1]
    assert mock.fetched and mock.fetched[0]["data"][:4] == b"RIFF"


def test_cli_hls_zu_schlecht_nichts_an_die_kef(tmp_path, monkeypatch, capsys):
    exe, urls = _fake_ffmpeg(monkeypatch, tmp_path)
    with FakeHls(standard_routes({"hi": 96}, labels={"hi": 250000})) as srv, MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, srv.url(), ["--ffmpeg", exe]))
    cap = capsys.readouterr()
    assert rc == 2 and "stream_quality_too_low" in cap.err
    assert urls == [] and mock.fetched == []
    assert SECRET not in cap.out + cap.err


def test_cli_hls_ohne_ffmpeg_abgelehnt(tmp_path, capsys):
    with FakeHls(standard_routes({"hi": 192})) as srv, MockRenderer() as mock:
        rc = cli.main(_args(tmp_path, mock, srv.url(), []))
    assert rc == 2 and "stream_format_unsupported" in capsys.readouterr().err
    assert mock.fetched == []


def test_cli_reconnect_prueft_hls_erneut_und_nutzt_gepruefte_variante(tmp_path, monkeypatch, capsys):
    exe, urls = _fake_ffmpeg(monkeypatch, tmp_path, frames=96000)
    real = radio_supervisor.ReconnectBudget
    monkeypatch.setattr(radio_supervisor, "ReconnectBudget", lambda: real(backoff=(0.05,), max_total=1, stable_s=999))
    with FakeHls(standard_routes({"lo": 64, "hi": 192})) as srv, MockRenderer(speedup=5.0) as mock:
        rc = cli.main(_args(tmp_path, mock, srv.url(), ["--ffmpeg", exe]))
        reqs = [p.split("?")[0] for p in srv.requests]
    assert rc == 1 and "stream_reconnect_exhausted" in capsys.readouterr().out
    assert len(urls) == 2 and all(u.split("?")[0].endswith("/hi.m3u8") for u, _ in urls)
    assert reqs.count("/live/master.m3u8") >= 2                  # vor dem Reconnect erneut geprueft


def test_radio_check_hls_flag(tmp_path, capsys):
    with FakeHls(standard_routes({"hi": 192})) as srv:
        fav = tmp_path / "f.json"
        fav.write_text(json.dumps([{"name": "Test", "url": srv.url()}]), encoding="utf-8")
        assert cli.main(["radio-check", str(fav), "--hls", "--json"]) == 0
        out = capsys.readouterr().out
        assert '"level": "full"' in out
        cli.main(["radio-check", str(fav), "--json", "--quality-warmup", "0.1", "--quality-window", "0.2"])
        out2 = capsys.readouterr().out
    assert "stream_format_unsupported" in out2


def test_hls_beruehrt_renderer_lock_nicht():
    import inspect
    src = inspect.getsource(hls)
    assert "RENDERER_LOCK" not in src and "upnp" not in src
