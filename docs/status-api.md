# Status-API (ab Version 0.2.0)

Lokale, **ausschließlich lesende** Schnittstelle (HTTP + JSON), mit der der Laufzeitzustand des KEFplayers abgefragt und für die gemeinsame technische Analyse exportiert werden kann. Modul: `kefplayer/api.py`.

Stand: 14.11.2026. Entwickelt ohne Zugang zu PC, KEF und Heimnetz, getestet gegen einen simulierten Renderer unter Linux. **PowerShell wurde nicht ausgeführt**, die Beispiele unten sind ungetestet (der Server ist mit Python-HTTP-Clients geprüft).

## Was sie tut und nicht tut

- Nur `GET` und `HEAD`. Jede andere Methode (POST, PUT, DELETE, PATCH, OPTIONS, TRACE …) wird mit 405 abgelehnt. Es gibt keinen Steuerbefehl: kein Stopp, keine Pause, kein Überspringen, keine Lautstärke.
- Nur lokal: bindet ausschließlich an `127.0.0.1`. Eine andere Bind-Adresse ist weder per Option noch im Code zugelassen.
- Die Wiedergabe kennt die API nicht. Der Player reicht seine Ereignisse nur an einen Rückruf (`on_event`), den ein Sammler bedient. Am Player-Code (`player.py`, `dsp.py`, `audio.py`, `upnp.py`, `mediaserver.py`, `runlog.py`) wurde für 0.2.0 nichts geändert; angepasst ist nur `cli.py` (Optionen, Start/Stopp) und die Versionsnummer.
- Die API läuft in eigenen Daemon-Threads. Der Sammler hält sein Schloss nur zum Kopieren des Zustands (Messwert: 15 µs je Ereignis im Test). Sie verändert weder Renderer-Aktionen noch Wiedergabeergebnisse noch die bitgenaue Auslieferung (Test mit 4 parallel abfragenden Clients).
- Werte, die nicht bekannt sind, stehen als `null` und erscheinen mit Grund in `unknown`. Es wird nichts aus alten Protokolldateien gelesen und nichts geschätzt.

## Starten und Abschalten

Beim Befehl `play` ist die API **standardmäßig an** (Entscheidung zur Bestätigung, siehe unten).

| Option | Wirkung |
|---|---|
| *(keine)* | API an, `http://127.0.0.1:8766/api/v1/…`, ohne Renderer-Abfragen |
| `--no-api` | API vollständig aus: kein Port, kein Sammler, `kefplayer.api` wird nicht einmal importiert |
| `--api-port N` | anderer Port (Standard **8766**; 0 = beliebig, der gewählte steht in der Startmeldung) |
| `--api-live` | Position und Transportstatus per gedrosselter, nur lesender Renderer-Abfrage liefern (siehe „Live-Abfrage“) |
| `--api-live-interval S` | Mindestabstand der Live-Abfragen, mindestens 0,5 s, Standard 1,0 s |
| `--api-details` | Pfade, Renderer-Name/IP/UDN und Medienserver-Adresse zeigen (Standard: verborgen) |
| `--diag-file DATEI` | am Ende der Wiedergabe den Diagnoseexport (JSON) schreiben, auch nach Strg+C |

Widersprüche werden vor jedem Netzwerkzugriff gemeldet: `--no-api` zusammen mit `--api-live`, `--api-details` oder `--diag-file`; `--api-port` gleich `--port`; `--api-live-interval` unter 0,5.

Ist der API-Port belegt, erscheint eine Warnung, und die **Wiedergabe läuft ohne API weiter**. Beim Beenden des Players (Ende der Warteschlange, Strg+C, Fehler) wird zuerst der Diagnoseexport geschrieben, dann der API-Server geschlossen. Nach dem Ende ist der Port zu: die Status-API lebt nur, solange `kefplayer play` läuft. Den Endzustand liefert `--diag-file`.

**Abweichung vom Vorschlag:** Der Port 8765 ist bereits der Standardport des Medienservers (`--port`). Die API verwendet deshalb 8766.

## Endpunkte

Alle unter `http://127.0.0.1:8766`. `/` und `/api/v1` listen die Endpunkte auf.

| Pfad | Inhalt |
|---|---|
| `/api/v1/status` | Phase, aktueller Titel (Name, Dauer, Verarbeitung), Wandzeit seit Titelstart, letzter beendeter Titel, letztes Ereignis; mit `--api-live` zusätzlich Position und Transportstatus |
| `/api/v1/renderer` | Hersteller/Modell, UPnP-Dienste, Sink-Formate (Eigenangabe des Geräts), abgeleitete Fähigkeiten (FLAC/WAV/MP3 im Sink), Verbindung (nur live belegt) |
| `/api/v1/dsp` | Aktives Profil, Filter, Preamp-Einstellung, Kanalpegel und -verzögerungen (L/R), je Titel angewendeter Preamp/Spitzenwerte |
| `/api/v1/audio` | Je Titel Quellformat, Zielformat (gesendet), MIME, „im Sink gemeldet“, Verarbeitungspfad (`original`/`convert`/`dsp`) |
| `/api/v1/logs` | Letzte Ereignisse dieses Prozesses, Parameter `limit` (1–300, Standard 50), `since` (nur Ereignisse mit größerer `seq`), `level` (`info`/`warn`/`error`) |
| `/api/v1/diagnostics` | Zusammenfassung für die technische Analyse (siehe unten), Parameter `events` (0–300, Standard 100) |

Alle Endpunkte akzeptieren `?pretty=1` (eingerückte Ausgabe). Unbekannte oder mehrfach angegebene Parameter werden mit 400 abgelehnt.

### Antwortrahmen

    {
      "api_version": 1, "kefplayer_version": "0.2.0", "endpoint": "status",
      "generated_at": "…UTC…", "uptime_s": 12.3, "read_only": true,
      "privacy": {"mode": "standard", "redacted": [...], "always_removed": ["URL-Token des Medienservers"]},
      "data": { … },
      "unknown": { "feld.pfad": "Grund, warum der Wert fehlt" }
    }

Fehler: `{"api_version":1,"error":{"status":404,"message":"…"}}` mit passendem HTTP-Status (400 Parameter, 403 Host/Origin, 404 Pfad, 405 Methode, 500 intern ohne Details, 503 zu viele Verbindungen).

### Phase (`status.data.phase`)

| Wert | Bedeutung |
|---|---|
| `no_queue` | noch keine Warteschlange gestartet |
| `preparing` | Warteschlange läuft, noch kein Titel gestartet |
| `track_active` | der Player führt einen Titel (Startwartezeit, Wiedergabe **oder Pause**; was davon, nur live) |
| `between_tracks` | Titel beendet, nächster noch nicht gestartet |
| `queue_ended` | Warteschlange beendet |

## Unbekannt, null, „redacted“

- `null` plus Eintrag in `unknown` = nicht verfügbar. Beispiele: `track.position_s`, `track.transport_state`, `track.paused` ohne `--api-live`; `connection.reachable` ohne Live-Abfrage; Quellformat, wenn die Quelldatei nicht mehr lesbar ist; `capabilities.gapless` (nicht abgefragt).
- `"redacted"` = absichtlich verborgen (Datenschutz), nicht unbekannt.
- **Titel** ist der Dateiname ohne Endung (`title.source = "filename"`). Tags liest die API nicht; der Player meldet sie nicht in seinen Ereignissen.
- **Position** gibt es nur live. `wall_since_start_s` ist die Wandzeit seit dem Titelstart-Ereignis (enthält Startwartezeit und Pausen), **keine** Wiedergabeposition.
- **Quellformat** bei verarbeiteten Titeln liest die API aus den Kopfdaten der Quelldatei (nur lesend, im HTTP-Thread, gecacht). Bei unverarbeiteten Titeln ist die gesendete Datei das Original.
- **Zeitwerte** (`startup_delay_s`, `gap_before_s`) haben die Auflösung der Player-Abfragen (`timing.resolution_hint`: ca. `fast_poll_s` beim Start und kurz vor dem Titelende, sonst `poll_s`, plus SOAP-Latenz). Das ist ein Modell, kein Messwert. Befunde (`findings`) nennen nur Tatsachen (Fehlerende, zu frühes Ende, MIME nicht im Sink, Sink nicht lesbar, fehlgeschlagenes Stoppen/Löschen, Clipping-Schutz aktiv). Für Lücken werden keine Schwellen bewertet, nur Statistik (n, min, Median, Mittel, max).

## Datenschutz

Standard (`--api-details` nicht gesetzt):

- Keine Verzeichnispfade (nur Dateinamen), keine Renderer-IP, -UDN, -URL und kein Gerätename, keine Medienserver-Adresse, keine Client-IPs der Renderer-Anfragen.
- Fehlertexte werden bereinigt: URLs (enthalten das Zugriffs-Token des Medienservers), Pfade (Windows, UNC, POSIX) und IPv4-Adressen werden ersetzt. Das ist **Best Effort**, keine Garantie. Dateinamen mit Leerzeichen in ungequoteten Pfaden können Reste hinterlassen. Einen Export vor dem Weitergeben ansehen.
- Unbekannte Ereignisarten erscheinen nur mit Feldnamen, nie mit Werten.
- Sichtbar bleiben: Dateinamen, Hersteller/Modell, Formate, Zeiten, Profil (Name, Filter, Pegel, Delays), Python-/Betriebssystem-/Bibliotheksversionen (kein Rechnername).

Mit `--api-details` erscheinen Pfade und Geräteangaben. Das Token der Medienserver-URL wird in **keinem** Modus ausgegeben. Der Modus lässt sich nur beim Start setzen, nicht per Anfrage.

## Live-Abfrage (`--api-live`)

- Aus im Standard, weil jede Abfrage zusätzlicher Verkehr zur KEF ist (Beobachtereffekt). Der Player fragt selbst 2 Anfragen je Abfrage (alle `--poll`, standardmäßig 0,5 s, und alle 0,05 s beim Start sowie in den letzten 3 s vor dem Titelende).
- Erlaubt sind ausschließlich `GetTransportInfo` und `GetPositionInfo` (im Code abgesichert, getestet).
- Nur auf Anfrage: kein Hintergrund-Polling. Ohne API-Aufruf entsteht kein zusätzlicher Verkehr. Höchstens eine Abfragerunde (2 Anfragen) je `--api-live-interval`, egal wie viele Clients fragen (Standard also höchstens 2 zusätzliche Anfragen je Sekunde). `/dsp`, `/audio`, `/logs` fragen den Renderer nie.
- Läuft im HTTP-Thread des API-Aufrufers mit eigener Verbindungsinstanz und 2 s Timeout; der Player-Thread wartet nie darauf. Ist der Renderer nicht erreichbar, antwortet die API mit `live.available = false`, Fehlertext und `null`-Werten (HTTP 200).
- Der Wert kann bis zu `age_s` alt sein (Cache innerhalb des Intervalls). Ist das letzte Ergebnis älter als max(3 × Intervall, 3 s), gilt es als veraltet (`live.stale = true`): Position, Transportstatus und `connection.reachable` bleiben dann `null`/unbekannt statt einen alten Wert als aktuell auszugeben. Das betrifft vor allem den Export am Ende (`--diag-file`), der den Renderer nicht mehr abfragt.
- Zwischen zwei Titeln kann der Renderer Position 0 oder den Vorgänger melden.
- Ob die KEF zusätzliche Abfragen verträgt, ist unbekannt (Messung zu Hause nötig, ein Schritt nach dem anderen).

## Diagnoseexport

`/api/v1/diagnostics` (und `--diag-file`, dort mit `events=300`, `final: true` und ohne neue Renderer-Abfrage) enthält:

- `summary`: Phase, Titel gesamt/beendet, Endegründe, Anzahl Befunde je Stufe, Profil, Renderer-Modell
- `findings`: Liste aus `level` (`error`/`warn`/`info`), `code`, `message`, `evidence`
- `timing`: Statistik von `gap_before_s` und `startup_delay_s`, Auflösungshinweis
- `status`, `renderer`, `dsp`, `audio`: dieselben Daten wie die Einzelendpunkte; `unknown` führt alles mit Präfix des Bereichs
- `tracks`: je beendetem Titel Name, Verarbeitung, MIME, `mime_in_sink`, Format, Endegrund, Startzeit, Lücke, Position bei Ende, Fehler, Anfragen (Anzahl, Methoden, Status, Bytes, Range) und angewendete DSP-Werte; höchstens die letzten 300 (`tracks_omitted` zählt den Rest)
- `events`: letzte Ereignisse; `environment`: Python, Betriebssystem, numpy, scipy, soundfile, libsndfile; `api`: Einstellungen und Zähler; `notes`: Grenzen der Daten

Die Antwort bleibt unter 1,5 MB (Windows PowerShell 5.1 liest JSON nur bis etwa 2 MB); größere Listen werden gekürzt und mit `truncated: true` markiert. Der Ereignispuffer hält 300 Ereignisse; verdrängte werden gezählt (`dropped_total`).

Ein vollständiges Beispiel eines Exports steht in `docs/beispiel_diagnose_simulator.json`. **Es stammt aus einem Lauf gegen den Simulator** (3 synthetische Töne à 3 s, `--api-live`), nicht von der KEF; die Zahlen sagen nichts über echtes Verhalten.

## PowerShell (ungetestet)

Immer `127.0.0.1` verwenden, nicht `localhost` (kann zuerst IPv6 versuchen; der Server lauscht nur auf IPv4).

    # Diagnose abfragen und exportieren (Beispiel aus der Anfrage; -Depth 15 ist nötig, der Standard 2 kürzt die Ausgabe)
    $D = Invoke-RestMethod "http://127.0.0.1:8766/api/v1/diagnostics"
    $D | ConvertTo-Json -Depth 15 | Set-Content "$HOME\Desktop\KEFplayer-Diagnose.json" -Encoding utf8

    # Oder die Antwort des Servers unverändert speichern (reines ASCII, eingerückt, bis zu 300 Ereignisse)
    Invoke-WebRequest "http://127.0.0.1:8766/api/v1/diagnostics?pretty=1&events=300" -OutFile "$HOME\Desktop\KEFplayer-Diagnose.json"

    # Aktuellen Titel und Position (Position nur mit --api-live), alle 2 s
    while ($true) {
        $s = (Invoke-RestMethod "http://127.0.0.1:8766/api/v1/status").data
        "{0}  {1}  {2}  {3}" -f $s.phase, $s.track.name, $s.track.transport_state, $s.track.position_s
        Start-Sleep 2
    }

    # Nur Warnungen und Fehler
    (Invoke-RestMethod "http://127.0.0.1:8766/api/v1/logs?level=warn&limit=100").data.events |
        Format-Table seq, ts, event, level, name, end_reason, error

    # Was ist unbekannt, und warum?
    $D.unknown.PSObject.Properties | ForEach-Object { "{0}: {1}" -f $_.Name, $_.Value }

Hinweise: `Set-Content -Encoding utf8` schreibt in Windows PowerShell 5.1 eine BOM (für JSON-Lesen unkritisch). Die API liefert reines ASCII (Umlaute als `\uXXXX`), daher gibt es keine Kodierungsfehler beim Einlesen. Lässt sich die Antwort nicht abfragen: läuft `kefplayer play` noch, und stimmt der Port in der Startzeile `Status-API: http://127.0.0.1:…`?

## Schutz gegen Fehlzugriffe

- Bindung nur 127.0.0.1; `Host`-Header muss `127.0.0.1` oder `localhost` (mit eigenem Port) sein, sonst 403 (Schutz gegen DNS-Rebinding). Fehlender `Host` wird ebenfalls abgelehnt.
- Keine CORS-Freigabe. Browser-Anfragen mit `Sec-Fetch-Site: cross-site` oder fremdem `Origin` werden mit 403 abgelehnt.
- Antwort je Verbindung (HTTP/1.0, `Connection: close`), 5 s Zeitlimit je Verbindung, höchstens 16 gleichzeitige Verbindungen (darüber 503), keine Tracebacks und keine Serverversion in Antworten, keine Ausgabe auf die Konsole des Players.
- Unter Windows ohne `SO_REUSEADDR` (kein Doppelbelegen des Ports).
- **Keine Anmeldung.** Jeder Prozess und jeder Benutzer auf demselben PC kann `127.0.0.1` erreichen. Mit den Datenschutz-Standardwerten sind die Inhalte unkritisch; auf einem geteilten PC `--no-api` verwenden oder die Standardwerte nicht ändern (kein `--api-details`).

## Nicht geprüft / Grenzen

- Echte KEF (Verhalten bei zusätzlichen Abfragen mit `--api-live`), Windows 11 (Port-Belegung, Firewall-Hinweise, Verhalten von `Invoke-RestMethod`), PowerShell 5.1/7 insgesamt.
- Eine Windows-Firewall-Regel ist nicht nötig, solange nur auf demselben PC abgefragt wird (Loopback).
- Pause ist ohne `--api-live` nicht erkennbar (`track_active`). Position und Transportstatus gibt es nur live.
- Der Medienserver-Anfrageverlauf wird nur über die Zusammenfassung je Titel (aus dem Ereignis `track_end`) gelesen, nicht live.
- Der Medienserver schreibt eine Zeile seines Anfrageprotokolls erst **nach** dem Senden der Antwort (Code in `mediaserver.py`, unverändert). Eine Anfrage, die genau beim Titelende noch läuft, kann deshalb in `requests` fehlen. Im Betrieb nicht beobachtet; im Test `test_server_request_log` zeigt sich dieselbe Reihenfolge als Zeitwettlauf (siehe README, Abschnitt Prüfung). Das betrifft den Player, nicht die API.
- Titel aus Tags, Interpret, Cover, Playlist-Namen: nicht enthalten.

## Prüfung (Linux, simulierter Renderer)

145 neue Tests in `tests/test_api.py` (Sammler, Datenschutz, Unbekanntes, Befunde, Statistik, JSON/ASCII, Größenlimit), `tests/test_api_server.py` (Bindung, Methoden, Host/Origin, kaputte Anfragen, langsame Clients, Verbindungslimit, Nebenläufigkeit, Beenden, belegter Port), `tests/test_api_integration.py` (Spiegelung des Players, unveränderte Wiedergabe unter Last, null Renderer-Anfragen ohne `--api-live`, nur `Get*` und gedrosselt mit Live, Renderer nicht erreichbar) und `tests/test_api_cli.py` (Standard, `--no-api` ohne Import und ohne Port, widersprüchliche Optionen, belegter Port, Diagnoseexport auch nach Strg+C, Aufräumen). Zusätzlich 14 Sabotageversuche (Host-Prüfung, Methodensperre, Bereinigung, Bind-Adresse, Get-Whitelist, Verbindungslimit, Rückruf-Schutz, Datenschutz-Filter, Origin-Prüfung, Token-Entfernung, Verbergen der Medienserver-Bind-Adresse, Veraltungsgrenze der Live-Werte, `--no-api`-Import, Server-Stopp): jede wurde von mindestens einem Test erkannt.
