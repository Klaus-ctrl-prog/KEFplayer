"""T10: Randfaelle und Fehlerbehandlung beim Verarbeiten von Audiodateien.

Beschreibt das aktuelle Verhalten (Stand 0.2.0 plus APO-Korrektur und Fehlerbehandlung von soundfile-Fehlern im Befehl
"process"). Wo das Verhalten eine moegliche Schwaeche ist (sehr kurze oder leere Dateien), steht das ausdruecklich
im Test: Der Test haelt den Ist-Zustand fest, er bewertet ihn nicht als gewuenscht.

Abgedeckt:
  * Dateiformate und Bittiefen (PCM 8/16/24/32, FLOAT, DOUBLE, FLAC, WAV, AIFF, OGG, MP3; 44,1 bis 192 kHz):
    gefilterter Kanal -6 dB, ungefilterter Kanal unveraendert, Abtastrate und Laenge bleiben.
  * 1, 3, 6 und 8 Kanaele: Profil-Index 0 = L, 1 = R, alle weiteren Kanaele nur gemeinsame Filter.
  * Ausgabeformat: Bittiefe der Ausgabe, FLOAT ohne Clipping-Schutz, 32 Bit, neutral bitgenau.
  * Sehr kurze Dateien (1 Sample bis kuerzer als das Einschwingen des Filters) und leere Dateien.
  * Unlesbare Dateien (fehlend, leer, Text, Verzeichnis, abgeschnitten) beim Dateiweg, beim Befehl "process" und
    in der Warteschlange.
  * Filter oberhalb der Nyquistfrequenz der Datei.
"""
import json

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

from dsp_reference import chain_sos
from kefplayer import audio, cli, dsp
from kefplayer.dsp import ChannelSpec, Filter, Profile

LSB24 = 2.0 ** -23


def write_tones(path, fs, channels, seconds=0.6, subtype="PCM_24", fmt="WAV", amp=0.2):
    """Gleicher 1-kHz-Sinus auf allen Kanaelen (Phase je Kanal verschieden). Rueckgabe: Pfad."""
    t = np.arange(int(seconds * fs)) / fs
    data = np.stack([amp * np.sin(2 * np.pi * 1000 * t + 0.3 * ch) for ch in range(channels)], axis=1)
    sf.write(str(path), data, fs, subtype=subtype, format=fmt)
    return path


def level_db(y, x, fs):
    """Pegel von y gegenueber x in dB ohne die ersten 0,25 s (Einschwingen)."""
    k = fs // 4
    return float(20 * np.log10(np.sqrt(np.mean(y[k:] ** 2)) / np.sqrt(np.mean(x[k:] ** 2))))


def available(fmt, subtype):
    return sf.check_format(fmt, subtype)


# ---- Formate und Abtastraten --------------------------------------------------------------------------

FORMATS = [
    ("WAV", "PCM_U8", 44100), ("WAV", "PCM_16", 96000), ("WAV", "PCM_24", 48000), ("WAV", "PCM_32", 44100),
    ("WAV", "FLOAT", 48000), ("WAV", "DOUBLE", 44100), ("FLAC", "PCM_16", 44100), ("FLAC", "PCM_24", 192000),
    ("AIFF", "PCM_16", 44100), ("AIFF", "PCM_24", 44100), ("OGG", "VORBIS", 44100), ("MP3", "MPEG_LAYER_III", 44100),
]


@pytest.mark.parametrize("fmt,subtype,fs", FORMATS, ids=[f"{f}-{s}-{r}" for f, s, r in FORMATS])
def test_format_matrix_filtered_channel_minus_6_db_other_channel_unchanged(fmt, subtype, fs, tmp_path):
    if not available(fmt, subtype):
        pytest.skip(f"libsndfile dieser Installation kann {fmt}/{subtype} nicht schreiben")
    src = write_tones(tmp_path / f"in.{fmt.lower()}", fs, 2, subtype=subtype, fmt=fmt)
    ref, rfs, info = audio.read_audio(src)
    prof = Profile(channels={0: ChannelSpec(filters=[Filter("peq", 1000, -6.0, 2.0)])})
    rep = audio.process_file(src, tmp_path / "out", prof)
    out, ofs = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert ofs == fs == rfs
    assert out.shape == ref.shape
    assert level_db(out[:, 0], ref[:, 0], fs) == pytest.approx(-6.0, abs=0.1)
    assert level_db(out[:, 1], ref[:, 1], fs) == pytest.approx(0.0, abs=0.05)
    # Ausgabe: FLAC (Standard) mit 16 Bit fuer Quellen bis 16 Bit, sonst 24 Bit. Ohne bekannte Bittiefe (OGG, MP3): 24 Bit.
    want_bits = 24 if info.bits is None else (16 if info.bits <= 16 else 24)
    assert rep["out"]["format"] == "FLAC" and rep["out"]["bits"] == want_bits


# ---- Ausgabeformat ------------------------------------------------------------------------------------

PEQ_CUT = Profile(filters=[Filter("peq", 1000, -3.0, 2.0)])
# (Quelle: Format, Subtyp), Ausgabeformat, bits, erwarteter Ausgabe-Subtyp laut libsndfile (nicht laut kefplayer)
OUTPUT_CASES = [
    (("WAV", "PCM_U8"), "WAV", None, "PCM_16"), (("WAV", "PCM_16"), "WAV", None, "PCM_16"),
    (("WAV", "PCM_24"), "WAV", None, "PCM_24"), (("WAV", "PCM_32"), "WAV", None, "PCM_32"),
    (("WAV", "FLOAT"), "WAV", None, "FLOAT"), (("WAV", "DOUBLE"), "WAV", None, "FLOAT"),
    (("FLAC", "PCM_24"), "WAV", None, "PCM_24"), (("WAV", "PCM_24"), "WAV", 32, "PCM_32"),
    (("WAV", "PCM_24"), "WAV", 16, "PCM_16"), (("WAV", "FLOAT"), "WAV", 24, "PCM_24"),
    (("WAV", "PCM_24"), "FLAC", 16, "PCM_16"), (("WAV", "PCM_32"), "FLAC", None, "PCM_24"),
    (("WAV", "FLOAT"), "FLAC", None, "PCM_24"),
]


@pytest.mark.parametrize("source,out_fmt,bits,want", OUTPUT_CASES, ids=[f"{s[1]}-{o}-bits{b}" for s, o, b, _ in OUTPUT_CASES])
def test_written_file_has_the_expected_subtype(source, out_fmt, bits, want, tmp_path):
    src = write_tones(tmp_path / f"in.{source[0].lower()}", 48000, 2, subtype=source[1], fmt=source[0])
    rep = audio.process_file(src, tmp_path / "out", PEQ_CUT, fmt=out_fmt, bits=bits)
    on_disk = sf.info(rep["dst"])  # unabhaengig von probe() aus kefplayer
    assert (on_disk.format, on_disk.subtype) == (out_fmt, want)
    assert (rep["out"]["format"], rep["out"]["subtype"]) == (out_fmt, want)


@pytest.mark.parametrize("fmt,bits", [("FLAC", 32), ("WAV", 20), ("FLAC", 8)])
def test_unsupported_bit_depth_is_rejected_before_anything_is_written(fmt, bits, tmp_path):
    src = write_tones(tmp_path / "in.wav", 48000, 2)
    with pytest.raises(ValueError, match="bits muss 16, 24 oder 32 sein|hoechstens 24 Bit"):
        audio.process_file(src, tmp_path / "out", PEQ_CUT, fmt=fmt, bits=bits)
    assert not list((tmp_path / "out").glob("*"))


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_float_output_is_not_clipped_or_scaled_down_but_pcm_is(subtype, tmp_path):
    """Gleiche Quelle und gleicher Pegelsprung (+6 dB bei 0,9): FLOAT behaelt Werte ueber 1,0 (kein Clipping-Schutz),
    PCM wird auf die Vollaussteuerung begrenzt und meldet den Schutz."""
    src = write_tones(tmp_path / "in.wav", 48000, 2, subtype=subtype, amp=0.9)
    prof = Profile(preamp_db=6.0)  # bewusst zu wenig Headroom
    rep_f = audio.process_file(src, tmp_path / "f", prof, fmt="WAV")
    out_f, _ = sf.read(rep_f["dst"], dtype="float64", always_2d=True)
    assert rep_f["out"]["subtype"] == "FLOAT" and rep_f["guard_gain_db"] == 0.0
    assert float(np.max(np.abs(out_f))) == pytest.approx(0.9 * 10 ** (6 / 20), rel=1e-6)
    assert float(np.max(np.abs(out_f))) > 1.0
    rep_p = audio.process_file(src, tmp_path / "p", prof, fmt="WAV", bits=24)
    out_p, _ = sf.read(rep_p["dst"], dtype="float64", always_2d=True)
    assert float(np.max(np.abs(out_p))) <= 1.0 - 2.0 ** -23 + 1e-12
    assert rep_p["guard_gain_db"] < -5.0 and any("Clipping-Schutz" in w for w in rep_p["warnings"])


def test_32_bit_output_keeps_the_filtered_signal_to_32_bit_resolution(tmp_path):
    src = write_tones(tmp_path / "in.wav", 48000, 2, subtype="PCM_24", amp=0.3)
    rep = audio.process_file(src, tmp_path / "out", PEQ_CUT, fmt="WAV", bits=32)
    out, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    x, _ = sf.read(str(src), dtype="float64", always_2d=True)
    sos = chain_sos([{"type": "peq", "fc": 1000.0, "gain_db": -3.0, "q": 2.0}], 48000)
    assert rep["preamp_db"] == 0.0
    # 24-Bit-Quelle, nur 1 Filter: Abweichung zur unabhaengig gerechneten Kette liegt bei Rechenrauschen, weit unter 24 Bit
    assert float(np.max(np.abs(out - signal.sosfilt(sos, x, axis=0)))) < 1e-9


@pytest.mark.parametrize("src_subtype,bits", [("PCM_24", 32), ("PCM_16", 24), ("PCM_16", 32)])
def test_neutral_widening_to_a_deeper_pcm_is_bit_exact_in_value(src_subtype, bits, tmp_path):
    """Neutral heisst: keine Veraenderung der Werte. Eine breitere Ausgabe haengt nur Nullbits an."""
    src = write_tones(tmp_path / "in.wav", 48000, 2, subtype=src_subtype)
    rep = audio.process_file(src, tmp_path / "out", Profile(), fmt="WAV", bits=bits)
    a, _ = sf.read(str(src), dtype="float64", always_2d=True)
    b, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert rep["neutral"] and not rep["dither"] and rep["out"]["subtype"] == f"PCM_{bits}"
    assert np.array_equal(a, b)


# ---- Kanalzahl --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["WAV", "FLAC"])
@pytest.mark.parametrize("nch", [1, 3, 6, 8])
def test_channel_counts_index_0_is_L_1_is_R_others_get_only_common_filters(nch, fmt, tmp_path):
    fs = 48000
    src = write_tones(tmp_path / f"in.{fmt.lower()}", fs, nch, fmt=fmt)
    ref, _, _ = audio.read_audio(src)
    prof = Profile(
        filters=[Filter("peq", 1000, -3.0, 2.0)],
        channels={0: ChannelSpec(gain_db=-2.0), 1: ChannelSpec(filters=[Filter("peq", 1000, -3.0, 2.0)])},
    )
    rep = audio.process_file(src, tmp_path / "out", prof, fmt=fmt)
    out, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert out.shape == ref.shape and rep["out"]["channels"] == nch
    want = {0: -5.0, 1: -6.0}  # gemeinsamer Filter -3 dB, dazu L -2 dB bzw. R -3 dB (zweiter Filter gleicher Art)
    for ch in range(nch):
        assert level_db(out[:, ch], ref[:, ch], fs) == pytest.approx(want.get(ch, -3.0), abs=0.05), f"Kanal {ch}"
    assert rep["preamp_db"] == 0.0
    ignored = [w for w in rep["warnings"] if "Kanal 1" in w and "ignoriert" in w]
    assert bool(ignored) == (nch == 1)  # nur bei Mono fehlt der Kanal, den das Profil nennt


def test_six_channel_delay_pads_every_channel_to_the_common_length(tmp_path):
    fs = 48000
    src = write_tones(tmp_path / "in.wav", fs, 6, seconds=0.2)
    ref, _, _ = audio.read_audio(src)
    prof = Profile(channels={1: ChannelSpec(delay_ms=2.0)})  # 96 Samples nur auf Kanal 1
    rep = audio.process_file(src, tmp_path / "out", prof, fmt="WAV")
    out, _ = sf.read(rep["dst"], dtype="int32", always_2d=True)
    exp, _ = sf.read(str(src), dtype="int32", always_2d=True)
    assert rep["delay_samples"] == [0, 96, 0, 0, 0, 0]
    assert out.shape == (ref.shape[0] + 96, 6)
    for ch in (0, 2, 3, 4, 5):  # unverzoegerte Kanaele: Inhalt unveraendert am Anfang, danach Stille
        assert np.array_equal(out[:ref.shape[0], ch], exp[:, ch])
        assert not out[ref.shape[0]:, ch].any()
    assert not out[:96, 1].any() and np.array_equal(out[96:, 1], exp[:, 1])


# ---- sehr kurze und leere Dateien ---------------------------------------------------------------------

SHORT_PROFILE = Profile(filters=[Filter("highpass", 20.0, 0.0, 0.7071), Filter("peq", 2000, -3.0, 1.0)])


@pytest.mark.parametrize("fmt", ["WAV", "FLAC"])
@pytest.mark.parametrize("frames", [1, 2, 10, 100, 1000])
def test_very_short_files_keep_their_length_and_stay_finite(frames, fmt, tmp_path):
    """Kuerzer als das Einschwingen des 20-Hz-Hochpasses (rund 2400 Samples bei 48 kHz)."""
    fs = 48000
    data = np.stack([0.3 * np.sin(2 * np.pi * 1000 * np.arange(frames) / fs + c) for c in (0.1, 0.9)], axis=1)
    src = tmp_path / f"in.{fmt.lower()}"
    sf.write(str(src), data, fs, subtype="PCM_24", format=fmt)
    rep = audio.process_file(src, tmp_path / "out", SHORT_PROFILE, fmt=fmt, dither=False)
    out, _ = sf.read(rep["dst"], dtype="float64", always_2d=True)
    assert out.shape == (frames, 2) and np.all(np.isfinite(out))
    # Sollwert unabhaengig: Eingang aus der Datei gelesen, Kette aus dsp_reference, kein Preamp noetig (Hochpass und Cut)
    x, _ = sf.read(str(src), dtype="float64", always_2d=True)
    chain = [{"type": "highpass", "fc": 20.0, "q": 0.7071}, {"type": "peq", "fc": 2000.0, "gain_db": -3.0, "q": 1.0}]
    want = signal.sosfilt(chain_sos(chain, fs), x, axis=0)
    assert rep["preamp_db"] == 0.0 and rep["guard_gain_db"] == 0.0
    assert float(np.max(np.abs(out - want))) <= LSB24  # hoechstens 1 LSB Rundung (24 Bit)


@pytest.mark.parametrize("fmt", ["WAV", "FLAC"])
def test_single_sample_neutral_is_bit_exact(fmt, tmp_path):
    src = tmp_path / f"one.{fmt.lower()}"
    sf.write(str(src), np.array([[0.25, -0.5]]), 44100, subtype="PCM_24", format=fmt)
    rep = audio.process_file(src, tmp_path / "out", Profile(), fmt=fmt)
    a, _ = sf.read(str(src), dtype="int32", always_2d=True)
    b, _ = sf.read(rep["dst"], dtype="int32", always_2d=True)
    assert np.array_equal(a, b)


def test_empty_file_with_filter_is_rejected_by_the_filter_step(tmp_path):
    """IST-ZUSTAND, moegliche Schwaeche: Eine Datei ohne Samples wird mit Filter nicht verarbeitet. Es entsteht ein
    ValueError aus scipy (sosfilt auf leerem Feld), nicht aus kefplayer; es wird nichts geschrieben. Der genaue
    Meldungstext ist Sache von scipy und wird nicht geprueft."""
    src = tmp_path / "empty.wav"
    sf.write(str(src), np.zeros((0, 2)), 48000, subtype="PCM_24", format="WAV")
    with pytest.raises(ValueError):
        audio.process_file(src, tmp_path / "out", SHORT_PROFILE, fmt="WAV")
    assert not list((tmp_path / "out").glob("*"))


def test_empty_file_neutral_flac_cannot_be_read_back_but_wav_works(tmp_path):
    """IST-ZUSTAND, moegliche Schwaeche: Neutral wird eine leere FLAC-Datei geschrieben und beim anschliessenden
    Pruefen (probe) von libsndfile abgelehnt; WAV mit 0 Samples ist lesbar."""
    src = tmp_path / "empty.wav"
    sf.write(str(src), np.zeros((0, 2)), 48000, subtype="PCM_24", format="WAV")
    rep = audio.process_file(src, tmp_path / "wav", Profile(), fmt="WAV")
    assert rep["out"]["frames"] == 0
    with pytest.raises(sf.LibsndfileError):
        audio.process_file(src, tmp_path / "flac", Profile(), fmt="FLAC")


# ---- unlesbare Dateien --------------------------------------------------------------------------------

def make_unreadable(tmp_path):
    good = write_tones(tmp_path / "good.flac", 48000, 2, subtype="PCM_16", fmt="FLAC")
    raw = good.read_bytes()
    cases = {"fehlend": tmp_path / "gibt_es_nicht.flac"}
    empty = tmp_path / "leer.flac"
    empty.write_bytes(b"")
    cases["leer"] = empty
    text = tmp_path / "text.flac"
    text.write_bytes(b"das ist keine Audiodatei" * 20)
    cases["text"] = text
    d = tmp_path / "ordner.flac"
    d.mkdir()
    cases["ordner"] = d
    half = tmp_path / "halb.flac"
    half.write_bytes(raw[: len(raw) // 2])
    cases["halb_abgeschnitten"] = half
    head = tmp_path / "kopf.flac"
    head.write_bytes(raw[:30])
    cases["nur_kopf"] = head
    return cases


UNREADABLE = ["fehlend", "leer", "text", "ordner", "halb_abgeschnitten", "nur_kopf"]


@pytest.mark.parametrize("which", UNREADABLE)
@pytest.mark.parametrize("profile", [Profile(), SHORT_PROFILE], ids=["neutral", "filter"])
def test_unreadable_file_raises_soundfile_error_and_writes_nothing(which, profile, tmp_path):
    bad = make_unreadable(tmp_path)[which]
    out_dir = tmp_path / "out"
    # Ein Verzeichnis meldet libsndfile unter Linux als LibsndfileError (RuntimeError), andere Systeme koennen OSError melden.
    expected = (sf.LibsndfileError, OSError) if which == "ordner" else (sf.LibsndfileError,)
    with pytest.raises(expected) as e:
        audio.process_file(bad, out_dir, profile)
    assert isinstance(e.value, (RuntimeError, OSError)) and str(e.value)
    assert not list(out_dir.glob("*"))


def test_process_command_with_unreadable_file_returns_2(tmp_path, capsys):
    bad = make_unreadable(tmp_path)["text"]
    rc = cli.main(["process", str(bad), "--out", str(tmp_path / "out")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Fehler" in err
    assert "Traceback" not in err

def test_process_command_with_missing_path_returns_2(tmp_path, capsys):
    assert cli.main(["process", str(tmp_path / "gibt_es_nicht.flac"), "--out", str(tmp_path / "out")]) == 2
    assert "Fehler" in capsys.readouterr().err


def test_queue_with_filter_skips_unreadable_file_with_prepare_error(rig, tone_factory, tmp_path):
    bad = make_unreadable(tmp_path)["halb_abgeschnitten"]
    good = tone_factory("g", seconds=0.8)
    mock, server, player = rig(SHORT_PROFILE)
    res = player.play_queue([bad, good])
    assert res[0]["end_reason"] == "prepare_error" and res[0]["error"]
    assert res[1]["end_reason"] == "ended" and res[1]["processed"] is True


def test_queue_neutral_hands_unreadable_file_to_the_renderer_unchanged(rig, tone_factory, tmp_path):
    """Neutral wird das Original ohne Verarbeitung bereitgestellt: auch eine Datei, die libsndfile nicht liest,
    wird dem Renderer unveraendert gereicht (served_format ist dann unbekannt). Der SIMULATOR dekodiert die Bytes
    mit soundfile, scheitert und startet nicht ('never_started' nach start_timeout_s). Wie eine echte KEF reagiert,
    ist offen und nicht geprueft."""
    bad = make_unreadable(tmp_path)["text"]
    mock, server, player = rig(start_timeout_s=1.0)
    res = player.play_queue([bad])
    assert res[0]["processed"] is False and res[0]["served_format"] is None
    assert mock.fetched and mock.fetched[0]["data"] == bad.read_bytes()
    assert res[0]["end_reason"] == "never_started"


# ---- Filter oberhalb der Nyquistfrequenz --------------------------------------------------------------

def test_filter_above_nyquist_of_the_file_fails_the_whole_file_with_a_clear_message(tmp_path):
    src = write_tones(tmp_path / "lo.wav", 22050, 2, seconds=0.2, subtype="PCM_16")
    prof = Profile(channels={0: ChannelSpec(filters=[Filter("peq", 15000, -3.0, 1.0)])})  # nur auf L
    with pytest.raises(ValueError, match=r"fc 15000.* nicht unter Nyquist \(11025"):
        audio.process_file(src, tmp_path / "out", prof)
    # dasselbe Profil funktioniert bei 48 kHz
    src2 = write_tones(tmp_path / "hi.wav", 48000, 2, seconds=0.2, subtype="PCM_16")
    assert audio.process_file(src2, tmp_path / "out2", prof)["out"]["samplerate"] == 48000


def test_queue_skips_a_title_whose_filter_exceeds_nyquist_and_continues(rig, tmp_path):
    lo = write_tones(tmp_path / "lo.flac", 22050, 2, seconds=0.8, subtype="PCM_16", fmt="FLAC")
    hi = write_tones(tmp_path / "hi.flac", 48000, 2, seconds=0.8, subtype="PCM_16", fmt="FLAC")
    mock, server, player = rig(Profile(filters=[Filter("peq", 15000, -3.0, 1.0)]))
    res = player.play_queue([lo, hi])
    assert res[0]["end_reason"] == "prepare_error" and "Nyquist" in res[0]["error"]
    assert res[1]["end_reason"] == "ended"
    log = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(e["event"] == "track_end" for e in log) == 2  # beide Titel sind protokolliert, auch der uebersprungene


def test_apply_profile_rejects_one_dimensional_data():
    with pytest.raises(ValueError, match="frames, channels"):
        dsp.apply_profile(np.zeros(10), 48000, Profile())
