"""Capture Ableton Live's plug-in windows so they can be shown on the MPC screen (read-only).

Live titles plug-in windows "<device name>/<track name>" (e.g. "kHs Gain/5-MIDI"). The window has to be open:
Live opens it when a plug-in loads if "Auto-Open Plug-In Windows" is on (it is on this PC), or you open it
with the device's wrench button. PrintWindow(PW_RENDERFULLCONTENT) also captures windows that are covered.
"""

import ctypes
import ctypes.wintypes as wt
import time

from PIL import Image

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32")
_kernel32 = ctypes.WinDLL("kernel32")
_EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_user32.GetWindowDC.restype = wt.HDC
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
# Handles are 64-bit: declare them, or ctypes passes them as 32-bit ints and big ones fail at random.
_user32.GetWindowDC.argtypes = [wt.HWND]
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wt.HDC]
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
_user32.WindowFromPoint.restype = wt.HWND
_user32.WindowFromPoint.argtypes = [wt.POINT]
_user32.GetAncestor.restype = wt.HWND
PW_RENDERFULLCONTENT = 2
try:        # physical pixels everywhere, so captures and clicks line up even with Windows display scaling
    _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))      # PER_MONITOR_AWARE_V2
except Exception:
    pass


def _exe(pid):
    h = _kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    n = wt.DWORD(260)
    _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    _kernel32.CloseHandle(h)
    return buf.value


def live_windows():
    """[(hwnd, title)] of Live's visible top-level windows."""
    found = []

    @_EnumProc
    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if "Ableton Live" in _exe(pid.value):
            t = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(hwnd, t, 256)
            found.append((hwnd, t.value))
        return True
    _user32.EnumWindows(cb, 0)
    return found


def find_plugin_window(device, track):
    """The window of plug-in `device` on track `track`, or None if it isn't open."""
    wins = live_windows()
    for hwnd, title in wins:
        if title == "%s/%s" % (device, track):
            return hwnd
    for hwnd, title in wins:                                # renamed track? match the device part
        if title.startswith(device + "/"):
            return hwnd
    return None


def capture(hwnd):
    """PIL image of the window, or None (e.g. minimised or closed)."""
    r = wt.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0 or _user32.IsIconic(hwnd):
        return None
    hdc = _user32.GetWindowDC(hwnd)
    mdc = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = _gdi32.SelectObject(mdc, bmp)
    try:
        if not _user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
            return None
        buf = ctypes.create_string_buffer(w * h * 4)
        bmi = (ctypes.c_uint32 * 10)(40, w, (-h) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
        _gdi32.GetDIBits(mdc, bmp, 0, h, buf, bmi, 0)
        return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        _gdi32.SelectObject(mdc, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mdc)
        _user32.ReleaseDC(hwnd, hdc)


# ---------------------------------------------------------------- touch -> mouse (user approved 2026-10-29)

class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", _MouseInput)]          # 40 bytes on x64, as SendInput requires


MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004


def _button(flag):
    inp = _Input(type=0, mi=_MouseInput(0, 0, 0, flag, 0, 0))
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input))


class Clicker:
    """Plays an MPC touch on a plug-in window with the real mouse: press, drag, release, then the pointer
    goes back where it was. It only presses if that spot on screen really belongs to the window."""

    def __init__(self):
        self.hwnd, self.saved, self.pressed = None, None, False

    def _screen(self, hwnd, wx, wy):
        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left + int(wx), r.top + int(wy)

    def _owns(self, hwnd, sx, sy):
        at = _user32.WindowFromPoint(wt.POINT(sx, sy))
        return bool(at) and _user32.GetAncestor(at, 2) == hwnd        # GA_ROOT

    def down(self, hwnd, wx, wy):
        """wx, wy: position inside the window (window pixels, title bar included). Returns "" or a reason."""
        sx, sy = self._screen(hwnd, wx, wy)
        if not self._owns(hwnd, sx, sy):
            _user32.ShowWindow(hwnd, 5)                                # SW_SHOW
            _user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
            if not self._owns(hwnd, sx, sy):
                return "the plug-in window is covered on the PC screen"
        pt = wt.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        self.saved, self.hwnd = (pt.x, pt.y), hwnd
        _user32.SetCursorPos(sx, sy)
        _button(MOUSEEVENTF_LEFTDOWN)
        self.pressed = True
        return ""

    def move(self, wx, wy):
        if self.pressed:
            _user32.SetCursorPos(*self._screen(self.hwnd, wx, wy))

    def up(self, wx=None, wy=None):
        if not self.pressed:
            return
        if wx is not None:
            _user32.SetCursorPos(*self._screen(self.hwnd, wx, wy))
        _button(MOUSEEVENTF_LEFTUP)
        self.pressed = False
        time.sleep(0.02)
        if self.saved:
            _user32.SetCursorPos(*self.saved)


# ---------------------------------------------------------------- keystrokes into Live (Save / Save As)
#
# Live's script API can't save a set, so SAVE on the MPC presses Ctrl+S in Live's main window, and names a new
# set by typing into Live's own Save dialog (a standard Windows file dialog).

class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KeyInput(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("ki", _KeybdInput), ("_pad", ctypes.c_ubyte * 8)]   # 40 bytes, like _Input


KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_CONTROL, VK_SHIFT, VK_MENU, VK_RETURN, VK_ESCAPE = 0x11, 0x10, 0x12, 0x0D, 0x1B
_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


def _key(vk=0, up=False, char=None):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_UNICODE if char else 0)
    inp = _KeyInput(type=1, ki=_KeybdInput(0 if char else vk, ord(char) if char else 0, flags, 0, 0))
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_KeyInput))


def chord(*vks):
    """Press keys together (e.g. VK_CONTROL, ord("S")) and release them in reverse order."""
    for vk in vks:
        _key(vk)
    for vk in reversed(vks):
        _key(vk, up=True)


def type_text(text):
    for ch in text:
        _key(char=ch)
        _key(char=ch, up=True)


def window_class(hwnd):
    b = ctypes.create_unicode_buffer(64)
    _user32.GetClassNameW(hwnd, b, 64)
    return b.value


def live_main_window():
    """(hwnd, title) of Live's main window ("<set>[*] - Ableton Live 12 Suite"), or (None, "")."""
    for hwnd, title in live_windows():
        if " - Ableton Live" in title:
            return hwnd, title
    return None, ""


def live_dialog():
    """A Windows dialog Live has open (e.g. its Save dialog), or None."""
    for hwnd, _title in live_windows():
        if window_class(hwnd) == "#32770":
            return hwnd
    return None


def focus(hwnd):
    """Bring a window to the front. Windows only lets a background program do that right after a key
    event, hence the Alt tap. Returns True if it worked."""
    if _user32.GetForegroundWindow() == hwnd:
        return True
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)                                    # SW_RESTORE
    _key(VK_MENU)
    _key(VK_MENU, up=True)
    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    return _user32.GetForegroundWindow() == hwnd


def save_live_set(name=None):
    """Save Live's set. name=None: Ctrl+S (the set already has a file). With a name: Ctrl+Shift+S, type the
    name into Live's Save dialog, Enter. Returns "" when the window title shows it saved, else a reason."""
    hwnd, _title = live_main_window()
    if not hwnd:
        return "Live's window wasn't found"
    if live_dialog():
        return "Live already has a dialog open on the PC screen"
    if not focus(hwnd):
        return "Windows wouldn't bring Live to the front"
    if name is None:
        chord(VK_CONTROL, ord("S"))
    else:
        chord(VK_CONTROL, VK_SHIFT, ord("S"))
        dlg = None
        for _ in range(30):                                           # the dialog takes a moment to open
            time.sleep(0.1)
            dlg = live_dialog()
            if dlg:
                break
        if not dlg:
            return "Live's Save dialog didn't open"
        time.sleep(0.3)
        focus(dlg)
        chord(VK_CONTROL, ord("A"))                                   # replace whatever name is suggested
        type_text(name)
        chord(VK_RETURN)
    for _ in range(40):                                               # saved = no dialog and no * in the title
        time.sleep(0.1)
        _h, title = live_main_window()
        if not live_dialog() and title and "*" not in title.split(" - Ableton Live")[0]:
            return ""
    if live_dialog():
        return "Live is asking something on the PC screen (maybe that name exists)"
    return "Live didn't confirm the save"


# ---------------------------------------------------------------- Live's dialogs, mirrored on the MPC

_user32.IsWindowEnabled.argtypes = [wt.HWND]


def live_modal_dialog():
    """The dialog Live is showing (Save changes?, Export Audio/Video, Open, Save As...), or None.
    While one is open, Live's main window is disabled; the dialog is the enabled Live window on top."""
    main, _title = live_main_window()
    if not main or _user32.IsWindowEnabled(main):
        return None
    for hwnd, _t in live_windows():                     # top-most first
        if hwnd != main and _user32.IsWindowEnabled(hwnd):
            return hwnd
    return None


def live_keys(*vks):
    """Press a shortcut in Live's main window (e.g. Ctrl+N). Returns "" or why not."""
    hwnd, _title = live_main_window()
    if not hwnd:
        return "Live's window wasn't found"
    if live_modal_dialog():
        return "Live already has a dialog open"
    if not focus(hwnd):
        return "Windows wouldn't bring Live to the front"
    chord(*vks)
    return ""


def open_live_set(path):
    """Open a set: Ctrl+O, type the path into Live's Open dialog, Enter. If the current set has unsaved
    changes, Live asks about them next (the app mirrors that question on the MPC)."""
    why = live_keys(VK_CONTROL, ord("O"))
    if why:
        return why
    dlg = None
    for _ in range(30):
        time.sleep(0.1)
        dlg = live_dialog()
        if dlg:
            break
    if not dlg:
        return "Live's Open dialog didn't appear"
    time.sleep(0.3)
    focus(dlg)
    type_text(path)
    chord(VK_RETURN)
    return ""
