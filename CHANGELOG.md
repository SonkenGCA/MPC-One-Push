# Changelog

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
