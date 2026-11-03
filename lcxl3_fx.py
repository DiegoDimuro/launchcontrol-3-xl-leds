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
Side buttons (same RGB SysEx): Page up/down 106/107, Track left/right 103/102,
Record 118, Play 116, Solo/Arm 65, Mute/Select 66, and the small button at 104.

The XL 3 runs two independent layers, each with its own effect, theme, speed, brightness,
loop and on/off (and its own control panel tab):
  Encoders  (rows 0-2): constellation, drift, showers, haze, breath, lighthouse, tides,
            fireflies, currents, veil - idle displays made for the 3 x 8 encoder grid:
            sparse, slow, soft fades.
  Mute/Solo (rows 3-4): the Launchkey's strip effects (strips.py): scanner, orbits, crossing,
            wings - moving lights with a fading tail.
Effects produce a brightness value per LED; the layer's theme turns that into
colour (shadow -> mid -> highlight).
Side buttons stay lit white, Play stays green, and Record blinks red like a studio REC light
(1 s, not the effect speed). That blink is their only movement.
Themes:   amber, ice, mono, indigo, moss, dusk, sea glass, slate, sand, tide, aurora

Controls (the encoder layer; the mute/solo layer is set from the control panel):
  Fader 1              speed
  Fader 2              brightness
  Fader 3              theme (one zone per theme, bottom to top)
  Play / Record        next / previous effect
  Ctrl+C               quit (fade out, device back to normal mode)

Setup (once):
  python3 -m venv .venv
  source .venv/bin/activate           (Windows: .venv\\Scripts\\activate)
  pip install --upgrade pip
  pip install mido python-rtmidi

Run (each time):
  source .venv/bin/activate           (Windows: .venv\\Scripts\\activate)
  python lcxl3_fx.py                 (or --list, --effect snow, --theme ice, --auto 5, --debug, --log fx.log)
  deactivate                         (when done)

Unplugging is fine: the script waits for the device and picks up where it left off.
Windows autostart: the "LCXL3-FX" scheduled task runs it with pythonw.exe at logon.
Control panel: http://127.0.0.1:8765 (lcxl3_fx_ui.html) - effects, loop, theme, speed,
brightness, on/off per layer. Settings are kept in lcxl3_fx.json.
A Launchkey MK4, if plugged in, gets its own effects and panel tab (launchkey_fx.py).
"""

import argparse
import math
import os
import random
import sys
import time

import mido

import fxcore
from fxcore import (BLACK, GAMMA, THEMES, ControlServer, clamp01, envelope, hex_color, lerp,
                    load_config, log, rec_blink, save_config, scale, smoothstep, themes_for_ui)
from launchkey_fx import LaunchkeyRig
from strips import COLS as STRIP_COLS, ROWS as STRIP_ROWS, StripEngine

# ---------------------------------------------------------------- protocol --

HEADER = [0x00, 0x20, 0x29, 0x02, 0x15]
ROWS, COLS = 5, 8
GRID_CC = [list(range(13 + r * 8, 21 + r * 8)) for r in range(ROWS)]  # 13..52
ENC_ROWS = 3                       # rows 0-2: the encoder layer
STRIP_CC = GRID_CC[ENC_ROWS:]      # rows 3-4: the mute/solo layer, a STRIP_ROWS x COLS strip
assert len(STRIP_CC) == STRIP_ROWS and COLS == STRIP_COLS

CH_BUTTONS = 0        # MIDI channel 1
CH_FEATURES = 6       # MIDI channel 7
CH_ANALOG = 15        # MIDI channel 16
CC_FADER_1, CC_FADER_2, CC_FADER_3 = 5, 6, 7
CC_PLAY, CC_RECORD = 116, 118
CC_MODE_REPORT = 30

# Side buttons with LEDs
PAGE_UP, PAGE_DOWN = 106, 107
TRACK_LEFT, TRACK_RIGHT = 103, 102
SOLO_ARM, MUTE_SELECT, CC_104 = 65, 66, 104
ARROWS = [PAGE_UP, TRACK_RIGHT, PAGE_DOWN, TRACK_LEFT]   # clockwise, for the chase
SIDE_BUTTONS = ARROWS + [CC_RECORD, CC_PLAY, SOLO_ARM, MUTE_SELECT, CC_104]
SIDE_LABELS = {
    PAGE_UP: "Page up", PAGE_DOWN: "Page down", TRACK_LEFT: "Track <", TRACK_RIGHT: "Track >",
    CC_RECORD: "Record", CC_PLAY: "Play", SOLO_ARM: "Solo/Arm", MUTE_SELECT: "Mute/Select", CC_104: "Button 104",
}
RED, GREEN = (1.0, 0.04, 0.02), (0.05, 1.0, 0.12)
WHITE = (1.0, 1.0, 1.0)

SCREEN_STATIONARY = 0x35


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
        # Last resort for unknown port names, but never a Launchkey's DAW port
        return next((n for n in names if "daw" in n.lower() and "launch" in n.lower()
                     and "launchkey" not in n.lower()), None)

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

    def screen_off(self):
        """Blank the stationary display (all fields empty) to spare the OLED."""
        self.screen("", "", "")

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
        for cc in [cc for row in GRID_CC for cc in row] + SIDE_BUTTONS:
            self.set_rgb(cc, BLACK)

    def close(self):
        self.disconnect()


def blank():
    return [[0.0] * COLS for _ in range(ENC_ROWS)]


# ----------------------------------------------------------------- effects --
# Encoder layer: row 0 = top encoder row ... row 2 = bottom encoder row (3 x 8).
# frame() returns an ENC_ROWS x COLS grid of brightness values 0..1.
# Tuned for 3 rows: fewer stars, slower drops with shorter tails so a drop stays readable.
# The mute/solo rows below run their own strip effects (strips.py).

class Effect:
    name = "effect"

    def frame(self, t, dt):
        raise NotImplementedError


def draw_streak(grid, x, y, peak=1.0, tail=0.8, slope=0.0, up=False):
    """A soft drop head at row y with a fading tail behind it, max-blended into grid.
    x may be fractional (split across two columns); slope = columns per row along the tail."""
    for r in range(ENC_ROWS):
        behind = (r - y) if up else (y - r)
        i = peak * (math.exp(-behind / tail) if behind >= 0 else math.exp(-behind * behind / 0.15))
        if i < 0.01:
            continue
        cx = x - behind * slope
        c0 = math.floor(cx)
        f = cx - c0
        for c, w in ((c0, 1 - f), (c0 + 1, f)):
            if 0 <= c < COLS and w > 0:
                grid[r][c] = max(grid[r][c], i * w)


class Constellation(Effect):
    name = "constellation"
    MAX_STARS = 4

    def __init__(self):
        self.stars = []  # [row, col, start, duration, peak]

    def frame(self, t, dt):
        self.stars = [s for s in self.stars if t - s[2] < s[3]]
        if len(self.stars) < self.MAX_STARS and random.random() < dt * 0.8:
            taken = {(s[0], s[1]) for s in self.stars}
            free = [(r, c) for r in range(ENC_ROWS) for c in range(COLS) if (r, c) not in taken]
            r, c = random.choice(free)
            self.stars.append([r, c, t, random.uniform(2.0, 5.0), random.uniform(0.6, 1.0)])
        grid = blank()
        for r, c, start, duration, peak in self.stars:
            grid[r][c] = peak * envelope(t - start, duration)
        return grid


class Drift(Effect):
    name = "drift"

    def __init__(self):
        self.drops = []  # [col, y, speed]

    def frame(self, t, dt):
        if random.random() < dt * 1.6:
            self.drops.append([random.randrange(COLS), -1.0, random.uniform(1.0, 2.0)])
        for drop in self.drops:
            drop[1] += drop[2] * dt
        self.drops = [d for d in self.drops if d[1] < ENC_ROWS + 3]
        grid = blank()
        for c, y, _ in self.drops:
            draw_streak(grid, c, y)
        return grid


class Showers(Effect):
    """Drift whose density wanders: long calm spells with a few drops, then gentle downpours."""
    name = "showers"

    def __init__(self):
        self.drops = []  # [col, y, speed]

    def frame(self, t, dt):
        weather = 0.5 + 0.3 * math.sin(t * 0.031) + 0.2 * math.sin(t * 0.017 + 2.0)  # 0..1, minutes-long
        rain = smoothstep(0.45, 0.9, weather)
        if random.random() < dt * (0.25 + 3.2 * rain):
            self.drops.append([random.randrange(COLS), -1.0, random.uniform(1.0, 2.0) + 0.7 * rain])
        for d in self.drops:
            d[1] += d[2] * dt
        self.drops = [d for d in self.drops if d[1] < ENC_ROWS + 3]
        grid = blank()
        for c, y, _ in self.drops:
            draw_streak(grid, c, y)
        return grid


class Haze(Effect):
    """Constellation over a very faint glow that drifts independently in every cell, so it's never fully black."""
    name = "haze"

    def __init__(self):
        self.sky = Constellation()
        self.waves = [[(random.uniform(0.08, 0.25), random.uniform(0, 2 * math.pi)) for _ in range(COLS)]
                      for _ in range(ENC_ROWS)]

    def frame(self, t, dt):
        grid = self.sky.frame(t, dt)
        for r in range(ENC_ROWS):
            for c in range(COLS):
                freq, phase = self.waves[r][c]
                glow = 0.08 + 0.16 * (0.5 + 0.5 * math.sin(t * freq + phase))
                grid[r][c] = max(grid[r][c], glow)
        return grid


def splat(grid, r, c, peak):
    """Light a fractional (r, c) position, spread over the up to 4 nearest cells, max-blended.
    Weights are softened (w ** 0.6) so a light between two cells doesn't dim much."""
    r0, c0 = math.floor(r), math.floor(c)
    fr, fc = r - r0, c - c0
    for rr, wr in ((r0, 1 - fr), (r0 + 1, fr)):
        for cc, wc in ((c0, 1 - fc), (c0 + 1, fc)):
            w = wr * wc
            if w > 0 and 0 <= rr < ENC_ROWS and 0 <= cc < COLS:
                grid[rr][cc] = max(grid[rr][cc], peak * w ** 0.6)


def wrap(d, period):
    """Signed distance d folded into -period/2 .. period/2."""
    return (d + period / 2) % period - period / 2


class Breath(Effect):
    """One column at a time breathes in: its rings light bottom to top, hold, then fade top-down.
    A new column starts while the last one is still exhaling."""
    name = "breath"
    DURATION = 7.0

    def __init__(self):
        self.breaths = []     # [col, start]
        self.next_at = 0.0

    def frame(self, t, dt):
        self.breaths = [b for b in self.breaths if t - b[1] < self.DURATION]
        if t >= self.next_at:
            recent = {b[0] for b in self.breaths}
            self.breaths.append([random.choice([c for c in range(COLS) if c not in recent]), t])
            self.next_at = t + random.uniform(3.5, 5.5)
        grid = blank()
        for c, start in self.breaths:
            p = (t - start) / self.DURATION
            for k in range(ENC_ROWS):                  # k = 0 bottom ring ... 2 top ring
                on_at = 0.05 + 0.12 * k                # inhale: bottom first
                off_at = 0.58 + 0.12 * (ENC_ROWS - 1 - k)   # exhale: top first
                level = smoothstep(on_at, on_at + 0.16, p) * (1 - smoothstep(off_at, off_at + 0.16, p))
                r = ENC_ROWS - 1 - k
                grid[r][c] = max(grid[r][c], (0.95 - 0.1 * k) * level)
        return grid


class Lighthouse(Effect):
    """A dim beam turns along the middle row, about once every 20 s, with a faint spill above and
    below. It wraps from the right edge back to the left, like a light going round."""
    name = "lighthouse"
    PERIOD = 20.0

    def frame(self, t, dt):
        pos = (t / self.PERIOD % 1.0) * COLS
        mid = ENC_ROWS // 2
        grid = blank()
        for c in range(COLS):
            beam = math.exp(-(wrap(c - pos, COLS) / 0.9) ** 2)
            for r in range(ENC_ROWS):
                grid[r][c] = beam * (0.85 if r == mid else 0.22)
        return grid


class Tides(Effect):
    """A slow swell runs along the columns: each column fills to a height that rises and falls,
    the bottom ring always faintly lit, the top ring only at the crests."""
    name = "tides"

    def frame(self, t, dt):
        grid = blank()
        for c in range(COLS):
            h = (0.5 + 0.3 * math.sin(t * 0.35 - c * 0.55)
                 + 0.2 * math.sin(t * 0.13 + c * 0.3 + 1.0))        # 0..1
            height = 0.4 + 2.5 * h ** 1.6                              # in rings, from the bottom
            for k in range(ENC_ROWS):
                fill = clamp01(height - k)
                grid[ENC_ROWS - 1 - k][c] = (0.3, 0.55, 0.9)[k] * fill ** 1.5   # faint low, bright crest
        return grid


class Fireflies(Effect):
    """A few lights wander the grid, gliding cell to cell with pauses in between. They come and go,
    and brighten when two of them meet."""
    name = "fireflies"
    COUNT = 3

    def __init__(self):
        self.flies = []

    def spawn(self, t):
        r, c = random.randrange(ENC_ROWS), random.randrange(COLS)
        return {"from": (r, c), "to": (r, c), "move_at": t + random.uniform(0.5, 2.0), "dur": 1.0,
                "born": t, "life": random.uniform(18.0, 35.0), "phase": random.uniform(0, 2 * math.pi)}

    def frame(self, t, dt):
        self.flies = [f for f in self.flies if t - f["born"] < f["life"]]
        if len(self.flies) < self.COUNT and random.random() < dt * 0.4:
            self.flies.append(self.spawn(t))
        spots = []
        for f in self.flies:
            p = smoothstep(0.0, 1.0, (t - f["move_at"]) / f["dur"]) if t > f["move_at"] else 0.0
            (r0, c0), (r1, c1) = f["from"], f["to"]
            r, c = r0 + (r1 - r0) * p, c0 + (c1 - c0) * p
            if p >= 1.0:          # arrived: rest a moment, then pick a neighbouring cell
                f["from"] = f["to"]
                options = [(r1 + dr, c1 + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                           if (dr or dc) and 0 <= r1 + dr < ENC_ROWS and 0 <= c1 + dc < COLS]
                f["to"] = random.choice(options)
                f["move_at"] = t + random.uniform(0.4, 2.0)
                f["dur"] = random.uniform(1.0, 2.0)
            age = t - f["born"]
            life = smoothstep(0.0, 2.0, age) * (1 - smoothstep(f["life"] - 2.5, f["life"], age))
            glow = 0.7 + 0.15 * math.sin(t * 1.3 + f["phase"])
            spots.append([r, c, glow * life])
        for i, a in enumerate(spots):
            for b in spots[i + 1:]:
                if math.hypot(a[0] - b[0], a[1] - b[1]) < 1.3:   # two fireflies meet
                    a[2] = min(1.0, a[2] + 0.25)
                    b[2] = min(1.0, b[2] + 0.25)
        grid = blank()
        for r, c, peak in spots:
            splat(grid, r, c, peak)
        return grid


class Currents(Effect):
    """A soft slanted band drifts across the grid, now and then, with a fainter one passing the other
    way. Mostly one band in view, sometimes none."""
    name = "currents"

    def frame(self, t, dt):
        grid = blank()
        for r in range(ENC_ROWS):
            for c in range(COLS):
                main = math.exp(-(wrap(c + 0.7 * r - 0.45 * t, 12.0) / 1.0) ** 2)
                back = math.exp(-(wrap(c - 0.6 * r + 0.3 * t + 5.0, 15.0) / 0.9) ** 2)
                grid[r][c] = max(0.8 * main, 0.35 * back)
        return grid


class Veil(Effect):
    """Aurora curtains: overlapping slow waves where only the crests show, so soft patches drift
    sideways and ripple from ring to ring."""
    name = "veil"

    def frame(self, t, dt):
        grid = blank()
        sway = 2.0 * math.sin(t * 0.11)
        for r in range(ENC_ROWS):
            for c in range(COLS):
                v = (math.sin(c * 0.7 + t * 0.38 + sway + r * 0.45)
                     + math.sin(c * 0.33 - t * 0.23 + r * 0.9 + 1.7)
                     + 0.5 * math.sin(t * 0.17 + r * 1.3 + c * 0.15))
                grid[r][c] = 0.85 * smoothstep(0.7, 2.0, v)
        return grid


def side_buttons(now):
    """Target colour (before brightness) of every side button: steady white, Play green, Record blinking."""
    out = {cc: WHITE for cc in SIDE_BUTTONS}
    out[CC_RECORD] = scale(RED, rec_blink(now))   # studio REC light, on the wall clock
    out[CC_PLAY] = scale(GREEN, 0.8)
    return out


EFFECTS = [Constellation, Drift, Showers, Haze, Breath, Lighthouse, Tides, Fireflies, Currents, Veil]
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
    "known": ["constellation", "drift", "showers", "haze"],   # effects the saved "enabled" list knew about
    "power": True,
    "screen_timeout": 30,      # seconds before the OLED blanks with no new info (0 = never)
    "launchkey": {},           # Launchkey MK4 settings (defaults in strips.DEFAULTS)
    "xl_strip": {},            # XL 3 mute/solo rows (defaults in STRIP_DEFAULTS)
}
STRIP_DEFAULTS = {"effect": "scanner", "theme": "ice", "brightness": 0.7}


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
    parser.add_argument("--screen-timeout", type=float, metavar="SEC",
                        help="blank the OLED after SEC seconds without new info (0 = never, default 30)")
    parser.add_argument("--debug", action="store_true", help="print incoming MIDI messages")
    parser.add_argument("--list", action="store_true", help="list effects and themes and exit")
    parser.add_argument("--log", metavar="PATH", help="also append status messages to this file")
    parser.add_argument("--config", default=os.path.join(HERE, "lcxl3_fx.json"),
                        help="settings file (default: lcxl3_fx.json next to the script)")
    parser.add_argument("--ui-port", type=int, default=8765, help="control panel port (default 8765, 0 = off)")
    parser.add_argument("--no-launchkey", action="store_true", help="leave a Launchkey MK4 alone")
    parser.add_argument("--lk-port", help="part of the Launchkey MIDI port name (default: auto-detect DAW port)")
    args = parser.parse_args()
    fxcore.LOG_PATH = args.log

    effects = [cls() for cls in EFFECTS]
    if args.list:
        print("Effects:")
        for i, e in enumerate(effects, 1):
            print(f"  {i:2d}. {e.name}")
        print("Themes:")
        for theme in THEMES:
            print(f"  - {theme.name}")
        return

    cfg = load_config(args.config, DEFAULTS)
    for key in ("launchkey", "xl_strip"):
        if not isinstance(cfg[key], dict):
            cfg[key] = {}
    if args.auto is not None:
        cfg["loop_seconds"] = args.auto * 60
    if args.screen_timeout is not None:
        cfg["screen_timeout"] = max(0.0, args.screen_timeout)
    names = [e.name for e in effects]
    added = [n for n in names if n not in cfg["known"]]   # effects new since the settings were saved
    if added:
        cfg["enabled"] = [n for n in names if n in cfg["enabled"] or n in added]
        cfg["known"] = names
        log(f"New effects added to the loop: {', '.join(added)}")
        save_config(args.config, cfg)
    index = find_by_name(effects, args.effect, "effect") if args.effect else (
        names.index(cfg["effect"]) if cfg["effect"] in names else 0)
    theme_names = [th.name for th in THEMES]
    theme_index = find_by_name(THEMES, args.theme, "theme") if args.theme else (
        theme_names.index(cfg["theme"]) if cfg["theme"] in theme_names else 0)
    speed, brightness = float(cfg["speed"]), float(cfg["brightness"])

    ui = None
    if args.ui_port:
        try:
            ui = ControlServer(args.ui_port, UI_FILE)
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

    flashes = {}                     # side button cc -> 0..1, lit up when pressed
    side = {cc: BLACK for cc in SIDE_BUTTONS}
    current = [[BLACK] * COLS for _ in range(ROWS)]  # eased colour actually shown
    t = 0.0
    frame_time = 1.0 / args.fps
    prev_index, switched_at = None, time.perf_counter()
    loop_from = switched_at          # auto-loop timer start
    power_k = 1.0 if cfg["power"] else 0.0
    dirty_at = None                  # when settings last changed (saved shortly after)
    screen_at = None                 # when the OLED was last written (None = blank / not in use)

    def changed():
        nonlocal dirty_at
        dirty_at = time.perf_counter()

    def save():
        cfg.update(effect=effects[index].name, theme=THEMES[theme_index].name,
                   speed=round(speed, 3), brightness=round(brightness, 3))
        save_config(args.config, cfg)

    def show_screen(wake=True, layer="encoders"):
        """wake=False (auto-loop): only refresh the text if the screen is already on, keep its timeout."""
        nonlocal screen_at
        if args.no_screen or (not wake and screen_at is None):
            return
        if layer == "strip":
            dev.screen("MUTE/SOLO FX", f"{strip.index + 1} {strip.name}",
                       f"{strip.theme.name} x{float(strip.cfg['speed']):.1f}")
        else:
            name, theme = effects[index].name, THEMES[theme_index].name
            dev.screen("LED FX", f"{index + 1} {name}", f"{theme} x{speed:.1f}")
        if wake:
            screen_at = time.perf_counter()

    def strip_announce(wake=True):
        log(f"Mute/solo effect {strip.index + 1}: {strip.name}  theme: {strip.theme.name}")
        show_screen(wake, layer="strip")

    def announce(wake=True):
        name, theme = effects[index].name, THEMES[theme_index].name
        log(f"Effect {index + 1}: {name}  theme: {theme}  (speed x{speed:.1f})")
        show_screen(wake)

    def select(new, wake=True):
        nonlocal index, prev_index, switched_at, loop_from
        loop_from = time.perf_counter()
        if new == index:
            return
        prev_index, index, switched_at = index, new, loop_from
        announce(wake)
        changed()

    def playlist():
        order = [i for i, e in enumerate(effects) if e.name in cfg["enabled"]]
        return order or list(range(len(effects)))

    def step(direction, wake=True):
        """Next/previous effect among the enabled ones."""
        order = playlist()
        later = [i for i in order if (i - index) * direction > 0]
        if later:
            select(min(later) if direction > 0 else max(later), wake)
        else:
            select(order[0] if direction > 0 else order[-1], wake)

    def advance():
        others = [i for i in playlist() if i != index]
        if cfg["shuffle"] and others:
            select(random.choice(others), wake=False)
        else:
            step(+1, wake=False)

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
        changed()

    def publish(now):
        loop = cfg["loop_seconds"]
        ui.publish({
            "connected": dev.connected,
            "effects": names,
            "themes": themes_for_ui(),
            "index": index,
            "theme": THEMES[theme_index].name,
            "speed": speed,
            "brightness": brightness,
            "loop_seconds": loop,
            "shuffle": cfg["shuffle"],
            "enabled": cfg["enabled"],
            "power": cfg["power"],
            "next_in": max(0.0, loop - (now - loop_from)) if loop > 0 else None,
            "leds": [hex_color(rgb) for row in current[:ENC_ROWS] for rgb in row],
            "buttons": [{"label": SIDE_LABELS[cc], "color": hex_color(side[cc])} for cc in SIDE_BUTTONS],
            "xl_strip": {**strip.state(), "connected": dev.connected,
                         "pads": [hex_color(rgb) for row in current[ENC_ROWS:] for rgb in row]},
            "launchkey": rig.state() if rig else None,
        })

    def compose(dt):
        """Current effect's frame, crossfaded from the previous one right after a switch."""
        grid = effects[index].frame(t, dt * speed)
        k = smoothstep(0.0, XFADE, time.perf_counter() - switched_at)
        if prev_index is None or k >= 1:
            return grid
        old = effects[prev_index].frame(t, dt * speed)
        return [[o + (n - o) * k for o, n in zip(orow, nrow)] for orow, nrow in zip(old, grid)]

    def paint(r, c, target, ease):
        rgb = lerp(current[r][c], target, ease)
        current[r][c] = rgb
        dev.set_rgb(GRID_CC[r][c], rgb if max(rgb) > 0.03 else BLACK)

    def render(grid, strip_grid, fade, dt):
        """Theme-colour both layers and ease every LED toward them (gives crossfades for free).
        fade = startup / exit fade; each layer adds its own on/off fade."""
        theme = THEMES[theme_index]
        enc_level = brightness * power_k * fade
        strip_level = float(strip.cfg["brightness"]) * strip.power_k * fade
        ease = 1 - math.exp(-dt * 8)          # slow: the encoder effects are idle displays
        for r in range(ENC_ROWS):
            for c in range(COLS):
                paint(r, c, scale(theme.color(grid[r][c]), enc_level), ease)
        ease = 1 - math.exp(-dt * 22)         # quick, like the Launchkey pads, so the tails stay crisp
        for r in range(STRIP_ROWS):
            for c in range(COLS):
                paint(ENC_ROWS + r, c, scale(strip.theme.color(strip_grid[r][c]), strip_level), ease)
        side_level = max(enc_level, strip_level)   # stay lit while either layer is on
        ease_side = 1 - math.exp(-dt * 40)   # quick, so the REC blink stays crisp
        for cc, rgb in side_buttons(time.perf_counter()).items():
            k = flashes.get(cc, 0.0)
            if k > 0:
                rgb = lerp(rgb, theme.color(1.0), k)
            rgb = lerp(side[cc], scale(rgb, side_level), ease_side)
            side[cc] = rgb
            dev.set_rgb(cc, rgb if max(rgb) > 0.03 else BLACK)

    strip = StripEngine(cfg["xl_strip"], changed, strip_announce, defaults=STRIP_DEFAULTS)

    def strip_frame(now, dt):
        return strip.frame(now, min(0.1, dt))

    rig = None if args.no_launchkey else LaunchkeyRig(
        cfg["launchkey"], cfg, changed, port=args.lk_port, no_screen=args.no_screen, debug=args.debug)

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
                    if cmd.get("device") == "launchkey":
                        if rig:
                            rig.apply(cmd)
                    elif cmd.get("device") == "xl_strip":
                        if "power" in cmd:
                            log("Mute/solo LEDs on" if cmd["power"] else "Mute/solo LEDs off (standby)")
                        strip.apply(cmd)
                    else:
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
                        side = {cc: BLACK for cc in SIDE_BUTTONS}
                        announce()
                        start = checked_at = time.perf_counter()
            if not dev.connected:
                if rig:
                    rig.tick(now)
                time.sleep(max(0.0, (frame_time if rig and rig.dev.connected else 0.1)
                               - (time.perf_counter() - now)))
                continue

            for msg in dev.pending():
                if msg.type == "sysex":
                    continue  # the device echoes SysEx back
                if args.debug:
                    print("in:", msg)
                if msg.type != "control_change":
                    continue
                if screen_at is None:
                    show_screen()    # touching a control wakes the blanked screen
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
                    if msg.control in side:
                        flashes[msg.control] = 1.0
                    if msg.control in (CC_PLAY, CC_RECORD):
                        step(1 if msg.control == CC_PLAY else -1)
                elif msg.channel == CH_FEATURES and msg.control == CC_MODE_REPORT and msg.value not in (1, 2):
                    log("Device switched to a Custom Mode - select DAW Mixer/Control to see the effects.")

            if cfg["loop_seconds"] > 0 and now - loop_from > cfg["loop_seconds"]:
                advance()

            timeout = float(cfg["screen_timeout"])
            if screen_at is not None and timeout > 0 and now - screen_at > timeout:
                dev.screen_off()
                screen_at = None

            t += dt * speed
            power_k += ((1.0 if cfg["power"] else 0.0) - power_k) * (1 - math.exp(-dt * 3))
            fade_in = smoothstep(0.0, 1.5, now - start)
            render(compose(dt), strip_frame(now, dt), fade_in, dt)
            flashes = {cc: k * math.exp(-dt / 0.3) for cc, k in flashes.items() if k > 0.01}
            if rig:
                rig.tick(now)

            time.sleep(max(0.0, frame_time - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        if dirty_at:
            save()
        if rig:
            rig.begin_exit()
        if dev.connected or (rig and rig.dev.connected):
            try:
                fade_start = last = time.perf_counter()
                while (now := time.perf_counter()) - fade_start < 1.0:
                    dt, last = now - last, now
                    t += dt * speed
                    if dev.connected:
                        render(compose(dt), strip_frame(now, dt), 1 - smoothstep(0.0, 1.0, now - fade_start), dt)
                    if rig:
                        rig.tick(now)
                    time.sleep(frame_time)
            except KeyboardInterrupt:
                pass
        if dev.connected:
            dev.all_off()
            if screen_at is not None:
                dev.screen_off()
            dev.daw_mode(False)
        dev.close()
        if rig:
            rig.close()
        log("LEDs off, devices back in standalone mode. Bye!")


if __name__ == "__main__":
    main()
