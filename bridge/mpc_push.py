"""MPC One Push: a Push 3-style Ableton Live controller on the MPC One's own screen, pads and knobs.

One process holds the MPC's screen + control session (mpc_screen / mpc_control, the same live USB
session MPC 3 uses; nothing is written to the MPC) and talks to Live through AbletonOSC (live.py).

Usage:  .venv\\Scripts\\python.exe bridge\\mpc_push.py              run it (MPC 3 closed, Live open)
        .venv\\Scripts\\python.exe bridge\\mpc_push.py --preview    on the MPC with made-up Live state
        .venv\\Scripts\\python.exe bridge\\mpc_push.py --demo       render each page to logs\\ui-*.png
Quit: Ctrl+C in the console, hold SHIFT and press MENU on the MPC, or create logs\\stop-push.
"""

import json
import math
import os
import sys
import threading
import time

from PIL import ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from live import BANK, GRID_PAGE, PLACES, SEARCH, Live, Track  # noqa: E402
from midi_out import MidiOut  # noqa: E402
import places  # noqa: E402
from preview import PREVIEWABLE, Previewer  # noqa: E402
from push_ui import (COLS, GRIDS, H, KEY_W, MAIN_Y, NOTE_NAMES, PAGES, RAIL_X, ROLL_ROW_H, ROLL_ROWS,  # noqa: E402
                     ROLL_RULER_H, ROWS, TL_X, W, ARR_RULER_H, ARR_LANE_H, Renderer, UiState, note_name, rgb, scale)

# Live 12's scale names (Song.scale_name's own documentation)
SCALE_NAMES = ["Major", "Minor", "Dorian", "Mixolydian", "Lydian", "Phrygian", "Locrian", "Whole Tone",
               "Half-whole Dim.", "Whole-half Dim.", "Minor Blues", "Minor Pentatonic", "Major Pentatonic",
               "Harmonic Minor", "Harmonic Major", "Dorian #4", "Phrygian Dominant", "Melodic Minor",
               "Lydian Augmented", "Lydian Dominant", "Super Locrian", "Bhairav", "Hungarian Minor", "8-Tone Spanish",
               "Hirajoshi", "In-Sen", "Iwato", "Kumoi", "Pelog Selisir", "Pelog Tembung", "Messiaen 3", "Messiaen 4",
               "Messiaen 5", "Messiaen 6", "Messiaen 7"]

PAGE_BUTTONS = {"MAIN": "session", "NEXT SEQ": "arrange", "STEP SEQ": "clip", "TRACK MIX": "mix",
                "PROGRAM EDIT": "device", "BROWSE": "browse"}
QUANTIZE_GRID = {1.0: 1, 0.5: 2, 0.25: 5, 0.125: 8}     # grid -> Live's RecordingQuantization index
PAD_BANK_BASES = {"A": 60, "B": 72, "C": 36, "D": 48}    # play mode: bottom-left pad = C3 / C4 / C1 / C2
DRUM_BANKS = {"A": 36, "B": 52, "C": 68, "D": 84}        # Drum Rack layout: 4 banks of 16 pads, like the MPC
GATE = 0.5                                               # Note Repeat: notes last half the time to the next one

# Pad notes in pad order: pad 1 (bottom-left) .. pad 16 (top-right); pad_color() uses the same order.
PAD_NOTES = [37, 36, 42, 82, 40, 38, 46, 44, 48, 47, 45, 43, 49, 55, 51, 53]
QLINK_CC = range(0x10, 0x14)
WHEEL_CC = 0x64
# The MPC screen takes pixels as blue, green, red (orange showed up blue on the MPC with plain RGB, 2026-10-29;
# MPC 3's own grey/white UI hid the swap when the format was worked out from its USB traffic).
SCREEN_RAW = ("raw", "BGR")
BAND = 50                  # screen diff granularity (rows)
STOP_FILE = os.path.join(HERE, "..", "logs", "stop-push")    # create it to make a running app quit cleanly
QUIT_FILE = os.path.join(HERE, "..", "logs", "user-quit")    # written when you quit on the MPC (auto-start waits)
SETTINGS_FILE = os.path.join(HERE, "..", "settings.json")    # your choices, kept between runs
SETTINGS = ["page", "pads", "pad_base", "note_layout", "drum_base", "full_level", "fixed_len", "rec_to_arr",
            "preview_on", "preview_loop", "repeat_rate", "grid", "clip_mode", "mix_view", "mix_target", "mix_returns",
            "plugin_ui", "arr_follow", "arr_len", "preset_sub"]
SETTINGS += ["chord_mode", "velocity_curve", "pressure_mode"]
CHORDS = {"off": (0,), "major": (0, 4, 7), "minor": (0, 3, 7), "seventh": (0, 4, 7, 10),
          "minor seventh": (0, 3, 7, 10), "sus2": (0, 2, 7), "sus4": (0, 5, 7), "octaves": (0, 12)}
FRAME_S = 0.04             # at most 25 frames a second
LED_OFF, LED_DIM, LED_ON = 0, 1, 3
LONG_PRESS_S = 0.45        # hold a clip, track or scene this long for its action sheet
FIXED_LENGTHS = [0, 1, 2, 4, 8]    # bars; 0 = Fixed Length off
DOUBLE_TAP_S = 0.35        # two taps this close on a fader / pan / send reset it
NO_LED = {"DATA WHEEL (push)"} | {"Q-LINK %d (touch, don't turn)" % i for i in range(1, 5)}


def strip_key(i):
    """Mixer drag targets carry a track index (older targets) or a ("track"/"return"/"master", n) key."""
    return ("track", i) if isinstance(i, int) else i


def load_buttons():
    with open(os.path.join(HERE, "mpc_one_buttons.json")) as f:
        names = json.load(f)
    return {note: name for name, note in names.items()}


def rel(v):
    """Relative encoder value: 1..63 clockwise, 127..65 anticlockwise."""
    return v if v < 64 else v - 128


class App:
    def __init__(self, live=None):
        os.makedirs(os.path.join(HERE, "..", "logs"), exist_ok=True)
        self.ui = UiState()
        self.renderer = Renderer()
        self.dirty = threading.Event()
        self.lock = threading.RLock()
        self.live = live or Live(on_change=self.dirty.set)
        self.live.on_change = self.dirty.set
        self.buttons = load_buttons()
        self.notes = {name: note for note, name in self.buttons.items()}
        self.targets = []
        self.prev = None
        self.pads_sent = [None] * 16
        self.leds_sent = {}
        self.touch = None
        self.touching = False
        self.q_acc = [0, 0, 0, 0]
        self.centre_pending = False     # clip page: scroll to the notes once they arrive
        self.midi = MidiOut()
        import winshot
        self.clicker = winshot.Clicker()   # plays touches on plug-in windows with the real mouse
        self.previewer = Previewer(on_error=self.live.toast)   # Browse: auditions samples from your Places
        self.live_previewing = False    # Live's own browser preview is playing (items from Live's browser)
        self.places_index = None        # every sample in your Places folders, for search (built when first needed)
        self.pre_search = None          # where the Browse page was before a search (Back returns there)
        self.last_query = ""
        self.sounding = {}              # pad -> note it's playing (note-off must match even if the octave changed)
        self.voices = {}                # held chord voices, captured at note-on
        self.repeat_vel = {}            # pad -> velocity for Note Repeat (pad pressure changes it, like the MPC)
        self.last_note = 60             # last note the pads played (16 Levels uses it)
        self.auto_armed = None          # track this app armed for playing (disarmed again when you move on)
        self.no_port_toast = 0.0
        self.last_send = {}
        self.quit = False
        self.last_tap = None            # (action, time) of the last tap on a fader / pan / send
        self.copy_pasted = False        # COPY held: something was pasted / duplicated during this press
        self.copy_src_at_press = None
        self.scr = self.ctl = None
        self.load_settings()

    # ------------------------------------------------------------ main loop

    def run(self, seconds=None, snapshots=False):
        """Release host resources even if device initialization or shutdown fails."""
        try:
            self._run_session(seconds, snapshots)
        finally:
            self.quit = True
            try:
                with self.lock:
                    self.all_notes_off()
                self.previewer.stop()
            finally:
                try:
                    self.clicker.up()
                finally:
                    try:
                        self.midi.close()
                    finally:
                        self.live.stop()

    def _run_session(self, seconds=None, snapshots=False):
        """snapshots: save the screen to logs/ui-live.png every 3 s (for checking it from the PC)."""
        from mpc_control import MpcControl
        from mpc_screen import MpcScreen
        self.live.start()
        t0 = time.time()
        frames, busy, last_snap, last_port_check = 0, 0.0, 0.0, 0.0
        self.last_pass = time.time()
        threading.Thread(target=self.watchdog, daemon=True).start()
        threading.Thread(target=self.plugin_capture_loop, daemon=True).start()
        threading.Thread(target=self.repeat_loop, daemon=True).start()
        threading.Thread(target=self.dialog_capture_loop, daemon=True).start()
        self.ui.midi_port = self.midi.open()
        print("Pads play into: %s" % (self.ui.midi_port or "nothing (no virtual MIDI port such as loopMIDI found)"),
              flush=True)
        with MpcScreen(self.on_screen) as scr, MpcControl(self.on_midi) as ctl:
            self.scr, self.ctl = scr, ctl
            print("MPC One Push running. Ctrl+C or SHIFT+MENU on the MPC to quit.", flush=True)
            try:
                last = 0.0
                while not self.quit and (seconds is None or time.time() - t0 < seconds):
                    self.dirty.wait(0.05)
                    now = time.time()
                    # Redraw when something changed (at most 25 fps), and 4x a second anyway for
                    # blinking clips and toasts. Pads and LEDs update every pass so pulses stay smooth.
                    if (self.dirty.is_set() and now - last >= FRAME_S) or now - last >= 0.25:
                        self.dirty.clear()
                        last = now
                        self.frame()
                        frames += 1
                        busy += time.time() - now
                    self.check_long_press()
                    self.update_pads()
                    self.update_leds()
                    if snapshots and self.prev is not None and now - last_snap >= 3:
                        last_snap = now
                        self.prev.save(os.path.join(HERE, "..", "logs", "ui-live.png"))
                    if not self.ui.midi_port and now - last_port_check >= 5:   # pick up loopMIDI once it exists
                        last_port_check = now
                        self.ui.midi_port = self.midi.open()
                        if self.ui.midi_port:
                            print("Pads now play into:", self.ui.midi_port, flush=True)
                            self.live.toast("Pads connected to %s" % self.ui.midi_port)
                    if now - getattr(self, "_settings_at", 0) >= 3:     # keep settings.json up to date
                        self._settings_at = now
                        self.save_settings()
                    if os.path.exists(STOP_FILE):       # lets another process ask for a clean exit
                        os.remove(STOP_FILE)
                        self.quit = True
                    self.last_pass = time.time()
            except KeyboardInterrupt:
                pass
            finally:
                self.save_settings()
                self.shutdown()
        if frames:
            print("%d frames, %.1f ms each on average" % (frames, busy * 1000 / frames), flush=True)

    def plugin_capture_loop(self):
        """While a plug-in is selected on the Device page, capture its Live window ~6x a second and hand the
        renderer a copy scaled to fit (at most 2x, so small plug-ins stay sharp enough to read)."""
        import winshot
        from PIL import Image
        hwnd, key, found_at = None, None, 0.0
        while not self.quit:
            time.sleep(0.16)
            ui, live, r = self.ui, self.live, self.renderer
            dev = live.dev
            info = dev.selected_info() if dev.names else ("",)
            if ui.page != "device" or dev.view != "devices" or not ui.plugin_ui or info[0] not in \
                    ("PluginDevice", "AuPluginDevice"):
                r.plugin_frame, hwnd, key = None, None, None
                continue
            tr = live.tracks[dev.track] if 0 <= dev.track < len(live.tracks) else None
            want = (dev.names[dev.idx], tr.name if tr else "")
            try:
                if want != key or hwnd is None or time.time() - found_at > 2:   # re-find now and then
                    hwnd, key, found_at = winshot.find_plugin_window(*want), want, time.time()
                img = winshot.capture(hwnd) if hwnd else None
            except Exception as e:
                print("plug-in capture failed: %r" % e, flush=True)
                img = None
            if img is None:
                r.plugin_frame, r.plugin_hwnd = None, None
            else:
                x0, y0, x1, y1 = r.plugin_area()
                s = min((x1 - x0) / img.width, (y1 - y0) / img.height, 2.0)
                r.plugin_frame = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                                            Image.BILINEAR)
                r.plugin_hwnd, r.plugin_scale = hwnd, s
            self.dirty.set()

    def user_quit(self):
        """Quit because you asked (SHIFT+MENU / MENU > Quit): auto-start then waits until the MPC is unplugged
        or Live is closed before starting the app again."""
        try:
            with open(QUIT_FILE, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass
        self.quit = True

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        choices = {"page": PAGES, "pads": ("play", "launch"), "note_layout": ("chromatic", "scale"),
                   "clip_mode": ("notes", "auto", "details"), "mix_view": ("mix", "sends", "io"),
                   "mix_target": ("vol", "pan"), "fixed_len": FIXED_LENGTHS}
        choices.update(chord_mode=tuple(CHORDS), velocity_curve=("linear", "soft", "hard"),
                       pressure_mode=("poly", "channel", "mod wheel", "off"))
        for k in SETTINGS:
            if k in saved and type(saved[k]) is type(getattr(self.ui, k)):
                value = saved[k]
                if k in choices and value not in choices[k]:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(value):
                        continue
                    if k in ("pad_base", "drum_base") and not 0 <= value <= 112:
                        continue
                    if k in ("grid", "repeat_rate", "arr_len") and not 0 < value <= 1024:
                        continue
                if k == "preset_sub" and not all(isinstance(a, str) and isinstance(b, list)
                                                  and all(isinstance(v, str) for v in b) for a, b in value.items()):
                    continue
                setattr(self.ui, k, saved[k])
        self.ui.help = False
        self._settings_saved = json.loads(json.dumps({k: getattr(self.ui, k) for k in SETTINGS}))

    def save_settings(self):
        now = {k: getattr(self.ui, k) for k in SETTINGS}
        if now == getattr(self, "_settings_saved", None):
            return
        try:
            import tempfile
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(SETTINGS_FILE),
                                                 prefix=".settings-", suffix=".tmp", delete=False) as f:
                    temp_path = f.name
                    json.dump(now, f, indent=1, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, SETTINGS_FILE)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            self._settings_saved = json.loads(json.dumps(now))
        except (OSError, ValueError) as e:
            print("couldn't save settings: %r" % e, flush=True)

    def dialog_capture_loop(self):
        """Whenever Live shows a dialog (Save changes?, Export, Open, Save As...), mirror it on the MPC ~6x a
        second; touches on it are played on the real dialog like plug-in windows."""
        import winshot
        from PIL import Image
        r = self.renderer
        while not self.quit:
            time.sleep(0.16)
            try:
                hwnd = winshot.live_modal_dialog()
                img = winshot.capture(hwnd) if hwnd else None
            except Exception as e:
                print("dialog capture failed: %r" % e, flush=True)
                hwnd, img = None, None
            if img is None:
                if r.dialog_frame is not None:
                    r.dialog_frame, r.dialog_hwnd = None, None
                    self.dirty.set()
                continue
            x0, y0, x1, y1 = r.DIALOG_AREA
            s = min((x1 - x0) / img.width, (y1 - y0) / img.height, 2.0)
            r.dialog_frame = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BILINEAR)
            r.dialog_hwnd, r.dialog_scale = hwnd, s
            self.dirty.set()

    def watchdog(self):
        """If the main loop stops for 5 s, write every thread's stack to logs/hang-*.txt (once per stall)."""
        import faulthandler
        dumped = False
        while not self.quit:
            time.sleep(1)
            stalled = time.time() - self.last_pass > 5
            if stalled and not dumped:
                path = os.path.join(HERE, "..", "logs", "hang-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
                with open(path, "w") as f:
                    faulthandler.dump_traceback(file=f, all_threads=True)
                print("main loop stalled; stacks written to", os.path.normpath(path), flush=True)
            dumped = stalled

    def shutdown(self):
        self.stop_preview()
        for p in range(16):
            self.ctl.pad_color(p, 0, 0, 0)
        for note, name in self.buttons.items():
            if name not in NO_LED:
                self.ctl.send(bytes([0xB0, note, LED_OFF]), port=1)
        from PIL import Image, ImageDraw
        from push_ui import font
        img = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(img).text((W // 2, H // 2), "MPC One Push stopped", font=font(48), fill=(120, 120, 120),
                                 anchor="mm")
        self.scr.draw_full(img.tobytes(*SCREEN_RAW))
        time.sleep(0.2)

    def centre_roll(self):
        """Once a newly opened clip's notes arrive, scroll the piano roll to them."""
        ui, live = self.ui, self.live
        if self.centre_pending and ui.page == "clip" and live.notes:
            pitches = [n[0] for n in live.notes]
            mid = (min(pitches) + max(pitches)) // 2
            ui.pitch_lo = max(0, min(127 - ROLL_ROWS + 1, mid - ROLL_ROWS // 2))
            ui.focus_pitch = min(pitches)
            self.centre_pending = False

    def auto_arm(self):
        """Push-style: while the pads play notes, the selected MIDI track is armed so you hear them.
        Only tracks this app armed get disarmed again; arms you set yourself are left alone."""
        live, ui = self.live, self.ui
        if ui.pad_mode() != "play" or not ui.midi_port or not live.connected:
            return
        t = live.sel
        tr = live.tracks[t] if 0 <= t < len(live.tracks) else None
        if self.auto_armed is not None and self.auto_armed != t:
            old = live.tracks[self.auto_armed] if self.auto_armed < len(live.tracks) else None
            if old and old.arm:
                live.toggle("arm", self.auto_armed)
            self.auto_armed = None
        if tr and tr.midi_in and tr.armable and not tr.arm and self.auto_armed is None:
            live.toggle("arm", t)
            self.auto_armed = t

    def follow_playhead(self):
        ui, live = self.ui, self.live
        if ui.page == "arrange" and ui.arr_follow and live.playing:
            t = live.song_time
            if t < ui.arr_start or t > ui.arr_start + ui.arr_len * 0.85:
                bpb = live.beats_per_bar()
                ui.arr_start = max(0.0, (t - ui.arr_len * 0.15) // bpb * bpb)

    def frame(self):
        with self.lock:
            self.live.mode = self.ui.page
            self.live.view_tracks = (self.ui.bank, self.ui.bank + COLS)
            self.live.arr_view = (self.ui.arr_start, self.ui.arr_start+self.ui.arr_len)
            self.live.mix_view, self.live.mix_returns = self.ui.mix_view, self.ui.mix_returns
            self.live.copy_recordings = self.ui.rec_to_arr
            if not getattr(self, "_page_ready", False) and self.live.connected and self.live.num_tracks > 0:
                self._page_ready = True             # the remembered page: open it properly once Live is there
                self.set_page(self.ui.page)
            self.live.need_time = self.ui.repeat_held or self.ui.repeat_latch
            self.live.want_details = self.ui.page == "clip" and self.ui.clip_mode == "details"
            self.follow_playhead()
            self.centre_roll()
            self.auto_arm()
            self.sync_knob_layout()
            if self.ui.sheet and self.ui.sheet.get("main_controls"):
                self.main_controls_sheet()
            try:
                img, self.targets = self.renderer.render(self.live, self.ui)
            except Exception:                       # a drawing bug skips a frame instead of stopping the app
                import traceback
                now = time.time()
                if now - getattr(self, "_render_err_at", 0) > 5:    # log it, but not 25 times a second
                    self._render_err_at = now
                    traceback.print_exc()
                    self.live.toast("Screen error on this page (logged); the rest keeps working")
                return
        if self.prev is None:
            self.scr.draw_full(img.tobytes(*SCREEN_RAW))
        else:
            diff = ImageChops.difference(img, self.prev)
            if diff.getbbox():
                for y in range(0, H, BAND):     # one rectangle per changed band, so far-apart changes stay small
                    bb = diff.crop((0, y, W, min(H, y + BAND))).getbbox()
                    if bb:
                        box = (bb[0], y + bb[1], bb[2], y + bb[3])
                        self.scr.draw(box[0], box[1], box[2] - box[0], box[3] - box[1], img.crop(box).tobytes(*SCREEN_RAW))
        self.prev = img

    # ------------------------------------------------------------ LEDs

    def pad_target(self, pad):
        row, col = divmod(pad, 4)          # row 0 = bottom
        return self.ui.ring_tracks()[col], self.ui.ring_scenes()[3 - row]

    # Clip page: the pads are 16 steps of the focused pitch, read like text (top-left = step 1).
    def pad_step(self, pad):
        row, col = divmod(pad, 4)
        a = self.live.clip_range()[0]
        return a + (self.ui.step_page * 16 + (3 - row) * 4 + col) * self.ui.grid

    def step_note(self, start):
        g = self.ui.grid
        for n in self.live.notes:
            if n[0] == self.ui.focus_pitch and start - g / 2 <= n[1] < start + g / 2:
                return n
        return None

    def update_step_pads(self):
        live, ui = self.live, self.ui
        tr = live.tracks[live.clip[0]] if live.clip and live.clip[0] < len(live.tracks) else None
        col = rgb(live.clip_info.get("color") or (tr.color if tr else 0x808080))
        a, b = live.clip_range()
        pos = live.clip_info.get("playing_position")
        playing = live.playing and tr is not None and tr.playing == live.clip[1]
        for pad in range(16):
            start = self.pad_step(pad)
            c = (0, 0, 0)
            if live.clip_exists() and start < b:
                n = self.step_note(start)
                if playing and isinstance(pos, (int, float)) and start <= pos < start + ui.grid:
                    c = (255, 255, 255)
                elif n:
                    c = scale(col, 0.3 + 0.7 * min(127, n[3]) / 127)
                elif abs(start - round(start)) < 1e-6:
                    c = (24, 24, 24)            # dim marker on each beat
            c7 = tuple(v >> 1 for v in c)
            if self.pads_sent[pad] != c7:
                self.pads_sent[pad] = c7
                self.ctl.pad_color(pad, *c7)

    def update_play_pads(self):
        """Play mode: C notes in the selected track's colour, other white keys dim, black keys dark,
        pads being held white."""
        live, ui = self.live, self.ui
        tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
        col = rgb(tr.color) if tr else (255, 118, 40)
        layout = self.note_layout()
        root = live.scale[0]
        for pad in range(16):
            note = self.pad_note(pad)
            if pad in self.sounding:
                c = (255, 255, 255)
            elif not 0 <= note <= 127:
                c = (0, 0, 0)
            elif ui.levels:                         # 16 Levels: brighter = harder
                c = scale(col, 0.08 + 0.92 * pad / 15)
            elif layout == "drum":                  # filled Drum Rack pads in their own colour, empty ones dark
                dp = live.drum_pads.get(note)
                c = (rgb(dp[1]) if dp[1] else col) if dp else scale(col, 0.03)
            elif layout == "scale":                 # in key: the root bright, the other scale notes dimmer
                c = col if (note - root) % 12 == 0 else scale(col, 0.2)
            elif note % 12 == 0:
                c = col
            elif note % 12 in (1, 3, 6, 8, 10):
                c = scale(col, 0.06)
            else:
                c = scale(col, 0.22)
            c7 = tuple(v >> 1 for v in c)
            if self.pads_sent[pad] != c7:
                self.pads_sent[pad] = c7
                self.ctl.pad_color(pad, *c7)

    def update_pads(self):
        live = self.live
        now = time.time()
        pulse = 0.25 + 0.75 * (0.5 + 0.5 * math.cos(now * 2 * 3.14159 * 1.5))
        blink = int(now * 4) % 2 == 0
        with live.lock:
            mode = self.ui.pad_mode()
            if mode == "step":
                return self.update_step_pads()
            if mode == "play":
                return self.update_play_pads()
            for pad in range(16):
                t, s = self.pad_target(pad)
                col = (0, 0, 0)
                if t < len(live.tracks) and s < live.num_scenes:
                    tr = live.tracks[t]
                    clip = tr.clips[s] if s < len(tr.clips) else (False, "", 0)
                    if clip[0]:
                        col = rgb(clip[2] or tr.color)
                        if tr.playing == s:
                            col = scale(col, pulse)
                        elif tr.fired == s:
                            col = col if blink else (0, 0, 0)
                    elif tr.arm:
                        col = (60, 0, 0) if tr.fired != s or blink else (255, 0, 0)
                    elif tr.fired == s and blink:
                        col = (60, 60, 60)
                c7 = tuple(v >> 1 for v in col)
                if self.pads_sent[pad] != c7:
                    self.pads_sent[pad] = c7
                    self.ctl.pad_color(pad, *c7)

    def update_leds(self):
        ui, live = self.ui, self.live
        page_btn = PAGE_BUTTONS
        state = {
            "PLAY": live.playing, "REC": live.rec_mode if ui.page == "arrange" else live.rec,
            "OVERDUB": live.overdub, "SHIFT": ui.shift, "MENU": ui.help or bool(ui.sheet),
            "ERASE": ui.erase_held, "COPY": ui.copy_held or bool(ui.copy_src),
            "NOTE REPEAT": ui.repeat_held or ui.repeat_latch, "16 LEVEL": ui.levels,
            "Q-LINK (bank button)": (ui.qlink_half > 0) if ui.page == "device" else ui.mix_target == "pan",
            "TRACK MUTE": ui.pads == "launch", "FULL LEVEL": ui.full_level,
        }
        mode = ui.pad_mode()
        bank_lit = {"step": "ABCD"[ui.step_page % 4], "launch": "ABCD"[ui.quad % 4],
                    "play": next((k for k, v in (DRUM_BANKS if self.note_layout() == "drum" else PAD_BANK_BASES).items()
                                 if v == (ui.drum_base if self.note_layout() == "drum" else ui.pad_base)), "")}[mode]
        assigned = set(page_btn) | set(state) | {"STOP", "PLAY START", "TAP TEMPO", "UNDO", "- (minus)",
                                                 "+ (plus)", "PAD BANK A", "PAD BANK B", "PAD BANK C",
                                                 "PAD BANK D"}
        for note, name in self.buttons.items():
            if name in NO_LED:
                continue
            if name in page_btn:
                v = LED_ON if ui.page == page_btn[name] else LED_DIM
            elif name in state:
                v = LED_ON if state[name] else LED_DIM
            elif name.startswith("PAD BANK "):
                v = LED_ON if name[-1] == bank_lit else LED_DIM
            else:
                v = LED_DIM if name in assigned else LED_OFF
            if self.leds_sent.get(note) != v:
                self.leds_sent[note] = v
                self.ctl.send(bytes([0xB0, note, v]), port=1)

    # ------------------------------------------------------------ input: pads, buttons, knobs

    def on_midi(self, msg):
        if len(msg) != 3:
            return
        st, a, b = msg[0], msg[1], msg[2]
        with self.lock:
            if st == 0x99 and b > 0 and a in PAD_NOTES:
                self.pad(PAD_NOTES.index(a), b)
            elif st in (0x89, 0x99) and a in PAD_NOTES:      # pad released
                self.pad_off(PAD_NOTES.index(a))
            elif st == 0xA9 and a in PAD_NOTES:             # pad pressure -> poly aftertouch
                pad = PAD_NOTES.index(a)
                note = self.sounding.get(pad)
                if note is not None:
                    if self.ui.pressure_mode == "poly":
                        for voice in self.pad_voices(pad):
                            self.midi.send(0xA0, voice, b)
                    elif self.ui.pressure_mode == "channel":
                        self.midi.send(0xD0, b, 0)
                    elif self.ui.pressure_mode == "mod wheel":
                        self.midi.send(0xB0, 1, b)
                    if b > 0 and not self.ui.levels:
                        self.repeat_vel[pad] = b
                return
            elif st & 0xF0 in (0x80, 0x90) and st & 0x0F == 0:
                self.button(self.buttons.get(a, "note 0x%02X" % a), st == 0x90 and b > 0)
            elif st == 0xB0 and a in QLINK_CC:
                self.qlink(a - 0x10, rel(b))
            elif st == 0xB0 and a == WHEEL_CC:
                self.wheel(rel(b))
            else:
                return
        self.dirty.set()

    def pad(self, pad, vel=100):
        live, ui = self.live, self.ui
        if ui.pad_mode() == "play":                 # play the selected track (layout: see pad_note)
            note = self.pad_note(pad)
            if not 0 <= note <= 127:
                return
            vel = self.pad_velocity(pad, vel)
            self.repeat_vel[pad] = vel
            if not ui.levels:
                self.last_note = note
            if live.drum_pads is not None:
                ui.last_drum = live.drum_pads.get(note, ("",))[0]
            if not ui.midi_port and time.time() - self.no_port_toast > 6:
                self.no_port_toast = time.time()
                live.toast("Pads need a virtual MIDI port (loopMIDI) to reach Live: see README")
            self.pad_off(pad)
            held_voices = {voice for other in self.sounding for voice in self.pad_voices(other)}
            self.sounding[pad] = note
            offsets = (0,) if live.drum_pads is not None or ui.levels else CHORDS[ui.chord_mode]
            self.voices[pad] = tuple(note + n for n in offsets if note + n <= 127)
            if not ui.levels:
                self.ui.drum_note = note    # samples loaded from Places land on this Drum Rack pad
            if not ui.pad_page <= note < ui.pad_page + 16:      # keep an open Drum Rack's pad page on it
                ui.pad_page = max(0, min(112, 36 + (note - 36) // 16 * 16))
            for voice in self.pad_voices(pad):
                if voice not in held_voices:
                    self.midi.send(0x90, voice, vel)
            return
        if ui.page == "clip":                       # step sequencer: toggle a note on the orange row
            if not live.clip_exists():
                return
            start = self.pad_step(pad)
            if start >= live.clip_range()[1]:
                return
            self.centre_pending = False
            n = self.step_note(start)
            if n:
                live.remove_note(n)
                self.ui.sel_note = None
            else:
                live.add_note(self.ui.focus_pitch, start, self.ui.grid, vel)
                self.ui.sel_note = live.notes[-1]
            return
        t, s = self.pad_target(pad)
        if t >= len(live.tracks) or s >= live.num_scenes:
            return
        if self.ui.shift:
            self.edit_clip(t, s)
        else:
            live.fire_clip(t, s)

    def pad_off(self, pad):
        notes = self.pad_voices(pad)
        note = self.sounding.pop(pad, None)
        getattr(self, "voices", {}).pop(pad, None)
        if note is not None:
            others = {n for other in self.sounding for n in self.pad_voices(other)}
            for voice in notes:
                if voice not in others:
                    self.midi.send(0x80, voice, 0)
        if not self.sounding and getattr(self.ui, "pressure_mode", "poly") == "mod wheel":
            self.midi.send(0xB0, 1, 0)

    def pad_voices(self, pad):
        if pad not in self.sounding:
            return ()
        return getattr(self, "voices", {}).get(pad, (self.sounding[pad],))

    def all_notes_off(self):
        for pad in list(self.sounding):
            self.pad_off(pad)

    def edit_clip(self, t, s):
        """Open (track, slot) in the clip editor."""
        live = self.live
        live.select_track(t)
        live.send("/live/view/set/selected_clip", t, s)
        self.ui.clip_view, self.ui.sel_note, self.ui.step_page = None, None, 0
        self.centre_pending = True
        live.open_clip(t, s)
        self.set_page("clip")           # live.clip[0] == live.sel now, so this keeps the clip just opened

    def pick_clip(self, t):
        """The clip to edit on track t: its playing clip, else its first clip, else slot 0."""
        tr = self.live.tracks[t] if 0 <= t < len(self.live.tracks) else None
        if not tr:
            return 0
        if 0 <= tr.playing < len(tr.clips):
            return tr.playing
        return next((s for s, c in enumerate(tr.clips) if c[0]), 0)

    def button(self, name, down):
        ui, live = self.ui, self.live
        if name == "SHIFT":
            ui.shift = down
            return
        if name == "ERASE":
            ui.erase_held = down
            return
        if name == "NOTE REPEAT":                   # hold: held pads repeat; SHIFT: latch on/off
            if down and (ui.shift or ui.repeat_latch):
                ui.repeat_latch = not ui.repeat_latch
                live.toast("Note Repeat latched: hold pads to repeat, NOTE REPEAT again to stop"
                           if ui.repeat_latch else "Note Repeat off")
            elif not ui.repeat_latch:
                ui.repeat_held = down
            return
        if name == "COPY":
            ui.copy_held = down
            if down:
                self.copy_pasted = False
                self.copy_src_at_press = ui.copy_src
            elif ui.copy_src and not self.copy_pasted and ui.copy_src != self.copy_src_at_press:
                live.duplicate_clip(*ui.copy_src)       # picked a clip but no destination: copy it below
                ui.copy_src = None
            return
        if name.startswith("Q-LINK ") and "touch" in name:
            q = int(name.split()[1]) - 1
            if down:
                ui.qlink_touch.add(q)
                if ui.page == "device":
                    params = ui.qlink_params(live.dev)
                    if q < len(params):
                        ui.focus_param, ui.focus_at = params[q], time.time()
            else:
                ui.qlink_touch.discard(q)
            return
        if not down:
            return
        if name in PAGE_BUTTONS:
            self.set_page(PAGE_BUTTONS[name])
        elif name == "MENU":
            if ui.shift:
                self.user_quit()
            elif ui.help or ui.sheet or ui.kb or ui.colors:
                ui.help, ui.sheet, ui.kb, ui.colors = False, None, None, None
            else:
                self.open_menu()
        elif name == "PLAY":
            live.song("stop_playing" if live.playing else "start_playing")
            live.playing = not live.playing
        elif name == "STOP":
            live.song("stop_all_clips" if ui.shift else "stop_playing")
        elif name == "PLAY START":
            live.set_song_time(0.0)
            live.song("start_playing")
        elif name == "REC" and ui.shift:               # SHIFT+REC: record-arm the selected track
            if 0 <= live.sel < len(live.tracks):
                live.toggle("arm", live.sel)
        elif name == "REC":
            if ui.page == "arrange":
                live.toggle_rec_mode()
            elif ui.fixed_len and 0 <= live.sel < len(live.tracks) and live.tracks[live.sel].armable:
                self.record_next(live.sel)
            else:
                live.rec = not live.rec
                live.set_song("session_record", int(live.rec))
        elif name == "OVERDUB":
            live.overdub = not live.overdub
            live.set_song("arrangement_overdub", int(live.overdub))
        elif name == "TAP TEMPO":
            if ui.shift:
                live.metro = not live.metro
                live.set_song("metronome", int(live.metro))
            else:
                live.song("tap_tempo")
        elif name == "UNDO":
            live.song("redo" if ui.shift else "undo")
            live.toast("Redo" if ui.shift else "Undo")
        elif name == "- (minus)":
            self.br_back() if ui.page == "browse" else self.move_bank(-COLS)
        elif name == "+ (plus)":
            self.br_open(ui.br_sel) if ui.page == "browse" else self.move_bank(COLS)
        elif name == "DATA WHEEL (push)":
            if ui.page == "device" and ui.add_menu:    # load the highlighted device / plug-in
                pick = self.menu_items()[ui.menu_sel] if self.menu_items() else None
                if pick:
                    self.tap(("plugin_pick" if ui.add_tab == "plugins" else "dev_add_pick", pick))
            elif ui.page == "browse":
                self.br_open(ui.br_sel)
            elif ui.page == "session":
                live.fire_scene(ui.scene_off)
        elif name.startswith("PAD BANK "):
            mode = ui.pad_mode()
            if mode == "step":                      # step pages 1-16, 17-32, ...
                ui.step_page = "ABCD".index(name[-1])
            elif mode == "play" and self.note_layout() == "drum":     # Drum Rack: 4 banks of 16 pads
                self.all_notes_off()
                ui.drum_base = DRUM_BANKS[name[-1]]
                live.toast("Drum Rack pads %s to %s (bank %s)" % (note_name(ui.drum_base),
                                                                 note_name(ui.drum_base + 15), name[-1]))
            elif mode == "play":                    # octave of the bottom-left pad
                self.all_notes_off()
                ui.pad_base = PAD_BANK_BASES[name[-1]]
                live.toast("Pads %s to %s%s" % (note_name(ui.pad_base), note_name(ui.pad_base + 15),
                                                "  (Drum Rack pads)" if ui.pad_base == 36 else ""))
            else:
                ui.quad = "ABCD".index(name[-1])
        elif name == "TRACK MUTE":                  # pads: play notes <-> launch clips
            self.toggle_pads()
        elif name == "16 LEVEL":                    # the last note on all 16 pads, softest to hardest
            self.all_notes_off()
            ui.levels = not ui.levels
            if ui.levels:
                ui.levels_note = self.last_note
                dp = (live.drum_pads or {}).get(ui.levels_note)
                live.toast("16 Levels: %s on every pad, velocity 8 (pad 1) to 127 (pad 16)"
                           % (dp[0] if dp else note_name(ui.levels_note)))
            else:
                live.toast("16 Levels off")
        elif name == "FULL LEVEL":
            ui.full_level = not ui.full_level
            live.toast("Full level " + ("on" if ui.full_level else "off"))
        elif name == "Q-LINK (bank button)":
            if ui.page == "device":                 # move the Q-Links along the knobs on screen, 4 at a time
                groups = max(1, -(-len(live.dev.bank_params()) // 4))
                ui.qlink_half = (ui.qlink_half + 1) % groups
                qp = ui.qlink_params(live.dev)
                if qp and ui.plugin_knobs(live.dev):
                    live.toast("Q-Links: knobs %d-%d" % (qp[0] - live.dev.bank_params()[0] + 1,
                                                         qp[-1] - live.dev.bank_params()[0] + 1))
            elif ui.page == "mix" and ui.mix_view == "sends" and live.returns:   # next send
                ui.send_sel = (ui.send_sel + 1) % len(live.returns)
                live.toast("Q-Links: send %s" % live.returns[ui.send_sel].name)
            else:
                ui.mix_target = "pan" if ui.mix_target == "vol" else "vol"
                ui.mix_view = "mix" if ui.page == "mix" else ui.mix_view
                live.toast("Q-Links: " + ("pan" if ui.mix_target == "pan" else "volume"))
        else:
            live.toast("%s isn't assigned yet" % name.title())

    def qlink(self, q, delta):
        ui, live = self.ui, self.live
        if ui.page == "clip" and ui.clip_mode == "details":     # Q1-Q4 = the top row of cards
            fields = self.renderer.clip_fields(live.clip_details)
            if q < len(fields) and fields[q][1] and live.clip_details:
                self.q_acc[q] += delta
                steps = int(self.q_acc[q] / 2)                  # two clicks a step
                self.q_acc[q] -= steps * 2
                if steps:
                    key = fields[q][1]
                    live.set_clip_detail(key, self.clip_field_step(key, self.clip_field_value(key), steps * 30))
            return
        if ui.page == "clip":
            v0, vlen = ui.clip_view_range(live)
            if q == 0:                                  # scroll time
                ui.clip_view = (max(0.0, v0 + delta * vlen / 64), vlen)
            elif q == 1:                                # zoom around the middle
                nl = min(1024.0, max(1.0, vlen * 1.04 ** -delta))
                ui.clip_view = (max(0.0, v0 + (vlen - nl) / 2), nl)
            elif q == 2 and ui.clip_mode == "notes":    # scroll pitch
                ui.pitch_lo = max(0, min(127 - ROLL_ROWS + 1, ui.pitch_lo + delta))
            elif q == 3 and ui.sel_note is not None:    # velocity of the last touched note
                self.q_acc[3] += delta
                steps = int(self.q_acc[3] / 1)
                self.q_acc[3] -= steps
                if steps and ui.sel_note in live.notes:
                    ui.sel_note = live.set_note_velocity(ui.sel_note, ui.sel_note[3] + steps * 2)
                    live.toast("Velocity %d" % ui.sel_note[3])
            return
        if ui.page == "arrange":
            if q == 0:                                  # scroll
                ui.arr_follow = False
                ui.arr_start = max(0.0, ui.arr_start + delta * ui.arr_len / 64)
            elif q == 1:                                # zoom around the middle of the view
                self.arr_zoom(1.04 ** -delta)
            elif q == 2:                                # loop start, one beat a click
                live.set_loop(round(live.loop_start + delta), live.loop_len)
            else:                                       # loop length
                live.set_loop(live.loop_start, round(live.loop_len + delta))
            return
        if ui.page == "device":
            params = ui.qlink_params(live.dev)
            if q >= len(params):
                return
            p = params[q]
            d = live.dev
            if p < len(d.pq) and d.pq[p]:          # stepped parameter: one step per 3 clicks
                self.q_acc[q] += delta
                steps = int(self.q_acc[q] / 3)
                self.q_acc[q] -= steps * 3
                if steps:
                    live.nudge_param(p, steps)
            else:
                live.nudge_param(p, delta * 0.5)
            ui.focus_param, ui.focus_at = p, time.time()
        elif ui.page == "mix":
            keys = ui.qlink_strips(live)
            if q >= len(keys) or not live.strip(keys[q]):
                return
            st = live.strip(keys[q])
            if ui.mix_view == "sends":
                if ui.send_sel < len(st.sends):
                    self.set_mix("send", (keys[q], ui.send_sel), st.sends[ui.send_sel] + delta * 0.004)
            elif ui.mix_target == "pan":
                self.set_mix("pan", keys[q], st.pan + delta * 0.01)
            else:
                self.set_mix("vol", keys[q], st.vol + delta * 0.004)
        elif ui.page == "session":
            t = ui.ring_tracks()[q]
            if t >= len(live.tracks):
                return
            tr = live.tracks[t]
            if ui.mix_target == "pan" and ui.page == "mix":
                live.set_pan(t, tr.pan + delta * 0.01)
            else:
                live.set_volume(t, tr.vol + delta * 0.004)
                self.throttled("vs%d" % t, lambda: live.send("/live/track/get/volume_string", t))

    def menu_items(self):
        """What the dial moves through in the add-device menu: Live's devices or your plug-ins."""
        if self.ui.add_tab == "plugins":
            return self.live.plugins or []
        return [n for _, names in self.renderer.DEVICE_MENU for n in names]

    def wheel(self, delta):
        ui, live = self.ui, self.live
        if ui.page == "device" and ui.add_menu:        # dial through the add menu; push loads
            items = self.menu_items()
            if items:
                ui.menu_sel = max(0, min(len(items) - 1, ui.menu_sel + delta))
                per = self.renderer.PLUGINS_PER_PAGE
                if ui.add_tab == "plugins" and not ui.plugin_page <= ui.menu_sel < ui.plugin_page + per:
                    ui.plugin_page = ui.menu_sel if delta > 0 and ui.menu_sel >= ui.plugin_page + per \
                        else max(0, ui.menu_sel - per + 1)
        elif ui.shift:
            live.set_tempo(live.tempo + delta)
        elif ui.page == "arrange":                     # move the playhead by whole bars
            bpb = live.beats_per_bar()
            live.set_song_time(max(0.0, (live.song_time // bpb + delta) * bpb))
        elif ui.page == "clip":                        # move the pads' row up/down, keeping it on screen
            ui.focus_pitch = max(0, min(127, ui.focus_pitch + delta))
            if ui.focus_pitch < ui.pitch_lo:
                ui.pitch_lo = ui.focus_pitch
            elif ui.focus_pitch > ui.pitch_lo + ROLL_ROWS - 1:
                ui.pitch_lo = ui.focus_pitch - ROLL_ROWS + 1
        elif ui.page == "session":
            ui.scene_off = max(0, min(max(0, live.num_scenes - ROWS), ui.scene_off + delta))
        elif ui.page == "browse":
            n = len(live.br_items)
            if n:
                ui.br_sel = max(0, min(n - 1, ui.br_sel + delta))
                self.br_scroll_to_sel()
                self.preview_sel()
        else:
            t = max(0, min(live.num_tracks - 1, live.sel + delta))
            live.select_track(t)
            ui.bank = (t // COLS) * COLS

    # ------------------------------------------------------------ input: touch

    def on_screen(self, kind, data):
        import struct
        if not (kind == "m5" and len(data) >= 29 and data[4] == 3):
            return
        x, y = struct.unpack_from("<2f", data, 5)
        flags = struct.unpack_from("<I", data, 13)[0]
        with self.lock:
            if flags == 0x10:                          # move
                if not self.touching:                   # missed the down event
                    self.touching = True
                    self.touch_down(x, y)
                else:
                    self.touch_move(x, y)
            elif flags == 0:                            # down and up both use 00
                self.touching = not self.touching
                if self.touching:
                    self.touch_down(x, y)
                else:
                    self.touch_up(x, y)
        self.dirty.set()

    def hit(self, x, y):
        for x0, y0, x1, y1, action in reversed(self.targets):
            if x0 <= x < x1 and y0 <= y < y1:
                return action
        return None

    def touch_down(self, x, y):
        action = self.hit(x, y)
        live = self.live
        start = None
        if action and action[0] == "drag":
            kind, i = action[1], action[2]
            if kind in ("vol", "pan") and live.strip(strip_key(i)):
                st = live.strip(strip_key(i))
                start = st.vol if kind == "vol" else st.pan
            elif kind == "cfield":                      # Clip DETAILS card
                start = self.clip_field_value(i)
            elif kind == "eqband":                      # EQ Eight dot: (frequency, gain) of band i
                d = live.dev
                fi, gi = d.pidx("%d Frequency A" % i), d.pidx("%d Gain A" % i)
                if 0 <= fi < len(d.pvals) and 0 <= gi < len(d.pvals):
                    start = (d.pvals[fi], d.pvals[gi])
            elif kind == "smark" and (live.dev.sample or {}).get("file"):
                start = live.dev.sample.get(i) or 0
            elif kind == "slice" and i >= 0:
                start = i
            elif kind == "send" and live.strip(i[0]) and i[1] < len(live.strip(i[0]).sends):
                start = live.strip(i[0]).sends[i[1]]
                self.ui.send_sel = i[1]
            elif kind == "param" and i < len(live.dev.pvals):
                start = live.dev.pvals[i]
                self.ui.focus_param, self.ui.focus_at = i, time.time()
            elif kind == "tempo":
                start = live.tempo
            elif kind == "arr":
                start = self.ui.arr_start
            elif kind in ("roll", "roll_scroll"):
                start = (self.ui.clip_view_range(live), self.ui.pitch_lo)
            elif kind == "env":
                start = True
        self.touch = {"action": action, "x": x, "y": y, "moved": False, "start": start, "last": None,
                      "t": time.time()}
        if action and action[0] == 'arr_clip':
            self.touch['arr_can_move'] = self.ui.arr_selected == action[1:] and len(self.ui.arr_selection) <= 1
            self.touch['arr_origin'] = self.ui.arr_start
            self.ui.arr_follow = False
        if action and action[:2] == ('drag','arr'):
            self.ui.arr_follow = False
        if action and action[:2] == ("drag", "env"):
            self.env_draw_at(x, y)
        elif action and action[:2] == ("drag", "dialog"):     # press on Live's real dialog
            r = self.renderer
            if r.dialog_hwnd:
                why = self.clicker.down(r.dialog_hwnd, *self.dialog_xy(x, y))
                if why:
                    live.toast("Can't touch it: " + why)
        elif action and action[:2] == ("drag", "plugin"):     # press on the real plug-in window
            r = self.renderer
            if r.plugin_hwnd:
                why = self.clicker.down(r.plugin_hwnd, *self.plugin_xy(x, y))
                if why:
                    live.toast("Can't touch it: " + why)

    def dialog_xy(self, x, y):
        r = self.renderer
        return (x - r.dialog_place[0]) / r.dialog_scale, (y - r.dialog_place[1]) / r.dialog_scale

    def dialog_key(self, what):
        """The mirrored dialog's buttons: type with the MPC keyboard, Enter, Escape."""
        import winshot
        hwnd = self.renderer.dialog_hwnd
        if what == "kb":
            self.ui.kb = {"what": ("type",), "text": "", "caps": False, "title": "Type into Live's dialog"}
            return
        if hwnd:
            winshot.focus(hwnd)
            winshot.chord(winshot.VK_RETURN if what == "enter" else winshot.VK_ESCAPE)

    def plugin_xy(self, x, y):
        """MPC screen point -> pixel inside the plug-in window (as captured)."""
        r = self.renderer
        return (x - r.plugin_place[0]) / r.plugin_scale, (y - r.plugin_place[1]) / r.plugin_scale

    def touch_move(self, x, y):
        tc = self.touch
        if not tc:
            return
        dx, dy = x - tc["x"], y - tc["y"]
        if abs(dx) + abs(dy) > 12:
            tc["moved"] = True
        action, v0 = tc["action"], tc["start"]
        if tc.get('long'):
            return
        if action and action[0] == 'arr_clip' and tc['moved']:
            if tc.get('arr_can_move'):
                snap = self.ui.grid if self.ui.shift else 1.0
                at = max(0., round((action[2]+dx/self.ui.arr_ppb())/snap)*snap)
                self.ui.arr_preview = (action[1:], 'move', at)
            else:
                self.arrangement_marquee(x, y)
            return
        if action and action[:2] == ('drag','arr') and action[2] != -1 and tc['moved']:
            self.arrangement_marquee(x, y)
            return
        if action and action[:2] == ("drag", "env"):    # drawing follows the finger from the first pixel
            self.env_draw_at(x, y)
            return
        if action and action[:2] == ("drag", "plugin"):  # dragging a plug-in knob or fader
            self.clicker.move(*self.plugin_xy(x, y))
            return
        if action and action[:2] == ("drag", "dialog"):
            self.clicker.move(*self.dialog_xy(x, y))
            return
        if not (action and action[0] == "drag" and v0 is not None and tc["moved"]):
            return
        live = self.live
        kind, i = action[1], action[2]
        if kind == "cfield":                            # up = more: positions in grid steps, lists one at a time
            v = self.clip_field_step(i, v0, -dy)
            tc["cval"] = v
            self.throttled("cfield", lambda: live.set_clip_detail(i, v))
            return
        if kind == "eqband":                            # sideways = frequency, up/down = gain
            geom = self.renderer.curve_geom
            if not geom:
                return
            px0, px1, mid, half, span = geom
            d = live.dev
            fi, gi = d.pidx("%d Frequency A" % i), d.pidx("%d Gain A" % i)
            fv = max(0.0, min(1.0, v0[0] + dx / (px1 - px0)))      # the knob position is the log-frequency axis
            gv = max(-15.0, min(15.0, v0[1] - dy * span / half))
            self.throttled("eqf%d" % i, lambda: (live.set_param(fi, fv), live.set_param(gi, gv)))
            tc["eq"] = (fi, fv, gi, gv)
            self.ui.focus_param, self.ui.focus_at = fi, time.time()
            return
        if kind in ("smark", "slice"):                 # Simpler: sideways drag moves a marker / slice
            geom = self.renderer.wave_geom
            if not geom:
                return
            frame = int(max(0, min(geom[2], v0 + dx * geom[2] / geom[1])))
            if kind == "smark":
                tc["frame"] = frame                     # the release sends the last one (the throttle may skip it)
                self.throttled("smark", lambda: live.set_sample(i + "_marker", frame))
            else:                                       # slices move when you let go; show it meanwhile
                sl = (live.dev.sample or {}).get("slices", [])
                cur = tc.get("cur", i)
                if cur in sl:
                    sl[sl.index(cur)] = frame
                tc["cur"] = frame
            return
        if kind in ("vol", "pan", "send"):
            self.throttled("%s%s" % (kind, i), lambda: self.set_mix(kind, i, v0 + (dx / 56 if kind == "pan"
                                                                                else -dy / 334)))
        elif kind == "param":
            if self.ui.shift:                           # SHIFT: fine adjustment
                dy /= 4
            d = live.dev
            lo = d.pmin[i] if i < len(d.pmin) else 0.0
            hi = d.pmax[i] if i < len(d.pmax) else 1.0
            quant = i < len(d.pq) and d.pq[i]
            v = round(v0 - dy / 24) if quant else v0 - dy / 250 * (hi - lo)
            self.throttled("p%d" % i, lambda: live.set_param(i, v))
            self.ui.focus_param, self.ui.focus_at = i, time.time()
        elif kind == "tempo":
            self.throttled("tempo", lambda: live.set_tempo(v0 - dy / 4))
        elif kind == "arr":                             # drag the timeline sideways
            self.ui.arr_follow = False
            self.ui.arr_start = max(0.0, v0 - dx / self.ui.arr_ppb())
        elif kind in ("roll", "roll_scroll"):           # drag the piano roll: time sideways, pitch up/down
            (s0, vlen), p0 = v0
            self.ui.clip_view = (max(0.0, s0 - dx * vlen / (RAIL_X - KEY_W)), vlen)
            if kind == "roll":
                self.ui.pitch_lo = max(0, min(127 - ROLL_ROWS + 1, p0 + round(dy / ROLL_ROW_H)))

    def arrangement_marquee(self, x, y):
        ui, tc = self.ui, self.touch
        top = MAIN_Y+ARR_RULER_H
        x0, x1 = sorted((max(TL_X,min(RAIL_X-1,tc['x'])), max(TL_X,min(RAIL_X-1,x))))
        y0, y1 = sorted((max(top,min(top+ROWS*ARR_LANE_H-1,tc['y'])),
                         max(top,min(top+ROWS*ARR_LANE_H-1,y))))
        ui.arr_marquee = (x0,y0,max(x0+1,x1),max(y0+1,y1))
        a = ui.arr_start+(x0-TL_X)/ui.arr_ppb()
        b = ui.arr_start+(max(x0+1,x1)-TL_X)/ui.arr_ppb()
        selected = []
        for r in range(ROWS):
            t = ui.bank+r
            if t >= len(self.live.tracks):
                break
            lane = top+r*ARR_LANE_H
            if max(y0+1,y1) <= lane+4 or y0 >= lane+ARR_LANE_H-5:
                continue
            for start,length,name in self.live.tracks[t].arrangement_clips():
                if start < b and start+length > a:
                    selected.append((t,start,length,name))
        ui.arr_selection = selected
        ui.arr_selected = selected[0] if selected else None

    def arrangement_group_sheet(self):
        selection = tuple(self.ui.arr_selection)
        if not selection:
            return
        if len(selection) > 128:
            self.live.toast('Select up to 128 clips at a time')
            self.ui.arr_selection, self.ui.arr_selected = [], None
            return
        self.ui.sheet = {'title': '%d clips selected' % len(selection), 'items': [
            ('Copy selection', ('arr_group','copy',selection)),
            ('Duplicate after', ('arr_group','duplicate',selection)),
            ('Paste at playhead', ('arr_clipboard','paste',min(c[0] for c in selection),self.live.song_time)),
            ('Undo', ('arr_history','undo')), ('Redo', ('arr_history','redo')),
            ('Clear selection', ('arr_clear',))]}

    def env_draw_at(self, x, y):
        """Automation lane: write the grid step under the finger, at the height of the finger."""
        ui, live = self.ui, self.live
        if not live.clip_exists():
            return
        (v0, vlen), (a, b) = ui.clip_view_range(live), live.clip_range()
        beat = v0 + (x - KEY_W) * vlen / (RAIL_X - KEY_W)
        if not a <= beat < b:
            return
        gy0 = MAIN_Y + ROLL_RULER_H
        gy1 = gy0 + ROLL_ROWS * ROLL_ROW_H
        frac = max(0.0, min(1.0, (gy1 - 4 - y) / (gy1 - gy0 - 8)))
        lo, hi = self.env_range()
        cell = int((beat - a) // ui.grid)
        key = (cell, round(frac * 200))
        if self.touch and self.touch.get("last") == key:
            return
        if self.touch:
            self.touch["last"] = key
        live.env_draw(a + cell * ui.grid, ui.grid, lo + frac * (hi - lo))

    def env_range(self):
        live = self.live
        if live.env and live.env.get("max", 0) > live.env.get("min", 0):
            return live.env["min"], live.env["max"]
        d, p = live.env_key
        if d < 0:
            return (0.0, 1.0) if p == 0 else (-1.0, 1.0)
        dev = live.dev
        lo = dev.pmin[p] if p < len(dev.pmin) else 0.0
        hi = dev.pmax[p] if p < len(dev.pmax) else 1.0
        return lo, hi

    def roll_tap(self, x, y):
        """Piano roll: tap a note to delete it, tap an empty cell to add one grid step long."""
        ui, live = self.ui, self.live
        if not live.clip_exists():
            return
        v0, vlen = ui.clip_view_range(live)
        beat = v0 + (x - KEY_W) * vlen / (RAIL_X - KEY_W)
        row = int((y - MAIN_Y - ROLL_RULER_H) // ROLL_ROW_H)
        pitch = ui.pitch_lo + ROLL_ROWS - 1 - row
        if not 0 <= pitch <= 127:
            return
        ui.focus_pitch = pitch
        self.centre_pending = False         # the user is working here; don't jump the view
        for n in live.notes:
            if n[0] == pitch and n[1] <= beat < n[1] + n[2]:
                live.remove_note(n)
                ui.sel_note = None
                return
        a, b = live.clip_range()
        start = math.floor(beat / ui.grid) * ui.grid
        if a <= start < b:
            live.add_note(pitch, start, ui.grid, 100)
            ui.sel_note = live.notes[-1]

    def touch_up(self, x, y):
        tc, self.touch = self.touch, None
        if not tc:
            return
        action = tc["action"]
        if self.ui.arr_marquee is not None:
            self.ui.arr_marquee = None
            if self.ui.arr_selection:
                self.arrangement_group_sheet()
            return
        if action and action[0] == 'arr_clip':
            preview, self.ui.arr_preview = self.ui.arr_preview, None
            if tc.get('long'):
                return
            if tc['moved']:
                if preview and preview[2] != action[2]:
                    self.live.send('/live/arrangement/edit', *preview[0], preview[1], preview[2])
                    self.ui.arr_selected = None
                    self.ui.arr_selection = []
            else:
                self.tap(action)
            return
        if tc.get("long"):                               # the action sheet already opened
            return
        if action and action[:2] == ("drag", "plugin"):  # release on the plug-in window, pointer goes back
            self.clicker.up(*self.plugin_xy(x, y))
            return
        if action and action[:2] == ("drag", "dialog"):
            self.clicker.up(*self.dialog_xy(x, y))
            return
        if action and action[0] == "drag":
            if not tc["moved"]:
                self.tap_drag_target(action, x, y)
            elif action[1] == "cfield" and "cval" in tc:
                self.live.set_clip_detail(action[2], tc["cval"])
            elif action[1] == "eqband" and "eq" in tc:     # the last position (the throttle may skip it)
                fi, fv, gi, gv = tc["eq"]
                self.live.set_param(fi, fv)
                self.live.set_param(gi, gv)
            elif action[1] == "smark" and "frame" in tc:
                self.live.set_sample(action[2] + "_marker", tc["frame"])
            elif action[1] == "slice" and action[2] >= 0 and "cur" in tc:
                self.live.slice_op("move", action[2], tc["cur"])
            elif action[1] == "vol" and strip_key(action[2])[0] == "track":
                self.live.send("/live/track/get/volume_string", strip_key(action[2])[1])
            return
        if action and not tc["moved"]:
            self.tap(action)

    def tap_drag_target(self, action, x, y):
        kind, i = action[1], action[2]
        if kind == "cfield":                              # tap: the next choice (on/off and list cards)
            if i in ("launch_mode", "launch_quantization", "legato", "warp_mode"):
                self.live.set_clip_detail(i, self.clip_field_step(i, self.clip_field_value(i), 30))
            return
        if kind == "eqband":                              # tap a band's dot: show that band's knobs
            d = self.live.dev
            for k, (name, _ps) in enumerate(d.layout or []):
                if name == "Band %d" % i:
                    d.bank, self.ui.qlink_half = k, 0
                    self.live.request_bank_strings()
            return
        if kind == "slice":                               # Slice mode: a tap on the waveform adds a slice there
            geom = self.renderer.wave_geom
            if i < 0 and geom:
                self.live.slice_op("insert", int(max(0, min(geom[2] - 1, (x - geom[0]) * geom[2] / geom[1]))))
            return
        if kind in ("vol", "pan", "send"):                # double-tap: volume 0 dB, pan centre, send off
            now = time.time()
            last, self.last_tap = self.last_tap, (action, now)
            if last and last[0] == action and now - last[1] < DOUBLE_TAP_S:
                self.set_mix(kind, i, {"vol": 0.85, "pan": 0.0, "send": 0.0}[kind])
                self.last_tap = None
                st = self.live.strip(i[0] if kind == "send" else strip_key(i))
                self.live.toast("%s: %s" % (st.name if st else "", {"vol": "0 dB", "pan": "centre",
                                                                     "send": "send off"}[kind]))
            return
        if kind == "roll":
            self.roll_tap(x, y)
        elif kind == "arr":                               # tap the timeline = jump there (and pick the lane's track)
            ui, live = self.ui, self.live
            ui.arr_selected = None
            ui.arr_selection = []
            beat = ui.arr_start + (x - TL_X) / ui.arr_ppb()
            snap = 1.0 if ui.arr_ppb() >= 12 else live.beats_per_bar()
            live.set_song_time(round(beat / snap) * snap)
            if i >= 0:
                live.select_track(i)
        elif kind == "tempo":
            self.live.song("tap_tempo")
        elif kind == "pan":
            self.live.set_pan(i, 0.0)        # tap the pan bar = centre
        elif kind == "vol" and i < len(self.live.tracks):
            self.live.select_track(i)

    def throttled(self, key, fn, every=0.03):
        now = time.time()
        if now - self.last_send.get(key, 0) >= every:
            self.last_send[key] = now
            fn()

    def tap(self, action):
        ui, live = self.ui, self.live
        kind = action[0]
        if kind == "page":
            self.set_page(action[1])
        elif kind == "arr_clip":
            ui.arr_selected = action[1:]
            ui.arr_selection = [action[1:]]
            live.select_track(action[1])
            if ui.shift:
                self.arrangement_sheet(action[1:])
            else:
                live.set_song_time(action[2])
        elif kind == "pads_mode":
            self.toggle_pads()
        elif kind == "play":
            self.button("PLAY", True)
        elif kind == "metro":
            live.metro = not live.metro
            live.set_song("metronome", int(live.metro))
        elif kind == "rec":
            self.button("REC", True)
        elif kind in ("track", "clip", "scene") and not isinstance(action[1], str) and self.modified_tap(action):
            pass
        elif kind == "track":
            if ui.page == "clip":                   # on the clip page a track button opens that track's clip
                self.edit_clip(action[1], self.pick_clip(action[1]))
            else:
                live.select_track(action[1])
        elif kind == "bank":
            self.move_bank(action[1])
        elif kind == "stop_track":
            live.stop_track(action[1])
        elif kind == "clip" and isinstance(action[1], str):     # clip page buttons
            self.clip_action(*action[1:])
        elif kind == "clip":                                    # a session grid cell
            t, s = action[1], action[2]
            tr = live.tracks[t] if t < len(live.tracks) else None
            has = tr is not None and s < len(tr.clips) and tr.clips[s][0]
            if ui.shift:
                self.edit_clip(t, s)
            elif not has and ui.fixed_len and tr and tr.arm:   # Fixed Length: record exactly N bars here
                self.record_into(t, s, ui.fixed_len)
            else:
                live.fire_clip(t, s)
        elif kind == "scene":
            live.fire_scene(action[1])
        elif kind == "mix_target":
            ui.mix_target = action[1]
        elif kind == "mix_tab":
            if action[1] in ("vol", "pan"):
                ui.mix_view, ui.mix_target = "mix", action[1]
            else:
                ui.mix_view = action[1]
                if action[1] == "io":
                    for t in range(ui.bank, min(ui.bank + COLS, live.num_tracks)):
                        live.send("/live/mixer/get/routing", t)
        elif kind == "repeat_rate":
            ui.repeat_rate = action[1]
        elif kind == "dialog_key":
            self.dialog_key(action[1])
        elif kind == "search":
            ui.kb = {"what": ("search",), "text": self.last_query, "caps": False,
                     "title": "Search Live's browser and your Places"}
        elif kind == "simpler":
            self.simpler_action(*action[1:])
        elif kind == "param_bank_set":
            d = live.dev
            if 0 <= action[1] < d.num_banks():
                d.bank, ui.qlink_half = action[1], 0
                live.request_bank_strings()
        elif kind == "presets":
            self.open_presets(action[1])
        elif kind == "mix_returns":
            ui.mix_returns = not ui.mix_returns
            live.send("/live/mixer/get/strips", "return", 0, 8)
            live.send("/live/mixer/get/strips", "master", 0, 1)
        elif kind == "strip_toggle":
            live.toggle_strip(action[2], action[1])
        elif kind == "monitor":
            live.set_monitor(action[1], action[2])
        elif kind == "route":
            self.pick_routing(action[1], action[2])
        elif kind == "toggle":
            live.toggle(action[1], action[2])
        elif kind == "device":                       # tap a device; tap the selected rack again to open it
            d = live.dev
            if action[1] == d.idx and d.view == "devices" and d.selected_info()[2]:
                live.open_rack()
            else:
                live.select_device(action[1])
        elif kind == "device_on":
            live.toggle_device_on()
        elif kind == "dev_open":
            if live.dev.view == "chains":
                live.dev.view = "devices"
            else:
                live.open_rack()
        elif kind == "dev_add":
            ui.add_menu = not ui.add_menu
            if ui.add_menu:
                live.request_plugins()
        elif kind == "dev_add_pick":
            ui.add_menu = False
            live.add_device(action[1])
        elif kind == "dev_add_tab":
            ui.add_tab, ui.menu_sel = action[1], 0
            ui.plugin_page = 0
            live.request_plugins()
        elif kind == "plugin_page":                 # plugin_page is the index of the first plug-in shown
            n = len(live.plugins or [])
            ui.plugin_page = max(0, min(max(0, n - 1), ui.plugin_page + action[1]))
        elif kind == "plugin_letter":               # start the page at the first plug-in with that letter
            names = live.plugins or []
            ui.plugin_page = next((k for k, n in enumerate(names) if self.renderer.plugin_letter(n) == action[1]), 0)
            ui.menu_sel = ui.plugin_page
        elif kind == "plugin_pick":
            ui.add_menu = False
            live.add_plugin(action[1])
        elif kind == "dev_delete":
            live.delete_device()
        elif kind == "dev_up":
            ui.add_menu = False
            live.chain_up()
        elif kind == "dev_chain":
            live.enter_chain(action[1])
        elif kind == "dev_pad":                      # Drum Rack pad: open its chain; empty pads become the load target
            note, ci = action[1], action[2]
            ui.drum_note = note
            if ci >= 0:
                live.enter_chain(ci)
            else:
                live.toast("Empty pad %s: load a sample onto it from BROWSE (Places or Drums)" % note_name(note))
        elif kind == "pad_page":
            ui.pad_page = max(0, min(112, ui.pad_page + action[1]))
        elif kind == "plugin_ui":
            ui.plugin_ui = not ui.plugin_ui
        elif kind == "noop":
            pass
        elif kind == "param_bank":
            d = live.dev
            nb = d.bank + action[1]
            if 0 <= nb < d.num_banks():
                d.bank = nb
                live.request_bank_strings()
        elif kind == "br_root":
            self.stop_preview()
            live.br_path = [action[1]]
            ui.br_sel = ui.br_scroll = 0
            live.browse()
        elif kind == "br_back":
            self.br_back()
        elif kind == "br_item":
            ui.br_sel = action[1]
            self.br_open(action[1])
        elif kind == "preview":
            ui.preview_on = not ui.preview_on
            self.preview_sel() if ui.preview_on else self.stop_preview()
            live.toast("Preview " + ("on: turn the wheel to hear samples" if ui.preview_on else "off"))
        elif kind == "preview_loop":
            ui.preview_loop = not ui.preview_loop
            self.previewer.set_loop(ui.preview_loop)
            live.toast("Preview loop " + ("on" if ui.preview_loop else "off")
                       + ("" if live.user_place_dir() is not None or not ui.preview_loop
                          else " (Live's own browser previews play once; looping works in your Places)"))
        elif kind == "help_close":
            ui.help = False
        elif kind == "overlay_close":
            ui.sheet = ui.kb = ui.colors = None
        elif kind == "sheet":
            items = ui.sheet["items"] if ui.sheet else []
            if action[1] < len(items):
                self.sheet_do(items[action[1]][1])
        elif kind == "kb":
            self.kb_key(action[1])
        elif kind == "color_pick":
            what = ui.colors["what"] if ui.colors else None
            ui.colors = None
            if what:
                if what[0] == 'arr_clip':
                    live.send('/live/arrangement/set', *what[1:], 'color_index', action[1])
                else:
                    live.set_color(what, action[1])
        elif kind == "add_track":
            ui.sheet = {"title": "New track", "items": [("MIDI track", ("new_track", "midi", -1)),
                                                        ("Audio track", ("new_track", "audio", -1)),
                                                        ("Return track", ("new_track", "return", -1))]}
        elif kind == "add_scene":
            live.new_scene(-1)
        elif kind == "arr":
            self.arr_action(action[1])

    def arr_action(self, act):
        ui, live = self.ui, self.live
        if act == "loop":
            live.toggle_loop()
        elif act == "follow":
            ui.arr_follow = not ui.arr_follow
        elif act == "prev_cue":
            live.song("jump_to_prev_cue")
        elif act == "next_cue":
            live.song("jump_to_next_cue")
        elif act == "add_cue":
            live.send("/live/song/cue_point/add_or_delete")
            live.toast("Locator set / removed at the playhead")
        elif act == "zoom_out":
            self.arr_zoom(2.0)
        elif act == "zoom_in":
            self.arr_zoom(0.5)
        elif act == "back":
            live.set_song("back_to_arranger", 0)
        elif act == "auto_arm":
            live.toggle_auto_arm()
        elif act == "reenable":
            live.song("re_enable_automation")
            live.toast("Automation re-enabled")
        elif act in ("scroll_back", "scroll_fwd"):
            ui.arr_follow = False
            ui.arr_start = max(0.0, ui.arr_start + (ui.arr_len / 2) * (-1 if act == "scroll_back" else 1))

    def clip_action(self, act, arg=None):
        ui, live = self.ui, self.live
        t, s = live.clip if live.clip else (live.sel, 0)
        if act == "mode_notes":
            ui.clip_mode = "notes"
        elif act == "mode_details":
            ui.clip_mode = "details"
            live.want_details = True
            live.request_clip()
        elif act == "loop_toggle":
            live.set_clip_detail("looping", int(not live.clip_details.get("looping")))
        elif act == "warp_toggle":
            live.set_clip_detail("warping", int(not live.clip_details.get("warping")))
        elif act == "crop":
            live.crop_clip()
            live.toast("Cropped to the %s (UNDO brings it back)" % ("loop" if live.clip_details.get("looping")
                                                                   else "start/end markers"))
        elif act == "tool":
            op, amount = arg
            live.note_tool(op, amount)
            live.toast({"transpose": "Notes %+d semitones" % amount, "velocity": "Velocities %+d" % amount,
                        "humanize": "Humanized timing and velocity", "legato": "Notes made legato"}[op])
        elif act == "mode_auto":
            ui.clip_mode = "auto"
            live.load_devices()                 # parameters of the clip track's device, for the lane picker
            live.request_clip()
        elif act == "grid":
            grids = [g for g, _ in GRIDS]
            ui.grid = grids[(grids.index(ui.grid) + 1) % len(grids)] if ui.grid in grids else 0.25
        elif act == "quantize":
            live.send("/live/clip/quantize", t, s, QUANTIZE_GRID.get(ui.grid, 5), 1.0)
            live.toast("Quantized to " + dict(GRIDS).get(ui.grid, "1/16"))
            live.request_clip()                 # Live handles messages in order, so this sees the result
        elif act == "duplicate":
            live.send("/live/clip/duplicate_loop", t, s)
            ui.clip_view = None
            live.request_clip()
            live.toast("Loop duplicated")
        elif act == "clear":
            live.clear_notes()
            live.toast("Notes cleared (UNDO brings them back)")
        elif act == "auto_arm":
            live.toggle_auto_arm()
        elif act == "reenable":
            live.song("re_enable_automation")
            live.toast("Automation re-enabled")
        elif act == "create":
            live.create_clip(t, s, arg * live.beats_per_bar())
            tr = live.tracks[t] if t < len(live.tracks) else None
            if tr and s < len(tr.clips):
                tr.clips[s] = (True, "", 0)     # show it now; the next poll fills in name and colour
            live.clip_info = {"loop_start": 0.0, "loop_end": arg * live.beats_per_bar()}
        elif act == "focus":
            ui.focus_pitch = arg
        elif act in ("oct_up", "oct_down"):
            ui.pitch_lo = max(0, min(127 - ROLL_ROWS + 1, ui.pitch_lo + (12 if act == "oct_up" else -12)))
        elif act == "env_key":
            live.set_env_key(arg)
        elif act == "env_clear":
            live.env_clear()
            live.toast("Automation cleared")

    def arr_zoom(self, factor):
        ui = self.ui
        mid = ui.arr_start + ui.arr_len / 2
        ui.arr_len = min(4096.0, max(4.0, ui.arr_len * factor))
        ui.arr_start = max(0.0, mid - ui.arr_len / 2)

    # ------------------------------------------------------------ helpers

    def toggle_pads(self):
        ui = self.ui
        self.all_notes_off()
        ui.pads = "launch" if ui.pads == "play" else "play"
        self.live.toast("Pads: " + ("launch clips (white block on Session)" if ui.pads == "launch"
                                    else "play notes from " + note_name(ui.pad_base)))

    def set_page(self, page):
        ui, live = self.ui, self.live
        if page != "browse":
            self.stop_preview()
        ui.page = page
        live.mode = page
        ui.help = False
        if page == "device":
            if live.dev.track == live.sel and live.dev.loaded:   # same track: stay where you were in its racks
                live.send("/live/chain/get/devices", live.dev.track, live.dev.chain)
                live._load_params()
            else:
                live.load_devices()
        elif page == "clip":
            if not live.clip or live.clip[0] != live.sel:    # follow the selected track
                ui.clip_view, ui.sel_note, ui.step_page = None, None, 0
                self.centre_pending = True
                live.open_clip(live.sel, self.pick_clip(live.sel))
            else:
                live.request_clip()
            if ui.clip_mode == "auto":
                live.load_devices()
        elif page == "browse" and not live.br_items:
            live.browse()
        elif page == "mix":
            for t in range(ui.bank, min(ui.bank + COLS, live.num_tracks)):
                live.send("/live/track/get/volume_string", t)
                live.send("/live/track/get/panning", t)

    def move_bank(self, step):
        n = max(1, self.live.num_tracks)
        nb = self.ui.bank + step
        if 0 <= nb < n:
            self.ui.bank = nb

    def br_back(self):
        live, ui = self.live, self.ui
        path = live.br_path
        if not path:
            return
        self.stop_preview()
        if path[0] == SEARCH:                       # back from search results: where you were before
            live.br_path = list(self.pre_search or [])
            ui.br_sel = ui.br_scroll = 0
            live.browse()
            return
        if len(path) == 1 and (path[0].startswith(("place:", "userplace:")) or path[0] == "current_project"):
            live.br_path = [PLACES]                 # back from a Places folder lands on the Places list
        else:
            path.pop()
        ui.br_sel = ui.br_scroll = 0
        live.browse()

    def br_open(self, i):
        live, ui = self.live, self.ui
        if not 0 <= i < len(live.br_items):
            return
        raw = live.br_items[i]
        if live.br_path[:1] == [SEARCH]:            # a search result: load it from where it lives
            if i < len(live.search_hits):
                self.stop_preview()
                kind, where = live.search_hits[i]
                tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
                if kind == "place":
                    live.send("/live/track/load_sample", live.sel, where, int(ui.drum_note))
                else:
                    live.send("/live/browser/load_path", live.sel, *where)
                live.toast("Loading %s on %s…" % (raw, tr.name if tr else "track %d" % (live.sel + 1)))
            return
        if live.br_root and raw.startswith("userplace:"):     # one of your own Places folders (read from disk)
            name = raw[len("userplace:"):]
            info = live.places_info.get(name, {})
            if not info.get("exists"):
                live.toast("%s isn't connected (%s)" % (name, info.get("path", "?")))
                return
            live.br_path = [raw]
        elif live.br_root:                          # a root starts a new path
            live.br_path = [raw]
        elif raw.startswith("[F] "):
            live.br_path.append(raw[4:])
        else:
            self.stop_preview()
            tr = live.tracks[live.sel] if 0 <= live.sel < len(live.tracks) else None
            if live.load_item(raw, drum_note=ui.drum_note):
                live.toast("Loading %s on %s…" % (raw, tr.name if tr else "track %d" % (live.sel + 1)))
            return
        self.stop_preview()
        ui.br_sel = ui.br_scroll = 0
        live.browse()

    # ------------------------------------------------------------ editing the set: sheets, keyboard, copy

    def sync_knob_layout(self):
        """16 knobs a page on the plug-in knob screen, 8 everywhere else; keeps the Q-Links on knobs that exist."""
        ui, dev = self.ui, self.live.dev
        size = GRID_PAGE if ui.plugin_knobs(dev) else BANK
        if dev.page_size != size:
            dev.set_page_size(size)
            self.live.request_bank_strings()
        ui.qlink_half %= max(1, -(-len(dev.bank_params()) // 4))

    def set_mix(self, kind, i, v):
        """Volume / pan of any mixer strip, or a send: kind "vol" / "pan" with a strip, "send" with (strip, n)."""
        live = self.live
        if kind == "send":
            live.set_strip(i[0], "send%d" % i[1], v)
            return
        key = strip_key(i)
        if key[0] == "track":                          # tracks keep the original path (and its volume text)
            if kind == "vol":
                live.set_volume(key[1], v)
                self.throttled("vs%d" % key[1], lambda: live.send("/live/track/get/volume_string", key[1]))
            else:
                live.set_pan(key[1], v)
        else:
            live.set_strip(key, "volume" if kind == "vol" else "panning", v)

    def simpler_action(self, what, arg=None):
        live, ui = self.live, self.ui
        smp = live.dev.sample or {}
        if what == "warp":
            live.set_sample("warping", not smp.get("warp"))
        elif what == "mode":
            live.set_sample("playback_mode", arg)
            if arg == 2:                                # slices start at C1 in Simpler, like PAD BANK C
                self.all_notes_off()
                ui.pad_base = PAD_BANK_BASES["C"]
                live.toast("Slice mode: pads play slices 1-16 (PAD BANK C). Tap the waveform to add a slice")
        elif what == "slice_style":
            style = (smp.get("style", 0) + 1) % len(self.renderer.SLICE_STYLES)
            live.set_sample("slicing_style", style)
            live.toast("Slicing by " + self.renderer.SLICE_STYLES[style])
        elif what == "slice_clear":
            live.slice_op("clear")
        elif what == "slice_reset":
            live.slice_op("reset")

    PRESETS_PER_PAGE = 33

    def open_presets(self, page):
        """The selected plug-in's own programs as a sheet, a page at a time."""
        live, ui = self.live, self.ui
        d = live.dev
        name = d.names[d.idx] if 0 <= d.idx < len(d.names) else "This plug-in"
        if d.selected_info()[0] not in ("PluginDevice", "AuPluginDevice"):     # stock device: Live's presets
            self.browse_presets(ui.preset_sub.get(d.selected_info()[0], []), page)
            return
        if len(d.presets) <= 1:
            # VST3 plug-ins (Phase Plant, Acid V, ...) give Live only "Default"; VST2 ones list their programs
            live.toast("%s keeps its presets in its own window: tap KNOBS and use its preset browser by touch"
                       % name)
            return
        per = self.PRESETS_PER_PAGE
        page = max(0, min(page, (len(d.presets) - 1) // per))
        first = page * per
        items = [(("> " if i == d.preset_sel else "") + p, ("set_preset", i))
                 for i, p in enumerate(d.presets[first:first + per], start=first)]
        if page > 0:
            items.append(("< Previous", ("preset_page", page - 1)))
        if first + per < len(d.presets):
            items.append(("Next >", ("preset_page", page + 1)))
        ui.sheet = {"title": "%s  ·  presets %d-%d of %d" % (name, first + 1, min(len(d.presets), first + per),
                                                             len(d.presets)), "items": items}

    def browse_presets(self, sub, page=0):
        """The selected stock device's presets from Live's browser, folder by folder; tapping one swaps it in
        and keeps the list open, so you can try one after another."""
        live, ui = self.live, self.ui
        d = live.dev
        cls = d.selected_info()[0]
        name = d.names[d.idx] if 0 <= d.idx < len(d.names) else "Device"

        def show(items):
            if items is None:
                if sub:                                 # the folder went away: start at the top
                    ui.preset_sub[cls] = []
                    self.browse_presets([], 0)
                else:
                    live.toast("Live's browser has no presets for %s" % name)
                return
            ui.preset_sub[cls] = list(sub)
            per = self.PRESETS_PER_PAGE - 3
            p = max(0, min(page, (len(items) - 1) // per if items else 0))
            shown = items[p * per:(p + 1) * per]
            entries = [("< Back", ("preset_dir", sub[:-1], 0))] if sub else []
            for it in shown:
                if it.startswith("[F] "):
                    entries.append(("> " + it[4:], ("preset_dir", sub + [it[4:]], 0)))
                else:
                    label = it.rsplit(".", 1)[0]
                    entries.append((("* " if label == ui.preset_loaded else "") + label, ("preset_load", sub, it)))
            if p > 0:
                entries.append(("< Previous page", ("preset_dir", sub, p - 1)))
            if (p + 1) * per < len(items):
                entries.append(("Next page >", ("preset_dir", sub, p + 1)))
            ui.sheet = {"title": "%s presets%s" % (name, "".join("  >  " + s for s in sub)), "items": entries}
            self.dirty.set()
        live.ask_device_presets(sub, show)

    def run_search(self, query):
        """Search your Places samples (on disk, indexed the first time) and Live's browser (the add-on's index)."""
        live, ui = self.live, self.ui
        self.last_query = query
        if live.br_path[:1] != [SEARCH]:
            self.pre_search = list(live.br_path)
        self.stop_preview()
        if live.index_state[0] != "done":           # e.g. Live was restarted: (re)build its index
            live.send("/live/browser/index/start")
        ui.br_sel = ui.br_scroll = 0
        live.br_path, live.br_items, live.br_pending, live.br_root = [SEARCH, query], [], True, False

        def work():
            if self.places_index is None:
                try:
                    self.places_index = places.sample_index()
                except OSError:
                    self.places_index = []
            hits = places.search(self.places_index, query, 60)
            with self.lock:
                if live.br_path == [SEARCH, query]:
                    live.search(query, hits)
                    if live.index_state[0] != "done":
                        live.toast("Live's browser is still being indexed: results may be missing")
            self.dirty.set()
        threading.Thread(target=work, daemon=True).start()

    def live_menu(self, what, path=None):
        """Open a set / new set / export audio through Live's own menus (keyboard shortcuts). Whatever Live
        asks next (save changes?, the Export dialog, a file name) appears on the MPC to answer by touch."""
        import winshot

        def run():
            if what == "open":
                why = winshot.open_live_set(path)
            elif what == "new":
                why = winshot.live_keys(winshot.VK_CONTROL, ord("N"))
            else:
                why = winshot.live_keys(winshot.VK_CONTROL, winshot.VK_SHIFT, ord("R"))
            self.live.toast({"open": "Opening %s…" % os.path.splitext(os.path.basename(path or ""))[0],
                             "new": "New set…", "export": "Export: set it up on the screen, then Export"}[what]
                            if not why else "Couldn't: " + why)
        threading.Thread(target=run, daemon=True).start()

    def open_set_list(self, page=0):
        """Your sets, newest first: the projects folder of the open set (e.g. E:\\Ableton\\projects) and
        Documents\\Ableton, skipping Live's Backup folders."""
        live, ui = self.live, self.ui

        def scan(path):
            roots = [os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "Ableton")]
            if path:
                roots.insert(0, os.path.dirname(os.path.dirname(path)))     # <projects>\<set> Project\<set>.als
            sets = []
            for r in roots:
                for root, dirs, files in os.walk(r):
                    dirs[:] = [d for d in dirs if d not in ("Backup", "Samples", "Ableton Project Info")]
                    if root.count(os.sep) - r.count(os.sep) >= 3:
                        dirs[:] = []
                    sets += [os.path.join(root, f) for f in files if f.lower().endswith(".als")]
            sets = sorted(set(sets), key=lambda p: os.path.getmtime(p), reverse=True)
            per = self.PRESETS_PER_PAGE - 3
            p = max(0, min(page, (len(sets) - 1) // per if sets else 0))
            items = [(os.path.splitext(os.path.basename(s))[0], ("open_set", s)) for s in sets[p * per:(p + 1) * per]]
            if p > 0:
                items.append(("< Newer", ("open_list", p - 1)))
            if (p + 1) * per < len(sets):
                items.append(("Older >", ("open_list", p + 1)))
            ui.sheet = {"title": "Open set  (%d sets, newest first)" % len(sets), "items": items}
            self.dirty.set()
        live.ask_file_path(lambda path: threading.Thread(target=scan, args=(path,), daemon=True).start())

    def pick_routing(self, t, field):
        """Tap on an input/output box: Live's choices for it as a sheet."""
        live, ui = self.live, self.ui
        names = {"input_routing_type": "Input", "input_routing_channel": "Input channel",
                 "output_routing_type": "Output", "output_routing_channel": "Output channel"}

        def show(options):
            if not options:
                live.toast("Live offers no other choice here")
                return
            tr = live.tracks[t] if t < len(live.tracks) else None
            ui.sheet = {"title": "%s  ·  %s" % (tr.name if tr else "Track %d" % (t + 1), names[field]),
                        "color": rgb(tr.color) if tr else None,
                        "items": [(o, ("set_route", t, field, o)) for o in options[:27]]}
            self.dirty.set()
        live.ask_routing_options(t, field, show)

    def clip_field_value(self, key):
        det = self.live.clip_details
        if key == "loop_length":
            return det.get("loop_end", 0.0) - det.get("loop_start", 0.0)
        return det.get(key, 0)

    def clip_field_step(self, key, v0, pixels):
        """A DETAILS card's value after a drag of `pixels` (up = positive) from v0: positions move one grid
        step (the Clip page's GRID) per 30 px, lists one choice per 30 px, gain smoothly."""
        steps = int(pixels / 30)
        det = self.live.clip_details
        if key in ("start_marker", "end_marker", "loop_start"):
            return max(0.0, v0 + steps * self.ui.grid)
        if key == "loop_length":
            return max(self.ui.grid, v0 + steps * self.ui.grid)
        if key == "launch_mode":
            return (int(v0) + steps) % 4
        if key == "launch_quantization":
            return (int(v0) + steps) % 15
        if key == "legato":
            return int(v0) ^ (steps % 2)
        if key == "gain":
            return max(0.0, min(1.0, v0 + pixels / 400))
        if key == "pitch_coarse":
            return max(-48, min(48, int(v0) + int(pixels / 15)))
        if key == "pitch_fine":
            return max(-50, min(50, int(v0) + int(pixels / 6)))
        if key == "warp_mode":
            modes = det.get("warp_modes") or [0, 1, 2, 3, 4, 6]
            k = modes.index(v0) if v0 in modes else 0
            return modes[(k + steps) % len(modes)]
        return v0

    def note_layout(self):
        """How the pads play notes: "drum" when the selected track has a Drum Rack, else the chosen layout."""
        return "drum" if self.live.drum_pads is not None else self.ui.note_layout

    def pad_note(self, pad):
        """The note a pad plays (0 = bottom left). 16 Levels: always the same note."""
        ui, live = self.ui, self.live
        if ui.levels:
            return ui.levels_note
        layout = self.note_layout()
        if layout == "drum":
            return ui.drum_base + pad
        if layout == "scale":                       # in key: 16 scale notes from the root, low to high
            root, _name, _on, iv = live.scale
            iv = iv or [0]
            base = (ui.pad_base // 12) * 12 + root % 12
            return base + 12 * (pad // len(iv)) + iv[pad % len(iv)]
        return ui.pad_base + pad

    def pad_velocity(self, pad, vel):
        if self.ui.levels:
            return max(1, min(127, round(8 + pad * 119 / 15)))
        if self.ui.full_level:
            return 127
        exponent = {"linear": 1., "soft": .65, "hard": 1.5}[self.ui.velocity_curve]
        return max(1, min(127, round(127 * (max(1, min(127, vel)) / 127) ** exponent)))

    def repeat_loop(self):
        """Note Repeat: while NOTE REPEAT is held (or latched), held pads retrigger every repeat_rate beats,
        on Live's beat grid while the song plays. Notes last GATE of the time between hits."""
        next_t, offs = None, []
        while not self.quit:
            ui, live = self.ui, self.live
            now = time.perf_counter()
            released = set()
            for item in list(offs):                     # note-offs that are due
                if now >= item[0]:
                    offs.remove(item)
                    if self.sounding.get(item[1]) == item[2]:
                        for voice in self.pad_voices(item[1]):
                            if voice not in released:
                                self.midi.send(0x80, voice, 0)
                                released.add(voice)
            active = (ui.repeat_held or ui.repeat_latch) and ui.pad_mode() == "play" and self.sounding
            if not active:
                next_t = None
                time.sleep(0.002)
                continue
            period = ui.repeat_rate * 60.0 / max(20.0, live.tempo or 120.0)
            if next_t is None:                          # the press itself was the first hit
                if live.playing and live.song_time_at and time.time() - live.song_time_at < 1.0:
                    beat = live.song_time + (time.time() - live.song_time_at) * live.tempo / 60.0
                    next_t = now + (ui.repeat_rate - beat % ui.repeat_rate) * 60.0 / live.tempo
                else:
                    next_t = now + period
                with self.lock:
                    offs += [(now + period * GATE, p, n) for p, n in self.sounding.items()]
            if now >= next_t:
                with self.lock:
                    repeated = set()
                    for p, n in list(self.sounding.items()):
                        for voice in self.pad_voices(p):
                            if voice not in repeated:
                                self.midi.send(0x80, voice, 0)
                                self.midi.send(0x90, voice, self.repeat_vel.get(p, 100))
                                repeated.add(voice)
                        offs.append((now + period * GATE, p, n))
                next_t += period
                if next_t < now:
                    next_t = now + period
            time.sleep(0.001)

    def check_long_press(self):
        """Holding a finger still on a clip, track or scene opens its action sheet."""
        with self.lock:
            tc = self.touch
            if not tc or tc["moved"] or tc.get("long") or time.time() - tc["t"] < LONG_PRESS_S:
                return
            a = tc["action"]
            if a and a[0] == 'arr_clip' and not self.ui.sheet:
                tc['long'] = True
                if a[1:] in self.ui.arr_selection and len(self.ui.arr_selection) > 1:
                    self.arrangement_group_sheet()
                    self.dirty.set()
                    return
                self.ui.arr_selected = a[1:]
                self.ui.arr_selection = [a[1:]]
                self.arrangement_sheet(a[1:])
                self.dirty.set()
                return
            if a and a[:2] == ('drag', 'arr') and a[2] >= 0 and not self.ui.sheet:
                tc['long'] = True
                at = max(0., round(self.ui.arr_start+(tc['x']-TL_X)/self.ui.arr_ppb()))
                self.ui.sheet = {'title': 'Bar %.2f  ·  %s' % (at/self.live.beats_per_bar()+1, self.live.track_name(a[2])),
                                 'items': [('Paste here', ('arr_clipboard','paste',a[2],at)),
                                           ('Undo', ('arr_history','undo')), ('Redo', ('arr_history','redo'))]}
                self.dirty.set()
                return
            if not (a and a[0] in ("clip", "track", "scene") and not isinstance(a[1], str)):
                return
            if self.ui.sheet or self.ui.kb or self.ui.colors or self.ui.help:
                return
            tc["long"] = True
            self.open_sheet(a)
        self.dirty.set()

    def arrangement_sheet(self, args):
        t, start, length, name = args
        live, ui = self.live, self.ui
        data = live.tracks[t].arr_details.get((round(start,4),round(length,4),name), {})
        ui.sheet = {'title': name or 'Arrangement clip', 'color': rgb(live.tracks[t].color),
                    'items': [
                        ('Copy', ('arr_clipboard','copy',*args)),
                        ('Cut', ('arr_clipboard','cut',*args)),
                        ('Paste at playhead', ('arr_clipboard','paste',t,live.song_time)),
                        ('Duplicate after', ('arr_edit',*args,'duplicate',start+length)),
                        ('Move to playhead', ('arr_edit',*args,'move',live.song_time)),
                        ('Activate' if data.get('muted') else 'Deactivate', ('arr_edit',*args,'mute',0.)),
                        ('Loop this clip', ('arr_loop',start,length)),
                        ('Rename', ('rename', ('arr_clip',*args))),
                        ('Colour', ('color', ('arr_clip',*args))),
                        ('Undo', ('arr_history','undo')), ('Redo', ('arr_history','redo')),
                        ('Delete clip', ('arr_edit',*args,'delete',0.))]}

    def modified_tap(self, action):
        """ERASE / COPY held, or a clip waiting to be pasted. Returns True if the tap was used up here."""
        ui, live = self.ui, self.live
        kind = action[0]
        if ui.erase_held:
            if kind == "track":
                live.delete_track(action[1])
            elif kind == "scene":
                live.delete_scene(action[1])
            else:
                t, s = action[1], action[2]
                tr = live.tracks[t] if t < len(live.tracks) else None
                if tr and s < len(tr.clips) and tr.clips[s][0]:
                    live.delete_clip(t, s)
            return True
        if kind == "clip" and ui.copy_src:              # paste the picked clip here
            src, dst = ui.copy_src, (action[1], action[2])
            ui.copy_src = None
            self.copy_pasted = True
            if dst == src:
                live.toast("Copy cancelled")
            elif live.tracks[src[0]].midi_in != live.tracks[dst[0]].midi_in:
                live.toast("Clips can't be copied between MIDI and audio tracks")
            else:
                live.copy_clip(*src, *dst)
            return True
        if ui.copy_held:
            if kind == "track":
                live.duplicate_track(action[1])
                self.copy_pasted = True
            elif kind == "scene":
                live.duplicate_scene(action[1])
                self.copy_pasted = True
            else:
                t, s = action[1], action[2]
                tr = live.tracks[t] if t < len(live.tracks) else None
                if tr and s < len(tr.clips) and tr.clips[s][0]:
                    ui.copy_src = (t, s)
                else:
                    live.toast("Tap a clip first, then where it goes")
            return True
        return False

    def open_sheet(self, action):
        ui, live = self.ui, self.live
        kind = action[0]
        bpb = live.beats_per_bar()
        if kind == "track":
            t = action[1]
            if not 0 <= t < len(live.tracks):
                return
            tr = live.tracks[t]
            items = [("Rename", ("rename", ("track", t))), ("Colour", ("color", ("track", t))),
                     ("Duplicate", ("dup_track", t))]
            if tr.armable:
                items.append(("Disarm" if tr.arm else "Arm for recording", ("arm", t)))
            items += [("New MIDI track after", ("new_track", "midi", t + 1)),
                      ("New audio track after", ("new_track", "audio", t + 1)),
                      ("Delete track", ("del_track", t))]
            ui.sheet = {"title": tr.name or "Track %d" % (t + 1), "color": rgb(tr.color), "items": items}
        elif kind == "scene":
            s = action[1]
            if not 0 <= s < max(0, live.num_scenes):
                return
            items = [("Rename", ("rename", ("scene", s))), ("Colour", ("color", ("scene", s))),
                     ("Duplicate", ("dup_scene", s)), ("New scene below", ("new_scene", s + 1)),
                     ("Capture playing clips", ("capture_scene",)), ("Delete scene", ("del_scene", s))]
            ui.sheet = {"title": live.scene_name(s).capitalize(), "items": items}
        else:
            t, s = action[1], action[2]
            if not 0 <= t < len(live.tracks):
                return
            tr = live.tracks[t]
            clip = tr.clips[s] if s < len(tr.clips) else (False, "", 0)
            where = "%s, scene %d" % (tr.name, s + 1)
            if clip[0]:
                items = [("Rename", ("rename", ("clip", t, s))), ("Colour", ("color", ("clip", t, s))),
                         ("Duplicate below", ("dup_clip", t, s)), ("Copy to...", ("copy_clip", t, s)),
                         ("Copy to arrangement", ("to_arr", t, s)),
                         ("Double loop", ("double", t, s)), ("Edit", ("edit", t, s)),
                         ("Delete clip", ("del_clip", t, s))]
                ui.sheet = {"title": (clip[1] or "Clip") + "  ·  " + where, "color": rgb(clip[2] or tr.color),
                            "items": items}
            else:
                items = []
                if tr.midi_in:
                    items += [("New clip, %d bar%s" % (b, "s" if b > 1 else ""), ("new_clip", t, s, b * bpb))
                              for b in (1, 2, 4, 8)]
                if tr.armable:
                    items += [("Record %d bar%s" % (b, "s" if b > 1 else ""), ("rec_fixed", t, s, b))
                              for b in (1, 2, 4, 8)]
                if not items:
                    live.toast("Nothing to do on this slot")
                    return
                ui.sheet = {"title": "Empty slot  ·  " + where, "color": rgb(tr.color), "items": items}

    def open_menu(self):
        ui = self.ui
        fixed = "Fixed length: %s" % ("off" if not ui.fixed_len else "%d bar%s" % (ui.fixed_len,
                                                                                  "s" if ui.fixed_len > 1 else ""))
        ui.sheet = {"title": "Menu", "items": [
            ("Save set", ("save",)), ("Save set as...", ("save_as",)), ("Open set...", ("open_list", 0)),
            ("New set", ("new_set",)), ("Export audio...", ("export",)), ("Capture MIDI", ("capture_midi",)),
            (fixed, ("fixed",)),
            ("Count-in: " + ({0:"off",1:"1 bar",2:"2 bars",3:"4 bars"}.get(live.count_in, "waiting for Live")), ("count_in",)),
            ("Chord: " + ui.chord_mode, ("play_option", "chord_mode")),
            ("Velocity: " + ui.velocity_curve, ("play_option", "velocity_curve")),
            ("Pressure: " + ui.pressure_mode, ("play_option", "pressure_mode")),
            ("Cue / Crossfader", ("main_controls",)),
            ("Rack macro variations", ("rack_menu",)),
            ("Move selected device", ("device_move_menu",)),
            ("Recordings to arrangement: %s" % ("on" if ui.rec_to_arr else "off"), ("rec_to_arr",)),
            ("Pads: %s" % ("in key" if ui.note_layout == "scale" else "chromatic"), ("layout",)),
            ("Key: %s" % NOTE_NAMES[self.live.scale[0] % 12], ("key_pick",)),
            ("Scale: %s" % self.live.scale[1], ("scale_pick",)),
            ("New MIDI track", ("new_track", "midi", -1)),
            ("New audio track", ("new_track", "audio", -1)), ("New return track", ("new_track", "return", -1)),
            ("New scene", ("new_scene", -1)), ("Capture scene", ("capture_scene",)),
            ("Help", ("help",)), ("Quit MPC One Push", ("quit",))]}

    def main_controls_sheet(self):
        items = []
        for field, label in (("cue_volume", "Cue"), ("crossfader", "Crossfader")):
            values = self.live.main_controls
            if values:
                readout = values[field][3]
                items.extend([(label + " -  " + readout, ("main_set", field, -.05)),
                              (label + " +  " + readout, ("main_set", field, .05))])
                if field == "crossfader":
                    items.append(("Crossfader center", ("main_set", field, "center")))
        self.ui.sheet = {"title": "Cue / Crossfader" if items else "Waiting for Live mixer...",
                         "main_controls": True, "items": items}

    def sheet_do(self, cmd):
        ui, live = self.ui, self.live
        ui.sheet = None
        k = cmd[0]
        if k == 'arr_group':
            live.send('/live/arrangement/group', cmd[1], json.dumps(cmd[2],separators=(',',':')))
            return
        if k == 'arr_clear':
            ui.arr_selection, ui.arr_selected = [], None
            return
        if k == 'arr_clipboard':
            live.send('/live/arrangement/clipboard', *cmd[1:])
            return
        if k == 'arr_history':
            live.song(cmd[1])
            ui.arr_selected = None
            ui.arr_selection = []
            return
        if k == 'arr_loop':
            live.set_song('loop_start', float(cmd[1]))
            live.set_song('loop_length', float(cmd[2]))
            live.set_song('loop', 1)
            return
        if k == "count_in":
            if live.count_in is not None:
                live.count_in = (live.count_in+1)%4
                live.send("/live/song/set/count_in_duration", live.count_in)
            else:
                live.send("/live/song/get/count_in_duration")
            self.open_menu()
            return
        if k == "device_move_menu":
            dev = live.dev
            if dev.names:
                args = (dev.track, dev.path(), dev.names[dev.idx])
                ui.sheet = {"title": "Move " + dev.names[dev.idx], "items": [
                    ("Move left", ("device_move", *args, -1)), ("Move right", ("device_move", *args, 1)),
                    ("Duplicate", ("device_duplicate", *args))]}
            else:
                live.toast("Select a device on the Device page first")
            return
        if k == "device_move":
            live.send("/live/dev/move", *cmd[1:])
            return
        if k == "device_duplicate":
            live.send("/live/dev/duplicate", *cmd[1:])
            return
        if k == "rack_menu":
            dev = live.dev
            if dev.names and dev.selected_info()[2]:
                args = (dev.track, dev.path(), dev.names[dev.idx])
                ui.sheet = {"title": "Rack macros", "items": [
                    (label, ("rack_action", *args, action)) for label, action in (
                        ("Randomize macros", "randomize"), ("Store variation", "store"),
                        ("Recall previous", "previous"), ("Recall next", "next"))]}
            else:
                live.toast("Select a rack on the Device page first")
            return
        if k == "rack_action":
            live.send("/live/dev/rack_action", *cmd[1:])
            return
        if k == "main_controls":
            live.send("/live/mixer/get/main_controls")
            self.main_controls_sheet()
            return
        if k == "main_set":
            field, step = cmd[1:]
            if live.main_controls:
                value, lo, hi, _ = live.main_controls[field]
                value = (lo+hi)/2 if step == "center" else max(lo, min(hi, value+step*(hi-lo)))
                live.send("/live/mixer/set/main_controls", field, float(value))
            self.main_controls_sheet()
            return
        if k == "arr_edit":
            live.send("/live/arrangement/edit", *cmd[1:])
            return
        if k == "play_option":
            choices = {"chord_mode": tuple(CHORDS), "velocity_curve": ("linear", "soft", "hard"),
                       "pressure_mode": ("poly", "channel", "mod wheel", "off")}
            key = cmd[1]
            self.all_notes_off()
            values = choices[key]
            setattr(ui, key, values[(values.index(getattr(ui, key)) + 1) % len(values)])
            self.open_menu()
            return
        if k == "rename":
            what = cmd[1]
            ui.kb = {"what": what, "text": self.name_of(what), "caps": False, "title": "Rename " + {
                "track": "track", "clip": "clip", "scene": "scene", 'arr_clip': 'clip'}[what[0]]}
        elif k == "color":
            ui.colors = {"what": cmd[1], "title": "Colour: " + (self.name_of(cmd[1]) or cmd[1][0])}
        elif k == "dup_track":
            live.duplicate_track(cmd[1])
        elif k == "del_track":
            live.delete_track(cmd[1])
        elif k == "arm":
            live.toggle("arm", cmd[1])
        elif k == "new_track":
            live.new_track(cmd[1], cmd[2])
        elif k == "dup_scene":
            live.duplicate_scene(cmd[1])
        elif k == "new_scene":
            live.new_scene(cmd[1])
        elif k == "capture_scene":
            live.capture_scene()
        elif k == "del_scene":
            live.delete_scene(cmd[1])
        elif k == "dup_clip":
            live.duplicate_clip(cmd[1], cmd[2])
        elif k == "copy_clip":
            ui.copy_src = (cmd[1], cmd[2])
        elif k == "double":
            live.double_clip(cmd[1], cmd[2])
        elif k == "edit":
            self.edit_clip(cmd[1], cmd[2])
        elif k == "del_clip":
            live.delete_clip(cmd[1], cmd[2])
        elif k == "new_clip":
            live.new_clip(cmd[1], cmd[2], cmd[3])
        elif k == "rec_fixed":
            self.record_into(cmd[1], cmd[2], cmd[3])
        elif k == "save":
            self.save_set()
        elif k == "save_as":
            ui.kb = {"what": ("set",), "text": "", "caps": False, "title": "Save the set as"}
        elif k == "capture_midi":
            live.capture_midi()
        elif k == "fixed":
            ui.fixed_len = FIXED_LENGTHS[(FIXED_LENGTHS.index(ui.fixed_len) + 1) % len(FIXED_LENGTHS)]
            live.toast("Fixed length off" if not ui.fixed_len else
                       "Fixed length %d bar%s: REC or an empty slot on an armed track records exactly that"
                       % (ui.fixed_len, "s" if ui.fixed_len > 1 else ""))
            self.open_menu()                        # stay in the menu so you can keep cycling
        elif k == "to_arr":
            live.copy_to_arrangement(cmd[1], cmd[2])
        elif k == "layout":
            self.all_notes_off()
            ui.note_layout = "chromatic" if ui.note_layout == "scale" else "scale"
            live.toast("Pads play %s" % ("the notes of %s %s, root at the bottom left" % (
                NOTE_NAMES[live.scale[0] % 12], live.scale[1]) if ui.note_layout == "scale" else "chromatically"))
            self.open_menu()
        elif k == "key_pick":
            ui.sheet = {"title": "Key (Live's scale root)", "items": [(n, ("set_key", i))
                                                                     for i, n in enumerate(NOTE_NAMES)]}
        elif k == "scale_pick":
            ui.sheet = {"title": "Scale (Live's scale)", "items": [(n, ("set_scale", n)) for n in SCALE_NAMES]}
        elif k == "set_key":
            live.set_scale(root=cmd[1])
            live.scale = (cmd[1],) + tuple(live.scale[1:])
        elif k == "set_scale":
            live.set_scale(name=cmd[1])
        elif k == "rec_to_arr":
            ui.rec_to_arr = not ui.rec_to_arr
            live.toast("Session recordings %s" % ("also go into the arrangement where you played them"
                                                  if ui.rec_to_arr else "stay in the Session only"))
            self.open_menu()
        elif k == "preset_dir":
            self.browse_presets(cmd[1], cmd[2])
        elif k == "preset_load":
            ui.preset_loaded = cmd[2].rsplit(".", 1)[0]
            live.load_device_preset(cmd[1], cmd[2])
            self.browse_presets(cmd[1], 0)              # stay in the folder to try the next one
        elif k == "set_preset":
            live.set_preset(cmd[1])
        elif k == "preset_page":
            self.open_presets(cmd[1])
        elif k == "open_list":
            self.open_set_list(cmd[1])
        elif k == "open_set":
            self.live_menu("open", cmd[1])
        elif k == "new_set":
            self.live_menu("new")
        elif k == "export":
            self.live_menu("export")
        elif k == "set_route":
            live.set_routing(cmd[1], cmd[2], cmd[3])
        elif k == "help":
            ui.help = True
        elif k == "quit":
            self.user_quit()

    def name_of(self, what):
        live = self.live
        if what[0] == 'arr_clip':
            return what[-1]
        if what[0] == "track" and what[1] < len(live.tracks):
            return live.tracks[what[1]].name
        if what[0] == "clip" and what[1] < len(live.tracks):
            clips = live.tracks[what[1]].clips
            return clips[what[2]][1] if what[2] < len(clips) else ""
        if what[0] == "scene":
            return live.scenes[what[1]] if what[1] < len(live.scenes) else ""
        return ""

    def kb_key(self, key):
        ui, live = self.ui, self.live
        kb = ui.kb
        if not kb:
            return
        if key == "OK":
            ui.kb = None
            name = kb["text"].strip()
            if kb["what"][0] == "set":
                if name:
                    self.save_set(name)
                return
            if kb["what"][0] == "type":            # into Live's dialog (e.g. Export's file name)
                import winshot
                hwnd = self.renderer.dialog_hwnd
                if hwnd and name:
                    winshot.focus(hwnd)
                    winshot.type_text(kb["text"])
                return
            if kb["what"][0] == "search":
                if name:
                    self.run_search(name)
                return
            if kb['what'][0] == 'arr_clip':
                live.send('/live/arrangement/set', *kb['what'][1:], 'name', name)
                ui.arr_selected = None
                return
            live.rename(kb["what"], name)
            what = kb["what"]                       # show the new name right away
            if what[0] == "track" and what[1] < len(live.tracks):
                live.tracks[what[1]].name = name
            elif what[0] == "scene" and what[1] < len(live.scenes):
                live.scenes[what[1]] = name
            elif what[0] == "clip" and what[1] < len(live.tracks) and what[2] < len(live.tracks[what[1]].clips):
                has, _n, col = live.tracks[what[1]].clips[what[2]]
                live.tracks[what[1]].clips[what[2]] = (has, name, col)
        elif key == "CANCEL":
            ui.kb = None
        elif key == "DEL":
            kb["text"] = kb["text"][:-1]
        elif key == "CLEAR":
            kb["text"] = ""
        elif key == "CAPS":
            kb["caps"] = not kb["caps"]
        elif key == "SPACE":
            kb["text"] += " "
        elif len(kb["text"]) < 80:
            kb["text"] += key

    def record_into(self, t, s, bars):
        """Record exactly `bars` into slot s of track t (arming the track first if needed)."""
        live = self.live
        tr = live.tracks[t]
        if not tr.armable:
            live.toast("This track can't record")
            return
        if not tr.arm:
            live.toggle("arm", t)
        live.record_fixed(t, s, bars * live.beats_per_bar())
        live.toast("Recording %d bar%s into scene %d" % (bars, "s" if bars > 1 else "", s + 1))

    def record_next(self, t):
        """REC with Fixed Length on: record into the track's next empty slot (a new scene if it has none)."""
        live, ui = self.live, self.ui
        tr = live.tracks[t]
        start = tr.playing + 1 if tr.playing >= 0 else 0
        free = [s for s in list(range(start, live.num_scenes)) + list(range(0, start))
                if not (s < len(tr.clips) and tr.clips[s][0])]
        if free:
            self.record_into(t, free[0], ui.fixed_len)
        else:
            live.new_scene(-1)
            self.record_into(t, live.num_scenes, ui.fixed_len)

    def save_set(self, name=None):
        """Save the set in Live (Ctrl+S in Live's window; Live's script API can't save). A set that was never
        saved first gets a name from the on-screen keyboard, typed into Live's Save dialog."""
        live, ui = self.live, self.ui

        def run(as_name):
            import winshot
            why = winshot.save_live_set(as_name)
            live.toast(("Saved as %s" % as_name if as_name else "Saved") if not why else "Couldn't save: " + why)

        def go(path):
            if not path:
                ui.kb = {"what": ("set",), "text": "", "caps": False,
                         "title": "This set has never been saved. Name it:"}
                self.dirty.set()
                return
            live.toast("Saving...")
            threading.Thread(target=run, args=(None,), daemon=True).start()

        if name:
            live.toast("Saving as %s..." % name)
            threading.Thread(target=run, args=(name,), daemon=True).start()
        else:
            live.ask_file_path(go)

    def preview_sel(self):
        """Audition the highlighted browser item (Browse page, PREVIEW on). Samples in your Places folders
        play through Windows (and can loop); items in Live's own browser use Live's preview."""
        live, ui = self.live, self.ui
        i = ui.br_sel
        if not ui.preview_on or live.br_root or not 0 <= i < len(live.br_items) or live.br_items[i].startswith("[F] "):
            self.stop_preview()
            return
        name = live.br_items[i]
        if live.br_path[:1] == [SEARCH] and i < len(live.search_hits):
            kind, where = live.search_hits[i]
            if kind == "place":
                if self.live_previewing:
                    self.stop_preview()
                if where.lower().endswith(PREVIEWABLE):
                    self.previewer.play(where, ui.preview_loop)
                else:
                    self.previewer.stop()
            else:
                self.previewer.stop()
                live.send("/live/browser/preview_path", *where)
                self.live_previewing = True
            return
        folder = live.user_place_dir()
        if folder is not None:
            if self.live_previewing:
                self.stop_preview()
            if name.lower().endswith(PREVIEWABLE):
                self.previewer.play(os.path.join(folder, name), ui.preview_loop)
            else:
                self.previewer.stop()
        else:
            self.previewer.stop()
            live.send("/live/browser/preview_path", *live.br_path, name)
            self.live_previewing = True

    def stop_preview(self):
        self.previewer.stop()
        if self.live_previewing:
            self.live.send("/live/browser/stop_preview")
            self.live_previewing = False

    def br_scroll_to_sel(self):
        ui = self.ui
        per = self.renderer.BR_ROWS * 2
        if ui.br_sel < ui.br_scroll:
            ui.br_scroll = ui.br_sel - ui.br_sel % self.renderer.BR_ROWS
        elif ui.br_sel >= ui.br_scroll + per:
            ui.br_scroll = ui.br_sel - per + 1 + (self.renderer.BR_ROWS - 1 - (ui.br_sel % self.renderer.BR_ROWS))
            ui.br_scroll = max(0, ui.br_scroll)


# ---------------------------------------------------------------- made-up state for --demo / --preview

def demo_live():
    live = Live(offline=True)
    names = ["Drums", "Bass", "Keys", "Pad", "Lead", "Vox", "FX", "Perc", "Strings", "Bus"]
    colors = [0xFF5A3C, 0xFFA829, 0xF7E45A, 0x7DDB4A, 0x2EC8B9, 0x4AA3FF, 0x9B6BFF, 0xE45ADB, 0xFF8FA0, 0xAAAAAA]
    live.num_tracks, live.num_scenes = len(names), 12
    live.scenes = ["Intro", "Verse", "Build", "Drop", "Break", "Verse 2", "Drop 2", "Outro", "", "", "", ""]
    for t, (n, c) in enumerate(zip(names, colors)):
        tr = Track()
        tr.name, tr.color, tr.armable = n, c, t != 9
        tr.clips = [((t * 7 + s * 3) % 5 != 0, "%s %d" % (n, s + 1), 0) for s in range(live.num_scenes)]
        tr.playing = [1, 1, 3, -1, 3, 2, -1, 1, -1, -1][t]
        tr.fired = [-1, -1, -1, 2, -1, -1, 4, -1, -1, -1][t]
        tr.arm = t == 5
        tr.mute, tr.solo = t == 6, t == 2
        tr.vol, tr.pan, tr.meter = 0.85 - 0.07 * (t % 4), [-0.3, 0, 0.5, 0, -1, 0.2, 0, 0, 0, 0][t], 0.3 + 0.06 * t
        tr.vol_str = "%.1f dB" % (-4.5 * (t % 4))
        starts = [s for s in range(0, 96, 8) if (t + s // 8) % 3 != 0]
        tr.arr = {"start_time": [float(s) * 4 for s in starts], "length": [32.0 - 8 * (t % 2)] * len(starts),
                  "name": ["%s %d" % (n, i + 1) for i in range(len(starts))]}
        live.tracks.append(tr)
    live.sel = 2
    live.playing, live.tempo, live.metro = True, 124.0, True
    d = live.dev
    d.track, d.idx, d.names = 2, 1, ["Analog", "EQ Eight", "Compressor", "Reverb", "Utility"]
    d.info = [("InstrumentVector", 1, 0, 0, 0), ("Eq8", 1, 0, 0, 0), ("Compressor2", 0, 0, 0, 0),
              ("Reverb", 1, 0, 0, 0), ("StereoGain", 1, 0, 0, 0)]
    d.loaded = True
    d.pnames = ["Device On", "1 Freq A", "1 Gain A", "1 Q A", "2 Freq A", "2 Gain A", "3 Freq A", "3 Gain A",
                "Output Gain", "Scale", "Adaptive Q"]
    d.pvals = [1, 0.3, 0.55, 0.4, 0.62, 0.5, 0.8, 0.35, 0.5, 0.7, 1]
    d.pmin, d.pmax, d.pq = [0] * 11, [1] * 11, [True] + [False] * 9 + [True]
    d.pstr = {1: "120 Hz", 2: "+1.2 dB", 3: "0.71", 4: "1.40 kHz", 5: "0.0 dB", 6: "8.00 kHz", 7: "-3.5 dB",
              8: "0.0 dB"}
    live.song_time, live.loop, live.loop_start, live.loop_len = 70.0, True, 64.0, 32.0
    live.cues = [("Intro", 0.0), ("Verse", 32.0), ("Drop", 96.0)]
    live.br_path = [PLACES]
    live.br_items = ["packs", "user_library", "current_project", "userplace:racks", "userplace:PACKS"]
    live.places_info = {"racks": {"exists": False, "linked": False, "path": r"E:\Ableton\racks"},
                        "PACKS": {"exists": True, "linked": False, "path": r"E:\Audio Production\PACKS"}}
    live.clip = (0, 1)
    live.clip_info = {"name": "Drums 2", "loop_start": 0.0, "loop_end": 8.0, "length": 8.0, "is_midi_clip": 1,
                      "playing_position": 2.6}
    live.notes = [[36, b, 0.25, 120, False] for b in (0, 1, 2, 2.75, 3, 4, 5, 6, 7, 7.5)] + \
                 [[38, b, 0.25, 110, False] for b in (1, 3, 5, 7)] + \
                 [[42, b / 2, 0.125, 50 + (b % 2) * 50, False] for b in range(16)] + \
                 [[46, 3.5, 0.5, 90, False], [46, 7.5, 0.5, 90, False], [49, 0, 1.0, 100, False]]
    live.env = {"key": (-1, 0), "exists": True, "min": 0.0, "max": 1.0,
                "vals": [0.55 + 0.3 * math.sin(i / 64 * 2 * math.pi * 2) for i in range(64)]}
    return live


def main():
    args = sys.argv[1:]
    if "--demo" in args:
        app = App(demo_live())
        out = os.path.join(HERE, "..", "logs")
        app.ui.pitch_lo, app.ui.focus_pitch = 34, 42
        for page in PAGES + ["clip-auto"]:
            app.ui.page = page.split("-")[0]
            app.ui.clip_mode = "auto" if page == "clip-auto" else "notes"
            img, _ = app.renderer.render(app.live, app.ui)
            path = os.path.normpath(os.path.join(out, "ui-%s.png" % page))
            img.save(path)
            print("wrote", path)
        d = app.live.dev                            # an opened Drum Rack, and the add-device menu
        d.names = ["Drum Rack", "Glue Compressor", "Reverb"]
        d.info = [("DrumGroupDevice", 1, 1, 1, 6), ("GlueCompressor", 1, 0, 0, 0), ("Reverb", 1, 0, 0, 0)]
        d.idx, d.view, d.drum = 0, "chains", True
        d.chains = [("Kick Atlas 1", 36), ("SideStick", 37), ("Snare 3", 38), ("Clap", 39), ("HH Closed", 42),
                    ("808 Long", 48)]
        app.ui.page, app.ui.drum_note = "device", 38
        app.renderer.render(app.live, app.ui)[0].save(os.path.join(out, "ui-device-drumrack.png"))
        d.view, app.ui.add_menu = "devices", True
        app.renderer.render(app.live, app.ui)[0].save(os.path.join(out, "ui-device-add.png"))
        app.ui.add_menu = False
        app.ui.page, app.ui.help = "session", True
        app.renderer.render(app.live, app.ui)[0].save(os.path.join(out, "ui-help.png"))
        app.ui.help = False
        for name, opener in (("sheet-clip", lambda: app.open_sheet(("clip", 1, 0))),
                             ("sheet-empty", lambda: app.open_sheet(("clip", 2, 5))),
                             ("sheet-track", lambda: app.open_sheet(("track", 2))),
                             ("menu", app.open_menu),
                             ("keyboard", lambda: app.sheet_do(("rename", ("track", 2)))),
                             ("colors", lambda: app.sheet_do(("color", ("clip", 1, 0))))):
            app.ui.sheet = app.ui.kb = app.ui.colors = None
            opener()
            app.renderer.render(app.live, app.ui)[0].save(os.path.join(out, "ui-%s.png" % name))
        app.ui.sheet = app.ui.kb = app.ui.colors = None
        app.ui.copy_src, app.ui.copy_held = (1, 0), True
        app.renderer.render(app.live, app.ui)[0].save(os.path.join(out, "ui-copy.png"))
        return
    seconds = float(args[args.index("--seconds") + 1]) if "--seconds" in args else None
    app = App(demo_live() if "--preview" in args else None)
    app.run(seconds, snapshots="--snapshots" in args)


if __name__ == "__main__":
    main()
