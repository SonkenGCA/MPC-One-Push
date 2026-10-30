"""
Browser access for AbletonOSC.

Stock AbletonOSC can set parameters on devices that already exist, but has no way
to *add* a device. Live's API does expose the browser, so this handler walks it and
loads devices/plugins onto a chosen track by name.

  /live/browser/load_device   (track_index, name)   load first match onto that track
  /live/browser/search        (query)               list matching loadable names
  /live/browser/list_roots    ()                    list top-level browser categories
"""
from typing import Tuple, Any
import Live
from .handler import AbletonOSCHandler


class BrowserHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "browser"

    def init_api(self):
        def song_seek(params):
            """Set playback position AND Live's start marker; no args reads both."""
            if params:
                import math
                beats = float(params[0])
                start = float(params[1]) if len(params) > 1 else beats
                if not all(math.isfinite(v) and 0 <= v <= 1576800 for v in (beats, start)):
                    raise ValueError('Invalid arrangement position')
                self.song.current_song_time = beats
                self.song.start_time = start
            self.osc_server.send("/live/song/seek", (float(self.song.current_song_time), float(self.song.start_time)))
        self.osc_server.add_handler("/live/song/seek", song_seek)
        def count_in(params):
            if params:
                value = int(params[0])
                if value not in (0, 1, 2, 3):
                    raise ValueError('Count-in must be off, 1, 2 or 4 bars')
                self.song.count_in_duration = value
            self.osc_server.send("/live/song/get/count_in_duration", (int(self.song.count_in_duration),))
        self.osc_server.add_handler("/live/song/get/count_in_duration", count_in)
        self.osc_server.add_handler("/live/song/set/count_in_duration", count_in)
        def browser():
            return Live.Application.get_application().browser

        def roots():
            b = browser()
            out = []
            for attr in ("plugins", "audio_effects", "instruments", "user_library",
                         "midi_effects", "drums", "max_for_live", "sounds", "packs"):
                item = getattr(b, attr, None)
                if item is not None:
                    out.append((attr, item))
            return out

        def all_roots():
            """roots() plus Current Project, Samples, Clips and the user's Places folders ("place:<name>").
            Only for browsing by path (list_roots / list_children / load_path): name search keeps using
            roots(), so big sample folders don't slow it down. Added 2026-10-29 for MPC One Push."""
            b = browser()
            out = list(roots())
            for attr in ("current_project", "samples", "clips"):
                item = getattr(b, attr, None)
                if item is not None:
                    out.append((attr, item))
            for item in list(getattr(b, "user_folders", []) or []):
                try:
                    out.append(("place:" + item.name, item))
                except Exception:
                    pass
            return out

        def node_at(path):
            """Browser item at [root key, child name, ...], or None."""
            node = dict(all_roots()).get(str(path[0])) if path else None
            for seg in [str(x) for x in path[1:]]:
                nxt = None
                for c in (node.children if node else []):
                    try:
                        if c.name.lower() == seg.lower():
                            nxt = c
                            break
                    except Exception:
                        pass
                node = nxt
                if node is None:
                    break
            return node

        def walk(item, depth=0, max_depth=5):
            """Yield every loadable descendant of a browser item."""
            if depth > max_depth:
                return
            try:
                children = item.children
            except Exception:
                children = []
            for child in children:
                try:
                    if child.is_loadable:
                        yield child
                except Exception:
                    pass
                yield from walk(child, depth + 1, max_depth)

        def find(name, exact=False):
            """Find a loadable browser item by name. Prefers exact, then startswith."""
            name_l = str(name).strip().lower()
            best_exact = None
            best_starts = None
            best_contains = None
            for _, root in roots():
                for item in walk(root):
                    try:
                        n = item.name.lower()
                    except Exception:
                        continue
                    if n == name_l:
                        return item
                    if best_starts is None and n.startswith(name_l):
                        best_starts = item
                    if best_contains is None and name_l in n:
                        best_contains = item
            if exact:
                return best_exact
            return best_starts or best_contains

        def find_plugin(name):
            """Plug-in folders load lazily, so walk them explicitly: plugins > (VST3|VST2) > vendor > plug-in."""
            name_l = str(name).strip().lower()
            rs = dict(roots())
            root = rs.get("plugins")
            if root is None:
                return None
            contains = None
            for fmt in list(getattr(root, "children", []) or []):
                for vendor in list(getattr(fmt, "children", []) or []):
                    kids = list(getattr(vendor, "children", []) or [])
                    if not kids:
                        try:
                            if vendor.is_loadable and vendor.name.lower() == name_l:
                                return vendor
                        except Exception:
                            pass
                        continue
                    for item in kids:
                        try:
                            n = item.name.lower()
                            if not item.is_loadable:
                                continue
                        except Exception:
                            continue
                        if n == name_l:
                            return item
                        if contains is None and name_l in n:
                            contains = item
            return contains

        def list_plugins(params: Tuple[Any]):
            out = []
            rs = dict(roots())
            root = rs.get("plugins")
            for fmt in list(getattr(root, "children", []) or []):
                for vendor in list(getattr(fmt, "children", []) or []):
                    for item in list(getattr(vendor, "children", []) or []):
                        try:
                            if item.is_loadable:
                                out.append(item.name)
                        except Exception:
                            pass
            q = str(params[0]).lower() if params else ""
            out = [n for n in out if q in n.lower()]
            self.osc_server.send("/live/browser/list_plugins", tuple(sorted(out)[:200]))

        self.osc_server.add_handler("/live/browser/list_plugins", list_plugins)

        def load_device(params: Tuple[Any]):
            track_index = int(params[0])
            name = str(params[1])
            song = self.song
            try:
                track = song.tracks[track_index]
            except IndexError:
                # allow addressing return tracks as tracks + n
                rets = list(song.return_tracks)
                idx = track_index - len(song.tracks)
                if 0 <= idx < len(rets):
                    track = rets[idx]
                else:
                    self.osc_server.send("/live/browser/load_device",
                                         (track_index, name, "no-such-track"))
                    return
            item = find_plugin(name) or find(name)
            if item is None:
                self.osc_server.send("/live/browser/load_device",
                                     (track_index, name, "not-found"))
                return
            # load_item drops the device onto the *selected* track
            song.view.selected_track = track
            try:
                browser().load_item(item)
                n_devices = len(track.devices)
                self.osc_server.send("/live/browser/load_device",
                                     (track_index, name, "ok", n_devices))
            except Exception as e:
                self.osc_server.send("/live/browser/load_device",
                                     (track_index, name, "error: %s" % str(e)))

        def search(params: Tuple[Any]):
            query = str(params[0]).lower() if params else ""
            limit = int(params[1]) if len(params) > 1 else 30
            found = []
            for _, root in roots():
                for item in walk(root):
                    try:
                        n = item.name
                    except Exception:
                        continue
                    if query in n.lower():
                        found.append(n)
                        if len(found) >= limit:
                            break
                if len(found) >= limit:
                    break
            self.osc_server.send("/live/browser/search", tuple(found))

        def list_roots(params: Tuple[Any]):
            self.osc_server.send("/live/browser/list_roots",
                                 tuple(name for name, _ in all_roots()))

        def load_path(params: Tuple[Any]):
            """/live/browser/load_path (track_index, root, folder, ..., item): load exactly that item.
            Replies like load_device. Added 2026-10-29 for MPC One Push."""
            track_index = int(params[0])
            path = [str(x) for x in params[1:]]
            name = path[-1] if path else ""
            song = self.song
            tracks = list(song.tracks)
            if not 0 <= track_index < len(tracks):
                self.osc_server.send("/live/browser/load_device", (track_index, name, "no-such-track"))
                return
            item = node_at(path)
            if item is None or not getattr(item, "is_loadable", False):
                self.osc_server.send("/live/browser/load_device", (track_index, name, "not-found"))
                return
            song.view.selected_track = tracks[track_index]
            try:
                browser().load_item(item)
                self.osc_server.send("/live/browser/load_device",
                                     (track_index, name, "ok", len(tracks[track_index].devices)))
            except Exception as e:
                self.osc_server.send("/live/browser/load_device", (track_index, name, "error: %s" % str(e)))

        self.osc_server.add_handler("/live/browser/load_path", load_path)

        def preview_path(params: Tuple[Any]):
            """/live/browser/preview_path (root, folder, ..., item): audition it like clicking it in Live's
            browser (plays on the Preview/Cue output). Added 2026-10-29 for MPC One Push."""
            item = node_at([str(x) for x in params])
            if item is not None:
                try:
                    browser().preview_item(item)
                except Exception:
                    pass

        def stop_preview(params: Tuple[Any]):
            try:
                browser().stop_preview()
            except Exception:
                pass

        self.osc_server.add_handler("/live/browser/preview_path", preview_path)
        self.osc_server.add_handler("/live/browser/stop_preview", stop_preview)

        self.osc_server.add_handler("/live/browser/load_device", load_device)
        self.osc_server.add_handler("/live/browser/search", search)
        self.osc_server.add_handler("/live/browser/list_roots", list_roots)

        def list_children(params: Tuple[Any]):
            """/live/browser/list_children "<root>" ["<sub folder>" ...] -> names of that node's children."""
            node = node_at(list(params) if params else ["plugins"])
            names = []
            if node is not None:
                for c in node.children:
                    try: names.append(("[F] " if not c.is_loadable else "") + c.name)
                    except Exception: pass
            # 800 names stays well under the UDP datagram limit; drum kit folders often pass 300.
            self.osc_server.send("/live/browser/list_children", tuple(names[:800]))

        self.osc_server.add_handler("/live/browser/list_children", list_children)


        # ---- return tracks -------------------------------------------------
        # Stock AbletonOSC only addresses song.tracks, so return tracks are
        # unreachable: no name, colour, volume or device access. Add it.
        def rets():
            return list(self.song.return_tracks)

        def ret_names(params: Tuple[Any]):
            self.osc_server.send("/live/return/names",
                                 tuple(r.name for r in rets()))

        def ret_count(params: Tuple[Any]):
            self.osc_server.send("/live/return/count", (len(rets()),))

        def ret_set_name(params: Tuple[Any]):
            i, name = int(params[0]), str(params[1])
            r = rets()
            if 0 <= i < len(r):
                r[i].name = name
                self.osc_server.send("/live/return/set/name", (i, name, "ok"))

        def ret_set_volume(params: Tuple[Any]):
            i, v = int(params[0]), float(params[1])
            r = rets()
            if 0 <= i < len(r):
                r[i].mixer_device.volume.value = v
                self.osc_server.send("/live/return/set/volume", (i, v, "ok"))

        def ret_set_color(params: Tuple[Any]):
            i, c = int(params[0]), int(params[1])
            r = rets()
            if 0 <= i < len(r):
                r[i].color_index = c

        def ret_num_devices(params: Tuple[Any]):
            i = int(params[0])
            r = rets()
            n = len(r[i].devices) if 0 <= i < len(r) else -1
            self.osc_server.send("/live/return/num_devices", (i, n))

        def ret_device_names(params: Tuple[Any]):
            i = int(params[0])
            r = rets()
            names = tuple(d.name for d in r[i].devices) if 0 <= i < len(r) else ()
            self.osc_server.send("/live/return/device_names", (i,) + names)

        def ret_load_device(params: Tuple[Any]):
            i, name = int(params[0]), str(params[1])
            r = rets()
            if not (0 <= i < len(r)):
                self.osc_server.send("/live/return/load_device", (i, name, "no-such-return"))
                return
            item = find_plugin(name) or find(name)
            if item is None:
                self.osc_server.send("/live/return/load_device", (i, name, "not-found"))
                return
            self.song.view.selected_track = r[i]
            try:
                browser().load_item(item)
                self.osc_server.send("/live/return/load_device",
                                     (i, name, "ok", len(r[i].devices)))
            except Exception as e:
                self.osc_server.send("/live/return/load_device",
                                     (i, name, "error: %s" % str(e)))

        # ---- deleting devices (stock AbletonOSC has no way to remove one) ----
        def del_device(params: Tuple[Any]):
            ti, di = int(params[0]), int(params[1])
            try:
                self.song.tracks[ti].delete_device(di)
                self.osc_server.send("/live/track/delete_device", (ti, di, "ok"))
            except Exception as e:
                self.osc_server.send("/live/track/delete_device", (ti, di, "error: %s" % e))

        def clear_track(params: Tuple[Any]):
            ti = int(params[0])
            try:
                t = self.song.tracks[ti]
                while len(t.devices):
                    t.delete_device(len(t.devices) - 1)
                self.osc_server.send("/live/track/clear_devices", (ti, "ok"))
            except Exception as e:
                self.osc_server.send("/live/track/clear_devices", (ti, "error: %s" % e))

        def clear_return(params: Tuple[Any]):
            i = int(params[0])
            try:
                t = list(self.song.return_tracks)[i]
                while len(t.devices):
                    t.delete_device(len(t.devices) - 1)
                self.osc_server.send("/live/return/clear_devices", (i, "ok"))
            except Exception as e:
                self.osc_server.send("/live/return/clear_devices", (i, "error: %s" % e))

        # ---- mixer readouts -------------------------------------------------
        # Live's fader is a curve, not linear in dB, so setting it needs the
        # plugin's own display string to verify against.
        def disp(param):
            """DeviceParameter has no .str property - it has str_for_value()."""
            for attempt in (lambda: param.str_for_value(param.value),
                            lambda: param.str):
                try:
                    return str(attempt())
                except Exception:
                    pass
            return "?"

        def vol_str(params: Tuple[Any]):
            ti = int(params[0])
            try:
                t = self.song.tracks[ti]
                v = t.mixer_device.volume
                self.osc_server.send("/live/track/get/volume_string",
                                     (ti, v.value, disp(v)))
            except Exception as e:
                self.osc_server.send("/live/track/get/volume_string", (ti, "error: %s" % e))

        def pan_str(params: Tuple[Any]):
            ti = int(params[0])
            try:
                t = self.song.tracks[ti]
                p = t.mixer_device.panning
                self.osc_server.send("/live/track/get/panning_string",
                                     (ti, p.value, disp(p)))
            except Exception as e:
                self.osc_server.send("/live/track/get/panning_string", (ti, "error: %s" % e))

        def ret_vol_str(params: Tuple[Any]):
            i = int(params[0])
            try:
                t = list(self.song.return_tracks)[i]
                v = t.mixer_device.volume
                self.osc_server.send("/live/return/get/volume_string",
                                     (i, v.value, disp(v)))
            except Exception as e:
                self.osc_server.send("/live/return/get/volume_string", (i, "error: %s" % e))

        def send_str(params: Tuple[Any]):
            ti, si = int(params[0]), int(params[1])
            try:
                p = self.song.tracks[ti].mixer_device.sends[si]
                self.osc_server.send("/live/track/get/send_string", (ti, si, p.value, disp(p)))
            except Exception as e:
                self.osc_server.send("/live/track/get/send_string", (ti, si, "error: %s" % e))

        self.osc_server.add_handler("/live/track/get/send_string", send_str)
        self.osc_server.add_handler("/live/track/get/volume_string", vol_str)
        self.osc_server.add_handler("/live/track/get/panning_string", pan_str)
        self.osc_server.add_handler("/live/return/get/volume_string", ret_vol_str)

        def save_set(params: Tuple[Any]):
            """Save the open set. The LOM doesn't advertise this, so try the
            known entry points and report which one worked."""
            attempts = []
            for label, fn in (
                ("song.save_set", lambda: self.song.save_set()),
                ("app.save_set", lambda: Live.Application.get_application().save_set()),
                ("doc.save_set", lambda: Live.Application.get_application().get_document().save_set()),
            ):
                try:
                    fn()
                    self.osc_server.send("/live/song/save", ("ok", label))
                    return
                except Exception as e:
                    attempts.append("%s:%s" % (label, type(e).__name__))
            self.osc_server.send("/live/song/save", ("failed",) + tuple(attempts))

        self.osc_server.add_handler("/live/song/save", save_set)
        self.osc_server.add_handler("/live/track/delete_device", del_device)
        self.osc_server.add_handler("/live/track/clear_devices", clear_track)
        self.osc_server.add_handler("/live/return/clear_devices", clear_return)

        # ---- device parameters on RETURN tracks -----------------------------
        # The stock device handler indexes song.tracks, so devices living on
        # return tracks are completely unreachable. Mirror the API for returns.
        def _rdev(i, di):
            return list(self.song.return_tracks)[i].devices[di]

        def rdev_pnames(params: Tuple[Any]):
            i, di = int(params[0]), int(params[1])
            try:
                ps = _rdev(i, di).parameters
                self.osc_server.send("/live/return/device/get/parameters/name",
                                     (i, di) + tuple(p.name for p in ps))
            except Exception as e:
                self.osc_server.send("/live/return/device/get/parameters/name",
                                     (i, di, "error: %s" % e))

        def rdev_vstr(params: Tuple[Any]):
            i, di, pi = int(params[0]), int(params[1]), int(params[2])
            try:
                p = _rdev(i, di).parameters[pi]
                self.osc_server.send("/live/return/device/get/parameter/value_string",
                                     (i, di, pi, disp(p)))
            except Exception as e:
                self.osc_server.send("/live/return/device/get/parameter/value_string",
                                     (i, di, pi, "error: %s" % e))

        def rdev_set(params: Tuple[Any]):
            i, di, pi, v = int(params[0]), int(params[1]), int(params[2]), float(params[3])
            try:
                _rdev(i, di).parameters[pi].value = v
                self.osc_server.send("/live/return/device/set/parameter/value",
                                     (i, di, pi, v, "ok"))
            except Exception as e:
                self.osc_server.send("/live/return/device/set/parameter/value",
                                     (i, di, pi, "error: %s" % e))

        self.osc_server.add_handler("/live/return/device/get/parameters/name", rdev_pnames)
        self.osc_server.add_handler("/live/return/device/get/parameter/value_string", rdev_vstr)
        self.osc_server.add_handler("/live/return/device/set/parameter/value", rdev_set)

        # ---- moving devices (Song.move_device, Live 11+) --------------------
        def move_dev(params: Tuple[Any]):
            ti, di, pos = int(params[0]), int(params[1]), int(params[2])
            try:
                t = self.song.tracks[ti]
                r = self.song.move_device(t.devices[di], t, pos)
                self.osc_server.send("/live/track/move_device", (ti, di, pos, "ok", r))
            except Exception as e:
                self.osc_server.send("/live/track/move_device", (ti, di, pos, "error: %s" % e))

        def ret_move_dev(params: Tuple[Any]):
            i, di, pos = int(params[0]), int(params[1]), int(params[2])
            try:
                t = list(self.song.return_tracks)[i]
                r = self.song.move_device(t.devices[di], t, pos)
                self.osc_server.send("/live/return/move_device", (i, di, pos, "ok", r))
            except Exception as e:
                self.osc_server.send("/live/return/move_device", (i, di, pos, "error: %s" % e))

        def ret_del_dev(params: Tuple[Any]):
            i, di = int(params[0]), int(params[1])
            try:
                list(self.song.return_tracks)[i].delete_device(di)
                self.osc_server.send("/live/return/delete_device", (i, di, "ok"))
            except Exception as e:
                self.osc_server.send("/live/return/delete_device", (i, di, "error: %s" % e))

        def rdev_values(params: Tuple[Any]):
            i, di = int(params[0]), int(params[1])
            try:
                ps = _rdev(i, di).parameters
                self.osc_server.send("/live/return/device/get/parameters/value",
                                     (i, di) + tuple(p.value for p in ps))
            except Exception as e:
                self.osc_server.send("/live/return/device/get/parameters/value",
                                     (i, di, "error: %s" % e))

        def del_all_clips(params: Tuple[Any]):
            """Delete every arrangement clip and session clip on a track (Track.delete_clip, Live 11+)."""
            ti = int(params[0])
            n, errs = 0, []
            try:
                t = self.song.tracks[ti]
            except Exception as e:
                self.osc_server.send("/live/track/delete_all_clips", (ti, "error: %s" % e)); return
            try:
                for c in list(t.arrangement_clips):
                    t.delete_clip(c); n += 1
            except Exception as e:
                errs.append("arr: %s" % e)
            try:
                for cs in t.clip_slots:
                    if cs.has_clip:
                        cs.delete_clip(); n += 1
            except Exception as e:
                errs.append("slots: %s" % e)
            self.osc_server.send("/live/track/delete_all_clips", (ti, "ok", n) + tuple(errs))

        def count_clips(params: Tuple[Any]):
            ti = int(params[0])
            try:
                t = self.song.tracks[ti]
                a = len(list(t.arrangement_clips)) if hasattr(t, "arrangement_clips") else -1
                s = sum(1 for cs in t.clip_slots if cs.has_clip)
                self.osc_server.send("/live/track/count_clips", (ti, a, s))
            except Exception as e:
                self.osc_server.send("/live/track/count_clips", (ti, "error: %s" % e))

        def rack_inner(params: Tuple[Any]):
            """(track, device, chain, inner_device) -> name + 'param=value' strings of a device inside a rack."""
            ti, di, ci, dj = int(params[0]), int(params[1]), int(params[2]), int(params[3])
            try:
                d = self.song.tracks[ti].devices[di].chains[ci].devices[dj]
                out = tuple("%s=%s" % (p.name, disp(p)) for p in d.parameters)
                self.osc_server.send("/live/track/rack_inner", (ti, di, ci, dj, d.name) + out)
            except Exception as e:
                self.osc_server.send("/live/track/rack_inner", (ti, di, ci, dj, "error: %s" % e))

        def rack_list(params: Tuple[Any]):
            ti, di = int(params[0]), int(params[1])
            try:
                r = self.song.tracks[ti].devices[di]
                names = tuple("%d:%s" % (ci, "|".join(x.name for x in c.devices)) for ci, c in enumerate(r.chains))
                self.osc_server.send("/live/track/rack_list", (ti, di) + names)
            except Exception as e:
                self.osc_server.send("/live/track/rack_list", (ti, di, "error: %s" % e))

        self.osc_server.add_handler("/live/track/rack_inner", rack_inner)
        self.osc_server.add_handler("/live/track/rack_list", rack_list)
        self.osc_server.add_handler("/live/track/delete_all_clips", del_all_clips)
        self.osc_server.add_handler("/live/track/count_clips", count_clips)
        self.osc_server.add_handler("/live/track/move_device", move_dev)
        self.osc_server.add_handler("/live/return/move_device", ret_move_dev)
        self.osc_server.add_handler("/live/return/delete_device", ret_del_dev)
        self.osc_server.add_handler("/live/return/device/get/parameters/value", rdev_values)

        self.osc_server.add_handler("/live/return/get/names", ret_names)
        self.osc_server.add_handler("/live/return/get/count", ret_count)
        self.osc_server.add_handler("/live/return/set/name", ret_set_name)
        self.osc_server.add_handler("/live/return/set/volume", ret_set_volume)
        self.osc_server.add_handler("/live/return/set/color", ret_set_color)
        self.osc_server.add_handler("/live/return/get/num_devices", ret_num_devices)
        self.osc_server.add_handler("/live/return/get/device_names", ret_device_names)
        self.osc_server.add_handler("/live/return/load_device", ret_load_device)

        # ---- automation + quantize (added 2026-10-29 for MPC One Push) ------
        # Clip envelopes: device index -1 = the track's mixer (param 0 volume, 1 pan).
        def _clip(ti, si):
            return self.song.tracks[ti].clip_slots[si].clip

        def _auto_param(ti, di, pi):
            t = self.song.tracks[ti]
            if di < 0:
                return t.mixer_device.volume if pi == 0 else t.mixer_device.panning
            return t.devices[di].parameters[pi]

        def env_get(params: Tuple[Any]):
            """(track, slot, device, param, points) -> (track, slot, device, param, exists, min, max, v0..vn-1)
            sampled evenly over the clip's loop."""
            ti, si, di, pi = int(params[0]), int(params[1]), int(params[2]), int(params[3])
            n = max(2, min(256, int(params[4]) if len(params) > 4 else 64))
            try:
                clip, prm = _clip(ti, si), _auto_param(ti, di, pi)
                env = clip.automation_envelope(prm)
                head = (ti, si, di, pi, 1 if env is not None else 0, prm.min, prm.max)
                if env is None:
                    self.osc_server.send("/live/clip/get/envelope", head)
                    return
                a, b = clip.loop_start, clip.loop_end
                vals = tuple(float(env.value_at_time(a + (b - a) * i / n)) for i in range(n))
                self.osc_server.send("/live/clip/get/envelope", head + vals)
            except Exception as e:
                self.osc_server.send("/live/clip/get/envelope", (ti, si, di, pi, -1, "error: %s" % e))

        def env_step(params: Tuple[Any]):
            """(track, slot, device, param, time, length, value [, time, length, value ...]): write steps."""
            ti, si, di, pi = int(params[0]), int(params[1]), int(params[2]), int(params[3])
            try:
                clip, prm = _clip(ti, si), _auto_param(ti, di, pi)
                env = clip.automation_envelope(prm) or clip.create_automation_envelope(prm)
                rest = params[4:]
                for k in range(0, len(rest) - 2, 3):
                    v = min(prm.max, max(prm.min, float(rest[k + 2])))
                    env.insert_step(float(rest[k]), float(rest[k + 1]), v)
            except Exception as e:
                self.osc_server.send("/live/clip/set/envelope_step", (ti, si, di, pi, "error: %s" % e))

        def env_clear(params: Tuple[Any]):
            ti, si, di, pi = int(params[0]), int(params[1]), int(params[2]), int(params[3])
            try:
                _clip(ti, si).clear_envelope(_auto_param(ti, di, pi))
                self.osc_server.send("/live/clip/clear/envelope", (ti, si, di, pi, "ok"))
            except Exception as e:
                self.osc_server.send("/live/clip/clear/envelope", (ti, si, di, pi, "error: %s" % e))

        def quantize(params: Tuple[Any]):
            """(track, slot, grid, amount): grid is Live's RecordingQuantization index (5 = 1/16)."""
            ti, si = int(params[0]), int(params[1])
            grid = int(params[2]) if len(params) > 2 else 5
            amount = float(params[3]) if len(params) > 3 else 1.0
            try:
                _clip(ti, si).quantize(grid, amount)
                self.osc_server.send("/live/clip/quantize", (ti, si, "ok"))
            except Exception as e:
                self.osc_server.send("/live/clip/quantize", (ti, si, "error: %s" % e))

        def get_auto_arm(params: Tuple[Any]):
            self.osc_server.send("/live/song/get/session_automation_record", (int(self.song.session_automation_record),))

        def set_auto_arm(params: Tuple[Any]):
            self.song.session_automation_record = bool(int(params[0]))

        self.osc_server.add_handler("/live/clip/get/envelope", env_get)
        self.osc_server.add_handler("/live/clip/set/envelope_step", env_step)
        self.osc_server.add_handler("/live/clip/clear/envelope", env_clear)
        self.osc_server.add_handler("/live/clip/quantize", quantize)
        self.osc_server.add_handler("/live/song/get/session_automation_record", get_auto_arm)
        self.osc_server.add_handler("/live/song/set/session_automation_record", set_auto_arm)

        # ---- diagnostics (added 2026-10-29 for MPC One Push): what does Live's browser expose? ----
        def browser_attrs(params: Tuple[Any]):
            b = browser()
            out = []
            for name in dir(b):
                if name.startswith("_"):
                    continue
                try:
                    v = getattr(b, name)
                except Exception as e:
                    out.append("%s=<error %s>" % (name, e))
                    continue
                if callable(v):
                    out.append(name + "()")
                    continue
                desc = type(v).__name__
                try:
                    items = list(v)
                    desc += "[%d]: %s" % (len(items), ", ".join(str(getattr(i, "name", i)) for i in items[:12]))
                except Exception:
                    desc += ": " + str(getattr(v, "name", ""))
                out.append("%s=%s" % (name, desc))
            self.osc_server.send("/live/browser/attrs", tuple(out))

        self.osc_server.add_handler("/live/browser/attrs", browser_attrs)

        def live_dir(params: Tuple[Any]):
            """Diagnostics: public names of a Live API class, e.g. ("ClipSlot.ClipSlot",)."""
            obj = Live
            try:
                for part in str(params[0]).split("."):
                    obj = getattr(obj, part)
                names = tuple(n for n in dir(obj) if not n.startswith("_"))
            except Exception as e:
                names = ("error: %s" % e,)
            self.osc_server.send("/live/api/dir", (str(params[0]),) + names)

        self.osc_server.add_handler("/live/api/dir", live_dir)

        def live_doc(params: Tuple[Any]):
            """Diagnostics: the docstring (signature) of a Live API name, e.g. ("ClipSlot.ClipSlot.fire",)."""
            obj = Live
            try:
                for part in str(params[0]).split("."):
                    obj = getattr(obj, part)
                doc = str(getattr(obj, "__doc__", "") or "")
            except Exception as e:
                doc = "error: %s" % e
            self.osc_server.send("/live/api/doc", (str(params[0]), doc[:1500]))

        self.osc_server.add_handler("/live/api/doc", live_doc)

        # ---- clip and track editing for the MPC (added 2026-10-29 for MPC One Push) ----

        def fire_record(params: Tuple[Any]):
            """/live/clip_slot/fire_record (track, slot, beats): launch the slot with a fixed recording length,
            like Push's Fixed Length. Recording stops by itself after `beats` (the track must be armed)."""
            t, s, beats = int(params[0]), int(params[1]), float(params[2])
            try:
                self.song.tracks[t].clip_slots[s].fire(record_length=beats)
                self.osc_server.send("/live/clip_slot/fire_record", (t, s, "ok"))
            except Exception as e:
                self.osc_server.send("/live/clip_slot/fire_record", (t, s, "error: %s" % e))

        def dup_clip_slot(params: Tuple[Any]):
            """/live/track/duplicate_clip_slot (track, slot): copy the clip into the slot below (Live adds a
            scene if it has to). Replies (track, new slot) or (track, -1, error)."""
            t, s = int(params[0]), int(params[1])
            try:
                new = self.song.tracks[t].duplicate_clip_slot(s)
                self.osc_server.send("/live/track/duplicate_clip_slot", (t, int(new)))
            except Exception as e:
                self.osc_server.send("/live/track/duplicate_clip_slot", (t, -1, "error: %s" % e))

        # ---- the MPC's mixer: tracks, return tracks and master as "strips" (added 2026-10-29 for MPC One Push)

        def _strip(kind, i):
            if kind == "track":
                return self.song.tracks[i]
            if kind == "return":
                return self.song.return_tracks[i]
            return self.song.master_track

        def _pstr(p):
            try:
                return str(p.str_for_value(p.value))
            except Exception:
                return ""

        def _flag(s, what):
            try:
                return int(getattr(s, what))
            except Exception:                    # the main track has no mute / solo (Live raises)
                return 0

        def mixer_strips(params: Tuple[Any]):
            """/live/mixer/get/strips (kind, first, last): one /live/mixer/strip reply per strip:
            (kind, i, name, color, volume, volume string, pan, mute, solo, meter, n sends, send values...,
            send strings...). kind: "track", "return" or "master". Also replies /live/mixer/count
            (tracks, returns) first."""
            kind, a, b = str(params[0]), int(params[1]), int(params[2])
            self.osc_server.send("/live/mixer/count", (len(self.song.tracks), len(self.song.return_tracks)))
            n = {"track": len(self.song.tracks), "return": len(self.song.return_tracks)}.get(kind, 1)
            for i in range(max(0, a), min(b, n)):
                try:
                    s = _strip(kind, i)
                    mx = s.mixer_device
                    sends = list(mx.sends) if kind != "master" else []
                    self.osc_server.send("/live/mixer/strip", (
                        kind, i, s.name, int(s.color), float(mx.volume.value), _pstr(mx.volume),
                        float(mx.panning.value), _flag(s, "mute"), _flag(s, "solo"),
                        float(getattr(s, "output_meter_level", 0.0)), len(sends))
                        + tuple(float(p.value) for p in sends) + tuple(_pstr(p) for p in sends))
                except Exception as e:
                    self.osc_server.send("/live/mixer/strip", (kind, i, "error: %s" % e))

        def mixer_set(params: Tuple[Any]):
            """/live/mixer/set (kind, i, "volume" | "panning" | "send<n>", value)"""
            kind, i, what, v = str(params[0]), int(params[1]), str(params[2]), float(params[3])
            mx = _strip(kind, i).mixer_device
            p = mx.volume if what == "volume" else mx.panning if what == "panning" else \
                list(mx.sends)[int(what[4:])]
            p.value = max(p.min, min(p.max, v))

        def main_controls(params):
            mx = self.song.master_track.mixer_device
            values = []
            for field in ("cue_volume", "crossfader"):
                p = getattr(mx, field)
                if params and str(params[0]) == field:
                    v = float(params[1])
                    if not __import__('math').isfinite(v):
                        raise ValueError('Invalid main control value')
                    p.value = max(p.min, min(p.max, v))
                values.extend((float(p.value), float(p.min), float(p.max), _pstr(p)))
            self.osc_server.send("/live/mixer/get/main_controls", tuple(values))
        self.osc_server.add_handler("/live/mixer/get/main_controls", main_controls)
        self.osc_server.add_handler("/live/mixer/set/main_controls", main_controls)

        def mixer_toggle(params: Tuple[Any]):
            """/live/mixer/toggle (kind, i, "mute" | "solo")"""
            s = _strip(str(params[0]), int(params[1]))
            what = str(params[2])
            setattr(s, what, not getattr(s, what))

        def mixer_routing(params: Tuple[Any]):
            """/live/mixer/get/routing (track): (track, input type, input channel, output type, output channel,
            monitoring 0 In / 1 Auto / 2 Off, or -1 when the track has no monitoring, e.g. a group)."""
            t = int(params[0])
            tr = self.song.tracks[t]
            def name(attr):
                try:
                    return str(getattr(tr, attr).display_name)
                except Exception:
                    return ""
            try:
                mon = int(tr.current_monitoring_state) if tr.can_be_armed else -1
            except Exception:
                mon = -1
            self.osc_server.send("/live/mixer/get/routing", (t, name("input_routing_type"),
                                 name("input_routing_channel"), name("output_routing_type"),
                                 name("output_routing_channel"), mon))

        def mixer_routing_options(params: Tuple[Any]):
            """/live/mixer/get/routing_options (track, field): field is input_routing_type, input_routing_channel,
            output_routing_type or output_routing_channel. Replies (track, field, option names...)."""
            t, field = int(params[0]), str(params[1])
            try:
                opts = tuple(str(o.display_name) for o in getattr(self.song.tracks[t], "available_" + field + "s"))
            except Exception:
                opts = ()
            self.osc_server.send("/live/mixer/get/routing_options", (t, field) + opts)

        def mixer_set_routing(params: Tuple[Any]):
            """/live/mixer/set/routing (track, field, option name)"""
            t, field, want = int(params[0]), str(params[1]), str(params[2])
            tr = self.song.tracks[t]
            for o in getattr(tr, "available_" + field + "s"):
                if str(o.display_name) == want:
                    setattr(tr, field, o)
                    return

        # ---- search: an index of Live's browser, built a slice at a time on Live's ticks so Live never stalls
        # (added 2026-10-29 for MPC One Push) ----

        import time as _time
        INDEX_ROOTS = ["sounds", "drums", "instruments", "audio_effects", "midi_effects", "plugins", "max_for_live",
                       "user_library", "clips", "samples", "packs"]
        idx = {"state": "idle", "entries": [], "stack": []}

        def _index_step():
            t0 = _time.time()
            stack, entries = idx["stack"], idx["entries"]
            while stack and _time.time() - t0 < 0.015:          # 15 ms of Live's 100 ms tick, at most
                node, path = stack.pop()
                try:
                    children = list(node.children)
                except Exception:
                    continue
                for c in reversed(children):
                    try:
                        name = c.name
                        p = path + [name]
                        if c.is_loadable:
                            entries.append((name.lower(), p))
                        if c.is_folder:
                            stack.append((c, p))
                    except Exception:
                        pass
            if stack:
                self.manager.schedule_message(1, _index_step)
            else:
                idx["state"] = "done"

        def index_start(params: Tuple[Any]):
            """/live/browser/index/start: build the search index (once; again with "rebuild")."""
            if idx["state"] == "running" or (idx["state"] == "done" and not (params and params[0] == "rebuild")):
                return index_status(())
            roots_now = dict(all_roots())
            idx.update(state="running", entries=[],
                       stack=[(roots_now[k], [k]) for k in reversed(INDEX_ROOTS) if k in roots_now])
            self.manager.schedule_message(1, _index_step)
            index_status(())

        def index_status(params: Tuple[Any]):
            self.osc_server.send("/live/browser/index/status", (idx["state"], len(idx["entries"])))

        def find(params: Tuple[Any]):
            """/live/browser/find (query[, limit]): browser items whose names contain every word of the query,
            names starting with it first. Replies (query, state, then each match's path joined with tabs)."""
            q = str(params[0]).lower().strip() if params else ""
            limit = int(params[1]) if len(params) > 1 else 80
            words = q.split()
            if not words:
                self.osc_server.send("/live/browser/find", (q, idx["state"]))
                return
            hits = [(not n.startswith(words[0]), len(n), p) for n, p in idx["entries"] if all(w in n for w in words)]
            hits.sort(key=lambda h: (h[0], h[1]))
            out, seen = [], set()
            for _a, _b, p in hits:              # the same item in a pack and a category: keep the first found
                k = p[-1].lower()               # (categories are indexed before packs)
                if k in seen:
                    continue
                seen.add(k)
                out.append("\t".join(p))
                if len(out) >= limit:
                    break
            self.osc_server.send("/live/browser/find", (q, idx["state"]) + tuple(out))

        self.osc_server.add_handler("/live/browser/index/start", index_start)
        self.osc_server.add_handler("/live/browser/index/status", index_status)
        self.osc_server.add_handler("/live/browser/find", find)

        # ---- device presets, swapped in place like Push's Swap (added 2026-10-29 for MPC One Push) ----

        def _preset_root(dev):
            """[browser root key, device folder] that holds a stock device's presets, or None."""
            name = str(getattr(dev, "class_display_name", "") or dev.name)
            kind = int(getattr(dev, "type", 0))      # 1 instrument, 2 audio effect, 4 MIDI effect
            roots = {1: ["instruments", "drums"], 2: ["audio_effects"], 4: ["midi_effects"]}.get(kind, [])
            if getattr(dev, "can_have_drum_pads", False):
                roots = ["drums", "instruments"]
            for r in roots:
                if r == "drums" and getattr(dev, "can_have_drum_pads", False):
                    return ["drums"]                  # Drum Racks swap to whole kits
                if node_at([r, name]) is not None:
                    return [r, name]
            return None

        def dev_presets_list(params: Tuple[Any]):
            """/live/dev/presets (track, device path, folder, ...): the device's presets in that folder, like
            list_children ("[F] " in front of folders). Replies (track, path, n folders deep, items...) or
            (track, path, -1) when the device has no presets in Live's browser."""
            ti, dp, sub = int(params[0]), str(params[1]), [str(x) for x in params[2:]]
            try:
                root = _preset_root(_walk(ti, dp))
                node = node_at(root + sub) if root else None
                if node is None:
                    _send("/live/dev/presets", [ti, dp, -1])
                    return
                items = []
                for c in node.children:
                    if c.is_folder:
                        items.append("[F] " + c.name)
                    elif getattr(c, "is_loadable", False):
                        items.append(c.name)
                _send("/live/dev/presets", [ti, dp, len(sub)] + items)
            except Exception as e:
                _send("/live/dev/presets", [ti, dp, -1, "error: %s" % e])

        def dev_preset_load(params: Tuple[Any]):
            """/live/dev/preset/load (track, device path, folder, ..., preset): replace the device with the
            preset, in the same place (hot-swap). Replies (track, path, preset, "ok" | error)."""
            ti, dp = int(params[0]), str(params[1])
            path = [str(x) for x in params[2:]]
            name = path[-1] if path else ""
            try:
                dev = _walk(ti, dp)
                root = _preset_root(dev)
                item = node_at(root + path) if root else None
                if item is None or not getattr(item, "is_loadable", False):
                    _send("/live/dev/preset/load", [ti, dp, name, "not found"])
                    return
                b = browser()
                self.song.view.selected_track = self.song.tracks[ti]
                self.song.view.select_device(dev)
                b.hotswap_target = dev
                b.load_item(item)
                try:
                    b.hotswap_target = None
                except Exception:
                    pass
                _send("/live/dev/preset/load", [ti, dp, name, "ok"])
            except Exception as e:
                _send("/live/dev/preset/load", [ti, dp, name, "error: %s" % e])

        self.osc_server.add_handler("/live/dev/presets", dev_presets_list)
        self.osc_server.add_handler("/live/dev/preset/load", dev_preset_load)

        # ---- clip details (added 2026-10-29 for MPC One Push) ----

        def _clip_at(t, s):
            return self.song.tracks[int(t)].clip_slots[int(s)].clip

        def clip_details(params: Tuple[Any]):
            """/live/clip/get/details (track, slot): (track, slot, audio?, length, looping, loop_start, loop_end,
            start_marker, end_marker, launch_mode, launch_quantization, legato, sig num, sig den, gain, gain text,
            pitch_coarse, pitch_fine, warping, warp_mode, file, sample_length, sample frames of start, end,
            loop start, loop end (-1 if unknown), then the available warp modes)."""
            t, s = int(params[0]), int(params[1])
            try:
                c = _clip_at(t, s)
                audio = bool(c.is_audio_clip)
                out = [t, s, int(audio), float(c.length), int(c.looping), float(c.loop_start), float(c.loop_end),
                       float(c.start_marker), float(c.end_marker), int(c.launch_mode), int(c.launch_quantization),
                       int(c.legato), int(c.signature_numerator), int(c.signature_denominator)]
                if audio:
                    conv = c.beat_to_sample_time if c.warping else c.seconds_to_sample_time
                    frames = []
                    for v in (c.start_marker, c.end_marker, c.loop_start, c.loop_end):
                        try:
                            frames.append(float(conv(v)))
                        except Exception:
                            frames.append(-1.0)
                    out += [float(c.gain), str(c.gain_display_string), int(c.pitch_coarse), float(c.pitch_fine),
                            int(c.warping), int(c.warp_mode), str(c.file_path), int(c.sample_length)] + frames
                    out += [int(m) for m in c.available_warp_modes]
                self.osc_server.send("/live/clip/get/details", tuple(out))
            except Exception as e:
                self.osc_server.send("/live/clip/get/details", (t, s, "error: %s" % e))

        def clip_set_region(params: Tuple[Any]):
            """/live/clip/set/region (track, slot, which, beats): start_marker, end_marker, loop_start or
            loop_length, kept in a valid order (Live refuses a start past the end). Replies with the details."""
            t, s, which, v = int(params[0]), int(params[1]), str(params[2]), float(params[3])
            try:
                c = _clip_at(t, s)
                if which == "loop_start":
                    length = c.loop_end - c.loop_start
                    if v + length > c.loop_end:
                        c.loop_end = v + length
                        c.loop_start = v
                    else:
                        c.loop_start = v
                        c.loop_end = v + length
                elif which == "loop_length":
                    c.loop_end = c.loop_start + max(1.0 / 16, v)
                elif which == "start_marker":
                    c.start_marker = min(v, c.end_marker - 1.0 / 16)
                elif which == "end_marker":
                    c.end_marker = max(v, c.start_marker + 1.0 / 16)
            except Exception:
                pass
            clip_details([t, s])

        def clip_note_tool(params: Tuple[Any]):
            """/live/clip/notes/tool (track, slot, op, amount): transpose (semitones), velocity (+/-),
            humanize (0..1: timing up to 1/32 beat and velocity up to 20 either way), legato. Undoable in Live."""
            import random
            t, s, op = int(params[0]), int(params[1]), str(params[2])
            amount = float(params[3]) if len(params) > 3 else 0.0
            c = _clip_at(t, s)
            notes = c.get_all_notes_extended()
            if op == "legato":
                order = sorted(notes, key=lambda n: n.start_time)
                for a, b in zip(order, order[1:]):
                    if b.start_time > a.start_time:
                        a.duration = b.start_time - a.start_time
            for n in notes:
                if op == "transpose":
                    n.pitch = max(0, min(127, int(n.pitch + amount)))
                elif op == "velocity":
                    n.velocity = max(1, min(127, n.velocity + amount))
                elif op == "humanize":
                    n.start_time = max(0.0, n.start_time + random.uniform(-1, 1) * amount * 0.125)
                    n.velocity = max(1, min(127, n.velocity + random.uniform(-1, 1) * amount * 20))
            c.apply_note_modifications(notes)
            self.osc_server.send("/live/clip/notes/tool", (t, s, op, len(notes)))

        def clip_crop(params: Tuple[Any]):
            t, s = int(params[0]), int(params[1])
            try:
                _clip_at(t, s).crop()
            except Exception:
                pass
            clip_details([t, s])

        self.osc_server.add_handler("/live/clip/get/details", clip_details)
        self.osc_server.add_handler("/live/clip/set/region", clip_set_region)
        self.osc_server.add_handler("/live/clip/notes/tool", clip_note_tool)
        self.osc_server.add_handler("/live/clip/crop", clip_crop)

        # ---- playing on the pads (added 2026-10-29 for MPC One Push) ----

        def song_scale(params: Tuple[Any]):
            """/live/song/get/scale: (root note 0-11, scale name, scale mode on, intervals...)."""
            s = self.song
            self.osc_server.send("/live/song/get/scale", (int(s.root_note), str(s.scale_name), int(bool(s.scale_mode)))
                                 + tuple(int(i) for i in s.scale_intervals))

        def drum_pads(params: Tuple[Any]):
            """/live/track/get/drum_pads (track): the track's first Drum Rack's filled pads as
            (track, 1, note, name, color, note, name, color, ...), or (track, 0) if it has no Drum Rack."""
            t = int(params[0])
            out = [t, 0]
            try:
                for dev in self.song.tracks[t].devices:
                    if getattr(dev, "can_have_drum_pads", False) and dev.can_have_drum_pads:
                        out[1] = 1
                        for p in dev.drum_pads:
                            chains = list(p.chains)
                            if chains:
                                out += [int(p.note), str(p.name), int(chains[0].color)]
                        break
            except Exception:
                pass
            self.osc_server.send("/live/track/get/drum_pads", tuple(out))

        self.osc_server.add_handler("/live/song/get/scale", song_scale)
        self.osc_server.add_handler("/live/track/get/drum_pads", drum_pads)

        def recording_clips(params: Tuple[Any]):
            """/live/song/get/recording_clips: (song time, then track, slot, start beat for every Session clip
            being recorded). start = where in the song the recording began (song time minus the clip's position)."""
            song = self.song
            now = float(song.current_song_time)
            out = [now]
            for t, tr in enumerate(song.tracks):
                try:
                    s = tr.playing_slot_index
                    if s < 0 or not tr.clip_slots[s].has_clip:
                        continue
                    clip = tr.clip_slots[s].clip
                    if not clip.is_recording:
                        continue
                    pos = float(clip.playing_position)
                    if clip.is_audio_clip and not clip.warping:      # unwarped audio counts in seconds
                        pos = pos * song.tempo / 60.0
                    out += [t, s, max(0.0, now - pos)]
                except Exception:
                    pass
            self.osc_server.send("/live/song/get/recording_clips", tuple(out))

        def clip_to_arrangement(params: Tuple[Any]):
            """/live/clip/to_arrangement (track, slot, beat): copy a Session clip into the track's arrangement
            (beat -1 = the bar the playhead is in). Replies (track, slot, "ok" or error, beat)."""
            t, s, at = int(params[0]), int(params[1]), float(params[2])
            try:
                if at < 0:                       # -1: at the playhead, on the bar it's in
                    bar = self.song.signature_numerator * 4.0 / self.song.signature_denominator
                    at = (float(self.song.current_song_time) // bar) * bar
                tr = self.song.tracks[t]
                tr.duplicate_clip_to_arrangement(tr.clip_slots[s].clip, at)
                self.osc_server.send("/live/clip/to_arrangement", (t, s, "ok", at))
            except Exception as e:
                self.osc_server.send("/live/clip/to_arrangement", (t, s, "error: %s" % e, at))

        self.osc_server.add_handler("/live/song/get/recording_clips", recording_clips)
        self.osc_server.add_handler("/live/clip/to_arrangement", clip_to_arrangement)

        # Also refresh the companion module when /live/api/reload reloads this handler.
        from importlib import reload
        from . import arrangement
        reload(arrangement)
        from . import automation
        reload(automation)

        def parameter_automation(params):
            import json
            token, op, payload = params[:3]
            try:
                result = automation.execute(self.song, json.loads(payload), str(op), params[3:])
            except Exception as e:
                result = dict(error=str(e))
            self.osc_server.send('/live/automation', (str(token),json.dumps(result)))
        self.osc_server.add_handler('/live/automation', parameter_automation)

        def arrangement_edit(params):
            from .arrangement import edit
            t, start, length, name, op, dest = params
            try:
                edit(self.song, int(t), float(start), float(length), str(name), str(op), float(dest))
                result = "ok"
            except Exception as e:
                result = "error: %s" % e
            self.osc_server.send("/live/arrangement/edit", (int(t), result))
        self.osc_server.add_handler("/live/arrangement/edit", arrangement_edit)

        def arrangement_get(params):
            import json
            from .arrangement import detail, clip_length
            t, left, right = int(params[0]), float(params[1]), float(params[2])
            import math
            if not 0 <= t < len(self.song.tracks) or not all(math.isfinite(v) for v in (left,right)) or right <= left:
                return
            clips = list(self.song.tracks[t].arrangement_clips)
            self.osc_server.send("/live/arrangement/list", (t, json.dumps(
                [(c.start_time, clip_length(c), c.name) for c in clips])))
            visible = [c for c in clips
                       if c.start_time < right and c.start_time+clip_length(c) > left]
            for c in visible[:32]:
                try:
                    data = detail(c, left, right)
                    self.osc_server.send('/live/arrangement/detail', (t, json.dumps(data, separators=(',', ':'))))
                except Exception as e:
                    self.osc_server.send('/live/arrangement/detail', (t, json.dumps(dict(
                        start=c.start_time, length=clip_length(c), name=c.name, error=str(e)))))
        self.osc_server.add_handler('/live/arrangement/get', arrangement_get)

        clipboard = [None]
        def arrangement_clipboard(params):
            from .arrangement import find_clip, empty_destination, clip_length
            op = str(params[0])
            t = int(params[1])
            try:
                if not 0 <= t < len(self.song.tracks):
                    raise ValueError('Track no longer exists')
                if op in ('copy', 'cut'):
                    tr, c = find_clip(self.song, t, float(params[2]), float(params[3]), str(params[4]))
                    clipboard[0] = (tr, c, op, (c.start_time, clip_length(c), c.name))
                    result = 'Clip copied' if op == 'copy' else 'Cut ready; source moves when pasted'
                elif op == 'paste':
                    if clipboard[0] is None:
                        raise ValueError('Copy a clip first')
                    if isinstance(clipboard[0], dict):
                        from .arrangement import paste_group
                        count = paste_group(self.song, clipboard[0], t, float(params[2]))
                        self.osc_server.send('/live/arrangement/edit', (t, 'Pasted %d clips' % count))
                        return
                    tr, c, mode, fingerprint = clipboard[0]
                    if tr not in self.song.tracks or c not in tr.arrangement_clips:
                        raise ValueError('Source clip no longer exists; copy again')
                    if (c.start_time, clip_length(c), c.name) != fingerprint:
                        raise ValueError('Source clip changed; copy again')
                    dst = self.song.tracks[t]
                    if bool(dst.has_midi_input) != bool(c.is_midi_clip):
                        raise ValueError('Choose the same type of track (MIDI or audio)')
                    if tr.is_frozen:
                        raise ValueError('Unfreeze the source track first')
                    at = float(params[2])
                    empty_destination(self.song, dst, at, clip_length(c))
                    self.song.begin_undo_step()
                    try:
                        dst.duplicate_clip_to_arrangement(c, at)
                        if mode == 'cut':
                            tr.delete_clip(c)
                            clipboard[0] = None
                    finally:
                        self.song.end_undo_step()
                    result = 'Pasted clip'
                else:
                    raise ValueError('Unknown clipboard action')
            except Exception as e:
                result = 'error: %s' % e
            self.osc_server.send('/live/arrangement/edit', (t, result))
        self.osc_server.add_handler('/live/arrangement/clipboard', arrangement_clipboard)

        def arrangement_group(params):
            import json
            from .arrangement import capture_group, paste_group
            t = 0
            try:
                op, payload = params
                group = capture_group(self.song, json.loads(str(payload)))
                t = group['base_track']
                if op == 'copy':
                    clipboard[0] = group
                    result = 'Copied %d clips; hold empty space to paste' % len(group['clips'])
                elif op == 'duplicate':
                    count = paste_group(self.song, group, t, group['end'])
                    result = 'Duplicated %d clips after selection' % count
                else:
                    raise ValueError('Unknown group action')
            except Exception as e:
                result = 'error: %s' % e
            self.osc_server.send('/live/arrangement/edit', (t, result))
        self.osc_server.add_handler('/live/arrangement/group', arrangement_group)

        def arrangement_property(params):
            from .arrangement import find_clip
            t, start, length, name, field, value = params
            try:
                tr, c = find_clip(self.song, int(t), float(start), float(length), str(name))
                if self.song.record_mode or tr.is_frozen:
                    raise ValueError('Stop recording / unfreeze the track before editing')
                if field not in ('name', 'color_index'):
                    raise ValueError('Unsupported property')
                value = str(value)[:80] if field == 'name' else int(value)
                if field == 'color_index' and not 0 <= value < 70:
                    raise ValueError('Invalid colour')
                self.song.begin_undo_step()
                try:
                    setattr(c, field, value)
                finally:
                    self.song.end_undo_step()
                result = 'ok'
            except Exception as e:
                result = 'error: %s' % e
            self.osc_server.send('/live/arrangement/edit', (int(t), result))
        self.osc_server.add_handler('/live/arrangement/set', arrangement_property)

        def dev_presets(params: Tuple[Any]):
            """/live/dev/get/presets (track, device path): a plug-in's own presets / programs.
            Replies (track, path, selected index, names...); just (track, path, -1) if it has none."""
            ti, dp = int(params[0]), str(params[1])
            try:
                dev = _walk(ti, dp)
                names = tuple(str(p) for p in getattr(dev, "presets", ()) or ())
                sel = int(getattr(dev, "selected_preset_index", -1)) if names else -1
            except Exception:
                names, sel = (), -1
            _send("/live/dev/get/presets", [ti, dp, sel] + list(names))

        def dev_set_preset(params: Tuple[Any]):
            """/live/dev/set/preset (track, device path, index)"""
            ti, dp, i = int(params[0]), str(params[1]), int(params[2])
            try:
                _walk(ti, dp).selected_preset_index = i
            except Exception:
                pass
            dev_presets([ti, dp])

        self.osc_server.add_handler("/live/dev/get/presets", dev_presets)
        self.osc_server.add_handler("/live/dev/set/preset", dev_set_preset)
        self.osc_server.add_handler("/live/mixer/get/strips", mixer_strips)
        self.osc_server.add_handler("/live/mixer/set", mixer_set)
        self.osc_server.add_handler("/live/mixer/toggle", mixer_toggle)
        self.osc_server.add_handler("/live/mixer/get/routing", mixer_routing)
        self.osc_server.add_handler("/live/mixer/get/routing_options", mixer_routing_options)
        self.osc_server.add_handler("/live/mixer/set/routing", mixer_set_routing)

        def set_file(params: Tuple[Any]):
            """/live/song/get/file_path: where the open set is saved ("" if it never was)."""
            self.osc_server.send("/live/song/get/file_path", (str(getattr(self.song, "file_path", "") or ""),))

        self.osc_server.add_handler("/live/song/get/file_path", set_file)
        self.osc_server.add_handler("/live/clip_slot/fire_record", fire_record)
        self.osc_server.add_handler("/live/track/duplicate_clip_slot", dup_clip_slot)

        # ---- load a sample file by path (added 2026-10-29 for MPC One Push) ----
        # Live 12.4's API hides user Places folders from scripts, so the MPC browses them itself and
        # loads samples by path: Drum Rack pad / Simpler on MIDI tracks, a clip on audio tracks.
        def _insert_device(container, name, index):
            for args in ((name, index), (name,)):
                try:
                    container.insert_device(*args)
                    return True
                except TypeError:
                    continue
            return False

        def _find_simpler(devices):
            for d in devices:
                if getattr(d, "class_name", "") == "OriginalSimpler":
                    return d
            return None

        def load_sample(params: Tuple[Any]):
            """(track, file_path, note): note = drum pad to fill when the track has a Drum Rack (-1 = none)."""
            ti, path = int(params[0]), str(params[1])
            note = int(params[2]) if len(params) > 2 else -1
            name = path.replace("\\", "/").split("/")[-1]
            reply = lambda status: self.osc_server.send("/live/browser/load_device", (ti, name, status))  # noqa: E731
            try:
                track = self.song.tracks[ti]
                if not track.has_midi_input:                       # audio track: clip in the first free slot
                    slots = list(track.clip_slots)
                    free = next((i for i, s in enumerate(slots) if not s.has_clip), None)
                    if free is None:
                        reply("no free clip slot")
                        return
                    slots[free].create_audio_clip(path)
                    reply("ok")
                    return
                rack = next((d for d in track.devices if getattr(d, "can_have_drum_pads", False)), None)
                if rack is not None and 0 <= note < 128:           # Drum Rack: put it on that pad
                    pad = rack.drum_pads[note]
                    chain = pad.chains[0] if len(pad.chains) else None
                    if chain is None:
                        n = len(rack.chains)
                        try:
                            rack.insert_chain(n)
                        except TypeError:
                            rack.insert_chain()
                        chain = rack.chains[len(rack.chains) - 1]
                        chain.in_note = note
                    simpler = _find_simpler(chain.devices)
                    if simpler is None:
                        _insert_device(chain, "Simpler", 0)
                        simpler = _find_simpler(chain.devices)
                    simpler.replace_sample(path)
                    chain.name = name.rsplit(".", 1)[0]
                    reply("ok")
                    return
                simpler = _find_simpler(track.devices)             # MIDI track: Simpler (reuse the first one)
                if simpler is None:
                    _insert_device(track, "Simpler", len(track.devices))
                    simpler = _find_simpler(track.devices)
                if simpler is None:
                    reply("error: couldn't add a Simpler")
                    return
                simpler.replace_sample(path)
                reply("ok")
            except Exception as e:
                reply("error: %s" % e)

        self.osc_server.add_handler("/live/track/load_sample", load_sample)

        def insert_device(params: Tuple[Any]):
            """(track, device name [, index]): add a Live device by name, e.g. "Drum Rack", "Simpler"."""
            ti, name = int(params[0]), str(params[1])
            try:
                t = self.song.tracks[ti]
                idx = int(params[2]) if len(params) > 2 else len(t.devices)
                ok = _insert_device(t, name, idx)
                self.osc_server.send("/live/track/insert_device", (ti, name, "ok" if ok else "error"))
            except Exception as e:
                self.osc_server.send("/live/track/insert_device", (ti, name, "error: %s" % e))

        self.osc_server.add_handler("/live/track/insert_device", insert_device)

        # ---- device chains by path (added 2026-10-29 for MPC One Push) ----
        # Stock AbletonOSC only reaches a track's top-level devices. Here any device is addressed by a path
        # string "d/c/d/c/d" (device index, chain index, device index, ...) from the track's device list;
        # a chain path ends on a chain index ("" = the track's own devices).
        def _walk(ti, path):
            obj = self.song.tracks[ti]
            for k, i in enumerate(int(x) for x in str(path).split("/") if x != ""):
                obj = obj.devices[i] if k % 2 == 0 else obj.chains[i]
            return obj

        def _split(path):
            """"2/0/1" -> ("2/0", 1): the chain holding a device, and its index there."""
            parts = [x for x in str(path).split("/") if x != ""]
            return "/".join(parts[:-1]), int(parts[-1])

        def _send(addr, args):
            self.osc_server.send(addr, tuple(args))

        def chain_devices(params: Tuple[Any]):
            """(track, chain path) -> (track, path, n, then per device: name, class, on, has_chains, is_drum, chains)."""
            ti, cp = int(params[0]), str(params[1]) if len(params) > 1 else ""
            try:
                out = []
                for d in _walk(ti, cp).devices:
                    try:
                        on = bool(d.parameters[0].value) if len(d.parameters) else True
                    except Exception:
                        on = True
                    racky = bool(getattr(d, "can_have_chains", False))
                    out += [d.name, getattr(d, "class_name", ""), int(on), int(racky),
                            int(bool(getattr(d, "can_have_drum_pads", False))), len(d.chains) if racky else 0]
                _send("/live/chain/get/devices", [ti, cp, len(out) // 6] + out)
            except Exception as e:
                _send("/live/chain/get/devices", [ti, cp, -1, "error: %s" % e])

        def dev_params(params: Tuple[Any]):
            """(track, device path) -> (track, path, n, then per parameter: name, value, min, max, quantized, text)."""
            ti, dp = int(params[0]), str(params[1])
            try:
                out = []
                for p in _walk(ti, dp).parameters:
                    out += [p.name, float(p.value), float(p.min), float(p.max), int(bool(p.is_quantized)), disp(p)]
                _send("/live/dev/get/params", [ti, dp, len(out) // 6] + out)
            except Exception as e:
                _send("/live/dev/get/params", [ti, dp, -1, "error: %s" % e])

        def dev_values(params: Tuple[Any]):
            ti, dp = int(params[0]), str(params[1])
            try:
                _send("/live/dev/get/values", [ti, dp] + [float(p.value) for p in _walk(ti, dp).parameters])
            except Exception as e:
                _send("/live/dev/get/values", [ti, dp, "error: %s" % e])

        def dev_strings(params: Tuple[Any]):
            """(track, device path, p, p, ...) -> (track, path, p, text, p, text, ...)."""
            ti, dp = int(params[0]), str(params[1])
            try:
                ps = _walk(ti, dp).parameters
                out = []
                for i in params[2:]:
                    out += [int(i), disp(ps[int(i)])]
                _send("/live/dev/get/strings", [ti, dp] + out)
            except Exception as e:
                _send("/live/dev/get/strings", [ti, dp, "error: %s" % e])

        def dev_set(params: Tuple[Any]):
            """(track, device path, param, value): set it and reply with its new text."""
            ti, dp, pi, v = int(params[0]), str(params[1]), int(params[2]), float(params[3])
            try:
                p = _walk(ti, dp).parameters[pi]
                p.value = min(p.max, max(p.min, v))
                _send("/live/dev/get/strings", [ti, dp, pi, disp(p)])
            except Exception as e:
                _send("/live/dev/set/param", [ti, dp, pi, "error: %s" % e])

        def dev_chains(params: Tuple[Any]):
            """(track, rack path) -> (track, path, is_drum, n, then per chain: name, in_note (-1 if not a drum pad))."""
            ti, dp = int(params[0]), str(params[1])
            try:
                rack = _walk(ti, dp)
                drum = bool(getattr(rack, "can_have_drum_pads", False))
                out = []
                for c in rack.chains:
                    out += [c.name, int(getattr(c, "in_note", -1)) if drum else -1]
                _send("/live/dev/get/chains", [ti, dp, int(drum), len(out) // 2] + out)
            except Exception as e:
                _send("/live/dev/get/chains", [ti, dp, 0, -1, "error: %s" % e])

        def dev_delete(params: Tuple[Any]):
            ti, dp = int(params[0]), str(params[1])
            try:
                cp, i = _split(dp)
                _walk(ti, cp).delete_device(i)
                _send("/live/dev/delete", [ti, dp, "ok"])
            except Exception as e:
                _send("/live/dev/delete", [ti, dp, "error: %s" % e])

        def dev_insert(params: Tuple[Any]):
            """(track, chain path, device name, index): add a Live device into that chain."""
            ti, cp, name, idx = int(params[0]), str(params[1]), str(params[2]), int(params[3])
            try:
                ok = _insert_device(_walk(ti, cp), name, idx)
                _send("/live/dev/insert", [ti, cp, name, "ok" if ok else "error"])
            except Exception as e:
                _send("/live/dev/insert", [ti, cp, name, "error: %s" % e])

        def dev_select(params: Tuple[Any]):
            """Show this device in Live's own device view too."""
            ti, dp = int(params[0]), str(params[1])
            try:
                self.song.view.selected_track = self.song.tracks[ti]
                self.song.view.select_device(_walk(ti, dp))
            except Exception:
                pass

        def rack_action(params):
            ti, dp, expected, action = int(params[0]), str(params[1]), str(params[2]), str(params[3])
            try:
                dev = _walk(ti, dp)
                if dev.name != expected or not dev.can_have_chains:
                    raise ValueError('Rack changed; select it again')
                self.song.begin_undo_step()
                try:
                    if action == "randomize":
                        dev.randomize_macros()
                    elif action == "store":
                        dev.store_variation()
                    elif action in ("next", "previous"):
                        if dev.variation_count < 1:
                            raise ValueError('Store a variation first')
                        step = 1 if action == "next" else -1
                        dev.selected_variation_index = (dev.selected_variation_index+step) % dev.variation_count
                        dev.recall_selected_variation()
                    else:
                        raise ValueError('Unsupported rack action')
                finally:
                    self.song.end_undo_step()
                result = "ok: %d variations" % dev.variation_count
            except Exception as e:
                result = "error: %s" % e
            _send("/live/dev/rack_action", [ti, dp, result])
        self.osc_server.add_handler("/live/dev/rack_action", rack_action)

        def dev_move(params):
            ti, dp, expected, step = int(params[0]), str(params[1]), str(params[2]), int(params[3])
            cp, index = _split(dp)
            try:
                chain = _walk(ti, cp)
                device = chain.devices[index]
                destination = index+step
                if step not in (-1, 1) or not 0 <= destination < len(chain.devices):
                    raise ValueError('Already at the edge of this chain')
                if device.name != expected:
                    raise ValueError('Device changed; select it again')
                position = self.song.move_device(device, chain, destination)
                result = "ok"
            except Exception as e:
                result, position = "error: %s" % e, index
            _send("/live/dev/move", [ti, cp, result, int(position)])
        self.osc_server.add_handler("/live/dev/move", dev_move)

        def dev_duplicate(params):
            ti, dp, expected = int(params[0]), str(params[1]), str(params[2])
            cp, index = _split(dp)
            try:
                chain = _walk(ti, cp)
                if chain.devices[index].name != expected:
                    raise ValueError('Device changed; select it again')
                if not hasattr(chain, 'duplicate_device'):
                    raise ValueError('Live cannot duplicate a device in this chain')
                chain.duplicate_device(index)
                result = "ok"
            except Exception as e:
                result = "error: %s" % e
            _send("/live/dev/duplicate", [ti, cp, result])
        self.osc_server.add_handler("/live/dev/duplicate", dev_duplicate)

        self.osc_server.add_handler("/live/chain/get/devices", chain_devices)
        self.osc_server.add_handler("/live/dev/get/params", dev_params)
        self.osc_server.add_handler("/live/dev/get/values", dev_values)
        self.osc_server.add_handler("/live/dev/get/strings", dev_strings)
        self.osc_server.add_handler("/live/dev/set/param", dev_set)
        self.osc_server.add_handler("/live/dev/get/chains", dev_chains)
        self.osc_server.add_handler("/live/dev/delete", dev_delete)
        self.osc_server.add_handler("/live/dev/insert", dev_insert)
        self.osc_server.add_handler("/live/dev/select", dev_select)

        # ---- plug-ins for the MPC's add-device menu (added 2026-10-29 for MPC One Push) ----
        def list_plugins_all(params: Tuple[Any]):
            """Every loadable plug-in name (all formats, de-duplicated, sorted), up to 1000."""
            names = set()
            root = dict(roots()).get("plugins")
            for fmt in list(getattr(root, "children", []) or []):
                for vendor in list(getattr(fmt, "children", []) or []):
                    kids = list(getattr(vendor, "children", []) or [])
                    for item in (kids or [vendor]):
                        try:
                            if item.is_loadable:
                                names.add(item.name)
                        except Exception:
                            pass
            self.osc_server.send("/live/browser/list_plugins_all", tuple(sorted(names, key=str.lower)[:1000]))

        def dev_load_plugin(params: Tuple[Any]):
            """(track, device path to load after ("" = end of the track), plug-in name).
            Live's browser always appends the plug-in to the track's chain (tested 2026-10-29), so it's
            loaded first and then moved after that device with Song.move_device (works into rack chains too)."""
            ti, dp, name = int(params[0]), str(params[1]), str(params[2])
            cp, after = _split(dp) if dp else ("", -1)
            try:
                track = self.song.tracks[ti]
                target = _walk(ti, cp)
                self.song.view.selected_track = track
                item = find_plugin(name)
                if item is None:
                    _send("/live/dev/insert", [ti, cp, name, "not found"])
                    return
                before = {id(c): [d.name for d in c.devices] for c in (track, target)}
                browser().load_item(item)
                new = None
                for c in (target, track):              # where did it land? first position whose name changed
                    names = [d.name for d in c.devices]
                    old = before[id(c)]
                    if len(names) == len(old) + 1:
                        i = next((k for k in range(len(old)) if names[k] != old[k]), len(old))
                        new = c.devices[i]
                        break
                want = after + 1 if dp else len(target.devices)
                if new is not None and dp:
                    self.song.move_device(new, target, want)
                _send("/live/dev/insert", [ti, cp, name, "ok"])
            except Exception as e:
                _send("/live/dev/insert", [ti, cp, name, "error: %s" % e])

        self.osc_server.add_handler("/live/browser/list_plugins_all", list_plugins_all)
        self.osc_server.add_handler("/live/dev/load_plugin", dev_load_plugin)

        # ---- Simpler's sample, for the MPC's waveform view (added 2026-10-29 for MPC One Push) ----
        def dev_sample(params: Tuple[Any]):
            """(track, device path) -> (track, path, file, length, start_marker, end_marker, playback_mode)
            in sample frames; file "" when the device has no sample."""
            ti, dp = int(params[0]), str(params[1])
            try:
                dev = _walk(ti, dp)
                s = getattr(dev, "sample", None)
                if s is None:
                    _send("/live/dev/get/sample", [ti, dp, ""])
                    return
                slices = [int(x) for x in getattr(s, "slices", ())]
                _send("/live/dev/get/sample", [ti, dp, s.file_path, int(s.length), int(s.start_marker),
                                               int(s.end_marker), int(getattr(dev, "playback_mode", 0)),
                                               int(bool(s.warping)), int(s.slicing_style), len(slices)] + slices)
            except Exception as e:
                _send("/live/dev/get/sample", [ti, dp, "", "error: %s" % e])

        self.osc_server.add_handler("/live/dev/get/sample", dev_sample)

        def dev_set_sample(params: Tuple[Any]):
            """/live/dev/set/sample (track, device path, what, value): Simpler's start_marker / end_marker (frames),
            warping (0/1), slicing_style (0 transient, 1 beat, 2 region, 3 manual) or playback_mode (0 classic,
            1 one-shot, 2 slicing). Replies with /live/dev/get/sample. Added 2026-10-29 for MPC One Push."""
            ti, dp, what, v = int(params[0]), str(params[1]), str(params[2]), params[3]
            try:
                dev = _walk(ti, dp)
                if what == "playback_mode":
                    dev.playback_mode = int(v)
                else:
                    smp = dev.sample
                    if what == "start_marker":
                        v = max(0, min(int(v), int(smp.end_marker) - 1))
                    elif what == "end_marker":
                        v = max(int(smp.start_marker) + 1, min(int(v), int(smp.length)))
                    elif what == "warping":
                        v = bool(v)
                    else:
                        v = int(v)
                    setattr(smp, what, v)
            except Exception:
                pass
            dev_sample([ti, dp])

        def dev_slice(params: Tuple[Any]):
            """/live/dev/sample/slice (track, device path, op, a, b): op "insert" (at frame a), "move" (a -> b),
            "clear" or "reset" (back to Live's automatic slices). Replies with /live/dev/get/sample."""
            ti, dp, op = int(params[0]), str(params[1]), str(params[2])
            try:
                smp = _walk(ti, dp).sample
                if op == "insert":
                    smp.insert_slice(int(params[3]))
                elif op == "move":
                    smp.move_slice(int(params[3]), int(params[4]))
                elif op == "clear":
                    smp.clear_slices()
                elif op == "reset":
                    smp.reset_slices()
            except Exception:
                pass
            dev_sample([ti, dp])

        self.osc_server.add_handler("/live/dev/set/sample", dev_set_sample)
        self.osc_server.add_handler("/live/dev/sample/slice", dev_slice)

        def dev_playhead(params: Tuple[Any]):
            """(track, device path) -> (track, path, position, playing): Simpler's playhead, position 0..1
            between its start and end markers. Added 2026-10-29 for MPC One Push."""
            ti, dp = int(params[0]), str(params[1])
            try:
                dev = _walk(ti, dp)
                _send("/live/dev/get/playhead", [ti, dp, float(dev.playing_position),
                                                 int(bool(dev.playing_position_enabled))])
            except Exception:
                _send("/live/dev/get/playhead", [ti, dp, 0.0, 0])

        self.osc_server.add_handler("/live/dev/get/playhead", dev_playhead)
