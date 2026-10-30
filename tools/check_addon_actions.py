"""Execute the actual nested add-on callbacks against fake Live objects."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest
root = Path(__file__).resolve().parent.parent
source = root/'addon/abletonosc/browser.py'
if not source.exists(): source = root/'addon/AbletonOSC/abletonosc/browser.py'
tree = ast.parse(source.read_text(encoding='utf-8'))

def callback(name, env):
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), env)
    return env[name]

class AddonActions(unittest.TestCase):
    def test_seek_sets_playhead_and_start_marker_without_playing(self):
        host = NS(song=NS(current_song_time=0., start_time=4., is_playing=False, record_mode=False),
                  osc_server=NS(send=Mock()))
        fn = callback('song_seek', {'self':host})
        fn((32.,))
        self.assertEqual((host.song.current_song_time,host.song.start_time),(32.,32.))
        self.assertFalse(host.song.is_playing)
        self.assertFalse(host.song.record_mode)
        fn((8.,16.))
        self.assertEqual((host.song.current_song_time,host.song.start_time),(8.,16.))
        for invalid in (-1.,float('nan'),float('inf')):
            with self.assertRaises(ValueError): fn((invalid,))
        self.assertEqual((host.song.current_song_time,host.song.start_time),(8.,16.))
    def test_count_in_validates_live_enum(self):
        host = NS(song=NS(count_in_duration=0), osc_server=NS(send=Mock()))
        fn = callback('count_in', {'self':host})
        for value in (1,2,3,0):
            fn((value,))
            self.assertEqual(host.song.count_in_duration,value)
        with self.assertRaises(ValueError): fn((4,))
        self.assertEqual(host.song.count_in_duration,0)

    def test_device_duplicate_checks_expected_device(self):
        chain=NS(devices=[NS(name='Utility')], duplicate_device=Mock())
        send=Mock()
        fn=callback('dev_duplicate', {'_walk':lambda *a:chain, '_split':lambda p:('',0), '_send':send})
        fn((0,'0','Wrong'))
        chain.duplicate_device.assert_not_called()
        fn((0,'0','Utility'))
        chain.duplicate_device.assert_called_once_with(0)

    def test_device_move_guards_chain_edges_and_stale_device(self):
        device = NS(name='EQ')
        chain = NS(devices=[device, NS(name='Reverb')])
        host = NS(song=NS(move_device=Mock(return_value=1)))
        send = Mock()
        fn = callback('dev_move', {'self':host, '_walk':lambda *a:chain,
                                   '_split':lambda p:('',0), '_send':send})
        fn((0,'0','EQ',-1))
        host.song.move_device.assert_not_called()
        fn((0,'0','Stale',1))
        host.song.move_device.assert_not_called()
        fn((0,'0','EQ',1))
        host.song.move_device.assert_called_once_with(device, chain, 1)
    def test_main_controls_clamp_and_return_actual_ranges(self):
        cue, cross = NS(value=.5, min=0., max=1.), NS(value=0., min=-1., max=1.)
        send = Mock()
        host = NS(song=NS(master_track=NS(mixer_device=NS(cue_volume=cue, crossfader=cross))),
                  osc_server=NS(send=send))
        fn = callback('main_controls', {'self':host, '_pstr':lambda p:str(p.value)})
        fn(('crossfader', 4.))
        self.assertEqual(cross.value, 1.)
        self.assertEqual(cue.value, .5)
        self.assertEqual(len(send.call_args.args[1]), 8)
        with self.assertRaises(ValueError): fn(('cue_volume',float('nan')))

    def test_rack_next_wraps_and_recalls(self):
        rack = NS(name='Rack', can_have_chains=True, variation_count=2, selected_variation_index=1,
                  recall_selected_variation=Mock())
        send = Mock()
        host = NS(song=NS(begin_undo_step=Mock(), end_undo_step=Mock()))
        fn = callback('rack_action', {'self':host, '_walk':lambda *a:rack, '_send':send})
        fn((0,'0','Rack','next'))
        self.assertEqual(rack.selected_variation_index, 0)
        rack.recall_selected_variation.assert_called_once()
        host.song.end_undo_step.assert_called_once()
        fn((0,'0','Wrong rack','next'))
        self.assertTrue(send.call_args.args[1][2].startswith('error:'))

if __name__ == '__main__': unittest.main(verbosity=2)
