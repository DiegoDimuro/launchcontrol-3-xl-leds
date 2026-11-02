# Launch Control XL 3 LEDs

Ambient LED effects for the **Novation Launch Control XL 3**: slow ambient animations
across the encoder and button LEDs, 11 colour themes, with a browser control panel. Built to run all
the time in the background and survive the device being unplugged and plugged back in.

## Features

- **4 effects**: constellation, drift, showers, haze.
  Textures of small, independent lights, made for an idle display: sparse, one cell or column at a time, random timing, slow fades
- **11 themes**: amber, ice, mono, indigo, moss, plus subtler two-hue ones: dusk, sea glass,
  slate, sand, tide, aurora
- **Control panel** at <http://127.0.0.1:8765>:
  - a live view of the LEDs
  - pick an effect, or choose which ones are in the loop
  - loop time and shuffle
  - theme, speed and brightness
  - LEDs on/off
- **Side buttons** keep a 1-second beat with the REC blink, whatever the effect speed: a light
  steps round the four arrows, Record blinks like a studio REC light, Play stays green,
  Solo/Arm and Mute/Select take alternate beats
- **Remembers your setup** in `lcxl3_fx.json` across restarts
- **Hot-plug safe**: if the device is unplugged it waits for it, then carries on where it left off
- **Device controls still work**:

  | Control | Does |
  |---|---|
  | Fader 1 | Speed |
  | Fader 2 | Brightness |
  | Fader 3 | Theme |
  | Lower buttons 1–4 | Pick effect |
  | Upper buttons | Swell on that column |
  | Play / Record | Next / previous effect |

## Launchkey MK4 (optional)

If a **Novation Launchkey MK4** (25/37/49/61) is plugged in, the same app drives it too, with its
own tab in the control panel and its own settings. Either device can be unplugged on its own.

- **4 effects**: moving lights with a fading tail, set to anywhere from 1 to 7 pads long:
  - **scanner**: one light sweeping side to side across both rows
  - **orbits**: two lights opposite each other, circling the 16 pads
  - **crossing**: the two rows sweep in opposite directions and cross in the middle
  - **wings**: two lights open out from the middle to the edges and back
- **Pads** push the effect: the harder you hit, the more it boosts or turns the motion.
  Holding a pad down keeps it glowing.
- **Button LEDs** keep their own 1-second beat, shared with the XL 3's REC blink and unaffected
  by the speed setting: each arrow pair swaps every beat, the utility buttons chase one per beat,
  Play stays lit, Record blinks like a studio REC light (the Launchkey keeps a faint glow on it even
  when off), Stop and Loop breathe on alternate beats, and the two RGB arrows by the pads swap
  every beat in white. Pressed buttons flash.
- **Device controls**:

  | Control | Does |
  |---|---|
  | Encoders 1 / 2 / 3 / 4 / 5 | Speed / brightness / theme / decay / trail length |
  | Track ◀ ▶ | Previous / next effect |
  | Page ▲ ▼ | Theme |
  | Pad bank ▲ ▼ | Trail length |

The app only takes the Launchkey's DAW port. Its keys, wheels and the plain MIDI port stay free
for a DAW or synth. Pass `--no-launchkey` to leave it alone, or `--lk-port "<part of the name>"` to
pick the port yourself.

## Windows setup (runs at logon)

```powershell
winget install -e --id Python.Python.3.12 --scope user
git clone https://github.com/DiegoDimuro/launchcontrol-3-xl-leds.git
cd launchcontrol-3-xl-leds
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
```

This creates a `.venv`, installs the dependencies, registers a **`LCXL3-FX`** scheduled task,
and adds a **LED FX Control** shortcut to the Start Menu. The task:

- starts 15 s after logon with no console window
- restarts itself if it crashes

Manage it with:

```powershell
Stop-ScheduledTask LCXL3-FX
Start-ScheduledTask LCXL3-FX
Get-Content .\lcxl3_fx.log -Tail 20
```

> On Windows, only one program can use a MIDI port at a time. Stop the task before using
> Novation Components or a DAW with the XL 3, then start it again afterwards.

## Run manually (any OS)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python lcxl3_fx.py               # --list, --effect snow, --theme ice, --auto 5, --debug, --log fx.log, --ui-port 0
```

## How it works

The script puts the device into DAW mode and sets each LED's colour over SysEx, as described
in the Launch Control XL 3 Programmer's Reference v1.0. It sends to the device's DAW port.

Windows doesn't put "DAW" in the port names; there the DAW port is the second one,
`MIDIOUT2 (LCXL3 1 MIDI)` / `MIDIIN2 (LCXL3 1 MIDI)`, and the script picks it automatically.
Use `--port "<part of the name>"` to override.

The Launchkey works the same way, following the Launchkey MK4 Programmer's Reference v2.0
(`launchkey_fx.py`). Its DAW port on Windows is `MIDIOUT2 (Launchkey MK4 25 MIDI`; Windows cuts
the name short.

Shared code (themes, settings file, control panel server) lives in `fxcore.py`.
