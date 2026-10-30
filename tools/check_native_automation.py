"""Native fallback selector, viewport and gesture contracts; no real Windows input."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'bridge'))
import native_automation as native
import mpc_push as engine
import push_ui
from push_studio_ui import Renderer

class NativeAutomation(unittest.TestCase):
    def test_pc_arrangement_view_selects_native_editor(self):
        a=engine.App.__new__(engine.App)
        a.ui,a.live=push_ui.UiState(),engine.demo_live()
        a.ui.page='device';a.ui.automation_arrangement=False
        a.live._on_live_automation_context((1,1));a.live.send=Mock()
        a.open_parameter_automation(dict(track=0,track_name=a.live.tracks[0].name,kind='volume'))
        self.assertTrue(a.ui.native_automation)
        self.assertTrue(a.live.native_draw_mode)

    def test_resized_native_window_releases_drag_without_moving(self):
        a=engine.App.__new__(engine.App)
        a.clicker=Mock();a.renderer=NS(native_frame=(None,None,None,None,(1000,800),1))
        a.touch=dict(action=('drag','native',1),x=0,y=0,start=None,moved=False,
                     native=(None,None,None,None,(2000,1000),1,(0,0)))
        a.touch_move(40,40)
        a.clicker.up.assert_called_once()
        a.clicker.move.assert_not_called()

    def test_open_cancellation_prevents_keyboard_or_control_input(self):
        helper=native.NativeAutomation.__new__(native.NativeAutomation)
        helper.hwnd=123;helper.auto=NS(SendKeys=Mock());helper.nodes=Mock()
        window=NS(focus=lambda hwnd:True,is_foreground=lambda hwnd:True)
        with patch.dict(sys.modules,{'winshot':window}):
            with self.assertRaises(ValueError):helper.open({},lambda:False)
        helper.nodes.assert_not_called()
        helper.auto.SendKeys.assert_not_called()
    def test_native_record_uses_arrangement_transport(self):
        a=engine.App.__new__(engine.App)
        a.ui,a.live=push_ui.UiState(),engine.demo_live()
        a.ui.page='clip';a.ui.native_automation=True
        a.live.rec_mode=False;a.live.send=Mock()
        a.button('REC',True)
        self.assertEqual(a.live.send.call_args.args,('/live/song/set/record_mode',1))
        self.assertFalse(any(c.args[0]=='/live/song/set/session_record' and c.args[1]==1 for c in a.live.send.call_args_list))

    def test_control_resolution_uses_exact_track_identity(self):
        a,b=object(),object()
        nodes=[(a,'Track Volume','ArrangementView.Track[0].Mixer.Volume',()),
               (b,'Track Volume','ArrangementView.Track[1].Mixer.Volume',())]
        target=dict(kind='volume',track=1,param_name='Track Volume',track_name='Bass')
        self.assertIs(native.choose_parameter(nodes,target),b)

    def test_device_resolution_rejects_ambiguous_parameters(self):
        target=dict(kind='device',param_name='Frequency',device_name='Filter')
        a,b=object(),object()
        nodes=[(a,'Frequency','',('Filter',)),(b,'Frequency','',('EQ',))]
        self.assertIs(native.choose_parameter(nodes,target),a)
        with self.assertRaises(ValueError):native.choose_parameter(nodes+[nodes[0]],target)
        with self.assertRaises(ValueError):native.choose_parameter([],target)

    def test_zoom_crop_clamps_after_window_resize(self):
        crop=native.crop_view((100,100,1900,1000),(1200,800),2.,(1.,1.))
        self.assertEqual(crop,(650,450,1200,800))
        with self.assertRaises(ValueError):native.crop_view((1300,0,1400,200),(1200,800),1.,(.5,.5))

    def test_pointer_mapping_uses_crop_offset_and_scale(self):
        self.assertEqual(native.point_to_window(110,120,(10,20),.5,(100,200,500,600),(200,200)),(300.,400.))
        with self.assertRaises(ValueError):native.point_to_window(0,0,(10,20),.5,(100,200,500,600),(200,200))

    def test_native_route_needs_no_session_or_arrangement_clip(self):
        a=engine.App.__new__(engine.App)
        a.ui,a.live=push_ui.UiState(),engine.demo_live()
        a.ui.page='device';a.ui.automation_arrangement=True;a.live.send=Mock()
        target=dict(track=0,track_name=a.live.tracks[0].name,kind='volume')
        a.open_parameter_automation(target)
        self.assertTrue(a.ui.native_automation)
        self.assertEqual(a.live.send.call_args.args[0],'/live/automation/native')
        self.assertIsNone(a.live.automation_target)

    def test_unready_or_error_native_views_have_no_pointer_target(self):
        live,ui=engine.demo_live(),push_ui.UiState()
        ui.page='clip';ui.native_automation=True
        r=Renderer(motion=False)
        for error in (None,'Parameter unavailable'):
            ui.native_error=error
            _,targets=r.render(live,ui)
            self.assertFalse(any(t[4][:2]==('drag','native') for t in targets))

    def test_native_frame_binds_touch_to_immutable_snapshot(self):
        a=engine.App.__new__(engine.App)
        a.ui,a.live=push_ui.UiState(),engine.demo_live()
        a.ui.page='clip';a.ui.native_automation=True
        a.renderer=Renderer(motion=False);a.clicker=Mock()
        a.clicker.down.return_value=None
        a.renderer.native_frame=(Image.new('RGB',(900,400)),(10,20,1810,820),.5,123,(2000,1000),a.live.automation_token)
        _,targets=a.renderer.render(a.live,a.ui)
        box=next(t for t in targets if t[4][:2]==('drag','native'))
        a.hit=Mock(return_value=box[4])
        a.touch_down(box[0]+20,box[1]+20)
        a.clicker.down.assert_called_once_with(123,50.,60.)
        a.touch_move(box[0]+30,box[1]+30)
        a.clicker.move.assert_called_once_with(70.,80.)
        a.touch_up(box[0]+30,box[1]+30)
        a.clicker.up.assert_called_once()

    def test_native_reply_from_old_target_is_ignored(self):
        live=engine.demo_live();live.automation_token=3
        live._on_live_automation_native(('2','{"param_name":"Wrong"}'))
        self.assertIsNone(live.native_automation_result)

if __name__=='__main__':unittest.main(verbosity=2)
