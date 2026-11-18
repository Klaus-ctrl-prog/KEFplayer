# Internetradio (E1, Stand 18.11.2026)

> **Hinweis (Planung E-6):** Die verbindliche Zielregel ist strenger als dieser Stand: **FLAC oder AAC ab 128 kbit/s** (Ziel 192; 128 bis unter 192 gilt als „reduzierte Qualität“; MP3 bleibt ausgeschlossen), mit Qualitätsprüfung vor dem Start. E1 lässt noch Ogg/Opus/MP4-Audio zu und prüft keine Bitrate; die Angleichung ist als E1.1 geplant. Siehe [radio-plan.md](radio-plan.md). Eine Favoritenvorlage liegt unter `docs/favorites.example.json` (Platzhalter-URLs, nicht belegt).

`kefplayer radio <URL oder Favoritenname> [--favorites DATEI] [--renderer ...]`

- **Durchleitung:** Der PC holt den Stream und reicht ihn unverändert über den Medienserver (`/s/<token>/...`) an den Renderer. Kein Decodieren, **kein Raum-EQ/Profil** (kommt in E1b), keine Status-API und keine Gesundheitsüberwachung in dieser Stufe.
- **Codecs:** nur AAC, Ogg/Opus/Vorbis (`audio/ogg`), FLAC, MP4-Audio. **MP3 (`audio/mpeg` u. a.) wird abgelehnt** (Entscheidung E-6). Unbekannte oder fehlende Inhaltstypen ebenfalls.
- **URLs:** nur `http://` und `https://`, ohne Zugangsdaten in der URL. In Meldungen, Protokoll und Ereignissen erscheint nur `Schema://Host:Port/...` (Pfad und Query können Token enthalten).
- **Favoritenliste:** JSON `[{"name": "...", "url": "http://..."}]`, Namen eindeutig (ohne Beachtung der Groß-/Kleinschreibung).
- **ICY-Metadaten** werden nicht angefordert (kein Titelanzeige-Dienst in E1).
- **Ende:** `q` + Enter oder Strg+C; auch Ende des Senders oder Stopp durch den Renderer beendet die Sitzung. Es gibt keine Zeitgrenze für Live-Streams.
- **Nicht unterstützt:** Playlist-URLs (PLS/M3U) und Shoutcast-v1-Sender, die mit `ICY 200 OK` antworten (Meldung `stream_unreachable`).

## Nicht geprüft
Alles nur im Simulator (lokaler Fake-Sender, `MockRenderer`). **Ungeprüft an einer echten KEF Gen1:** ob sie `audioBroadcast`, `DLNA.ORG_OP=00`, den Live-Header-Satz und endlose Antworten ohne Content-Length akzeptiert und puffert. Die DLNA-FLAGS für Live sind aus dem Dateimodus übernommen (Annahme). Hardware-Test frühestens März 2027.
