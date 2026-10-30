#!/usr/bin/env python3
"""
lcxl3_fx.py - ambient LED art piece for the Novation Launch Control XL 3.

Uses the XL 3's DAW mode (Launch Control XL 3 Programmer's Reference v1.0):
  Enable DAW mode:   F0 00 20 29 02 15 02 7F F7   (disable: ... 02 00 F7)
  RGB LED colour:    F0 00 20 29 02 15 01 53 <control index> <R> <G> <B> F7
  Screen config:     F0 00 20 29 02 15 04 <target> <config> F7
  Screen text:       F0 00 20 29 02 15 06 <target> <field> <ascii...> F7
All messages go to the device's DAW port (not the plain MIDI port).

LED grid used here (control indices = DAW-mode CC numbers):
  row 0  encoders row 1     CC 13-20
  row 1  encoders row 2     CC 21-28
  row 2  encoders row 3     CC 29-36
  row 3  upper button row   CC 37-44
  row 4  lower button row   CC 45-52

Effects produce a brightness value per LED; the active theme turns that into
colour (shadow -> mid -> highlight), so every effect shares one palette.

Effects (two banks of 8 on the lower buttons):
  bank 1:  1 breathe  2 tide  3 aurora  4 ember
           5 constellation  6 comet  7 ripple  8 lighthouse
  bank 2:  9 drift  10 horizon  11 heartbeat  12 interference
           13 fireflies  14 candle  15 orbit  16 lattice
Themes:   amber, ice, mono, indigo, moss

Controls:
  Fader 1              speed
  Fader 2              brightness
  Fader 3              theme (5 zones, bottom to top)
  Lower button row     pick effect 1-8 in the current bank
                       (press the active effect's button again to flip bank)
  Upper button row     soft swell on that column
  Play / Record        next / previous effect (through both banks)
  Ctrl+C               quit (fade out, device back to normal mode)

Setup (once):
  python3 -m venv .venv
  source .venv/bin/activate           (Windows: .venv\\Scripts\\activate)
  pip install --upgrade pip
  pip install mido python-rtmidi

Run (each time):
  source .venv/bin/activate           (Windows: .venv\\Scripts\\activate)
  python lcxl3_fx.py                 (or --list, --effect aurora, --theme ice, --auto 5, --debug, --log fx.log)
  deactivate                         (when done)

Unplugging is fine: the script waits for the device and picks up where it left off.
Windows autostart: the "LCXL3-FX" scheduled task runs it with pythonw.exe at logon.
Control panel: http://127.0.0.1:8765 (lcxl3_fx_ui.html) - effects, loop, theme, speed,
brightness, on/off. Settings are kept in lcxl3_fx.json.
"""

import argparse
import json
import math
import os
import queue
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mido

# ---------------------------------------------------------------- protocol --

HEADER = [0x00, 0x20, 0x29, 0x02, 0x15]
ROWS, COLS = 5, 8
GRID_CC = [list(range(13 + r * 8, 21 + r * 8)) for r in range(ROWS)]  # 13..52

CH_BUTTONS = 0        # MIDI channel 1
CH_FEATURES = 6       # MIDI channel 7
CH_ANALOG = 15        # MIDI channel 16
CC_FADER_1, CC_FADER_2, CC_FADER_3 = 5, 6, 7
CC_PLAY, CC_RECORD = 116, 118
CC_MODE_REPORT = 30

SCREEN_STATIONARY = 0x35
GAMMA = 2.2

BLACK = (0.0, 0.0, 0.0)


class LaunchControlXL3:
    """Survives unplug/replug: connect() can be retried, failed I/O drops the connection."""

    def __init__(self, port_hint=None):
        self.hint = port_hint
        self.out = self.inp = None
        self._cache = {}

    @property
    def connected(self):
        return self.out is not None

    @staticmethod
    def _find(names, hint):
        if hint:
            return next((n for n in names if hint.lower() in n.lower()), None)
        # Prefer the DAW port of the XL 3
        for n in names:
            low = n.lower()
            if "daw" in low and ("lcxl3" in low or "launch control xl" in low):
                return n
        # Windows class driver: the DAW port is the device's second port, e.g. "MIDIOUT2 (LCXL3 1 MIDI)"
        for n in names:
            low = n.lower()
            if ("midiout2" in low or "midiin2" in low) and ("lcxl3" in low or "launch control xl" in low):
                return n
        return next((n for n in names if "daw" in n.lower() and "launch" in n.lower()), None)

    def connect(self):
        """Open the DAW ports and enter DAW mode. Returns False if the device isn't there."""
        try:
            out_name = self._find(mido.get_output_names(), self.hint)
            in_name = self._find(mido.get_input_names(), self.hint)
            if out_name is None:
                return False
            self.out = mido.open_output(out_name)
            self.inp = mido.open_input(in_name) if in_name else None
        except Exception:
            self.disconnect()
            return False
        self._cache.clear()
        log(f"Connected. Output: {out_name}  Input: {in_name or '(none - controls disabled)'}")
        self.daw_mode(True)
        return self.connected

    def disconnect(self):
        for port in (self.out, self.inp):
            try:
                if port:
                    port.close()
            except Exception:
                pass
        self.out = self.inp = None

    def present(self):
        """True while the device's DAW port is still listed by the OS."""
        try:
            return self._find(mido.get_output_names(), self.hint) is not None
        except Exception:
            return False

    def _sysex(self, payload):
        if not self.out:
            return
        try:
            self.out.send(mido.Message("sysex", data=HEADER + payload))
        except Exception:
            self.disconnect()

    def daw_mode(self, on):
        self._sysex([0x02, 0x7F if on else 0x00])

    def set_rgb(self, cc, rgb):
        """rgb: floats 0..1. Only sends if the colour changed."""
        values = tuple(max(0, min(127, round(127 * (max(0.0, min(1.0, v)) ** GAMMA)))) for v in rgb)
        if self._cache.get(cc) == values:
            return
        self._cache[cc] = values
        self._sysex([0x01, 0x53, cc, *values])

    def screen(self, title, name, value):
        """Best effort: 3-line layout (arrangement 2) on the stationary display."""
        def ascii7(text):
            return [ord(ch) for ch in text if 32 <= ord(ch) <= 126][:16]
        self._sysex([0x04, SCREEN_STATIONARY, 0x60 | 0x02])
        for field, text in enumerate((title, name, value)):
            self._sysex([0x06, SCREEN_STATIONARY, field] + ascii7(text))
        self._sysex([0x04, SCREEN_STATIONARY, 0x7F])

    def pending(self):
        if not self.inp:
            return []
        try:
            return list(self.inp.iter_pending())
        except Exception:
            self.disconnect()
            return []

    def all_off(self):
        self._cache.clear()
        for row in GRID_CC:
            for cc in row:
                self.set_rgb(cc, BLACK)

    def close(self):
        self.disconnect()


# ----------------------------------------------------------------- helpers --

LOG_PATH = None


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


def scale(rgb, k):
    return tuple(c * k for c in rgb)


def lerp(a, b, k):
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def blank():
    return [[0.0] * COLS for _ in range(ROWS)]


def clamp01(x):
    return max(0.0, min(1.0, x))


def smoothstep(lo, hi, x):
    k = clamp01((x - lo) / (hi - lo))
    return k * k * (3 - 2 * k)


def blob(grid, x, y, level, spread=0.6):
    """Soft round light centred at (row y, col x), max-blended into grid."""
    for r in range(ROWS):
        for c in range(COLS):
            d2 = (c - x) ** 2 + (r - y) ** 2
            grid[r][c] = max(grid[r][c], level * math.exp(-d2 / spread))


def _hash(ix, iy, seed):
    n = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFFFF) / 0xFFFFFF


def noise2(x, y, seed=0):
    """Smooth 2D value noise, 0..1."""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    ux, uy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a = _hash(ix, iy, seed)
    b = _hash(ix + 1, iy, seed)
    c = _hash(ix, iy + 1, seed)
    d = _hash(ix + 1, iy + 1, seed)
    return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy


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
]


# ----------------------------------------------------------------- effects --
# Row 0 = top encoder row ... row 4 = lower button row.
# frame() returns a ROWS x COLS grid of brightness values 0..1.

CENTER_R, CENTER_C = (ROWS - 1) / 2, (COLS - 1) / 2


class Effect:
    name = "effect"

    def frame(self, t, dt):
        raise NotImplementedError


class Breathe(Effect):
    name = "breathe"
    PERIOD = 6.0

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                delay = math.hypot((r - CENTER_R) * 1.3, c - CENTER_C) * 0.22
                b = 0.5 - 0.5 * math.cos(2 * math.pi * (t - delay) / self.PERIOD)
                grid[r][c] = 0.15 + 0.75 * b ** 1.5
        return grid


class Tide(Effect):
    name = "tide"

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            x = CENTER_C + 4.2 * math.sin(t * 0.35) + 0.6 * math.sin(t * 0.5 + r * 0.4)
            for c in range(COLS):
                grid[r][c] = 0.08 + 0.92 * math.exp(-((c - x) ** 2) / (2 * 1.6 ** 2))
        return grid


class Aurora(Effect):
    name = "aurora"

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                n = (noise2(c * 0.45 + t * 0.25, r * 0.25 - t * 0.08) * 0.65
                     + noise2(c * 0.9 - t * 0.4, r * 0.6 + t * 0.15, 1) * 0.35)
                grid[r][c] = smoothstep(0.25, 0.85, n)
        return grid


class Ember(Effect):
    name = "ember"
    STEP = 1 / 10

    def __init__(self):
        self.heat = blank()
        self.acc = 0.0
        self.t = 0.0

    def frame(self, t, dt):
        self.acc += dt
        while self.acc >= self.STEP:
            self.acc -= self.STEP
            self.t += self.STEP
            h = self.heat
            for c in range(COLS):
                glow = 0.3 + 0.5 * noise2(c * 0.7, self.t * 0.6, 2)
                if random.random() < 0.02:
                    glow = 0.95  # occasional flare
                h[ROWS - 1][c] = glow
            for r in range(ROWS - 1):
                below = h[r + 1]
                for c in range(COLS):
                    avg = (below[c] * 2 + below[max(c - 1, 0)] + below[min(c + 1, COLS - 1)]) / 4
                    h[r][c] = max(0.0, avg - random.uniform(0.1, 0.2))
        return [row[:] for row in self.heat]


class Constellation(Effect):
    name = "constellation"
    MAX_STARS = 7

    def __init__(self):
        self.stars = []  # [row, col, start, duration, peak]

    def frame(self, t, dt):
        self.stars = [s for s in self.stars if t - s[2] < s[3]]
        if len(self.stars) < self.MAX_STARS and random.random() < dt * 1.2:
            taken = {(s[0], s[1]) for s in self.stars}
            free = [(r, c) for r in range(ROWS) for c in range(COLS) if (r, c) not in taken]
            r, c = random.choice(free)
            self.stars.append([r, c, t, random.uniform(2.0, 5.0), random.uniform(0.6, 1.0)])
        grid = blank()
        for r, c, start, duration, peak in self.stars:
            grid[r][c] = peak * math.sin(math.pi * (t - start) / duration) ** 2
        return grid


class Comet(Effect):
    name = "comet"
    TRAIL = 14

    def frame(self, t, dt):
        grid = blank()
        for k in range(self.TRAIL):
            tk = t - k * 0.08
            x = CENTER_C + 3.6 * math.sin(tk * 0.7)
            y = CENTER_R + 2.1 * math.sin(tk * 1.1 + 1.0)
            blob(grid, x, y, math.exp(-k * 0.22))
        return grid


class Ripple(Effect):
    name = "ripple"

    def __init__(self):
        self.rings = []  # [row, col, start]
        self.next_at = 0.0

    def frame(self, t, dt):
        if t >= self.next_at or t < self.next_at - 10:
            self.rings.append([random.uniform(0, ROWS - 1), random.uniform(0, COLS - 1), t])
            self.next_at = t + random.uniform(2.5, 4.5)
        self.rings = [ring for ring in self.rings if 0 <= t - ring[2] < 6]
        grid = blank()
        for r0, c0, start in self.rings:
            age = t - start
            radius = age * 2.2
            fade = math.exp(-age * 0.6)
            for r in range(ROWS):
                for c in range(COLS):
                    d = math.hypot(r - r0, c - c0)
                    grid[r][c] = max(grid[r][c], fade * math.exp(-((d - radius) ** 2) / (2 * 0.55 ** 2)))
        return grid


class Lighthouse(Effect):
    name = "lighthouse"

    def frame(self, t, dt):
        beam = t * 1.2
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                dy, dx = (r - CENTER_R) * 1.3, c - CENTER_C
                behind = (beam - math.atan2(dy, dx)) % (2 * math.pi)  # radians since the beam passed
                ahead = 2 * math.pi - behind
                i = max(math.exp(-behind / 0.9), math.exp(-ahead / 0.12))
                grid[r][c] = i * (0.55 + 0.45 * min(1.0, math.hypot(dy, dx) / 3))
        return grid


class Drift(Effect):
    name = "drift"

    def __init__(self):
        self.drops = []  # [col, y, speed]

    def frame(self, t, dt):
        if random.random() < dt * 2.5:
            self.drops.append([random.randrange(COLS), -1.0, random.uniform(1.5, 3.0)])
        for drop in self.drops:
            drop[1] += drop[2] * dt
        self.drops = [d for d in self.drops if d[1] < ROWS + 4]
        grid = blank()
        for c, y, _ in self.drops:
            for r in range(ROWS):
                behind = y - r
                i = math.exp(-behind / 1.2) if behind >= 0 else math.exp(-behind * behind / 0.15)
                grid[r][c] = max(grid[r][c], i)
        return grid


class Horizon(Effect):
    name = "horizon"
    DAY = 30.0

    def frame(self, t, dt):
        rise = 0.5 - 0.5 * math.cos(2 * math.pi * t / self.DAY)
        y = ROWS - 0.5 - (ROWS + 1) * rise          # horizon line, rows below it glow
        x = CENTER_C + 3 * math.sin(2 * math.pi * t / (2 * self.DAY))
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                shimmer = 0.85 + 0.15 * noise2(c * 0.6 + t * 0.5, r * 0.6, 3)
                glow = smoothstep(-1.5, 2.0, r - y) * 0.7 * shimmer
                sun = math.exp(-((c - x) ** 2 + 1.5 * (r - y) ** 2) / 1.5)
                grid[r][c] = max(glow, sun)
        return grid


class Heartbeat(Effect):
    name = "heartbeat"
    PERIOD = 60 / 54  # 54 bpm

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                d = math.hypot((r - CENTER_R) * 1.3, c - CENTER_C)
                p = (t - d * 0.05) % self.PERIOD
                beat = math.exp(-((p - 0.12) / 0.07) ** 2) + 0.6 * math.exp(-((p - 0.38) / 0.08) ** 2)
                grid[r][c] = 0.08 + 0.85 * beat * (1 - d / 12)
        return grid


class Interference(Effect):
    name = "interference"

    def frame(self, t, dt):
        sources = [(CENTER_R + 2.5 * math.sin(t * 0.21), CENTER_C + 4.5 * math.cos(t * 0.17)),
                   (CENTER_R + 2.5 * math.cos(t * 0.13), CENTER_C + 4.5 * math.sin(t * 0.19 + 2))]
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                v = sum(math.sin(math.hypot(r - sr, c - sc) * 1.4 - t * 2) for sr, sc in sources)
                grid[r][c] = smoothstep(0.45, 0.95, (v + 2) / 4)
        return grid


class Fireflies(Effect):
    name = "fireflies"
    COUNT = 4

    def frame(self, t, dt):
        grid = blank()
        for k in range(self.COUNT):
            x = noise2(t * 0.15, k * 10, 5) * (COLS + 1) - 1
            y = noise2(t * 0.15, k * 10, 9) * (ROWS + 1) - 1
            glow = 0.3 + 0.7 * smoothstep(0.3, 0.7, noise2(t * 0.5, k * 7, 6))
            blob(grid, x, y, glow, spread=0.5)
        return grid


class Candle(Effect):
    name = "candle"

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                base = 0.25 + 0.3 * r / (ROWS - 1)  # warmer toward the bottom
                flicker = (0.25 * noise2(c * 0.5 + t * 1.2, r * 0.5, 7)
                           + 0.1 * noise2(t * 3.5, r * COLS + c, 8))
                grid[r][c] = base + flicker
        return grid


class Orbit(Effect):
    name = "orbit"
    BODIES = [(3.4, 0.6, 1.0), (1.7, -1.1, 0.75)]  # radius, angular speed, brightness

    def frame(self, t, dt):
        grid = blank()
        for radius, omega, level in self.BODIES:
            for k in range(7):
                a = (t - k * 0.07) * omega
                blob(grid, CENTER_C + radius * math.cos(a), CENTER_R + radius * 0.55 * math.sin(a),
                     level * math.exp(-k * 0.35))
        return grid


class Lattice(Effect):
    name = "lattice"

    def frame(self, t, dt):
        grid = blank()
        for r in range(ROWS):
            for c in range(COLS):
                v = (0.5 + 0.5 * math.sin(c * 0.9 + t * 0.8)) * (0.5 + 0.5 * math.sin(r * 1.2 - t * 0.6))
                grid[r][c] = smoothstep(0.1, 0.8, v)
        return grid


EFFECTS = [Breathe, Tide, Aurora, Ember, Constellation, Comet, Ripple, Lighthouse,
           Drift, Horizon, Heartbeat, Interference, Fireflies, Candle, Orbit, Lattice]
BANK = COLS  # effects per bank of lower buttons
XFADE = 1.5  # seconds to crossfade between effects


# ------------------------------------------------------------------ config --
# Everything the control panel can change, saved as JSON so it survives restarts.

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "lcxl3_fx_ui.html")

DEFAULTS = {
    "effect": EFFECTS[0].name,
    "theme": THEMES[0].name,
    "speed": 1.0,
    "brightness": 0.7,
    "loop_seconds": 0,         # 0 = stay on one effect
    "shuffle": False,
    "enabled": [cls.name for cls in EFFECTS],   # effects in the auto-loop / Play-Record cycle
    "power": True,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
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


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(round(255 * clamp01(v)) for v in rgb)


# -------------------------------------------------------------- control ui --
# Tiny local web server (stdlib only): http://127.0.0.1:8765
# HTTP threads only queue commands and read the last published snapshot;
# all device and effect state stays on the main thread.

class ControlServer:
    def __init__(self, port):
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
                        with open(UI_FILE, "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send(404, b"lcxl3_fx_ui.html not found next to lcxl3_fx.py", "text/plain")
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


# -------------------------------------------------------------------- main --

def find_by_name(items, query, kind):
    matches = [i for i, item in enumerate(items) if query.lower() in item.name]
    if not matches:
        print(f"Unknown {kind} '{query}'. Options: {', '.join(item.name for item in items)}")
        sys.exit(1)
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description="Ambient LED effects for the Novation Launch Control XL 3")
    parser.add_argument("--effect", help="effect to start with (see --list)")
    parser.add_argument("--theme", help="colour theme to start with (see --list)")
    parser.add_argument("--auto", type=float, metavar="MIN", help="move to the next effect every MIN minutes")
    parser.add_argument("--fps", type=float, default=30.0, help="frames per second (default 30)")
    parser.add_argument("--port", help="part of the MIDI port name to use (default: auto-detect DAW port)")
    parser.add_argument("--no-screen", action="store_true", help="don't write to the OLED screen")
    parser.add_argument("--debug", action="store_true", help="print incoming MIDI messages")
    parser.add_argument("--list", action="store_true", help="list effects and themes and exit")
    parser.add_argument("--log", metavar="PATH", help="also append status messages to this file")
    parser.add_argument("--config", default=os.path.join(HERE, "lcxl3_fx.json"),
                        help="settings file (default: lcxl3_fx.json next to the script)")
    parser.add_argument("--ui-port", type=int, default=8765, help="control panel port (default 8765, 0 = off)")
    args = parser.parse_args()
    global LOG_PATH
    LOG_PATH = args.log

    effects = [cls() for cls in EFFECTS]
    if args.list:
        print("Effects:")
        for i, e in enumerate(effects, 1):
            print(f"  {i:2d}. {e.name}")
        print("Themes:")
        for theme in THEMES:
            print(f"  - {theme.name}")
        return

    cfg = load_config(args.config)
    if args.auto is not None:
        cfg["loop_seconds"] = args.auto * 60
    names = [e.name for e in effects]
    index = find_by_name(effects, args.effect, "effect") if args.effect else (
        names.index(cfg["effect"]) if cfg["effect"] in names else 0)
    theme_names = [th.name for th in THEMES]
    theme_index = find_by_name(THEMES, args.theme, "theme") if args.theme else (
        theme_names.index(cfg["theme"]) if cfg["theme"] in theme_names else 0)
    speed, brightness = float(cfg["speed"]), float(cfg["brightness"])

    ui = None
    if args.ui_port:
        try:
            ui = ControlServer(args.ui_port)
            log(f"Control panel: http://127.0.0.1:{args.ui_port}")
        except OSError as e:
            log(f"Control panel disabled - port {args.ui_port} unavailable ({e})")

    dev = LaunchControlXL3(args.port)
    if dev.connect():
        time.sleep(0.2)
    else:
        log("Waiting for the Launch Control XL 3 DAW port... MIDI outputs right now:")
        for name in mido.get_output_names():
            log(f"  - {name}")
        log("(if it's plugged in but not listed above, pass --port \"<part of the name>\")")

    pulses = [0.0] * COLS
    current = [[BLACK] * COLS for _ in range(ROWS)]  # eased colour actually shown
    t = 0.0
    frame_time = 1.0 / args.fps
    prev_index, switched_at = None, time.perf_counter()
    loop_from = switched_at          # auto-loop timer start
    power_k = 1.0 if cfg["power"] else 0.0
    dirty_at = None                  # when settings last changed (saved shortly after)

    def changed():
        nonlocal dirty_at
        dirty_at = time.perf_counter()

    def save():
        cfg.update(effect=effects[index].name, theme=THEMES[theme_index].name,
                   speed=round(speed, 3), brightness=round(brightness, 3))
        save_config(args.config, cfg)

    def announce():
        name, theme = effects[index].name, THEMES[theme_index].name
        bank = index // BANK + 1
        log(f"Effect {index + 1}: {name}  bank {bank}  theme: {theme}  (speed x{speed:.1f})")
        if not args.no_screen:
            dev.screen(f"LED FX  bank {bank}", f"{index + 1} {name}", f"{theme} x{speed:.1f}")

    def select(new):
        nonlocal index, prev_index, switched_at, loop_from
        loop_from = time.perf_counter()
        if new == index:
            return
        prev_index, index, switched_at = index, new, loop_from
        announce()
        changed()

    def playlist():
        order = [i for i, e in enumerate(effects) if e.name in cfg["enabled"]]
        return order or list(range(len(effects)))

    def step(direction):
        """Next/previous effect among the enabled ones."""
        order = playlist()
        later = [i for i in order if (i - index) * direction > 0]
        if later:
            select(min(later) if direction > 0 else max(later))
        else:
            select(order[0] if direction > 0 else order[-1])

    def advance():
        others = [i for i in playlist() if i != index]
        if cfg["shuffle"] and others:
            select(random.choice(others))
        else:
            step(+1)

    def apply(cmd):
        """A command from the control panel."""
        nonlocal speed, brightness, theme_index, loop_from
        if "select" in cmd:
            select(max(0, min(len(effects) - 1, int(cmd["select"]))))
        if "step" in cmd:
            step(1 if int(cmd["step"]) > 0 else -1)
        if "theme" in cmd and cmd["theme"] in theme_names:
            theme_index = theme_names.index(cmd["theme"])
            announce()
        if "speed" in cmd:
            speed = max(0.2, min(3.0, float(cmd["speed"])))
        if "brightness" in cmd:
            brightness = max(0.05, min(1.0, float(cmd["brightness"])))
        if "loop_seconds" in cmd:
            cfg["loop_seconds"] = max(0.0, float(cmd["loop_seconds"]))
            loop_from = time.perf_counter()
        if "shuffle" in cmd:
            cfg["shuffle"] = bool(cmd["shuffle"])
        if "enabled" in cmd and isinstance(cmd["enabled"], list):
            cfg["enabled"] = [n for n in names if n in cmd["enabled"]]
        if "power" in cmd:
            cfg["power"] = bool(cmd["power"])
            log("LEDs on" if cfg["power"] else "LEDs off (standby)")
        if "pulse" in cmd:
            pulses[max(0, min(COLS - 1, int(cmd["pulse"])))] = 1.0
        changed()

    def publish(now):
        loop = cfg["loop_seconds"]
        ui.publish({
            "connected": dev.connected,
            "effects": names,
            "themes": [{"name": th.name, "colors": [hex_color(c) for _, c in th.stops[1:]]} for th in THEMES],
            "index": index,
            "theme": THEMES[theme_index].name,
            "speed": speed,
            "brightness": brightness,
            "loop_seconds": loop,
            "shuffle": cfg["shuffle"],
            "enabled": cfg["enabled"],
            "power": cfg["power"],
            "next_in": max(0.0, loop - (now - loop_from)) if loop > 0 else None,
            "leds": [hex_color(rgb) for row in current for rgb in row],
        })

    def compose(dt):
        """Current effect's frame, crossfaded from the previous one right after a switch."""
        grid = effects[index].frame(t, dt * speed)
        k = smoothstep(0.0, XFADE, time.perf_counter() - switched_at)
        if prev_index is None or k >= 1:
            return grid
        old = effects[prev_index].frame(t, dt * speed)
        return [[o + (n - o) * k for o, n in zip(orow, nrow)] for orow, nrow in zip(old, grid)]

    def render(grid, fade, dt):
        """Theme-colour the grid and ease every LED toward it (gives crossfades for free)."""
        theme = THEMES[theme_index]
        ease = 1 - math.exp(-dt * 8)
        for r in range(ROWS):
            for c in range(COLS):
                i = 1 - (1 - grid[r][c]) * (1 - pulses[c])  # screen-blend the column swell
                target = scale(theme.color(i), brightness * fade)
                rgb = lerp(current[r][c], target, ease)
                current[r][c] = rgb
                dev.set_rgb(GRID_CC[r][c], rgb if max(rgb) > 0.03 else BLACK)

    announce()
    start = last = checked_at = retry_at = published_at = time.perf_counter()
    was_connected = dev.connected
    try:
        while True:
            now = time.perf_counter()
            dt = now - last
            last = now

            for cmd in ui.pending() if ui else []:
                try:
                    apply(cmd)
                except (TypeError, ValueError, KeyError):
                    pass
            if dirty_at and now - dirty_at > 1.0:
                save()
                dirty_at = None
            if ui and now - published_at > 0.1:
                published_at = now
                publish(now)

            # Hot-plug: notice the device going away, keep retrying until it's back
            if dev.connected and now - checked_at > 2.0:
                checked_at = now
                if not dev.present():
                    dev.disconnect()
            if not dev.connected:
                if was_connected:
                    log("Device disconnected - waiting for it to come back...")
                    was_connected = False
                if now - retry_at > 1.0:
                    retry_at = now
                    if dev.connect():
                        time.sleep(0.2)
                        log("Reconnected.")
                        was_connected = True
                        current = [[BLACK] * COLS for _ in range(ROWS)]
                        announce()
                        start = last = checked_at = time.perf_counter()
                        continue
                time.sleep(0.1)
                continue

            for msg in dev.pending():
                if msg.type == "sysex":
                    continue  # the device echoes SysEx back
                if args.debug:
                    print("in:", msg)
                if msg.type != "control_change":
                    continue
                if msg.channel == CH_ANALOG and msg.control == CC_FADER_1:
                    speed = 0.2 + msg.value / 127 * 2.8
                    changed()
                elif msg.channel == CH_ANALOG and msg.control == CC_FADER_2:
                    brightness = 0.05 + msg.value / 127 * 0.95
                    changed()
                elif msg.channel == CH_ANALOG and msg.control == CC_FADER_3:
                    zone = min(len(THEMES) - 1, msg.value * len(THEMES) // 128)
                    if zone != theme_index:
                        theme_index = zone
                        announce()
                        changed()
                elif msg.channel == CH_BUTTONS and msg.value > 0:
                    if 37 <= msg.control <= 44:
                        pulses[msg.control - 37] = 1.0
                    elif 45 <= msg.control <= 52:
                        slot = msg.control - 45
                        bank = index // BANK
                        if index == bank * BANK + slot:  # same button again: flip bank
                            bank = (bank + 1) % math.ceil(len(effects) / BANK)
                        select(min(bank * BANK + slot, len(effects) - 1))
                    elif msg.control in (CC_PLAY, CC_RECORD):
                        step(1 if msg.control == CC_PLAY else -1)
                elif msg.channel == CH_FEATURES and msg.control == CC_MODE_REPORT and msg.value not in (1, 2):
                    log("Device switched to a Custom Mode - select DAW Mixer/Control to see the effects.")

            if cfg["loop_seconds"] > 0 and now - loop_from > cfg["loop_seconds"]:
                advance()

            t += dt * speed
            power_k += ((1.0 if cfg["power"] else 0.0) - power_k) * (1 - math.exp(-dt * 3))
            fade_in = smoothstep(0.0, 1.5, now - start)
            render(compose(dt), fade_in * power_k, dt)
            pulses = [p * math.exp(-dt / 0.35) for p in pulses]

            time.sleep(max(0.0, frame_time - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        if dirty_at:
            save()
        if dev.connected:
            try:
                fade_start = last = time.perf_counter()
                while (now := time.perf_counter()) - fade_start < 1.0:
                    dt, last = now - last, now
                    t += dt * speed
                    render(compose(dt), (1 - smoothstep(0.0, 1.0, now - fade_start)) * power_k, dt)
                    time.sleep(frame_time)
            except KeyboardInterrupt:
                pass
            dev.all_off()
            dev.daw_mode(False)
        dev.close()
        log("LEDs off, device back in standalone mode. Bye!")


if __name__ == "__main__":
    main()
