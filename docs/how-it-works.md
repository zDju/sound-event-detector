# How it works

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

## Measured performance

On a laptop with an Intel Meteor Lake CPU (Manjaro, PipeWire 1.6, Python 3.14), with a 0.72 s
synthetic reference played through the real audio stack:

| Measure | Result |
|---|---|
| Detection delay | 0.78–0.84 s |
| CPU, whole `monitor` process | ~3 % of one core |
| Memory | ~120 MB resident |
| Similarity, synthetic tests | 0.94–1.00 (events), ≤ 0.59 (others) |
| Similarity, through the audio stack | 0.82–1.00 |
| Per-application capture | others absent (−110 dB) |

- **Detection delay**: from the start of the sound to its detection. It is the template length
  (0.72 s) + 20 ms peak hold + 40–100 ms capture buffering and processing.
- **CPU**: matching itself takes ~1.5 % of one core.
- **Memory**: mostly NumPy/SciPy.
- **Similarity, synthetic tests**: real events score 0.94–1.00 under music or pink noise down to
  ~5 dB SNR; unrelated sounds, music and noise score ≤ 0.59.
- **Similarity, through the audio stack**: the lowest score is an event at −12 dB under two
  applications' music, with whole-output capture; the same event scores 0.92 with
  per-application capture.
- **Per-application capture**: other applications' audio is absent (−110 dB) from the capture.

### A real game's sound effect

Two sessions (8 min) with the player's own and other players' copies of a pitch-randomised
splash, music and water ambience; 4 reference clips × 7 speeds, 0.3 s templates, threshold 0.8,
`--min-level -35`:

- own events 9/9 (0.82–0.95), no false detections, best other candidate 0.73;
- reported 0.30–0.32 s after the sound starts (measured live);
- matching 3–7 % of a core.

### Detection delay

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
