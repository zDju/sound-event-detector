"""Command line interface: ``sounddetector <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .audio import find_end, find_onset, level_db, load_wav, save_wav, to_mono, trim
from .capture import CaptureError, list_apps, list_devices, open_source, record
from .detector import SoundDetector
from .library import Library
from .matching import Detection, SoundEvent, scan, suggest_threshold

log = logging.getLogger("sound_event_detector")
_STOP_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM") if hasattr(signal, name)
)


class UsageError(Exception):
    """A problem with what the user asked for; printed without a traceback."""


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger("sound_event_detector")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


def _clock(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m):02d}:{s:06.3f}"


def _say(msg: str) -> None:
    log.info(msg)


# -- devices ---------------------------------------------------------------------------------


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = list_devices()
    except CaptureError as e:
        print(f"Output devices: unavailable ({e})")
        devices = []
    if devices:
        print("Output devices (loopback); use --source device:<text from id or name>")
        for d in devices:
            mark = "*" if d.is_default else " "
            print(f"  {mark} {d.name}\n      id: {d.id}")
        print("  (* = default output, used when no --source is given)")
    if sys.platform.startswith("linux"):
        try:
            apps = list_apps()
        except CaptureError as e:
            print(f"\nApplications: unavailable ({e})")
            return 0
        print("\nApplications playing audio; use --source app:<name or pid>")
        if not apps:
            print("  (none right now; an application appears here while it has sound open)")
        for a in apps:
            pid = f"pid {a.pid}" if a.pid is not None else "pid ?"
            extra = f"  — {a.media}" if a.media and a.media != a.name else ""
            print(f"    {a.name}  ({pid}, {a.binary or '?'}){extra}")
    return 0


# -- record ----------------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    if args.duration <= 0:
        raise UsageError("--duration must be > 0")
    source = open_source(args.source, sample_rate=args.rate, channels=args.channels)
    stop = threading.Event()
    last = [0.0]

    def progress(block: object, seconds: float) -> None:
        if last[0] == 0.0:
            _say(f"Source: {source.name}")
            last[0] = 1e-9
        if seconds - last[0] >= 1.0 or seconds >= args.duration:
            last[0] = seconds
            print(f"\r  {seconds:5.1f} / {args.duration:g} s", end="", file=sys.stderr, flush=True)

    _say(
        f"Recording {args.duration:g} s at {args.rate} Hz, {args.channels} channel(s); "
        "Ctrl+C stops early"
    )
    # Ctrl+C (or SIGTERM) ends the recording early and still saves what was captured.
    previous = {sig: signal.getsignal(sig) for sig in _STOP_SIGNALS}
    for sig in _STOP_SIGNALS:
        signal.signal(sig, lambda *_: stop.set())
    try:
        data = record(source, args.duration, on_block=progress, should_stop=stop.is_set)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(file=sys.stderr)
    if len(data) == 0:
        raise UsageError("nothing was recorded")
    save_wav(args.output, data, args.rate, float32=args.float)
    lvl = level_db(data)
    peak = float(abs(data).max())
    _say(
        f"Saved {args.output} ({len(data) / args.rate:.1f} s, level {lvl:.1f} dBFS, "
        f"peak {20 * math.log10(peak + 1e-12):.1f} dBFS)"
    )
    if peak < 1e-4:
        log.warning("The recording is silent. Was anything playing on %s?", source.name)
    return 0


# -- library: add / events / set ------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    lib = Library(args.library)
    samples, rate = load_wav(args.wav)
    mono = to_mono(samples)
    total = len(mono) / rate
    start = args.start if args.start is not None else find_onset(mono, rate)
    if args.end is not None and args.length is not None:
        raise UsageError("give --end or --length, not both")
    if args.end is not None:
        length = args.end - start
    elif args.length is not None:
        length = args.length
    else:
        length = find_end(mono, rate, start) - start
    if length <= 0:
        raise UsageError(f"the clip would be empty (start {start:.3f}s, end {start + length:.3f}s)")
    clip = trim(mono, rate, start, length)
    path = lib.add(args.name, clip, rate, label=args.label)
    how = "given" if args.start is not None else "detected"
    print(
        f"Added a reference for '{args.name}': {path}\n"
        f"  taken from {start:.3f}s to {start + len(clip) / rate:.3f}s of {args.wav} "
        f"({total:.2f}s long; start {how})"
    )
    settings = {"threshold": args.threshold, "cooldown": args.cooldown}
    if any(v is not None for v in settings.values()):
        lib.update_settings(args.name, settings)
    ev = lib.load(args.name)
    tpl = next(t for t in ev.templates if t.label == path.stem)
    print(
        f"  template: {tpl.duration:.3f}s of sound (starting {tpl.offset:.3f}s into the clip); "
        f"it is reported about {tpl.duration + 0.02:.2f}s after it starts"
    )
    if tpl.duration > 1.5:
        print("  note: long templates delay detection; --length can shorten it")
    print(
        f"  event '{args.name}' now has {len(ev.templates)} reference clip(s), threshold "
        f"{ev.threshold:g}, cooldown {ev.cooldown:g}s\n"
        f"Next: record a session with a few occurrences and run\n"
        f"  sounddetector scan SESSION.wav --event {args.name} --expect N"
    )
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    lib = Library(args.library)
    names = lib.names()
    print(f"Library: {lib.root}")
    if not names:
        print("  (no events yet; add one with: sounddetector add CLIP.wav --name NAME)")
    for name in names:
        info = lib.info(name)
        s = info.settings
        print(
            f"  {name}: {len(info.clips)} clip(s), threshold {s['threshold']:g}, cooldown "
            f"{s['cooldown']:g}s, min level {s['min_level_db']:g} dBFS, mask {s['mask_db']:g} dB"
        )
        for c in info.clips:
            print(f"      {c.name}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    lib = Library(args.library)
    lib.info(args.name)  # must exist
    values = {
        "threshold": args.threshold,
        "cooldown": args.cooldown,
        "min_level_db": args.min_level,
        "mask_db": args.mask_db,
    }
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        raise UsageError(
            "nothing to change; give --threshold, --cooldown, --min-level or --mask-db"
        )
    s = lib.update_settings(args.name, values)
    print(", ".join(f"{k} = {v:g}" for k, v in s.items()))
    return 0


# -- choosing events -------------------------------------------------------------------------


def _events(args: argparse.Namespace) -> list[SoundEvent]:
    lib = Library(args.library)
    overrides = {"threshold": args.threshold, "cooldown": args.cooldown}
    events: list[SoundEvent] = []
    for path in args.reference or []:
        events.append(
            SoundEvent.from_wav(path, **{k: v for k, v in overrides.items() if v is not None})
        )
    names = args.event or ([] if args.reference else lib.names())
    for name in names:
        events.append(lib.load(name, **overrides))
    if not events:
        raise UsageError(
            "no events to look for: add one with `sounddetector add`, or pass --reference FILE.wav"
        )
    return events


def _describe(events: Sequence[SoundEvent]) -> str:
    return ", ".join(
        f"'{e.name}' ({len(e.templates)} clip{'s' if len(e.templates) > 1 else ''}, "
        f"threshold {e.threshold:g}, cooldown {e.cooldown:g}s)"
        for e in events
    )


# -- scan ------------------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    events = _events(args)
    samples, rate = load_wav(args.wav)
    t0 = time.perf_counter()
    result = scan(samples, rate, events)
    took = time.perf_counter() - t0
    print(f"Scanned {args.wav} ({result.duration:.1f}s in {took:.2f}s) for {_describe(events)}")
    print(f"\nDetections ({len(result.detections)}):")
    for d in result.detections:
        print(f"  {_clock(d.start)}  {d.name}  similarity {d.similarity:.3f}  (clip {d.template})")
    if not result.detections:
        print("  none")
    for ev in events:
        peaks = result.peaks(ev.name)
        shown = max(args.top, (args.expect or 0) + 3)
        print(f"\nStrongest matches of '{ev.name}' (start, score):")
        for i, (t, s) in enumerate(peaks[:shown], 1):
            mark = "  <- detected" if s >= ev.threshold else ""
            print(f"  {i:3d}. {_clock(t)}  {s:.3f}{mark}")
        if not peaks:
            print("  none: the recording is silent or quieter than the minimum level")
        if args.expect:
            suggestion = suggest_threshold([s for _, s in peaks], args.expect)
            if suggestion is None:
                print(
                    f"\nOnly {len(peaks)} candidate(s) found, "
                    f"fewer than the {args.expect} expected."
                )
                continue
            threshold, margin = suggestion
            print(
                f"\nIf the top {args.expect} are the real '{ev.name}' events, a threshold of "
                f"{threshold:.2f} separates them from everything else (margin {margin:.2f})."
            )
            if margin < 0.1:
                print("  The margin is small: expect false or missed detections. Try other clips.")
            if args.apply:
                if args.event and ev.name in args.event:
                    Library(args.library).update_settings(
                        ev.name, {"threshold": round(threshold, 3)}
                    )
                    print(f"  Saved threshold {threshold:.3f} for '{ev.name}'.")
                else:
                    print("  --apply only changes events from the library (--event NAME).")
    return 0


# -- monitor ---------------------------------------------------------------------------------


def cmd_monitor(args: argparse.Namespace) -> int:
    events = _events(args)
    detector = SoundDetector(events, source=args.source)
    as_json = args.json

    def report(d: Detection) -> None:
        if as_json:
            print(
                json.dumps(
                    {
                        "time": d.timestamp.isoformat() if d.timestamp else None,
                        "name": d.name,
                        "similarity": round(d.similarity, 4),
                        "template": d.template,
                        "stream_start": round(d.start, 4),
                        "latency": round(d.latency, 4),
                    }
                ),
                flush=True,
            )
        else:
            _say(
                f"DETECTED {d.name}  similarity={d.similarity:.2f}  "
                f"(clip {d.template}, {d.latency:.2f}s after it started)"
            )

    detector.on_detect(report)
    _say(f"Monitoring {_describe(events)}; Ctrl+C to stop")
    stop_at = time.monotonic() + args.duration if args.duration else None

    finished = threading.Event()

    def watchdog() -> None:
        while not finished.wait(1.0):
            if args.debug:
                st = detector.stats()
                peaks = detector.take_peaks()
                best = "  ".join(f"{k}={v:.2f}" if v > -1 else f"{k}=-" for k, v in peaks.items())
                log.debug(
                    f"level {st.level_db:6.1f} dBFS | best {best} | load {st.load * 100:.2f}%"
                )
            if stop_at is not None and time.monotonic() >= stop_at:
                detector.stop()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: detector.stop())
    try:
        detector.run()
    except KeyboardInterrupt:
        detector.stop()
        print(file=sys.stderr)
    finally:
        finished.set()
    st = detector.stats()
    _say(
        f"Stopped after {st.audio_seconds:.1f}s of audio; analysis used {st.load * 100:.2f}% of "
        f"real time (slowest block {st.max_feed_ms:.1f} ms), {st.reconnects} reconnect(s)"
    )
    return 0


# -- selftest ------------------------------------------------------------------------------


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    _say("Self-test: playing two short test sounds (the second at -10 dB) and listening for them")
    r = run_selftest(args.source, volume=args.volume)
    _say(f"Played on: {r.speaker}")
    _say(f"Listened to: {r.source}")
    for (offset, gain), d, lat in zip(r.played, r.heard, r.latencies, strict=True):
        what = f"test sound at +{offset:.1f}s (gain {gain:g})"
        if d is None:
            _say(f"  MISSED {what}")
        else:
            _say(
                f"  heard  {what}: similarity {d.similarity:.3f}, "
                f"reported {lat:.2f}s after it played"
            )
    for d in r.extra:
        _say(f"  unexpected detection: similarity {d.similarity:.3f}")
    if r.ok:
        _say("PASS: loopback capture and detection work on this output")
        return 0
    _say(
        "FAIL: check that the output is not muted, that the test sound is audible, and that the "
        "source listens to the device that plays it (see `sounddetector devices`)"
    )
    return 1


# -- parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sounddetector",
        description="Detect short, predefined sounds in system audio or in WAV recordings.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--library", type=Path, help="event library folder (default: user data dir)")
    p.add_argument("-v", "--verbose", action="store_true", help="more diagnostic output")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def source_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-s",
            "--source",
            help="what to listen to: default output (default), device:TEXT, app:NAME or app:PID "
            "(Linux), or file:PATH.wav",
        )

    def event_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-e",
            "--event",
            action="append",
            help="event from the library (repeatable; default: all)",
        )
        sp.add_argument(
            "-r",
            "--reference",
            action="append",
            type=Path,
            help="reference WAV used as is (repeatable)",
        )
        sp.add_argument("--threshold", type=float, help="override the similarity threshold")
        sp.add_argument("--cooldown", type=float, help="override the cooldown, seconds")

    sp = sub.add_parser("devices", help="list output devices and applications you can listen to")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("record", help="record system (or application) audio to a WAV file")
    sp.add_argument("-d", "--duration", type=float, default=10.0, help="seconds (default 10)")
    sp.add_argument("-o", "--output", type=Path, required=True, help="WAV file to write")
    source_arg(sp)
    sp.add_argument("--rate", type=int, default=48000, help="sample rate (default 48000)")
    sp.add_argument(
        "--channels", type=int, choices=(1, 2), default=1, help="1 = mono (default), 2 = stereo"
    )
    sp.add_argument("--float", action="store_true", help="write 32-bit float instead of 16-bit PCM")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("add", help="add a reference clip of an event to the library")
    sp.add_argument("wav", type=Path, help="recording that contains the sound")
    sp.add_argument("-n", "--name", required=True, help="event name, e.g. bite")
    sp.add_argument(
        "--start", type=float, help="where the sound starts, seconds (default: detected)"
    )
    sp.add_argument("--length", type=float, help="clip length, seconds (default: until it fades)")
    sp.add_argument("--end", type=float, help="where the sound ends, seconds")
    sp.add_argument("--label", help="file name for the clip (default: the event name)")
    sp.add_argument("--threshold", type=float, help="similarity threshold for this event")
    sp.add_argument("--cooldown", type=float, help="seconds to ignore the event after a detection")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("events", help="list the events in the library")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("set", help="change an event's settings")
    sp.add_argument("name")
    sp.add_argument("--threshold", type=float)
    sp.add_argument("--cooldown", type=float)
    sp.add_argument(
        "--min-level", type=float, help="quieter audio is not matched, dBFS (default -50)"
    )
    sp.add_argument(
        "--mask-db",
        type=float,
        help="compare reference cells this close to its loudest (default 30)",
    )
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("scan", help="find events in a WAV recording and help choose a threshold")
    sp.add_argument("wav", type=Path)
    event_args(sp)
    sp.add_argument(
        "--expect", type=int, help="how many real events the recording holds; suggests a threshold"
    )
    sp.add_argument(
        "--apply",
        action="store_true",
        help="save the suggested threshold (with --event and --expect)",
    )
    sp.add_argument(
        "--top", type=int, default=10, help="how many strongest matches to list (default 10)"
    )
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("selftest", help="play a test sound and check that it is detected")
    sp.add_argument(
        "-s", "--source", help="output device to test, device:TEXT (default: the default output)"
    )
    sp.add_argument("--volume", type=float, default=0.3, help="playback gain, 0-1 (default 0.3)")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("monitor", help="listen continuously and report events")
    event_args(sp)
    source_arg(sp)
    sp.add_argument("--duration", type=float, help="stop after this many seconds")
    sp.add_argument(
        "--json", action="store_true", help="print each detection as a JSON line on stdout"
    )
    sp.add_argument(
        "--debug", action="store_true", help="print level, best score and CPU load every second"
    )
    sp.set_defaults(func=cmd_monitor)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose or getattr(args, "debug", False))
    try:
        return int(args.func(args))
    except (UsageError, CaptureError, KeyError, ValueError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
