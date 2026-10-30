"""Regression checks without USB, MIDI, OSC, or changes to the user's set/settings."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bridge'))
import mpc_push as engine
import push_ui
import autostart


class Reliability(unittest.TestCase):
    def test_startup_failure_releases_resources_and_notes(self):
        from unittest.mock import Mock
        app = engine.App.__new__(engine.App)
        app.lock, app.sounding, app.quit = threading.RLock(), {0: 60}, False
        app.midi, app.live, app.previewer, app.clicker = Mock(), Mock(), Mock(), Mock()
        app._run_session = Mock(side_effect=OSError('device unavailable'))
        with self.assertRaisesRegex(OSError, 'device unavailable'):
            app.run()
        self.assertTrue(app.quit)
        self.assertFalse(app.sounding)
        app.midi.send.assert_called_once_with(0x80, 60, 0)
        app.midi.close.assert_called_once()
        app.live.stop.assert_called_once()
        app.previewer.stop.assert_called_once()
        app.clicker.up.assert_called_once()

    def test_shutdown_failure_still_closes_midi_and_osc(self):
        from unittest.mock import Mock
        app = engine.App.__new__(engine.App)
        app.lock, app.sounding = threading.RLock(), {}
        app.midi, app.live, app.previewer, app.clicker = Mock(), Mock(), Mock(), Mock()
        app._run_session = Mock()
        app.previewer.stop.side_effect = OSError('preview failed')
        with self.assertRaises(OSError): app.run()
        app.midi.close.assert_called_once()
        app.live.stop.assert_called_once()

    def test_live_stop_releases_osc_port(self):
        from unittest.mock import Mock
        live = engine.Live(offline=True)
        live.offline, live.rx, live.tx = False, Mock(), Mock()
        live.stop()
        self.assertTrue(live._stop.is_set())
        live.rx.close.assert_called_once()
        live.tx.close.assert_called_once()

    def test_arrangement_edges_and_short_clips(self):
        live, ui = engine.demo_live(), push_ui.UiState()
        ui.page, ui.arr_start = 'arrange', 64
        # Ends exactly at the viewport, crosses either edge, tiny and zero lengths.
        live.tracks[0].arr = {'start_time': [32., 63., 64., 80., 127.99],
                              'length': [32., 2., 0., .001, 8.], 'name': ['edge'] * 5}
        for zoom in (16., 64., 1024.):
            ui.arr_len = zoom
            image, targets = push_ui.Renderer().render(live, ui)
            self.assertEqual(image.size, (1280, 800))
            self.assertTrue(targets)

    def test_settings_recovery_and_nested_changes(self):
        app = engine.App.__new__(engine.App)
        app.ui = push_ui.UiState()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            with patch.object(engine, 'SETTINGS_FILE', str(path)):
                for bad in ('[]', 'null', '{broken', '{"page":"oops","arr_len":NaN,"pad_base":999}'):
                    path.write_text(bad)
                    app.load_settings()
                    self.assertEqual(app.ui.page, 'session')
                    self.assertEqual(app.ui.pad_base, 60)
                    self.assertEqual(app.ui.arr_len, 64.)
                path.write_text('{"page":"clip","preset_sub":{"Operator":["Bass"]}}')
                app.load_settings()
                app.ui.preset_sub['Operator'].append('Analog')
                app.save_settings()
                self.assertEqual(json.loads(path.read_text())['preset_sub']['Operator'], ['Bass', 'Analog'])

    def test_failed_settings_replace_preserves_old_file(self):
        app = engine.App.__new__(engine.App)
        app.ui = push_ui.UiState()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text('{"page":"mix"}')
            with patch.object(engine, 'SETTINGS_FILE', str(path)), patch.object(engine.os, 'replace', side_effect=OSError('simulated')):
                app.save_settings()
            self.assertEqual(json.loads(path.read_text()), {'page': 'mix'})
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_watcher_backs_off_and_closes_parent_log_handle(self):
        child = SimpleNamespace(poll=lambda: 1, returncode=1)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(autostart, 'RUN_LOG', str(Path(folder) / 'run.log')), \
                 patch.object(autostart, 'mpc_connected', return_value=True), \
                 patch.object(autostart, 'live_open', return_value=True), \
                 patch.object(autostart, 'osc_port_free', return_value=True), \
                 patch.object(autostart, 'process_running', return_value=False), \
                 patch.object(autostart.os.path, 'exists', return_value=False), \
                 patch.object(autostart, 'log'), \
                 patch.object(autostart.subprocess, 'Popen', return_value=child) as spawn, \
                 patch.object(autostart.time, 'monotonic', return_value=10.), \
                 patch.object(autostart.time, 'sleep', side_effect=[None, RuntimeError('end simulation')]):
                with self.assertRaisesRegex(RuntimeError, 'end simulation'):
                    autostart.main()
                self.assertEqual(spawn.call_count, 1)
                self.assertTrue(spawn.call_args.kwargs['stdout'].closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
