# D2: Opt-in-Reparatur (Stand: nur R5)

Standard: **AUS**. Ohne `--enable-repair` laeuft nur D1 im Schattenmodus (keine Eingriffe).

Mit `kefplayer play --enable-repair ...` (nicht mit `--no-health`):

- Einzige Aktion **R5**: haengenden Diagnose-Monitor (Zustand `stalled`) neu starten. Der Monitor liest nur; der Neustart ist umkehrbar.
- **Nie waehrend ein Titel spielt** (`Player.track_active`); unbekannter Zustand gilt als "beschaeftigt".
- Greift **nie** auf `RENDERER_LOCK` zu und wartet nie auf ihn. Kein Renderer-Zugriff, kein Reboot, kein Neustart des Medienservers.
- Budget: max. 3 Versuche je 10 min, 30 s Pause, global max. 6/h, Flapping-Sperre (siehe `repair_budget.py`).
- Pruefung nach dem Neustart (laeuft, mind. ein Takt); sonst wird zurueckgerollt (alter Monitor bleibt, Ergebnis `failed`).
- Audit: Ereignisse `repair_start`, `repair_result`, `repair_blocked` im JSONL-Protokoll (`--log`) und Abschnitt `repair` in `/api/v1/health`.

Nicht auf echter KEF Gen1 validiert; nur Simulatortests. R1/R3 liegen getrennt in PR #10 und sind nicht aktiv.
