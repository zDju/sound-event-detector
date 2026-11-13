"""Log-mel spectrogram frames, computed with NumPy only."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass(frozen=True)
class FeatureParams:
    """How audio is turned into feature frames. References and live audio must share these.

    The hop is deliberately small. A short sound's correlation peak is about one hop wide, so with
    a coarse hop the best score depends on where the sound happens to fall between two frames;
    at 64 samples (2.9 ms at 22050 Hz) that jitter is negligible.
    """

    sample_rate: int = 22050
    n_fft: int = 512
    hop: int = 64
    n_mels: int = 40
    fmin: float = 300.0
    fmax: float = 8000.0

    def __post_init__(self) -> None:
        if self.fmax > self.sample_rate / 2:
            raise ValueError(f"fmax {self.fmax} Hz is above Nyquist for {self.sample_rate} Hz")
        if not 0 < self.hop <= self.n_fft:
            raise ValueError("hop must be in (0, n_fft]")

    @property
    def frame_seconds(self) -> float:
        return self.hop / self.sample_rate

    def frames_for(self, n_samples: int) -> int:
        """How many full analysis frames ``n_samples`` samples hold."""
        if n_samples < self.n_fft:
            return 0
        return 1 + (n_samples - self.n_fft) // self.hop


DEFAULT_PARAMS = FeatureParams()


def _hz_to_mel(f: np.ndarray | float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


@lru_cache(maxsize=8)
def mel_filterbank(p: FeatureParams) -> np.ndarray:
    """``(n_mels, n_fft // 2 + 1)`` triangular filters; each band spans at least one FFT bin."""
    n_bins = p.n_fft // 2 + 1
    mel_points = np.linspace(_hz_to_mel(p.fmin), _hz_to_mel(p.fmax), p.n_mels + 2)
    bins = np.floor((p.n_fft + 1) * _mel_to_hz(mel_points) / p.sample_rate).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    fb = np.zeros((p.n_mels, n_bins), dtype=np.float32)
    for i in range(p.n_mels):
        left, centre, right = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        centre = max(centre, left + 1)
        right = max(right, centre + 1)
        fb[i, left:centre] = (np.arange(left, centre) - left) / (centre - left)
        fb[i, centre:right] = (right - np.arange(centre, right)) / (right - centre)
    return fb


@lru_cache(maxsize=4)
def _window(n_fft: int) -> np.ndarray:
    return np.hanning(n_fft).astype(np.float32)


def analyze(
    samples: np.ndarray, p: FeatureParams = DEFAULT_PARAMS
) -> tuple[np.ndarray, np.ndarray]:
    """Log-mel frames and frame power of mono audio at ``p.sample_rate``.

    Returns ``(log_mel, power)``: ``log_mel`` is ``(T, n_mels)`` in dB, ``power`` is ``(T,)``, the
    mean square of each frame's raw samples (its level, before windowing).
    ``T = p.frames_for(len(samples))``.
    """
    x = np.asarray(samples, dtype=np.float32)
    n = p.frames_for(len(x))
    if n == 0:
        return np.zeros((0, p.n_mels), dtype=np.float32), np.zeros(0, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, p.n_fft)[:: p.hop][:n]
    power = np.mean(np.square(frames, dtype=np.float32), axis=1)
    spectrum = np.abs(np.fft.rfft(frames * _window(p.n_fft), axis=1)) ** 2
    mel = spectrum.astype(np.float32) @ mel_filterbank(p).T
    return (10.0 * np.log10(mel + 1e-10)).astype(np.float32), power.astype(np.float32)


def log_mel(samples: np.ndarray, p: FeatureParams = DEFAULT_PARAMS) -> np.ndarray:
    """``(T, n_mels)`` log-mel spectrogram in dB."""
    return analyze(samples, p)[0]
