"""Minimal COM helpers for talking to WASAPI from ctypes.

Deliberately dependency-free: the sounddevice/PortAudio wheels on PyPI ship a
PortAudio build that does *not* expose WASAPI loopback devices, and the usual
workaround (PyAudioWPatch) has no Python 3.12 wheel. Talking to the COM
interfaces directly also gives us per-process loopback, which is what keeps
Discord/music/browser audio out of the detector's input.

Vtable slots are declared with `Vtbl`, a sequential builder. Hand-written slot
indices are easy to get wrong and fail silently, so every interface is spelled
out in declaration order instead.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_void_p, cast
from ctypes.wintypes import DWORD, LPCWSTR, WORD

ole32 = ctypes.WinDLL("ole32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HRESULT = ctypes.c_long

ole32.CLSIDFromString.argtypes = [LPCWSTR, ctypes.c_void_p]
ole32.CLSIDFromString.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
ole32.CoTaskMemAlloc.restype = c_void_p

S_OK = 0
S_FALSE = 1
E_PENDING = 0x8000000A
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

AUDCLNT_S_BUFFER_EMPTY = 0x08890001
AUDCLNT_S_NO_SINGLE_DEVICE = 0x08890005

# PROPVARIANT vt tags
VT_EMPTY = 0
VT_LPWSTR = 31
VT_BLOB = 65


class GUID(Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, spec: str):
        super().__init__()
        ole32.CLSIDFromString(LPCWSTR(spec), byref(self))

    def __repr__(self) -> str:
        d4 = "".join(f"{b:02x}" for b in self.Data4)
        return f"{{{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{d4[:4]}-{d4[4:]}}}"


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]

    def __init__(self, fmtid: GUID, pid: int):
        super().__init__()
        self.fmtid = fmtid
        self.pid = pid


PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
PKEY_Device_DeviceDesc = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 2)

# --------------------------------------------------------------------------
# Interface / class ids
# --------------------------------------------------------------------------
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = GUID("{D66E0A90-8A1B-4A31-8B0F-5C4C6B8E5B9A}")

DEVICE_STATE_ACTIVE = 0x00000001
DEVICE_STATEMASK_ALL = 0x0000000F

eRender, eCapture, eAll = 0, 1, 2
eConsole, eMultimedia, eCommunications = 0, 1, 2

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_SHAREMODE_EXCLUSIVE = 1

AUDCLNT_STREAMFLAGS_CROSSPROCESS = 0x00010000
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_NOPERSIST = 0x00080000

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4

AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 0x17

REFERENCE_TIME = ctypes.c_longlong


# --------------------------------------------------------------------------
# Structs
# --------------------------------------------------------------------------
class WAVEFORMATEX(Structure):
    _pack_ = 1  # MMReg.h is #pragma pack(1); without this ctypes pads to 20
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class WAVEFORMATEXTENSIBLE(Structure):
    # Must stay 40 bytes to match MMReg.h; ctypes' native alignment would make
    # it 44 and silently shift SubFormat by 4 bytes, corrupting the GUID.
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
        ("Samples", WORD),
        ("dwChannelMask", DWORD),
        ("SubFormat", GUID),
    ]


assert ctypes.sizeof(WAVEFORMATEX) == 18, ctypes.sizeof(WAVEFORMATEX)
assert ctypes.sizeof(WAVEFORMATEXTENSIBLE) == 40, ctypes.sizeof(WAVEFORMATEXTENSIBLE)


class BLOB(Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", POINTER(ctypes.c_byte))]


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("pwszVal", ctypes.c_wchar_p),
        ("blob", BLOB),
        ("ulVal", ctypes.c_ulong),
        ("uhVal", ctypes.c_ulonglong),
        ("_raw", ctypes.c_byte * 16),
    ]


class PROPVARIANT(Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("_u", _PROPVARIANT_UNION),
    ]

    def reset(self) -> None:
        self.vt = VT_EMPTY


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


# --------------------------------------------------------------------------
# Vtable plumbing
# --------------------------------------------------------------------------
class Vtbl:
    """Sequential COM vtable builder.

    ``slot(fn_type)`` appends one entry and returns its index, so the order of
    calls in an interface definition mirrors the C++ declaration exactly.
    ``build(iface_ptr)`` produces a caller: ``call(ptr, name, *args)``.
    """

    def __init__(self):
        self._fns: list[tuple[str, object]] = []

    def slot(self, name: str, restype, *argtypes) -> int:
        # WINFUNCTYPE must be reached as an attribute lookup on the module each
        # time: storing it in a class attribute yields a bound method, which
        # ctypes rejects as a restype.
        self._fns.append((name, ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)))
        return len(self._fns) - 1

    def names(self) -> list[str]:
        return [n for n, _ in self._fns]

    def validate(self, names: list[str]) -> None:
        got = self.names()
        if got != names:
            raise AssertionError(f"vtable mismatch:\n  declared {got}\n  expected {names}")

    def build(self, ptr, names: list[str]):
        self.validate(names)
        vtbl = cast(ptr, POINTER(POINTER(c_void_p)))[0]

        def call(name: str, *args):
            index = names.index(name)
            fn = self._fns[index][1]
            bound = fn(vtbl[index])
            return bound(ptr, *args)

        return call


# --- IUnknown -------------------------------------------------------------
def _iunknown(vt: Vtbl) -> None:
    vt.slot("QueryInterface", HRESULT, POINTER(GUID), POINTER(c_void_p))
    vt.slot("AddRef", ctypes.c_ulong)
    vt.slot("Release", ctypes.c_ulong)


# --- IMMDeviceCollection --------------------------------------------------
def _immdevicecollection(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("GetCount", HRESULT, POINTER(ctypes.c_uint))
    vt.slot("Item", HRESULT, ctypes.c_uint, POINTER(c_void_p))


# --- IMMDevice ------------------------------------------------------------
def _immdevice(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("Activate", HRESULT, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p))
    vt.slot("OpenPropertyStore", HRESULT, DWORD, POINTER(c_void_p))
    vt.slot("GetId", HRESULT, POINTER(ctypes.c_wchar_p))
    vt.slot("GetState", HRESULT, POINTER(DWORD))


# --- IMMDeviceEnumerator --------------------------------------------------
def _immdeviceenumerator(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("EnumAudioEndpoints", HRESULT, ctypes.c_int, DWORD, POINTER(c_void_p))
    vt.slot("GetDefaultAudioEndpoint", HRESULT, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))
    vt.slot("GetDevice", HRESULT, LPCWSTR, POINTER(c_void_p))
    vt.slot("RegisterEndpointNotificationCallback", HRESULT, c_void_p)
    vt.slot("UnregisterEndpointNotificationCallback", HRESULT, c_void_p)


# --- IPropertyStore -------------------------------------------------------
def _ipropertystore(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("GetCount", HRESULT, POINTER(DWORD))
    vt.slot("GetAt", HRESULT, DWORD, POINTER(PROPERTYKEY))
    vt.slot("GetValue", HRESULT, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))
    vt.slot("SetValue", HRESULT, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))
    vt.slot("Commit", HRESULT)


# --- IAudioClient (WASAPI 1.0) --------------------------------------------
_AUDIO_CLIENT_SLOTS = [
    "QueryInterface", "AddRef", "Release",
    "Initialize", "GetBufferSize", "GetStreamLatency", "GetCurrentPadding",
    "IsFormatSupported", "GetMixFormat", "GetDevicePeriod",
    "Start", "Stop", "Reset", "SetEventHandle", "GetService",
]


def _audioclient(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("Initialize", HRESULT, ctypes.c_int, DWORD, REFERENCE_TIME, REFERENCE_TIME,
            POINTER(WAVEFORMATEX), POINTER(GUID))
    vt.slot("GetBufferSize", HRESULT, POINTER(ctypes.c_uint))
    vt.slot("GetStreamLatency", HRESULT, POINTER(REFERENCE_TIME))
    vt.slot("GetCurrentPadding", HRESULT, POINTER(ctypes.c_uint))
    vt.slot("IsFormatSupported", HRESULT, ctypes.c_int, POINTER(WAVEFORMATEX),
            POINTER(POINTER(WAVEFORMATEX)))
    vt.slot("GetMixFormat", HRESULT, POINTER(POINTER(WAVEFORMATEX)))
    vt.slot("GetDevicePeriod", HRESULT, POINTER(REFERENCE_TIME), POINTER(REFERENCE_TIME))
    vt.slot("Start", HRESULT)
    vt.slot("Stop", HRESULT)
    vt.slot("Reset", HRESULT)
    vt.slot("SetEventHandle", HRESULT, ctypes.c_void_p)
    vt.slot("GetService", HRESULT, POINTER(GUID), POINTER(c_void_p))


# --- IAudioCaptureClient --------------------------------------------------
def _audiocaptureclient(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("GetBuffer", HRESULT, POINTER(POINTER(ctypes.c_byte)), POINTER(ctypes.c_uint),
            POINTER(DWORD), POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))
    vt.slot("ReleaseBuffer", HRESULT, ctypes.c_uint)
    vt.slot("GetNextPacketSize", HRESULT, POINTER(ctypes.c_uint))


# --- IActivateAudioInterfaceAsyncOperation --------------------------------
def _asyncop(vt: Vtbl) -> None:
    _iunknown(vt)
    vt.slot("GetActivateResult", HRESULT, POINTER(HRESULT), POINTER(c_void_p))


def _vt(define) -> tuple[Vtbl, list[str]]:
    vt = Vtbl()
    define(vt)
    return vt, vt.names()


VT_IMMDEVICEENUMERATOR, N_IMMDEVICEENUMERATOR = _vt(_immdeviceenumerator)
VT_IMMDEVICECOLLECTION, N_IMMDEVICECOLLECTION = _vt(_immdevicecollection)
VT_IMMDEVICE, N_IMMDEVICE = _vt(_immdevice)
VT_IPROPERTYSTORE, N_IPROPERTYSTORE = _vt(_ipropertystore)
VT_IAUDIOCLIENT, N_IAUDIOCLIENT = _vt(_audioclient)
VT_IAUDIOCAPTURECLIENT, N_IAUDIOCAPTURECLIENT = _vt(_audiocaptureclient)
VT_IASYNC_OP, N_IASYNC_OP = _vt(_asyncop)

# Fail loudly at import time if an interface drifted from the C declaration.
assert N_IAUDIOCLIENT == _AUDIO_CLIENT_SLOTS, N_IAUDIOCLIENT


class ComInit:
    """RAII around CoInitializeEx; safe to nest."""

    def __init__(self, mode: int = COINIT_MULTITHREADED):
        self.hr = ole32.CoInitializeEx(None, mode)
        self.owned = self.hr == S_OK
        if self.hr == RPC_E_CHANGED_MODE:
            self.owned = False  # already initialised with another apartment model
        elif self.hr not in (S_OK, S_FALSE):
            raise OSError(f"CoInitializeEx failed: 0x{self.hr & 0xFFFFFFFF:08X}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.owned:
            ole32.CoUninitialize()
        return False


def check(hr: int, what: str) -> int:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def release(ptr) -> None:
    if ptr:
        try:
            VT_IMMDEVICE.build(ptr, N_IMMDEVICE)("Release")
        except Exception:
            pass


# --------------------------------------------------------------------------
# Interface wrappers
# --------------------------------------------------------------------------
class IMMDevice:
    def __init__(self, ptr):
        self.ptr = ptr
        self._call = VT_IMMDEVICE.build(ptr, N_IMMDEVICE)

    def device_id(self) -> str:
        sid = ctypes.c_wchar_p()
        check(self._call("GetId", byref(sid)), "IMMDevice::GetId")
        return sid.value or ""

    def state(self) -> int:
        st = DWORD()
        check(self._call("GetState", byref(st)), "IMMDevice::GetState")
        return st.value

    def activate(self, iid: GUID):
        out = c_void_p()
        check(
            self._call("Activate", byref(iid), CLSCTX_ALL, None, byref(out)),
            f"IMMDevice::Activate({iid})",
        )
        return out

    def friendly_name(self) -> str:
        for pkey in (PKEY_Device_FriendlyName, PKEY_Device_DeviceDesc):
            store = c_void_p()
            hr = self._call("OpenPropertyStore", 0, byref(store))
            if hr < 0:
                continue
            try:
                call = VT_IPROPERTYSTORE.build(store, N_IPROPERTYSTORE)
                pv = PROPVARIANT()
                pv.reset()
                if call("GetValue", byref(pkey), byref(pv)) >= 0 and pv.vt == VT_LPWSTR:
                    name = pv.pwszVal
                    if name:
                        return name
            finally:
                release(store)
        return "<unnamed>"

    def release(self):
        self._call("Release")
        self.ptr = c_void_p()


class IMMDeviceCollection:
    def __init__(self, ptr):
        self.ptr = ptr
        self._call = VT_IMMDEVICECOLLECTION.build(ptr, N_IMMDEVICECOLLECTION)

    def count(self) -> int:
        n = ctypes.c_uint()
        check(self._call("GetCount", byref(n)), "IMMDeviceCollection::GetCount")
        return n.value

    def item(self, index: int) -> IMMDevice:
        dev = c_void_p()
        check(self._call("Item", index, byref(dev)), "IMMDeviceCollection::Item")
        return IMMDevice(dev)

    def release(self):
        self._call("Release")
        self.ptr = c_void_p()


class IMMDeviceEnumerator:
    def __init__(self):
        self.ptr = c_void_p()
        check(
            ole32.CoCreateInstance(
                byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                byref(IID_IMMDeviceEnumerator), byref(self.ptr),
            ),
            "CoCreateInstance(MMDeviceEnumerator)",
        )
        self._call = VT_IMMDEVICEENUMERATOR.build(self.ptr, N_IMMDEVICEENUMERATOR)

    def enum_audio_endpoints(self, flow: int,
                             state: int = DEVICE_STATE_ACTIVE) -> IMMDeviceCollection:
        coll = c_void_p()
        check(self._call("EnumAudioEndpoints", flow, state, byref(coll)),
              "IMMDeviceEnumerator::EnumAudioEndpoints")
        return IMMDeviceCollection(coll)

    def get_default_audio_endpoint(self, flow: int, role: int = eConsole) -> IMMDevice:
        dev = c_void_p()
        check(self._call("GetDefaultAudioEndpoint", flow, role, byref(dev)),
              "IMMDeviceEnumerator::GetDefaultAudioEndpoint")
        return IMMDevice(dev)

    def get_device(self, device_id: str) -> IMMDevice:
        dev = c_void_p()
        check(self._call("GetDevice", LPCWSTR(device_id), byref(dev)),
              "IMMDeviceEnumerator::GetDevice")
        return IMMDevice(dev)

    def release(self):
        self._call("Release")
        self.ptr = c_void_p()


def async_operation(ptr):
    return VT_IASYNC_OP.build(ptr, N_IASYNC_OP)
