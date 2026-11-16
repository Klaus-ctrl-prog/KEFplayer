"""Reine Anzeigelogik der GUI (ohne tkinter, ohne Threads): Formatierung, Protokollzeilen, Schalterzustaende."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import controller as ctl


def window_size(req_w: int, req_h: int, scale: float, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Startgroesse und Mindestgroesse in Pixeln: vom Inhalt angefordert, mindestens 900 Pixel bei 100 %,
    hoechstens 90 % des Bildschirms. Rueckgabe: (Breite, Hoehe, Mindestbreite, Mindesthoehe)."""
    w = min(max(req_w, int(900 * scale)), int(screen_w * 0.9))
    h = min(req_h, int(screen_h * 0.9))
    return w, h, min(int(780 * scale), w), min(int(req_h * 0.75), h)


def fmt_hms(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def wall_since(ts_iso: str | None, now: datetime | None = None) -> float | None:
    """Sekunden seit einem UTC-Zeitstempel des Protokolls (``ts`` in den Player-Ereignissen)."""
    if not ts_iso:
        return None
    try:
        t = datetime.fromisoformat(ts_iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or datetime.now(timezone.utc)) - t).total_seconds())


def format_text(info: dict | None) -> str:
    if not info:
        return "unbekannt"
    try:
        bits = f"{info['bits']} Bit" if info.get("bits") else "Bittiefe unbekannt"
        return f"{info['format']} {info['samplerate'] / 1000:g} kHz {bits}"
    except (KeyError, TypeError):
        return "unbekannt"


def item_row(index: int, item: ctl.Item) -> tuple[str, str, str, str]:
    """(Nr., Datei, Format, Status) fuer die Warteschlange."""
    if item.ok:
        return (f"{index + 1:03d}", item.path.name, format_text(item.info), "")
    return (f"{index + 1:03d}", item.path.name, "nicht lesbar", "uebersprungen: " + (item.error or "?"))


END_TEXT = {
    "ended": "gespielt", "skipped": "uebersprungen", "stopped": "gestoppt", "error": "Fehler",
    "never_started": "startete nie", "timeout": "Zeitueberschreitung", "prepare_error": "Vorbereitung fehlgeschlagen",
}


def end_text(reason: str | None) -> str:
    return END_TEXT.get(reason or "", reason or "unbekannt")


def now_text(now: dict | None, started_ts: str | None, *, pause_requested: bool, now_utc: datetime | None = None) -> str:
    """Anzeige 'Titel x von n' mit Wandzeit seit Titelstart (enthaelt Startwartezeit und Pausen, keine Position)."""
    if not now or now.get("index") is None:
        return "Kein Titel aktiv."
    txt = f"Titel {now['index'] + 1} von {now['total']}: {now['name']}"
    txt += f" | Wandzeit seit Titelstart {fmt_hms(wall_since(started_ts, now_utc))}"
    if pause_requested:
        txt += " | Pause/Weiter angefordert (Zustand nicht bestaetigt)"
    return txt


def log_line(msg: ctl.Msg) -> str | None:
    """Protokollzeile zu einer Controller-Meldung; None = nicht anzeigen."""
    k, d = msg.kind, msg.data
    if k == "warning":
        return "Hinweis: " + d["text"]
    if k == "error":
        return "FEHLER: " + d["text"]
    if k == "devices":
        n = len(d["devices"])
        return f"{n} Geraet(e) gefunden." if n else (d.get("hint") or "Keine Geraete gefunden.")
    if k == "device":
        return f"Verbunden: {d.get('name') or d.get('host')} ({d.get('host')})"
    if k == "profile":
        return f"Profil: {d['name']} ({'neutral' if d['neutral'] else 'aktiv'})"
    if k == "pause_requested":
        return "Pause/Weiter angefordert."
    if k == "playback_done":
        return f"Fertig: {d['played']} Titel, {d['problems']} mit Problemen. Protokoll: {d.get('log')}"
    if k == "queue_changed":
        return None
    if k == "player_event":
        return _player_line(d["event"])
    return None


def _player_line(r: dict) -> str | None:
    ev = r.get("event")
    if ev == "queue_start":
        return f"Warteschlange gestartet: {len(r.get('tracks', []))} Titel, Renderer {r.get('renderer')}."
    if ev == "sink":
        return f"Renderer meldet {len(r.get('mimes', []))} MIME-Typen."
    if ev == "sink_unavailable":
        return "Sink-Formate nicht lesbar, Standard-MIME wird verwendet."
    if ev == "track_processed":
        return f"  verarbeitet: Preamp {r['preamp_db']:+.2f} dB"
    if ev == "track_start":
        f = r.get("served_format") or {}
        sink = {True: "im Sink gemeldet", False: "NICHT im Sink gemeldet", None: "Sink unbekannt"}.get(r.get("mime_in_sink"))
        fmt = f"{f.get('format', '?')} {f.get('samplerate', '?')} Hz {f.get('bits', '?')} Bit"
        return (f"> {r['index'] + 1:03d} {Path(r['path']).name} [{fmt}, {r.get('mime')} ({sink})"
                f"{', verarbeitet' if r.get('processed') else ', unveraendert'}]")
    if ev == "track_end":
        extra = []
        if r.get("startup_delay_s") is not None:
            extra.append(f"Start nach {r['startup_delay_s']} s")
        if r.get("gap_before_s") is not None:
            extra.append(f"Luecke zum Vorgaenger {r['gap_before_s']} s")
        if r.get("ended_early"):
            extra.append("Ende frueher als die Dauer")
        if r.get("error"):
            extra.append(str(r["error"]))
        return f"  Ende: {end_text(r.get('end_reason'))}" + (f" ({'; '.join(extra)})" if extra else "")
    if ev == "cleanup_failed":
        return "Hinweis: temporaere Datei konnte nicht geloescht werden (Wiedergabe laeuft weiter)."
    if ev == "stop_failed":
        return f"Hinweis: Stop am Renderer fehlgeschlagen ({r.get('error')})."
    if ev == "queue_end":
        return None  # 'playback_done' meldet das Ende
    return None


def button_states(state: str, *, has_device: bool, n_playable: int) -> dict[str, bool]:
    """Welche Schalter sind aktiv? Schluessel: search, connect, queue_edit, profile, play, pause, skip, stop."""
    idle = state == ctl.STATE_IDLE
    playing = state == ctl.STATE_PLAYING
    return {
        "search": idle, "connect": idle, "queue_edit": idle, "profile": idle,
        "play": idle and has_device and n_playable > 0,
        "pause": playing, "skip": playing, "stop": playing,
    }


STATE_TEXT = {
    ctl.STATE_IDLE: "Bereit", ctl.STATE_BUSY: "Bitte warten ...", ctl.STATE_PLAYING: "Wiedergabe",
    ctl.STATE_STOPPING: "Wird gestoppt ...", ctl.STATE_PROCESSING: "Verarbeitung ...",
}
