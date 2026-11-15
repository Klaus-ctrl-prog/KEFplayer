# Changelog

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
