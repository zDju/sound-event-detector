"""The WASAPI bindings (``_wasapi``) against Windows itself, on Windows only.

Everything that calls into COM runs in a child process: a mistake in a vtable or structure can
crash Python, and should fail one test with its output rather than end the test run. Where
Windows has no audio service or device (CI), activation and session listing may fail with a
CaptureError; that is accepted, a crash or a hang is not. On a machine where they must work,
``SOUNDDETECTOR_REQUIRE_WASAPI=1`` makes such a failure fail the test, including a format,
flag or binding error that would otherwise pass as "unavailable".
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="WASAPI exists only on Windows")
REQUIRE_WASAPI = os.environ.get("SOUNDDETECTOR_REQUIRE_WASAPI") == "1"


def check_outcome(out: str) -> None:
    """``ok ...``, or ``unavailable: ...`` where Windows may lack an audio service (CI)."""
    print(out)  # shown with -s
    if REQUIRE_WASAPI:
        assert out.startswith("ok"), f"SOUNDDETECTOR_REQUIRE_WASAPI=1, but: {out}"
    assert out.startswith(("ok", "unavailable:")), out


def run_python(code: str, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    report = f"exit code {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.returncode == 0, report
    return proc.stdout


def test_structures_match_the_windows_headers():
    from sound_event_detector import _wasapi as w

    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(w.GUID) == 16
    assert ctypes.sizeof(w.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(w.PROPVARIANT) == (24 if wide else 16)
    assert w.PROPVARIANT.blob.offset == 8
    assert ctypes.sizeof(w.WAVEFORMATEX) == 18
    assert ctypes.sizeof(w.WAVEFORMATEXTENSIBLE) == 40
    assert w.WAVEFORMATEXTENSIBLE.SubFormat.offset == 24
    assert ctypes.sizeof(w.PROCESSENTRY32W) == (568 if wide else 556)
    assert bytes(w.IID_IAudioClient)[:4] == bytes.fromhex("4CADB91C")  # Data1, little-endian


def test_wave_formats():
    from sound_event_detector import _wasapi as w

    f32 = w.wave_format(22050, 1, "f32")
    assert isinstance(f32, w.WAVEFORMATEXTENSIBLE)
    assert (f32.Format.wFormatTag, f32.Format.cbSize) == (w.WAVE_FORMAT_EXTENSIBLE, 22)
    assert (f32.Format.nBlockAlign, f32.Format.nAvgBytesPerSec) == (4, 22050 * 4)
    assert bytes(f32.SubFormat) == bytes(w.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
    assert f32.dwChannelMask == 0x4
    s16 = w.wave_format(48000, 2, "s16")
    assert isinstance(s16, w.WAVEFORMATEX)
    assert (s16.wFormatTag, s16.nBlockAlign, s16.wBitsPerSample) == (w.WAVE_FORMAT_PCM, 4, 16)


def test_process_list_has_parents_and_executable_names():
    from sound_event_detector import _wasapi as w
    from sound_event_detector.winapp import process_tree

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        procs = {p.pid: p for p in w.list_processes()}
    finally:
        child.kill()
        child.wait()
    assert procs[os.getpid()].exe.lower().startswith("python")
    assert procs[child.pid].exe.lower().startswith("python")
    assert procs[child.pid].ppid == os.getpid()
    assert child.pid in process_tree(os.getpid(), list(procs.values()))


def test_completion_handler_is_an_agile_com_object():
    out = run_python(
        """
        from ctypes import byref, c_void_p
        from sound_event_detector import _wasapi as w

        with w._com():
            h = w.CompletionHandler()
            qi = w.vtable_method(h.ptr, w.QUERY_INTERFACE, w.QueryInterfaceFn)
            for name in ("IUnknown", "IActivateAudioInterfaceCompletionHandler",
                         "IAgileObject", "IMarshal"):
                p = c_void_p()
                hr = qi(h.ptr, byref(getattr(w, "IID_" + name)), byref(p))
                assert hr == 0 and p.value, (name, w.hresult_text(hr))
                assert name == "IMarshal" or p.value == h.ptr, name
                w.vtable_method(p.value, w.RELEASE, w.ReleaseFn)(p.value)
            p = c_void_p()
            hr = qi(h.ptr, byref(w.IID_IAudioClient), byref(p))
            assert hr == w.E_NOINTERFACE and not p.value, w.hresult_text(hr)
            w.vtable_method(h.ptr, 3, w.ActivateCompletedFn)(h.ptr, None)
            assert h.done.is_set()
            assert h in w._live_handlers
            h.release()
            assert h not in w._live_handlers
        print("ok")
        """
    )
    assert out.strip() == "ok"


def test_process_loopback_of_this_process():
    out = run_python(
        """
        import os
        from sound_event_detector import _wasapi as w
        from sound_event_detector.capture import CaptureError

        results = []
        try:
            for rate, channels in ((22050, 1), (48000, 2)):  # monitor's and record's formats
                stream = w.open_stream(os.getpid(), rate, channels)
                try:
                    block = stream.read(0.2)
                finally:
                    stream.close()
                assert block.dtype.name == "float32" and block.shape[1:] == (channels,), block.shape
                results.append(f"{rate} Hz x{channels}: {stream.sample_format}")
        except CaptureError as e:
            print("unavailable:", e)
        else:
            print("ok", "; ".join(results))
        """
    )
    check_outcome(out)


def test_session_listing():
    out = run_python(
        """
        from sound_event_detector import _wasapi as w
        from sound_event_detector.capture import CaptureError

        try:
            sessions = w.list_sessions()
        except CaptureError as e:
            print("unavailable:", e)
        else:
            assert all(s.state in ("active", "inactive", "expired") for s in sessions)
            print("ok", len(sessions), "sessions")
        """
    )
    check_outcome(out)


def test_missing_application_and_devices_command():
    out = run_python(
        """
        from sound_event_detector import AppSource
        from sound_event_detector.capture import CaptureError

        try:
            AppSource("no-such-application-0123").open()
        except CaptureError as e:
            assert "no application matching 'no-such-application-0123'" in str(e), e
        else:
            raise AssertionError("opened an application that does not exist")
        print("ok")
        """
    )
    assert out.strip() == "ok"
    proc = subprocess.run(
        [sys.executable, "-m", "sound_event_detector", "devices"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Applications" in proc.stdout, proc.stdout
