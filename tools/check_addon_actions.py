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
