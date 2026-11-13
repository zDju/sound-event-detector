# Troubleshooting

## Common problems

**`selftest` fails / recordings are silent.** Is the output muted? Is the default output the
device you hear? `sounddetector devices` lists outputs; try `--source device:...`.

**`no application matching 'X' is playing audio`.** The application must have sound open;
`sounddetector devices` lists current streams with names and PIDs. On Windows use the executable
name (`app:firefox.exe`); `(... running, but no audio open yet)` means it was found but has not
opened audio yet.

**`capturing one application needs Windows 10 version 2004`.** Update Windows, or capture the
whole output (no `--source`).

**Detections are missed.** `monitor --debug` shows the best score each second; run `scan` on a
recording of the situation and lower the threshold, or add a clip recorded in that situation.

**False detections.** Raise the threshold, use `app:` capture, or record a cleaner reference.

**`audio backend unavailable` (Linux).** PulseAudio or `pipewire-pulse` must be running for your
user.

## Known limitations

- **Windows is unverified on real hardware.** Output loopback uses SoundCard's WASAPI path.
  Per-application capture uses WASAPI process loopback, called through ctypes as in
  Microsoft's ApplicationLoopback sample; no extra dependency. Its logic and its COM calls are
  tested on every platform against a simulated Windows, and the Windows test suite (without a
  sound device) will run in CI on the first push. Under Wine, which has no process loopback,
  the structure sizes and the completion handler's COM calls were checked, and activation got
  as far as Wine's `ActivateAudioInterfaceAsync`, which failed because Wine cannot capture a
  process. Until someone confirms it on real Windows, expect problems there (see
  [Testing on Windows](windows-testing.md)). macOS is not supported (it has no loopback
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
  window loses focus; there is then nothing to detect. Enable their "sound in background"
  option.
- **Similarity is not a probability**, and thresholds do not transfer between sounds or setups.
