from __future__ import annotations

import json
import re

import numpy as np
import pytest

import synth
from sound_event_detector.audio import save_wav
from sound_event_detector.cli import main

R = 48000


def write_session(tmp_path):
    x = synth.mix(
        synth.music(12, R, -28),
        R,
        [(2.0, synth.splash(R), 1.0), (6.0, synth.splash(R), 0.5), (9.0, synth.bell(R), 1.0)],
    )
    save_wav(tmp_path / "session.wav", x, R)
    clip = synth.mix(np.zeros(R * 3, np.float32), R, [(1.1, synth.splash(R), 1.0)])
    save_wav(tmp_path / "clip.wav", clip, R)
    return tmp_path / "session.wav", tmp_path / "clip.wav"


def test_add_events_scan_apply(tmp_path, capsys):
    session, clip = write_session(tmp_path)
    lib = tmp_path / "lib"
    assert main(["--library", str(lib), "add", str(clip), "--name", "bite"]) == 0
    out = capsys.readouterr().out
    assert "Added a reference for 'bite'" in out
    assert "from 1.09" in out or "from 1.10" in out  # detected onset

    assert main(["--library", str(lib), "events"]) == 0
    assert "bite: 1 clip(s)" in capsys.readouterr().out

    args = ["--library", str(lib), "scan", str(session), "-e", "bite", "--expect", "2", "--apply"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Detections (2)" in out
    times = [float(m) for m in re.findall(r"^  00:(\d\d\.\d+)  bite", out, re.M)]
    assert times == pytest.approx([2.0, 6.0], abs=0.02)
    assert "Saved threshold" in out


def test_scan_with_reference_file(tmp_path, capsys):
    session, _ = write_session(tmp_path)
    ref = tmp_path / "ref.wav"
    save_wav(ref, synth.splash(R), R)
    assert main(["scan", str(session), "-r", str(ref)]) == 0
    assert "Detections (2)" in capsys.readouterr().out


def test_monitor_file_source_json(tmp_path, capsys):
    x = synth.mix(
        synth.music(4, R, -28), R, [(0.5, synth.splash(R), 1.0), (2.5, synth.splash(R), 1.0)]
    )
    save_wav(tmp_path / "short.wav", x, R)
    ref = tmp_path / "ref.wav"
    save_wav(ref, synth.splash(R), R)
    # a file source plays at real-time pace and ends the monitor when the file ends
    args = ["monitor", "-r", str(ref), "--source", f"file:{tmp_path / 'short.wav'}", "--json"]
    assert main([*args, "--cooldown", "1"]) == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [round(d["stream_start"], 1) for d in lines] == [0.5, 2.5]


def test_errors_are_reported_without_traceback(tmp_path, capsys):
    assert main(["--library", str(tmp_path), "scan", "missing.wav", "-e", "nothing"]) == 2
    assert "error:" in capsys.readouterr().err
    assert main(["--library", str(tmp_path / "empty"), "monitor"]) == 2
    assert "no events" in capsys.readouterr().err
