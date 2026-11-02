"""Shared pieces for the LED FX app: logging, colour helpers, themes, settings file, control panel server."""

import json
import math
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAMMA = 2.2
BLACK = (0.0, 0.0, 0.0)

# ----------------------------------------------------------------- logging --

LOG_PATH = None   # set by main() from --log


def log(text):
    """Print, and append to --log file (pythonw has no console)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}"
    if sys.stdout:
        print(line, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ----------------------------------------------------------------- helpers --

def scale(rgb, k):
    return tuple(c * k for c in rgb)


def lerp(a, b, k):
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def clamp01(x):
    return max(0.0, min(1.0, x))


def smoothstep(lo, hi, x):
    k = clamp01((x - lo) / (hi - lo))
    return k * k * (3 - 2 * k)


def envelope(age, duration):
    """Soft sin^2 bump: 0 -> 1 -> 0 over duration, 0 outside it."""
    if not 0 <= age < duration:
        return 0.0
    return math.sin(math.pi * age / duration) ** 2


def breathe(t, period, phase=0.0):
    """0..1, slow in and out, lingering a little at the bottom."""
    return (0.5 - 0.5 * math.cos(2 * math.pi * (t / period + phase))) ** 1.6


def rec_blink(now, period=1.0):
    """Studio REC light: on for half the period, off for the other half, with barely softened edges.
    Uses wall-clock time so the speed setting doesn't change it."""
    x = (now / period) % 1.0
    return smoothstep(0.0, 0.04, x) * (1 - smoothstep(0.5, 0.54, x))


def to7bit(v):
    """0..1 float -> 0..127 with gamma, for LED levels."""
    return max(0, min(127, round(127 * (clamp01(v) ** GAMMA))))


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(round(255 * clamp01(v)) for v in rgb)


# ------------------------------------------------------------------ themes --

class Theme:
    """Maps brightness 0..1 to colour: black -> shadow -> mid -> highlight."""

    def __init__(self, name, shadow, mid, highlight):
        self.name = name
        self.stops = [(0.0, BLACK), (0.3, shadow), (0.68, mid), (1.0, highlight)]

    def color(self, i):
        i = clamp01(i)
        for (p0, c0), (p1, c1) in zip(self.stops, self.stops[1:]):
            if i <= p1:
                return lerp(c0, c1, (i - p0) / (p1 - p0))
        return self.stops[-1][1]


THEMES = [
    Theme("amber",  (0.50, 0.10, 0.00), (0.95, 0.42, 0.06), (1.00, 0.80, 0.50)),
    Theme("ice",    (0.02, 0.12, 0.40), (0.10, 0.55, 0.65), (0.70, 0.92, 1.00)),
    Theme("mono",   (0.45, 0.38, 0.30), (0.80, 0.70, 0.55), (1.00, 0.92, 0.80)),
    Theme("indigo", (0.18, 0.05, 0.50), (0.35, 0.35, 0.85), (0.80, 0.75, 1.00)),
    Theme("moss",   (0.05, 0.30, 0.08), (0.40, 0.65, 0.30), (0.95, 0.92, 0.70)),
    # subtler, mostly two-hue: the shadow and the highlight come from different colour families
    Theme("dusk",      (0.22, 0.06, 0.30), (0.65, 0.30, 0.38), (1.00, 0.75, 0.60)),
    Theme("sea glass", (0.02, 0.20, 0.22), (0.35, 0.55, 0.42), (0.80, 0.95, 0.82)),
    Theme("slate",     (0.10, 0.14, 0.25), (0.40, 0.48, 0.58), (0.88, 0.92, 0.96)),
    Theme("sand",      (0.30, 0.18, 0.10), (0.65, 0.52, 0.38), (0.95, 0.88, 0.75)),
    Theme("tide",      (0.04, 0.08, 0.35), (0.15, 0.50, 0.55), (1.00, 0.90, 0.60)),
    Theme("aurora",    (0.02, 0.25, 0.15), (0.10, 0.55, 0.55), (0.75, 0.65, 1.00)),
]
THEME_NAMES = [th.name for th in THEMES]


def themes_for_ui():
    return [{"name": th.name, "colors": [hex_color(c) for _, c in th.stops[1:]]} for th in THEMES]


# ------------------------------------------------------------------ config --

def load_config(path, defaults):
    cfg = json.loads(json.dumps(defaults))   # deep copy
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in defaults})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(path, cfg):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log(f"Couldn't save settings: {e}")


# -------------------------------------------------------------- control ui --
# Tiny local web server (stdlib only): http://127.0.0.1:8765
# HTTP threads only queue commands and read the last published snapshot;
# all device and effect state stays on the main thread.

class ControlServer:
    def __init__(self, port, ui_file):
        self.port = port
        self.commands = queue.Queue()
        self.snapshot = b"{}"
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # pythonw has no stderr
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _local(self):
                # Reject DNS-rebinding tricks: only answer to our own host name
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def do_GET(self):
                if not self._local():
                    return self._send(403, b"forbidden", "text/plain")
                if self.path in ("/", "/index.html"):
                    try:
                        with open(ui_file, "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send(404, b"control panel html not found next to the script", "text/plain")
                elif self.path == "/api/state":
                    self._send(200, server.snapshot, "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                # JSON only: a browser won't send that cross-origin without a CORS preflight we never answer
                if (not self._local() or self.path != "/api/command"
                        or not self.headers.get("Content-Type", "").startswith("application/json")):
                    return self._send(400, b"bad request", "text/plain")
                try:
                    length = min(int(self.headers.get("Content-Length", 0)), 65536)
                    cmd = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._send(400, b"bad json", "text/plain")
                if isinstance(cmd, dict):
                    server.commands.put(cmd)
                self._send(200, b'{"ok": true}', "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def pending(self):
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                return out

    def publish(self, data):
        self.snapshot = json.dumps(data).encode()
