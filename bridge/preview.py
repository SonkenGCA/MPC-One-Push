"""Sample preview for the Browse page: plays a WAV/AIFF from your Places folders, once or looping.

Live's script API can only preview items from its own browser (browser.preview_item), and your own
Places folders aren't reachable through it (see places.py), so these play through Windows instead:
winmm PlaySound on the default output device. Standard library only. The file is decoded to 16-bit
PCM in memory (first PREVIEW_S seconds, at most two channels) on a worker thread, after the wheel has
rested for SETTLE_S, so scrolling fast through a folder doesn't queue up a burst of sounds.
"""

import array
import ctypes
import struct
import sys
import threading
import time

from waveform import _to_int16

SND_ASYNC, SND_NODEFAULT, SND_MEMORY, SND_LOOP = 0x0001, 0x0002, 0x0004, 0x0008
PREVIEW_S = 60          # longest stretch decoded (loops are short; this caps long stems and songs)
FLOAT_PREVIEW_S = 20    # float files are converted in pure Python, so they're capped shorter
SETTLE_S = 0.12
PREVIEWABLE = (".wav", ".aif", ".aiff")

_play = ctypes.windll.winmm.PlaySoundW
_play.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_play.restype = ctypes.c_int


def _chunks(f, big):
    """(id, offset, size) of each chunk after the 12-byte RIFF/FORM header."""
    f.seek(12)
    while True:
        head = f.read(8)
        if len(head) < 8:
            return
        size = struct.unpack(">I" if big else "<I", head[4:])[0]
        pos = f.tell()
        yield head[:4], pos, size
        f.seek(pos + size + (size & 1))


def decode(path):
    """WAV file bytes (16-bit PCM, 1-2 channels) of the start of a WAV/AIFF file."""
    with open(path, "rb") as f:
        head = f.read(12)
        big = head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC")
        if not big and not (head[:4] == b"RIFF" and head[8:12] == b"WAVE"):
            raise ValueError("not a WAV or AIFF file")
        tag, ch, rate, bits, data_at, data_len = 1, 1, 44100, 16, None, 0
        for cid, pos, size in _chunks(f, big):
            if cid == b"fmt ":
                f.seek(pos)
                fmt = f.read(size)
                tag, ch, rate, _br, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
                if tag == 0xFFFE and len(fmt) >= 26:           # WAVE_FORMAT_EXTENSIBLE: real tag in the GUID
                    tag = struct.unpack("<H", fmt[24:26])[0]
            elif cid == b"COMM":
                f.seek(pos)
                comm = f.read(size)
                ch, _frames, bits = struct.unpack(">hIh", comm[:8])
                rate = int(_ext80(comm[8:18]))
                if head[8:12] == b"AIFC" and comm[18:22] in (b"fl32", b"FL32"):
                    tag = 3
                elif head[8:12] == b"AIFC" and comm[18:22] == b"sowt":
                    big = False                                # little-endian AIFF-C
            elif cid in (b"data", b"SSND"):
                data_at, data_len = pos, size
                if cid == b"SSND":
                    f.seek(pos)
                    offset = struct.unpack(">I", f.read(4))[0]
                    data_at, data_len = pos + 8 + offset, size - 8 - offset
        if data_at is None or ch < 1 or bits < 8:
            raise ValueError("no audio in the file")
        width = bits // 8
        frame = width * ch
        seconds = FLOAT_PREVIEW_S if tag == 3 else PREVIEW_S
        f.seek(data_at)
        raw = f.read(min(data_len, rate * seconds * frame))
    raw = raw[:len(raw) - len(raw) % frame]
    if tag == 3:                                               # float -> int16
        a = array.array("f" if width == 4 else "d", raw)
        if big != (sys.byteorder == "big"):
            a.byteswap()
        pcm = array.array("h", (32767 if x >= 1.0 else -32767 if x <= -1.0 else int(x * 32767) for x in a))
    else:
        pcm = _to_int16(raw, width, big)
    if ch > 2:                                                 # keep the first two channels
        pcm = array.array("h", (pcm[i + k] for i in range(0, len(pcm), ch) for k in (0, 1)))
        ch = 2
    if sys.byteorder == "big":
        pcm.byteswap()
    body = pcm.tobytes()
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * 2, ch * 2, 16)
    return (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(body)) + body)


def _ext80(b):
    """AIFF's 80-bit extended float (the sample rate)."""
    exp = ((b[0] & 0x7F) << 8 | b[1]) - 16383
    mant = int.from_bytes(b[2:10], "big")
    return mant * 2.0 ** (exp - 63) if mant else 0.0


class Previewer:
    """Plays one sample at a time. play() returns at once; the newest request wins."""

    def __init__(self, on_error=None):
        self.on_error = on_error or (lambda msg: None)
        self.path = None          # what's playing (or about to)
        self.loop = False
        self._buf = None          # PlaySound reads from this while the sound plays, so it's kept alive
        self._want = None         # (path, loop, asked_at) for the worker
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cache = (None, None)
        threading.Thread(target=self._worker, name="preview", daemon=True).start()

    def play(self, path, loop=False):
        with self._lock:
            self.path, self.loop = path, loop
            self._want = (path, loop, time.monotonic())
        self._wake.set()

    def stop(self):
        with self._lock:
            self.path, self._want = None, None
            _play(None, None, 0)
            self._buf = None

    def set_loop(self, loop):
        """Turn looping on/off for what's playing (restarts it from the top)."""
        if self.path:
            self.play(self.path, loop)
        self.loop = loop

    def _worker(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                want = self._want
            if not want:
                continue
            wait = want[2] + SETTLE_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
                with self._lock:
                    if self._want is not want:        # the wheel moved on: the newer request takes over
                        continue
            path, loop = want[0], want[1]
            try:
                wav = self._cache[1] if self._cache[0] == path else decode(path)
                self._cache = (path, wav)
            except Exception as e:
                with self._lock:
                    if self._want is want:
                        self._want, self.path = None, None
                self.on_error("Can't preview this file (%s)" % (e.strerror if isinstance(e, OSError) else e))
                continue
            with self._lock:
                if self._want is not want:
                    continue
                buf = ctypes.create_string_buffer(wav, len(wav))
                _play(None, None, 0)                   # stop the old sound before its buffer goes away
                self._buf = buf
                _play(ctypes.addressof(buf), None, SND_MEMORY | SND_ASYNC | SND_NODEFAULT | (SND_LOOP if loop else 0))
                self._want = None
