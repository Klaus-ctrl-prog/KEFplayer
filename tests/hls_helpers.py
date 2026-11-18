"""Bausteine fuer HLS-Tests: Segmentbau (ADTS, MPEG-TS, fMP4) und ein Fake-HLS-Server (lokal, kein Internet)."""
import http.server
import struct
import threading

from radio_helpers import adts_frame

SR = 44100
FRAMES_PER_S = SR / 1024.0


def adts_es(kbps, seconds, sr_idx=4):
    """ADTS-Strom mit (nahezu) fester Bitrate: Rahmenlaenge aus kbps, Rahmenzahl aus seconds."""
    flen = round(kbps * 1000 / 8 / FRAMES_PER_S)
    n = round(seconds * FRAMES_PER_S)
    return b"".join(adts_frame(payload_len=flen - 7, sr_idx=sr_idx) for _ in range(n))


def _ts_packet(pid, payload, pusi, cc):
    hdr = bytearray([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, 0x10 | (cc & 15)])
    pad = 184 - len(payload)
    if pad > 0:
        hdr[3] = 0x30 | (cc & 15)
        af = b"\x00" if pad == 1 else bytes([pad - 1, 0x00]) + b"\xff" * (pad - 2)
        return bytes(hdr) + af + payload
    return bytes(hdr) + payload


def _psi(pid, section):
    return _ts_packet(pid, (b"\x00" + section).ljust(184, b"\xff"), True, 0)


def ts_segment(es, stream_type=0x0F, apid=0x101):
    pat = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0]) + struct.pack(">HH", 1, 0xE000 | 0x1000) + b"\0\0\0\0"
    pmt_body = struct.pack(">HBBBH", 1, 0xC1, 0, 0, 0xE000 | apid) + struct.pack(">H", 0xF000)
    pmt_body += bytes([stream_type]) + struct.pack(">HH", 0xE000 | apid, 0xF000)
    pmt = bytes([0x02, 0xB0 | ((len(pmt_body) + 4) >> 8), (len(pmt_body) + 4) & 0xFF]) + pmt_body + b"\0\0\0\0"
    out = [_psi(0, pat), _psi(0x1000, pmt)]
    pes = b"\x00\x00\x01\xc0\x00\x00\x80\x00\x00" + es
    cc = 0
    for i in range(0, len(pes), 184):
        chunk = pes[i:i + 184]
        out.append(_ts_packet(apid, chunk, i == 0, cc))
        cc += 1
    return b"".join(out)


def _box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def mp4_init(codec="aac", oti=0x40, aot=2, sr=SR):
    if codec == "flac":
        entry = _box(b"fLaC", bytes(28))
    else:
        asc = bytes([(aot << 3) | 4 >> 1, 0x10])
        dec_specific = bytes([0x05, len(asc)]) + asc
        dcd = bytes([0x04, 13 + len(dec_specific), oti, 0x15]) + bytes(3) + bytes(4) + bytes(4) + dec_specific
        esd = bytes([0x03, 3 + len(dcd), 0, 0, 0]) + dcd
        esds = _box(b"esds", bytes(4) + esd)
        entry = _box(b"mp4a", bytes(6) + struct.pack(">H", 1) + bytes(8) + struct.pack(">HH", 2, 16) + bytes(4)
                     + struct.pack(">I", sr << 16) + esds)
    return _box(b"ftyp", b"iso6" + bytes(8)) + _box(b"moov", _box(b"stsd", bytes(8) + entry))


def mp4_segment(kbps, seconds):
    payload = bytes(round(kbps * 1000 / 8 * seconds))
    return _box(b"moof", bytes(16)) + _box(b"mdat", payload)


def media_playlist(names, dur=6.0, map_name=None, extra=""):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(dur) + 1}", "#EXT-X-MEDIA-SEQUENCE:7", extra]
    if map_name:
        lines.append(f'#EXT-X-MAP:URI="{map_name}"')
    for n in names:
        lines += [f"#EXTINF:{dur:.3f},", n]
    return "\n".join(x for x in lines if x) + "\n"


def master_playlist(variants):
    """variants: [(uri, bandwidth, codecs or None)]"""
    out = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for uri, bw, codecs in variants:
        c = f',CODECS="{codecs}"' if codecs else ""
        out += [f"#EXT-X-STREAM-INF:BANDWIDTH={bw},AVERAGE-BANDWIDTH={bw}{c}", uri]
    return "\n".join(out) + "\n"


class FakeHls:
    """Lokaler HTTP-Server mit festen Pfaden: routes = {"/a.m3u8": (content_type, bytes)}."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                outer.requests.append(self.path)
                r = outer.routes.get(path)
                if r is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", r[0])
                self.send_header("Content-Length", str(len(r[1])))
                self.end_headers()
                try:
                    # in kleinen Stuecken senden: ein einzelnes grosses Senden stockt auf Windows-Loopback gelegentlich
                    for i in range(0, len(r[1]), 16384):
                        self.wfile.write(r[1][i:i + 16384])
                except OSError:
                    pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def url(self, path="/live/master.m3u8"):
        return f"http://127.0.0.1:{self.port}{path}?token=GEHEIM123"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


MPEGURL = "application/vnd.apple.mpegurl"


def standard_routes(kbps_by_variant, container="ts", labels=None, codecs="mp4a.40.2", seconds=6.0, nseg=3):
    """Master + je Variante eine Medien-Playlist mit nseg Segmenten. kbps_by_variant: {name: tatsaechliche kbps}.
    labels: {name: deklarierte Bandbreite in bit/s} (Standard: tatsaechlich * 1100)."""
    routes = {}
    variants = []
    for name, kbps in kbps_by_variant.items():
        names = [f"{name}_{i}.{'ts' if container == 'ts' else 'm4s'}" for i in range(nseg)]
        for n in names:
            if container == "ts":
                body = ts_segment(adts_es(kbps, seconds))
            else:
                body = mp4_segment(kbps, seconds)
            routes[f"/live/{n}"] = ("video/mp2t" if container == "ts" else "audio/mp4", body)
        if container != "ts":
            routes[f"/live/{name}_init.mp4"] = ("audio/mp4", mp4_init())
        routes[f"/live/{name}.m3u8"] = (MPEGURL, media_playlist(names, seconds, f"{name}_init.mp4" if container != "ts" else None).encode())
        bw = (labels or {}).get(name, int(kbps * 1100))
        variants.append((f"{name}.m3u8", bw, codecs))
    routes["/live/master.m3u8"] = (MPEGURL, master_playlist(variants).encode())
    return routes
