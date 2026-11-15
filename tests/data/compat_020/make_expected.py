"""Erzeugt expected.json aus einer UNVERAENDERTEN Version 0.2.0 von kefplayer.

Aufruf (aus dem Verzeichnis dieses Skripts oder von ueberall):

    python -B make_expected.py <Checkout von 0.2.0, Tag v0.2.0 / Commit 8788254> [<Ausgabedatei>]

Das Skript importiert kefplayer aus dem angegebenen Checkout und die Fall-Beschreibung (compat_020_cases.py)
aus dem Testordner. Es bricht ab, wenn der Checkout nicht Version 0.2.0 meldet. Es veraendert den Checkout nicht
(mit "python -B" werden keine .pyc-Dateien dort abgelegt).

Die gespeicherten Werte sind das Verhalten von 0.2.0. Sie duerfen nicht aus einem neueren Stand erzeugt werden,
sonst pruefen die Tests nur noch den Code gegen sich selbst.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent.parent


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "expected.json"
    sys.path.insert(0, str(TESTS))
    sys.path.insert(0, str(repo))
    import kefplayer

    if kefplayer.__version__ != "0.2.0" or Path(kefplayer.__file__).resolve().parent.parent != repo:
        print(f"Abbruch: erwartet kefplayer 0.2.0 aus {repo}, gefunden {kefplayer.__version__} in {kefplayer.__file__}")
        return 1
    import compat_020_cases as cases

    try:
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    data = cases.build_expected()
    for key, err in data["errors"].items():
        if err["type"] is None:
            print(f"Abbruch: Fehlerfall {key!r} loest in 0.2.0 keinen Fehler aus")
            return 1
    data = {
        "_meta": {
            "erzeugt_mit": "kefplayer " + kefplayer.__version__,
            "quell_commit": commit,
            "quell_arbeitsverzeichnis_veraendert": dirty,
            "hinweis": "Verhalten von 0.2.0, nicht aus dem aktuellen Stand erzeugen. Siehe make_expected.py.",
        },
        **data,
    }
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"geschrieben: {out} ({len(data['profiles'])} Profile, {len(data['errors'])} Fehlerfaelle), Quelle {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
