# KEFplayer

KEFplayer spielt Audiodateien (FLAC/WAV) per UPnP/DLNA auf einem Renderer ab (Ziel: KEF LS50 Wireless Gen1) und kann die Dateien vorher mit einem Filterprofil bearbeiten (PEQ, Preamp mit automatischem Headroom, Pegel/Delay je Kanal). Pro Titel wird protokolliert, was tatsächlich gesendet wurde und wie sich der Renderer verhielt.

Version 0.3.0 (grafische Oberfläche als zusätzlicher Einstieg, Kern unverändert gegenüber 0.2.1; Status-API seit 0.2.0). Python-Paket und Befehl heißen `kefplayer`. Lizenz: MIT. Änderungen: siehe [CHANGELOG.md](CHANGELOG.md).

> **Stand der Prüfung:** Getestet wurde bisher nur gegen einen **simulierten Renderer**. **Ein echter KEF-Lautsprecher wurde noch nicht getestet.** Die Validierung mit echter Hardware steht aus und ist frühestens ab März 2027 vorgesehen. Es gibt keine Aussage über einen nachgewiesenen Klanggewinn oder eine validierte Kompatibilität mit KEF-Hardware.

## Einordnung

- Entwickelt und getestet ausschließlich gegen einen simulierten Renderer (`tests/mock_renderer.py`). Das Verhalten der echten KEF ist damit **nicht belegt**.
- Das Standardprofil ist **neutral**: ohne Profil und ohne `--format`/`--bits` werden Dateien unverändert gesendet. Es gibt keine Bass-Voreinstellung. Beispielwerte in dieser Datei sind Syntaxbeispiele, keine Empfehlungen und keine Messwerte.
- Nicht enthalten: Musikbibliothek, Internetradio, FIR/Faltung, Lautstärkeregelung (die Lautstärke des Renderers wird nie verändert). Die Oberfläche (tkinter, einfach gehalten, siehe „Oberfläche (GUI, 0.3.0)“) ist ein zusätzlicher Einstieg neben der Kommandozeile.

## Installation

Python ab 3.10 (Mindestangabe in `pyproject.toml`). Historischer Prüfstand von Version 0.2.1: Linux mit CPython 3.10, 3.11, 3.12 und 3.13; Windows 11 mit CPython 3.12.10 (Einzelheiten unter „Tests und Prüfstand“). Für Version 0.3.0 wurde unter Linux nur CPython 3.12.3 neu geprüft. Abhängigkeiten: numpy, scipy, soundfile (alle von PyPI). Reiner Python-Quelltext, keine EXE. Zum Bauen wird `setuptools>=77` benötigt; `pip` holt es bei der Installation selbst.

Im Projektordner (dort liegt `pyproject.toml`):

    pip install .
    kefplayer --version          # oder, falls das Skript nicht im PATH liegt:  python -m kefplayer --version

## Befehle

| Befehl | Zweck |
|---|---|
| `kefplayer discover` | Renderer per SSDP suchen, zeigt IP und Beschreibungs-URL |
| `kefplayer probe [--renderer IP-oder-URL]` | Gerät, Dienste, Transportstatus und die vom Gerät gemeldeten Formate (Eigenangabe, kein Wiedergabetest) |
| `kefplayer response --profile P.json --fs 44100` | Rechnerischen Frequenzgang je Kanal und Preamp anzeigen, ohne etwas abzuspielen |
| `kefplayer process DATEI... --out ORDNER [--profile P.json]` | Dateien filtern und speichern |
| `kefplayer health [--renderer IP-oder-URL] [--json]` | Einmalige Gesundheitsprüfung: Renderer (HTTP, UPnP), Medienserver-Port (ohne laufenden Server muss er frei sein), Cache. Exitcode 1 nur bei `error`; `warn`/`unknown` liefern 0 (Entwicklungsstand nach 0.3.0) |
| `kefplayer play DATEI-ODER-ORDNER... [--profile P.json]` | Abspielen auf dem Renderer, Protokoll in `kefplayer_log.jsonl`; lokale, lesende Status-API auf `127.0.0.1:8766` (abschaltbar mit `--no-api`) |

Während `play` im Terminal: `n` = nächster Titel, `p` = Pause/Weiter, `q` = Ende (jeweils mit Enter).

Wichtige Optionen bei `play`:
- `--format flac|wav`, `--bits 16|24|32`: verlangen eine Umwandlung (FLAC höchstens 24 Bit, kein Resampling). Bei neutralem Profil wird **nur dann** umgewandelt, wenn eine dieser Optionen angegeben ist. Umgewandelte oder gefilterte Dateien verlieren Tags und Cover (Titel/Interpret gehen per DLNA-Metadaten an den Renderer).
- `--no-dither` (kein TPDF-Dither bei 16 Bit), `--cache-dir` (gefilterte Dateien behalten), `--always-process` (auch ohne Profil neu schreiben).
- `--poll` (Abfrageintervall, Standard 0,5 s), `--bind-ip` (mehrere Netzwerkkarten/VPN), `--host-ip`, `--port`, `--bind` (Schnittstelle des Medienservers, Standard `0.0.0.0`, siehe „Grenzen“).
- `--log DATEI`: Protokoll im JSON-Lines-Format. Standardmäßig wird `kefplayer_log.jsonl` verwendet; mit `--log DATEI` lässt sich ein eigener Protokollpfad bzw. Dateiname festlegen (z. B. `--log T01_neutral_16_44.jsonl`).
- Status-API (ab 0.2.0, Details in `docs/status-api.md`): `--no-api` (ganz aus), `--api-port` (Standard 8766), `--api-live` (Position/Transportstatus per gedrosselter Renderer-Abfrage, Standard aus), `--api-live-interval` (Mindestabstand der Live-Abfragen in Sekunden, mindestens 0,5, Standard 1,0), `--api-details` (Pfade/Gerätedaten zeigen, Standard verborgen), `--diag-file DATEI` (Diagnoseexport am Ende). Gesundheitsüberwachung (Entwicklungsstand nach 0.3.0, Standard an): `--no-health`, `--health-interval` (Standard 30 s, mindestens 15 s); sie fragt im Leerlauf den Renderer lesend ab, nie während ein Titel spielt (Verhalten an der echten KEF ungeprüft). D1-Shadow-Entscheidungen werden im Diagnose-Endpunkt mit ausgegeben; sie führen keine Reparaturen aus (Details und PowerShell-Abfrage: [docs/repair-decisions.md](docs/repair-decisions.md)).
- Falls die KEF Titel ablehnt, sind `--mime audio/x-flac` und `--plain-protocolinfo` Ausweichoptionen. Ob sie nötig sind, ist unverifiziert.

## Oberfläche (GUI, 0.3.0)

Start: `python -m kefplayer.gui` (im Projektordner bzw. mit installiertem Paket). Voraussetzung ist `tkinter`: Bei python.org-Installationen für Windows enthalten, unter Linux ist es meist ein eigenes Paket (z. B. `python3-tk`). Ohne `tkinter` meldet die Oberfläche das und beendet sich mit Exitcode 2; die Kommandozeile braucht `tkinter` nicht und importiert die Oberfläche nicht.

Die Oberfläche nutzt den unveränderten Player-Kern und bietet:
- Renderer suchen und verbinden, auch per Adresse.
- Warteschlange: Dateien oder Ordner hinzufügen, entfernen, nach oben/unten verschieben, leeren; Statusspalte je Titel.
- Profil laden (JSON oder Equalizer-APO-Text) oder neutral; Warnungen werden angezeigt, ein fehlerhaftes Profil wird im Protokoll gemeldet und das bisherige bleibt erhalten.
- Abspielen, Pause/Weiter, nächster Titel, Stopp; Protokollanzeige. Das Wiedergabeprotokoll ist dasselbe wie bei `play` (`kefplayer_log.jsonl` im aktuellen Ordner).

Optionen: `--no-dpi` (kein DPI-Aufruf unter Windows, zum Vergleich der Darstellung), `--selftest` (Fenster kurz öffnen, Daten ausgeben, beenden).

Einschränkungen:
- Nicht enthalten: Profil-Editor, Verarbeiten von Dateien ohne Wiedergabe (dafür `kefplayer process`), Frequenzgang-Anzeige, Anzeige der Status-API. Es gibt keine Windows-EXE der Oberfläche.
- Pause/Weiter ist nur eine Anforderung; der Zustand wird nicht bestätigt. Angezeigt wird die Wandzeit seit Titelstart, keine Wiedergabeposition.
- Preamp „auto“ wird ohne berechneten Wert angezeigt. Die Fehlermeldung bei einer ungültigen Profildatei nennt die erlaubten Formate noch nicht. Die Texte sind ohne Umlaute.
- **Ungeprüft:** Wiedergabe über die Oberfläche an der echten KEF (nur Simulator), das Aussehen mit `--no-dpi` unter Windows und das Verhalten der Statusspalte bei echten Fehlern an der KEF.

## Filterprofil (JSON)

Syntaxbeispiel, keine Empfehlung:

    {
      "name": "beispiel",
      "preamp_db": "auto",
      "filters": [ {"type": "peq", "fc": 3000, "gain_db": -2.0, "q": 3.0} ],
      "channels": { "R": {"gain_db": -0.5, "delay_ms": 0.0, "filters": []} }
    }

- Typen: `peq`, `lowshelf`, `highshelf`, `lowpass`, `highpass` (Biquads nach RBJ-Cookbook, Q-Form; Tief-/Hochpass 2. Ordnung).
- `preamp_db`: `"auto"` setzt den Preamp so, dass der höchste Betragsgang aller Kanäle ≤ 0 dB bleibt (gemeinsam für beide Kanäle, die Balance bleibt). Ein manueller Wert wird verwendet, bei zu wenig Headroom gibt es eine Warnung. Zusätzlich skaliert ein Clipping-Schutz das Stück, falls die Spitze trotzdem über Vollaussteuerung läge, und meldet das.
- Import von Equalizer-APO-/REW-Textdateien (`Preamp`, `Filter n: ON PK|LS|HS|LSC|HSC|LP|HP ...`, `Channel: L|R`, `Delay: ms` mit 0 bis 50 ms). Nicht Unterstütztes (z. B. `Include`, `Convolution`, `BW Oct`, Delay in Samples) und Delay außerhalb von 0 bis 50 ms führen zu einem Fehler statt stillem Ignorieren.
- **Shelf-Filter aus APO-Dateien:** Equalizer APO kennt mehrere Shelf-Varianten (LS/HS, LSC/HSC, "LS 6dB/12dB") mit unterschiedlicher Definition von Fc (Eck- oder Mittenfrequenz) und Steilheit. Gelesen wird nur die Q-Form (`Fc`, `Gain`, `Q`); Formen mit dB/Oktave-Angabe werden abgelehnt. Auch bei der Q-Form ist **nicht verifiziert**, dass Fc und Q exakt wie in APO/REW zu verstehen sind. Dazu erscheint bei jedem importierten Shelf ein Hinweis. Vor der Nutzung den Frequenzgang mit `kefplayer response` gegen REW vergleichen. Ebenso unverifiziert: der Standard-Q (0,7071) von `LP`/`HP` ohne Q-Angabe.
- Hinweise beim Laden eines Profils (nur Warnungen, keine Sperre): Filter im Bassbereich unter 150 Hz (mögliche Doppelkorrektur, wenn dieselbe Korrektur schon an anderer Stelle im Signalweg wirkt, z. B. in einem Subwoofer-DSP), Filter über 150 Hz (sinnvoll nur auf Basis über mehrere Positionen gemittelter Messungen), Boosts (vorher prüfen, ob an der Stelle ein Einbruch durch Auslöschung vorliegt; solche Einbrüche lassen sich nicht wegentzerren), positiver Preamp (Clipping-Gefahr), Kanalunterschiede wirken nur auf diesen Signalweg.
- Quantisierung: bei neutralem Profil wird die Originaldatei unverändert ausgeliefert (Bytes identisch, im Test gegen den Simulator belegt; was der Renderer intern damit macht, ist nicht geprüft). Sonst Ausgabe in der Bittiefe der Quelle, bei 16 Bit mit TPDF-Dither (24 Bit ohne Dither). Keine Neuberechnung der Abtastrate.

## Protokoll (`kefplayer_log.jsonl`)

Ein JSON-Objekt pro Zeile. Wichtige Felder im Ereignis `track_end`:

| Aspekt | Feld |
|---|---|
| Format | `served_format` (Format, Untertyp, Bit, Samplerate, Kanäle der gesendeten Datei) und `mime` |
| Abbrüche | `end_reason`: `ended`, `error`, `never_started`, `timeout`, `skipped`, `stopped`, `prepare_error`. Dazu `ended_early` (Ende deutlich vor der Dauer) und `last_position_s` |
| Startverzögerung | `startup_delay_s` (Play-Befehl bis Status PLAYING) |
| Lückenübergänge | `gap_before_s` (Ende des Vorgängers erkannt bis PLAYING des nächsten) |
| Pegel | Ereignis `track_processed`: `preamp_db`, `peak_in_dbfs`, `peak_out_dbfs`, `guard_gain_db` |
| Verhalten des Renderers am Medienserver | `requests`: HEAD/GET, `Range`, Status, gesendete Bytes |

Das Ereignis `sink` enthält die vom Gerät gemeldeten Formate. `cleanup_failed` meldet eine temporäre Datei, die nicht gelöscht werden konnte (die Wiedergabe läuft weiter).

**Zeitauflösung:** Beim Warten auf den Start und in den letzten 3 s vor dem Titelende wird alle 0,05 s abgefragt, sonst alle `--poll` s. Die Werte sind daher auf etwa 0,05 s genau (bei Startverzögerung und Lücke), sonst gröber. Ein Hörvergleich bleibt für die Beurteilung von Lücken maßgeblich.

## Tests und Prüfstand

**Aktueller Entwicklungsstand im PR #2 (Branch `phase-bc-diagnose-health`, Commit `0fa0568`, 17.11.2026):** GitHub Actions hat die vollständige Testsuite mit 769 gesammelten Tests auf beiden Plattformen erfolgreich ausgeführt:

- [Windows, Python 3.12 — Run 44062638651](https://github.com/Klaus-ctrl-prog/KEFplayer/actions/runs/44062638651): 757 bestanden, 12 übersprungen, 0 fehlgeschlagen. Die 12 übersprungenen Tests benötigen unter Windows/MSVC zusätzliche `np.longdouble`-Präzision, die dort nicht verfügbar ist.
- [Ubuntu — Run 44062638651](https://github.com/Klaus-ctrl-prog/KEFplayer/actions/runs/44062638651): 758 bestanden, 11 übersprungen, 0 fehlgeschlagen. Die übersprungenen GUI-Tests benötigen ein Display.
- Beide CI-Jobs verwenden den Renderer-Simulator. Das ist kein Nachweis für das Verhalten einer realen KEF. Der PR bleibt offen und ungemergt.

Historischer Stand 0.3.0: Die Testsuite umfasste 679 Tests: 51 zur Oberfläche (`tests/test_gui_*.py`: 25 Controller, 15 Anzeigelogik, 11 Fenster) und 628 bis 0.2.1 (119 zur Wiedergabe, 145 zur Status-API, 53 zu den L/R-Funktionen und zur APO-Delay-Grenze, 311 zu Rückwärtskompatibilität gegen 0.2.0, numerischer Stabilität, Randfällen, Simulator und Fehlerbehandlung von `process`; Stand der Veröffentlichung 0.2.0: 264). Sie läuft ausschließlich gegen den simulierten Renderer (`tests/mock_renderer.py`) und braucht weder eine KEF noch ein Heimnetz. **Alle Nachweise stammen aus Tests mit dem Simulator, nicht aus Tests mit echter KEF-Hardware.**

**Windows** (vom Entwickler ausgeführt, getrennt von den Linux-Läufen):

| Umgebung | Nachweis |
|---|---|
| Windows 11, CPython 3.12.10, pytest 9.2.0 | 264 Tests, drei erfolgreiche vollständige Testläufe |
| Windows 11, CPython 3.12.10 | `tests/test_upnp.py::test_server_request_log` einzeln wiederholt: 10 von 10 Läufen erfolgreich |
| Windows, CPython 3.12.10 (Prüfziel; Windows-Version und Bibliotheksversionen für diesen Lauf nicht festgehalten) | 628 Tests (Stand nach 0.2.0): 616 bestanden, 12 übersprungen (Referenz in erweiterter Genauigkeit unter Windows nicht verfügbar, siehe CHANGELOG), 0 fehlgeschlagen; ein Lauf |
| Windows 11 Home (Build 26300), CPython 3.12.10 (64 Bit), numpy 2.5.4, scipy 1.18.1, soundfile 0.14.0 (libsndfile 1.2.2), pytest 9.2.0 | 628 Tests mit dem Release-ZIP v0.2.1 (SHA-256 `43fb5fe0f269cc8993d05f00f2b58bc61d2762cf05445379ef81835969af784c`): 616 bestanden, 12 übersprungen (`np.longdouble` nur float64), 0 fehlgeschlagen, 84,9 s; ein Lauf am 15.11.2026. Zusätzlich `kefplayer process` mit einer beschädigten Datei: Meldung `Fehler: …`, Exitcode 2, kein Traceback |

Die Windows-Läufe mit 264 Tests erfolgten vor der reinen Textänderung des Hinweises zu Filtern im Bassbereich in `kefplayer/dsp.py` (siehe CHANGELOG, Abschnitt „Geändert“) und wurden nicht wiederholt. Die Filterlogik ist unverändert. Der erste Lauf mit 628 Tests (vorletzte Zeile der Tabelle) umfasst die geänderte Fassung und alle Tests nach 0.2.0 (siehe CHANGELOG, Abschnitt „0.2.1“). Er erfolgte vor dem Entfernen vereinzelter CR-Zeichen und einer zusätzlichen Leerzeile am Dateiende in `kefplayer/cli.py`, `tests/test_cli.py` und `tests/test_audio_edge_cases.py`, vor der Berichtigung eines Satzes in der Modulbeschreibung von `tests/test_audio_edge_cases.py` und vor der Anhebung der Version auf 0.2.1 (`kefplayer/__init__.py`, `pyproject.toml`, eine Zeile in `tests/test_api.py`). Mit dem Release-ZIP v0.2.1 wurde unter Windows danach erneut getestet (letzte Zeile der Tabelle).

**Linux** (jeweils frische virtuelle Umgebung, `pip install ".[test]"`, `pip check` ohne Befund, `kefplayer --version` und `python -m kefplayer --version` ergeben `kefplayer 0.2.0`; Lauf vom 15.11.2026 mit dem Stand von 0.2.0, pytest 9.2.0):

| CPython | numpy | scipy | soundfile | Ergebnis |
|---|---|---|---|---|
| 3.10.20 | 2.2.6 | 1.15.3 | 0.14.0 | 264 bestanden |
| 3.11.18 | 2.4.6 | 1.17.1 | 0.14.0 | 264 bestanden |
| 3.12.3 | 2.5.4 | 1.18.1 | 0.14.0 | 264 bestanden |
| 3.13.18 | 2.5.4 | 1.18.1 | 0.14.0 | 264 bestanden |

**Linux, Zwischenstand nach 0.2.0 (vor 0.2.1):** volle Testsuite mit 317 Tests am 15.11.2026, in den bereits vorhandenen Testumgebungen (nicht neu aufgesetzt, ohne erneutes `pip install` und `pip check`) mit denselben Bibliotheksversionen wie in der Tabelle: CPython 3.10.20, 3.11.18, 3.12.3 und 3.13.18, jeweils 317 bestanden.

**Linux, Stand 0.2.1:** volle Testsuite mit 628 Tests am 15.11.2026 mit denselben Bibliotheksversionen wie in der Tabelle: CPython 3.10.20, 3.11.18, 3.12.3 und 3.13.18, jeweils 628 bestanden, keine übersprungen. Diese Läufe erfolgten wie der Windows-Lauf vor der Bereinigung der drei genannten Dateien und vor der Anhebung der Version auf 0.2.1. Mit dem veröffentlichten Stand wurde nur CPython 3.12.3 ausgeführt: 628 bestanden.

**Stand 0.3.0 (Oberfläche):** Linux, CPython 3.12.3 mit Tk unter xvfb: volle Testsuite mit 679 Tests, 679 bestanden, davon 51 neue Tests der Oberfläche; ohne Display werden 11 Tk-Tests übersprungen. Windows 11, dokumentierter Testlauf vom 16.11.2026 (CPython 3.12.10, Tk 8.6.15, neue virtuelle Umgebung): die 51 Tests der Oberfläche bestanden. Diese Angaben stammen aus dem vorliegenden Testbericht des Entwicklers und wurden im aktuellen Arbeitslauf nicht erneut ausgeführt. Ausgeführt wurden nur die Tests der Oberfläche; die volle Testsuite wurde für 0.3.0 unter Windows **nicht** ausgeführt. Für 0.3.0 wurde auf Linux nur CPython 3.12.3 ausgeführt, die weiteren Versionen der älteren Linux-Zeilen oben sind für 0.3.0 nicht neu ausgeführt.

Nicht getestet: Windows mit anderen Python-Versionen als 3.12.10, macOS sowie jede echte KEF-Hardware.

Die Tests decken unter anderem ab: Filter-Eigenschaften, unabhängige L/R-Funktionen (EQ, Pegel, Delay, Kanaltrennung, Dateilänge), neutralen Durchlauf bitgenau (16/24 Bit; 44,1/48/96 kHz), Medienserver (Range, HEAD, 416, nur registrierte Token), UPnP (Beschreibung, SOAP-Fehlercodes, DIDL, SSDP per Unicast) und Wiedergabe (Reihenfolge, Pause, Überspringen, Stopp, Ablehnung, nie startender Renderer, zu frühes Ende, verschwindender Renderer, fehlgeschlagenes Löschen).

Status-API (0.2.0): 145 Tests (Sammler, Datenschutz, HTTP-Härtung, Wiedergabe unter API-Last unverändert, Live-Abfrage nur `Get*` und gedrosselt, Kommandozeile, Beenden). Vierzehn absichtliche Sabotageversuche an den Schutzmaßnahmen wurden bei der Entwicklung jeweils von einem Test erkannt (die dafür verwendeten Skripte liegen nicht im Repository). Siehe `docs/status-api.md`.

### Weitere Eigenprüfungen der Entwicklung

Einmalig am 14.11.2026 unter Linux mit Skripten durchgeführt, die **nicht im Repository liegen**. Die Ergebnisse sind daher nicht unabhängig nachvollziehbar. „Bitgenauigkeit“ bezieht sich auf die vom Medienserver ausgelieferten Bytes (gegen den Simulator), nicht auf das, was ein Renderer daraus macht.

| Frage | Prüfung | Ergebnis |
|---|---|---|
| Filterformeln | 4000 Zufallsfilter (5 Typen, 44,1–192 kHz) gegen die analogen RBJ-Prototypen mit Bilinear-Transformation und Vorverzerrung | größte Abweichung 5·10⁻⁸ dB, wo der Betrag über −80 dB liegt |
| Rechengenauigkeit | float64-Kette (wie im Player) gegen longdouble-Referenz, 2 s Rauschen | höchstens −215 dBFS (192 kHz, tiefe schmale Filter); zum Vergleich 24-Bit-LSB = −138,5 dBFS |
| Headroom | 300 Zufallsprofile, 400 000 Frequenzpunkte | Spitze nach Preamp nie über 0 dB, höchstens 0,01 dB zu konservativ |
| Einschwingen | Vollaussteuerungs-Sinus auf der Boost-Frequenz, hart eingeschaltet | keine Spitze über dem Eingangspegel (Q 2, 5, 10) |
| Bitgenauigkeit | 6 MiB Zufallsbytes ganz und in 7 Range-Stücken; 14,5 MB 24/96-FLAC über Player und Simulator | Bytes identisch (sha256) |

Lücke im Simulator (Renderer-Sicht, Standard-`--poll` 0,5 s): von 0,53 s (vor der Umstellung auf schnelles Abfragen) auf 0,08 s; darin stecken 0,05 s künstliche Startverzögerung des Simulators. Das sagt nichts über die KEF.

## Was **nicht** geprüft ist

- Die echte KEF (Validierung ausstehend, frühestens ab März 2027): ob sie die Titel annimmt, welche Formate/Samplerates/Bittiefen sie wirklich spielt, wie sie auf DIDL-Metadaten und `protocolInfo` reagiert, Startzeiten, Lücken, Abbrüche, Pegelverhalten.
- Ob Shelf-Filter und `LP`/`HP`-Standard-Q wie in Equalizer APO/REW wirken (siehe oben). Inter-Sample-Spitzen (True Peak) werden nicht berücksichtigt. Das Ausklingen von Filtern (Schwanz) nach dem Dateiende wird nicht erzeugt.
- SSDP-Multicast im Heimnetz (getestet nur per Unicast). Echte Zeitwerte (Simulator läuft beschleunigt). Windows mit anderen Python-Versionen als 3.12.10 sowie macOS.
- Schnelles Abfragen (alle 0,05 s beim Warten auf den Start und in den letzten 3 s vor dem Titelende, je zwei Anfragen): ob die UPnP-Umsetzung der KEF das verträgt, ist unbekannt. Das Intervall lässt sich bisher nur im Code ändern (`fast_poll_s`), nicht auf der Kommandozeile.
- Die schnelle Endeerkennung setzt voraus, dass die KEF die Wiedergabeposition (`RelTime`) meldet. Fehlt sie, fällt der Player auf `--poll` zurück, und die Lücke ist dann wieder etwa ein Abfrageintervall groß. Nicht getestet.
- Import echter REW-/APO-Exportdateien: nur mit selbst geschriebenen Beispielzeilen getestet.
- Aufräumen temporärer Dateien unter realen Sperrbedingungen (Virenscanner, offene Verbindungen): nur mit simuliertem Löschfehler getestet. Die Aufräum-Option des Temp-Ordners (`ignore_cleanup_errors`) ist nicht durch einen eigenen Test abgedeckt.
- Status-API: die PowerShell-Beispiele in `docs/status-api.md` sind nicht geprüft; `--api-live` nicht gegen die echte KEF (Verhalten bei zusätzlichen Abfragen unbekannt).
- Oberfläche: Wiedergabe über die Oberfläche an der echten KEF, das Aussehen mit `--no-dpi` unter Windows, die Statusspalte bei echten Fehlern, andere Windows- und Python-Versionen.
- Hörbarkeit/Klang. Es wurde nichts gemessen oder gehört.

## Hinweise für Windows 11

Die Testsuite lief unter Windows 11 (CPython 3.12.10) gegen den Simulator. Die folgenden Punkte betreffen den Betrieb mit echtem Renderer; sie sind erwartbare Stolpersteine und nicht praktisch geprüft.

- Start: Liegt `kefplayer` nicht im PATH, `python -m kefplayer` (oder `py -m kefplayer`) verwenden.
- Firewall: Beim ersten `play` fragt Windows nach Zugriff für Python. Eingehend TCP 8765 im **privaten** Netz erlauben. Symptom bei Blockade: `never_started`, im Protokoll bei `requests` keine Einträge.
- Mehrere Netzwerkadapter (WLAN, Ethernet, Hyper-V, WSL, VPN): findet `discover` nichts, `--bind-ip <PC-IP>` setzen oder die Beschreibungs-URL direkt mit `--renderer` angeben. Die eigene Adresse für die KEF wird anhand der Route zur KEF bestimmt.
- Temporäre Dateien können durch Virenscanner oder offene Verbindungen gesperrt sein: wird abgefangen (`cleanup_failed`, Wiedergabe läuft weiter).
- Pfade über 260 Zeichen (tiefe Album-Ordner) können scheitern, sofern lange Pfade in Windows nicht aktiviert sind. Nicht abgefangen.
- Ruhezustand: Schläft der PC während der Wiedergabe ein, bricht sie ab. Nicht abgefangen, Energiesparplan anpassen.
- Konsole: Sonderzeichen in Dateinamen werden bei der Anzeige ersetzt, Dateien selbst bleiben unverändert.

## Informationen zur KEF (Quellen, nicht selbst geprüft)

- Offizielle KEF-FAQ ([us.kef.com/pages/ls50-wireless-faq](https://us.kef.com/pages/ls50-wireless-faq)): "LS50 Wireless does not support gapless playback." (B16); "Disabling Gapless Control on media players (e.g. BubbleUPnP) may solve some playback stability issues." (B5); die KEF ist ein DLNA Digital Media Renderer (B13); Hi-Res-Dateien über 5-GHz-WLAN empfohlen. Die Seite nennt die Hardware-Generation nicht. Die unterstützten Netzwerkformate stehen dort nur als Bild und waren nicht lesbar.
- Community-Berichte, widersprüchlich: Roon-Forum 2019 ([Thread](https://community.roonlabs.com/t/kef-ls50w-cant-play-hi-res-above-96khz/70103)): ein Nutzer kann über 96 kHz nicht abspielen, andere berichten 24/192 ohne Probleme. Audirvana-Forum ([Thread](https://community.audirvana.com/t/audirvana-stops-end-of-first-track-upnp-kef-lsx/16808)): bei KEF LSX/LS50 Wireless stoppt UPnP-Wiedergabe am Titelende oder nach dem ersten Titel, Abhilfe nur durch Aus- und Einstecken der KEF, keine bestätigte Ursache. JRiver-Forum ([Thread](https://yabb.jriver.com/interact/index.php/topic,118497.0.html)): ein Geräteprotokoll nennt `SetNextAVTransportURI` als unterstützt, gleichzeitig werden 1–2 s Pause bei Live-Aufnahmen berichtet (aus zweiter Hand, Generation unklar).
- Folgerung für diesen Player: Lückenlosigkeit wird nicht angestrebt, `SetNextAVTransportURI` ist bewusst nicht umgesetzt. Ziel ist die kleinstmögliche, gemessene Lücke. Ob die KEF 24 Bit/192 kHz über UPnP annimmt, ist unbelegt, bis die Tests ab T02 (unten) mit echter Hardware gelaufen sind.

## Geplante Hardware-Validierung (ausstehend, frühestens ab März 2027)

Diese Prüfliste ist für Tests mit einer echten KEF vorgesehen und wurde **noch nicht ausgeführt**. Pro Schritt nur eine Änderung.

Vorab: Lautstärke der KEF fest einstellen und notieren, keine weiteren Einstellungen am Renderer ändern. Software-Equalizer, die systemweit auf dem PC wirken (z. B. Equalizer APO), greifen auf diesem Weg nicht, weil die Datei per DLNA ausgeliefert wird; eine Doppelfilterung entsteht dadurch nicht. Immer mit `--log` und einem eindeutigen Dateinamen pro Test arbeiten, z. B. `--log T01_neutral_16_44.jsonl`.

1. `kefplayer discover`, dann `kefplayer probe`. Notieren: IP, Beschreibungs-URL, gemeldete Sink-Formate. Findet `discover` nichts: `--bind-ip <PC-IP>`, Firewall prüfen, sonst die URL direkt mit `--renderer` angeben.
2. **T01** neutral, 16 Bit/44,1 kHz: ein Album-Ordner (mindestens 3 Titel, davon zwei, die nahtlos ineinander übergehen) ohne Profil und ohne `--format`/`--bits`. Auswerten: Wird gespielt? `served_format`, `startup_delay_s`, `gap_before_s`, `end_reason`. Hören: Lücke an den Übergängen?
3. **T02, T03, …** nur das Format ändern: 16/48, dann 24/48, 24/96, 24/192. Samplerate kommt aus der Quelldatei (kein Resampling), die Bittiefe lässt sich mit `--bits` ändern. Möglichst dasselbe Stück in verschiedenen Formaten verwenden (sonst im Log vermerken, dass Stück und Format wechseln). Notieren, ab wann `error`, `never_started` oder `ended_early` auftreten.
4. **T10** Wirksamkeit der Filterkette: `--profile examples/test_tiefpass_500.json` mit einem Titel aus T01. Erwartung: deutlich dumpferer Klang (Tiefpass 500 Hz). Hört man ihn nicht, wird die gefilterte Datei nicht gespielt. Zurück zu neutral, gegenhören.
5. **T11** Pegel/Headroom: eigenes Profil mit einem Boost laden (vorher `kefplayer response` ansehen), laut aufgenommenen Titel spielen. Prüfen: `preamp_db` im Log, `peak_out_dbfs` ≤ 0, kein Clipping hörbar. Boosts nur, wenn kein Einbruch durch Auslöschung vorliegt.
6. Falls Titel abgelehnt werden: einzeln `--mime audio/x-flac`, danach `--plain-protocolinfo` testen und das Ergebnis protokollieren.
7. Logdateien und Beobachtungen (Hörnotizen, Uhrzeit, Lautstärke) sichern und auswerten. Danach bewerten: funktioniert ein Weg zuverlässig und mit wirksamer DSP-Kette?

## Offen für stabile Wiedergabe mit kleinen Lücken

Erst nach T01 sinnvoll zu entscheiden, weil das Fehlerbild der KEF unbekannt ist:
- Kein Ereignis-Abo (UPnP-Events, `LastChange`): Titelende und Start werden per Abfrage erkannt.
- Keine Wiederholung bei `never_started`: der Titel wird übersprungen, die Warteschlange läuft weiter.
- Ein externer Stopp (Fernbedienung, Eingangswechsel an der KEF, Abbruch am Titelende) erscheint als Ende. Er wird mit `ended_early` markiert, die Warteschlange läuft aber mit dem nächsten Titel weiter. Kein Fortsetzen per Seek.
- Kein zeitbasierter Rückfall für die Endeerkennung (nur über die gemeldete Position) und keine Kommandozeilenoption für das schnelle Abfrageintervall.
- Kein Schutz vor Ruhezustand des PCs.
- Kein Dauerstream-Modus (die ganze Warteschlange als ein durchgehender Strom, damit die KEF nie einen Titelwechsel sieht). Das wäre die einzige Möglichkeit zu echter Lückenlosigkeit, kostet aber Titelgrenzen, Überspringen und Positionsanzeige, und ob die KEF unbegrenzte Streams annimmt, ist unbekannt.

## Status-API (0.2.0)

- Lokal (nur `127.0.0.1`), nur lesend (GET/HEAD, keine Steuerbefehle), eigenständiges Modul `kefplayer/api.py`, vollständig abschaltbar mit `--no-api` (dann wird das Modul nicht einmal importiert). Endpunkte: `/api/v1/status`, `renderer`, `dsp`, `audio`, `logs`, `diagnostics` (Entwicklungsstand nach 0.3.0 zusätzlich `health`). Ausführlich: `docs/status-api.md`.
- Festlegungen: (1) Port **8766** statt 8765, weil 8765 der Standardport des Medienservers ist. (2) Die API ist bei `play` **standardmäßig an** (die API selbst sendet keine Renderer-Abfragen; die getrennte Gesundheitsüberwachung des Entwicklungsstands nach 0.3.0 fragt im Leerlauf lesend ab, siehe `docs/status-api.md`); `--no-api` schaltet die API ab. (3) Position und Transportstatus gibt es nur mit `--api-live` (Standard aus, weil zusätzlicher Verkehr zur KEF). (4) Titel = Dateiname, nicht Tag-Titel.
- Ungetestet: die PowerShell-Beispiele und die echte KEF.

## Grenzen

- Ganze Datei wird als float64 im Speicher gefiltert (für normale Alben unkritisch, bei sehr langen Hi-Res-Dateien nicht).
- Nur Titel/Interpret als Metadaten. Kein Resampling. FLAC nur 16/24 Bit.
- Die Status-API bindet ausschließlich an 127.0.0.1 und hat keine Anmeldung: jeder Prozess und Benutzer auf demselben PC kann sie lesen (Standard-Datenschutz verbirgt Pfade und Gerätedaten; auf geteilten PCs `--no-api`).
- **Der Medienserver bindet standardmäßig an alle Schnittstellen (`0.0.0.0`) und kann dadurch im Netzwerk erreichbar sein.** Ausgeliefert werden nur Dateien, die für den laufenden Titel registriert sind (zufälliger Token in der URL), und nur solange der Titel läuft. Mit `--bind` lässt sich eine andere Schnittstelle für den Medienserver festlegen.
- Der MIME-Typ wird aus dem Dateiinhalt bestimmt (nicht aus der Endung); bei unbekanntem Format aus der Endung.
- Kanalunterschiede (Pegel, Delay, Filter je Seite) wirken nur auf Wiedergabe über diesen Player, nicht auf TV, Bluetooth oder andere KEF-Quellen.

## Änderungsstand

- **0.3.0** (gegenüber 0.2.1): Grafische Oberfläche `kefplayer/gui/` (tkinter), Start mit `python -m kefplayer.gui`; 51 neue Tests; `pyproject.toml` nimmt `kefplayer.gui` in die Pakete auf. Player-Kern, Kommandozeile, Status-API und Profilformat unverändert. Einzelheiten: siehe CHANGELOG.
- **0.2.1** (gegenüber 0.2.0): `dsp.py`: Der APO-Import lehnt `Delay:` außerhalb von 0 bis 50 ms mit einer Fehlermeldung ab (vorher wurde die Grenze dort nicht geprüft). `cli.py`: `process` meldet eine nicht lesbare oder beschädigte Audiodatei mit „Fehler: …“ und Exitcode 2 statt mit einem Traceback (Exitcode 1). 364 neue Tests (Testsuite jetzt 628): 53 Tests (`tests/test_lr_channels.py`, neu, 49 Tests zu den unabhängigen L/R-Funktionen, und vier Tests in `tests/test_dsp.py`) sowie 311 weitere Tests (`tests/test_compat_020.py`, `tests/test_numeric_stability.py`, `tests/test_audio_edge_cases.py` und `tests/test_lr_simulator.py`, alle neu, dazu ein Test in `tests/test_cli.py`). `__init__.py`, `pyproject.toml` (Version 0.2.1) und eine Zeile in `tests/test_api.py` (erwartete Versionsnummer). Sonst unverändert gegenüber 0.2.0. Siehe CHANGELOG.
- **0.2.0** (gegenüber 0.1.2): Lokale, lesende Status-API (`kefplayer/api.py`, `docs/status-api.md`), Optionen `--no-api`, `--api-port`, `--api-live`, `--api-live-interval`, `--api-details`, `--diag-file` bei `play`; 145 neue Tests. Geändert gegenüber 0.1.2: `cli.py` (Optionen, Start/Stopp der API), `dsp.py` (nur der Text des Hinweises zu Filtern im Bassbereich, keine Logik), `__init__.py` (Version), `player.py` (verwendet für Laufzeitmessungen `time.perf_counter()` statt `time.monotonic()`. In der getesteten Windows-Umgebung mit CPython 3.12.10 lieferte `time.monotonic()` eine effektive Auflösung von etwa 15,6 ms), `pyproject.toml` (Version, Metadaten, Build-Anforderung `setuptools>=77`), die Testhelfer `tests/mock_renderer.py` (gleiche Zeitmessung), `tests/test_player.py` (eine Zeile) und `tests/test_upnp.py` (Synchronisation im Test `test_server_request_log`, der Medienserver schreibt den Protokolleintrag erst nach dem Senden der Antwort). Unverändert gegenüber 0.1.2: `audio.py`, `upnp.py`, `mediaserver.py`, `runlog.py`, `__main__.py` sowie `tests/test_audio.py`, `test_cli.py`, `test_dsp.py`, `test_review_fixes.py`. Schnittstellen: `cli.cmd_play` hat neue Argumente (nur über die Kommandozeile erreichbar), `Player(on_event=...)` unverändert.
- **0.1.2** (gegenüber 0.1.1): Ausgabe sagt "verarbeitet" statt "gefiltert", wenn nur umgewandelt wird. Drei Tests ergänzt (Kommandozeilenpfad von `--bits`, Kopfzeile, weitere Formate). Dokumentation: bekannte Schwächen, geplante Status-API.
- **0.1.1** (gegenüber 0.1.0): `--format`/`--bits` wirken auch bei neutralem Profil. Fehlgeschlagenes Löschen temporärer Dateien bricht die Warteschlange nicht mehr ab. Schnelles Abfragen beim Start und kurz vor dem Titelende (Lücke im Simulator 0,53 s → 0,08 s). MIME-Typ und URL-Endung nach Dateiinhalt. APO-Import: Shelf-Formen mit dB/Oktave abgelehnt, Hinweis bei jedem Shelf. Start per `python -m kefplayer`. Ereignis `cleanup_failed` im Protokoll neu.
- Schnittstellen: `Player(fmt=...)` ist jetzt standardmäßig `None` (vorher `"FLAC"`), neue Parameter `fast_poll_s`, `end_window_s`. `upnp.pick_mime` hat den optionalen Parameter `fmt`, `upnp.ext_for_format` ist neu. Kommandozeile: `play --format` hat keinen Standardwert mehr (für `process` weiterhin flac).

## Entwicklung

### Vollständige Testsuite unter Windows (PowerShell)

Die folgenden Befehle im Projektordner ausführen, in dem `pyproject.toml` liegt. Sie verwenden Python 3.12 und eine lokale virtuelle Umgebung. Die Aktivierung der Umgebung ist nicht erforderlich; dadurch muss auch kein PowerShell-Ausführungsrichtlinien-Skript gestartet werden.

```powershell
py -3.12 -m venv .venv
$Python = ".\.venv\Scripts\python.exe"

& $Python -m pip install --upgrade pip
& $Python -m pip install ".[test]"
& $Python -m pip check
& $Python -m kefplayer --version
& $Python -m pytest -q -p no:cacheprovider -rs
```

Der letzte Befehl führt die vollständige Testsuite aus. Die Tests verwenden den Renderer-Simulator; eine KEF und ein Heimnetz sind dafür nicht erforderlich. `-rs` zeigt die Gründe für übersprungene Tests an. Der Aufruf entspricht dem Testschritt der GitHub-Actions-CI; dort wird zusätzlich auf Ubuntu getestet.

Bei Bedarf kann die virtuelle Umgebung später mit `Remove-Item -Recurse -Force .venv` entfernt werden. Die Befehle verändern keine globale Python-Installation.

Aufbau: `kefplayer/dsp.py` (Filter, Profile), `audio.py` (Dateien), `upnp.py` (SSDP, SOAP, DIDL), `mediaserver.py` (HTTP mit Range), `player.py` (Warteschlange, Messung), `cli.py`, `runlog.py`, `api.py` (Status-API, nur lesend), `gui/` (Oberfläche: `controller.py` ohne Toolkit-Import, `viewmodel.py` Anzeigelogik, `view_main.py` tkinter-Fenster, `texts.py`, `__main__.py`). Tests der Oberfläche: `tests/test_gui_*.py`. Tests der API: `tests/test_api*.py`, gemeinsame Hilfen in `tests/api_helpers.py`.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Copyright (c) 2026 Klaus.
