"""Start per ``python -m kefplayer.gui`` (Entwurf).

Optionen nur fuer die Sichtpruefung unter Windows:
  --no-dpi    keinen DPI-Awareness-Aufruf (zum Vergleich der Darstellung)
  --selftest  Fenster kurz oeffnen, Tk-/Bildschirmdaten ausgeben, beenden
"""

from __future__ import annotations

import argparse
import sys


def _set_dpi_awareness() -> str:
    """Unter Windows vor dem ersten Tk-Fenster aufrufen. Wirkung am ASUS noch zu pruefen. Rueckgabe: Beschreibung."""
    if sys.platform != "win32":
        return "nicht Windows: kein Aufruf"
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
            return "shcore.SetProcessDpiAwareness(1)"
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
            return "user32.SetProcessDPIAware()"
    except Exception as e:  # noqa: BLE001 - nie am Start scheitern
        return f"fehlgeschlagen: {type(e).__name__}: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kefplayer.gui", description="KEFplayer GUI (Entwurf)")
    ap.add_argument("--no-dpi", action="store_true", help="keinen DPI-Awareness-Aufruf (nur Windows)")
    ap.add_argument("--selftest", action="store_true", help="Fenster kurz oeffnen, Daten ausgeben, beenden")
    args = ap.parse_args(argv)
    try:
        import tkinter as tk
    except ImportError as e:
        print(f"tkinter nicht verfuegbar ({e}). Bei python.org-Installationen ist es enthalten; "
              "bei Linux Paket python3-tk installieren.", file=sys.stderr)
        return 2
    from .controller import Controller
    from .view_main import MainWindow

    dpi = "uebersprungen (--no-dpi)" if args.no_dpi else _set_dpi_awareness()
    root = tk.Tk()
    win = MainWindow(root, Controller())
    if args.selftest:
        root.update()
        print(f"Python {sys.version.split()[0]} auf {sys.platform}")
        print(f"Tk {root.tk.call('info', 'patchlevel')}; tk scaling {float(root.tk.call('tk', 'scaling')):.3f} "
              f"(96 dpi = 1.333)")
        print(f"DPI-Aufruf: {dpi}")
        print(f"Bildschirm {root.winfo_screenwidth()}x{root.winfo_screenheight()} Pixel; "
              f"Fenster {root.winfo_width()}x{root.winfo_height()} bei +{root.winfo_x()}+{root.winfo_y()}")
        root.after(1500, win.on_close)
    root.mainloop()
    if win.shutdown_ok is False:
        print("Hinweis: Nicht alle Vorgaenge endeten rechtzeitig.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
