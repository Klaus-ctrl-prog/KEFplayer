"""UPnP/DLNA-Steuerung: SSDP-Suche, Geraetebeschreibung, SOAP-Aktionen, DIDL-Lite.

Nur Standardbibliothek. Es wird bewusst nichts an der Lautstaerke des Renderers veraendert.
Alle Verbindungen laufen ohne Proxy (Geraetesteuerung im LAN).
"""

from __future__ import annotations

import functools
import http.client
import re
import socket
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
ST_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
SVC_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
SVC_CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
UA = "kefplayer/0.1 UPnP/1.0"

# Eine Sperre fuer alle Renderer-Instanzen (Player, HealthMonitor und API-Liveprobe).
# RLock erlaubt dem Player, zusammengehoerige Aktionssequenzen zu schuetzen, waehrend
# Renderer.soap dieselbe Sperre fuer einzelne SOAP-Aufrufe und fremde Renderer-Instanzen nutzt.
RENDERER_LOCK = threading.RLock()
DLNA_FLAGS = "01700000000000000000000000000000"
DLNA_FEATURES = f"DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}"
# Live-Stream: kein Byte-Seek, kein Zeit-Seek. Die FLAGS sind uebernommen (Annahme, an der Gen1 ungeprueft).
DLNA_FEATURES_LIVE = f"DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}"

# Vertrauensgrenze (F-06): nur http, keine Userinfo, Ziele nur auf Host/Port der LOCATION, keine Weiterleitungen,
# Antwortgroesse begrenzt, Zeitlimit auch als Gesamtfrist (gegen tropfende Antworten). Werte sind Arbeitswerte.
ALLOWED_SCHEMES = ("http",)
MAX_RESPONSE_BYTES = 1 << 20  # Geraetebeschreibung und SOAP-Antworten


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nie folgen: 3xx kommt als HTTPError beim Aufrufer an."""

    def redirect_request(self, *args, **kwargs):
        return None


class _Watchdog:
    """Gesamtfrist je Anfrage: trennt den Socket, auch wenn jede einzelne Leseoperation knapp unter dem Timeout bleibt."""

    def __init__(self, total_s: float):
        self._lock = threading.Lock()
        self._sock = None
        self.fired = False
        self._timer = threading.Timer(max(0.0, total_s), self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()

    def attach(self, sock) -> None:
        with self._lock:
            self._sock = sock
            if self.fired:
                self._kill(sock)

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            if self._sock is not None:
                self._kill(self._sock)

    @staticmethod
    def _kill(sock) -> None:
        try:
            sock.settimeout(0.01)  # portabel: der naechste recv() laeuft sofort ab
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _TrackedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, _kp_watchdog: _Watchdog | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._kp_watchdog = _kp_watchdog

    def connect(self):
        super().connect()
        if self._kp_watchdog is not None:
            self._kp_watchdog.attach(self.sock)


class _TrackingHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(functools.partial(_TrackedHTTPConnection, _kp_watchdog=getattr(req, "_kp_watchdog", None)), req)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _TrackingHTTPHandler)


class UpnpError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"UPnP-Fehler {code}: {description}" if code is not None else description)
        self.code = code
        self.description = description


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---- URL-Pruefung ----------------------------------------------------------------


def _endpoint(p: urllib.parse.SplitResult) -> tuple[str, int]:
    return (p.hostname or "").lower(), p.port or 80


def check_url(url: str, *, what: str = "URL", anchor: str | None = None) -> str:
    """Prueft Schema (nur http), Host, Port und Userinfo. Mit anchor muss Host und Port dem anchor entsprechen."""
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise UpnpError(None, f"{what} ungueltig (leer oder mit Steuer-/Leerzeichen)")
    try:
        p = urllib.parse.urlsplit(url)
        p.port  # wirft ValueError bei ungueltigem Port
    except ValueError:
        raise UpnpError(None, f"{what} ungueltig (Aufbau oder Port)") from None
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise UpnpError(None, f"{what}: Schema {p.scheme or '(keins)'!r} nicht erlaubt (nur http)")
    if "@" in p.netloc:
        raise UpnpError(None, f"{what}: Benutzerinformationen in der URL nicht erlaubt")
    if not p.hostname:
        raise UpnpError(None, f"{what}: Host fehlt")
    if anchor is not None and _endpoint(p) != _endpoint(urllib.parse.urlsplit(anchor)):
        raise UpnpError(None, f"{what} zeigt auf einen anderen Host/Port als die Geraetebeschreibung")
    return url


def _read_limited(resp, limit: int) -> bytes:
    getheader = getattr(resp, "getheader", None)
    cl = getheader("Content-Length") if callable(getheader) else None
    if cl and cl.strip().isdigit() and int(cl) > limit:
        raise UpnpError(None, f"Antwort zu gross (Content-Length {int(cl)} > {limit} Byte)")
    try:
        buf = bytearray()
        while True:
            chunk = resp.read(min(65536, limit + 1 - len(buf)))
            if not chunk:
                break
            buf += chunk
            if len(buf) > limit:
                raise UpnpError(None, f"Antwort zu gross (> {limit} Byte)")
        return bytes(buf)
    except TypeError:  # einfache Antwortobjekte ohne read(n)
        data = resp.read()
        if len(data) > limit:
            raise UpnpError(None, f"Antwort zu gross (> {limit} Byte)") from None
        return data


def _fetch(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """(HTTP-Status, Rumpf). 3xx/4xx/5xx kommen als Status zurueck (Rumpf ebenfalls begrenzt)."""
    wd = _Watchdog(timeout)
    req._kp_watchdog = wd
    wd.start()
    try:
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                status, data = getattr(resp, "status", None) or 200, _read_limited(resp, MAX_RESPONSE_BYTES)
            if wd.fired:  # Socket wurde getrennt: Rumpf kann still abgeschnitten sein (http.client meldet das nicht)
                raise UpnpError(None, f"Zeitlimit {timeout:g} s ueberschritten (Antwort zu langsam)")
            return status, data
        except urllib.error.HTTPError as e:
            try:
                body = _read_limited(e, MAX_RESPONSE_BYTES)
            except (OSError, http.client.HTTPException):
                body = b""
            finally:
                e.close()
            return e.code, body
    except (OSError, http.client.HTTPException):
        if wd.fired:
            raise UpnpError(None, f"Zeitlimit {timeout:g} s ueberschritten (Antwort zu langsam)") from None
        raise
    finally:
        wd.cancel()


# ---- SSDP ---------------------------------------------------------------------


def ssdp_location_trusted(location: str | None, sender_ip: str) -> bool:
    """LOCATION muss eine erlaubte URL sein und auf die Absender-IP der SSDP-Antwort zeigen."""
    try:
        p = urllib.parse.urlsplit(check_url(location or "", what="LOCATION"))
    except UpnpError:
        return False
    return (p.hostname or "").lower() == (sender_ip or "").lower()


def parse_ssdp_response(data: bytes) -> dict | None:
    lines = data.decode("latin-1").split("\r\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def ssdp_search(timeout: float = 3.0, st: str = ST_RENDERER, addr=SSDP_ADDR, bind_ip: str | None = None, mx: int = 2):
    """Sucht Renderer. Rueckgabe: Liste von Header-Dicts (eindeutig nach LOCATION), plus '_from' (Absender-IP).

    bind_ip: Schnittstelle waehlen (bei mehreren Netzwerkkarten unter Windows oft noetig).
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {addr[0]}:{addr[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n"
        f"USER-AGENT: {UA}\r\n\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if bind_ip:
            sock.bind((bind_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
        sock.settimeout(0.3)
        for _ in range(2):  # UDP kann verloren gehen
            sock.sendto(msg, addr)
        found: dict[str, dict] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, src = sock.recvfrom(65535)
            except (socket.timeout, ConnectionResetError):
                continue
            r = parse_ssdp_response(data)
            if r and r.get("location") and ssdp_location_trusted(r["location"], src[0]):
                r["_from"] = src[0]
                found.setdefault(r["location"], r)
        return list(found.values())
    finally:
        sock.close()


# ---- Geraetebeschreibung --------------------------------------------------------


@dataclass
class Service:
    type: str
    id: str
    control_url: str
    event_url: str | None = None
    scpd_url: str | None = None


@dataclass
class DeviceInfo:
    location: str
    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    udn: str = ""
    device_type: str = ""
    services: dict[str, Service] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.location).hostname or ""


def http_get(url: str, timeout: float = 5.0) -> bytes:
    """GET ohne Weiterleitungen, Rumpf begrenzt, timeout gilt je Operation und als Gesamtfrist."""
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    status, data = _fetch(req, timeout)
    if not 200 <= status < 300:
        msg = "Weiterleitung nicht erlaubt" if 300 <= status < 400 else "HTTP-Fehler"
        raise urllib.error.HTTPError(url, status, msg, None, None)
    return data


def parse_description(xml_bytes: bytes, location: str) -> DeviceInfo:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise UpnpError(None, "Geraetebeschreibung ist kein gueltiges XML") from None
    check_url(location, what="LOCATION")
    base = location
    ub = root.find("{*}URLBase")
    if ub is not None and (ub.text or "").strip():
        base = check_url(ub.text.strip(), what="URLBase", anchor=location)
    dev = root.find(".//{*}device")
    if dev is None:
        raise UpnpError(None, "Geraetebeschreibung ohne <device>")

    def txt(tag):
        e = dev.find(f"{{*}}{tag}")
        return (e.text or "").strip() if e is not None else ""

    info = DeviceInfo(
        location=location,
        friendly_name=txt("friendlyName"),
        manufacturer=txt("manufacturer"),
        model_name=txt("modelName"),
        udn=txt("UDN"),
        device_type=txt("deviceType"),
    )
    for s in root.iter():
        if _local(s.tag) != "service":
            continue
        d = {_local(c.tag): (c.text or "").strip() for c in s}
        if d.get("serviceType") and d.get("controlURL"):

            def join(key):
                return check_url(urllib.parse.urljoin(base, d[key]), what=key, anchor=location) if d.get(key) else None

            info.services[d["serviceType"]] = Service(
                type=d["serviceType"],
                id=d.get("serviceId", ""),
                control_url=join("controlURL"),
                event_url=join("eventSubURL"),
                scpd_url=join("SCPDURL"),
            )
    return info


# ---- SOAP ---------------------------------------------------------------------


def parse_fault(data: bytes, http_status: int) -> UpnpError:
    try:
        root = ET.fromstring(data)
        err = root.find(".//{*}UPnPError")
        if err is not None:
            code = err.find("{*}errorCode")
            desc = err.find("{*}errorDescription")
            return UpnpError(int(code.text) if code is not None and code.text else None, (desc.text or "") if desc is not None else "")
    except ET.ParseError:
        pass
    return UpnpError(None, f"HTTP {http_status}: {data[:200].decode('utf-8', 'replace')}")


def parse_response(data: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise UpnpError(None, "SOAP-Antwort ist kein gueltiges XML") from None
    body = root.find("{*}Body")
    if body is None or len(body) == 0:
        raise UpnpError(None, "SOAP-Antwort ohne Body")
    resp = body[0]
    if _local(resp.tag) == "Fault":
        raise parse_fault(data, 500)
    return {_local(c.tag): (c.text or "") for c in resp}


def build_envelope(service_type: str, action: str, args: dict[str, str]) -> bytes:
    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{inner}</u:{action}></s:Body></s:Envelope>'
    ).encode("utf-8")


# ---- Zeit, DIDL, MIME -------------------------------------------------------------


def fmt_hms(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def parse_hms(text: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*", text or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


_MIME_BY_EXT = {
    ".flac": ["audio/flac", "audio/x-flac"],
    ".wav": ["audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"],
    ".mp3": ["audio/mpeg"],
    ".ogg": ["audio/ogg"],
    ".aif": ["audio/aiff", "audio/x-aiff"],
    ".aiff": ["audio/aiff", "audio/x-aiff"],
}


# Formatnamen von libsndfile (soundfile) -> uebliche Endung
_EXT_BY_FORMAT = {"FLAC": ".flac", "WAV": ".wav", "AIFF": ".aif", "OGG": ".ogg", "MPEG": ".mp3"}


def ext_for_format(fmt: str | None) -> str | None:
    return _EXT_BY_FORMAT.get((fmt or "").upper())


def pick_mime(ext: str, sink_mimes: list[str] | None = None, fmt: str | None = None) -> tuple[str, bool | None]:
    """MIME-Typ nach Dateiinhalt (fmt, vom Header gelesen), sonst nach Endung. Meldet der Renderer Sink-Typen,
    wird einer davon bevorzugt.

    Rueckgabe: (mime, in_sink) mit in_sink=None, wenn keine Sink-Liste vorliegt.
    """
    key = ext_for_format(fmt) or ext
    cands = _MIME_BY_EXT.get(key.lower(), ["application/octet-stream"])
    if sink_mimes:
        low = {m.lower() for m in sink_mimes}
        for c in cands:
            if c in low:
                return c, True
        return cands[0], False
    return cands[0], None


def protocol_info(mime: str, plain: bool = False, live: bool = False) -> str:
    return f"http-get:*:{mime}:{'*' if plain else (DLNA_FEATURES_LIVE if live else DLNA_FEATURES)}"


def build_didl(
    title: str,
    url: str,
    mime: str,
    *,
    artist: str | None = None,
    album: str | None = None,
    duration_s: float | None = None,
    samplerate: int | None = None,
    channels: int | None = None,
    bits: int | None = None,
    size: int | None = None,
    plain: bool = False,
    live: bool = False,
) -> str:
    attrs = [f'protocolInfo="{escape(protocol_info(mime, plain, live), {chr(34): "&quot;"})}"']
    if duration_s is not None:
        attrs.append(f'duration="{fmt_hms(duration_s)}"')
    if samplerate:
        attrs.append(f'sampleFrequency="{samplerate}"')
    if channels:
        attrs.append(f'nrAudioChannels="{channels}"')
    if bits:
        attrs.append(f'bitsPerSample="{bits}"')
    if size:
        attrs.append(f'size="{size}"')
    extra = ""
    if artist:
        extra += f"<upnp:artist>{escape(artist)}</upnp:artist><dc:creator>{escape(artist)}</dc:creator>"
    if album:
        extra += f"<upnp:album>{escape(album)}</upnp:album>"
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        f"{extra}"
        f"<upnp:class>{'object.item.audioItem.audioBroadcast' if live else 'object.item.audioItem.musicTrack'}</upnp:class>"
        f'<res {" ".join(attrs)}>{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


# ---- Renderer -------------------------------------------------------------------


class Renderer:
    def __init__(self, info: DeviceInfo, timeout: float = 8.0):
        self.info = info
        self.timeout = timeout

    @classmethod
    def from_location(cls, location: str, timeout: float = 8.0) -> "Renderer":
        check_url(location, what="Renderer-URL")  # auch die direkt gewaehlte URL: nur http, keine Userinfo
        info = parse_description(http_get(location, timeout), location)
        return cls(info, timeout)

    def _svc(self, service_type: str) -> Service:
        svc = self.info.services.get(service_type)
        if svc is None:
            raise UpnpError(None, f"Dienst {service_type} fehlt in der Geraetebeschreibung")
        return svc

    def soap(self, service_type: str, action: str, args: dict[str, str] | None = None) -> dict[str, str]:
        svc = self._svc(service_type)
        check_url(svc.control_url, what="controlURL", anchor=self.info.location or None)
        req = urllib.request.Request(
            svc.control_url,
            data=build_envelope(service_type, action, args or {}),
            method="POST",
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{service_type}#{action}"',
                "User-Agent": UA,
            },
        )
        # Auch getrennte Renderer-Objekte duerfen keine parallelen SOAP-Aufrufe senden.
        with RENDERER_LOCK:
            try:
                status, data = _fetch(req, self.timeout)
            except http.client.HTTPException as e:
                raise UpnpError(None, f"HTTP-Antwort unvollstaendig ({type(e).__name__})") from None
        if not 200 <= status < 300:
            raise parse_fault(data, status)
        return parse_response(data)

    # AVTransport
    def set_uri(self, uri: str, didl: str = "") -> None:
        self.soap(SVC_AVT, "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": uri, "CurrentURIMetaData": didl})

    def play(self) -> None:
        self.soap(SVC_AVT, "Play", {"InstanceID": "0", "Speed": "1"})

    def pause(self) -> None:
        self.soap(SVC_AVT, "Pause", {"InstanceID": "0"})

    def stop(self) -> None:
        self.soap(SVC_AVT, "Stop", {"InstanceID": "0"})

    def transport_info(self) -> dict[str, str]:
        return self.soap(SVC_AVT, "GetTransportInfo", {"InstanceID": "0"})

    def transport_state(self) -> str:
        return self.transport_info().get("CurrentTransportState", "")

    def position_info(self) -> dict[str, str]:
        return self.soap(SVC_AVT, "GetPositionInfo", {"InstanceID": "0"})

    def position_s(self) -> float | None:
        return parse_hms(self.position_info().get("RelTime", ""))

    # ConnectionManager
    def sink_protocol_infos(self) -> list[str]:
        sink = self.soap(SVC_CM, "GetProtocolInfo").get("Sink", "")
        return [s.strip() for s in sink.split(",") if s.strip()]

    def sink_mimes(self) -> list[str]:
        mimes = []
        for entry in self.sink_protocol_infos():
            parts = entry.split(":")
            if len(parts) >= 3 and parts[2] not in mimes:
                mimes.append(parts[2])
        return mimes
