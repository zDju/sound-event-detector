# Sound Event Detector

Detect short, predefined sounds — a notification chime, a game sound effect, a doorbell in a
stream — in your computer's audio output or in WAV recordings, using classical signal
processing (log-mel spectrogram template matching). No machine learning, no GPU, no root.

- **Listens to system output** (loopback), **one application only** (Linux/PipeWire), or a file.
- **CLI and library**: `sounddetector monitor`, or `SoundDetector(...)` with callbacks or an
  iterator in your own Python program.
- **Robust to volume and background**: playback level does not matter, and only the loud
  time-frequency cells of the reference are compared, so music and noise underneath have
  limited effect.
- **Light**: a few percent of one CPU core, bounded memory, NumPy + SciPy + SoundCard only.

> **Platform status.** Verified on Linux (Manjaro, PipeWire 1.6 with pipewire-pulse; live
> checks on Python 3.14, test suite on 3.12 and 3.14). Windows uses SoundCard's WASAPI loopback and is **not yet verified on real
> hardware**; run `sounddetector selftest` there and please report the result. Capturing a
> single application is implemented for Linux only.

## Install

```bash
# with uv (recommended)
uv tool install git+https://github.com/zDju/sound-event-detector
# or into a project / virtualenv
pip install git+https://github.com/zDju/sound-event-detector
```

Requires Python 3.12+.

**Linux**: needs PulseAudio or PipeWire with its PulseAudio server (`pipewire-pulse`, the
default on current distributions). Capturing one application needs `pw-record`, part of
PipeWire's tools (`pipewire` on Arch/Manjaro, `pipewire-bin` on Debian/Ubuntu). No system
configuration changes and no root are needed.

**Windows 10/11**: no extra setup; loopback capture goes through WASAPI.

Check the whole chain on your machine (it plays two short test sounds):

```bash
sounddetector selftest
```

## Quick start

```bash
sounddetector devices                         # outputs you can listen to (and apps, on Linux)
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

### 1. Record the sound

Play the sound while recording the output. On Linux you can record just the application that
makes it, so nothing else ends up in the clip:

```bash
sounddetector record -d 15 -o bite-raw.wav --source app:firefox
```

`--source` accepts: nothing (default output, following changes of the default device),
`device:TEXT` (an output whose id or name contains TEXT), `app:NAME` or `app:PID` (one
application's audio, Linux), `file:PATH.wav` (a file, played at real-time pace).

### 2. Add it as a reference

```bash
sounddetector add bite-raw.wav --name bite                 # start and end are detected
sounddetector add bite-raw.wav --name bite --start 3.2 --length 0.6
```

Each event can have several clips (variants of the same sound); the event fires when any of
them matches. The library lives in your user data folder (`~/.local/share/sound-event-detector`
on Linux, `%LOCALAPPDATA%\sound-event-detector` on Windows), one folder per event with its WAV
clips and an optional `event.toml`. Use `--library DIR` or `SOUND_EVENT_DETECTOR_HOME` to put it
elsewhere, and `sounddetector events` to list it.

### 3. Choose a threshold from a real recording

Record a session that contains the sound a known number of times and normal background
otherwise, then (example output):

```bash
sounddetector scan session.wav -e bite --expect 4
```

```text
Strongest matches of 'bite' (start, score):
    1. 00:12.341  0.962  <- detected
    2. 00:47.902  0.948  <- detected
    3. 01:20.115  0.931  <- detected
    4. 02:03.660  0.874  <- detected
    5. 00:31.208  0.552
If the top 4 are the real 'bite' events, a threshold of 0.71 separates them from everything
else (margin 0.32).
```

`--apply` saves that threshold. The score is a **similarity** (cosine of the angle between the
reference and the audio, in −1…1), not a probability: what counts as "high enough" depends on
the sound and the background, which is why it is measured rather than guessed. A small margin
means some false or missed detections should be expected.

### 4. Monitor

```bash
sounddetector monitor -e bite --source app:firefox  # or no --source for all system audio
sounddetector monitor -e bite --json             # one JSON object per detection on stdout
sounddetector monitor -e bite --debug            # level, best score and CPU load every second
```

`sounddetector set bite --threshold 0.8 --cooldown 2` changes stored settings; `--threshold` and
`--cooldown` on `scan`/`monitor` override them for one run. `--reference FILE.wav` uses a WAV
directly, without the library.

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

Or iterate in your own thread (blocks until `stop()`):

```python
for event in SoundDetector("bite.wav").detections():
    print(event.name, round(event.similarity, 2))
```

Events from the library, several events at once, and offline detection:

```python
from sound_event_detector import Library, SoundDetector, SoundEvent, load_wav, scan

lib = Library()
detector = SoundDetector([lib.load("bite"), SoundEvent.from_wav("door.wav", threshold=0.7)])

samples, rate = load_wav("session.wav")
result = scan(samples, rate, [lib.load("bite")])
for d in result.detections:
    print(f"{d.start:.2f}s  {d.name}  {d.similarity:.3f}")
```

A `Detection` has `name`, `similarity`, `start`/`end` (stream seconds of the matched sound),
`detected_at`, `latency` (from the start of the sound to its report), `template` (which clip
matched) and `timestamp` (wall clock, live only). `Matcher` is the I/O-free core if you bring
your own audio: `Matcher(events).feed(mono_samples_at_22050_hz)` returns detections.

Capture runs in its own thread, so audio keeps flowing while your code works; detections are
handed over through a bounded queue. Logging goes to the `sound_event_detector` logger.

## How it works

```text
audio ─► mono, 22050 Hz ─► log-mel frames ─► masked normalized ─► peak picking ─► Detection
         (sound server        (512-pt FFT,      cross-correlation     (threshold,
          resamples)           2.9 ms hop,      with each reference    20 ms peak hold,
                               40 bands         at every frame         cooldown)
                               300-8000 Hz)
```

1. **Features.** Audio is converted to a log-mel spectrogram with a small hop (64 samples): a
   short sound's correlation peak is about one hop wide, so a coarse hop would make the score
   depend on where the sound falls between frames.
2. **Reference template.** Only the reference's *active* cells — within 30 dB (`mask_db`) of its
   loudest cell — are kept. Quiet parts of a reference (a decaying tail, the room behind it) are
   exactly where background sound dominates in real use, so comparing them would only add noise.
   Frames with no active cell are cut from both ends, which also shortens the detection delay.
3. **Similarity.** For a window ending at every new frame, the active cells of each frame are
   centred (removing playback volume and flat background) and the cosine similarity with the
   reference is computed. This is done incrementally: each frame is processed once, and only the
   last template-length of frames is kept.
4. **Gating and events.** Windows quieter than −50 dBFS (`min_level_db`) are not scored. When the
   score crosses the threshold the detector waits up to 20 ms for the peak, reports it, and then
   stays quiet until the score drops and the cooldown has passed.

The live detector, `scan` and `selftest` all use the same streaming `Matcher`, and its results do
not depend on how the audio is split into blocks (tested).

### Measured performance

On a laptop with an Intel Meteor Lake CPU (Manjaro, PipeWire 1.6, Python 3.14), with a 0.72 s
synthetic reference played through the real audio stack:

| | |
|---|---|
| Delay from the start of the sound to its detection | 0.78–0.84 s (template length 0.72 s + 20 ms peak hold + 40–100 ms capture buffering and processing) |
| CPU, whole `monitor` process | ~3 % of one core (matching itself ~1.5 %) |
| Memory | ~120 MB resident (mostly NumPy/SciPy) |
| Similarity, synthetic tests | real events 0.94–1.00 under music or pink noise down to ~5 dB SNR; unrelated sounds, music and noise ≤ 0.59 |
| Similarity, through the audio stack | 0.82–1.00 (lowest: event at −12 dB under two applications' music, whole-output capture; 0.92 for the same event with per-application capture) |
| Per-application capture | other applications' audio absent (−110 dB) from the capture |

**The detection delay is dominated by the reference length**: the detector can only be sure
once it has heard the whole reference. For time-critical uses, keep the reference short (cut it
with `--length`, e.g. to the first 0.3–0.5 s) and verify with `scan` that it still separates
from the background.

## Architecture

```text
src/sound_event_detector/
  audio.py      WAV I/O, mono, resampling, onset/end detection, trimming
  features.py   log-mel spectrogram (NumPy only)
  matching.py   Template, SoundEvent, Detection, the streaming Matcher, offline scan()
  capture.py    sources: SoundCard loopback, PipeWire per-app (pw-record), WAV file
  detector.py   SoundDetector: capture thread, reconnects, queue, callbacks
  library.py    on-disk event library (WAV clips + event.toml)
  selftest.py   plays a test sound and checks it is detected
  cli.py        `sounddetector` command
```

The detection algorithm knows nothing about devices; capture knows nothing about detection.

## Known limitations

- **Windows is unverified.** The code path is SoundCard's WASAPI loopback; per-application
  capture (Windows 10 2004+ process loopback) is not implemented yet. macOS is not supported
  (it has no loopback capture without a virtual audio device).
- **Per-application capture** follows the first stream of the matching application; an
  application that opens several streams is heard through one of them. When the application
  stops playing, capture waits for it to come back.
- **Output-device capture hears everything** that plays: other applications, voice chat,
  notifications. Prefer `app:` on Linux, and turn off music in the source application if you can.
- **Loopback level may depend on the output volume**, depending on the platform. Matching does
  not care about level, but very quiet audio falls under the −50 dBFS gate
  (`sounddetector set NAME --min-level -60`).
- **Fixed templates.** Sounds that vary a lot between occurrences (pitch-shifted, randomly
  layered) need several reference clips, and may still be beyond template matching.
- **Similarity is not a probability**, and thresholds do not transfer between sounds or setups.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `selftest` fails / recordings are silent | Is the output muted? Is the default output the device you hear? `sounddetector devices` lists outputs; try `--source device:...`. |
| `no application matching 'X' is playing audio` | The application must have sound open; `sounddetector devices` lists current streams with names and PIDs. |
| Detections are missed | `monitor --debug` shows the best score each second; run `scan` on a recording of the situation and lower the threshold, or add a clip recorded in that situation. |
| False detections | Raise the threshold, use `app:` capture, or record a cleaner reference. |
| `audio backend unavailable` (Linux) | PulseAudio or `pipewire-pulse` must be running for your user. |

## Development

```bash
uv sync
uv run pytest            # synthetic audio only, no sound device needed
uv run pytest -m live    # plays a test sound and detects it through loopback
uv run ruff check src tests examples && uv run basedpyright
```

Tests never use real recordings: game and application sound effects are usually copyrighted, so
keep your own clips out of the repository (`*.wav` is ignored).

## Acknowledgements

The log-mel template matching approach — per-frame mean removal for volume invariance and the
small hop, which its author chose by measuring a real game sound — follows
[wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) (MIT). This project is an independent,
application-neutral implementation; it adds masking of the reference's quiet cells and
incremental scoring.

## License

MIT
