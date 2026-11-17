# Status-API (ab Version 0.2.0)

Lokale, **ausschließlich lesende** Schnittstelle (HTTP + JSON), mit der der Laufzeitzustand des KEFplayers abgefragt und für die gemeinsame technische Analyse exportiert werden kann. Modul: `kefplayer/api.py`.

Stand: 15.11.2026. Die Status-API wurde nur gegen einen simulierten Renderer getestet, nicht mit einer echten KEF. Die Testsuite lief unter Linux (CPython 3.10–3.13) und unter Windows 11 (CPython 3.12.10), Einzelheiten im README, Abschnitt „Tests und Prüfstand“. **Die PowerShell-Beispiele in diesem Dokument wurden bisher nicht ausgeführt.** Die Endpunkte selbst sind mit Python-Clients getestet.

## Änderungen nach 0.3.0 (Entwicklungsstand, noch nicht veröffentlicht)

Alles Folgende ist rein additiv (`api_version` bleibt 1) und wurde nur gegen den simulierten Renderer getestet, nicht mit einer echten KEF.

- **Fehlerkategorien** (`error_category`) an Titelergebnissen und Befunden, siehe „Fehlerkategorien“.
- **Wiederholung bei vorübergehenden Verbindungsfehlern** (Player): Ereignisse `renderer_retry` und `renderer_recovered`, Befund `renderer_retries`.
- **Kategorie, Gültigkeitsbereich und Zeitstempel**: jedes Ereignis in `/logs` und jeder Befund hat `category`; Befunde haben zusätzlich `scope` (`queue` oder `process`), `ts` und `ts_source`. `/logs` kennt den Parameter `category`. Die Zusammenfassung enthält `findings_by_category` und `findings_by_scope`.
- **Gesundheitsüberwachung** mit eigenem Endpunkt `/api/v1/health`, siehe „Gesundheit“. **Sie fragt im Leerlauf den Renderer lesend ab** und ändert damit die Aussage „ohne Renderer-Abfragen“ aus dem Abschnitt „Starten und Abschalten“ für den Standardbetrieb von `play` (abschaltbar mit `--no-health`).
- Am Player-Code wurden dafür geändert: Wiederholung bei Timeout/Nichterreichbarkeit, Fehlerkategorien, das Flag `track_active` und die öffentliche Methode `emit()`. Die Aussage im folgenden Abschnitt („Am Player-Code wurde für die API nichts geändert“) bezieht sich auf 0.2.0.

## Was sie tut und nicht tut

- Nur `GET` und `HEAD`. Jede andere Methode (POST, PUT, DELETE, PATCH, OPTIONS, TRACE …) wird mit 405 abgelehnt. Es gibt keinen Steuerbefehl: kein Stopp, keine Pause, kein Überspringen, keine Lautstärke.
- Nur lokal: bindet ausschließlich an `127.0.0.1`. Eine andere Bind-Adresse ist weder per Option noch im Code zugelassen.
- Die Wiedergabe kennt die API nicht. Der Player reicht seine Ereignisse nur an einen Rückruf (`on_event`), den ein Sammler bedient. Für die API selbst (0.2.0) wurde am Player-Code nichts geändert (`Player(on_event=...)` ist unverändert). Gegenüber 0.1.2 geändert sind in 0.2.0 nur `player.py` (Zeitquelle `time.perf_counter()` statt `time.monotonic()`, siehe CHANGELOG), `dsp.py` (nur der Text eines Hinweises, keine DSP-Logik), `cli.py` (Optionen, Start/Stopp) und die Versionsnummer; `audio.py`, `upnp.py`, `mediaserver.py` und `runlog.py` sind unverändert. **Änderungsstand nach 0.3.0 (B1–C4):** geändert sind `player.py` (Fehlerklassifikation, Wiederholung, `track_active`, `emit()`), `api.py` (Kategorien, Zeitstempel, Gesundheit), `cli.py` (`health`, Überwachung in `play`) und neu `health.py`; `audio.py`, `dsp.py`, `upnp.py`, `mediaserver.py` und `runlog.py` sind gegenüber 0.3.0 unverändert.
- Die API läuft in eigenen Daemon-Threads. Der Sammler hält sein Schloss nur zum Kopieren des Zustands (Messwert: 15 µs je Ereignis im Test). Sie verändert weder Renderer-Aktionen noch Wiedergabeergebnisse noch die bitgenaue Auslieferung (Test mit 4 parallel abfragenden Clients).
- Werte, die nicht bekannt sind, stehen als `null` und erscheinen mit Grund in `unknown`. Es wird nichts aus alten Protokolldateien gelesen und nichts geschätzt.

## Starten und Abschalten

Beim Befehl `play` ist die API **standardmäßig an** (Entscheidung zur Bestätigung, siehe unten).

| Option | Wirkung |
|---|---|
| *(keine)* | API an, `http://127.0.0.1:8766/api/v1/…`; die API selbst sendet keine Renderer-Abfragen (die Gesundheitsüberwachung schon, siehe unten) |
| `--no-health` | Gesundheitsüberwachung aus (keine Abfragen im Leerlauf; `/api/v1/health` meldet `enabled: false`) |
| `--health-interval S` | Abstand der Gesundheitsprüfung, Standard **30 s**, Untergrenze **15 s** |
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
| `/api/v1/logs` | Letzte Ereignisse dieses Prozesses, Parameter `limit` (1–300, Standard 50), `since` (nur Ereignisse mit größerer `seq`), `level` (`info`/`warn`/`error`), `category` (`renderer`, `upnp`, `media_server`, `audio`, `dsp`, `cache`, `process`, `diagnostics`; ungültig = 400) |
| `/api/v1/diagnostics` | Zusammenfassung, Befunde (`findings`), D0-Ursachenhypothesen (`diagnoses`) und D1-Shadow-Entscheidungen (`repair_decisions`), Parameter `events` (0–300, Standard 100) |
| `/api/v1/health` | Zustand von Renderer (HTTP und UPnP), Medienserver und Cache sowie der Überwachung selbst (siehe „Gesundheit“) |

Alle Endpunkte akzeptieren `?pretty=1` (eingerückte Ausgabe). Unbekannte oder mehrfach angegebene Parameter werden mit 400 abgelehnt.

### D0-Diagnosen (`data.diagnoses`)

Die Diagnosen werden ausschließlich aus den bereits vorhandenen `findings` abgeleitet. Sie führen keine zusätzlichen Prüfungen, Netzwerkzugriffe oder Aktionen aus. `findings` bleiben unverändert und sind weiterhin die maßgebliche Beobachtungsebene.

- `shadow_propose`: Eine bekannte, vorsichtige Regel passt; die Ursache ist eine Hypothese, keine bestätigte Diagnose.
- `suppress`: Keine ausreichend passende Regel oder widersprüchliche Evidenz; es wird keine Ursache behauptet.
- `observe`: Nur im internen Modus `off`; es wird ausschließlich beobachtet.
- `proposed_action` ist in D0 immer `null`. Es gibt keinen Reparaturcode.
- `confidence` ist `low`, `medium` oder `unknown`; `high` wird in D0 nicht vergeben.
- `evidence` enthält bis zu 25 zugehörige vorhandene Befunde einschließlich Kategorie, Zeitstempel und Evidenzdaten. Weitere Befunde werden nicht dupliziert; ihre Anzahl steht in `evidence_omitted`. Die Hypothesenregeln werten weiterhin alle Befunde aus, nicht nur die angezeigten 25.
- `scope: "queue"` kennzeichnet Befunde, deren Gültigkeit auf die aktuelle beziehungsweise zuletzt beendete Warteschlange begrenzt ist; sie tragen `evidence.queue_started_at` als Sitzungsbezug. Dazu gehören Track-/MIME-Befunde, Sink-Status, Retry-/Recovery-, Stop- und Cache-Cleanup-Zähler sowie Clipping-Guard. `scope: "process"` kennzeichnet prozessweite Zustände und Fehlerzähler, etwa Health-Monitor, API und Sammler. Ereignisse, die vor der ersten Queue eintreffen, bleiben bei den operativen Zählern prozessweit sichtbar. Retry-, Stop- und Cache-Cleanup-Zähler für Queue-Befunde sowie der Sink-Prüfstatus werden bei `queue_start` zurückgesetzt; die prozessweiten Summenzähler bleiben intern getrennt erhalten. Die Zusammenfassung zählt Befunde zusätzlich nach `findings_by_scope`. Die Kennzeichnung ist keine dauerhafte Historie und überlebt keinen Prozessneustart. Wiederholte API-Abfragen desselben Snapshots erzeugen keine neuen Ereignisse.
- `verification` ist `not_applicable_d0_no_action`, weil keine Aktion ausgeführt wird.

Gleiche Befundcodes werden grundsätzlich zusammengefasst. Bei `track_problem` erfolgt die Gruppierung zusätzlich nach der konkret belegten `evidence.error_category`: unterschiedliche bekannte Fehlerklassen erhalten getrennte Diagnoseeinträge und können unabhängig bewertet werden. Fehlende oder unbekannte Fehlerklassen bleiben jeweils in eigenen Einträgen ohne Hypothese (`suppress`); sie werden keiner bekannten Ursache zugeordnet. Die Diagnose-ID enthält bei `track_problem` die Fehlerklasse, damit getrennte Einträge eindeutig bleiben. Unbekannte Befundcodes bleiben sichtbar, erhalten aber keine Hypothese. D0 ist damit eine Diagnose-/Beobachtungsschicht und keine Selbstreparatur.

### D1-Shadow-Entscheidungen (`data.repair_decisions`)

D1 bewertet die D0-Hypothesen anhand der vollständigen Originalbefunde und – nur wenn ursachenspezifisch relevant – des Health-Zustands. D1 läuft synchron bei einer Anfrage an `/api/v1/diagnostics`; es gibt keinen Hintergrund-Worker und keine zusätzlichen Renderer-Abfragen. Die Ausgaben sind rein beobachtend:

- `status` ist aktuell `deferred` oder `suppressed`; `eligible` wird in D1 noch nicht vergeben.
- `reason` nennt den konkreten Zurückstellungs- oder Unterdrückungsgrund.
- `scope` ist nur nach exakter Korrelation der vollständigen D0-Evidenz mit den Originalbefunden `queue` oder `process`; andernfalls `unknown`.
- `finding_count` zählt die zugeordneten Originalbefunde. `simulated` ist immer `true`, `repair_executed` immer `false`.
- Auch bei passender Evidenz und gesundem erforderlichem Health-Zustand bleibt die Entscheidung `deferred` mit `no_repair_action_approved_in_d1`: D1 enthält noch keine freigegebene Reparaturaktion.

D1 verändert weder Findings noch D0-Diagnosen, Retry-Zähler oder Player-Zustand. Es führt keine Reparatur, keinen Neustart und keinen zusätzlichen Renderer-Befehl aus. Die detaillierte Feld- und Reason-Referenz sowie PowerShell-Beispiele stehen in [D1 – Shadow-Entscheidungen](repair-decisions.md).

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
- Fehlertexte werden bereinigt: URLs (enthalten das Zugriffs-Token des Medienservers), Pfade (Windows, UNC, POSIX) und IPv4-Adressen werden ersetzt. Das gilt auch für Fehlertexte fehlgeschlagener Live-Proben, bevor sie in Befunde und D0-Evidenz übernommen werden. Das ist **Best Effort**, keine Garantie. Dateinamen mit Leerzeichen in ungequoteten Pfaden können Reste hinterlassen. Einen Export vor dem Weitergeben ansehen.
- Unbekannte Ereignisarten erscheinen nur mit Feldnamen, nie mit Werten.
- Sichtbar bleiben: Dateinamen, Hersteller/Modell, Formate, Zeiten, Profil (Name, Filter, Pegel, Delays), Python-/Betriebssystem-/Bibliotheksversionen (kein Rechnername).

Mit `--api-details` erscheinen Pfade und Geräteangaben. Das Token der Medienserver-URL wird in **keinem** Modus ausgegeben. Der Modus lässt sich nur beim Start setzen, nicht per Anfrage.

## Live-Abfrage (`--api-live`)

- Aus im Standard, weil jede Abfrage zusätzlicher Verkehr zur KEF ist (Beobachtereffekt). Der Player fragt selbst 2 Anfragen je Abfrage (alle `--poll`, standardmäßig 0,5 s, und alle 0,05 s beim Start sowie in den letzten 3 s vor dem Titelende).
- Erlaubt sind ausschließlich `GetTransportInfo` und `GetPositionInfo` (im Code abgesichert, getestet).
- Nur auf Anfrage: kein Hintergrund-Polling der Live-Abfrage. Ohne API-Aufruf entsteht durch `--api-live` kein zusätzlicher Verkehr (die Gesundheitsüberwachung ist davon getrennt). Höchstens eine Abfragerunde (2 Anfragen) je `--api-live-interval`, egal wie viele Clients fragen (Standard also höchstens 2 zusätzliche Anfragen je Sekunde). `/dsp`, `/audio`, `/logs` fragen den Renderer nie.
- Läuft im HTTP-Thread des API-Aufrufers mit eigener Verbindungsinstanz und 2 s Timeout; der Player-Thread wartet nie darauf. Ist der Renderer nicht erreichbar, antwortet die API mit `live.available = false`, Fehlertext und `null`-Werten (HTTP 200).
- Der Wert kann bis zu `age_s` alt sein (Cache innerhalb des Intervalls). Ist das letzte Ergebnis älter als max(3 × Intervall, 3 s), gilt es als veraltet (`live.stale = true`): Position, Transportstatus und `connection.reachable` bleiben dann `null`/unbekannt statt einen alten Wert als aktuell auszugeben. Das betrifft vor allem den Export am Ende (`--diag-file`), der den Renderer nicht mehr abfragt.
- Zwischen zwei Titeln kann der Renderer Position 0 oder den Vorgänger melden.
- Ob die KEF zusätzliche Abfragen verträgt, ist unbekannt (Messung zu Hause nötig, ein Schritt nach dem anderen).

## Fehlerkategorien

`error_category` steht an `track_end` (und damit in `tracks` und `findings`) bei den Endgründen `error`, `prepare_error`, `never_started` und `timeout`. Es ist eine **Einordnung des Symptoms**, keine bewiesene Ursache.

| `error_category` | Bedeutung | Systemteil (`category`) |
|---|---|---|
| `renderer_timeout` | Zeitüberschreitung bei einer Renderer-Anfrage | renderer |
| `renderer_unreachable` | Verbindung zum Renderer nicht möglich (Netzwerkfehler) | renderer |
| `renderer_soap_fault` | Renderer antwortet mit einem UPnP-Fehlercode | upnp |
| `renderer_http_error` | Renderer antwortet mit einem HTTP-Fehlerstatus | upnp |
| `renderer_bad_response` | Antwort des Renderers nicht auswertbar | upnp |
| `renderer_never_fetched` | Titel startete nicht, und der Renderer hat die Datei beim Medienserver nie angefragt (Hinweis auf Netz, Firewall oder abgelehnte URL; kein Beweis) | media_server |
| `renderer_fetched_no_play` | Titel startete nicht, obwohl der Renderer die Datei angefragt hat (eher Format oder Renderer) | renderer |
| `playback_timeout` | Wiedergabe überschritt die Höchstdauer | renderer |
| `source_file_access` | Quelldatei nicht lesbar (fehlt, keine Berechtigung) | audio |
| `source_format` | Quelldatei in einem nicht lesbaren Format | audio |
| `processing_error` | Fehler bei der Verarbeitung (DSP) | dsp |
| `cache_io` | Fehler beim Schreiben in den Cache | cache |

Der Hinweis `renderer_never_fetched` stützt sich auf das Anfrageprotokoll des Medienservers, das erst nach dem Senden schreibt (siehe „Nicht geprüft / Grenzen“).

## Gesundheit (`/api/v1/health`)

Die Gesundheitsüberwachung (`kefplayer/health.py`) läuft bei `play` standardmäßig in einem eigenen Daemon-Thread, auch zwischen den Titeln und vor dem Start. Einmalig ohne Wiedergabe: `kefplayer health` (siehe README).

Beim einmaligen Befehl `kefplayer health` läuft der Medienserver noch nicht: Die Prüfung kontrolliert deshalb, ob der konfigurierte Port gebunden werden kann (Port frei = `ok`; belegt oder nicht nutzbar = `warn`). Während `play` prüft sie dagegen, ob der gestartete Medienserver-Thread läuft und Verbindungen annimmt. Der CLI-Exitcode ist **1 nur bei Gesamtstatus `error`**; `warn` und `unknown` liefern 0.

| Komponente | Prüfung |
|---|---|
| `renderer_http` | Antwortet die Gerätebeschreibung (HTTP-GET)? |
| `renderer_upnp` | Antwortet der AVTransport-Dienst auf `GetTransportInfo`? Wird übersprungen (`unknown`), wenn `renderer_http` fehlschlägt |
| `media_server` | Läuft der Server-Thread und nimmt der Port Verbindungen an? |
| `cache` | Ist das Cache-Verzeichnis beschreibbar? Warnung unter 500 MB, Fehler unter 50 MB freiem Platz |

- **Status:** `ok`, `warn`, `error` oder `unknown`; `overall` ist der schlechteste Wert.
- **Intervall:** Standard 30 s, Untergrenze 15 s (`--health-interval`). Nur lesende Abfragen (HTTP-GET, `GetTransportInfo`).
- **Keine Renderer-Abfragen, solange ein Titel spielt** (`monitor.renderer_checks_paused`); dann fragt der Player selbst. Medienserver und Cache werden weiter lokal geprüft. Während der Pause werden die Renderer-Komponenten auf `unknown` gesetzt: Ein vorheriger Fehler bleibt nicht fälschlich als aktueller Fehler stehen. Nach Ende der Pause stellt die nächste echte Prüfung den Zustand wieder fest.
- **Ereignisse:** nur bei Zustandswechsel (`health_change` mit `previous`, `status`, Kategorie, Meldung); kein Dauerrauschen im Protokoll. In `/logs` erscheinen sie mit `category` der Komponente und `level` nach Status. Auf der Konsole von `play` stehen nur Probleme und deren Behebung.
- **Befunde:** `health_<komponente>` bei `warn`/`error` (Zeitpunkt = Beginn des Zustands), `health_monitor_stalled`, wenn die Überwachung keine aktuellen Runden mehr liefert. Nach regulärem Beenden gilt sie als `stopped`, nicht als hängend.
- **Überwachung selbst:** `monitor.state` (`running`, `stalled`, `stopped`, `not_started`), `ticks`, `errors`, `last_error`, `last_tick_at`. Ein Fehler in einer Runde beendet den Thread nicht.
- **Datenschutz:** `host` und `dir` in der Evidenz nur mit `--api-details`; Fehlertexte werden bereinigt wie sonst.

**Grenzen:**
- Wie die echte KEF auf periodische `GetTransportInfo`-Abfragen im Leerlauf reagiert, ist **nicht geprüft**.
- Eine Windows-Firewall, die eingehende Verbindungen vom Renderer blockiert, ist von hier aus **nicht erkennbar** (lokale Verbindungen umgehen sie). Ein Hinweis darauf ist nur `renderer_never_fetched`.
- Ein Titelstart kann mit einer gerade laufenden Prüfrunde zusammenfallen; das kostet höchstens eine zusätzliche Abfrage.
- Ein Health-Status `ok` bestätigt nur die Erreichbarkeit der jeweils geprüften Dienste (HTTP-Gerätebeschreibung, `GetTransportInfo`, lokaler Medienserver, Cache), **nicht** die vollständige Funktionsfähigkeit der KEF.
- `renderer_unreachable` fasst unterschiedliche Ursachen von `OSError` zusammen (z. B. Verbindung verweigert, zurückgesetzt, kein Weg zum Host, Namensauflösung, lokale Socket-Fehler). Die Kategorie nennt das Symptom, nicht die Ursache.
- Das Verhalten der realen KEF im Standby ist **nicht geprüft**. Ob und wie sie dort auf HTTP- und UPnP-Abfragen antwortet, ist unbekannt.

## Diagnoseexport

`/api/v1/diagnostics` (und `--diag-file`, dort mit `events=300`, `final: true` und ohne neue Renderer-Abfrage) enthält:

- `summary`: Phase, Titel gesamt/beendet, Endegründe, Anzahl Befunde je Stufe, Profil, Renderer-Modell
- `findings`: Liste aus `level` (`error`/`warn`/`info`), `code`, `category`, `scope` (`queue`/`process`), `message`, `ts`, `ts_source`, `evidence`. `queue` gilt für die aktuelle/zuletzt beendete Queue, `process` für den laufenden Player-Prozess. `ts` ist der Zeitpunkt des zugrunde liegenden Ereignisses (`ts_source: "event"`, z. B. Titelende oder Zustandswechsel der Überwachung); bei Befunden, die aus Zählern abgeleitet sind (z. B. `stop_failed`), ist es der Zeitpunkt der Auswertung (`ts_source: "evaluation"`). Die Zusammenfassung enthält zusätzlich `findings_by_category` und `findings_by_scope`.
- `health`: derselbe Inhalt wie `/api/v1/health`
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

- Echte KEF (Verhalten bei zusätzlichen Abfragen mit `--api-live` und bei der Gesundheitsüberwachung im Leerlauf): nicht geprüft.
- Windows 11: Die Testsuite (einschließlich der API-Tests zu Bindung und belegtem Port) lief gegen den Simulator. Nicht geprüft sind dort die Firewall-Hinweise und das Verhalten von `Invoke-RestMethod`.
- PowerShell 5.1/7: Die Beispiele in diesem Dokument wurden bisher nicht ausgeführt.
- Eine Windows-Firewall-Regel ist nicht nötig, solange nur auf demselben PC abgefragt wird (Loopback).
- Pause ist ohne `--api-live` nicht erkennbar (`track_active`). Position und Transportstatus gibt es nur live.
- Der Medienserver-Anfrageverlauf wird nur über die Zusammenfassung je Titel (aus dem Ereignis `track_end`) gelesen, nicht live.
- Der Medienserver schreibt eine Zeile seines Anfrageprotokolls erst **nach** dem Senden der Antwort (Code in `mediaserver.py`, unverändert). Eine Anfrage, die genau beim Titelende noch läuft, kann deshalb in `requests` fehlen. Im Betrieb nicht beobachtet; im Test `test_server_request_log` zeigt sich dieselbe Reihenfolge als Zeitwettlauf (siehe README, Abschnitt Prüfung). Das betrifft den Player, nicht die API.
- Titel aus Tags, Interpret, Cover, Playlist-Namen: nicht enthalten.

## Prüfung (simulierter Renderer)

Die Testsuite (264 Tests) lief unter Linux (CPython 3.10–3.13) und unter Windows 11 (CPython 3.12.10), jeweils gegen den Simulator. Einzelheiten und Versionen stehen im README, Abschnitt „Tests und Prüfstand“.

145 neue Tests in `tests/test_api.py` (Sammler, Datenschutz, Unbekanntes, Befunde, Statistik, JSON/ASCII, Größenlimit), `tests/test_api_server.py` (Bindung, Methoden, Host/Origin, kaputte Anfragen, langsame Clients, Verbindungslimit, Nebenläufigkeit, Beenden, belegter Port), `tests/test_api_integration.py` (Spiegelung des Players, unveränderte Wiedergabe unter Last, null Renderer-Anfragen ohne `--api-live`, nur `Get*` und gedrosselt mit Live, Renderer nicht erreichbar) und `tests/test_api_cli.py` (Standard, `--no-api` ohne Import und ohne Port, widersprüchliche Optionen, belegter Port, Diagnoseexport auch nach Strg+C, Aufräumen). Zusätzlich 14 Sabotageversuche (Host-Prüfung, Methodensperre, Bereinigung, Bind-Adresse, Get-Whitelist, Verbindungslimit, Rückruf-Schutz, Datenschutz-Filter, Origin-Prüfung, Token-Entfernung, Verbergen der Medienserver-Bind-Adresse, Veraltungsgrenze der Live-Werte, `--no-api`-Import, Server-Stopp): jede wurde von mindestens einem Test erkannt.
