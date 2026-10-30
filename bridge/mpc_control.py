r"""MPC One control surface (pads, knobs, buttons, pad/button LEDs) in controller mode.

Protocol, reverse-engineered 2026-10-28 from MPC 3 3.9.1 / MPC One firmware 2.10.0.
MPC 3 doesn't use Windows MIDI for this. It sends KS property requests to the Akai driver's
topology filter on USB interface 3:

    path  \\?\usb#vid_09e8&pid_1046&mi_03#...#{dda54a40-1e4c-11d1-a050-405705c10000}\{48b66355-...}
    set   a0634132-2f7f-4fda-93ba-ea6d7d1bdd0d
      setup, in this order:  2 SET u64 event handle (signalled on input), 0 GET, 1 GET, 5 SET u32 0, 4 SET u32 0
      id 11 GET  -> u32 "input waiting" flag
      id 12 GET  input {u64 0, ptr, size} -> fills *ptr with raw MIDI bytes; output {u64 ts, u32 count}
      id 13 SET  output buffer holds {u64 0, ptr, u32 size, u32 port} pointing at raw MIDI bytes to send.
                 MPC 3 uses port 0 for the session start/settings/LED setup and port 1 for most else.
    Every buffer handed to the driver is kept alive for the life of the connection: with short-lived
    buffers Python crashed with an access violation, so the driver seems to use some of them after
    the ioctl returns. Sends rotate through a ring of long-lived slots.

The bytes are ordinary MIDI / Akai SysEx (F0 47 7F 46 ...; 46 = MPC One):
    F0 7E 00 06 01 F7                         identity request
    F0 47 00 46 52 00 04 <4-byte token> F7    start controller session; resent on port 1 every 4 s as the
                                              keep-alive, each answered by heartbeat F0 47 7F 46 53 00 05 <token> 01 F7
    F0 47 7F 46 64 00 03 xx yy zz F7          / 63 / 41: settings and state queries sent at startup
    F0 47 7F 46 42 00 18 ...                  per-button LED setup (mpc_one_led_setup.hex); pads stay dark without it
    F0 47 7F 46 65 00 04 <pad 0-15> R G B F7  pad colour, 7-bit RGB
    Bn cc vv                                  button LEDs

Same calls MPC 3 makes. Nothing is written to the MPC's storage.
"""

import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time
import uuid

from mpc_screen import device_paths

KSCATEGORY_TOPOLOGY = "{dda54a40-1e4c-11d1-a050-405705c10000}"
AKAI_FILTER_REF = "{48b66355-0a68-4106-aac6-35804251c614}"
AKAI_SET = uuid.UUID("a0634132-2f7f-4fda-93ba-ea6d7d1bdd0d").bytes_le
IOCTL_KS_PROPERTY = 0x2F0003
KSPROPERTY_TYPE_GET, KSPROPERTY_TYPE_SET = 1, 2
PROP_PENDING, PROP_READ, PROP_WRITE = 11, 12, 13
SLOTS, SLOT_SIZE = 128, 1024

SESSION_TOKEN = bytes.fromhex("0a792915")    # what MPC 3 sent; it changes per run, and any value works
SESSION_START = bytes.fromhex("f0470046520004" + SESSION_TOKEN.hex() + "f7")
# MPC 3 resends the session start on port 1 every 4 s. Without it the device sends one heartbeat
# and drops the session: buttons and touch still arrive, pads don't (verified 2026-10-29).
KEEPALIVE_S = 4.0
# (hex, port) in MPC 3's order, from capture usbcap-20261028-235650 (the first with port-1 messages).
STARTUP = [
    ("f07e000601f7", 0),
    (SESSION_START.hex(), 0),
    ("f0477f4662000161f7", 1),       # meaning unknown; MPC 3 always sends it right after the session start
    ("f0477f46640003000101f7", 0),
    ("f0477f46640003030000f7", 0),
    ("f0477f46630003010000f7", 0),
    ("f0477f46410000f7", 0),
    ("f07e000601f7", 1),
]
# Sent by MPC 3 after the LED setup and around its first pad colours.
AFTER_LEDS = [
    ("f0477f465a00200000010800000207000000020e080f0600000001000000000000000100000000f7", 0),
    ("b03503", 0),
    ("b03501", 0),
    ("b00201", 1),
]

import os as _os

with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "mpc_one_led_setup.hex")) as _f:
    LED_SETUP = [bytes.fromhex(l.strip()) for l in _f if l.strip() and not l.startswith("#")]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                  ctypes.c_void_p]
_kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                      wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CreateEventW.restype = ctypes.c_void_p
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
_INVALID = ctypes.c_void_p(-1).value


class ControlError(RuntimeError):
    pass


def _ksproperty(prop_id, flags):
    return AKAI_SET + struct.pack("<II", prop_id, flags)


def find_filter():
    for p in device_paths(KSCATEGORY_TOPOLOGY):
        low = p.lower()
        if "vid_09e8&pid_1046&mi_03" in low and low.endswith(AKAI_FILTER_REF.lower()):
            return p
    return None


def split_midi(data):
    """Split a raw MIDI byte stream into messages (SysEx kept whole)."""
    out, i = [], 0
    while i < len(data):
        b = data[i]
        if b == 0xF0:
            j = data.find(b"\xf7", i)
            j = len(data) - 1 if j < 0 else j
            out.append(data[i:j + 1])
            i = j + 1
        elif b >= 0x80:
            n = {0xC0: 2, 0xD0: 2}.get(b & 0xF0, 3) if b < 0xF0 else 1
            out.append(data[i:i + n])
            i += n
        else:
            i += 1      # stray data byte (running status isn't used by the MPC as far as seen)
    return out


class MpcControl:
    def __init__(self, on_midi=None):
        """on_midi(bytes) is called for every incoming MIDI/SysEx message."""
        self.on_midi = on_midi or (lambda msg: None)
        self._h = None
        self._event = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._keepalive = None
        self._rx = ctypes.create_string_buffer(0x4000)
        self._rx_out = (ctypes.c_ubyte * 16)()
        self._rx_req = ctypes.create_string_buffer(
            _ksproperty(PROP_READ, KSPROPERTY_TYPE_GET)
            + struct.pack("<QQQ", 0, ctypes.addressof(self._rx), ctypes.sizeof(self._rx)), 48)
        self._pending_out = (ctypes.c_ubyte * 4)()
        self._reader_pending_out = (ctypes.c_ubyte * 4)()
        self._setup_out = (ctypes.c_ubyte * 8)()
        # Long-lived send slots: (request struct, data buffer).
        self._slots = [((ctypes.c_ubyte * 24)(), ctypes.create_string_buffer(SLOT_SIZE)) for _ in range(SLOTS)]
        self._next_slot = 0
        self._send_lock = threading.Lock()

    def _ioctl(self, inbuf, outbuf):
        got = wt.DWORD()
        with self._lock:
            ok = _kernel32.DeviceIoControl(self._h, IOCTL_KS_PROPERTY, inbuf, len(inbuf) if isinstance(inbuf, bytes)
                                           else ctypes.sizeof(inbuf), outbuf, ctypes.sizeof(outbuf),
                                           ctypes.byref(got), None)
        if not ok:
            raise ControlError("Akai driver request failed (error %d)" % ctypes.get_last_error())
        return got.value

    def open(self, startup=True):
        path = find_filter()
        if not path:
            raise ControlError("MPC One control surface not found (USB, controller mode?)")
        h = _kernel32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0x80, None)
        if h in (None, _INVALID):
            raise ControlError("Can't open the MPC control surface (error %d). Close MPC 3 first."
                               % ctypes.get_last_error())
        self._h = h
        # Same setup MPC 3 does before its first message; without it writes fail with error 31.
        self._event = _kernel32.CreateEventW(None, False, False, None)
        struct.pack_into("<Q", self._setup_out, 0, self._event)
        self._ioctl(_ksproperty(2, KSPROPERTY_TYPE_SET), self._setup_out)
        for prop, kind in ((0, KSPROPERTY_TYPE_GET), (1, KSPROPERTY_TYPE_GET),
                           (5, KSPROPERTY_TYPE_SET), (4, KSPROPERTY_TYPE_SET)):
            ctypes.memset(self._pending_out, 0, 4)
            self._ioctl(_ksproperty(prop, kind), self._pending_out)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        if startup:
            for hexmsg, port in STARTUP:
                self.send(bytes.fromhex(hexmsg), port)
                time.sleep(0.002)
            for msg in LED_SETUP:      # without this the pad LEDs stay dark
                self.send(msg)
            for hexmsg, port in AFTER_LEDS:
                self.send(bytes.fromhex(hexmsg), port)
            self._keepalive = threading.Thread(target=self._keep_alive, daemon=True)
            self._keepalive.start()
        return self

    def _keep_alive(self):
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self.send(SESSION_START, port=1)
            except ControlError as e:
                print("mpc_control keep-alive failed:", e, flush=True)

    def close(self):
        self._stop.set()
        for t in (self._thread, self._keepalive):
            if t:
                t.join(timeout=1)
        time.sleep(0.05)     # let the driver finish with the last sends before buffers can be freed
        if self._h:
            _kernel32.CloseHandle(self._h)
            _kernel32.CloseHandle(self._event)
        self._h = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ output

    def send(self, data, port=0):
        """port = last u32 of the send struct. MPC 3 uses 1 for most traffic (pad colours, CCs,
        a startup sequence we haven't decoded yet) and 0 for the session start and LED setup."""
        data = bytes(data)
        if len(data) > SLOT_SIZE:
            raise ValueError("message too long (%d bytes)" % len(data))
        with self._send_lock:
            req, buf = self._slots[self._next_slot]
            self._next_slot = (self._next_slot + 1) % SLOTS
            ctypes.memmove(buf, data, len(data))
            struct.pack_into("<QQII", req, 0, 0, ctypes.addressof(buf), len(data), port)
            # The driver queues only ~24 messages and answers error 31 when full; it drains within
            # a few ms, so back off and retry rather than fail.
            deadline = time.time() + 1.0
            while True:
                try:
                    self._ioctl(_ksproperty(PROP_WRITE, KSPROPERTY_TYPE_SET), req)
                    return
                except ControlError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.002)

    def pad_color(self, pad, r, g, b):
        """pad 0-15, colour components 0-127. Port 1, as MPC 3 sends nearly all of its pad colours."""
        self.send(bytes([0xF0, 0x47, 0x7F, 0x46, 0x65, 0x00, 0x04, pad & 0x7F,
                         r & 0x7F, g & 0x7F, b & 0x7F, 0xF7]), port=1)

    # ------------------------------------------------------------ input

    def _pending(self):
        out = self._reader_pending_out
        ctypes.memset(out, 0, 4)
        self._ioctl(_ksproperty(PROP_PENDING, KSPROPERTY_TYPE_GET), out)
        return struct.unpack("<I", bytes(out))[0]

    def _read(self):
        req = self._rx_req
        ctypes.memset(self._rx_out, 0, 16)
        ctypes.memset(self._rx, 0, ctypes.sizeof(self._rx))
        self._ioctl(req, self._rx_out)
        _ts, count = struct.unpack_from("<QI", bytes(self._rx_out))
        count = min(count, ctypes.sizeof(self._rx))
        # The driver can report the count a moment before it finishes copying the bytes in.
        # Wait until the tail of the data has landed (last byte non-zero), up to ~5 ms.
        for _ in range(50):
            data = self._rx.raw[:count]
            if not count or data[-1] != 0:
                break
            time.sleep(0.0001)
        return data

    def _reader(self):
        partial = b""     # SysEx split across reads
        while not self._stop.is_set():
            try:
                if not self._pending():
                    _kernel32.WaitForSingleObject(self._event, 20)
                    continue
                data = self._read()
            except ControlError:
                time.sleep(0.05)
                continue
            except Exception as e:      # never let the reader die silently
                print("mpc_control reader error:", repr(e), flush=True)
                time.sleep(0.05)
                continue
            data = partial + data
            partial = b""
            last_f0 = data.rfind(b"\xf0")
            if last_f0 >= 0 and data.find(b"\xf7", last_f0) < 0:
                data, partial = data[:last_f0], data[last_f0:]
            for msg in split_midi(data):
                self.on_midi(msg)
