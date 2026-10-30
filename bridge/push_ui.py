"""Push 3-style screens for the MPC One's 1280x800 display, drawn with Pillow.

Layout (same skeleton on every page, like Push's display with its rows of buttons):
    header  0-52     connection, selected track, page tabs, transport, tempo
    upper   52-116   8 soft buttons (150 px each) + side rail (80 px)
    main    116-720  the page
    lower   720-800  8 track selectors + side rail

render() returns the frame plus the touch targets it drew, as (x0, y0, x1, y1, action) tuples.
"""

import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFont

import device_views as dv
import waveform

_wave_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='waveform')

W, H = 1280, 800
HEAD_H = 52
UP_Y, UP_H = 52, 64
MAIN_Y = UP_Y + UP_H
LOW_Y, LOW_H = 720, 80
MAIN_H = LOW_Y - MAIN_Y
COL_W, COLS, RAIL_X = 150, 8, 1200
ROWS = 8
ROW_H = MAIN_H // ROWS

BG = (0, 0, 0)
PANEL = (14, 14, 14)
CELL = (30, 30, 30)
LINE = (44, 44, 44)
TEXT = (255, 255, 255)
DIM = (150, 150, 150)
FAINT = (85, 85, 85)
ACCENT = (255, 118, 40)
PLAY = (60, 220, 120)
REC = (255, 64, 64)
MUTE = (255, 196, 40)
SOLO = (70, 150, 255)

PAGES = ["session", "arrange", "clip", "mix", "device", "browse"]
KEY_W = 96                  # clip page: keyboard / label column
ROLL_RULER_H = 30
ROLL_ROWS = 18
ROLL_ROW_H = (MAIN_H - ROLL_RULER_H) // ROLL_ROWS
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PLUGIN_CLASSES = ("PluginDevice", "AuPluginDevice")
GRIDS = [(1.0, "1/4"), (0.5, "1/8"), (0.25, "1/16"), (0.125, "1/32")]


def note_name(p):
    return "%s%d" % (NOTE_NAMES[p % 12], p // 12 - 2)       # Live's naming: 60 = C3
PLACES = "@places"
# Upper-row shortcuts on the Browse page
BROWSE_ROOTS = [("sounds", "Sounds"), ("drums", "Drums"), ("instruments", "Instruments"),
                ("audio_effects", "Audio FX"), ("midi_effects", "MIDI FX"), ("plugins", "Plug-ins"),
                ("user_library", "User Library"), (PLACES, "Places")]
ROOT_LABELS = dict(BROWSE_ROOTS, **{"@search": "Search"}, max_for_live="Max for Live", packs="Packs", current_project="Current Project",
                   samples="Samples", clips="Clips")
# Live's 70-colour palette by color_index (read from Live 12.4, 2026-10-29): 5 rows of 14, as in Live
LIVE_COLORS = [
    0xFF94A6, 0xFFA529, 0xCC9927, 0xF7F47C, 0xBFFB00, 0x1AFF2F, 0x25FFA8, 0x5CFFE8, 0x8BC5FF, 0x5480E4, 0x92A7FF, 0xD86CE4, 0xE553A0, 0xFFFFFF,
    0xFF3636, 0xF66C03, 0x99724B, 0xFFF034, 0x87FF67, 0x3DC300, 0x00BFAF, 0x19E9FF, 0x10A4EE, 0x007DC0, 0x886CE4, 0xB677C6, 0xFF39D4, 0xD0D0D0,
    0xE2675A, 0xFFA374, 0xD3AD71, 0xEDFFAE, 0xD2E498, 0xBAD074, 0x9BC48D, 0xD4FDE1, 0xCDF1F8, 0xB9C1E3, 0xCDBBE4, 0xAE98E5, 0xE5DCE1, 0xA9A9A9,
    0xC6928B, 0xB78256, 0x99836A, 0xBFBA69, 0xA6BE00, 0x7DB04D, 0x88C2BA, 0x9BB3C4, 0x85A5C2, 0x8393CC, 0xA595B5, 0xBF9FBE, 0xBC7196, 0x7B7B7B,
    0xAF3333, 0xA95131, 0x724F41, 0xDBC300, 0x85961F, 0x539F31, 0x0A9C8E, 0x236384, 0x1A2F96, 0x2F52A2, 0x624BAD, 0xA34BAD, 0xCC2E6E, 0x3C3C3C,
]
TL_X = 132                  # arrangement: left edge of the timeline (lane names sit left of it)
ARR_RULER_H = 46
ARR_LANE_H = (MAIN_H - ARR_RULER_H - 6) // ROWS


def root_label(key):
    if key.startswith("userplace:"):
        return key[len("userplace:"):]
    return key[6:] if key.startswith("place:") else ROOT_LABELS.get(key, key)

_fonts = {}


def font(size, style="SemiBold SemiCondensed"):
    key = (size, style)
    f = _fonts.get(key)
    if f is None:
        f = ImageFont.truetype("bahnschrift.ttf", size)
        try:
            f.set_variation_by_name(style)
        except Exception:
            pass
        _fonts[key] = f
    return f


def rgb(c):
    c = int(c or 0) & 0xFFFFFF
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def lum(c):
    return c[0] * 0.299 + c[1] * 0.587 + c[2] * 0.114


def scale(c, f):
    return tuple(max(0, min(255, int(v * f))) for v in c)


def ink(bg):
    return (0, 0, 0) if lum(bg) > 150 else TEXT


def fit(d, text, f, maxw):
    text = str(text)
    if d.textlength(text, font=f) <= maxw:
        return text
    while text and d.textlength(text + "…", font=f) > maxw:
        text = text[:-1]
    return text + "…"


def fit_start(d, text, f, maxw):
    """Like fit, but cuts the start: for paths, whose end is the part that tells them apart."""
    text = str(text)
    if d.textlength(text, font=f) <= maxw:
        return text
    while text and d.textlength("…" + text, font=f) > maxw:
        text = text[1:]
    return "…" + text


class UiState:
    """View state owned by the app (what's on screen), as opposed to Live's state."""

    def __init__(self):
        self.page = "session"
        self.bank = 0               # first track on screen (multiple of 8)
        self.scene_off = 0          # first scene row on screen
        self.quad = 0               # which 4x4 block of the 8x8 grid the pads play (pad bank A-D)
        self.qlink_half = 0         # device page: which 4 knobs the Q-Links turn (0: 1-4, 1: 5-8, 2: 9-12, 3: 13-16)
        self.mix_target = "vol"     # mix page: Q-Links on volume or pan
        self.mix_view = "mix"       # mix page: "mix" (faders), "sends" or "io" (routing and monitoring)
        self.mix_returns = False    # mix page: show the return tracks and the main track instead of tracks
        self.send_sel = 0           # mix page, Sends: which send the Q-Links turn
        self.shift = False
        self.qlink_touch = set()    # Q-Links (0-3) under a finger
        self.focus_param = None     # device page: last touched parameter, shown big
        self.focus_at = 0.0
        self.help = False
        self.br_sel = 0
        self.br_scroll = 0
        self.sheet = None           # action sheet (long-press / MENU): {"title", "color", "items": [(label, cmd)]}
        self.preset_sub = {}        # PRESET: the preset folder last opened, per device type
        self.preset_loaded = ""     # PRESET: the preset loaded last (marked in the list)
        self.kb = None              # on-screen keyboard: {"what", "text", "caps", "title"}
        self.colors = None          # colour picker: {"what", "title"}
        self.fixed_len = 0          # bars recorded by REC / an empty slot on an armed track (0 = off)
        self.rec_to_arr = True      # Session recordings are also copied into the arrangement where they were played
        self.erase_held = False     # ERASE / COPY held: tap a clip, track or scene to delete / duplicate it
        self.copy_held = False
        self.copy_src = None        # (track, slot) waiting to be pasted into the next slot you tap
        self.note_layout = "chromatic"   # pads playing notes: "chromatic" or "scale" (in key; Drum Racks get drums)
        self.chord_mode = "off"
        self.velocity_curve = "linear"
        self.pressure_mode = "poly"
        self.drum_base = 36         # Drum Rack layout: the note of pad 1 (PAD BANK A-D = 36 / 52 / 68 / 84)
        self.last_drum = ""         # name of the drum pad hit last (header chip)
        self.levels = False         # 16 Levels: all pads play levels_note, pad = velocity
        self.levels_note = 60
        self.repeat_held = False    # NOTE REPEAT held (or latched with SHIFT): held pads retrigger
        self.repeat_latch = False
        self.repeat_rate = 0.25     # beats between repeats (0.25 = 1/16)
        self.preview_on = True      # browse: play the highlighted sample as the wheel moves
        self.preview_loop = False   # ... once, or looping
        self.arr_start = 0.0        # arrangement: first beat in view
        self.arr_selected = None   # (track, start, length, name)
        self.arr_preview = None    # (selection, operation, destination)
        self.arr_selection = []    # multi-clip selection, in track/time order
        self.arr_marquee = None    # screen-space selection rectangle during a drag
        self.arr_len = 64.0         # beats in view (zoom)
        self.arr_follow = True      # scroll along with the playhead
        self.clip_mode = "notes"    # clip page: "notes" (piano roll) or "auto" (automation lane)
        self.grid = 0.25            # beats per grid step / pad step
        self.pitch_lo = 48          # lowest pitch row on screen
        self.focus_pitch = 60       # the row the pads step-sequence
        self.step_page = 0          # which 16 steps the pads show
        self.clip_view = None       # (start, length) in beats; None = the clip's loop
        self.sel_note = None        # last touched note (Q4 = its velocity)
        self.pads = "play"          # "play" = notes into Live, "launch" = clips (Clip page: always steps)
        self.pad_base = 60          # play mode: note of the bottom-left pad (60 = C3)
        self.full_level = False     # play mode: every hit at velocity 127 (MPC's FULL LEVEL)
        self.midi_port = None       # name of the virtual MIDI port the pads play into, if any
        self.drum_note = 36         # last pad note played; samples from Places load onto this Drum Rack pad
        self.add_menu = False       # device page: the add-device menu is open
        self.add_tab = "live"       # add menu tab: "live" (Live's devices) or "plugins"
        self.plugin_page = 0
        self.menu_sel = 0           # add menu: item the data wheel has highlighted (index in the tab's list)
        self.plugin_ui = False      # device page: a plug-in's own window (True) or its knob screen (False)
        self.pad_page = 36          # device page, opened Drum Rack: lowest note of the 16 pads shown

    def pad_mode(self):
        return "step" if self.page == "clip" else self.pads

    def play_label(self, live):
        """What the pads play, for the header chip: notes from C3, a scale, a drum pad's name, 16 Levels."""
        if self.levels:
            return "16 LEVELS"
        if live.drum_pads is not None:
            return (self.last_drum or "DRUMS").upper()
        if self.note_layout == "scale":
            return "%s %s" % (NOTE_NAMES[live.scale[0] % 12], live.scale[1].upper())
        return "NOTES " + note_name(self.pad_base)

    def arr_ppb(self):
        return (RAIL_X - TL_X) / self.arr_len

    def clip_view_range(self, live):
        a, b = live.clip_range()
        v0, vlen = self.clip_view or (a, b - a)
        return v0, max(0.25, min(1024.0, vlen))

    def roll_ppb(self, live):
        return (RAIL_X - KEY_W) / self.clip_view_range(live)[1]

    def ring_tracks(self):
        a = self.bank + (self.quad % 2) * 4
        return list(range(a, a + 4))

    def qlink_strips(self, live):
        """Mix page: the strips the 4 Q-Links control (the pads' 4 tracks, or the first 4 returns / main)."""
        if self.mix_returns:
            keys = [("return", r) for r in range(min(len(live.returns), COLS - 1))] + [("master", 0)]
            return keys[:4]
        return [("track", t) for t in self.ring_tracks() if t < len(live.tracks)]

    def ring_scenes(self):
        a = self.scene_off + (self.quad // 2) * 4
        return list(range(a, a + 4))

    def qlink_params(self, dev):
        shown = dev.bank_params()
        return shown[self.qlink_half * 4:self.qlink_half * 4 + 4]

    def plugin_knobs(self, dev):
        """True when the Device page shows the selected plug-in as the 16-knob screen."""
        return (self.page == "device" and dev.view == "devices" and bool(dev.names) and not self.plugin_ui
                and dev.selected_info()[0] in PLUGIN_CLASSES)


class Renderer:
    def __init__(self):
        self.targets = []
        self.dialog_frame = None    # Live's open dialog, captured and scaled (app thread)
        self.dialog_hwnd, self.dialog_scale, self.dialog_place = None, 1.0, (0, 0)
        self.curve_geom = None      # EQ / filter picture: (left, right, 0 dB y, half height, dB at the edge)
        self.wave_geom = None       # Simpler waveform on screen: (left x, width, sample length, top, bottom)
        self.wave_cache = {}        # (file, columns) -> peaks, None while reading, or "error: ..."
        self.plugin_frame = None    # latest capture of the selected plug-in's window, scaled to fit (app thread)
        self.plugin_hwnd, self.plugin_scale, self.plugin_place = None, 1.0, (0, 0)

    def target(self, x0, y0, x1, y1, action):
        self.targets.append((x0, y0, x1, y1, action))

    def render(self, live, ui):
        self.targets = []
        self.tempo = live.tempo or 120.0             # Delay / Echo pictures place synced echoes in time
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        with live.lock:
            self._header(d, live, ui)
            if not live.connected:
                self._waiting(d)
            else:
                getattr(self, "_page_" + ui.page)(d, live, ui)
                if ui.page != "browse":
                    self._lower_tracks(d, live, ui)
                self._rail_banks(d, live, ui)
            if ui.erase_held or ui.copy_held or ui.copy_src:
                self._modifier_banner(d, ui)
            if (ui.repeat_held or ui.repeat_latch) and ui.pad_mode() == "play":
                self._repeat_strip(d, ui)
            if self.dialog_frame is not None:
                self._dialog(img, d)
            if ui.sheet or ui.kb or ui.colors:
                self._dim(img, 0.95 if ui.kb else 0.75)
                d = ImageDraw.Draw(img)
                self.target(0, 0, W, H, ("noop",) if ui.kb else ("overlay_close",))
                if ui.kb:
                    self._keyboard(d, ui)
                elif ui.colors:
                    self._color_picker(d, ui)
                else:
                    self._sheet(d, ui)
            if ui.help:
                self._help(img)
                d = ImageDraw.Draw(img)
            if live.toast_msg and time.time() - live.toast_at < 2.2:
                self._toast(d, live.toast_msg)
        return img, self.targets

    # ------------------------------------------------------------ chrome

    def _header(self, d, live, ui):
        d.rectangle([0, 0, W, HEAD_H - 1], fill=PANEL)
        d.line([0, HEAD_H - 1, W, HEAD_H - 1], fill=LINE)
        d.ellipse([16, 20, 28, 32], fill=PLAY if live.connected else REC)
        tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
        if tr:
            c = rgb(tr.color)
            d.rounded_rectangle([40, 14, 64, 38], 4, fill=c)
            d.text((76, 26), fit(d, "%d  %s" % (live.sel + 1, tr.name), font(24), 190), font=font(24),
                   fill=TEXT, anchor="lm")
        # what the pads do; tap to switch between playing notes and launching clips
        mode = ui.pad_mode()
        label = {"play": ui.play_label(live), "launch": "PADS CLIPS", "step": "PADS STEPS"}[mode]
        warn = mode == "play" and not ui.midi_port
        d.rounded_rectangle([274, 12, 374, 40], 14, outline=MUTE if warn else (ACCENT if mode == "play" else DIM),
                            width=2)
        d.text((324, 26), label, font=font(15, "Bold SemiCondensed"), fill=MUTE if warn else TEXT, anchor="mm")
        self.target(270, 0, 378, HEAD_H, ("pads_mode",))
        x, tw = 380, 96
        for p in PAGES:
            active = p == ui.page
            d.text((x + tw // 2, 25), p.upper(), font=font(22, "Bold SemiCondensed"),
                   fill=TEXT if active else FAINT, anchor="mm")
            if active:
                d.rectangle([x + 16, HEAD_H - 5, x + tw - 16, HEAD_H - 2], fill=ACCENT)
            self.target(x, 0, x + tw, HEAD_H, ("page", p))
            x += tw
        # transport, right to left: tempo, play, metronome, record
        d.text((W - 16, 27), "%.2f" % live.tempo, font=font(30, "Bold SemiCondensed"), fill=TEXT, anchor="rm")
        d.text((W - 128, 30), "BPM", font=font(14), fill=DIM, anchor="rm")
        self.target(W - 130, 0, W, HEAD_H, ("drag", "tempo", 0))
        px = W - 190
        d.polygon([(px, 14), (px, 38), (px + 21, 26)], fill=PLAY if live.playing else FAINT)
        self.target(px - 16, 0, px + 34, HEAD_H, ("play",))
        mx = W - 252
        mc = ACCENT if live.metro else FAINT
        d.polygon([(mx, 38), (mx + 26, 38), (mx + 17, 12), (mx + 9, 12)], outline=mc, width=2)
        d.line([(mx + 13, 33), (mx + 24, 15)], fill=mc, width=3)
        d.ellipse([mx + 20, 17, mx + 26, 23], fill=mc)
        self.target(mx - 14, 0, mx + 36, HEAD_H, ("metro",))
        rx = W - 300
        rec = live.rec_mode if ui.page == "arrange" else live.rec     # REC is arrangement record on that page
        d.ellipse([rx, 15, rx + 22, 37], fill=REC if rec else None, outline=REC if rec else FAINT, width=3)
        self.target(rx - 14, 0, rx + 36, HEAD_H, ("rec",))

    def _soft(self, d, i, label, active=False, color=None, sub=None, action=None, y=UP_Y, h=UP_H):
        x0 = i * COL_W
        box = [x0 + 3, y + 4, x0 + COL_W - 4, y + h - 5]
        if active:
            bg = color or TEXT
            d.rounded_rectangle(box, 5, fill=bg)
            fg = ink(bg)
        else:
            d.rounded_rectangle(box, 5, fill=PANEL)
            fg = TEXT if label else FAINT
            if color:
                d.rectangle([box[0], box[1], box[2], box[1] + 3], fill=color)
        if sub:
            d.text((x0 + COL_W // 2, y + h // 2 - 9), fit(d, label, font(20), COL_W - 18), font=font(20), fill=fg,
                   anchor="mm")
            d.text((x0 + COL_W // 2, y + h // 2 + 13), fit(d, sub, font(15), COL_W - 18), font=font(15),
                   fill=fg if active else DIM, anchor="mm")
        else:
            d.text((x0 + COL_W // 2, y + h // 2), fit(d, label, font(21), COL_W - 18), font=font(21), fill=fg,
                   anchor="mm")
        if action:
            self.target(x0, y, x0 + COL_W, y + h, action)

    def _lower_tracks(self, d, live, ui):
        d.line([0, LOW_Y, RAIL_X, LOW_Y], fill=LINE)
        for c in range(COLS):
            t = ui.bank + c
            if t >= len(live.tracks):
                continue
            tr = live.tracks[t]
            col = rgb(tr.color)
            sel = t == live.sel
            sub = "  ".join(s for s, on in (("ARM", tr.arm), ("MUTE", tr.mute), ("SOLO", tr.solo)) if on)
            self._soft(d, c, tr.name or "Track %d" % (t + 1), active=sel, color=col, sub=sub or None,
                       action=("track", t), y=LOW_Y, h=LOW_H)
            if tr.armable:  # record-arm dot, tappable (Bahnschrift has no ● glyph, so it's drawn)
                x1, y1 = (c + 1) * COL_W - 20, LOW_Y + 22
                d.ellipse([x1 - 8, y1 - 8, x1 + 8, y1 + 8], fill=REC if tr.arm else None,
                          outline=REC if tr.arm else (ink(col) if sel else FAINT), width=2)
                self.target(x1 - 24, LOW_Y, x1 + 20, LOW_Y + 44, ("toggle", "arm", t))

    def _rail_banks(self, d, live, ui):
        n = max(1, live.num_tracks)
        first, last = ui.bank + 1, min(ui.bank + COLS, n)
        for y, glyph, step in ((UP_Y, "‹", -COLS), (LOW_Y, "›", COLS)):
            h = UP_H if y == UP_Y else LOW_H
            ok = 0 <= ui.bank + step < n
            d.rounded_rectangle([RAIL_X + 4, y + 4, W - 4, y + h - 5], 5, fill=PANEL)
            d.text((RAIL_X + 40, y + h // 2 - 8), glyph, font=font(34), fill=TEXT if ok else FAINT, anchor="mm")
            d.text((RAIL_X + 40, y + h // 2 + 16), "%d-%d" % (first, last), font=font(13), fill=DIM, anchor="mm")
            self.target(RAIL_X, y, W, y + h, ("bank", step))

    def _toast(self, d, msg):
        f = font(24)
        w = d.textlength(msg, font=f) + 48
        x0, y0 = (W - w) // 2, LOW_Y - 70
        d.rounded_rectangle([x0, y0, x0 + w, y0 + 48], 24, fill=(235, 235, 235))
        d.text((W // 2, y0 + 24), msg, font=f, fill=(0, 0, 0), anchor="mm")

    def _waiting(self, d):
        d.text((W // 2, 300), "Waiting for Ableton Live…", font=font(52, "Bold SemiCondensed"), fill=TEXT,
               anchor="mm")
        for i, line in enumerate(["Open Live with AbletonOSC enabled as a Control Surface",
                                  "(Settings > Link, Tempo & MIDI). This screen updates by itself."]):
            d.text((W // 2, 380 + i * 36), line, font=font(26, "SemiLight SemiCondensed"), fill=DIM, anchor="mm")

    # ------------------------------------------------------------ session

    def _page_session(self, d, live, ui):
        blink = int(time.time() * 4) % 2 == 0
        for c in range(COLS):            # upper row: what each track is playing; tap = stop the track
            t = ui.bank + c
            if t == len(live.tracks) and live.num_tracks >= 0:
                self._soft(d, c, "+ Track", action=("add_track",))
                continue
            if t >= len(live.tracks):
                continue
            tr = live.tracks[t]
            playing = tr.clips[tr.playing][1] if 0 <= tr.playing < len(tr.clips) else ""
            self._soft(d, c, playing or "     Stop", color=rgb(tr.color), sub=None, action=("stop_track", t))
            if playing:
                d.polygon([(c * COL_W + 12, UP_Y + 26), (c * COL_W + 12, UP_Y + 38), (c * COL_W + 22, UP_Y + 32)],
                          fill=PLAY)
            else:           # no ■ glyph in Bahnschrift
                x = c * COL_W + COL_W // 2 - 34
                d.rectangle([x, UP_Y + 26, x + 11, UP_Y + 37], fill=DIM)
        ring_t, ring_s = ui.ring_tracks(), ui.ring_scenes()
        for r in range(ROWS):
            s = ui.scene_off + r
            y0 = MAIN_Y + r * ROW_H
            if s == live.num_scenes and live.num_scenes >= 0:
                d.rounded_rectangle([RAIL_X + 4, y0 + 3, W - 4, y0 + ROW_H - 4], 6, outline=LINE, width=2)
                d.text((RAIL_X + 40, y0 + ROW_H // 2), "+ Scene", font=font(16), fill=DIM, anchor="mm")
                self.target(RAIL_X, y0, W, y0 + ROW_H, ("add_scene",))
            if s >= live.num_scenes:
                continue
            for c in range(COLS):
                t = ui.bank + c
                if t >= len(live.tracks):
                    continue
                self._clip_cell(d, live.tracks[t], s, c * COL_W, y0, blink)
                self.target(c * COL_W, y0, (c + 1) * COL_W, y0 + ROW_H, ("clip", t, s))
            # scene launch on the rail
            name = live.scenes[s] if s < len(live.scenes) else ""
            d.rounded_rectangle([RAIL_X + 4, y0 + 3, W - 4, y0 + ROW_H - 4], 6, fill=PANEL)
            d.polygon([(RAIL_X + 30, y0 + 20), (RAIL_X + 30, y0 + 40), (RAIL_X + 47, y0 + 30)], fill=DIM)
            d.text((RAIL_X + 40, y0 + 56), fit(d, name or str(s + 1), font(13), 70), font=font(13), fill=FAINT,
                   anchor="mm")
            self.target(RAIL_X, y0, W, y0 + ROW_H, ("scene", s))
        if ui.copy_src:                  # the clip waiting to be pasted
            t, s = ui.copy_src
            c, r = t - ui.bank, s - ui.scene_off
            if 0 <= c < COLS and 0 <= r < ROWS:
                d.rounded_rectangle([c * COL_W + 1, MAIN_Y + r * ROW_H + 1, (c + 1) * COL_W - 2,
                                     MAIN_Y + (r + 1) * ROW_H - 2], 8, outline=ACCENT, width=4)
        # the pads' 4x4 block (only while the pads launch clips)
        if ui.pads == "launch":
            c0, r0 = (ui.quad % 2) * 4, (ui.quad // 2) * 4
            d.rounded_rectangle([c0 * COL_W, MAIN_Y + r0 * ROW_H, (c0 + 4) * COL_W - 1,
                                 MAIN_Y + (r0 + 4) * ROW_H - 1], 8, outline=TEXT, width=3)
        # scene scroll position
        if live.num_scenes > ROWS:
            h = MAIN_H * ROWS / live.num_scenes
            y = MAIN_Y + MAIN_H * ui.scene_off / live.num_scenes
            d.rectangle([W - 3, y, W, y + h], fill=DIM)

    def _clip_cell(self, d, tr, s, x0, y0, blink):
        box = [x0 + 3, y0 + 3, x0 + COL_W - 4, y0 + ROW_H - 4]
        clip = tr.clips[s] if s < len(tr.clips) else (False, "", 0)
        playing, fired = tr.playing == s, tr.fired == s and tr.playing != s
        if clip[0]:
            col = rgb(clip[2] or tr.color)
            fill = col if (not fired or blink) else scale(col, 0.35)
            d.rounded_rectangle(box, 6, fill=fill)
            fg = ink(fill)
            tx = x0 + 12
            if playing:
                d.rounded_rectangle(box, 6, outline=TEXT, width=3)
                d.polygon([(x0 + 12, y0 + 14), (x0 + 12, y0 + 30), (x0 + 26, y0 + 22)], fill=fg)
            d.text((tx, y0 + ROW_H - 14), fit(d, clip[1], font(18), COL_W - 24), font=font(18), fill=fg,
                   anchor="ls")
        else:
            d.rounded_rectangle(box, 6, fill=CELL if not (fired and blink) else (70, 70, 70))
            cx, cy = x0 + COL_W // 2, y0 + ROW_H // 2
            if tr.arm:
                d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=REC, width=3)
            else:
                d.rectangle([cx - 5, cy - 5, cx + 5, cy + 5], fill=(58, 58, 58))

    # ------------------------------------------------------------ arrangement

    def _page_arrange(self, d, live, ui):
        for i, (label, active, act) in enumerate((
                ("LOOP", live.loop, "loop"), ("FOLLOW", ui.arr_follow, "follow"), ("‹ LOCATOR", False, "prev_cue"),
                ("LOCATOR ›", False, "next_cue"), ("SET LOCATOR", False, "add_cue"),
                ("AUTO ARM", live.auto_arm, "auto_arm"), ("RE-ENABLE", False, "reenable"),
                ("BACK TO ARR.", live.back_to_arranger, "back"))):
            self._soft(d, i, label, active=active,
                       color=REC if act == "auto_arm" else (ACCENT if act in ("loop", "back") else None),
                       sub="automation" if act == "reenable" else None, action=("arr", act))
        ppb, a0 = ui.arr_ppb(), ui.arr_start
        bpb = live.beats_per_bar()
        x_of = lambda beat: TL_X + (beat - a0) * ppb     # noqa: E731
        ry, ly0 = MAIN_Y, MAIN_Y + ARR_RULER_H
        ly1 = ly0 + ARR_LANE_H * ROWS
        # position readout where the ruler meets the lane names
        bar, rest = divmod(live.song_time, bpb)
        pos = "%d. %d. %d" % (bar + 1, int(rest) + 1, int((rest % 1) * 4) + 1)
        d.text((TL_X // 2, ry + ARR_RULER_H // 2), pos, font=font(24, "Bold SemiCondensed"),
               fill=PLAY if live.playing else TEXT, anchor="mm")
        # ruler: bar numbers, loop brace, locators
        d.rectangle([TL_X, ry, RAIL_X - 1, ly0 - 2], fill=PANEL)
        step = next((s for s in (1, 2, 4, 8, 16, 32, 64, 128) if s * bpb * ppb >= 56), 256)
        first_bar = int(a0 // bpb)
        for b in range(first_bar, first_bar + int(ui.arr_len / bpb) + 2):
            x = x_of(b * bpb)
            if x < TL_X or x >= RAIL_X:
                continue
            major = b % step == 0
            if major:
                d.line([x, ry + 22, x, ly0 - 2], fill=DIM)
                d.text((x + 4, ry + 14), str(b + 1), font=font(16), fill=DIM, anchor="lm")
            if major or bpb * ppb >= 10:
                d.line([x, ly0, x, ly1], fill=(34, 34, 34) if not major else LINE)
        lx0, lx1 = max(TL_X, x_of(live.loop_start)), min(RAIL_X - 1, x_of(live.loop_start + live.loop_len))
        if lx1 > lx0:
            d.rectangle([lx0, ly0 - 12, lx1, ly0 - 4], fill=ACCENT if live.loop else FAINT)
        for name, t in live.cues:
            x = x_of(t)
            if TL_X <= x < RAIL_X:
                d.polygon([(x - 7, ry + 30), (x + 7, ry + 30), (x, ry + 40)], fill=TEXT)
                d.text((x + 10, ry + 36), fit(d, name, font(14), 120), font=font(14), fill=TEXT, anchor="lm")
        self.target(TL_X, ry, RAIL_X, ly0, ("drag", "arr", -1))
        # lanes
        for r in range(ROWS):
            t = ui.bank + r
            if t >= len(live.tracks):
                break
            tr = live.tracks[t]
            col = rgb(tr.color)
            y0 = ly0 + r * ARR_LANE_H
            sel = t == live.sel
            d.rectangle([0, y0 + 1, TL_X - 3, y0 + ARR_LANE_H - 2], fill=scale(col, 0.35) if sel else PANEL)
            d.rectangle([0, y0 + 1, 5, y0 + ARR_LANE_H - 2], fill=col)
            d.text((14, y0 + ARR_LANE_H // 2), fit(d, tr.name, font(19), TL_X - 24), font=font(19),
                   fill=TEXT, anchor="lm")
            d.line([TL_X, y0 + ARR_LANE_H - 1, RAIL_X - 1, y0 + ARR_LANE_H - 1], fill=(26, 26, 26))
            for start, length, name in tr.arrangement_clips():
                data = tr.arr_details.get((round(start, 4), round(length, 4), name))
                clip_col = rgb(data['color']) if data and 'color' in data else col
                x0, x1 = x_of(start), x_of(start + length)
                if x1 < TL_X or x0 >= RAIL_X:
                    continue
                x0, x1 = max(TL_X, x0), min(RAIL_X - 1, x1)
                if not math.isfinite(x0) or not math.isfinite(x1) or x1 - x0 < 1:
                    continue  # clipped/subpixel clips have no drawable rectangle at this zoom
                d.rectangle([x0, y0 + 4, x1 - 1, y0 + ARR_LANE_H - 5], fill=scale(clip_col, 0.2 if data and data.get('muted') else 0.45))
                d.rectangle([x0, y0 + 4, x1 - 1, y0 + 9], fill=clip_col)
                self._arr_content(d, data, start, x0, x1, y0, x_of, ppb)
                if x1 - x0 > 30:
                    d.text((x0 + 6, y0 + 20), fit(d, name, font(14), x1 - x0 - 10), font=font(14),
                           fill=TEXT, anchor="lm")
                if ui.arr_selected == (t, start, length, name) or (t, start, length, name) in ui.arr_selection:
                    d.rectangle([x0, y0+4, x1-1, y0+ARR_LANE_H-5], outline=TEXT, width=2)
            self.target(0, y0, TL_X, y0 + ARR_LANE_H, ("track", t))
            self.target(TL_X, y0, RAIL_X, y0 + ARR_LANE_H, ("drag", "arr", t))
            for start, length, name in tr.arrangement_clips():
                x0, x1 = max(TL_X, x_of(start)), min(RAIL_X-1, x_of(start+length))
                if math.isfinite(x0) and math.isfinite(x1) and x1-x0 >= 8:
                    self.target(x0, y0+4, x1, y0+ARR_LANE_H-5, ("arr_clip", t, start, length, name))
            if ui.arr_preview and ui.arr_preview[0][0] == t:
                selection, op, dest = ui.arr_preview
                a, b = (dest, dest+selection[2]) if op == 'move' else (selection[1], selection[1]+dest)
                left, right = max(TL_X, x_of(a)), min(RAIL_X-1, x_of(b))
                if right-left > 2:
                    d.rectangle((left,y0+2,right,y0+ARR_LANE_H-3), outline=ACCENT, width=3)
                    d.text((left+8,y0+20), 'Bar %.2f' % (a/bpb+1), font=font(14), fill=ACCENT)
        if ui.arr_marquee:
            left, top, right, bottom = ui.arr_marquee
            d.rectangle((left, top, right, bottom), outline=ACCENT, width=2)
            label = '%d clips' % len(ui.arr_selection)
            yy = max(ly0, top-27)
            d.rounded_rectangle((left,yy,min(RAIL_X-1,left+96),yy+25),4,fill=PANEL)
            d.text((left+7,yy+12),label,font=font(14),fill=ACCENT,anchor='lm')
        # playhead
        x = x_of(live.song_time)
        if TL_X <= x < RAIL_X:
            pc = PLAY if live.playing else TEXT
            d.line([x, ry + 4, x, ly1], fill=pc, width=2)
            d.polygon([(x - 7, ry + 2), (x + 7, ry + 2), (x, ry + 12)], fill=pc)
        # scrolling on the rail
        for y, glyph, step_dir in ((MAIN_Y, "‹", -1), (MAIN_Y + MAIN_H // 2, "›", 1)):
            d.rounded_rectangle([RAIL_X + 4, y + 4, W - 4, y + MAIN_H // 2 - 5], 6, fill=PANEL)
            d.text((RAIL_X + 40, y + MAIN_H // 4), glyph, font=font(40), fill=TEXT, anchor="mm")
            self.target(RAIL_X, y, W, y + MAIN_H // 2, ("arr", "scroll_back" if step_dir < 0 else "scroll_fwd"))

    def _arr_content(self, d, data, start, x0, x1, y0, x_of, ppb):
        if not data or x1-x0 < 5:
            return
        top, bottom = y0+31, y0+ARR_LANE_H-10
        ink = DIM if data.get('muted') else TEXT
        if data.get('error'):
            return
        if data.get('midi'):
            notes = data.get('notes', [])
            if not notes:
                return
            lo, hi = min(n[0] for n in notes), max(n[0] for n in notes)
            span = max(12, hi-lo+3)
            offset, ls, le = data['offset'], data['loop_start'], data['loop_end']
            visible_a, visible_b = (x0-x_of(start))/ppb, (x1-x_of(start))/ppb
            # Initial pass, then only the loop iterations intersecting this viewport.
            passes = [(0., False)]
            if data['looping'] and le > ls:
                n0 = max(1, int((visible_a+offset-le)//(le-ls))+1)
                n1 = max(n0, int((visible_b+offset-ls)//(le-ls))+1)
                passes += [(k*(le-ls), True) for k in range(n0, min(n1+1, n0+256))]
            for shift, loop in passes:
                for pitch, beat, duration, velocity, muted in notes:
                    if loop and not ls <= beat < le:
                        continue
                    if not loop and data['looping'] and beat >= le:
                        continue
                    a = beat+shift-offset
                    end = min(beat+duration, le) if data['looping'] else beat+duration
                    b = end+shift-offset
                    left, right = max(x0+2, x_of(start+a)), min(x1-2, x_of(start+b))
                    if b <= visible_a or a >= visible_b or right < left:
                        continue
                    y = bottom-(pitch-lo+1)*(bottom-top)/span
                    d.line((left,y,max(left,right),y), fill=scale(ink,.35 if muted else .85), width=2)
        elif data.get('file') and data.get('sample_positions') and data.get('frames', 0) > 0:
            peaks = self.wave_peaks(data['file'], 4096)
            if not isinstance(peaks, list):
                if x1-x0 > 100:
                    d.text((x0+6,top),'Loading audio…' if peaks is None else 'Preview unavailable',font=font(12),fill=DIM)
                return
            positions, (a,b) = data['sample_positions'], data['range']
            mid, half = (top+bottom)/2, (bottom-top)/2
            for x in range(int(x0)+2, int(x1)-2, 2):
                rel = (x-x_of(start))/ppb
                if not a <= rel < b or b <= a:
                    continue
                f = (rel-a)/(b-a)*(len(positions)-1)
                k = min(len(positions)-2, int(f))
                s0, s1 = positions[k:k+2]
                frame = s0 if s1 < s0 else s0+(s1-s0)*(f-k)
                ix = int(frame/data['frames']*len(peaks))
                if 0 <= ix < len(peaks):
                    low, high = peaks[ix]
                    d.line((x,mid-max(-1,min(1,high))*half,x,mid-max(-1,min(1,low))*half),fill=ink)

    # ------------------------------------------------------------ clip (piano roll + automation)

    def _page_clip(self, d, live, ui):
        auto = ui.clip_mode == "auto"
        self._soft(d, 0, "NOTES", active=ui.clip_mode == "notes", action=("clip", "mode_notes"))
        self._soft(d, 1, "AUTOMATION", active=auto, action=("clip", "mode_auto"))
        self._soft(d, 7, "DETAILS", active=ui.clip_mode == "details", action=("clip", "mode_details"))
        t, s = live.clip if live.clip else (live.sel, 0)
        tr = live.tracks[t] if 0 <= t < len(live.tracks) else None
        col = rgb(live.clip_info.get("color") or (tr.color if tr else 0x808080))
        if not live.clip_exists():
            self._soft(d, 6, "AUTO ARM", active=live.auto_arm, color=REC, action=("clip", "auto_arm"))
            msg = "%s, scene %d: empty slot" % (tr.name if tr else "?", s + 1)
            d.text((W // 2, MAIN_Y + 150), msg, font=font(34), fill=DIM, anchor="mm")
            d.text((W // 2, MAIN_Y + 200), "SHIFT + tap a clip (or SHIFT + pad) on the Session page to pick another",
                   font=font(22, "SemiLight SemiCondensed"), fill=FAINT, anchor="mm")
            for i, bars in enumerate((1, 2, 4)):
                x0 = W // 2 - 330 + i * 230
                d.rounded_rectangle([x0, MAIN_Y + 270, x0 + 200, MAIN_Y + 350], 10, outline=col, width=3)
                d.text((x0 + 100, MAIN_Y + 310), "New %d-bar clip" % bars, font=font(24), fill=TEXT, anchor="mm")
                self.target(x0, MAIN_Y + 270, x0 + 200, MAIN_Y + 350, ("clip", "create", bars))
            return
        if ui.clip_mode == "details":
            self._clip_details(d, live, ui, col)
            return
        if auto:
            self._clip_auto_buttons(d, live, ui)
        else:
            grid_label = dict(GRIDS).get(ui.grid, "?")
            for i, (label, active, act) in enumerate((
                    ("GRID " + grid_label, False, "grid"), ("QUANTIZE", False, "quantize"),
                    ("DUPLICATE", False, "duplicate"), ("CLEAR NOTES", False, "clear"),
                    ("AUTO ARM", live.auto_arm, "auto_arm"))):
                self._soft(d, i + 2, label, active=active, color=REC if act == "auto_arm" else None,
                           action=("clip", act))
        v0, vlen = ui.clip_view_range(live)
        ppb = (RAIL_X - KEY_W) / vlen
        x_of = lambda beat: KEY_W + (beat - v0) * ppb      # noqa: E731
        bpb = live.beats_per_bar()
        ry, gy0 = MAIN_Y, MAIN_Y + ROLL_RULER_H
        gy1 = gy0 + ROLL_ROWS * ROLL_ROW_H
        # ruler + corner label
        d.rectangle([KEY_W, ry, RAIL_X - 1, gy0 - 2], fill=PANEL)
        name = live.clip_info.get("name") or "Clip"
        d.text((KEY_W // 2, ry + ROLL_RULER_H // 2), fit(d, name, font(16), KEY_W - 8), font=font(16), fill=col,
               anchor="mm")
        beat_step = 1.0 if ppb >= 18 else bpb
        while vlen / beat_step > 64:            # never more than ~64 labels, whatever the zoom
            beat_step *= 2
        b = (v0 // beat_step) * beat_step
        while b <= v0 + vlen:
            x = x_of(b)
            if KEY_W <= x < RAIL_X:
                bar, beat = divmod(b, bpb)
                lab = "%d" % (bar + 1) if beat == 0 else "%d.%d" % (bar + 1, beat + 1)
                d.text((x + 3, ry + ROLL_RULER_H // 2), lab, font=font(14), fill=DIM if beat == 0 else FAINT,
                       anchor="lm")
            b += beat_step
        if not auto:        # the pads' 16 steps
            p0 = live.clip_range()[0] + ui.step_page * 16 * ui.grid
            sx0, sx1 = max(KEY_W, x_of(p0)), min(RAIL_X - 1, x_of(p0 + 16 * ui.grid))
            if sx1 > sx0:
                d.rectangle([sx0, gy0 - 5, sx1, gy0 - 2], fill=ACCENT)
        self.target(KEY_W, ry, RAIL_X, gy0, ("drag", "roll_scroll", 0))
        # grid lines
        g = ui.grid if ui.grid * ppb >= 6 else (1.0 if ppb >= 6 else bpb)
        while vlen / g > 256:                   # cap the number of grid lines too
            g *= 2
        k = (v0 // g) * g
        while k <= v0 + vlen:
            x = x_of(k)
            if KEY_W <= x < RAIL_X:
                strong = abs(k / bpb - round(k / bpb)) < 1e-6
                beat = abs(k - round(k)) < 1e-6
                d.line([x, gy0, x, gy1], fill=(70, 70, 70) if strong else ((44, 44, 44) if beat else (28, 28, 28)))
            k += g
        a, bend = live.clip_range()
        for edge in (a, bend):                          # loop brace ends
            x = x_of(edge)
            if KEY_W <= x < RAIL_X:
                d.line([x, ry, x, gy1], fill=col, width=2)
        if auto:
            self._clip_auto_lane(d, live, ui, x_of, gy0, gy1, col)
        else:
            self._clip_notes(d, live, ui, x_of, ppb, gy0, col)
        pos = live.clip_info.get("playing_position")
        if live.playing and tr and tr.playing == s and isinstance(pos, (int, float)):
            x = x_of(pos)
            if KEY_W <= x < RAIL_X:
                d.line([x, ry, x, gy1], fill=PLAY, width=2)
        # rail: pitch scroll (notes) / clear (automation)
        half = MAIN_H // 2
        for i, (y, act) in enumerate(((MAIN_Y, "up"), (MAIN_Y + half, "down"))):
            d.rounded_rectangle([RAIL_X + 4, y + 4, W - 4, y + half - 5], 6, fill=PANEL)
            cx, cy = RAIL_X + 40, y + half // 2
            if auto:
                if i == 0:
                    d.text((cx, cy), "CLEAR", font=font(18, "Bold SemiCondensed"), fill=TEXT, anchor="mm")
                    self.target(RAIL_X, y, W, y + half, ("clip", "env_clear"))
                continue
            tri = [(cx - 14, cy + 8), (cx + 14, cy + 8), (cx, cy - 10)] if act == "up" else \
                [(cx - 14, cy - 8), (cx + 14, cy - 8), (cx, cy + 10)]
            d.polygon(tri, fill=TEXT)
            d.text((cx, cy + (30 if act == "up" else -30)), "octave", font=font(13), fill=FAINT, anchor="mm")
            self.target(RAIL_X, y, W, y + half, ("clip", "oct_" + act))

    def _clip_notes(self, d, live, ui, x_of, ppb, gy0, col):
        for r in range(ROLL_ROWS):
            p = ui.pitch_lo + ROLL_ROWS - 1 - r                   # top row = highest pitch
            y0 = gy0 + r * ROLL_ROW_H
            black = NOTE_NAMES[p % 12].endswith("#")
            focus = p == ui.focus_pitch
            d.rectangle([0, y0, KEY_W - 3, y0 + ROLL_ROW_H - 2],
                        fill=ACCENT if focus else ((40, 40, 40) if black else (200, 200, 200)))
            d.text((KEY_W - 10, y0 + ROLL_ROW_H // 2), note_name(p), font=font(15),
                   fill=(0, 0, 0) if focus or not black else TEXT, anchor="rm")
            if black or focus:
                d.rectangle([KEY_W, y0, RAIL_X - 1, y0 + ROLL_ROW_H - 1],
                            fill=(40, 26, 12) if focus else (12, 12, 12))
            d.line([KEY_W, y0 + ROLL_ROW_H - 1, RAIL_X - 1, y0 + ROLL_ROW_H - 1], fill=(24, 24, 24))
            self.target(0, y0, KEY_W, y0 + ROLL_ROW_H, ("clip", "focus", p))
        top = ui.pitch_lo + ROLL_ROWS - 1
        for note in live.notes:
            p, start, dur, vel = note[0], note[1], note[2], note[3]
            if not ui.pitch_lo <= p <= top:
                continue
            x0, x1 = max(KEY_W, x_of(start)), min(RAIL_X - 1, x_of(start + dur))
            if x1 <= KEY_W or x0 >= RAIL_X:
                continue
            y0 = gy0 + (top - p) * ROLL_ROW_H
            fill = scale(col, 0.35 + 0.65 * min(127, vel) / 127)
            d.rectangle([x0 + 1, y0 + 2, max(x0 + 3, x1 - 1), y0 + ROLL_ROW_H - 3], fill=fill,
                        outline=TEXT if note is ui.sel_note else scale(col, 1.2), width=2 if note is ui.sel_note else 1)
        self.target(KEY_W, gy0, RAIL_X, gy0 + ROLL_ROWS * ROLL_ROW_H, ("drag", "roll", 0))

    LAUNCH_MODES = ["Trigger", "Gate", "Toggle", "Repeat"]
    LAUNCH_QUANT = ["Global", "None", "8 Bars", "4 Bars", "2 Bars", "1 Bar", "1/2", "1/2T", "1/4", "1/4T", "1/8",
                    "1/8T", "1/16", "1/16T", "1/32"]
    WARP_MODES = {0: "Beats", 1: "Tones", 2: "Texture", 3: "Re-Pitch", 4: "Complex", 5: "REX", 6: "Complex Pro"}
    NOTE_TOOLS = [("-12 st", "transpose", -12), ("-1 st", "transpose", -1), ("+1 st", "transpose", 1),
                  ("+12 st", "transpose", 12), ("Vel -10", "velocity", -10), ("Vel +10", "velocity", 10),
                  ("Humanize", "humanize", 0.5), ("Legato", "legato", 0)]

    @staticmethod
    def clip_fields(det):
        """The DETAILS view's cards for this clip: [(label, key)], the first row (and the Q-Links) first."""
        row1 = [("Start", "start_marker"), ("End", "end_marker"), ("Loop start", "loop_start"),
                ("Loop length", "loop_length")]
        if det.get("audio"):
            return row1 + [("Gain", "gain"), ("Transpose", "pitch_coarse"), ("Detune", "pitch_fine"),
                           ("Warp mode", "warp_mode")]
        return row1 + [("Launch", "launch_mode"), ("Quantize", "launch_quantization"), ("Legato", "legato"),
                       ("Signature", None)]

    def clip_field_text(self, det, key, bpb):
        def pos(b):                                 # Live's bars.beats.sixteenths, from 1.1.1
            b = max(0.0, b)
            return "%d.%d.%d" % (b // bpb + 1, int(b % bpb) + 1, int(round((b % 1) * 4)) % 4 + 1)

        def span(b):                                # a length: bars.beats.sixteenths from 0.0.0
            return "%d.%d.%d" % (b // bpb, int(b % bpb), int(round((b % 1) * 4)) % 4)
        if key in ("start_marker", "end_marker", "loop_start"):
            return pos(det.get(key, 0.0))
        if key == "loop_length":
            return span(det.get("loop_end", 0.0) - det.get("loop_start", 0.0))
        if key == "launch_mode":
            return self.LAUNCH_MODES[det.get("launch_mode", 0) % 4]
        if key == "launch_quantization":
            q = det.get("launch_quantization", 0)
            return self.LAUNCH_QUANT[q] if 0 <= q < len(self.LAUNCH_QUANT) else str(q)
        if key == "legato":
            return "On" if det.get("legato") else "Off"
        if key == "gain":
            return det.get("gain_str", "")
        if key == "pitch_coarse":
            return "%+d st" % det.get("pitch_coarse", 0)
        if key == "pitch_fine":
            return "%+d ct" % round(det.get("pitch_fine", 0))
        if key == "warp_mode":
            return self.WARP_MODES.get(det.get("warp_mode"), "?") if det.get("warping") else "Warp off"
        if key is None:
            return "%d/%d" % (det.get("sig_num", 4), det.get("sig_den", 4))
        return ""

    def _clip_details(self, d, live, ui, col):
        """Clip page DETAILS: loop, markers, launch settings; audio clips also gain / pitch / warp and the
        waveform; MIDI clips note tools. Drag a card up/down (tap the on/off and list ones), Q-Links = top row."""
        det = live.clip_details
        if not det:
            d.text((W // 2, MAIN_Y + 260), "Reading the clip…", font=font(30), fill=FAINT, anchor="mm")
            return
        audio = bool(det.get("audio"))
        tools = [("LOOP", "loop_toggle", det.get("looping")), ("CROP", "crop", False), ("DOUBLE", "duplicate", False)]
        tools += [("WARP", "warp_toggle", det.get("warping"))] if audio else [("QUANTIZE", "quantize", False)]
        for i, (label, act, on) in enumerate(tools):
            self._soft(d, i + 2, label, active=bool(on), color=col, action=("clip", act))
        bpb = live.beats_per_bar()
        y = MAIN_Y + 12
        kind = "Audio clip" if audio else "MIDI clip"
        if audio:
            kind += "  ·  " + str(det.get("file", "")).replace("\\", "/").split("/")[-1]
        d.text((16, y + 12), fit(d, "%s  ·  %s" % (live.clip_info.get("name") or "Clip", kind), font(20), 900),
               font=font(20), fill=TEXT, anchor="lm")
        y += 36
        if audio:                                    # the sample with the clip's start / end and loop marked
            x0, x1, y0, y1 = 16, RAIL_X - 16, y, y + 130
            d.rounded_rectangle([x0, y0, x1, y1], 8, fill=PANEL)
            p = self.wave_peaks(det.get("file", ""), x1 - x0 - 16)
            n = max(1, det.get("sample_length") or 1)
            fx = lambda fr: x0 + 8 + (x1 - x0 - 16) * max(0.0, min(1.0, fr / n))   # noqa: E731
            if isinstance(p, list):
                mid, half = (y0 + y1) / 2, (y1 - y0) / 2 - 8
                a, b = fx(det.get("f_start", 0)), fx(det.get("f_end", n))
                for c, (lo, hi) in enumerate(p):
                    x = x0 + 8 + c
                    d.line([x, mid - hi * half, x, mid - lo * half], fill=col if a <= x <= b else (60, 60, 60))
            if det.get("looping") and det.get("f_loop_start", -1) >= 0:
                la, lb = fx(det["f_loop_start"]), fx(det["f_loop_end"])
                d.rectangle([la, y0 + 2, lb, y0 + 8], fill=col)
            for key, label in (("f_start", "S"), ("f_end", "E")):
                if det.get(key, -1) >= 0:
                    x = fx(det[key])
                    d.line([x, y0 + 4, x, y1 - 4], fill=TEXT, width=2)
                    d.text((x + (4 if label == "S" else -4), y0 + 16), label, font=font(13, "Bold SemiCondensed"),
                           fill=TEXT, anchor="lm" if label == "S" else "rm")
            y = y1 + 12
        cw, ch = COL_W * 2, 112
        for k, (label, key) in enumerate(self.clip_fields(det)):
            r, c = divmod(k, 4)
            x0, y0 = c * cw + 6, y + r * (ch + 10)
            d.rounded_rectangle([x0, y0, x0 + cw - 12, y0 + ch], 10, fill=PANEL,
                                outline=TEXT if r == 0 and c in ui.qlink_touch else LINE, width=2)
            d.text((x0 + 16, y0 + 24), label, font=font(18), fill=DIM, anchor="lm")
            if r == 0:
                d.text((x0 + cw - 28, y0 + 24), "Q%d" % (c + 1), font=font(14), fill=ACCENT, anchor="rm")
            d.text((x0 + 16, y0 + 72), self.clip_field_text(det, key, bpb), font=font(34, "Bold SemiCondensed"),
                   fill=TEXT if key else DIM, anchor="lm")
            if key:
                self.target(x0, y0, x0 + cw - 12, y0 + ch, ("drag", "cfield", key))
        if not audio:                                 # note tools for MIDI clips (Live's UNDO undoes them)
            ty = y + 2 * (ch + 10) + 8
            d.text((16, ty + 12), "Notes", font=font(16), fill=DIM, anchor="lm")
            for i, (label, op, amount) in enumerate(self.NOTE_TOOLS):
                x = i * COL_W
                d.rounded_rectangle([x + 6, ty + 28, x + COL_W - 6, ty + 84], 8, fill=CELL, outline=LINE, width=1)
                d.text((x + COL_W // 2, ty + 56), label, font=font(20), fill=TEXT, anchor="mm")
                self.target(x, ty + 24, x + COL_W, ty + 88, ("clip", "tool", (op, amount)))

    def _clip_auto_buttons(self, d, live, ui):
        keys = [((-1, 0), "VOLUME"), ((-1, 1), "PAN")]
        dev = live.dev
        # clip envelopes can only address the track's own devices, so skip devices inside racks
        if dev.names and not dev.chain and dev.track == (live.clip[0] if live.clip else -1):
            for p in ui.qlink_params(dev):
                keys.append(((dev.idx, p), dev.pnames[p] if p < len(dev.pnames) else "?"))
        for i, (key, label) in enumerate(keys[:5]):
            self._soft(d, i + 2, label, active=tuple(live.env_key) == key, action=("clip", "env_key", key))

    def _clip_auto_lane(self, d, live, ui, x_of, gy0, gy1, col):
        e = live.env
        key = tuple(live.env_key)
        label = {(-1, 0): "Volume", (-1, 1): "Pan"}.get(key)
        if label is None:
            dev = live.dev
            label = dev.pnames[key[1]] if key[0] == dev.idx and key[1] < len(dev.pnames) else "Parameter"
        d.rectangle([0, gy0, KEY_W - 3, gy1], fill=PANEL)
        d.text((KEY_W // 2, gy0 + 24), fit(d, label, font(18), KEY_W - 10), font=font(18), fill=TEXT, anchor="mm")
        d.text((KEY_W // 2, gy0 + 48), "max", font=font(13), fill=FAINT, anchor="mm")
        d.text((KEY_W // 2, gy1 - 16), "min", font=font(13), fill=FAINT, anchor="mm")
        a, b = live.clip_range()
        if e and e.get("vals"):
            lo, hi = e["min"], e["max"]
            n = len(e["vals"])
            pts = []
            for i, v in enumerate(e["vals"] + [e["vals"][-1]]):
                x = x_of(a + (b - a) * i / n)
                y = gy1 - (gy1 - gy0 - 8) * ((v - lo) / (hi - lo) if hi > lo else 0) - 4
                pts.append((x, y))
            d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=scale(col, 0.3))
            d.line(pts, fill=col, width=3)
        msg = ""
        if e and e.get("error"):
            msg = "Can't read this automation (%s)" % e["error"]
        elif not (e and e.get("exists")):
            msg = "No automation yet: draw it with your finger, or arm AUTO ARM + record and turn a Q-Link"
        if msg:
            d.text(((KEY_W + RAIL_X) // 2, (gy0 + gy1) // 2), msg, font=font(22, "SemiLight SemiCondensed"),
                   fill=DIM, anchor="mm")
        self.target(KEY_W, gy0, RAIL_X, gy1, ("drag", "env", 0))

    # ------------------------------------------------------------ mix

    MIX_TABS = [("Volume", "vol"), ("Pan", "pan"), ("Sends", "sends"), ("In / Out", "io")]
    MIX_FIELDS = [("IN", "input_routing_type", 0), ("", "input_routing_channel", 1),
                  ("OUT", "output_routing_type", 2), ("", "output_routing_channel", 3)]

    def mix_strips(self, live, ui):
        """[(column, key, strip)] on the Mix page: 8 tracks, or the return tracks followed by the master."""
        if ui.mix_returns:
            keys = [("return", r) for r in range(min(len(live.returns), COLS - 1))] + [("master", 0)]
        else:
            keys = [("track", t) for t in range(ui.bank, min(ui.bank + COLS, len(live.tracks)))]
        return [(c, k, live.strip(k)) for c, k in enumerate(keys) if live.strip(k) is not None]

    def _page_mix(self, d, live, ui):
        for i, (label, tab) in enumerate(self.MIX_TABS):
            active = ui.mix_view == tab if tab in ("sends", "io") else (ui.mix_view == "mix" and ui.mix_target == tab)
            self._soft(d, i, label, active=active, action=("mix_tab", tab))
        self._soft(d, 7, "RETURNS", sub="and main", active=ui.mix_returns, action=("mix_returns",))
        qkeys = ui.qlink_strips(live)
        for c, key, st in self.mix_strips(live, ui):
            q = qkeys.index(key) if key in qkeys else None
            if ui.mix_view == "sends":
                self._mix_sends(d, c, key, st, live, ui, q)
            elif ui.mix_view == "io":
                self._mix_io(d, c, key, st, live, ui)
            else:
                self._mix_fader(d, c, key, st, ui, q)
        if ui.mix_view == "io" and ui.mix_returns:
            d.text((W // 2, LOW_Y - 40), "Routing is shown for tracks: turn RETURNS off", font=font(20), fill=FAINT,
                   anchor="mm")
        if ui.mix_view == "sends" and not live.returns:
            d.text((W // 2, MAIN_Y + 200), "No return tracks: add one from MENU (New return track)", font=font(24),
                   fill=FAINT, anchor="mm")

    def _strip_buttons(self, key, st):
        """(what, label, on, colour, action) for the M / S / arm buttons under a strip."""
        kind, i = key
        if kind == "master":
            return []
        out = [("mute", "M", st.mute, MUTE), ("solo", "S", st.solo, SOLO)]
        if kind == "track":
            return [(w, l, on, c, ("toggle", w, i)) for w, l, on, c in out] + \
                [("arm", "", st.arm, REC, ("toggle", "arm", i))]
        return [(w, l, on, c, ("strip_toggle", w, key)) for w, l, on, c in out]

    def _mix_fader(self, d, c, key, tr, ui, q):
        x0, cx = c * COL_W, c * COL_W + COL_W // 2
        col = rgb(tr.color)
        if q is not None:
            d.rounded_rectangle([x0 + 3, MAIN_Y + 4, x0 + COL_W - 4, LOW_Y - 6], 8,
                                outline=TEXT if q in ui.qlink_touch else LINE, width=2)
            d.text((x0 + 14, MAIN_Y + 18), "Q%d" % (q + 1), font=font(15), fill=ACCENT, anchor="lm")
        if key[0] != "track":
            d.text((cx, MAIN_Y + 18), fit(d, tr.name, font(15), COL_W - 50), font=font(15), fill=col, anchor="mm")
        d.text((cx, MAIN_Y + 48), tr.vol_str or "", font=font(26, "Bold SemiCondensed"), fill=TEXT, anchor="mm")
        # pan bar
        py = MAIN_Y + 84
        d.rectangle([cx - 56, py, cx + 56, py + 6], fill=CELL)
        p = max(-1.0, min(1.0, tr.pan))
        d.rectangle([min(cx, cx + p * 56), py, max(cx, cx + p * 56) + 1, py + 6], fill=col)
        d.line([cx, py - 4, cx, py + 10], fill=DIM)
        pan_txt = "C" if abs(p) < 0.01 else "%d%s" % (round(abs(p) * 50), "L" if p < 0 else "R")
        d.text((cx, py + 24), pan_txt, font=font(15), fill=DIM, anchor="mm")
        self.target(x0, py - 16, x0 + COL_W, py + 34, ("drag", "pan", key))
        # fader + meter
        fy0, fy1 = MAIN_Y + 136, MAIN_Y + 470
        fx = cx - 22
        d.rounded_rectangle([fx - 6, fy0, fx + 6, fy1], 6, fill=CELL)
        fy = fy1 - (fy1 - fy0) * tr.vol
        d.rounded_rectangle([fx - 6, fy, fx + 6, fy1], 6, fill=col)
        zy = fy1 - (fy1 - fy0) * 0.85
        d.line([fx - 14, zy, fx + 14, zy], fill=DIM, width=2)
        d.rounded_rectangle([fx - 18, fy - 7, fx + 18, fy + 7], 4, fill=TEXT)
        mx = cx + 18
        d.rectangle([mx - 6, fy0, mx + 6, fy1], fill=CELL)
        m = max(0.0, min(1.0, tr.meter))
        if m > 0.001:
            my = fy1 - (fy1 - fy0) * m
            d.rectangle([mx - 6, my, mx + 6, fy1], fill=PLAY if m < 0.8 else (MUTE if m < 0.95 else REC))
        self.target(x0, fy0 - 10, x0 + COL_W, fy1 + 10, ("drag", "vol", key))
        # buttons
        by = MAIN_Y + 500
        for i, (what, label, on, oncol, action) in enumerate(self._strip_buttons(key, tr)):
            bx = x0 + 13 + i * 43
            box = [bx, by, bx + 38, by + 44]
            usable = what != "arm" or tr.armable
            if on:
                d.rounded_rectangle(box, 6, fill=oncol)
            else:
                d.rounded_rectangle(box, 6, outline=FAINT if usable else LINE, width=2)
            fg = (0, 0, 0) if on else (DIM if usable else LINE)
            if what == "arm":       # record dot, drawn (no ● glyph in Bahnschrift)
                d.ellipse([bx + 12, by + 15, bx + 26, by + 29], fill=fg if on else None,
                          outline=fg if on else (REC if usable else LINE), width=3)
            else:
                d.text((bx + 19, by + 22), label, font=font(20, "Bold SemiCondensed"), fill=fg, anchor="mm")
            self.target(bx - 3, by - 6, bx + 41, by + 50, action)

    SEND_ROW_H = 82

    def _mix_sends(self, d, c, key, st, live, ui, q):
        """A strip's sends as a column of knobs, one per return track. The Q-Links turn the highlighted send."""
        x0 = c * COL_W
        col = rgb(st.color)
        if key[0] != "track":
            d.text((x0 + COL_W // 2, MAIN_Y + 14), fit(d, st.name, font(15), COL_W - 16), font=font(15), fill=col,
                   anchor="mm")
        rows = (LOW_Y - MAIN_Y - 30) // self.SEND_ROW_H
        for n in range(min(len(st.sends), rows)):
            y0 = MAIN_Y + 28 + n * self.SEND_ROW_H
            hot = n == ui.send_sel
            if hot and q is not None:
                d.rounded_rectangle([x0 + 4, y0, x0 + COL_W - 5, y0 + self.SEND_ROW_H - 6], 8,
                                    fill=PANEL, outline=TEXT if q in ui.qlink_touch else LINE, width=2)
                d.text((x0 + COL_W - 12, y0 + 14), "Q%d" % (q + 1), font=font(13), fill=ACCENT, anchor="rm")
            rname = live.returns[n].name if n < len(live.returns) else chr(65 + n)
            d.text((x0 + 12, y0 + 14), fit(d, rname, font(15), COL_W - 50), font=font(15),
                   fill=TEXT if hot else DIM, anchor="lm")
            self._knob(d, x0 + 36, y0 + 50, 20, st.sends[n], col, key=("send", key, n))
            txt = st.send_strs[n] if n < len(st.send_strs) else ""
            d.text((x0 + 64, y0 + 50), fit(d, txt, font(18), COL_W - 72), font=font(18), fill=TEXT, anchor="lm")
            self.target(x0, y0, x0 + COL_W, y0 + self.SEND_ROW_H - 6, ("drag", "send", (key, n)))
        if len(st.sends) > rows:
            d.text((x0 + COL_W // 2, LOW_Y - 14), "+%d more" % (len(st.sends) - rows), font=font(13), fill=FAINT,
                   anchor="mm")

    def _mix_io(self, d, c, key, tr, live, ui):
        """A track's input, monitoring and output, like the I/O section of Live's mixer. Tap a box to change it."""
        if key[0] != "track":
            return
        t = key[1]
        x0 = c * COL_W
        r = tr.route
        if r is None:
            d.text((x0 + COL_W // 2, MAIN_Y + 60), "…", font=font(22), fill=FAINT, anchor="mm")
            return
        y = MAIN_Y + 16
        for label, field, idx in self.MIX_FIELDS:
            if label:
                d.text((x0 + 12, y + 10), label, font=font(14), fill=DIM, anchor="lm")
                y += 22
            val = r[idx]
            box = [x0 + 6, y, x0 + COL_W - 7, y + 56]
            d.rounded_rectangle(box, 6, fill=CELL if val else BG, outline=LINE, width=1)
            d.text((x0 + 14, y + 28), fit(d, val or "—", font(17), COL_W - 30), font=font(17),
                   fill=TEXT if val else FAINT, anchor="lm")
            if val:
                self.target(box[0], box[1], box[2], box[3], ("route", t, field))
            y += 64
            if field == "input_routing_channel":            # monitoring sits between the input and the output
                d.text((x0 + 12, y + 10), "MONITOR", font=font(14), fill=DIM, anchor="lm")
                y += 22
                for k, name in enumerate(("In", "Auto", "Off")):
                    bw = (COL_W - 16) // 3
                    bx = x0 + 6 + k * (bw + 1)
                    on = r[4] == k
                    if r[4] < 0:
                        d.rounded_rectangle([bx, y, bx + bw - 2, y + 44], 5, outline=LINE, width=1)
                        d.text((bx + bw // 2, y + 22), name, font=font(15), fill=LINE, anchor="mm")
                        continue
                    d.rounded_rectangle([bx, y, bx + bw - 2, y + 44], 5, fill=ACCENT if on else CELL)
                    d.text((bx + bw // 2, y + 22), name, font=font(15), fill=ink(ACCENT) if on else DIM, anchor="mm")
                    self.target(bx, y, bx + bw - 2, y + 44, ("monitor", t, k))
                y += 60

    # ------------------------------------------------------------ device

    DEVICE_MENU = [
        ("Instruments", ["Drum Rack", "Simpler", "Sampler", "Instrument Rack", "Operator", "Wavetable", "Drift",
                         "Meld", "Analog", "Electric", "Tension", "Collision"]),
        ("MIDI Effects", ["Arpeggiator", "Chord", "Note Length", "Pitch", "Random", "Scale", "Velocity",
                          "MIDI Effect Rack"]),
        ("Audio Effects", ["EQ Eight", "Compressor", "Glue Compressor", "Multiband Dynamics", "Limiter", "Saturator",
                           "Roar", "Drum Buss", "Auto Filter", "Reverb", "Hybrid Reverb", "Delay", "Echo",
                           "Chorus-Ensemble", "Phaser-Flanger", "Utility", "Audio Effect Rack"]),
    ]
    STRIP_Y, STRIP_H = MAIN_Y + 48, 78           # device chain strip
    KNOBS_Y = MAIN_Y + 140                       # knob block top

    def _page_device(self, d, live, ui):
        dev = live.dev
        tr = live.tracks[dev.track] if 0 <= dev.track < len(live.tracks) else None
        col = rgb(tr.color) if tr else ACCENT
        info = dev.selected_info()
        on = bool(dev.pvals[0]) if dev.pvals else bool(info[1])
        if dev.names:
            self._soft(d, 0, "ON" if on else "OFF", active=on, color=col, action=("device_on",))
            if info[2]:
                self._soft(d, 1, "CLOSE RACK" if dev.view == "chains" else "OPEN RACK",
                           sub="%d %s" % (info[4], "pads" if info[3] else "chains"), active=dev.view == "chains",
                           color=col, action=("dev_open",))
            self._soft(d, 3, "DELETE", action=("dev_delete",))
            smp = dev.sample or {}
            if info[0] not in PLUGIN_CLASSES and dev.view == "devices":    # swap presets, like Push
                slot = 7 if dev.is_simpler() else 5
                if not (slot == 7 and dev.chain):
                    self._soft(d, slot, "PRESET", sub=dev.names[dev.idx] if dev.idx < len(dev.names) else None,
                               color=col, action=("presets", 0))
            if dev.is_simpler() and smp.get("file") and dev.view == "devices":
                self._soft(d, 1, "WARP", active=bool(smp.get("warp")), color=col, action=("simpler", "warp"))
                for k, (label, mode) in enumerate((("CLASSIC", 0), ("1-SHOT", 1), ("SLICE", 2))):
                    self._soft(d, 4 + k, label, active=smp.get("mode") == mode, color=col,
                               action=("simpler", "mode", mode))
            if info[0] in PLUGIN_CLASSES:
                self._soft(d, 4, "PLUG-IN UI" if ui.plugin_ui else "KNOBS", sub="tap: knobs" if ui.plugin_ui
                           else "tap: plug-in window", active=True, color=col, action=("plugin_ui",))
                cur = dev.presets[dev.preset_sel] if 0 <= dev.preset_sel < len(dev.presets) else ""
                self._soft(d, 5, "PRESET", sub=cur or None, color=col if len(dev.presets) > 1 else None,
                           action=("presets", 0))
        self._soft(d, 2, "ADD DEVICE", active=ui.add_menu, color=col, action=("dev_add",))
        if dev.chain or dev.view == "chains":
            self._soft(d, 7, "‹ UP", action=("dev_up",))
        # breadcrumb: track > rack > chain ...
        crumbs = [tr.name if tr else "?"] + dev.crumbs
        if dev.view == "chains" and dev.names:
            crumbs.append(dev.names[dev.idx])
        x = 16
        if dev.chain or dev.view == "chains":
            d.rounded_rectangle([12, MAIN_Y + 6, 104, MAIN_Y + 38], 16, outline=FAINT, width=2)
            d.text((58, MAIN_Y + 22), "‹ Back", font=font(18), fill=TEXT, anchor="mm")
            self.target(0, MAIN_Y, 112, MAIN_Y + 44, ("dev_up",))
            x = 120
        d.text((x, MAIN_Y + 22), fit(d, "  ›  ".join(crumbs), font(22), RAIL_X - x - 20), font=font(22), fill=DIM,
               anchor="lm")
        if not dev.loaded and not dev.names:
            d.text((W // 2, MAIN_Y + 260), "Loading devices…", font=font(30), fill=FAINT, anchor="mm")
        else:
            self._chain_strip(d, dev, col)
            if not dev.names:
                where = "this chain" if dev.chain else (tr.name if tr else "this track")
                d.text((W // 2, MAIN_Y + 260), "No devices on %s" % where, font=font(34), fill=DIM, anchor="mm")
                d.text((W // 2, MAIN_Y + 310), "ADD DEVICE, or load one from BROWSE",
                       font=font(24, "SemiLight SemiCondensed"), fill=FAINT, anchor="mm")
            elif dev.view == "chains":
                self._rack_view(d, dev, ui, col)
            elif dev.selected_info()[0] in PLUGIN_CLASSES and ui.plugin_ui:   # the plug-in's own window
                self._plugin_window(d)
            elif dev.selected_info()[0] in PLUGIN_CLASSES:     # the plug-in as a screen of touch knobs
                self._plugin_knobs(d, dev, ui, col)
            elif dev.is_simpler():                      # Simpler: its sample on screen, knobs below
                self._simpler_wave(d, dev, col)
                self._bank_tabs(d, dev, col, self.KNOBS_Y + self.WAVE_H + 6)
                self._device_knobs(d, dev, ui, col, ky0=self.KNOBS_Y + self.WAVE_H + 40, focus=False)
            elif dev.selected_info()[0] in self.VISUALS + self.SYNTHS:   # a picture first (EQ curve, envelope..)
                self._device_visual(d, dev, col)
                self._bank_tabs(d, dev, col, self.KNOBS_Y + self.WAVE_H + 6)
                self._device_knobs(d, dev, ui, col, ky0=self.KNOBS_Y + self.WAVE_H + 40, focus=False)
            elif dev.layout:                            # a stock device with named knob pages
                self._bank_tabs(d, dev, col, self.KNOBS_Y - 8)
                self._device_knobs(d, dev, ui, col, ky0=self.KNOBS_Y + 26)
            else:
                self._device_knobs(d, dev, ui, col)
        if ui.add_menu:
            self._add_menu(d, live, ui, col)

    def _chain_strip(self, d, dev, col):
        """Every device in the chain side by side, like Live's device view, plus an add box."""
        bw, gap = 180, 14
        n = len(dev.names) + 1
        vis = (RAIL_X - 16) // (bw + gap)
        off = max(0, min(dev.idx - 2, n - vis))
        y0, y1 = self.STRIP_Y, self.STRIP_Y + self.STRIP_H
        for k in range(off, min(n, off + vis)):
            x0 = 12 + (k - off) * (bw + gap)
            if k == len(dev.names):                     # "+" box
                d.rounded_rectangle([x0, y0, x0 + bw, y1], 8, outline=FAINT, width=2)
                d.text((x0 + bw // 2, (y0 + y1) // 2), "+  Add device", font=font(20), fill=DIM, anchor="mm")
                self.target(x0, y0, x0 + bw, y1, ("dev_add",))
                continue
            cls, on, racky, drum, nch = (dev.info[k] if k < len(dev.info) else ("", 1, 0, 0, 0))
            sel = k == dev.idx
            fill = col if sel else PANEL
            d.rounded_rectangle([x0, y0, x0 + bw, y1], 8, fill=fill)
            fg = ink(fill) if sel else (TEXT if on else FAINT)
            d.ellipse([x0 + 10, y0 + 12, x0 + 20, y0 + 22], fill=fg if sel else (PLAY if on else FAINT))
            d.text((x0 + 28, y0 + 17), fit(d, dev.names[k], font(19), bw - 38), font=font(19), fill=fg, anchor="lm")
            sub = ("%d pads" % nch if drum else "%d chains" % nch) if racky else ("off" if not on else "")
            if sub:
                d.text((x0 + 12, y1 - 18), sub + ("  ›" if racky else ""), font=font(15), fill=fg if sel else DIM,
                       anchor="lm")
            if k + 1 < n:                                # signal flow arrow to the next device
                ax, ay = x0 + bw + 2, (y0 + y1) // 2
                d.polygon([(ax + 2, ay - 6), (ax + 2, ay + 6), (ax + 10, ay)], fill=FAINT)
            self.target(x0, y0, x0 + bw, y1, ("device", k))
        if off > 0 or off + vis < n:
            d.text((RAIL_X - 14, y1 + 10), "%d devices" % (n - 1), font=font(14), fill=FAINT, anchor="rm")

    SIMPLER_MODES = {0: "Classic", 1: "One-Shot", 2: "Slice"}
    PLUGIN_AREA = (16, None, RAIL_X - 16, LOW_Y - 8)     # y0 filled in from KNOBS_Y

    def plugin_area(self):
        x0, _, x1, y1 = self.PLUGIN_AREA
        return x0, self.KNOBS_Y, x1, y1

    def _plugin_window(self, d):
        x0, y0, x1, y1 = self.plugin_area()
        frame = self.plugin_frame
        if frame is None:
            d.rounded_rectangle([x0, y0, x1, y1], 8, fill=PANEL)
            d.text(((x0 + x1) // 2, (y0 + y1) // 2 - 16), "The plug-in window isn't open in Live",
                   font=font(28), fill=DIM, anchor="mm")
            d.text(((x0 + x1) // 2, (y0 + y1) // 2 + 22), "Open it once with the device's wrench button and it "
                   "shows here  ·  KNOBS (top row) for its knobs", font=font(20, "SemiLight SemiCondensed"),
                   fill=FAINT, anchor="mm")
            return
        img = frame
        px = x0 + (x1 - x0 - img.width) // 2
        py = y0 + (y1 - y0 - img.height) // 2
        d._image.paste(img, (px, py))
        self.plugin_place = (px, py)                    # touches here are played on the real window
        self.target(px, py, px + img.width, py + img.height, ("drag", "plugin", 0))

    def wave_peaks(self, path, columns):
        """Waveform peaks, computed once per file in a background thread (None while reading)."""
        key = (path, columns)
        if key not in self.wave_cache:
            if len(self.wave_cache) >= 64:
                # Never evict pending jobs: that would queue the same file again every frame.
                old = next((k for k,v in self.wave_cache.items() if v is not None), None)
                if old is None:
                    return None
                self.wave_cache.pop(old)
            self.wave_cache[key] = None

            def work():
                try:
                    self.wave_cache[key] = waveform.peaks(path, columns)
                except Exception as e:
                    self.wave_cache[key] = "error: %s" % e
            _wave_workers.submit(work)
        return self.wave_cache[key]

    WAVE_H = 150
    SLICE_STYLES = ["Transient", "Beat", "Region", "Manual"]

    def _bank_tabs(self, d, dev, col, y):
        """The device's named knob pages (Sample, Filter, ...) as tabs; tap one to show it."""
        if not dev.layout:
            return
        f = font(17)
        widths = [d.textlength(name, font=f) + 36 for name, _ps in dev.layout]
        first, room = min(dev.bank, len(widths) - 1), RAIL_X - 60
        while first > 0 and sum(widths[first - 1:dev.bank + 1]) < room * 0.6:     # keep the current tab in view
            first -= 1
        x = 16
        if first > 0:
            d.text((x + 6, y + 14), "…", font=f, fill=DIM, anchor="lm")
            x += 28
        for i, (name, _ps) in enumerate(dev.layout):
            if i < first:
                continue
            w = widths[i] - 8
            if x + w > RAIL_X - 10:
                d.text((RAIL_X - 14, y + 14), "…", font=f, fill=DIM, anchor="rm")
                break
            on = i == dev.bank
            d.rounded_rectangle([x, y, x + w, y + 28], 14, fill=col if on else None, outline=col if on else LINE,
                                width=1)
            d.text((x + w / 2, y + 14), name, font=f, fill=ink(col) if on else DIM, anchor="mm")
            self.target(x, y - 4, x + w, y + 32, ("param_bank_set", i))
            x += w + 8

    VISUALS = ("Eq8", "AutoFilter2", "Compressor2", "GlueCompressor", "Reverb", "Hybrid", "Delay", "Echo", "Saturator", "StereoGain")

    def _device_visual(self, d, dev, col):
        x0, x1, y0, y1 = 16, RAIL_X - 16, self.KNOBS_Y, self.KNOBS_Y + self.WAVE_H
        d.rounded_rectangle([x0, y0, x1, y1], 8, fill=PANEL)
        if not dev.pnames:
            return
        cls = dev.selected_info()[0]
        box = (x0, y0, x1, y1)
        if cls in self.SYNTHS:
            self._synth_picture(d, dev, col, box)
        elif cls in ("Reverb", "Hybrid"):
            self._visual_reverb(d, dev, col, box, hybrid=cls == "Hybrid")
        elif cls in ("Delay", "Echo"):
            self._visual_delay(d, dev, col, box)
        elif cls == "Saturator":
            self._visual_saturator(d, dev, col, box)
        elif cls == "StereoGain":
            self._visual_utility(d, dev, col, box)
        elif cls in ("Compressor2", "GlueCompressor"):
            self._visual_comp(d, dev, col, (x0, y0, x1, y1), glue=cls == "GlueCompressor")
        else:
            self._visual_curve(d, dev, col, (x0, y0, x1, y1), eq=cls == "Eq8")

    def _pv(self, dev, name, default=0.0):
        i = dev.pidx(name)
        return dev.pvals[i] if 0 <= i < len(dev.pvals) else default

    def _pt(self, dev, name):
        return dev.pstr.get(dev.pidx(name), "")

    def _visual_curve(self, d, dev, col, box, eq):
        """EQ Eight (all 8 bands, dots you can drag: sideways = frequency, up/down = gain) or Auto Filter."""
        x0, y0, x1, y1 = box
        px0, px1, py0, py1 = x0 + 14, x1 - 14, y0 + 12, y1 - 12
        mid, half = (py0 + py1) / 2, (py1 - py0) / 2
        for f, label in ((100, "100"), (1000, "1k"), (10000, "10k")):
            x = px0 + (px1 - px0) * dv.x_of(f)
            d.line([x, py0, x, py1], fill=LINE)
            d.text((x + 4, py1 - 2), label, font=font(12), fill=FAINT, anchor="ls")
        d.line([px0, mid, px1, mid], fill=LINE)
        bands, dots = [], []
        if eq:
            for n in range(1, 9):
                on = self._pv(dev, "%d Filter On A" % n) > 0.5
                f = dv.eq8_freq(self._pv(dev, "%d Frequency A" % n))
                q = dv.eq8_q(self._pv(dev, "%d Q A" % n, 0.5))
                g = self._pv(dev, "%d Gain A" % n)
                kind, stages = dv.eq8_kind(self._pt(dev, "%d Filter Type A" % n))
                if on:
                    bands.append((kind, stages, f, q, g if kind in ("bell", "lowshelf", "highshelf") else 0.0))
                dots.append((n, on, f, g if kind in ("bell", "lowshelf", "highshelf") else 0.0))
            span = 15.0
        else:
            f = dv.number(self._pt(dev, "Frequency"), 1000.0)
            q = 0.7 + 12 * self._pv(dev, "Resonance")
            kind, stages = dv.autofilter_kind(self._pt(dev, "Filter Type"), self._pt(dev, "Filter Slope"))
            bands.append((kind, stages, f, q, 0.0))
            dots.append((0, True, f, 0.0))
            span = 30.0
        key = (tuple(bands), px0, px1, span)
        if getattr(self, "_curve_key", None) != key:            # the maths is only redone when something moved
            xs = list(range(int(px0), int(px1) + 1, 3))
            freqs = [dv.freq_of((x - px0) / (px1 - px0)) for x in xs]
            dbs = dv.response(bands, freqs)
            self._curve_key = key
            self._curve_pts = [(x, mid - max(-1.0, min(1.0, db / span)) * half) for x, db in zip(xs, dbs)]
        pts = self._curve_pts
        d.polygon([(px0, mid)] + pts + [(px1, mid)], fill=scale(col, 0.18))
        d.line(pts, fill=col, width=3)
        self.curve_geom = (px0, px1, mid, half, span)
        for n, on, f, g in dots:
            x = px0 + (px1 - px0) * dv.x_of(f)
            y = mid - max(-1.0, min(1.0, g / span)) * half
            r = 12
            d.ellipse([x - r, y - r, x + r, y + r], fill=col if on else PANEL, outline=col if on else FAINT, width=2)
            if n:
                d.text((x, y), str(n), font=font(13, "Bold SemiCondensed"), fill=ink(col) if on else DIM,
                       anchor="mm")
                self.target(x - 24, y - 24, x + 24, y + 24, ("drag", "eqband", n))
        if not eq:
            d.text((x1 - 14, y0 + 16), "%s  ·  %s" % (self._pt(dev, "Filter Type"), self._pt(dev, "Frequency")),
                   font=font(16), fill=DIM, anchor="rm")

    def _labels(self, d, box, rows, x=None):
        """Name / value pairs across the top right of a picture."""
        x0, y0, x1, y1 = box
        lx = x1 - 14 if x is None else x
        for label, val in reversed(rows):
            txt = "%s %s" % (label, val)
            d.text((lx, y0 + 16), txt, font=font(15), fill=TEXT, anchor="rm")
            lx -= d.textlength(txt, font=font(15)) + 18

    def _visual_reverb(self, d, dev, col, box, hybrid):
        """The reverb's tail over time: the dry hit, the predelay gap, then the decay (to -60 dB at Decay)."""
        x0, y0, x1, y1 = box
        decay = max(0.05, dv.seconds(self._pt(dev, "Decay" if hybrid else "Decay Time")))
        pre = dv.seconds(self._pt(dev, "Predelay"))
        total = pre + decay * 1.2
        px0, px1, py0, py1 = x0 + 14, x1 - 14, y0 + 36, y1 - 14
        fx = lambda t: px0 + (px1 - px0) * t / total       # noqa: E731
        d.line([px0, py1, px1, py1], fill=LINE)
        d.line([px0, py0, px0, py1], fill=TEXT, width=3)    # the dry sound
        pts = [(fx(pre + decay * 1.2 * k / 120), py1 - (py1 - py0) * 0.8 * math.exp(-6.9 * 1.2 * k / 120))
               for k in range(121)]
        d.polygon([(fx(pre), py1)] + pts + [(px1, py1)], fill=scale(col, 0.25))
        d.line(pts, fill=col, width=3)
        rows = [("Decay", self._pt(dev, "Decay" if hybrid else "Decay Time")), ("Predelay", self._pt(dev, "Predelay")),
                ("Size", self._pt(dev, "Size" if hybrid else "Room Size")), ("Dry/Wet", self._pt(dev, "Dry/Wet"))]
        if hybrid:
            rows.insert(0, ("", self._pt(dev, "Algo Type")))
        self._labels(d, box, rows)

    def _visual_delay(self, d, dev, col, box):
        """Delay / Echo as its echoes: left channel above the line, right below, each one quieter by Feedback."""
        x0, y0, x1, y1 = box
        texts = {n: dev.pstr.get(i, "") for i, n in enumerate(dev.pnames)}
        tl = max(0.01, dv.delay_seconds(texts, "L", self.tempo))
        tr = tl if texts.get("Link", "").lower() == "on" else max(0.01, dv.delay_seconds(texts, "R", self.tempo))
        fb = dv.level(texts.get("Feedback"), 0.5)
        ping = texts.get("Ping Pong", "").lower() == "on"
        total = max(tl, tr) * 8.5
        px0, px1, py0, py1 = x0 + 14, x1 - 14, y0 + 34, y1 - 10
        mid, half = (py0 + py1) / 2, (py1 - py0) / 2 - 2
        fx = lambda t: px0 + (px1 - px0) * t / total       # noqa: E731
        d.line([px0, mid, px1, mid], fill=LINE)
        d.line([px0, mid - half, px0, mid + half], fill=TEXT, width=3)
        for side, t, sign in (("L", tl, -1), ("R", tr, 1)):
            for n in range(1, 9):
                h = fb ** (n - 1)
                if h < 0.03:
                    break
                when = t * n
                if ping:                                    # ping pong: the echoes take turns left and right
                    if (n % 2 == 1) != (side == "L"):
                        continue
                x = fx(when)
                if x > px1:
                    break
                d.line([x, mid, x, mid + sign * half * h], fill=col, width=4)
        sync = lambda s: (texts.get(s + " Synced") or texts.get(s + " 16th", "")) \
            if texts.get(s + " Sync", "").lower() == "on" else texts.get(s + " Time", "")   # noqa: E731
        linked = texts.get("Link", "").lower() == "on"
        self._labels(d, box, [("L", "%s  %.0f ms" % (sync("L"), tl * 1000)),
                              ("R", "linked" if linked else "%s  %.0f ms" % (sync("R"), tr * 1000)),
                              ("Feedback", texts.get("Feedback", "")),
                              ("Dry/Wet", texts.get("Dry/Wet") or texts.get("Dry Wet", ""))])

    def _visual_saturator(self, d, dev, col, box):
        """Saturator's curve type at the current drive (a sketch of the shape, input across, output up)."""
        x0, y0, x1, y1 = box
        size = y1 - y0 - 24
        gx0, gy0 = x0 + 16, y0 + 12
        kind = self._pt(dev, "Type")
        g = 10 ** (dv.number(self._pt(dev, "Drive"), 0.0) / 20)
        d.rectangle([gx0, gy0, gx0 + size, gy0 + size], outline=LINE)
        cx, cy, hs = gx0 + size / 2, gy0 + size / 2, size / 2
        d.line([gx0, cy, gx0 + size, cy], fill=LINE)
        d.line([cx, gy0, cx, gy0 + size], fill=LINE)
        pts = [(cx + hs * x, cy - hs * max(-1.0, min(1.0, dv.saturate(kind, x * g))))
               for x in [k / 60 - 1 for k in range(121)]]
        d.line(pts, fill=col, width=3)
        rows = [("Type", kind), ("Drive", self._pt(dev, "Drive")), ("Output", self._pt(dev, "Output")),
                ("Dry/Wet", self._pt(dev, "Dry/Wet"))]
        for k, (label, val) in enumerate(rows):
            x = gx0 + size + 40 + k * 220
            d.text((x, y0 + 50), label, font=font(17), fill=DIM, anchor="lm")
            d.text((x, y0 + 92), val, font=font(28, "Bold SemiCondensed"), fill=TEXT, anchor="lm")

    def _visual_utility(self, d, dev, col, box):
        """Utility's stereo picture: the image's width around the balance point, plus gain and phase flips."""
        x0, y0, x1, y1 = box
        px0, px1 = x0 + 60, x1 - 260
        ym = (y0 + y1) // 2 + 6
        d.line([px0, ym, px1, ym], fill=LINE, width=2)
        d.text((px0 - 20, ym), "L", font=font(18), fill=DIM, anchor="mm")
        d.text((px1 + 20, ym), "R", font=font(18), fill=DIM, anchor="mm")
        cx = (px0 + px1) / 2
        bal = self._pv(dev, "Balance")
        width = 0.0 if self._pt(dev, "Mono").lower() == "on" else dv.number(self._pt(dev, "Stereo Width"), 100) / 100
        c = cx + bal * (px1 - px0) / 2
        w = min(1.0, width / 2) * (px1 - px0) / 2
        d.rounded_rectangle([max(px0, c - w - 2), ym - 26, min(px1, c + w + 2), ym + 26], 26, fill=scale(col, 0.3),
                            outline=col, width=2)
        d.ellipse([c - 7, ym - 7, c + 7, ym + 7], fill=TEXT)
        flips = " ".join(n for n, p in (("Ø L", "Left Inv"), ("Ø R", "Right Inv")) if self._pt(dev, p).lower() == "on")
        rows = [("Width", self._pt(dev, "Stereo Width") if width else "Mono"), ("Balance", self._pt(dev, "Balance")),
                ("Gain", self._pt(dev, "Output"))]
        for k, (label, val) in enumerate(rows):
            d.text((x1 - 230, y0 + 34 + k * 36), label, font=font(16), fill=DIM, anchor="lm")
            d.text((x1 - 20, y0 + 34 + k * 36), val, font=font(22, "Bold SemiCondensed"), fill=TEXT, anchor="rm")
        if flips:
            d.text((px0, y0 + 18), "Phase flipped: " + flips, font=font(15), fill=MUTE, anchor="lm")
        if self._pt(dev, "Bass Mono").lower() == "on":
            d.text((px1, y0 + 18), "Bass mono below " + self._pt(dev, "Bass Freq"), font=font(15), fill=TEXT,
                   anchor="rm")

    def _visual_comp(self, d, dev, col, box, glue):
        """A compressor's curve: input level across, output level up, -60..0 dB, with the threshold marked."""
        x0, y0, x1, y1 = box
        size = y1 - y0 - 24
        gx0, gy0 = x0 + 16, y0 + 12
        gx1, gy1 = gx0 + size, gy0 + size
        if glue:
            thr, ratio, knee = self._pv(dev, "Threshold"), dv.number(self._pt(dev, "Ratio"), 4.0), 6.0
        else:
            thr = dv.number(self._pt(dev, "Threshold"), 0.0)
            ratio = dv.number(self._pt(dev, "Ratio"), 4.0)
            knee = self._pv(dev, "Knee", 6.0)
        ratio = max(1.0, ratio)
        d.rectangle([gx0, gy0, gx1, gy1], outline=LINE)
        fx = lambda db: gx0 + size * (db + 60) / 60          # noqa: E731
        fy = lambda db: gy1 - size * (max(-60.0, min(0.0, db)) + 60) / 60   # noqa: E731
        d.line([fx(-60), fy(-60), fx(0), fy(0)], fill=LINE)
        tx = fx(max(-60.0, min(0.0, thr)))
        d.line([tx, gy0, tx, gy1], fill=FAINT)
        pts = [(fx(x), fy(dv.comp_curve(thr, ratio, knee, x))) for x in [i * 0.5 - 60 for i in range(121)]]
        d.line(pts, fill=col, width=3)
        tx0 = gx1 + 40
        rows = [("Threshold", self._pt(dev, "Threshold")), ("Ratio", self._pt(dev, "Ratio")),
                ("Attack", self._pt(dev, "Attack")), ("Release", self._pt(dev, "Release"))]
        rows.append(("Range", self._pt(dev, "Range")) if glue else ("Knee", self._pt(dev, "Knee")))
        for k, (label, val) in enumerate(rows):
            x = tx0 + k * 190
            d.text((x, y0 + 50), label, font=font(17), fill=DIM, anchor="lm")
            d.text((x, y0 + 92), val, font=font(30, "Bold SemiCondensed"), fill=TEXT, anchor="lm")

    # Synths: the picture follows the knob page (envelope, filter, LFO / oscillator wave); Main shows filter +
    # amp envelope side by side, Operator's Main its four operators.
    SYNTHS = ("InstrumentVector", "Drift", "Operator", "UltraAnalog", "MultiSampler", "InstrumentMeld")

    def _page_pic(self, dev, k):
        """device_views.page_picture for layout page k."""
        if not dev.layout or not 0 <= k < len(dev.layout):
            return None
        name, ps = dev.layout[k]
        return dv.page_picture(name, [dev.pnames[i] for i in ps], [dev.pstr.get(i, "") for i in ps],
                               [dev.norm(i) for i in ps])

    def _synth_picture(self, d, dev, col, box):
        x0, y0, x1, y1 = box
        if not dev.layout:
            return
        page = dev.layout[min(dev.bank, len(dev.layout) - 1)][0]
        if page == "Main" and dev.selected_info()[0] == "Operator":
            self._operators(d, dev, col, box)
            return
        if page == "Main":                                  # filter next to the amp envelope
            names = [n for n, _ in dev.layout]
            fk = next((k for k, n in enumerate(names) if ("Filter" in n or "Flt" in n) and "Env" not in n), None)
            ek = next((k for k, n in enumerate(names) if "Amp Env" in n), None)
            if ek is None:
                ek = next((k for k, n in enumerate(names) if "Env" in n and "Filter" not in n
                           and "Pitch" not in n), None)
            mid = (x0 + x1) // 2
            if fk is not None:
                self._draw_pic(d, self._page_pic(dev, fk), dev.layout[fk][0], col, (x0, y0, mid - 6, y1))
            _n, ps = dev.layout[dev.bank]                   # an amp envelope on Main itself (Drift) comes first
            env = dv.page_picture("Amp Env", [dev.pnames[i] for i in ps], [dev.pstr.get(i, "") for i in ps],
                                  [dev.norm(i) for i in ps])
            if env is not None:
                self._draw_pic(d, env, "Amp Env", col, (mid + 6, y0, x1, y1))
            elif ek is not None:
                self._draw_pic(d, self._page_pic(dev, ek), dev.layout[ek][0], col, (mid + 6, y0, x1, y1))
            return
        pic = self._page_pic(dev, dev.bank)
        if pic is None:
            d.text(((x0 + x1) // 2, (y0 + y1) // 2), "%s  ·  %s" % (dev.names[dev.idx] if dev.names else "", page),
                   font=font(24), fill=FAINT, anchor="mm")
            return
        self._draw_pic(d, pic, page, col, box)

    def _draw_pic(self, d, pic, title, col, box):
        x0, y0, x1, y1 = box
        if pic is None:
            return
        d.text((x0 + 14, y0 + 16), title, font=font(16), fill=DIM, anchor="lm")
        px0, px1, py0, py1 = x0 + 14, x1 - 14, y0 + 34, y1 - 12
        if pic["kind"] == "env":
            pts = [(px0 + (px1 - px0) * x, py1 - (py1 - py0) * y) for x, y in pic["points"]]
            d.polygon([(px0, py1)] + pts + [(px1, py1)], fill=scale(col, 0.18))
            d.line(pts, fill=col, width=3)
            for x, y in pts[1:-1]:
                d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=TEXT)
            lx = x1 - 14
            for label, val in reversed(pic["labels"]):
                txt = "%s %s" % (label, val)
                d.text((lx, y0 + 16), txt, font=font(15), fill=TEXT, anchor="rm")
                lx -= d.textlength(txt, font=font(15)) + 18
        elif pic["kind"] == "filter":
            mid, half = (py0 + py1) / 2, (py1 - py0) / 2
            xs = list(range(int(px0), int(px1) + 1, 4))
            dbs = dv.response(pic["bands"], [dv.freq_of((x - px0) / max(1, px1 - px0)) for x in xs])
            pts = [(x, mid - max(-1.0, min(1.0, db / 30)) * half) for x, db in zip(xs, dbs)]
            d.line([px0, mid, px1, mid], fill=LINE)
            d.polygon([(px0, py1)] + pts + [(px1, py1)], fill=scale(col, 0.18))
            d.line(pts, fill=col, width=3)
            d.text((x1 - 14, y0 + 16), pic["label"], font=font(15), fill=TEXT, anchor="rm")
        elif pic["kind"] == "wave":
            mid, half = (py0 + py1) / 2, (py1 - py0) / 2 - 2
            n = max(2, int(px1 - px0) // 3)
            pts = [(px0 + (px1 - px0) * k / n, mid - half * dv.wave(pic["shape"], 2.0 * k / n)) for k in range(n + 1)]
            d.line([px0, mid, px1, mid], fill=LINE)
            d.line(pts, fill=col, width=3)
            d.text((x1 - 14, y0 + 16), pic["title"], font=font(15), fill=TEXT, anchor="rm")
        elif pic["kind"] == "position":
            ym = (py0 + py1) // 2
            d.rounded_rectangle([px0, ym - 10, px1, ym + 10], 10, fill=CELL)
            d.rounded_rectangle([px0, ym - 10, px0 + (px1 - px0) * pic["value"], ym + 10], 10, fill=col)
            d.text((x1 - 14, y0 + 16), pic["title"], font=font(15), fill=TEXT, anchor="rm")

    def _operators(self, d, dev, col, box):
        """Operator's Main page: its four oscillators (on, wave, level, frequency ratio) and the algorithm."""
        x0, y0, x1, y1 = box
        cw = (x1 - x0 - 200) // 4
        for k, op in enumerate("ABCD"):
            cx0 = x0 + 14 + k * cw
            on = self._pv(dev, "Osc-%s On" % op) > 0.5
            lvl = dev.norm(dev.pidx("Osc-%s Level" % op)) if dev.pidx("Osc-%s Level" % op) >= 0 else 0.0
            shape = self._pt(dev, "Osc-%s Wave" % op)
            c = col if on else FAINT
            d.text((cx0, y0 + 18), op, font=font(22, "Bold SemiCondensed"), fill=c, anchor="lm")
            d.text((cx0 + 28, y0 + 18), "%s  x%s" % (shape, self._pt(dev, "%s Coarse" % op)), font=font(15),
                   fill=TEXT if on else FAINT, anchor="lm")
            wy0, wy1 = y0 + 40, y1 - 36
            mid, half = (wy0 + wy1) / 2, (wy1 - wy0) / 2 - 2
            if dv.wave(shape, 0.25) is not None:
                n = 60
                d.line([(cx0 + (cw - 30) * i / n, mid - half * dv.wave(shape, i / n)) for i in range(n + 1)],
                       fill=c, width=2)
            d.rounded_rectangle([cx0, y1 - 24, cx0 + cw - 30, y1 - 14], 5, fill=CELL)
            d.rounded_rectangle([cx0, y1 - 24, cx0 + (cw - 30) * lvl, y1 - 14], 5, fill=c)
        d.text((x1 - 20, (y0 + y1) // 2 - 14), self._pt(dev, "Algorithm"), font=font(30, "Bold SemiCondensed"),
               fill=TEXT, anchor="rm")
        d.text((x1 - 20, (y0 + y1) // 2 + 18), "algorithm", font=font(15), fill=DIM, anchor="rm")

    def _simpler_wave(self, d, dev, col):
        """Simpler's sample: drag the S and E markers; in Slice mode the slices are numbered, a tap adds one and
        dragging one moves it. The orange line is where Simpler is playing."""
        x0, x1, y0, y1 = 16, RAIL_X - 16, self.KNOBS_Y, self.KNOBS_Y + self.WAVE_H
        self.wave_geom = None
        d.rounded_rectangle([x0, y0, x1, y1], 8, fill=PANEL)
        s = dev.sample
        if not s:
            d.text(((x0 + x1) // 2, (y0 + y1) // 2), "Reading sample…", font=font(22), fill=FAINT, anchor="mm")
            return
        if not s.get("file"):
            d.text(((x0 + x1) // 2, (y0 + y1) // 2), "No sample: load one from BROWSE", font=font(22), fill=FAINT,
                   anchor="mm")
            return
        name = s["file"].replace("\\", "/").split("/")[-1]
        d.text((x0 + 12, y0 + 16), fit(d, name, font(18), 560), font=font(18), fill=TEXT, anchor="lm")
        slicing = s.get("mode") == 2
        if slicing:                                           # slice controls, top right of the panel
            style = s.get("style", 0)
            bx = x1 - 12
            for label, action in (("Reset", ("simpler", "slice_reset")), ("Clear", ("simpler", "slice_clear")),
                                  ("By: " + self.SLICE_STYLES[style % 4], ("simpler", "slice_style"))):
                w = d.textlength(label, font=font(15)) + 22
                d.rounded_rectangle([bx - w, y0 + 4, bx, y0 + 28], 12, outline=LINE, width=1)
                d.text((bx - w / 2, y0 + 16), label, font=font(15), fill=DIM, anchor="mm")
                self.target(bx - w, y0, bx, y0 + 32, action)
                bx -= w + 8
        wy0, wy1 = y0 + 34, y1 - 8
        mid, half = (wy0 + wy1) // 2, (wy1 - wy0) // 2
        cols = x1 - x0 - 16
        p = self.wave_peaks(s["file"], cols)
        if p is None or isinstance(p, str):
            d.text(((x0 + x1) // 2, mid), "Reading waveform…" if p is None else "Can't draw this file",
                   font=font(18), fill=FAINT, anchor="mm")
            return
        length = max(1, s.get("length") or 1)
        self.wave_geom = (x0 + 8, cols, length, wy0, wy1)
        fx = lambda frame: x0 + 8 + cols * frame / length      # noqa: E731
        a, b = fx(s.get("start") or 0) - x0 - 8, fx(s.get("end") or length) - x0 - 8
        for c, (lo, hi) in enumerate(p):
            x = x0 + 8 + c
            inside = a <= c <= b
            d.line([x, mid - hi * half, x, mid - lo * half], fill=col if inside else (60, 60, 60))
        d.line([x0 + 8, mid, x1 - 8, mid], fill=(50, 50, 50))
        if slicing:                                           # tap the waveform to add a slice there
            self.target(x0, wy0, x1, wy1, ("drag", "slice", -1))
            for k, fr in enumerate(s.get("slices", [])):
                x = fx(fr)
                d.line([x, wy0, x, wy1], fill=TEXT, width=1)
                d.rectangle([x, wy1 - 16, x + 18, wy1], fill=TEXT)
                d.text((x + 9, wy1 - 8), str(k + 1), font=font(11, "Bold SemiCondensed"), fill=(0, 0, 0),
                       anchor="mm")
                if fr > 0:                                    # the first slice always sits at the start
                    self.target(x - 14, wy0, x + 14, wy1, ("drag", "slice", fr))
        if dev.playhead is not None:                          # where Simpler is playing, like Live's orange line
            x = x0 + 8 + a + dev.playhead * (b - a)
            d.line([x, wy0, x, wy1], fill=ACCENT, width=2)
        for xm, label, which in ((a, "S", "start"), (b, "E", "end")):     # start / end markers: drag them
            x = x0 + 8 + xm
            d.line([x, wy0, x, wy1], fill=TEXT, width=2)
            d.rectangle([x - 9 if label == "E" else x, wy0, x if label == "E" else x + 9, wy0 + 14], fill=TEXT)
            d.text((x - 4 if label == "E" else x + 4, wy0 + 7), label, font=font(11, "Bold SemiCondensed"),
                   fill=(0, 0, 0), anchor="mm")
            self.target(x - 22, wy0 - 4, x + 22, wy0 + 40, ("drag", "smark", which))

    def _device_knobs(self, d, dev, ui, col, ky0=None, focus=True):
        ky0 = self.KNOBS_Y if ky0 is None else ky0
        d.text((RAIL_X - 16, ky0 - 2), "Bank %d/%d" % (dev.bank + 1, dev.num_banks()), font=font(16), fill=DIM,
               anchor="rm")
        shown = dev.bank_params()
        qparams = ui.qlink_params(dev)
        for c in range(COLS):
            if c >= len(shown):
                continue
            p = shown[c]
            x0, cx, ky = c * COL_W, c * COL_W + COL_W // 2, ky0 + 120
            q = qparams.index(p) if p in qparams else None
            if q is not None:
                d.rounded_rectangle([x0 + 3, ky0 + 10, x0 + COL_W - 4, ky0 + 262], 8,
                                    outline=TEXT if q in ui.qlink_touch else LINE, width=2)
                d.text((cx, ky0 + 246), "Q%d" % (q + 1), font=font(15), fill=ACCENT, anchor="mm")
            pname = dev.pnames[p] if p < len(dev.pnames) else ""
            d.text((cx, ky0 + 32), fit(d, pname, font(19), COL_W - 16), font=font(19), fill=DIM, anchor="mm")
            r = 48
            box = [cx - r, ky - r, cx + r, ky + r]
            d.arc(box, 135, 405, fill=CELL, width=10)
            n = dev.norm(p)
            if n > 0.004:
                d.arc(box, 135, 135 + 270 * n, fill=col, width=10)
            a = math.radians(135 + 270 * n)
            d.line([cx + math.cos(a) * (r - 22), ky + math.sin(a) * (r - 22), cx + math.cos(a) * (r - 4),
                    ky + math.sin(a) * (r - 4)], fill=TEXT, width=4)
            val = dev.pstr.get(p, "")
            d.text((cx, ky0 + 212), fit(d, val, font(22), COL_W - 12), font=font(22), fill=TEXT, anchor="mm")
            self.target(x0, ky0 + 10, x0 + COL_W, ky0 + 262, ("drag", "param", p))
        p = ui.focus_param                              # the parameter under a finger, big
        y = ky0 + 290
        if not focus:
            pass
        elif p is not None and time.time() - ui.focus_at < 3 and p < len(dev.pnames):
            d.text((40, y + 24), dev.pnames[p], font=font(34, "SemiLight SemiCondensed"), fill=DIM, anchor="lm")
            d.text((40, y + 90), dev.pstr.get(p, ""), font=font(64, "Bold SemiCondensed"), fill=TEXT, anchor="lm")
            d.rounded_rectangle([600, y + 78, RAIL_X - 40, y + 100], 11, fill=CELL)
            d.rounded_rectangle([600, y + 78, 600 + (RAIL_X - 640) * dev.norm(p), y + 100], 11, fill=col)
        else:
            d.text((W // 2, y + 60), "Turn or touch a Q-Link, or drag a knob   ·   Q-LINK button: knobs %s" % (
                "5-8" if ui.qlink_half == 0 else "1-4"), font=font(22, "SemiLight SemiCondensed"), fill=FAINT,
                anchor="mm")
        for yy, glyph, step in ((MAIN_Y, "‹", -1), (MAIN_Y + MAIN_H // 2, "›", 1)):    # parameter banks
            ok = 0 <= dev.bank + step < dev.num_banks()
            d.rounded_rectangle([RAIL_X + 4, yy + 4, W - 4, yy + MAIN_H // 2 - 5], 6, fill=PANEL)
            d.text((RAIL_X + 40, yy + MAIN_H // 4), glyph, font=font(40), fill=TEXT if ok else FAINT, anchor="mm")
            self.target(RAIL_X, yy, W, yy + MAIN_H // 2, ("param_bank", step))

    GRID_ROW_H = 230

    def _knob(self, d, cx, cy, r, n, col, key=None):
        """One knob: arc from 7 to 5 o'clock filled up to n (0..1), with a pointer."""
        box = [cx - r, cy - r, cx + r, cy + r]
        d.arc(box, 135, 405, fill=CELL, width=8)
        if n > 0.004:
            d.arc(box, 135, 135 + 270 * n, fill=col, width=8)
        a = math.radians(135 + 270 * n)
        d.line([cx + math.cos(a) * (r - 18), cy + math.sin(a) * (r - 18), cx + math.cos(a) * (r - 3),
                cy + math.sin(a) * (r - 3)], fill=TEXT, width=4)

    def _plugin_knobs(self, d, dev, ui, col):
        """MPC-style plug-in screen: 16 parameters a page as big touch knobs (drag up/down, SHIFT = fine).
        The 4 knobs the Q-Links turn are outlined; the Q-LINK button moves them along."""
        ky0 = self.KNOBS_Y - 6
        shown = dev.bank_params()
        n_params = max(0, len(dev.pnames) - 1)
        if not dev.pnames:
            d.text((W // 2, ky0 + 200), "Reading parameters…", font=font(28), fill=FAINT, anchor="mm")
        elif not n_params:
            # Live lists a plug-in's parameters by itself only when it has few of them (big synths such as
            # Phase Plant or Acid V show none until they're configured; checked 2026-10-29).
            for k, (line, size, fill) in enumerate((
                    ("This plug-in has too many parameters for Live to list by itself", 28, DIM),
                    ("Once, on the PC: click Configure on the device in Live, then move the controls you want.", 21, FAINT),
                    ("They show up here as knobs. Save the device as its default preset to keep them in new sets.",
                     21, FAINT),
                    ("Meanwhile, tap KNOBS to play the plug-in's own window by touch.", 21, FAINT))):
                d.text((W // 2, ky0 + 150 + k * 42 + (12 if k else 0)), line,
                       font=font(size) if not k else font(size, "SemiLight SemiCondensed"), fill=fill, anchor="mm")
        d.text((RAIL_X - 16, ky0 - 4), "Page %d/%d  ·  %d parameters" % (dev.bank + 1, dev.num_banks(), n_params),
               font=font(16), fill=DIM, anchor="rm")
        qparams = ui.qlink_params(dev)
        focus = ui.focus_param if ui.focus_param is not None and time.time() - ui.focus_at < 3 else None
        for k, p in enumerate(shown):
            r, c = divmod(k, COLS)
            x0, y0 = c * COL_W, ky0 + 8 + r * self.GRID_ROW_H
            cx, cy = x0 + COL_W // 2, y0 + 104
            q = qparams.index(p) if p in qparams else None
            if p == focus:
                d.rounded_rectangle([x0 + 4, y0, x0 + COL_W - 5, y0 + self.GRID_ROW_H - 8], 8, fill=PANEL)
            if q is not None:
                d.rounded_rectangle([x0 + 4, y0, x0 + COL_W - 5, y0 + self.GRID_ROW_H - 8], 8,
                                    outline=TEXT if q in ui.qlink_touch else LINE, width=2)
                d.text((x0 + COL_W - 14, y0 + self.GRID_ROW_H - 24), "Q%d" % (q + 1), font=font(14), fill=ACCENT,
                       anchor="rm")
            name = dev.pnames[p] if p < len(dev.pnames) else ""
            d.text((cx, y0 + 26), fit(d, name, font(18), COL_W - 16), font=font(18), fill=DIM, anchor="mm")
            self._knob(d, cx, cy, 44, dev.norm(p), col, key=("grid", p))
            d.text((cx, y0 + 180), fit(d, dev.pstr.get(p, ""), font(21), COL_W - 14), font=font(21), fill=TEXT,
                   anchor="mm")
            self.target(x0, y0, x0 + COL_W, y0 + self.GRID_ROW_H - 8, ("drag", "param", p))
        for yy, glyph, step in ((MAIN_Y, "‹", -1), (MAIN_Y + MAIN_H // 2, "›", 1)):    # pages
            ok = 0 <= dev.bank + step < dev.num_banks()
            d.rounded_rectangle([RAIL_X + 4, yy + 4, W - 4, yy + MAIN_H // 2 - 5], 6, fill=PANEL)
            d.text((RAIL_X + 40, yy + MAIN_H // 4 - 10), glyph, font=font(40), fill=TEXT if ok else FAINT,
                   anchor="mm")
            d.text((RAIL_X + 40, yy + MAIN_H // 4 + 26), "page", font=font(13), fill=DIM, anchor="mm")
            self.target(RAIL_X, yy, W, yy + MAIN_H // 2, ("param_bank", step))

    def _rack_view(self, d, dev, ui, col):
        """An opened rack: its chains, or for a Drum Rack a 4x4 page of pads (bottom-left = lowest note)."""
        y0, y1 = self.KNOBS_Y, LOW_Y - 8
        if not dev.chains and not dev.drum:
            d.text((W // 2, (y0 + y1) // 2), "No chains in this rack", font=font(30), fill=FAINT, anchor="mm")
            return
        if dev.drum:
            by_note = {n: (ci, name) for ci, (name, n) in enumerate(dev.chains)}
            tw, th = RAIL_X // 4, (y1 - y0) // 4
            for k in range(16):
                note = ui.pad_page + k
                if note > 127:
                    continue
                row, c = divmod(k, 4)
                x0, yy = c * tw, y1 - (row + 1) * th
                ci, name = by_note.get(note, (-1, ""))
                hit = note == ui.drum_note
                box = [x0 + 4, yy + 4, x0 + tw - 5, yy + th - 5]
                d.rounded_rectangle(box, 8, fill=scale(col, 0.45) if ci >= 0 else PANEL,
                                    outline=TEXT if hit else None, width=3)
                d.text((x0 + 14, yy + 20), note_name(note), font=font(16), fill=TEXT if ci >= 0 else FAINT,
                       anchor="lm")
                d.text((x0 + tw // 2, yy + th // 2 + 6), fit(d, name or "empty", font(22), tw - 24), font=font(22),
                       fill=TEXT if ci >= 0 else FAINT, anchor="mm")
                self.target(x0, yy, x0 + tw, yy + th, ("dev_pad", note, ci))
            for yy, glyph, step in ((MAIN_Y, "‹", -16), (MAIN_Y + MAIN_H // 2, "›", 16)):   # pad pages
                ok = 0 <= ui.pad_page + step <= 112
                d.rounded_rectangle([RAIL_X + 4, yy + 4, W - 4, yy + MAIN_H // 2 - 5], 6, fill=PANEL)
                d.text((RAIL_X + 40, yy + MAIN_H // 4 - 8), glyph, font=font(40), fill=TEXT if ok else FAINT,
                       anchor="mm")
                d.text((RAIL_X + 40, yy + MAIN_H // 4 + 26), "pads", font=font(13), fill=FAINT, anchor="mm")
                self.target(RAIL_X, yy, W, yy + MAIN_H // 2, ("pad_page", step))
            return
        tw, th = RAIL_X // 4, 90
        for ci, (name, _n) in enumerate(dev.chains[:20]):
            row, c = divmod(ci, 4)
            x0, yy = c * tw, y0 + row * (th + 8)
            d.rounded_rectangle([x0 + 4, yy, x0 + tw - 5, yy + th], 8, fill=PANEL)
            d.rectangle([x0 + 4, yy, x0 + 9, yy + th], fill=col)
            d.text((x0 + 22, yy + th // 2), fit(d, name or "Chain %d" % (ci + 1), font(22), tw - 40), font=font(22),
                   fill=TEXT, anchor="lm")
            self.target(x0, yy, x0 + tw, yy + th, ("dev_chain", ci))

    PLUGIN_COLS, PLUGIN_ROWS = 5, 8
    PLUGINS_PER_PAGE = PLUGIN_COLS * PLUGIN_ROWS
    LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def _add_menu(self, d, live, ui, col):
        """Pick a Live device or one of your plug-ins to insert after the selected device."""
        dev = live.dev
        y0, y1 = self.STRIP_Y + self.STRIP_H + 8, LOW_Y - 4
        d.rounded_rectangle([8, y0, RAIL_X - 8, y1], 10, fill=(22, 22, 22), outline=LINE, width=2)
        self.target(8, y0, RAIL_X - 8, y1, ("noop",))
        where = "after " + fit(d, dev.names[dev.idx], font(24), 260) if dev.names else "into this chain"
        d.text((28, y0 + 26), "Add " + where, font=font(24, "Bold SemiCondensed"), fill=TEXT, anchor="lm")
        n_plug = len(live.plugins) if live.plugins is not None else None
        for i, (tab, label) in enumerate((("live", "LIVE DEVICES"), ("plugins", "PLUG-INS" + (
                " (%d)" % n_plug if n_plug is not None else "")))):
            x = 520 + i * 200
            on = ui.add_tab == tab
            d.rounded_rectangle([x, y0 + 10, x + 186, y0 + 44], 17, fill=col if on else None, outline=col, width=2)
            d.text((x + 93, y0 + 27), label, font=font(17, "Bold SemiCondensed"), fill=ink(col) if on else col,
                   anchor="mm")
            self.target(x, y0, x + 186, y0 + 52, ("dev_add_tab", tab))
        d.text((RAIL_X - 30, y0 + 26), "Close", font=font(20), fill=DIM, anchor="rm")
        self.target(RAIL_X - 120, y0, RAIL_X - 8, y0 + 50, ("dev_add",))
        if ui.add_tab == "plugins":
            self._plugin_grid(d, live, ui, y0, y1)
            return
        colw = (RAIL_X - 40) // 3
        flat = 0                                        # running index, to match the wheel's highlight
        for gi, (group, names) in enumerate(self.DEVICE_MENU):
            gx = 24 + gi * colw
            d.text((gx, y0 + 66), group.upper(), font=font(15, "Bold SemiCondensed"), fill=col, anchor="lm")
            per_col = 9
            for i, name in enumerate(names):
                sub, r = divmod(i, per_col)
                x = gx + sub * (colw // 2)
                y = y0 + 92 + r * 42
                hi = flat == ui.menu_sel
                flat += 1
                d.rounded_rectangle([x, y, x + colw // 2 - 10, y + 36], 6, fill=TEXT if hi else PANEL)
                d.text((x + 10, y + 18), fit(d, name, font(18), colw // 2 - 28), font=font(18),
                       fill=(0, 0, 0) if hi else TEXT, anchor="lm")
                self.target(x, y, x + colw // 2 - 10, y + 36, ("dev_add_pick", name))

    @staticmethod
    def plugin_letter(name):
        c = name[:1].upper()
        return c if "A" <= c <= "Z" else "#"

    def _plugin_grid(self, d, live, ui, y0, y1):
        if live.plugins is None:
            d.text((RAIL_X // 2, (y0 + y1) // 2), "Loading your plug-ins…", font=font(28), fill=FAINT, anchor="mm")
            return
        if not live.plugins:
            d.text((RAIL_X // 2, (y0 + y1) // 2), "No plug-ins found in Live's browser", font=font(28), fill=FAINT,
                   anchor="mm")
            return
        per, n = self.PLUGINS_PER_PAGE, len(live.plugins)
        ui.plugin_page = max(0, min(n - 1, ui.plugin_page))     # index of the first plug-in shown
        start = ui.plugin_page
        shown = live.plugins[start:start + per]
        here = {self.plugin_letter(n) for n in shown}
        for i, ch in enumerate(self.LETTERS):           # jump to a letter
            x = 24 + i * 34
            has = any(self.plugin_letter(n) == ch for n in live.plugins)
            d.rounded_rectangle([x, y0 + 56, x + 30, y0 + 88], 6, fill=PANEL if ch not in here else LINE)
            d.text((x + 15, y0 + 72), ch, font=font(17, "Bold SemiCondensed"), fill=TEXT if has else LINE,
                   anchor="mm")
            if has:
                self.target(x, y0 + 52, x + 32, y0 + 92, ("plugin_letter", ch))
        if n > per:                                     # page arrows (a page = 40 plug-ins)
            for i, (glyph, step) in enumerate((("‹", -per), ("›", per))):
                x = RAIL_X - 150 + i * 70
                ok = (start > 0) if step < 0 else (start + per < n)
                d.rounded_rectangle([x, y0 + 56, x + 60, y0 + 88], 15, outline=FAINT if ok else LINE, width=2)
                d.text((x + 30, y0 + 72), glyph, font=font(26), fill=TEXT if ok else FAINT, anchor="mm")
                self.target(x, y0 + 50, x + 60, y0 + 94, ("plugin_page", step))
            d.text((RAIL_X - 160, y0 + 72), "%d-%d of %d" % (start + 1, min(n, start + per), n), font=font(16),
                   fill=DIM, anchor="rm")
        colw = (RAIL_X - 40) // self.PLUGIN_COLS
        for k, name in enumerate(shown):
            c, r = divmod(k, self.PLUGIN_ROWS)
            x, y = 24 + c * colw, y0 + 100 + r * 42
            hi = start + k == ui.menu_sel
            d.rounded_rectangle([x, y, x + colw - 12, y + 36], 6, fill=TEXT if hi else PANEL)
            d.text((x + 10, y + 18), fit(d, name, font(18), colw - 30), font=font(18),
                   fill=(0, 0, 0) if hi else TEXT, anchor="lm")
            self.target(x, y, x + colw - 12, y + 36, ("plugin_pick", name))

    # ------------------------------------------------------------ browse

    BR_ROW_H, BR_ROWS = 54, 9

    def _page_browse(self, d, live, ui):
        root = live.br_path[0] if live.br_path else None
        if root and (root.startswith(("place:", "userplace:")) or root == "current_project"):
            root = PLACES
        for i, (key, label) in enumerate(BROWSE_ROOTS):
            self._soft(d, i, label, active=key == root, action=("br_root", key))
        y = MAIN_Y + 30
        if live.br_path:
            d.rounded_rectangle([12, y - 20, 112, y + 20], 20, outline=FAINT, width=2)
            d.text((62, y), "‹ Back", font=font(20), fill=TEXT, anchor="mm")
            self.target(0, MAIN_Y, 130, MAIN_Y + 60, ("br_back",))
        path = " › ".join([root_label(live.br_path[0])] + live.br_path[1:]) if live.br_path else "Browser"
        d.text((130, y), fit(d, path, font(24), 1000), font=font(24), fill=DIM, anchor="lm")
        tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
        if tr:
            where = "Loads onto  " + fit(d, tr.name, font(20), 200)
            if live.br_path and live.br_path[0].startswith("userplace:") and tr.midi_in:
                where += "  (Drum Rack: pad %s)" % note_name(ui.drum_note)
            d.text((RAIL_X - 16, y), where, font=font(20), fill=rgb(tr.color), anchor="rm")
        items = live.br_items
        if live.br_pending:
            d.text((W // 2, MAIN_Y + 260), "Loading…", font=font(30), fill=FAINT, anchor="mm")
        elif not items:
            d.text((W // 2, MAIN_Y + 260), "Empty", font=font(30), fill=FAINT, anchor="mm")
        per_col = self.BR_ROWS
        top = ui.br_scroll
        for k in range(per_col * 2):
            i = top + k
            if i >= len(items):
                break
            raw = items[i]
            folder = live.br_root or raw.startswith("[F] ")
            name = root_label(raw) if live.br_root else raw.replace("[F] ", "", 1)
            col, row = divmod(k, per_col)
            x0, y0 = col * 600 + 8, MAIN_Y + 66 + row * self.BR_ROW_H
            sel = i == ui.br_sel
            box = [x0, y0 + 2, x0 + 584, y0 + self.BR_ROW_H - 4]
            if sel:
                d.rounded_rectangle(box, 6, fill=TEXT)
            note = live.br_hints.get(i, "")        # search results: where the item lives
            if raw.startswith("userplace:") and not live.places_info.get(name, {}).get("exists"):
                note = "drive not connected"
            searching = live.br_path[:1] == ["@search"]
            d.text((x0 + 16, y0 + self.BR_ROW_H // 2), fit(d, name, font(24), (290 if searching else 380) if note
                                                         else 520), font=font(24),
                   fill=(0, 0, 0) if sel else (DIM if note == "drive not connected" else TEXT), anchor="lm")
            if note:
                warn = note == "drive not connected"
                d.text((x0 + 544, y0 + self.BR_ROW_H // 2), fit_start(d, note, font(16), 220 if not warn else 200),
                       font=font(16), fill=(0, 0, 0) if sel else (MUTE if warn else DIM), anchor="rm")
            if folder:
                d.text((x0 + 568, y0 + self.BR_ROW_H // 2), "›", font=font(30), fill=(0, 0, 0) if sel else DIM,
                       anchor="rm")
            self.target(x0, y0, x0 + 584, y0 + self.BR_ROW_H, ("br_item", i))
        n = len(items)
        if n > per_col * 2:
            h = MAIN_H * per_col * 2 / n
            yy = MAIN_Y + 66 + (MAIN_H - 66) * top / n
            d.rectangle([RAIL_X - 4, yy, RAIL_X, yy + h], fill=DIM)
        d.line([0, LOW_Y, RAIL_X, LOW_Y], fill=LINE)
        self._soft(d, 0, "PREVIEW", active=ui.preview_on, color=ACCENT, sub="on" if ui.preview_on else "off",
                   action=("preview",), y=LOW_Y, h=LOW_H)
        self._soft(d, 1, "LOOP", active=ui.preview_loop, color=ACCENT, sub="on" if ui.preview_loop else "off",
                   action=("preview_loop",), y=LOW_Y, h=LOW_H)
        self._soft(d, 2, "SEARCH", active=live.br_path[:1] == ["@search"], color=ACCENT,
                   sub=(live.br_path[1] if live.br_path[:1] == ["@search"] else None), action=("search",),
                   y=LOW_Y, h=LOW_H)
        d.text((3 * COL_W + 20, LOW_Y + LOW_H // 2), "Wheel: move + hear  ·  Wheel push or +: open / load  ·  -: back",
               font=font(20, "SemiLight SemiCondensed"), fill=FAINT, anchor="lm")

    # ------------------------------------------------------------ overlays: action sheet, keyboard, colours

    SHEET_W, SHEET_COLS, SHEET_BTN_H = 1000, 3, 84

    def _dim(self, img, amount=0.75):
        img.paste(Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), amount))

    REPEAT_RATES = [(1.0, "1/4"), (2 / 3, "1/4T"), (0.5, "1/8"), (1 / 3, "1/8T"), (0.25, "1/16"), (1 / 6, "1/16T"),
                    (0.125, "1/32"), (1 / 12, "1/32T")]

    def _repeat_strip(self, d, ui):
        """Note Repeat rates above the track row while NOTE REPEAT is held or latched; tap one to pick it."""
        y0, y1 = UP_Y + 6, UP_Y + UP_H - 2        # over the top button row: toasts sit at the bottom
        d.rounded_rectangle([8, y0 - 6, RAIL_X - 8, y1 + 4], 10, fill=PANEL, outline=ACCENT, width=2)
        d.text((24, (y0 + y1) // 2), "NOTE REPEAT" + ("  (latched)" if ui.repeat_latch else ""), font=font(18),
               fill=ACCENT, anchor="lm")
        bw = (RAIL_X - 260) // len(self.REPEAT_RATES)
        for k, (beats, label) in enumerate(self.REPEAT_RATES):
            x = 240 + k * bw
            on = abs(ui.repeat_rate - beats) < 1e-6
            d.rounded_rectangle([x, y0, x + bw - 8, y1 - 4], 8, fill=ACCENT if on else CELL)
            d.text((x + (bw - 8) / 2, (y0 + y1 - 4) / 2), label, font=font(20), fill=ink(ACCENT) if on else TEXT,
                   anchor="mm")
            self.target(x, y0, x + bw - 8, y1, ("repeat_rate", beats))

    DIALOG_AREA = (40, HEAD_H + 44, W - 40, LOW_Y - 80)      # clear of the toasts at the bottom

    def _dialog(self, img, d):
        """Live's open dialog on the MPC: touch it like on the PC; KEYBOARD types into it, ENTER / ESC."""
        self._dim(img, 0.85)
        frame = self.dialog_frame
        x0, y0, x1, y1 = self.DIALOG_AREA
        px, py = x0 + (x1 - x0 - frame.width) // 2, y0 + (y1 - y0 - frame.height) // 2
        self.target(0, 0, W, H, ("noop",))
        img.paste(frame, (px, py))
        self.dialog_place = (px, py)
        self.target(px, py, px + frame.width, py + frame.height, ("drag", "dialog", 0))
        d.text((W // 2, HEAD_H + 24), "Live is asking: touch it like on the PC", font=font(22), fill=TEXT,
               anchor="mm")
        for k, (label, what) in enumerate((("KEYBOARD", "kb"), ("ENTER", "enter"), ("ESC / CANCEL", "esc"))):
            bx = W // 2 - 330 + k * 230
            d.rounded_rectangle([bx, LOW_Y - 4, bx + 200, LOW_Y + 60], 10, fill=ACCENT if what == "kb" else CELL,
                                outline=LINE, width=2)
            d.text((bx + 100, LOW_Y + 28), label, font=font(22), fill=ink(ACCENT) if what == "kb" else TEXT,
                   anchor="mm")
            self.target(bx, LOW_Y - 4, bx + 200, LOW_Y + 60, ("dialog_key", what))

    def _modifier_banner(self, d, ui):
        if ui.erase_held:
            msg, col = "ERASE: tap a clip, track or scene to delete it", REC
        elif ui.copy_src:
            msg, col = "COPY: tap the slot it goes to  ·  tap the clip again to cancel", ACCENT
        else:
            msg, col = "COPY: tap a clip, then where it goes  ·  or tap a track or scene to duplicate it", ACCENT
        f = font(22)
        w = d.textlength(msg, font=f) + 48
        d.rounded_rectangle([(W - w) // 2, HEAD_H + 6, (W + w) // 2, HEAD_H + 50], 22, fill=col)
        d.text((W // 2, HEAD_H + 28), msg, font=f, fill=ink(col), anchor="mm")

    def _sheet(self, d, ui):
        sh = ui.sheet
        items = sh["items"][:40]
        cols = self.SHEET_COLS if len(items) <= 12 else 4          # long lists (e.g. Live's inputs): 4 across,
        rows = (len(items) + cols - 1) // cols                     # buttons as tall as the screen allows
        btn_h = max(40, min(self.SHEET_BTN_H, (H - HEAD_H - 16 - 120) // max(1, rows) - 12))
        h = 96 + rows * (btn_h + 12) + 24
        x0, y0 = (W - self.SHEET_W) // 2, max(HEAD_H + 8, (H - h) // 2)
        d.rounded_rectangle([x0, y0, x0 + self.SHEET_W, y0 + h], 14, fill=PANEL, outline=LINE, width=2)
        self.target(x0, y0, x0 + self.SHEET_W, y0 + h, ("noop",))
        col = sh.get("color") or TEXT
        d.rounded_rectangle([x0 + 24, y0 + 30, x0 + 40, y0 + 46], 3, fill=col)
        d.text((x0 + 54, y0 + 38), fit(d, sh["title"], font(28, "Bold SemiCondensed"), self.SHEET_W - 260),
               font=font(28, "Bold SemiCondensed"), fill=TEXT, anchor="lm")
        d.text((x0 + self.SHEET_W - 24, y0 + 38), "Close", font=font(20), fill=DIM, anchor="rm")
        self.target(x0 + self.SHEET_W - 120, y0, x0 + self.SHEET_W, y0 + 76, ("overlay_close",))
        bw = (self.SHEET_W - 48 - 12 * (cols - 1)) // cols
        f = font(22 if cols == 3 else 18)
        for i, (label, cmd) in enumerate(items):
            r, c = divmod(i, cols)
            bx, by = x0 + 24 + c * (bw + 12), y0 + 84 + r * (btn_h + 12)
            danger = cmd[0].startswith("del_") or (cmd[0] == "arr_edit" and cmd[-2] == "delete")
            d.rounded_rectangle([bx, by, bx + bw, by + btn_h], 8, fill=CELL,
                                outline=REC if danger else LINE, width=2)
            d.text((bx + bw // 2, by + btn_h // 2), fit(d, label, f, bw - 20), font=f,
                   fill=REC if danger else TEXT, anchor="mm")
            self.target(bx, by, bx + bw, by + btn_h, ("sheet", i))

    KB_ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm-_."]
    KB_KEY_W, KB_KEY_H, KB_GAP = 116, 96, 10

    def _keyboard(self, d, ui):
        kb = ui.kb
        d.text((40, HEAD_H + 34), kb["title"], font=font(24), fill=DIM, anchor="lm")
        d.rounded_rectangle([30, HEAD_H + 62, W - 30, HEAD_H + 132], 8, fill=CELL, outline=LINE, width=2)
        f = font(36)
        shown = kb["text"]
        while shown and d.textlength(shown, font=f) > W - 120:
            shown = shown[1:]                        # long names: keep the end (where you type) in view
        d.text((50, HEAD_H + 97), shown, font=f, fill=TEXT, anchor="lm")
        cx = 50 + d.textlength(shown, font=f) + 4
        d.rectangle([cx, HEAD_H + 76, cx + 3, HEAD_H + 118], fill=ACCENT)
        kw, kh, g = self.KB_KEY_W, self.KB_KEY_H, self.KB_GAP
        y = HEAD_H + 152
        for r, keys in enumerate(self.KB_ROWS):
            n = len(keys) + (1 if r == 2 else 0)            # row 3 ends with the delete key
            x = (W - (n * kw + (n - 1) * g)) // 2
            for ch in keys:
                label = ch.upper() if kb["caps"] else ch
                d.rounded_rectangle([x, y, x + kw, y + kh], 8, fill=PANEL)
                d.text((x + kw // 2, y + kh // 2), label, font=font(32), fill=TEXT, anchor="mm")
                self.target(x, y, x + kw, y + kh, ("kb", label))
                x += kw + g
            if r == 2:
                d.rounded_rectangle([x, y, x + kw, y + kh], 8, fill=CELL, outline=LINE, width=2)
                d.text((x + kw // 2, y + kh // 2), "DEL", font=font(24), fill=TEXT, anchor="mm")
                self.target(x, y, x + kw, y + kh, ("kb", "DEL"))
            y += kh + g
        specials = [("CAPS", 170, kb["caps"]), ("SPACE", 420, False), ("CLEAR", 170, False),
                    ("CANCEL", 190, False), ("OK", 190, True)]
        x = (W - (sum(w for _l, w, _a in specials) + g * (len(specials) - 1))) // 2
        for label, w, hot in specials:
            fill = ACCENT if (label == "OK" or (label == "CAPS" and hot)) else CELL
            d.rounded_rectangle([x, y, x + w, y + kh], 8, fill=fill, outline=LINE, width=2)
            d.text((x + w // 2, y + kh // 2), label, font=font(24), fill=ink(fill), anchor="mm")
            self.target(x, y, x + w, y + kh, ("kb", label))
            x += w + g

    def _color_picker(self, d, ui):
        cols, size, g = 14, 76, 10
        x0 = (W - (cols * size + (cols - 1) * g)) // 2
        y0 = HEAD_H + 110
        d.rounded_rectangle([x0 - 24, HEAD_H + 20, W - x0 + 24, y0 + 5 * (size + g) + 30], 14, fill=PANEL,
                            outline=LINE, width=2)
        d.text((x0, HEAD_H + 62), ui.colors["title"], font=font(28, "Bold SemiCondensed"), fill=TEXT, anchor="lm")
        d.text((W - x0, HEAD_H + 62), "Close", font=font(20), fill=DIM, anchor="rm")
        self.target(W - x0 - 120, HEAD_H + 20, W - x0 + 24, HEAD_H + 100, ("overlay_close",))
        for i, c in enumerate(LIVE_COLORS):
            r, k = divmod(i, cols)
            x, y = x0 + k * (size + g), y0 + r * (size + g)
            d.rounded_rectangle([x, y, x + size, y + size], 8, fill=rgb(c))
            self.target(x, y, x + size, y + size, ("color_pick", i))

    # ------------------------------------------------------------ help overlay

    HELP = [
        ("Pages", "MAIN Session · NEXT SEQ Arrange · STEP SEQ Clip · TRACK MIX Mix · PROGRAM EDIT Device · BROWSE"),
        ("Pads", "Play the selected track (bottom-left = C3, auto-arms MIDI tracks) · TRACK MUTE: launch clips"),
        ("PAD BANK A-D", "Playing: C3 / C4 / C1 (drum racks) / C2 · Launching: move the white 4×4 block"),
        ("FULL LEVEL", "Every pad hit at full velocity"),
        ("Record arm", "Tap the dot on a track button (bottom row)   ·   SHIFT+REC: arm the selected track"),
        ("Clip (STEP SEQ)", "Tap grid = add/remove note · pads = 16 steps of the orange row · Q1-Q4 time/zoom/pitch/vel"),
        ("Arrange", "Wheel moves playhead by bars · Q1 scroll · Q2 zoom · Q3/Q4 loop start/length"),
        ("Data wheel", "Session: scroll scenes   ·   Mix/Device: select track   ·   SHIFT: tempo"),
        ("- / +", "Previous / next 8 tracks"),
        ("Q-Links", "Mix: volume or pan   ·   Device: parameters   ·   Q-LINK: switch"),
        ("PLAY · STOP · PLAY START", "Start/stop · Stop (SHIFT: stop all clips) · From the start"),
        ("REC · OVERDUB", "Session record (Arrange page: arrangement record) · Arrangement overdub"),
        ("TAP TEMPO · UNDO", "Tap tempo (SHIFT: metronome) · Undo (SHIFT: redo)"),
        ("MENU", "Menu (save, capture, fixed length) · hold clip/track/scene = edit · ERASE/COPY + tap"),
    ]

    def _help(self, img):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        img.paste(Image.blend(img, overlay, 0.93))
        d = ImageDraw.Draw(img)
        d.text((60, 92), "Controls", font=font(44, "Bold SemiCondensed"), fill=TEXT, anchor="lm")
        d.text((W - 60, 92), "Tap anywhere or press MENU to close  ·  SHIFT+MENU quits", font=font(20), fill=DIM,
               anchor="rm")
        for i, (k, v) in enumerate(self.HELP):
            y = 150 + i * 44
            d.text((60, y), k, font=font(22, "Bold SemiCondensed"), fill=ACCENT, anchor="lm")
            d.text((360, y), fit(d, v, font(22, "SemiLight SemiCondensed"), W - 400), font=font(22,
                   "SemiLight SemiCondensed"), fill=TEXT, anchor="lm")
        self.targets = [(0, 0, W, H, ("help_close",))]
