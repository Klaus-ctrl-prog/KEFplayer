"""Kommandozeile: discover, probe, response, process, play."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.parse
from pathlib import Path

from . import __version__, audio, dsp, upnp
import soundfile as sf
from .mediaserver import MediaServer, local_ip_for
from .player import Player, collect_files
from .runlog import RunLog

RESPONSE_FREQS = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
                  1000, 2000, 4000, 8000, 16000]


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _parse_addr(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host, int(port)


def load_profile(path: str | None) -> dsp.Profile:
    prof = dsp.Profile.load(path) if path else dsp.Profile()
    for n in prof.notes:
        _err(f"Hinweis (Import): {n}")
    for w in prof.validate():
        _err(f"Hinweis: {w}")
    return prof


def resolve_renderer(args) -> upnp.Renderer:
    spec = args.renderer
    if spec and spec.lower().startswith("http"):
        return upnp.Renderer.from_location(spec)
    addr = _parse_addr(args.ssdp_addr) if args.ssdp_addr else upnp.SSDP_ADDR
    found = upnp.ssdp_search(timeout=args.discover_timeout, addr=addr, bind_ip=args.bind_ip)
    if spec:
        found = [f for f in found if f["_from"] == spec or urllib.parse.urlsplit(f["location"]).hostname == spec]
    if not found:
        sys.exit(
            "Kein Renderer gefunden. Tipps: --renderer http://<IP>:<PORT>/<beschreibung>.xml direkt angeben, "
            "bei mehreren Netzwerkkarten --bind-ip <eigene IP> setzen, Firewall (UDP 1900) pruefen."
        )
    if len(found) > 1:
        lines = "\n".join(f"  {f['_from']}  {f['location']}" for f in found)
        sys.exit(f"Mehrere Renderer gefunden, bitte --renderer <IP oder URL> angeben:\n{lines}")
    return upnp.Renderer.from_location(found[0]["location"])


# ---- Befehle ----------------------------------------------------------------------


def cmd_discover(args) -> int:
    addr = _parse_addr(args.ssdp_addr) if args.ssdp_addr else upnp.SSDP_ADDR
    found = upnp.ssdp_search(timeout=args.timeout, addr=addr, bind_ip=args.bind_ip)
    if not found:
        print("Keine Renderer gefunden.")
        return 1
    for f in found:
        print(f"{f['_from']:<16} {f['location']}  {f.get('server', '')}")
    return 0


def cmd_probe(args) -> int:
    r = resolve_renderer(args)
    i = r.info
    print(f"Name:       {i.friendly_name}\nHersteller: {i.manufacturer}\nModell:     {i.model_name}\nUDN:        {i.udn}\nURL:        {i.location}")
    print("Dienste:")
    for s in i.services.values():
        print(f"  {s.type}")
    try:
        print("Transport:", r.transport_info())
    except (upnp.UpnpError, OSError) as e:
        print(f"Transportstatus nicht lesbar: {e}")
    try:
        infos = r.sink_protocol_infos()
        print(f"Vom Geraet gemeldete Sink-Formate ({len(infos)}), Eigenangabe, kein Wiedergabetest:")
        for s in infos:
            print(f"  {s}")
    except (upnp.UpnpError, OSError) as e:
        print(f"Sink-Formate nicht lesbar: {e}")
    return 0


def cmd_health(args) -> int:
    import json

    from . import health

    try:
        info = resolve_renderer(args).info
    except (OSError, upnp.UpnpError):
        if not (args.renderer and args.renderer.lower().startswith("http")):
            raise
        info = upnp.DeviceInfo(location=args.renderer)  # Geraet nicht erreichbar: genau das soll die Pruefung melden
    checks = health.run_checks(info, None, args.cache_dir, bind=args.bind, port=args.port, timeout=args.timeout)
    status = health.overall(checks)
    if args.json:
        print(json.dumps({"overall": status, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            lat = f" ({c['latency_ms']} ms)" if c["latency_ms"] is not None else ""
            print(f"[{c['status'].upper():7}] {c['component']}: {c['message']}{lat}")
        print(f"Gesamt: {status}")
    return 1 if status == "error" else 0


def cmd_response(args) -> int:
    prof = load_profile(args.profile)
    fs = args.fs
    freqs = [f for f in RESPONSE_FREQS if f < fs / 2]
    chans = sorted(set([0, 1]) if prof.channels else [0])
    labels = {0: "L" if prof.channels else "alle", 1: "R"}
    pre = prof.auto_preamp_db(max(chans) + 1, fs) if prof.preamp_db is None else prof.preamp_db
    print(f"Profil {prof.name!r}, fs={fs:g} Hz, Preamp {pre:+.2f} dB ({'auto' if prof.preamp_db is None else 'manuell'}), Betragsgang inkl. Preamp")
    print("   f/Hz  " + "  ".join(f"{labels[c]:>8}" for c in chans))
    def clean(v: float) -> float:
        return round(float(v), 2) + 0.0  # "-0.00" vermeiden

    rows = {c: prof.response_db(c, fs, freqs) + pre for c in chans}
    for k, f in enumerate(freqs):
        print(f"{f:>7g}  " + "  ".join(f"{clean(rows[c][k]):>+8.2f}" for c in chans))
    for c in chans:
        print(f"Spitze {labels[c]}: {clean(prof.peak_gain_db(c, fs) + pre):+.2f} dB")
    return 0


def cmd_process(args) -> int:
    files = collect_files(args.inputs)
    if not files:
        sys.exit("Keine Audiodateien gefunden")
    prof = load_profile(args.profile)
    log = RunLog(args.log)
    for n, f in enumerate(files):
        rep = audio.process_file(f, args.out, prof, fmt=args.format or "flac", bits=args.bits, dither=not args.no_dither,
                                 stem=f"{n:03d}_{f.stem}" if args.number else None)
        log.write("process", **rep)
        o = rep["out"]
        print(f"{Path(rep['dst']).name}: {o['format']} {o['samplerate']} Hz {o['bits']} Bit, Preamp {rep['preamp_db']:+.2f} dB, "
              f"Spitze {rep['peak_in_dbfs']:+.2f} -> {rep['peak_out_dbfs']:+.2f} dBFS"
              + (", Clipping-Schutz aktiv" if rep["guard_gain_db"] else ""))
    return 0


def _print_event(rec: dict) -> None:
    ev = rec["event"]
    if ev == "sink":
        print(f"Renderer meldet {len(rec['mimes'])} MIME-Typen: {', '.join(rec['mimes'])}")
    elif ev == "sink_unavailable":
        print(f"Hinweis: Sink-Formate nicht lesbar ({rec['error']}), Standard-MIME wird verwendet")
    elif ev == "track_processed":
        print(f"  verarbeitet: Preamp {rec['preamp_db']:+.2f} dB, {rec['elapsed_s']} s Rechenzeit")
    elif ev == "track_start":
        f = rec.get("served_format") or {}
        fmt = f"{f.get('format', '?')} {f.get('samplerate', '?')} Hz {f.get('bits', '?')} Bit {f.get('channels', '?')} Kanal/Kanaele"
        sink = {True: "im Sink gemeldet", False: "NICHT im Sink gemeldet", None: "Sink unbekannt"}[rec["mime_in_sink"]]
        print(f"> {rec['index'] + 1:03d} {Path(rec['path']).name}  [{fmt}, {rec['mime']} ({sink}){', verarbeitet' if rec['processed'] else ', unveraendert'}]")
    elif ev == "track_end":
        extra = []
        if rec.get("startup_delay_s") is not None:
            extra.append(f"Start nach {rec['startup_delay_s']} s")
        if rec.get("gap_before_s") is not None:
            extra.append(f"Luecke zum Vorgaenger {rec['gap_before_s']} s")
        if rec.get("ended_early"):
            extra.append("ENDE FRUEHER ALS DIE DAUER")
        if rec.get("error"):
            extra.append(rec["error"])
        print(f"  Ende: {rec['end_reason']}" + (" (" + "; ".join(extra) + ")" if extra else ""))
    elif ev == "stream_start":
        how = "dekodiert, Raum-EQ, 24 Bit" if rec["mime"] == "audio/wav" else "unveraendert durchgeleitet"
        print(f"> Radio: {rec['title']}  [{rec['mime']}, {how}, Quelle {rec['source']}]")
    elif ev == "stream_end":
        extra = f" ({rec['error']})" if rec.get("error") else ""
        print(f"  Ende: {rec['end_reason']}{extra}")
    elif ev == "health_change":
        bad = ("warn", "error")
        if rec["status"] in bad or rec.get("previous") in bad:  # nur Probleme und deren Behebung, kein "ok"-Rauschen
            print(f"Gesundheit: {rec['component']}: {rec.get('previous') or '-'} -> {rec['status']} ({rec['message']})")


API_DEFAULT_PORT = 8766  # 8765 ist der Standardport des Medienservers; Test sichert Gleichheit mit api.DEFAULT_PORT


def _check_api_args(args) -> None:
    """Widerspruechliche API-Optionen frueh und ohne Netzwerkzugriff melden."""
    if args.no_api:
        stray = [o for o, on in (("--api-live", args.api_live), ("--api-details", args.api_details),
                                 ("--diag-file", args.diag_file is not None)) if on]
        if stray:
            sys.exit(f"{', '.join(stray)} geht nicht zusammen mit --no-api (API vollstaendig aus).")
        return
    if not 0 <= args.api_port <= 65535:
        sys.exit("--api-port muss 0..65535 sein.")
    if args.api_port != 0 and args.api_port == args.port:
        sys.exit(f"--api-port {args.api_port} ist auch der Port des Medienservers. Einen anderen waehlen.")
    if args.api_live_interval < 0.5:
        sys.exit("--api-live-interval: mindestens 0.5 s (sonst zu viel zusaetzlicher Verkehr zur KEF).")


def _check_repair_args(args) -> None:
    """--enable-repair braucht die Gesundheitsueberwachung (sie ist der einzige reparierbare Teil)."""
    if getattr(args, "enable_repair", False) and args.no_health:
        sys.exit("--enable-repair geht nicht zusammen mit --no-health (nichts zu ueberwachen).")


def _start_api(api_mod, collector, args):
    """API-Server starten. Ein belegter Port ist kein Grund, die Wiedergabe abzubrechen."""
    try:
        srv = api_mod.StatusApiServer(collector, port=args.api_port).start()
    except Exception as e:  # noqa: BLE001 - belegter Port (OSError) oder Fehler in der API: nie die Wiedergabe verhindern
        _err(f"Warnung: Status-API nicht gestartet ({type(e).__name__}: {e}). Anderen --api-port waehlen oder --no-api. "
             "Wiedergabe laeuft ohne API.")
        print("Status-API: nicht gestartet")
        return None
    print(f"Status-API: http://127.0.0.1:{srv.port}/api/v1/diagnostics (nur lesend, nur lokal)"
          + (", Live-Abfrage an (zusaetzlicher Verkehr zur KEF)" if args.api_live else ""))
    return srv


def _finish_api(api_mod, collector, srv, args) -> None:
    """Am Ende: Diagnoseexport schreiben, dann den API-Server schliessen. Fehler hier ueberdecken nie das Ergebnis."""
    if args.diag_file:
        try:
            print(f"Diagnoseexport: {api_mod.write_diag_file(collector, args.diag_file)}")
        except Exception as e:  # noqa: BLE001 - darf das Ergebnis der Wiedergabe nie ueberdecken
            _err(f"Warnung: Diagnoseexport nicht geschrieben ({type(e).__name__}: {e})")
    if srv is not None:
        try:
            srv.stop()
        except Exception as e:  # noqa: BLE001
            _err(f"Warnung: Status-API nicht sauber beendet ({type(e).__name__}: {e})")


def cmd_play(args) -> int:
    _check_api_args(args)
    _check_repair_args(args)
    files = collect_files(args.inputs)
    if not files:
        sys.exit("Keine Audiodateien gefunden")
    prof = load_profile(args.profile)
    renderer = resolve_renderer(args)
    host_ip = args.host_ip or local_ip_for(renderer.info.host)
    server = MediaServer(bind=args.bind, port=args.port, advertise_host=host_ip)
    log = RunLog(args.log)
    try:
        server.start()
    except OSError as e:
        sys.exit(f"Medienserver konnte nicht starten ({e}). Anderen --port waehlen.")
    unchanged = prof.is_neutral() and not args.always_process and args.format is None and args.bits is None
    print(f"Renderer: {renderer.info.friendly_name} ({renderer.info.host}); Medienserver {host_ip}:{server.port}; "
          f"Profil: {prof.name}{' (neutral, Dateien unveraendert)' if unchanged else ''}")
    print("Hinweis: die Lautstaerke des Renderers wird nicht veraendert.")
    api_mod = collector = api_srv = None
    on_event = _print_event
    if not args.no_api:
        from . import api as api_mod  # nur wenn aktiv geladen: --no-api laedt das Modul nie

        collector = api_mod.StatusCollector(details=args.api_details)
        on_event = api_mod.compose_hooks(collector.on_event, _print_event)
    else:
        print("Status-API: aus (--no-api)")
    player = Player(
        renderer, server, prof,
        fmt=args.format, bits=args.bits, dither=not args.no_dither, cache_dir=args.cache_dir, log=log,
        on_event=on_event, poll_s=args.poll, serve_original_if_neutral=not args.always_process,
        plain_protocolinfo=args.plain_protocolinfo, mime_override=args.mime,
    )
    monitor = None
    if not args.no_health:
        from .health import HealthMonitor

        monitor = HealthMonitor(player.emit, info=renderer.info, server=server, cache_dir=args.cache_dir,
                                busy=player.track_active.is_set, interval_s=args.health_interval)
        if collector is not None:
            collector.set_health(monitor)
    if collector is not None:
        try:
            collector.attach(player=player, renderer=renderer, server=server, profile=prof)
            if args.api_live:
                collector.set_live(api_mod.LiveProbe(renderer.info, interval_s=args.api_live_interval))
        except Exception as e:  # noqa: BLE001 - die API darf die Wiedergabe nie verhindern
            _err(f"Warnung: Status-API unvollstaendig eingerichtet ({type(e).__name__}: {e})")
    supervisor = None
    if monitor is not None and args.enable_repair:
        from .repair_exec import RepairSupervisor

        def _make_monitor():
            return HealthMonitor(player.emit, info=renderer.info, server=server, cache_dir=args.cache_dir,
                                 busy=player.track_active.is_set, interval_s=args.health_interval)

        def _on_swap(new):
            if collector is not None:
                collector.set_health(new)

        supervisor = RepairSupervisor(monitor, _make_monitor, busy=player.track_active.is_set,
                                      emit=player.emit, on_swap=_on_swap)
        if collector is not None:
            collector.set_repair(supervisor)
        print("Reparatur: AN (--enable-repair): nur R5 (Diagnose-Monitor neu starten), nie waehrend ein Titel spielt.")
    if sys.stdin is not None and sys.stdin.isatty():
        print("Eingabe + Enter: n = naechster Titel, p = Pause/Weiter, q = Ende")

        def reader():
            for line in sys.stdin:
                c = line.strip().lower()
                if c == "n":
                    player.skip()
                elif c == "p":
                    player.toggle_pause()
                elif c == "q":
                    player.stop()
                    return

        threading.Thread(target=reader, daemon=True).start()
    try:
        if collector is not None:
            api_srv = _start_api(api_mod, collector, args)
        if monitor is not None:
            monitor.start()
        if supervisor is not None:
            supervisor.start()
        results = player.play_queue(files)
    except KeyboardInterrupt:
        player.stop()
        try:
            renderer.stop()
        except (upnp.UpnpError, OSError):
            pass
        print("\nAbgebrochen.")
        return 130
    finally:
        if supervisor is not None:
            supervisor.stop()
            monitor = supervisor.monitor  # evtl. ersetzter Monitor
        if monitor is not None:
            monitor.stop()  # vor dem Medienserver, sonst meldet die letzte Runde ihn als ausgefallen
        if collector is not None:
            _finish_api(api_mod, collector, api_srv, args)
        server.stop()
    bad = [r for r in results if r.get("end_reason") not in ("ended", "skipped", "stopped")]
    print(f"Fertig: {len(results)} Titel, {len(bad)} mit Problemen. Protokoll: {args.log}")
    return 1 if bad else 0


# ---- Parser -----------------------------------------------------------------------


def _print_verdict(name, rep, verdict) -> None:
    label = {"full": "volle Qualitaet", "reduced": "REDUZIERTE Qualitaet", "rejected": "ABGELEHNT"}[verdict.level]
    br = f"{verdict.effective_kbps:.0f} kbit/s" if verdict.effective_kbps else "?"
    prof = rep.profile or "Profil unbekannt"
    flags = f" [{', '.join(verdict.flags)}]" if verdict.flags else ""
    print(f"  {name}: {label}: Codec {rep.codec}, {prof}, {br} ({rep.source}){flags}")
    for r in verdict.reasons:
        print(f"    - {r}")
    for n in rep.notes:
        if n.startswith("HLS"):
            print(f"    - {n}")


def cmd_radio_check(args) -> int:
    """Alle Favoriten (oder einen) pruefen, ohne an den Renderer zu senden."""
    from . import radio

    try:
        favs = radio.load_favorites(args.favorites)
    except radio.RadioError as e:
        _err(f"Fehler ({e.category}): {e}")
        return 2
    if args.only:
        favs = [f for f in favs if f["name"].lower() == args.only.strip().lower()]
        if not favs:
            _err(f"Favorit '{args.only}' nicht in der Liste.")
            return 2
    rows = []
    for fav in favs:
        name, url = fav["name"], fav["url"]
        if radio.is_placeholder(url):
            rows.append({"name": name, "level": "no_url", "category": "stream_url_invalid"})
            print(f"  {name}: keine URL (Platzhalter)")
            continue
        try:
            _, rep, verdict = radio.quality_gate(url, favorite=fav, ffprobe=args.ffprobe, warmup_s=args.quality_warmup,
                                                 window_s=args.quality_window, strict_lc=args.strict_lc, hls_ok=args.hls)
        except radio.RadioError as e:
            rows.append({"name": name, "level": "error", "category": e.category})
            print(f"  {name}: Fehler ({e.category}): {e}")
            continue
        _print_verdict(name, rep, verdict)
        rows.append({"name": name, "level": verdict.level, "category": verdict.category, "codec": rep.codec,
                     "profile": rep.profile, "effective_kbps": verdict.effective_kbps, "flags": verdict.flags,
                     "source": rep.source})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    print("Hinweis: Ergebnis nur fuer diesen Zeitpunkt; nichts an einen Renderer gesendet; nicht an einer KEF Gen1 geprueft.")
    return 0


def _print_radio_event(ev: str, kw: dict) -> None:
    if ev == "radio_stream_up":
        print("  Radio: Strom laeuft" + (" (wieder verbunden)" if not kw.get("initial") else ""))
    elif ev == "radio_gap":
        rc = kw.get("decoder_exit")
        extra = f", Decoder-Exitcode {rc}" if rc not in (None, 0) else ""
        print(f"  Radio: Luecke ({kw.get('reason')}{extra}); KEF bekommt Stille")
        if rc not in (None, 0) and kw.get("decoder_stderr"):
            print(f"    Decoder: {kw['decoder_stderr']}")
    elif ev == "radio_reconnect_wait":
        print(f"  Radio: neuer Versuch in {kw.get('delay_s'):g} s (Versuch {kw.get('attempt')})")
    elif ev == "radio_failed":
        print(f"  Radio: beendet ({kw.get('category')}): {kw.get('reason')}")


def cmd_radio(args) -> int:
    """Internetradio. Ohne --ffmpeg (E1): Stream unveraendert durchleiten. Mit --ffmpeg (E1b): dekodieren, Raum-EQ, WAV 24 Bit."""
    from . import decoder, radio, radio_supervisor
    from .radio_pipeline import RadioPipeline, choose_rate

    e1b = bool(args.ffmpeg)
    if not e1b and (args.profile or args.ffmpeg_sha256 or args.radio_rate or args.no_reconnect):
        _err("Fehler (decoder_unavailable): --profile, --ffmpeg-sha256, --radio-rate und --no-reconnect brauchen --ffmpeg.")
        return 2
    try:
        ff = None
        if e1b:
            ff = decoder.check_ffmpeg(args.ffmpeg, pin_sha256=args.ffmpeg_sha256)
            print(f"ffmpeg: {ff.path}\n  {ff.version}\n  SHA-256 {ff.sha256}")
        profile = load_profile(args.profile) if e1b else None
        fav = radio.resolve_favorite(args.target, args.favorites)
        name, url = fav["name"], fav["url"]
        if radio.is_placeholder(url):
            raise radio.RadioError("stream_url_invalid", f"Favorit '{name}' hat noch keine echte URL (Platzhalter).")
        print(f"Pruefe Stream-Qualitaet ({radio.mask_url(url)}), ca. {args.quality_warmup + args.quality_window:.0f} s ...")
        info, rep, verdict = radio.quality_gate(url, favorite=fav, ffprobe=args.ffprobe, warmup_s=args.quality_warmup,
                                                window_s=args.quality_window, strict_lc=args.strict_lc, hls_ok=e1b)
    except radio.RadioError as e:
        _err(f"Fehler ({e.category}): {e}")
        return 2
    _print_verdict(name, rep, verdict)
    if not verdict.ok:
        _err(f"Fehler ({verdict.category}): Stream erfuellt die Qualitaetsregel nicht; nichts an die KEF gesendet.")
        return 2
    renderer = resolve_renderer(args)
    host_ip = args.host_ip or local_ip_for(renderer.info.host)
    server = MediaServer(bind=args.bind, port=args.port, advertise_host=host_ip)
    try:
        server.start()
    except OSError as e:
        sys.exit(f"Medienserver konnte nicht starten ({e}). Anderen --port waehlen.")
    print(f"Renderer: {renderer.info.friendly_name} ({renderer.info.host}); Medienserver {host_ip}:{server.port}")
    player = Player(renderer, server, load_profile(None), log=RunLog(args.log), on_event=_print_event,
                    poll_s=args.poll, plain_protocolinfo=args.plain_protocolinfo)
    pipe = sup = None
    if e1b:
        rate = choose_rate(rep.sample_rate, args.radio_rate)
        pipe = RadioPipeline(None, profile, rate=rate, channels=2, supervised=True)
        for w in pipe.stats().warnings:
            _err(f"Hinweis: {w}")
        print(f"Hinweis: die Lautstaerke des Renderers wird nicht veraendert. Radio mit Raum-EQ ueber ffmpeg: {rate} Hz, "
              f"WAV PCM 24 Bit (an echter KEF Gen1 ungeprueft). Reconnect: {'aus' if args.no_reconnect else 'an'}.")
        cached = [verdict]
        chosen = {"url": info.url}   # HLS: URL der gepruefte Variante (ffmpeg bekommt nie die Master-Playlist)

        def gate(u):
            if cached:
                return cached.pop()
            i2, _, v2 = radio.quality_gate(u, favorite=fav, ffprobe=args.ffprobe, warmup_s=args.quality_warmup,
                                           window_s=args.quality_window, strict_lc=args.strict_lc, hls_ok=True)
            if v2.ok:
                chosen["url"] = i2.url
            return v2

        def make(u):
            d = decoder.Decoder(ff, chosen["url"], rate=rate, channels=2)
            d.start()
            return d

        def emit(ev, **kw):
            player.log.write(ev, **kw)
            _print_radio_event(ev, kw)

        sup = radio_supervisor.RadioSupervisor(info.hls_master or info.url, pipeline=pipe, gate=gate, make_decoder=make,
                                               budget=radio_supervisor.ReconnectBudget(), emit=emit,
                                               reconnect=not args.no_reconnect)
    else:
        print("Hinweis: die Lautstaerke des Renderers wird nicht veraendert. Radio: nur durchgeleitet (kein Raum-EQ).")
    if sys.stdin is not None and sys.stdin.isatty():
        print("Eingabe + Enter: q = Ende")

        def reader():
            for line in sys.stdin:
                if line.strip().lower() == "q":
                    player.stop()
                    return

        threading.Thread(target=reader, daemon=True).start()
    try:
        if e1b:
            pipe.start()
            sup.start()
            if not sup.wait_ready(30.0):
                st = sup.stats()
                _err(f"Fehler ({st.last_category or 'decoder_failed'}): Decoder nicht gestartet: {st.last_error or 'Zeitueberschreitung'}")
                return 2
            res = player.play_stream(pipe.open_source, "audio/wav", ".wav", name, radio.mask_url(info.url))
        else:
            res = player.play_stream(lambda: radio.open_stream(info.url), info.mime, info.ext, name, radio.mask_url(info.url))
    except KeyboardInterrupt:
        player.stop()
        try:
            renderer.stop()
        except (upnp.UpnpError, OSError):
            pass
        print("\nAbgebrochen.")
        return 130
    finally:
        if sup is not None:
            sup.stop()
        if pipe is not None:
            pipe.stop()
        server.stop()
    ok = res.get("end_reason") in ("ended", "stopped")
    if e1b:
        ps, ss = pipe.stats(), sup.stats()
        print(f"Radio-Zaehler: Reconnects {ss.reconnects}, Luecken {ps.gaps}, Unterlaeufe {ps.underruns}, "
              f"Ueberlauf {ps.overrun_bytes} Byte, uebersteuerte Samples {ps.clipped_samples}")
        if ss.last_category and ss.state == "failed" and ss.last_category != "stream_ended":
            _err(f"Fehler ({ss.last_category}): {ss.last_error}")
            ok = False
    print(f"Fertig: {res.get('end_reason')}. Protokoll: {args.log}")
    return 0 if ok else 1


def _quality_args(p) -> None:
    p.add_argument("--ffprobe", help="Pfad zu ffprobe (optional, vom Nutzer installiert; liefert Profil und deklarierte Bitrate)")
    p.add_argument("--strict-lc", action="store_true", help="nur AAC-LC zulassen; HE-AAC und nicht ausschliessbares HE-AAC ablehnen")
    p.add_argument("--quality-warmup", type=float, default=3.0, help="Sekunden Einlauf vor der Messung (Standard 3)")
    p.add_argument("--quality-window", type=float, default=10.0, help="Messfenster in Sekunden (Standard 10)")


def _renderer_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--renderer", help="URL der Geraetebeschreibung (http://...) oder IP; ohne Angabe wird gesucht")
    p.add_argument("--bind-ip", help="eigene IP der Netzwerkkarte fuer die Suche (mehrere Karten/VPN)")
    p.add_argument("--discover-timeout", type=float, default=3.0)
    p.add_argument("--ssdp-addr", help=argparse.SUPPRESS)  # nur fuer Tests


def _output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", help="Filterprofil (.json oder Equalizer-APO-Textformat); ohne Angabe neutral")
    p.add_argument("--format", choices=["flac", "wav"], default=None,
                   help="Ausgabeformat (Standard: flac). Bei 'play' mit neutralem Profil wird nur umgewandelt, wenn angegeben")
    p.add_argument("--bits", type=int, choices=[16, 24, 32],
                   help="Ausgabe-Bittiefe (Standard: wie Quelle, FLAC max. 24). Bei 'play' mit neutralem Profil wird nur umgewandelt, wenn angegeben")
    p.add_argument("--no-dither", action="store_true", help="kein TPDF-Dither bei 16 Bit")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kefplayer", description="Dateien (optional gefiltert) per UPnP/DLNA an einen Renderer streamen.")
    p.add_argument("--version", action="version", version=f"kefplayer {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="Renderer im Netz suchen (SSDP)")
    d.add_argument("--timeout", type=float, default=3.0)
    d.add_argument("--bind-ip")
    d.add_argument("--ssdp-addr", help=argparse.SUPPRESS)
    d.set_defaults(func=cmd_discover)

    pr = sub.add_parser("probe", help="Renderer abfragen: Geraet, Dienste, gemeldete Formate, Transportstatus")
    _renderer_args(pr)
    pr.set_defaults(func=cmd_probe)

    hl = sub.add_parser("health", help="Einmalige Gesundheitspruefung: Renderer, Medienserver-Port, Cache (Exitcode 1 bei Fehler)")
    _renderer_args(hl)
    hl.add_argument("--bind", default="0.0.0.0", help="Bind-Adresse fuer die Portpruefung des Medienservers")
    hl.add_argument("--port", type=int, default=8765, help="Port des Medienservers (Standard wie bei 'play')")
    hl.add_argument("--cache-dir", default=None, help="Cache-Verzeichnis (Standard: Temp-Verzeichnis)")
    hl.add_argument("--timeout", type=float, default=3.0)
    hl.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    hl.set_defaults(func=cmd_health)

    rs = sub.add_parser("response", help="Rechnerischen Frequenzgang eines Profils anzeigen")
    rs.add_argument("--profile", required=True)
    rs.add_argument("--fs", type=float, default=44100.0)
    rs.set_defaults(func=cmd_response)

    pc = sub.add_parser("process", help="Dateien filtern und in einen Ordner schreiben (ohne Renderer)")
    pc.add_argument("inputs", nargs="+")
    pc.add_argument("--out", required=True)
    pc.add_argument("--log", default=None, help="JSON-Lines-Protokoll")
    pc.add_argument("--number", action="store_true", help="Dateinamen mit laufender Nummer versehen")
    _output_args(pc)
    pc.set_defaults(func=cmd_process)

    rd = sub.add_parser("radio", help="Internetradio (E1): Stream unveraendert durchleiten (nur AAC/Ogg/Opus/FLAC, kein MP3, kein Raum-EQ)")
    rd.add_argument("target", help="Stream-URL (http/https) oder Name aus --favorites")
    rd.add_argument("--favorites", help="Favoritenliste (JSON: [{\"name\": ..., \"url\": ...}])")
    _quality_args(rd)
    _renderer_args(rd)
    rd.add_argument("--port", type=int, default=8765, help="Port des Medienservers (Standard 8765; 0 = beliebig)")
    rd.add_argument("--bind", default="0.0.0.0", help="Schnittstelle des Medienservers")
    rd.add_argument("--host-ip", help="IP, unter der der Renderer diesen PC erreicht (Standard: automatisch)")
    rd.add_argument("--log", default="kefplayer_log.jsonl", help="JSON-Lines-Protokoll")
    rd.add_argument("--poll", type=float, default=0.5, help="Abfrageintervall in s")
    rd.add_argument("--plain-protocolinfo", action="store_true", help="in der Metadaten-protocolInfo '*' statt DLNA-Flags verwenden")
    rd.add_argument("--ffmpeg", help="E1b: ABSOLUTER Pfad zu ffmpeg (vom Nutzer installiert, keine PATH-Suche). Aktiviert Raum-EQ (Dekodieren, WAV 24 Bit)")
    rd.add_argument("--ffmpeg-sha256", help="E1b: erwartete SHA-256 der ffmpeg-Datei (Pin); Abweichung = kein Start")
    rd.add_argument("--profile", help="E1b: Filterprofil (.json oder Equalizer-APO-Textformat) als Raum-EQ; braucht --ffmpeg")
    rd.add_argument("--radio-rate", type=int, help="E1b: Samplerate des Stroms in Hz (Standard: Senderrate, Rueckfall 48000)")
    rd.add_argument("--no-reconnect", action="store_true", help="E1b: bei Stromabbruch nicht neu verbinden")
    rd.set_defaults(func=cmd_radio)

    rc = sub.add_parser("radio-check", help="Favoriten auf Stream-Qualitaet pruefen (E-6), ohne Renderer")
    rc.add_argument("favorites", help="Favoritenliste (JSON)")
    rc.add_argument("--only", help="nur diesen Favoriten pruefen")
    rc.add_argument("--json", action="store_true", help="Ergebnis zusaetzlich als JSON")
    rc.add_argument("--hls", action="store_true", help="HLS (.m3u8) wie bei 'radio --ffmpeg' pruefen (sonst wird HLS abgelehnt)")
    _quality_args(rc)
    rc.set_defaults(func=cmd_radio_check)

    pl = sub.add_parser("play", help="Dateien oder Ordner auf dem Renderer abspielen")
    pl.add_argument("inputs", nargs="+")
    _renderer_args(pl)
    _output_args(pl)
    pl.add_argument("--port", type=int, default=8765, help="Port des Medienservers (Standard 8765; 0 = beliebig)")
    pl.add_argument("--bind", default="0.0.0.0", help="Schnittstelle des Medienservers")
    pl.add_argument("--host-ip", help="IP, unter der der Renderer diesen PC erreicht (Standard: automatisch)")
    pl.add_argument("--log", default="kefplayer_log.jsonl", help="JSON-Lines-Protokoll (Format, Startzeit, Luecken, Anfragen)")
    pl.add_argument("--cache-dir", help="gefilterte Dateien hier behalten (Standard: temporaer, wird geloescht)")
    pl.add_argument("--always-process", action="store_true", help="auch bei neutralem Profil neu schreiben statt Original zu senden")
    pl.add_argument("--plain-protocolinfo", action="store_true", help="in der Metadaten-protocolInfo '*' statt DLNA-Flags verwenden")
    pl.add_argument("--mime", help="MIME-Typ erzwingen (z. B. audio/x-flac)")
    pl.add_argument("--poll", type=float, default=0.5, help="Abfrageintervall in s (= Zeitaufloesung der Messwerte)")
    pl.add_argument("--no-api", action="store_true",
                    help="lokale Status-API vollstaendig aus (Standard: an, nur 127.0.0.1, nur lesend)")
    pl.add_argument("--api-port", type=int, default=API_DEFAULT_PORT,
                    help=f"Port der Status-API auf 127.0.0.1 (Standard {API_DEFAULT_PORT}; 0 = beliebig)")
    pl.add_argument("--no-health", action="store_true",
                    help="Gesundheitsueberwachung aus (Standard: an; fragt im Leerlauf den Renderer lesend ab, nie waehrend ein Titel spielt)")
    pl.add_argument("--enable-repair", action="store_true",
                    help="D2 Opt-in (Standard: AUS, dann nur D1-Schattenentscheidungen): fuehrt R5 aus "
                         "(haengenden Diagnose-Monitor neu starten), nie waehrend ein Titel spielt, mit Budget und Audit-Log")
    pl.add_argument("--health-interval", type=float, default=30.0,
                    help="Abstand der Gesundheitspruefung in Sekunden (Standard 30, mindestens 15)")
    pl.add_argument("--api-live", action="store_true",
                    help="Position/Transportstatus per gedrosselter, nur lesender Renderer-Abfrage liefern "
                         "(zusaetzlicher Verkehr zur KEF; Standard: aus)")
    pl.add_argument("--api-live-interval", type=float, default=1.0,
                    help="Mindestabstand der Live-Abfragen in s (mind. 0.5, Standard 1.0)")
    pl.add_argument("--api-details", action="store_true",
                    help="in der API Pfade, Renderer-Name/IP/UDN und Medienserver-Adresse zeigen (Standard: verborgen)")
    pl.add_argument("--diag-file", metavar="DATEI",
                    help="am Ende der Wiedergabe den Diagnoseexport (JSON) hierher schreiben")
    pl.set_defaults(func=cmd_play)
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:  # z. B. "kefplayer response ... | head"
        try:
            sys.stdout = open("/dev/null", "w") if sys.platform != "win32" else sys.stdout
        except OSError:
            pass
        return 0
    except (ValueError, FileNotFoundError, sf.SoundFileError, upnp.UpnpError) as e:
        _err(f"Fehler: {e}")
        return 2
    except OSError as e:
        _err(f"Netzwerk-/Dateifehler: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
