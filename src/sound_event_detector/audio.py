"""WAV files and the sample-level preprocessing shared by files and live capture.

Everything here works on float32 samples in [-1, 1]. Mono audio is a 1-D array; multichannel
audio is ``(frames, channels)``.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV file as float32 samples in [-1, 1]; return ``(samples, sample_rate)``."""
    try:
        rate, data = wavfile.read(str(path))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read WAV file {path}: {e}") from e
    return to_float32(data), int(rate)


def save_wav(
    path: str | Path, samples: np.ndarray, sample_rate: int, *, float32: bool = False
) -> None:
    """Write samples as 16-bit PCM (the most widely readable format) or as 32-bit float."""
    x = np.asarray(samples, dtype=np.float32)
    data = x if float32 else (np.clip(x, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(str(path), int(sample_rate), data)


def to_float32(data: np.ndarray) -> np.ndarray:
    """Integer PCM (any width scipy returns, left-justified) or float samples -> float32."""
    if data.dtype.kind == "f":
        return data.astype(np.float32, copy=False)
    if data.dtype == np.uint8:
        return (data.astype(np.float32) - 128.0) / 128.0
    if data.dtype.kind == "i":
        return data.astype(np.float32) / float(-np.iinfo(data.dtype).min)
    raise ValueError(f"unsupported sample type {data.dtype}")


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Average the channels of ``(frames, channels)`` audio; 1-D input is returned as is."""
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        return x
    if x.ndim == 2:
        return x[:, 0].copy() if x.shape[1] == 1 else x.mean(axis=1, dtype=np.float32)
    raise ValueError(f"expected 1-D or (frames, channels) audio, got shape {x.shape}")


def resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Polyphase resampling along the first axis (anti-aliased, exact for rational ratios)."""
    x = np.asarray(samples, dtype=np.float32)
    if from_rate == to_rate or len(x) == 0:
        return x
    ratio = Fraction(int(to_rate), int(from_rate)).limit_denominator(1000)
    y = signal.resample_poly(x, ratio.numerator, ratio.denominator, axis=0)
    return np.asarray(y, dtype=np.float32)


def prepare(samples: np.ndarray, sample_rate: int, target_rate: int) -> np.ndarray:
    """Mono float32 at ``target_rate``: what the detector consumes, from files and devices alike."""
    return resample(to_mono(samples), sample_rate, target_rate)


def level_db(samples: np.ndarray) -> float:
    """RMS level in dBFS (-120 for digital silence)."""
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return max(-120.0, 20.0 * np.log10(rms + 1e-12))


def find_onset(
    samples: np.ndarray, sample_rate: int, *, rise_db: float = 15.0, frame_s: float = 0.005
) -> float:
    """Seconds into ``samples`` where the loudest sound starts.

    The onset is the first short frame whose level is ``rise_db`` above the recording's floor
    (its 10th-percentile frame level) and within 30 dB of its loudest frame, so a quiet hiss
    before the sound does not count but a soft attack does.
    """
    x = to_mono(samples)
    n = max(1, int(frame_s * sample_rate))
    usable = len(x) // n * n
    if usable == 0:
        return 0.0
    frames = x[:usable].reshape(-1, n).astype(np.float64)
    levels = 10.0 * np.log10(np.mean(frames * frames, axis=1) + 1e-12)
    floor = float(np.percentile(levels, 10))
    peak = float(levels.max())
    if peak - floor < 6.0:  # nothing stands out: the whole clip is the sound
        return 0.0
    threshold = max(floor + rise_db, peak - 30.0)
    first = int(np.argmax(levels >= threshold))
    return max(0, first - 1) * n / sample_rate  # one frame of margin before the attack


def find_end(
    samples: np.ndarray,
    sample_rate: int,
    start: float = 0.0,
    *,
    range_db: float = 30.0,
    max_length: float = 2.0,
    frame_s: float = 0.005,
) -> float:
    """Seconds into ``samples`` where the sound that begins at ``start`` has faded.

    That is the end of the last short frame within ``range_db`` of the loudest frame after
    ``start`` (plus 20 ms), but no later than ``start + max_length``.
    """
    x = to_mono(samples)
    a = int(start * sample_rate)
    b = min(len(x), a + int(max_length * sample_rate))
    n = max(1, int(frame_s * sample_rate))
    seg = x[a:b]
    usable = len(seg) // n * n
    if usable == 0:
        return b / sample_rate
    frames = seg[:usable].reshape(-1, n).astype(np.float64)
    levels = 10.0 * np.log10(np.mean(frames * frames, axis=1) + 1e-12)
    loud = np.flatnonzero(levels >= levels.max() - range_db)
    end = a + (int(loud[-1]) + 1) * n + int(0.02 * sample_rate)
    return min(end, b) / sample_rate


def trim(
    samples: np.ndarray, sample_rate: int, start: float = 0.0, length: float | None = None
) -> np.ndarray:
    """The part of ``samples`` from ``start`` seconds lasting ``length`` seconds (or to the end)."""
    if start < 0:
        raise ValueError("start must be >= 0")
    a = round(start * sample_rate)
    if a >= len(samples):
        raise ValueError(f"start {start:.3f}s is past the end ({len(samples) / sample_rate:.3f}s)")
    b = len(samples) if length is None else min(len(samples), a + round(length * sample_rate))
    if b <= a:
        raise ValueError("length must be > 0")
    return samples[a:b]
