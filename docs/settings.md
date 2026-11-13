# Settings

Each event in the library stores its settings in its `event.toml`. Change them with
`sounddetector set NAME ...`. The same options on `scan` and `monitor` (all but `--mask-db`,
which only `set` takes) override the stored values for one run, which is the way to try them
out. `--reference FILE.wav` uses a WAV directly, without the library.

| Setting | Default |
|---|---|
| `--threshold` | 0.75 |
| `--cooldown` | 3 s |
| `--min-level` | −50 dBFS |
| `--pitch-tolerance` | 0 |
| `--template-length` | 0 (all) |
| `--mask-db` | 30 dB |

In Python, `SoundEvent.from_wav(..., pitch_tolerance=0.06, template_length=0.3)` and
`Library().load(name, min_level_db=-31)` take the same settings as the CLI (see
[Library usage](library-usage.md)).

## Threshold

`--threshold`: the similarity needed to report the event. The similarity is the cosine of the
angle between the reference and the audio, in −1…1, not a probability, and thresholds do not
transfer between sounds or setups. Choose it from a real recording with `scan`
([how](usage.md#3-choose-a-threshold-from-a-real-recording)).

## Cooldown

`--cooldown`: ignore the event this long after reporting it. The detector stays quiet until the
score drops and the cooldown has passed.

## Minimum level

`--min-level`: windows quieter than this are not matched. Use it to ignore distant copies of the
sound: the [`scan` example](usage.md#3-choose-a-threshold-from-a-real-recording) shows how the
window level separates them. Leave a few dB of room below your quietest real event: its level
varies with distance and volume settings.

Loopback level may depend on the output volume, depending on the platform. Matching does not
care about level, but very quiet audio falls under the −50 dBFS gate
(`sounddetector set NAME --min-level -60`).

## Pitch tolerance

`--pitch-tolerance`: also match each clip played up to this much faster or slower; 0.06 = ±6 %
in 2 % steps.

**Sounds whose pitch varies.** Many games play a sound effect slightly faster or slower each
time. Even a 3 % change shifts a 0.8 s template by 24 ms and ruins the match; with
`--pitch-tolerance 0.06` each clip is also matched at 0.94…1.06× speed (7 variants; the
`template` field shows which one matched, e.g. `bite-1@0.98`). Each variant costs about as much
CPU as another clip.

## Template length

`--template-length`: use only the first seconds of each clip, so the event is reported sooner.
0 uses the whole clip. The detection delay is dominated by the reference length; see
[Detection delay](how-it-works.md#detection-delay).

## Mask

`--mask-db`: how far below its loudest part the reference is still compared. Quieter cells of
the reference are left out; see [How it works](how-it-works.md), step 2.
