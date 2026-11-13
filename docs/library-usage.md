# Library usage

Using the detector from your own Python program.

## Callbacks

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

## Iterating

Or iterate in your own thread (blocks until `stop()`):

```python
for event in SoundDetector("bite.wav").detections():
    print(event.name, round(event.similarity, 2))
```

## Library events, several events, offline detection

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

## Detections and settings

A `Detection` has:

- `name`, `similarity`;
- `start`/`end`: stream seconds of the matched sound;
- `detected_at`;
- `latency`: from the start of the sound to its report;
- `template`: which clip and speed matched;
- `level_db`: level of the matched window;
- `timestamp`: wall clock, live only.

`SoundEvent.from_wav(..., pitch_tolerance=0.06, template_length=0.3)` and
`Library().load(name, min_level_db=-31)` take the same settings as the CLI (see
[Settings](settings.md)).

## Your own audio

`Matcher` is the I/O-free core if you bring your own audio:
`Matcher(events).feed(mono_samples_at_22050_hz)` returns detections.

## Threads and logging

Capture runs in its own thread, so audio keeps flowing while your code works; detections are
handed over through a bounded queue. Logging goes to the `sound_event_detector` logger.

`SoundDetector.connected` (a `threading.Event`) is set while audio is being captured and cleared
while the detector waits for its source (see
[When the application is not playing](capture.md#when-the-application-is-not-playing)).
