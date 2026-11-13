from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import synth
from sound_event_detector import SoundEvent, Template

RATE = 48000


@pytest.fixture(scope="session")
def splash_event() -> SoundEvent:
    return SoundEvent("splash", (Template.from_audio(synth.splash(RATE), RATE, label="splash"),))
