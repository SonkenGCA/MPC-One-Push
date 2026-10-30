"""Waveform peaks for the Simpler view: read a WAV/AIFF file and return (min, max) per screen column.

Standard library only. Handles PCM 8/16/24/32-bit and 32/64-bit float WAV (incl. WAVE_FORMAT_EXTENSIBLE)
and 16/24-bit AIFF. Uses the first channel and, for long files, a subset of frames per column, so a
few-minute sample still takes well under a second.
"""

import array
import struct
import sys


def _chunks(data, start, big):
    i = start
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = struct.unpack(">I" if big else "<I", data[i + 4:i + 8])[0]
        yield cid, data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)


def _to_int16(raw, width, big):
    """Raw interleaved samples -> array of int16 (keeps the top 16 bits)."""
    if width == 1:
        return array.array("h", ((b - 128) << 8 for b in raw))
    if width == 2:
        a = array.array("h", raw)
        if big != (sys.byteorder == "big"):
            a.byteswap()
        return a
    n = len(raw) // width
    b = bytearray(n * 2)
    hi = 0 if big else width - 1                 # most significant byte of each sample
    b[1::2] = raw[hi::width][:n] if not big else raw[0::width][:n]
    b[0::2] = raw[hi - 1::width][:n] if not big else raw[1::width][:n]
    return array.array("h", bytes(b)) if sys.byteorder == "little" else array.array("h", bytes(b[::-1]))[::-1]


def read(path):
    """(samples, channels, scale): samples is an array whose values divide by `scale` into -1..1."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        fmt, raw = None, None
        for cid, body in _chunks(data, 12, False):
            if cid == b"fmt ":
                fmt = body
            elif cid == b"data":
                raw = body
        tag, ch, _rate, _br, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == 0xFFFE and len(fmt) >= 26:              # WAVE_FORMAT_EXTENSIBLE: real tag in the GUID
            tag = struct.unpack("<H", fmt[24:26])[0]
        if tag == 3:                                       # IEEE float
            a = array.array("f" if bits == 32 else "d", raw[:len(raw) - len(raw) % (bits // 8)])
            return a, ch, 1.0
        return _to_int16(raw, bits // 8, False), ch, 32768.0
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        ch, bits, raw = 1, 16, b""
        for cid, body in _chunks(data, 12, True):
            if cid == b"COMM":
                ch, _frames, bits = struct.unpack(">hIh", body[:8])
            elif cid == b"SSND":
                offset = struct.unpack(">I", body[:4])[0]
                raw = body[8 + offset:]
        return _to_int16(raw, bits // 8, True), ch, 32768.0
    raise ValueError("not a WAV or AIFF file")


def peaks(path, columns, start=0, end=None):
    """[(lo, hi), ...] for `columns` columns between frames start..end (first channel)."""
    samples, ch, scale = read(path)
    frames = len(samples) // ch
    end = frames if end is None else max(start + 1, min(end, frames))
    span = end - start
    out = []
    for c in range(columns):
        a = start + span * c // columns
        b = max(a + 1, start + span * (c + 1) // columns)
        step = max(1, (b - a) // 256)                      # at most ~256 frames looked at per column
        chunk = samples[a * ch:b * ch:ch * step]
        if not chunk:
            out.append((0.0, 0.0))
            continue
        out.append((min(chunk) / scale, max(chunk) / scale))
    return out
