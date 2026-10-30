# Changelog

## 0.2.1 — Arrangement recording start fix

Arrangement dial/timeline seeking now sets Live’s playback start marker as well as its current
playhead position. PLAY START resets both to zero. This fixes Play/Record returning to an older
mouse-selected start position. Verified both positions against Live while stopped, then restored them.

## 0.2.0 — Playing, mixer, racks and arrangement editing

Added native Live count-in and device duplication/reordering.
Added cue/crossfader controls and rack macro variation store/recall/randomization.
Added chord modes, soft/hard velocity response and pressure routing (poly/channel/mod wheel/off).
Held chord voicings are captured so octave/mode changes cannot leave their notes stuck.
Drum Rack and 16 Levels bypass chords. Added SHIFT+tap arrangement actions: duplicate, delete,
move earlier/later or to the playhead. Occupied destinations, stale selections, frozen tracks and
recording are rejected; edits are grouped for Undo. These additions have offline test coverage;
physical pad testing remains pending. Arrangement and device editing passed a real Live smoke test
on a temporary track that was deleted afterward.

## 0.1.2 — Setup and shutdown

The single launcher now creates its Python environment and installs Pillow when needed.
Startup/USB failure and normal exit now clean up held notes, preview, pointer state, MIDI and OSC resources.
Added regression checks for startup and shutdown failures and releasing OSC resources.

## 0.1.1 — One launcher

Simplified the public package to start-push.bat for the current Studio UI. Removed alternate launchers
and classic-interface instructions. Added a setup hint when the Python environment is missing.

## 0.1.0 â€” Public preview

Initial public snapshot of the working Windows/MPC One controller: touchscreen Studio UI,
session/arrangement/clip/mixer/device/browser workflows and controller-mode USB integration.
Includes motion feedback, settings recovery, auto-start safeguards and 12 offline regression checks.

This release uses clean public history; private device captures and development-machine files are excluded.
