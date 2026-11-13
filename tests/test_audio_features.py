from __future__ import annotations

import numpy as np
import pytest

import synth
from sound_event_detector.audio import (
    find_end,
    find_onset,
    load_wav,
    prepare,
    resample,
    save_wav,
    to_mono,
    trim,
)
from sound_event_detector.features import DEFAULT_PARAMS, FeatureParams, analyze, mel_filterbank

R = 48000


def test_wav_round_trip_pcm16_and_float(tmp_path):
    x = synth.splash(R)
    save_wav(tmp_path / "a.wav", x, R)
    y, rate = load_wav(tmp_path / "a.wav")
    assert rate == R and y.dtype == np.float32
    assert np.max(np.abs(y - x)) < 1e-4  # 16-bit quantisation
    save_wav(tmp_path / "b.wav", x, R, float32=True)
    z, _ = load_wav(tmp_path / "b.wav")
    assert np.array_equal(z, x)


def test_to_mono_averages_channels():
    stereo = np.stack([np.ones(10), -np.ones(10)], axis=1).astype(np.float32)
    assert np.allclose(to_mono(stereo), 0)
    assert to_mono(stereo[:, :1]).shape == (10,)


def test_resample_keeps_a_tone_and_its_length():
    t = np.arange(R) / R
    x = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    y = resample(x, R, 22050)
    assert len(y) == 22050
    spec = np.abs(np.fft.rfft(y))
    assert abs(np.fft.rfftfreq(len(y), 1 / 22050)[spec.argmax()] - 1000) < 2


def test_prepare_44100_stereo():
    x = np.zeros((44100, 2), np.float32)
    assert prepare(x, 44100, 22050).shape == (22050,)


def test_find_onset_and_end():
    x = synth.mix(synth.pink_noise(3, R, -60), R, [(1.234, synth.splash(R), 1.0)])
    onset = find_onset(x, R)
    assert 1.22 <= onset <= 1.24
    end = find_end(x, R, onset)
    assert 1.234 + 0.5 < end <= 1.234 + 0.75  # the splash lasts 0.7 s and decays


def test_find_onset_takes_the_loudest_sound_not_the_first():
    # a quieter sound first (e.g. the start of an action), the wanted one later and louder
    x = synth.mix(
        synth.pink_noise(6, R, -60),
        R,
        [(1.0, synth.splash(R), 0.3), (4.5, synth.splash(R), 1.0)],
    )
    onset = find_onset(x, R)
    assert 4.48 <= onset <= 4.51
    end = find_end(x, R, onset)
    assert 4.5 + 0.5 < end <= 4.5 + 0.75


def test_trim_bounds():
    x = np.arange(100, dtype=np.float32)
    assert len(trim(x, 10, 2.0, 3.0)) == 30
    assert len(trim(x, 10, 9.0)) == 10
    with pytest.raises(ValueError):
        trim(x, 10, 11.0)


def test_analyze_shapes_and_silence():
    p = DEFAULT_PARAMS
    x = np.zeros(p.sample_rate, np.float32)
    mel, power = analyze(x, p)
    assert mel.shape == (p.frames_for(len(x)), p.n_mels)
    assert np.all(np.isfinite(mel)) and np.all(power == 0)
    assert analyze(x[: p.n_fft - 1], p)[0].shape == (0, p.n_mels)


def test_filterbank_covers_every_band():
    fb = mel_filterbank(DEFAULT_PARAMS)
    assert fb.shape == (DEFAULT_PARAMS.n_mels, DEFAULT_PARAMS.n_fft // 2 + 1)
    assert np.all(fb.sum(axis=1) > 0)


def test_params_validation():
    with pytest.raises(ValueError):
        FeatureParams(sample_rate=16000, fmax=9000)
