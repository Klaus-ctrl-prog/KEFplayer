"""Anbindung der Status-API an `kefplayer play`: Standard, Abschalten, Fehlerfaelle, sauberes Beenden."""

import json
import socket
import sys
import threading

import pytest

from kefplayer import api, cli
from mock_renderer import MockRenderer
from api_helpers import call, free_port, get_json, wait_for


def play_args(tmp_path, mock, *files, extra=()):
    return ["play", *map(str, files), "--renderer", mock.location, "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", "0", "--poll", "0.02", "--log", str(tmp_path / "r.jsonl"), *extra]


def api_threads():
    return [t for t in threading.enumerate() if t.name == "kefplayer-status-api" and t.is_alive()]


def refused(port):
    s = socket.socket()
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def test_default_port_constant_matches_module():
    assert cli.API_DEFAULT_PORT == api.DEFAULT_PORT == 8766
    from kefplayer.cli import build_parser

    assert build_parser().parse_args(["play", "x"]).api_port == 8766
    assert build_parser().parse_args(["play", "x"]).port == 8765  # Medienserver: anderer Port als die API


def test_default_run_prints_banner_writes_diag_file_and_cleans_up(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=1.0)
    port = free_port()
    diag = tmp_path / "diag.json"
    with MockRenderer() as mock:
        rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", str(port), "--diag-file", str(diag)]))
    out = capsys.readouterr().out
    assert rc == 0
    assert f"Status-API: http://127.0.0.1:{port}/api/v1/diagnostics (nur lesend, nur lokal)" in out
    assert f"Diagnoseexport: {diag}" in out
    raw = diag.read_bytes()
    raw.decode("ascii")  # PowerShell 5.1: kein Kodierungsproblem
    d = json.loads(raw)
    assert d["data"]["final"] is True and d["data"]["summary"]["end_reasons"] == {"ended": 1}
    assert d["data"]["summary"]["phase"] == "queue_ended" and d["read_only"] is True
    assert str(tmp_path).encode() not in raw  # kein privates Verzeichnis im Export
    assert refused(port) and api_threads() == []  # nach dem Ende: Port zu, Thread weg
    assert not list(tmp_path.glob("diag.json.tmp*"))  # keine Reste der atomaren Schreibweise


def test_api_answers_during_playback_and_is_gone_afterwards(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=3.0, fs=8000)
    port = free_port()
    result = {}
    with MockRenderer(speedup=1.0) as mock:
        t = threading.Thread(target=lambda: result.update(rc=cli.main(
            play_args(tmp_path, mock, a, extra=["--api-port", str(port), "--api-live", "--api-live-interval", "0.5"]))))
        t.start()

        def playing():
            try:
                d = get_json(port, "/api/v1/status")["data"]
            except OSError:
                return None
            return d if d["track"] and d["track"]["transport_state"] == "PLAYING" else None

        d = wait_for(playing, timeout=15)
        diag = get_json(port, "/api/v1/diagnostics") if d else None
        t.join(30)
    out = capsys.readouterr().out
    assert d and result["rc"] == 0
    assert d["track"]["name"] == "a.flac" and d["track"]["title"]["source"] == "filename" and 0 <= d["track"]["position_s"] <= 3.5
    assert diag["data"]["api"]["live_enabled"] is True and diag["data"]["summary"]["phase"] == "track_active"
    assert "Live-Abfrage an" in out
    assert refused(port) and api_threads() == []


def test_no_api_never_imports_the_module_and_opens_no_port(monkeypatch, tone_factory, tmp_path, capsys):
    import kefplayer

    monkeypatch.delitem(sys.modules, "kefplayer.api", raising=False)
    monkeypatch.delattr(kefplayer, "api", raising=False)
    monkeypatch.setitem(sys.modules, "kefplayer.api", None)  # jeder Importversuch scheitert mit ImportError
    a = tone_factory("a", seconds=2.0, fs=8000)
    port = free_port()
    result, probes = {}, []
    with MockRenderer(speedup=1.0) as mock:
        t = threading.Thread(target=lambda: result.update(rc=cli.main(
            play_args(tmp_path, mock, a, extra=["--no-api", "--api-port", str(port)]))))
        t.start()
        assert wait_for(lambda: mock.state == "PLAYING", 10)
        probes.append(refused(port))
        t.join(30)
    out = capsys.readouterr().out
    assert result["rc"] == 0 and probes == [True]
    assert "Status-API: aus (--no-api)" in out and "http://127.0.0.1" not in out
    assert api_threads() == []


@pytest.mark.parametrize("extra,msg", [
    (["--no-api", "--diag-file", "x.json"], "--diag-file"),
    (["--no-api", "--api-live"], "--api-live"),
    (["--no-api", "--api-details"], "--api-details"),
    (["--api-live-interval", "0.1"], "0.5"),
    (["--api-port", "70000"], "0..65535"),
    (["--api-port", "-5"], "0..65535"),
    (["--api-port", "9000", "--port", "9000"], "Medienserver"),
])
def test_contradictory_api_options_fail_early_without_network(extra, msg, tone_factory, tmp_path):
    a = tone_factory("a", seconds=0.3)
    argv = ["play", str(a), "--renderer", "http://127.0.0.1:9/gibt-es-nicht.xml", "--log", str(tmp_path / "r.jsonl"), *extra]
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert msg in str(e.value) and e.value.code not in (0, None)


def test_busy_api_port_warns_but_playback_continues(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=0.8)
    occupant = socket.socket()
    occupant.bind(("127.0.0.1", 0))
    occupant.listen(1)
    port = occupant.getsockname()[1]
    try:
        with MockRenderer() as mock:
            rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", str(port)]))
            assert len(mock.fetched) == 1 and mock.fetched[0]["data"] == a.read_bytes()
    finally:
        occupant.close()
    cap = capsys.readouterr()
    assert rc == 0 and "Status-API nicht gestartet" in cap.err and "Status-API: nicht gestartet" in cap.out
    assert api_threads() == []


def test_unwritable_diag_file_warns_and_keeps_exit_code(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=0.8)
    with MockRenderer() as mock:
        rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", "0", "--diag-file", str(tmp_path / "gibtsnicht" / "d.json")]))
    cap = capsys.readouterr()
    assert rc == 0 and "Diagnoseexport nicht geschrieben" in cap.err and "Diagnoseexport:" not in cap.out
    assert api_threads() == []


def test_ctrl_c_still_writes_diag_file_and_closes_the_api(tone_factory, tmp_path, capsys, monkeypatch):
    from kefplayer.player import Player

    a = tone_factory("a", seconds=0.8)
    diag = tmp_path / "abbruch.json"
    seen = {}

    def interrupted(self, paths):
        seen["port_open"] = not refused(seen["port"])
        raise KeyboardInterrupt

    monkeypatch.setattr(Player, "play_queue", interrupted)
    seen["port"] = free_port()
    with MockRenderer() as mock:
        rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", str(seen["port"]), "--diag-file", str(diag)]))
    assert rc == 130 and seen["port_open"] is True
    d = json.loads(diag.read_text(encoding="ascii"))
    assert d["data"]["final"] is True and d["data"]["summary"]["phase"] == "no_queue"
    assert refused(seen["port"]) and api_threads() == []


def test_details_flag_reaches_the_export(tone_factory, tmp_path):
    a = tone_factory("a", seconds=0.8)
    diag = tmp_path / "d.json"
    with MockRenderer() as mock:
        assert cli.main(play_args(tmp_path, mock, a, extra=["--api-port", "0", "--api-details", "--diag-file", str(diag)])) == 0
    d = json.loads(diag.read_text(encoding="ascii"))
    assert d["privacy"]["mode"] == "details" and d["data"]["renderer"]["identity"]["host"] == "127.0.0.1"
    assert d["data"]["tracks"][0]["path"] == str(a)


def test_player_console_output_is_unchanged_by_the_api(tone_factory, tmp_path, capsys):
    a = tone_factory("a", seconds=0.8)
    with MockRenderer() as mock:
        cli.main(play_args(tmp_path, mock, a, extra=["--api-port", "0"]))
    with_api = capsys.readouterr().out
    with MockRenderer() as mock:
        cli.main(play_args(tmp_path, mock, a, extra=["--no-api"]))
    without = capsys.readouterr().out

    def norm(s):  # variable Teile: Port, Zeiten, Pfade, Banner der API
        import re

        s = re.sub(r"Medienserver 127\.0\.0\.1:\d+", "Medienserver X", s)
        s = re.sub(r"Start nach [\d.]+ s", "Start", s)
        s = re.sub(r"Fertig: .*", "Fertig", s)
        return "\n".join(l for l in s.splitlines() if not l.startswith("Status-API"))

    assert norm(with_api) == norm(without)


def test_api_failures_never_change_playback_result(tone_factory, tmp_path, capsys, monkeypatch):
    """Ein Fehler beim Einrichten, Starten oder Beenden der API darf Wiedergabe und Exit-Code nicht beeinflussen."""
    a = tone_factory("a", seconds=0.8)

    def boom(*args, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(api.StatusCollector, "attach", boom)
    monkeypatch.setattr(api.StatusApiServer, "start", boom)
    monkeypatch.setattr(api, "write_diag_file", boom)
    with MockRenderer() as mock:
        rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", "0", "--diag-file", str(tmp_path / "d.json")]))
        assert len(mock.fetched) == 1 and mock.fetched[0]["data"] == a.read_bytes()
    cap = capsys.readouterr()
    assert rc == 0 and "unvollstaendig eingerichtet" in cap.err and "Status-API nicht gestartet" in cap.err
    assert "Diagnoseexport nicht geschrieben" in cap.err and api_threads() == []


def test_api_stop_failure_is_only_a_warning(tone_factory, tmp_path, capsys, monkeypatch):
    a = tone_factory("a", seconds=0.8)
    real_stop = api.StatusApiServer.stop

    def bad_stop(self):
        real_stop(self)
        raise RuntimeError("stop kaputt")

    monkeypatch.setattr(api.StatusApiServer, "stop", bad_stop)
    with MockRenderer() as mock:
        rc = cli.main(play_args(tmp_path, mock, a, extra=["--api-port", "0"]))
    assert rc == 0 and "nicht sauber beendet" in capsys.readouterr().err
