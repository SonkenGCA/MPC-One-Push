"""Guard arrangement mutations against stale selections and occupied destinations."""
from pathlib import Path
import importlib.util
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
root = Path(__file__).resolve().parent.parent
source = root/'addon/abletonosc/arrangement.py'
if not source.exists():
    source = root/'addon/AbletonOSC/abletonosc/arrangement.py'
spec = importlib.util.spec_from_file_location('arrangement', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Arrangement(unittest.TestCase):
    def setUp(self):
        self.clip = SimpleNamespace(start_time=0., length=4., name='Beat')
        self.track = SimpleNamespace(is_frozen=False, arrangement_clips=[self.clip],
                                     duplicate_clip_to_arrangement=Mock(), delete_clip=Mock())
        self.song = SimpleNamespace(tracks=[self.track], record_mode=False,
                                    begin_undo_step=Mock(), end_undo_step=Mock())
    def edit(self, op='duplicate', dest=4., name='Beat'):
        module.edit(self.song, 0, 0., 4., name, op, dest)
    def test_duplicate_and_move_use_undo_group(self):
        self.edit('move')
        self.track.duplicate_clip_to_arrangement.assert_called_once_with(self.clip, 4.)
        self.track.delete_clip.assert_called_once_with(self.clip)
        self.song.end_undo_step.assert_called_once()
    def test_overlap_and_stale_selection_make_no_changes(self):
        for kw in ({'dest':2.}, {'name':'Changed'}, {'dest':float('nan')}, {'dest':-4.}):
            with self.assertRaises(ValueError): self.edit(**kw)
        self.track.duplicate_clip_to_arrangement.assert_not_called()
        self.track.delete_clip.assert_not_called()
    def test_failed_duplicate_does_not_delete_source(self):
        self.track.duplicate_clip_to_arrangement.side_effect = RuntimeError('Live refused')
        with self.assertRaises(RuntimeError): self.edit('move')
        self.track.delete_clip.assert_not_called()
        self.song.end_undo_step.assert_called_once()
    def test_recording_and_frozen_track_are_rejected(self):
        self.song.record_mode = True
        with self.assertRaises(ValueError): self.edit('delete')
        self.song.record_mode, self.track.is_frozen = False, True
        with self.assertRaises(ValueError): self.edit('delete')
        self.track.delete_clip.assert_not_called()

if __name__ == '__main__': unittest.main(verbosity=2)
