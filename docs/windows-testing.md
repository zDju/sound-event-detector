# Testing on Windows

Capturing one application is confirmed on one Windows 11 computer (build 26300); output loopback
and Windows 10 are not confirmed yet. On Windows 10 (version 2004 or later) or 11 these steps,
about 15 minutes, tell whether it works. As the application, use a
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

If something fails, [Troubleshooting](troubleshooting.md) lists the common messages.
