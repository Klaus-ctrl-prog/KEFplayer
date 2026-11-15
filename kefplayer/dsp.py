"""DSP-Kern: Biquad-Filter, Preamp/Headroom, Profile.

Biquad-Formeln nach dem "Audio EQ Cookbook" (RBJ). Peaking, Lowpass und Highpass
folgen dem Cookbook. Ob die Shelf-Filter bei gleichem Q exakt wie in Equalizer APO
oder REW klingen, ist NICHT verifiziert (unterschiedliche Q-Konventionen moeglich).

Das Standardprofil ist neutral (keine Filter, kein Pegeleingriff).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

SQRT_HALF = math.sqrt(0.5)
MAX_GAIN_DB = 24.0
MAX_CHANNEL_GAIN_DB = 12.0
MAX_DELAY_MS = 50.0
BASS_LIMIT_HZ = 150.0

TYPES = ("peq", "lowshelf", "highshelf", "lowpass", "highpass")
_ALIASES = {
    "peq": "peq", "pk": "peq", "peaking": "peq",
    "lowshelf": "lowshelf", "ls": "lowshelf", "lsc": "lowshelf",
    "highshelf": "highshelf", "hs": "highshelf", "hsc": "highshelf",
    "lowpass": "lowpass", "lp": "lowpass", "lpq": "lowpass",
    "highpass": "highpass", "hp": "highpass", "hpq": "highpass",
}
CHANNEL_INDEX = {"L": 0, "R": 1}


def normalize_type(name: str) -> str:
    key = str(name).strip().lower()
    if key not in _ALIASES:
        raise ValueError(
            f"Unbekannter Filtertyp {name!r}. Erlaubt: {', '.join(sorted(_ALIASES))}"
        )
    return _ALIASES[key]


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin_to_db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-12))


@dataclass(frozen=True)
class Filter:
    type: str
    fc: float
    gain_db: float = 0.0
    q: float = SQRT_HALF

    def __post_init__(self):
        object.__setattr__(self, "type", normalize_type(self.type))
        for name in ("fc", "gain_db", "q"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} muss eine endliche Zahl sein")
        if self.fc <= 0:
            raise ValueError(f"fc muss > 0 sein (war {self.fc})")
        if self.q <= 0:
            raise ValueError(f"q muss > 0 sein (war {self.q})")
        if abs(self.gain_db) > MAX_GAIN_DB:
            raise ValueError(f"|gain_db| darf {MAX_GAIN_DB} dB nicht ueberschreiten (war {self.gain_db})")
        if self.type in ("lowpass", "highpass") and self.gain_db != 0:
            raise ValueError("gain_db ist bei lowpass/highpass nicht zulaessig")

    def sos(self, fs: float) -> np.ndarray:
        """Ein Biquad-Abschnitt als SOS-Zeile [b0 b1 b2 1 a1 a2]."""
        if self.fc >= fs / 2:
            raise ValueError(f"fc {self.fc} Hz liegt bei fs={fs} Hz nicht unter Nyquist ({fs / 2} Hz)")
        w0 = 2.0 * math.pi * self.fc / fs
        cosw, sinw = math.cos(w0), math.sin(w0)
        alpha = sinw / (2.0 * self.q)
        A = 10.0 ** (self.gain_db / 40.0)
        t = self.type
        if t == "peq":
            b0, b1, b2 = 1 + alpha * A, -2 * cosw, 1 - alpha * A
            a0, a1, a2 = 1 + alpha / A, -2 * cosw, 1 - alpha / A
        elif t == "lowpass":
            b0, b1, b2 = (1 - cosw) / 2, 1 - cosw, (1 - cosw) / 2
            a0, a1, a2 = 1 + alpha, -2 * cosw, 1 - alpha
        elif t == "highpass":
            b0, b1, b2 = (1 + cosw) / 2, -(1 + cosw), (1 + cosw) / 2
            a0, a1, a2 = 1 + alpha, -2 * cosw, 1 - alpha
        elif t == "lowshelf":
            s = 2.0 * math.sqrt(A) * alpha
            b0 = A * ((A + 1) - (A - 1) * cosw + s)
            b1 = 2 * A * ((A - 1) - (A + 1) * cosw)
            b2 = A * ((A + 1) - (A - 1) * cosw - s)
            a0 = (A + 1) + (A - 1) * cosw + s
            a1 = -2 * ((A - 1) + (A + 1) * cosw)
            a2 = (A + 1) + (A - 1) * cosw - s
        else:  # highshelf
            s = 2.0 * math.sqrt(A) * alpha
            b0 = A * ((A + 1) + (A - 1) * cosw + s)
            b1 = -2 * A * ((A - 1) + (A + 1) * cosw)
            b2 = A * ((A + 1) + (A - 1) * cosw - s)
            a0 = (A + 1) - (A - 1) * cosw + s
            a1 = 2 * ((A - 1) - (A + 1) * cosw)
            a2 = (A + 1) - (A - 1) * cosw - s
        return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])

    def to_dict(self) -> dict:
        d = {"type": self.type, "fc": self.fc}
        if self.type not in ("lowpass", "highpass"):
            d["gain_db"] = self.gain_db
        d["q"] = self.q
        return d


@dataclass
class ChannelSpec:
    gain_db: float = 0.0
    delay_ms: float = 0.0
    filters: list[Filter] = field(default_factory=list)

    def __post_init__(self):
        if abs(self.gain_db) > MAX_CHANNEL_GAIN_DB:
            raise ValueError(f"|gain_db| je Kanal max. {MAX_CHANNEL_GAIN_DB} dB (war {self.gain_db})")
        if not 0 <= self.delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"delay_ms muss in 0..{MAX_DELAY_MS} liegen (war {self.delay_ms})")

    def is_neutral(self) -> bool:
        return not self.filters and self.gain_db == 0 and self.delay_ms == 0


@dataclass
class Profile:
    name: str = "neutral"
    preamp_db: float | None = None  # None = automatisch (Headroom gegen Clipping)
    filters: list[Filter] = field(default_factory=list)  # gelten fuer alle Kanaele
    channels: dict[int, ChannelSpec] = field(default_factory=dict)  # 0 = L, 1 = R
    notes: list[str] = field(default_factory=list)

    # ---- Aufbau ---------------------------------------------------------
    def is_neutral(self) -> bool:
        return (
            not self.filters
            and all(c.is_neutral() for c in self.channels.values())
            and self.preamp_db in (None, 0, 0.0)
        )

    def channel_filters(self, ch: int) -> list[Filter]:
        spec = self.channels.get(ch)
        return list(self.filters) + (list(spec.filters) if spec else [])

    def channel_gain_db(self, ch: int) -> float:
        spec = self.channels.get(ch)
        return spec.gain_db if spec else 0.0

    def channel_delay_ms(self, ch: int) -> float:
        spec = self.channels.get(ch)
        return spec.delay_ms if spec else 0.0

    # ---- Frequenzgang, Headroom ------------------------------------------
    def response_db(self, ch: int, fs: float, freqs) -> np.ndarray:
        """Betragsgang des Kanals in dB (ohne Preamp, inkl. Kanalpegel)."""
        freqs = np.asarray(freqs, dtype=float)
        fl = self.channel_filters(ch)
        if fl:
            sos = np.vstack([f.sos(fs) for f in fl])
            _, h = signal.sosfreqz(sos, worN=freqs, fs=fs)
            mag = 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))
        else:
            mag = np.zeros_like(freqs)
        return mag + self.channel_gain_db(ch)

    def peak_gain_db(self, ch: int, fs: float) -> float:
        top = fs / 2 * 0.9999
        grid = np.geomspace(5.0, top, 8000)
        extra = [f.fc for f in self.channel_filters(ch) if 0 < f.fc < top]
        if extra:
            grid = np.union1d(grid, np.array(extra))
        return float(np.max(self.response_db(ch, fs, grid)))

    def auto_preamp_db(self, n_channels: int, fs: float) -> float:
        """Preamp, der den hoechsten Betragsgang aller Kanaele auf <= 0 dB bringt."""
        peak = max(self.peak_gain_db(ch, fs) for ch in range(n_channels))
        if peak <= 0:
            return 0.0
        return -math.ceil(peak * 100.0) / 100.0

    def resolve_preamp_db(self, n_channels: int, fs: float) -> float:
        if self.preamp_db is not None:
            return float(self.preamp_db)
        return self.auto_preamp_db(n_channels, fs)

    # ---- Hinweise nach Projektregeln ------------------------------------
    def validate(self) -> list[str]:
        """Hinweise zu den EQ-Regeln des Projekts. Keine Blockade, nur Warnungen."""
        out: list[str] = []
        all_filters = list(self.filters)
        for spec in self.channels.values():
            all_filters += spec.filters
        for f in all_filters:
            active = f.type in ("lowpass", "highpass") or f.gain_db != 0
            if f.fc < BASS_LIMIT_HZ and active:
                out.append(
                    f"Filter {f.type} bei {f.fc:g} Hz liegt im Bassbereich (< {BASS_LIMIT_HZ:g} Hz): "
                    "wirkt auf das gesamte Signal, das die KEF erhält, und kann damit auch ein Subwoofer-Signal der KEF "
                    "beeinflussen (an echter Hardware nicht geprüft). "
                    "Doppelkorrektur mit EQ-Einstellungen von Subwoofer oder KEF vermeiden."
                )
            elif f.fc >= BASS_LIMIT_HZ and f.gain_db != 0 and f.type in ("peq", "lowshelf", "highshelf"):
                out.append(
                    f"Filter {f.type} bei {f.fc:g} Hz ({f.gain_db:+g} dB): oberhalb ca. {BASS_LIMIT_HZ:g} Hz nur "
                    "auf Basis ueber mehrere Positionen gemittelter Messungen, Kammfilter nicht wegfiltern."
                )
            if f.gain_db > 0 and f.type in ("peq", "lowshelf", "highshelf"):
                out.append(
                    f"Boost {f.gain_db:+g} dB bei {f.fc:g} Hz: vorher pruefen, dass dort kein Einbruch durch "
                    "Ausloeschung (SBIR, Raummoden-Knoten, Interferenz) liegt. Solche Einbrueche lassen sich nicht wegentzerren."
                )
        if any(not c.is_neutral() for c in self.channels.values()):
            out.append(
                "Kanalunterschiede (Pegel, Delay, Filter je Seite) wirken nur auf diesen Signalweg "
                "(PC -> DLNA). TV (optisch), Bluetooth und andere KEF-Quellen bleiben unveraendert."
            )
        if self.preamp_db is not None and self.preamp_db > 0:
            out.append(f"Preamp {self.preamp_db:+g} dB ist positiv: Clipping-Gefahr.")
        return out

    # ---- Serialisierung -----------------------------------------------------
    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "preamp_db": "auto" if self.preamp_db is None else self.preamp_db,
            "filters": [f.to_dict() for f in self.filters],
        }
        chans = {}
        for idx, spec in sorted(self.channels.items()):
            key = {0: "L", 1: "R"}.get(idx, str(idx))
            chans[key] = {
                "gain_db": spec.gain_db,
                "delay_ms": spec.delay_ms,
                "filters": [f.to_dict() for f in spec.filters],
            }
        if chans:
            d["channels"] = chans
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        allowed = {"name", "description", "preamp_db", "filters", "channels"}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"Unbekannte Profilfelder: {sorted(unknown)}")
        pre = d.get("preamp_db", "auto")
        if isinstance(pre, str):
            if pre.strip().lower() != "auto":
                raise ValueError(f"preamp_db muss Zahl oder 'auto' sein (war {pre!r})")
            pre = None
        elif pre is not None:
            pre = float(pre)
        channels: dict[int, ChannelSpec] = {}
        for key, spec in (d.get("channels") or {}).items():
            k = str(key).strip().upper()
            if k not in CHANNEL_INDEX:
                raise ValueError(f"Unbekannter Kanal {key!r} (erlaubt: L, R)")
            bad = set(spec) - {"gain_db", "delay_ms", "filters"}
            if bad:
                raise ValueError(f"Unbekannte Felder in Kanal {key}: {sorted(bad)}")
            channels[CHANNEL_INDEX[k]] = ChannelSpec(
                gain_db=float(spec.get("gain_db", 0.0)),
                delay_ms=float(spec.get("delay_ms", 0.0)),
                filters=[_filter_from_dict(x) for x in spec.get("filters", [])],
            )
        return cls(
            name=str(d.get("name", "profile")),
            preamp_db=pre,
            filters=[_filter_from_dict(x) for x in d.get("filters", [])],
            channels=channels,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        p = Path(path)
        text = p.read_text(encoding="utf-8-sig")
        if p.suffix.lower() == ".json":
            d = json.loads(text)
            d.setdefault("name", p.stem)
            return cls.from_dict(d)
        return parse_apo(text, name=p.stem)


def _filter_from_dict(x: dict) -> Filter:
    bad = set(x) - {"type", "fc", "gain_db", "q"}
    if bad:
        raise ValueError(f"Unbekannte Filterfelder: {sorted(bad)}")
    if "type" not in x or "fc" not in x:
        raise ValueError(f"Filter braucht mindestens 'type' und 'fc': {x}")
    kwargs = {"type": x["type"], "fc": float(x["fc"])}
    if "gain_db" in x:
        kwargs["gain_db"] = float(x["gain_db"])
    if "q" in x:
        kwargs["q"] = float(x["q"])
    return Filter(**kwargs)


# ---- Equalizer-APO-Textformat (Import) ---------------------------------------

_FILTER_LINE = re.compile(r"^filter\s*\d*\s*:\s*(on|off)\s+(\w+)\s*(.*)$", re.I)


def _num(text: str, lineno: int) -> float:
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", text)
    if not m:
        raise ValueError(f"Zeile {lineno}: Zahl erwartet in {text!r}")
    return float(m.group(0).replace(",", "."))


def _param(rest: str, key: str, lineno: int) -> float | None:
    m = re.search(rf"\b{key}\s+([-+]?\d+(?:[.,]\d+)?)", rest, re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_apo(text: str, name: str = "apo") -> Profile:
    """Liest Preamp/Filter/Channel/Delay im Equalizer-APO-Textformat (REW-Export eingeschlossen).

    Alles andere (Include, Convolution, Copy, ...) fuehrt zu einem Fehler, damit nichts still ignoriert wird.
    """
    prof = Profile(name=name)
    scope: tuple[int, ...] | None = None  # None = alle Kanaele
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("preamp:"):
            prof.preamp_db = _num(line.split(":", 1)[1], lineno)
        elif low.startswith("channel:"):
            tokens = [t.upper() for t in line.split(":", 1)[1].split()]
            if not tokens or "ALL" in tokens:
                scope = None
            else:
                for t in tokens:
                    if t not in CHANNEL_INDEX:
                        raise ValueError(f"Zeile {lineno}: Kanal {t!r} nicht unterstuetzt (nur L, R, ALL)")
                idx = tuple(sorted({CHANNEL_INDEX[t] for t in tokens}))
                scope = None if idx == (0, 1) else idx
        elif low.startswith("delay:"):
            rest = line.split(":", 1)[1]
            if "sample" in rest.lower():
                raise ValueError(f"Zeile {lineno}: Delay in Samples nicht unterstuetzt, bitte in ms angeben")
            ms = _num(rest, lineno)
            for idx in scope if scope is not None else (0, 1):
                prof.channels.setdefault(idx, ChannelSpec()).delay_ms = ms
        elif low.startswith("filter"):
            m = _FILTER_LINE.match(line)
            if not m:
                raise ValueError(f"Zeile {lineno}: Filterzeile nicht lesbar: {raw.strip()!r}")
            state, ftype, rest = m.groups()
            if state.lower() == "off":
                continue
            if re.search(r"\bbw\b", rest, re.I):
                raise ValueError(f"Zeile {lineno}: 'BW Oct' nicht unterstuetzt, bitte Q angeben")
            fc = _param(rest, "fc", lineno)
            if fc is None:
                raise ValueError(f"Zeile {lineno}: Fc fehlt")
            gain = _param(rest, "gain", lineno)
            q = _param(rest, "q", lineno)
            t = normalize_type(ftype)
            if t == "peq" and q is None:
                raise ValueError(f"Zeile {lineno}: Q fehlt bei PK")
            if t in ("lowshelf", "highshelf"):
                # Equalizer APO kennt mehrere Shelf-Varianten (LS/HS, LSC/HSC, LS 6dB/12dB) mit unterschiedlicher
                # Fc-Definition (Eck- oder Mittenfrequenz) und Steilheit. Nur die Q-Form wird gelesen.
                stripped = re.sub(r"\bgain\s+[-+]?\d+(?:[.,]\d+)?\s*db\b", "", rest, flags=re.I)
                if re.search(r"\d\s*db\b", stripped, re.I):
                    raise ValueError(
                        f"Zeile {lineno}: Shelf mit dB/Oktave-Angabe ({ftype.upper()} x dB) nicht unterstuetzt, "
                        "bitte die Q-Form (Fc, Gain, Q) verwenden"
                    )
                prof.notes.append(
                    f"Zeile {lineno}: Shelf {ftype.upper()}: Fc- und Q-Definition gegen Equalizer APO/REW nicht "
                    "verifiziert, Frequenzgang vor Nutzung vergleichen"
                )
            if t in ("lowshelf", "highshelf") and q is None:
                prof.notes.append(
                    f"Zeile {lineno}: Q fehlt bei {ftype.upper()}, {SQRT_HALF:.4f} angenommen (unverifiziert)"
                )
            kwargs = {"type": t, "fc": fc}
            if t not in ("lowpass", "highpass"):
                kwargs["gain_db"] = gain if gain is not None else 0.0
            elif gain not in (None, 0.0):
                raise ValueError(f"Zeile {lineno}: Gain bei {ftype.upper()} nicht zulaessig")
            if q is not None:
                kwargs["q"] = q
            f = Filter(**kwargs)
            if scope is None:
                prof.filters.append(f)
            else:
                for idx in scope:
                    prof.channels.setdefault(idx, ChannelSpec()).filters.append(f)
        else:
            raise ValueError(f"Zeile {lineno}: nicht unterstuetzt: {raw.strip()!r}")
    return prof


# ---- Anwenden -----------------------------------------------------------------


def apply_profile(data: np.ndarray, fs: float, profile: Profile, ceiling: float | None = None):
    """Wendet das Profil auf data (frames, channels) an. Rueckgabe: (out, info).

    Preamp wirkt auf alle Kanaele gleich (Balance bleibt). ceiling: Skalierung des ganzen Stuecks,
    falls die Spitze darueber liegt (Clipping-Schutz). Bei neutralem Profil unveraendert.
    """
    if data.ndim != 2:
        raise ValueError("data muss die Form (frames, channels) haben")
    n, nch = data.shape
    info = {
        "preamp_db": 0.0,
        "peak_in": float(np.max(np.abs(data))) if data.size else 0.0,
        "guard_gain_db": 0.0,
        "delay_samples": [0] * nch,
        "warnings": [],
    }
    if profile.is_neutral():
        info["peak_out"] = info["peak_in"]
        return data.copy(), info

    for idx in profile.channels:
        if idx >= nch:
            info["warnings"].append(f"Profil enthaelt Kanal {idx}, Datei hat nur {nch} Kanal/Kanaele: ignoriert")

    preamp = profile.resolve_preamp_db(nch, fs)
    info["preamp_db"] = preamp
    if profile.preamp_db is not None:
        needed = profile.auto_preamp_db(nch, fs)
        if preamp > needed + 1e-9:
            info["warnings"].append(
                f"Manueller Preamp {preamp:+.2f} dB liegt ueber dem noetigen Headroom {needed:+.2f} dB: Clipping moeglich"
            )
    g = db_to_lin(preamp)

    delays = [int(round(profile.channel_delay_ms(ch) * fs / 1000.0)) for ch in range(nch)]
    maxd = max(delays) if delays else 0
    info["delay_samples"] = delays
    out = np.zeros((n + maxd, nch), dtype=np.float64)
    for ch in range(nch):
        x = data[:, ch]
        fl = profile.channel_filters(ch)
        if fl:
            sos = np.vstack([f.sos(fs) for f in fl])
            x = signal.sosfilt(sos, x)
        y = x * (g * db_to_lin(profile.channel_gain_db(ch)))
        out[delays[ch]:delays[ch] + n, ch] = y

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if ceiling is not None and peak > ceiling:
        scale = ceiling / peak
        out *= scale
        info["guard_gain_db"] = lin_to_db(scale)
        info["warnings"].append(
            f"Clipping-Schutz: Stueck um {-info['guard_gain_db']:.2f} dB abgesenkt (Spitze {lin_to_db(peak):+.2f} dBFS)"
        )
        peak = ceiling
    info["peak_out"] = peak
    return out, info
