"""D2-Audit: Reparaturereignisse im JSONL-Protokoll und in /api/v1/health (Simulator)."""
import json

from kefplayer import api, repair_exec as X
from kefplayer.health import HealthMonitor


class StalledThenOk:
    def __init__(self, ok):
        self.ok = ok
        self._real = HealthMonitor(lambda *a, **k: None, info=None, server=None, cache_dir=None,
                                   busy=lambda: False, interval_s=30)

    def snapshot(self):
        return {**self._real.snapshot(), "state": "running" if self.ok else "stalled",
                "ticks": 1 if self.ok else 0}

    def start(self):
        return self

    def stop(self):
        pass


def test_repair_events_reach_jsonl_and_health_api(rig):
    mock, server, player = rig()
    collector = api.StatusCollector()
    player.on_event = collector.on_event
    old, new = StalledThenOk(False), StalledThenOk(True)
    sup = X.RepairSupervisor(old, lambda: new, busy=player.track_active.is_set, emit=player.emit,
                             on_swap=collector.set_health)
    collector.set_repair(sup)
    assert sup.tick() == "success"

    log = (player.log.path if hasattr(player.log, "path") else None)
    lines = [json.loads(x) for x in open(log, encoding="utf-8")]
    names = [(r["event"], r.get("action"), r.get("result")) for r in lines if r["event"].startswith("repair_")]
    assert names == [("repair_start", X.ACTION_R5, None), ("repair_result", X.ACTION_R5, "success")]

    data = json.loads(api.respond(collector, "/api/v1/health")[1])
    rep = data["data"]["repair"] if "data" in data else data["repair"]
    assert rep["enabled"] is True and rep["actions"] == [X.ACTION_R5]
    assert [h["event"] for h in rep["recent"]] == ["repair_start", "repair_result"]


def test_health_api_reports_repair_off_by_default():
    collector = api.StatusCollector()
    collector.set_health(HealthMonitor(lambda *a, **k: None, info=None, server=None, cache_dir=None,
                                       busy=lambda: False, interval_s=30))
    data = json.loads(api.respond(collector, "/api/v1/health")[1])
    rep = data["data"]["repair"] if "data" in data else data["repair"]
    assert rep["enabled"] is False
