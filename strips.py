"""
strips.py - moving-light effects for a 2 x 8 strip, shared by the Launchkey MK4 pads and the
Launch Control XL 3's two button rows (mute / solo).

Effects are moving lights with a fading tail of `trail` cells (Scanner, Orbits and friends).
They run on their own; hits push them (velocity = how hard), a held cell keeps glowing.

StripEngine is the device-independent half of a strip: settings, effect choice, auto-loop,
crossfades and the control-panel commands. The device code renders its frame() and does its own I/O.
"""

import math
import random
import time

from fxcore import THEMES, THEME_NAMES, clamp01, smoothstep

ROWS, COLS = 2, 8


def blank():
    return [[0.0] * COLS for _ in range(ROWS)]


# ----------------------------------------------------------------- effects --
# frame() returns a ROWS x COLS grid of brightness 0..1.
# hit(r, c, v): a cell was struck with velocity v (0..1).
# ctx gives the shared knobs (decay, trail) and flash(name, k).

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
    """Brightness of a trail cell that the head left `steps_ago` cells back (head = 0)."""
    if steps_ago < 0:
        return 0.0
    k = 1 - steps_ago / (trail + 1)
    return max(0.0, k) ** 1.6


class Scanner(PadEffect):
    """The car-front sweep: a light runs side to side with a fading tail of `trail` cells."""
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
    """Two opposite orbs circle the strip (top row, then bottom row back), each with a fading tail."""
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
    """Cells the head has passed keep a level that drops by 1/(trail+1) per cell the head moves:
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

DEFAULTS = {
    "effect": "orbits",
    "theme": "ice",
    "speed": 1.0,
    "brightness": 0.8,
    "decay": 1.0,          # how long hits linger (x)
    "trail": 4,            # cells in the tails
    "loop_seconds": 0,
    "shuffle": False,
    "enabled": [cls.name for cls in EFFECTS],
    "power": True,
}


# ------------------------------------------------------------------ engine --

class StripEngine:
    """Settings, effects, auto-loop and crossfades of one strip. No device I/O.

    on_change():          settings changed (save soon)
    on_announce(wake):    the effect or theme changed (log it, show it on a screen)
    """

    def __init__(self, cfg, on_change, on_announce=None, defaults=None):
        for k, v in (defaults or DEFAULTS).items():
            cfg.setdefault(k, v)
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        self.cfg, self.on_change = cfg, on_change
        self.on_announce = on_announce or (lambda wake: None)
        self.effects = [cls(self) for cls in EFFECTS]
        self.names = [e.name for e in self.effects]
        self.index = self.names.index(cfg["effect"]) if cfg["effect"] in self.names else 0
        self.theme_index = THEME_NAMES.index(cfg["theme"]) if cfg["theme"] in THEME_NAMES else 0
        self.prev_index, self.switched_at = None, time.perf_counter()
        self.loop_from = self.switched_at
        self.t = 0.0
        self.power_k = 1.0 if cfg["power"] else 0.0
        self.held = {}                   # (r, c) -> level from velocity / aftertouch
        self.flashes = {}                # name -> 0..1, decays every frame

    # shared knobs for effects
    @property
    def decay(self):
        return float(self.cfg["decay"])

    @property
    def trail(self):
        return int(self.cfg["trail"])

    @property
    def theme(self):
        return THEMES[self.theme_index]

    @property
    def name(self):
        return self.names[self.index]

    def flash(self, name, k):
        self.flashes[name] = max(self.flashes.get(name, 0.0), clamp01(k))

    # ------------------------------------------------------------ settings --

    def changed(self):
        self.cfg.update(effect=self.names[self.index], theme=THEME_NAMES[self.theme_index])
        self.on_change()

    def select(self, new, wake=True):
        self.loop_from = time.perf_counter()
        if new == self.index:
            return
        self.prev_index, self.index, self.switched_at = self.index, new, self.loop_from
        self.on_announce(wake)
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
        self.on_announce(True)
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
        if "hit" in cmd and isinstance(cmd["hit"], dict):
            h = cmd["hit"]
            self.hit(max(0, min(ROWS - 1, int(h.get("row", 0)))), max(0, min(COLS - 1, int(h.get("col", 0)))),
                     clamp01(float(h.get("velocity", 0.8))))
        self.changed()

    def state(self):
        loop = self.cfg["loop_seconds"]
        now = time.perf_counter()
        return {
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
        }

    # -------------------------------------------------------------- frames --

    def auto_loop(self, now):
        loop = float(self.cfg["loop_seconds"])
        if loop > 0 and now - self.loop_from > loop:
            others = [i for i in self.playlist() if i != self.index]
            if self.cfg["shuffle"] and others:
                self.select(random.choice(others), wake=False)
            else:
                self.step(1, wake=False)

    def compose(self, dt):
        grid = self.effects[self.index].frame(self.t, dt)
        k = smoothstep(0.0, XFADE, time.perf_counter() - self.switched_at)
        if self.prev_index is None or k >= 1:
            return grid
        old = self.effects[self.prev_index].frame(self.t, dt)
        return [[o + (n - o) * k for o, n in zip(orow, nrow)] for orow, nrow in zip(old, grid)]

    def frame(self, now, dt):
        """Advance by dt wall-clock seconds: auto-loop, power fade, effect frame with held cells
        blended in. Returns the brightness grid; self.power_k is the on/off fade."""
        self.auto_loop(now)
        speed = float(self.cfg["speed"])
        self.t += dt * speed
        self.power_k += ((1.0 if self.cfg["power"] else 0.0) - self.power_k) * (1 - math.exp(-dt * 3))
        grid = self.compose(dt * speed)
        for (r, c), h in self.held.items():
            grid[r][c] = 1 - (1 - grid[r][c]) * (1 - h)
        return grid

    def decay_flashes(self, dt):
        f = self.flashes
        for name in list(f):
            f[name] *= math.exp(-dt / 0.18)
