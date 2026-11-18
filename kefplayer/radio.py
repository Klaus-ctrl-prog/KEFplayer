"""Internetradio (E1): URL-Pruefung, Maskierung, Codec-Richtlinie, Favoritenliste, Quelle oeffnen.

E1 leitet den Stream unveraendert durch (kein Decodieren, kein DSP). Metadaten (ICY) werden
nicht angefordert, der Audiostrom enthaelt dadurch keine eingebetteten Metadatenbloecke.
Nur hochwertige Codecs; MP3 wird bewusst abgelehnt (Entscheidung E-6 vom 18.11.2026).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = "kefplayer-radio"
CONNECT_TIMEOUT_S = 10.0

# Zugelassene Inhaltstypen (E-6, Stand 18.11.2026): nur AAC und FLAC. Die Bitrate wird in quality.py geprueft.
ALLOWED_MIMES = {
    "audio/aac": ".aac",
    "audio/aacp": ".aac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}
HLS_MIMES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}
# Ausdruecklich abgelehnt (Richtlinie, kein technischer Fehler).
REJECTED_MIMES = {"audio/mpeg", "audio/mp3", "audio/x-mpeg", "audio/mpeg3", "audio/x-mp3"}


class RadioError(Exception):
    """Fehler mit Kategorie (nur Einordnung des Symptoms)."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def validate_url(url: str) -> str:
    """Nur http/https mit Host, ohne Zugangsdaten in der URL. Gibt die URL zurueck."""
    if not isinstance(url, str) or not url.strip():
        raise RadioError("stream_url_invalid", "Leere Stream-URL.")
    url = url.strip()
    if any(ord(c) < 32 or c in " \\" for c in url):
        raise RadioError("stream_url_invalid", "Stream-URL enthaelt Leer- oder Steuerzeichen.")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RadioError("stream_url_invalid", "Nur http:// und https:// sind erlaubt.")
    if not parts.hostname:
        raise RadioError("stream_url_invalid", "Stream-URL ohne Host.")
    if parts.username is not None or parts.password is not None:
        raise RadioError("stream_url_invalid", "Zugangsdaten in der URL sind nicht erlaubt.")
    try:
        parts.port  # noqa: B018 - loest bei ungueltigem Port ValueError aus
    except ValueError:
        raise RadioError("stream_url_invalid", "Ungueltiger Port in der Stream-URL.") from None
    return url


def mask_url(url: str) -> str:
    """Fuer Logs, Status und Meldungen: nur Schema, Host und Port. Pfad und Query koennen Token enthalten."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "?"
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme or '?'}://{host}{port}/..."
    except (ValueError, AttributeError):
        return "(ungueltige URL)"


def normalize_mime(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def check_codec(content_type: str | None) -> tuple[str, str]:
    """Gibt (mime, endung) zurueck oder wirft RadioError(stream_format_unsupported)."""
    mime = normalize_mime(content_type)
    if mime in REJECTED_MIMES:
        raise RadioError("stream_format_unsupported",
                         f"{mime}: MP3 ist ausgeschlossen (nur AAC ab 128 kbit/s und FLAC).")
    if mime in HLS_MIMES:
        raise RadioError("stream_format_unsupported", "HLS/Playlist: nur ueber die ffmpeg-Stufe (E1b) abspielbar, E1 kann kein HLS.")
    if mime not in ALLOWED_MIMES:
        raise RadioError("stream_format_unsupported", f"Inhaltstyp nicht zugelassen oder unbekannt: {mime or '(keiner)'}.")
    return mime, ALLOWED_MIMES[mime]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nur auf http/https ohne Zugangsdaten und nie von https auf http (urllib erlaubt sonst auch ftp)."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        try:
            target = validate_url(target)
        except RadioError:
            raise RadioError("stream_url_invalid",
                             f"Weiterleitung auf unzulaessiges Ziel abgelehnt ({mask_url(req.full_url)}).") from None
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(target).scheme != "https":
            raise RadioError("stream_url_invalid",
                             f"Weiterleitung von https auf http abgelehnt ({mask_url(req.full_url)}).")
        return super().redirect_request(req, fp, code, msg, headers, target)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def open_stream(url: str, timeout: float = CONNECT_TIMEOUT_S):
    """Quelle oeffnen. Es werden bewusst keine ICY-Metadaten angefordert."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        resp = _OPENER.open(req, timeout=timeout)  # Schema vorher auf http/https geprueft, Weiterleitungen werden ebenso geprueft
    except RadioError:
        raise
    except urllib.error.HTTPError as e:
        raise RadioError("stream_http_error", f"HTTP {e.code} vom Sender ({mask_url(url)}).") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RadioError("stream_unreachable", f"Sender nicht erreichbar ({mask_url(url)}): {type(e).__name__}.") from None
    except Exception as e:  # noqa: BLE001 - z. B. http.client.BadStatusLine bei "ICY 200 OK"
        raise RadioError("stream_unreachable", f"Sender antwortet unerwartet ({mask_url(url)}): {type(e).__name__}.") from None
    status = getattr(resp, "status", 200)
    if status != 200:
        resp.close()
        raise RadioError("stream_http_error", f"HTTP {status} vom Sender ({mask_url(url)}).")
    return resp


@dataclass
class StreamInfo:
    url: str
    mime: str
    ext: str
    hls_master: str | None = None   # nur HLS (E1b): URL der Master-Playlist; ``url`` ist dann die gewaehlte Variante


def probe(url: str, timeout: float = CONNECT_TIMEOUT_S, allow_hls: bool = False) -> StreamInfo:
    """Einmal verbinden, Inhaltstyp pruefen, wieder schliessen. Es werden keine Audiodaten gelesen.

    ``allow_hls`` (nur E1b mit ffmpeg): ein HLS-Inhaltstyp wird gemeldet statt abgelehnt; die Pruefung macht hls.py.
    """
    url = validate_url(url)
    resp = open_stream(url, timeout)
    try:
        if allow_hls and normalize_mime(resp.headers.get("Content-Type")) in HLS_MIMES:
            return StreamInfo(url, normalize_mime(resp.headers.get("Content-Type")), ".m3u8")
        mime, ext = check_codec(resp.headers.get("Content-Type"))
    finally:
        resp.close()
    return StreamInfo(url, mime, ext)


def load_favorites(path: str | Path) -> list[dict]:
    """Favoritenliste: JSON-Liste von {"name": ..., "url": ...}. Ungueltige Eintraege fuehren zum Fehler."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RadioError("favorites_invalid", f"Favoritenliste nicht lesbar: {type(e).__name__}.") from None
    if not isinstance(data, list):
        raise RadioError("favorites_invalid", "Favoritenliste muss eine JSON-Liste sein.")
    out, seen = [], set()
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise RadioError("favorites_invalid", f"Eintrag {i}: 'name' fehlt.")
        name = item["name"].strip()
        if name.lower() in seen:
            raise RadioError("favorites_invalid", f"Eintrag {i}: Name '{name}' doppelt.")
        seen.add(name.lower())
        fav = {"name": name, "url": validate_url(item.get("url", ""))}
        for k in ("genre", "expected_codec", "min_bitrate_kbps", "status", "verified_at", "notes"):
            if k in item:
                fav[k] = item[k]
        out.append(fav)
    return out


def resolve_target(target: str, favorites_path: str | Path | None) -> tuple[str, str]:
    """Gibt (name, url) zurueck. ``target`` ist eine URL oder ein Favoritenname."""
    fav = resolve_favorite(target, favorites_path)
    return fav["name"], fav["url"]


def resolve_favorite(target: str, favorites_path: str | Path | None) -> dict:
    """Wie resolve_target, aber mit allen Favoritenfeldern (name, url, expected_codec, min_bitrate_kbps, ...)."""
    if "://" in target or target.lower().startswith(("javascript:", "file:", "data:")):
        url = validate_url(target)  # jedes Schema ausser http/https endet hier mit einem klaren Fehler
        return {"name": urllib.parse.urlsplit(url).hostname or "radio", "url": url}
    if favorites_path is None:
        raise RadioError("favorites_invalid", "Kein Favorit ohne --favorites DATEI; oder eine http(s)-URL angeben.")
    for fav in load_favorites(favorites_path):
        if fav["name"].lower() == target.strip().lower():
            return fav
    raise RadioError("favorites_invalid", f"Favorit '{target}' nicht in der Liste.")


PLACEHOLDER_HOST = "example.invalid"


def is_placeholder(url: str) -> bool:
    try:
        return (urllib.parse.urlsplit(url).hostname or "") == PLACEHOLDER_HOST
    except ValueError:
        return False


def quality_gate(url: str, *, favorite: dict | None = None, ffprobe: str | None = None,
                 warmup_s: float = 3.0, window_s: float = 10.0, strict_lc: bool = False, hls_ok: bool = False):
    """Qualitaetspruefung vor der Wiedergabe (E-6). Gibt (StreamInfo, StreamReport, Verdict) zurueck.

    Wirft RadioError bei Verbindungs-/Formatfehlern. Das Urteil kann ``rejected`` sein (kein Wurf).
    ``hls_ok`` (nur mit ffmpeg-Stufe E1b): HLS (.m3u8) wird ueber hls.py geprueft; ``info.url`` ist dann die
    gewaehlte Variante. Ohne ``hls_ok`` bleibt HLS abgelehnt.
    """
    from . import quality

    if hls_ok:
        from . import hls
        if hls.is_hls_url(url):
            return hls.hls_gate(url, favorite=favorite, ffprobe=ffprobe, window_s=window_s, strict_lc=strict_lc)
    info = probe(url, allow_hls=hls_ok)
    if hls_ok and info.mime in HLS_MIMES:
        from . import hls
        return hls.hls_gate(info.url, favorite=favorite, ffprobe=ffprobe, window_s=window_s, strict_lc=strict_lc)
    fav = favorite or {}
    floor = quality.effective_floor(fav.get("min_bitrate_kbps"))
    rep = quality.native_probe(lambda: open_stream(info.url), warmup_s=warmup_s, window_s=window_s)
    if ffprobe:
        rep = quality.merge(rep, quality.ffprobe_probe(info.url, [ffprobe]))
    verdict = quality.evaluate(rep, floor_kbps=floor, strict_lc=strict_lc)
    exp = (fav.get("expected_codec") or "").lower()
    if verdict.ok and exp and exp != rep.codec:
        verdict = quality.Verdict("rejected", "stream_format_unsupported",
                                  [f"Erwarteter Codec '{exp}', erkannt '{rep.codec}'."], verdict.effective_kbps, verdict.flags)
    return info, rep, verdict
