# Changelog

## Unreleased

Arrangement pads now always play notes instead of inheriting Session launch mode. Entering
Arrangement or starting its Play/Record clears Session recording and restores Arrangement
playback, preventing Session overrides from greying out recorded clips. Session recording
mirroring is suppressed on Arrangement to avoid duplicate copies. Four regression checks added.

Arrangement now has a permanent + TRACK button at the upper left, opening MIDI, audio and return
track creation. Position remains visible beneath the label. Available even with a full track bank.

Box selection can now start in empty space below the last track and drag upward across clips.
Added a regression check for the actual background touch target and upward selection.

## 0.3.1 - Arrangement multi-clip selection

Drag from empty lane space or an unselected clip to box-select across tracks. Selected clips
are outlined and a live count follows the box. Lift to open Copy selection / Duplicate after;
hold empty space to paste the group. Timing gaps and track offsets are preserved. The entire
destination is checked before copying, with one Undo step and cleanup of new copies if Live
rejects a later copy. Ruler dragging still scrolls; single selected clips still drag-move.

49 offline checks pass, including reverse-direction selection, callback wiring, stale sources,
occupied destinations and rollback. A real Live test duplicated and pasted three clips across two
tracks, verified preserved spacing, rejected occupied destinations and reversed a whole paste with
one Undo. Temporary tracks were removed and original selection restored. Physical touchscreen
testing remains pending. Update the add-on and restart Live/controller to activate.

## 0.3.0 - Arrangement thumbnails and touch editing

Arrangement clips now show MIDI notes and local WAV/AIFF waveforms with Live's audio timing map.
Tap to select, then drag to an empty destination; hold a clip for an animated action sheet.
Copy, deferred Cut, Paste, Duplicate, Move, Activate/Deactivate, Loop, Rename, Colour, Undo/Redo
and Delete are available. Hold empty lane space to paste at that position. Edits reject stale
selections, frozen tracks, recording and occupied destinations. Background waveform reads use
a bounded worker pool. The sheet has staggered entry fades and clip selection feedback.

Verified MIDI thumbnail data, warped/unwarped audio maps and clip operations on temporary Live
tracks, removed afterward. Added seven offline gesture/data/clipboard checks (38 total).
Resizing/splitting and moves that overlap the source are not implemented. No MPC firmware changes.

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
