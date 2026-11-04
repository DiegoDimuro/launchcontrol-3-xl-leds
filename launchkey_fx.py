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

Effects (strips.py, shared with the XL 3's mute/solo rows) are moving lights with a fading tail
of `trail` pads (Scanner, Orbits and friends). They run on their own; pad hits push them
(velocity = how hard), holding a pad keeps it glowing.
"""

import colorsys
import math
import time

import mido

from fxcore import BLACK, clamp01, hex_color, lerp, log, rec_blink, scale, smoothstep, to7bit
from midilink import MidiLink
from strips import COLS, ROWS, StripEngine

# ---------------------------------------------------------------- protocol --

HEADER = [0x00, 0x20, 0x29, 0x02, 0x14]
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
BUTTON_WHITE = 1.0                    # steady level of every button LED but Record
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
    """Same contract as LaunchControlXL3: all MIDI I/O runs on a MidiLink worker thread."""

    def __init__(self, port_hint=None):
        self.hint = port_hint
        self._cache = {}
        self.link = MidiLink("Launchkey", lambda names: self._find(names, port_hint))

    @property
    def connected(self):
        return self.link.connected

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

    def poll(self):
        """Call every frame. True right after the device (re)connected: it's in DAW mode, repaint it."""
        if not self.link.poll():
            return False
        self._cache.clear()
        self.daw_mode(True)
        self._raw(0xB6, CC_PAD_MODE, PAD_MODE_DAW)
        self._raw(0xB6, 0x45, 0x7F)   # relative encoders
        return True

    def connect(self, timeout):
        """Startup: wait up to timeout for the device. Returns whether it's connected."""
        return self.link.wait(timeout) and self.poll()

    def _send(self, msg):
        self.link.send(msg)

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
        return self.link.pending()

    def all_off(self):
        self._cache.clear()
        for row in PAD_NOTES:
            for note in row:
                self.set_pad(note, BLACK)
        for cc in MONO_BUTTONS:
            self.set_mono(cc, 0.0)
        for cc in RGB_BUTTONS:
            self.set_palette(cc, 0)




# --------------------------------------------------------------------- rig --
# The effects themselves live in strips.py (shared with the XL 3's mute/solo rows).

class LaunchkeyRig:
    """Owns the Launchkey: its device, input, screen and LEDs. Settings, effects and the
    auto-loop are a StripEngine. Driven by tick() from the main loop."""

    def __init__(self, cfg, root_cfg, on_change, port=None, no_screen=False, debug=False):
        self.cfg, self.root_cfg = cfg, root_cfg
        self.no_screen, self.debug = no_screen, debug
        self.dev = LaunchkeyMK4(port)
        self.screen_at = None
        self.engine = StripEngine(cfg, on_change, self.announce)
        self.last = self.start = time.perf_counter()
        self.exit_k = 1.0
        self.exiting = False
        self.paused = False              # pads taken over by another pad mode on the device
        self.enc_acc = {}
        self.pads = [[BLACK] * COLS for _ in range(ROWS)]
        self.buttons = {cc: BLACK for cc in MONO_BUTTONS + RGB_BUTTONS}
        self.was_connected = False
        if self.dev.connect(timeout=1.5):
            self.was_connected = True
            time.sleep(0.1)
            self.announce()
        else:
            log("Launchkey not found yet - will pick it up when it's plugged in.")

    # ------------------------------------------------------------ settings --

    def show_screen(self, name=None, value=None, wake=True):
        """wake=False (auto-loop): only refresh the text if the screen is already on, keep its timeout."""
        if self.no_screen or not self.dev.connected or (not wake and self.screen_at is None):
            return
        e = self.engine
        if name is None:
            name = f"{e.index + 1} {e.name}"
            value = f"{e.theme.name} x{float(self.cfg['speed']):.1f}"
        self.dev.screen("LED FX", name, value)
        if wake:
            self.screen_at = time.perf_counter()

    def announce(self, wake=True):
        e = self.engine
        log(f"Launchkey effect {e.index + 1}: {e.name}  theme: {e.theme.name}")
        self.show_screen(wake=wake)

    def apply(self, cmd):
        """A command from the control panel."""
        if "power" in cmd:
            log("Launchkey LEDs on" if cmd["power"] else "Launchkey LEDs off (standby)")
        self.engine.apply(cmd)

    def state(self):
        return {
            **self.engine.state(),
            "connected": self.dev.connected,
            "paused": self.paused,
            "pads": [hex_color(rgb) for row in self.pads for rgb in row],
            "buttons": [{"cc": cc, "label": BUTTON_LABELS[cc], "rgb": cc in RGB_BUTTONS,
                         "color": hex_color(self.buttons[cc])} for cc in MONO_BUTTONS + RGB_BUTTONS],
        }

    # --------------------------------------------------------------- input --

    def encoder(self, n, delta):
        cfg, e = self.cfg, self.engine
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
                e.set_theme(e.theme_index + steps)
            elif steps:
                cfg["trail"] = max(1, min(7, cfg["trail"] + steps))
                self.show_screen("trail", f"{cfg['trail']} pads")
        elif n == 3:
            cfg["decay"] = max(0.3, min(3.0, cfg["decay"] + delta * 0.08))
            self.show_screen("decay", f"x{cfg['decay']:.2f}")
        else:
            return
        e.changed()

    def read_input(self):
        e = self.engine
        for msg in self.dev.pending():
            if msg.type == "sysex":
                continue
            if self.debug:
                print("launchkey in:", msg)
            if msg.type in ("note_on", "note_off") and msg.channel == 0 and msg.note in NOTE_TO_PAD:
                rc = NOTE_TO_PAD[msg.note]
                if msg.type == "note_on" and msg.velocity > 0:
                    e.hit(*rc, msg.velocity / 127)
                    e.held[rc] = 0.35 * msg.velocity / 127
                else:
                    e.held.pop(rc, None)
            elif msg.type == "polytouch" and msg.note in NOTE_TO_PAD:
                rc = NOTE_TO_PAD[msg.note]
                if rc in e.held:
                    e.held[rc] = max(0.2, msg.value / 127)
            elif msg.type == "aftertouch":
                for rc in e.held:
                    e.held[rc] = max(0.2, msg.value / 127)
            elif msg.type != "control_change":
                continue
            elif msg.channel == CH_ENCODERS and (msg.control in ENCODERS_ABS or msg.control in ENCODERS_REL):
                base = 21 if msg.control in ENCODERS_ABS else 85
                self.encoder(msg.control - base, msg.value - 64)
            elif msg.channel == CH_BUTTONS and msg.value > 0:
                if msg.control in self.buttons:
                    e.flash(msg.control, 1.0)
                if self.screen_at is None:
                    self.show_screen()
                if msg.control == TRACK_LEFT:
                    e.step(-1)
                elif msg.control == TRACK_RIGHT:
                    e.step(1)
                elif msg.control in (PAGE_UP, PAGE_DOWN):
                    e.set_theme(e.theme_index + (1 if msg.control == PAGE_UP else -1))
                elif msg.control in (BANK_UP, BANK_DOWN):
                    self.cfg["trail"] = max(1, min(7, self.cfg["trail"] + (1 if msg.control == BANK_UP else -1)))
                    self.show_screen("trail", f"{self.cfg['trail']} pads")
                    e.changed()
            elif msg.channel == CH_FEATURES and msg.control == CC_PAD_MODE:
                paused = msg.value != PAD_MODE_DAW
                if paused != self.paused:
                    self.paused = paused
                    log("Launchkey pads in another mode - effects paused on the pads." if paused
                        else "Launchkey pads back in DAW mode.")
                    self.dev.forget()

    # -------------------------------------------------------------- render --

    def render(self, grid, fade, dt):
        e = self.engine
        theme = e.theme
        level = float(self.cfg["brightness"]) * fade
        ease = 1 - math.exp(-dt * 22)
        for r in range(ROWS):
            for c in range(COLS):
                rgb = lerp(self.pads[r][c], scale(theme.color(grid[r][c]), level), ease)
                self.pads[r][c] = rgb
                if not self.paused:
                    self.dev.set_pad(PAD_NOTES[r][c], rgb if max(rgb) > 0.02 else BLACK)

        # Buttons stay lit white, apart from the pads and the speed setting. The only movement is
        # Record, blinking like a studio REC light in step with the XL 3's. Pressing a button flashes it.
        f, now = e.flashes, time.perf_counter()
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
        e.decay_flashes(dt)

    # ---------------------------------------------------------------- tick --

    def tick(self, now):
        dt = min(0.1, now - self.last)
        self.last = now
        dev = self.dev
        if dev.poll():
            log("Launchkey reconnected.")
            self.was_connected = True
            self.paused = False
            self.pads = [[BLACK] * COLS for _ in range(ROWS)]
            self.start = now
            self.announce()
        if not dev.connected:
            if self.was_connected:
                log("Launchkey disconnected - waiting for it to come back...")
                self.was_connected = False
            return

        self.read_input()
        timeout = float(self.root_cfg.get("screen_timeout", 30))
        if self.screen_at is not None and timeout > 0 and now - self.screen_at > timeout:
            dev.screen_off()
            self.screen_at = None

        grid = self.engine.frame(now, dt)
        if self.exiting:
            self.exit_k = max(0.0, self.exit_k - dt)
        fade = smoothstep(0.0, 1.5, now - self.start) * self.engine.power_k * smoothstep(0.0, 1.0, self.exit_k)
        self.render(grid, fade, dt)

    def begin_exit(self):
        self.exiting = True

    def close(self):
        if self.dev.connected:
            self.dev.all_off()
            if self.screen_at is not None:
                self.dev.screen_off()
            self.dev.daw_mode(False)
        self.dev.link.close()
        log("Launchkey LEDs off, back in standalone mode.")
