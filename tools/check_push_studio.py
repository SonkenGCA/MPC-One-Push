"""Offline contract checks; never constructs App or opens hardware/OSC ports."""
from pathlib import Path
import sys
import time
import unittest
from collections import Counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bridge'))
import mpc_push as engine
import push_ui as base
from push_studio_ui import Renderer, install_theme


class RenderContract(unittest.TestCase):
    def setUp(self):
        self.live, self.ui = engine.demo_live(), base.UiState()
        self.ui.pitch_lo, self.ui.focus_pitch = 34, 42
        self.renderer = Renderer(motion=False)

    def test_all_pages_preserve_engine_actions(self):
        for page in base.PAGES + ['clip-auto', 'help']:
            with self.subTest(page=page):
                self.ui.page = 'session' if page == 'help' else page.split('-')[0]
                self.ui.help = page == 'help'
                self.ui.clip_mode = 'auto' if page == 'clip-auto' else 'notes'
                _, previous = base.Renderer().render(self.live, self.ui)
                image, current = self.renderer.render(self.live, self.ui)
                self.assertEqual(image.size, (1280, 800))
                self.assertEqual(Counter(t[4] for t in previous), Counter(t[4] for t in current))

    def test_drag_geometry_matches_engine(self):
        for page in ('mix', 'device', 'clip', 'arrange'):
            self.ui.page = page
            old = base.Renderer().render(self.live, self.ui)[1]
            new = self.renderer.render(self.live, self.ui)[1]
            pick = lambda ts: {t[4]: t[:4] for t in ts if t[4][0] == 'drag' and t[4][1] != 'tempo'}
            self.assertEqual(pick(old), pick(new))

    def test_header_targets_do_not_overlap(self):
        targets = [t for t in self.renderer.render(self.live, self.ui)[1] if t[1] == 0 and t[3] == base.HEAD_H]
        for i, a in enumerate(targets):
            for b in targets[i + 1:]:
                self.assertLessEqual(min(a[2], b[2]), max(a[0], b[0]), (a, b))

    def test_interrupted_motion_keeps_destination_targets(self):
        tick = [0.0]
        moving = Renderer(clock=lambda: tick[0])
        for page in ['session', 'mix', 'device', 'browse', 'clip', 'session']:
            self.ui.page = page
            tick[0] += .04
            moving.press(500, 300)
            _, actual = moving.render(self.live, self.ui)
            _, expected = Renderer(motion=False).render(self.live, self.ui)
            self.assertEqual(actual, expected)

    def test_sheet_motion_keeps_modal_hit_order(self):
        tick = [0.0]
        moving = Renderer(clock=lambda: tick[0])
        moving.render(self.live, self.ui)
        self.ui.sheet = {'title': 'Track', 'items': [('Rename', ('rename',)), ('Delete', ('del_track',))]}
        tick[0] = .04
        _, actual = moving.render(self.live, self.ui)
        _, expected = Renderer(motion=False).render(self.live, self.ui)
        self.assertEqual(actual, expected)
        self.assertTrue(moving.animating)

    def test_offline_render_never_sends_commands(self):
        with patch.object(self.live, 'send', side_effect=AssertionError('Unexpected OSC command')):
            for page in base.PAGES:
                self.ui.page = page
                before = list(self.live.dev.pvals)
                self.renderer.render(self.live, self.ui)
                self.assertEqual(before, self.live.dev.pvals)
        self.assertNotIn('mpc_screen', sys.modules)
        self.assertNotIn('mpc_control', sys.modules)

    def test_specialist_views_keep_targets(self):
        self.ui.page = 'device'
        dev = self.live.dev
        for state in ('add', 'plugins', 'rack', 'simpler', 'plugin'):
            with self.subTest(state=state):
                self.ui.add_menu = state in ('add', 'plugins')
                self.ui.add_kind = 'plugins' if state == 'plugins' else 'native'
                self.live.plugins = ['Example Synth', 'Example Reverb']
                dev.view = 'chains' if state == 'rack' else 'devices'
                dev.drum = state == 'rack'
                dev.chains = [('Kick', 36), ('Snare', 38)]
                dev.info[dev.idx] = ('OriginalSimpler' if state == 'simpler' else 'PluginDevice' if state == 'plugin' else 'DrumGroupDevice', 1, 1, 1, 2)
                self.ui.plugin_ui = state == 'plugin'
                _, old = base.Renderer().render(self.live, self.ui)
                _, new = self.renderer.render(self.live, self.ui)
                self.assertEqual(Counter(t[4] for t in old), Counter(t[4] for t in new))

    def test_motion_settles_and_reduced_motion_is_stable(self):
        tick = [0.0]
        r = Renderer(clock=lambda: tick[0])
        r.render(self.live, self.ui)
        self.ui.page = 'device'
        tick[0] += .04
        r.render(self.live, self.ui)
        self.assertTrue(r.animating)
        for _ in range(30):
            tick[0] += .04
            r.render(self.live, self.ui)
        self.assertFalse(r.animating)
        self.ui.page = 'session'
        a = self.renderer.render(self.live, self.ui)[0]
        self.assertFalse(self.renderer.animating)
        b = self.renderer.render(self.live, self.ui)[0]
        self.assertEqual(a.tobytes(), b.tobytes())


if __name__ == '__main__':
    install_theme()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RenderContract))
    if not result.wasSuccessful():
        raise SystemExit(1)
    live, ui = engine.demo_live(), base.UiState()
    for page in base.PAGES:
        ui.page = page
        timings = []
        for cls in (base.Renderer, Renderer):
            r = cls()
            r.render(live, ui)
            start = time.perf_counter()
            for _ in range(10): r.render(live, ui)
            timings.append((time.perf_counter() - start) * 100)
        print('%s: existing %.1f ms / studio %.1f ms' % (page, *timings))
