"""HLS (.m3u8) fuer die ffmpeg-Stufe E1b (nur mit ``--ffmpeg``; E1 bleibt ohne HLS).

Ablauf (docs/radio-e1b-design.md, Abschnitt 4: "die hoechste Stufe muss erzwungen werden, und die
Qualitaetspruefung gilt fuer die tatsaechlich gewaehlte Variante"):

1. Master-Playlist holen, Audio-Varianten lesen, die **hoechste** zulaessige Variante waehlen.
2. Genau diese Variante pruefen: einige Segmente werden geladen und die Bitrate aus den **Nutzdaten**
   gemessen (ADTS-Rahmen, TS-Audiostrom bzw. ``mdat``). Der Codec kommt aus den Segmentdaten, nie nur aus
   dem ``CODECS``-Attribut; deklarierte ``BANDWIDTH`` zaehlt nicht (sie enthaelt Container-Overhead).
3. Bewertung mit derselben Regel wie bei allen Streams (``quality.evaluate``, E-6).
4. ffmpeg bekommt die **URL der gewaehlten Variante** (Medien-Playlist), nicht die Master-Playlist, und kann
   daher keine andere Stufe waehlen.

Sicherheit: nur http/https (jede URL aus der Playlist wird mit ``validate_url`` geprueft, kein Wechsel von
https auf http), Groessen- und Zeitgrenzen, keine URL in Meldungen (nur ``mask_url``). Verschluesselte
Playlists (``EXT-X-KEY``) sind nicht pruefbar und werden abgelehnt. Nichts wird gespeichert.
"""
from __future__ import annotations

import re
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from . import quality
from .radio import (CONNECT_TIMEOUT_S, HLS_MIMES, RadioError, StreamInfo, mask_url, open_stream,
                    validate_url)

HLS_MIME = "application/vnd.apple.mpegurl"
MAX_PLAYLIST_BYTES = 512 * 1024
MAX_SEGMENT_BYTES = 6 * 1024 * 1024
MAX_SEGMENTS = 10               # hoechstens so viele Segmente werden zur Messung geladen
MAX_VARIANTS = 64
FETCH_DEADLINE_S = 60.0         # Gesamtzeit je Abruf (gegen tropfende Server)

Fetch = Callable[[str, int], "tuple[bytes, str]"]   # (url, max_bytes) -> (daten, endgueltige_url)


# ---------------------------------------------------------------- Abruf

def http_fetch(url: str, max_bytes: int, *, timeout: float = CONNECT_TIMEOUT_S,
               deadline_s: float = FETCH_DEADLINE_S) -> tuple[bytes, str]:
    """Begrenzter Abruf. Wirft RadioError (Meldungen ohne volle URL)."""
    resp = open_stream(url, timeout)
    try:
        final = resp.geturl() if hasattr(resp, "geturl") else url
        t0 = time.monotonic()
        buf = bytearray()
        while len(buf) <= max_bytes:
            if time.monotonic() - t0 > deadline_s:
                raise RadioError("stream_unreachable", f"HLS-Abruf zu langsam ({mask_url(url)}).")
            try:
                d = resp.read(min(65536, max_bytes + 1 - len(buf)))
            except OSError as e:
                raise RadioError("stream_unreachable", f"HLS-Abruf unterbrochen ({mask_url(url)}): {type(e).__name__}.") from None
            if not d:
                break
            buf += d
        if len(buf) > max_bytes:
            raise RadioError("stream_format_unsupported", f"HLS-Antwort zu gross ({mask_url(url)}).")
        return bytes(buf), final
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def is_hls_url(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(url).path.lower().endswith(".m3u8")
    except ValueError:
        return False


def _resolve(base: str, ref: str) -> str:
    """Relative Referenz aufloesen und pruefen. Kein Wechsel von https auf http."""
    u = validate_url(urllib.parse.urljoin(base, ref.strip()))
    if urllib.parse.urlsplit(base).scheme == "https" and urllib.parse.urlsplit(u).scheme != "https":
        raise RadioError("stream_url_invalid", f"HTTPS-Playlist verweist auf eine unverschluesselte http-URL ({mask_url(base)}).")
    return u


# ---------------------------------------------------------------- Playlists

_ATTR = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(s: str) -> dict[str, str]:
    return {k.upper(): v.strip().strip('"') for k, v in _ATTR.findall(s)}


@dataclass
class Variant:
    url: str
    bandwidth: int | None = None
    avg_bandwidth: int | None = None
    codecs: tuple[str, ...] = ()
    video: bool = False

    @property
    def rank(self) -> int:
        return self.avg_bandwidth or self.bandwidth or 0

    def codec_hint(self) -> tuple[str | None, str | None]:
        return codec_hint(self.codecs)


@dataclass
class MediaPlaylist:
    segments: list[tuple[str, float]] = field(default_factory=list)   # (url, Dauer s)
    map_url: str | None = None
    encrypted: bool = False
    endlist: bool = False


def _to_int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def parse_playlist(text: str, base_url: str):
    """Gibt ("master", [Variant]) oder ("media", MediaPlaylist) zurueck. Wirft RadioError."""
    text = text.lstrip("﻿")
    lines = [ln.strip() for ln in text.splitlines()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise RadioError("stream_format_unsupported", f"Keine HLS-Playlist ({mask_url(base_url)}).")
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        variants: list[Variant] = []
        pending: dict | None = None
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF:"):
                pending = _attrs(ln.split(":", 1)[1])
            elif ln and not ln.startswith("#") and pending is not None:
                codecs = tuple(c.strip() for c in pending.get("CODECS", "").split(",") if c.strip())
                video = "RESOLUTION" in pending or any(c.lower().startswith(("avc", "hvc", "hev", "vp", "av01")) for c in codecs)
                variants.append(Variant(_resolve(base_url, ln), _to_int(pending.get("BANDWIDTH")),
                                        _to_int(pending.get("AVERAGE-BANDWIDTH")), codecs, video))
                pending = None
                if len(variants) >= MAX_VARIANTS:
                    break
        return "master", variants
    pl = MediaPlaylist()
    dur: float | None = None
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                dur = None
        elif ln.startswith("#EXT-X-MAP:"):
            u = _attrs(ln.split(":", 1)[1]).get("URI")
            if u:
                pl.map_url = _resolve(base_url, u)
        elif ln.startswith("#EXT-X-KEY:"):
            if _attrs(ln.split(":", 1)[1]).get("METHOD", "NONE").upper() != "NONE":
                pl.encrypted = True
        elif ln.startswith("#EXT-X-ENDLIST"):
            pl.endlist = True
        elif ln and not ln.startswith("#"):
            if dur is not None and dur > 0:
                pl.segments.append((_resolve(base_url, ln), dur))
            dur = None
    return "media", pl


def classify_codec(token: str) -> tuple[str, str | None]:
    """Ein CODECS-Eintrag (RFC 6381) -> (codec, profil). codec: aac | flac | mp3 | other."""
    t = token.strip().lower()
    if t.startswith("mp4a.40."):
        aot = t.rsplit(".", 1)[1]
        if aot in ("5", "29"):
            return "aac", "HE-AAC"
        if aot == "34":
            return "mp3", None
        return "aac", {"1": "Main", "2": "LC", "3": "SSR", "4": "LTP"}.get(aot, f"AOT{aot}")
    if t in ("mp4a.6b", "mp4a.69", "mp3"):
        return "mp3", None
    if t == "flac":
        return "flac", None
    return "other", None


def codec_hint(codecs: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(codec, profil) aus CODECS; (None, None) wenn das Attribut fehlt."""
    if not codecs:
        return None, None
    found = [classify_codec(c) for c in codecs]
    for c, p in found:
        if c in ("aac", "flac"):
            return c, p
    return found[0]


def select_variant(variants: list[Variant]) -> Variant:
    """Hoechste zulaessige Audio-Variante. MP3, Video und sonstige Codecs scheiden aus; ohne CODECS-Angabe
    bleibt die Variante Kandidat (die Segmentpruefung entscheidet)."""
    ok = [v for v in variants if not v.video and v.codec_hint()[0] in (None, "aac", "flac")]
    if not ok:
        raise RadioError("stream_format_unsupported", "HLS: keine zulaessige Audio-Variante (nur AAC oder FLAC; MP3/Video/andere Codecs ausgeschlossen).")
    return max(ok, key=lambda v: (v.rank, v.codec_hint()[0] == "flac"))


# ---------------------------------------------------------------- Segment-Analyse

def _skip_id3(buf: bytes) -> bytes:
    while buf[:3] == b"ID3" and len(buf) >= 10:
        size = (buf[6] & 0x7F) << 21 | (buf[7] & 0x7F) << 14 | (buf[8] & 0x7F) << 7 | (buf[9] & 0x7F)
        buf = buf[10 + size:]
    return buf


def adts_walk(buf: bytes) -> dict | None:
    """Ketten von ADTS-Rahmen ab dem ersten bestaetigten Kopf. None, wenn weniger als 5 Rahmen."""
    n = len(buf)
    for start in range(min(n, 4096)):
        if buf[start] != 0xFF or (buf[start + 1:start + 2] and (buf[start + 1] & 0xF6) != 0xF0):
            continue
        hdr = quality._adts_header(buf, start)  # noqa: SLF001
        if not hdr or hdr["frame_len"] < 7:
            continue
        i, frames, total = start, 0, 0
        while i + 7 <= n and buf[i] == 0xFF and (buf[i + 1] & 0xF6) == 0xF0:
            h = quality._adts_header(buf, i)  # noqa: SLF001
            if not h or h["frame_len"] < 7 or h["sr_idx"] != hdr["sr_idx"] or i + h["frame_len"] > n:
                break
            frames += 1
            total += h["frame_len"]
            i += h["frame_len"]
        if frames >= 5:
            return {"frames": frames, "bytes": total, "sample_rate": hdr["sample_rate"],
                    "channels": hdr["channels"], "object_type": hdr["object_type"]}
    return None


def ts_demux(data: bytes) -> tuple[int, bytes] | None:
    """Audio-Elementarstrom aus einem MPEG-TS-Segment: (stream_type, bytes). Annahme: PAT/PMT je in einem Paket."""
    pkts = [data[i:i + 188] for i in range(0, len(data) - 187, 188)]
    if not pkts or any(p[0] != 0x47 for p in pkts[:8]):
        return None

    def payload(p: bytes) -> tuple[int, bool, bytes]:
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        off = 4
        if afc & 2:
            off += 1 + p[4]
        return pid, bool(p[1] & 0x40), (p[off:] if afc & 1 and off < 188 else b"")

    pmt_pid = None
    for p in pkts:
        pid, start, pl = payload(p)
        if pid == 0 and start and len(pl) > 12 and pl[1] == 0x00:
            sec = pl[1 + pl[0]:]
            end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
            for k in range(8, min(end, len(sec)) - 3, 4):
                prog, pid2 = struct.unpack(">HH", sec[k:k + 4])
                if prog != 0:
                    pmt_pid = pid2 & 0x1FFF
                    break
            break
    if pmt_pid is None:
        return None
    audio_pid = stype = None
    for p in pkts:
        pid, start, pl = payload(p)
        if pid == pmt_pid and start and len(pl) > 13:
            sec = pl[1 + pl[0]:]
            if not sec or sec[0] != 0x02 or len(sec) < 12:
                continue
            end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
            k = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            while k + 5 <= min(end, len(sec)):
                st = sec[k]
                epid = ((sec[k + 1] & 0x1F) << 8) | sec[k + 2]
                ilen = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
                if st in (0x0F, 0x11, 0x03, 0x04, 0x81, 0x87, 0x06) and audio_pid is None:
                    audio_pid, stype = epid, st
                k += 5 + ilen
            break
    if audio_pid is None:
        return None
    es = bytearray()
    for p in pkts:
        pid, start, pl = payload(p)
        if pid != audio_pid or not pl:
            continue
        if start:
            if pl[:3] != b"\x00\x00\x01" or len(pl) < 9:
                continue
            pl = pl[9 + pl[8]:]
        es += pl
    return stype, bytes(es)


def _boxes(buf: bytes):
    i, n = 0, len(buf)
    while i + 8 <= n:
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        hdr = 8
        if size == 1 and i + 16 <= n:
            size, hdr = struct.unpack(">Q", buf[i + 8:i + 16])[0], 16
        elif size == 0:
            size = n - i
        if size < hdr:
            return
        yield typ, buf[i + hdr:i + size]
        i += size


def _desc_len(b: bytes, i: int) -> tuple[int, int]:
    n = 0
    for _ in range(4):
        if i >= len(b):
            break
        c = b[i]
        i += 1
        n = (n << 7) | (c & 0x7F)
        if not c & 0x80:
            break
    return n, i


def parse_esds(b: bytes) -> tuple[int | None, int | None]:
    """(objectTypeIndication, audioObjectType) aus dem Inhalt einer 'esds'-Box (nach Version/Flags)."""
    oti = aot = None
    try:
        i = 0
        while i < len(b):
            tag = b[i]
            ln, j = _desc_len(b, i + 1)
            if tag == 0x03:
                flags = b[j + 2]
                i = j + 3 + (2 if flags & 0x80 else 0)
                if flags & 0x40:
                    i += 1 + b[i]
                if flags & 0x20:
                    i += 2
            elif tag == 0x04:
                oti = b[j]
                i = j + 13
            elif tag == 0x05:
                aot = b[j] >> 3
                if aot == 31:
                    aot = 32 + (((b[j] & 7) << 3) | (b[j + 1] >> 5))
                break
            else:
                i = j + ln
    except IndexError:
        pass
    return oti, aot


def sniff_mp4_init(init: bytes) -> dict | None:
    """Audio-Codec aus den Sample-Eintraegen einer fMP4-Initialisierung (oder eines Segments mit moov)."""
    for tag, codec in ((b"mp4a", "aac"), (b"fLaC", "flac"), (b".mp3", "mp3"), (b"Opus", "other"),
                       (b"ac-3", "other"), (b"ec-3", "other")):
        i = init.find(tag)
        if i < 4:
            continue
        out = {"codec": codec, "profile": None, "sample_rate": None, "channels": None}
        if tag == b"mp4a":
            e = init[i + 4:]
            if len(e) >= 28:
                out["channels"] = struct.unpack(">H", e[16:18])[0]
                out["sample_rate"] = struct.unpack(">I", e[24:28])[0] >> 16
            k = init.find(b"esds", i)
            oti, aot = parse_esds(init[k + 8:k + 8 + 64]) if k >= 0 else (None, None)
            if oti in (0x40, 0x66, 0x67, 0x68):
                out["profile"] = {1: "Main", 2: "LC", 3: "SSR", 4: "LTP", 5: "HE-AAC", 29: "HE-AAC"}.get(aot) if aot else None
            elif oti in (0x69, 0x6B):
                out["codec"] = "mp3"
            else:
                out["codec"] = "unknown"   # nicht eindeutig -> nie als gut werten
        return out
    return None


@dataclass
class SegInfo:
    codec: str = "unknown"
    profile: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    nbytes: int = 0
    audio_s: float | None = None     # aus den Rahmen berechnet; None -> EXTINF
    note: str | None = None


def analyze_segment(data: bytes, init: bytes = b"") -> SegInfo:
    data = _skip_id3(data)
    if data[:4] == b"fLaC":
        return SegInfo("flac", nbytes=len(data))
    if len(data) >= 376 and data[0] == 0x47 and data[188] == 0x47:
        dm = ts_demux(data)
        if dm is None:
            return SegInfo(note="TS ohne erkennbaren Audiostrom")
        st, es = dm
        if st == 0x0F:
            w = adts_walk(es)
            if w:
                return _adts_info(w)
            return SegInfo(note="TS-Audio ohne lesbare ADTS-Rahmen")
        if st == 0x11:
            return SegInfo("aac", nbytes=len(es), note="LATM: Profil nicht bestimmbar")
        if st in (0x03, 0x04):
            return SegInfo("mp3")
        return SegInfo("other")
    top = list(_boxes(data[:min(len(data), 1 << 20)])) if len(data) >= 8 else []
    if any(t in (b"moof", b"mdat") for t, _ in top):
        sniff = sniff_mp4_init(init or data)
        if not sniff:
            return SegInfo(note="fMP4 ohne erkennbare Initialisierung (EXT-X-MAP)")
        mdat = sum(len(p) for t, p in _boxes(data) if t == b"mdat")
        return SegInfo(sniff["codec"], sniff["profile"], sniff["sample_rate"], sniff["channels"], nbytes=mdat)
    w = adts_walk(data)
    if w:
        return _adts_info(w)
    return SegInfo(note="Segmentformat nicht erkannt")


def _adts_info(w: dict) -> SegInfo:
    prof = {1: "Main", 2: "LC", 3: "SSR", 4: "LTP"}.get(w["object_type"], f"AOT{w['object_type']}")
    return SegInfo("aac", prof, w["sample_rate"], w["channels"], nbytes=w["bytes"],
                   audio_s=w["frames"] * 1024.0 / w["sample_rate"])


# ---------------------------------------------------------------- Pruefung der gewaehlten Variante

def check_variant(variant_url: str, *, hint: tuple[str | None, str | None] = (None, None), declared_bw: int | None = None,
                  window_s: float = 10.0, fetch: Fetch | None = None) -> quality.StreamReport:
    """Segmente der Variante laden und messen. Der Codec kommt aus den Daten; ``hint`` (CODECS) darf nur widersprechen."""
    fetch = fetch or http_fetch
    rep = quality.StreamReport(source="hls")
    text, final = fetch(variant_url, MAX_PLAYLIST_BYTES)
    kind, pl = parse_playlist(text.decode("utf-8", "replace"), final)
    if kind != "media":
        rep.notes.append("HLS: Variante ist wieder eine Master-Playlist (verschachtelt, nicht unterstuetzt).")
        return rep
    if pl.encrypted:
        rep.notes.append("HLS: Segmente sind verschluesselt (EXT-X-KEY); Qualitaet nicht pruefbar.")
        return rep
    if not pl.segments:
        rep.notes.append("HLS: keine Segmente in der Medien-Playlist.")
        return rep
    chosen: list[tuple[str, float]] = []
    total = 0.0
    for seg in reversed(pl.segments):     # neueste zuerst (Live-Fenster laeuft vorn ab)
        chosen.append(seg)
        total += seg[1]
        if total >= window_s or len(chosen) >= MAX_SEGMENTS:
            break
    chosen.reverse()
    init = b""
    if pl.map_url:
        init, _ = fetch(pl.map_url, MAX_SEGMENT_BYTES)
    infos: list[tuple[SegInfo, float]] = []
    for url, dur in chosen:
        data, _ = fetch(url, MAX_SEGMENT_BYTES)
        infos.append((analyze_segment(data, init), dur))
    codecs = {i.codec for i, _ in infos}
    for i, _ in infos:
        if i.note and i.note not in rep.notes:
            rep.notes.append("HLS: " + i.note)
    if len(codecs) != 1:
        rep.notes.append("HLS: Segmente uneinheitlich oder nicht erkennbar.")
        return rep
    codec = codecs.pop()
    h_codec, h_prof = hint
    if h_codec is not None and h_codec != codec:
        rep.notes.append(f"HLS: CODECS-Angabe ({h_codec}) widerspricht den Segmentdaten ({codec}); abgelehnt.")
        return rep
    rep.codec = codec
    first = infos[0][0]
    rep.sample_rate, rep.channels = first.sample_rate, first.channels
    if codec == "aac":
        rep.profile = "HE-AAC" if "HE" in (h_prof or "").upper() or "HE" in (first.profile or "").upper() else first.profile
        rep.he_unknown = "HE" not in (rep.profile or "").upper()    # SBR/PS nur durch ffprobe oder CODECS sicher auszuschliessen
        nbytes = sum(i.nbytes for i, _ in infos)
        secs = sum((i.audio_s if i.audio_s is not None else d) for i, d in infos)
        if nbytes > 0 and secs >= 2.0:
            rep.measured_kbps = nbytes * 8 / secs / 1000.0
        else:
            rep.notes.append("HLS: zu wenig Audio fuer eine Bitratenmessung.")
    rep.notes.append(f"HLS: {len(infos)} Segment(e), gemessen aus den Nutzdaten"
                     + (f", deklariert {declared_bw // 1000} kbit/s Bandbreite (inkl. Container, zaehlt nicht)" if declared_bw else "") + ".")
    return rep


def hls_gate(url: str, *, favorite: dict | None = None, ffprobe: str | None = None, window_s: float = 10.0,
             strict_lc: bool = False, fetch: Fetch | None = None):
    """Wie ``radio.quality_gate`` fuer HLS. Gibt (StreamInfo mit URL der GEWAEHLTEN Variante, Report, Urteil) zurueck."""
    fetch = fetch or http_fetch
    fav = favorite or {}
    master = validate_url(url)
    text, final = fetch(master, MAX_PLAYLIST_BYTES)
    kind, content = parse_playlist(text.decode("utf-8", "replace"), final)
    if kind == "master":
        var = select_variant(content)
        vurl, hint, bw = var.url, var.codec_hint(), var.avg_bandwidth or var.bandwidth
    else:
        vurl, hint, bw = master, (None, None), None
    rep = check_variant(vurl, hint=hint, declared_bw=bw, window_s=window_s, fetch=fetch)
    if ffprobe:
        rep = quality.merge(rep, quality.ffprobe_probe(vurl, [ffprobe]))
        rep.source = "ffprobe+hls"
    floor = quality.effective_floor(fav.get("min_bitrate_kbps"))
    verdict = quality.evaluate(rep, floor_kbps=floor, strict_lc=strict_lc)
    if not verdict.ok and rep.codec == "unknown" and any("verschluesselt" in n for n in rep.notes):
        verdict = quality.Verdict("rejected", "stream_quality_unverified", ["HLS verschluesselt: nicht pruefbar."])
    exp = (fav.get("expected_codec") or "").lower()
    if verdict.ok and exp and exp != rep.codec:
        verdict = quality.Verdict("rejected", "stream_format_unsupported",
                                  [f"Erwarteter Codec '{exp}', erkannt '{rep.codec}'."], verdict.effective_kbps, verdict.flags)
    return StreamInfo(vurl, HLS_MIME, ".m3u8", hls_master=master), rep, verdict


__all__ = ["HLS_MIMES", "hls_gate", "is_hls_url", "select_variant", "parse_playlist", "check_variant"]
