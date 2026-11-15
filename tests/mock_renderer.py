"""Simulierter UPnP-MediaRenderer fuer Tests.

Spielt beschleunigt ab (speedup), ruft die Medien-URL wirklich per HTTP ab (HEAD, dann GET),
dekodiert die Datei mit soundfile und speichert die abgerufenen Bytes. Damit laesst sich pruefen,
WAS beim Renderer ankommt. Das Verhalten einer echten KEF ist damit NICHT belegt.
"""

import http.server
import io
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import soundfile as sf

from kefplayer.upnp import fmt_hms

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"

DEFAULT_SINK = [
    "http-get:*:audio/flac:*",
    "http-get:*:audio/x-flac:*",
    "http-get:*:audio/wav:*",
    "http-get:*:audio/x-wav:*",
    "http-get:*:audio/mpeg:*",
]

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Fault(Exception):
    def __init__(self, code, desc):
        super().__init__(desc)
        self.code, self.desc = code, desc


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class MockRenderer:
    def __init__(self, speedup=5.0, sink=None, reject_mimes=(), start_delay=0.02, ssdp=True,
                 cut_fraction=None, stall=False):
        self.speedup = speedup
        self.cut_fraction = cut_fraction  # Wiedergabe endet vorzeitig bei diesem Anteil der Dauer
        self.stall = stall                # bleibt nach Play in TRANSITIONING haengen
        self.sink = list(DEFAULT_SINK if sink is None else sink)
        self.reject_mimes = {m.lower() for m in reject_mimes}
        self.start_delay = start_delay
        self.timeline = []  # ("start"|"end", time.perf_counter()) aus Sicht des Renderers (Wahrheit fuer Luecken)
        self.actions = []   # (Aktion, Argumente) in Reihenfolge
        self.fetched = []   # {"uri", "content_type", "data"}
        self.head_count = 0
        self.metadata = []  # DIDL je SetAVTransportURI
        self._lock = threading.RLock()
        self._state = "NO_MEDIA_PRESENT"
        self._uri = ""
        self._gen = 0
        self._duration = None
        self._pos = 0.0
        self._t_ref = None
        self._httpd = None
        self._udp = None
        self._want_ssdp = ssdp
        self._closed = False

    # ---- Lebenszyklus ------------------------------------------------------
    def start(self):
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/description.xml":
                    body = mock.description().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                svc = {"/avt/control": AVT, "/cm/control": CM, "/rc/control": RC}.get(self.path)
                action = (self.headers.get("SOAPACTION") or "").strip('"').split("#")[-1]
                try:
                    root = ET.fromstring(raw)
                    call = root.find("{*}Body")[0]
                    args = {_local(c.tag): (c.text or "") for c in call}
                    out = mock.handle(svc, action, args)
                    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in out.items())
                    body = (
                        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                        f'<u:{action}Response xmlns:u="{svc}">{inner}</u:{action}Response></s:Body></s:Envelope>'
                    ).encode()
                    status = 200
                except Fault as f:
                    body = (
                        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
                        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
                        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                        f"<errorCode>{f.code}</errorCode><errorDescription>{escape(f.desc)}</errorDescription>"
                        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
                    ).encode()
                    status = 500
                self.send_response(status)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        if self._want_ssdp:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.bind(("127.0.0.1", 0))
            self._udp.settimeout(0.1)
            self.ssdp_addr = self._udp.getsockname()
            threading.Thread(target=self._ssdp_loop, daemon=True).start()
        return self

    def stop(self):
        self._closed = True
        with self._lock:
            self._gen += 1
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._udp:
            self._udp.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def location(self):
        return f"http://127.0.0.1:{self.port}/description.xml"

    @property
    def state(self):
        with self._lock:
            return self._state

    def description(self):
        return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>
<device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<friendlyName>Mock KEF</friendlyName><manufacturer>Mock</manufacturer><modelName>MockRenderer</modelName>
<UDN>uuid:mock-0001</UDN><serviceList>
<service><serviceType>{AVT}</serviceType><serviceId>urn:upnp-org:serviceId:AVTransport</serviceId><SCPDURL>/avt/scpd.xml</SCPDURL><controlURL>/avt/control</controlURL><eventSubURL>/avt/event</eventSubURL></service>
<service><serviceType>{CM}</serviceType><serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId><SCPDURL>/cm/scpd.xml</SCPDURL><controlURL>/cm/control</controlURL><eventSubURL>/cm/event</eventSubURL></service>
<service><serviceType>{RC}</serviceType><serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId><SCPDURL>/rc/scpd.xml</SCPDURL><controlURL>/rc/control</controlURL><eventSubURL>/rc/event</eventSubURL></service>
</serviceList></device></root>"""

    # ---- SSDP -----------------------------------------------------------------
    def _ssdp_loop(self):
        while not self._closed:
            try:
                data, src = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            text = data.decode("latin-1")
            if text.startswith("M-SEARCH") and "MediaRenderer" in text:
                resp = (
                    "HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\n"
                    f"LOCATION: {self.location}\r\nSERVER: Mock/1.0 UPnP/1.0\r\n"
                    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\nUSN: uuid:mock-0001::urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
                )
                self._udp.sendto(resp.encode(), src)

    # ---- Aktionen ---------------------------------------------------------------
    def _cur_pos(self):
        if self._state == "PLAYING":
            return self._pos + (time.perf_counter() - self._t_ref) * self.speedup
        return self._pos

    @staticmethod
    def _didl_info(didl):
        try:
            root = ET.fromstring(didl)
            res = root.find(".//{*}res")
            title = root.find(".//{http://purl.org/dc/elements/1.1/}title")
            mime = res.get("protocolInfo").split(":")[2]
            return {"mime": mime, "url": res.text, "title": title.text if title is not None else None, "res": dict(res.attrib)}
        except Exception:
            return None

    def handle(self, svc, action, args):
        with self._lock:
            self.actions.append((action, dict(args)))
            if svc == AVT:
                if action == "SetAVTransportURI":
                    info = self._didl_info(args.get("CurrentURIMetaData", ""))
                    if info and info["mime"].lower() in self.reject_mimes:
                        raise Fault(714, "Illegal MIME-type")
                    self.metadata.append(info)
                    self._gen += 1
                    self._uri = args["CurrentURI"]
                    self._state, self._pos, self._duration = "STOPPED", 0.0, None
                    return {}
                if action == "Play":
                    if self._state == "PAUSED_PLAYBACK":
                        self._t_ref = time.perf_counter()
                        self._state = "PLAYING"
                    elif self._state == "STOPPED" and self._uri:
                        self._gen += 1
                        self._state = "TRANSITIONING"
                        threading.Thread(target=self._run, args=(self._gen, self._uri), daemon=True).start()
                    elif self._state in ("PLAYING", "TRANSITIONING"):
                        pass
                    else:
                        raise Fault(701, "Transition not available")
                    return {}
                if action == "Pause":
                    if self._state == "PLAYING":
                        self._pos = self._cur_pos()
                        self._state = "PAUSED_PLAYBACK"
                    return {}
                if action == "Stop":
                    self._gen += 1
                    if self._state != "NO_MEDIA_PRESENT":
                        self._state = "STOPPED"
                    self._pos = 0.0
                    return {}
                if action == "GetTransportInfo":
                    return {"CurrentTransportState": self._state, "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}
                if action == "GetPositionInfo":
                    return {
                        "Track": "1",
                        "TrackDuration": fmt_hms(self._duration or 0),
                        "TrackMetaData": "",
                        "TrackURI": self._uri,
                        "RelTime": fmt_hms(self._cur_pos()),
                        "AbsTime": fmt_hms(self._cur_pos()),
                    }
            elif svc == CM and action == "GetProtocolInfo":
                return {"Source": "", "Sink": ",".join(self.sink)}
        raise Fault(401, "Invalid Action")

    def _http(self, method, uri):
        req = urllib.request.Request(uri, method=method)
        with _OPENER.open(req, timeout=5) as resp:
            return resp.headers, resp.read()

    def _set_if_gen(self, gen, state):
        with self._lock:
            if gen == self._gen:
                self._state = state

    def _run(self, gen, uri):
        time.sleep(self.start_delay)
        if self.stall:
            while not self._closed:
                with self._lock:
                    if gen != self._gen:
                        return
                time.sleep(0.01)
            return
        try:
            self._http("HEAD", uri)
            with self._lock:
                self.head_count += 1
            headers, data = self._http("GET", uri)
        except Exception:
            self._set_if_gen(gen, "STOPPED")
            return
        with self._lock:
            self.fetched.append({"uri": uri, "content_type": headers.get("Content-Type"), "data": data})
        try:
            dur = sf.info(io.BytesIO(data)).duration
        except Exception:
            self._set_if_gen(gen, "STOPPED")
            return
        with self._lock:
            if gen != self._gen:
                return
            self._duration, self._pos, self._t_ref, self._state = dur, 0.0, time.perf_counter(), "PLAYING"
            self.timeline.append(("start", self._t_ref))
        while True:
            time.sleep(0.002)
            with self._lock:
                if gen != self._gen:
                    return
                if self._state == "PLAYING" and self._cur_pos() >= self._duration * (self.cut_fraction or 1.0):
                    self._pos = 0.0
                    self._state = "STOPPED"
                    self.timeline.append(("end", time.perf_counter()))
                    return
