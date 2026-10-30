"""Send MIDI to a Windows MIDI output port (winmm, standard library only).

Used to play the MPC's pads into Live through a virtual MIDI cable such as loopMIDI
(Windows 10 has no built-in way to route MIDI between programs).
"""

import ctypes
import ctypes.wintypes as wt

_winmm = ctypes.WinDLL("winmm")
_winmm.midiOutOpen.argtypes = [ctypes.POINTER(wt.HANDLE), wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
_winmm.midiOutShortMsg.argtypes = [wt.HANDLE, wt.DWORD]
_winmm.midiOutClose.argtypes = [wt.HANDLE]
_winmm.midiOutReset.argtypes = [wt.HANDLE]

# Port names tried in order (case-insensitive substring match).
PREFERRED = ("mpc one push", "loopmidi", "loopbe", "virtual")


class _OutCaps(ctypes.Structure):
    _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT), ("szPname", wt.WCHAR * 32),
                ("wTechnology", wt.WORD), ("wVoices", wt.WORD), ("wNotes", wt.WORD), ("wChannelMask", wt.WORD),
                ("dwSupport", wt.DWORD)]


def output_names():
    out = []
    for i in range(_winmm.midiOutGetNumDevs()):
        caps = _OutCaps()
        _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        out.append(caps.szPname)
    return out


class MidiOut:
    def __init__(self):
        self.h, self.name = None, None

    def open(self):
        """Open the first virtual MIDI port found. Returns its name, or None if there isn't one."""
        names = output_names()
        for want in PREFERRED:
            for i, n in enumerate(names):
                if want in n.lower():
                    h = wt.HANDLE()
                    if _winmm.midiOutOpen(ctypes.byref(h), i, None, None, 0) == 0:
                        self.h, self.name = h, n
                        return n
        return None

    def send(self, status, d1, d2=0):
        if self.h:
            _winmm.midiOutShortMsg(self.h, status | (d1 & 0x7F) << 8 | (d2 & 0x7F) << 16)

    def close(self):
        if self.h:
            _winmm.midiOutReset(self.h)      # note-offs for anything still sounding
            _winmm.midiOutClose(self.h)
            self.h = None
