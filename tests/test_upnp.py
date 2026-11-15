import http.client
import threading
import xml.etree.ElementTree as ET

import pytest

from kefplayer import upnp
from kefplayer.mediaserver import MediaServer, parse_range, RangeNotSatisfiable
from mock_renderer import MockRenderer


# ---- Hilfsfunktionen ----

def test_hms_roundtrip():
    assert upnp.fmt_hms(3725.5) == "1:02:05.500"
    assert upnp.parse_hms("1:02:05.500") == pytest.approx(3725.5)
    assert upnp.parse_hms("0:00:07") == 7.0
    assert upnp.parse_hms("NOT_IMPLEMENTED") is None
    assert upnp.parse_hms("") is None


def test_pick_mime():
    assert upnp.pick_mime(".flac") == ("audio/flac", None)
    assert upnp.pick_mime(".FLAC", ["audio/x-flac", "audio/mpeg"]) == ("audio/x-flac", True)
    assert upnp.pick_mime(".flac", ["audio/mpeg"]) == ("audio/flac", False)
    assert upnp.pick_mime(".xyz")[0] == "application/octet-stream"


def test_didl_is_wellformed_and_escapes():
    title = 'A & B <"C"> \'D\' Ü'
    didl = upnp.build_didl(
        title, "http://1.2.3.4:5/m/t/x.flac?a=1&b=2", "audio/flac",
        artist="Ä & Ö", duration_s=185.0, samplerate=44100, channels=2, bits=16, size=1234,
    )
    root = ET.fromstring(didl)
    assert root.find(".//{http://purl.org/dc/elements/1.1/}title").text == title
    res = root.find(".//{*}res")
    assert res.text == "http://1.2.3.4:5/m/t/x.flac?a=1&b=2"
    assert res.get("protocolInfo").startswith("http-get:*:audio/flac:DLNA.ORG_OP=01")
    assert res.get("duration") == "0:03:05.000" and res.get("sampleFrequency") == "44100"
    plain = ET.fromstring(upnp.build_didl("t", "http://h/x.wav", "audio/wav", plain=True))
    assert plain.find(".//{*}res").get("protocolInfo") == "http-get:*:audio/wav:*"


def test_didl_survives_soap_escaping():
    # DIDL wird als Argument noch einmal escaped; Gegenseite muss das Original zurueckbekommen
    didl = upnp.build_didl("A & B", "http://h/x.flac", "audio/flac")
    env = upnp.build_envelope(upnp.SVC_AVT, "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": "http://h/x.flac", "CurrentURIMetaData": didl})
    root = ET.fromstring(env)
    meta = root.find(".//CurrentURIMetaData")
    assert meta.text == didl
    assert ET.fromstring(meta.text).find(".//{http://purl.org/dc/elements/1.1/}title").text == "A & B"


def test_ssdp_response_parsing():
    ok = b"HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.5:1234/d.xml\r\nST: x\r\nServer: s\r\n\r\n"
    r = upnp.parse_ssdp_response(ok)
    assert r["location"] == "http://10.0.0.5:1234/d.xml" and r["server"] == "s"
    assert upnp.parse_ssdp_response(b"NOTIFY * HTTP/1.1\r\n\r\n") is None
    assert upnp.parse_ssdp_response(b"") is None


def test_parse_fault_and_response():
    fault = (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault><detail>'
             b'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>714</errorCode>'
             b'<errorDescription>Illegal MIME-type</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
    e = upnp.parse_fault(fault, 500)
    assert e.code == 714 and "MIME" in e.description
    e2 = upnp.parse_fault(b"<html>boom", 500)
    assert e2.code is None and "HTTP 500" in e2.description
    with pytest.raises(upnp.UpnpError) as ei:
        upnp.parse_response(fault)
    assert ei.value.code == 714


# ---- gegen den Mock-Renderer ----

def test_description_and_actions_against_mock():
    with MockRenderer() as mock:
        r = upnp.Renderer.from_location(mock.location)
        assert r.info.friendly_name == "Mock KEF"
        assert set(r.info.services) >= {upnp.SVC_AVT, upnp.SVC_CM}
        assert r.info.services[upnp.SVC_AVT].control_url == f"http://127.0.0.1:{mock.port}/avt/control"
        assert r.transport_state() == "NO_MEDIA_PRESENT"
        assert "audio/flac" in r.sink_mimes()
        assert r.sink_protocol_infos()[0] == "http-get:*:audio/flac:*"
        with pytest.raises(upnp.UpnpError) as ei:
            r.soap("urn:schemas-upnp-org:service:RenderingControl:1", "SetVolume", {"InstanceID": "0"})
        assert ei.value.code == 401
        with pytest.raises(upnp.UpnpError, match="Dienst"):
            r.soap("urn:schemas-upnp-org:service:Nope:1", "X")


def test_reject_mime_gives_714():
    with MockRenderer(reject_mimes=["audio/wav"]) as mock:
        r = upnp.Renderer.from_location(mock.location)
        didl = upnp.build_didl("t", "http://h/x.wav", "audio/wav")
        with pytest.raises(upnp.UpnpError) as ei:
            r.set_uri("http://h/x.wav", didl)
        assert ei.value.code == 714


def test_ssdp_search_over_udp_to_mock():
    # Prueft Senden/Empfangen/Parsen ueber einen echten UDP-Socket (Unicast an den Mock).
    # Der Multicast-Beitritt im Heimnetz ist damit NICHT getestet.
    with MockRenderer() as mock:
        found = upnp.ssdp_search(timeout=0.8, addr=mock.ssdp_addr)
        assert len(found) == 1  # doppelt gesendet, aber nach LOCATION zusammengefasst
        assert found[0]["location"] == mock.location
        assert found[0]["_from"] == "127.0.0.1"


def test_ssdp_search_without_answer_returns_empty():
    # Ziel ohne Antwort (Mock ohne SSDP); Adresse nur zum Belegen eines freien UDP-Ports
    with MockRenderer(ssdp=False) as mock:
        assert upnp.ssdp_search(timeout=0.4, addr=("127.0.0.1", mock.port)) == []


# ---- Medienserver ----

@pytest.fixture
def served(tmp_path):
    payload = bytes(range(256)) * 4  # 1024 Bytes
    f = tmp_path / "file.bin"
    f.write_bytes(payload)
    with MediaServer(bind="127.0.0.1", port=0) as srv:
        url, token = srv.register(f, "audio/flac", name="a b.flac")
        yield srv, url, token, payload


def _req(srv, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_server_full_get_and_headers(served):
    srv, url, token, payload = served
    assert url.endswith("/a%20b.flac")
    path = url.split(f":{srv.port}", 1)[1]
    r, body = _req(srv, path)
    assert r.status == 200 and body == payload
    assert r.getheader("Content-Type") == "audio/flac"
    assert r.getheader("Accept-Ranges") == "bytes"
    assert r.getheader("Content-Length") == "1024"
    assert r.getheader("transferMode.dlna.org") == "Streaming"
    assert "DLNA.ORG_OP=01" in r.getheader("contentFeatures.dlna.org")


@pytest.mark.parametrize(
    "rng,status,start,end",
    [("bytes=0-99", 206, 0, 99), ("bytes=900-", 206, 900, 1023), ("bytes=-50", 206, 974, 1023),
     ("bytes=1000-5000", 206, 1000, 1023), ("bytes=-5000", 206, 0, 1023)],
)
def test_server_ranges(served, rng, status, start, end):
    srv, url, token, payload = served
    path = url.split(f":{srv.port}", 1)[1]
    r, body = _req(srv, path, headers={"Range": rng})
    assert r.status == status
    assert body == payload[start:end + 1]
    assert r.getheader("Content-Range") == f"bytes {start}-{end}/1024"
    assert r.getheader("Content-Length") == str(end - start + 1)


@pytest.mark.parametrize("rng", ["bytes=2000-", "bytes=1024-1030", "bytes=-0"])
def test_server_unsatisfiable_range_416(served, rng):
    srv, url, token, payload = served
    path = url.split(f":{srv.port}", 1)[1]
    r, body = _req(srv, path, headers={"Range": rng})
    assert r.status == 416 and r.getheader("Content-Range") == "bytes */1024"


@pytest.mark.parametrize("rng", ["bytes=5-2", "bytes=0-1,5-6", "items=0-5", "garbage"])
def test_server_ignores_invalid_or_unsupported_range(served, rng):
    srv, url, token, payload = served
    path = url.split(f":{srv.port}", 1)[1]
    r, body = _req(srv, path, headers={"Range": rng})
    assert r.status == 200 and body == payload


def test_server_head_has_no_body(served):
    srv, url, token, payload = served
    path = url.split(f":{srv.port}", 1)[1]
    r, body = _req(srv, path, method="HEAD")
    assert r.status == 200 and body == b"" and r.getheader("Content-Length") == "1024"


def test_server_only_registered_tokens(served, tmp_path):
    srv, url, token, payload = served
    for bad in ["/m/deadbeef/x.flac", f"/m/{token}", f"/m/{token}/a/b.flac", "/m/../../etc/passwd", "/", f"/x/{token}/a.flac",
                f"/m/{token}/..%2F..%2Fetc%2Fpasswd/x"]:
        r, _ = _req(srv, bad)
        assert r.status == 404, bad
    srv.unregister(token)
    r, _ = _req(srv, url.split(f":{srv.port}", 1)[1])
    assert r.status == 404


def test_server_request_log(served):
    srv, url, token, payload = served
    path = url.split(f":{srv.port}", 1)[1]
    logged = threading.Semaphore(0)  # wird vom Handler-Thread NACH dem Anhaengen des Log-Eintrags freigegeben
    real_log = srv._log_request

    def log_and_signal(rec):
        real_log(rec)
        logged.release()

    srv._log_request = log_and_signal
    _req(srv, path, method="HEAD")
    assert logged.acquire(timeout=5)  # HEAD ist eingetragen, bevor der GET startet (Reihenfolge); Timeout nur als Fehlergrenze
    _req(srv, path, headers={"Range": "bytes=0-9"})
    assert logged.acquire(timeout=5)  # GET ist eingetragen
    log = srv.requests_for(token)
    assert [(x["method"], x["status"], x["bytes_sent"]) for x in log] == [("HEAD", 200, 0), ("GET", 206, 10)]
    assert log[1]["range"] == "bytes=0-9" and log[0]["client"] == "127.0.0.1"


def test_server_needs_advertise_host_when_bound_to_all(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    with MediaServer(bind="0.0.0.0", port=0) as srv:
        with pytest.raises(ValueError):
            srv.register(f, "audio/flac")
        srv.advertise_host = "192.168.1.2"
        assert srv.register(f, "audio/flac")[0].startswith("http://192.168.1.2:")


def test_parse_range_unit():
    assert parse_range(None, 10) is None
    assert parse_range("bytes=2-4", 10) == (2, 4)
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=10-", 10)
