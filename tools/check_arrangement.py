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

class Groups(unittest.TestCase):
    def test_arrangement_extent_is_not_loop_length(self):
        clip=SimpleNamespace(start_time=16.,end_time=18.,length=32.,name='Trimmed')
        tr=SimpleNamespace(arrangement_clips=[clip])
        song=SimpleNamespace(tracks=[tr])
        self.assertEqual(module.clip_length(clip),2.)
        self.assertIs(module.find_clip(song,0,16.,2.,'Trimmed')[1],clip)
        with self.assertRaises(ValueError):module.find_clip(song,0,16.,32.,'Trimmed')
    def setUp(self):
        self.a = SimpleNamespace(start_time=0.,length=4.,name='A',is_midi_clip=True)
        self.b = SimpleNamespace(start_time=8.,length=2.,name='B',is_midi_clip=True)
        self.tracks = [SimpleNamespace(arrangement_clips=[c],is_frozen=False,has_midi_input=True,
                       duplicate_clip_to_arrangement=Mock(),delete_clip=Mock()) for c in (self.a,self.b)]
        self.song = SimpleNamespace(tracks=self.tracks,record_mode=False,begin_undo_step=Mock(),end_undo_step=Mock())
        self.group = module.capture_group(self.song,[[0,0.,4.,'A'],[1,8.,2.,'B']])

    def test_duplicate_after_uses_whole_group_span(self):
        self.assertEqual(self.group['end'],10.)
        module.paste_group(self.song,self.group,0,self.group['end'])
        self.tracks[0].duplicate_clip_to_arrangement.assert_called_once_with(self.a,10.)
        self.tracks[1].duplicate_clip_to_arrangement.assert_called_once_with(self.b,18.)
        self.song.begin_undo_step.assert_called_once()
        self.song.end_undo_step.assert_called_once()

    def test_paste_preserves_track_and_beat_offsets(self):
        extra = [SimpleNamespace(arrangement_clips=[],is_frozen=False,has_midi_input=True,
                    duplicate_clip_to_arrangement=Mock(),delete_clip=Mock()) for _ in range(2)]
        self.tracks.extend(extra)
        module.paste_group(self.song,self.group,2,32.)
        extra[0].duplicate_clip_to_arrangement.assert_called_once_with(self.a,32.)
        extra[1].duplicate_clip_to_arrangement.assert_called_once_with(self.b,40.)

    def test_late_destination_collision_causes_zero_changes(self):
        self.tracks[1].arrangement_clips.append(SimpleNamespace(start_time=18.,length=2.,name='Occupied'))
        with self.assertRaises(ValueError):module.paste_group(self.song,self.group,0,10.)
        for tr in self.tracks:tr.duplicate_clip_to_arrangement.assert_not_called()
        self.song.begin_undo_step.assert_not_called()

    def test_failed_second_copy_rolls_back_first_copy_only(self):
        new = SimpleNamespace(name='New copy')
        self.tracks[0].duplicate_clip_to_arrangement.return_value = new
        self.tracks[1].duplicate_clip_to_arrangement.side_effect = RuntimeError('Refused')
        with self.assertRaises(RuntimeError):module.paste_group(self.song,self.group,0,16.)
        self.tracks[0].delete_clip.assert_called_once_with(new)
        self.tracks[1].delete_clip.assert_not_called()
        self.song.end_undo_step.assert_called_once()

    def test_stale_source_and_missing_tracks_are_rejected(self):
        self.b.name='Changed'
        with self.assertRaises(ValueError):module.paste_group(self.song,self.group,0,16.)
        self.b.name='B'
        with self.assertRaises(ValueError):module.paste_group(self.song,self.group,1,16.)
        self.song.begin_undo_step.assert_not_called()

    def test_recording_frozen_mismatch_and_bad_time_are_rejected(self):
        for field,value in (('is_frozen',True),('has_midi_input',False)):
            old=getattr(self.tracks[1],field);setattr(self.tracks[1],field,value)
            with self.assertRaises(ValueError):module.paste_group(self.song,self.group,0,16.)
            setattr(self.tracks[1],field,old)
        for at in (float('nan'),-1.):
            with self.assertRaises(ValueError):module.paste_group(self.song,self.group,0,at)
        self.song.record_mode=True
        with self.assertRaises(ValueError):module.paste_group(self.song,self.group,0,16.)
        self.song.begin_undo_step.assert_not_called()

    def test_empty_and_duplicate_selections_are_rejected(self):
        for selection in ([],[[0,0.,4.,'A']]*2):
            with self.assertRaises(ValueError):module.capture_group(self.song,selection)

if __name__ == '__main__': unittest.main(verbosity=2)
