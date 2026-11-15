"""Sammler und Darstellung der Status-API (ohne Sockets)."""

import json
import shutil
import time

import pytest

from kefplayer import api
from api_helpers import (FRIENDLY, PRIVATE_DIR, RENDERER_IP, SERVED, TOKEN, UDN, FakeServer, ev, fake_renderer, feed,
                         one_track_run)


def body(collector, path, query=""):
    status, raw = api.respond(collector, path, query)
    assert status == 200, raw
    assert raw.decode("ascii")  # nur ASCII
    return json.loads(raw)


def attached(details=False):
    from kefplayer.dsp import Profile

    c = api.StatusCollector(details=details)
    c.attach(player=None, renderer=fake_renderer(), server=FakeServer(), profile=Profile())
    return c


# ---- leerer Zustand: nichts erfinden ----------------------------------------------------------------

def test_empty_collector_reports_unknown_instead_of_inventing():
    c = api.StatusCollector()
    for path in ["/api/v1/status", "/api/v1/renderer", "/api/v1/dsp", "/api/v1/audio", "/api/v1/logs", "/api/v1/diagnostics"]:
        d = body(c, path)
        assert d["read_only"] is True and d["api_version"] == 1 and d["endpoint"] == path.rsplit("/", 1)[1]
    st = body(c, "/api/v1/status")
    assert st["data"]["phase"] == "no_queue" and st["data"]["track"] is None and st["data"]["queue"] is None
    assert st["data"]["last_event"] is None and "track" in st["unknown"]
    assert body(c, "/api/v1/audio")["data"]["current"] is None
    rn = body(c, "/api/v1/renderer")
    assert rn["data"]["identity"] is None and rn["data"]["connection"]["reachable"] is None
    assert "connection.reachable" in rn["unknown"] and "identity" in rn["unknown"]


def test_nothing_is_read_from_old_logs(tmp_path, monkeypatch):
    # Alte Protokolldatei im Arbeitsordner darf nie in die API gelangen.
    (tmp_path / "kefplayer_log.jsonl").write_text(json.dumps({"event": "track_end", "path": "alt.flac", "end_reason": "ended"}) + "\n")
    monkeypatch.chdir(tmp_path)
    c = api.StatusCollector()
    assert body(c, "/api/v1/logs")["data"]["events"] == []
    assert "alt.flac" not in api.respond(c, "/api/v1/diagnostics")[1].decode()


# ---- Phasen aus Ereignissen ---------------------------------------------------------------------------

def test_phase_follows_events_and_position_stays_unknown_without_live():
    c = attached()
    path = PRIVATE_DIR + "\\02 Song.flac"
    feed(c, ev("queue_start", tracks=[path], profile={"name": "neutral", "preamp_db": "auto", "filters": []},
               profile_neutral=True, renderer=FRIENDLY, poll_s=0.5))
    assert body(c, "/api/v1/status")["data"]["phase"] == "preparing"
    feed(c, ev("track_start", index=0, path=path, served=path, processed=False, mime="audio/flac", mime_in_sink=True,
               served_format=dict(SERVED)))
    st = body(c, "/api/v1/status")
    t = st["data"]["track"]
    assert st["data"]["phase"] == "track_active"
    assert t["name"] == "02 Song.flac" and t["title"] == {"value": "02 Song", "source": "filename"}
    assert t["duration_s"] == 10.0 and t["number"] == 1
    assert t["position_s"] is None and t["transport_state"] is None
    assert {"track.position_s", "track.transport_state", "track.paused"} <= set(st["unknown"])
    assert st["data"]["live"]["enabled"] is False
    feed(c, ev("track_end", index=0, path=path, end_reason="ended", startup_delay_s=0.2, wall_s=10.5,
               last_position_s=10.0, ended_early=False, gap_before_s=None, requests=[]))
    st = body(c, "/api/v1/status")["data"]
    assert st["phase"] == "between_tracks" and st["track"] is None
    assert st["last_finished_track"]["end_reason"] == "ended"
    feed(c, ev("queue_end", played=1, reasons=["ended"]))
    st = body(c, "/api/v1/status")["data"]
    assert st["phase"] == "queue_ended" and st["queue"]["tracks_finished"] == 1 and st["queue"]["ended_at"]


def test_new_queue_resets_per_queue_state():
    c = attached()
    one_track_run(c)
    assert body(c, "/api/v1/status")["data"]["queue"]["tracks_finished"] == 1
    feed(c, ev("queue_start", tracks=["a.flac", "b.flac"], profile={"name": "x"}, profile_neutral=False, renderer="r", poll_s=0.5))
    st = body(c, "/api/v1/status")["data"]
    assert st["phase"] == "preparing" and st["queue"]["tracks_finished"] == 0 and st["queue"]["n_tracks"] == 2
    assert body(c, "/api/v1/diagnostics")["data"]["api"]["queues_seen"] == 2


# ---- Datenschutz ------------------------------------------------------------------------------------------

def _private_run():
    c = attached()
    path = one_track_run(c, end_reason="error", error=(
        f"[WinError 32] Zugriff auf '{PRIVATE_DIR}\\01 Test.flac' fehlgeschlagen, URL "
        f"http://{RENDERER_IP}:8765/m/{TOKEN}/01_Test.flac, Renderer {RENDERER_IP}"))
    feed(c, ev("cleanup_failed", path=PRIVATE_DIR + "\\tmp\\000_x.flac", error=f"PermissionError: '{PRIVATE_DIR}\\tmp\\000_x.flac'"))
    feed(c, ev("sink_unavailable", error=f"timeout http://{RENDERER_IP}:50001/cm/control"))
    return c, path


@pytest.mark.parametrize("path", ["/api/v1/status", "/api/v1/renderer", "/api/v1/dsp", "/api/v1/audio",
                                  "/api/v1/logs", "/api/v1/diagnostics"])
def test_default_output_contains_no_private_data(path):
    c, _ = _private_run()
    text = api.respond(c, path, "limit=300" if path.endswith("logs") else "")[1].decode()
    for secret in ("Klaus", "Users", "Musik", "Jazz", TOKEN, RENDERER_IP, UDN, FRIENDLY, "50001", "192.168"):
        assert secret not in text, (path, secret)


def test_default_still_shows_what_is_needed_for_analysis():
    c, _ = _private_run()
    d = body(c, "/api/v1/diagnostics")["data"]
    assert d["renderer"]["identity"]["model_name"] == "LS50 Wireless" and d["renderer"]["identity"]["manufacturer"] == "KEF"
    assert d["renderer"]["identity"]["host"] == "redacted" and d["tracks"][0]["name"] == "01 Test.flac"
    assert d["tracks"][0]["end_reason"] == "error" and "<pfad>" in d["tracks"][0]["error"] or "<url>" in d["tracks"][0]["error"]
    assert any(f["code"] == "track_problem" for f in d["findings"])


def test_details_mode_shows_paths_but_never_the_url_token():
    c = attached(details=True)
    one_track_run(c, end_reason="error", error=f"Fehler bei http://{RENDERER_IP}:8765/m/{TOKEN}/01_Test.flac")
    texts = [api.respond(c, p, "limit=300" if p.endswith("logs") else "")[1].decode() for p in
             ("/api/v1/status", "/api/v1/renderer", "/api/v1/audio", "/api/v1/logs", "/api/v1/diagnostics")]
    joined = "".join(texts)
    assert TOKEN not in joined
    d = json.loads(texts[1])
    assert d["data"]["identity"]["host"] == RENDERER_IP and d["data"]["identity"]["udn"] == UDN
    assert d["privacy"]["mode"] == "details"
    assert "Jazz" in joined  # Pfad sichtbar
    assert body(c, "/api/v1/diagnostics")["privacy"]["redacted"] == []


@pytest.mark.parametrize("text,expect_gone,expect_kept", [
    (r"[WinError 32] 'C:\\Users\\Klaus\\Musik\\a b.flac' gesperrt", ["Klaus", "Musik"], ["WinError 32", "gesperrt"]),
    (r"C:\Users\Klaus\x.flac nicht gefunden", ["Klaus"], ["nicht gefunden"]),
    (r"\\NAS\musik\a.flac nicht lesbar", ["NAS", "musik"], ["nicht lesbar"]),
    ("No such file '/home/klaus/Musik/a.flac'", ["klaus", "Musik"], ["No such file"]),
    ("fehler in /home/klaus/x.flac: kaputt", ["klaus"], ["kaputt"]),
    ("Verbindung zu 192.168.1.50 verloren", ["192.168.1.50"], ["Verbindung"]),
    ("GET http://192.168.1.20:8765/m/abc123/x.flac abgelehnt", ["abc123", "192.168", "8765"], ["abgelehnt"]),
    ("Illegal MIME-type audio/x-flac http-get:*:audio/flac:*", [], ["audio/x-flac", "http-get:*:audio/flac:*", "Illegal MIME-type"]),
    ("Position 1.5 s / 3 Titel, Faktor 10 / 20", [], ["1.5 s / 3 Titel", "10 / 20"]),
])
def test_scrub(text, expect_gone, expect_kept):
    out = api.scrub(text)
    for g in expect_gone:
        assert g not in out, (text, out)
    for k in expect_kept:
        assert k in out, (text, out)


def test_scrub_details_keeps_paths_but_removes_url_token():
    out = api.scrub(r"'C:\Users\Klaus\a.flac' via http://192.168.1.20:8765/m/SECRETTOKEN/a.flac", details=True)
    assert "Klaus" in out and "SECRETTOKEN" not in out and "192.168.1.20:8765" in out


def test_unknown_event_types_expose_field_names_only():
    c = attached()
    feed(c, ev("neues_ereignis", pfad=r"C:\Users\Klaus\geheim.flac", token=TOKEN))
    text = api.respond(c, "/api/v1/logs")[1].decode()
    assert "geheim" not in text and TOKEN not in text and "pfad" in text


# ---- Robustheit des Rueckrufs -----------------------------------------------------------------------------

@pytest.mark.parametrize("rec", [
    {}, {"event": None}, {"event": "track_end"}, {"event": "track_start", "index": "x", "served_format": 5},
    {"event": "track_end", "index": 0, "requests": "kaputt"}, {"event": "queue_start", "tracks": 5, "profile": 3},
    {"event": "sink", "protocol_infos": 7}, {"event": "track_processed", "warnings": 3},
])
def test_hook_never_raises_and_output_stays_valid(rec):
    c = attached()
    c.on_event(rec)
    c.on_event(None)  # kein dict
    for p in ("/api/v1/status", "/api/v1/renderer", "/api/v1/dsp", "/api/v1/audio", "/api/v1/diagnostics", "/api/v1/logs"):
        assert api.respond(c, p)[0] == 200


def test_event_without_valid_index_is_flagged_not_crashing():
    c = attached()
    feed(c, ev("track_end", end_reason="ended"), ev("track_start", index=True, path="a"), ev("track_processed", index="x"))
    d = body(c, "/api/v1/diagnostics")["data"]
    assert any(f["code"] == "malformed_events" for f in d["findings"]) and d["tracks"] == []
    assert len(body(c, "/api/v1/logs")["data"]["events"]) == 3  # bleiben im Protokoll sichtbar


def test_hook_errors_are_counted_and_reported_as_finding(monkeypatch):
    c = attached()
    monkeypatch.setattr(api.StatusCollector, "_make_entry", staticmethod(lambda *a, **k: 1 / 0))
    c.on_event(ev("queue_start", tracks=[]))
    f = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert any(x["code"] == "collector_error" for x in f)


# ---- Ringpuffer, Abfrage-Parameter -----------------------------------------------------------------------------

def test_ring_buffer_is_bounded_and_reports_drops():
    c = api.StatusCollector(buffer=5)
    feed(c, *[ev("stop_failed", error=f"e{i}") for i in range(12)])
    d = body(c, "/api/v1/logs", "limit=300")["data"]
    assert d["returned"] == 5 and d["dropped_total"] == 7 and d["oldest_seq_in_buffer"] == 8 and d["newest_seq"] == 12
    assert [e["seq"] for e in d["events"]] == [8, 9, 10, 11, 12]
    assert any(f["code"] == "event_buffer_overflow" for f in body(c, "/api/v1/diagnostics")["data"]["findings"])


def test_logs_since_limit_and_level():
    c = attached()
    one_track_run(c)
    feed(c, ev("stop_failed", error="x"))
    allv = body(c, "/api/v1/logs", "limit=300")["data"]["events"]
    assert [e["event"] for e in allv] == ["queue_start", "sink", "track_start", "track_end", "queue_end", "stop_failed"]
    assert [e["seq"] for e in allv] == [1, 2, 3, 4, 5, 6]
    assert [e["seq"] for e in body(c, "/api/v1/logs", "since=4")["data"]["events"]] == [5, 6]
    assert [e["seq"] for e in body(c, "/api/v1/logs", "limit=2")["data"]["events"]] == [5, 6]
    warn = body(c, "/api/v1/logs", "level=warn")["data"]["events"]
    assert [e["event"] for e in warn] == ["stop_failed"] and warn[0]["level"] == "warn"


@pytest.mark.parametrize("path,query", [
    ("/api/v1/status", "x=1"), ("/api/v1/logs", "limit=0"), ("/api/v1/logs", "limit=abc"), ("/api/v1/logs", "limit=301"),
    ("/api/v1/logs", "limit=-1"), ("/api/v1/logs", "since=-1"), ("/api/v1/logs", "level=debug"),
    ("/api/v1/logs", "limit=1&limit=2"), ("/api/v1/status", "limit=5"), ("/api/v1/diagnostics", "events=999"),
    ("/api/v1/status", "pretty=2"), ("/api/v1/logs", "limit=" + "9" * 40),
])
def test_invalid_query_rejected(path, query):
    with pytest.raises(api.QueryError):
        api.respond(api.StatusCollector(), path, query)


def test_unknown_path_is_404_and_pretty_is_multiline():
    c = api.StatusCollector()
    assert api.respond(c, "/api/v1/steuern")[0] == 404 and api.respond(c, "/etc/passwd")[0] == 404
    assert api.respond(c, "/api/v1/status/")[0] == 200  # abschliessender Schraegstrich
    assert b"\n" in api.respond(c, "/api/v1/status", "pretty=1")[1] and b"\n" not in api.respond(c, "/api/v1/status")[1]


# ---- JSON-Eigenschaften ---------------------------------------------------------------------------------------

def test_json_is_ascii_and_roundtrips_umlauts_and_nan_becomes_null():
    raw = api.encode_json({"x": float("nan"), "y": float("inf"), "z": 1.5, "ü": "Süße Töne ß"})
    raw.decode("ascii")
    assert json.loads(raw) == {"x": None, "y": None, "z": 1.5, "ü": "Süße Töne ß"}
    c = attached()
    one_track_run(c, name="Süße Töne – ß.flac")
    d = json.loads(api.respond(c, "/api/v1/diagnostics")[1])
    assert d["data"]["tracks"][0]["name"] == "Süße Töne – ß.flac"


def test_response_size_is_capped_for_powershell_5_1():
    huge = {"data": {"tracks": [{"x": "y" * 1000}] * 3000, "events": [{"a": "b" * 500}] * 3000}}
    raw = api._shrink(huge, False)
    assert len(raw) <= api.MAX_RESPONSE_BYTES and json.loads(raw)["truncated"] is True


def test_many_tracks_diagnostics_stay_below_cap_and_report_omitted():
    c = attached()
    feed(c, ev("queue_start", tracks=["x"] * 1000, profile={"name": "neutral"}, profile_neutral=True, renderer="r", poll_s=0.5))
    for i in range(1000):
        feed(c, ev("track_end", index=i, path=f"{PRIVATE_DIR}\\{i:04d}.flac", end_reason="ended", startup_delay_s=0.2,
                   wall_s=1.0, last_position_s=1.0, ended_early=False, gap_before_s=0.1, requests=[]))
    t0 = time.perf_counter()
    raw = api.respond(c, "/api/v1/diagnostics")[1]
    dt = time.perf_counter() - t0
    d = json.loads(raw)["data"]
    assert len(raw) < api.MAX_RESPONSE_BYTES and len(d["tracks"]) == api.MAX_TRACKS_IN_DIAG and d["tracks_omitted"] == 700
    assert d["timing"]["gap_before_s"]["n"] == 1000  # Statistik ueber alle, Liste nur die letzten
    assert dt < 2.0, dt


# ---- Befunde und Statistik ---------------------------------------------------------------------------------------

def test_findings_only_from_real_evidence():
    c = attached()
    one_track_run(c)  # sauberer Lauf
    f = body(c, "/api/v1/diagnostics")["data"]["findings"]
    assert [x for x in f if x["level"] in ("warn", "error")] == []
    c2 = attached()
    feed(c2, ev("queue_start", tracks=["a", "b", "c", "d"], profile={"name": "p"}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("sink_unavailable", error="boom"),
         ev("track_start", index=0, path="a.flac", served="a.flac", processed=False, mime="audio/x-weird", mime_in_sink=False,
            served_format=dict(SERVED)),
         ev("track_end", index=0, path="a.flac", end_reason="ended", ended_early=True, last_position_s=3.0, requests=[]),
         ev("track_end", index=1, path="b.flac", end_reason="never_started", error="kein Start", requests=[]),
         ev("track_end", index=2, path="c.flac", end_reason="prepare_error", error="kaputt"),
         ev("stop_failed", error="x"), ev("cleanup_failed", error="y", path="t"),
         ev("track_processed", index=3, src="d.flac", preamp_db=-3.0, peak_in_dbfs=-1.0, peak_out_dbfs=-0.1, guard_gain_db=-0.5,
            warnings=[], elapsed_s=0.1))
    codes = {x["code"] for x in body(c2, "/api/v1/diagnostics")["data"]["findings"]}
    assert {"track_problem", "ended_early", "mime_not_in_sink", "sink_unavailable", "stop_failed", "cleanup_failed",
            "clipping_guard"} <= codes
    errs = [x for x in body(c2, "/api/v1/diagnostics")["data"]["findings"] if x["code"] == "track_problem"]
    assert {e["evidence"]["end_reason"] for e in errs} == {"never_started", "prepare_error"}


def test_timing_statistics_exact_and_without_judgement():
    c = attached()
    feed(c, ev("queue_start", tracks=["a", "b", "c", "d"], profile={"name": "p"}, profile_neutral=True, renderer="r", poll_s=0.5))
    for i, (gap, su) in enumerate([(None, 0.5), (0.1, 0.4), (0.3, 0.3), (0.2, 0.2)]):
        feed(c, ev("track_end", index=i, path=f"{i}.flac", end_reason="ended", startup_delay_s=su, wall_s=1.0,
                   last_position_s=1.0, ended_early=False, gap_before_s=gap, requests=[]))
    t = body(c, "/api/v1/diagnostics")["data"]["timing"]
    assert t["gap_before_s"] == {"n": 3, "min": 0.1, "median": 0.2, "mean": 0.2, "max": 0.3}
    assert t["startup_delay_s"]["n"] == 4 and t["startup_delay_s"]["max"] == 0.5
    assert api._stats([]) == {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    assert api._stats([float("nan"), True, "x", 2]) ["n"] == 1
    assert "Modellangabe" in t["resolution_hint"]["note"]


# ---- DSP, Audio -----------------------------------------------------------------------------------------------------

def test_dsp_shows_profile_channels_and_defaults_for_missing_channel():
    from kefplayer.dsp import ChannelSpec, Filter, Profile

    prof = Profile(name="test", preamp_db=None, filters=[Filter("peq", 2000, -1.5, 2.0)],
                   channels={1: ChannelSpec(gain_db=-0.5, delay_ms=2.5, filters=[Filter("highpass", 80)])})
    c = api.StatusCollector()
    c.attach(profile=prof)
    d = body(c, "/api/v1/dsp")["data"]
    assert d["source"] == "Player-Konfiguration" and d["profile"]["name"] == "test"
    assert d["profile"]["preamp_setting_db"] == "auto" and d["profile"]["is_neutral"] is False
    assert d["profile"]["filters_all_channels"] == [{"type": "peq", "fc": 2000, "gain_db": -1.5, "q": 2.0}]
    ch = d["profile"]["channels"]
    assert ch["L"] == {"gain_db": 0.0, "delay_ms": 0.0, "filters": [], "defined_in_profile": False}
    assert ch["R"]["delay_ms"] == 2.5 and ch["R"]["defined_in_profile"] is True
    assert d["channel_delays_ms"] == {"L": 0.0, "R": 2.5}
    assert any("Kanalunterschiede" in w for w in d["profile"]["warnings"])
    assert "applied_per_track" in body(c, "/api/v1/dsp")["unknown"]  # noch nichts verarbeitet -> nicht erfunden


def test_dsp_applied_values_per_track_come_from_events():
    c = attached()
    feed(c, ev("queue_start", tracks=["a.flac"], profile={"name": "p", "preamp_db": "auto", "filters": []},
               profile_neutral=False, renderer="r", poll_s=0.5),
         ev("track_processed", index=0, src="a.flac", preamp_db=-2.5, peak_in_dbfs=-1.0, peak_out_dbfs=-0.5,
            guard_gain_db=0.0, warnings=[], elapsed_s=0.4),
         ev("track_start", index=0, path="a.flac", served="x.flac", processed=True, mime="audio/flac", mime_in_sink=True,
            served_format=dict(SERVED)))
    d = body(c, "/api/v1/dsp")["data"]
    assert d["source"] == "queue_start-Ereignis"
    assert d["applied_per_track"] == [{"index": 0, "name": "a.flac", "preamp_db": -2.5, "peak_in_dbfs": -1.0,
                                       "peak_out_dbfs": -0.5, "guard_gain_db": 0.0, "elapsed_s": 0.4, "current": True}]


def test_audio_original_path_has_source_equal_target():
    c = attached()
    one_track_run(c, processed=False)
    a = body(c, "/api/v1/audio")["data"]["recent"][0]
    assert a["processing_path"]["mode"] == "original" and a["source_format"]["value"] == SERVED == a["target_format"]


def test_audio_processed_path_reads_source_header_and_reports_differences(tone_factory, tmp_path):
    src = tone_factory("quelle", seconds=0.5, fs=48000, subtype="PCM_24")
    c = attached()
    feed(c, ev("queue_start", tracks=[str(src)], profile={"name": "p", "filters": []}, profile_neutral=False, renderer="r", poll_s=0.5),
         ev("track_start", index=0, path=str(src), served="x", processed=True, mime="audio/flac", mime_in_sink=None,
            served_format={**SERVED, "bits": 16, "samplerate": 48000}))
    a = body(c, "/api/v1/audio")["data"]["current"]
    assert a["source_format"]["value"]["bits"] == 24 and a["source_format"]["value"]["samplerate"] == 48000
    assert a["differs"] == {"format": False, "samplerate": False, "channels": False, "bits": True}
    assert a["processing_path"]["mode"] == "dsp" and a["mime_in_sink"] is None
    # Quelle nicht mehr da: unbekannt mit Grund, kein Fehler
    shutil.move(src, tmp_path / "weg.flac")
    c._source_cache.clear()
    r = api.respond(c, "/api/v1/audio")
    assert r[0] == 200
    d = json.loads(r[1])
    assert d["data"]["current"]["source_format"]["value"] is None and any(k.startswith("current.source_format") for k in d["unknown"])


def test_audio_convert_only_when_profile_neutral(tone_factory):
    src = tone_factory("q", seconds=0.3)
    c = attached()
    feed(c, ev("queue_start", tracks=[str(src)], profile={"name": "neutral"}, profile_neutral=True, renderer="r", poll_s=0.5),
         ev("track_start", index=0, path=str(src), served="x", processed=True, mime="audio/wav", mime_in_sink=True,
            served_format={**SERVED, "format": "WAV"}))
    a = body(c, "/api/v1/audio")["data"]["current"]
    assert a["processing_path"]["mode"] == "convert" and "kein Filter" in a["processing_path"]["description"]
    assert a["differs"]["format"] is True


# ---- Renderer -----------------------------------------------------------------------------------------------------------

def test_renderer_capabilities_come_from_sink_and_never_claim_gapless():
    c = attached()
    one_track_run(c)
    d = body(c, "/api/v1/renderer")
    cap = d["data"]["capabilities"]
    assert cap["flac_in_sink"] is True and cap["wav_in_sink"] is False and cap["mp3_in_sink"] is False
    assert "capabilities.gapless" in d["unknown"]
    assert d["data"]["services"] == ["AVTransport:1"]
    assert d["data"]["connection"]["reachable"] is None and d["data"]["connection"]["evidence"]["sink_state"] == "read"


def test_renderer_sink_unavailable_is_flagged_not_guessed():
    c = attached()
    feed(c, ev("sink_unavailable", error="timeout"))
    d = body(c, "/api/v1/renderer")
    assert d["data"]["capabilities"]["flac_in_sink"] is None and "sink.mimes" in d["unknown"]
    assert d["data"]["sink"]["state"] == "unavailable"


# ---- Zustand-Kopie: Lesen darf den Sammler nicht veraendern ----------------------------------------------------------------

def test_reading_does_not_mutate_state():
    c = attached()
    one_track_run(c)
    before = json.dumps(c.snapshot(), sort_keys=True, default=str)
    for p in ("/api/v1/status", "/api/v1/renderer", "/api/v1/dsp", "/api/v1/audio", "/api/v1/logs", "/api/v1/diagnostics"):
        api.respond(c, p)
    s = c.snapshot()
    after = json.dumps(s, sort_keys=True, default=str)
    keep = lambda x: {k: v for k, v in json.loads(x).items() if k not in ("now_mono", "now_iso", "uptime_s", "api_requests")}
    assert keep(before) == keep(after)


def test_version_is_reported():
    from kefplayer import __version__

    assert body(api.StatusCollector(), "/api/v1/status")["kefplayer_version"] == __version__ == "0.2.1"


def test_media_server_bind_address_is_redacted_when_it_is_a_lan_ip():
    class LanServer(FakeServer):
        bind = "192.168.1.77"

    c = api.StatusCollector()
    c.attach(server=LanServer())
    text = api.respond(c, "/api/v1/renderer")[1].decode()
    assert "192.168.1.77" not in text and json.loads(text)["data"]["media_server"]["bind"] == "redacted"
    cd = api.StatusCollector(details=True)
    cd.attach(server=LanServer())
    assert json.loads(api.respond(cd, "/api/v1/renderer")[1])["data"]["media_server"]["bind"] == "192.168.1.77"
    c2 = api.StatusCollector()
    c2.attach(server=FakeServer())  # Standard 0.0.0.0 verraet nichts
    md = json.loads(api.respond(c2, "/api/v1/renderer")[1])["data"]["media_server"]
    assert md["bind"] == "0.0.0.0" and md["advertise_host"] == "redacted"
