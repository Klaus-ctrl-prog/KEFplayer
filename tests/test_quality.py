"""E1.1 Qualitaetspruefung (E-6): Erkennung, Messung, Bewertung, ffprobe (Fake), radio-check. Kein Netzwerk."""
import json
import sys

import pytest

from kefplayer import quality as Q, radio, cli
from radio_helpers import FakeClock, FakeStation, RateSource, adts_frame, adts_stream


# ------------------------------------------------------------------ Erkennung

def test_adts_detection_lc_and_he_unknown_flag():
    rep = Q.detect_codec(adts_stream(sr_idx=4, channels=2))
    assert rep.codec == "aac" and rep.profile == "LC" and rep.sample_rate == 44100 and rep.channels == 2
    assert rep.he_unknown is True   # SBR im ADTS-Kopf nicht sichtbar


def test_adts_needs_two_consistent_headers():
    assert Q.detect_codec(adts_frame()).codec == "unknown"          # nur ein Frame, keine Bestaetigung
    assert Q.detect_codec(b"\xff\xf1" + bytes(50)).codec == "unknown"


def test_detect_other_formats():
    assert Q.detect_codec(b"fLaC" + bytes(40)).codec == "flac"
    assert Q.detect_codec(b"OggS" + bytes(40)).codec == "ogg"
    assert Q.detect_codec(b"#EXTM3U\n#EXT-X-VERSION:3").codec == "hls"
    assert Q.detect_codec(b"\xff\xfb\x90\x00" + bytes(40)).codec == "mp3"
    id3 = b"ID3\x03\x00\x00\x00\x00\x00\x04" + bytes(4)
    assert Q.detect_codec(id3 + b"\xff\xfb\x90\x00" + bytes(40)).codec == "mp3"
    assert Q.detect_codec(b"<html>nope</html>").codec == "unknown"
    assert Q.detect_codec(b"").codec == "unknown"


# ------------------------------------------------------------------ Messung

def _measure(kbps, **kw):
    clock = FakeClock()
    src = RateSource(clock, kbps, head=adts_stream(n_frames=20), **kw)
    rep = Q.native_probe(lambda: src, clock=clock, warmup_s=3.0, window_s=10.0)
    return rep, src


@pytest.mark.parametrize("kbps", [96, 128, 192, 256])
def test_measured_bitrate_close_to_real(kbps):
    rep, src = _measure(kbps)
    assert rep.codec == "aac" and src.closed
    assert rep.measured_kbps == pytest.approx(kbps, rel=0.02)


def test_burst_at_connect_is_not_counted():
    rep, _ = _measure(128, burst=500_000)   # grosser Anfangs-Burst darf die Messung nicht verfaelschen
    assert rep.measured_kbps == pytest.approx(128, rel=0.02)


def test_short_stream_gives_no_bitrate_and_note():
    clock = FakeClock()
    src = RateSource(clock, 192, head=adts_stream(n_frames=20), total_s=2.0)
    rep = Q.native_probe(lambda: src, clock=clock, warmup_s=3.0, window_s=10.0)
    assert rep.measured_kbps is None and rep.notes


def test_empty_stream_is_unknown_without_bitrate():
    clock = FakeClock()

    class Empty:
        def read(self, n=None):
            return b""

        def close(self):
            pass
    rep = Q.native_probe(lambda: Empty(), clock=clock)
    assert rep.codec == "unknown" and rep.measured_kbps is None


# ------------------------------------------------------------------ Bewertung (E-6)

def R(**kw):
    base = dict(codec="aac", profile="LC", declared_kbps=None, measured_kbps=None)
    base.update(kw)
    return Q.StreamReport(**base)


def test_flac_always_full():
    assert Q.evaluate(Q.StreamReport(codec="flac")).level == "full"


@pytest.mark.parametrize("codec", ["mp3", "ogg", "unknown", "hls"])
def test_other_codecs_rejected(codec):
    v = Q.evaluate(Q.StreamReport(codec=codec, declared_kbps=320, measured_kbps=320))
    assert v.level == "rejected" and v.category == "stream_format_unsupported" and not v.ok


@pytest.mark.parametrize("declared,level", [(256, "full"), (192, "full"), (191, "reduced"), (160, "reduced"),
                                            (128, "reduced"), (127, "rejected"), (96, "rejected"), (64, "rejected")])
def test_thresholds_with_declared_bitrate_exact(declared, level):
    assert Q.evaluate(R(declared_kbps=declared)).level == level


def test_too_low_category():
    v = Q.evaluate(R(declared_kbps=96))
    assert v.category == "stream_quality_too_low" and not v.ok


def test_measured_tolerance_applies_only_to_measurement():
    assert Q.evaluate(R(measured_kbps=192 * 0.96)).level == "full"       # innerhalb Messtoleranz
    assert Q.evaluate(R(measured_kbps=128 * 0.96)).level == "reduced"
    assert Q.evaluate(R(measured_kbps=128 * 0.90)).level == "rejected"
    assert Q.evaluate(R(declared_kbps=191.9)).level == "reduced"           # deklariert: exakt


def test_smaller_value_wins_when_declared_and_measured_differ():
    v = Q.evaluate(R(declared_kbps=192, measured_kbps=100))
    assert v.level == "rejected" and v.effective_kbps == 100
    v = Q.evaluate(R(declared_kbps=100, measured_kbps=200))
    assert v.level == "rejected" and v.effective_kbps == 100


def test_unknown_bitrate_never_good():
    v = Q.evaluate(R())
    assert v.level == "rejected" and v.category == "stream_quality_unverified"


def test_he_aac_allowed_but_flagged_by_default():
    v = Q.evaluate(R(profile="HE-AAC", declared_kbps=192))
    assert v.level == "full" and "he_aac" in v.flags
    v = Q.evaluate(R(he_unknown=True, measured_kbps=192))
    assert v.level == "full" and "he_aac_possible" in v.flags


def test_strict_lc_rejects_he_and_unprovable():
    assert Q.evaluate(R(profile="HE-AAC", declared_kbps=192), strict_lc=True).level == "rejected"
    v = Q.evaluate(R(he_unknown=True, measured_kbps=192), strict_lc=True)
    assert v.level == "rejected" and v.category == "stream_quality_unverified"
    assert Q.evaluate(R(profile="LC", declared_kbps=192), strict_lc=True).level == "full"


def test_favorite_floor_can_only_be_raised():
    assert Q.effective_floor(None) == 128
    assert Q.effective_floor(96) == 128          # Senken ist nicht moeglich
    assert Q.effective_floor("x") == 128
    assert Q.effective_floor(192) == 192
    assert Q.evaluate(R(declared_kbps=160), floor_kbps=Q.effective_floor(192)).level == "rejected"
    assert Q.evaluate(R(declared_kbps=160), floor_kbps=Q.effective_floor(96)).level == "reduced"


# ------------------------------------------------------------------ ffprobe (Fake-Skript)

def _fake_ffprobe(tmp_path, payload, rc=0):
    script = tmp_path / "fake_ffprobe.py"
    script.write_text("import sys, json\nprint(" + repr(json.dumps(payload)) + ")\nsys.exit(" + str(rc) + ")\n",
                      encoding="utf-8")
    return [sys.executable, str(script)]


def test_ffprobe_parses_profile_and_bitrate(tmp_path):
    cmd = _fake_ffprobe(tmp_path, {"streams": [{"codec_type": "audio", "codec_name": "aac", "profile": "HE-AAC",
                                                "sample_rate": "48000", "channels": 2, "bit_rate": "192000"}],
                                   "format": {}})
    rep = Q.ffprobe_probe("http://x.de/s", cmd)
    assert (rep.codec, rep.profile, rep.declared_kbps, rep.sample_rate) == ("aac", "HE-AAC", 192.0, 48000)
    assert Q.evaluate(rep).flags == ["he_aac"]


def test_ffprobe_bitrate_from_format_and_missing(tmp_path):
    cmd = _fake_ffprobe(tmp_path, {"streams": [{"codec_type": "audio", "codec_name": "aac", "profile": "LC"}],
                                   "format": {"bit_rate": "128000"}})
    assert Q.ffprobe_probe("http://x.de/s", cmd).declared_kbps == 128.0
    cmd = _fake_ffprobe(tmp_path, {"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}})
    assert Q.ffprobe_probe("http://x.de/s", cmd).declared_kbps is None


def test_ffprobe_failures_do_not_raise_and_do_not_leak_url(tmp_path):
    for cmd in (_fake_ffprobe(tmp_path, {}, rc=1), ["/gibt/es/nicht"], _fake_ffprobe(tmp_path, {"streams": []})):
        rep = Q.ffprobe_probe("http://x.de/GEHEIM?token=1", cmd)
        assert rep.codec == "unknown" and rep.notes and "GEHEIM" not in " ".join(rep.notes)


def test_ffprobe_called_with_whitelist_and_no_shell(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw

        class P:
            returncode, stdout, stderr = 1, "", ""
        return P()
    monkeypatch.setattr(Q.subprocess, "run", fake_run)
    Q.ffprobe_probe("http://x.de/s", ["ffprobe"])
    assert seen["cmd"][0] == "ffprobe" and "-protocol_whitelist" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"
    assert seen["kw"].get("shell") in (None, False) and isinstance(seen["cmd"], list)


def test_merge_keeps_measurement_and_adds_profile():
    nat = Q.StreamReport(codec="aac", measured_kbps=190, he_unknown=True)
    ff = Q.StreamReport(codec="aac", profile="LC", declared_kbps=192, source="ffprobe")
    m = Q.merge(nat, ff)
    assert m.profile == "LC" and m.declared_kbps == 192 and m.measured_kbps == 190 and m.he_unknown is False
    assert Q.merge(nat, None) is nat
    assert Q.merge(nat, Q.StreamReport(codec="unknown")) is nat


# ------------------------------------------------------------------ Gate und CLI mit lokalem Fake-Sender

def _gate(st, **kw):
    return radio.quality_gate(st.url, warmup_s=0.05, window_s=0.3, **kw)


def test_gate_rejects_mp3_station_by_content_type():
    with FakeStation(body=b"x", content_type="audio/mpeg") as st:
        with pytest.raises(radio.RadioError) as e:
            _gate(st)
    assert e.value.category == "stream_format_unsupported"


def test_gate_rejects_mp3_disguised_as_aac():
    with FakeStation(body=b"\xff\xfb\x90\x00" + bytes(5000), content_type="audio/aac") as st:
        _, rep, v = _gate(st)
    assert rep.codec == "mp3" and v.level == "rejected"


def test_gate_flac_station_ok():
    with FakeStation(body=b"fLaC" + bytes(5000), content_type="audio/flac") as st:
        _, rep, v = _gate(st)
    assert rep.codec == "flac" and v.level == "full"


def test_gate_aac_without_measurable_bitrate_is_rejected():
    """Kurzer Endlos-Test: Das Fenster ist kurz, ohne Messwert gilt AAC nie als gut."""
    with FakeStation(body=adts_stream(n_frames=30), content_type="audio/aac") as st:
        _, rep, v = _gate(st)
    assert rep.codec == "aac" and v.level == "rejected" and v.category == "stream_quality_unverified"


def test_gate_expected_codec_mismatch_rejected():
    with FakeStation(body=b"fLaC" + bytes(5000), content_type="audio/flac") as st:
        _, _, v = _gate(st, favorite={"expected_codec": "aac"})
    assert v.level == "rejected" and "Erwarteter Codec" in v.reasons[0]


def test_radio_check_cli(tmp_path, capsys):
    with FakeStation(body=b"fLaC" + bytes(5000), content_type="audio/flac") as ok, \
         FakeStation(body=b"x", content_type="audio/mpeg") as mp3:
        fav = tmp_path / "f.json"
        fav.write_text(json.dumps([
            {"name": "Gut", "url": ok.url},
            {"name": "MP3", "url": mp3.url},
            {"name": "Leer", "url": "https://example.invalid/x"},
            {"name": "Weg", "url": "http://127.0.0.1:9/zzz"},
        ]), encoding="utf-8")
        rc = cli.main(["radio-check", str(fav), "--quality-warmup", "0.05", "--quality-window", "0.3", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    rows = json.loads(out[out.index("["):out.rindex("]") + 1])
    by = {r["name"]: r for r in rows}
    assert by["Gut"]["level"] == "full" and by["MP3"]["level"] == "error" and by["MP3"]["category"] == "stream_format_unsupported"
    assert by["Leer"]["level"] == "no_url" and by["Weg"]["category"] == "stream_unreachable"
    assert "GEHEIM123" not in out and "nicht an einer KEF Gen1" in out


def test_radio_check_only_and_unknown_name(tmp_path, capsys):
    fav = tmp_path / "f.json"
    fav.write_text(json.dumps([{"name": "A", "url": "https://example.invalid/x"}]), encoding="utf-8")
    assert cli.main(["radio-check", str(fav), "--only", "a"]) == 0
    assert cli.main(["radio-check", str(fav), "--only", "zzz"]) == 2
    assert cli.main(["radio-check", str(tmp_path / "nope.json")]) == 2


def test_radio_placeholder_favorite_refused_without_renderer(tmp_path, capsys):
    from mock_renderer import MockRenderer
    fav = tmp_path / "f.json"
    fav.write_text(json.dumps([{"name": "A", "url": "https://example.invalid/x"}]), encoding="utf-8")
    with MockRenderer() as mock:
        rc = cli.main(["radio", "A", "--favorites", str(fav), "--renderer", mock.location, "--log", str(tmp_path / "l")])
        assert mock.actions == []
    assert rc == 2 and "Platzhalter" in capsys.readouterr().err


def test_radio_rejected_stream_sends_nothing_to_renderer(tmp_path, capsys):
    from mock_renderer import MockRenderer
    with FakeStation(body=b"\xff\xfb\x90\x00" + bytes(5000), content_type="audio/aac") as st, MockRenderer() as mock:
        rc = cli.main(["radio", st.url, "--renderer", mock.location, "--quality-warmup", "0.05",
                       "--quality-window", "0.3", "--log", str(tmp_path / "l")])
        assert mock.actions == []
    cap = capsys.readouterr()
    assert rc == 2 and "ABGELEHNT" in cap.out and "nichts an die KEF gesendet" in cap.err
    assert "GEHEIM123" not in cap.out + cap.err


def test_template_favorites_all_placeholders_or_valid():
    from pathlib import Path
    favs = radio.load_favorites(Path(__file__).resolve().parent.parent / "docs" / "favorites.example.json")
    assert len(favs) >= 15 and any(radio.is_placeholder(f["url"]) for f in favs)
