"""Check the whole chain on this machine: play a test sound, see whether the detector hears it.

Playback goes through SoundCard to an output device, so this verifies loopback capture and
detection end to end on any platform SoundCard supports (``sounddetector selftest``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .capture import CaptureError, DeviceSource, _soundcard, open_source
from .detector import SoundDetector
from .matching import Detection, SoundEvent, Template

RATE = 48000
PLAN = ((0.6, 1.0), (2.4, 0.3))  # (offset in seconds, gain) of each test sound
LENGTH = 4.2  # seconds of playback


def make_test_sound(rate: int = RATE, seed: int = 1) -> np.ndarray:
    """A synthetic 0.7 s 'splash' (noise burst, rising chirp, late blip); peak amplitude 0.5."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(0.7 * rate)) / rate
    env = np.minimum(t / 0.005, 1.0) * np.exp(-t / 0.25)
    chirp = np.sin(2 * np.pi * (600 * t + (2400 - 600) / (2 * 0.7) * t * t))
    spec = np.fft.rfft(rng.standard_normal(len(t)))
    f = np.fft.rfftfreq(len(t), 1 / rate)
    spec[(f < 2000) | (f > 5000)] = 0
    noise = np.fft.irfft(spec, len(t))
    burst = noise / (np.abs(noise).max() + 1e-12) * np.exp(-t / 0.05)
    blip = np.sin(2 * np.pi * 1500 * t) * np.exp(-(((t - 0.35) / 0.03) ** 2))
    y = env * chirp + 0.8 * burst + 0.6 * blip
    return (0.5 * y / np.abs(y).max()).astype(np.float32)


@dataclass(frozen=True)
class SelftestResult:
    source: str
    speaker: str
    played: tuple[tuple[float, float], ...]  # (offset, gain)
    heard: tuple[Detection | None, ...]  # the detection of each played sound, if any
    latencies: tuple[float | None, ...]  # seconds from the sound's playback to its detection
    extra: tuple[Detection, ...]  # detections that match no played sound

    @property
    def ok(self) -> bool:
        return all(d is not None for d in self.heard) and not self.extra


def _speaker_for(sc: Any, device: str | None) -> Any:
    if device is None:
        return sc.default_speaker()
    q = device.lower().removesuffix(".monitor")
    for s in sc.all_speakers():
        if q in str(s.id).lower() or q in str(s.name).lower():
            return s
    raise CaptureError(f"no output device matches {device!r} to play the test sound on")


def run_selftest(
    source: str | None = None, *, volume: float = 0.3, connect_timeout: float = 5.0
) -> SelftestResult:
    """Play two test sounds (one at -10 dB) on the output device and detect them."""
    sound = make_test_sound()
    event = SoundEvent(
        "selftest",
        (Template.from_audio(sound, RATE, label="selftest"),),
        threshold=0.75,
        cooldown=0.5,
    )
    src = open_source(source, sample_rate=event.params.sample_rate)
    if not isinstance(src, DeviceSource):
        raise CaptureError("the self-test plays through an output device; use a device source")
    sc = _soundcard()
    speaker = _speaker_for(sc, src.device)
    signal = np.zeros(round(LENGTH * RATE), np.float32)
    for offset, gain in PLAN:
        a = round(offset * RATE)
        signal[a : a + len(sound)] += gain * volume * sound

    found: list[Detection] = []
    detector = SoundDetector(event, source=src)
    detector.on_detect(found.append)
    detector.start()
    try:
        if not detector.connected.wait(connect_timeout):
            raise CaptureError(f"could not start capturing from {src.name}")
        time.sleep(0.3)
        t0 = time.time()
        speaker.play(signal, samplerate=RATE)
        time.sleep(1.0)  # let the last detection arrive
    finally:
        detector.stop()

    heard: list[Detection | None] = []
    latencies: list[float | None] = []
    unused = list(found)
    for offset, _ in PLAN:
        expected = t0 + offset
        match = next(
            (
                d
                for d in unused
                if d.timestamp is not None and 0 <= d.timestamp.timestamp() - expected < 2.0
            ),
            None,
        )
        heard.append(match)
        if match is not None and match.timestamp is not None:
            unused.remove(match)
            latencies.append(match.timestamp.timestamp() - expected)
        else:
            latencies.append(None)
    return SelftestResult(
        src.name,
        str(speaker.name),
        PLAN,
        tuple(heard),
        tuple(latencies),
        tuple(unused),
    )
