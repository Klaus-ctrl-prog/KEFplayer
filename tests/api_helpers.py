"""Gemeinsame Hilfen fuer die API-Tests (nur neue Datei, die bestehenden Tests bleiben unveraendert)."""

import http.client
import json
import socket
import time

from kefplayer import api, upnp

TOKEN = "t0k3n-GEHEIM-4711"
PRIVATE_DIR = r"C:\Users\Klaus\Musik\Jazz"
RENDERER_IP = "192.168.1.50"
FRIENDLY = "Klaus Wohnzimmer KEF"
UDN = "uuid:abc-123-secret-udn"

SERVED = {"format": "FLAC", "subtype": "PCM_16", "bits": 16, "samplerate": 44100, "channels": 2,
          "frames": 441000, "duration_s": 10.0}


def ev(event, **kw):
    return {"ts": "2026-11-14T12:00:00.000+00:00", "event": event, **kw}


def feed(collector, *records):
    for r in records:
        collector.on_event(r)


def fake_renderer():
    """Echte upnp-Objekte, aber ohne Netzwerk. Enthaelt absichtlich private Angaben."""
    svc = {upnp.SVC_AVT: upnp.Service(upnp.SVC_AVT, "urn:upnp-org:serviceId:AVTransport",
                                      f"http://{RENDERER_IP}:50001/avt/control")}
    info = upnp.DeviceInfo(location=f"http://{RENDERER_IP}:50001/desc.xml", friendly_name=FRIENDLY,
                           manufacturer="KEF", model_name="LS50 Wireless", udn=UDN,
                           device_type="urn:schemas-upnp-org:device:MediaRenderer:1", services=svc)
    return upnp.Renderer(info)


class FakeServer:
    port = 8765
    bind = "0.0.0.0"
    advertise_host = RENDERER_IP.rsplit(".", 1)[0] + ".20"


def one_track_run(collector, name="01 Test.flac", processed=False, end_reason="ended", **end_kw):
    path = PRIVATE_DIR + "\\" + name
    start = dict(index=0, path=path, served=path, processed=processed, mime="audio/flac", mime_in_sink=True,
                 served_format=dict(SERVED))
    end = dict(start, end_reason=end_reason, startup_delay_s=0.2, wall_s=10.4, last_position_s=9.9,
               ended_early=False, gap_before_s=None,
               requests=[{"client": RENDERER_IP, "method": "HEAD", "range": None, "status": 200, "bytes_sent": 0},
                         {"client": RENDERER_IP, "method": "GET", "range": "bytes=0-", "status": 206, "bytes_sent": 1000}])
    end.update(end_kw)
    feed(collector,
         ev("queue_start", tracks=[path], profile={"name": "neutral", "preamp_db": "auto", "filters": []},
            profile_neutral=True, renderer=FRIENDLY, poll_s=0.5),
         ev("sink", protocol_infos=["http-get:*:audio/flac:*"], mimes=["audio/flac"]),
         ev("track_start", **start),
         ev("track_end", **end),
         ev("queue_end", played=1, reasons=[end_reason]))
    return path


def call(port, path, method="GET", headers=None, host=None, body=None):
    """Roher HTTP-Aufruf. host=None -> Standard (127.0.0.1:port); host='' -> ohne Host-Header."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host != "":
            conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        conn.close()


def get_json(port, path):
    st, hd, data = call(port, path)
    assert st == 200, (st, data)
    return json.loads(data)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(cond, timeout=10.0, step=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


def all_endpoint_paths():
    return ["/"] + [f"/api/v1/{e}" for e in api.ENDPOINTS]
