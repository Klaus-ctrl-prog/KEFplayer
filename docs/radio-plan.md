# KEFplayer – Phase E: Internetradio (Plan, Stand 18.11.2026, abgeschlossen für heute)

Status: **E0 abgeschlossen, E1 gemergt (`0df3584`), E1b geplant (noch kein Code).** Alles nur im Simulator entwickelt; **nichts davon ist an einer echten KEF Gen1 geprüft** (Hardware-Tests frühestens März 2027). Annahmen sind als solche markiert.

## 1. Feste Vorgaben (Nutzer, 18.11.2026)
- **E-1:** Variante B (PC holt den Stream und leitet ihn über den Medienserver an die KEF).
- **E-2:** nur eigene Sender-URLs / Favoritenliste (kein Verzeichnis-Scraping).
- **E-3:** Raum-EQ für Radio (E1b) kommt auf jeden Fall.
- **E-6 (Stand 18.11.2026, 07:35, gelockert durch den Nutzer):** zulässig sind **FLAC** und **AAC ab 128 kbit/s**. **Ziel bleibt AAC ≥ 192 kbit/s** („volle Qualität“); **AAC von 128 bis unter 192 kbit/s** ist zulässig, wird aber in Prüfung und Anzeige als **„reduzierte Qualität“** gekennzeichnet. **AAC unter 128 kbit/s und MP3 (jede Bitrate) werden abgelehnt.** Die Untergrenze 128 und die Zielmarke 192 sind feste Nutzerentscheidungen; das Feld `min_bitrate_kbps` je Favorit darf die Untergrenze nur **anheben**, nicht senken.
  - Frühere Fassung (nur FLAC oder AAC ≥ 192) ist damit ersetzt. Die Qualitätsprüfung vor dem Start bleibt (ffprobe/ffmpeg). Die tatsächliche Stream-Qualität (Codec, Bitrate) wird **vor** der Wiedergabe auf der KEF verifiziert (ffprobe/ffmpeg).
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
   - AAC: zulassen nur wenn **Codec = AAC** und (deklarierte Bitrate **oder**, falls nicht deklariert, gemessene Bitrate) ≥ 128 kbit/s (mit festgelegter Toleranz, E-9). Ab 192 kbit/s „volle Qualität“, von 128 bis unter 192 „reduzierte Qualität“ (zulässig, gekennzeichnet). Bei Widerspruch zwischen deklariert und gemessen: **ablehnen**, nicht raten.
   - Alles andere, unbekannt oder nicht messbar: **ablehnen** mit Kategorie `stream_format_unsupported` bzw. `stream_quality_unverified`. Unbekannt gilt nie als gut.
5. Erst nach bestandener Prüfung wird `SetAVTransportURI` an die KEF gesendet.
- **Offene Entscheidungen:** E-9 Toleranz bei der gemessenen Bitrate (Vorschlag: nur ein kleiner Abschlag wegen Messfenster; Wert erst nach Messungen im Simulator festlegen, hier keine Zahl erfunden); E-10 nur **AAC-LC** zulassen und HE-AAC (SBR/PS) ablehnen? (Vorschlag: ja, da HE-AAC bei gleicher Nennbitrate ein Effizienzcodec ist; bitte entscheiden).
- **Adaptive Streams (HLS, z. B. hr3):** ffmpeg wählt eine Variante; die **höchste Stufe muss erzwungen** werden (Variante gezielt wählen) und die Prüfung gilt für die tatsächlich gewählte Variante. Ob die Startkandidaten entsprechende URLs anbieten, ist **nicht belegt**.

## 4. Startliste (Rock, Pop, Classic Rock) und Vorlage
Vorlage: `docs/favorites.example.json` (Liste von Objekten, angelehnt an die strukturierte Senderverwaltung von moOde Audio): `name`, `url`, `genre`, `expected_codec`, `min_bitrate_kbps`, `status` (`candidate`/`to_verify`/`verified`/`rejected`), `verified_at`, `notes`. Der heutige Loader liest `name` und `url`, die übrigen Felder werden ignoriert (Auswertung folgt mit E1.1).

| Sender | Rolle | Stand |
|---|---|---|
| 1LIVE | Startkandidat (laut Nutzerhinweis HLS-AAC 192, Profil offen) | URL fehlt, nicht belegt; **HLS nur über E1b/ffmpeg**, E1 kann kein HLS |
| WDR 2 | Startkandidat (laut Nutzerhinweis HLS-AAC 192, Profil offen) | URL fehlt (Region wählen), nicht belegt; **HLS nur über E1b/ffmpeg**, E1 kann kein HLS |
| hr3 | Startkandidat (adaptives AAC bis 192, höchste Stufe erzwingen) | URL fehlt (Region wählen), nicht verifiziert |
| SWR3 | vom Nutzer gewünscht (18.11.2026), unabhängig von der Qualität aufgenommen | URL fehlt (nur Verzeichnisse gefunden), E-6-Konformität offen |
| ROCK ANTENNE Live, ROCK ANTENNE Classic Rock, ROCK ANTENNE Bayern Classic Rock | zu prüfen | **offizielle URLs eingetragen** (Streamlink-Seiten rockantenne.de / rockantenne.bayern, abgerufen 18.11.2026, `.../stream/aacp`); die Seiten nennen **keine Bitrate**, `aacp` kann HE-AAC bedeuten (nicht belegt); Aufnahme als `verified` erst nach Stream-Prüfung (E-6) |
| ROCK ANTENNE Österreich Classic Rock | zu prüfen | **offizielle URL eingetragen** (rockantenne.at/empfang/streamlinks, abgerufen 18.11.2026, `.../classic-perlen/stream/aacp`); keine Bitrate genannt, HE-AAC-Risiko wie oben |
| ROCK ANTENNE 70er Rock, 80er Rock, OLDIE ANTENNE (80er/90er) | zu prüfen | **offizielle URLs eingetragen** (rockantenne.de, `.../stream/aacp`); keine Bitrate genannt, HE-AAC-Risiko |
| Radio Swiss Pop (Pop/Softrock, Schweiz) | zu prüfen | offizielle SRG-Tabelle (broadcast.ch, abgerufen 18.11.2026): 192 kbps, Codec dort als **„aacPlus“ (= HE-AAC)** angegeben, Profil nicht ausdrücklich genannt; bei E-10 (nur AAC-LC) voraussichtlich **abgelehnt**; URL der Tabelle ist `http://` |
| ANTENNE BAYERN Classic Rock | zu prüfen | URL **vom Nutzer genannt** (`stream.antenne.de/classic-rock-live/stream/aacp`, Herkunft KI-Text); offizielle Seite für mich nicht abrufbar, Adresse **nicht belegt** |
| MDR JUMP, 80s80s | zu prüfen | nur aufnehmen, wenn AAC ≥ 192 kbit/s oder FLAC tatsächlich verfügbar |

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
`docs/radio.md` (E1), `docs/radio-plan.md` (diese Planung auf main), `docs/favorites.example.json`, Projektdokument `claude/phase_e0_befunde.md` (nur im Projekt).

**Aussortiert nach E-6 ohne Messung (laut Nutzerhinweis/Verzeichnissen, nicht von mir belegt):** RADIO BOB! (Verzeichnisse nennen MP3 192; AAC-Zugang niedriger), 0nlineradio Classic Rock (Verzeichnis: MP3 192). Radio 88.6 Classic Rock: AAC-Links noch nicht für 192 kbit/s qualifiziert; keine URL übernommen.

## 11. Folgen der Lockerung (Stand 18.11.2026)
- Direkte AAC-Streams mit 128 bis 191 kbit/s sind jetzt zulässig (gekennzeichnet). Radio Swiss Pop in der 96-kbps-Variante bleibt abgelehnt (< 128); die 192-kbps-Variante hängt an E-10 (HE-AAC „aacPlus“).
- **MP3 bleibt aus:** Die bekannten WDR-/1LIVE-/hr3-Adressen der Community-Liste (MP3 128) sind weiterhin **nicht zulässig**. Zulässig wären nur AAC-Streams dieser Sender (z. B. HLS-AAC über E1b), sobald sie belegt sind.
- E1 (gemergt) lässt weiterhin Ogg/Opus/MP4-Audio zu und prüft keine Bitrate; Angleichung an diese Regel in E1.1.
- Offene Entscheidungen unverändert: E-8, E-9, E-10, E-11, E-13.

**Hinweis zur Liste (18.11.2026):** Die Liste enthält auf Wunsch des Nutzers alle genannten Sender **unabhängig von der Qualität** (derzeit 15 Einträge, inkl. SWR3; hr3 war bereits enthalten). Ob ein Sender abgespielt wird, entscheidet weiterhin die Prüfung nach E-6; eine Ausnahme dafür ist nicht festgelegt (offen, E-14: Sender trotz Nichterfüllung von E-6 manuell freigeben?).

## 12. Urheberrecht und Quellen (Stand 18.11.2026; keine Rechtsberatung)
- **Nur Wiedergabe, kein Mitschnitt:** Der Stream wird nur im Arbeitsspeicher gepuffert (Ringpuffer) und live an die eigene KEF im Heimnetz geleitet. **Kein Speichern auf Platte, kein Cache, keine Aufzeichnung, keine Weitergabe** an Dritte oder ins Internet. Die Medienserver-Bindung (`--bind`) soll im Heimnetz bleiben.
- **Verzeichnisdienste (z. B. radio.de):** keine automatische Abfrage, kein Scraping und keine Übernahme von Senderlisten (E-2 bleibt). Die Nutzungsbedingungen von radio.de habe ich **nicht gelesen**; ob und wie deren Daten oder Weiterleitungs-URLs genutzt werden dürfen, ist **ungeprüft**. Vorrang haben die **offiziellen Streamlink-Seiten der Sender**. Wer eine Adresse aus einem Verzeichnis von Hand in die eigene Favoritenliste übernimmt, trägt Quelle und Datum in `notes` ein; sie wird wie jede Nutzer-URL behandelt (Prüfung nach E-6).
- **Verzeichnis-Weiterleitungen** können Werbung, Tracking-Parameter oder wechselnde Adressen enthalten; URLs werden in Meldungen ohnehin maskiert.
