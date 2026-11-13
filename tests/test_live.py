"""End-to-end check on a real machine: plays a test sound and detects it through loopback.

Deselected by default (it needs a sound server and makes a sound). Run with ``pytest -m live``.
"""

from __future__ import annotations

import pytest

from sound_event_detector.selftest import run_selftest


@pytest.mark.live
def test_selftest_on_default_output():
    result = run_selftest()
    assert result.ok, result
    assert all(lat is not None and lat < 1.5 for lat in result.latencies)
