"""Fake-Sender fuer Radio-Tests (lokaler HTTP-Server, kein Internet)."""
import http.server
import threading
import time


class FakeStation:
    def __init__(self, body=b"", content_type="audio/aac", status=200, endless_chunk=None, chunk_delay=0.01):
        self.body, self.content_type, self.status = body, content_type, status
        self.endless_chunk, self.chunk_delay = endless_chunk, chunk_delay
        self.requests = []   # {"path", "headers"}
        self.open_streams = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                self.send_response(outer.status)
                if outer.content_type:
                    self.send_header("Content-Type", outer.content_type)
                self.end_headers()
                if outer.status != 200:
                    return
                outer.open_streams += 1
                try:
                    if outer.endless_chunk:
                        while True:
                            self.wfile.write(outer.endless_chunk)
                            self.wfile.flush()
                            time.sleep(outer.chunk_delay)
                    else:
                        self.wfile.write(outer.body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    outer.open_streams -= 1

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/live/stream?token=GEHEIM123"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


# ---- Bausteine fuer Qualitaetstests (kein Netzwerk, keine Uhr) ---------------------------

def adts_frame(payload_len=300, object_type=2, sr_idx=4, channels=2):
    """Ein ADTS-Frame (7-Byte-Kopf + Nutzdaten aus Nullen)."""
    flen = 7 + payload_len
    b = bytearray(7)
    b[0] = 0xFF
    b[1] = 0xF1                      # Sync, MPEG-4, Layer 00, ohne CRC
    b[2] = ((object_type - 1) << 6) | (sr_idx << 2) | (channels >> 2)
    b[3] = ((channels & 3) << 6) | (flen >> 11)
    b[4] = (flen >> 3) & 0xFF
    b[5] = ((flen & 7) << 5) | 0x1F
    b[6] = 0xFC
    return bytes(b) + bytes(payload_len)


def adts_stream(n_frames=20, **kw):
    return b"".join(adts_frame(**kw) for _ in range(n_frames))


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class RateSource:
    """Liefert Daten mit fester Bitrate gegen eine FakeClock (jeder read() = 'chunk' Bytes, Zeit laeuft entsprechend)."""

    def __init__(self, clock, kbps, head=b"", burst=0, total_s=None, chunk=4096):
        self.clock, self.kbps, self.chunk = clock, kbps, chunk
        self.head, self.burst, self.total_s = head, burst, total_s
        self.closed = False
        self._first = True
        self._burst_sent = False
        self._reads = 0

    def read(self, n=None):
        if self.total_s is not None and self.clock.t >= self.total_s:
            return b""
        if self._first:
            self._first = False
            d = (self.head + bytes(max(0, self.chunk - len(self.head))))[:max(self.chunk, len(self.head))]
            return d
        self._reads += 1
        if self.burst and self._reads == 2 and not self._burst_sent:
            self._burst_sent = True          # Puffer-Burst kurz nach dem Verbinden: Daten ohne Zeitfortschritt
            return bytes(self.burst)
        d = bytes(self.chunk)
        self.clock.t += len(d) * 8 / (self.kbps * 1000.0)
        return d

    def close(self):
        self.closed = True
