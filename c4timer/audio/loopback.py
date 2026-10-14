"""WASAPI loopback capture (system-wide and per-process).

Two capture modes:
  * ``mode="system"``  - classic loopback on a render endpoint (the default one
    unless an explicit device id is given).
  * ``mode="process"`` - AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, which
    captures only the target process tree. Requires Windows 10 2004+
    (build 19041+); this is what keeps chat/music/browser audio out of the
    detector's input.

The capture always runs at the endpoint's own mix format. Resampling for
analysis happens later, in the detector, so the raw signal stays untouched.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_void_p, cast
from ctypes.wintypes import DWORD, LPCWSTR
from dataclasses import dataclass

from .combase import (
    AUDCLNT_BUFFERFLAGS_SILENT,
    AUDCLNT_SHAREMODE_SHARED,
    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
    AUDCLNT_STREAMFLAGS_LOOPBACK,
    AUDIOCLIENT_ACTIVATION_PARAMS,
    AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
    AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS,
    E_PENDING,
    GUID,
    HRESULT,
    IID_IAudioCaptureClient,
    IID_IAudioClient,
    IMMDeviceEnumerator,
    N_IAUDIOCAPTURECLIENT,
    N_IAUDIOCLIENT,
    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
    PROPVARIANT,
    REFERENCE_TIME,
    S_OK,
    VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
    VT_BLOB,
    WAVEFORMATEX,
    WAVEFORMATEXTENSIBLE,
    VT_IAUDIOCAPTURECLIENT,
    VT_IAUDIOCLIENT,
    async_operation,
    check,
    eCapture,
    eConsole,
    eRender,
    ole32,
    release,
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

SUBTYPE_PCM = "{00000001-0000-0010-8000-00AA00389B71}"
SUBTYPE_IEEE_FLOAT = "{00000003-0000-0010-8000-00AA00389B71}"

TH32CS_SNAPPROCESS = 0x00000002
CS2_EXE_NAMES = ("cs2.exe",)


@dataclass(frozen=True)
class Format:
    sample_rate: int
    channels: int
    bytes_per_sample: int

    @property
    def block_align(self) -> int:
        return self.channels * self.bytes_per_sample

    @property
    def sample_width(self) -> int:
        return self.bytes_per_sample

    def __str__(self) -> str:
        return f"{self.sample_rate}Hz/{self.channels}ch/{self.bytes_per_sample * 8}bit"


def _make_wave_format(fmt: Format) -> WAVEFORMATEX:
    """32-bit float WAVEFORMATEX - the format WASAPI shared mode speaks."""
    wf = WAVEFORMATEX()
    wf.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
    wf.nChannels = fmt.channels
    wf.nSamplesPerSec = fmt.sample_rate
    wf.wBitsPerSample = fmt.bytes_per_sample * 8
    wf.nBlockAlign = fmt.block_align
    wf.nAvgBytesPerSec = fmt.sample_rate * fmt.block_align
    wf.cbSize = 0
    return wf


def _format_from_waveformat(ptr) -> Format:
    wf = cast(ptr, POINTER(WAVEFORMATEX)).contents
    tag, bits = wf.wFormatTag, wf.wBitsPerSample
    if tag == WAVE_FORMAT_EXTENSIBLE:
        ext = cast(ptr, POINTER(WAVEFORMATEXTENSIBLE)).contents
        sub = str(ext.SubFormat).upper()
        if SUBTYPE_PCM in sub:
            tag = WAVE_FORMAT_PCM
        elif SUBTYPE_IEEE_FLOAT in sub:
            tag = WAVE_FORMAT_IEEE_FLOAT
        else:
            raise OSError(f"unknown WAVE_FORMAT_EXTENSIBLE SubFormat {sub}")
    if tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT):
        raise OSError(f"unsupported mix format tag 0x{tag:04X}")
    return Format(sample_rate=wf.nSamplesPerSec, channels=wf.nChannels, bytes_per_sample=bits // 8)


class AudioClient:
    """Wrapper over IAudioClient + IAudioCaptureClient."""

    def __init__(self, client_ptr, fmt: Format, *, owns_client: bool = True):
        self._client_ptr = client_ptr
        self._client_call = VT_IAUDIOCLIENT.build(client_ptr, N_IAUDIOCLIENT)
        self.fmt = fmt
        self._capture_ptr = c_void_p()
        self._capture_call = None
        self._event = None
        self._closed = False
        self._started = False

    # -- construction ------------------------------------------------------
    @classmethod
    def from_device(cls, device, buffer_ms: int | None = None) -> "AudioClient":
        client_ptr = device.activate(IID_IAudioClient)
        return cls._init(client_ptr, buffer_ms=buffer_ms, fixed=None)

    @classmethod
    def from_process(cls, pid: int, fmt: Format | None = None,
                     buffer_ms: int | None = None) -> "AudioClient":
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = (
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE)
        client_ptr = _activate_client_for_process(params)
        # The process-loopback virtual device has no mix format, so we choose one.
        return cls._init(client_ptr, buffer_ms=buffer_ms, fixed=fmt or Format(48000, 2, 4))

    @classmethod
    def _init(cls, client_ptr, *, buffer_ms: int | None, fixed: Format | None) -> "AudioClient":
        call = VT_IAUDIOCLIENT.build(client_ptr, N_IAUDIOCLIENT)

        mix = POINTER(WAVEFORMATEX)()
        hr = call("GetMixFormat", byref(mix))
        mix_fmt = None
        if hr >= 0 and mix:
            mix_fmt = _format_from_waveformat(mix)

        if fixed is not None:
            fmt = fixed
            wf = _make_wave_format(fmt)
            wf_ptr = ctypes.pointer(wf)
        elif mix_fmt is not None:
            fmt = mix_fmt
            wf_ptr = mix  # reuse the engine's own format verbatim
        else:
            check(hr, "IAudioClient::GetMixFormat")
            raise AssertionError("unreachable")

        period = REFERENCE_TIME()
        min_period = REFERENCE_TIME()
        call("GetDevicePeriod", byref(period), byref(min_period))

        # Shared mode requires the buffer duration to be a multiple of the period.
        base = max(period.value, min_period.value, 10_000)  # >= 1ms
        want = max(buffer_ms or 200, 20) * 10_000
        duration = max(base, (want // base) * base)

        handle = kernel32.CreateEventW(None, False, False, None)
        if not handle:
            if mix:
                ole32.CoTaskMemFree(mix)
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            check(
                call(
                    "Initialize",
                    AUDCLNT_SHAREMODE_SHARED,
                    flags,
                    duration,
                    0,
                    wf_ptr,
                    None,
                ),
                f"IAudioClient::Initialize({fmt}) [duration={duration} "
                f"period={base} mix={'yes' if mix else 'no'}]",
            )
            check(call("SetEventHandle", ctypes.c_void_p(handle)), "IAudioClient::SetEventHandle")
            cap = c_void_p()
            check(
                call("GetService", byref(IID_IAudioCaptureClient), byref(cap)),
                "IAudioClient::GetService(IAudioCaptureClient)",
            )
        except Exception:
            kernel32.CloseHandle(handle)
            release(client_ptr)
            raise
        finally:
            if mix:
                ole32.CoTaskMemFree(mix)

        obj = cls(client_ptr, fmt)
        obj._capture_ptr = cap
        obj._capture_call = VT_IAUDIOCAPTURECLIENT.build(cap, N_IAUDIOCAPTURECLIENT)
        obj._event = handle
        return obj

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        check(self._client_call("Start"), "IAudioClient::Start")
        self._started = True

    def stop(self) -> None:
        if self._started and not self._closed:
            self._client_call("Stop")
            self._started = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._client_call("Stop")
            self._started = False
        if self._capture_ptr:
            self._capture_call("Release")
            self._capture_ptr = c_void_p()
        self._client_call("Release")
        self._client_ptr = c_void_p()
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None

    def __enter__(self) -> "AudioClient":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # -- reading -----------------------------------------------------------
    def wait(self, timeout_ms: int = 200) -> bool:
        return kernel32.WaitForSingleObject(self._event, timeout_ms) == 0  # WAIT_OBJECT_0

    def buffer_size(self) -> int:
        n = ctypes.c_uint()
        check(self._client_call("GetBufferSize", byref(n)), "IAudioClient::GetBufferSize")
        return n.value

    def padding(self) -> int:
        n = ctypes.c_uint()
        check(self._client_call("GetCurrentPadding", byref(n)), "IAudioClient::GetCurrentPadding")
        return n.value

    def read_packet(self) -> bytes | None:
        """Drain one packet; returns raw interleaved float32 bytes or None."""
        data = POINTER(ctypes.c_byte)()
        frames = ctypes.c_uint()
        flags = DWORD()
        hr = self._capture_call(
            "GetBuffer", byref(data), byref(frames), byref(flags), None, None
        )
        if hr < 0:
            raise OSError(f"IAudioCaptureClient::GetBuffer failed: 0x{hr & 0xFFFFFFFF:08X}")
        if frames.value == 0:
            return None
        try:
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                return b"\x00" * (frames.value * self.fmt.block_align)
            return ctypes.string_at(data, frames.value * self.fmt.block_align)
        finally:
            self._capture_call("ReleaseBuffer", frames.value)

    def read_chunk(self, timeout_ms: int = 200, max_packets: int = 256) -> bytes:
        """Wait for data, then drain everything currently queued."""
        if not self.wait(timeout_ms):
            return b""
        out = bytearray()
        for _ in range(max_packets):
            pkt = self.read_packet()
            if pkt is None:
                break
            out += pkt
        return bytes(out)


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------
@dataclass
class RenderDevice:
    device_id: str
    name: str
    is_default: bool


def list_render_devices() -> list[RenderDevice]:
    out: list[RenderDevice] = []
    with ComInitShim():
        enum = IMMDeviceEnumerator()
        try:
            default_id = ""
            try:
                dev = enum.get_default_audio_endpoint(eRender, eConsole)
                try:
                    default_id = dev.device_id()
                finally:
                    dev.release()
            except OSError:
                pass
            coll = enum.enum_audio_endpoints(eRender)
            try:
                for i in range(coll.count()):
                    dev = coll.item(i)
                    try:
                        did = dev.device_id()
                        out.append(RenderDevice(did, dev.friendly_name(), did == default_id))
                    finally:
                        dev.release()
            finally:
                coll.release()
        finally:
            enum.release()
    return out


class ComInitShim:
    """Alias kept so call sites read naturally next to IMM* usage."""

    def __init__(self):
        from .combase import ComInit

        self._inner = ComInit()

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def open_system_capture(device_id: str | None = None, buffer_ms: int | None = None) -> AudioClient:
    with ComInitShim():
        enum = IMMDeviceEnumerator()
        try:
            dev = (enum.get_device(device_id) if device_id
                   else enum.get_default_audio_endpoint(eRender, eConsole))
            try:
                return AudioClient.from_device(dev, buffer_ms=buffer_ms)
            finally:
                dev.release()
        finally:
            enum.release()


# --------------------------------------------------------------------------
# Process loopback activation
# --------------------------------------------------------------------------
class _CompletionHandler:
    """IActivateAudioInterfaceCompletionHandler signalled via a Win32 event.

    The callback hands us the IActivateAudioInterfaceAsyncOperation; it may only
    be used (and only be queried) *after* this callback has fired. Querying it
    earlier fails with E_ILLEGAL_METHOD_CALL (0x8000000E).
    """

    def __init__(self):
        self.event = kernel32.CreateEventW(None, True, False, None)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self.operation = c_void_p()  # borrowed reference, owned by the caller
        self.completed = False
        self._refs = 1
        # ctypes rejects bound methods: each vtable entry must be an actual
        # C function pointer, and holding them here keeps them from being GC'd.
        self._keepalive = [
            _HANDLER_QI(self._qi),
            _HANDLER_ADDREF(self._addref),
            _HANDLER_RELEASE(self._release),
            _HANDLER_COMPLETED(self._completed),
        ]
        self._vtbl = _HANDLER_VTBL(*self._keepalive)
        self._obj = _HANDLER_OBJ(ctypes.pointer(self._vtbl))
        self.ptr = cast(ctypes.pointer(self._obj), c_void_p)

    def _qi(self, this, riid, ppv):
        cast(ppv, POINTER(c_void_p))[0] = this
        self._refs += 1
        return S_OK

    def _addref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        return self._refs

    def _completed(self, this, op):
        self.operation = c_void_p(op)
        self.completed = True
        kernel32.SetEvent(self.event)
        return S_OK

    def wait(self, timeout_ms: int = 8000) -> bool:
        if self.completed:
            return True
        if kernel32.WaitForSingleObject(self.event, timeout_ms) != 0:
            return False
        return self.completed

    def close(self):
        if self.event:
            kernel32.CloseHandle(self.event)
            self.event = None


from ctypes import WINFUNCTYPE  # noqa: E402

_HANDLER_QI = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_HANDLER_ADDREF = WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_HANDLER_RELEASE = WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_HANDLER_COMPLETED = WINFUNCTYPE(HRESULT, c_void_p, c_void_p)


class _HANDLER_VTBL(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _HANDLER_QI),
        ("AddRef", _HANDLER_ADDREF),
        ("Release", _HANDLER_RELEASE),
        ("ActivateCompleted", _HANDLER_COMPLETED),
    ]


class _HANDLER_OBJ(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_HANDLER_VTBL))]


_ACTIVATE_ASYNC = None


def _activate_async_fn():
    global _ACTIVATE_ASYNC
    if _ACTIVATE_ASYNC is None:
        mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
        fn = mmdevapi.ActivateAudioInterfaceAsync
        fn.restype = HRESULT
        fn.argtypes = [LPCWSTR, POINTER(GUID), c_void_p, c_void_p, POINTER(c_void_p)]
        _ACTIVATE_ASYNC = fn
    return _ACTIVATE_ASYNC


def _activate_client_for_process(params: AUDIOCLIENT_ACTIVATION_PARAMS):
    # ActivateAudioInterfaceAsync needs a COM apartment on this thread; without
    # it the call fails with CO_E_NOTINITIALIZED (0x800401F0).
    with ComInitShim():
        return _activate_client_for_process_inner(params)


def _activate_client_for_process_inner(params: AUDIOCLIENT_ACTIVATION_PARAMS):
    handler = _CompletionHandler()
    op = c_void_p()
    # ActivateAudioInterfaceAsync requires activationParams to be a
    # CoTaskMemAlloc'd PROPVARIANT-shaped blob (vt = VT_BLOB); a plain stack
    # pointer is rejected with E_INVALIDARG (0x80070057).
    blob_size = ctypes.sizeof(PROPVARIANT) + ctypes.sizeof(params)
    blob = ole32.CoTaskMemAlloc(blob_size)
    if not blob:
        raise MemoryError("CoTaskMemAlloc for activation params failed")
    try:
        ctypes.memset(blob, 0, blob_size)
        pv = PROPVARIANT.from_address(blob)
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(blob + ctypes.sizeof(PROPVARIANT), POINTER(ctypes.c_byte))
        ctypes.memmove(blob + ctypes.sizeof(PROPVARIANT), byref(params), ctypes.sizeof(params))

        hr = _activate_async_fn()(
            LPCWSTR(str(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK)),
            byref(IID_IAudioClient),
            ctypes.c_void_p(blob),
            handler.ptr,
            byref(op),
        )
        if hr < 0:
            check(hr, "ActivateAudioInterfaceAsync")
        # S_OK means the completion handler already ran; E_PENDING means we wait.
        if not handler.wait(8000):
            raise OSError("ActivateAudioInterfaceAsync never completed")

        async_op = handler.operation if handler.operation.value else op
        if not async_op.value:
            raise OSError("ActivateAudioInterfaceAsync returned no operation")

        call = async_operation(async_op)
        result = HRESULT()
        client = c_void_p()
        check(call("GetActivateResult", byref(result), byref(client)),
              "IActivateAudioInterfaceAsyncOperation::GetActivateResult")
        check(result.value, "process loopback activation")
        return client
    finally:
        ole32.CoTaskMemFree(ctypes.c_void_p(blob))
        handler.close()


# --------------------------------------------------------------------------
# Game process lookup
# --------------------------------------------------------------------------
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def find_process_ids(names: tuple[str, ...] = CS2_EXE_NAMES) -> list[int]:
    wanted = {n.lower() for n in names}
    pids: list[int] = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFFFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def open_capture(mode: str = "auto", device_id: str | None = None,
                 buffer_ms: int | None = None, process_names: tuple[str, ...] = CS2_EXE_NAMES):
    """Open the best available capture.

    Returns ``(client, description, warnings)``. Process loopback is preferred
    because it keeps chat/music out of the detector, but it is rejected with
    E_INVALIDARG on Windows 10 builds before 20348; that is recorded once in
    ``warnings`` rather than raised, so startup always succeeds.
    """
    warnings: list[str] = []

    if mode in ("auto", "process"):
        pids = find_process_ids(tuple(process_names))
        if pids:
            last_err: Exception | None = None
            for pid in pids:
                try:
                    client = AudioClient.from_process(pid, buffer_ms=buffer_ms)
                    return client, f"process loopback pid={pid} ({client.fmt})", warnings
                except OSError as exc:
                    last_err = exc
            warnings.append(
                f"process loopback unavailable ({last_err}); using system loopback - "
                "other apps' audio will be captured, so silence chat/music while playing"
            )
            if mode == "process":
                raise OSError(f"process loopback unavailable: {last_err}")
        elif mode == "process":
            raise OSError(f"no process matching {process_names}")
        else:
            warnings.append(
                f"no process matching {process_names}; using system loopback"
            )

    client = open_system_capture(device_id=device_id, buffer_ms=buffer_ms)
    return client, f"system loopback ({client.fmt})", warnings
