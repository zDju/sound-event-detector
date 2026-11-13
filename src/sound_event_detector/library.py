"""A folder of named events, each with its reference clips and settings.

Layout (human-readable, editable by hand)::

    <library>/
      <event-name>/
        event.toml      optional: threshold, cooldown, min_level_db, mask_db,
                        pitch_tolerance, template_length
        <label>.wav     one file per reference clip (variant) of the sound

The default library lives in the user's data directory; set ``SOUND_EVENT_DETECTOR_HOME`` or
pass a path to use another.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import load_wav, save_wav, to_mono
from .features import DEFAULT_PARAMS, FeatureParams
from .matching import (
    DEFAULT_COOLDOWN,
    DEFAULT_MASK_DB,
    DEFAULT_MIN_LEVEL_DB,
    DEFAULT_THRESHOLD,
    SoundEvent,
    Template,
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SETTINGS = {
    "threshold": DEFAULT_THRESHOLD,
    "cooldown": DEFAULT_COOLDOWN,
    "min_level_db": DEFAULT_MIN_LEVEL_DB,
    "mask_db": DEFAULT_MASK_DB,
    "pitch_tolerance": 0.0,  # also match each clip played up to this much faster/slower
    "template_length": 0.0,  # use only the first seconds of each clip; 0 = all of it
}


def default_home() -> Path:
    env = os.environ.get("SOUND_EVENT_DETECTOR_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "sound-event-detector"


@dataclass(frozen=True)
class EventInfo:
    name: str
    clips: tuple[Path, ...]
    settings: dict[str, float]


def _check_name(name: str, what: str = "event name") -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid {what} {name!r}: use letters, digits, '_', '-' and '.'")


class Library:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_home()

    def path(self, name: str) -> Path:
        _check_name(name)
        return self.root / name

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and any(p.glob("*.wav")))

    def info(self, name: str) -> EventInfo:
        folder = self.path(name)
        clips = tuple(sorted(folder.glob("*.wav")))
        if not clips:
            known = ", ".join(self.names()) or "none"
            raise KeyError(f"no event {name!r} in {self.root} (known: {known})")
        return EventInfo(name, clips, self.settings(name))

    def settings(self, name: str) -> dict[str, float]:
        values = dict(SETTINGS)
        f = self.path(name) / "event.toml"
        if f.is_file():
            with f.open("rb") as fh:
                data = tomllib.load(fh)
            unknown = set(data) - set(SETTINGS)
            if unknown:
                raise ValueError(f"{f}: unknown settings {sorted(unknown)}")
            values.update({k: float(v) for k, v in data.items()})
        return values

    def update_settings(self, name: str, values: Mapping[str, float | None]) -> dict[str, float]:
        """Change some settings of an event (``None`` resets one to its default)."""
        unknown = set(values) - set(SETTINGS)
        if unknown:
            raise ValueError(f"unknown settings {sorted(unknown)}")
        folder = self.path(name)
        current = {}
        f = folder / "event.toml"
        if f.is_file():
            with f.open("rb") as fh:
                current = {k: float(v) for k, v in tomllib.load(fh).items()}
        for k, v in values.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = float(v)
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["# Settings for this event; delete a line to use the default.\n"]
        lines += [f"{k} = {current[k]!r}\n" for k in SETTINGS if k in current]
        f.write_text("".join(lines), encoding="utf-8")
        return self.settings(name)

    def add(
        self,
        name: str,
        samples: np.ndarray,
        sample_rate: int,
        *,
        label: str | None = None,
        params: FeatureParams = DEFAULT_PARAMS,
    ) -> Path:
        """Store a reference clip (already trimmed) for ``name``; returns the new file.

        The clip is checked first: it must produce a usable template.
        """
        mono = to_mono(samples)
        mask_db = self.settings(name)["mask_db"] if self.path(name).is_dir() else DEFAULT_MASK_DB
        Template.from_audio(mono, sample_rate, label=label or name, params=params, mask_db=mask_db)
        folder = self.path(name)
        base = label or name
        _check_name(base, "clip label")
        target = folder / f"{base}.wav"
        n = 2
        while target.exists():
            target = folder / f"{base}-{n}.wav"
            n += 1
        save_wav(target, mono, sample_rate, float32=True)
        return target

    def remove_clip(self, path: Path) -> None:
        path.unlink()

    def load(
        self, name: str, *, params: FeatureParams = DEFAULT_PARAMS, **overrides: float | None
    ) -> SoundEvent:
        """The event with its templates; keyword overrides (e.g. ``threshold=0.8``) win over the
        stored settings when not ``None``."""
        info = self.info(name)
        s = dict(info.settings)
        unknown = set(overrides) - set(SETTINGS)
        if unknown:
            raise ValueError(f"unknown settings {sorted(unknown)}")
        s.update({k: float(v) for k, v in overrides.items() if v is not None})
        templates = tuple(
            t
            for c in info.clips
            for t in Template.variants(
                *load_wav(c),
                label=c.stem,
                params=params,
                mask_db=s["mask_db"],
                pitch_tolerance=s["pitch_tolerance"],
                length=s["template_length"] or None,
            )
        )
        return SoundEvent(name, templates, s["threshold"], s["cooldown"], s["min_level_db"])
