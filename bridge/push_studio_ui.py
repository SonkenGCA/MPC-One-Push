"""Opt-in visual layer. Inherits the current engine's actions and specialist views.

No USB, MIDI, OSC, filesystem writes, or Live mutations occur in this renderer.
Geometry used by the engine's drag calculations is deliberately preserved.
"""
import math
import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageEnhance
import push_ui as base
from push_ui import (W, H, HEAD_H, UP_Y, UP_H, MAIN_Y, LOW_Y, LOW_H,
                     MAIN_H, COL_W, COLS, RAIL_X, ROW_H, rgb, scale, fit, note_name)

BG = (0, 0, 0)
SURFACE = (17, 18, 19)
LINE = (38, 40, 42)
TEXT = (238, 239, 235)
DIM = (155, 159, 158)
FAINT = (90, 94, 94)
GREEN = (163, 232, 116)
RED = (255, 92, 96)
AMBER = (245, 190, 91)


@lru_cache(maxsize=96)
def font(size, bold=False):
    # Installed Windows font, no downloaded or proprietary Ableton assets.
    return ImageFont.truetype('segoeuib.ttf' if bold else 'segoeui.ttf', size)


def text(d, xy, value, size=18, color=TEXT, anchor='lm', width=None, bold=False):
    f = font(size, bold)
    value = str(value)
    d.text(xy, fit(d, value, f, width) if width is not None else value,
           font=f, fill=color, anchor=anchor)


def install_theme():
    """Theme inherited views in this opt-in process only; never rewrite source."""
    base.font = lambda size, style='': font(size, 'Bold' in style and 'SemiBold' not in style)
    base.PANEL, base.CELL, base.LINE = SURFACE, (24, 26, 27), LINE
    base.TEXT, base.DIM, base.FAINT = TEXT, DIM, FAINT
    base.ACCENT, base.PLAY, base.REC = AMBER, GREEN, RED


class Renderer(base.Renderer):
    def __init__(self, motion=True, clock=time.monotonic):
        super().__init__()
        self.motion = motion
        self.clock = clock
        self.animating = False
        self._values = {}
        self._last = None
        self._tab = None
        self._now = 0.0
        self._dt = 0.04
        self._scene = None
        self._scene_at = -100.0
        self._selection = None
        self._selection_at = -100.0
        self._press = None
        self._toast_key = None
        self._toast_start = -100.0

    def press(self, x, y):
        """Visual acknowledgment only. Never dispatches an action or moves a target."""
        if self.motion:
            self._press = (x, y, self.clock())

    def _finish_motion(self, img, live, ui):
        # Render the destination immediately: all labels and hit boxes stay aligned.
        # A short luminance reveal gives changes weight without ghost controls.
        overlay = ('keyboard' if ui.kb else 'sheet' if ui.sheet else 'colors' if ui.colors else
                   'help' if ui.help else 'dialog' if self.dialog_frame is not None else '')
        scene = (ui.page, ui.bank, ui.clip_mode, ui.mix_view, ui.mix_returns,
                 live.dev.path() if ui.page == 'device' else None,
                 live.dev.bank if ui.page == 'device' else None,
                 tuple(live.br_path) if ui.page == 'browse' else None, overlay, ui.add_menu)
        if scene != self._scene:
            self._scene_at = self._now if self._scene is not None else -100
            self._scene = scene
            self._press = None
        elapsed = self._now - self._scene_at
        # Keep mirrored windows, recording indicators and transport unfiltered.
        mirrored_plugin = ui.page == 'device' and ui.plugin_ui and self.plugin_frame is not None
        if self.motion and 0 <= elapsed < .18 and not overlay and not mirrored_plugin:
            area = (0, UP_Y, W, LOW_Y)
            amount = 1 - (1 - elapsed / .18) ** 3
            img.paste(ImageEnhance.Brightness(img.crop(area)).enhance(.78 + .22 * amount), area)
            self.animating = True
        selected = (live.sel, ui.br_sel, live.dev.idx, live.dev.bank, overlay, ui.arr_selected)
        if selected != self._selection:
            self._selection_at = self._now if self._selection is not None else -100
            self._selection = selected
        age = self._now - self._selection_at
        d = ImageDraw.Draw(img)
        if self.motion and overlay and overlay != 'dialog' and 0 <= elapsed < .24:
            self.animating = True
            # A growing hairline on the modal's top edge; geometry stays fixed.
            bounds = next((t[:4] for t in self.targets if t[4] == ('noop',)
                           and t[0] > 0 and t[2]-t[0] > 300), None)
            if bounds:
                x0,y0,x1,y1 = bounds
                reach = (x1-x0-48) * (1-(1-elapsed/.24)**3)
                d.line((x0+24,y0+1,x0+24+reach,y0+1), fill=DIM, width=2)
        if self.motion and 0 <= age < .28:
            self.animating = True
            strength = (1 - age / .28) ** 2
            # Highlight the real selected control; never interpolate its position.
            wanted = ('br_item', ui.br_sel) if ui.page == 'browse' else ('track', live.sel)
            if ui.page == 'arrange' and ui.arr_selected:
                wanted = ('arr_clip', *ui.arr_selected)
            for x0, y0, x1, y1, action in self.targets:
                if action == wanted and not overlay:
                    d.rounded_rectangle((x0 + 3, y0 + 3, x1 - 4, y1 - 4), 4,
                                        outline=scale(TEXT, .25 + .65 * strength), width=2)
        if self.motion and self._press and overlay != 'dialog' and not mirrored_plugin:
            x, y, started = self._press
            age = self._now - started
            if 0 <= age < .22:
                self.animating = True
                radius = 7 + 13 * (1 - (1 - age / .22) ** 3)
                d.ellipse((x-radius, y-radius, x+radius, y+radius),
                          outline=scale(TEXT, .65 * (1-age/.22)), width=2)
            else:
                self._press = None
        return img

    def ease(self, key, value, tau=0.075):
        old = self._values.get(key, value)
        current = value if not self.motion else old + (value - old) * (1 - math.exp(-self._dt / tau))
        if abs(value - current) < 0.002:
            current = value
        else:
            self.animating = True
        self._values[key] = current
        return current

    def render(self, live, ui):
        self._now = self.clock()
        self._dt = max(0.001, min(0.25, self._now - self._last)) if self._last is not None else 0.04
        self._last = self._now
        self.animating = False
        self._beat = live.song_time
        self._playing = live.playing
        # Prune visual history on context changes; no accumulated per-clip cache.
        context = (ui.page, ui.bank, live.dev.track, live.dev.path(), live.dev.bank)
        if getattr(self, '_context', None) != context:
            self._values.clear()
            self._context = context
        img, targets = super().render(live, ui)
        return self._finish_motion(img, live, ui), targets

    def _chain_strip(self, d, dev, col):
        bw, gap = 180, 14
        n = len(dev.names) + 1
        vis = (RAIL_X - 16) // (bw + gap)
        off = max(0, min(dev.idx - 2, n - vis))
        y0, y1 = self.STRIP_Y, self.STRIP_Y + self.STRIP_H
        for k in range(off, min(n, off + vis)):
            x = 12 + (k - off) * (bw + gap)
            selected = k == dev.idx
            d.rounded_rectangle((x, y0, x + bw, y1), 4,
                                fill=scale(col, .12) if selected else SURFACE,
                                outline=scale(col, .55) if selected else LINE)
            if k == len(dev.names):
                text(d, (x + 90, (y0+y1)//2), '+ Add device', 17, DIM, 'mm')
                self.target(x, y0, x+bw, y1, ('dev_add',))
                continue
            cls, on, rack, drum, count = dev.info[k] if k < len(dev.info) else ('', 1, 0, 0, 0)
            d.ellipse((x+12, y0+16, x+18, y0+22), fill=col if on else FAINT)
            text(d, (x+27, y0+20), dev.names[k], 17, TEXT if on else FAINT, width=140)
            sub = '%d %s' % (count, 'pads' if drum else 'chains') if rack else ('Active' if on else 'Bypassed')
            text(d, (x+13, y1-21), sub, 11, col if selected else FAINT)
            if selected:
                length = self.ease(('chain-line', k), bw-26)
                d.line((x+13, y1-1, x+13+length, y1-1), fill=col, width=2)
            if k+1 < n:
                d.line((x+bw+4, (y0+y1)//2, x+bw+10, (y0+y1)//2), fill=FAINT)
            self.target(x, y0, x+bw, y1, ('device', k))

    def _sheet(self, d, ui):
        sh, items = ui.sheet, ui.sheet['items'][:40]
        if sh is not getattr(self, '_last_sheet', None):
            self._last_sheet, self._sheet_at = sh, self._now
        age = self._now-self._sheet_at
        cols = self.SHEET_COLS if len(items) <= 12 else 4
        rows = (len(items)+cols-1)//cols
        bh = max(40, min(self.SHEET_BTN_H, (H-HEAD_H-16-120)//max(1, rows)-12))
        h = 120+rows*(bh+12)
        x, y = (W-self.SHEET_W)//2, max(HEAD_H+8, (H-h)//2)
        color = sh.get('color') or TEXT
        d.rounded_rectangle((x,y,x+self.SHEET_W,y+h), 10, fill=SURFACE, outline=(65,68,67))
        d.line((x+24,y+70,x+self.SHEET_W-24,y+70), fill=LINE)
        d.rounded_rectangle((x+24,y+28,x+28,y+48), 2, fill=color)
        text(d,(x+44,y+38),sh['title'],25,TEXT,width=self.SHEET_W-210,bold=True)
        text(d,(x+self.SHEET_W-30,y+38),'Close',16,DIM,'rm')
        self.target(x,y,x+self.SHEET_W,y+h,('noop',))
        self.target(x+self.SHEET_W-120,y,x+self.SHEET_W,y+76,('overlay_close',))
        bw=(self.SHEET_W-48-12*(cols-1))//cols
        for i,(label,cmd) in enumerate(items):
            row,c=divmod(i,cols)
            bx,by=x+24+c*(bw+12), y+84+row*(bh+12)
            danger=cmd[0].startswith('del_') or (cmd[0] == 'arr_edit' and cmd[-2] == 'delete')
            cc=RED if danger else TEXT
            progress = min(1., max(0., (age-row*.025)/.18)) if self.motion else 1.
            reveal = 1-(1-progress)**3
            if progress < 1:
                self.animating = True
            d.rounded_rectangle((bx,by,bx+bw,by+bh),4,fill=scale((25,27,28),.65+.35*reveal),outline=scale(RED,.35) if danger else LINE)
            text(d,(bx+16,by+bh//2),label,18 if cols==3 else 16,scale(cc,.35+.65*reveal),width=bw-38)
            self.target(bx,by,bx+bw,by+bh,('sheet',i))

    def _header(self, d, live, ui):
        d.rectangle((0, 0, W, HEAD_H - 1), fill=SURFACE)
        d.line((0, HEAD_H - 1, W, HEAD_H - 1), fill=LINE)
        d.ellipse((14, 23, 20, 29), fill=GREEN if live.connected else RED)
        tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
        text(d, (32, 25), tr.name if tr else 'MPC / PUSH', 18, width=164, bold=True)
        mode = ui.pad_mode()
        label = {'play': ui.play_label(live), 'launch': 'CLIPS', 'step': 'STEPS'}[mode]
        color = AMBER if mode == 'play' and not ui.midi_port else (rgb(tr.color) if tr else DIM)
        d.rounded_rectangle((208, 12, 306, 40), 3, outline=LINE)
        text(d, (257, 26), label, 12, color, 'mm', width=90, bold=True)
        self.target(204, 0, 310, HEAD_H, ('pads_mode',))
        # Every transport target has a separate interval; the old last tab overlapped REC.
        for i, p in enumerate(base.PAGES):
            x = 320 + i * 100
            text(d, (x + 50, 25), p.capitalize(), 17, TEXT if p == ui.page else DIM, 'mm')
            self.target(x, 0, x + 100, HEAD_H, ('page', p))
        wanted = 320 + base.PAGES.index(ui.page) * 100
        if self._tab is None or not self.motion:
            self._tab = float(wanted)
        self._tab += (wanted - self._tab) * (1 - math.exp(-self._dt / 0.055))
        if abs(wanted - self._tab) > 0.3:
            self.animating = True
        else:
            self._tab = float(wanted)
        d.rectangle((round(self._tab) + 20, 49, round(self._tab) + 80, 51), fill=TEXT)
        rec = live.rec_mode if ui.page == 'arrange' else live.rec
        d.ellipse((938, 18, 954, 34), fill=RED if rec else None, outline=RED if rec else DIM, width=2)
        self.target(922, 0, 970, HEAD_H, ('rec',))
        mc = AMBER if live.metro else FAINT
        d.polygon(((988, 34), (1008, 34), (1002, 17), (994, 17)), outline=mc, width=2)
        d.line((998, 29, 1008, 17), fill=mc, width=2)
        self.target(974, 0, 1022, HEAD_H, ('metro',))
        d.polygon(((1041, 17), (1041, 35), (1057, 26)), fill=GREEN if live.playing else DIM)
        self.target(1026, 0, 1074, HEAD_H, ('play',))
        text(d, (1136, 27), 'BPM', 11, FAINT, 'rm')
        text(d, (1260, 26), '%.2f' % live.tempo, 27, TEXT, 'rm')
        self.target(1084, 0, W, HEAD_H, ('drag', 'tempo', 0))

    def _soft(self, d, i, label, active=False, color=None, sub=None, action=None, y=UP_Y, h=UP_H):
        x = i * COL_W
        col = color or TEXT
        amount = self.ease(('soft', i, y), float(active), .055)
        d.rectangle((x + 3, y + 5, x + COL_W - 4, y + h - 5), fill=scale(col, 0.14 * amount))
        d.line((x + COL_W - 1, y + 16, x + COL_W - 1, y + h - 16), fill=LINE)
        if active or color:
            d.rectangle((x + 15, y + h - 7, x + COL_W - 16, y + h - 5), fill=col if active else scale(col, .55))
        text(d, (x + COL_W // 2, y + h // 2 - (8 if sub else 0)), label, 16,
             col if active else DIM, 'mm', COL_W - 24, bold=active)
        if sub:
            text(d, (x + COL_W // 2, y + h // 2 + 15), sub, 12, DIM, 'mm', COL_W - 24)
        if action:
            self.target(x, y, x + COL_W, y + h, action)

    def _lower_tracks(self, d, live, ui):
        d.rectangle((0, LOW_Y, RAIL_X - 1, H), fill=SURFACE)
        for c in range(COLS):
            t = ui.bank + c
            if t >= len(live.tracks):
                continue
            tr = live.tracks[t]
            x, col = c * COL_W, rgb(tr.color)
            selected = t == live.sel
            if selected:
                d.rectangle((x + 2, LOW_Y + 3, x + 147, H - 3), fill=scale(col, .15))
            d.rectangle((x + 12, LOW_Y + 3, x + 137, LOW_Y + (6 if selected else 4)), fill=col)
            text(d, (x + 13, LOW_Y + 23), '%02d' % (t + 1), 11, col)
            text(d, (x + 13, LOW_Y + 49), tr.name or 'Track %d' % (t + 1), 18,
                 TEXT if selected else DIM, width=126, bold=selected)
            status = ' / '.join(s for s, on in [('MUTE', tr.mute), ('SOLO', tr.solo)] if on)
            text(d, (x + 42, LOW_Y + 23), status, 10, AMBER, width=65)
            self.target(x, LOW_Y, x + COL_W, H, ('track', t))
            if tr.armable:
                d.ellipse((x + 122, LOW_Y + 15, x + 136, LOW_Y + 29), fill=RED if tr.arm else None,
                          outline=RED if tr.arm else FAINT, width=2)
                self.target(x + 106, LOW_Y, x + 149, LOW_Y + 43, ('toggle', 'arm', t))

    def _clip_cell(self, d, tr, s, x0, y0, blink):
        clip = tr.clips[s] if s < len(tr.clips) else (False, '', 0)
        playing, queued = tr.playing == s, tr.fired == s and tr.playing != s
        box = (x0 + 5, y0 + 5, x0 + COL_W - 6, y0 + ROW_H - 6)
        if clip[0]:
            col = rgb(clip[2] or tr.color)
            d.rounded_rectangle(box, 3, fill=scale(col, .26 if playing else .11), outline=scale(col, .65 if playing else .25))
            d.rectangle((x0 + 6, y0 + 10, x0 + 8, y0 + ROW_H - 11), fill=col)
            if playing:
                d.polygon(((x0 + 17, y0 + 17), (x0 + 17, y0 + 28), (x0 + 26, y0 + 22)), fill=col)
                text(d, (x0 + 34, y0 + 22), 'PLAYING', 9, col)
            elif queued:
                # A localized breath, not a full-cell flash. Never implies clip progress.
                a = .55 + .45 * math.sin(self._now * 2 * math.pi) ** 2 if self.motion else 1
                d.ellipse((x0 + 18, y0 + 17, x0 + 25, y0 + 24), fill=scale(col, a))
                text(d, (x0 + 34, y0 + 22), 'QUEUED', 9, col)
                self.animating = self.motion or self.animating
            text(d, (x0 + 17, y0 + ROW_H - 23), clip[1], 16, TEXT if playing else col, width=COL_W - 31)
        else:
            d.rounded_rectangle(box, 3, fill=(10, 11, 12), outline=(22, 24, 25))
            cx, cy = x0 + COL_W // 2, y0 + ROW_H // 2
            if tr.arm:
                d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=RED, width=1)
            else:
                d.rectangle((cx - 2, cy - 2, cx + 2, cy + 2), fill=LINE)

    def _mix_fader(self, d, c, key, tr, ui, q):
        """One mixer strip (track, return or main) in this look; the Mix page tabs and views are inherited."""
        x, cx, col = c * COL_W, c * COL_W + 75, rgb(tr.color)
        d.line((x + 149, MAIN_Y + 20, x + 149, LOW_Y - 24), fill=LINE)
        if q is not None:
            text(d, (x + 16, MAIN_Y + 22), 'Q%d' % (q + 1), 11, col)
            if q in ui.qlink_touch:
                d.line((x + 15, MAIN_Y + 5, x + 135, MAIN_Y + 5), fill=TEXT, width=2)
        if key[0] != 'track':
            text(d, (cx + 8, MAIN_Y + 22), tr.name, 12, col, 'mm', 100, bold=True)
        text(d, (cx, MAIN_Y + 54), tr.vol_str or '—', 23, TEXT, 'mm')
        py = MAIN_Y + 84
        p = max(-1., min(1., tr.pan))
        d.line((cx - 51, py + 3, cx + 51, py + 3), fill=LINE, width=3)
        px = cx + 51 * p
        d.line((cx, py - 2, cx, py + 8), fill=FAINT)
        d.ellipse((px - 4, py - 1, px + 4, py + 7), fill=col)
        text(d, (cx, py + 25), 'C' if abs(p) < .01 else '%d%s' % (round(abs(p) * 50), 'L' if p < 0 else 'R'), 12, DIM, 'mm')
        self.target(x, py - 16, x + COL_W, py + 34, ('drag', 'pan', key))
        fy0, fy1, fx = MAIN_Y + 136, MAIN_Y + 470, cx - 22
        for j in range(9):
            yy = fy0 + j * (fy1 - fy0) / 8
            d.line((fx - 20, yy, fx - 12, yy), fill=FAINT if j % 2 == 0 else LINE)
        d.line((fx, fy0, fx, fy1), fill=LINE, width=3)
        fy = fy1 - (fy1 - fy0) * max(0, min(1, tr.vol))
        d.line((fx, fy, fx, fy1), fill=col, width=3)
        d.rounded_rectangle((fx - 13, fy - 4, fx + 13, fy + 4), 2, fill=TEXT)
        # Meter release is visual only; the audio value and OSC state are untouched.
        m = self.ease(('meter', key), max(0., min(1., tr.meter)), .09)
        mx = cx + 20
        for j in range(42):
            yy = fy1 - j * (fy1 - fy0) / 42
            cc = (GREEN if j < 34 else AMBER if j < 40 else RED) if j / 42 < m else (23, 26, 24)
            d.rectangle((mx, yy - 4, mx + 7, yy), fill=cc)
        self.target(x, fy0 - 10, x + COL_W, fy1 + 10, ('drag', 'vol', key))
        by = MAIN_Y + 500
        colours = {'mute': AMBER, 'solo': (135, 186, 255), 'arm': RED}
        for i, (what, label, on, _c, action) in enumerate(self._strip_buttons(key, tr)):
            cc = colours[what]
            bx = x + 13 + i * 43
            enabled = what != 'arm' or tr.armable
            d.rounded_rectangle((bx, by, bx + 38, by + 44), 3, fill=scale(cc, .2) if on else SURFACE,
                                outline=cc if on else LINE)
            if what == 'arm':
                d.ellipse((bx + 13, by + 16, bx + 25, by + 28), fill=cc if on else None, outline=cc if enabled else LINE)
            else:
                text(d, (bx + 19, by + 22), label, 14, cc if on else DIM, 'mm')
            if enabled:
                self.target(bx - 3, by - 6, bx + 41, by + 50, action)

    def _knob(self, d, cx, cy, r, n, col, key=None):
        n = self.ease(key, n) if key is not None else n
        box = (cx - r, cy - r, cx + r, cy + r)
        d.arc(box, 135, 405, fill=LINE, width=3)
        if n > .001:
            d.arc(box, 135, 135 + 270 * n, fill=col, width=3)
        a = math.radians(135 + 270 * n)
        ex, ey = cx + math.cos(a) * r, cy + math.sin(a) * r
        d.ellipse((ex - 4, ey - 4, ex + 4, ey + 4), fill=TEXT)

    def _device_knobs(self, d, dev, ui, col, ky0=None, focus=True):
        ky0 = self.KNOBS_Y if ky0 is None else ky0
        text(d, (RAIL_X - 18, ky0 - 2), '%02d / %02d' % (dev.bank + 1, dev.num_banks()), 12, DIM, 'rm')
        shown, qparams = dev.bank_params(), ui.qlink_params(dev)
        for c, p in enumerate(shown):
            x, cx, cy = c * COL_W, c * COL_W + 75, ky0 + 120
            q = qparams.index(p) if p in qparams else None
            touched = q is not None and q in ui.qlink_touch
            d.line((x + 149, ky0 + 25, x + 149, ky0 + 245), fill=LINE)
            text(d, (cx, ky0 + 34), dev.pnames[p], 16, TEXT if touched else DIM, 'mm', 130)
            n = self.ease(('param', p), dev.norm(p))
            box = (cx - 38, cy - 38, cx + 38, cy + 38)
            d.arc(box, 135, 405, fill=LINE, width=3)
            if n > .001:
                d.arc(box, 135, 135 + 270 * n, fill=col, width=3)
            a = math.radians(135 + 270 * n)
            ex, ey = cx + math.cos(a) * 38, cy + math.sin(a) * 38
            d.ellipse((ex - 3, ey - 3, ex + 3, ey + 3), fill=TEXT)
            text(d, (cx, ky0 + 200), dev.pstr.get(p, ''), 23, col, 'mm', 135)
            if q is not None:
                d.rounded_rectangle((cx - 20, ky0 + 233, cx + 20, ky0 + 255), 3, fill=scale(col, .16) if touched else SURFACE)
                text(d, (cx, ky0 + 244), 'Q%d' % (q + 1), 11, TEXT if touched else DIM, 'mm')
            self.target(x, ky0 + 10, x + COL_W, ky0 + 262, ('drag', 'param', p))
        p = ui.focus_param
        if focus:
            y = ky0 + 290
            if p is not None and time.time() - ui.focus_at < 3 and p < len(dev.pnames):
                text(d, (30, y + 20), dev.pnames[p], 18, DIM)
                text(d, (30, y + 82), dev.pstr.get(p, ''), 48, TEXT)
                d.line((610, y + 80, 1160, y + 80), fill=LINE, width=3)
                d.line((610, y + 80, 610 + 550 * dev.norm(p), y + 80), fill=col, width=3)
            else:
                text(d, (30, y + 54), 'TOUCH TO FOCUS', 11, FAINT)
                text(d, (30, y + 84), 'Four Q-Links. Every detail at your fingertips.', 18, DIM)
                text(d, (1160, y + 84), 'Q-LINK  /  %s' % ('5—8' if ui.qlink_half == 0 else '1—4'), 12, col, 'rm')
        for yy, glyph, step in ((MAIN_Y, '‹', -1), (MAIN_Y + MAIN_H // 2, '›', 1)):
            ok = 0 <= dev.bank + step < dev.num_banks()
            text(d, (1240, yy + MAIN_H // 4), glyph, 32, TEXT if ok else FAINT, 'mm')
            self.target(RAIL_X, yy, W, yy + MAIN_H // 2, ('param_bank', step))

    def _toast(self, d, msg):
        if msg != self._toast_key:
            self._toast_key, self._toast_start = msg, self._now
        age = self._now-self._toast_start
        msg = fit(d, msg, font(19), 1050)
        w = min(1120, d.textlength(msg, font=font(19)) + 64)
        x, y = (W - w) // 2, LOW_Y - 68
        if self.motion and age < .2:
            y += round(12 * (1-min(1,age/.2))**3)
            self.animating = True
        d.rounded_rectangle((x, y, x + w, y + 48), 5, fill=(34, 36, 36), outline=(75, 79, 77))
        text(d, (W // 2, y + 24), msg, 19, TEXT, 'mm')

    def _help(self, img):
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, H), fill=SURFACE)
        text(d, (32, 40), 'At your fingertips.', 30, TEXT)
        text(d, (W - 32, 40), 'TAP TO CLOSE   /   SHIFT + MENU TO QUIT', 12, DIM, 'rm')
        for i, (label, body) in enumerate(self.HELP):
            y = 92 + i * 48
            d.line((32, y - 18, W - 32, y - 18), fill=LINE)
            text(d, (32, y), label, 16, AMBER, width=250)
            words, lines, line = body.split(), [], ''
            for word in words:
                candidate = (line + ' ' + word).strip()
                if d.textlength(candidate, font=font(15)) > 930:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
            for j, part in enumerate(lines[:2]):
                text(d, (300, y - 5 + j * 19), part, 15, DIM, width=930)
        self.targets = [(0, 0, W, H, ('help_close',))]

    def _waiting(self, d):
        for i in range(4):
            d.rounded_rectangle((590 + i * 27, 260, 607 + i * 27, 277), 3, fill=GREEN if i == 0 else LINE)
        text(d, (640, 340), 'Ready when you are.', 40, TEXT, 'mm')
        text(d, (640, 398), 'Open Live with AbletonOSC enabled to connect.', 20, DIM, 'mm')
        text(d, (640, 462), 'MPC ONE  /  PUSH STUDIO', 12, FAINT, 'mm')
