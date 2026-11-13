"""Per-application capture on Windows: the logic, run on every platform with a fake backend.

The WASAPI calls themselves are in ``_wasapi``: test_wasapi_fake.py runs them against COM
objects made in Python, test_wasapi.py against Windows (on Windows only).
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import synth
from sound_event_detector import AppSource, SoundDetector, list_apps, open_source
from sound_event_detector.audio import resample
from sound_event_detector.capture import CaptureError
from sound_event_detector.cli import main
from sound_event_detector.winapp import (
    HRESULT_NAMES,
    Target,
    WindowsAppSource,
    WinProcess,
    WinSession,
    apps_from_sessions,
    choose_target,
    exe_key,
    hresult_text,
    match_processes,
    plain_display_name,
    process_tree,
    to_samples,
    tree_is_playing,
)

R = 48000
RATE = 22050

PROCS = [
    WinProcess(4, 0, "System"),
    WinProcess(500, 4, "explorer.exe"),
    WinProcess(100, 500, "browser.exe"),  # a browser: main process...
    WinProcess(101, 100, "browser.exe"),  # ...a helper...
    WinProcess(102, 100, "browser.exe"),  # ...and the helper that plays its audio
    WinProcess(200, 500, "Player.exe"),  # a media player...
    WinProcess(201, 200, "player-audio.exe"),  # ...whose audio comes from a child process
    WinProcess(300, 500, "notes.exe"),  # no audio
]
SESSIONS = [
    WinSession(102, "active"),
    WinSession(201, "inactive", "Media Player"),
    WinSession(0, "inactive", "@%SystemRoot%\\System32\\AudioSrv.Dll,-202", system=True),
    WinSession(999, "active"),  # its process has exited
]


# -- choosing what to capture ----------------------------------------------------------------


def test_names():
    assert exe_key(" Firefox.EXE ") == exe_key("firefox") == "firefox"
    assert plain_display_name("@%SystemRoot%\\System32\\AudioSrv.Dll,-202") == ""
    assert plain_display_name(" Media Player ") == "Media Player"


def test_executable_name_matches_case_insensitively_with_or_without_exe():
    for query in ("browser", "BROWSER.exe", "Browser.Exe"):
        assert match_processes(query, PROCS, SESSIONS) == [100, 101, 102]
    assert match_processes("player", PROCS, SESSIONS) == [200]  # Player.exe, not playing itself
    assert match_processes("notes", PROCS, SESSIONS) == [300]  # matched, even without audio


def test_display_name_then_part_of_a_name_among_applications_with_audio():
    assert match_processes("media player", PROCS, SESSIONS) == [201]
    assert match_processes("audio", PROCS, SESSIONS) == [201]  # part of player-audio.exe
    assert match_processes("brow", PROCS, SESSIONS) == [102]  # only the one with audio
    assert match_processes("not", PROCS, SESSIONS) == []  # notes.exe has no audio open
    assert match_processes("", PROCS, SESSIONS) == []


def test_target_is_the_topmost_process_of_the_tree_that_plays():
    assert choose_target("browser.exe", PROCS, SESSIONS) == Target(100, "browser.exe", True)
    # the session belongs to a child process with another name: the tree still counts
    assert choose_target("player", PROCS, SESSIONS) == Target(200, "Player.exe", False)
    assert choose_target("Media Player", PROCS, SESSIONS).pid == 201


def test_target_by_pid():
    assert choose_target("102", PROCS, SESSIONS) == Target(102, "browser.exe", True)
    assert choose_target("200", PROCS, SESSIONS).pid == 200
    with pytest.raises(CaptureError, match="no application matching '101' is playing audio"):
        choose_target("101", PROCS, SESSIONS)  # running, but its tree has no audio
    with pytest.raises(CaptureError, match="'12345'"):
        choose_target("12345", PROCS, SESSIONS)  # not running


def test_no_target_until_the_application_plays():
    with pytest.raises(CaptureError) as e:
        choose_target("notes", PROCS, SESSIONS)
    assert str(e.value) == (
        "no application matching 'notes' is playing audio "
        "(notes.exe pid 300: running, but no audio open yet)"
    )
    with pytest.raises(CaptureError) as e:
        choose_target("nothing", PROCS, SESSIONS)
    assert str(e.value) == "no application matching 'nothing' is playing audio"
    # sessions could not be listed: take the process as it is
    assert choose_target("notes", PROCS, [], require_session=False).pid == 300


def test_expired_and_system_sessions_do_not_count():
    procs = [WinProcess(10, 1, "player.exe")]
    with pytest.raises(CaptureError):
        choose_target("player", procs, [WinSession(10, "expired")])
    with pytest.raises(CaptureError):
        choose_target("player", procs, [WinSession(10, "active", system=True)])
    assert not tree_is_playing(10, procs, [WinSession(10, "expired")])
    assert tree_is_playing(10, procs, [WinSession(10, "inactive")])


def test_of_several_instances_one_that_is_playing_now_is_taken():
    procs = [WinProcess(10, 1, "player.exe"), WinProcess(20, 1, "player.exe")]
    playing = [WinSession(10, "inactive"), WinSession(20, "active")]
    assert choose_target("player", procs, playing).pid == 20
    paused = [WinSession(10, "inactive"), WinSession(20, "inactive")]
    assert choose_target("player", procs, paused).pid == 10
    one = [WinSession(10, "expired"), WinSession(20, "inactive")]
    assert choose_target("player", procs, one).pid == 20


def test_process_trees_with_reused_parent_ids_do_not_loop():
    procs = [WinProcess(1, 2, "a.exe"), WinProcess(2, 1, "a.exe"), WinProcess(3, 2, "c.exe")]
    assert process_tree(1, procs) == {1, 2, 3}
    assert choose_target("a", procs, [WinSession(3, "active")]).pid in (1, 2)
    assert process_tree(7, procs) == {7}


def test_applications_listed_from_sessions():
    sessions = [*SESSIONS, WinSession(102, "inactive")]  # a second device
    apps = apps_from_sessions(PROCS, sessions)
    assert [(a.name, a.pid, a.binary, a.media, a.state) for a in apps] == [
        ("browser.exe", 102, "browser.exe", "", "active"),
        ("player-audio.exe", 201, "player-audio.exe", "Media Player", "inactive"),
    ]


# -- capture buffers ---------------------------------------------------------------------------


def test_capture_buffers_become_float_samples():
    x = np.array([[0.5, -0.5], [0.25, 0.0]], np.float32)
    y = to_samples(x.astype("<f4").tobytes(), 2, 2, "f32")
    assert y.dtype == np.float32 and np.array_equal(y, x) and y.flags.writeable
    pcm = np.array([[16384, -32768], [0, 32767]], "<i2")
    z = to_samples(pcm.tobytes(), 2, 2, "s16")
    assert z.dtype == np.float32 and z.tolist() == [[0.5, -1.0], [0.0, 32767 / 32768]]
    mono = to_samples(np.arange(3, dtype="<f4").tobytes() + b"extra", 3, 1, "f32")
    assert mono.shape == (3, 1)
    assert np.array_equal(to_samples(b"", 4, 1, "f32", silent=True), np.zeros((4, 1)))
    assert to_samples(b"", 0, 2, "s16").shape == (0, 2)
    with pytest.raises(CaptureError, match="short"):
        to_samples(b"\0" * 6, 2, 1, "f32")
    with pytest.raises(ValueError):
        to_samples(b"", 0, 1, "s24")


def test_hresult_text():
    assert hresult_text(0x88890008 - (1 << 32)) == "0x88890008 (AUDCLNT_E_UNSUPPORTED_FORMAT)"
    assert hresult_text(0x80070005) == "0x80070005 (E_ACCESSDENIED)"
    assert hresult_text(0x12345678) == "0x12345678"


# The definitions of every name in HRESULT_NAMES, copied from the Windows SDK headers as
# mingw-w64 and Wine ship them (audioclient.h, winerror.h, mmdeviceapi.h).
HEADER_DEFINES = """
#define FACILITY_AUDCLNT 2185
#define AUDCLNT_E_NOT_INITIALIZED AUDCLNT_ERR(0x1)
#define AUDCLNT_E_ALREADY_INITIALIZED AUDCLNT_ERR(0x2)
#define AUDCLNT_E_WRONG_ENDPOINT_TYPE AUDCLNT_ERR(0x3)
#define AUDCLNT_E_DEVICE_INVALIDATED AUDCLNT_ERR(0x4)
#define AUDCLNT_E_NOT_STOPPED AUDCLNT_ERR(0x5)
#define AUDCLNT_E_OUT_OF_ORDER AUDCLNT_ERR(0x7)
#define AUDCLNT_E_UNSUPPORTED_FORMAT AUDCLNT_ERR(0x8)
#define AUDCLNT_E_INVALID_SIZE AUDCLNT_ERR(0x9)
#define AUDCLNT_E_DEVICE_IN_USE AUDCLNT_ERR(0xa)
#define AUDCLNT_E_ENDPOINT_CREATE_FAILED AUDCLNT_ERR(0xf)
#define AUDCLNT_E_SERVICE_NOT_RUNNING AUDCLNT_ERR(0x10)
#define AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL AUDCLNT_ERR(0x13)
#define AUDCLNT_E_BUFFER_ERROR AUDCLNT_ERR(0x18)
#define AUDCLNT_E_INVALID_STREAM_FLAG AUDCLNT_ERR(0x21)
#define AUDCLNT_E_RESOURCES_INVALIDATED AUDCLNT_ERR(0x26)
#define AUDCLNT_S_BUFFER_EMPTY AUDCLNT_SUCCESS(0x1)
#define E_ILLEGAL_METHOD_CALL _HRESULT_TYPEDEF_(0x8000000E)
#define E_NOTIMPL _HRESULT_TYPEDEF_(0x80004001)
#define E_NOINTERFACE _HRESULT_TYPEDEF_(0x80004002)
#define E_POINTER _HRESULT_TYPEDEF_(0x80004003)
#define E_FAIL _HRESULT_TYPEDEF_(0x80004005)
#define E_UNEXPECTED _HRESULT_TYPEDEF_(0x8000FFFF)
#define RPC_E_CHANGED_MODE _HRESULT_TYPEDEF_(0x80010106)
#define E_ACCESSDENIED _HRESULT_TYPEDEF_(0x80070005)
#define E_INVALIDARG _HRESULT_TYPEDEF_(0x80070057)
#define ERROR_FILE_NOT_FOUND 2
#define ERROR_NOT_FOUND 1168
#define E_NOTFOUND HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
"""
WINE_HEADERS = Path("/usr/include/wine/windows")


def header_hresults(text: str) -> dict[str, int]:
    """HRESULT values of the ``#define`` lines in ``text``; a Win32 ``ERROR_*`` code is given
    as HRESULT_FROM_WIN32 of it, the way HRESULT_NAMES names it."""
    defines = dict(re.findall(r"^#define[ \t]+(\w+)[ \t]+(.+?)[ \t]*$", text, re.M))
    facility = int(defines.get("FACILITY_AUDCLNT", "0"))
    values = {}
    for name, expr in defines.items():
        if m := re.fullmatch(r"AUDCLNT_(ERR|SUCCESS)\((0x[0-9a-fA-F]+|\d+)\)", expr):
            severity = 0x80000000 if m[1] == "ERR" else 0
            values[name] = severity | facility << 16 | int(m[2], 0)
        elif m := re.fullmatch(r"_HRESULT_TYPEDEF_\((0x[0-9a-fA-F]+)L?\)", expr):
            values[name] = int(m[1], 16)
        elif m := re.fullmatch(r"HRESULT_FROM_WIN32\((\w+)\)", expr):
            values[name] = 0x80070000 | int(defines[m[1]])
        elif name.startswith("ERROR_") and expr.isdigit():
            values[name] = 0x80070000 | int(expr)
    return values


@pytest.mark.parametrize("headers", ["copied", "wine"])
def test_hresult_names_match_the_windows_headers(headers):
    if headers == "copied":
        text = HEADER_DEFINES
    else:  # the real files, where Wine's development headers are installed
        files = [WINE_HEADERS / f for f in ("audioclient.h", "winerror.h", "mmdeviceapi.h")]
        if not all(f.exists() for f in files):
            pytest.skip("Wine's Windows headers are not installed")
        text = "\n".join(f.read_text(errors="replace") for f in files)
    values = header_hresults(text)
    for code, name in HRESULT_NAMES.items():
        assert values.get(name) == code, f"{name}: header {values.get(name)}, table {code:#x}"


# -- the source, with a fake Windows -----------------------------------------------------------


class FakeStream:
    """Hands out the backend's audio in 20 ms packets, twice as fast as real time."""

    def __init__(self, backend: FakeWindows, channels: int) -> None:
        self.backend = backend
        self.channels = channels
        self.closed = False

    def read(self, timeout: float) -> np.ndarray:
        time.sleep(min(timeout, 0.01))
        b = self.backend
        chunk = b.audio[b.pos : b.pos + RATE // 50]
        b.pos += len(chunk)
        return np.repeat(chunk[:, None], self.channels, axis=1).astype(np.float32)

    def close(self) -> None:
        self.closed = True


class StuckStream(FakeStream):
    """Blocks in read() until ``gate`` opens, whatever close() does: a Windows call that hangs."""

    def __init__(self, backend: FakeWindows, gate: threading.Event) -> None:
        super().__init__(backend, 1)
        self.gate = gate

    def read(self, timeout: float) -> np.ndarray:
        self.gate.wait()
        return np.full((441, 1), 0.9, np.float32)


class FailingStream(FakeStream):
    def __init__(self, backend: FakeWindows, error: BaseException) -> None:
        super().__init__(backend, 1)
        self.error = error

    def read(self, timeout: float) -> np.ndarray:
        raise self.error


class FakeWatch:
    def __init__(self, backend: FakeWindows, pid: int) -> None:
        self.backend = backend
        self.pid = pid
        self.closed = False

    def exited(self) -> bool:
        return all(p.pid != self.pid for p in self.backend.procs)

    def close(self) -> None:
        self.closed = True


class FakeWindows:
    """Processes, audio sessions and process loopback, all made up."""

    def __init__(self, procs=PROCS, sessions=SESSIONS, audio=None) -> None:
        self.procs = list(procs)
        self.session_list = list(sessions)
        self.sessions_error: CaptureError | None = None
        self.audio = np.zeros(0, np.float32) if audio is None else audio
        self.pos = 0
        self.opened: list[tuple[int, int, int]] = []
        self.streams: list[FakeStream] = []
        self.next_streams: list[FakeStream] = []  # handed out before plain FakeStreams
        self.watches: list[FakeWatch] = []
        self.fail_open: BaseException | None = None

    def processes(self) -> list[WinProcess]:
        return list(self.procs)

    def sessions(self) -> list[WinSession]:
        if self.sessions_error is not None:
            raise self.sessions_error
        return list(self.session_list)

    def watch(self, pid: int) -> FakeWatch:
        self.watches.append(FakeWatch(self, pid))
        return self.watches[-1]

    def open_stream(self, pid: int, sample_rate: int, channels: int) -> FakeStream:
        if self.fail_open is not None:
            raise self.fail_open
        self.opened.append((pid, sample_rate, channels))
        stream = self.next_streams.pop(0) if self.next_streams else FakeStream(self, channels)
        self.streams.append(stream)
        return stream

    def remove(self, *pids: int) -> None:
        self.procs = [p for p in self.procs if p.pid not in pids]


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def read_until_error(src: WindowsAppSource, tries: int = 100) -> None:
    for _ in range(tries):
        src.read(441)


def test_source_waits_until_the_application_plays():
    win = FakeWindows(procs=[p for p in PROCS if p.pid not in (200, 201)])
    src = WindowsAppSource("player", backend=win, check_interval=0.05)
    with pytest.raises(CaptureError, match="no application matching 'player' is playing audio"):
        src.open()  # not running yet
    win.procs = list(PROCS)
    win.session_list = []
    with pytest.raises(CaptureError, match="running, but no audio open yet"):
        src.open()
    win.session_list = list(SESSIONS)
    win.audio = np.full(RATE, 0.25, np.float32)
    src.open()
    try:
        assert src.name == "Player.exe (pid 200) with its child processes, application audio only"
        assert win.opened == [(200, RATE, 1)]
        block = src.read(1000)
        assert block.shape == (1000, 1) and block[0, 0] == pytest.approx(0.25)
    finally:
        src.close()
    assert win.streams[0].closed and win.watches[-1].closed  # both closed by the capture thread


def test_source_fails_when_the_application_exits():
    win = FakeWindows(audio=np.full(RATE, 0.1, np.float32))
    src = WindowsAppSource("player", backend=win, check_interval=0.05)
    src.open()
    try:
        assert len(src.read(441)) == 441
        win.remove(200, 201)
        with pytest.raises(CaptureError, match=r"Player\.exe \(pid 200\) exited"):
            read_until_error(src)
    finally:
        src.close()


def test_source_fails_when_the_application_stops_playing():
    win = FakeWindows()  # silent: process loopback delivers nothing
    src = WindowsAppSource("player", backend=win, check_interval=0.05, misses=2)
    src.open()
    try:
        assert src.read(441).shape == (0, 1)  # silence is not an error
        win.session_list = [WinSession(201, "expired")]
        with pytest.raises(CaptureError, match="stopped playing audio"):
            read_until_error(src, 20)
    finally:
        src.close()


def test_source_keeps_capturing_when_sessions_cannot_be_listed():
    win = FakeWindows(audio=np.full(RATE * 2, 0.1, np.float32))
    src = WindowsAppSource("player", backend=win, check_interval=0.02, misses=1)
    src.open()
    try:
        win.sessions_error = CaptureError("audio service restarting")
        time.sleep(0.2)
        assert len(src.read(441)) == 441
    finally:
        src.close()
    # unknown at open time: the application is taken without its sessions
    src = WindowsAppSource("notes", backend=win, check_interval=0.02, misses=1)
    src.open()
    try:
        time.sleep(0.1)
        assert win.opened[-1][0] == 300
        src.read(1)
    finally:
        src.close()


def test_source_reports_why_capture_could_not_start():
    win = FakeWindows()
    win.fail_open = CaptureError("Windows refused to capture pid 200")
    with pytest.raises(CaptureError, match="refused"):
        WindowsAppSource("player", backend=win).open()
    # ctypes reports an access violation as OSError: a bug, raised as it is, never retried
    win.fail_open = OSError("exception: access violation reading 0x0000000000000000")
    with pytest.raises(OSError, match="access violation"):
        WindowsAppSource("player", backend=win).open()
    win.fail_open = RuntimeError("a bug")
    with pytest.raises(RuntimeError, match="a bug"):
        WindowsAppSource("player", backend=win).open()
    with pytest.raises(CaptureError, match="not open"):
        WindowsAppSource("player", backend=win).read(10)


def test_detector_connected_follows_the_application(splash_event):
    """``connected`` is set while the application plays and clears when it exits, as on Linux."""
    x = synth.mix(synth.pink_noise(4, R, -40), R, [(1.0, synth.splash(R), 1.0)])
    win = FakeWindows(audio=resample(x, R, RATE))
    src = WindowsAppSource("player", backend=win, check_interval=0.05)
    det = SoundDetector(splash_event, source=src, retry_interval=0.05)
    found = []
    det.on_detect(found.append)
    det.start()
    try:
        assert det.connected.wait(5)
        assert wait_for(lambda: found)
        assert [round(d.start, 1) for d in found] == [1.0]
        win.remove(200, 201)  # the application exits
        assert wait_for(lambda: not det.connected.is_set())
        win.procs = list(PROCS)  # and starts again
        assert wait_for(det.connected.is_set)
        assert det.stats().reconnects == 1
        assert det.running
    finally:
        det.stop()


# -- through the public API and the CLI, as on Windows ------------------------------------------


def test_app_source_uses_process_loopback_on_windows(monkeypatch):
    win = FakeWindows(audio=np.full(RATE, 0.25, np.float32))
    monkeypatch.setattr("sys.platform", "win32")
    src = AppSource("browser", backend=win, channels=2, sample_rate=48000)
    src.open()
    try:
        assert src.name.startswith("browser.exe (pid 100)")
        assert win.opened == [(100, 48000, 2)]
        assert src.read(100).shape == (100, 2)
    finally:
        src.close()
    assert isinstance(open_source("app:browser"), AppSource)


def test_list_apps_and_devices_on_windows(monkeypatch, capsys):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sound_event_detector.winapp.default_backend", FakeWindows)
    monkeypatch.setattr("sound_event_detector.cli.list_devices", lambda: [])
    assert [(a.name, a.pid, a.state) for a in list_apps()] == [
        ("browser.exe", 102, "active"),
        ("player-audio.exe", 201, "inactive"),
    ]
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "Applications with audio open; use --source app:<name or pid>" in out
    assert "    browser.exe  (pid 102, active)\n" in out
    assert "    player-audio.exe  (pid 201, inactive)  — Media Player\n" in out


def test_app_source_refused_elsewhere(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(CaptureError, match=r"Linux .* and Windows"):
        AppSource("player").open()
    with pytest.raises(CaptureError, match=r"Linux .* and Windows"):
        list_apps()


# -- capture thread edge cases -------------------------------------------------------------------


def test_errors_during_capture_reach_the_reader():
    win = FakeWindows()
    src = WindowsAppSource("player", backend=win)
    device_gone = CaptureError("GetBuffer failed: 0x88890004 (AUDCLNT_E_DEVICE_INVALIDATED)")
    win.next_streams.append(FailingStream(win, device_gone))
    src.open()
    try:
        with pytest.raises(CaptureError, match="DEVICE_INVALIDATED"):  # the detector reopens
            read_until_error(src)
    finally:
        src.close()
    win.next_streams.append(FailingStream(win, OSError("exception: access violation")))
    src.open()
    try:
        with pytest.raises(OSError, match="access violation"):  # a bug: not a CaptureError
            read_until_error(src)
    finally:
        src.close()


def test_a_capture_thread_that_does_not_stop_blocks_reopening():
    gate = threading.Event()
    win = FakeWindows(audio=np.full(RATE, 0.1, np.float32))
    win.next_streams.append(StuckStream(win, gate))
    src = WindowsAppSource("player", backend=win, close_timeout=0.1)
    src.open()
    old = src._capture
    assert old is not None and old.thread is not None
    src.close()  # its thread hangs in a Windows call and outlives the join
    assert old.thread.is_alive() and src._stuck is old.thread
    with pytest.raises(CaptureError, match="previous capture of 'player' has not stopped yet"):
        src.open()
    gate.set()  # the call returns: the thread writes into its own, abandoned state and ends
    assert wait_for(lambda: not old.thread.is_alive())
    assert old.avail == 441 and not old.running
    assert win.streams[0].closed and win.watches[0].closed
    src.open()
    try:
        block = src.read(441)
        assert block.shape == (441, 1) and np.all(block == np.float32(0.1))  # nothing of 0.9
    finally:
        src.close()


def test_audio_nobody_reads_is_bounded(monkeypatch):
    monkeypatch.setattr("sound_event_detector.winapp.MAX_BUFFERED", 0.1)  # 2205 frames
    win = FakeWindows(audio=np.arange(RATE, dtype=np.float32) / RATE)  # a ramp: shows the age
    src = WindowsAppSource("player", backend=win)
    src.open()
    try:
        assert wait_for(lambda: win.pos >= RATE // 2)  # 25 packets captured, none read
        capture = src._capture
        assert capture is not None and capture.dropped
        assert capture.avail < 2205 + 441
        assert src.read(1)[0, 0] > 0.3  # the oldest audio was dropped
    finally:
        src.close()


@pytest.mark.skipif(sys.platform == "win32", reason="checks the other platforms")
def test_wasapi_is_never_imported_off_windows():
    code = """
        import sys
        from sound_event_detector import AppSource, CaptureError, list_apps, open_source, winapp
        from sound_event_detector.cli import build_parser

        build_parser()
        open_source("app:player")
        for platform in (sys.platform, "darwin"):
            sys.platform = platform
            for attempt in (
                lambda: AppSource("player", dump=lambda: "[]").open(),
                lambda: list_apps(dump=lambda: "[]"),
                winapp.default_backend,
                winapp.list_windows_apps,
            ):
                try:
                    attempt()
                except CaptureError:
                    pass
        assert "sound_event_detector._wasapi" not in sys.modules
        print("ok")
    """
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stdout + proc.stderr
