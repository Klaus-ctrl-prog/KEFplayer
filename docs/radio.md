# Internetradio (E1, Stand 18.11.2026)

> **Stand E1.1 (E-6):** zulässig sind **FLAC** und **AAC ab 128 kbit/s** (Ziel 192; 128 bis unter 192 = „reduzierte Qualität“, gekennzeichnet). **MP3, Ogg/Opus und HLS-Playlists werden abgelehnt** (HLS erst mit E1b). Vor der Wiedergabe prüft `kefplayer radio` die Stream-Qualität, und bei Nichterfüllung wird **nichts an die KEF gesendet**. Mit `kefplayer radio-check FAVORITEN.json` lassen sich alle Favoriten ohne Renderer prüfen. Siehe [radio-plan.md](radio-plan.md). Favoritenvorlage: `docs/favorites.example.json` (Platzhalter-URLs sind keine Adressen und werden abgelehnt).

`kefplayer radio <URL oder Favoritenname> [--favorites DATEI] [--renderer ...]`

- **Durchleitung:** Der PC holt den Stream und reicht ihn unverändert über den Medienserver (`/s/<token>/...`) an den Renderer. Kein Decodieren, **kein Raum-EQ/Profil** (kommt in E1b), keine Status-API und keine Gesundheitsüberwachung in dieser Stufe.
- **Codecs:** nur AAC (`audio/aac`, `audio/aacp`, `audio/mp4`) und FLAC. MP3, Ogg/Opus, HLS und unbekannte oder fehlende Inhaltstypen werden abgelehnt.
- **Qualitätsprüfung (E1.1, `kefplayer/quality.py`):** Der Strom wird selbst gelesen (ADTS-Kopf, Codec-Erkennung, **gemessene Bitrate** nach 3 s Einlauf über 10 s; Optionen `--quality-warmup`, `--quality-window`). Optional liefert `--ffprobe PFAD` (vom Nutzer installiert) Profil und deklarierte Bitrate; ffprobe wird nie automatisch geladen. Bei mehreren Bitratenwerten zählt der **kleinere**. Ist keine Bitrate bestimmbar, gilt der Stream als **nicht verifiziert und wird abgelehnt**. Bei gemessener Bitrate gilt eine Toleranz von 5 % (Arbeitswert, offen E-9).
- **HE-AAC:** Mit dem eigenen Messweg ist HE-AAC (SBR) **nicht sicher erkennbar**; solche Ströme werden gekennzeichnet (`he_aac_possible`), nicht abgelehnt. `--strict-lc` lehnt sie ab (dafür ist ffprobe nötig). Standardwert vorläufig, E-10 offen.
- **Favoritenfelder:** `expected_codec` (Abweichung → abgelehnt), `min_bitrate_kbps` (hebt die Untergrenze nur an, nie unter 128).
- **URLs:** nur `http://` und `https://`, ohne Zugangsdaten in der URL. In Meldungen, Protokoll und Ereignissen erscheint nur `Schema://Host:Port/...` (Pfad und Query können Token enthalten).
- **Favoritenliste:** JSON `[{"name": "...", "url": "http://..."}]`, Namen eindeutig (ohne Beachtung der Groß-/Kleinschreibung).
- **ICY-Metadaten** werden nicht angefordert (kein Titelanzeige-Dienst in E1).
- **Ende:** `q` + Enter oder Strg+C; auch Ende des Senders oder Stopp durch den Renderer beendet die Sitzung. Es gibt keine Zeitgrenze für Live-Streams.
- **Nicht unterstützt:** HLS und Playlist-URLs (PLS/M3U) und Shoutcast-v1-Sender, die mit `ICY 200 OK` antworten (Meldung `stream_unreachable`).

## E1b: Raum-EQ für Radio (`--ffmpeg`, Stand 18.11.2026, nur Simulator)

`kefplayer radio <URL oder Favorit> --ffmpeg C:\\Pfad\\ffmpeg.exe --profile RAUM.json [--ffmpeg-sha256 HEX] [--radio-rate HZ] [--no-reconnect]`

- **Ablauf:** Qualitätsprüfung (E-6, wie oben) → ffmpeg dekodiert nach PCM → Raum-EQ (`--profile`, Filterzustand über Blöcke erhalten, fester Preamp) → WAV PCM **24 Bit** (Python) → Medienserver → KEF. Ohne `--ffmpeg` bleibt alles wie in E1 (unverändert durchgeleitet). `--profile`, `--ffmpeg-sha256`, `--radio-rate` und `--no-reconnect` ohne `--ffmpeg` werden mit Fehler abgelehnt.
- **ffmpeg:** nur **absoluter Pfad**, keine PATH-Suche, nie automatisch geladen. Beim Start werden Pfad, Version und SHA-256 ausgegeben; mit `--ffmpeg-sha256` wird bei Abweichung **nicht gestartet**. Windows Application Control wird nicht umgangen (blockiert sie ffmpeg, kommt `decoder_unavailable`). Aufruf ohne Shell, mit Protokoll-Whitelist und ohne eigenes Reconnect von ffmpeg.
- **Samplerate (E-17):** Senderrate ohne unnötiges Resampling, Rückfall 48 kHz (auch bei Raten unter 32 kHz, z. B. HE-AAC-Kernrate); `--radio-rate` überschreibt.
- **Reconnect (E-11):** standardmäßig an, nur Upstream; vor **jedem** Versuch läuft die Qualitätsprüfung erneut. Während einer Lücke bekommt die KEF **Stille**. Backoff und Budget sind **Arbeitswerte, nicht gemessen** (2/5/10/30/60 s, höchstens 5 Versuche je 10 min, 30 je Sitzung, Stillstand nach 10 s). Endet das Budget: `stream_reconnect_exhausted`, Exitcode 1. Am Ende werden Zähler ausgegeben (Reconnects, Lücken, Unterläufe, Überlauf, übersteuerte Samples). Der Renderer und `RENDERER_LOCK` werden vom Reconnect nicht berührt.
- **Pegel:** Es gibt keinen Stück-Limiter. Ist der Preamp zu hoch, werden übersteuerte Samples begrenzt und gezählt (kein Dither). Ohne manuellen Preamp setzt das Profil einen Headroom-Preamp aus dem Filter-Betragsgang.
- **Grenzen:** **HLS bleibt auch hier abgelehnt** (die Qualitätsprüfung liest den Strom selbst; HLS-Unterstützung ist nicht gebaut). Keine Status-API/Health für Radio, keine Titelanzeige. Kein echtes ffmpeg und keine echte KEF getestet.

## Nicht geprüft
Alles nur im Simulator (lokaler Fake-Sender, `MockRenderer`, Fake-Decoder statt ffmpeg). **Ungeprüft an einer echten KEF Gen1:** ob sie `audioBroadcast`, `DLNA.ORG_OP=00`, den Live-Header-Satz und endlose Antworten ohne Content-Length akzeptiert und puffert; für E1b zusätzlich, ob sie einen endlosen **WAV-24-Bit**-Strom mit unbekannter Länge und die eingefügte Stille akzeptiert. Die DLNA-FLAGS für Live sind aus dem Dateimodus übernommen (Annahme). Hardware-Test frühestens März 2027.
