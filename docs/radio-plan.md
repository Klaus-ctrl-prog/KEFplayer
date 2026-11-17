# KEFplayer – Phase E: Internetradio (Plan, Stand 18.11.2026, abgeschlossen für heute)

Status: **E0 abgeschlossen, E1 gemergt (`0df3584`), E1b geplant (noch kein Code).** Alles nur im Simulator entwickelt; **nichts davon ist an einer echten KEF Gen1 geprüft** (Hardware-Tests frühestens März 2027). Annahmen sind als solche markiert.

## 1. Feste Vorgaben (Nutzer, 18.11.2026)
- **E-1:** Variante B (PC holt den Stream und leitet ihn über den Medienserver an die KEF).
- **E-2:** nur eigene Sender-URLs / Favoritenliste (kein Verzeichnis-Scraping).
- **E-3:** Raum-EQ für Radio (E1b) kommt auf jeden Fall.
- **E-6 (streng):** zulässig **nur FLAC oder AAC mit mindestens 192 kbit/s**. MP3 sowie AAC unter 192 kbit/s werden abgelehnt. Die tatsächliche Stream-Qualität (Codec, Bitrate) wird **vor** der Wiedergabe auf der KEF verifiziert (ffprobe/ffmpeg).
- **E-12:** E1b nutzt **ffmpeg als externe Decoder-/Puffer-Stufe** (subprocess) nach dem Prinzip moOde/CamillaDSP: Stream → PCM → Raum-EQ → KEF, mit kontrolliertem Reconnect.
- Genre-Schwerpunkt der Startliste: Rock, Pop, Classic Rock (keine Klassik).

## 2. Stand E1 (auf main) und Abweichung von E-6
E1 (`kefplayer radio`, `docs/radio.md`) leitet Streams unverändert durch und lehnt MP3 ab. **E1 ist weiter gefasst als E-6:** zugelassen sind dort zusätzlich Ogg/Opus/MP4-Audio, und es gibt **keine Bitratenprüfung**. Das wird erst im Folgeschritt **E1.1** an E-6 angeglichen (Code, nicht heute). Bis dahin gilt für die Nutzung: nur Sender verwenden, die E-6 erfüllen.

## 3. E-6 im Detail: Qualitätsprüfung vor dem Start (Entwurf E1.1)
Ablauf (`kefplayer radio` bzw. eigenes `kefplayer radio-check`):
1. URL validieren (nur http/https, keine Zugangsdaten), URL in allen Meldungen maskiert.
2. Kurzer Probelauf mit **ffprobe** (feste Zeitgrenze, z. B. wenige Sekunden): `codec_name`, `profile`, Samplerate, Kanäle, deklarierte `bit_rate`.
3. Zusätzlich **gemessene Bitrate** über ein Zeitfenster (empfangene Bytes ÷ Zeit). Grund: Bei Live-AAC (ADTS) fehlt die Bitrate im Datenstrom oft oder ist nur ein Schätzwert; Server-Header (`icy-br`) sind unzuverlässig und werden nicht als Beleg gewertet.
4. Entscheidung:
   - FLAC: zulassen (keine Mindestbitrate; Samplerate/Bittiefe protokollieren).
   - AAC: zulassen nur wenn **Codec = AAC** und (deklarierte Bitrate ≥ 192 kbit/s **oder**, falls nicht deklariert, gemessene Bitrate ≥ 192 kbit/s mit festgelegter Toleranz). Bei Widerspruch zwischen deklariert und gemessen: **ablehnen**, nicht raten.
   - Alles andere, unbekannt oder nicht messbar: **ablehnen** mit Kategorie `stream_format_unsupported` bzw. `stream_quality_unverified`. Unbekannt gilt nie als gut.
5. Erst nach bestandener Prüfung wird `SetAVTransportURI` an die KEF gesendet.
- **Offene Entscheidungen:** E-9 Toleranz bei der gemessenen Bitrate (Vorschlag: nur ein kleiner Abschlag wegen Messfenster; Wert erst nach Messungen im Simulator festlegen, hier keine Zahl erfunden); E-10 nur **AAC-LC** zulassen und HE-AAC (SBR/PS) ablehnen? (Vorschlag: ja, da HE-AAC bei gleicher Nennbitrate ein Effizienzcodec ist; bitte entscheiden).
- **Adaptive Streams (HLS, z. B. hr3):** ffmpeg wählt eine Variante; die **höchste Stufe muss erzwungen** werden (Variante gezielt wählen) und die Prüfung gilt für die tatsächlich gewählte Variante. Ob die Startkandidaten entsprechende URLs anbieten, ist **nicht belegt**.

## 4. Startliste (Rock, Pop, Classic Rock) und Vorlage
Vorlage: `examples/favorites.example.json` (Liste von Objekten, angelehnt an die strukturierte Senderverwaltung von moOde Audio): `name`, `url`, `genre`, `expected_codec`, `min_bitrate_kbps`, `status` (`candidate`/`to_verify`/`verified`/`rejected`), `verified_at`, `notes`. Der heutige Loader liest `name` und `url`, die übrigen Felder werden ignoriert (Auswertung folgt mit E1.1).

| Sender | Rolle | Stand |
|---|---|---|
| 1LIVE | Startkandidat (erwartet AAC 192) | URL fehlt, nicht verifiziert |
| WDR 2 | Startkandidat (erwartet AAC 192) | URL fehlt (Region wählen), nicht verifiziert |
| hr3 | Startkandidat (adaptives AAC bis 192, höchste Stufe erzwingen) | URL fehlt (Region wählen), nicht verifiziert |
| ROCK ANTENNE, ANTENNE BAYERN Classic Rock, MDR JUMP, 80s80s | zu prüfen | nur aufnehmen, wenn AAC ≥ 192 kbit/s oder FLAC tatsächlich verfügbar |

**Wichtiger Befund:** In der Community-Liste [ubuntuusers: Internetradio/Stationen](https://wiki.ubuntuusers.de/Internetradio/Stationen/) (abgerufen 18.11.2026, möglicherweise veraltet) stehen für 1LIVE und WDR 2 URLs mit `mp3/128` und für hr3 „128kbit MP3 stereo“. Die Angabe „AAC 192 kbit/s“ wurde **nicht belegt**. Nach E-6 wären diese bekannten URLs **abzulehnen**. In der Vorlage stehen deshalb bewusst **Platzhalter-URLs** (`example.invalid`), keine erfundenen Adressen. Echte, geprüfte AAC-URLs müssen vom Nutzer oder aus der Senderseite des jeweiligen Anbieters stammen und per Qualitätsprüfung bestätigt werden.

## 5. E1b: Architektur (Raum-EQ für Radio, moOde/CamillaDSP-Prinzip)
```
Sender-URL (HTTP/HLS)
  → ffmpeg A (Decoder): AAC/FLAC → PCM (feste Samplerate, 2 Kanäle) über stdout
  → Leser-Thread → Ringpuffer (Reserve gegen Netzschwankungen)
  → Raum-EQ-Block (IIR-Filterkette mit Zustand über Blöcke, Preamp/Headroom)
  → ffmpeg B (Encoder): PCM → FLAC (Live) über stdout
  → MediaServer /s/<token> (Live-Stream, E1)
  → KEF Gen1
```
- **ffmpeg als externe Stufe (subprocess):** nur Argumentliste, nie Shell; `-nostdin`; **Protokoll-Whitelist** (nur `http,https,tcp,tls,crypto`; kein `file`, keine fremden Protokolle); Zeitüberschreitungen (`-rw_timeout`); ffmpeg-Ausgaben werden vor dem Loggen von URLs bereinigt (Maskierung).
- **ffmpeg-Herkunft und Sicherheit (Projektregeln):** wird **nie automatisch geladen oder ausgeführt**. Der Nutzer installiert es selbst aus einer offiziellen Quelle; Pfad per `--ffmpeg PFAD`; beim Start `ffmpeg -version`, Pfad und Version werden protokolliert, optional Prüfsumme festpinnen. Hinweis: Windows-Builds sind häufig **unsigniert**; Windows Application Control wird **nicht umgangen**. Ist ffmpeg blockiert oder fehlt, ist E1b mit klarer Meldung nicht verfügbar (E1-Durchleitung bleibt nutzbar, aber ohne EQ). Ich habe ffmpeg weder installiert noch ausgeführt.
- **Raum-EQ:** vorhandene Filterkette wiederverwenden. **Befund im Code:** `dsp.apply_profile` verarbeitet ein ganzes Signal mit `scipy.signal.sosfilt` **ohne Zustandsübergabe** zwischen Blöcken; für Live ist ein blockweiser Weg mit Zustand (`zi`) nötig (Refactor, mit Test „Blockweise = am Stück“). Ein Limiter/„Ceiling“ mit Vorausschau ist live nicht ohne Latenz möglich; Schutz gegen Clipping per fest berechnetem Preamp (Projektregel Headroom). Nur Minimalphasen-IIR (kein FIR ohne belegtes Ziel).
- **Doppelfilterung:** Dieser Weg läuft über DLNA, nicht über Equalizer APO. Der Player-Profil-EQ ist hier der einzige PC-seitige EQ; keine zusätzliche gleichartige Korrektur an der KEF/Saxx aktivieren.
- **Kontrollierter Reconnect bei Stream-Abbruch:**
  - Erkennung: ffmpeg A endet oder liefert für N Sekunden keine PCM-Daten.
  - Wiederholung mit begrenztem Backoff, Versuchsbudget je Zeitfenster und Gesamtbudget (wie `repair_budget.py`), danach sauberes Ende mit Kategorie (`stream_reconnect_exhausted`).
  - Während der Lücke **Stille einspeisen**, damit die Verbindung zur KEF offen bleibt (**Annahme**, dass die Gen1 dann nicht stoppt; ungeprüft).
  - Greift nie in `RENDERER_LOCK` ein und fasst den Renderer nicht an; erneute Qualitätsprüfung vor jeder Wiederverbindung (E-6 gilt auch dann).
  - Audit: Ereignisse (Start, Ergebnis, blockiert) im JSONL-Protokoll; Status-API (E3).
  - **Offen E-11:** Reconnect standardmäßig an (nur Upstream, kein Renderer-Eingriff) oder analog D2 hinter `--enable-repair`?
- **Puffer und Pegel:** Ringpuffer-Unterlauf → Stille plus Zähler/Ereignis; Überlauf → ältesten Block verwerfen plus Zähler. Zielwerte (Reserve, Latenz) **nur durch Messung im Simulator festlegen**, hier keine Zahlen.
- **Ausgabeformat zur KEF:** FLAC-Livestream (Annahme, dass die Gen1 einen endlosen FLAC-Strom ohne Länge akzeptiert; **ungeprüft**; Alternative WAV/PCM nur falls FLAC scheitert).

## 6. Arbeitspakete
- **E1 (erledigt, gemergt):** Durchleitung (`kefplayer radio`), MP3-Ablehnung, Favoriten (`name`,`url`), maskierte URLs, Live-DIDL, Wartezeit ohne Zeitgrenze.
- **E1.1:** Anpassung an E-6 (nur FLAC/AAC), ffprobe-Qualitätsprüfung, Auswertung der Favoritenfelder, `kefplayer radio-check`. Tests mit Fake-ffprobe (CI ohne ffmpeg lauffähig), echte ffmpeg-Tests nur wenn vorhanden (Skip).
- **E1b:** Decoder-/Encoder-Stufe (ffmpeg), blockweiser Raum-EQ mit Zustand, Ringpuffer, Reconnect, Audit. Ebenfalls mit Fake-Decoder-Skript im Simulator.
- **E2:** Fehlerkategorien Stream (`stream_unreachable`, `stream_http_error`, `stream_format_unsupported`, `stream_quality_unverified`, `stream_stalled`, `stream_reconnect_exhausted`), D0-Hypothesen, D1 nur Schatten.
- **E3:** Status-API/Health für Radio, optional ICY-Titelanzeige (E-8).
- **E5:** GUI-Senderliste (später).

## 7. Tests und CI
- Simulator mit Fake-Sender und **Fake-Decoder/Encoder** (kleine Python-Skripte statt ffmpeg), damit Ubuntu- und Windows-CI ohne ffmpeg laufen. **Offen E-13:** ffmpeg in der CI installieren (Windows: Paketmanager) oder nur lokal optional testen?
- Pflichtfälle: Zustand der Filterkette blockweise = am Stück; Stille bei Unterlauf; Reconnect-Budget; Ablehnung MP3/AAC < 192/HE-AAC/unbekannt; Maskierung von URLs in ffmpeg-Fehlerausgaben; Protokoll-Whitelist; kein Zugriff auf `RENDERER_LOCK`.

## 8. Nicht prüfbar vor März 2027
Annahme von `audioBroadcast`, `DLNA.ORG_OP=00` und endlosen FLAC-Strömen durch die Gen1; Verhalten bei Stille-Einspeisung und Senderabbruch; Latenz-/Pufferverhalten in der Praxis; Raumkorrektur-Wirkung nur durch Messung (UMIK-1/REW, nach März 2027).

## 9. Offene Entscheidungen (Stand heute)
- **E-8** ICY-Titelanzeige gewünscht?
- **E-9** Toleranz der gemessenen Bitrate; **E-10** nur AAC-LC zulassen?
- **E-11** Reconnect Standard an oder hinter `--enable-repair`?
- **E-13** ffmpeg in der CI?
- Echte, geprüfte **AAC-≥192-/FLAC-URLs** der Startsender (liefert der Nutzer oder die Senderseite; nicht aus Community-Listen übernehmen).

## 10. Verweise
`docs/radio.md` (E1), `docs/radio-plan.md` (diese Planung auf main), `examples/favorites.example.json`, Projektdokument `claude/phase_e0_befunde.md` (nur im Projekt).
