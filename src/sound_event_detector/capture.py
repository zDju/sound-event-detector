"""Audio sources: system output loopback, one application's audio, or a WAV file.

* Loopback of an output device goes through SoundCard (PulseAudio/PipeWire monitor sources on
  Linux, WASAPI loopback on Windows). SoundCard is imported only when a device is opened, because
  on Linux importing it connects to the sound server.
* One application's audio is captured on Linux with ``pw-record`` linked straight to that
  application's PipeWire playback stream, so nothing else playing on the machine is heard.
* A WAV file can stand in for a device, for tests and demonstrations.

Every source delivers float32 audio shaped ``(frames, channels)`` at the rate it was asked for;
the sound server does the resampling.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .audio import load_wav, resample, to_mono

log = logging.getLogger("sound_event_detector")

DEFAULT_BLOCK = 1024  # frames per read: 46 ms at 22050 Hz
DEFAULT_CHECK_INTERVAL = 2.0  # seconds between checks whether the default output changed


class CaptureError(Exception):
    """Audio capture could not start or was interrupted (missing device, app not playing...)."""


class EndOfStream(CaptureError):
    """A finite source (a file) has nothing more to give."""


@runtime_checkable
class Source(Protocol):
    """Something audio can be read from, opened and closed as many times as needed."""

    name: str
    sample_rate: int
    channels: int

    def open(self) -> None: ...

    def read(self, frames: int) -> np.ndarray:
        """Up to ``frames`` frames of float32 audio, ``(n, channels)``, waiting at most ~0.5 s.

        Raises :class:`CaptureError` when capture fails or the source is gone.
        """
        ...

    def close(self) -> None: ...

    def stale(self) -> bool:
        """True when the source should be reopened (e.g. the default output changed)."""
        ...


# -- output devices (SoundCard) --------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    is_default: bool


def _soundcard() -> Any:
    try:
        import soundcard
    except Exception as e:  # ImportError, or the sound server refusing the connection
        raise CaptureError(f"audio backend unavailable: {type(e).__name__}: {e}") from e
    return soundcard


def _loopback_mics(sc: Any) -> list[Any]:
    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as e:
        raise CaptureError(f"cannot list audio devices: {type(e).__name__}: {e}") from e
    return [m for m in mics if getattr(m, "isloopback", False)]


def _loopback_of(speaker: Any, mics: list[Any]) -> Any | None:
    """The loopback 'microphone' of an output device.

    WASAPI gives it the speaker's own id; PulseAudio/PipeWire call it ``<sink>.monitor``.
    """
    for m in mics:
        if m.id in (speaker.id, f"{speaker.id}.monitor"):
            return m
    for m in mics:
        if str(speaker.name) in str(m.name):
            return m
    return None


def _default_speaker(sc: Any) -> Any | None:
    try:
        return sc.default_speaker()
    except Exception:
        return None


def list_devices() -> list[Device]:
    """Loopback sources of all output devices, the default output's first."""
    sc = _soundcard()
    mics = _loopback_mics(sc)
    speaker = _default_speaker(sc)
    default = _loopback_of(speaker, mics) if speaker is not None else None
    devices = [Device(str(m.id), str(m.name), m is default) for m in mics]
    return sorted(devices, key=lambda d: not d.is_default)


def _pick_device(query: str, mics: list[Any]) -> Any:
    exact = [m for m in mics if str(m.id) == query or str(m.name) == query]
    if exact:
        return exact[0]
    q = query.lower()
    found = [m for m in mics if q in str(m.id).lower() or q in str(m.name).lower()]
    if len(found) == 1:
        return found[0]
    names = "\n  ".join(f"{m.name}  [{m.id}]" for m in (found or mics))
    if found:
        raise CaptureError(f"{query!r} matches several devices; be more specific:\n  {names}")
    raise CaptureError(f"no output device matches {query!r}; available:\n  {names}")


class DeviceSource:
    """Loopback capture of an output device; ``device=None`` follows the default output."""

    def __init__(
        self,
        device: str | None = None,
        *,
        sample_rate: int = 22050,
        channels: int = 1,
        blocksize: int = DEFAULT_BLOCK,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.check_interval = check_interval
        self.name = "default output" if device is None else device
        self._sc: Any = None
        self._rec: Any = None
        self._default_id: str | None = None
        self._next_check = 0.0

    def open(self) -> None:
        sc = _soundcard()
        mics = _loopback_mics(sc)
        if self.device is None:
            speaker = _default_speaker(sc)
            if speaker is None:
                raise CaptureError("there is no default output device")
            mic = _loopback_of(speaker, mics)
            if mic is None:
                raise CaptureError(f"no loopback source for the default output {speaker.name!r}")
            self._default_id = str(speaker.id)
            self.name = f"{mic.name} (default output)"
        else:
            mic = _pick_device(self.device, mics)
            self.name = str(mic.name)
        # CoreAudio rejects buffer sizes outside the device's range; elsewhere a fixed block
        # keeps latency predictable.
        blocksize = None if sys.platform == "darwin" else self.blocksize
        try:
            rec = mic.recorder(
                samplerate=self.sample_rate, channels=self.channels, blocksize=blocksize
            )
            rec.__enter__()
        except Exception as e:
            raise CaptureError(f"cannot open {mic.name!r}: {type(e).__name__}: {e}") from e
        self._sc, self._rec = sc, rec
        self._next_check = time.monotonic() + self.check_interval

    def read(self, frames: int) -> np.ndarray:
        if self._rec is None:
            raise CaptureError("source is not open")
        try:
            data = self._rec.record(numframes=frames)
        except Exception as e:
            raise CaptureError(f"capture from {self.name!r} failed: {type(e).__name__}: {e}") from e
        data = np.asarray(data, dtype=np.float32)
        return data.reshape(len(data), -1)

    def close(self) -> None:
        rec, self._rec = self._rec, None
        if rec is not None:
            with contextlib.suppress(Exception):
                rec.__exit__(None, None, None)

    def stale(self) -> bool:
        if self.device is not None or self._sc is None:
            return False
        now = time.monotonic()
        if now < self._next_check:
            return False
        self._next_check = now + self.check_interval
        speaker = _default_speaker(self._sc)
        return speaker is not None and str(speaker.id) != self._default_id


# -- one application's audio (PipeWire) ------------------------------------------------------


@dataclass(frozen=True)
class AppStream:
    serial: int  # object.serial, what `pw-record --target` accepts
    name: str  # application.name (or node.name)
    pid: int | None
    binary: str  # application.process.binary
    media: str  # media.name, e.g. the tab or track title


PW_STREAM_CLASS = "Stream/Output/Audio"
# Without these, pw-record silently falls back to the default input (the microphone) when the
# application's stream goes away. With them it exits, and the detector waits for the app.
PW_STREAM_PROPS = "{ node.dont-reconnect = true node.dont-fallback = true }"


def _pw_dump() -> str:
    try:
        return subprocess.run(
            ["pw-dump"], capture_output=True, text=True, check=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise CaptureError(f"cannot run pw-dump (is PipeWire running?): {e}") from e


def parse_app_streams(dump: str) -> list[AppStream]:
    """Application playback streams in ``pw-dump`` JSON output."""
    try:
        objects = json.loads(dump)
    except ValueError as e:
        raise CaptureError(f"unexpected pw-dump output: {e}") from e
    streams = []
    for obj in objects if isinstance(objects, list) else []:
        props = ((obj.get("info") or {}).get("props") or {}) if isinstance(obj, dict) else {}
        if props.get("media.class") != PW_STREAM_CLASS or "object.serial" not in props:
            continue
        pid = str(props.get("application.process.id") or "")
        streams.append(
            AppStream(
                serial=int(props["object.serial"]),
                name=str(props.get("application.name") or props.get("node.name") or "?"),
                pid=int(pid) if pid.isdigit() else None,
                binary=str(props.get("application.process.binary") or ""),
                media=str(props.get("media.name") or ""),
            )
        )
    return streams


def list_apps(*, dump: Callable[[], str] | None = None) -> list[AppStream]:
    """Applications currently playing audio (Linux with PipeWire only)."""
    if not sys.platform.startswith("linux"):
        raise CaptureError("listing applications is only implemented for Linux with PipeWire")
    return parse_app_streams((dump or _pw_dump)())


def find_app(query: str, streams: list[AppStream]) -> AppStream | None:
    """The stream of process ``query`` if it is a number, else the first whose application name,
    binary or node name contains ``query`` (case-insensitive)."""
    if query.isdigit():
        for s in streams:
            if s.pid == int(query):
                return s
    q = query.lower()
    for s in streams:
        if q in s.name.lower() or q in s.binary.lower():
            return s
    return None


class AppSource:
    """Capture one application's audio through PipeWire (``pw-record`` linked to its stream)."""

    def __init__(
        self,
        app: str,
        *,
        sample_rate: int = 22050,
        channels: int = 1,
        dump: Callable[[], str] | None = None,
    ) -> None:
        self.app = app
        self.sample_rate = sample_rate
        self.channels = channels
        self.name = f"application {app!r}"
        self._dump = dump
        self._proc: subprocess.Popen[bytes] | None = None
        self._rest = b""

    def open(self) -> None:
        if not sys.platform.startswith("linux"):
            raise CaptureError(
                "capturing one application's audio is only implemented for Linux (PipeWire); "
                "use the default output or a device instead"
            )
        if shutil.which("pw-record") is None:
            raise CaptureError("pw-record not found; install PipeWire's command-line tools")
        stream = find_app(self.app, list_apps(dump=self._dump))
        if stream is None:
            raise CaptureError(f"no application matching {self.app!r} is playing audio")
        cmd = [
            "pw-record", "--target", str(stream.serial), "--rate", str(self.sample_rate),
            "--channels", str(self.channels), "--format", "f32", "--raw",
            "-P", PW_STREAM_PROPS, "-",
        ]  # fmt: skip
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            )
        except OSError as e:
            raise CaptureError(f"cannot start pw-record: {e}") from e
        assert self._proc.stdout is not None
        os.set_blocking(self._proc.stdout.fileno(), False)
        self._rest = b""
        who = f"pid {stream.pid}" if stream.pid is not None else stream.binary or "?"
        self.name = f"{stream.name} ({who}, application audio only)"

    def read(self, frames: int) -> np.ndarray:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise CaptureError("source is not open")
        frame_bytes = 4 * self.channels
        want = frames * frame_bytes
        fd = proc.stdout.fileno()
        parts = [self._rest]
        got = len(self._rest)
        deadline = time.monotonic() + 0.5
        while got < want:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # the application is silent or paused: hand back what there is
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, want - got)
            if not chunk:
                raise CaptureError(f"the audio stream of {self.app!r} ended")
            parts.append(chunk)
            got += len(chunk)
        data = b"".join(parts)
        usable = len(data) // frame_bytes * frame_bytes
        self._rest = data[usable:]
        return np.frombuffer(data[:usable], dtype="<f4").reshape(-1, self.channels).copy()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    def stale(self) -> bool:
        return False


# -- WAV file --------------------------------------------------------------------------------


class FileSource:
    """A WAV file read like a device, at real-time pace or as fast as possible."""

    def __init__(
        self,
        path: str | Path,
        *,
        sample_rate: int = 22050,
        channels: int = 1,
        realtime: bool = True,
    ) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.realtime = realtime
        self.name = f"file {self.path.name}"
        self._data: np.ndarray | None = None
        self._pos = 0
        self._t0 = 0.0

    def open(self) -> None:
        if self._data is None:
            try:
                samples, rate = load_wav(self.path)
            except ValueError as e:
                raise CaptureError(str(e)) from e
            if self.channels == 1:
                samples = to_mono(samples)[:, None]
            elif samples.ndim == 1:
                samples = np.repeat(samples[:, None], self.channels, axis=1)
            self._data = resample(samples, rate, self.sample_rate)[:, : self.channels]
        self._t0 = time.monotonic() - self._pos / self.sample_rate

    def read(self, frames: int) -> np.ndarray:
        if self._data is None:
            raise CaptureError("source is not open")
        if self._pos >= len(self._data):
            raise EndOfStream(f"end of {self.path.name}")
        block = self._data[self._pos : self._pos + frames]
        self._pos += len(block)
        if self.realtime:
            wait = self._t0 + self._pos / self.sample_rate - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        return block

    def close(self) -> None:
        pass

    def stale(self) -> bool:
        return False


# -- choosing a source -----------------------------------------------------------------------


def open_source(spec: str | None = None, *, sample_rate: int = 22050, channels: int = 1) -> Source:
    """A source from a short description (it is opened later, by whoever reads it).

    * ``None``, ``""`` or ``"default"``: loopback of the default output, following changes;
    * ``"app:NAME"`` or ``"app:PID"``: one application's audio (Linux/PipeWire);
    * ``"file:PATH"``: a WAV file played at real-time pace;
    * ``"device:QUERY"`` or any other text: the output device whose id or name contains it.
    """
    if spec is None or spec.strip() in ("", "default"):
        return DeviceSource(None, sample_rate=sample_rate, channels=channels)
    kind, sep, value = spec.partition(":")
    if sep and kind == "app":
        return AppSource(value, sample_rate=sample_rate, channels=channels)
    if sep and kind == "file":
        return FileSource(value, sample_rate=sample_rate, channels=channels)
    if sep and kind == "device":
        return DeviceSource(value, sample_rate=sample_rate, channels=channels)
    return DeviceSource(spec, sample_rate=sample_rate, channels=channels)


def record(
    source: Source,
    seconds: float,
    *,
    on_block: Callable[[np.ndarray, float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> np.ndarray:
    """Read ``seconds`` of audio from an unopened source; returns ``(frames, channels)``.

    ``on_block(block, seconds_so_far)`` is called after every read (for progress display). Stops
    early, returning what it has, when ``should_stop()`` turns true, a file ends, or the source
    fails after something was recorded.
    """
    total = round(seconds * source.sample_rate)
    blocks: list[np.ndarray] = []
    got = 0
    source.open()
    try:
        while got < total and not (should_stop and should_stop()):
            try:
                block = source.read(min(DEFAULT_BLOCK, total - got))
            except EndOfStream:
                break
            except CaptureError as e:
                if not blocks:
                    raise
                log.warning("Recording stopped early: %s", e)
                break
            blocks.append(block)
            got += len(block)
            if on_block is not None:
                on_block(block, got / source.sample_rate)
    finally:
        source.close()
    if not blocks:
        return np.zeros((0, source.channels), dtype=np.float32)
    return np.concatenate(blocks)[:total]
