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
