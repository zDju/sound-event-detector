"""Reference templates, and the streaming matcher that finds them in audio.

Similarity is a masked, normalized cross-correlation of log-mel frames:

1. the reference's log-mel spectrogram is reduced to its *active* cells, those within
   ``mask_db`` of its loudest cell. Quieter parts of a reference (a decaying tail, the room
   behind it) are exactly where background sound takes over in real use, so they are not
   compared; rows with no active cell are cut from both ends of the template;
2. in every frame, the active cells of the reference and of the incoming window have their mean
   removed, which cancels playback volume and any flat background;
3. the score is the cosine of the angle between the two sets of cells (1 = same shape,
   0 = unrelated), computed for a window ending at every new analysis frame.

The score is a similarity, not a probability. What separates real events from everything else
depends on the sound and the background, so thresholds come from measurement (see :func:`scan`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import as_strided
from scipy import signal

from .audio import load_wav, prepare, resample, to_mono, trim
from .features import DEFAULT_PARAMS, FeatureParams, analyze

DEFAULT_THRESHOLD = 0.75
DEFAULT_COOLDOWN = 3.0  # seconds
DEFAULT_MIN_LEVEL_DB = -50.0  # dBFS; quieter windows are not scored
DEFAULT_MASK_DB = 30.0  # reference cells this far below its loudest cell are ignored
PEAK_HOLD_SECONDS = 0.02  # after crossing the threshold, wait this long for a higher peak
PITCH_STEP = 0.02  # speed step between pitch variants of a reference


def _center(log_mel: np.ndarray) -> np.ndarray:
    return log_mel - log_mel.mean(axis=1, keepdims=True)


def _tail(a: np.ndarray, n: int) -> np.ndarray:
    """The last ``n`` rows of ``a`` (all of them if it is shorter; none if ``n`` is 0)."""
    return a[max(0, len(a) - n) :] if n > 0 else a[:0]


def _skew_sum(rows: np.ndarray, pending: np.ndarray) -> np.ndarray:
    """``out[m] = pending[m] + sum(rows[k, j] for k + j == m)``, of length ``n + L - 1``.

    Row ``k`` holds what frame ``k`` of a block adds to the windows ending ``0..L-1`` frames
    after it; ``pending`` holds the partial sums of the next ``L - 1`` windows from earlier
    frames. Writing each row one place further right and summing the columns adds it all up.
    """
    n, length = rows.shape
    skewed = np.zeros((n, n + length - 1))
    s0, s1 = skewed.strides
    as_strided(skewed, shape=(n, length), strides=(s0 + s1, s1))[...] = rows
    out = skewed.sum(axis=0)
    out[: length - 1] += pending
    return out


def pitch_rates(tolerance: float, step: float = PITCH_STEP) -> list[float]:
    """Playback speeds from ``1 - tolerance`` to ``1 + tolerance`` in ``step`` (1.0 included).

    Many games and applications play a sound effect slightly faster or slower each time (which
    shifts its pitch) so that repetitions do not sound identical. Matching against references
    resampled to these speeds tolerates that.
    """
    if tolerance < 0:
        raise ValueError("pitch tolerance must be >= 0")
    n = math.floor(tolerance / step + 1e-9)
    return [round(1.0 + k * step, 6) for k in range(-n, n + 1)]


@dataclass(frozen=True, eq=False)
class Template:
    """One reference recording, reduced to the cells the matcher compares against."""

    label: str
    features: np.ndarray  # (frames, n_mels): active cells centred per frame, unit norm; 0 elsewhere
    mask: np.ndarray  # (frames, n_mels) float 0/1: the active cells
    counts: np.ndarray  # (frames,) active cells per frame (at least 1, to divide by)
    duration: float  # seconds of audio the template spans, from its first to its last active frame
    offset: float = 0.0  # seconds cut from the start of the reference (quiet lead-in)
    params: FeatureParams = DEFAULT_PARAMS
    mask_db: float = DEFAULT_MASK_DB

    @property
    def frames(self) -> int:
        return len(self.features)

    @classmethod
    def from_audio(
        cls,
        samples: np.ndarray,
        sample_rate: int,
        *,
        label: str = "reference",
        params: FeatureParams = DEFAULT_PARAMS,
        mask_db: float = DEFAULT_MASK_DB,
    ) -> Template:
        if mask_db <= 0:
            raise ValueError("mask_db must be > 0")
        mel, _ = analyze(prepare(samples, sample_rate, params.sample_rate), params)
        if len(mel) < 1:
            raise ValueError(
                f"reference {label!r} is too short "
                f"(needs at least {params.n_fft} samples at {params.sample_rate} Hz)"
            )
        mel = mel.astype(np.float64)
        mask = mel >= mel.max() - mask_db
        mask[mask.sum(axis=1) < 2] = False  # a frame needs two cells to have a shape
        rows = np.flatnonzero(mask.any(axis=1))
        if len(rows) == 0:
            raise ValueError(f"reference {label!r} is silent or has no spectral detail")
        first, last = int(rows[0]), int(rows[-1])
        mel, mask = mel[first : last + 1], mask[first : last + 1]
        counts = np.maximum(mask.sum(axis=1), 1)
        mean = (mel * mask).sum(axis=1) / counts
        feats = (mel - mean[:, None]) * mask
        norm = float(np.linalg.norm(feats))
        if norm < 1e-6:
            raise ValueError(f"reference {label!r} has no spectral detail")
        duration = ((last - first) * params.hop + params.n_fft) / params.sample_rate
        return cls(
            label,
            feats / norm,
            mask.astype(np.float64),
            counts.astype(np.float64),
            duration,
            first * params.hop / params.sample_rate,
            params,
            mask_db,
        )

    @classmethod
    def variants(
        cls,
        samples: np.ndarray,
        sample_rate: int,
        *,
        label: str = "reference",
        params: FeatureParams = DEFAULT_PARAMS,
        mask_db: float = DEFAULT_MASK_DB,
        pitch_tolerance: float = 0.0,
        length: float | None = None,
    ) -> tuple[Template, ...]:
        """Templates of one reference: its first ``length`` seconds (all of it if ``None``),
        played at each speed of :func:`pitch_rates` (labelled ``label@speed``)."""
        sr = params.sample_rate
        x = prepare(samples, sample_rate, sr)
        if length:
            x = x[: round(length * sr)]
        out = []
        for k in pitch_rates(pitch_tolerance):
            v = x if k == 1.0 else resample(x, sr, round(sr / k))
            name = label if k == 1.0 else f"{label}@{k:.2f}"
            out.append(cls.from_audio(v, sr, label=name, params=params, mask_db=mask_db))
        return tuple(out)

    @classmethod
    def from_wav(
        cls,
        path: str | Path,
        *,
        label: str | None = None,
        start: float = 0.0,
        length: float | None = None,
        params: FeatureParams = DEFAULT_PARAMS,
        mask_db: float = DEFAULT_MASK_DB,
    ) -> Template:
        samples, rate = load_wav(path)
        if start or length is not None:
            samples = trim(samples, rate, start, length)
        return cls.from_audio(
            samples, rate, label=label or Path(path).stem, params=params, mask_db=mask_db
        )


@dataclass(frozen=True)
class SoundEvent:
    """A named sound to detect: one or more reference templates and the rules for reporting it.

    The event fires when any of its templates matches; several templates cover a sound that has
    variants (e.g. a game playing one of three splash recordings at random).
    """

    name: str
    templates: tuple[Template, ...]
    threshold: float = DEFAULT_THRESHOLD
    cooldown: float = DEFAULT_COOLDOWN
    min_level_db: float = DEFAULT_MIN_LEVEL_DB

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("event name must not be empty")
        if not self.templates:
            raise ValueError(f"event {self.name!r} has no reference templates")
        if len({t.params for t in self.templates}) != 1:
            raise ValueError(f"templates of event {self.name!r} use different feature parameters")
        if not -1.0 < self.threshold <= 1.0:
            raise ValueError("threshold must be in (-1, 1]")
        if self.cooldown < 0:
            raise ValueError("cooldown must be >= 0")

    @property
    def params(self) -> FeatureParams:
        return self.templates[0].params

    @property
    def duration(self) -> float:
        """Seconds of the longest template: how much of the sound must be heard to detect it."""
        return max(t.duration for t in self.templates)

    @classmethod
    def from_wav(
        cls,
        *paths: str | Path,
        name: str | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        cooldown: float = DEFAULT_COOLDOWN,
        min_level_db: float = DEFAULT_MIN_LEVEL_DB,
        params: FeatureParams = DEFAULT_PARAMS,
        mask_db: float = DEFAULT_MASK_DB,
        pitch_tolerance: float = 0.0,
        template_length: float | None = None,
    ) -> SoundEvent:
        """An event from one or more reference WAV files.

        Each file is used whole, or its first ``template_length`` seconds; with a
        ``pitch_tolerance`` (e.g. 0.06 for ±6 %) it is also matched played faster and slower.
        """
        if not paths:
            raise ValueError("at least one reference file is required")
        templates = tuple(
            t
            for p in paths
            for t in Template.variants(
                *load_wav(p),
                label=Path(p).stem,
                params=params,
                mask_db=mask_db,
                pitch_tolerance=pitch_tolerance,
                length=template_length,
            )
        )
        return cls(name or Path(paths[0]).stem, templates, threshold, cooldown, min_level_db)


@dataclass(frozen=True)
class Detection:
    """One occurrence of an event. Times are seconds of audio since the stream started."""

    name: str
    similarity: float  # peak score in [-1, 1]; not a probability
    start: float  # where the matched sound began
    end: float  # where the matched window ended (start + template duration)
    detected_at: float  # how much audio had been consumed when the detection was reported
    template: str  # label of the reference that matched best (``name@speed`` for pitch variants)
    level_db: float  # level of the matched window, dBFS (a nearby source is louder than a far one)
    timestamp: datetime | None = None  # wall-clock time of the report (live detection only)

    @property
    def latency(self) -> float:
        """Seconds from the start of the sound to its detection, in stream time."""
        return self.detected_at - self.start


@dataclass
class _Armed:
    similarity: float
    start: float
    end: float
    template: str
    level_db: float
    since_peak: int = 0


class Matcher:
    """Streaming detector core with no I/O: feed it mono audio, get :class:`Detection` objects.

    It keeps only the last few hundred feature frames, so memory stays bounded however long it
    runs. Results do not depend on how the audio is split into chunks.
    """

    def __init__(
        self,
        events: Sequence[SoundEvent],
        *,
        peak_hold: float = PEAK_HOLD_SECONDS,
    ) -> None:
        if not events:
            raise ValueError("at least one event is required")
        names = [e.name for e in events]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate event names: {names}")
        if len({e.params for e in events}) != 1:
            raise ValueError("all events must use the same feature parameters")
        self.events = tuple(events)
        self.params = events[0].params
        self._hold_frames = max(1, round(peak_hold / self.params.frame_seconds))
        self._max_frames = max(t.frames for e in events for t in e.templates)
        self._consumed = 0  # samples fed since construction (the stream clock)
        self._cooldown_until = dict.fromkeys(names, -math.inf)
        self._peaks = dict.fromkeys(names, -1.0)
        self.reset()

    @property
    def sample_rate(self) -> int:
        return self.params.sample_rate

    @property
    def time(self) -> float:
        """Seconds of audio consumed so far."""
        return self._consumed / self.params.sample_rate

    def reset(self, *, keep_cooldown: bool = True) -> None:
        """Forget buffered audio, e.g. after a capture gap; the stream clock keeps running."""
        self._pending = np.zeros(0, dtype=np.float32)
        self._power = np.zeros(0, dtype=np.float64)  # mean square of each frame's samples
        # A window's score is a sum, over its frames, of each frame's dot product with the
        # template row it lines up with (numerator) and of its spread over that row's active
        # cells (denominator). Each new frame adds its terms to the windows it belongs to at
        # once; per template, only the partial sums of the next L - 1 windows are kept.
        self._pend_num: dict[tuple[int, int], np.ndarray] = {}
        self._pend_var: dict[tuple[int, int], np.ndarray] = {}
        for ei, ev in enumerate(self.events):
            for ti, tpl in enumerate(ev.templates):
                self._pend_num[ei, ti] = np.zeros(tpl.frames - 1)
                self._pend_var[ei, ti] = np.zeros(tpl.frames - 1)
        self._origin = self._consumed  # stream sample index of frame 0 of this segment
        self._frames = 0  # frames produced in this segment
        self._armed: dict[str, _Armed] = {}
        self._latched = dict.fromkeys(self._cooldown_until, False)
        if not keep_cooldown:
            self._cooldown_until = dict.fromkeys(self._cooldown_until, -math.inf)
        self.reset_last()

    def take_peaks(self) -> dict[str, float]:
        """Each event's highest score since the previous call (-1: nothing was scored)."""
        peaks, self._peaks = self._peaks, dict.fromkeys(self._peaks, -1.0)
        return peaks

    def feed(self, samples: np.ndarray) -> list[Detection]:
        """Consume mono audio at ``self.sample_rate``; return detections completed by it.

        After the call, ``last_times`` holds the end time of each new analysis window and
        ``last_scores[name]`` the event's score for it (NaN: too quiet, or not enough audio yet).
        """
        p = self.params
        x = to_mono(np.asarray(samples, dtype=np.float32))
        self._consumed += len(x)
        buf = np.concatenate([self._pending, x])
        n_new = p.frames_for(len(buf))
        if n_new == 0:
            self._pending = buf
            self.reset_last()
            return []
        mel, power = analyze(buf[: (n_new - 1) * p.hop + p.n_fft], p)
        self._pending = buf[n_new * p.hop :]
        # Centring each frame does not change any score (active cells are re-centred per row)
        # but keeps the numbers small, so the variance sums below do not lose precision.
        wc = _center(mel).astype(np.float64)
        wc2 = wc * wc
        old_p = len(self._power)
        powers = np.concatenate([self._power, power.astype(np.float64)])
        cum_power = np.concatenate([[0.0], np.cumsum(powers)])
        first = self._frames  # segment index of the first new frame
        self._frames += n_new
        seg_index = np.arange(first, first + n_new)
        times = (self._origin + seg_index * p.hop + p.n_fft) / p.sample_rate

        detections: list[Detection] = []
        for ei, ev in enumerate(self.events):
            best = np.full(n_new, np.nan, dtype=np.float64)
            label = np.full(n_new, -1, dtype=np.int16)
            levels = np.full(n_new, np.nan, dtype=np.float64)
            for ti, tpl in enumerate(ev.templates):
                length = tpl.frames
                sums = wc @ tpl.mask.T
                spread = wc2 @ tpl.mask.T - sums * sums / tpl.counts
                num_all = _skew_sum((wc @ tpl.features.T)[:, ::-1], self._pend_num[ei, ti])
                var_all = _skew_sum(spread[:, ::-1], self._pend_var[ei, ti])
                self._pend_num[ei, ti] = num_all[n_new:]
                self._pend_var[ei, ti] = var_all[n_new:]
                j = np.flatnonzero(seg_index >= length - 1)  # windows with all frames heard
                if len(j) == 0:
                    continue
                num, var = num_all[j], var_all[j]
                score = np.full(len(j), np.nan)
                ok = var > 1e-6
                score[ok] = np.clip(num[ok] / np.sqrt(var[ok]), -1.0, 1.0)
                pe = old_p + j  # the same frames in the power buffer
                power_mean = (cum_power[pe + 1] - cum_power[pe + 1 - length]) / length
                level = 10.0 * np.log10(power_mean + 1e-12)
                score[level < ev.min_level_db] = np.nan
                current = best[j]
                take = ~np.isnan(score) & (np.isnan(current) | (score > current))
                best[j[take]] = score[take]
                label[j[take]] = ti
                levels[j[take]] = level[take]
            self.last_scores[ev.name] = best.astype(np.float32)
            self.last_labels[ev.name] = label
            self.last_levels[ev.name] = levels.astype(np.float32)
            if not np.all(np.isnan(best)):
                self._peaks[ev.name] = max(self._peaks[ev.name], float(np.nanmax(best)))
            self._pick_peaks(ev, best, label, levels, seg_index, times, detections)
        self.last_times = times
        self._power = _tail(powers, self._max_frames - 1)
        detections.sort(key=lambda d: d.detected_at)
        return detections

    def flush(self) -> list[Detection]:
        """Report detections still waiting for their peak (call at the end of a recording)."""
        out = [self._emit(name, armed, self.time) for name, armed in list(self._armed.items())]
        return sorted(out, key=lambda d: d.detected_at)

    def reset_last(self) -> None:
        self.last_times = np.zeros(0, dtype=np.float64)
        self.last_scores = {e.name: np.zeros(0, dtype=np.float32) for e in self.events}
        self.last_labels = {e.name: np.zeros(0, dtype=np.int16) for e in self.events}
        self.last_levels = {e.name: np.zeros(0, dtype=np.float32) for e in self.events}

    def _pick_peaks(
        self,
        ev: SoundEvent,
        scores: np.ndarray,
        labels: np.ndarray,
        levels: np.ndarray,
        seg_index: np.ndarray,
        times: np.ndarray,
        out: list[Detection],
    ) -> None:
        name = ev.name
        if (
            name not in self._armed
            and not self._latched[name]
            and not np.any(scores >= ev.threshold)
        ):
            return
        p = self.params
        for j in range(len(scores)):
            s = scores[j]
            t_end = float(times[j])
            armed = self._armed.get(name)
            if not s >= ev.threshold:  # below threshold, or NaN
                self._latched[name] = False
                if armed is not None:
                    out.append(self._emit(name, armed, t_end))
                continue
            tpl = ev.templates[labels[j]]
            start = (self._origin + (int(seg_index[j]) - tpl.frames + 1) * p.hop) / p.sample_rate
            level = float(levels[j])
            if armed is not None:
                if s > armed.similarity:
                    self._armed[name] = _Armed(float(s), start, t_end, tpl.label, level)
                else:
                    armed.since_peak += 1
                    if armed.since_peak >= self._hold_frames:
                        out.append(self._emit(name, armed, t_end))
            elif not self._latched[name] and t_end >= self._cooldown_until[name]:
                self._armed[name] = _Armed(float(s), start, t_end, tpl.label, level)

    def _emit(self, name: str, armed: _Armed, now: float) -> Detection:
        del self._armed[name]
        # Stay quiet until the score drops below the threshold again, so one long match is
        # reported once even with no cooldown.
        self._latched[name] = True
        ev = next(e for e in self.events if e.name == name)
        self._cooldown_until[name] = now + ev.cooldown
        return Detection(
            name, armed.similarity, armed.start, armed.end, now, armed.template, armed.level_db
        )


@dataclass(frozen=True)
class Peak:
    """A local maximum of an event's score in a recording."""

    start: float  # seconds into the recording where the matched sound begins
    score: float
    level_db: float  # level of the matched window, dBFS
    template: str


@dataclass(frozen=True)
class ScanResult:
    """Offline detection over a whole recording, with the score trace for diagnostics."""

    detections: list[Detection]
    times: np.ndarray  # end time of each analysis window, seconds
    scores: dict[str, np.ndarray]  # per event; NaN where the audio was too quiet
    labels: dict[str, np.ndarray]  # per event: index of the best template at each window
    levels: dict[str, np.ndarray]  # per event: level of that template's window, dBFS
    events: tuple[SoundEvent, ...] = field(repr=False)
    duration: float = 0.0

    def peaks(self, name: str, *, min_distance: float | None = None) -> list[Peak]:
        """The score's local maxima, strongest first.

        Peaks closer than ``min_distance`` seconds (default: the event's cooldown, at least its
        duration) count as one. Used to choose a threshold: real events should be the top peaks.
        """
        ev = next(e for e in self.events if e.name == name)
        s = np.nan_to_num(self.scores[name], nan=-1.0)
        if len(s) == 0:
            return []
        gap = max(ev.duration, ev.cooldown) if min_distance is None else min_distance
        distance = max(1, round(gap / ev.params.frame_seconds))
        idx, _ = signal.find_peaks(np.concatenate([[-1.0], s, [-1.0]]), distance=distance)
        idx = idx - 1
        idx = idx[s[idx] > -1.0]
        out = []
        for i in idx[np.argsort(-s[idx], kind="stable")]:
            tpl = ev.templates[max(0, int(self.labels[name][i]))]
            start = float(self.times[i] - tpl.duration)
            out.append(Peak(start, float(s[i]), float(self.levels[name][i]), tpl.label))
        return out


def scan(
    samples: np.ndarray,
    sample_rate: int,
    events: Sequence[SoundEvent],
    *,
    chunk_seconds: float = 1.0,
) -> ScanResult:
    """Find events in a recording, using exactly the streaming matcher the live detector uses."""
    matcher = Matcher(events)
    x = prepare(samples, sample_rate, matcher.sample_rate)
    step = max(1, int(chunk_seconds * matcher.sample_rate))
    detections: list[Detection] = []
    times: list[np.ndarray] = []
    names = [e.name for e in events]
    scores: dict[str, list[np.ndarray]] = {n: [] for n in names}
    labels: dict[str, list[np.ndarray]] = {n: [] for n in names}
    levels: dict[str, list[np.ndarray]] = {n: [] for n in names}
    for i in range(0, len(x), step):
        detections += matcher.feed(x[i : i + step])
        times.append(matcher.last_times)
        for n in names:
            scores[n].append(matcher.last_scores[n])
            labels[n].append(matcher.last_labels[n])
            levels[n].append(matcher.last_levels[n])
    detections += matcher.flush()

    def cat(parts: list[np.ndarray], dtype: type) -> np.ndarray:
        return np.concatenate(parts) if parts else np.zeros(0, dtype=dtype)

    return ScanResult(
        detections,
        cat(times, np.float64),
        {k: cat(v, np.float32) for k, v in scores.items()},
        {k: cat(v, np.int16) for k, v in labels.items()},
        {k: cat(v, np.float32) for k, v in levels.items()},
        tuple(events),
        len(x) / matcher.sample_rate,
    )


def suggest_threshold(scores: Sequence[float], expected: int) -> tuple[float, float] | None:
    """Threshold halfway between the ``expected``-th strongest peak and the next one.

    ``scores`` are peak scores, strongest first, of a recording known to hold ``expected``
    events. Returns ``(threshold, margin)`` where margin is the gap between the weakest real
    event and the strongest non-event; ``None`` when there are fewer peaks than expected.
    """
    if expected < 1 or len(scores) < expected:
        return None
    weakest = scores[expected - 1]
    if len(scores) == expected:
        return max(0.0, weakest - 0.1), math.inf
    strongest_other = scores[expected]
    return (weakest + strongest_other) / 2.0, weakest - strongest_other
