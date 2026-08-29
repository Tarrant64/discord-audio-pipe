# discord-audio-pipe
[![GitHub Workflow Status](https://github.com/QiCuiHub/discord-audio-pipe/workflows/CI/badge.svg)](https://github.com/QiCuiHub/discord-audio-pipe/actions?query=workflow%3ACI)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/QiCuiHub/discord-audio-pipe)](https://github.com/QiCuiHub/discord-audio-pipe/releases/latest)

Simple program to send stereo audio (microphone, stereo mix, virtual audio cable, etc) into a discord bot.

You can download the latest release [**here**](https://github.com/QiCuiHub/discord-audio-pipe/releases)
- If you are using the source code, install the dependencies and start the program using `main.pyw`
- The `.exe` does not require python or dependencies

## Setting up a Bot account
1. Follow the steps [**here**](https://docs.pycord.dev/en/master/discord.html) to setup and invite a discord bot
2. To link the program to your bot, create a file ``token.txt`` in the same directory as the `.exe` / `main.pyw` and save the bot token inside

## Dependencies
Requires Python 3.10+ (PyQt6 6.11 does not support older versions). Install dependencies by running `pip3 install -r requirements.txt`

In some cases PortAudio and xcb libraries may be missing on linux. On Ubuntu they can be installed with
```
    $ sudo apt-get install libportaudio2
    $ sudo apt-get install libxcb-xinerama0
    $ sudo apt-get install libxcb-cursor0
```
macOS requires PortAudio and Opus libraries
```
    $ brew install portaudio --HEAD
    $ brew install opus
```

## CLI
Running the `.exe` / `main.pyw` without any arguments will start the graphical interface. Alternatively, discord-audio-pipe can be run from the command line and contains some tools to query system audio devices and accessible channels.
```
usage: main.pyw [-h] [-t TOKEN] [-v] [--diagnose] [-c CHANNEL] [-d DEVICE]
                [-D] [-C]

Discord Audio Pipe

options:
  -h, --help            show this help message and exit
  -t TOKEN, --token TOKEN
                        The token for the bot
  -v, --verbose         Enable verbose logging
  --diagnose            Enable audio/voice-state diagnostics (see
                        DAP_session.log)

Command Line Mode:
  -c CHANNEL, --channel CHANNEL
                        The channel to connect to as an id
  -d DEVICE, --device DEVICE
                        The device to listen from as an index

Queries:
  -D, --devices         Query compatible audio devices
  -C, --channels        Query servers and channels (requires token)
```

## Logging and diagnostics

Three log files are written next to `main.pyw`. All three rotate at 2 MB and
keep 3 backups, so they can no longer grow without bound.

| File | When | Contents |
|---|---|---|
| `DAP_errors.log` | always | ERROR and above; unhandled exceptions from the app. |
| `DAP_session.log` | always | INFO lifecycle trail: start (with versions and redacted args), device selection, channel joins, disconnects, shutdown. Also carries a handful of discord.py voice diagnostics promoted from DEBUG to WARNING — most importantly `Aborting playback`, which discord.py emits immediately before it silently abandons the audio player thread. |
| `discord.log` | `-v` only | Full DEBUG firehose from the `discord` logger. |

Bot tokens are never written to any log file; `-t` / `--token` values are
redacted.

### `--diagnose`

Off by default. When enabled it adds observation only — it never restarts,
reconnects or otherwise alters behaviour — aimed at the long-standing bug
where audio stops after 15–20 minutes while the bot stays connected
(upstream issues #16 and #46). Output goes to `DAP_session.log` under the
`dap.diag` logger.

Every 5 seconds it records:

- **Audio thread** — max/mean blocking time of the PortAudio `read()` call,
  the `overflowed` flag that the normal code path discards, the input ring
  buffer depth, and a drift ledger comparing frames actually read against
  elapsed wall time (reported in seconds and ppm).
- **Voice state** — `is_playing`, `is_paused`, `is_connected`, whether an
  audio player thread exists and whether it is *alive*, its loop counter,
  RTP sequence/timestamp, latency, SSRC, and the identities of the voice
  websocket and UDP socket (so a silent socket swap is visible).

Two conditions escalate to WARNING:

- `SILENT ABORT` — `is_playing()` reports True while the player thread is
  dead. discord.py cannot detect this state; it means audio has stopped for
  good.
- `PLAYER PARKED` — the player thread is alive but its loop counter has not
  advanced, i.e. it is blocked, most likely inside the audio device read.

A callback is also attached to playback so that the player thread logs its
own exit, including the case where it exits with no error at all.

Diagnostics cost roughly 0.2 µs per audio read against a 20 ms budget, and
log one summary line per five seconds rather than per read.
