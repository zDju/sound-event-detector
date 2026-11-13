# Capture sources

What `record` and `monitor` listen to is chosen with `--source` (`-s`):

| `--source` | Listens to |
|---|---|
| nothing | the default output |
| `device:TEXT` | an output whose id or name contains TEXT |
| `app:NAME` | one application, by name |
| `app:PID` | one application, by process id |
| `file:PATH.wav` | a file, played at real-time pace |

With no `--source`, capture follows changes of the default device. `sounddetector devices` lists
the outputs you can listen to and the applications that have audio open.

## Requirements

**Linux**: needs PulseAudio or PipeWire with its PulseAudio server (`pipewire-pulse`, the
default on current distributions). Capturing one application needs `pw-record`, part of
PipeWire's tools (`pipewire` on Arch/Manjaro, `pipewire-bin` on Debian/Ubuntu). No system
configuration changes and no root are needed.

**Windows 10/11**: no extra setup; loopback capture goes through WASAPI. Capturing one
application uses Windows' own process loopback, with no drivers or virtual cables. Microsoft
documents it from build 20348 (Windows Server 2022; every Windows 11 has it); OBS Studio uses
it from Windows 10 version 2004 (build 19041), where it works in practice, and so does this
tool. If you run Windows 10 2004 to 22H2 (builds 19041–19045), please report whether it works
for you (see [Testing on Windows](windows-testing.md)).

## Capturing one application

`app:NAME` and `app:PID` hear one application and nothing else that plays on the machine.
`sounddetector devices` lists the applications that have audio open, with their names and PIDs.

### Linux (PipeWire)

- **`app:NAME` matches** part of the application name or binary, case-insensitive.
- **`app:PID`** is the process with that id.
- **What is heard**: the first playback stream of the application.
- **Several matches**: the first one is used.
- **"Playing audio"** means it has a PipeWire playback stream.

### Windows 10 2004+ (verified on one Windows 11 computer)

- **`app:NAME` matches** the executable name, case-insensitive, `.exe` optional (`app:firefox`,
  `app:Firefox.exe`); else the name the application gave its audio session; else part of
  either, among applications with audio open.
- **`app:PID`** is the process with that id.
- **What is heard**: everything the process and its child processes play (all their streams),
  so a browser's main process covers its helper processes.
- **Several matches**: one that is playing right now is used; use `app:PID` to choose.
- **"Playing audio"** means it has an audio session that has not expired (what the volume mixer
  lists).

### When the application is not playing

Until the application plays audio the source waits for it, retrying every second. When it
exits or closes its audio (on Windows: no audio session for a few seconds), capture stops and
waits for it to come back. A paused application that keeps its audio open stays connected.
In a program, `SoundDetector.connected` (a `threading.Event`) is set while audio is being
captured and cleared while the detector waits for its source.

On Windows this mirrors Linux on purpose. Process loopback itself keeps working for as long
as the process runs, audio session or not, so a later option could stay connected until the
application exits and miss nothing while it reopens its audio.

See also the [known limitations](troubleshooting.md#known-limitations) of per-application and
output-device capture.
