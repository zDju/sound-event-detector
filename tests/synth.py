"""Synthetic audio for tests: distinctive short sounds, background noise and 'music'.

Nothing here comes from a real recording, so the fixtures can live in a public repository.
"""

from __future__ import annotations

import numpy as np

from sound_event_detector.selftest import make_test_sound


def _t(seconds: float, rate: int) -> np.ndarray:
    return np.arange(round(seconds * rate)) / rate


def _bandpassed_noise(
    n: int, rate: int, lo: float, hi: float, rng: np.random.Generator
) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / rate)
    spec[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(spec, n)
    return y / (np.abs(y).max() + 1e-12)


def splash(rate: int = 48000, seed: int = 1) -> np.ndarray:
    """A 0.7 s 'splash': noise burst, rising chirp and a late blip. Peak amplitude 0.5.

    The same sound ``sounddetector selftest`` plays; ``seed`` varies the noise burst.
    """
    return make_test_sound(rate, seed)


def bell(rate: int = 48000) -> np.ndarray:
    """A 0.7 s bell with a click: a different short sound, for negative tests."""
    t = _t(0.7, rate)
    y = sum(
        a * np.sin(2 * np.pi * f * t)
        for a, f in [(1.0, 523), (0.6, 1046), (0.4, 1569), (0.2, 2615)]
    )
    y = y * np.exp(-t / 0.2)
    y[: int(0.002 * rate)] += 0.8
    return (0.5 * y / np.abs(y).max()).astype(np.float32)


def falling_chirp(rate: int = 48000) -> np.ndarray:
    """The splash's chirp played downwards, with a burst: similar ingredients, different sound."""
    rng = np.random.default_rng(7)
    t = _t(0.7, rate)
    env = np.minimum(t / 0.005, 1.0) * np.exp(-t / 0.25)
    chirp = np.sin(2 * np.pi * (2400 * t - (2400 - 600) / (2 * 0.7) * t * t))
    burst = _bandpassed_noise(len(t), rate, 2000, 5000, rng) * np.exp(-t / 0.05)
    y = env * chirp + 0.8 * burst
    return (0.5 * y / np.abs(y).max()).astype(np.float32)


def pink_noise(
    seconds: float, rate: int = 48000, level_db: float = -30.0, seed: int = 2
) -> np.ndarray:
    """Pink (1/f) noise at an RMS of ``level_db`` dBFS."""
    rng = np.random.default_rng(seed)
    n = round(seconds * rate)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / rate)
    spec[1:] /= np.sqrt(f[1:])
    spec[0] = 0
    y = np.fft.irfft(spec, n)
    y *= 10 ** (level_db / 20) / (np.sqrt(np.mean(y * y)) + 1e-12)
    return y.astype(np.float32)


def music(seconds: float, rate: int = 48000, level_db: float = -25.0, seed: int = 3) -> np.ndarray:
    """Random harmonic notes, 0.25 s each: tonal, ever-changing background."""
    rng = np.random.default_rng(seed)
    note = int(0.25 * rate)
    t = np.arange(note) / rate
    env = np.minimum(t / 0.01, 1.0) * np.exp(-t / 0.15)
    parts = []
    for _ in range(int(np.ceil(seconds * rate / note))):
        f0 = 110 * 2 ** (rng.integers(0, 36) / 12)
        parts.append(env * sum(np.sin(2 * np.pi * k * f0 * t) / k for k in range(1, 6)))
    y = np.concatenate(parts)[: round(seconds * rate)]
    y *= 10 ** (level_db / 20) / (np.sqrt(np.mean(y * y)) + 1e-12)
    return y.astype(np.float32)


def mix(
    background: np.ndarray, rate: int, inserts: list[tuple[float, np.ndarray, float]]
) -> np.ndarray:
    """``background`` with each ``(time_s, sound, gain)`` added in at ``time_s``."""
    y = background.astype(np.float32).copy()
    for at, sound, gain in inserts:
        a = round(at * rate)
        b = min(len(y), a + len(sound))
        y[a:b] += gain * sound[: b - a]
    return y
