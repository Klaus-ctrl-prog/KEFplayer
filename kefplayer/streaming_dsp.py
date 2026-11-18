"""Blockweise (zustandsbehaftete) Anwendung eines Profils fuer Live-Streams (Phase E1b, Schritt 1).

Reiner DSP-Code ohne ffmpeg und ohne Netz. Gegenstueck zu ``dsp.apply_profile`` (ganzes Stueck):
Filterzustand (``zi``) und Delay-Leitungen bleiben ueber Bloecke erhalten, die Blockgrenzen sind
hoerbar/messbar neutral (Aequivalenztest: blockweise == ganzes Stueck, bis auf Rundung).

Unterschiede zum Dateipfad (bewusst):
- Kein Stueck-Clipping-Schutz (``ceiling``), da das Ende eines Streams unbekannt ist. Stattdessen ein
  FESTER Preamp (manuell oder automatisch aus dem Filter-Betragsgang). Spitzen darueber werden vom
  spaeteren WAV-Packer gezaehlt (clip counter), nicht still skaliert.
- Der Delay verschiebt den Stream; der Nachlauf am Ende (maxd Samples) entfaellt.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from .dsp import Profile, db_to_lin


class StreamingProfile:
    def __init__(self, profile: Profile, fs: float, n_channels: int):
        if n_channels < 1:
            raise ValueError("n_channels muss >= 1 sein")
        if fs <= 0:
            raise ValueError("fs muss > 0 sein")
        self.profile = profile
        self.fs = float(fs)
        self.n_channels = int(n_channels)
        self.neutral = profile.is_neutral()
        self.preamp_db = 0.0 if self.neutral else profile.resolve_preamp_db(n_channels, fs)
        self.warnings: list[str] = []
        if not self.neutral and profile.preamp_db is not None:
            needed = profile.auto_preamp_db(n_channels, fs)
            if self.preamp_db > needed + 1e-9:
                self.warnings.append(
                    f"Manueller Preamp {self.preamp_db:+.2f} dB liegt ueber dem noetigen Headroom {needed:+.2f} dB: Clipping moeglich"
                )
        for idx in profile.channels:
            if idx >= n_channels:
                self.warnings.append(f"Profil enthaelt Kanal {idx}, Stream hat nur {n_channels} Kanal/Kanaele: ignoriert")
        g = db_to_lin(self.preamp_db)
        self._gain = [g * db_to_lin(profile.channel_gain_db(ch)) for ch in range(n_channels)]
        self.delay_samples = [int(round(profile.channel_delay_ms(ch) * fs / 1000.0)) for ch in range(n_channels)]
        self._sos = []
        self._zi = []
        for ch in range(n_channels):
            fl = profile.channel_filters(ch)
            if fl:
                sos = np.vstack([f.sos(fs) for f in fl])
                self._sos.append(sos)
                self._zi.append(signal.sosfilt_zi(sos) * 0.0)
            else:
                self._sos.append(None)
                self._zi.append(None)
        self._delay = [np.zeros(d, dtype=np.float64) for d in self.delay_samples]

    def reset(self) -> None:
        """Zustand loeschen (z. B. nach laengerer Luecke/Reconnect, damit keine alten Samples nachklingen)."""
        for ch in range(self.n_channels):
            if self._zi[ch] is not None:
                self._zi[ch][:] = 0.0
            self._delay[ch][:] = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        """block: (frames, channels) -> gleiche Form, float64. Eingabe wird nicht veraendert."""
        if block.ndim != 2 or block.shape[1] != self.n_channels:
            raise ValueError(f"block muss die Form (frames, {self.n_channels}) haben")
        if self.neutral:
            return block.astype(np.float64, copy=True)
        n = block.shape[0]
        out = np.empty((n, self.n_channels), dtype=np.float64)
        for ch in range(self.n_channels):
            x = block[:, ch].astype(np.float64, copy=False)
            if self._sos[ch] is not None:
                x, self._zi[ch] = signal.sosfilt(self._sos[ch], x, zi=self._zi[ch])
            y = x * self._gain[ch]
            d = self.delay_samples[ch]
            if d:
                cat = np.concatenate([self._delay[ch], y])
                out[:, ch] = cat[:n]
                self._delay[ch] = cat[n:].copy()
            else:
                out[:, ch] = y
        return out
