"""Start per `python -m kefplayer` (hilfreich, wenn das Skript `kefplayer` nicht im PATH liegt, z. B. unter Windows)."""

import sys

from .cli import main

sys.exit(main())
