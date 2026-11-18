# Changelog

## 0.4.0 (noch nicht veröffentlicht)

Änderungen gegenüber 0.3.0 (Tag `v0.3.0`, Commit `f2d880c`). Stand: `main` am 17.11.2026. Es gibt noch **keinen** Tag und kein Release 0.4.0.

### Hinzugefügt

- **Fehlerklassifikation und Wiederholung** (B1/B2): Fehler beim Vorbereiten und bei Renderer-Aufrufen werden in Kategorien eingeordnet; vorübergehende Fehler werden begrenzt wiederholt.
- **Gesundheitsprüfung und -überwachung** (C1–C3): neues Modul `kefplayer/health.py` mit Prüfungen für Renderer (HTTP, UPnP), Medienserver und Cache, Kommando `kefplayer health` und ein Hintergrund-Monitor (Standard an, `--no-health` schaltet ihn ab, `--health-interval` Standard 30 s, mindestens 15 s). Der Monitor sendet nie Renderer-Abfragen, während ein Titel spielt. Neuer Endpunkt `/api/v1/health`.
- **Status-API** (C4): Fehlerkategorien und Befunde in den Endpunkten; Beschreibung in `docs/status-api.md`.
- **D0 Diagnose** (`kefplayer/diagnosis.py`): deterministische Gruppierung der Befunde mit vorsichtigen Ursachenhypothesen. Rein lesend.
- **D1 Schattenentscheidungen** (`kefplayer/repair_decision.py`, `docs/repair-decisions.md`): bewertet D0-Hypothesen gegen Originalbefunde und Health-Zustand; Status nur `deferred` oder `suppressed`, nie `eligible`; keine Seiteneffekte. Ausgegeben unter `repair_decisions` im Diagnose-Endpunkt.
- **D2 Opt-in-Reparatur** (`kefplayer/repair_exec.py`, `kefplayer/repair_budget.py`, `docs/repair-d2.md`): Standard **aus**. Nur mit `kefplayer play --enable-repair` (nicht zusammen mit `--no-health`) wird eine einzige Aktion ausgeführt: R5, Neustart eines hängenden Diagnose-Monitors. Nie während ein Titel spielt, nie `RENDERER_LOCK`, Versuchsbudget mit Pause und Flapping-Sperre, Prüfung nach dem Neustart, Ereignisse `repair_start`/`repair_result`/`repair_blocked` im Protokoll und Abschnitt `repair` in `/api/v1/health`.
- **Internetradio E1** (`kefplayer radio`, `kefplayer/radio.py`, `docs/radio.md`): Stream wird über den Medienserver unverändert durchgeleitet; nur AAC/Ogg/Opus/FLAC, **MP3 abgelehnt**; Favoritenliste; URLs in Meldungen maskiert; keine ICY-Metadaten; Live-DIDL (`audioBroadcast`, `DLNA.ORG_OP=00`); Wartezeit ohne Zeitgrenze. Kein Raum-EQ (E1b folgt), keine API/Health für Radio. Nur Simulator.
- **E1.1 Qualitätsprüfung** (`kefplayer/quality.py`, `kefplayer radio-check`): Regel E-6 (FLAC oder AAC ab 128 kbit/s, Ziel 192, MP3/Ogg/Opus/HLS abgelehnt) vor jeder Radiowiedergabe; Bitrate wird gemessen, optional `--ffprobe`; bei Nichterfüllung wird nichts an die KEF gesendet. Nur Simulator.
- Detailentwurf E1b (`docs/radio-e1b-design.md`, nur Dokumentation).
- Planung Phase E (`docs/radio-plan.md`, `docs/favorites.example.json`): Codec-Regel (FLAC oder AAC ab 128 kbit/s, Ziel 192, MP3 ausgeschlossen), E1b mit ffmpeg als externer Decoder-/Encoder-Stufe. Nur Dokumentation, kein Code.
- Neue Tests für alle genannten Teile (u. a. D1-Invarianten, Regressionstests, Simulator); die Testsuite umfasst nun mehr Tests (siehe CI).

### Behoben

- D0/D1: Gruppenkollision zwischen Befunden gleichen Codes und Evidenz-Aliasing; Härtung gegen fehlerhafte Kategorien und Eingaben.
- Health-Monitor: Zustand beim Beenden (`stopping`/`stopped`).

### Geändert

- Version 0.4.0 in `pyproject.toml` und `kefplayer/__init__.py`; `tests/test_api.py` erwartet 0.4.0.
- Der Diagnose-Endpunkt enthält zusätzliche Felder (`repair_decisions`); der Health-Endpunkt den Abschnitt `repair`.

### Bekannte Einschränkungen

- Es wurde **kein echter KEF-Lautsprecher** getestet; alle Nachweise stammen aus dem Simulator (Hardware-Validierung frühestens ab März 2027). Das Verhalten von Health-Monitor und R5 an der echten KEF Gen1 ist ungeprüft.
- Weitere Reparaturaktionen (R1 Medienserver neu starten, R3 Renderer neu auflösen) sind **nicht** enthalten; sie liegen ungemergt in PR #10.
- CI (GitHub Actions) lief für den D2-Merge auf Ubuntu und Windows grün; ein manueller Windows-Lauf mit dem Release-ZIP wurde für 0.4.0 nicht gemacht.

## 0.3.0

Änderungen gegenüber 0.2.1.

### Hinzugefügt

- **Grafische Oberfläche (tkinter/ttk)** im neuen Paket `kefplayer/gui/` (`controller.py`, `viewmodel.py`, `view_main.py`, `texts.py`, `__main__.py`). Start: `python -m kefplayer.gui`; Optionen `--no-dpi` und `--selftest`.
  - Funktionen: Renderer suchen und verbinden (auch per Adresse), Warteschlange (Dateien oder Ordner hinzufügen, entfernen, nach oben/unten verschieben, leeren), Profil laden (JSON oder Equalizer-APO-Text) oder neutral mit Anzeige der Warnungen, Abspielen, Pause/Weiter, nächster Titel, Stopp, Statusspalte je Titel, Protokollanzeige. Ein fehlerhaftes Profil wird im Protokoll gemeldet, das bisherige Profil bleibt.
  - Die Oberfläche verwendet den unveränderten Player-Kern und dasselbe Wiedergabeprotokoll (`kefplayer_log.jsonl`) wie `play`. Die Lautstärke des Renderers wird nicht verändert.
- 51 neue Tests: `tests/test_gui_controller.py` (25), `tests/test_gui_viewmodel.py` (15), `tests/test_gui_view.py` (11). Die Testsuite umfasst damit 679 Tests (0.2.1: 628). Ohne Display werden 11 Tk-Tests übersprungen.

### Geändert

- `pyproject.toml`: `packages` enthält jetzt `kefplayer.gui` (sonst fehlte die Oberfläche in Wheel und sdist); Version 0.3.0. `kefplayer/__init__.py`: Version 0.3.0. `tests/test_api.py`: die eine Zeile, die die Versionsnummer prüft, erwartet jetzt 0.3.0.
- Sonst keine Änderung an Player-Kern, Kommandozeile, Status-API, DSP-Funktionen oder Profilformat. Keine neue Pflichtabhängigkeit; `tkinter` wird nur für die Oberfläche benötigt, die Kommandozeile importiert die Oberfläche nicht.

### Bekannte Einschränkungen

- Wie bisher: Es wurde **kein echter KEF-Lautsprecher** getestet, auch nicht über die Oberfläche (Hardware-Validierung frühestens ab März 2027).
- Linux: 679 Tests bestanden mit CPython 3.12.3 (mit Tk unter xvfb); weitere Python-Versionen wurden für 0.3.0 nicht ausgeführt. Windows 11 (CPython 3.12.10): die 51 Tests der Oberfläche bestanden, die volle Testsuite wurde für 0.3.0 unter Windows nicht ausgeführt.
- Nicht geprüft: das Aussehen mit `--no-dpi` unter Windows und die Statusspalte bei echten Fehlern an der KEF.
- Pause/Weiter wird nur angefordert, der Zustand wird nicht bestätigt; angezeigt wird die Wandzeit seit Titelstart, keine Wiedergabeposition. Preamp „auto“ wird ohne berechneten Wert angezeigt. Die Fehlermeldung bei einer ungültigen Profildatei nennt die erlaubten Formate nicht. Die Texte der Oberfläche sind ohne Umlaute.
- Dateien werden beim Hinzufügen im Oberflächen-Thread gelesen (`Controller.add_paths`), und beim Schließen des Fensters wird `self.c.shutdown(10.0)` aufgerufen.
- Keine Windows-EXE der Oberfläche.

## 0.2.1 (veröffentlicht am 15.11.2026)

Änderungen gegenüber 0.2.0.

### Behoben

- `kefplayer/dsp.py`: Der Import von Equalizer-APO-Dateien lehnt `Delay:` außerhalb von 0 bis 50 ms jetzt mit einer Fehlermeldung ab (Zeilennummer, Grenze, Wert). Bisher umging der Import die Grenzprüfung von `ChannelSpec`: `Delay: 100 ms` wurde angenommen und angewandt, `Delay: -1 ms` wurde angenommen und führte erst beim Anwenden des Profils zu einem Fehler. Für JSON-Profile galt die Grenze schon vorher, ihre Verarbeitung ist unverändert.
- `kefplayer/cli.py`: `kefplayer process` meldet eine nicht lesbare oder beschädigte Audiodatei jetzt mit `Fehler: …` und Exitcode 2. Bisher fing `main()` die Ausnahmen von soundfile (`soundfile.SoundFileError`, darunter `LibsndfileError`) nicht ab: `process` endete mit einem Traceback und Exitcode 1. Die Änderung gilt in `main()` für alle Befehle, geprüft ist sie für `process`. Unverändert: Bei mehreren Dateien bricht `process` am ersten defekten Titel ab (bereits geschriebene Ausgabedateien bleiben liegen, die übrigen Titel werden nicht verarbeitet). Die Meldung stammt von libsndfile und nennt den Dateinamen nicht in jedem Fall (zum Beispiel nicht bei einer abgeschnittenen FLAC-Datei).

### Hinzugefügt

- 53 neue Tests (Zwischenstand: 317 Tests, 0.2.0: 264).
  - `tests/test_lr_channels.py` (neu, 49 Tests): unabhängige L/R-Funktionen mit Impulsen und getrennten L/R-Signalen, Sollwerte fest im Test. EQ (getrennte Ketten, unterschiedliche Filtertypen, Kanaltrennung), Pegel (unabhängige Verstärkung, Grenzen ±12 dB, Ausgangsamplituden, gemeinsamer Preamp) und Delay (Sample-Rundung, Grenzen 0 bis 50 ms, Dateilänge).
  - `tests/test_dsp.py` (4 neue Tests): Delay-Grenze beim APO-Import (−1, 0, 50 und 100 ms).
- 311 weitere Tests, nur Testcode und Testdaten. Die Testsuite umfasst damit 628 Tests (0.2.0: 264).
  - `tests/test_cli.py` (1 neuer Test): `test_process_corrupt_audio_returns_2` (siehe „Behoben“).
  - `tests/test_compat_020.py` (neu, 187 Tests), dazu `tests/compat_020_cases.py` und `tests/data/compat_020/`: Rückwärtskompatibilität gegen 0.2.0. Profile (Beispiele, JSON, Equalizer-APO-Text) ergeben im aktuellen Stand dieselben Werte wie 0.2.0. Die gespeicherten Sollwerte (`expected.json`) wurden mit `make_expected.py` aus dem unveränderten Tag v0.2.0 erzeugt. Zusätzlich wird die Filterausgabe mit einer unabhängigen Referenzrechnung (`tests/dsp_reference.py`) verglichen.
  - `tests/test_numeric_stability.py` (neu, 35 Tests): Stabilität, Endlichkeit und Genauigkeit der Biquad-Filter über ein Raster extremer Parameter (384 Fälle: 44,1 bis 192 kHz, 20 Hz bis 20 kHz, Güte 0,5 bis 1000, ±24 dB) und der Auto-Preamp bei sehr schmalen Spitzen.
  - `tests/test_audio_edge_cases.py` (neu, 76 Tests): Randfälle beim Verarbeiten von Audiodateien (Formate und Bittiefen, 1, 3, 6 und 8 Kanäle, sehr kurze, leere und nicht lesbare Dateien, Filter oberhalb der Nyquistfrequenz, Befehl `process`).
  - `tests/test_lr_simulator.py` (neu, 12 Tests): Stereo und Mono mit unterschiedlichen L/R-Ketten gegen den simulierten Renderer. Mono und Dither sind als Ist-Zustand festgehalten, nicht bewertet.
  - Unter Windows werden 12 Tests aus `tests/test_numeric_stability.py` übersprungen, weil `numpy.longdouble` dort nur float64 ist und die Referenz in erweiterter Genauigkeit fehlt. Diese Genauigkeitsprüfung ist unter Windows nicht belegt, unter Linux läuft sie.

### Geändert

- `kefplayer/__init__.py`, `pyproject.toml`: Version 0.2.1. `tests/test_api.py`: die eine Zeile, die die Versionsnummer prüft, erwartet jetzt 0.2.1.
- Sonst keine Änderung an DSP-Funktionen, Audioausgabe oder Profilformat.

### Bekannte Einschränkungen

- Wie bei 0.2.0: Es wurde **kein echter KEF-Lautsprecher** getestet, die Hardware-Validierung ist frühestens ab März 2027 vorgesehen. Die übrigen Einschränkungen von 0.2.0 (siehe unten) gelten unverändert.
- Windows: 628 Tests, davon 616 bestanden und 12 übersprungen (siehe oben; Python-Version und Bibliotheksversionen für diesen Lauf nicht festgehalten, Prüfziel CPython 3.12.10). Der Lauf erfolgte vor der Anhebung der Version, vor dem Entfernen vereinzelter CR-Zeichen und einer zusätzlichen Leerzeile am Dateiende in `kefplayer/cli.py`, `tests/test_cli.py` und `tests/test_audio_edge_cases.py` und vor der Berichtigung eines Satzes in der Modulbeschreibung von `tests/test_audio_edge_cases.py`. Nachtrag nach der Veröffentlichung: Mit dem Release-ZIP v0.2.1 wurde unter Windows erneut getestet (CPython 3.12.10, 616 bestanden, 12 übersprungen, 0 fehlgeschlagen, siehe README).
- Linux: 628 Tests bestanden auf CPython 3.10, 3.11, 3.12 und 3.13 vor diesen Änderungen. Mit dem veröffentlichten Stand wurde nur CPython 3.12.3 ausgeführt.
- Keine Windows-EXE enthalten.

## 0.2.0 (veröffentlicht am 15.11.2026)

Änderungen gegenüber 0.1.2 (die Versionen 0.1.x waren nicht öffentlich).

### Hinzugefügt

- **Lokale, nur lesende Status-API** (`kefplayer/api.py`, Beschreibung in `docs/status-api.md`):
  - bindet ausschließlich an `127.0.0.1`, erlaubt nur GET und HEAD (alles andere wird mit 405 abgelehnt), höchstens 16 gleichzeitige Verbindungen (darüber 503), 5 s Zeitlimit je Verbindung;
  - Endpunkte `/api/v1/status`, `/api/v1/renderer`, `/api/v1/dsp`, `/api/v1/audio`, `/api/v1/logs`, `/api/v1/diagnostics`;
  - bei `kefplayer play` standardmäßig an (Port 8766, ohne zusätzliche Renderer-Abfragen); Pfade und Gerätedaten sind standardmäßig verborgen.
- **Neue Optionen bei `play`:** `--no-api`, `--api-port`, `--api-live`, `--api-live-interval`, `--api-details`, `--diag-file`.
  - `--no-api` lädt das API-Modul gar nicht.
  - Widersprüchliche Angaben werden vor dem Start gemeldet: `--api-live`, `--api-details` oder `--diag-file` zusammen mit `--no-api`, `--api-port` gleich `--port`, `--api-live-interval` unter 0,5 s.
  - Ein belegter API-Port verhindert die Wiedergabe nicht (Warnung, die Wiedergabe läuft ohne API).
- `docs/status-api.md` und `docs/beispiel_diagnose_simulator.json` (Beispiel mit Werten aus dem Simulator, nicht von echter Hardware).
- 145 neue Tests zur Status-API. Die Testsuite umfasst damit 264 Tests (0.1.2: 119).
- `LICENSE` (MIT), `.gitignore`, `CHANGELOG.md`.

### Geändert

- `kefplayer/player.py` verwendet für Laufzeitmessungen (Start der Wiedergabe, Titelende, Warteschleife) `time.perf_counter()` statt `time.monotonic()`. In der getesteten Windows-Umgebung mit CPython 3.12.10 lieferte `time.monotonic()` eine effektive Auflösung von etwa 15,6 ms. Die aus diesen Zeitpunkten berechneten Protokollwerte (`startup_delay_s`, `wall_s`, `gap_before_s`) waren dadurch nur in Schritten dieser Größe aufgelöst. Im beobachteten Windows-Testlauf trat der sporadische Fehler `tests/test_player.py::test_neutral_serves_original_bytes` auf, weil `startup_delay_s` mit 0,015–0,016 s unter dem im Test erwarteten Mindestwert von 0,020 s lag.
- `kefplayer/cli.py`: neue Optionen und Start/Stopp der Status-API (siehe oben).
- `kefplayer/dsp.py`: nur der Text des Hinweises zu Filtern im Bassbereich (< 150 Hz) wurde allgemein verständlich umformuliert, projektinterne Begriffe sind entfernt. Keine Änderung an DSP-Logik, Filterberechnung, Schwellwerten oder an den übrigen Hinweisen.
- `kefplayer/__init__.py`, `pyproject.toml`: Version 0.2.0. `pyproject.toml` enthält jetzt Autor (Klaus), Lizenz (`license = "MIT"`, `license-files`), README als Beschreibung und die Build-Anforderung `setuptools>=77`. Laufzeitabhängigkeiten unverändert (numpy, scipy, soundfile).
- Tests:
  - `tests/mock_renderer.py` und `tests/test_player.py` (eine Zeile) verwenden dieselbe Zeitquelle `time.perf_counter()`.
  - `tests/test_upnp.py`: `test_server_request_log` wartet nach jeder Anfrage, bis der Medienserver den Protokolleintrag geschrieben hat. Der Server schreibt den Eintrag erst nach dem Senden der Antwort; der Test war dadurch zeitabhängig und schlug sporadisch fehl.
- `README.md` überarbeitet (Teststand, Hinweis auf die ausstehende Hardware-Validierung, Sicherheitshinweis zum Medienserver).

### Unverändert gegenüber 0.1.2

`kefplayer/audio.py`, `upnp.py`, `mediaserver.py`, `runlog.py`, `__main__.py`, die Beispielprofile in `examples/` sowie die Tests `test_audio.py`, `test_cli.py`, `test_dsp.py`, `test_review_fixes.py` und `conftest.py`.

### Bekannte Einschränkungen

- Es wurde **kein echter KEF-Lautsprecher** getestet. Alle Tests laufen gegen einen simulierten Renderer. Die Hardware-Validierung steht aus und ist frühestens ab März 2027 vorgesehen.
- Windows: getestet nur mit CPython 3.12.10 (Windows 11). Linux: CPython 3.10, 3.11, 3.12 und 3.13. Einzelheiten und Versionen der Bibliotheken: siehe README, Abschnitt „Tests und Prüfstand“.
- Der Medienserver bindet standardmäßig an `0.0.0.0` und kann dadurch im Netzwerk erreichbar sein.
- Es werden keine Aussagen über Klang, Klanggewinn oder Hardwarekompatibilität gemacht.
