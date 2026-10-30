"""MIDI behavior checks using an offline Live state and a fake MIDI output."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bridge'))
import mpc_push as engine
import push_ui


class Playing(unittest.TestCase):
    def test_arrangement_pads_play_notes_even_when_session_mode_is_launch(self):
        a=self.app
        a.ui.page,a.ui.pads='arrange','launch'
        a.live.send=Mock()
        a.pad(0,90)
        self.assertEqual(a.ui.pad_mode(),'play')
        self.assertEqual(a.midi.send.call_args.args,(0x90,60,90))
        a.live.send.assert_not_called()
        a.ui.page='session'
        self.assertEqual(a.ui.pad_mode(),'launch')

    def test_arrangement_record_restores_timeline_before_record_enable(self):
        a=self.app
        a.ui.page='arrange'
        a.live.send=Mock()
        a.live.rec_mode=False
        a.button('REC',True)
        self.assertEqual([c.args for c in a.live.send.call_args_list],[
            ('/live/song/set/session_record',0),('/live/song/set/back_to_arranger',0),
            ('/live/song/set/record_mode',1)])
        a.live.send.reset_mock()
        a.button('REC',True)
        a.live.send.assert_called_once_with('/live/song/set/record_mode',0)

    def test_arrangement_play_restores_timeline_without_session_launch(self):
        a=self.app
        a.ui.page='arrange';a.live.playing=False;a.live.send=Mock()
        a.button('PLAY',True)
        self.assertEqual([c.args for c in a.live.send.call_args_list],[
            ('/live/song/set/session_record',0),('/live/song/set/back_to_arranger',0),
            ('/live/song/start_playing',)])

    def test_session_recording_watcher_does_not_copy_in_arrangement(self):
        a=self.app
        a.live.mode='arrange';a.live.send=Mock()
        a.live._rec_watch={(0,0):[0.,1]}
        a.live._on_live_song_get_recording_clips([])
        self.assertEqual(a.live._rec_watch,{})
        a.live.send.assert_not_called()
    def test_arrangement_wheel_updates_live_start_marker_path(self):
        a = engine.App.__new__(engine.App)
        a.ui, a.live = push_ui.UiState(), engine.demo_live()
        a.ui.page, a.live.song_time = 'arrange', 0.
        a.live.send = Mock()
        a.wheel(2)
        a.live.send.assert_called_with('/live/song/seek',8.)
        a.wheel(-9)
        a.live.send.assert_called_with('/live/song/seek',0.)

    def setUp(self):
        self.app = engine.App.__new__(engine.App)
        a = self.app
        a.ui, a.live = push_ui.UiState(), engine.demo_live()
        a.ui.midi_port = 'fake'
        a.live.drum_pads = None
        a.midi, a.lock = Mock(), threading.RLock()
        a.sounding, a.voices, a.repeat_vel = {}, {}, {}
        a.last_note, a.no_port_toast = 60, 0

    def test_chord_note_off_uses_original_voices_after_mode_change(self):
        a = self.app
        a.ui.chord_mode = 'major'
        a.pad(0, 90)
        self.assertEqual(a.pad_voices(0), (60, 64, 67))
        a.ui.chord_mode, a.ui.pad_base = 'minor', 72
        a.midi.reset_mock()
        a.pad_off(0)
        self.assertEqual([c.args for c in a.midi.send.call_args_list], [(0x80, 60, 0), (0x80, 64, 0), (0x80, 67, 0)])

    def test_shared_voice_not_released_until_last_pad(self):
        a = self.app
        a.ui.chord_mode = 'major'
        a.pad(0); a.pad(7)
        a.midi.reset_mock()
        a.pad_off(0)
        self.assertNotIn((0x80, 67, 0), [c.args for c in a.midi.send.call_args_list])
        a.pad_off(7)
        self.assertIn((0x80, 67, 0), [c.args for c in a.midi.send.call_args_list])

    def test_drum_and_levels_bypass_chords(self):
        a = self.app
        a.ui.chord_mode = 'major'
        a.live.drum_pads = {36: ('Kick',)}
        a.pad(0)
        self.assertEqual(a.pad_voices(0), (36,))
        a.all_notes_off()
        a.live.drum_pads, a.ui.levels = None, True
        a.pad(0)
        self.assertEqual(a.pad_voices(0), (60,))

    def test_velocity_curves_are_monotonic_and_full_level_wins(self):
        a = self.app
        for curve in ('linear', 'soft', 'hard'):
            a.ui.velocity_curve = curve
            values = [a.pad_velocity(0, v) for v in range(1, 128)]
            self.assertEqual(values, sorted(values))
            self.assertTrue(all(1 <= v <= 127 for v in values))
        a.ui.full_level = True
        self.assertEqual(a.pad_velocity(0, 1), 127)

    def test_modulation_resets_on_last_release(self):
        a = self.app
        a.ui.pressure_mode = 'mod wheel'
        a.pad(0)
        a.pad_off(0)
        self.assertEqual(a.midi.send.call_args.args, (0xB0, 1, 0))


if __name__ == '__main__': unittest.main(verbosity=2)
