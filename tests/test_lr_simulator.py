"""T16 (Simulator mit Stereo und Mono) sowie T6c und T12 (Ist-Zustand, nicht veraendert).

T16: Der Player liefert dem simulierten Renderer (tests/mock_renderer.py, ruft die URL per HTTP ab und dekodiert die
Datei) Stereo mit unterschiedlichen L/R-Ketten und Mono mit verschiedenen Profilen. Geprueft werden Kanalzahl,
Inhalt und Laenge der gelieferten Datei sowie das Protokoll, ebenso der gemeinsame Auto-Preamp (richtet sich
nach dem lauteren Kanal, wirkt auf beide). Das ist ein Signal- und Simulatortest: Er sagt nichts ueber das
Verhalten einer echten KEF und nichts ueber Klang.

T6c und T12 beschreiben den AKTUELLEN Stand und bewerten ihn nicht:
  * T6c: Eine Mono-Datei mit einem Profil, das nur Kanal R (Index 1) nennt, gilt nicht als neutral, wird neu kodiert,
    die R-Kette hat keine Wirkung (die Datei hat keinen zweiten Kanal), Warnung 'Kanal 1 ... ignoriert'.
    Bei 16 Bit mit Dither weicht die Ausgabe vom Eingang um hoechstens 1 LSB ab; ohne Dither ist sie bitgleich.
  * T12: Bei 16-Bit-Ausgabe wird auch ein ungefilterter Kanal mit Dither neu quantisiert (hoechstens 1 LSB Abweichung).
    Bei 24 Bit, ohne Dither und bei neutralem Profil bleibt er bitgleich.
Eine Aenderung dieses Verhaltens ist nicht beschlossen (Mono und Dither: keine Aenderung nur deshalb, weil ein Kanal
ungefiltert ist). Die Tests halten den Ist-Zustand fest, damit eine spaetere Aenderung bewusst und sichtbar geschieht.
Der neutrale 16-Bit-Fall (bitgleich, ohne Dither) ist bereits in test_audio.py abgedeckt und hier nicht wiederholt.
"""
import io
import json

import numpy as np
import pytest
import soundfile as sf

from kefplayer import audio
from kefplayer.dsp import ChannelSpec, Filter, Profile

FS = 48000
LSB24 = 2.0 ** -23


def write_source(path, channels=2, subtype="PCM_24", seconds=1.0, fs=FS):
    """Kanal 0: 1 kHz, Kanal 1: 3 kHz (Amplitude 0,2), damit jede Kette an ihrer eigenen Frequenz wirkt."""
    t = np.arange(int(seconds * fs)) / fs
    cols = [0.2 * np.sin(2 * np.pi * 1000 * t), 0.2 * np.sin(2 * np.pi * 3000 * t)][:channels]
    sf.write(str(path), np.stack(cols, axis=1), fs, subtype=subtype, format="FLAC")
    return path


def decode(data, dtype="float64"):
    return sf.read(io.BytesIO(data), dtype=dtype, always_2d=True)


def level_db(y, x):
    k = FS // 4
    return float(20 * np.log10(np.sqrt(np.mean(y[k:] ** 2)) / np.sqrt(np.mean(x[k:] ** 2))))


def read_log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]


L_CUT = Filter("peq", 1000, -6.0, 2.0)
R_CUT = Filter("peq", 3000, -3.0, 2.0)
STEREO_PROFILE = Profile(channels={0: ChannelSpec(filters=[L_CUT]), 1: ChannelSpec(gain_db=-2.0, delay_ms=1.0, filters=[R_CUT])})


# ---- T16 Stereo -----------------------------------------------------------------------------------------

def test_stereo_with_different_lr_chains_arrives_as_specified(rig, tmp_path):
    src = write_source(tmp_path / "lr.flac")
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    n = ref.shape[0]
    mock, server, player = rig(STEREO_PROFILE)
    res = player.play_queue([src])[0]
    assert res["end_reason"] == "ended" and res["processed"] is True
    got, fs = decode(mock.fetched[0]["data"])
    assert fs == FS and got.shape == (n + 48, 2)  # R um 1,0 ms = 48 Samples verzoegert, Datei entsprechend laenger
    assert level_db(got[:n, 0], ref[:, 0]) == pytest.approx(-6.0, abs=0.05)  # L: nur eigener Filter
    assert level_db(got[48:48 + n, 1], ref[:, 1]) == pytest.approx(-5.0, abs=0.05)  # R: -3 dB Filter und -2 dB Pegel
    assert float(np.max(np.abs(got[:48, 1]))) <= LSB24  # Vorlauf von R ist Stille
    assert float(np.max(np.abs(got[n:, 0]))) <= LSB24  # L ist am Ende aufgefuellt
    # Die gelieferte Datei ist sampleidentisch zum Dateiweg mit demselben Profil
    offline = audio.process_file(src, tmp_path / "offline", STEREO_PROFILE)
    off, _ = sf.read(offline["dst"], dtype="int32", always_2d=True)
    assert np.array_equal(decode(mock.fetched[0]["data"], "int32")[0], off)
    # gemeldetes Format und Metadaten am Renderer
    fmt = res["served_format"]
    assert (fmt["channels"], fmt["samplerate"], fmt["bits"], fmt["frames"]) == (2, FS, 24, n + 48)
    md = mock.metadata[0]["res"]
    assert md["nrAudioChannels"] == "2" and md["sampleFrequency"] == str(FS) and md["bitsPerSample"] == "24"


def test_stereo_protocol_reports_preamp_and_channel_hint(rig, tmp_path):
    src = write_source(tmp_path / "lr.flac", seconds=0.8)
    mock, server, player = rig(STEREO_PROFILE)
    player.play_queue([src])
    log = read_log(tmp_path)
    start = next(e for e in log if e["event"] == "queue_start")
    assert start["profile_neutral"] is False
    assert start["profile"]["channels"]["R"]["delay_ms"] == 1.0 and start["profile"]["channels"]["L"]["delay_ms"] == 0.0
    proc = next(e for e in log if e["event"] == "track_processed")
    assert proc["preamp_db"] == 0.0 and proc["guard_gain_db"] == 0.0
    assert any("nur auf diesen Signalweg" in w for w in proc["warnings"])


@pytest.mark.parametrize("side,freq", [(0, 1000), (1, 3000)], ids=["L_lauter", "R_lauter"])
def test_stereo_auto_preamp_is_shared_and_taken_from_the_louder_channel(side, freq, rig, tmp_path):
    """Boost (+6 dB) nur auf einer Seite: Der Auto-Preamp richtet sich nach dieser Seite und gilt fuer BEIDE Kanaele.
    Ein Peaking-Filter hat bei seiner Mittenfrequenz genau seinen Gain. Der Preamp ist deshalb -6,00 dB; weil auf
    0,01 dB aufgerundet wird, ist -6,01 dB wegen Rechenrauschen ebenfalls zulaessig."""
    src = write_source(tmp_path / "lr.flac")
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    prof = Profile(channels={side: ChannelSpec(filters=[Filter("peq", freq, 6.0, 2.0)])})
    mock, server, player = rig(prof)
    assert player.play_queue([src])[0]["end_reason"] == "ended"
    proc = next(e for e in read_log(tmp_path) if e["event"] == "track_processed")
    assert -6.01 - 1e-9 <= proc["preamp_db"] <= -6.0 + 1e-9
    got, _ = decode(mock.fetched[0]["data"])
    other = 1 - side
    assert level_db(got[:, side], ref[:, side]) == pytest.approx(0.0, abs=0.05)  # +6 dB Boost, -6 dB Preamp
    assert level_db(got[:, other], ref[:, other]) == pytest.approx(-6.0, abs=0.05)  # nur der Preamp wirkt


# ---- T16 Mono -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "profile,delay",
    [
        (Profile(filters=[L_CUT]), 0),
        (Profile(channels={0: ChannelSpec(filters=[L_CUT])}), 0),
        (Profile(channels={0: ChannelSpec(delay_ms=1.0, filters=[L_CUT])}), 48),
    ],
    ids=["gemeinsamer_Filter", "L-Kette", "L-Kette_mit_Delay"],
)
def test_mono_uses_common_filters_and_the_L_chain(profile, delay, rig, tmp_path):
    src = write_source(tmp_path / "mono.flac", channels=1)
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    mock, server, player = rig(profile)
    res = player.play_queue([src])[0]
    assert res["end_reason"] == "ended" and res["processed"] is True
    got, _ = decode(mock.fetched[0]["data"])
    assert got.shape == (ref.shape[0] + delay, 1)
    assert level_db(got[delay:delay + ref.shape[0], 0], ref[:, 0]) == pytest.approx(-6.0, abs=0.05)
    assert res["served_format"]["channels"] == 1 and mock.metadata[0]["res"]["nrAudioChannels"] == "1"


def test_mono_with_only_an_R_entry_is_processed_but_unchanged_T6c(rig, tmp_path):
    """T6c, Ist-Zustand: nicht neutral, neu kodiert, Inhalt (24 Bit) bitgleich, Warnung statt Wirkung."""
    src = write_source(tmp_path / "mono.flac", channels=1)
    prof = Profile(channels={1: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)])})
    assert prof.is_neutral() is False
    mock, server, player = rig(prof)
    res = player.play_queue([src])[0]
    assert res["end_reason"] == "ended" and res["processed"] is True and res["served"] != str(src)
    got, _ = decode(mock.fetched[0]["data"], "int32")
    exp, _ = sf.read(str(src), dtype="int32", always_2d=True)
    assert np.array_equal(got, exp)
    proc = next(e for e in read_log(tmp_path) if e["event"] == "track_processed")
    assert any("Kanal 1" in w and "ignoriert" in w for w in proc["warnings"])
    assert proc["preamp_db"] == 0.0


def test_mono_with_different_lr_chains_applies_only_the_L_chain(rig, tmp_path):
    """T6d ohne Upmix, Ist-Zustand: Die R-Kette (Pegel, Delay, Filter) hat bei Mono keine Wirkung, Warnung."""
    src = write_source(tmp_path / "mono.flac", channels=1)
    ref, _ = sf.read(str(src), dtype="float64", always_2d=True)
    prof = Profile(channels={0: ChannelSpec(filters=[L_CUT]), 1: ChannelSpec(gain_db=-3.0, delay_ms=2.0, filters=[R_CUT])})
    mock, server, player = rig(prof)
    player.play_queue([src])
    got, _ = decode(mock.fetched[0]["data"])
    assert got.shape == ref.shape  # kein Delay der R-Kette
    assert level_db(got[:, 0], ref[:, 0]) == pytest.approx(-6.0, abs=0.05)
    proc = next(e for e in read_log(tmp_path) if e["event"] == "track_processed")
    assert any("Kanal 1" in w and "ignoriert" in w for w in proc["warnings"])


# ---- T6c / T12 Ist-Zustand bei 16 Bit und Dither -----------------------------------------------------------

def int16(path):
    return sf.read(str(path), dtype="int16", always_2d=True)[0].astype(np.int64)


def test_mono_16bit_with_only_an_R_entry_changes_samples_by_dither_only_T6c(tmp_path):
    src = write_source(tmp_path / "mono16.flac", channels=1, subtype="PCM_16")
    prof = Profile(channels={1: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)])})
    a = int16(src)
    with_dither = audio.process_file(src, tmp_path / "d", prof)
    without = audio.process_file(src, tmp_path / "n", prof, dither=False)
    assert with_dither["neutral"] is False and with_dither["dither"] is True and without["dither"] is False
    b, c = int16(with_dither["dst"]), int16(without["dst"])
    assert np.array_equal(c, a)  # ohne Dither bitgleich
    assert 0 < int(np.sum(b != a)) and int(np.max(np.abs(b - a))) == 1  # mit Dither einzelne Samples um genau 1 LSB


def test_stereo_16bit_unfiltered_channel_is_dithered_filtered_channel_within_one_lsb_T12(tmp_path):
    src = write_source(tmp_path / "lr16.flac", subtype="PCM_16")
    prof = Profile(channels={0: ChannelSpec(filters=[L_CUT])})  # R ungefiltert
    a = int16(src)
    dithered = audio.process_file(src, tmp_path / "d", prof)
    plain = audio.process_file(src, tmp_path / "n", prof, dither=False)
    d, p = int16(dithered["dst"]), int16(plain["dst"])
    assert np.array_equal(p[:, 1], a[:, 1])  # ohne Dither: R bitgleich
    assert int(np.sum(d[:, 1] != a[:, 1])) > 0 and int(np.max(np.abs(d[:, 1] - a[:, 1]))) == 1  # Ist-Zustand: R mit Dither veraendert
    assert int(np.max(np.abs(d[:, 0] - p[:, 0]))) == 1  # gefilterter Kanal: Dither bewegt hoechstens 1 LSB
    assert int(np.sum(d[:, 0] != p[:, 0])) > 0  # der Dither wirkt auch auf den gefilterten Kanal


def test_stereo_24bit_unfiltered_channel_stays_bit_exact_with_default_dither_T12(tmp_path):
    src = write_source(tmp_path / "lr24.flac", subtype="PCM_24")
    prof = Profile(channels={0: ChannelSpec(filters=[L_CUT])})
    rep = audio.process_file(src, tmp_path / "o", prof)
    assert rep["dither"] is False  # Dither gibt es nur bei 16-Bit-Ausgabe
    a, _ = sf.read(str(src), dtype="int32", always_2d=True)
    b, _ = sf.read(rep["dst"], dtype="int32", always_2d=True)
    assert np.array_equal(a[:, 1], b[:, 1]) and not np.array_equal(a[:, 0], b[:, 0])
