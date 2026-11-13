"""WASAPI process loopback and audio-session listing through ctypes.

Used on Windows only, by :mod:`.winapp` when capture starts. It needs nothing beyond ctypes:
COM interfaces are called through their vtables, and the one COM object Windows calls back,
the completion handler of ``ActivateAudioInterfaceAsync``, is built here by hand. It is agile
(it answers IAgileObject and aggregates the free-threaded marshaler, like WRL's ``FtmBase`` in
Microsoft's ApplicationLoopback sample).

The module imports on any platform: it only defines structures (with fixed-width Windows
types), COM method signatures and logic. Windows DLLs are loaded on first use by :func:`api`,
which refuses on other platforms. Tests install fake DLLs there (``_api``) and run this code on
Linux against COM objects made in Python.

COM objects are used only on the thread that created them, inside a COM apartment that thread
entered (:class:`_Apartment`).

If Windows never completes an activation, its completion handler and the parameters it reads
stay alive until the process exits: at most :data:`MAX_PENDING_ACTIVATIONS` of them, after which
activation refuses with an error until one completes. Should Windows release such a handler
while Python is shutting down, its callback could run after the interpreter is gone; nothing
in Python can prevent that, and it needs Windows to have stalled first.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from ctypes import POINTER, byref, c_void_p, sizeof
from typing import Any

import numpy as np

from .capture import CaptureError
from .winapp import (
    SESSION_STATES,
    WINDOWS_MIN_BUILD,
    WinProcess,
    WinSession,
    hresult_text,
    to_samples,
)

log = logging.getLogger("sound_event_detector")

# Windows types with their Windows widths (Windows is LLP64: LONG and ULONG are 32-bit).
HRESULT = ctypes.c_int32
LONG = ctypes.c_int32
BOOL = ctypes.c_int32
ULONG = ctypes.c_uint32
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
WORD = ctypes.c_uint16
ENUM = ctypes.c_int32  # C enums
REFERENCE_TIME = ctypes.c_int64
HANDLE = c_void_p
LPCWSTR = ctypes.c_wchar_p


def _signed(code: int) -> int:
    """An HRESULT as the signed 32-bit value COM methods return."""
    return code - (1 << 32) if code & 0x80000000 else code


S_OK = 0
S_FALSE = 1
E_NOINTERFACE = _signed(0x80004002)
E_POINTER = _signed(0x80004003)
E_UNEXPECTED = _signed(0x8000FFFF)
RPC_E_CHANGED_MODE = _signed(0x80010106)
AUDCLNT_S_BUFFER_EMPTY = 0x08890001

COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 0x17
E_RENDER = 0  # EDataFlow eRender
DEVICE_STATE_ACTIVE = 0x1
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
STREAM_FLAGS = (
    AUDCLNT_STREAMFLAGS_LOOPBACK
    | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
    | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
)
BUFFER_HNS = 10_000_000  # 1 s of buffer (100 ns units): room for a busy Python thread
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
CHANNEL_MASKS = {1: 0x4, 2: 0x3}  # front centre; front left + right
TH32CS_SNAPPROCESS = 0x2
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
INVALID_HANDLE_VALUE = c_void_p(-1).value
ACTIVATE_TIMEOUT = 5.0
MAX_PENDING_ACTIVATIONS = 3  # activations Windows has not completed before we stop asking


# -- structures --------------------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ULONG),
        ("Data2", WORD),
        ("Data3", WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
IID_IMarshal = _guid("00000003-0000-0000-C000-000000000046")
IID_IAgileObject = _guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
IID_IActivateAudioInterfaceCompletionHandler = _guid("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = _guid("00000003-0000-0010-8000-00AA00389B71")


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", ENUM)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    # The C type has a union of one member here, the process loopback parameters.
    _fields_ = [
        ("ActivationType", ENUM),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", ULONG), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    # Only the VT_BLOB member of the union is used; it is as large as the union.
    _fields_ = [
        ("vt", WORD),
        ("wReserved1", WORD),
        ("wReserved2", WORD),
        ("wReserved3", WORD),
        ("blob", BLOB),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", WORD),  # a union with wSamplesPerBlock
        ("dwChannelMask", DWORD),
        ("SubFormat", GUID),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", LONG),
        ("dwFlags", DWORD),
        ("szExeFile", ctypes.c_wchar * 260),  # WCHAR is 16-bit on Windows
    ]


# -- Windows functions -------------------------------------------------------------------------


def _fn(lib: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    f = getattr(lib, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


class Api:
    """The Windows functions used here, from the DLLs ``load(name)`` returns."""

    def __init__(self, load: Callable[[str], Any]) -> None:
        self._load = load
        ole32 = load("ole32")
        kernel32 = load("kernel32")
        self.CoInitializeEx = _fn(ole32, "CoInitializeEx", HRESULT, c_void_p, DWORD)
        self.CoUninitialize = _fn(ole32, "CoUninitialize", None)
        self.CoCreateInstance = _fn(
            ole32, "CoCreateInstance", HRESULT,
            POINTER(GUID), c_void_p, DWORD, POINTER(GUID), POINTER(c_void_p),
        )  # fmt: skip
        self.CoTaskMemFree = _fn(ole32, "CoTaskMemFree", None, c_void_p)
        self.CoCreateFreeThreadedMarshaler = _fn(
            ole32, "CoCreateFreeThreadedMarshaler", HRESULT, c_void_p, POINTER(c_void_p)
        )
        self.CreateEventW = _fn(kernel32, "CreateEventW", HANDLE, c_void_p, BOOL, BOOL, LPCWSTR)
        self.WaitForSingleObject = _fn(kernel32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
        self.CloseHandle = _fn(kernel32, "CloseHandle", BOOL, HANDLE)
        self.OpenProcess = _fn(kernel32, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
        self.CreateToolhelp32Snapshot = _fn(
            kernel32, "CreateToolhelp32Snapshot", HANDLE, DWORD, DWORD
        )
        self.Process32FirstW = _fn(
            kernel32, "Process32FirstW", BOOL, HANDLE, POINTER(PROCESSENTRY32W)
        )
        self.Process32NextW = _fn(
            kernel32, "Process32NextW", BOOL, HANDLE, POINTER(PROCESSENTRY32W)
        )
        self._activate: Any = None

    @property
    def ActivateAudioInterfaceAsync(self) -> Any:
        """From Mmdevapi.dll, loaded on first use."""
        if self._activate is None:
            try:
                f = self._load("mmdevapi").ActivateAudioInterfaceAsync
            except (OSError, AttributeError) as e:
                raise CaptureError(f"ActivateAudioInterfaceAsync is not available: {e}") from e
            f.restype = HRESULT
            f.argtypes = (LPCWSTR, POINTER(GUID), c_void_p, c_void_p, POINTER(c_void_p))
            self._activate = f
        return self._activate


def _windll(name: str) -> Any:
    if sys.platform != "win32":
        raise CaptureError("WASAPI is only available on Windows")
    # Own WinDLL instances, so argtypes set here never clash with another library's.
    return ctypes.WinDLL(name, use_last_error=True)


_api: Api | None = None
_api_lock = threading.Lock()


def api() -> Api:
    """The Windows functions, loaded on first use (Windows only)."""
    global _api
    with _api_lock:
        if _api is None:
            if sys.platform != "win32":
                raise CaptureError("WASAPI is only available on Windows")
            _api = Api(_windll)
        return _api


def windows_build() -> int:
    if sys.platform != "win32":
        raise CaptureError("not running on Windows")
    return sys.getwindowsversion().build


def _last_error() -> int:
    get = getattr(ctypes, "get_last_error", None)  # Windows only
    return int(get()) if get is not None else 0


# -- calling COM interfaces --------------------------------------------------------------------

# stdcall on 32-bit Windows; on x64 (and elsewhere, for tests) the C convention is the same.
_W = ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE
# Method signatures; the first argument is always the interface pointer itself.
QueryInterfaceFn = _W(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
AddRefFn = _W(ULONG, c_void_p)
ReleaseFn = _W(ULONG, c_void_p)
NoArgsFn = _W(HRESULT, c_void_p)  # IAudioClient::Start/Stop, IsSystemSoundsSession
OutPtrFn = _W(HRESULT, c_void_p, POINTER(c_void_p))
OutU32Fn = _W(HRESULT, c_void_p, POINTER(UINT))
IndexOutFn = _W(HRESULT, c_void_p, UINT, POINTER(c_void_p))  # Item, GetSession
EnumAudioEndpointsFn = _W(HRESULT, c_void_p, ENUM, DWORD, POINTER(c_void_p))
DeviceActivateFn = _W(HRESULT, c_void_p, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p))
InitializeFn = _W(
    HRESULT, c_void_p, ENUM, DWORD, REFERENCE_TIME, REFERENCE_TIME, c_void_p, c_void_p
)
SetEventHandleFn = _W(HRESULT, c_void_p, HANDLE)
GetServiceFn = _W(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
GetBufferFn = _W(
    HRESULT, c_void_p,
    POINTER(c_void_p), POINTER(UINT), POINTER(DWORD), c_void_p, c_void_p,
)  # fmt: skip
ReleaseBufferFn = _W(HRESULT, c_void_p, UINT)
GetActivateResultFn = _W(HRESULT, c_void_p, POINTER(HRESULT), POINTER(c_void_p))
ActivateCompletedFn = _W(HRESULT, c_void_p, c_void_p)

# vtable slots (IUnknown takes 0-2 everywhere)
QUERY_INTERFACE, ADD_REF, RELEASE = 0, 1, 2
ENUMERATOR_ENUM_AUDIO_ENDPOINTS = 3
COLLECTION_GET_COUNT, COLLECTION_ITEM = 3, 4
DEVICE_ACTIVATE = 3
SESSION_MANAGER2_GET_SESSION_ENUMERATOR = 5
SESSION_ENUMERATOR_GET_COUNT, SESSION_ENUMERATOR_GET_SESSION = 3, 4
SESSION_CONTROL_GET_STATE, SESSION_CONTROL_GET_DISPLAY_NAME = 3, 4
SESSION_CONTROL2_GET_PROCESS_ID, SESSION_CONTROL2_IS_SYSTEM_SOUNDS = 14, 15
CLIENT_INITIALIZE, CLIENT_START, CLIENT_STOP = 3, 10, 11
CLIENT_SET_EVENT_HANDLE, CLIENT_GET_SERVICE = 13, 14
CAPTURE_GET_BUFFER, CAPTURE_RELEASE_BUFFER, CAPTURE_GET_NEXT_PACKET_SIZE = 3, 4, 5
OPERATION_GET_ACTIVATE_RESULT = 3
HANDLER_ACTIVATE_COMPLETED = 3


def vtable_method(ptr: int, index: int, prototype: Any) -> Any:
    """Method ``index`` of the COM interface at ``ptr``, callable as ``f(ptr, *args)``."""
    vtable = ctypes.cast(c_void_p(ptr), POINTER(POINTER(c_void_p))).contents
    return prototype(vtable[index])


class _Com:
    """One owned reference to a COM interface."""

    def __init__(self, ptr: int | None, what: str) -> None:
        if not ptr:
            raise CaptureError(f"{what}: Windows returned no interface")
        self.ptr: int | None = ptr
        self.what = what

    def call(self, index: int, prototype: Any, *args: Any, what: str) -> int:
        if not self.ptr:
            raise CaptureError(f"{what}: {self.what} was already released")
        hr = int(vtable_method(self.ptr, index, prototype)(self.ptr, *args))
        if hr < 0:
            raise CaptureError(f"{what} failed: {hresult_text(hr)}")
        return hr

    def out(self, index: int, prototype: Any, *args: Any, what: str) -> _Com:
        """Call a method whose last argument receives an interface pointer."""
        ptr = c_void_p()
        self.call(index, prototype, *args, byref(ptr), what=what)
        return _Com(ptr.value, what)

    def query(self, iid: GUID, what: str) -> _Com:
        return self.out(QUERY_INTERFACE, QueryInterfaceFn, byref(iid), what=what)

    def release(self) -> None:
        ptr, self.ptr = self.ptr, None
        if ptr:
            vtable_method(ptr, RELEASE, ReleaseFn)(ptr)


class _Scope:
    """Releases the interfaces added to it when the ``with`` block ends."""

    def __init__(self) -> None:
        self._items: list[_Com] = []

    def add(self, com: _Com) -> _Com:
        self._items.append(com)
        return com

    def __enter__(self) -> _Scope:
        return self

    def __exit__(self, *exc: object) -> None:
        while self._items:
            self._items.pop().release()


class _Apartment:
    """COM entered on this thread (multithreaded apartment) until :meth:`close`."""

    def __init__(self) -> None:
        hr = int(api().CoInitializeEx(None, COINIT_MULTITHREADED))
        if hr == RPC_E_CHANGED_MODE:
            self._entered = False  # the thread is in a single-threaded apartment: usable as is
        elif hr < 0:
            raise CaptureError(f"cannot initialise COM: {hresult_text(hr)}")
        else:
            self._entered = True  # S_OK, or S_FALSE when already entered: both need closing

    def close(self) -> None:
        if self._entered:
            self._entered = False
            api().CoUninitialize()


@contextlib.contextmanager
def _com() -> Iterator[None]:
    apartment = _Apartment()
    try:
        yield
    finally:
        apartment.close()


# -- the completion handler (a COM object made in Python) -------------------------------------

_HANDLER_IIDS = frozenset(
    bytes(g) for g in (IID_IUnknown, IID_IActivateAudioInterfaceCompletionHandler, IID_IAgileObject)
)
_IMARSHAL = bytes(IID_IMarshal)
_live_handlers: set[CompletionHandler] = set()  # handlers Windows may still call
_live_lock = threading.Lock()


class _HandlerObject(ctypes.Structure):
    _fields_ = [("vtable", POINTER(c_void_p * 4))]


class CompletionHandler:
    """``IActivateAudioInterfaceCompletionHandler``: sets :attr:`done` when activation ends.

    Windows holds references to it from its own threads, so it stays alive (in
    ``_live_handlers``) until the last reference is released, together with ``keep`` (the
    activation parameters Windows reads).
    """

    def __init__(self, keep: tuple[object, ...] = ()) -> None:
        self.done = threading.Event()
        self.keep = keep
        self._refs = 1  # ours, given up by release()
        self._lock = threading.Lock()
        self._callbacks = (
            QueryInterfaceFn(self._query_interface),
            AddRefFn(self._add_ref),
            ReleaseFn(self._release),
            ActivateCompletedFn(self._activate_completed),
        )
        self._vtable = (c_void_p * 4)(*(ctypes.cast(f, c_void_p).value for f in self._callbacks))
        self._object = _HandlerObject(ctypes.pointer(self._vtable))
        self.ptr: int = ctypes.addressof(self._object)
        inner = c_void_p()
        # The free-threaded marshaler, aggregated: it answers IMarshal for this object.
        hr = int(api().CoCreateFreeThreadedMarshaler(self.ptr, byref(inner)))
        self._marshaler: int | None = inner.value if hr >= 0 else None
        with _live_lock:
            _live_handlers.add(self)

    def release(self) -> None:
        self._release(self.ptr)

    def _query_interface(self, this: int, riid: Any, ppv: Any) -> int:
        try:
            if not ppv:
                return E_POINTER
            iid = bytes(riid.contents)
            if iid in _HANDLER_IIDS:
                ppv[0] = this
                self._add_ref(this)
                return S_OK
            marshaler = self._marshaler
            if iid == _IMARSHAL and marshaler:
                return int(
                    vtable_method(marshaler, QUERY_INTERFACE, QueryInterfaceFn)(
                        marshaler, riid, ppv
                    )
                )
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:
            return E_UNEXPECTED

    def _add_ref(self, this: int) -> int:
        with self._lock:
            self._refs += 1
            return self._refs

    def _release(self, this: int) -> int:
        with self._lock:
            self._refs -= 1
            refs = self._refs
        if refs == 0:
            marshaler, self._marshaler = self._marshaler, None
            if marshaler:
                vtable_method(marshaler, RELEASE, ReleaseFn)(marshaler)
            with _live_lock:
                _live_handlers.discard(self)
        return max(refs, 0)

    def _activate_completed(self, this: int, operation: int) -> int:
        self.done.set()
        return S_OK


def pending_activations() -> int:
    """Activations started but not completed by Windows (their handlers are still alive)."""
    with _live_lock:
        return sum(1 for h in _live_handlers if not h.done.is_set())


def _activate_process_loopback(pid: int) -> _Com:
    """An ``IAudioClient`` capturing process ``pid`` and its child processes (not initialised)."""
    pending = pending_activations()
    if pending >= MAX_PENDING_ACTIVATIONS:
        raise CaptureError(
            f"Windows has not completed {pending} earlier process-loopback activations; "
            "not starting another (restart the program if this persists)"
        )
    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = pid
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )
    prop = PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = sizeof(params)
    prop.blob.pBlobData = ctypes.addressof(params)
    handler = CompletionHandler(keep=(params, prop))
    operation_ptr = c_void_p()
    try:
        hr = int(
            api().ActivateAudioInterfaceAsync(
                VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                byref(IID_IAudioClient),
                byref(prop),
                c_void_p(handler.ptr),
                byref(operation_ptr),
            )
        )
        if hr < 0:
            raise CaptureError(f"ActivateAudioInterfaceAsync failed: {hresult_text(hr)}")
        with _Scope() as scope:
            operation = scope.add(_Com(operation_ptr.value, "activation operation"))
            if not handler.done.wait(ACTIVATE_TIMEOUT):
                raise CaptureError(f"activating process loopback for pid {pid} timed out")
            result = HRESULT()
            unknown = c_void_p()
            operation.call(
                OPERATION_GET_ACTIVATE_RESULT, GetActivateResultFn, byref(result), byref(unknown),
                what="GetActivateResult",
            )  # fmt: skip
            activated = (
                scope.add(_Com(unknown.value, "activated interface")) if unknown.value else None
            )
            if result.value < 0 or activated is None:
                raise CaptureError(
                    f"process loopback for pid {pid} failed: {hresult_text(result.value)}"
                )
            return activated.query(IID_IAudioClient, "IAudioClient")  # a reference of its own
    finally:
        handler.release()


# -- capture -----------------------------------------------------------------------------------


def wave_format(sample_rate: int, channels: int, sample_format: str) -> ctypes.Structure:
    """The format asked of Windows: 32-bit float (WAVEFORMATEXTENSIBLE) or 16-bit PCM."""
    if sample_format == "f32":
        ext = WAVEFORMATEXTENSIBLE()
        fmt = ext.Format
        fmt.wFormatTag = WAVE_FORMAT_EXTENSIBLE
        fmt.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX)
        bits = 32
        ext.wValidBitsPerSample = bits
        ext.dwChannelMask = CHANNEL_MASKS.get(channels, 0)
        ext.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
        result: ctypes.Structure = ext
    else:
        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.cbSize = 0
        bits = 16
        result = fmt
    fmt.nChannels = channels
    fmt.nSamplesPerSec = sample_rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = channels * bits // 8
    fmt.nAvgBytesPerSec = sample_rate * fmt.nBlockAlign
    return result


class LoopbackStream:
    """A started process-loopback capture (see :class:`.winapp.LoopbackStream`)."""

    def __init__(
        self,
        client: _Com,
        capture: _Com,
        event: int,
        sample_format: str,
        channels: int,
        apartment: _Apartment,
    ) -> None:
        self.client = client
        self.capture = capture
        self.event: int | None = event
        self.sample_format = sample_format
        self.channels = channels
        self.frame_bytes = channels * (4 if sample_format == "f32" else 2)
        self.apartment = apartment
        self._glitch_logged = False

    def read(self, timeout: float) -> np.ndarray:
        api().WaitForSingleObject(self.event, max(0, round(timeout * 1000)))
        blocks: list[np.ndarray] = []
        size = UINT()
        data = c_void_p()
        frames = UINT()
        flags = DWORD()
        while len(blocks) < 256:
            self.capture.call(
                CAPTURE_GET_NEXT_PACKET_SIZE, OutU32Fn, byref(size), what="GetNextPacketSize"
            )
            if size.value == 0:
                break
            hr = self.capture.call(
                CAPTURE_GET_BUFFER, GetBufferFn,
                byref(data), byref(frames), byref(flags), None, None,
                what="GetBuffer",
            )  # fmt: skip
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                break
            n = frames.value
            silent = bool(flags.value & AUDCLNT_BUFFERFLAGS_SILENT)
            ptr = data.value
            # Copied out before the buffer goes back to Windows; silence is not read at all.
            raw = ctypes.string_at(ptr, n * self.frame_bytes) if n and ptr and not silent else b""
            self.capture.call(CAPTURE_RELEASE_BUFFER, ReleaseBufferFn, n, what="ReleaseBuffer")
            if flags.value & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY and not self._glitch_logged:
                self._glitch_logged = True
                log.debug("Process loopback reported a gap in the audio (capture was late)")
            blocks.append(to_samples(raw, n, self.channels, self.sample_format, silent=silent))
        if not blocks:
            return np.zeros((0, self.channels), np.float32)
        return blocks[0] if len(blocks) == 1 else np.concatenate(blocks)

    def close(self) -> None:
        if self.client.ptr:
            with contextlib.suppress(CaptureError):
                self.client.call(CLIENT_STOP, NoArgsFn, what="Stop")
        self.capture.release()
        self.client.release()
        event, self.event = self.event, None
        if event:
            api().CloseHandle(event)
        self.apartment.close()


def _start(
    client: _Com, sample_format: str, channels: int, apartment: _Apartment
) -> LoopbackStream:
    event = api().CreateEventW(None, False, False, None)
    if not event:
        raise CaptureError(f"CreateEvent failed (error {_last_error()})")
    capture: _Com | None = None
    try:
        client.call(CLIENT_SET_EVENT_HANDLE, SetEventHandleFn, event, what="SetEventHandle")
        capture = client.out(
            CLIENT_GET_SERVICE, GetServiceFn, byref(IID_IAudioCaptureClient),
            what="GetService(IAudioCaptureClient)",
        )  # fmt: skip
        client.call(CLIENT_START, NoArgsFn, what="Start")
        return LoopbackStream(client, capture, event, sample_format, channels, apartment)
    except BaseException:
        if capture is not None:
            capture.release()
        api().CloseHandle(event)
        raise


def open_stream(pid: int, sample_rate: int, channels: int) -> LoopbackStream:
    """Start capturing process ``pid`` and its child processes, as float32 at ``sample_rate``.

    Windows converts to the rate and channel count asked for. 32-bit float is tried first,
    then 16-bit PCM (the format of Microsoft's sample), each with a fresh client.
    """
    build = windows_build()
    if build < WINDOWS_MIN_BUILD:
        raise CaptureError(
            "capturing one application needs Windows 10 version 2004 "
            f"(build {WINDOWS_MIN_BUILD}) or later; this is build {build}"
        )
    apartment = _Apartment()
    try:
        refused = []
        for sample_format in ("f32", "s16"):
            client = _activate_process_loopback(pid)
            try:
                fmt = wave_format(sample_rate, channels, sample_format)
                try:
                    client.call(
                        CLIENT_INITIALIZE, InitializeFn,
                        AUDCLNT_SHAREMODE_SHARED, STREAM_FLAGS, BUFFER_HNS, 0, byref(fmt), None,
                        what=sample_format,
                    )  # fmt: skip
                except CaptureError as e:
                    refused.append(str(e))
                    client.release()
                    continue
                return _start(client, sample_format, channels, apartment)
            except BaseException:
                client.release()
                raise
        raise CaptureError(
            f"Windows refused to capture pid {pid} at {sample_rate} Hz, {channels} channel(s) "
            f"({'; '.join(refused)})"
        )
    except BaseException:
        apartment.close()
        raise


# -- processes and sessions --------------------------------------------------------------------


def list_processes() -> list[WinProcess]:
    """Every running process with its parent and executable name (a Toolhelp snapshot)."""
    win = api()
    snap = win.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise CaptureError(f"cannot list processes (error {_last_error()})")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = sizeof(PROCESSENTRY32W)
        processes = []
        ok = win.Process32FirstW(snap, byref(entry))
        while ok:
            processes.append(
                WinProcess(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile)
            )
            ok = win.Process32NextW(snap, byref(entry))
        return processes
    finally:
        win.CloseHandle(snap)


def _session(control: _Com, scope: _Scope) -> WinSession:
    control2 = scope.add(control.query(IID_IAudioSessionControl2, "IAudioSessionControl2"))
    state = UINT()
    control.call(SESSION_CONTROL_GET_STATE, OutU32Fn, byref(state), what="GetState")
    name_ptr = c_void_p()
    control.call(SESSION_CONTROL_GET_DISPLAY_NAME, OutPtrFn, byref(name_ptr), what="GetDisplayName")
    try:
        name = ctypes.wstring_at(name_ptr.value) if name_ptr.value else ""
    finally:
        api().CoTaskMemFree(name_ptr)
    pid = UINT()
    control2.call(SESSION_CONTROL2_GET_PROCESS_ID, OutU32Fn, byref(pid), what="GetProcessId")
    system = control2.call(SESSION_CONTROL2_IS_SYSTEM_SOUNDS, NoArgsFn, what="IsSystem") == S_OK
    states = SESSION_STATES
    return WinSession(
        pid.value, states[state.value] if state.value < len(states) else "expired", name, system
    )


def _device_sessions(devices: _Com, index: int) -> list[WinSession]:
    with _Scope() as scope:
        device = scope.add(
            devices.out(COLLECTION_ITEM, IndexOutFn, index, what="IMMDeviceCollection::Item")
        )
        manager = scope.add(
            device.out(
                DEVICE_ACTIVATE, DeviceActivateFn,
                byref(IID_IAudioSessionManager2), CLSCTX_ALL, None,
                what="activating IAudioSessionManager2",
            )
        )  # fmt: skip
        enumerator = scope.add(
            manager.out(
                SESSION_MANAGER2_GET_SESSION_ENUMERATOR, OutPtrFn, what="GetSessionEnumerator"
            )
        )
        count = UINT()
        enumerator.call(SESSION_ENUMERATOR_GET_COUNT, OutU32Fn, byref(count), what="GetCount")
        sessions = []
        for i in range(count.value):
            try:
                control = scope.add(
                    enumerator.out(SESSION_ENUMERATOR_GET_SESSION, IndexOutFn, i, what="GetSession")
                )
                sessions.append(_session(control, scope))
            except CaptureError as e:
                log.debug("Skipping audio session %d of device %d: %s", i, index, e)
        return sessions


def list_sessions() -> list[WinSession]:
    """Audio sessions on every active output device."""
    with _com(), _Scope() as scope:
        ptr = c_void_p()
        hr = int(
            api().CoCreateInstance(
                byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                byref(IID_IMMDeviceEnumerator), byref(ptr),
            )
        )  # fmt: skip
        if hr < 0:
            raise CaptureError(f"cannot list audio devices: {hresult_text(hr)}")
        enumerator = scope.add(_Com(ptr.value, "IMMDeviceEnumerator"))
        devices = scope.add(
            enumerator.out(
                ENUMERATOR_ENUM_AUDIO_ENDPOINTS, EnumAudioEndpointsFn,
                E_RENDER, DEVICE_STATE_ACTIVE,
                what="EnumAudioEndpoints",
            )
        )  # fmt: skip
        count = UINT()
        devices.call(COLLECTION_GET_COUNT, OutU32Fn, byref(count), what="GetCount")
        sessions = []
        for i in range(count.value):
            try:
                sessions.extend(_device_sessions(devices, i))
            except CaptureError as e:
                log.debug("Skipping audio device %d: %s", i, e)
        return sessions


class HandleWatch:
    """Whether a process has exited, from a handle to it."""

    def __init__(self, handle: int) -> None:
        self.handle: int | None = handle

    def exited(self) -> bool:
        return self.handle is None or api().WaitForSingleObject(self.handle, 0) == WAIT_OBJECT_0

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            api().CloseHandle(handle)


class SnapshotWatch:
    """For a process Windows will not open (a protected one): looks for it once a second."""

    def __init__(self, pid: int, exe: str, interval: float = 1.0) -> None:
        self.pid = pid
        self.exe = exe
        self.interval = interval
        self._next = 0.0
        self._gone = False

    def exited(self) -> bool:
        now = time.monotonic()
        if not self._gone and now >= self._next:
            self._next = now + self.interval
            try:
                running = list_processes()
            except CaptureError:
                return False  # cannot tell this time; ask again later
            self._gone = not any(p.pid == self.pid and p.exe == self.exe for p in running)
        return self._gone

    def close(self) -> None:
        pass


class Wasapi:
    """The Windows side of :class:`.winapp.WindowsAppSource` (its ``Backend``)."""

    def processes(self) -> list[WinProcess]:
        return list_processes()

    def sessions(self) -> list[WinSession]:
        return list_sessions()

    def watch(self, pid: int) -> HandleWatch | SnapshotWatch:
        handle = api().OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            return HandleWatch(handle)
        exe = next((p.exe for p in list_processes() if p.pid == pid), "")
        return SnapshotWatch(pid, exe)

    def open_stream(self, pid: int, sample_rate: int, channels: int) -> LoopbackStream:
        return open_stream(pid, sample_rate, channels)
