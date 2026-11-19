"""E1.1 Qualitaetspruefung fuer Radio-Streams (Regel E-6), vor der Wiedergabe auf der KEF.

Regel (Nutzerentscheidung 18.11.2026): FLAC und AAC ab 128 kbit/s zulaessig, Ziel 192 kbit/s
("volle Qualitaet"); 128 bis unter 192 = "reduzierte Qualitaet" (zulaessig, gekennzeichnet);
AAC darunter, MP3 und alles andere/unbekannte wird abgelehnt. Unbekannt gilt nie als gut.

Zwei Messwege:
- ``native_probe``: liest den Strom selbst (ADTS-Kopf, FLAC/MP3/Ogg-Erkennung, gemessene Bitrate).
  Braucht kein ffmpeg. Kann HE-AAC (SBR) NICHT sicher erkennen.
- ``ffprobe_probe``: optional, wenn der Nutzer einen ffprobe-Pfad angibt (Profil, deklarierte Bitrate).
Der kleinere vorhandene Bitratenwert zaehlt (konservativ).
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

FLOOR_KBPS = 128
TARGET_KBPS = 192
# Arbeitswert (offen E-9): bei GEMESSENER Bitrate zaehlt eine kleine Unterschreitung der Schwelle noch
# (Messfenster, Protokoll-Overhead). Bei deklarierter Bitrate wird exakt verglichen.
MEASURE_TOLERANCE = 0.95

_ADTS_SR = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


@dataclass
class StreamReport:
    codec: str = "unknown"            # aac | flac | mp3 | ogg | hls | unknown
    profile: str | None = None        # z. B. "LC", "HE-AAC"; None = nicht bestimmbar
    he_unknown: bool = False          # True: HE-AAC (SBR) laesst sich mit diesem Messweg nicht ausschliessen
    sample_rate: int | None = None
    channels: int | None = None
    declared_kbps: float | None = None
    measured_kbps: float | None = None
    source: str = "native"            # native | ffprobe
    notes: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    level: str                        # full | reduced | rejected
    category: str | None              # None bei full/reduced, sonst Fehlerkategorie
    reasons: list[str]
    effective_kbps: float | None = None
    flags: list[str] = field(default_factory=list)   # z. B. "he_aac_possible"

    @property
    def ok(self) -> bool:
        return self.level in ("full", "reduced")


def evaluate(rep: StreamReport, *, floor_kbps: float = FLOOR_KBPS, target_kbps: float = TARGET_KBPS,
             strict_lc: bool = False) -> Verdict:
    """Bewertung nach E-6. ``floor_kbps`` darf nur ANGEHOBEN werden (siehe ``effective_floor``)."""
    floor_kbps = max(float(floor_kbps), FLOOR_KBPS)
    if rep.codec == "mp3":
        return Verdict("rejected", "stream_format_unsupported", ["MP3 ist ausgeschlossen (E-6)."])
    if rep.codec == "hls":
        return Verdict("rejected", "stream_format_unsupported",
                       ["HLS-Playlist: nur ueber die ffmpeg-Stufe (E1b) abspielbar, E1 kann kein HLS."])
    if rep.codec == "flac":
        return Verdict("full", None, ["FLAC (verlustfrei)."])
    if rep.codec != "aac":
        return Verdict("rejected", "stream_format_unsupported",
                       [f"Codec nicht zugelassen oder nicht erkannt: {rep.codec}."])
    flags: list[str] = []
    if rep.profile and "HE" in rep.profile.upper():
        flags.append("he_aac")
        if strict_lc:
            return Verdict("rejected", "stream_format_unsupported", [f"HE-AAC ({rep.profile}) bei strict-lc abgelehnt."], flags=flags)
    elif rep.he_unknown:
        flags.append("he_aac_possible")
        if strict_lc:
            return Verdict("rejected", "stream_quality_unverified",
                           ["strict-lc: HE-AAC mit diesem Messweg nicht auszuschliessen (ffprobe noetig)."], flags=flags)
    vals = [v for v in (rep.declared_kbps, rep.measured_kbps) if v]
    if not vals:
        return Verdict("rejected", "stream_quality_unverified", ["Bitrate weder deklariert noch messbar."], flags=flags)
    eff = min(vals)
    # Toleranz nur, wenn der massgebliche Wert wirklich nur gemessen ist (nicht deklariert oder deklariert und gleich)
    measured_only = rep.declared_kbps is None or (rep.measured_kbps is not None and rep.measured_kbps < rep.declared_kbps)
    tol = MEASURE_TOLERANCE if measured_only else 1.0
    if eff >= target_kbps * tol:
        return Verdict("full", None, [f"AAC {eff:.0f} kbit/s (Ziel {target_kbps:.0f})."], eff, flags)
    if eff >= floor_kbps * tol:
        return Verdict("reduced", None, [f"AAC {eff:.0f} kbit/s: reduzierte Qualitaet (Ziel {target_kbps:.0f}, Untergrenze {floor_kbps:.0f})."], eff, flags)
    return Verdict("rejected", "stream_quality_too_low", [f"AAC {eff:.0f} kbit/s liegt unter der Untergrenze {floor_kbps:.0f}."], eff, flags)


def effective_floor(favorite_min: float | int | None) -> float:
    """Die Mindestbitrate eines Favoriten darf die Untergrenze nur anheben."""
    try:
        return max(float(favorite_min), FLOOR_KBPS) if favorite_min is not None else float(FLOOR_KBPS)
    except (TypeError, ValueError):
        return float(FLOOR_KBPS)


# ---------------------------------------------------------------- native Erkennung

def parse_adts(buf: bytes) -> dict | None:
    """Ersten ADTS-Kopf suchen und plausibel pruefen (zwei aufeinanderfolgende Koepfe)."""
    n = len(buf)
    i = 0
    while i + 7 <= n:
        if buf[i] == 0xFF and (buf[i + 1] & 0xF6) == 0xF0:   # Sync 12 Bit, Layer 00
            hdr = _adts_header(buf, i)
            if hdr and hdr["frame_len"] >= 7:
                j = i + hdr["frame_len"]
                if j + 7 <= n:
                    nxt = _adts_header(buf, j)
                    if nxt and buf[j] == 0xFF and (buf[j + 1] & 0xF6) == 0xF0 and nxt["sr_idx"] == hdr["sr_idx"]:
                        return hdr
                elif j + 7 > n:
                    return None  # zu wenig Daten fuer die Bestaetigung
        i += 1
    return None


def _adts_header(buf: bytes, i: int) -> dict | None:
    if i + 7 > len(buf):
        return None
    b = buf[i:i + 7]
    sr_idx = (b[2] >> 2) & 0xF
    if sr_idx >= len(_ADTS_SR):
        return None
    return {
        "object_type": ((b[2] >> 6) & 0x3) + 1,
        "sr_idx": sr_idx,
        "sample_rate": _ADTS_SR[sr_idx],
        "channels": ((b[2] & 1) << 2) | (b[3] >> 6),
        "frame_len": ((b[3] & 0x3) << 11) | (b[4] << 3) | (b[5] >> 5),
    }


def detect_codec(buf: bytes) -> StreamReport:
    rep = StreamReport(source="native")
    if buf[:4] == b"fLaC":
        rep.codec = "flac"
        return rep
    if buf[:4] == b"OggS":
        rep.codec = "ogg"
        return rep
    if buf[:7] == b"#EXTM3U":
        rep.codec = "hls"
        return rep
    adts = parse_adts(buf)
    if adts:
        rep.codec = "aac"
        rep.profile = {1: "Main", 2: "LC", 3: "SSR", 4: "LTP"}.get(adts["object_type"], f"AOT{adts['object_type']}")
        rep.sample_rate, rep.channels = adts["sample_rate"], adts["channels"]
        rep.he_unknown = True   # SBR ist im ADTS-Kopf nicht sichtbar
        rep.notes.append("Profil aus ADTS-Kopf; HE-AAC (SBR) damit nicht ausschliessbar.")
        return rep
    # MP3: optionaler ID3-Kopf, dann Frame-Sync 11 Bit, Layer != 00
    off = 0
    if buf[:3] == b"ID3" and len(buf) >= 10:
        size = (buf[6] & 0x7F) << 21 | (buf[7] & 0x7F) << 14 | (buf[8] & 0x7F) << 7 | (buf[9] & 0x7F)
        off = 10 + size
    if off + 2 <= len(buf) and buf[off] == 0xFF and (buf[off + 1] & 0xE0) == 0xE0 and (buf[off + 1] & 0x06) != 0:
        rep.codec = "mp3"
        return rep
    if buf[:4] == b"\x56\xe0" or (len(buf) >= 2 and buf[0] == 0x56 and (buf[1] & 0xE0) == 0xE0):
        rep.codec = "aac"
        rep.he_unknown = True
        rep.notes.append("LOAS/LATM: Profil nicht bestimmbar.")
        return rep
    return rep


def native_probe(open_fn: Callable[[], object], *, warmup_s: float = 3.0, window_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic, chunk: int = 4096) -> StreamReport:
    """Strom oeffnen, Codec erkennen und die Bitrate im Fenster [warmup, warmup+window] messen.

    Die ersten Sekunden werden verworfen: viele Sender schicken beim Verbinden einen Puffer-Burst.
    """
    src = open_fn()
    try:
        read = getattr(src, "read1", None) or src.read
        head = b""
        t0 = clock()
        while len(head) < 4096 and clock() - t0 < warmup_s + window_s:
            d = read(chunk)
            if not d:
                break
            head += d
        rep = detect_codec(head)
        got, t_start, started = 0, None, False
        while True:
            now = clock()
            if now - t0 >= warmup_s + window_s:
                break
            d = read(chunk)
            if not d:
                rep.notes.append("Strom endete waehrend der Messung.")
                break
            now = clock()
            if now - t0 >= warmup_s:
                if not started:
                    started, t_start = True, now
                else:
                    got += len(d)
        if started and t_start is not None:
            span = clock() - t_start
            if span >= window_s * 0.5 and got > 0:
                rep.measured_kbps = got * 8 / span / 1000.0
            else:
                rep.notes.append("Messfenster zu kurz fuer eine Bitrate.")
        else:
            rep.notes.append("Keine Daten im Messfenster.")
        return rep
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- ffprobe (optional)

def ffprobe_probe(url: str, ffprobe_cmd: list[str], *, timeout_s: float = 20.0) -> StreamReport:
    """ffprobe auf die URL. ``ffprobe_cmd`` = z. B. [pfad]. Nur Argumentliste, nie Shell."""
    cmd = [*ffprobe_cmd, "-v", "error", "-protocol_whitelist", "http,https,tcp,tls,crypto",
           "-rw_timeout", str(int(timeout_s * 1_000_000 // 2)), "-print_format", "json",
           "-show_streams", "-show_format", url]
    rep = StreamReport(source="ffprobe")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        rep.notes.append(f"ffprobe nicht nutzbar: {type(e).__name__}.")
        return rep
    if out.returncode != 0:
        rep.notes.append("ffprobe meldete einen Fehler (Ausgabe nicht uebernommen, kann die URL enthalten).")
        return rep
    try:
        data = json.loads(out.stdout)
        st = next(s for s in data.get("streams", []) if s.get("codec_type") == "audio")
    except (ValueError, StopIteration):
        rep.notes.append("ffprobe lieferte keinen Audio-Stream.")
        return rep
    name = (st.get("codec_name") or "").lower()
    rep.codec = {"aac": "aac", "flac": "flac", "mp3": "mp3", "vorbis": "ogg", "opus": "ogg"}.get(name, "unknown")
    rep.profile = st.get("profile") or None
    try:
        rep.sample_rate = int(st["sample_rate"]) if st.get("sample_rate") else None
    except (TypeError, ValueError):
        pass
    rep.channels = st.get("channels") if isinstance(st.get("channels"), int) else None
    for src_ in (st.get("bit_rate"), (data.get("format") or {}).get("bit_rate")):
        try:
            v = float(src_) / 1000.0
            if v > 0:
                rep.declared_kbps = v
                break
        except (TypeError, ValueError):
            continue
    return rep


def merge(native: StreamReport, ff: StreamReport | None) -> StreamReport:
    """ffprobe liefert Codec/Profil/deklarierte Bitrate, die Messung die gemessene Bitrate."""
    if ff is None or ff.codec == "unknown":
        return native
    out = StreamReport(codec=ff.codec, profile=ff.profile, sample_rate=ff.sample_rate or native.sample_rate,
                       channels=ff.channels or native.channels, declared_kbps=ff.declared_kbps,
                       measured_kbps=native.measured_kbps, source="ffprobe+native",
                       notes=[*native.notes, *ff.notes])
    out.he_unknown = ff.profile is None and ff.codec == "aac"
    return out
