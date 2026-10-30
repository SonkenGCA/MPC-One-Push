"""Ableton Live state and commands over AbletonOSC, for the MPC screen app.

Python port of the tablet UI's logic (web/app.js): state is polled rather than listened to,
because AbletonOSC's track listeners are bound to track indices and go stale when tracks move.

    this process --UDP OSC :11000--> AbletonOSC (inside Live) --UDP :11001--> this process

Only one program can own UDP 11001, so the tablet bridge (server.py) can't run at the same time.
Talks to Ableton only through AbletonOSC; never touches Live's files or preferences.
"""

import math
import json
import os
import socket
import threading
import time

import device_layouts
import places
from server import decode, encode

LIVE_HOST, LIVE_PORT, REPLY_PORT = "127.0.0.1", 11000, 11001
PROBE_S = 4.0          # no replies for this long = disconnected
HOLD_S = 0.4           # after a local change, ignore polled values for this long
BANK = 8               # device parameters per page (Push-style knob row)
GRID_PAGE = 16         # plug-in knob screen: parameters per page (2 rows of 8, like the MPC's plug-in screen)
PLACES = "@places"     # pseudo browser path: the user's Places folders
SEARCH = "@search"     # pseudo browser path: search results ([SEARCH, query])
MAX_CLIP_BEATS = 1024  # clip page never shows more than this (256 bars of 4/4)

SLOW_PROPS = ["track.name", "track.color", "track.can_be_armed", "track.has_midi_input",
              "clip_slot.has_clip", "clip.name", "clip.color"]
SLOW_TRACK_PROPS = 4   # track.* entries at the start of SLOW_PROPS
FAST_PROPS = ["track.playing_slot_index", "track.fired_slot_index", "track.mute", "track.solo"]
METER_PROPS = ["track.output_meter_level"]
CHUNK = 2              # tracks per slow request (keeps replies under the UDP size limit)


class Track:
    def __init__(self):
        self.name, self.color, self.armable, self.arm = "", 0x808080, False, False
        self.midi_in = False     # MIDI track (pads in play mode auto-arm it)
        self.mute = self.solo = False
        self.vol, self.vol_str, self.pan, self.meter = 0.85, "", 0.0, 0.0
        self.playing = self.fired = -1
        self.clips = []          # list of (has, name, color)
        self.sends, self.send_strs = [], []     # send levels 0..1 (one per return track) and Live's text for them
        self.route = None        # (input type, input channel, output type, output channel, monitoring 0/1/2/-1)
        self.arr = {"name": [], "start_time": [], "length": []}     # arrangement clips, parallel lists
        self.arr_details = {}
        self.arr_snapshot = None

    def arrangement_clips(self):
        if self.arr_snapshot is not None:
            return self.arr_snapshot
        a = self.arr
        n = min(len(a["name"]), len(a["start_time"]), len(a["length"]))
        return [(a["start_time"][i], a["length"][i], a["name"][i]) for i in range(n)]


class Strip:
    """A return track or the master on the Mix page (same fields the Mix page reads from a Track)."""

    def __init__(self, name=""):
        self.name, self.color = name, 0x808080
        self.vol, self.vol_str, self.pan, self.meter = 0.85, "", 0.0, 0.0
        self.mute = self.solo = False
        self.sends, self.send_strs = [], []


class Device:
    """The device chain shown on the Device page and its selected device.

    Devices are addressed by path through the add-on (see /live/chain/get/devices): `chain` is the
    chain being shown ("" = the track's own devices, "2/0" = chain 0 of device 2, ...), `idx` the
    selected device in it. view "chains" shows the selected rack's chains (or Drum Rack pads)."""

    def __init__(self, track=-1):
        self.track, self.idx, self.bank = track, 0, 0
        self.page_size = BANK    # parameters per page: BANK, or GRID_PAGE on the plug-in knob screen
        self.layout = None       # stock devices: [(page name, [param indices])] (device_layouts.py)
        self.chain = ""
        self.names, self.pnames, self.pvals, self.pmin, self.pmax, self.pq = [], [], [], [], [], []
        self.info = []           # per device: (class_name, on, has_chains, is_drum, n_chains)
        self.pstr = {}
        self.stale = False
        self.loaded = False      # first chain reply arrived
        self.view = "devices"    # or "chains"
        self.chains = []         # selected rack's chains: (name, in_note)
        self.drum = False
        self.crumbs = []         # names along the path into racks: [rack, chain, rack, chain, ...]
        self.sample = None       # selected Simpler's sample: {file, length, start, end, mode}
        self.playhead = None     # Simpler playing: 0..1 between its start and end markers; None = silent
        self.presets, self.preset_sel = [], -1    # a plug-in's own programs (VST2 plug-ins usually have them)

    def pidx(self, name):
        """Index of the parameter called `name`, or -1."""
        if getattr(self, "_pidx_of", None) is not self.pnames:
            self._pidx, self._pidx_of = {n: i for i, n in enumerate(self.pnames)}, self.pnames
        return self._pidx.get(name, -1)

    def is_simpler(self):
        return self.selected_info()[0] == "OriginalSimpler"

    def path(self, i=None):
        i = self.idx if i is None else i
        return ("%s/%d" % (self.chain, i)) if self.chain else str(i)

    def selected_info(self):
        return self.info[self.idx] if 0 <= self.idx < len(self.info) else ("", True, False, False, 0)

    def bank_params(self):
        """Parameter indices on the current page. Parameter 0 is "Device On"; the rest page in page_size."""
        if self.layout and self.page_size == BANK:
            return [p for p in self.layout[min(self.bank, len(self.layout) - 1)][1] if p < len(self.pnames)]
        n = max(0, len(self.pnames) - 1)
        k = self.page_size
        return [p for p in range(1 + self.bank * k, 1 + (self.bank + 1) * k) if p <= n]

    def num_banks(self):
        if self.layout and self.page_size == BANK:
            return len(self.layout)
        return max(1, -(-(len(self.pnames) - 1) // self.page_size))

    def set_page_size(self, k):
        """Switch between the 8-knob row and the 16-knob screen, keeping the first knob shown on screen."""
        if k != self.page_size:
            first = self.bank * self.page_size
            self.page_size, self.bank = k, first // k

    def norm(self, p):
        lo = self.pmin[p] if p < len(self.pmin) else 0.0
        hi = self.pmax[p] if p < len(self.pmax) else 1.0
        v = self.pvals[p] if p < len(self.pvals) else lo
        return 0.0 if hi <= lo else min(1.0, max(0.0, (v - lo) / (hi - lo)))


class Live:
    def __init__(self, on_change=lambda: None, offline=False):
        """offline=True: no sockets, always "connected"; for --demo/--preview with made-up state."""
        self.on_change = on_change
        self.offline = offline
        self.lock = threading.RLock()
        if not offline:
            self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.rx.bind((LIVE_HOST, REPLY_PORT))
            except OSError:
                raise SystemExit("UDP port %d is busy. Close the tablet bridge (start-bridge.bat) first." % REPLY_PORT)
            self.rx.settimeout(0.5)
        self.mode = "session"            # which page the app shows; decides what gets polled
        self.view_tracks = (0, 8)        # tracks on screen, for meters and volumes
        self.arr_view = (0., 64.)
        self.returns, self.master = [], Strip("Main")     # Mix page: return tracks and the master
        self.main_controls = None
        self.clip_details, self.want_details = {}, False   # Clip page DETAILS (want_details set by the app)
        self._detail_pending = set()
        self.mix_view, self.mix_returns = "mix", False   # what the Mix page shows (set by the app)
        # Session recordings also go into the arrangement where they were played (set by the app from MENU)
        self.copy_recordings = True
        self._rec_watch = {}             # (track, slot) being recorded -> [start beat in the song, missed replies]
        self.tracks, self.num_tracks, self.num_scenes, self.scenes = [], -1, -1, []
        self.playing = self.metro = self.rec = self.overdub = False
        self.tempo = 120.0
        self.count_in = None
        self.sel = 0
        self.dev = Device()
        # Browser: br_path [] = every root, [PLACES] = the user's Places folders + Current Project,
        # otherwise [root key, folder, ...]. Root keys for Places look like "place:PACKS".
        self.br_path, self.br_items, self.br_root, self.br_pending = [], [], True, False
        self.search_hits, self.br_hints = [], {}   # search: ("live", browser path) / ("place", file) per item
        self.index_state = ("idle", 0)             # Live's browser search index (built inside Live, once)
        self._place_hits = []
        self.places_info = {}            # Places folders from Library.cfg: name -> {path, exists, linked}
        self.plugins = None              # your plug-in names, fetched when the add-device menu first opens
        # Arrangement (times in beats)
        self.song_time, self.song_len = 0.0, 0.0
        self.song_time_at = 0.0          # when song_time arrived (Note Repeat estimates the position from it)
        self.need_time = False           # the app wants song_time often (Note Repeat on while playing)
        self.scale = (0, "Major", False, [0, 2, 4, 5, 7, 9, 11])   # Live's scale: root, name, mode on, intervals
        self.drum_pads = None            # selected track's Drum Rack: {note: (name, color)}; None = no Drum Rack
        self.drum_track = -1
        self.loop, self.loop_start, self.loop_len = False, 0.0, 16.0
        self.sig = [4, 4]
        self.rec_mode = self.back_to_arranger = False
        self.cues = []                   # (name, time)
        # Clip page: the Session clip being edited, its notes and one automation envelope
        self.clip = None                 # (track, slot)
        self.clip_info = {}              # name, color, length, loop_start, loop_end, is_midi_clip, playing_position
        self.notes = []                  # [pitch, start, duration, velocity, mute]
        self.env = None                  # {"key": (dev, param), "exists", "min", "max", "vals"}
        self.automation_target = None
        self.automation_data = None
        self.automation_token = 0
        self.native_automation_result = None
        self.native_draw_mode = False
        self.native_arrangement_visible = None
        self.env_key = (-1, 0)           # which parameter the automation lane shows; (-1, 0/1) = volume/pan
        self.auto_arm = False            # Live's Automation Arm (session_automation_record)
        self.last_msg = 0.0
        self.toast_msg, self.toast_at = "", 0.0
        self._td_q = []                  # outstanding track_data requests: (kind, a, b, ns, at)
        self._hold = {}                  # key -> time of last local change
        self._sel_backoff = 0.0
        self._last_resync = 0.0
        self._stop = threading.Event()
        self._configured = False

    # ------------------------------------------------------------ plumbing

    def start(self):
        if not self.offline:
            for target in (self._recv_loop, self._poll_loop):
                threading.Thread(target=target, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        if not self.offline:
            self.rx.close()
            self.tx.close()

    @property
    def connected(self):
        return self.offline or time.time() - self.last_msg < PROBE_S

    def send(self, address, *args):
        if self.offline:
            return
        try:
            self.tx.sendto(encode(address, list(args)), (LIVE_HOST, LIVE_PORT))
        except OSError:
            pass

    def hold(self, key):
        self._hold[key] = time.time()

    def held(self, key):
        return time.time() - self._hold.get(key, 0) < HOLD_S

    def toast(self, msg):
        self.toast_msg, self.toast_at = msg, time.time()
        self.on_change()

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self.rx.recvfrom(65536)
                messages = decode(data)
            except socket.timeout:
                continue
            except Exception:
                continue
            self.last_msg = time.time()
            for address, args in messages:
                h = getattr(self, "_on_" + address.strip("/").replace("/", "_"), None)
                if h:
                    try:
                        with self.lock:
                            h(args)
                    except Exception as e:
                        print("live: error handling %s: %r" % (address, e), flush=True)
            self.on_change()

    def _poll_loop(self):
        tick = 0
        while not self._stop.wait(0.05):
            tick += 1
            connected = self.connected
            if not connected and self._configured and not self.offline:
                self._configured = False             # Live went away (closed or restarting): set up again later
            if not self._configured and connected:
                self._configured = True
                self.send("/live/api/set/log_level", "warning")   # AbletonOSC logs every poll at "info"
                self.send("/live/browser/index/start")              # build the search index in the background
                if self.num_tracks >= 0:             # a reconnect (Live restarted, maybe another set): reread it all
                    self.refresh_layout()
                    self.send("/live/song/get/scale")
                    if 0 <= self.sel < max(1, self.num_tracks):
                        self.send("/live/track/get/drum_pads", self.sel)
                    if self.mode == "device":
                        self.load_devices()
                    self.toast("Connected to Live again")
            if tick % 5 == 0:            # 250 ms
                for prop in ("is_playing", "tempo", "metronome", "session_record", "arrangement_overdub"):
                    self.send("/live/song/get/" + prop)
                self._request_fast()
            if self.mode == "mix" and tick % 2 == 0:      # 100 ms meters
                self._request_meters()
            if self.mode == "mix":
                self._poll_mixer(tick)
            if self.need_time and self.mode != "arrange" and tick % 2 == 0:
                self.send("/live/song/get/current_song_time")
            if tick % 40 == 0:                            # 2 s: Live's scale and the selected track's drum pads
                self.send("/live/song/get/scale")
                if 0 <= self.sel < self.num_tracks:
                    self.send("/live/track/get/drum_pads", self.sel)
            if tick % 2 == 0 and self.playing and (self._rec_watch or any(tr.arm for tr in self.tracks)):
                self.send("/live/song/get/recording_clips")          # 100 ms while something could record
            if tick % 20 == 0 and time.time() >= self._sel_backoff:     # 1 s
                self.send("/live/view/get/selected_track")
            if tick % 30 == 0 or (tick % 6 == 0 and self.num_tracks < 0):   # 1.5 s, faster until first contact
                self.send("/live/song/get/num_tracks")
                self.send("/live/song/get/num_scenes")
                self.send("/live/song/get/scenes/name")
                a, b = self.view_tracks
                for t, tr in enumerate(self.tracks):
                    if tr.armable:
                        self.send("/live/track/get/arm", t)
                    if a <= t < b:
                        self.send("/live/track/get/volume_string", t)
                        self.send("/live/track/get/panning", t)
                if self.mode == "device" and self.dev.track >= 0:
                    self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)
                    if self.dev.names and self.dev.is_simpler():      # markers may have moved in Live
                        self.send("/live/dev/get/sample", self.dev.track, self.dev.path())
                    if self.dev.view == "chains":
                        self.send("/live/dev/get/chains", self.dev.track, self.dev.path())
            if self.mode == "arrange":
                if (self.playing and tick % 2 == 0) or tick % 10 == 0:     # 100 ms playing, 500 ms stopped
                    self.send("/live/song/get/current_song_time")
                if tick % 20 == 0:
                    for prop in ("loop", "loop_start", "loop_length", "signature_numerator",
                                 "signature_denominator", "record_mode", "song_length", "back_to_arranger",
                                 "cue_points"):
                        self.send("/live/song/get/" + prop)
                if tick % 40 == 0:                        # 2 s: clips on the tracks in view
                    a, b = self.view_tracks
                    for index, track in enumerate(self.tracks):
                        if not a <= index < b:
                            track.arr_details.clear()
                    for t in range(a, min(b, self.num_tracks)):
                        for prop in ("name", "start_time", "length"):
                            self.send("/live/track/get/arrangement_clips/" + prop, t)
                # Spread thumbnail work over the cycle instead of asking Live for eight lanes at once.
                if tick % 5 == 0:
                    a, b = self.view_tracks
                    count = min(b, self.num_tracks)-a
                    if count > 0:
                        self.send('/live/arrangement/get', a+(tick//5)%count, *self.arr_view)
            if tick % 20 == 0:
                self.send("/live/song/get/session_automation_record")
                self.send("/live/mixer/get/main_controls")
                self.send('/live/automation/context')
                self.send("/live/song/get/count_in_duration")
            if self.mode == 'clip' and self.automation_target:
                if tick % 10 == 0 and not self.held('env'):
                    self.request_automation()
            elif self.mode == "clip" and self.clip_exists():
                t, s = self.clip
                if tick % 2 == 0 and self.playing:
                    self.send("/live/clip/get/playing_position", t, s)
                if tick % 20 == 0:
                    self.request_clip()
            if self.mode == "device" and self.dev.names and self.dev.is_simpler() and (self.dev.sample or {}).get("file"):
                self.send("/live/dev/get/playhead", self.dev.track, self.dev.path())     # 50 ms: Simpler's playhead
            if self.mode == "device" and tick % 6 == 0:   # 300 ms
                d = self.dev
                if d.track >= 0 and d.names and d.pnames and not d.stale:
                    self.send("/live/dev/get/values", d.track, d.path())

    # ------------------------------------------------------------ track_data

    def _td(self, kind, a, b, props):
        now = time.time()
        self._td_q = [q for q in self._td_q if now - q[4] < 2.0]
        self._td_q.append((kind, a, b, self.num_scenes, now))
        self.send("/live/song/get/track_data", a, b, *props)

    def _request_clip_data(self):
        for a in range(0, self.num_tracks, CHUNK):
            self._td("slow", a, min(a + CHUNK, self.num_tracks), SLOW_PROPS)

    def _request_fast(self):
        if self.num_tracks > 0:
            self._td("fast", 0, self.num_tracks, FAST_PROPS)

    def _request_meters(self):
        a, b = self.view_tracks
        b = min(b, self.num_tracks)
        if b > a:
            self._td("meter", a, b, METER_PROPS)

    @staticmethod
    def _expected(q):
        kind, a, b, ns, _ = q
        per = {"fast": len(FAST_PROPS), "meter": 1}.get(kind, SLOW_TRACK_PROPS + 3 * ns)
        return (b - a) * per

    def _on_live_song_get_track_data(self, v):
        q = None
        while self._td_q:                 # find the request this answers; anything before it was lost
            cand = self._td_q.pop(0)
            if self._expected(cand) == len(v):
                q = cand
                break
        if not q:
            return
        kind, a, b, ns, _ = q
        i = 0
        for t in range(a, b):
            tr = self.tracks[t] if t < len(self.tracks) else None
            if kind == "fast":
                if tr:
                    tr.playing, tr.fired = v[i], v[i + 1]
                    if not self.held("mute%d" % t):
                        tr.mute = bool(v[i + 2])
                    if not self.held("solo%d" % t):
                        tr.solo = bool(v[i + 3])
                i += 4
            elif kind == "meter":
                if tr:
                    tr.meter = float(v[i] or 0)
                i += 1
            else:
                if tr:
                    tr.name, tr.color, tr.armable = v[i], v[i + 1] or 0x808080, bool(v[i + 2])
                    tr.midi_in = bool(v[i + 3])
                    j = i + SLOW_TRACK_PROPS
                    has, names, colors = v[j:j + ns], v[j + ns:j + 2 * ns], v[j + 2 * ns:j + 3 * ns]
                    tr.clips = [(bool(has[s]), names[s] or "", colors[s] or 0) for s in range(len(has))]
                i += SLOW_TRACK_PROPS + 3 * ns

    # ------------------------------------------------------------ song

    def _on_live_song_get_is_playing(self, v):
        self.playing = bool(v[0])

    def _on_live_song_get_tempo(self, v):
        if not self.held("tempo"):
            self.tempo = float(v[0])

    def _on_live_song_get_metronome(self, v):
        self.metro = bool(v[0])

    def _on_live_song_get_count_in_duration(self, v):
        if v and int(v[0]) in (0, 1, 2, 3):
            self.count_in = int(v[0])

    def _on_live_song_get_session_record(self, v):
        self.rec = bool(v[0])

    def _on_live_song_get_arrangement_overdub(self, v):
        self.overdub = bool(v[0])

    def _on_live_song_get_current_song_time(self, v):
        if not self.held("time"):
            self.song_time = float(v[0])
            self.song_time_at = time.time()

    def _on_live_song_get_song_length(self, v):
        self.song_len = float(v[0])

    def _on_live_song_get_loop(self, v):
        if not self.held("loop_on"):
            self.loop = bool(v[0])

    def _on_live_song_get_loop_start(self, v):
        if not self.held("loop"):
            self.loop_start = float(v[0])

    def _on_live_song_get_loop_length(self, v):
        if not self.held("loop"):
            self.loop_len = float(v[0])

    def _on_live_song_get_signature_numerator(self, v):
        self.sig[0] = int(v[0]) or 4

    def _on_live_song_get_signature_denominator(self, v):
        self.sig[1] = int(v[0]) or 4

    def _on_live_song_get_record_mode(self, v):
        if not self.held("rec_mode"):
            self.rec_mode = bool(v[0])

    def _on_live_song_get_back_to_arranger(self, v):
        self.back_to_arranger = bool(v[0])

    def _on_live_song_get_cue_points(self, v):
        self.cues = [(v[i], float(v[i + 1])) for i in range(0, len(v) - 1, 2)]

    def beats_per_bar(self):
        return self.sig[0] * 4.0 / self.sig[1]

    # ------------------------------------------------------------ clip page

    CLIP_PROPS = ("name", "color", "length", "loop_start", "loop_end", "is_midi_clip", "playing_position")
    DETAIL_KEYS = ("audio", "length", "looping", "loop_start", "loop_end", "start_marker", "end_marker", "launch_mode",
                   "launch_quantization", "legato", "sig_num", "sig_den")
    AUDIO_KEYS = ("gain", "gain_str", "pitch_coarse", "pitch_fine", "warping", "warp_mode", "file", "sample_length",
                  "f_start", "f_end", "f_loop_start", "f_loop_end")

    def _on_live_clip_get_details(self, v):
        """Clip page DETAILS: everything about the open clip (see the add-on's /live/clip/get/details)."""
        if len(v) < 14 or self.clip != (v[0], v[1]) or isinstance(v[2], str):
            return
        d = dict(zip(self.DETAIL_KEYS, v[2:14]))
        if d["audio"] and len(v) >= 26:
            d.update(zip(self.AUDIO_KEYS, v[14:26]))
            d["warp_modes"] = list(v[26:])
        if self.held("details"):                    # a drag is still sending: keep what it set
            d.update({k: self.clip_details[k] for k in self._detail_pending if k in self.clip_details})
        self.clip_details = d

    def set_clip_detail(self, key, value):
        """Change one clip setting: start_marker / end_marker / loop_start / loop_length (beats, kept valid by the
        add-on), looping, launch_mode, launch_quantization, legato, gain, pitch_coarse, pitch_fine, warping,
        warp_mode."""
        if not self.clip_exists():
            return
        t, s = self.clip
        d = self.clip_details
        self.hold("details")
        self._detail_pending = {key, "loop_end"} if key in ("loop_start", "loop_length") else {key}
        if key == "loop_length":
            d["loop_end"] = d.get("loop_start", 0.0) + value
        elif key == "loop_start":
            d["loop_end"] = value + (d.get("loop_end", 0.0) - d.get("loop_start", 0.0))
        if key != "loop_length":
            d[key] = value
        if key in ("start_marker", "end_marker", "loop_start", "loop_length"):
            self.send("/live/clip/set/region", t, s, key, float(value))
        else:
            self.send("/live/clip/set/" + key, t, s, float(value) if key in ("gain", "pitch_fine") else int(value))
            self.send("/live/clip/get/details", t, s)
        if key in ("loop_start", "loop_length"):
            self.clip_info["loop_start"] = d.get("loop_start", 0.0)
            self.clip_info["loop_end"] = d.get("loop_end", 0.0)

    def note_tool(self, op, amount=0.0):
        """MIDI clip note tools: transpose, velocity, humanize, legato (all notes; Live's UNDO undoes them)."""
        if self.clip_exists():
            t, s = self.clip
            self.send("/live/clip/notes/tool", t, s, op, float(amount))
            self.send("/live/clip/get/notes", t, s)

    def crop_clip(self):
        if self.clip_exists():
            self.send("/live/clip/crop", *self.clip)
            self.request_clip()

    def clip_exists(self):
        if not self.clip:
            return False
        t, s = self.clip
        tr = self.tracks[t] if 0 <= t < len(self.tracks) else None
        return bool(tr and s < len(tr.clips) and tr.clips[s][0])

    def open_clip(self, t, s):
        if self.clip != (t, s):
            self.clip, self.clip_info, self.notes, self.env = (t, s), {}, [], None
            self.clip_details = {}
        self.request_clip()

    def request_clip(self):
        """Fetch the clip's properties, notes and the shown envelope (only if the slot has a clip:
        asking an empty slot makes AbletonOSC log a traceback)."""
        if not self.clip_exists():
            return
        t, s = self.clip
        for prop in self.CLIP_PROPS:
            self.send("/live/clip/get/" + prop, t, s)
        if not self.held("notes"):
            self.send("/live/clip/get/notes", t, s)
        if self.want_details and not self.held("details"):
            self.send("/live/clip/get/details", t, s)
        if not self.held("env"):
            self.send("/live/clip/get/envelope", t, s, self.env_key[0], self.env_key[1], 64)

    def _clip_reply(self, prop, v):
        if len(v) >= 3 and self.clip == (v[0], v[1]):
            self.clip_info[prop] = v[2]

    def _on_live_clip_get_name(self, v):
        self._clip_reply("name", v)

    def _on_live_clip_get_color(self, v):
        self._clip_reply("color", v)

    def _on_live_clip_get_length(self, v):
        self._clip_reply("length", v)

    def _on_live_clip_get_loop_start(self, v):
        self._clip_reply("loop_start", v)

    def _on_live_clip_get_loop_end(self, v):
        self._clip_reply("loop_end", v)

    def _on_live_clip_get_is_midi_clip(self, v):
        self._clip_reply("is_midi_clip", v)

    def _on_live_clip_get_playing_position(self, v):
        self._clip_reply("playing_position", v)

    def _on_live_clip_get_notes(self, v):
        if len(v) >= 2 and self.clip == (v[0], v[1]) and not self.held("notes"):
            n = v[2:]
            self.notes = [list(n[i:i + 5]) for i in range(0, len(n) - 4, 5)]

    def _on_live_clip_get_envelope(self, v):
        if len(v) < 5 or self.clip != (v[0], v[1]) or (v[2], v[3]) != tuple(self.env_key) or self.held("env"):
            return
        if v[4] == -1:
            self.env = {"key": (v[2], v[3]), "exists": False, "min": 0.0, "max": 1.0, "vals": [],
                        "error": v[5] if len(v) > 5 else "?"}
            return
        self.env = {"key": (v[2], v[3]), "exists": bool(v[4]), "min": float(v[5]), "max": float(v[6]),
                    "vals": [float(x) for x in v[7:]]}

    def _on_live_song_get_session_automation_record(self, v):
        if not self.held("auto_arm"):
            self.auto_arm = bool(v[0])

    def clip_range(self):
        """(loop_start, loop_end) in beats, with a 1-bar fallback until Live answers."""
        i = self.clip_info
        try:
            a = float(i.get("loop_start", 0.0) or 0.0)
            b = float(i.get("loop_end", 0.0) or 0.0)
        except (TypeError, ValueError):
            a = b = 0.0
        if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
            return 0.0, self.beats_per_bar()
        # A clip that's still recording reports a huge loop end; drawing all of it hung the app (2026-10-29).
        return a, min(b, a + MAX_CLIP_BEATS)

    def add_note(self, pitch, start, dur, vel=100):
        t, s = self.clip
        self.notes.append([pitch, start, dur, vel, False])
        self.hold("notes")
        self.send("/live/clip/add/notes", t, s, int(pitch), float(start), float(dur), float(vel), 0)

    def remove_note(self, note):
        t, s = self.clip
        if note in self.notes:
            self.notes.remove(note)
        self.hold("notes")
        # removes notes of this pitch that start inside [start, start + 1/64 beat)
        self.send("/live/clip/remove/notes", t, s, int(note[0]), 1, float(note[1]), 1 / 64)

    def set_note_velocity(self, note, vel):
        vel = max(1, min(127, int(vel)))
        self.remove_note(note)
        self.add_note(note[0], note[1], note[2], vel)
        return self.notes[-1]

    def clear_notes(self):
        t, s = self.clip
        self.notes = []
        self.hold("notes")
        self.send("/live/clip/remove/notes", t, s)

    def create_clip(self, t, s, length):
        self.send("/live/clip_slot/create_clip", t, s, float(length))
        self.toast("New %d-bar clip" % round(length / self.beats_per_bar()))

    def env_draw(self, time, length, value):
        """Write one automation step into the shown envelope (local copy updated too)."""
        t, s = self.clip
        d, p = self.env_key
        e = self.env
        if e is not None:
            e["exists"] = True
            a, b = self.clip_range()
            n = len(e["vals"]) or 64
            if not e["vals"]:
                e["vals"] = [value] * n
            for i in range(n):
                x = a + (b - a) * i / n
                if time <= x < time + length:
                    e["vals"][i] = value
        self.hold("env")
        self.send("/live/clip/set/envelope_step", t, s, d, p, float(time), float(length), float(value))

    def env_clear(self):
        t, s = self.clip
        d, p = self.env_key
        self.env = None
        self.send("/live/clip/clear/envelope", t, s, d, p)
        self.send("/live/clip/get/envelope", t, s, d, p, 64)

    def set_env_key(self, key):
        self.env_key, self.env = tuple(key), None
        self.request_clip()

    def toggle_auto_arm(self):
        self.auto_arm = not self.auto_arm
        self.hold("auto_arm")
        self.send("/live/song/set/session_automation_record", int(self.auto_arm))

    def _arr_reply(self, key, v):
        t = v[0] if v else -1
        if isinstance(t, int) and 0 <= t < len(self.tracks):
            self.tracks[t].arr[key] = list(v[1:])

    def _on_live_automation_context(self,v):
        self.native_arrangement_visible=bool(v[0])
        self.native_draw_mode=bool(v[1])

    def _on_live_automation_native(self,v):
        if str(v[0])==str(self.automation_token):
            self.native_automation_result=json.loads(v[1])
            self.native_draw_mode=bool(self.native_automation_result.get('draw_mode',False))

    def _on_live_automation_draw_mode(self,v):
        self.native_draw_mode=bool(v[0])

    def request_automation(self, op='get', values=()):
        self.send('/live/automation',str(self.automation_token),op,
                  json.dumps(self.automation_target,separators=(',',':')),*values)

    def _on_live_automation(self, v):
        if not self.automation_target or str(v[0]) != str(self.automation_token):
            return
        data = json.loads(v[1])
        if data.get('error'):
            self.toast(data['error'])
            if self.automation_data is None:
                self.automation_data = data
            return
        self.automation_data = data
        self.env = data

    def _on_live_arrangement_list(self, v):
        if len(v) == 2 and 0 <= v[0] < len(self.tracks):
            self.tracks[v[0]].arr_snapshot = [tuple(c) for c in json.loads(v[1])]

    def _on_live_arrangement_detail(self, v):
        if len(v) != 2 or not 0 <= v[0] < len(self.tracks):
            return
        data = json.loads(v[1])
        tr = self.tracks[v[0]]
        key = (round(data['start'], 4), round(data['length'], 4), data['name'])
        # Only cache visible thumbnails; old zoom/track data cannot grow without bound.
        if len(tr.arr_details) >= 64 and key not in tr.arr_details:
            tr.arr_details.pop(next(iter(tr.arr_details)))
        tr.arr_details[key] = data

    def _on_live_track_get_arrangement_clips_name(self, v):
        self._arr_reply("name", v)

    def _on_live_track_get_arrangement_clips_start_time(self, v):
        self._arr_reply("start_time", v)

    def _on_live_track_get_arrangement_clips_length(self, v):
        self._arr_reply("length", v)

    def _on_live_song_get_num_tracks(self, v):
        n = v[0]
        if n != self.num_tracks:
            self.num_tracks = n
            while len(self.tracks) < n:
                self.tracks.append(Track())
            del self.tracks[n:]
            self.sel = min(self.sel, max(0, n - 1))

    def _on_live_song_get_num_scenes(self, v):
        self.num_scenes = v[0]
        self._request_clip_data()

    def _on_live_song_get_scenes_name(self, v):
        self.scenes = list(v)

    def _on_live_view_get_selected_track(self, v):
        t = v[0]
        if isinstance(t, int) and t != self.sel and t >= 0:
            self.sel = t
            self.drum_pads, self.drum_track = None, -1
            self.send("/live/track/get/drum_pads", t)
            if self.mode == "device":
                self.load_devices()

    def _on_live_error(self, v):
        msg = str(v[0]) if v else ""
        now = time.time()
        if "not in list" in msg:
            self._sel_backoff = now + 5
        if now - self._last_resync < 0.5:
            return
        self._last_resync = now
        self.send("/live/song/get/num_tracks")
        self.send("/live/song/get/num_scenes")
        if self.mode == "device":
            self.dev.stale = True
            self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)

    # ------------------------------------------------------------ tracks

    def _on_live_track_get_arm(self, v):
        t, val = v[0], v[1]
        if 0 <= t < len(self.tracks) and not self.held("arm%d" % t):
            self.tracks[t].arm = bool(val)

    def _on_live_track_get_volume_string(self, v):
        if len(v) < 3 or not isinstance(v[1], (int, float)):
            return
        t = v[0]
        if 0 <= t < len(self.tracks):
            if not self.held("vol%d" % t):
                self.tracks[t].vol = float(v[1])
            self.tracks[t].vol_str = v[2]

    def _on_live_track_get_panning(self, v):
        t = v[0]
        if 0 <= t < len(self.tracks) and not self.held("pan%d" % t):
            self.tracks[t].pan = float(v[1])

    # ------------------------------------------------------------ session recordings -> arrangement

    def _on_live_song_get_recording_clips(self, v):
        """Remember where each Session recording started; once it stops, copy the clip into the arrangement there.
        A recording only counts as stopped after two replies without it (a lost UDP reply must not cut it short)."""
        if self.mode == 'arrange':
            self._rec_watch.clear()
            return  # direct Arrangement recording must not also mirror Session clips
        now = {(v[k], v[k + 1]): v[k + 2] for k in range(1, len(v) - 2, 3)}
        for key, start in now.items():
            w = self._rec_watch.setdefault(key, [start, 0])
            w[1] = 0
        for key in list(self._rec_watch):
            if key in now:
                continue
            w = self._rec_watch[key]
            w[1] += 1
            if w[1] >= 2:
                del self._rec_watch[key]
                if self.copy_recordings:
                    self.copy_to_arrangement(key[0], key[1], w[0])

    def copy_to_arrangement(self, t, s, at=-1.0):
        """Copy a Session clip into its track's arrangement at beat `at` (-1 = the bar the playhead is in)."""
        self.send("/live/clip/to_arrangement", t, s, float(at))

    def _on_live_arrangement_edit(self, v):
        t, result = int(v[0]), str(v[1])
        self.toast("Arrangement updated" if result == "ok" else result)
        if not result.startswith('error:'):
            for prop in ("name", "start_time", "length"):
                self.send("/live/track/get/arrangement_clips/" + prop, t)
            self.send('/live/arrangement/get', t, *self.arr_view)

    def _on_live_clip_to_arrangement(self, v):
        if len(v) >= 4 and v[2] == "ok":
            self.toast("In the arrangement too: %s, bar %d" % (self.track_name(v[0]), int(v[3] // self.beats_per_bar()) + 1))
        elif len(v) >= 3:
            self.toast("Couldn't copy to the arrangement (%s)" % v[2])

    # ------------------------------------------------------------ mixer: returns, master, sends, routing

    def _on_live_mixer_get_main_controls(self, v):
        if len(v) == 8:
            self.main_controls = {name: tuple(v[i:i+4]) for name, i in (("cue_volume", 0), ("crossfader", 4))}

    def _poll_mixer(self, tick):
        a, b = self.view_tracks
        if self.mix_returns and tick % 4 == 0:           # 200 ms: the returns and master on screen
            self.send("/live/mixer/get/strips", "return", 0, 8)
            self.send("/live/mixer/get/strips", "master", 0, 1)
        elif tick % 20 == 0:                              # 1 s: return names for the Sends view
            self.send("/live/mixer/get/strips", "return", 0, 8)
        if not self.mix_returns and self.mix_view == "sends" and tick % 4 == 0:
            self.send("/live/mixer/get/strips", "track", a, b)
        if not self.mix_returns and self.mix_view == "io" and tick % 20 == 0:
            for t in range(a, min(b, self.num_tracks)):
                self.send("/live/mixer/get/routing", t)

    def strip(self, key):
        """("track", t) / ("return", r) / ("master", 0) -> its Track or Strip, or None."""
        kind, i = key
        if kind == "track":
            return self.tracks[i] if 0 <= i < len(self.tracks) else None
        if kind == "return":
            return self.returns[i] if 0 <= i < len(self.returns) else None
        return self.master

    @staticmethod
    def _hold_key(kind, i, what):
        return {"track": "%s%d" % (what, i), "return": "r%s%d" % (what, i)}.get(kind, "m" + what)

    def _on_live_mixer_count(self, v):
        n = v[1]
        while len(self.returns) < n:
            self.returns.append(Strip())
        del self.returns[n:]

    def _on_live_mixer_strip(self, v):
        if len(v) < 11:
            return
        kind, i = v[0], v[1]
        s = self.strip((kind, i))
        if s is None:
            return
        if kind != "track":
            s.name, s.color = v[2], v[3]
        hk = lambda what: self._hold_key(kind, i, what)
        if not self.held(hk("vol")):
            s.vol = float(v[4])
        s.vol_str = v[5]
        if not self.held(hk("pan")):
            s.pan = float(v[6])
        if not self.held(hk("mute")):
            s.mute = bool(v[7])
        if not self.held(hk("solo")):
            s.solo = bool(v[8])
        if kind != "track":
            s.meter = float(v[9])
        n = v[10]
        vals, strs = list(v[11:11 + n]), list(v[11 + n:11 + 2 * n])
        s.sends = [s.sends[k] if k < len(s.sends) and self.held(hk("send%d" % k)) else float(vals[k])
                   for k in range(n)]
        s.send_strs = strs

    def set_strip(self, key, what, v):
        """Volume ("volume", 0..1), pan ("panning", -1..1) or a send ("send<n>", 0..1) of any strip."""
        kind, i = key
        s = self.strip(key)
        if s is None:
            return
        if what == "volume":
            s.vol = v = min(1.0, max(0.0, v))
            self.hold(self._hold_key(kind, i, "vol"))
        elif what == "panning":
            s.pan = v = min(1.0, max(-1.0, v))
            self.hold(self._hold_key(kind, i, "pan"))
        else:
            n = int(what[4:])
            if n >= len(s.sends):
                return
            s.sends[n] = v = min(1.0, max(0.0, v))
            self.hold(self._hold_key(kind, i, "send%d" % n))
        self.send("/live/mixer/set", kind, i, what, float(v))

    def toggle_strip(self, key, what):
        kind, i = key
        s = self.strip(key)
        if s is None or kind == "master":
            return
        setattr(s, what, not getattr(s, what))
        self.hold(self._hold_key(kind, i, what))
        self.send("/live/mixer/toggle", kind, i, what)

    def _on_live_mixer_get_routing(self, v):
        if len(v) >= 6 and 0 <= v[0] < len(self.tracks):
            self.tracks[v[0]].route = tuple(v[1:6])

    def ask_routing_options(self, t, field, then):
        """then(options) with the names Live offers for a track's input/output type or channel."""
        self._route_cb = (t, field, then)
        self.send("/live/mixer/get/routing_options", t, field)

    def _on_live_mixer_get_routing_options(self, v):
        cb, self._route_cb = getattr(self, "_route_cb", None), None
        if cb and len(v) >= 2 and (v[0], v[1]) == cb[:2]:
            cb[2](list(v[2:]))

    def set_routing(self, t, field, name):
        self.send("/live/mixer/set/routing", t, field, name)
        self.send("/live/mixer/get/routing", t)

    def set_monitor(self, t, state):
        tr = self.tracks[t] if 0 <= t < len(self.tracks) else None
        if tr and tr.route:
            tr.route = tr.route[:4] + (state,)
        self.send("/live/track/set/current_monitoring_state", t, int(state))
        self.send("/live/mixer/get/routing", t)

    # ------------------------------------------------------------ devices

    def load_devices(self):
        """Start the Device page on the selected track's own device list."""
        self.dev = Device(self.sel)
        self.send("/live/chain/get/devices", self.sel, "")

    def _load_params(self):
        d = self.dev
        d.pnames, d.pvals, d.pmin, d.pmax, d.pq, d.pstr = [], [], [], [], [], {}
        d.layout = None                 # the old device's pages must go with its parameters
        d.sample = None
        d.presets, d.preset_sel = [], -1
        if d.names:
            self.send("/live/dev/get/params", d.track, d.path())
            if d.selected_info()[0] in ("PluginDevice", "AuPluginDevice"):
                self.send("/live/dev/get/presets", d.track, d.path())
            if d.is_simpler():
                self.send("/live/dev/get/sample", d.track, d.path())

    def _on_live_dev_get_presets(self, v):
        d = self.dev
        if len(v) >= 3 and v[0] == d.track and str(v[1]) == d.path():
            d.preset_sel, d.presets = v[2], list(v[3:])

    def ask_device_presets(self, sub, then):
        """then(items) with the selected stock device's presets in folder `sub` (a list of folder names);
        items is None when Live's browser has no presets for it."""
        d = self.dev
        self._presets_cb = (d.track, d.path(), list(sub), then)
        self.send("/live/dev/presets", d.track, d.path(), *sub)

    def _on_live_dev_presets(self, v):
        cb, self._presets_cb = getattr(self, "_presets_cb", None), None
        if cb and len(v) >= 3 and (v[0], str(v[1])) == cb[:2]:
            cb[3](None if v[2] < 0 else [x for x in v[3:] if not str(x).startswith("error")])

    def load_device_preset(self, sub, name):
        """Swap the selected device for a preset from its folder `sub`, in place (Push's Swap)."""
        d = self.dev
        self.send("/live/dev/preset/load", d.track, d.path(), *sub, name)

    def _on_live_dev_preset_load(self, v):
        if len(v) >= 4 and v[3] == "ok":
            self.toast("Loaded " + str(v[2]).rsplit(".", 1)[0])
            if self.dev.track == v[0]:              # the device was replaced: read its chain and knobs again
                self.dev.stale = True
                self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)
                self._load_params()
        elif len(v) >= 4:
            self.toast("Couldn't load %s (%s)" % (v[2], v[3]))

    def set_preset(self, i):
        d = self.dev
        d.preset_sel = i
        self.send("/live/dev/set/preset", d.track, d.path(), int(i))
        if 0 <= i < len(d.presets):
            self.toast("Preset: " + d.presets[i])

    def _on_live_dev_get_playhead(self, v):
        d = self.dev
        if len(v) >= 4 and v[0] == d.track and str(v[1]) == d.path():
            d.playhead = v[2] if v[3] else None

    def _on_live_dev_get_sample(self, v):
        d = self.dev
        if len(v) < 3 or v[0] != d.track or str(v[1]) != d.path():
            return
        if self.held("sample") and v[2] and d.sample and d.sample.get("file"):
            return                  # a drag is still sending; don't jump back to an older reply
        if not v[2] or len(v) < 7:
            d.sample = {"file": ""}
            return
        d.sample = {"file": v[2], "length": v[3], "start": v[4], "end": v[5], "mode": v[6]}
        if len(v) >= 10:            # warping, slicing style, slice points (frames)
            d.sample.update(warp=bool(v[7]), style=v[8], slices=list(v[10:10 + v[9]]))

    def set_sample(self, what, value):
        """Simpler: start_marker / end_marker (frames), warping, slicing_style or playback_mode."""
        d = self.dev
        s = d.sample or {}
        key = {"start_marker": "start", "end_marker": "end", "playback_mode": "mode", "warping": "warp",
               "slicing_style": "style"}.get(what)
        if key and s.get("file"):
            s[key] = value                                  # show it at once; Live's reply corrects it
        self.hold("sample")
        self.send("/live/dev/set/sample", d.track, d.path(), what, int(value))

    def slice_op(self, op, a=0, b=0):
        """Simpler slices: "insert" at frame a, "move" a -> b, "clear", "reset"."""
        d = self.dev
        s = d.sample or {}
        if op == "insert" and "slices" in s:
            s["slices"] = sorted(set(s["slices"]) | {int(a)})
        elif op == "move" and a in s.get("slices", []):
            s["slices"] = sorted((set(s["slices"]) - {a}) | {int(b)})
        self.hold("sample")
        self.send("/live/dev/sample/slice", d.track, d.path(), op, int(a), int(b))

    def _on_live_chain_get_devices(self, v):
        d = self.dev
        if len(v) < 3 or v[0] != d.track or str(v[1]) != d.chain:
            return
        d.stale, d.loaded = False, True
        n = v[2]
        if n < 0:                                   # the chain is gone (rack deleted?): back to the track
            if d.chain:
                self.dev = Device(d.track)
                self.send("/live/chain/get/devices", d.track, "")
            return
        rest = v[3:]
        names = [rest[k * 6] for k in range(n)]
        d.info = [tuple(rest[k * 6 + 1:k * 6 + 6]) for k in range(n)]
        changed = names != d.names
        d.names = names
        if d.idx >= len(names):
            d.idx = max(0, len(names) - 1)
        if changed:
            self._load_params()
        elif d.is_simpler() and d.sample is None:     # class info arrived after the params request
            self.send("/live/dev/get/sample", d.track, d.path())

    def _on_live_dev_get_params(self, v):
        d = self.dev
        if len(v) < 3 or v[0] != d.track or str(v[1]) != d.path() or v[2] < 0:
            return
        n, rest = v[2], v[3:]
        rows = [rest[k * 6:k * 6 + 6] for k in range(n)]
        d.pnames = [r[0] for r in rows]
        d.pvals = [float(r[1]) for r in rows]
        d.pmin = [float(r[2]) for r in rows]
        d.pmax = [float(r[3]) for r in rows]
        d.pq = [bool(r[4]) for r in rows]
        d.pstr = {p: r[5] for p, r in enumerate(rows)}
        d.layout = device_layouts.resolve(d.selected_info()[0], d.pnames)
        # devices drawn as a picture (EQ curve, compressor) need every value's text, not just the page's
        d.watch_all = d.selected_info()[0] in ("Eq8", "AutoFilter2", "Compressor2", "GlueCompressor", "InstrumentVector",
                                             "Drift", "Operator", "UltraAnalog", "MultiSampler", "InstrumentMeld",
                                             "Reverb", "Hybrid", "Delay", "Echo", "Saturator", "StereoGain")
        if d.bank >= d.num_banks():
            d.bank = 0

    def _on_live_dev_get_values(self, v):
        d = self.dev
        if len(v) < 2 or v[0] != d.track or str(v[1]) != d.path():
            return
        vals = v[2:]
        if vals and isinstance(vals[0], str):       # error text: the device went away
            d.stale = True
            return
        changed = []
        for p, val in enumerate(vals):
            if p >= len(d.pvals) or self.held("p%d" % p):
                continue
            if d.pvals[p] != val:
                d.pvals[p] = float(val)
                changed.append(p)
        shown = [p for p in changed if p in d.bank_params() or p == 0 or getattr(d, "watch_all", False)]
        if shown:
            self.send("/live/dev/get/strings", d.track, d.path(), *shown)

    def _on_live_dev_get_strings(self, v):
        d = self.dev
        if len(v) < 2 or v[0] != d.track or str(v[1]) != d.path():
            return
        rest = v[2:]
        for k in range(0, len(rest) - 1, 2):
            if isinstance(rest[k], int):
                d.pstr[rest[k]] = rest[k + 1]

    def _on_live_dev_get_chains(self, v):
        d = self.dev
        if len(v) < 4 or v[0] != d.track or str(v[1]) != d.path() or v[3] < 0:
            return
        d.drum = bool(v[2])
        rest = v[4:]
        d.chains = [(rest[k * 2], rest[k * 2 + 1]) for k in range(v[3])]

    def _on_live_dev_insert(self, v):
        if len(v) >= 4 and v[3] != "ok":
            self.toast("Couldn't add %s (%s)" % (v[2], v[3]))
        elif len(v) >= 4:
            self.toast("Added %s" % v[2])
        self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)

    def _on_live_dev_delete(self, v):
        if len(v) >= 3 and v[2] != "ok":
            self.toast("Couldn't delete (%s)" % v[2])
        self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)

    def _on_live_dev_rack_action(self, v):
        self.toast(str(v[2]))
        if str(v[2]).startswith("ok"):
            self._load_params()

    def _on_live_dev_move(self, v):
        if self.dev.track != int(v[0]) or self.dev.chain != str(v[1]):
            return
        if v[2] == "ok":
            self.dev.idx = int(v[3])
            self.dev.bank = 0
            self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)
            self._load_params()
        else:
            self.toast(str(v[2]))

    def _on_live_dev_duplicate(self, v):
        self.toast("Device duplicated" if v[2] == "ok" else str(v[2]))
        if self.dev.track == int(v[0]) and self.dev.chain == str(v[1]):
            self.send("/live/chain/get/devices", self.dev.track, self.dev.chain)

    def request_bank_strings(self):
        d = self.dev
        if d.names and d.pnames:
            self.send("/live/dev/get/strings", d.track, d.path(), *d.bank_params())

    # --- chain navigation (Device page)

    def open_rack(self):
        """Show the selected rack's chains (Drum Rack: its pads)."""
        d = self.dev
        info = d.selected_info()
        if not info[2]:
            return False
        d.view, d.chains, d.drum = "chains", [], bool(info[3])
        self.send("/live/dev/get/chains", d.track, d.path())
        return True

    def enter_chain(self, ci):
        d = self.dev
        rack = d.names[d.idx] if d.idx < len(d.names) else "Rack"
        chain = d.chains[ci][0] if ci < len(d.chains) else "Chain %d" % (ci + 1)
        d.crumbs = d.crumbs + [rack, chain or "Chain %d" % (ci + 1)]
        d.chain, d.idx, d.view, d.bank = "%s/%d" % (d.path(), ci), 0, "devices", 0
        d.names, d.info, d.loaded = [], [], False
        self._load_params()
        self.send("/live/chain/get/devices", d.track, d.chain)

    def chain_up(self):
        """Back one level: chains view -> the rack's devices; a chain -> its rack's chains."""
        d = self.dev
        if d.view == "chains":
            d.view = "devices"
            return True
        if not d.chain:
            return False
        parts = d.chain.split("/")                   # ".../rack/chain": land on that rack's chain list
        d.chain, d.idx = "/".join(parts[:-2]), int(parts[-2])
        d.crumbs = d.crumbs[:-2]
        d.names, d.info, d.loaded, d.bank = [], [], False, 0
        d.view, d.chains = "chains", []
        self._load_params()
        self.send("/live/chain/get/devices", d.track, d.chain)
        self.send("/live/dev/get/chains", d.track, d.path())
        return True

    def add_device(self, name):
        d = self.dev
        at = d.idx + 1 if d.names else 0
        self.send("/live/dev/insert", d.track, d.chain, name, at)
        d.idx = at
        self.toast("Adding %s" % name)

    def request_plugins(self):
        if self.plugins is None:
            self.send("/live/browser/list_plugins_all")

    def _on_live_browser_list_plugins_all(self, v):
        self.plugins = list(v)

    def add_plugin(self, name):
        """Load a plug-in after the selected device (Live's browser does plug-ins; insert_device can't)."""
        d = self.dev
        self.send("/live/dev/load_plugin", d.track, d.path() if d.names else "", name)
        if d.names:
            d.idx += 1
        self.toast("Loading %s…" % name)

    def delete_device(self):
        d = self.dev
        if d.names:
            name = d.names[d.idx]
            self.send("/live/dev/delete", d.track, d.path())
            d.idx = max(0, d.idx - 1)
            self.toast("Deleted %s (UNDO brings it back)" % name)

    # ------------------------------------------------------------ browser

    def user_place_dir(self, path=None):
        """Real folder for a ["userplace:<name>", sub, ...] path, or None."""
        path = self.br_path if path is None else path
        if not path or not path[0].startswith("userplace:"):
            return None
        base = dict(places.read()).get(path[0][len("userplace:"):])
        return os.path.join(base, *path[1:]) if base else None

    def search(self, query, place_hits):
        """Show search results in the Browse list: your Places samples (place_hits, already found on disk)
        first, then Live's browser (asked from the add-on's index)."""
        self.br_path, self.br_root, self.br_pending = [SEARCH, query], False, True
        self._place_hits = place_hits
        self._show_hits([])
        self.br_pending = True
        self.send("/live/browser/find", query, 80)

    def _show_hits(self, live_paths):
        items, hints, hits = [], {}, []
        for path, place, rel in self._place_hits:
            parts = [x for x in rel.split(os.sep) if x]       # the last two folders tell same-named files apart
            hints[len(items)] = "  ›  ".join(parts[-2:]) if parts else place
            items.append(os.path.basename(path))
            hits.append(("place", path))
        for p in live_paths:
            hints[len(items)] = "  ›  ".join([{"audio_effects": "Audio Effects", "midi_effects": "MIDI Effects",
                                               "max_for_live": "Max for Live", "user_library": "User Library"}
                                              .get(p[0], p[0].replace("_", " ").title())] + p[1:-1])
            items.append(p[-1])
            hits.append(("live", p))
        self.br_items, self.br_hints, self.search_hits = items, hints, hits

    def _on_live_browser_find(self, v):
        if self.br_path[:1] == [SEARCH] and v and str(v[0]) == self.br_path[1].lower().strip():
            self.index_state = (v[1], self.index_state[1]) if len(v) > 1 else self.index_state
            self._show_hits([str(x).split("\t") for x in v[2:]])
            self.br_pending = False

    def _on_live_browser_index_status(self, v):
        if len(v) >= 2:
            self.index_state = (str(v[0]), int(v[1]))

    def browse(self):
        if self.br_path[:1] == [SEARCH]:            # results stay until you leave them
            return
        self.br_hints = {}
        self.br_items, self.br_pending = [], True
        folder = self.user_place_dir()
        if folder is not None:                   # your own Places folder: list it straight from disk
            try:
                self.br_items = places.list_dir(folder)
            except OSError as e:
                self.toast("Can't open %s (%s)" % (folder, e.strerror))
            self.br_root, self.br_pending = False, False
            self.on_change()
            return
        if self.br_path and self.br_path[0] != PLACES:
            self.send("/live/browser/list_children", *self.br_path)
        else:
            self.send("/live/browser/list_roots")

    def _on_live_browser_list_roots(self, v):
        roots = list(v)
        if not self.br_path:
            self.br_items, self.br_root, self.br_pending = roots, True, False
        elif self.br_path == [PLACES]:
            # Like Live's Places panel: Packs, User Library, Current Project, then your own folders. Live's API
            # doesn't list your own folders, so they come from Library.cfg (places.py) as "userplace:<name>".
            items = [r for r in ("packs", "user_library", "current_project") if r in roots]
            try:
                self.places_info = {p["name"]: p for p in places.places()}
            except Exception as e:
                print("places: can't read Live's Library.cfg: %r" % e, flush=True)
                self.places_info = {}
            items += ["userplace:" + n for n in self.places_info]
            self.br_items, self.br_root, self.br_pending = items, True, False

    def _on_live_browser_list_children(self, v):
        if self.br_path and self.br_path[0] != PLACES:
            self.br_items, self.br_root, self.br_pending = list(v), False, False

    def load_item(self, name, drum_note=-1):
        """Load the item called name from the folder now shown, onto the selected track (exact path).
        From your own Places folders only samples can load (by file path); drum_note picks the Drum Rack pad."""
        folder = self.user_place_dir()
        if folder is not None:
            if not places.is_sample(name):
                self.toast("Live only lets scripts load samples from Places, not presets or racks")
                return False
            self.send("/live/track/load_sample", self.sel, os.path.join(folder, name), int(drum_note))
            return True
        self.send("/live/browser/load_path", self.sel, *self.br_path, name)
        return True

    def _on_live_browser_load_device(self, v):
        name = v[1] if len(v) > 1 else "?"
        status = v[2] if len(v) > 2 else "?"
        self.toast("Loaded " + name if status == "ok" else "Couldn't load %s (%s)" % (name, status))
        if self.mode == "device":
            self.load_devices()

    # ------------------------------------------------------------ commands

    # ------------------------------------------------------------ playing: scale, drum pads

    def _on_live_song_get_scale(self, v):
        if len(v) >= 3:
            self.scale = (int(v[0]), str(v[1]), bool(v[2]), [int(x) for x in v[3:]] or [0])

    def set_scale(self, root=None, name=None):
        if root is not None:
            self.send("/live/song/set/root_note", int(root))
        if name is not None:
            self.send("/live/song/set/scale_name", str(name))
        self.send("/live/song/get/scale")

    def _on_live_track_get_drum_pads(self, v):
        if not v or v[0] != self.sel:
            return
        self.drum_track = v[0]
        if len(v) < 2 or not v[1]:
            self.drum_pads = None
            return
        rest = v[2:]
        self.drum_pads = {rest[k]: (rest[k + 1], rest[k + 2]) for k in range(0, len(rest) - 2, 3)}

    def select_track(self, t):
        if 0 <= t < max(0, self.num_tracks):
            if t != self.sel:
                self.drum_pads, self.drum_track = None, -1
            self.sel = t
            self.send("/live/view/set/selected_track", t)
            self.send("/live/track/get/drum_pads", t)
            if self.mode == "device":
                self.load_devices()

    def fire_clip(self, t, s):
        self.send("/live/clip_slot/fire", t, s)

    def fire_scene(self, s):
        self.send("/live/scene/fire", s)

    def stop_track(self, t):
        self.send("/live/track/stop_all_clips", t)

    def set_volume(self, t, v):
        tr = self.tracks[t]
        tr.vol = min(1.0, max(0.0, v))
        self.hold("vol%d" % t)
        self.send("/live/track/set/volume", t, float(tr.vol))

    def set_pan(self, t, v):
        tr = self.tracks[t]
        tr.pan = min(1.0, max(-1.0, v))
        self.hold("pan%d" % t)
        self.send("/live/track/set/panning", t, float(tr.pan))

    def toggle(self, what, t):
        tr = self.tracks[t]
        if what == "arm" and not tr.armable:
            return self.toast("This track can't be armed")
        val = not getattr(tr, what)
        setattr(tr, what, val)
        self.hold("%s%d" % (what, t))
        self.send("/live/track/set/" + what, t, int(val))

    def set_param(self, p, v):
        d = self.dev
        if p >= len(d.pvals):
            return
        lo = d.pmin[p] if p < len(d.pmin) else 0.0
        hi = d.pmax[p] if p < len(d.pmax) else 1.0
        quant = p < len(d.pq) and d.pq[p]
        v = min(hi, max(lo, round(v) if quant else v))
        d.pvals[p] = v
        self.hold("p%d" % p)
        self.send("/live/dev/set/param", d.track, d.path(), p, float(v))     # replies with the new text

    def nudge_param(self, p, steps):
        """Relative change from a Q-Link: quantized params move one step per click, others 1/100 of range."""
        d = self.dev
        if p >= len(d.pvals):
            return
        lo = d.pmin[p] if p < len(d.pmin) else 0.0
        hi = d.pmax[p] if p < len(d.pmax) else 1.0
        quant = p < len(d.pq) and d.pq[p]
        self.set_param(p, d.pvals[p] + (steps if quant else steps * (hi - lo) / 100))

    def select_device(self, i):
        d = self.dev
        if 0 <= i < len(d.names):
            d.idx, d.bank, d.view = i, 0, "devices"
            self.send("/live/dev/select", d.track, d.path())      # Live's own device view follows
            self._load_params()

    def toggle_device_on(self):
        d = self.dev
        if d.names and d.pvals:
            self.set_param(0, 0 if d.pvals[0] else 1)

    def song(self, method):
        self.send("/live/song/" + method)

    def set_song(self, prop, value):
        self.send("/live/song/set/" + prop, value)

    # ------------------------------------------------------------ editing the set: tracks, scenes, clips

    def refresh_layout(self):
        """Re-read tracks, scenes and clips right after the set's layout changed (instead of the 1.5 s poll)."""
        for addr in ("/live/song/get/num_tracks", "/live/song/get/num_scenes", "/live/song/get/scenes/name"):
            self.send(addr)

    def _edit(self, addr, *args, msg=None):
        self.send(addr, *args)
        self.refresh_layout()
        if msg:
            self.toast(msg)

    def new_track(self, kind, index=-1):
        """kind: "midi", "audio" or "return". index -1 = at the end."""
        if kind == "return":
            self._edit("/live/song/create_return_track", msg="New return track")
        else:
            self._edit("/live/song/create_%s_track" % kind, int(index), msg="New %s track" % kind.upper()
                       if kind == "midi" else "New audio track")

    def duplicate_track(self, t):
        self._edit("/live/song/duplicate_track", t, msg="Duplicated %s" % self.track_name(t))

    def delete_track(self, t):
        if self.num_tracks <= 1:
            self.toast("Live needs at least one track")
            return
        self._edit("/live/song/delete_track", t, msg="Deleted %s (UNDO brings it back)" % self.track_name(t))
        self.sel = max(0, min(self.sel, self.num_tracks - 2))

    def track_name(self, t):
        return self.tracks[t].name if 0 <= t < len(self.tracks) else "track %d" % (t + 1)

    def scene_name(self, s):
        return self.scenes[s] if 0 <= s < len(self.scenes) and self.scenes[s] else "scene %d" % (s + 1)

    def new_scene(self, index=-1):
        self._edit("/live/song/create_scene", int(index), msg="New scene")

    def duplicate_scene(self, s):
        self._edit("/live/song/duplicate_scene", s, msg="Duplicated %s" % self.scene_name(s))

    def delete_scene(self, s):
        if self.num_scenes <= 1:
            self.toast("Live needs at least one scene")
            return
        self._edit("/live/song/delete_scene", s, msg="Deleted %s (UNDO brings it back)" % self.scene_name(s))

    def capture_scene(self):
        self._edit("/live/song/capture_and_insert_scene", msg="Captured the playing clips as a new scene")

    def capture_midi(self):
        self.send("/live/song/capture_midi")
        self.refresh_layout()
        self.toast("Captured MIDI (UNDO if nothing was played)")

    def new_clip(self, t, s, beats):
        self._edit("/live/clip_slot/create_clip", t, s, float(beats),
                   msg="New %g-bar clip" % (beats / self.beats_per_bar()))

    def duplicate_clip(self, t, s):
        """Copy the clip into the next free slot below (Live adds a scene if there's none)."""
        self._edit("/live/track/duplicate_clip_slot", t, s)

    def _on_live_track_duplicate_clip_slot(self, v):
        if len(v) >= 2 and v[1] >= 0:
            self.toast("Duplicated into scene %d" % (v[1] + 1))
        else:
            self.toast("Couldn't duplicate (%s)" % (v[2] if len(v) > 2 else "?"))

    def copy_clip(self, t, s, t2, s2):
        self._edit("/live/clip_slot/duplicate_clip_to", t, s, t2, s2, msg="Copied to %s, scene %d"
                   % (self.track_name(t2), s2 + 1))

    def delete_clip(self, t, s):
        self._edit("/live/clip_slot/delete_clip", t, s, msg="Deleted the clip (UNDO brings it back)")

    def double_clip(self, t, s):
        self._edit("/live/clip/duplicate_loop", t, s, msg="Doubled the loop")
        if self.clip == (t, s):
            self.request_clip()

    def record_fixed(self, t, s, beats):
        """Record into an empty slot for exactly `beats`, then play it back (Push's Fixed Length)."""
        self.send("/live/clip_slot/fire_record", t, s, float(beats))

    def _on_live_clip_slot_fire_record(self, v):
        if len(v) > 2 and v[2] != "ok":
            self.toast("Couldn't record: %s" % v[2])

    def rename(self, what, name):
        """what: ("track", t), ("clip", t, s) or ("scene", s)."""
        self._edit("/live/%s/set/name" % what[0], *what[1:], str(name))

    def set_color(self, what, index):
        """Colour from Live's 70-colour palette (push_ui.LIVE_COLORS)."""
        self._edit("/live/%s/set/color_index" % what[0], *what[1:], int(index))

    def ask_file_path(self, then):
        """Ask Live where the set is saved; then(path) gets "" for a set that was never saved."""
        self._file_cb = then
        self.send("/live/song/get/file_path")

    def _on_live_song_get_file_path(self, v):
        cb, self._file_cb = getattr(self, "_file_cb", None), None
        if cb:
            cb(str(v[0]) if v else "")

    def set_song_time(self, beats):
        if not math.isfinite(beats):
            return
        self.song_time = max(0.0, beats)
        self.hold("time")
        self.send("/live/song/seek", float(self.song_time))

    def set_loop(self, start, length):
        self.loop_start, self.loop_len = max(0.0, start), max(1.0, length)
        self.hold("loop")
        self.send("/live/song/set/loop_start", float(self.loop_start))
        self.send("/live/song/set/loop_length", float(self.loop_len))

    def toggle_loop(self):
        self.loop = not self.loop
        self.hold("loop_on")
        self.send("/live/song/set/loop", int(self.loop))

    def prepare_arrangement(self):
        self.rec = False
        self.back_to_arranger = False
        self._rec_watch.clear()
        self.send('/live/song/set/session_record', 0)
        self.send('/live/song/set/back_to_arranger', 0)

    def toggle_rec_mode(self):
        if not self.rec_mode:
            self.prepare_arrangement()
        self.rec_mode = not self.rec_mode
        self.hold("rec_mode")
        self.send("/live/song/set/record_mode", int(self.rec_mode))

    def set_tempo(self, bpm):
        self.tempo = min(999.0, max(20.0, round(bpm, 2)))
        self.hold("tempo")
        self.send("/live/song/set/tempo", float(self.tempo))
