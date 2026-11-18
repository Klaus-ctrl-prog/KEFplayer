# E1b Detailentwurf: Radio mit Raum-EQ (ffmpeg-Stufe), Stand 18.11.2026

Status: **Entwurf, kein Code.** Nichts ist an einer echten KEF Gen1 geprüft (frühestens März 2027). Annahmen sind markiert. Grundlage: `docs/radio-plan.md`.

## 1. Entscheidungen (Nutzer, 18.11.2026)
- **E-8:** ICY-Titelanzeige erst später (E3), nicht Teil von E1b.
- **E-17:** Samplerate = Senderrate (kein unnötiges Resampling), Rückfall 48 kHz, `--radio-rate` überschreibt. Siehe Abschnitt 6.
- **E-11:** Reconnect **standardmäßig an**, betrifft nur die Verbindung zum Sender, nie den Renderer; mit Versuchsbudget und Protokoll. Abschaltbar mit `--no-reconnect`.
- **E-13:** ffmpeg **nicht** in der CI. CI testet mit Fake-Decodern; echte ffmpeg-Tests laufen nur lokal und werden sonst übersprungen.
- **E-15 (entschieden):** Ausgabe mit **24 Bit**.
- **E-16 (entschieden):** Ausgabe **ohne FLAC**: unkomprimiertes **WAV (PCM 24 Bit)**. Es gibt **keinen Encoder-Prozess**; die WAV-Verpackung macht Python selbst. Es läuft nur **ein** ffmpeg (Decoder).
- Weiter gelten E-1 bis E-3, E-6 (FLAC oder AAC ab 128 kbit/s, MP3 aus), E-12 (ffmpeg extern).

## 2. Befunde im vorhandenen Code (`kefplayer/dsp.py`, gelesen)
- `Profile` enthält gemeinsame Filter, **je Kanal** Filter, Pegel (`gain_db`) und **Verzögerung** (`delay_ms`) sowie einen Preamp (`preamp_db`, automatisch über `auto_preamp_db(nch, fs)`).
- `apply_profile` arbeitet auf einem **ganzen Stück**: `sosfilt` ohne Zustand, Verzögerung durch Verlängerung des Arrays, **Clipping-Schutz durch Skalierung des gesamten Stücks** (`ceiling`). **Alle drei Punkte sind live nicht übertragbar** (kein Zustand zwischen Blöcken, keine Delay-Leitung, keine Stückspitze).
- Filter werden mit `f.sos(fs)` für eine feste Samplerate entworfen: Im Live-Betrieb muss die Samplerate fest sein.

## 3. Datenfluss und Threads
```
Sender-URL ─► [Qualitätsprüfung E1.1] ─► ffmpeg (Decoder) ─stdout PCM f32le─► Leser-Thread
   ─► Ringpuffer ─► Verarbeitungs-Thread (StreamingProfile) ─► WAV-Verpackung 24 Bit (Python) ─► MediaServer /s/<token> ─► KEF
```
- **Leser-Thread:** liest ffmpeg-stdout in festen Blöcken, schreibt in den Ringpuffer, erkennt Stillstand (kein Datenfluss für N Sekunden) und Prozessende.
- **Stderr-Thread:** leert ffmpeg-stderr dauernd (sonst Blockade der Pipe), bereinigt URLs/Tokens und hält nur die letzten Zeilen für die Diagnose.
- **Verarbeitungs-Thread:** zieht Blöcke aus dem Ringpuffer, wendet das Profil an, wandelt in 24-Bit-PCM und übergibt an die WAV-Verpackung; bei Unterlauf Stille.
- **Medienserver-Anbindung:** Die WAV-Ausgabe wird dem bestehenden Live-Handler (E1) als Quelle (`read1/close`) gegeben; der Handler endet, wenn der Stream abgemeldet wird.
- **Beenden:** Stopp/Strg+C → Stream abmelden, ffmpeg-Prozesse beenden (zuerst sanft, dann hart, unter Windows inkl. Kindprozessen), Threads zusammenführen, Pipes schließen; keine Zombie-Prozesse (Test).

## 4. ffmpeg: Aufruf, Herkunft, Sicherheit
- **Herkunft:** nur vom Nutzer installiert; **absoluter Pfad** per `--ffmpeg PFAD` (keine PATH-Suche, kein Standardordner: unter Windows würde die Suche sonst auch das aktuelle Verzeichnis berücksichtigen). Beim Start: Datei vorhanden, `ffmpeg -version` auslesen, Pfad, Version und **SHA-256 der Datei** ins Protokoll; optional `--ffmpeg-sha256` zum Festpinnen (Abweichung = Abbruch). Es wird **nichts heruntergeladen oder ausgeführt, was nicht der Nutzer ausdrücklich angegeben hat**. Windows-Builds sind oft **unsigniert**; Windows Application Control wird **nicht umgangen**: Blockiert sie ffmpeg, meldet E1b „nicht verfügbar“ (E1 bleibt nutzbar, ohne EQ).
- **Aufruf:** `subprocess` mit Argumentliste, `shell=False`, `stdin=DEVNULL`, `-nostdin`, `-hide_banner`, `-loglevel warning`, `-protocol_whitelist http,https,tcp,tls,crypto`, `-rw_timeout`, `-reconnect*`-Optionen **nicht** verwenden (Reconnect macht der Supervisor, damit er geprüft und gezählt ist), Ausgabe `-f f32le -ac 2 -ar <Rate> pipe:1`.
- **URL-Hygiene:** URL nur als Argument; in Meldungen und Protokoll maskiert (Schema://Host:Port/...); stderr wird vor dem Speichern bereinigt.
- **HLS:** ffmpeg wählt die Variante; die **höchste Stufe muss erzwungen** werden (Variantenwahl per Option oder direkter Varianten-URL), und die Qualitätsprüfung gilt für die tatsächlich gewählte Variante. Ob die Sender entsprechende URLs anbieten, ist **nicht belegt**. **Umgesetzt in Schritt 6** (`kefplayer/hls.py`): höchste Variante gewählt, ffmpeg bekommt die Varianten-URL, Messung aus den Segment-Nutzdaten, siehe `docs/radio.md`.

## 5. Raum-EQ im Live-Betrieb (Refactor, noch nicht implementiert)
Neue Klasse **`StreamingProfile`** (in `dsp.py` oder eigenem Modul), die das vorhandene `Profile` wiederverwendet:
- **Filterzustand:** je Kanal `sos = vstack(f.sos(fs))` einmal berechnen; `sosfilt(sos, block, zi=zi)` mit **übergebenem und zurückgegebenem Zustand**. Startzustand Null.
- **Pegel:** `preamp_db` und `channel_gain_db` fest einmal bestimmt. **Auto-Preamp** aus `auto_preamp_db(nch, fs)` (Headroom nach größter Anhebung des Profils), kein stückweises Skalieren.
- **Verzögerung:** pro Kanal eine Delay-Leitung (Ringpuffer) mit Zustand über Blöcke.
- **Clipping:** statt Stückskalierung **fester Headroom** (Preamp); zusätzlich **Zähler** für Übersteuerung beim Umwandeln ins Ausgabeformat und Warnung im Protokoll. Ein Limiter mit Vorausschau kostet Latenz und ist **nicht** vorgesehen (offen, falls Zähler Übersteuerung zeigen).
- **Äquivalenztest (Pflicht):** Verarbeitung in Blöcken beliebiger Größe (auch 1 Sample, ungerade Größen) liefert **dasselbe Ergebnis wie `apply_profile` am Stück** (ohne Stückskalierung), innerhalb Rechengenauigkeit.
- **Profilwechsel im laufenden Stream:** nicht in E1b (Neustart des Streams); später mit Überblendung.
- **Neutrales Profil:** Durchleitung der PCM-Daten ohne Filter (nur Format/Pegelweg getestet).
- **Projektregeln:** nur Minimalphasen-IIR, keine FIR; keine Boosts in Auslöschungen (liegt am Profil, nicht am Player); Hinweis im Start: Dieser Weg läuft über DLNA, **nicht** über Equalizer APO, keine Doppelfilterung zusammen mit KEF/Saxx-Korrektur gleicher Art.

## 6. Ausgabeformat zur KEF (entschieden: WAV, 24 Bit, ohne FLAC)
- **Format:** WAV (RIFF, `fmt ` mit PCM-Formatkennung 1, 24 Bit, 2 Kanäle, little endian), Inhaltstyp **`audio/wav`** (WAV-Dateien über DLNA sind im Projekt bereits üblich; **Annahme**, dass die Gen1 24-Bit-WAV akzeptiert, ist ungeprüft).
- **Endlosstrom:** Die Länge ist unbekannt. Der WAV-Kopf wird mit **Größenfeldern 0xFFFFFFFF** geschrieben (**Annahme**, dass die Gen1 das als „unbekannte Länge“ liest; Alternative: sehr große Länge). Der Live-Handler von E1 sendet ohne Content-Length (kein Seek).
- **Umwandlung Float → 24 Bit:** Runden auf ganze 24-Bit-Werte, Begrenzung bei ±Vollaussteuerung mit **Übersteuerungszähler**. Bei 24 Bit ist kein Dither vorgesehen (Quantisierungsrauschen weit unter hörbar; kein Messwert, Modellannahme).
- **Bandbreite:** unkomprimiert, im LAN unkritisch (24 Bit × 2 Kanäle × Rate; bei 48 kHz rund 2,3 Mbit/s, aus der Formel gerechnet).
- **Samplerate (E-17, entschieden 18.11.2026, 10:53: „best mögliche“, Auslegung):** keine unnötige Umrechnung. Der Decoder liefert die vom Qualitätscheck ermittelte Samplerate des Senders unverändert (bei AAC meist 44,1 oder 48 kHz). ffmpeg resampelt nur, wenn die Rate nicht ermittelbar oder nicht zulässig ist; Rückfall 48 kHz. Die Filter werden für die tatsächlich verwendete Rate entworfen. `--radio-rate` bleibt als manuelle Überschreibung. Welche Raten die Gen1 als Endlosstrom akzeptiert, ist **ungeprüft** (frühestens März 2027).
- **Nicht vorgesehen:** FLAC-Ausgabe und ein zweiter ffmpeg-Prozess. Falls die Gen1 den WAV-Endlosstrom nicht annimmt, ist das nach dem Hardwaretest (März 2027) neu zu bewerten.

## 7. Ringpuffer, Unterlauf, Überlauf
- Puffer nur im **Arbeitsspeicher** (keine Datei, kein Cache, kein Mitschnitt; siehe Plan, Abschnitt 12).
- **Unterlauf:** Stille einspeisen, Zähler und Ereignis `radio_underrun`. **Überlauf:** ältesten Block verwerfen, Zähler und Ereignis.
- **Zielwerte** (Reserve, maximale Füllung, Blockgröße) **nur durch Messung im Simulator festlegen**; hier absichtlich keine Zahlen.
- Taktabweichung zwischen Sender und KEF wird vom Puffer aufgefangen, da die KEF den Strom über TCP abruft (Rückdruck): **Annahme**, am Gerät zu prüfen.

## 8. Reconnect (kontrolliert, E-11)
- **Zustände:** `connecting → streaming → stalled → reconnecting → streaming | failed`.
- **Auslöser:** ffmpeg endet, oder kein PCM-Datenfluss für N Sekunden (N durch Messung festzulegen).
- **Ablauf:** alten ffmpeg beenden → Wartezeit mit begrenztem Backoff → **erneute Qualitätsprüfung (E-6)** → neuer ffmpeg. Währenddessen **Stille** an die WAV-Verpackung, damit die Verbindung zur KEF offen bleibt (**Annahme**, dass die Gen1 dann nicht stoppt).
- **Budget:** je Zeitfenster und gesamt begrenzt (Muster `repair_budget.py`, nur Zähler, keine Schleifen ohne Ende). Danach sauberes Ende mit Kategorie `stream_reconnect_exhausted`.
- **Abgrenzung:** Reconnect berührt weder Renderer noch `RENDERER_LOCK` und gehört **nicht** zur D2-Reparatur (kein `--enable-repair`). Die Wiedergabesitzung (`track_active`) bleibt während des Reconnects gesetzt, damit der Health-Monitor den Renderer nicht abfragt.
- **Protokoll:** Ereignisse `radio_reconnect_start/result/blocked` (Ziel maskiert), Zähler im Status.
- **Umsetzung Schritt 4 (`kefplayer/radio_supervisor.py`, Simulator, 18.11.2026):** Zustände `idle | connecting | streaming | reconnecting | failed | stopped`. Ein Qualitätsurteil `rejected` beendet endgültig (Wiederholen ändert nichts), ein Verbindungsfehler wird mit Backoff wiederholt. Der **erste** Versuch wird standardmäßig nicht wiederholt (sofortige Fehlermeldung), `retry_initial` ändert das. Ereignisse: `radio_connect`, `radio_stream_up`, `radio_gap`, `radio_reconnect_wait`, `radio_failed` (Quelle maskiert). Stillstand = kein PCM seit `stall_s`; der haengende Decoder wird beendet, die Pipeline meldet `gap`, der Verbraucher bekommt Stille.
- **Arbeitswerte (NICHT gemessen, nur Startwerte):** Backoff 2, 5, 10, 30, 60 s (danach 60 s); höchstens 5 Versuche je 600 s und 30 je Sitzung; Stillstand nach 10 s; Backoff-Reset nach 60 s ununterbrochenem Betrieb. Festlegung nach Messung (frühestens mit echtem Sender/Hardware) offen.


## 9. Fehlerkategorien und Ereignisse (für E2/E3)
`stream_unreachable`, `stream_http_error`, `stream_format_unsupported`, `stream_quality_too_low`, `stream_quality_unverified`, `stream_stalled`, `stream_reconnect_exhausted`, `decoder_unavailable` (ffmpeg fehlt/blockiert/Pin verletzt), `decoder_failed` (Prozess beendet, Exitcode), `output_failed`. Status (E3): Zustand, Reconnect-Zähler, Unter-/Überläufe, Übersteuerungszähler, Pufferfüllung, ffmpeg-Version. Alle Ausgaben ohne volle URL.

## 10. Fehlerbilder
| Fall | Verhalten |
|---|---|
| ffmpeg fehlt, blockiert oder Prüfsumme weicht ab | E1b nicht verfügbar, klare Meldung, kein Start |
| Sender bricht ab | Reconnect nach Abschnitt 8 |
| Sender liefert Qualität unter E-6 nach Reconnect | Reconnect abgelehnt, Ende |
| ffmpeg hängt ohne Ausgabe | Stillstandserkennung, Prozess beenden, Reconnect |
| ffmpeg stderr voll | nicht möglich (eigener Lese-Thread) |
| KEF trennt die Verbindung | Stream abmelden, Prozesse beenden, Ende mit Grund |
| Pufferunterlauf | Stille plus Zähler |
| Profil nicht ladbar | Start abgelehnt vor jeder Verbindung |

## 11. Tests (Simulator, ohne ffmpeg in der CI)
- **Fake-Decoder:** kleines Python-Skript, das PCM (Sinus/Impulse) in Echtzeit liefert, abbricht, hängt oder Müll schreibt; Pfad per `--ffmpeg`-Ersatz für Tests (nur im Testcode).
- **WAV-Verpackung:** echter Code (kein Fake nötig): Kopf mit unbekannter Länge, 24-Bit-Umwandlung (Rundung, Begrenzung, Zähler), Ausgabe wird von `MockRenderer` oder einem Testleser gelesen und gegen die Eingabe verglichen.
- **Pflichtfälle:** Äquivalenztest Block vs. Stück; Delay-Leitung über Blockgrenzen; Auto-Preamp verhindert Übersteuerung bei Testprofil; Unterlauf/Überlauf-Zähler; Reconnect-Budget und -Backoff mit Fake-Uhr; erneute Qualitätsprüfung vor Reconnect; Stillstandserkennung; Prozessende ohne Zombies; stderr-Bereinigung (keine URL im Protokoll); ffmpeg-Pfadprüfung (relativ, fehlend, Prüfsumme); kein Zugriff auf `RENDERER_LOCK`; Stoppen während Reconnect; Windows- und Ubuntu-CI.
- **Lokal optional (nicht CI):** echter ffmpeg gegen lokalen Fake-Sender.

## 12. Nicht prüfbar vor März 2027 (Hardware)
Akzeptanz eines endlosen WAV-Stroms (24 Bit, unbekannte Länge) und `audioBroadcast`; Verhalten bei Stille-Einspeisung; Puffer-/Latenzverhalten; Raumkorrektur-Wirkung (nur durch Messung mit UMIK-1/REW).

## 13. Arbeitsschritte E1b (jeweils eigener PR, CI auf Ubuntu und Windows grün)
1. `StreamingProfile` samt Äquivalenztests (reiner DSP-Code, kein ffmpeg).
2. ffmpeg-Wrapper (Pfadprüfung, Version/Prüfsumme, Prozessführung, stderr-Bereinigung) mit Fake-Decoder-Tests.
3. Ringpuffer, Leser- und Verarbeitungs-Thread, WAV-Verpackung (24 Bit), Anbindung an den Live-Handler.
4. Reconnect-Supervisor mit Budget und Protokoll.
5. CLI (`--ffmpeg`, `--ffmpeg-sha256`, `--profile`, `--no-reconnect`, `--radio-rate`), Doku, Windows-Testlauf mit ZIP.
6. HLS (`.m3u8`) mit `--ffmpeg`: Variantenwahl, Prüfung der gewählten Variante, `radio-check --hls`.
