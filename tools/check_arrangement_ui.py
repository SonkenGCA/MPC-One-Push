"""Arrangement thumbnails, touch gestures and clipboard safety; no hardware connections."""
import sys
import json
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'bridge'))
import mpc_push as engine
import push_ui as ui_module
from push_studio_ui import Renderer
from check_arrangement import module as arrangement
from check_addon_actions import callback
NS = types.SimpleNamespace


class ArrangementTouch(unittest.TestCase):
    def setUp(self):
        a = self.app = engine.App.__new__(engine.App)
        a.ui, a.live = ui_module.UiState(), engine.demo_live()
        a.ui.page = 'arrange'
        a.ui.arr_len = 64.
        a.lock, a.dirty = threading.RLock(), threading.Event()
        a.live.send = Mock()
        a.live.select_track = Mock()
        a.action = ('arr_clip', 0, 0., 4., 'Beat')
        a.hit = Mock(return_value=a.action)

    def test_hold_opens_menu_without_seek_or_edit(self):
        a = self.app
        a.touch_down(150,180)
        a.touch['t'] = time.time()-2
        a.check_long_press()
        self.assertTrue(a.ui.sheet)
        a.touch_move(350,180)
        a.touch_up(350,180)
        a.live.send.assert_not_called()
        self.assertEqual(a.ui.arr_selected,a.action[1:])

    def test_select_then_drag_sends_only_on_release(self):
        a = self.app
        a.touch_down(150,180); a.touch_up(150,180)
        self.assertEqual(a.ui.arr_selected,a.action[1:])
        a.live.send.reset_mock()
        a.touch_down(150,180)
        a.touch_move(150+8*a.ui.arr_ppb(),180)
        a.live.send.assert_not_called()
        self.assertEqual(a.ui.arr_preview[2],8.)
        a.touch_up(150+8*a.ui.arr_ppb(),180)
        a.live.send.assert_called_once_with('/live/arrangement/edit',0,0.,4.,'Beat','move',8.)

    def test_unselected_clip_drag_selects_without_mutation(self):
        a = self.app
        a.ui.arr_start = 8.
        a.touch_down(200,180); a.touch_move(150,180); a.touch_up(150,180)
        self.assertEqual(a.ui.arr_start,8.)
        a.live.send.assert_not_called()

    def test_marquee_selects_multiple_tracks_in_both_directions(self):
        a = self.app
        for t in range(2):
            a.live.tracks[t].arr = dict(name=['A','B'],start_time=[0.,8.],length=[4.,4.])
        x0, x1 = ui_module.TL_X+2, ui_module.TL_X+10*a.ui.arr_ppb()
        y0 = engine.MAIN_Y+engine.ARR_RULER_H+8
        y1 = y0+engine.ARR_LANE_H+25
        a.hit.return_value = ('drag','arr',0)
        for first,last in (((x0,y0),(x1,y1)),((x1,y1),(x0,y0))):
            a.ui.sheet = None
            a.touch_down(*first);a.touch_move(*last)
            self.assertEqual(len(a.ui.arr_selection),4)
            self.assertIsNotNone(a.ui.arr_marquee)
            a.touch_up(*last)
            self.assertEqual(a.ui.sheet['title'],'4 clips selected')
            self.assertIsNone(a.ui.arr_marquee)
        a.live.send.assert_not_called()

    def test_hold_selected_group_member_preserves_group(self):
        a = self.app
        a.ui.arr_selection = [a.action[1:],(1,8.,4.,'Other')]
        a.touch_down(150,180);a.touch['t']=time.time()-2;a.check_long_press()
        self.assertEqual(a.ui.sheet['title'],'2 clips selected')
        a.touch_up(150,180)
        self.assertEqual(len(a.ui.arr_selection),2)
        a.live.send.assert_not_called()

    def test_ruler_still_scrolls(self):
        a = self.app
        a.hit.return_value = ('drag','arr',-1)
        a.ui.arr_start = 8.
        a.touch_down(200,130);a.touch_move(150,130);a.touch_up(150,130)
        self.assertGreater(a.ui.arr_start,8.)
        self.assertIsNone(a.ui.arr_marquee)
        a.live.send.assert_not_called()

    def test_drag_from_below_last_track_selects_upward(self):
        a = self.app
        a.live.tracks = a.live.tracks[:2]
        for tr in a.live.tracks:
            tr.arr = dict(name=['A'],start_time=[0.],length=[4.])
        a.renderer = Renderer(motion=False)
        _, targets = a.renderer.render(a.live,a.ui)
        x = ui_module.TL_X+6*a.ui.arr_ppb()
        y = engine.MAIN_Y+engine.ARR_RULER_H+3*engine.ARR_LANE_H
        action = next(t[4] for t in reversed(targets) if t[0]<=x<t[2] and t[1]<=y<t[3])
        self.assertEqual(action,('drag','arr',-2))
        a.hit.return_value = action
        a.touch_down(x,y)
        a.touch_move(ui_module.TL_X+1,engine.MAIN_Y+engine.ARR_RULER_H+8)
        self.assertEqual(len(a.ui.arr_selection),2)
        a.touch_up(ui_module.TL_X+1,engine.MAIN_Y+engine.ARR_RULER_H+8)
        self.assertEqual(a.ui.sheet['title'],'2 clips selected')
        a.live.send.assert_not_called()

    def test_blank_space_hold_paste_location(self):
        a = self.app
        a.hit.return_value = ('drag','arr',0)
        a.touch_down(ui_module.TL_X+8*a.ui.arr_ppb(),200)
        a.touch['t'] = time.time()-2
        a.check_long_press()
        self.assertEqual(a.ui.sheet['items'][0][1],('arr_clipboard','paste',0,8.))
        a.touch_up(400,200)
        a.live.send.assert_not_called()

    def test_notes_and_waveforms_render_with_clipped_edges(self):
        a = self.app
        r = Renderer(motion=False)
        tr = a.live.tracks[0]
        tr.arr = {'name':['Beat'], 'start_time':[0.], 'length':[16.]}
        key = (0.,16.,'Beat')
        data = dict(start=0.,length=16.,name='Beat',midi=True,color=0x55CCAA,muted=False,
                    offset=0.,looping=True,loop_start=0.,loop_end=4.,notes=[(60,0.,1.,100,False),(67,2.,.5,100,False)])
        tr.arr_details[key] = data
        a.ui.arr_selected = (0,0.,16.,'Beat')
        for start in (0.,3.9,15.95):
            a.ui.arr_start = start
            image, targets = r.render(a.live,a.ui)
            self.assertEqual(image.size,(1280,800))
        data.update(midi=False,file='synthetic.wav',frames=44100,rate=44100,
                    sample_positions=[44100*i/256 for i in range(257)],range=[0.,16.])
        r.wave_cache[('synthetic.wav',4096)] = [(-.6,.8)]*4096
        a.ui.arr_start = 0.
        image, targets = r.render(a.live,a.ui)
        self.assertTrue(any(t[4]==('arr_clip',0,0.,16.,'Beat') for t in targets))


class ArrangementData(unittest.TestCase):
    def test_group_callbacks_share_single_clipboard_paste_action(self):
        clips = [NS(start_time=float(i*8),length=4.,name=str(i),is_midi_clip=True) for i in range(2)]
        tracks = [NS(arrangement_clips=[c],is_frozen=False,has_midi_input=True,
                     duplicate_clip_to_arrangement=Mock(),delete_clip=Mock()) for c in clips]
        host = NS(song=NS(tracks=tracks,record_mode=False,begin_undo_step=Mock(),end_undo_step=Mock()),
                  osc_server=NS(send=Mock()))
        package = types.ModuleType('testaddon');package.__path__=[]
        env = {'__package__':'testaddon','self':host,'clipboard':[None]}
        with patch.dict(sys.modules,{'testaddon':package,'testaddon.arrangement':arrangement}):
            group = callback('arrangement_group',env)
            paste = callback('arrangement_clipboard',env)
            group(('copy',json.dumps([[0,0.,4.,'0'],[1,8.,4.,'1']])))
            paste(('paste',0,32.))
            tracks[0].duplicate_clip_to_arrangement.assert_called_once_with(clips[0],32.)
            tracks[1].duplicate_clip_to_arrangement.assert_called_once_with(clips[1],40.)
            self.assertEqual(host.osc_server.send.call_args.args[1],(0,'Pasted 2 clips'))

    def test_audio_mapping_uses_live_warp_conversion_and_loop(self):
        clip = NS(start_time=8.,length=8.,name='Audio',color=1,muted=False,is_midi_clip=False,
                  start_marker=0.,looping=True,loop_start=0.,loop_end=4.,end_marker=4.,
                  warping=True,file_path='audio.wav',sample_length=44100,sample_rate=44100,
                  beat_to_sample_time=lambda b:44100*b/4,
                  seconds_to_sample_time=lambda s:44100*s)
        detail = arrangement.detail(clip,8.,16.)
        self.assertAlmostEqual(detail['sample_positions'][64],22050.)
        self.assertEqual(detail['sample_positions'][128],0.)
        clip.warping, clip.looping, clip.loop_end = False,False,2.
        detail = arrangement.detail(clip,8.,16.)
        self.assertEqual(detail['sample_positions'][-1],88200.)

    def test_cut_only_deletes_after_successful_paste(self):
        clip = NS(start_time=0.,length=4.,name='Beat',is_midi_clip=True)
        source = NS(arrangement_clips=[clip],is_frozen=False,delete_clip=Mock(),has_midi_input=True)
        dest = NS(arrangement_clips=[],is_frozen=False,has_midi_input=True,
                  duplicate_clip_to_arrangement=Mock(side_effect=RuntimeError('refused')))
        song = NS(tracks=[source,dest],record_mode=False,begin_undo_step=Mock(),end_undo_step=Mock())
        host = NS(song=song,osc_server=NS(send=Mock()))
        package = types.ModuleType('testaddon');package.__path__=[]
        with patch.dict(sys.modules,{'testaddon':package,'testaddon.arrangement':arrangement}):
            fn = callback('arrangement_clipboard', {'__package__':'testaddon','self':host,'clipboard':[None]})
            fn(('cut',0,0.,4.,'Beat'))
            fn(('paste',1,8.))
            source.delete_clip.assert_not_called()
            self.assertIn('error:',host.osc_server.send.call_args.args[1][1])
            dest.duplicate_clip_to_arrangement.side_effect = None
            fn(('paste',1,8.))
            source.delete_clip.assert_called_once_with(clip)
            self.assertEqual(song.begin_undo_step.call_count,2)
            self.assertEqual(song.end_undo_step.call_count,2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
