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
