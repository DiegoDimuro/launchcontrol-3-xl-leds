"""
launchkey_fx.py - LED effects for the Novation Launchkey MK4 (25/37/49/61), driven from lcxl3_fx.py.

DAW mode (Launchkey MK4 Programmer's Reference v2.0), all on the DAW port:
  DAW mode on/off:   9F 0C 7F / 9F 0C 00
  Pads to DAW layout B6 1D 02        (the device reports pad-mode changes as B6 1D <mode>)
  Relative encoders  B6 45 7F
  Pad RGB colour:    F0 00 20 29 02 14 01 43 <pad note> <R> <G> <B> F7
  Button brightness  B3 <cc> <0..127>        (channel 4)
  Button palette     B0 <cc> <palette index> (channel 1, RGB buttons)
  Screen:            F0 00 20 29 02 14 04 <target> <config> F7 / ... 06 <target> <field> <ascii> F7

Pads (DAW layout) send Note On on channel 1 with velocity, poly aftertouch A0:
  top row     0x60-0x67
  bottom row  0x70-0x77

Effects are moving lights with a fading tail of `trail` pads (Scanner, Orbits and friends).
They run on their own; pad hits push them (velocity = how hard), holding a pad keeps it glowing.
"""

import colorsys
import math
import random
import time

import mido

from fxcore import (BLACK, THEMES, THEME_NAMES, clamp01, hex_color, lerp, log, rec_blink, scale,
                    smoothstep, to7bit)

# ---------------------------------------------------------------- protocol --

HEADER = [0x00, 0x20, 0x29, 0x02, 0x14]
ROWS, COLS = 2, 8
PAD_NOTES = [list(range(0x60, 0x68)), list(range(0x70, 0x78))]
NOTE_TO_PAD = {n: (r, c) for r, row in enumerate(PAD_NOTES) for c, n in enumerate(row)}

CH_BUTTONS = 0        # channel 1: button presses / palette colours
CH_MONO = 3           # channel 4: button brightness
CH_FEATURES = 6       # channel 7: mode reports
CH_ENCODERS = 15      # channel 16
CC_PAD_MODE = 0x1D
PAD_MODE_DAW = 2
SCREEN_STATIONARY = 0x20

# Buttons with LEDs (CC numbers in DAW mode)
TRACK_LEFT, TRACK_RIGHT = 103, 102
PAGE_UP, PAGE_DOWN = 51, 52
BANK_UP, BANK_DOWN = 106, 107
SCENE, FUNCTION = 104, 105
UTILITY = [74, 75, 76, 77]           # Capture MIDI, Quantise, Metronome, Undo
TRANSPORT = [115, 116, 117, 118]     # Play, Stop, Record, Loop
BUTTON_WHITE = 0.6                    # steady level of every button LED but Record
MONO_BUTTONS = [TRACK_LEFT, TRACK_RIGHT, PAGE_UP, PAGE_DOWN, BANK_UP, BANK_DOWN] + UTILITY + TRANSPORT
RGB_BUTTONS = [SCENE, FUNCTION]
BUTTON_LABELS = {
    TRACK_LEFT: "Track <", TRACK_RIGHT: "Track >", PAGE_UP: "Page up", PAGE_DOWN: "Page down",
    BANK_UP: "Pads up", BANK_DOWN: "Pads down", SCENE: "Scene >", FUNCTION: "Function",
    74: "Capture", 75: "Quantise", 76: "Metronome", 77: "Undo",
    115: "Play", 116: "Stop", 117: "Record", 118: "Loop",
}
ENCODERS_ABS = range(21, 29)     # plugin / mixer modes
ENCODERS_REL = range(85, 93)     # transport mode

# Novation colour palette, approximated: 0 off, 1-3 greys/white, then 14 hue families of 4
# (pastel, full, half, dim) starting at index 4.
PALETTE_HUES = [0.0, 0.07, 0.15, 0.22, 0.31, 0.36, 0.42, 0.5, 0.55, 0.6, 0.66, 0.75, 0.83, 0.92]


def palette_index(rgb):
    """Closest palette entry for an RGB colour. Returns (index, rgb the button roughly shows)."""
    h, s, v = colorsys.rgb_to_hsv(*(clamp01(x) for x in rgb))
    if v < 0.04:
        return 0, BLACK
    if s < 0.25:
        if v > 0.6:
            return 3, (1.0, 1.0, 1.0)
        return (2, (0.5, 0.5, 0.5)) if v > 0.3 else (1, (0.22, 0.22, 0.22))
    group = min(range(len(PALETTE_HUES)),
                key=lambda g: min(abs(h - PALETTE_HUES[g]), 1 - abs(h - PALETTE_HUES[g])))
    variant, level = (1, 1.0) if v > 0.66 else (2, 0.5) if v > 0.33 else (3, 0.22)
    return 4 + 4 * group + variant, colorsys.hsv_to_rgb(PALETTE_HUES[group], 1.0, level)


class LaunchkeyMK4:
    """Same contract as LaunchControlXL3: connect() can be retried, failed I/O drops the connection."""

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
        for n in names:
            low = n.lower()
            if "launchkey" in low and "mini" not in low and (
                    "daw" in low or "midiout2" in low or "midiin2" in low):
                return n
        return None

    def connect(self):
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
        log(f"Launchkey connected. Output: {out_name}  Input: {in_name or '(none - controls disabled)'}")
        self.daw_mode(True)
        self._raw(0xB6, CC_PAD_MODE, PAD_MODE_DAW)
        self._raw(0xB6, 0x45, 0x7F)   # relative encoders
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
        try:
            return self._find(mido.get_output_names(), self.hint) is not None
        except Exception:
            return False

    def _send(self, msg):
        if not self.out:
            return
        try:
            self.out.send(msg)
        except Exception:
            self.disconnect()

    def _raw(self, *data):
        self._send(mido.Message.from_bytes(list(data)))

    def _sysex(self, payload):
        self._send(mido.Message("sysex", data=HEADER + payload))

    def daw_mode(self, on):
        self._raw(0x9F, 0x0C, 0x7F if on else 0x00)

    def forget(self):
        """Resend everything next frame (after the device repainted the pads itself)."""
        self._cache.clear()

    def set_pad(self, note, rgb):
        values = tuple(to7bit(v) for v in rgb)
        if self._cache.get(("pad", note)) != values:
            self._cache[("pad", note)] = values
            self._sysex([0x01, 0x43, note, *values])

    def set_mono(self, cc, level):
        value = to7bit(level)
        if self._cache.get(("mono", cc)) != value:
            self._cache[("mono", cc)] = value
            self._raw(0xB0 | CH_MONO, cc, value)

    def set_palette(self, cc, index):
        if self._cache.get(("pal", cc)) != index:
            self._cache[("pal", cc)] = index
            self._raw(0xB0 | CH_BUTTONS, cc, index)

    def screen(self, title, name, value):
        def ascii7(text):
            return [ord(ch) for ch in text if 32 <= ord(ch) <= 126][:16]
        self._sysex([0x04, SCREEN_STATIONARY, 0x60 | 0x02])
        for field, text in enumerate((title, name, value)):
            self._sysex([0x06, SCREEN_STATIONARY, field] + ascii7(text))
        self._sysex([0x04, SCREEN_STATIONARY, 0x7F])

    def screen_off(self):
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
        for row in PAD_NOTES:
            for note in row:
                self.set_pad(note, BLACK)
        for cc in MONO_BUTTONS:
            self.set_mono(cc, 0.0)
        for cc in RGB_BUTTONS:
            self.set_palette(cc, 0)


# ----------------------------------------------------------------- effects --
# frame() returns a ROWS x COLS grid of brightness 0..1.
# hit(r, c, v): a pad was struck with velocity v (0..1).
# ctx gives the shared knobs (decay, trail) and flash(name, k) for the button LEDs.

def blank():
    return [[0.0] * COLS for _ in range(ROWS)]


class PadEffect:
    name = "effect"

    def __init__(self, ctx):
        self.ctx = ctx
        self.t = 0.0

    def hit(self, r, c, v):
        pass

    def frame(self, t, dt):
        raise NotImplementedError


def trail_level(steps_ago, trail):
    """Brightness of a trail cell that the head left `steps_ago` pads back (head = 0)."""
    if steps_ago < 0:
        return 0.0
    k = 1 - steps_ago / (trail + 1)
    return max(0.0, k) ** 1.6


class Scanner(PadEffect):
    """The car-front sweep: a light runs side to side with a fading tail of `trail` pads."""
    name = "scanner"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.x, self.direction, self.travel, self.boost = 0.0, 1, 0.0, 0.0
        self.stamp = [-99.0] * COLS    # travel distance when the head last sat on each column

    def hit(self, r, c, v):
        self.boost = min(1.5, self.boost + 0.5 + v)

    def frame(self, t, dt):
        step = (5.5 + 5.0 * self.boost) * dt
        self.boost *= math.exp(-dt / 1.0)
        self.x += step * self.direction
        self.travel += step
        if self.x >= COLS - 1:
            self.x, self.direction = COLS - 1, -1
            self.ctx.flash("right", 1.0)
        elif self.x <= 0:
            self.x, self.direction = 0.0, 1
            self.ctx.flash("left", 1.0)
        self.stamp[round(self.x)] = self.travel
        peak = min(1.0, 0.8 + 0.2 * self.boost)
        grid = blank()
        for c in range(COLS):
            i = peak * trail_level(self.travel - self.stamp[c], self.ctx.trail)
            grid[0][c] = grid[1][c] = i
        return grid


LOOP = [(0, c) for c in range(COLS)] + [(1, c) for c in reversed(range(COLS))]   # clockwise ring


class Orbits(PadEffect):
    """Two opposite orbs circle the pads (top row, then bottom row back), each with a fading tail."""
    name = "orbits"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.pos, self.direction, self.travel, self.boost = 0.0, 1, 0.0, 0.0
        self.stamp = [-99.0] * len(LOOP)

    def hit(self, r, c, v):
        self.direction = -self.direction
        self.boost = min(2.0, self.boost + 0.6 + v)
        self.ctx.flash("hit", v)

    def frame(self, t, dt):
        step = (3.2 + 4.0 * self.boost) * dt
        self.boost *= math.exp(-dt / 1.2)
        self.pos = (self.pos + step * self.direction) % len(LOOP)
        self.travel += step
        for offset in (0, len(LOOP) // 2):
            self.stamp[(round(self.pos) + offset) % len(LOOP)] = self.travel
        grid = blank()
        for i, (r, c) in enumerate(LOOP):
            grid[r][c] = trail_level(self.travel - self.stamp[i], self.ctx.trail)
        return grid


class Trails:
    """Cells the head has passed keep a level that drops by 1/(trail+1) per pad the head moves:
    the same tail Scanner and Orbits draw, for effects with several heads."""

    def __init__(self):
        self.level = blank()

    def fade(self, steps, trail):
        k = steps / (trail + 1)
        for row in self.level:
            for c in range(COLS):
                row[c] = max(0.0, row[c] - k)

    def mark(self, r, c, peak=1.0):
        if 0 <= r < ROWS and 0 <= c < COLS:
            self.level[r][c] = max(self.level[r][c], peak)

    def grid(self):
        return [[v ** 1.6 for v in row] for row in self.level]


class Crossing(PadEffect):
    """Scanner split in two: the top row sweeps one way while the bottom row sweeps the other."""
    name = "crossing"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.x, self.direction, self.boost = 0.0, 1, 0.0
        self.trails = Trails()

    def hit(self, r, c, v):
        self.boost = min(1.5, self.boost + 0.5 + v)

    def frame(self, t, dt):
        step = (5.0 + 5.0 * self.boost) * dt
        self.boost *= math.exp(-dt / 1.0)
        before = self.x
        self.x += step * self.direction
        if self.x >= COLS - 1:
            self.x, self.direction = COLS - 1, -1
        elif self.x <= 0:
            self.x, self.direction = 0.0, 1
        if (before - 3.5) * (self.x - 3.5) < 0:     # the heads pass each other
            self.ctx.flash("hit", 0.7)
        self.trails.fade(step, self.ctx.trail)
        peak = min(1.0, 0.8 + 0.2 * self.boost)
        self.trails.mark(0, round(self.x), peak)
        self.trails.mark(1, COLS - 1 - round(self.x), peak)
        return self.trails.grid()


class Wings(PadEffect):
    """Two heads open out from the middle to the edges and fold back in, on both rows."""
    name = "wings"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.d, self.direction, self.boost = 0.0, 1, 0.0     # distance from the centre, 0..3
        self.trails = Trails()

    def hit(self, r, c, v):
        self.boost = min(1.5, self.boost + 0.5 + v)

    def frame(self, t, dt):
        step = (3.0 + 3.0 * self.boost) * dt
        self.boost *= math.exp(-dt / 1.0)
        self.d += step * self.direction
        if self.d >= 3:
            self.d, self.direction = 3.0, -1
            self.ctx.flash("left", 1.0)
            self.ctx.flash("right", 1.0)
        elif self.d <= 0:
            self.d, self.direction = 0.0, 1
        self.trails.fade(step, self.ctx.trail)
        peak = min(1.0, 0.8 + 0.2 * self.boost)
        o = round(self.d)
        for r in range(ROWS):
            self.trails.mark(r, 3 - o, peak)
            self.trails.mark(r, 4 + o, peak)
        return self.trails.grid()


EFFECTS = [Scanner, Orbits, Crossing, Wings]
XFADE = 1.0


# --------------------------------------------------------------------- rig --

DEFAULTS = {
    "effect": "orbits",
    "theme": "ice",
    "speed": 1.0,
    "brightness": 0.8,
    "decay": 1.0,          # how long hits linger (x)
    "trail": 4,            # pads in the Scanner / Orbits tails
    "loop_seconds": 0,
    "shuffle": False,
    "enabled": [cls.name for cls in EFFECTS],
    "power": True,
}


class LaunchkeyRig:
    """Owns the Launchkey: its device, effects, settings and input. Driven by tick() from the main loop."""

    def __init__(self, cfg, root_cfg, on_change, port=None, no_screen=False, debug=False):
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        self.cfg, self.root_cfg, self.on_change = cfg, root_cfg, on_change
        self.no_screen, self.debug = no_screen, debug
        self.dev = LaunchkeyMK4(port)
        self.effects = [cls(self) for cls in EFFECTS]
        self.names = [e.name for e in self.effects]
        self.index = self.names.index(cfg["effect"]) if cfg["effect"] in self.names else 0
        self.theme_index = THEME_NAMES.index(cfg["theme"]) if cfg["theme"] in THEME_NAMES else 0
        self.prev_index, self.switched_at = None, time.perf_counter()
        self.loop_from = self.switched_at
        self.t = 0.0
        self.last = self.start = self.checked_at = self.retry_at = time.perf_counter()
        self.power_k = 1.0 if cfg["power"] else 0.0
        self.exit_k = 1.0
        self.exiting = False
        self.paused = False              # pads taken over by another pad mode on the device
        self.screen_at = None
        self.held = {}                   # (r, c) -> level from velocity / aftertouch
        self.flashes = {}                # name -> 0..1
        self.enc_acc = {}
        self.pads = [[BLACK] * COLS for _ in range(ROWS)]
        self.buttons = {cc: BLACK for cc in MONO_BUTTONS + RGB_BUTTONS}
        self.was_connected = False
        if self.dev.connect():
            self.was_connected = True
            time.sleep(0.1)
            self.announce()
        else:
            log("Launchkey not found yet - will pick it up when it's plugged in.")

    # shared knobs for effects
    @property
    def decay(self):
        return float(self.cfg["decay"])

    @property
    def trail(self):
        return int(self.cfg["trail"])

    def flash(self, name, k):
        self.flashes[name] = max(self.flashes.get(name, 0.0), clamp01(k))

    # ------------------------------------------------------------ settings --

    def changed(self):
        self.cfg.update(effect=self.names[self.index], theme=THEME_NAMES[self.theme_index])
        self.on_change()

    def show_screen(self, name=None, value=None, wake=True):
        """wake=False (auto-loop): only refresh the text if the screen is already on, keep its timeout."""
        if self.no_screen or not self.dev.connected or (not wake and self.screen_at is None):
            return
        if name is None:
            name = f"{self.index + 1} {self.names[self.index]}"
            value = f"{THEME_NAMES[self.theme_index]} x{float(self.cfg['speed']):.1f}"
        self.dev.screen("LED FX", name, value)
        if wake:
            self.screen_at = time.perf_counter()

    def announce(self, wake=True):
        log(f"Launchkey effect {self.index + 1}: {self.names[self.index]}  theme: {THEME_NAMES[self.theme_index]}")
        self.show_screen(wake=wake)

    def select(self, new, wake=True):
        self.loop_from = time.perf_counter()
        if new == self.index:
            return
        self.prev_index, self.index, self.switched_at = self.index, new, self.loop_from
        self.announce(wake)
        self.changed()

    def playlist(self):
        order = [i for i, n in enumerate(self.names) if n in self.cfg["enabled"]]
        return order or list(range(len(self.names)))

    def step(self, direction, wake=True):
        order = self.playlist()
        later = [i for i in order if (i - self.index) * direction > 0]
        if later:
            self.select(min(later) if direction > 0 else max(later), wake)
        else:
            self.select(order[0] if direction > 0 else order[-1], wake)

    def set_theme(self, i):
        self.theme_index = i % len(THEMES)
        self.announce()
        self.changed()

    def hit(self, r, c, v):
        self.effects[self.index].hit(r, c, v)
        self.flash("hit", v)

    def apply(self, cmd):
        """A command from the control panel."""
        cfg = self.cfg
        if "select" in cmd:
            self.select(max(0, min(len(self.effects) - 1, int(cmd["select"]))))
        if "step" in cmd:
            self.step(1 if int(cmd["step"]) > 0 else -1)
        if "theme" in cmd and cmd["theme"] in THEME_NAMES:
            self.set_theme(THEME_NAMES.index(cmd["theme"]))
        if "speed" in cmd:
            cfg["speed"] = max(0.2, min(3.0, float(cmd["speed"])))
        if "brightness" in cmd:
            cfg["brightness"] = max(0.05, min(1.0, float(cmd["brightness"])))
        if "decay" in cmd:
            cfg["decay"] = max(0.3, min(3.0, float(cmd["decay"])))
        if "trail" in cmd:
            cfg["trail"] = max(1, min(7, int(cmd["trail"])))
        if "loop_seconds" in cmd:
            cfg["loop_seconds"] = max(0.0, float(cmd["loop_seconds"]))
            self.loop_from = time.perf_counter()
        if "shuffle" in cmd:
            cfg["shuffle"] = bool(cmd["shuffle"])
        if "enabled" in cmd and isinstance(cmd["enabled"], list):
            cfg["enabled"] = [n for n in self.names if n in cmd["enabled"]]
        if "power" in cmd:
            cfg["power"] = bool(cmd["power"])
            log("Launchkey LEDs on" if cfg["power"] else "Launchkey LEDs off (standby)")
        if "hit" in cmd and isinstance(cmd["hit"], dict):
            h = cmd["hit"]
            self.hit(max(0, min(ROWS - 1, int(h.get("row", 0)))), max(0, min(COLS - 1, int(h.get("col", 0)))),
                     clamp01(float(h.get("velocity", 0.8))))
        self.changed()

    def state(self):
        loop = self.cfg["loop_seconds"]
        now = time.perf_counter()
        return {
            "connected": self.dev.connected,
            "paused": self.paused,
            "effects": self.names,
            "index": self.index,
            "theme": THEME_NAMES[self.theme_index],
            "speed": self.cfg["speed"],
            "brightness": self.cfg["brightness"],
            "decay": self.cfg["decay"],
            "trail": self.cfg["trail"],
            "loop_seconds": loop,
            "shuffle": self.cfg["shuffle"],
            "enabled": self.cfg["enabled"],
            "power": self.cfg["power"],
            "next_in": max(0.0, loop - (now - self.loop_from)) if loop > 0 else None,
            "pads": [hex_color(rgb) for row in self.pads for rgb in row],
            "buttons": [{"cc": cc, "label": BUTTON_LABELS[cc], "rgb": cc in RGB_BUTTONS,
                         "color": hex_color(self.buttons[cc])} for cc in MONO_BUTTONS + RGB_BUTTONS],
        }

    # --------------------------------------------------------------- input --

    def encoder(self, n, delta):
        cfg = self.cfg
        if n == 0:
            cfg["speed"] = max(0.2, min(3.0, cfg["speed"] + delta * 0.08))
            self.show_screen("speed", f"x{cfg['speed']:.2f}")
        elif n == 1:
            cfg["brightness"] = max(0.05, min(1.0, cfg["brightness"] + delta * 0.04))
            self.show_screen("brightness", f"{round(cfg['brightness'] * 100)}%")
        elif n in (2, 4):
            acc = self.enc_acc.get(n, 0) + delta
            steps, self.enc_acc[n] = int(acc / 3), acc - int(acc / 3) * 3
            if steps and n == 2:
                self.set_theme(self.theme_index + steps)
            elif steps:
                cfg["trail"] = max(1, min(7, cfg["trail"] + steps))
                self.show_screen("trail", f"{cfg['trail']} pads")
        elif n == 3:
            cfg["decay"] = max(0.3, min(3.0, cfg["decay"] + delta * 0.08))
            self.show_screen("decay", f"x{cfg['decay']:.2f}")
        else:
            return
        self.changed()

    def read_input(self):
        for msg in self.dev.pending():
            if msg.type == "sysex":
                continue
            if self.debug:
                print("launchkey in:", msg)
            if msg.type in ("note_on", "note_off") and msg.channel == 0 and msg.note in NOTE_TO_PAD:
                rc = NOTE_TO_PAD[msg.note]
                if msg.type == "note_on" and msg.velocity > 0:
                    self.hit(*rc, msg.velocity / 127)
                    self.held[rc] = 0.35 * msg.velocity / 127
                else:
                    self.held.pop(rc, None)
            elif msg.type == "polytouch" and msg.note in NOTE_TO_PAD:
                rc = NOTE_TO_PAD[msg.note]
                if rc in self.held:
                    self.held[rc] = max(0.2, msg.value / 127)
            elif msg.type == "aftertouch":
                for rc in self.held:
                    self.held[rc] = max(0.2, msg.value / 127)
            elif msg.type != "control_change":
                continue
            elif msg.channel == CH_ENCODERS and (msg.control in ENCODERS_ABS or msg.control in ENCODERS_REL):
                base = 21 if msg.control in ENCODERS_ABS else 85
                self.encoder(msg.control - base, msg.value - 64)
            elif msg.channel == CH_BUTTONS and msg.value > 0:
                if msg.control in self.buttons:
                    self.flash(msg.control, 1.0)
                if self.screen_at is None:
                    self.show_screen()
                if msg.control == TRACK_LEFT:
                    self.step(-1)
                elif msg.control == TRACK_RIGHT:
                    self.step(1)
                elif msg.control in (PAGE_UP, PAGE_DOWN):
                    self.set_theme(self.theme_index + (1 if msg.control == PAGE_UP else -1))
                elif msg.control in (BANK_UP, BANK_DOWN):
                    self.cfg["trail"] = max(1, min(7, self.cfg["trail"] + (1 if msg.control == BANK_UP else -1)))
                    self.show_screen("trail", f"{self.cfg['trail']} pads")
                    self.changed()
            elif msg.channel == CH_FEATURES and msg.control == CC_PAD_MODE:
                paused = msg.value != PAD_MODE_DAW
                if paused != self.paused:
                    self.paused = paused
                    log("Launchkey pads in another mode - effects paused on the pads." if paused
                        else "Launchkey pads back in DAW mode.")
                    self.dev.forget()

    # -------------------------------------------------------------- render --

    def compose(self, dt):
        grid = self.effects[self.index].frame(self.t, dt)
        k = smoothstep(0.0, XFADE, time.perf_counter() - self.switched_at)
        if self.prev_index is None or k >= 1:
            return grid
        old = self.effects[self.prev_index].frame(self.t, dt)
        return [[o + (n - o) * k for o, n in zip(orow, nrow)] for orow, nrow in zip(old, grid)]

    def render(self, grid, fade, dt):
        theme = THEMES[self.theme_index]
        level = float(self.cfg["brightness"]) * fade
        ease = 1 - math.exp(-dt * 22)
        for (r, c), h in self.held.items():
            grid[r][c] = 1 - (1 - grid[r][c]) * (1 - h)
        for r in range(ROWS):
            for c in range(COLS):
                rgb = lerp(self.pads[r][c], scale(theme.color(grid[r][c]), level), ease)
                self.pads[r][c] = rgb
                if not self.paused:
                    self.dev.set_pad(PAD_NOTES[r][c], rgb if max(rgb) > 0.02 else BLACK)

        # Buttons stay lit white, apart from the pads and the speed setting. The only movement is
        # Record, blinking like a studio REC light in step with the XL 3's. Pressing a button flashes it.
        f, now = self.flashes, time.perf_counter()
        mono = {cc: BUTTON_WHITE for cc in MONO_BUTTONS}
        mono[117] = rec_blink(now)                        # Record
        fast = 1 - math.exp(-dt * 40)
        for cc, v in mono.items():
            v = clamp01(max(v, f.get(cc, 0))) * level
            self.buttons[cc] = lerp(self.buttons[cc], (v, v, v), fast)
            self.dev.set_mono(cc, self.buttons[cc][0])
        # The two RGB arrow buttons, steady white too
        for cc in (FUNCTION, SCENE):
            v = clamp01(max(BUTTON_WHITE, f.get(cc, 0))) * level
            index, shown = palette_index((v, v, v))
            self.buttons[cc] = shown
            self.dev.set_palette(cc, index)
        for name in list(f):
            f[name] *= math.exp(-dt / 0.18)

    # ---------------------------------------------------------------- tick --

    def tick(self, now):
        dt = min(0.1, now - self.last)
        self.last = now
        dev = self.dev
        if dev.connected and now - self.checked_at > 2.0:
            self.checked_at = now
            if not dev.present():
                dev.disconnect()
        if not dev.connected:
            if self.was_connected:
                log("Launchkey disconnected - waiting for it to come back...")
                self.was_connected = False
            if now - self.retry_at > 1.0 and not self.exiting:
                self.retry_at = now
                if dev.connect():
                    log("Launchkey reconnected.")
                    self.was_connected = True
                    self.paused = False
                    self.pads = [[BLACK] * COLS for _ in range(ROWS)]
                    self.start = self.checked_at = now
                    self.announce()
            return

        self.read_input()
        loop = float(self.cfg["loop_seconds"])
        if loop > 0 and now - self.loop_from > loop:
            others = [i for i in self.playlist() if i != self.index]
            if self.cfg["shuffle"] and others:
                self.select(random.choice(others), wake=False)
            else:
                self.step(1, wake=False)

        timeout = float(self.root_cfg.get("screen_timeout", 30))
        if self.screen_at is not None and timeout > 0 and now - self.screen_at > timeout:
            dev.screen_off()
            self.screen_at = None

        speed = float(self.cfg["speed"])
        self.t += dt * speed
        self.power_k += ((1.0 if self.cfg["power"] else 0.0) - self.power_k) * (1 - math.exp(-dt * 3))
        if self.exiting:
            self.exit_k = max(0.0, self.exit_k - dt)
        fade = smoothstep(0.0, 1.5, now - self.start) * self.power_k * smoothstep(0.0, 1.0, self.exit_k)
        self.render(self.compose(dt * speed), fade, dt)

    def begin_exit(self):
        self.exiting = True

    def close(self):
        if self.dev.connected:
            self.dev.all_off()
            if self.screen_at is not None:
                self.dev.screen_off()
            self.dev.daw_mode(False)
        self.dev.disconnect()
        log("Launchkey LEDs off, back in standalone mode.")
