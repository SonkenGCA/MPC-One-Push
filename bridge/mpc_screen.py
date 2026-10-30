"""Drive the MPC One's touchscreen from the PC over USB (controller mode, MPC 3 closed).

Protocol, reverse-engineered 2026-10-28 from MPC 3 3.9.1 talking to MPC One firmware 2.10.0.
USB interface 5 "Akai Professional Remote Screen" (WinUSB, VID 09E8 PID 1046):

  ep 0x08 OUT / 0x88 IN (interrupt, 10-byte "M5" packets)
      06 00 00 00 01 4D 35 00 00 <cmd>
      host 00 = hello -> device 03;  host 01 = ping (every 2 s) -> device 04;  host 06 after a ping
  ep 0x06 OUT / 0x87 IN (bulk, framed messages)
      u32 LE length-of-rest, u8 type, ...
      type 0 "smex" key/value:  u32 keylen, key, u32 vallen, value      (both directions)
      type 2 screen rectangle: u32 0, u16 x, y, w, h, zlib(RGB888 rows)  (host -> device)

Screen is 1280x800. Nothing is ever written to the MPC's storage; this is the same live
display stream MPC 3 uses, and the MPC drops it when the cable or app goes away.
"""

import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time
import uuid
import zlib

WIDTH, HEIGHT = 1280, 800
SCREEN_GUID = "{48b66355-0a68-4106-aac6-35804251c614}"
EP_BULK_OUT, EP_BULK_IN, EP_INT_OUT, EP_INT_IN = 0x06, 0x87, 0x08, 0x88
M5 = bytes.fromhex("06000000014d350000")
HELLO, PING, PING_ACK = 0x00, 0x01, 0x06

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_winusb = ctypes.WinDLL("winusb", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _IFDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wt.DWORD),
                ("Reserved", ctypes.c_size_t)]


_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                  wt.DWORD, ctypes.POINTER(_IFDATA)]
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IFDATA), ctypes.c_void_p,
                                                       wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                  ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_winusb.WinUsb_Initialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_winusb.WinUsb_Free.argtypes = [ctypes.c_void_p]
_winusb.WinUsb_WritePipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wt.ULONG,
                                     ctypes.POINTER(wt.ULONG), ctypes.c_void_p]
_winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wt.ULONG,
                                    ctypes.POINTER(wt.ULONG), ctypes.c_void_p]
_winusb.WinUsb_SetPipePolicy.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, wt.ULONG, wt.ULONG, ctypes.c_void_p]
_winusb.WinUsb_AbortPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]

_PIPE_TRANSFER_TIMEOUT = 0x03
_INVALID = ctypes.c_void_p(-1).value


class ScreenError(RuntimeError):
    pass


def device_paths(interface_guid):
    """All present device-interface paths for a class GUID (string form)."""
    u = uuid.UUID(interface_guid)
    g = _GUID(u.fields[0], u.fields[1], u.fields[2])
    g.Data4[:] = u.bytes[8:]
    info = _setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x2 | 0x10)
    paths = []
    try:
        i = 0
        while True:
            ifd = _IFDATA()
            ifd.cbSize = ctypes.sizeof(ifd)
            if not _setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(g), i, ctypes.byref(ifd)):
                break
            i += 1
            need = wt.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


def _device_path():
    paths = device_paths(SCREEN_GUID)
    return paths[0] if paths else None


def smex(key, value=""):
    k, v = key.encode(), str(value).encode()
    body = b"\x00" + struct.pack("<I", len(k)) + k + struct.pack("<I", len(v)) + v
    return struct.pack("<I", len(body)) + body


def parse_smex(msg):
    """msg = full message incl. length prefix. Returns (key, value) or None."""
    if len(msg) < 9 or msg[4] != 0:
        return None
    (kl,) = struct.unpack_from("<I", msg, 5)
    key = msg[9:9 + kl].decode("utf-8", "replace")
    (vl,) = struct.unpack_from("<I", msg, 9 + kl)
    return key, msg[13 + kl:13 + kl + vl].decode("utf-8", "replace")


def rect_message(x, y, w, h, rgb, level=1):
    """rgb: bytes of w*h*3, row-major."""
    z = zlib.compress(rgb, level)
    body = b"\x02" + b"\x00\x00\x00\x00" + struct.pack("<4H", x, y, w, h) + z
    msg = struct.pack("<I", len(body)) + body
    if len(msg) % 512 == 0:
        # Avoid an exact multiple of the packet size, which would need a zero-length packet.
        return rect_message(x, y, w, h, rgb, level=level + 1 if level < 9 else 0)
    return msg


class MpcScreen:
    def __init__(self, on_event=None):
        """on_event(kind, data): kind is 'm5' (int-IN packet) or 'msg' (bulk-IN message)."""
        self.on_event = on_event or (lambda kind, data: None)
        self._file = None
        self._h = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    # ------------------------------------------------------------ lifecycle

    def open(self):
        path = _device_path()
        if not path:
            raise ScreenError("MPC One screen not found. Is it plugged in over USB and in controller mode?")
        f = _kernel32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0x40000000, None)
        if f in (None, _INVALID):
            raise ScreenError("Can't open the MPC screen (error %d). Close MPC 3 first." % ctypes.get_last_error())
        h = ctypes.c_void_p()
        if not _winusb.WinUsb_Initialize(f, ctypes.byref(h)):
            _kernel32.CloseHandle(f)
            raise ScreenError("WinUsb_Initialize failed (error %d)" % ctypes.get_last_error())
        self._file, self._h = f, h
        for ep, ms in ((EP_INT_IN, 250), (EP_BULK_IN, 250), (EP_INT_OUT, 1000), (EP_BULK_OUT, 5000)):
            t = wt.ULONG(ms)
            _winusb.WinUsb_SetPipePolicy(h, ep, _PIPE_TRANSFER_TIMEOUT, 4, ctypes.byref(t))
        for target in (self._read_int, self._read_bulk, self._heartbeat):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._threads.append(th)
        self._m5(HELLO)
        for key, val in (("smex.protocolversion", "1"), ("smex.version", "1"),
                         ("smex.screensaver.set", "off"), ("smex.battery", ""),
                         ("smex.brightness.request", ""), ("smex.time.set", str(int(time.time() * 1000)))):
            self.send(smex(key, val))
        return self

    def close(self):
        self._stop.set()
        if self._h:
            for ep in (EP_INT_IN, EP_BULK_IN):
                _winusb.WinUsb_AbortPipe(self._h, ep)
        for th in self._threads:
            th.join(timeout=1)
        if self._h:
            _winusb.WinUsb_Free(self._h)
            _kernel32.CloseHandle(self._file)
        self._h = self._file = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ output

    def _write(self, ep, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        n = wt.ULONG()
        with self._lock:
            ok = _winusb.WinUsb_WritePipe(self._h, ep, buf, len(data), ctypes.byref(n), None)
        if not ok:
            raise ScreenError("write to ep %02X failed (error %d)" % (ep, ctypes.get_last_error()))
        return n.value

    def _m5(self, cmd):
        self._write(EP_INT_OUT, M5 + bytes([cmd]))

    def send(self, msg):
        self._write(EP_BULK_OUT, msg)

    def draw(self, x, y, w, h, rgb):
        self.send(rect_message(x, y, w, h, rgb))

    def draw_full(self, rgb):
        self.draw(0, 0, WIDTH, HEIGHT, rgb)

    def brightness(self, level):
        self.send(smex("smex.brightness.set", str(level)))

    # ------------------------------------------------------------ input

    def _read(self, ep, size):
        buf = ctypes.create_string_buffer(size)
        n = wt.ULONG()
        if _winusb.WinUsb_ReadPipe(self._h, ep, buf, size, ctypes.byref(n), None):
            return buf.raw[:n.value]
        return None

    def _read_int(self):
        while not self._stop.is_set():
            data = self._read(EP_INT_IN, 64)
            if data:
                self.on_event("m5", data)

    def _read_bulk(self):
        pending = b""
        while not self._stop.is_set():
            data = self._read(EP_BULK_IN, 65536)
            if not data:
                continue
            pending += data
            while len(pending) >= 4:
                (n,) = struct.unpack_from("<I", pending)
                if len(pending) < 4 + n:
                    break
                self.on_event("msg", pending[:4 + n])
                pending = pending[4 + n:]

    def _heartbeat(self):
        while not self._stop.wait(2.0):
            try:
                self._m5(PING)
                time.sleep(0.03)
                self._m5(PING_ACK)
            except ScreenError:
                pass
