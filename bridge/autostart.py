"""Auto-start: runs MPC One Push whenever the MPC One is connected (controller mode) and Live is open.

Checks every 3 s. It doesn't start the app while
  - MPC 3 is running (both would want the MPC's USB connection),
  - something already uses AbletonOSC's reply port 11001 (the app started by hand, or the tablet bridge),
  - you quit the app on the MPC (SHIFT+MENU or MENU > Quit): then it waits until you unplug the MPC or close
    Live before starting it again.
If the app stops for any other reason (MPC unplugged, a crash) it starts again once everything is back.

Run it with no console:  .venv\\Scripts\\pythonw.exe bridge\\autostart.py   (start-autostart.bat does that)
It writes what it does to logs\\autostart.txt; the app's own output goes to logs\\autostart-run.txt.
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mpc_screen  # noqa: E402
import winshot  # noqa: E402

LOG = os.path.join(ROOT, "logs", "autostart.txt")
RUN_LOG = os.path.join(ROOT, "logs", "autostart-run.txt")
QUIT_FILE = os.path.join(ROOT, "logs", "user-quit")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
APP = os.path.join(HERE, "mpc_push_studio.py")
CHECK_S = 3.0


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def mpc_connected():
    return bool(mpc_screen.device_paths(mpc_screen.SCREEN_GUID))


def live_open():
    return winshot.live_main_window()[0] is not None


def process_running(exe):
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe, "/NH"], capture_output=True, text=True,
                         creationflags=0x08000000).stdout      # CREATE_NO_WINDOW
    return exe.lower() in out.lower()


def osc_port_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 11001))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    log("auto-start watching (MPC connected + Live open -> start MPC One Push)")
    app, waiting_for_replug, last_state = None, os.path.exists(QUIT_FILE), None
    failures, retry_at, started_at = 0, 0.0, 0.0
    while True:
        mpc, live = mpc_connected(), live_open()
        if app is not None and app.poll() is not None:
            log("app stopped (exit code %s)%s" % (app.returncode, ", you quit it on the MPC"
                                                   if os.path.exists(QUIT_FILE) else ""))
            app = None
            waiting_for_replug = os.path.exists(QUIT_FILE)
            failures = failures + 1 if time.monotonic() - started_at < 30 else 0
            retry_at = time.monotonic() + min(60, 3 * 2 ** min(failures, 5))
        if waiting_for_replug and (not mpc or not live):        # unplugged / Live closed: ready again
            waiting_for_replug = False
            try:
                os.remove(QUIT_FILE)
            except OSError:
                pass
        state = (mpc, live, waiting_for_replug)
        if state != last_state:
            log("MPC %s, Live %s%s" % ("connected" if mpc else "not connected", "open" if live else "closed",
                                       ", waiting for a replug after you quit" if waiting_for_replug else ""))
            last_state = state
        if app is None and mpc and live and not waiting_for_replug and time.monotonic() >= retry_at:
            if not osc_port_free():
                time.sleep(CHECK_S)                 # the app (started by hand) or the tablet bridge is running
                continue
            if process_running("MPC 3.exe"):
                log("MPC 3 is running: not starting (close MPC 3 to use MPC One Push)")
                time.sleep(15)
                continue
            log("starting MPC One Push")
            with open(RUN_LOG, "a", encoding="utf-8") as out:
                app = subprocess.Popen([PYTHON, "-u", APP, "--live"], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                       creationflags=0x08000000)
            started_at = time.monotonic()
        time.sleep(CHECK_S)


def check():
    """--check: say what auto-start would do right now, without starting anything."""
    mpc, live = mpc_connected(), live_open()
    print("MPC One connected (controller mode):", mpc)
    print("Live open:", live)
    print("MPC 3 running:", process_running("MPC 3.exe"))
    print("reply port 11001 free (app / tablet bridge not running):", osc_port_free())
    print("you quit the app on the MPC (waits for a replug):", os.path.exists(QUIT_FILE))
    if not (mpc and live):
        print("-> would wait for the MPC and Live")
    elif os.path.exists(QUIT_FILE):
        print("-> would wait until the MPC is unplugged or Live is closed")
    elif not osc_port_free():
        print("-> nothing to do: the app (or the tablet bridge) is already running")
    elif process_running("MPC 3.exe"):
        print("-> would not start: close MPC 3 first")
    else:
        print("-> would start MPC One Push now")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
        sys.exit()
    try:
        # Windows releases the byte lock even if pythonw exits unexpectedly.
        import msvcrt
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "autostart.lock"), "a+b") as guard:
            guard.seek(0)
            if not guard.read(1):
                guard.write(b"0")
                guard.flush()
            guard.seek(0)
            try:
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                log("auto-start is already watching; duplicate watcher exiting")
                sys.exit(0)
            main()
    except Exception as e:                          # pythonw has no console: leave a note
        log("auto-start stopped: %r" % e)
        raise
