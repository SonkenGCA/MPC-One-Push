"""Check native envelope resolution and write boundaries without Live/hardware."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parent.parent
folder=ROOT/'addon/abletonosc'
if not (folder/'automation.py').exists():folder=ROOT/'addon/AbletonOSC/abletonosc'
pkg=types.ModuleType('auto_test');pkg.__path__=[str(folder)];sys.modules['auto_test']=pkg
spec=importlib.util.spec_from_file_location('auto_test.automation',folder/'automation.py')
auto=importlib.util.module_from_spec(spec);spec.loader.exec_module(auto)
NS=types.SimpleNamespace

class Automation(unittest.TestCase):
    def setUp(self):
        self.param=NS(name='Cutoff',min=20.,max=20000.,value=500.,is_enabled=True)
        self.env=NS(insert_step=Mock(),value_at_time=lambda at:500.)
        self.clip=NS(name='Beat',loop_start=0.,loop_end=4.,automation_envelope=Mock(return_value=self.env),clear_envelope=Mock())
        device=NS(name='Filter',parameters=[self.param])
        rack=NS(chains=[NS(devices=[device])])
        self.tr=NS(name='Track',clip_slots=[NS(clip=self.clip)],devices=[rack],is_frozen=False)
        self.song=NS(tracks=[self.tr],record_mode=False,begin_undo_step=Mock(),end_undo_step=Mock())
        self.target=dict(track=0,track_name='Track',slot=0,clip_name='Beat',kind='device',path='0/0/0',device_name='Filter',param=0,param_name='Cutoff')

    def test_nested_parameter_write_clamps_value_and_time_to_clip(self):
        result=auto.execute(self.song,self.target,'step',(3.5,4.,50000.))
        self.env.insert_step.assert_called_once_with(3.5,.5,20000.)
        self.assertEqual(result['label'],'Cutoff')
        self.assertEqual(len(result['vals']),128)
        self.song.end_undo_step.assert_called_once()

    def test_get_reflects_live_values_without_writes(self):
        self.env.value_at_time=lambda at:1000.
        result=auto.execute(self.song,self.target,'get')
        self.assertEqual(result['vals'],[1000.]*128)
        self.song.begin_undo_step.assert_not_called()

    def test_stale_parameter_and_arrangement_are_rejected(self):
        for update in ({'param_name':'Different'},{'arr':[0.,4.,'Beat']}):
            with self.assertRaises(ValueError):auto.execute(self.song,{**self.target,**update},'step',(0.,1.,500.))
        self.env.insert_step.assert_not_called()

    def test_invalid_time_recording_and_frozen_track_are_rejected(self):
        for at in (-1.,4.,float('nan')):
            with self.assertRaises(ValueError):auto.execute(self.song,self.target,'step',(at,1.,500.))
        self.song.record_mode=True
        with self.assertRaises(ValueError):auto.execute(self.song,self.target,'clear')
        self.env.insert_step.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
