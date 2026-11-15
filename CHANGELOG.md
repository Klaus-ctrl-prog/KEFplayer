# Changelog

## 0.2.0 (Release-Kandidat, Stand 15.11.2026)

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
