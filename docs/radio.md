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

## Nicht geprüft
Alles nur im Simulator (lokaler Fake-Sender, `MockRenderer`). **Ungeprüft an einer echten KEF Gen1:** ob sie `audioBroadcast`, `DLNA.ORG_OP=00`, den Live-Header-Satz und endlose Antworten ohne Content-Length akzeptiert und puffert. Die DLNA-FLAGS für Live sind aus dem Dateimodus übernommen (Annahme). Hardware-Test frühestens März 2027.
