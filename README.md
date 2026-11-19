<p align="center">
  <img src="assets/logo.svg" alt="eve" width="640">
</p>

<p align="center">
  <b>Finally, a good reason to talk to yourself.</b><br>
  A pocket push-to-talk stick that types what you say wherever your cursor is.
</p>

<p align="center">
  <a href="https://atech.dev"><img alt="Made with Atech" src="https://img.shields.io/badge/made%20with-Atech-7cc8ff?style=flat-square"></a>
  <img alt="Runs locally" src="https://img.shields.io/badge/speech--to--text-100%25%20local-bfe6ff?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-e8f6ff?style=flat-square">
</p>

---

**eve** is a tiny USB-C voice stick. You hold its button and talk, and your
words appear in the window that has your cursor: a terminal, an editor, a chat
box. A **tap** sends it with Enter, and a **double-tap** deletes it.

It was built for driving AI coding sessions (Claude Code and friends) while
walking around with the laptop open, so you don't have to stop and type. It
works in any app.

- **Live:** each phrase is typed as soon as you pause. You don't have to let go first.
- **Private:** speech recognition runs on your own computer with
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper). No audio leaves the machine.
- **Hands-free send and undo:** tap to press Enter (or Tab to accept a suggestion), double-tap to delete.
- **Plug and go:** no drivers. It reconnects by itself and starts at login.

## How it works

```
 ┌──────────── eve stick ────────────┐          ┌──────────── your computer ─────────────┐
 │  mic  ──▶  ESP32-S3  ◀──  button  │  USB-C   │  voicetype.py                          │
 │            streams 16 kHz audio   │ ───────▶ │   ├─ splits speech at pauses (VAD)     │
 │            while the button is    │          │   ├─ transcribes each phrase (Whisper) │
 │            held; tap = ⏎ / ⇥      │ ◀─────── │   └─ types it at your cursor (xdotool) │
 └───────────────────────────────────┘  light   └────────────────────────────────────────┘
```

The stick only captures and streams audio. Transcription runs on your
computer, because a small microcontroller can't run a good speech model.

## Hardware

eve is built from [**Atech**](https://atech.dev) plug-together modules, with
no soldering:

| Part | Where it goes |
|---|---|
| Atech **Tiny** board (ESP32-S3, 3 ports) | — |
| Atech **Microphone** module (ICS-43434, I²S) | ports 1 + 2 |
| Atech **Button** module (V2.0 for the RGB status light) | port 3 |
| Atech **USB-C** module | port 4 |
| A USB-C cable that carries **data** | — |

> On our unit the microphone answered on the opposite port lines from the board
> docs, so the firmware includes a wiring diagnostic (below). Run it if you get
> silence.

## Using it

| You do | eve does |
|---|---|
| **Hold** the button and talk | types each phrase as you pause |
| **Let go** | finishes and types the last phrase |
| **Tap** | presses **Enter** to send what eve typed. With nothing waiting, presses **Tab** to accept Claude Code's suggestion, and the next tap sends it |
| **Double-tap** | deletes the text eve typed that hasn't been sent yet. After a Tab, clears the whole prompt, suggestion included |

A tap waits about a third of a second to make sure it isn't a double-tap.
eve only tracks text it typed itself: if you type or edit by hand in between,
a tap still sees "nothing waiting", and a double-tap deletes only as many
characters as eve typed. Clearing after a Tab uses Claude Code's Ctrl+U, so it
clears everything in the prompt.

The button light shows what's happening:

| Light | Meaning |
|---|---|
| green blink at power-up | firmware booted |
| **red** | listening |
| **amber** | finishing the last phrase |
| green flash | typed |
| blue flash | tap (Enter or Tab) |
| violet flash | double-tap (delete) |
| red flash | something went wrong (see the log) |

## Alternative build: XIAO ESP32S3 Sense + macOS

The same stick without Atech modules. It uses one small off-the-shelf board,
a 12 mm button and a 3D-printed case. The firmware in `firmware-xiao/` speaks
the same USB protocol, so `host/voicetype.py` works with either stick.

| Part | Notes |
|---|---|
| **Seeed Studio XIAO ESP32S3 Sense** | Not the "Pre-Soldered" version: its pins don't fit the case. The mic is on the Sense add-on board, so keep that clipped on. |
| **12×12×7.3 mm tactile button** with cap | Solder one leg to **D1**, the diagonally opposite leg to **GND**. |
| 28 AWG silicone wire, solder, soldering iron | Two joints in total. |
| 3D-printed case | `hardware/xiao-case/eve_plate.stl`: PLA, 0.2 mm layers, no supports. |

**The light is the board's single orange LED**, so the colours become patterns:

| Light | Meaning |
|---|---|
| 3 blinks at power-up | firmware booted |
| **solid** | listening |
| **fast blink** | finishing the last phrase |
| one long blink | typed |
| one short blink | tap (Enter or Tab) |
| two short blinks | double-tap (delete) |
| five rapid blinks | something went wrong (see the log) |

### Flash the XIAO

You only need [PlatformIO](https://platformio.org), no Atech library:

```bash
cd firmware-xiao
pio run -e xiao-boot -t upload   # first test: uses the board's BOOT button, no soldering
pio run -e xiao -t upload        # once the 12 mm button is wired to D1
```

If the upload fails, hold **BOOT**, tap **RESET**, release BOOT, and try again.

### Run it on a Mac

```bash
python3 -m venv host/.venv
host/.venv/bin/pip install -r host/requirements.txt
host/.venv/bin/python host/voicetype.py              # add --language it to force Italian
```

- On Apple Silicon, transcription runs on the Mac's GPU with
  [mlx-whisper](https://github.com/ml-explore/mlx-examples) and
  **large-v3-turbo**, a multilingual model that detects the language itself.
  The first run downloads it once (~1.6 GB). Use `--engine faster` for the
  original faster-whisper engine.
- Typing uses the clipboard and System Events, so accents and emoji work. Allow
  your terminal in **System Settings → Privacy & Security → Accessibility**,
  or nothing gets typed.
- To start eve at every login: `host/install-autostart-macos.sh` (add flags
  after it, e.g. `--language it`). It prints the Python path to allow under
  Accessibility, and logs to `~/Library/Logs/voicetype.log`.

## Setup

The steps below are for the Atech stick on Linux, tested with X11. Typing there uses `xdotool`, which doesn't work on Wayland.

### 1. Flash the stick

You need [PlatformIO](https://platformio.org) and the Atech driver library.
Get the driver library through [atech.dev](https://atech.dev).

```bash
export ATECH_LIB=/path/to/atech/firmware/esp32
cd firmware
pio run -t upload
```

If the upload times out, hold the board's **BOOT** button while it starts.

### 2. Install the desktop app

```bash
sudo apt install xdotool
python3 -m venv host/.venv
host/.venv/bin/pip install -r host/requirements.txt
```

### 3. Run it

```bash
host/.venv/bin/python host/voicetype.py
```

The first run downloads the speech model (~150 MB) once. Then hold the button
and talk.

To start it automatically every time you log in:

```bash
host/install-autostart.sh            # add flags after it, e.g. --model small.en
```

It logs to `~/.cache/voicetype.log`.

## Options

| Flag | What it does |
|---|---|
| `--model small.en` | more accurate on technical words, about 3× slower than the default `base.en` |
| `--language it` | force a spoken language (default: auto-detect; `*.en` models are English only) |
| `--engine faster` | use faster-whisper even on Apple Silicon (default there: mlx-whisper, large-v3-turbo) |
| `--pause 0.6` | how long a pause (seconds) ends a phrase. Default `0.4` |
| `--enter` | also press Enter every time you let go |
| `--no-live` | type everything at once when you let go |
| `--dry-run` | print the text instead of typing it |
| `--save-wav last.wav` | keep the last recording, for checking the mic |
| `-v` | also show the stick's log lines |

## Troubleshooting

**"waiting for the voice stick" forever.** The cable is probably charge-only,
or the firmware isn't flashed. `ls /dev/ttyACM*` should show a device.

**`error -71` in `journalctl -k`.** The laptop sees the stick but can't talk to
it, which means a bad data contact. Try another cable, then reseat the USB-C
module.

**Text is empty.** Record with `--save-wav last.wav` and play it with `aplay`.
If it's silent, run the wiring diagnostic:

```bash
cd firmware && pio run -e diag -t upload && pio device monitor
```

It tries every way the mic could be connected and reports which one is live.
Put those pins into `MIC_*` in `firmware/src/main.cpp`.

**Slow.** Transcription time depends on CPU speed. Laptops often throttle on a
low battery or a weak charger. Stay on `base.en`, or try `--model tiny.en`.

## Project layout

```
firmware/            PlatformIO project for the Atech stick (ESP32-S3, Arduino)
  src/main.cpp       recording, button gestures, USB wire protocol
  diag/diag.cpp      microphone wiring diagnostic
firmware-xiao/       the same firmware for the XIAO ESP32S3 Sense (no Atech library)
host/
  voicetype.py       desktop app: VAD phrase splitting, Whisper, typing (Linux + macOS)
  install-autostart.sh         start at login (Linux)
  install-autostart-macos.sh   start at login (macOS LaunchAgent)
hardware/xiao-case/  3D-printable case for the XIAO build (make_stl.py + STLs)
assets/logo.svg
```

The USB protocol is small and documented at the top of `firmware/src/main.cpp`.
It's a good starting point for a different front end, such as a macOS or
Wayland typer, or a cloud speech backend.

## Credits

Made with [**Atech**](https://atech.dev) hardware modules. Speech recognition by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI's Whisper
models, with Silero VAD for phrase detection. The logo is set in
[Montserrat](https://github.com/JulietaUla/Montserrat) ExtraLight (SIL Open Font License).

## License

[MIT](LICENSE)
