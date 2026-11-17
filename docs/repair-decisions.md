# D1 – Shadow-Entscheidungen zur Selbstreparatur

Stand: 17.11.2026 · Implementiert in `kefplayer/repair_decision.py` und in die lesende Diagnose-API integriert.

## Zweck und Abgrenzung

D1 ist die zweite, weiterhin rein beobachtende Stufe der Diagnosekette:

1. **Findings** halten beobachtete Ereignisse und Befunde fest.
2. **D0** gruppiert diese Befunde und formuliert vorsichtige Ursachenhypothesen.
3. **D1** prüft, ob die D0-Hypothesen anhand der Originalbefunde und des relevanten Health-Zustands ausreichend belastbar eingeordnet werden können.

D1 führt **keine Reparatur** aus. Es startet und stoppt keine Dienste, sendet keine zusätzlichen Renderer-Befehle, verändert keine Wiederholungszähler und schreibt keine Reparaturversuche. Es gibt in dieser Stufe auch keine freigegebenen Reparaturaktionen. Die eigentliche Ausführung und Verifikation gehören in eine spätere, separat abzusichernde Stufe (D2).

D1 wird zusammen mit einer Anfrage an `/api/v1/diagnostics` berechnet. Es ist kein Hintergrund-Worker und erzeugt keine eigenständigen periodischen Entscheidungen.

## Abruf unter Windows / PowerShell

Während `kefplayer play` läuft, in einer zweiten PowerShell:

```powershell
$diag = Invoke-RestMethod 'http://127.0.0.1:8766/api/v1/diagnostics?pretty=1'
$diag.data.repair_decisions | Format-Table diagnosis_id, status, reason, scope, finding_count -AutoSize
```

Einzelne Entscheidung mit allen Feldern:

```powershell
$diag.data.repair_decisions | ConvertTo-Json -Depth 12
```

Die API ist lokal und lesend. Die Entscheidungen beziehen sich auf den Snapshot, den die jeweilige Anfrage ausliest. Nach Prozessende ist die laufende API nicht mehr erreichbar. Für einen Diagnoseexport am Ende kann `--diag-file DATEI.json` verwendet werden; der Export enthält ebenfalls `data.repair_decisions`.

## Ausgabefelder

Jeder Eintrag in `data.repair_decisions` bezieht sich auf eine D0-Diagnose:

| Feld | Bedeutung |
|---|---|
| `diagnosis_id` | D0-ID der zugehörigen Diagnosegruppe |
| `status` | `deferred` oder `suppressed` |
| `reason` | maschinenlesbarer Grund für die Einordnung |
| `scope` | verifizierter `queue`-/`process`-Geltungsbereich oder `unknown` |
| `finding_count` | Anzahl der zugeordneten Originalbefunde |
| `simulated` | in D1 immer `true`; Entscheidung ist nur eine Simulation |
| `repair_executed` | in D1 immer `false`; es wurde nichts repariert |

### Status

- **`deferred`**: Es gibt eine unterstützte D0-Hypothese, aber D1 gibt keine Reparatur frei. Entweder fehlen belastbare Zuordnungen/Health-Daten oder D1 enthält absichtlich noch keine freigegebene Reparaturaktion.
- **`suppressed`**: D0 liefert keine unterstützte Hypothese. D1 behauptet deshalb keine Ursache.

D1 liefert im aktuellen Entwicklungsstand **keinen Status `eligible`**. Auch eine vollständig passende Evidenz bei gesundem Health-Zustand endet mit `deferred` und `no_repair_action_approved_in_d1`. Das ist beabsichtigt und kein Fehler.

## Entscheidungsgründe

Die folgenden Gründe sind die aktuell implementierten Werte:

| Grund | Bedeutung |
|---|---|
| `no_supported_d0_hypothesis` | D0 hat keine unterstützte Ursachenhypothese geliefert; Status `suppressed` |
| `finding_scope_unresolved` | Die D0-Diagnose lässt sich keiner Originalbefundgruppe sicher zuordnen |
| `finding_evidence_invalid` | Evidenzstruktur oder Auslassungszähler ist ungültig |
| `finding_evidence_truncated` | D0 hat Evidenz gekürzt; eine vollständige Zuordnung ist daher nicht möglich |
| `finding_evidence_mismatch` | D0-Evidenz stimmt nicht exakt mit den zugehörigen Originalbefunden überein |
| `finding_scope_ambiguous` | Originalbefunde haben gemischte oder ungültige Geltungsbereiche |
| `health_mapping_unresolved` | Für eine breite Wiedergabepfad-Hypothese lässt sich aus `error_category` keine eindeutige Health-Komponente ableiten |
| `health_unavailable` | Erforderliche Gesundheitsüberwachung ist nicht verfügbar oder deaktiviert |
| `health_state_unknown` | Health-Monitor-Zustand ist nicht bekannt |
| `health_monitor_not_running` | Erforderlicher Health-Monitor läuft nicht |
| `renderer_health_checks_paused` | Renderer-Prüfungen sind aktuell pausiert; der Zustand darf nicht als gesund angenommen werden |
| `health_components_unknown` | Health-Komponentenstruktur fehlt |
| `required_health_unknown` | Eine für die Hypothese erforderliche Health-Komponente fehlt oder hat Status `unknown` |
| `required_health_not_ok` | Eine erforderliche Health-Komponente hat nicht den Status `ok` |
| `no_repair_action_approved_in_d1` | Evidenzzuordnung und gegebenenfalls relevante Health-Prüfung sind ausreichend, aber D1 genehmigt grundsätzlich noch keine Reparatur |

## Evidenz- und Health-Gates

### Originalbefunde und Geltungsbereich

D1 übernimmt den Geltungsbereich nicht blind aus einer Diagnose-ID. Es rekonstruiert die D0-Gruppierung aus den Originalbefunden und vergleicht die Evidenz vollständig und einschließlich Duplikaten. Nur wenn die Evidenz exakt übereinstimmt und nicht gekürzt wurde, wird `scope` als `queue` oder `process` übernommen.

- D0-Evidenz ist auf 25 Einträge begrenzt. Bei `evidence_omitted > 0` wird die Zuordnung zurückgestellt.
- Abweichende oder unvollständige Evidenz führt zu `scope: "unknown"`.
- Befunde mit gemischtem Geltungsbereich führen ebenfalls zu `scope: "unknown"`.
- D0-Evidenz enthält den Geltungsbereich nicht selbst; D1 korreliert sie mit den Originalbefunden der API.

Damit wird insbesondere verhindert, dass eine Queue-Störung versehentlich als prozessweiter Fehler oder umgekehrt eingeordnet wird. Eine dauerhafte Queue-ID wird nicht erfunden.

### Relevante Health-Komponenten

Health wird nur geprüft, wenn eine Ursache tatsächlich von einer bestimmten Komponente abhängt. Beispielsweise benötigt eine HTTP-Renderer-Ursache `renderer_http`, eine UPnP-Steuerursache `renderer_upnp`, ein Medienserverproblem `media_server` und ein Cacheproblem `cache`. Unbeteiligte Komponenten blockieren eine andere Ursache nicht.

Bei der breiten D0-Hypothese `renderer_or_delivery_path_playback_failure` wird die relevante Komponente anhand der belegten `error_category` bestimmt. Fehlt diese Zuordnung oder ist sie unbekannt, wird zurückgestellt. `unknown`, fehlende oder pausierte Health-Daten werden niemals stillschweigend als `ok` behandelt.

Ursachen ohne direkten Bezug zu einer Health-Komponente – etwa ein nicht lesbares Quelldateiobjekt – werden nicht durch einen sachfremden Health-Fehler blockiert.

## Was D1 ausdrücklich nicht bedeutet

- `deferred` ist keine Aufforderung, einen Dienst neu zu starten.
- `simulated: true` ist kein protokollierter Reparaturversuch.
- `finding_count` ist kein Retry-Zähler.
- Der bestehende Player-Retry-Mechanismus bleibt unabhängig; seine Backoff- und Versuchszähler sind kein D1-Budget oder D1-Cooldown.
- D1 schreibt keine eigenen persistenten Repair-Logs und verändert keine D0-Findings.
- Ein `ok`-Health-Status beweist nur den Zustand der tatsächlich geprüften Komponente zum Snapshot-Zeitpunkt, nicht die Fehlerfreiheit des gesamten Wiedergabepfads.

## Validierungsstand und nächste Prüfschritte

Der D1-Code ist durch Unit- und API-Tests abgesichert. Der gemergte PR #4 wurde in GitHub Actions auf Ubuntu und Windows erfolgreich geprüft. Das belegt die deterministische Logik und die API-Integration gegen die Testumgebung, **nicht** das Verhalten eines realen KEF-Lautsprechers.

Für die nächste Betriebsprüfung:

1. Eine normale Wiedergabe starten und `/api/v1/diagnostics` mehrfach während der Queue abrufen.
2. Prüfen, ob `diagnosis_id`, `finding_count`, `scope` und `reason` zu den sichtbaren `findings` und `diagnoses` passen.
3. Nach Queue-Wechseln kontrollieren, dass Queue-Befunde nicht in eine andere Queue übertragen werden.
4. Bei absichtlich fehlender/pausierter Health-Prüfung kontrollieren, dass D1 `deferred` statt gesund annimmt.
5. Diagnoseexport und Protokoll vor dem Teilen auf persönliche Pfade, Dateinamen und Geräteinformationen prüfen.

Bis zur geplanten Hardware-Validierung bleibt der KEF-Gen1-Betrieb unbestätigt. D2 darf erst nach Auswertung realer, nachvollziehbarer D1-Entscheidungen und einer eigenen Sicherheits-/Abbruchstrategie begonnen werden.
