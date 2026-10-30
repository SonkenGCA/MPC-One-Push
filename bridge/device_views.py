"""The maths behind the Device page's pictures: EQ Eight's curve, Auto Filter's response, compressor curves.

Live gives a script each parameter's raw value plus the text it shows ("469 Hz", "4.00 : 1"). Where the raw
value is a 0..1 position, the text is used. EQ Eight's mappings were measured in Live 12.4 (2026-10-29):
frequency = 10 Hz * 2200^value (10 Hz .. 22 kHz) and Q = 0.1 * 180^value (0.1 .. 18).
"""

import cmath
import math
import re

FS = 48000.0
F_LO, F_HI = 10.0, 22000.0


def number(text, default=0.0):
    """First number in Live's text for a value; "k" (kHz) multiplies by 1000, "inf" is a huge number."""
    t = str(text or "")
    if "inf" in t.lower():
        return -1e9 if "-inf" in t.lower() else 1e9
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        return default
    v = float(m.group())
    return v * 1000 if re.search(r"\d\s*k", t) else v


def x_of(freq):
    """0..1 position of a frequency on a 10 Hz .. 22 kHz log axis (the same as EQ Eight's knob position)."""
    return math.log(max(F_LO, freq) / F_LO) / math.log(F_HI / F_LO)


def freq_of(x):
    return F_LO * (F_HI / F_LO) ** x


def eq8_freq(v):
    return 10.0 * 2200.0 ** v


def eq8_q(v):
    return 0.1 * 180.0 ** v


def _biquad(kind, f, q, gain_db):
    """RBJ cookbook coefficients (b0, b1, b2, a0, a1, a2)."""
    w = 2 * math.pi * min(f, FS * 0.49) / FS
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2 * max(q, 0.05))
    a = 10 ** (gain_db / 40)
    if kind == "bell":
        return 1 + alpha * a, -2 * cw, 1 - alpha * a, 1 + alpha / a, -2 * cw, 1 - alpha / a
    if kind == "lowshelf" or kind == "highshelf":
        sa = 2 * math.sqrt(a) * alpha
        if kind == "lowshelf":
            return (a * ((a + 1) - (a - 1) * cw + sa), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - sa),
                    (a + 1) + (a - 1) * cw + sa, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - sa)
        return (a * ((a + 1) + (a - 1) * cw + sa), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - sa),
                (a + 1) - (a - 1) * cw + sa, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - sa)
    if kind == "hp":
        return (1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    if kind == "lp":
        return (1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    if kind == "bp":
        return alpha, 0.0, -alpha, 1 + alpha, -2 * cw, 1 - alpha
    if kind == "notch":
        return 1.0, -2 * cw, 1.0, 1 + alpha, -2 * cw, 1 - alpha
    return 1.0, 0.0, 0.0, 1.0, 0.0, 0.0


def _db(coef, f):
    b0, b1, b2, a0, a1, a2 = coef
    z = cmath.exp(-1j * 2 * math.pi * min(f, FS * 0.49) / FS)
    h = (b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z)
    return 20 * math.log10(max(abs(h), 1e-6))


def eq8_kind(type_text):
    """EQ Eight's filter type text -> (biquad kind, how many in a row)."""
    t = (type_text or "").lower()
    stages = 4 if "48" in t else 1
    if "high pass" in t or "low cut" in t:
        return "hp", stages
    if "low pass" in t or "high cut" in t:
        return "lp", stages
    if "low shelf" in t:
        return "lowshelf", 1
    if "high shelf" in t:
        return "highshelf", 1
    if "notch" in t:
        return "notch", 1
    return "bell", 1


def response(bands, points):
    """dB at each frequency in `points` for [(kind, stages, freq, q, gain_db)] filters in a row."""
    coefs = [(_biquad(kind, f, q, g), n) for kind, n, f, q, g in bands]
    return [sum(_db(c, f) * n for c, n in coefs) for f in points]


def autofilter_kind(type_text, slope_text):
    t = (type_text or "").lower()
    n = 2 if "24" in (slope_text or "") else 1
    if "high" in t:
        return "hp", n
    if "band" in t:
        return "bp", n
    if "notch" in t:
        return "notch", n
    return "lp", n


def comp_curve(threshold, ratio, knee, x):
    """Output level (dB) of a compressor for input x (dB): threshold, ratio (1e9 = limiter), soft knee width."""
    over = x - threshold
    if knee > 0 and abs(over) <= knee / 2:
        return x + (1 / ratio - 1) * (over + knee / 2) ** 2 / (2 * knee)
    return x if over <= 0 else threshold + over / ratio


# ---------------------------------------------------------------- synths: envelopes, waves, page pictures

def seconds(text):
    """Live's time text ("600 ms", "1.20 s", "5 ms") -> seconds."""
    t = str(text or "").lower()
    v = number(t, 0.0)
    return v / 1000.0 if "ms" in t else (v if "s" in t else v / 1000.0)


def level(text, norm=0.5):
    """Live's level text -> 0..1: "-6.0 dB" (amplitude), "50 %", "+12 st" (pitch envelopes), "0.50"."""
    t = str(text or "").lower()
    if "-inf" in t:
        return 0.0
    v = number(t, None)
    if v is None:
        return norm
    if "db" in t:
        return max(0.0, min(1.0, 10 ** (v / 20)))
    if "%" in t:
        return max(0.0, min(1.0, v / 100))
    if "st" in t:
        return max(0.0, min(1.0, 0.5 + v / 96))
    return max(0.0, min(1.0, v)) if 0 <= v <= 1 else norm


def envelope_points(a, d, s, r, init=0.0, peak=1.0, end=0.0):
    """[(x 0..1, level 0..1)] of an envelope, segment widths on a log time scale (0 ms .. 20 s)."""
    seg = lambda t: 0.04 + 0.96 * math.log10(1 + max(0.0, t) * 1000) / math.log10(1 + 20000)   # noqa: E731
    wa, wd, wr, hold = seg(a), seg(d), seg(r), 0.6
    total = wa + wd + hold + wr
    xs = [0.0, wa, wa + wd, wa + wd + hold, total]
    return [(x / total, y) for x, y in zip(xs, (init, peak, s, s, end))]


def wave(shape, phase):
    """-1..1 at phase 0..1 of an oscillator / LFO named `shape` ("Sine", "Saw", "Square", "Shark Tooth", ...)."""
    t = (shape or "").lower()
    p = phase % 1.0
    if "shark" in t:
        return 1 - 2 * (p / 0.8) ** 0.5 if p < 0.8 else -1 + 2 * (p - 0.8) / 0.2
    if "satur" in t:
        return math.tanh(3 * math.sin(2 * math.pi * p)) / math.tanh(3)
    if "tri" in t:
        return 1 - 4 * abs(p - 0.5)
    if "saw" in t or t.startswith("sw"):
        return (1 - 2 * p) if "down" in t else (2 * p - 1)
    if "sq" in t or "pulse" in t or "rect" in t:
        return 1.0 if p < 0.5 else -1.0
    if "noise" in t or "random" in t or "s&h" in t or "s & h" in t:
        k = int(p * 8)
        return ((k * 7919 + 13) % 17) / 8.0 - 1
    if "sin" in t:
        return math.sin(2 * math.pi * p)
    return None                                       # unknown shape: don't draw a made-up one


def delay_seconds(texts, side, tempo):
    """One echo's time for Delay / Echo's left or right side ("L" / "R"), synced or in ms."""
    if (texts.get(side + " Sync") or "").lower() != "on":
        return seconds(texts.get(side + " Time"))
    beat = 60.0 / max(20.0, tempo)
    synced = texts.get(side + " Synced")                 # Echo: "1/8" plus a mode
    if synced is not None:
        mode = (texts.get(side + " Sync Mode") or "").lower()
        if "16th" in mode:
            return number(texts.get(side + " 16th"), 1) * beat / 4
        m = re.search(r"(\d+)\s*/\s*(\d+)", synced)
        beats = 4.0 * int(m.group(1)) / int(m.group(2)) if m else 1.0
        return beats * beat * (1.5 if "dot" in mode else (2 / 3 if "trip" in mode else 1.0))
    return number(texts.get(side + " 16th"), 1) * beat / 4    # Delay: a number of 16ths


def saturate(kind, x):
    """A sketch of Saturator's curve types (input -> output, both -1..1 after drive). Not Live's exact maths."""
    t = (kind or "").lower()
    clip = lambda v: max(-1.0, min(1.0, v))             # noqa: E731
    if "digital" in t:
        return clip(x)
    if "sinoid" in t or "fold" in t:
        return math.sin(x * math.pi / 2)
    if "soft sine" in t:
        return math.sin(clip(x) * math.pi / 2)
    if "hard" in t:
        v = clip(x)
        return 1.5 * v - 0.5 * v ** 3
    if "medium" in t:
        return x / (1 + abs(x)) * 2 if abs(x) < 1 else math.copysign(1.0, x)
    return math.tanh(1.5 * x) / math.tanh(1.5)


def _ends(name, *suffixes):
    return any(name.endswith(s) for s in suffixes)


def page_picture(page, names, texts, norms):
    """What to draw for a knob page, from the names of the knobs on it.
    names / texts / norms: parallel lists for the page's parameters. Returns a dict or None:
    {"kind": "env", "points": [...], "labels": [...]}, {"kind": "filter", "bands": [...]},
    {"kind": "wave", "shape": text, "title": ...}."""
    def find(*suffixes, avoid=("<", "Slope", "Mod", "Key", "Vel", "Fix", "Amt", "Mode", "Loop", "Retrig")):
        for i, n in enumerate(names):
            if _ends(n, *suffixes) and not any(a in n for a in avoid):
                return i
        return None
    p = page.lower()
    att, dec, sus = find("Attack"), find("Decay"), find("Sustain")
    rel = find("Release", "Rel")
    if "env" in p and None not in (att, dec, sus, rel):
        init, peak, end = find("Init", "Initial"), find("Peak"), find("End", "Final")
        lv = lambda i, dflt: level(texts[i], norms[i]) if i is not None else dflt   # noqa: E731
        pts = envelope_points(seconds(texts[att]), seconds(texts[dec]), lv(sus, 0.5), seconds(texts[rel]),
                              lv(init, 0.0), lv(peak, 1.0), lv(end, 0.0))
        return {"kind": "env", "points": pts,
                "labels": [("A", texts[att]), ("D", texts[dec]), ("S", texts[sus]), ("R", texts[rel])]}
    freq = find("Freq", "Frequency")
    if freq is not None and ("filter" in p or "flt" in p or p.startswith("f")):
        res = find("Res", "Resonance", "Q")
        typ, slope = find("Type"), find("Slope")
        q = 0.7 + 10 * (norms[res] if res is not None else 0.0)
        f = number(texts[freq], 1000.0)
        bands = []
        if names[freq].startswith("LP "):             # Drift: a low-pass plus a separate high-pass
            bands.append(("lp", 2, f, q, 0.0))
            hp = find("HP Freq")
            if hp is not None:
                bands.append(("hp", 1, number(texts[hp], 10.0), 0.7, 0.0))
        else:
            kind, n = autofilter_kind(texts[typ] if typ is not None else "", (texts[slope] if slope is not None
                                                                              else "") + (texts[typ] if typ is not None else ""))
            bands.append((kind, n, f, q, 0.0))
        kind_txt = texts[typ] if typ is not None else ""
        if typ is not None and len(kind_txt) <= 3:          # Drift's "LP Type" just says "I" / "II"
            kind_txt = "%s %s" % (names[typ], kind_txt)
        return {"kind": "filter", "bands": bands, "label": "%s  ·  %s" % (kind_txt, texts[freq])}
    if "lfo" in p or "osc" in p or "sub" in p:
        sh = find("Wave", "Waveform", "Shape", "Type", avoid=("<", "Mod", "Amt", "FX"))
        if sh is not None and wave(texts[sh], 0.25) is not None:
            return {"kind": "wave", "shape": texts[sh], "title": "%s: %s" % (names[sh], texts[sh])}
        pos = find("Pos")
        if pos is not None:                           # Wavetable: no wave shapes reach scripts, so its position
            return {"kind": "position", "value": norms[pos], "title": "%s  %s" % (names[pos], texts[pos])}
    return None
