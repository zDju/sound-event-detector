"""``_wasapi`` against fake Windows DLLs and COM objects made in Python, on every platform.

``_wasapi._api`` is replaced by an :class:`~sound_event_detector._wasapi.Api` over fake DLLs,
and every COM interface Windows would hand out is a :class:`FakeObject` with a real vtable of
ctypes callbacks, so the code calls them exactly as it calls Windows. Each test checks that
every reference it was given is released again.
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections.abc import Callable
from ctypes import byref, c_void_p
from typing import Any, ClassVar

import numpy as np
import pytest

from sound_event_detector import _wasapi as w
from sound_event_detector.capture import CaptureError
from sound_event_detector.winapp import WinProcess, WinSession

E_FAIL = w._signed(0x80004005)
E_NOTIMPL = w._signed(0x80004001)
E_ACCESSDENIED = w._signed(0x80070005)
E_NOTFOUND = w._signed(0x80070490)
AUDCLNT_E_DEVICE_INVALIDATED = w._signed(0x88890004)
AUDCLNT_E_UNSUPPORTED_FORMAT = w._signed(0x88890008)
WAIT_TIMEOUT = 0x102


def wait_for(condition: Callable[[], object], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


# -- fake DLLs ---------------------------------------------------------------------------------


class FakeFunction:
    """A DLL function: restype and argtypes are accepted; calls go to the test's ``impls``."""

    def __init__(self, dlls: FakeDLLs, name: str) -> None:
        self.dlls = dlls
        self.name = name
        self.restype: Any = None
        self.argtypes: Any = ()

    def __call__(self, *args: Any) -> Any:
        self.dlls.calls.append((self.name, args))
        return self.dlls.impls[self.name](*args)


class FakeDLLs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.impls: dict[str, Callable[..., Any]] = {
            "CoInitializeEx": lambda reserved, flags: 0,
            "CoUninitialize": lambda: None,
            "CoCreateFreeThreadedMarshaler": lambda outer, out: E_FAIL,
            "CoTaskMemFree": lambda ptr: None,
            "CreateEventW": lambda *args: 1234,
            "WaitForSingleObject": lambda handle, ms: WAIT_TIMEOUT,
            "CloseHandle": lambda handle: 1,
        }

    def load(self, name: str) -> Any:
        dlls = self

        class DLL:
            def __getattr__(self, attr: str) -> FakeFunction:
                return FakeFunction(dlls, attr)

        return DLL()

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def args(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a in self.calls if n == name]


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch):
    dlls = FakeDLLs()
    monkeypatch.setattr(w, "_api", w.Api(dlls.load))
    monkeypatch.setattr(w, "windows_build", lambda: 19045)
    FakeObject.created.clear()
    yield dlls
    leaked = [(o.name, o.refs) for o in FakeObject.created if o.refs != 0 or o.over_released]
    FakeObject.created.clear()
    assert not leaked, f"references not released (or released too often): {leaked}"


# -- fake COM objects --------------------------------------------------------------------------


def noop() -> Any:
    return w.NoArgsFn(lambda this: 0)


class FakeObject:
    """A COM object made in Python. ``methods`` fill the vtable from slot 3; QueryInterface
    hands out ``self.other`` (default: itself). ``refs`` counts the references given out."""

    created: ClassVar[list[FakeObject]] = []

    def __init__(self, name: str, *methods: Any) -> None:
        self.name = name
        self.refs = 0
        self.over_released = False
        self.other: FakeObject | None = None
        self._fns = [
            w.QueryInterfaceFn(self._query_interface),
            w.AddRefFn(self._add_ref),
            w.ReleaseFn(self._release),
            *methods,
        ]
        self._vtable = (c_void_p * len(self._fns))(
            *(ctypes.cast(f, c_void_p).value for f in self._fns)
        )
        self._object = (c_void_p * 1)(ctypes.addressof(self._vtable))
        self.ptr = ctypes.addressof(self._object)
        FakeObject.created.append(self)

    def give(self) -> int:
        """A new reference, as an out parameter hands it over."""
        self.refs += 1
        return self.ptr

    def _query_interface(self, this: int, riid: Any, ppv: Any) -> int:
        ppv[0] = (self.other or self).give()
        return 0

    def _add_ref(self, this: int) -> int:
        self.refs += 1
        return self.refs

    def _release(self, this: int) -> int:
        self.refs -= 1
        if self.refs < 0:
            self.over_released = True
        return max(self.refs, 0)


def out_ptr(arg: Any) -> c_void_p:
    """The ``c_void_p`` behind a ``byref(...)`` argument passed to a fake DLL function."""
    return arg._obj


# -- loading -----------------------------------------------------------------------------------


def test_api_refuses_where_there_is_no_windows(monkeypatch):
    monkeypatch.setattr(w, "_api", None)
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(CaptureError, match="only available on Windows"):
        w.api()


def test_activate_function_is_loaded_on_first_use(win):
    api = w.api()
    assert api._activate is None
    assert isinstance(api.ActivateAudioInterfaceAsync, FakeFunction)
    assert api.ActivateAudioInterfaceAsync is api.ActivateAudioInterfaceAsync

    def missing(name: str) -> Any:
        if name == "mmdevapi":
            raise OSError("module not found")
        return win.load(name)

    with pytest.raises(CaptureError, match="ActivateAudioInterfaceAsync is not available"):
        _ = w.Api(missing).ActivateAudioInterfaceAsync


# -- the completion handler --------------------------------------------------------------------


def test_completion_handler_identity_and_reference_count(win):
    h = w.CompletionHandler()
    qi = w.vtable_method(h.ptr, w.QUERY_INTERFACE, w.QueryInterfaceFn)
    for iid in (w.IID_IUnknown, w.IID_IActivateAudioInterfaceCompletionHandler, w.IID_IAgileObject):
        p = c_void_p()
        assert qi(h.ptr, byref(iid), byref(p)) == 0 and p.value == h.ptr
    p = c_void_p()
    assert qi(h.ptr, byref(w.IID_IMarshal), byref(p)) == w.E_NOINTERFACE and not p.value
    assert qi(h.ptr, byref(w.IID_IAudioClient), byref(p)) == w.E_NOINTERFACE
    assert qi(h.ptr, byref(w.IID_IUnknown), None) == w.E_POINTER
    assert h._refs == 4
    for _ in range(3):
        w.vtable_method(h.ptr, w.RELEASE, w.ReleaseFn)(h.ptr)
    assert w.pending_activations() == 1
    done = w.vtable_method(h.ptr, w.HANDLER_ACTIVATE_COMPLETED, w.ActivateCompletedFn)
    assert done(h.ptr, None) == 0 and h.done.is_set()
    assert h in w._live_handlers and w.pending_activations() == 0
    h.release()
    assert h not in w._live_handlers and h._refs == 0


def test_completion_handler_aggregates_the_free_threaded_marshaler(win):
    marshal = FakeObject("IMarshal")
    inner = FakeObject("free-threaded marshaler")
    inner.other = marshal
    outers = []

    def create_ftm(outer: Any, out: Any) -> int:
        outers.append(outer)
        out_ptr(out).value = inner.give()
        return 0

    win.impls["CoCreateFreeThreadedMarshaler"] = create_ftm
    h = w.CompletionHandler()
    assert outers == [h.ptr]
    p = c_void_p()
    qi = w.vtable_method(h.ptr, w.QUERY_INTERFACE, w.QueryInterfaceFn)
    assert qi(h.ptr, byref(w.IID_IMarshal), byref(p)) == 0 and p.value == marshal.ptr
    w.vtable_method(marshal.ptr, w.RELEASE, w.ReleaseFn)(marshal.ptr)
    assert inner.refs == 1
    h.release()  # the last reference: the aggregated marshaler goes with it
    assert inner.refs == 0 and h not in w._live_handlers


# -- activation --------------------------------------------------------------------------------


class FakeActivation:
    """``ActivateAudioInterfaceAsync``: completes on another thread, as Windows does."""

    def __init__(
        self, win: FakeDLLs, result: int = 0, *, complete: bool = True, delay: float = 0.0
    ) -> None:
        self.client = FakeObject("IAudioClient")
        self.result = result
        self.complete = complete
        self.delay = delay
        self.params: list[tuple[int, int, int]] = []
        self.handlers: list[int] = []
        self.threads: list[threading.Thread] = []
        win.impls["ActivateAudioInterfaceAsync"] = self

    def __call__(self, path: str, riid: Any, prop: Any, handler: Any, out: Any) -> int:
        assert path == "VAD\\Process_Loopback"
        assert bytes(riid._obj) == bytes(w.IID_IAudioClient)
        pv = prop._obj
        assert pv.vt == w.VT_BLOB and pv.blob.cbSize == ctypes.sizeof(
            w.AUDIOCLIENT_ACTIVATION_PARAMS
        )
        p = w.AUDIOCLIENT_ACTIVATION_PARAMS.from_address(pv.blob.pBlobData)
        self.params.append(
            (
                p.ActivationType,
                p.ProcessLoopbackParams.TargetProcessId,
                p.ProcessLoopbackParams.ProcessLoopbackMode,
            )
        )
        ptr = handler.value
        self.handlers.append(ptr)
        w.vtable_method(ptr, w.ADD_REF, w.AddRefFn)(ptr)

        def get_result(this: int, phr: Any, punk: Any) -> int:
            phr[0] = self.result
            punk[0] = self.client.give() if self.result >= 0 else None
            return 0

        operation = FakeObject("activation operation", w.GetActivateResultFn(get_result))

        def finish() -> None:
            time.sleep(self.delay)
            if self.complete:
                completed = w.vtable_method(ptr, 3, w.ActivateCompletedFn)
                completed(ptr, operation.ptr)
                w.vtable_method(ptr, w.RELEASE, w.ReleaseFn)(ptr)

        thread = threading.Thread(target=finish)
        thread.start()
        self.threads.append(thread)
        out_ptr(out).value = operation.give()
        return 0

    def finish_late(self) -> None:
        """Windows completes and releases the handlers it kept (the never-completed ones)."""
        for ptr in self.handlers:
            w.vtable_method(ptr, 3, w.ActivateCompletedFn)(ptr, None)
            w.vtable_method(ptr, w.RELEASE, w.ReleaseFn)(ptr)


def test_activation_returns_the_client_and_releases_everything_else(win):
    fake = FakeActivation(win, delay=0.02)
    client = w._activate_process_loopback(4242)
    assert client.ptr == fake.client.ptr and fake.client.refs == 1
    assert fake.params == [(1, 4242, 0)]  # process loopback, pid, include the process tree
    client.release()
    assert wait_for(lambda: not w._live_handlers)


def test_activation_failure(win):
    FakeActivation(win, result=E_ACCESSDENIED)
    with pytest.raises(CaptureError, match=r"pid 4242 failed: 0x80070005 \(E_ACCESSDENIED\)"):
        w._activate_process_loopback(4242)
    assert wait_for(lambda: not w._live_handlers)


def test_activation_refused_at_once(win):
    win.impls["ActivateAudioInterfaceAsync"] = lambda *args: E_NOTIMPL
    with pytest.raises(CaptureError, match=r"ActivateAudioInterfaceAsync failed: .*E_NOTIMPL"):
        w._activate_process_loopback(4242)
    assert not w._live_handlers


def test_activations_windows_never_completes_are_bounded(win, monkeypatch):
    monkeypatch.setattr(w, "ACTIVATE_TIMEOUT", 0.02)
    fake = FakeActivation(win, complete=False)
    try:
        for _ in range(w.MAX_PENDING_ACTIVATIONS):
            with pytest.raises(CaptureError, match="timed out"):
                w._activate_process_loopback(4242)
        assert w.pending_activations() == w.MAX_PENDING_ACTIVATIONS
        with pytest.raises(CaptureError, match="has not completed 3 earlier"):
            w._activate_process_loopback(4242)
        assert win.count("ActivateAudioInterfaceAsync") == w.MAX_PENDING_ACTIVATIONS
    finally:
        fake.finish_late()
    assert not w._live_handlers and w.pending_activations() == 0
    FakeActivation(win)  # now Windows answers again
    w._activate_process_loopback(4242).release()


# -- reading captured audio --------------------------------------------------------------------


class FakeCapture:
    """``IAudioCaptureClient`` delivering ``packets`` of (frames, flags, samples)."""

    def __init__(self, packets: list[tuple[int, int, list[float]]], dtype: Any = ctypes.c_float):
        self.packets = packets
        self.dtype = dtype
        self.next = 0
        self.held: int | None = None
        self.released: list[int] = []
        self.buffers: list[Any] = []
        self.size_error = 0
        self.empty = False
        self.obj = FakeObject(
            "IAudioCaptureClient",
            w.GetBufferFn(self.get_buffer),
            w.ReleaseBufferFn(self.release_buffer),
            w.OutU32Fn(self.next_packet_size),
        )

    def next_packet_size(self, this: int, out: Any) -> int:
        out[0] = self.packets[self.next][0] if self.next < len(self.packets) else 0
        return self.size_error

    def get_buffer(self, this: int, data: Any, frames: Any, flags: Any, dev: Any, qpc: Any) -> int:
        assert self.held is None, "GetBuffer twice without ReleaseBuffer"
        if self.empty or self.next >= len(self.packets):
            frames[0] = 0
            return w.AUDCLNT_S_BUFFER_EMPTY
        n, f, values = self.packets[self.next]
        buf = (self.dtype * max(len(values), 1))(*values)
        self.buffers.append(buf)
        data[0] = ctypes.addressof(buf)
        frames[0] = n
        flags[0] = f
        self.held = n
        return 0

    def release_buffer(self, this: int, n: int) -> int:
        assert self.held == n
        self.held = None
        self.released.append(n)
        self.next += 1
        return 0


def make_stream(
    win: FakeDLLs, capture: FakeCapture, sample_format: str = "f32", channels: int = 1
) -> tuple[w.LoopbackStream, list[int]]:
    stops: list[int] = []

    def stop(this: int) -> int:
        stops.append(1)
        return 0

    client = FakeObject("IAudioClient", *[noop() for _ in range(8)], w.NoArgsFn(stop))
    stream = w.LoopbackStream(
        w._Com(client.give(), "client"),
        w._Com(capture.obj.give(), "capture"),
        1234,
        sample_format,
        channels,
        w._Apartment(),
    )
    return stream, stops


def test_stream_read_copies_every_packet_and_returns_each_buffer(win):
    capture = FakeCapture(
        [
            (3, 0, [0.1, 0.2, 0.3]),
            (2, w.AUDCLNT_BUFFERFLAGS_SILENT, [9.0, 9.0]),  # read as silence
            (1, w.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, [0.5]),
        ]
    )
    stream, stops = make_stream(win, capture)
    block = stream.read(0.05)
    assert block.dtype == np.float32 and block.shape == (6, 1)
    assert block.ravel().tolist() == pytest.approx([0.1, 0.2, 0.3, 0.0, 0.0, 0.5])
    assert capture.released == [3, 2, 1]
    assert win.args("WaitForSingleObject") == [(1234, 50)]
    assert stream.read(0.0).shape == (0, 1)  # nothing more
    stream.close()
    assert stops == [1] and win.args("CloseHandle") == [(1234,)]
    assert win.count("CoInitializeEx") == win.count("CoUninitialize") == 1


def test_stream_read_16_bit_stereo(win):
    capture = FakeCapture([(2, 0, [16384, -16384, 0, 32767])], dtype=ctypes.c_int16)
    stream, _ = make_stream(win, capture, "s16", 2)
    assert stream.read(0.0).tolist() == [[0.5, -0.5], [0.0, 32767 / 32768]]
    stream.close()


def test_stream_read_stops_on_an_empty_buffer_and_reports_errors(win):
    capture = FakeCapture([(4, 0, [0.1] * 4)])
    capture.empty = True  # GetNextPacketSize says 4 frames, GetBuffer finds none
    stream, _ = make_stream(win, capture)
    assert stream.read(0.0).shape == (0, 1) and capture.released == []
    capture.size_error = AUDCLNT_E_DEVICE_INVALIDATED
    with pytest.raises(CaptureError, match=r"GetNextPacketSize failed: .*DEVICE_INVALIDATED"):
        stream.read(0.0)
    stream.close()


# -- opening a stream --------------------------------------------------------------------------


class FakeWasapi:
    """Synchronous activation of fresh ``IAudioClient``s; records what Initialize was given."""

    def __init__(self, win: FakeDLLs, refuse: tuple[int, ...] = (), start: int = 0) -> None:
        self.refuse = refuse  # format tags Initialize refuses
        self.start = start
        self.formats: list[tuple[int, int, int, int, int, int]] = []
        self.subformats: list[bytes] = []
        win.impls["ActivateAudioInterfaceAsync"] = self

    def __call__(self, path: str, riid: Any, prop: Any, handler: Any, out: Any) -> int:
        capture = FakeCapture([])

        def initialize(this, mode, flags, buffer, period, fmt, session) -> int:
            f = w.WAVEFORMATEX.from_address(fmt)
            self.formats.append(
                (f.wFormatTag, f.nSamplesPerSec, f.nChannels, f.wBitsPerSample, flags, buffer)
            )
            if f.wFormatTag == w.WAVE_FORMAT_EXTENSIBLE:
                self.subformats.append(bytes(w.WAVEFORMATEXTENSIBLE.from_address(fmt).SubFormat))
            return AUDCLNT_E_UNSUPPORTED_FORMAT if f.wFormatTag in self.refuse else 0

        def get_service(this: int, riid: Any, out: Any) -> int:
            assert bytes(riid.contents) == bytes(w.IID_IAudioCaptureClient)
            out[0] = capture.obj.give()
            return 0

        client = FakeObject(
            "IAudioClient",
            w.InitializeFn(initialize),
            *[noop() for _ in range(6)],  # slots 4-9
            w.NoArgsFn(lambda this: self.start),  # Start
            noop(),  # Stop
            noop(),  # Reset
            w.SetEventHandleFn(lambda this, event: 0),
            w.GetServiceFn(get_service),
        )

        def get_result(this: int, phr: Any, punk: Any) -> int:
            phr[0] = 0
            punk[0] = client.give()
            return 0

        operation = FakeObject("activation operation", w.GetActivateResultFn(get_result))
        ptr = handler.value
        w.vtable_method(ptr, w.ADD_REF, w.AddRefFn)(ptr)
        w.vtable_method(ptr, 3, w.ActivateCompletedFn)(ptr, operation.ptr)
        w.vtable_method(ptr, w.RELEASE, w.ReleaseFn)(ptr)
        out_ptr(out).value = operation.give()
        return 0


def test_open_stream_asks_for_float_at_the_detector_rate(win):
    fake = FakeWasapi(win)
    stream = w.open_stream(77, 22050, 1)
    assert stream.sample_format == "f32"
    # LOOPBACK | EVENTCALLBACK | SRC_DEFAULT_QUALITY | AUTOCONVERTPCM; a 1 s buffer
    assert fake.formats == [(0xFFFE, 22050, 1, 32, 0x88060000, 10_000_000)]
    assert fake.subformats == [bytes.fromhex("0300000000001000800000aa00389b71")]
    stream.close()
    assert win.count("CoInitializeEx") == win.count("CoUninitialize")
    assert (1234,) in win.args("CloseHandle")


def test_open_stream_falls_back_to_16_bit_pcm(win):
    fake = FakeWasapi(win, refuse=(w.WAVE_FORMAT_EXTENSIBLE,))
    stream = w.open_stream(77, 48000, 2)
    assert stream.sample_format == "s16" and stream.frame_bytes == 4
    assert [f[:4] for f in fake.formats] == [
        (w.WAVE_FORMAT_EXTENSIBLE, 48000, 2, 32),
        (w.WAVE_FORMAT_PCM, 48000, 2, 16),
    ]
    stream.close()


def test_open_stream_failures_release_everything(win):
    FakeWasapi(win, refuse=(w.WAVE_FORMAT_EXTENSIBLE, w.WAVE_FORMAT_PCM))
    with pytest.raises(CaptureError) as e:
        w.open_stream(77, 22050, 1)
    assert "f32 failed: 0x88890008 (AUDCLNT_E_UNSUPPORTED_FORMAT)" in str(e.value)
    assert "s16 failed: 0x88890008" in str(e.value)
    FakeWasapi(win, start=E_FAIL)
    with pytest.raises(CaptureError, match=r"Start failed: 0x80004005 \(E_FAIL\)"):
        w.open_stream(77, 22050, 1)
    assert (1234,) in win.args("CloseHandle")
    assert win.count("CoInitializeEx") == win.count("CoUninitialize") == 2
    assert not w._live_handlers


def test_open_stream_refuses_older_windows(win, monkeypatch):
    monkeypatch.setattr(w, "windows_build", lambda: 18363)
    with pytest.raises(CaptureError, match=r"needs Windows 10 version 2004 .* build 18363"):
        w.open_stream(77, 22050, 1)
    assert win.count("CoInitializeEx") == 0


# -- audio sessions and processes --------------------------------------------------------------


def session_objects(state: int, name: str, pid: int, system: bool, pid_hr: int = 0):
    names: list[Any] = []

    def get_state(this: int, out: Any) -> int:
        out[0] = state
        return 0

    def get_display_name(this: int, out: Any) -> int:
        buf = ctypes.create_unicode_buffer(name)  # CoTaskMemAlloc'd, in Windows
        names.append(buf)
        out[0] = ctypes.addressof(buf)
        return 0

    def get_process_id(this: int, out: Any) -> int:
        out[0] = pid
        return pid_hr

    control = FakeObject(
        "IAudioSessionControl", w.OutU32Fn(get_state), w.OutPtrFn(get_display_name)
    )
    control.other = FakeObject(
        "IAudioSessionControl2",
        *[noop() for _ in range(11)],  # slots 3-13
        w.OutU32Fn(get_process_id),
        w.NoArgsFn(lambda this: w.S_OK if system else w.S_FALSE),
    )
    return control, names


def test_list_sessions_on_every_active_output(win):
    sessions = [
        session_objects(1, "", 102, False),
        session_objects(0, "@%SystemRoot%\\System32\\AudioSrv.Dll,-202", 0, True),
        session_objects(1, "", 1, False, pid_hr=E_FAIL),  # skipped
    ]

    def get_count(n: int):
        def f(this: int, out: Any) -> int:
            out[0] = n
            return 0

        return f

    def get_session(this: int, i: int, out: Any) -> int:
        out[0] = sessions[i][0].give()
        return 0

    enum = FakeObject(
        "IAudioSessionEnumerator", w.OutU32Fn(get_count(3)), w.IndexOutFn(get_session)
    )
    manager = FakeObject(
        "IAudioSessionManager2",
        noop(),
        noop(),
        w.OutPtrFn(lambda this, out: (out.__setitem__(0, enum.give()), 0)[1]),
    )

    def activate(this: int, riid: Any, ctx: int, params: Any, out: Any) -> int:
        assert bytes(riid.contents) == bytes(w.IID_IAudioSessionManager2)
        out[0] = manager.give()
        return 0

    devices = [
        FakeObject("IMMDevice", w.DeviceActivateFn(activate)),
        FakeObject("IMMDevice", w.DeviceActivateFn(lambda *args: E_NOTFOUND)),  # skipped
    ]

    def item(this: int, i: int, out: Any) -> int:
        out[0] = devices[i].give()
        return 0

    collection = FakeObject("IMMDeviceCollection", w.OutU32Fn(get_count(2)), w.IndexOutFn(item))

    def enum_endpoints(this: int, flow: int, mask: int, out: Any) -> int:
        assert (flow, mask) == (w.E_RENDER, w.DEVICE_STATE_ACTIVE)
        out[0] = collection.give()
        return 0

    enumerator = FakeObject("IMMDeviceEnumerator", w.EnumAudioEndpointsFn(enum_endpoints))

    def create_instance(clsid: Any, outer: Any, ctx: int, iid: Any, out: Any) -> int:
        assert bytes(clsid._obj) == bytes(w.CLSID_MMDeviceEnumerator)
        assert bytes(iid._obj) == bytes(w.IID_IMMDeviceEnumerator)
        out_ptr(out).value = enumerator.give()
        return 0

    win.impls["CoCreateInstance"] = create_instance
    assert w.list_sessions() == [
        WinSession(102, "active", "", False),
        WinSession(0, "inactive", "@%SystemRoot%\\System32\\AudioSrv.Dll,-202", True),
    ]
    freed = [a[0].value for a in win.args("CoTaskMemFree")]
    named = [ctypes.addressof(names[0]) for _, names in sessions]
    assert freed == named  # every display name given back
    assert win.count("CoInitializeEx") == win.count("CoUninitialize") == 1

    win.impls["CoCreateInstance"] = lambda *args: w._signed(0x800401F0)  # CO_E_NOTINITIALIZED
    with pytest.raises(CaptureError, match="cannot list audio devices: 0x800401F0"):
        w.list_sessions()


def test_list_processes(win):
    table = [(4, 0, "System"), (100, 4, "player.exe")]
    position = [0]

    def fill(snap: int, entry_ref: Any) -> int:
        entry = entry_ref._obj
        assert snap == 77 and entry.dwSize == ctypes.sizeof(w.PROCESSENTRY32W)
        if position[0] >= len(table):
            return 0
        entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile = table[position[0]]
        position[0] += 1
        return 1

    win.impls["CreateToolhelp32Snapshot"] = lambda flags, pid: 77
    win.impls["Process32FirstW"] = fill
    win.impls["Process32NextW"] = fill
    assert w.list_processes() == [WinProcess(4, 0, "System"), WinProcess(100, 4, "player.exe")]
    assert win.args("CloseHandle") == [(77,)]
    win.impls["CreateToolhelp32Snapshot"] = lambda flags, pid: w.INVALID_HANDLE_VALUE
    with pytest.raises(CaptureError, match="cannot list processes"):
        w.list_processes()


# -- watching the process ----------------------------------------------------------------------


def test_handle_watch(win):
    watch = w.HandleWatch(555)
    assert not watch.exited()
    win.impls["WaitForSingleObject"] = lambda handle, ms: w.WAIT_OBJECT_0
    assert watch.exited()
    assert win.args("WaitForSingleObject") == [(555, 0), (555, 0)]
    watch.close()
    watch.close()
    assert win.args("CloseHandle") == [(555,)] and watch.exited()


def test_snapshot_watch(win, monkeypatch):
    running = [WinProcess(100, 4, "player.exe")]
    listings = [0]

    def list_processes() -> list[WinProcess]:
        listings[0] += 1
        return list(running)

    monkeypatch.setattr(w, "list_processes", list_processes)
    watch = w.SnapshotWatch(100, "player.exe", interval=0.0)
    assert not watch.exited()
    running[:] = [WinProcess(100, 4, "other.exe")]  # the pid was reused: still gone
    assert watch.exited()
    running[:] = [WinProcess(100, 4, "player.exe")]
    assert watch.exited()  # once gone, gone
    slow = w.SnapshotWatch(100, "player.exe", interval=60.0)
    before = listings[0]
    assert not slow.exited() and not slow.exited()
    assert listings[0] == before + 1  # asks Windows at most once per interval

    def failing() -> list[WinProcess]:
        raise CaptureError("cannot list processes")

    monkeypatch.setattr(w, "list_processes", failing)
    assert not w.SnapshotWatch(100, "player.exe", interval=0.0).exited()  # unknown: not gone


def test_backend_watch_falls_back_to_snapshots(win, monkeypatch):
    monkeypatch.setattr(w, "list_processes", lambda: [WinProcess(9, 4, "protected.exe")])
    win.impls["OpenProcess"] = lambda access, inherit, pid: 555
    watch = w.Wasapi().watch(9)
    assert isinstance(watch, w.HandleWatch) and watch.handle == 555
    assert win.args("OpenProcess") == [(w.SYNCHRONIZE, False, 9)]
    watch.close()
    win.impls["OpenProcess"] = lambda access, inherit, pid: None  # access denied
    watch = w.Wasapi().watch(9)
    assert isinstance(watch, w.SnapshotWatch) and watch.exe == "protected.exe"
