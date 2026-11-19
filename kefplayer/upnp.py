"""UPnP/DLNA-Steuerung: SSDP-Suche, Geraetebeschreibung, SOAP-Aktionen, DIDL-Lite.

Nur Standardbibliothek. Es wird bewusst nichts an der Lautstaerke des Renderers veraendert.
Alle Verbindungen laufen ohne Proxy (Geraetesteuerung im LAN).
"""

from __future__ import annotations

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

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UpnpError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"UPnP-Fehler {code}: {description}" if code is not None else description)
        self.code = code
        self.description = description


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---- SSDP ---------------------------------------------------------------------


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
            if r and r.get("location"):
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
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def parse_description(xml_bytes: bytes, location: str) -> DeviceInfo:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise UpnpError(None, "Geraetebeschreibung ist kein gueltiges XML") from None
    base = location
    ub = root.find("{*}URLBase")
    if ub is not None and (ub.text or "").strip():
        base = ub.text.strip()
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
            info.services[d["serviceType"]] = Service(
                type=d["serviceType"],
                id=d.get("serviceId", ""),
                control_url=urllib.parse.urljoin(base, d["controlURL"]),
                event_url=urllib.parse.urljoin(base, d["eventSubURL"]) if d.get("eventSubURL") else None,
                scpd_url=urllib.parse.urljoin(base, d["SCPDURL"]) if d.get("SCPDURL") else None,
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
        info = parse_description(http_get(location, timeout), location)
        return cls(info, timeout)

    def _svc(self, service_type: str) -> Service:
        svc = self.info.services.get(service_type)
        if svc is None:
            raise UpnpError(None, f"Dienst {service_type} fehlt in der Geraetebeschreibung")
        return svc

    def soap(self, service_type: str, action: str, args: dict[str, str] | None = None) -> dict[str, str]:
        svc = self._svc(service_type)
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
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    data = resp.read()
            except urllib.error.HTTPError as e:
                raise parse_fault(e.read(), e.code) from None
            except http.client.HTTPException as e:
                raise UpnpError(None, f"HTTP-Antwort unvollstaendig ({type(e).__name__})") from None
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
