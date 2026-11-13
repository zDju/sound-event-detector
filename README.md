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
> Windows, please run the [Windows test checklist](#testing-on-windows) and report the result.

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

**Windows 10/11**: no extra setup; loopback capture goes through WASAPI. Capturing one
application uses Windows' own process loopback, with no drivers or virtual cables. Microsoft
documents it from build 20348 (Windows Server 2022; every Windows 11 has it); OBS Studio uses
it from Windows 10 version 2004 (build 19041), where it works in practice, and so does this
tool. If you run Windows 10 2004 to 22H2 (builds 19041–19045), please report whether it works
for you (see [Testing on Windows](#testing-on-windows)).

Check the whole chain on your machine (it plays two short test sounds):

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

### 1. Record the sound

Play the sound while recording the output. You can record just the application that makes it,
so nothing else ends up in the clip:

```bash
sounddetector record -d 15 -o bite-raw.wav --source app:firefox       # Linux
sounddetector record -d 15 -o bite-raw.wav --source app:firefox.exe   # Windows (.exe optional)
```

`--source` accepts: nothing (default output, following changes of the default device),
`device:TEXT` (an output whose id or name contains TEXT), `app:NAME` or `app:PID` (one
application's audio, see [Capturing one application](#capturing-one-application)),
`file:PATH.wav` (a file, played at real-time pace).

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
Strongest matches of 'bite' (start, score, window level, template):
    1. 01:34.238  0.901   -29.6 dBFS  bite-2  <- detected
    2. 01:25.278  0.896   -28.2 dBFS  bite-1@0.98  <- detected
    3. 00:36.238  0.881   -27.2 dBFS  bite-2@0.96  <- detected
    4. 02:00.802  0.880   -28.2 dBFS  bite-1@1.06  <- detected
    5. 00:23.978  0.795   -36.2 dBFS  bite-3@1.02
If the top 4 are the real 'bite' events, a threshold of 0.84 separates them from everything
else (margin 0.09).
```

`--apply` saves that threshold. The window level helps when the same sound also comes from
farther away (another player's, another tab's): here the fifth match is a real but distant
occurrence, 7 dB quieter, so `--min-level -35` removes it; the best remaining candidate scores
0.73, a margin of 0.15. Leave a few dB of room below your quietest real event: its level varies
with distance and volume settings.

The score is a **similarity** (cosine of the angle between the
reference and the audio, in −1…1), not a probability: what counts as "high enough" depends on
the sound and the background, which is why it is measured rather than guessed. A small margin
means some false or missed detections should be expected.

### 4. Monitor

```bash
sounddetector monitor -e bite --source app:firefox  # or no --source for all system audio
sounddetector monitor -e bite --json             # one JSON object per detection on stdout
sounddetector monitor -e bite --debug            # level, best score and CPU load every second
```

### Settings

| Setting (`sounddetector set NAME ...`) | Default | What it does |
|---|---|---|
| `--threshold` | 0.75 | similarity needed to report the event |
| `--cooldown` | 3 s | ignore the event this long after reporting it |
| `--min-level` | −50 dBFS | windows quieter than this are not matched (use it to ignore distant copies of the sound) |
| `--pitch-tolerance` | 0 | also match each clip played up to this much faster or slower; 0.06 = ±6 % in 2 % steps |
| `--template-length` | 0 (all) | use only the first seconds of each clip, so the event is reported sooner |
| `--mask-db` | 30 dB | how far below its loudest part the reference is still compared |

The same options on `scan` and `monitor` override the stored values for one run, which is the
way to try them out. `--reference FILE.wav` uses a WAV directly, without the library.

**Sounds whose pitch varies.** Many games play a sound effect slightly faster or slower each
time. Even a 3 % change shifts a 0.8 s template by 24 ms and ruins the match; with
`--pitch-tolerance 0.06` each clip is also matched at 0.94…1.06× speed (7 variants; the
`template` field shows which one matched, e.g. `bite-1@0.98`). Each variant costs about as much
CPU as another clip.

### Capturing one application

`app:NAME` and `app:PID` hear one application and nothing else that plays on the machine.
`sounddetector devices` lists the applications that have audio open, with their names and PIDs.

| | Linux (PipeWire) | Windows 10 2004+ (unverified) |
|---|---|---|
| `app:NAME` matches | part of the application name or binary, case-insensitive | the executable name, case-insensitive, `.exe` optional (`app:firefox`, `app:Firefox.exe`); else the name the application gave its audio session; else part of either, among applications with audio open |
| `app:PID` | the process with that id | the process with that id |
| What is heard | the first playback stream of the application | everything the process and its child processes play (all their streams), so a browser's main process covers its helper processes |
| Several matches | the first one | one that is playing right now; use `app:PID` to choose |
| "Playing audio" means | it has a PipeWire playback stream | it has an audio session that has not expired (what the volume mixer lists) |

Until the application plays audio the source waits for it, retrying every second. When it
exits or closes its audio (on Windows: no audio session for a few seconds), capture stops and
waits for it to come back. A paused application that keeps its audio open stays connected.
In a program, `SoundDetector.connected` (a `threading.Event`) is set while audio is being
captured and cleared while the detector waits for its source.

On Windows this mirrors Linux on purpose. Process loopback itself keeps working for as long
as the process runs, audio session or not, so a later option could stay connected until the
application exits and miss nothing while it reopens its audio.

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
and speed matched), `level_db` (level of the matched window) and `timestamp` (wall clock, live
only). `SoundEvent.from_wav(..., pitch_tolerance=0.06, template_length=0.3)` and
`Library().load(name, min_level_db=-31)` take the same settings as the CLI. `Matcher` is the I/O-free core if you bring
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
   With a pitch tolerance, each reference is also resampled to several playback speeds, each
   becoming a template of its own.
3. **Similarity.** For a window ending at every new frame, the active cells of each frame are
   centred (removing playback volume and flat background) and the cosine similarity with the
   reference is computed. This is done incrementally: each new frame adds its terms to every
   window it belongs to at once, so per template only one vector of partial sums is kept.
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
| A real game's sound effect: two sessions (8 min) with the player's own and other players' copies of a pitch-randomised splash, music and water ambience; 4 reference clips × 7 speeds, 0.3 s templates, threshold 0.8, `--min-level -35` | own events 9/9 (0.82–0.95), no false detections, best other candidate 0.73; reported 0.30–0.32 s after the sound starts (measured live); matching 3–7 % of a core |

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
  capture.py    sources: SoundCard loopback, one application (pw-record / Windows), WAV file
  winapp.py     Windows per-application capture: which process tree, when it plays (plain Python)
  _wasapi.py    WASAPI process loopback and audio sessions through ctypes (Windows only)
  detector.py   SoundDetector: capture thread, reconnects, queue, callbacks
  library.py    on-disk event library (WAV clips + event.toml)
  selftest.py   plays a test sound and checks it is detected
  cli.py        `sounddetector` command
```

The detection algorithm knows nothing about devices; capture knows nothing about detection.

## Known limitations

- **Windows is unverified on real hardware.** Output loopback uses SoundCard's WASAPI path.
  Per-application capture uses WASAPI process loopback, called through ctypes as in
  Microsoft's ApplicationLoopback sample; no extra dependency. Its logic and its COM calls are
  tested on every platform against a simulated Windows, and the Windows test suite (without a
  sound device) will run in CI on the first push. Under Wine, which has no process loopback,
  the structure sizes and the completion handler's COM calls were checked, and activation got
  as far as Wine's `ActivateAudioInterfaceAsync`, which failed because Wine cannot capture a
  process. Until someone confirms it on real Windows, expect problems there (see
  [Testing on Windows](#testing-on-windows)). macOS is not supported (it has no loopback
  capture without a virtual audio device).
- **Per-application capture on Linux** follows the first stream of the matching application;
  an application that opens several streams is heard through one of them. On Windows all of
  the process tree's audio is heard. An application whose sound comes from a process outside
  its own tree must be named by that process (`sounddetector devices` shows which one has the
  audio session). Whether Windows lets a normal user capture an application that runs as
  administrator is not known yet.
- **Output-device capture hears everything** that plays: other applications, voice chat,
  notifications. Prefer `app:`, and turn off music in the source application if you can.
- **Loopback level may depend on the output volume**, depending on the platform. Matching does
  not care about level, but very quiet audio falls under the −50 dBFS gate
  (`sounddetector set NAME --min-level -60`).
- **Every copy of the sound is detected.** If others nearby make the same sound (other players
  in a game), only the level tells near from far (`--min-level`), and that depends on volume
  settings and distance. Telling *your* occurrence apart (e.g. by timing) is up to the consumer.
- **Templates are fixed.** Pitch variation is covered by `--pitch-tolerance`; sounds that vary
  in other ways (randomly layered, different each time) need several reference clips and may
  still be beyond template matching.
- **Background muting.** Some applications (games especially) mute themselves when their
  window loses focus; there is then nothing to detect. Enable their "sound in background" option.
- **Similarity is not a probability**, and thresholds do not transfer between sounds or setups.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `selftest` fails / recordings are silent | Is the output muted? Is the default output the device you hear? `sounddetector devices` lists outputs; try `--source device:...`. |
| `no application matching 'X' is playing audio` | The application must have sound open; `sounddetector devices` lists current streams with names and PIDs. On Windows use the executable name (`app:firefox.exe`); `(... running, but no audio open yet)` means it was found but has not opened audio yet. |
| `capturing one application needs Windows 10 version 2004` | Update Windows, or capture the whole output (no `--source`). |
| Detections are missed | `monitor --debug` shows the best score each second; run `scan` on a recording of the situation and lower the threshold, or add a clip recorded in that situation. |
| False detections | Raise the threshold, use `app:` capture, or record a cleaner reference. |
| `audio backend unavailable` (Linux) | PulseAudio or `pipewire-pulse` must be running for your user. |

## Testing on Windows

Windows support has not been confirmed on real hardware yet. On Windows 10 (version 2004 or
later) or 11 these steps, about 15 minutes, tell whether it works. As the application, use a
browser playing a video and, if you have one, a media player (VLC, Windows Media Player).
Copy each command's output from the console window as you go.

1. **Install and identify.** `uv tool install git+https://github.com/zDju/sound-event-detector`
   (or `pip install` it into a virtualenv), then `sounddetector --version` and `winver` (note
   the version and build; builds 19041–19045, Windows 10 2004 to 22H2, are the ones Microsoft
   does not document, so they are the most useful to hear about).
2. **Whole output.** With speakers or headphones on, `sounddetector selftest`. It plays two
   short test sounds. Expected: two `heard` lines and `PASS`.
3. **Applications.** Start a video in the browser, then `sounddetector devices`. Expected: the
   output devices, one marked `*`, and under *Applications with audio open* the browser's
   executable with a PID, e.g. `firefox.exe  (pid 1234, active)`. Pause the video and run it
   again (is it still listed, `inactive`?).
4. **Record one application.** Play the video and, at the same time, something else (music in
   another program). Run `sounddetector record -d 10 -o app.wav -s app:firefox.exe` with the
   name from step 3. Expected: `Source: firefox.exe (pid ...) with its child processes,
   application audio only`, a level well above −60 dBFS, and in `app.wav` only the video's
   sound. Repeat with `-s app:PID`, using the PID from step 3, and once with the application
   closed (expected: `error: no application matching ... is playing audio`).
5. **Monitor with a test event.** Make an event from a Windows sound, in a separate library so
   your own stays untouched:

   ```bat
   sounddetector --library sed-test add C:\Windows\Media\tada.wav --name tada
   sounddetector --library sed-test monitor -e tada -s app:vlc.exe --debug
   ```

   Play `tada.wav` in the media player a few times (any short sound file works). Expected: a
   `DETECTED tada` line each time, and nothing while other programs play it. Then close the
   player: expected `Capture interrupted: ... exited`, then `no application matching ...;
   retrying`. Start it and play the sound again: expected `Listening to ...` and detections
   again. Stop with Ctrl+C.
6. **Bindings check (optional, needs a clone of the repository and uv).** In PowerShell:

   ```powershell
   $env:SOUNDDETECTOR_REQUIRE_WASAPI = "1"
   uv run pytest -q -s tests/test_wasapi.py
   ```

   (in cmd: `set SOUNDDETECTOR_REQUIRE_WASAPI=1`). With the variable set, a capture that
   Windows refuses fails the test instead of counting as "unavailable", as it does in CI.
7. **Send back:** the Windows version and build, `python --version`, the console output of
   every step (including any traceback, hang or crash), and whether `app.wav` held only the
   application's sound.

## Development

```bash
uv sync
uv run pytest            # synthetic audio only, no sound device needed
uv run pytest -m live    # plays a test sound and detects it through loopback
uv run ruff check src tests examples && uv run basedpyright
```

Tests never use real recordings: game and application sound effects are usually copyrighted, so
keep your own clips out of the repository (`*.wav` is ignored). Windows per-application capture
is tested on every platform against a simulated Windows (`tests/test_windows_app_capture.py`),
and so are its ctypes bindings, against fake DLLs and COM objects made in Python
(`tests/test_wasapi_fake.py`). `tests/test_wasapi.py` runs them against Windows itself, on
Windows only, without needing a sound device.

## Acknowledgements

The log-mel template matching approach — per-frame mean removal for volume invariance and the
small hop, which its author chose by measuring a real game sound — follows
[wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) (MIT). This project is an independent,
application-neutral implementation; it adds masking of the reference's quiet cells and
incremental scoring.

## License

MIT
