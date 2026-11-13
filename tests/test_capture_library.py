from __future__ import annotations

import json

import numpy as np
import pytest

import synth
from sound_event_detector import FileSource, Library, open_source, record
from sound_event_detector.audio import load_wav, save_wav
from sound_event_detector.capture import (
    AppSource,
    CaptureError,
    DeviceSource,
    find_app,
    parse_app_streams,
)

R = 48000

PW_DUMP = json.dumps(
    [
        {
            "id": 1,
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": "Audio/Sink", "object.serial": 50}},
        },
        {
            "id": 2,
            "info": {
                "props": {
                    "media.class": "Stream/Output/Audio",
                    "object.serial": 101,
                    "application.name": "Firefox",
                    "application.process.id": 4321,
                    "application.process.binary": "firefox",
                    "media.name": "Some video",
                }
            },
        },
        {
            "id": 3,
            "info": {
                "props": {
                    "media.class": "Stream/Output/Audio",
                    "object.serial": 102,
                    "node.name": "wine-stream",
                    "application.process.id": "777",
                    "application.process.binary": "Wow.exe",
                }
            },
        },
        {"id": 4, "info": None},
    ]
)


def test_parse_app_streams():
    streams = parse_app_streams(PW_DUMP)
    assert [(s.serial, s.name, s.pid, s.binary) for s in streams] == [
        (101, "Firefox", 4321, "firefox"),
        (102, "wine-stream", 777, "Wow.exe"),
    ]


def test_find_app_by_name_binary_or_pid():
    streams = parse_app_streams(PW_DUMP)
    assert find_app("firefox", streams).serial == 101
    assert find_app("wow", streams).serial == 102
    assert find_app("777", streams).serial == 102
    assert find_app("spotify", streams) is None


def test_app_source_reports_missing_app(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    src = AppSource("spotify", dump=lambda: PW_DUMP)
    with pytest.raises(CaptureError, match="spotify"):
        src.open()


def test_open_source_specs():
    assert isinstance(open_source(None), DeviceSource) and open_source(None).device is None
    assert open_source("device:HDMI").device == "HDMI"
    assert open_source("Headphones").device == "Headphones"
    assert isinstance(open_source("app:firefox"), AppSource)
    assert isinstance(open_source("file:x.wav"), FileSource)


def test_file_source_and_record(tmp_path):
    x = synth.splash(R)
    save_wav(tmp_path / "s.wav", np.stack([x, x], axis=1), R, float32=True)
    src = FileSource(tmp_path / "s.wav", sample_rate=22050, realtime=False)
    data = record(src, 10.0)  # stops at the end of the file
    assert data.shape == (round(len(x) * 22050 / R), 1)


def test_library_add_load_settings(tmp_path):
    lib = Library(tmp_path)
    assert lib.names() == []
    p1 = lib.add("bite", synth.splash(R), R)
    p2 = lib.add("bite", synth.splash(R, seed=5), R)
    assert p1.name == "bite.wav" and p2.name == "bite-2.wav"
    y, rate = load_wav(p1)
    assert rate == R and y.ndim == 1
    assert lib.names() == ["bite"]
    lib.update_settings("bite", {"threshold": 0.8, "cooldown": 1.5})
    ev = lib.load("bite")
    assert (ev.threshold, ev.cooldown, len(ev.templates)) == (0.8, 1.5, 2)
    assert lib.load("bite", threshold=0.6).threshold == 0.6
    assert lib.update_settings("bite", {"threshold": None})["threshold"] == 0.75
    lib.update_settings("bite", {"pitch_tolerance": 0.04, "template_length": 0.3})
    ev = lib.load("bite")
    assert len(ev.templates) == 2 * 5 and ev.duration <= 0.3 / 0.96 + 0.01  # slowed = longer
    assert {t.label for t in ev.templates} >= {"bite", "bite@0.96", "bite-2@1.04"}
    assert len(lib.load("bite", pitch_tolerance=0).templates) == 2
    with pytest.raises(ValueError):
        lib.load("bite", treshold=0.5)
    with pytest.raises(KeyError):
        lib.info("nothing")
    with pytest.raises(ValueError):
        lib.add("../evil", synth.splash(R), R)
    with pytest.raises(ValueError):
        lib.add("silent", np.zeros(R, np.float32), R)


def test_library_rejects_unknown_settings(tmp_path):
    lib = Library(tmp_path)
    lib.add("x", synth.splash(R), R)
    (tmp_path / "x" / "event.toml").write_text("treshold = 0.5\n")
    with pytest.raises(ValueError, match="treshold"):
        lib.settings("x")


class FakeMic:
    def __init__(self, id, name):
        self.id, self.name, self.isloopback = id, name, True

    def recorder(self, samplerate, channels, blocksize):
        mic = self

        class Rec:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def record(self, numframes):
                return np.full((numframes, channels), 0.1 if mic.id.startswith("a") else 0.2)

        return Rec()


class FakeSpeaker:
    def __init__(self, id, name):
        self.id, self.name = id, name


class FakeSoundcard:
    def __init__(self):
        self.speakers = [FakeSpeaker("a_sink", "Speakers A"), FakeSpeaker("b_sink", "Headset B")]
        self.default = self.speakers[0]

    def all_microphones(self, include_loopback=False):
        return [FakeMic(f"{s.id}.monitor", f"Monitor of {s.name}") for s in self.speakers]

    def default_speaker(self):
        return self.default


def test_device_source_follows_default_output(monkeypatch):
    sc = FakeSoundcard()
    monkeypatch.setattr("sound_event_detector.capture._soundcard", lambda: sc)
    src = DeviceSource(None, check_interval=0.0)
    src.open()
    assert "Speakers A" in src.name and src.read(4)[0, 0] == pytest.approx(0.1)
    assert not src.stale()
    sc.default = sc.speakers[1]  # e.g. a USB headset was plugged in
    assert src.stale()
    src.close()
    src.open()
    assert "Headset B" in src.name and src.read(4)[0, 0] == pytest.approx(0.2)


def test_device_source_explicit_choice(monkeypatch):
    sc = FakeSoundcard()
    monkeypatch.setattr("sound_event_detector.capture._soundcard", lambda: sc)
    src = DeviceSource("headset", check_interval=0.0)
    src.open()
    assert src.name == "Monitor of Headset B"
    sc.default = sc.speakers[0]
    assert not src.stale()  # an explicit choice does not follow the default
    with pytest.raises(CaptureError, match="several"):
        DeviceSource("monitor").open()
    with pytest.raises(CaptureError, match="no output device"):
        DeviceSource("hdmi").open()
