"""Tablet bridge: serves the touch UI and relays between the browser and AbletonOSC.

    browser  --HTTP POST /osc-->  this server  --UDP OSC :11000-->  AbletonOSC (inside Live)
    browser  <--SSE /events-----  this server  <--UDP OSC :11001--  AbletonOSC

Standard library only. Talks to Ableton only through AbletonOSC; never touches
Live's files or preferences.

Usage:  py bridge\\server.py [--port 8800]
"""

import argparse
import json
import os
import queue
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIVE_HOST = "127.0.0.1"
LIVE_PORT = 11000    # AbletonOSC listens here
REPLY_PORT = 11001   # AbletonOSC replies here
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")

# ---------------------------------------------------------------- OSC codec


def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def _osc_str(s):
    return _pad(s.encode("utf-8"))


def encode(address, args):
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, dict) and "f" in a:     # explicit float from the UI: {"f": 1}
            tags += "f"
            payload += struct.pack(">f", float(a["f"]))
        elif isinstance(a, bool):
            tags += "i"
            payload += struct.pack(">i", int(a))
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif a is None:
            tags += "N"
        else:
            tags += "s"
            payload += _osc_str(str(a))
    return _osc_str(address) + _osc_str(tags) + payload


def _read_str(data, i):
    end = data.index(b"\0", i)
    s = data[i:end].decode("utf-8", "replace")
    return s, (end + 4) & ~3


def decode(data):
    """Return a list of (address, args); handles bundles."""
    if data.startswith(b"#bundle"):
        out, i = [], 16
        while i < len(data):
            (size,) = struct.unpack(">i", data[i:i + 4])
            out += decode(data[i + 4:i + 4 + size])
            i += 4 + size
        return out
    address, i = _read_str(data, 0)
    if i >= len(data):
        return [(address, [])]
    tags, i = _read_str(data, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
        elif t == "f":
            args.append(round(struct.unpack(">f", data[i:i + 4])[0], 6)); i += 4
        elif t == "h":
            args.append(struct.unpack(">q", data[i:i + 8])[0]); i += 8
        elif t == "d":
            args.append(struct.unpack(">d", data[i:i + 8])[0]); i += 8
        elif t == "s":
            s, i = _read_str(data, i); args.append(s)
        elif t == "b":
            (n,) = struct.unpack(">i", data[i:i + 4]); i += 4
            args.append(data[i:i + n].hex()); i = (i + n + 3) & ~3
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "N":
            args.append(None)
    return [(address, args)]

# ---------------------------------------------------------------- relay


class Relay:
    def __init__(self):
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind((LIVE_HOST, REPLY_PORT))
        self.clients = set()
        self.lock = threading.Lock()
        self.last_reply = 0.0
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def send(self, address, args):
        self.tx.sendto(encode(address, args), (LIVE_HOST, LIVE_PORT))

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def _recv_loop(self):
        while True:
            try:
                data, _ = self.rx.recvfrom(65536)
                messages = decode(data)
            except OSError:
                continue
            except Exception as e:
                print("bad OSC packet:", e)
                continue
            self.last_reply = time.time()
            for address, args in messages:
                line = json.dumps({"a": address, "v": args})
                with self.lock:
                    for q in list(self.clients):
                        try:
                            q.put_nowait(line)
                        except queue.Full:
                            pass   # slow tablet: drop rather than block Live's replies


relay = None

# ---------------------------------------------------------------- HTTP

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".json": "application/json", ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/events":
            return self._events()
        if path == "/status":
            alive = time.time() - relay.last_reply < 10
            return self._send(200, json.dumps({"live": alive}).encode(), "application/json")
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._send(404, b"not found")
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_POST(self):
        if self.path != "/osc":
            return self._send(404, b"not found")
        n = int(self.headers.get("Content-Length", 0))
        try:
            msgs = json.loads(self.rfile.read(n) or b"[]")
            if isinstance(msgs, dict):
                msgs = [msgs]
            for m in msgs:
                relay.send(str(m["a"]), m.get("v", []))
        except Exception as e:
            return self._send(400, str(e).encode())
        self._send(204)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = relay.subscribe()
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                batch = []
                try:
                    batch.append(q.get(timeout=15))
                    while len(batch) < 200:
                        batch.append(q.get_nowait())
                except queue.Empty:
                    pass
                if batch:
                    body = "".join("data: %s\n\n" % line for line in batch)
                else:
                    body = ": ping\n\n"
                self.wfile.write(body.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            relay.unsubscribe(q)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips) or ["<this-pc-ip>"]


def main():
    global relay
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    opts = ap.parse_args()
    try:
        relay = Relay()
    except OSError as e:
        print("Can't listen on UDP %d (%s). Is another copy of this bridge already running?" % (REPLY_PORT, e))
        return
    relay.send("/live/test", [])
    httpd = ThreadingHTTPServer(("0.0.0.0", opts.port), Handler)
    httpd.daemon_threads = True
    print("MPC-One-Push bridge running.")
    for ip in lan_ips():
        print("  On your tablet/phone open:  http://%s:%d" % (ip, opts.port))
    print("  On this PC:                 http://localhost:%d" % opts.port)
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
