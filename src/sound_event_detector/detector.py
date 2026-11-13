"""Live detection: a capture thread feeds the matcher, detections reach you through a queue.

One background thread reads audio and runs the matcher (a few milliseconds of work per second
of audio), so capture never waits on your code. Detections are handed over through a bounded
queue and delivered in *your* thread: by iterating :meth:`SoundDetector.detections`, by
:meth:`SoundDetector.run`, or, after :meth:`SoundDetector.start`, in one dispatcher thread that
calls the registered callbacks in order.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .audio import level_db, to_mono
from .capture import DEFAULT_BLOCK, CaptureError, EndOfStream, Source, open_source
from .matching import Detection, Matcher, SoundEvent

log = logging.getLogger("sound_event_detector")

Callback = Callable[[Detection], object]
_DONE = object()


@dataclass(frozen=True)
class Stats:
    """Counters for diagnostics (``--debug`` in the CLI)."""

    audio_seconds: float  # audio analysed
    busy_seconds: float  # time spent in the matcher
    max_feed_ms: float  # slowest single block
    level_db: float  # level of the last block, dBFS
    reconnects: int
    source: str

    @property
    def load(self) -> float:
        """Fraction of real time spent analysing (0.01 = 1%)."""
        return self.busy_seconds / self.audio_seconds if self.audio_seconds else 0.0


def _as_events(events: object) -> list[SoundEvent]:
    if isinstance(events, SoundEvent):
        return [events]
    if isinstance(events, (str, Path)):
        return [SoundEvent.from_wav(events)]
    if isinstance(events, Sequence):
        return [e if isinstance(e, SoundEvent) else SoundEvent.from_wav(e) for e in events]
    raise TypeError("events must be a SoundEvent, a WAV path, or a sequence of them")


class SoundDetector:
    """Listen to a source and report the events heard in it.

    ``events`` is a :class:`SoundEvent`, a reference WAV path (the event is named after the
    file), or a list of them. ``source`` is a :class:`Source` or a description understood by
    :func:`open_source` (default: the default output's loopback).

    Example::

        detector = SoundDetector("bite.wav", threshold=0.8)

        @detector.on_detect
        def handle(d):
            print(d.name, d.similarity)

        detector.run()  # until Ctrl+C
    """

    def __init__(
        self,
        events: SoundEvent | str | Path | Sequence[SoundEvent | str | Path],
        *,
        source: Source | str | None = None,
        threshold: float | None = None,
        cooldown: float | None = None,
        block: int = DEFAULT_BLOCK,
        retry_interval: float = 1.0,
        max_queue: int = 100,
    ) -> None:
        evs = _as_events(events)
        if threshold is not None or cooldown is not None:
            evs = [
                replace(
                    e,
                    threshold=e.threshold if threshold is None else threshold,
                    cooldown=e.cooldown if cooldown is None else cooldown,
                )
                for e in evs
            ]
        self.matcher = Matcher(evs)
        rate = self.matcher.sample_rate
        if source is None or isinstance(source, str):
            source = open_source(source, sample_rate=rate)
        if source.sample_rate != rate:
            raise ValueError(
                f"source delivers {source.sample_rate} Hz, the detector needs {rate} Hz"
            )
        self.source = source
        self.block = block
        self.retry_interval = retry_interval
        self._callbacks: list[Callback] = []
        self._queue: queue.Queue[object] = queue.Queue(max_queue)
        self._stop = threading.Event()
        self._capture: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._audio_seconds = 0.0
        self._busy = 0.0
        self._max_feed = 0.0
        self._level = -120.0
        self._reconnects = 0
        self.connected = threading.Event()

    @property
    def events(self) -> tuple[SoundEvent, ...]:
        return self.matcher.events

    @property
    def running(self) -> bool:
        return self._capture is not None and self._capture.is_alive()

    def on_detect(self, callback: Callback) -> Callback:
        """Register a callback (usable as a decorator); called once per detection."""
        self._callbacks.append(callback)
        return callback

    def stats(self) -> Stats:
        with self._lock:
            return Stats(
                self._audio_seconds,
                self._busy,
                self._max_feed * 1000.0,
                self._level,
                self._reconnects,
                self.source.name,
            )

    def take_peaks(self) -> dict[str, float]:
        """Each event's best score since the previous call (-1: nothing scored, e.g. silence)."""
        with self._lock:
            return self.matcher.take_peaks()

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        """Start listening in the background; registered callbacks run in a dispatcher thread."""
        self._start_capture()
        if self._dispatcher is None or not self._dispatcher.is_alive():
            self._dispatcher = threading.Thread(
                target=self._dispatch, name="sed-dispatch", daemon=True
            )
            self._dispatcher.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop listening and release the audio source. Safe to call more than once."""
        self._stop.set()
        for t in (self._capture, self._dispatcher):
            if t is not None and t is not threading.current_thread():
                t.join(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until listening ends (``stop()``, end of a file, or an error); True if it did."""
        t = self._capture
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    def run(self) -> None:
        """Listen in this thread, calling the callbacks, until :meth:`stop` or Ctrl+C."""
        try:
            for detection in self.detections():
                self._call(detection)
        finally:
            self.stop()

    def detections(self) -> Iterator[Detection]:
        """Start listening (if needed) and yield detections as they happen.

        Ends when :meth:`stop` is called or a finite source runs out; re-raises an unexpected
        error from the capture thread. Use either this or callbacks, not both at once.
        """
        if self._dispatcher is not None and self._dispatcher.is_alive():
            raise RuntimeError("detections are being delivered to callbacks (start() was called)")
        self._start_capture()
        yield from self._drain()

    def __enter__(self) -> SoundDetector:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- internals -----------------------------------------------------------------------

    def _start_capture(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._error = None
        self._queue = queue.Queue(self._queue.maxsize)
        self._capture = threading.Thread(target=self._capture_loop, name="sed-capture", daemon=True)
        self._capture.start()

    def _drain(self) -> Iterator[Detection]:
        while True:
            try:
                item = self._queue.get(timeout=0.2)  # a timeout keeps Ctrl+C responsive
            except queue.Empty:
                continue
            if item is _DONE:
                if self._error is not None:
                    raise RuntimeError(f"audio capture failed: {self._error}") from self._error
                return
            assert isinstance(item, Detection)
            yield item

    def _dispatch(self) -> None:
        for detection in self._drain():
            self._call(detection)

    def _call(self, detection: Detection) -> None:
        for cb in list(self._callbacks):
            try:
                cb(detection)
            except Exception:
                log.exception("detection callback %r failed", cb)

    def _put(self, item: object) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                    if dropped is not _DONE:
                        log.warning("detection queue full: dropped the oldest detection")
                except queue.Empty:
                    pass

    def _capture_loop(self) -> None:
        src = self.source
        waiting = False
        first = True
        try:
            while not self._stop.is_set():
                try:
                    src.open()
                except EndOfStream:
                    return
                except CaptureError as e:
                    if not waiting:
                        log.warning("%s; retrying every %gs", e, self.retry_interval)
                        waiting = True
                    self._stop.wait(self.retry_interval)
                    continue
                waiting = False
                if not first:
                    with self._lock:
                        self._reconnects += 1
                first = False
                with self._lock:
                    self.matcher.reset(keep_cooldown=True)
                log.info("Listening to %s at %d Hz", src.name, src.sample_rate)
                self.connected.set()
                retry = True
                try:
                    retry = self._read_until_stale(src)
                except EndOfStream:
                    return
                except CaptureError as e:
                    log.warning("Capture interrupted: %s", e)
                finally:
                    self.connected.clear()
                    src.close()
                if retry:
                    self._stop.wait(self.retry_interval)
        except BaseException as e:  # a bug, not a device problem: surface it to the consumer
            self._error = e
            log.exception("capture thread failed")
        finally:
            self._put(_DONE)

    def _read_until_stale(self, src: Source) -> bool:
        """Read and analyse until stopped or the source goes stale; False = reopen at once."""
        while not self._stop.is_set():
            block = src.read(self.block)
            if len(block) == 0:
                continue
            mono = to_mono(block)
            t0 = time.perf_counter()
            with self._lock:
                found = self.matcher.feed(mono)
                spent = time.perf_counter() - t0
                self._busy += spent
                self._max_feed = max(self._max_feed, spent)
                self._audio_seconds += len(mono) / src.sample_rate
                self._level = level_db(mono)
            if found:
                now = datetime.now().astimezone()
                for d in found:
                    self._put(replace(d, timestamp=now))
            if src.stale():
                log.info("The default output changed; switching")
                return False
        return False
