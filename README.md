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
  --diagnose            Log verbose audio/voice-state diagnostics every 5s
                        to DAP_session.log. The metrics themselves are
                        always collected and shown in the status strip;
                        this only controls the logging.

Command Line Mode:
  -c CHANNEL, --channel CHANNEL
                        The channel to connect to as an id
  -d DEVICE, --device DEVICE
                        The device to listen from as an index

Queries:
  -D, --devices         Query compatible audio devices
  -C, --channels        Query servers and channels (requires token)
```

## Live status strip

The bar along the bottom of the window is a health readout for the stream,
refreshed every 2 seconds. It exists because audio can stop while the bot
still looks connected — the strip lets you see the stream degrading before
you hear it.

```
  ● Live · 38ms · 0 drops · drift +12ppm · 18m
```

| Field | Meaning |
|---|---|
| state word | `Live`, `Degrading`, `Stalled`, `Disconnected`, `Muted`, `Idle`, … |
| latency | Voice websocket round-trip reported by discord.py, in ms. |
| drops | Cumulative input dropouts: PortAudio ring overflows plus empty reads. |
| drift | Audio frames actually read versus elapsed wall time, in ppm. Positive means the stream is running ahead of the clock, negative means it is falling behind. |
| uptime | Time since the voice connection was established. |

The state is written out in words as well as coloured (green healthy, amber
degrading, red failing), so the strip is readable without relying on colour.
A field shows `--` when its value is genuinely unknown — no audio device
selected, or a discord.py internal that the probe can no longer reach. A
stale value is never shown as if it were live. Hover the strip for the full
numbers, including the slowest single device read and any retired probes.

### Thresholds are provisional

The green/amber/red boundaries are **guesses**, and are deliberately loose.
We have two clean captures (a 19-minute silent run and a ~30-minute music
run) and no capture at all of a stream on its way down, so there is no
measured "normal" band to key off yet. The bias is intentional: a strip that
occasionally under-warns costs one missed early warning, while a strip that
cries wolf gets ignored, and an ignored readout is worse than none at all.

Current values (all defined together at the top of `gui.py`):

| Metric | Amber at | Red at |
|---|---|---|
| voice latency | 300 ms | 1000 ms |
| clock drift | 2 000 ppm | 10 000 ppm |
| dropouts | 10 | 100 |
| slowest device read | 60 ms | 500 ms |

Red is also shown unconditionally when the voice client reports itself
disconnected, when the player thread has died while discord.py still thinks
it is playing, or when the player thread is alive but no longer looping.

To recalibrate: run with `--diagnose`, capture a session that actually
degrades, and read the 5-second lines in `DAP_session.log` from the last
clean minute through the failure. Set amber to roughly where each metric
leaves its steady-state band.

### Auto-recover checkbox

**Not implemented yet**, which is why it is labelled `(soon)`. Ticking it
saves the preference and nothing else; a stalled stream still needs a manual
reconnect. Automatic recovery is a separate change, deliberately held back
until the strip has told us which metric moves first.

## Settings file

`DAP_config.json` is written next to `main.pyw` / the `.exe`, alongside
`token.txt`. It currently holds the one `auto_recover` preference and is
created the first time a setting changes.

It is written atomically (temp file plus rename), so a crash mid-save cannot
corrupt it. A missing, unreadable or malformed file is not an error: the
defaults are used, one warning is logged, and the app starts normally. Keys
the build does not recognise are preserved rather than discarded, so the
file survives moving between versions. **The bot token is never stored
there** — it lives in `token.txt` and nowhere else, and a token key
hand-added to the settings file is ignored and stripped on the next write.

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

Metric *collection* is always on — the status strip needs it, and it costs
about 0.2 µs per audio read against a 20 ms budget (0.001%). `--diagnose`
controls only how much of that collection is written to disk.

It is observation only — it never restarts, reconnects or otherwise alters
behaviour — and is aimed at the long-standing bug where audio stops after
15–20 minutes while the bot stays connected (upstream issues #16 and #46).
Output goes to `DAP_session.log` under the `dap.diag` logger.

With the flag on, every 5 seconds it records:

- **Audio thread** — max/mean blocking time of the PortAudio `read()` call,
  the `overflowed` flag that the normal code path discards, the input ring
  buffer depth, and a drift ledger comparing frames actually read against
  elapsed wall time (reported in seconds and ppm).
- **Voice state** — `is_playing`, `is_paused`, `is_connected`, whether an
  audio player thread exists and whether it is *alive*, its loop counter,
  RTP sequence/timestamp, latency, SSRC, and the identities of the voice
  websocket and UDP socket (so a silent socket swap is visible).

Two conditions escalate to WARNING. These are **not** behind the flag — they
fire at most once per episode, they are the whole point of the
instrumentation, and someone who hits the bug without `--diagnose` should
still end up with the evidence in `DAP_session.log`:

- `SILENT ABORT` — `is_playing()` reports True while the player thread is
  dead. discord.py cannot detect this state; it means audio has stopped for
  good. The status strip shows `Stalled` for the same condition.
- `PLAYER PARKED` — the player thread is alive but its loop counter has not
  advanced, i.e. it is blocked, most likely inside the audio device read.

With `--diagnose` a callback is also attached to playback so that the player
thread logs its own exit, including the case where it exits with no error at
all. That one stays behind the flag because it changes what gets passed to
`VoiceClient.play()`, and the status strip does not need it — the voice
poller already spots a dead player thread within one poll.
