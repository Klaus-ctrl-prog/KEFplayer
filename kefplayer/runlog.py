"""Einfaches JSON-Lines-Protokoll (ein Ereignis pro Zeile)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class RunLog:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **fields}
        if self.path:
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec
