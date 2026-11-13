"""Offline detection on synthetic recordings: matches, volumes, noise, negatives, rates."""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

import synth
from sound_event_detector import Matcher, SoundEvent, Template, scan, suggest_threshold
from sound_event_detector.audio import resample

R = 48000
SPLASH = synth.splash(R)


def starts(result):
    return [round(d.start, 2) for d in result.detections]


def test_exact_matches_in_silence(splash_event):
    x = synth.mix(np.zeros(R * 10, np.float32), R, [(2.0, SPLASH, 1.0), (6.5, SPLASH, 1.0)])
    r = scan(x, R, [splash_event])
    assert starts(r) == [2.0, 6.5]
    assert all(d.similarity > 0.99 for d in r.detections)


@pytest.mark.parametrize("gain", [0.03, 0.2, 1.0, 1.9])
def test_volume_does_not_matter(splash_event, gain):
    x = synth.mix(synth.pink_noise(5, R, -75), R, [(2.0, SPLASH, gain)])
    r = scan(np.clip(x, -1, 1), R, [splash_event])
    assert starts(r) == [2.0]
    assert r.detections[0].similarity > 0.95


@pytest.mark.parametrize(
    ("background", "min_score"),
    [
        (synth.pink_noise(10, R, -21), 0.95),  # SNR about 10 dB
        (synth.pink_noise(10, R, -16), 0.9),  # about 5 dB
        (synth.music(10, R, -22), 0.9),
        (synth.music(10, R, -16), 0.85),
    ],
)
def test_noise_and_music(splash_event, background, min_score):
    x = synth.mix(background, R, [(3.0, SPLASH, 1.0), (7.0, SPLASH, 1.0)])
    r = scan(x, R, [splash_event])
    assert starts(r) == [3.0, 7.0]
    assert min(d.similarity for d in r.detections) > min_score


@pytest.mark.parametrize(
    "x",
    [
        synth.pink_noise(10, R, -20),
        synth.music(10, R, -18),
        synth.mix(synth.music(10, R, -25), R, [(2.0, synth.bell(R), 1.0)]),
        synth.mix(synth.pink_noise(10, R, -60), R, [(2.0, synth.falling_chirp(R), 1.0)]),
        np.zeros(R * 5, np.float32),
    ],
    ids=["pink", "music", "bell", "falling-chirp", "silence"],
)
def test_no_false_detections(splash_event, x):
    r = scan(x, R, [splash_event])
    assert r.detections == []
    best = max((s for _, s in r.peaks("splash")), default=-1.0)
    assert best < splash_event.threshold - 0.1  # a clear margin, not a near miss


@pytest.mark.parametrize("rate", [44100, 22050, 16000])
def test_recording_at_another_sample_rate(splash_event, rate):
    x = synth.mix(synth.pink_noise(6, R, -40), R, [(2.5, SPLASH, 1.0)])
    r = scan(resample(x, R, rate), rate, [splash_event])
    assert starts(r) == [2.5]
    assert r.detections[0].similarity > 0.9


def test_reference_at_another_sample_rate():
    ev = SoundEvent("s", (Template.from_audio(resample(SPLASH, R, 44100), 44100),))
    x = synth.mix(synth.pink_noise(5, R, -40), R, [(1.0, SPLASH, 1.0)])
    r = scan(x, R, [ev])
    assert starts(r) == [1.0] and r.detections[0].similarity > 0.95


def test_stereo_recording(splash_event):
    mono = synth.mix(synth.pink_noise(5, R, -40), R, [(1.5, SPLASH, 1.0)])
    stereo = np.stack([mono, 0.5 * mono], axis=1)
    assert starts(scan(stereo, R, [splash_event])) == [1.5]


def test_cooldown_and_single_report(splash_event):
    x = synth.mix(np.zeros(R * 8, np.float32), R, [(1.0, SPLASH, 1.0), (2.0, SPLASH, 1.0)])
    assert starts(scan(x, R, [splash_event])) == [1.0]  # second is inside the 3 s cooldown
    no_cooldown = replace(splash_event, cooldown=0.0)
    assert starts(scan(x, R, [no_cooldown])) == [1.0, 2.0]


def test_streaming_is_independent_of_chunking(splash_event):
    x = synth.mix(synth.music(6, R, -25), R, [(1.0, SPLASH, 1.0), (4.2, SPLASH, 0.3)])
    x = resample(x, R, 22050)
    reference = None
    for chunk in [1, 63, 64, 1000, 22050, len(x)]:
        m = Matcher([splash_event])
        dets, scores = [], []
        for i in range(0, len(x), chunk):
            dets += m.feed(x[i : i + chunk])
            scores.append(m.last_scores["splash"])
        dets += m.flush()
        got = ([(d.start, round(d.similarity, 5)) for d in dets], np.concatenate(scores))
        if reference is None:
            reference = got
        else:
            assert got[0] == reference[0]
            np.testing.assert_allclose(got[1], reference[1], atol=1e-5, equal_nan=True)


def test_detection_timing(splash_event):
    ev = splash_event
    x = synth.mix(np.zeros(R * 4, np.float32), R, [(1.0, SPLASH, 1.0)])
    d = scan(x, R, [ev]).detections[0]
    assert abs(d.start - 1.0) < 0.005
    assert abs(d.end - (1.0 + ev.duration)) < 0.005
    assert 0 <= d.detected_at - d.end <= 0.03  # reported within the 20 ms peak hold
    assert d.latency == pytest.approx(d.detected_at - d.start)


def test_multiple_events_and_variants():
    splash = Template.from_audio(SPLASH, R, label="splash")
    other = Template.from_audio(synth.splash(R, seed=9), R, label="splash-b")
    bell = SoundEvent("bell", (Template.from_audio(synth.bell(R), R, label="bell"),))
    events = [SoundEvent("splash", (splash, other)), bell]
    x = synth.mix(
        synth.pink_noise(10, R, -45),
        R,
        [(1.0, SPLASH, 1.0), (4.0, synth.bell(R), 1.0), (7.0, synth.splash(R, seed=9), 1.0)],
    )
    r = scan(x, R, events)
    assert [(d.name, round(d.start, 1), d.template) for d in r.detections] == [
        ("splash", 1.0, "splash"),
        ("bell", 4.0, "bell"),
        ("splash", 7.0, "splash-b"),
    ]


def test_quiet_audio_is_not_scored(splash_event):
    x = synth.mix(np.zeros(R * 3, np.float32), R, [(1.0, SPLASH, 10 ** (-60 / 20))])
    r = scan(x, R, [splash_event])
    assert r.detections == [] and np.all(np.isnan(r.scores["splash"]))


def test_template_trims_inactive_rows():
    padded = np.concatenate([np.zeros(R // 2, np.float32), SPLASH, np.zeros(R // 2, np.float32)])
    tpl = Template.from_audio(padded, R)
    assert abs(tpl.offset - 0.5) < 0.02
    assert tpl.duration < 0.75


def test_invalid_references():
    with pytest.raises(ValueError):
        Template.from_audio(np.zeros(R, np.float32), R)
    with pytest.raises(ValueError):
        Template.from_audio(SPLASH[:100], R)


def test_suggest_threshold():
    assert suggest_threshold([0.95, 0.9, 0.5, 0.4], 2) == pytest.approx((0.7, 0.4))
    assert suggest_threshold([0.95], 2) is None


def test_peaks_find_each_event(splash_event):
    x = synth.mix(synth.pink_noise(12, R, -30), R, [(2.0, SPLASH, 1.0), (8.0, SPLASH, 0.5)])
    peaks = scan(x, R, [splash_event]).peaks("splash")
    assert [round(t, 1) for t, _ in peaks[:2]] == [2.0, 8.0] or [
        round(t, 1) for t, _ in peaks[:2]
    ] == [8.0, 2.0]
    assert peaks[2][1] < 0.6


def test_analysis_is_much_faster_than_real_time(splash_event):
    x = synth.music(60, 22050, -25)
    t0 = time.perf_counter()
    scan(x, 22050, [splash_event])
    assert time.perf_counter() - t0 < 6.0  # < 10% of real time even on a slow CI machine
