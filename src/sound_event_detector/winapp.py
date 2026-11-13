"""One application's audio on Windows, through WASAPI process loopback (Windows 10 2004+).

Everything that decides *what* is captured (which process tree, whether it is playing) and how
raw capture buffers become samples is plain Python here, testable on any platform. The COM
calls live in :mod:`._wasapi`, which is imported only on Windows, when capture starts.

It mirrors ``app:NAME`` / ``app:PID`` on Linux:

* an application counts as *playing audio* while one of its processes has an audio session
  that has not expired (what the Windows volume mixer lists);
* until it plays, opening fails with :class:`CaptureError`, so a detector keeps waiting for it;
* capture covers the matched process and all its child processes, every stream they play;
* when that process exits, or none of its processes has had an audio session for a few
  seconds, reading fails and a detector goes back to waiting (``SoundDetector.connected``
  clears), as when an application's PipeWire stream goes away on Linux.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .capture import AppStream, CaptureError

log = logging.getLogger("sound_event_detector")

# Microsoft documents process loopback (AUDIOCLIENT_ACTIVATION_PARAMS) from build 20348; OBS
# Studio enables it from 19041 (Windows 10 version 2004), where it works in practice.
WINDOWS_MIN_BUILD = 19041
SESSION_CHECK_INTERVAL = 2.0  # seconds between checks that the application still plays
SESSION_MISSES = 2  # consecutive checks without a session before capture stops
READ_WAIT = 0.05  # how long the capture thread waits for the next packet
MAX_BUFFERED = 10.0  # seconds of captured audio kept when nobody reads it

SESSION_STATES = ("inactive", "active", "expired")  # AudioSessionState 0, 1, 2


# -- processes and audio sessions ------------------------------------------------------------


@dataclass(frozen=True)
class WinProcess:
    pid: int
    ppid: int  # parent process id (may name a process that has exited)
    exe: str  # executable file name, e.g. "firefox.exe"


@dataclass(frozen=True)
class WinSession:
    """An audio session on an output device (one per process and device, roughly)."""

    pid: int
    state: str  # "active" (playing now), "inactive" (has streams, paused) or "expired"
    display_name: str = ""  # set by the application, usually empty
    system: bool = False  # the system sounds session

    @property
    def live(self) -> bool:
        """The session has streams: the application has audio open."""
        return self.state != "expired" and not self.system


@dataclass(frozen=True)
class Target:
    """The process tree to capture."""

    pid: int  # root of the tree
    exe: str
    active: bool  # one of its processes is playing right now


def exe_key(name: str) -> str:
    """An executable name compared case-insensitively, with or without ``.exe``."""
    return name.strip().casefold().removesuffix(".exe")


def plain_display_name(name: str) -> str:
    """A session display name, or "" for none or a resource reference (``@%SystemRoot%...``)."""
    name = name.strip()
    return "" if name.startswith("@") else name


def process_tree(root: int, processes: Iterable[WinProcess]) -> set[int]:
    """``root`` and every process descended from it."""
    children: dict[int, list[int]] = {}
    for p in processes:
        if p.pid != p.ppid:
            children.setdefault(p.ppid, []).append(p.pid)
    tree = {root}
    todo = [root]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in tree:  # parent ids of exited processes can be reused: no loops
                tree.add(child)
                todo.append(child)
    return tree


def _topmost(pids: Sequence[int], processes: Sequence[WinProcess]) -> list[int]:
    """Those of ``pids`` with no ancestor among ``pids``: one per process tree."""
    parent = {p.pid: p.ppid for p in processes}
    chosen = set(pids)
    roots = []
    for pid in pids:
        seen = {pid}
        up = parent.get(pid)
        while up is not None and up not in seen and up not in chosen:
            seen.add(up)
            up = parent.get(up)
        if up is None or up in seen:  # reached the top (or a loop) without meeting another
            roots.append(pid)
    return roots or list(pids[:1])  # parent ids reused into a loop of candidates: take one


def match_processes(
    query: str, processes: Sequence[WinProcess], sessions: Sequence[WinSession]
) -> list[int]:
    """Process ids ``app:query`` names, from the first rule that matches anything:

    1. the executable name, case-insensitively, with or without ``.exe`` (``firefox``,
       ``Firefox.exe``), whether or not it plays;
    2. the display name an application gave its audio session;
    3. part of the executable or display name, among applications that have audio open.
    """
    key = exe_key(query)
    if not key:
        return []
    found = [p.pid for p in processes if exe_key(p.exe) == key]
    if found:
        return found
    by_pid = {p.pid: p for p in processes}
    live = [(s, plain_display_name(s.display_name).casefold()) for s in sessions if s.live]
    live = [(s, name) for s, name in live if s.pid in by_pid]
    q = query.strip().casefold()
    found = sorted({s.pid for s, name in live if name == q})
    if found:
        return found
    return sorted({s.pid for s, name in live if key in exe_key(by_pid[s.pid].exe) or q in name})


def choose_target(
    query: str,
    processes: Sequence[WinProcess],
    sessions: Sequence[WinSession],
    *,
    require_session: bool = True,
) -> Target:
    """The process tree ``app:query`` captures; :class:`CaptureError` if nothing plays.

    ``query`` is a process id or a name (see :func:`match_processes`). Of several processes
    with the same name, the topmost one stands for its whole tree (a browser's main process
    for its helpers). A tree counts only if one of its processes has audio open; trees that
    are playing right now come first. With ``require_session=False`` (sessions could not be
    listed) any match is taken.
    """
    by_pid = {p.pid: p for p in processes}
    query = query.strip()
    if query.isdigit() and int(query) in by_pid and int(query) > 0:
        roots = [int(query)]
    else:
        roots = _topmost(match_processes(query, processes, sessions), processes)
    live = [s for s in sessions if s.live]
    ranked = []
    for root in roots:
        tree = process_tree(root, processes)
        mine = [s for s in live if s.pid in tree]
        if mine or not require_session:
            active = any(s.state == "active" for s in mine)
            ranked.append((not active, root, active))
    if not ranked:
        running = ", ".join(f"{by_pid[r].exe} pid {r}" for r in roots[:3])
        detail = f" ({running}: running, but no audio open yet)" if running else ""
        raise CaptureError(f"no application matching {query!r} is playing audio{detail}")
    _, root, active = min(ranked)
    return Target(root, by_pid[root].exe, active)


def tree_is_playing(
    root: int, processes: Sequence[WinProcess], sessions: Sequence[WinSession]
) -> bool:
    """Whether a process of the tree under ``root`` has audio open."""
    tree = process_tree(root, processes)
    return any(s.live and s.pid in tree for s in sessions)


def apps_from_sessions(
    processes: Sequence[WinProcess], sessions: Sequence[WinSession]
) -> list[AppStream]:
    """One entry per process with audio open, as :func:`~.capture.list_apps` returns them.

    ``name`` and ``binary`` are the executable name (what ``app:NAME`` matches first),
    ``media`` the session's display name if the application set one, ``state`` "active"
    while it plays and "inactive" while its streams are paused.
    """
    by_pid = {p.pid: p for p in processes}
    apps: dict[int, AppStream] = {}
    for s in sessions:
        proc = by_pid.get(s.pid)
        if not s.live or proc is None:
            continue
        before = apps.get(s.pid)  # a process has a session on each device it plays on
        active = s.state == "active" or (before is not None and before.state == "active")
        media = (before.media if before else "") or plain_display_name(s.display_name)
        state = "active" if active else "inactive"
        apps[s.pid] = AppStream(0, proc.exe, s.pid, proc.exe, media, state)
    return sorted(apps.values(), key=lambda a: (a.name.casefold(), a.pid or 0))


# -- capture buffers ---------------------------------------------------------------------------

SAMPLE_FORMATS = {"f32": np.dtype("<f4"), "s16": np.dtype("<i2")}


def to_samples(
    raw: bytes, frames: int, channels: int, sample_format: str, *, silent: bool = False
) -> np.ndarray:
    """Float32 ``(frames, channels)`` from a capture buffer of 32-bit float or 16-bit PCM.

    ``silent`` is WASAPI's AUDCLNT_BUFFERFLAGS_SILENT: the buffer is to be read as silence.
    """
    dtype = SAMPLE_FORMATS.get(sample_format)
    if dtype is None:
        raise ValueError(f"unknown sample format {sample_format!r}")
    if silent or frames == 0:
        return np.zeros((frames, channels), np.float32)
    count = frames * channels
    if len(raw) < count * dtype.itemsize:
        raise CaptureError(f"short capture buffer: {len(raw)} bytes for {frames} frames")
    x = np.frombuffer(raw, dtype, count=count).reshape(frames, channels)
    if dtype.kind == "i":
        return x.astype(np.float32) / np.float32(32768.0)
    return x.astype(np.float32)  # a copy: the capture buffer is reused


HRESULT_NAMES = {
    0x80004001: "E_NOTIMPL",
    0x80004002: "E_NOINTERFACE",
    0x80004003: "E_POINTER",
    0x80004005: "E_FAIL",
    0x8000000E: "E_ILLEGAL_METHOD_CALL",
    0x8000FFFF: "E_UNEXPECTED",
    0x80010106: "RPC_E_CHANGED_MODE",
    0x80070002: "ERROR_FILE_NOT_FOUND",
    0x80070005: "E_ACCESSDENIED",
    0x80070057: "E_INVALIDARG",
    0x80070490: "E_NOTFOUND",
    # audioclient.h: AUDCLNT_ERR(n) = 0x88890000 | n, AUDCLNT_SUCCESS(n) = 0x08890000 | n
    0x08890001: "AUDCLNT_S_BUFFER_EMPTY",
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890003: "AUDCLNT_E_WRONG_ENDPOINT_TYPE",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890005: "AUDCLNT_E_NOT_STOPPED",
    0x88890007: "AUDCLNT_E_OUT_OF_ORDER",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x88890009: "AUDCLNT_E_INVALID_SIZE",
    0x8889000A: "AUDCLNT_E_DEVICE_IN_USE",
    0x8889000F: "AUDCLNT_E_ENDPOINT_CREATE_FAILED",
    0x88890010: "AUDCLNT_E_SERVICE_NOT_RUNNING",
    0x88890013: "AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL",
    0x88890018: "AUDCLNT_E_BUFFER_ERROR",
    0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG",
    0x88890026: "AUDCLNT_E_RESOURCES_INVALIDATED",
}


def hresult_text(hr: int) -> str:
    """``0x88890008 (AUDCLNT_E_UNSUPPORTED_FORMAT)`` for a (signed or unsigned) HRESULT."""
    code = hr & 0xFFFFFFFF
    name = HRESULT_NAMES.get(code)
    return f"0x{code:08X}" + (f" ({name})" if name else "")


# -- the source --------------------------------------------------------------------------------


class LoopbackStream(Protocol):
    """A started process-loopback capture; used only by the thread that opened it."""

    def read(self, timeout: float) -> np.ndarray:
        """Everything captured so far as float32 ``(n, channels)``; waits up to ``timeout``
        for the first packet and may return no frames."""
        ...

    def close(self) -> None: ...


class ProcessWatch(Protocol):
    def exited(self) -> bool: ...

    def close(self) -> None: ...


class Backend(Protocol):
    """What :class:`WindowsAppSource` needs from Windows (:mod:`._wasapi`, or a fake in tests)."""

    def processes(self) -> list[WinProcess]: ...

    def sessions(self) -> list[WinSession]:
        """Audio sessions on all active output devices; :class:`CaptureError` if unavailable."""
        ...

    def watch(self, pid: int) -> ProcessWatch: ...

    def open_stream(self, pid: int, sample_rate: int, channels: int) -> LoopbackStream:
        """Start capturing process ``pid`` and its child processes, converted to the given
        rate and channel count by Windows."""
        ...


def default_backend() -> Backend:
    if sys.platform != "win32":
        raise CaptureError("WASAPI process loopback is only available on Windows")
    from ._wasapi import Wasapi

    return Wasapi()


def list_windows_apps(backend: Backend | None = None) -> list[AppStream]:
    """Processes with audio open on Windows (see :func:`apps_from_sessions`)."""
    backend = backend or default_backend()
    return apps_from_sessions(backend.processes(), backend.sessions())


class _Capture:
    """One opening of a :class:`WindowsAppSource`: its capture thread and what it collected.

    Every ``open()`` makes a new one, so a capture thread that outlives ``close()`` (stuck in a
    Windows call) only ever writes into its own, abandoned state.
    """

    def __init__(self, label: str, sample_rate: int, channels: int) -> None:
        self.label = label
        self.sample_rate = sample_rate
        self.channels = channels
        self.halt = threading.Event()
        self.ready = threading.Event()
        self.cond = threading.Condition()
        self.blocks: deque[np.ndarray] = deque()
        self.avail = 0
        self.running = True
        self.started = False
        self.error: BaseException | None = None
        self.dropped = False
        self.thread: threading.Thread | None = None

    def push(self, block: np.ndarray) -> None:
        limit = round(MAX_BUFFERED * self.sample_rate)
        with self.cond:
            self.blocks.append(block)
            self.avail += len(block)
            while self.avail - len(self.blocks[0]) >= limit:
                self.avail -= len(self.blocks.popleft())
                if not self.dropped:
                    self.dropped = True
                    log.warning("Nobody reads the audio of %s; dropping the oldest", self.label)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.running = False
            self.cond.notify_all()
        self.ready.set()

    def read(self, frames: int) -> np.ndarray:
        deadline = time.monotonic() + 0.5
        with self.cond:
            while self.avail < frames and self.running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break  # the application is silent: hand back what there is
                self.cond.wait(remaining)
            if self.avail == 0 and not self.running:
                raise self.error or CaptureError(f"capture of {self.label} stopped")
            return self._take(frames)

    def _take(self, frames: int) -> np.ndarray:
        parts: list[np.ndarray] = []
        need = frames
        while need > 0 and self.blocks:
            block = self.blocks[0]
            if len(block) <= need:
                parts.append(self.blocks.popleft())
                need -= len(block)
            else:
                parts.append(block[:need])
                self.blocks[0] = block[need:]
                need = 0
        self.avail -= frames - need
        if not parts:
            return np.zeros((0, self.channels), np.float32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)


class WindowsAppSource:
    """Capture one application's audio (and its child processes') on Windows.

    Used through :class:`~.capture.AppSource` (``app:NAME`` / ``app:PID``). A thread of its own
    runs the WASAPI capture, so every COM object stays on the thread that made it; :meth:`read`
    takes what it has collected. If that thread does not stop within ``close_timeout`` after
    :meth:`close`, the source is not reopened until it has.
    """

    def __init__(
        self,
        app: str,
        *,
        sample_rate: int = 22050,
        channels: int = 1,
        backend: Backend | None = None,
        check_interval: float = SESSION_CHECK_INTERVAL,
        misses: int = SESSION_MISSES,
        open_timeout: float = 10.0,
        close_timeout: float = 5.0,
    ) -> None:
        self.app = app
        self.sample_rate = sample_rate
        self.channels = channels
        self.name = f"application {app!r}"
        self.check_interval = check_interval
        self.misses = misses
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout
        self._backend = backend
        self._capture: _Capture | None = None
        self._stuck: threading.Thread | None = None  # a capture thread that did not stop

    def open(self) -> None:
        self.close()
        if self._stuck is not None:
            if self._stuck.is_alive():
                raise CaptureError(f"the previous capture of {self.app!r} has not stopped yet")
            self._stuck = None
        backend = self._backend or default_backend()
        self._backend = backend
        processes = backend.processes()
        try:
            sessions, known = backend.sessions(), True
        except CaptureError as e:
            log.debug(
                "Cannot list audio sessions (%s); taking %r without checking it plays", e, self.app
            )
            sessions, known = [], False
        target = choose_target(self.app, processes, sessions, require_session=known)
        capture = _Capture(f"{target.exe} (pid {target.pid})", self.sample_rate, self.channels)
        watch = backend.watch(target.pid)  # closed by the capture thread
        capture.thread = threading.Thread(
            target=self._run,
            args=(capture, backend, target, watch, known),
            name="sed-wasapi",
            daemon=True,
        )
        try:
            capture.thread.start()
        except BaseException:
            watch.close()
            raise
        self._capture = capture
        if not capture.ready.wait(self.open_timeout):
            self.close()
            raise CaptureError(f"timed out starting capture of {capture.label}")
        if not capture.started:
            error = capture.error  # a CaptureError, or a bug: raised as it is, like read() does
            self.close()
            raise error or CaptureError(f"capture of {capture.label} ended before it started")
        self.name = f"{capture.label} with its child processes, application audio only"

    def read(self, frames: int) -> np.ndarray:
        capture = self._capture
        if capture is None:
            raise CaptureError("source is not open")
        return capture.read(frames)

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is None or capture.thread is None:
            return
        capture.halt.set()
        capture.thread.join(self.close_timeout)
        if capture.thread.is_alive():
            log.warning(
                "The capture of %s did not stop within %g s; it is not reopened until it does",
                capture.label,
                self.close_timeout,
            )
            self._stuck = capture.thread

    def stale(self) -> bool:
        return False

    # -- capture thread -------------------------------------------------------------------

    def _run(
        self,
        capture: _Capture,
        backend: Backend,
        target: Target,
        watch: ProcessWatch,
        check: bool,
    ) -> None:
        stream: LoopbackStream | None = None
        try:
            stream = backend.open_stream(target.pid, self.sample_rate, self.channels)
            capture.started = True
            capture.ready.set()
            misses = 0
            next_check = time.monotonic() + self.check_interval
            while not capture.halt.is_set():
                block = stream.read(READ_WAIT)
                if len(block):
                    capture.push(block)
                if watch.exited():
                    raise CaptureError(f"{capture.label} exited")
                if check and time.monotonic() >= next_check:
                    next_check = time.monotonic() + self.check_interval
                    playing = self._playing(backend, target.pid)
                    misses = 0 if playing in (True, None) else misses + 1
                    if misses >= self.misses:
                        raise CaptureError(f"{capture.label} stopped playing audio")
        except BaseException as e:
            # A CaptureError makes the caller wait and reopen. Anything else, an OSError from
            # ctypes (an access violation) included, is a bug and reaches the caller as it is.
            capture.error = e
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception as e:
                    log.debug("Closing the capture of %s failed: %s", capture.label, e)
            with contextlib.suppress(Exception):
                watch.close()
            capture.finish()

    @staticmethod
    def _playing(backend: Backend, pid: int) -> bool | None:
        """Whether the tree still has audio open; None if Windows would not say."""
        try:
            return tree_is_playing(pid, backend.processes(), backend.sessions())
        except CaptureError as e:
            log.debug("Cannot check whether pid %d still plays: %s", pid, e)
            return None
