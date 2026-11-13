"""The live detector, driven by fake and file sources (no sound server needed)."""

from __future__ import annotations

import threading
import time

import numpy as np

import synth
from sound_event_detector import FileSource, SoundDetector
from sound_event_detector.audio import resample, save_wav
from sound_event_detector.capture import CaptureError, EndOfStream

R = 48000


def session(tmp_path, times=(1.0, 4.5), seconds=7.0):
    x = synth.mix(synth.pink_noise(seconds, R, -40), R, [(t, synth.splash(R), 1.0) for t in times])
    path = tmp_path / "session.wav"
    save_wav(path, x, R)
    return path


def test_detections_iterator(tmp_path, splash_event):
    src = FileSource(session(tmp_path), realtime=False)
    det = SoundDetector(splash_event, source=src)
    found = list(det.detections())
    assert [round(d.start, 2) for d in found] == [1.0, 4.5]
    assert all(d.timestamp is not None for d in found)
    assert not det.running


def test_callbacks_and_start_stop(tmp_path, splash_event):
    src = FileSource(session(tmp_path), realtime=False)
    det = SoundDetector(splash_event, source=src)
    got = []
    det.on_detect(got.append)
    det.start()
    assert det.wait(10)
    time.sleep(0.3)  # let the dispatcher deliver
    det.stop()
    assert [round(d.start, 2) for d in got] == [1.0, 4.5]


def test_reference_path_and_overrides(tmp_path):
    ref = tmp_path / "bite.wav"
    save_wav(ref, synth.splash(R), R)
    det = SoundDetector(ref, source=FileSource(session(tmp_path)), threshold=0.9, cooldown=1)
    assert det.events[0].name == "bite"
    assert (det.events[0].threshold, det.events[0].cooldown) == (0.9, 1)


def test_stop_from_another_thread(tmp_path, splash_event):
    det = SoundDetector(
        splash_event, source=FileSource(session(tmp_path, seconds=30.0), realtime=True)
    )
    threading.Timer(0.5, det.stop).start()
    t0 = time.monotonic()
    list(det.detections())
    assert time.monotonic() - t0 < 3


def test_callback_errors_do_not_stop_detection(tmp_path, splash_event):
    det = SoundDetector(splash_event, source=FileSource(session(tmp_path), realtime=False))
    calls = []

    @det.on_detect
    def broken(d):
        calls.append(d)
        raise RuntimeError("consumer bug")

    det.run()
    assert len(calls) == 2


class FlakySource:
    """Fails to open once, then delivers audio with a dropout in the middle, then ends."""

    name = "flaky"
    sample_rate = 22050
    channels = 1

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio
        self.pos = 0
        self.opens = 0
        self.broke = False

    def open(self) -> None:
        self.opens += 1
        if self.opens == 1:
            raise CaptureError("device not ready")

    def read(self, frames: int) -> np.ndarray:
        if self.pos >= len(self.audio):
            raise EndOfStream("done")
        if not self.broke and self.pos > len(self.audio) // 2:
            self.broke = True
            raise CaptureError("device unplugged")
        block = self.audio[self.pos : self.pos + frames]
        self.pos += len(block)
        return block[:, None]

    def close(self) -> None:
        pass

    def stale(self) -> bool:
        return False


def test_reconnects_after_errors(splash_event):
    x = synth.mix(
        synth.pink_noise(8, R, -40), R, [(1.0, synth.splash(R), 1.0), (6.0, synth.splash(R), 1.0)]
    )
    src = FlakySource(resample(x, R, 22050))
    det = SoundDetector(splash_event, source=src, retry_interval=0.01)
    found = list(det.detections())
    assert src.opens == 3  # failed, worked, reopened after the dropout
    assert det.stats().reconnects == 1
    assert [round(d.start, 1) for d in found] == [1.0, 6.0]
