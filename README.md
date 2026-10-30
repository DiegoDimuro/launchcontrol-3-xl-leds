# Launch Control XL 3 LEDs

Ambient LED effects for the **Novation Launch Control XL 3**: 16 animations across the
encoder and button LEDs, 5 colour themes, with a browser control panel. Built to run all
the time in the background and survive the device being unplugged and plugged back in.

## Features

- **16 effects** in two banks: breathe, tide, aurora, ember, constellation, comet, ripple,
  lighthouse, drift, horizon, heartbeat, interference, fireflies, candle, orbit, lattice
- **5 themes**: amber, ice, mono, indigo, moss
- **Control panel** at <http://127.0.0.1:8765>:
  - a live view of the LEDs
  - pick an effect, or choose which ones are in the loop
  - loop time and shuffle
  - theme, speed and brightness
  - LEDs on/off
- **Remembers your setup** in `lcxl3_fx.json` across restarts
- **Hot-plug safe**: if the device is unplugged it waits for it, then carries on where it left off
- **Device controls still work**:

  | Control | Does |
  |---|---|
  | Fader 1 | Speed |
  | Fader 2 | Brightness |
  | Fader 3 | Theme |
  | Lower buttons | Pick effect (press again to flip bank) |
  | Upper buttons | Swell on that column |
  | Play / Record | Next / previous effect |

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
python lcxl3_fx.py               # --list, --effect aurora, --theme ice, --auto 5, --debug, --log fx.log, --ui-port 0
```

## How it works

The script puts the device into DAW mode and sets each LED's colour over SysEx, as described
in the Launch Control XL 3 Programmer's Reference v1.0. It sends to the device's DAW port.

Windows doesn't put "DAW" in the port names; there the DAW port is the second one,
`MIDIOUT2 (LCXL3 1 MIDI)` / `MIDIIN2 (LCXL3 1 MIDI)`, and the script picks it automatically.
Use `--port "<part of the name>"` to override.
