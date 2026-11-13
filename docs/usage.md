# Command-line usage

The [quick start](../README.md#quick-start) step by step: record the sound, add it as a
reference, choose a threshold from a real recording, monitor.

```bash
sounddetector devices                         # outputs you can listen to, apps playing audio
sounddetector record -d 10 -o sample.wav      # record what the computer plays
sounddetector add sample.wav --name bite      # cut the sound out and store it as a reference
sounddetector scan session.wav -e bite --expect 5 --apply   # choose a threshold from data
sounddetector monitor -e bite                 # listen and report
```

Check the whole chain on your machine first with `sounddetector selftest` (it plays two short
test sounds).

## 1. Record the sound

Play the sound while recording the output. You can record just the application that makes it,
so nothing else ends up in the clip:

```bash
sounddetector record -d 15 -o bite-raw.wav --source app:firefox       # Linux
sounddetector record -d 15 -o bite-raw.wav --source app:firefox.exe   # Windows (.exe optional)
```

`--source` accepts nothing (the default output), `device:TEXT`, `app:NAME` or `app:PID` (one
application's audio), or `file:PATH.wav`; see [Capture sources](capture.md).

## 2. Add it as a reference

```bash
sounddetector add bite-raw.wav --name bite                 # start and end are detected
sounddetector add bite-raw.wav --name bite --start 3.2 --length 0.6
```

Without `--start`, `add` takes the **loudest** sound in the recording, from where it begins to
where it fades. A recording where the wanted sound is the loudest thing in it works best: stop
the recording before anything louder happens.

### The event library

Each event can have several clips (variants of the same sound); the event fires when any of
them matches. The library lives in your user data folder:

- Linux: `~/.local/share/sound-event-detector`
- Windows: `%LOCALAPPDATA%\sound-event-detector`

There is one folder per event with its WAV clips and an optional `event.toml`. Use
`--library DIR` or `SOUND_EVENT_DETECTOR_HOME` to put it elsewhere, and `sounddetector events`
to list it.

## 3. Choose a threshold from a real recording

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

`--apply` saves that threshold.

The window level helps when the same sound also comes from farther away (another player's,
another tab's): here the fifth match is a real but distant occurrence, 7 dB quieter, so
`--min-level -35` removes it; the best remaining candidate scores 0.73, a margin of 0.15. Leave
a few dB of room below your quietest real event: its level varies with distance and volume
settings.

The score is a **similarity** (cosine of the angle between the reference and the audio, in
−1…1), not a probability: what counts as "high enough" depends on the sound and the background,
which is why it is measured rather than guessed. A small margin means some false or missed
detections should be expected.

## 4. Monitor

```bash
sounddetector monitor -e bite --source app:firefox  # or no --source for all system audio
sounddetector monitor -e bite --json             # one JSON object per detection on stdout
sounddetector monitor -e bite --debug            # level, best score and CPU load every second
```

```text
[14:32:10] Monitoring 'bite' (1 clip, threshold 0.75, cooldown 3s); Ctrl+C to stop
[14:32:10] Listening to Monitor of Built-in Audio (default output) at 22050 Hz
[14:32:17] DETECTED bite  similarity=0.94  (clip bite, 0.78s after it started)
```

To change an event's threshold, cooldown, minimum level, pitch tolerance or template length, see
[Settings](settings.md).
