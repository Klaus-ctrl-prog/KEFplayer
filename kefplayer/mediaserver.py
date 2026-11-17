"""HTTP-Medienserver fuer den Renderer: nur registrierte Dateien, Range-Unterstuetzung, Anfrageprotokoll."""

from __future__ import annotations

import http.server
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from typing import Callable

from .upnp import DLNA_FEATURES, DLNA_FEATURES_LIVE


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Einzelner Byte-Bereich -> (start, ende) inklusiv. None = Header ignorieren (ganze Datei senden).

    Mehrfachbereiche, fremde Einheiten und ungueltige Angaben werden ignoriert (RFC 7233 erlaubt das).
    """
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m:
        return None
    a, b = m.groups()
    if a == "" and b == "":
        return None
    if a == "":  # Suffix: letzte n Bytes
        n = int(b)
        if n == 0:
            raise RangeNotSatisfiable
        return (max(size - n, 0), size - 1) if size else (0, -1)
    start = int(a)
    if start >= size:
        raise RangeNotSatisfiable
    end = size - 1 if b == "" else min(int(b), size - 1)
    if end < start:
        return None
    return (start, end)


@dataclass
class _Entry:
    path: Path
    mime: str
    name: str


@dataclass
class _StreamEntry:
    opener: Callable[[], object]  # liefert ein Objekt mit read/read1 und close
    mime: str
    name: str


def local_ip_for(target_host: str, port: int = 9) -> str:
    """Eigene IP auf der Route zum Zielgeraet (es wird nichts gesendet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, port))
        return s.getsockname()[0]
    finally:
        s.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kefplayer"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _stream_entry(self):
        parts = urllib.parse.urlsplit(self.path).path.split("/")
        if len(parts) == 4 and parts[0] == "" and parts[1] == "s":
            return self.server.media.streams.get(parts[2]), parts[2]
        return None, None

    def _serve_stream(self, entry, token: str, head: bool) -> None:
        """Live-Stream: keine Laenge, kein Seek; die Antwort endet mit dem Schliessen der Verbindung."""
        media = self.server.media
        status, sent = 200, 0
        src = None
        try:
            if not head:
                try:
                    src = entry.opener()
                except Exception:  # noqa: BLE001 - Quelle nicht erreichbar: dem Renderer 502 melden
                    status = 502
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
            self.send_response(200)
            self.send_header("Content-Type", entry.mime)
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", DLNA_FEATURES_LIVE)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if head:
                return
            read = getattr(src, "read1", None) or src.read  # read1: nicht auf volle Blockgroesse warten
            while token in media.streams:
                chunk = read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # Renderer oder Sender beendet die Verbindung
        finally:
            if src is not None:
                try:
                    src.close()
                except Exception:  # noqa: BLE001
                    pass
            media._log_request({
                "ts": time.time(), "client": self.client_address[0], "method": "HEAD" if head else "GET",
                "token": token, "name": entry.name, "range": self.headers.get("Range"),
                "status": status, "bytes_sent": sent,
            })

    def _entry(self):
        parts = urllib.parse.urlsplit(self.path).path.split("/")
        if len(parts) == 4 and parts[0] == "" and parts[1] == "m":
            return self.server.media.entries.get(parts[2]), parts[2]
        return None, None

    def _serve(self, head: bool):
        media = self.server.media
        s_entry, s_token = self._stream_entry()
        if s_entry is not None:
            return self._serve_stream(s_entry, s_token, head)
        entry, token = self._entry()
        rng_header = self.headers.get("Range")
        status, sent = 404, 0
        try:
            if entry is None or not entry.path.is_file():
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                return
            size = entry.path.stat().st_size
            try:
                rng = parse_range(rng_header, size)
            except RangeNotSatisfiable:
                status = 416
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            length = end - start + 1
            status = 206 if rng else 200
            self.send_response(status)
            self.send_header("Content-Type", entry.mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", DLNA_FEATURES)
            self.end_headers()
            if head:
                return
            with entry.path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Renderer schliesst Verbindungen gerne frueh
        finally:
            media._log_request(
                {
                    "ts": time.time(),
                    "client": self.client_address[0],
                    "method": "HEAD" if head else "GET",
                    "token": token,
                    "name": entry.name if entry else None,
                    "range": rng_header,
                    "status": status,
                    "bytes_sent": sent,
                }
            )


class MediaServer:
    def __init__(self, bind: str = "0.0.0.0", port: int = 8765, advertise_host: str | None = None):
        self.bind = bind
        self.requested_port = port
        self.advertise_host = advertise_host
        self.entries: dict[str, _Entry] = {}
        self.streams: dict[str, _StreamEntry] = {}
        self.request_log: list[dict] = []
        self._lock = threading.Lock()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = port

    def start(self) -> None:
        self._httpd = http.server.ThreadingHTTPServer((self.bind, self.requested_port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.media = self
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _host(self) -> str:
        host = self.advertise_host or self.bind
        if host in ("0.0.0.0", ""):
            raise ValueError("advertise_host muss gesetzt sein, wenn an 0.0.0.0 gebunden wird")
        return host

    def register(self, path: str | Path, mime: str, name: str | None = None) -> tuple[str, str]:
        """Gibt (url, token) zurueck. Die URL endet auf den Dateinamen (manche Renderer lesen die Endung)."""
        path = Path(path)
        name = name or path.name
        token = secrets.token_hex(12)
        with self._lock:
            self.entries[token] = _Entry(path, mime, name)
        return f"http://{self._host()}:{self.port}/m/{token}/{urllib.parse.quote(name)}", token

    def unregister(self, token: str) -> None:
        with self._lock:
            self.entries.pop(token, None)

    def register_stream(self, opener: Callable[[], object], mime: str, name: str) -> tuple[str, str]:
        """Live-Stream registrieren. ``opener`` oeffnet die Quelle erst beim GET des Renderers."""
        token = secrets.token_hex(12)
        with self._lock:
            self.streams[token] = _StreamEntry(opener, mime, name)
        return f"http://{self._host()}:{self.port}/s/{token}/{urllib.parse.quote(name)}", token

    def unregister_stream(self, token: str) -> None:
        with self._lock:
            self.streams.pop(token, None)

    def _log_request(self, rec: dict) -> None:
        with self._lock:
            self.request_log.append(rec)

    def requests_for(self, token: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.request_log if r["token"] == token]
