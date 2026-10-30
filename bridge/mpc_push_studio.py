"""Opt-in launcher. Default = offline desktop preview; --live uses the existing engine."""
import argparse
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw
import mpc_push as engine
import push_ui as base
from push_studio_ui import Renderer, install_theme, font

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'design' / 'push-studio' / 'preview'


def demo_state():
    live, ui = engine.demo_live(), base.UiState()
    ui.pitch_lo, ui.focus_pitch = 34, 42
    ui.midi_port = 'Offline preview'
    return live, ui


def render_gallery(motion=True):
    OUT.mkdir(parents=True, exist_ok=True)
    live, ui = demo_state()
    r = Renderer(motion=False)
    pages = []
    def save(name):
        img, targets = r.render(live, ui)
        img.save(OUT / (name + '.png'))
        pages.append(name)
        print('%s: %d touch targets' % (name, len(targets)))
    for p in base.PAGES:
        ui.page = p
        save(p)
    ui.page, ui.clip_mode = 'clip', 'auto'
    save('automation')
    ui.page, ui.pads = 'session', 'launch'
    save('pad-bank')
    ui.page, ui.focus_param, ui.focus_at = 'device', 1, time.time()
    ui.qlink_touch = {0}
    save('parameter-focus')
    ui.focus_param, ui.qlink_touch = None, set()
    dev = live.dev
    dev.names = ['Drum Rack', 'Glue Compressor', 'Reverb']
    dev.info = [('DrumGroupDevice', 1, 1, 1, 6), ('GlueCompressor', 1, 0, 0, 0), ('Reverb', 1, 0, 0, 0)]
    dev.idx, dev.view, dev.drum = 0, 'chains', True
    dev.chains = [('Kick Atlas', 36), ('SideStick', 37), ('Snare', 38), ('Clap', 39), ('Closed Hat', 42), ('808', 48)]
    ui.drum_note = 38
    save('drum-rack')
    dev.view, ui.add_menu = 'devices', True
    save('add-device')
    ui.add_menu, ui.help = False, True
    save('help')
    ui.help = False
    # Offline Live.connected is a property; last_rx controls live connections only.
    r._waiting(ImageDraw.Draw(waiting := Image.new('RGB', (1280, 800))))
    waiting.save(OUT / 'waiting.png')
    pages.append('waiting')
    live, ui = demo_state()
    tick = [0.0]
    r = Renderer(motion=motion, clock=lambda: tick[0])
    frames = []
    for frame in range(160):
        tick[0] = frame / 20
        ui.page = ['session', 'mix', 'device', 'browse'][min(3, frame // 40)]
        live.sel = [2, 3, 4, 2][(frame // 10) % 4]
        ui.br_sel = (frame // 10) % 5
        ui.sheet = {'title': 'Track options', 'color': (247, 228, 90),
                    'items': [('Rename', ('rename',)), ('Duplicate', ('dup_track',)),
                              ('Delete track', ('del_track',))]} if 100 <= frame < 115 else None
        if frame % 20 == 8:
            r.press(500, 750)
        for i, tr in enumerate(live.tracks):
            tr.meter = .25 + .55 * abs(__import__('math').sin(frame / 7 + i))
        frames.append(r.render(live, ui)[0].resize((960, 600)))
    frames[0].save(OUT / 'motion.gif', save_all=True, append_images=frames[1:], duration=50, loop=0)
    cards = ''.join('<section id="%s"><h2>%s</h2><img src="%s.png" alt="%s screen"></section>' %
                    (p, p.replace('-', ' ').title(), p, p) for p in pages)
    (OUT / 'index.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MPC One / Push Studio</title>
<style>*{box-sizing:border-box}body{margin:0;background:#080909;color:#eeefeb;font:16px Segoe UI,sans-serif}
header,main{max-width:1328px;margin:auto;padding:32px 24px}header{padding-top:60px}h1{font-size:40px;font-weight:500;margin:10px 0}
p{color:#a1a7a4;line-height:1.6}small{letter-spacing:3px;color:#b3db98}nav{display:flex;gap:10px;flex-wrap:wrap}a{color:#ced8cd;text-decoration:none;padding:9px 14px;border:1px solid #303432;border-radius:4px}
section{margin-bottom:48px;scroll-margin-top:20px}h2{font-size:17px;font-weight:400;color:#a1a7a4}img{display:block;width:100%;border:1px solid #272b28;border-radius:5px}
@media(prefers-reduced-motion:reduce){.motion{display:none}}</style>
<header><small>MPC ONE / PUSH STUDIO</small><h1>Made for your fingertips.</h1>
<p>Offline design preview · 1280 × 800 · simulated Live state<br>Uses your existing controller engine when launched with --live.</p>
<nav>''' + ''.join('<a href="#%s">%s</a>' % (p, p.capitalize()) for p in base.PAGES) +
        '</nav></header><main><section class="motion"><h2>Motion study · queued clips, tab glide, meter response</h2><img src="motion.gif" alt="Animated interface preview"></section>' + cards + '</main></html>', encoding='utf-8')
    print(OUT / 'index.html')


def desktop(motion=True):
    import tkinter as tk
    from PIL import ImageTk
    live, ui = demo_state()
    renderer = Renderer(motion=motion)
    window = tk.Tk()
    window.title('MPC One / Push Studio — OFFLINE PREVIEW — 1–6: pages, A: automation, H: help')
    canvas = tk.Canvas(window, width=1280, height=800, highlightthickness=0, bg='black')
    canvas.pack(fill='both', expand=True)
    window.minsize(800, 500)
    item = canvas.create_image(0, 0, anchor='nw')
    targets = []
    scale = [1.0]
    def draw():
        img, current = renderer.render(live, ui)
        targets[:] = current
        scale[0] = min(canvas.winfo_width() / 1280, canvas.winfo_height() / 800)
        size = (max(1, round(1280 * scale[0])), max(1, round(800 * scale[0])))
        canvas.photo = ImageTk.PhotoImage(img.resize(size))
        canvas.itemconfigure(item, image=canvas.photo)
        window.after(50, draw)
    def click(event):
        x, y = event.x / scale[0], event.y / scale[0]
        renderer.press(x, y)
        for x0, y0, x1, y1, action in reversed(targets):
            if x0 <= x < x1 and y0 <= y < y1:
                if action[0] == 'page': ui.page = action[1]
                elif action[0] == 'help_close': ui.help = False
                elif action[0] == 'track': live.sel = action[1]
                elif action[0] == 'pads_mode': ui.pads = 'launch' if ui.pads == 'play' else 'play'
                elif action[0] == 'mix_target': ui.mix_target = action[1]
                elif action[0] == 'play': live.playing = not live.playing
                elif action[0] == 'metro': live.metro = not live.metro
                elif action[0] == 'rec': live.rec = not live.rec
                elif action[0] == 'clip' and len(action) == 3 and isinstance(action[1], int):
                    live.tracks[action[1]].playing = action[2]
                elif action[0] == 'toggle':
                    tr = live.tracks[action[2]]
                    setattr(tr, action[1], not getattr(tr, action[1]))
                return
    def key(event):
        if event.char in '123456' and event.char:
            ui.page = base.PAGES[int(event.char) - 1]
        elif event.char.lower() == 'a': ui.clip_mode = 'auto' if ui.clip_mode == 'notes' else 'notes'
        elif event.char.lower() == 'h': ui.help = not ui.help
        elif event.keysym == 'Escape': window.destroy()
    canvas.bind('<Button-1>', click)
    window.bind('<Key>', key)
    window.after(100, draw)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--live', action='store_true', help='Use the MPC and Ableton through the existing engine')
    modes.add_argument('--demo', action='store_true', help='Write offline screenshots and motion gallery')
    modes.add_argument('--desktop', action='store_true', help='Offline desktop preview (default)')
    parser.add_argument('--reduced-motion', action='store_true')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--snapshots', action='store_true')
    args = parser.parse_args()
    install_theme()
    if args.demo:
        render_gallery(not args.reduced_motion)
    elif args.live:
        # Renderer factory injection is local to this process. No change to engine files.
        engine.Renderer = lambda: Renderer(motion=not args.reduced_motion)
        class StudioApp(engine.App):
            def touch_down(self, x, y):
                self.renderer.press(x, y)
                self.dirty.set()
                super().touch_down(x, y)

            def frame(self):
                super().frame()
                if self.renderer.animating:
                    self.dirty.set()
        app = StudioApp()
        app.run(args.seconds, snapshots=args.snapshots)
    else:
        desktop(not args.reduced_motion)


if __name__ == '__main__':
    main()
