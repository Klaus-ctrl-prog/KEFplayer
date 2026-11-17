import threading
import types

import pytest
from kefplayer import health, upnp
from kefplayer import repair_budget as B
from kefplayer import repair_exec as X
from mock_renderer import MockRenderer

ROUNDS = [("ok", "ok"), ("ok", "error"), ("ok", "error")]


def _info(udn="uuid:1", loc="http://x/d.xml"):
    return types.SimpleNamespace(udn=udn, location=loc, friendly_name="KEF")


class FakePlayer:
    def __init__(self, info=None):
        self.renderer = types.SimpleNamespace(info=info or _info())
        self.track_active = threading.Event()
        self.swapped = []

    def request_renderer_swap(self, new):
        self.swapped.append(new)
        return "applied"


def _env():
    t = [0.0]
    events = []
    return B.RepairBudget(clock=lambda: t[0]), events, lambda *a, **k: events.append((a[0], k))


OK = lambda info: {"status": "ok"}
BAD = lambda info: {"status": "error"}


def _run(player, rounds=ROUNDS, make=None, verify=OK, env=None):
    b, events, emit = env or _env()
    make = make or (lambda loc: types.SimpleNamespace(info=_info(loc=loc)))
    res = X.refresh_renderer(player, rounds, budget=b, emit=emit, make_renderer=make, verify=verify)
    return res, events, b


@pytest.mark.parametrize("rounds,expected", [
    ([("ok", "error"), ("ok", "error")], True),
    ([("ok", "ok"), ("ok", "error")], False),
    ([("error", "error"), ("error", "error")], False),   # HTTP weg: Geraet/Netz, nicht R3
    ([("ok", "unknown"), ("ok", "error")], False),
    ([("ok", "error")], False), ([], False), (None, False),
])
def test_trigger_needs_http_ok_and_upnp_error_twice(rounds, expected):
    assert X.should_refresh_renderer(rounds)[0] is expected


def test_success_swaps_after_verification():
    p = FakePlayer()
    res, events, b = _run(p)
    assert res == "success" and len(p.swapped) == 1
    assert events[-1][1]["result"] == "success"


def test_not_triggered_touches_nothing():
    p = FakePlayer()
    res, events, _ = _run(p, rounds=[("ok", "ok"), ("ok", "ok")])
    assert res == "not_triggered" and p.swapped == [] and events == []


def test_blocked_while_track_active():
    p = FakePlayer()
    p.track_active.set()
    made = []
    res, events, _ = _run(p, make=lambda loc: made.append(loc))
    assert res == "blocked" and made == [] and events[-1][1]["reason"] == "track_active"


def test_other_device_is_never_adopted():
    p = FakePlayer(_info(udn="uuid:1"))
    res, events, _ = _run(p, make=lambda loc: types.SimpleNamespace(info=_info(udn="uuid:OTHER")))
    assert res == "failed" and p.swapped == [] and events[-1][1]["reason"] == "udn_mismatch"


def test_unverified_new_renderer_is_not_swapped_old_kept():
    p = FakePlayer()
    old = p.renderer
    res, events, _ = _run(p, verify=BAD)
    assert res == "failed" and p.swapped == [] and p.renderer is old
    assert events[-1][1]["reason"] == "not_verified"


def test_description_error_is_failure_and_counted_once():
    def boom(loc):
        raise OSError("timeout")
    p = FakePlayer()
    res, events, b = _run(p, make=boom)
    assert res == "failed" and events[-1][1]["reason"] == "description_error"
    assert len(b.attempts[X.ACTION_R3]) == 1


def test_budget_cooldown_blocks():
    env = _env()
    env[0].record_attempt(X.ACTION_R3, succeeded=False)
    p = FakePlayer()
    res, events, _ = _run(p, env=env)
    assert res == "blocked" and events[-1][1]["reason"] == "cooldown"


# ---- echter Player + simulierter Renderer ----

def test_real_player_swap_when_idle(rig):
    mock, server, player = rig()
    old = player.renderer
    b, events, emit = _env()
    res = X.refresh_renderer(player, ROUNDS, budget=b, emit=emit,
                             make_renderer=lambda loc: upnp.Renderer.from_location(loc),
                             verify=health.check_renderer_upnp)
    assert res == "success" and player.renderer is not old
    assert player.renderer.info.udn == old.info.udn
    assert events[-1][1]["applied"] == "applied"


def test_real_player_swap_requested_during_queue_is_applied_at_next_track(rig, tone_factory):
    a, c = tone_factory("a", seconds=1.0, fs=8000), tone_factory("c", seconds=1.0, fs=8000)
    mock, server, player = rig()
    old = player.renderer
    new = upnp.Renderer.from_location(mock.location)
    seen = {}

    orig_emit = player._emit

    def spy(event, **kw):
        if event == "track_start" and kw.get("index") == 0:
            seen["first"] = player.request_renderer_swap(new)
            seen["renderer_during_first"] = player.renderer
        if event == "track_start" and kw.get("index") == 1:
            seen["renderer_during_second"] = player.renderer
        return orig_emit(event, **kw)

    player._emit = spy
    res = player.play_queue([a, c])
    assert [r["end_reason"] for r in res] == ["ended", "ended"]
    assert seen["first"] == "pending"
    assert seen["renderer_during_first"] is old          # laufender Titel behaelt seinen Renderer
    assert seen["renderer_during_second"] is new         # naechster Titel nutzt den neuen
