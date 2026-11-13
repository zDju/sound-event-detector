# Sound Event Detector

Detect short, predefined sounds — a notification chime, a game sound effect, a doorbell in a
stream — in your computer's audio output or in WAV recordings, using classical signal
processing (log-mel spectrogram template matching). No machine learning, no GPU, no root.

- **Listens to system output** (loopback), **one application only** (Linux/PipeWire, Windows
  10 2004+), or a file.
- **CLI and library**: `sounddetector monitor`, or `SoundDetector(...)` with callbacks or an
  iterator in your own Python program.
- **Robust to volume and background**: playback level does not matter, and only the loud
  time-frequency cells of the reference are compared, so music and noise underneath have
  limited effect.
- **Tolerates pitch variation**: sound effects that a game or app plays slightly faster or
  slower each time are matched with `pitch_tolerance`.
- **Light**: a few percent of one CPU core, bounded memory, NumPy + SciPy + SoundCard only.

> **Platform status.** Verified on Linux (Manjaro, PipeWire 1.6 with pipewire-pulse; live
> checks on Python 3.14, test suite on 3.12 and 3.14). Windows is implemented but **unverified
> on real hardware**: output loopback goes through SoundCard's WASAPI path, and capturing one
> application through WASAPI process loopback (Windows 10 version 2004 or later). If you have
> Windows, please run the [Windows test checklist](docs/windows-testing.md) and report the
> result. macOS is not supported.

## Install

```bash
# with uv (recommended)
uv tool install git+https://github.com/zDju/sound-event-detector
# or into a project / virtualenv
pip install git+https://github.com/zDju/sound-event-detector
```

Requires Python 3.12+.

- **Linux**: PulseAudio, or PipeWire with `pipewire-pulse` (the default on current
  distributions). Capturing one application also needs `pw-record`, part of PipeWire's tools.
  No system configuration changes and no root.
- **Windows 10/11**: no extra setup, no drivers or virtual cables. Capturing one application
  needs Windows 10 version 2004 or later.

Details: [requirements](docs/capture.md#requirements). Check the whole chain on your machine (it
plays two short test sounds):

```bash
sounddetector selftest
```

## Quick start

```bash
sounddetector devices                         # outputs you can listen to, apps playing audio
sounddetector record -d 10 -o sample.wav      # record what the computer plays
sounddetector add sample.wav --name bite      # cut the sound out and store it as a reference
sounddetector scan session.wav -e bite --expect 5 --apply   # choose a threshold from data
sounddetector monitor -e bite                 # listen and report
```

```text
[14:32:10] Monitoring 'bite' (1 clip, threshold 0.75, cooldown 3s); Ctrl+C to stop
[14:32:10] Listening to Monitor of Built-in Audio (default output) at 22050 Hz
[14:32:17] DETECTED bite  similarity=0.94  (clip bite, 0.78s after it started)
```

1. **Record** while the sound plays. `--source app:firefox` (Linux) or `app:firefox.exe`
   (Windows) records just that application, so nothing else ends up in the clip.
2. **Add** it: `add` takes the loudest sound in the recording, from where it begins to where it
   fades (`--start` and `--length` choose by hand). An event can have several clips.
3. **Choose a threshold** with `scan` on a recording that holds the sound a known number of
   times (`--expect N`); `--apply` saves the threshold it suggests.
4. **Monitor**: `--json` prints one JSON object per detection, `--debug` the level, best score
   and CPU load every second.

Each step in detail, with example output: [docs/usage.md](docs/usage.md).

## Settings

| Setting | Default | What it does |
|---|---|---|
| `--threshold` | 0.75 | similarity needed to report |
| `--cooldown` | 3 s | quiet time after a report |
| `--min-level` | −50 dBFS | quieter windows are not matched |
| `--pitch-tolerance` | 0 | also match faster or slower playback |
| `--template-length` | 0 (all) | use only the clip's first seconds |
| `--mask-db` | 30 dB | compare reference cells this close to its loudest |

`sounddetector set NAME ...` stores them for an event; the same options on `scan` and `monitor`
(all but `--mask-db`) override them for one run. Each one in detail: [docs/settings.md](docs/settings.md).

## Library usage

```python
from sound_event_detector import SoundDetector

detector = SoundDetector("bite.wav", threshold=0.8, source="app:firefox")  # or source=None

@detector.on_detect
def handle(event):
    print(event.name, event.similarity, event.timestamp)

detector.start()      # background; callbacks run in one dispatcher thread
...
detector.stop()
```

Iterating, library events, offline `scan()`, the `Detection` fields and the I/O-free `Matcher`:
[docs/library-usage.md](docs/library-usage.md).

## Known limitations

- **Windows is unverified on real hardware**; macOS is not supported.
- **Output-device capture hears everything** that plays; prefer `app:`.
- **Every copy of the sound is detected**; only the level tells near from far (`--min-level`).
- **Templates are fixed**; sounds that vary in other ways than pitch may be beyond template
  matching.
- **Background muting**: some applications mute themselves when their window loses focus.
- **Similarity is not a probability**, and thresholds do not transfer between sounds or setups.

All of them in detail: [docs/troubleshooting.md](docs/troubleshooting.md#known-limitations).

## Documentation

- [Command-line usage](docs/usage.md): record, add, choose a threshold, monitor
- [Settings](docs/settings.md): each setting in detail
- [Capture sources](docs/capture.md): `--source`, capturing one application, requirements
- [Library usage](docs/library-usage.md): the Python API
- [How it works](docs/how-it-works.md): the algorithm, measured performance, architecture
- [Troubleshooting](docs/troubleshooting.md): common problems, known limitations
- [Testing on Windows](docs/windows-testing.md): the checklist for Windows testers
- [Development](docs/development.md): tests and checks

## Acknowledgements

The log-mel template matching approach — per-frame mean removal for volume invariance and the
small hop, which its author chose by measuring a real game sound — follows
[wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) (MIT). This project is an
independent, application-neutral implementation; it adds masking of the reference's quiet cells
and incremental scoring.

## License

MIT
