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
  -v, --verbose         Echo logs to the console and enable DEBUG for DAP's
                        own logger. discord.log is written either way.
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

## Connecting and disconnecting

> ### ⚠️ Behaviour change: picking a channel no longer joins it
>
> **In older builds, choosing a channel from the dropdown joined it
> immediately.** That is gone. Selecting a device, a server and a channel now
> only *stages* what you want; nothing enters voice until you press
> **Connect**.
>
> If you have used this app before, this is the one thing that will look
> broken and is not: pick your channel as usual, then press the Connect
> button that now sits at the end of the row.

Each connection row has its own **Connect** button. It is one button, not
two: the label is always the next action, and the colour is always the
current state.

| The row is | The button says | and looks |
|---|---|---|
| idle, nothing chosen yet | `Connect`, disabled | grey; the tooltip names what is still missing |
| idle, ready to go | `Connect` | grey outline, green on hover |
| joining | `Connecting…`, disabled | amber |
| **in voice** | `Disconnect` | **solid green — on air** |
| leaving | `Leaving…`, disabled | amber |
| the last attempt failed | `Retry` | red outline; the tooltip says why |

Connect is disabled until all three dropdowns are set. Hover it to see which
of the device, the server and the channel is still missing.

**Disconnect keeps your selections.** Leaving the channel does not clear the
dropdowns and does not close the audio device, so pressing Connect again
rejoins the same channel in one click. This is the intended way to hop out of
voice for a minute.

**While a row is in voice, its server and channel are locked.** Leave first
if you want to move. The *device* dropdown stays live, so you can still
switch input mid-broadcast — the stream is re-plugged into the running
player without dropping the connection.

**While a row is joining or leaving, nothing on it can be pressed.** A join
cannot be cancelled mid-flight and a second click cannot start a second
join; discord.py's own 10-second timeout bounds how long the wait can last,
after which the row lands in `Retry` rather than sitting there forever.

**Rows are independent.** One row failing to join says nothing about any
other. The status strip shows the most urgent state across all of them: a
failure outranks everything, and a live row outranks another row that is
still connecting, so a running stream's health numbers stay on screen.

**Mute** is only available while the row is in voice, and it is now a
latched button: muted is amber and reads `Resume`, live is indigo and reads
`Mute`. Disconnecting un-mutes, so the button can never claim a mute that is
not in effect.

### Under the hood

All of the above is one small state machine per row, in `connection_state.py`
— no Qt, no discord.py, so its rules are unit-testable on their own:

```
IDLE ──connect──▶ CONNECTING ──joined──▶ LIVE ──disconnect──▶ DISCONNECTING
  ▲                    │                   │                       │
  │                    └── error/timeout ──┴──▶ FAILED             │
  │                                              │                 │
  └──── selection changed ───────────────────────┴─────────────────┘
                                          FAILED ──connect──▶ CONNECTING
```

Every transition not in that table is *refused* — it returns false and logs,
it does not raise, because under PyQt6 an exception escaping a slot calls
`qFatal()` and aborts the process. Disabling the buttons is the first line of
defence; the machine refusing the request is the second.

The button labels, the button colours, which dropdowns are editable, whether
mute does anything, what the status strip says, and whether auto-connect may
fire are all derived from that one value. They used to be four separate
guesses — `voice is not None`, `voice.is_connected()`, `voice.is_playing()`,
and the strip's own count of instrumentation samples — which could and did
disagree. In particular **`is_playing()` is no longer trusted anywhere**: it
keeps returning true forever once the audio player thread has died, so it
cannot be used to decide whether anything is actually connected or playing.

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
| state word | `Idle`, `Connecting`, `Starting`, `Live`, `Degrading`, `Stalled`, `Disconnected`, `Muted`, `Disconnecting`, `Failed` |
| latency | Voice websocket round-trip reported by discord.py, in ms. |
| drops | Cumulative input dropouts: PortAudio ring overflows plus empty reads. |
| drift | Audio frames actually read versus elapsed wall time, in ppm. Positive means the stream is running ahead of the clock, negative means it is falling behind. |
| uptime | Time since the voice connection was established. |

`Idle`, `Connecting`, `Disconnecting` and `Failed` come straight from the
connection state machine described above — the strip does not form its own
opinion about whether the app is in voice, it reads the same value the
buttons do. The health metrics are only consulted once a row is actually
live, because only then is there a stream to measure.

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

### Auto-connect checkbox

**Off by default.** Your last-used setup is saved and pre-filled into the
dropdowns whether this is ticked or not — the checkbox only controls whether
the app turns that restored selection into an actual voice connection on
launch, without being asked. Joining is audible to everyone in the channel,
so it is opt-in.

When it is on, auto-connect presses the Connect button for you: it goes
through exactly the same code as a click, so it obeys the same rules. An
incomplete restored row is refused rather than half-joined, and an
auto-connect that fails leaves that row showing `Retry` instead of hanging.

## Remembering your last setup

Whenever you change a dropdown, the app records that row's audio device,
server, channel and mute state in `DAP_config.json`, and restores them the
next time it starts. Multiple connection rows (the `＋` button) are all
saved, in order.

Two details are worth knowing:

**The audio device is remembered by name, not by number.** PortAudio hands
out device indices in enumeration order, and that order changes when you
plug in a headset, update a driver, or sometimes just reboot. Storing the
index would eventually select a *different* device — and the next thing the
app does with a device is stream it into a voice channel, so that could mean
broadcasting a live microphone. The name is matched exactly, or not at all:
if the saved device is not present, the row starts blank and says so in
`DAP_session.log`. It is never resolved to a nearby device.

**Servers and channels are remembered by id.** Those ids are stable, so a
renamed channel is still found. A channel that was deleted, or that the bot
can no longer see, is not — the row keeps whatever still resolves, leaves
the rest blank, and logs one line saying which part was dropped and why.

Restoring happens in two phases, because the server list does not exist
until the bot has finished logging in: devices are restored immediately, and
servers/channels once the client is ready.

**Restoring is not connecting.** The saved selection is put back into the
dropdowns and stops there; the app joins voice only if *Auto-connect on
launch* is ticked. This is the same rule as for a selection you make by
hand — choosing a channel stages it, pressing Connect joins it.

## Settings file

`DAP_config.json` is written next to `main.pyw` / the `.exe`, alongside
`token.txt`. It holds the `auto_recover` and `auto_connect` preferences and
the saved `profile` rows, and is **created with defaults on first launch**,
so it is there to find and hand-edit even if you have never changed a
setting.

It is written atomically (temp file plus rename), so a crash mid-save cannot
corrupt it. A missing, unreadable or malformed file is not an error: the
defaults are used, one warning is logged, and the app starts normally. Keys
the build does not recognise are preserved rather than discarded, so the
file survives moving between versions. **The bot token is never stored
there** — it lives in `token.txt` and nowhere else, and a token key
hand-added to the settings file is ignored and stripped on the next write.
That check is recursive: because profile rows are nested objects, a secret
buried inside a container is rejected too, and each row is rebuilt from a
fixed field whitelist rather than round-tripped.

`DAP_config.json` is listed in `.gitignore` and must stay there.

## Crash safety: exceptions in Qt slots

Under PyQt5, an exception escaping a Python slot back into Qt was printed
and the app carried on. **PyQt6 calls `qFatal()` instead**, which aborts the
process immediately — no Python traceback, nothing flushed to any log file,
just a native "Abort trap: 6". The PyQt5 → PyQt6 migration therefore turned
every unguarded slot into a potential silent crash.

**The fix is `gui.guarded_slot`.** It wraps a method in try/except +
`logging.exception` and is applied to every method connected to a Qt signal
and to the reimplemented virtuals Qt calls directly (`paintEvent`,
`resizeEvent`, `closeEvent`, the mouse handlers). The exception never
escapes, so `qFatal` is never reached — on any build. `sizeHint` and
`minimumSizeHint` must hand a `QSize` back to C++, so they keep a
hand-written guard with Qt's own hint as the fallback.

A test walks `gui.py`, collects every `.connect(self.…)` target, and fails
if any of them is undecorated, so the guarantee does not depend on someone
remembering.

`main.pyw` also installs a `sys.excepthook`
(`logging_setup.install_excepthook`). **That records crashes; it does not
prevent them.** `pyqt6_err_print()` runs the hook and then calls `qFatal()`
anyway on at least some builds. Measured with a slot raising `RuntimeError`
from `setCurrentIndex()`:

| slot | `sys.excepthook` | outcome |
|---|---|---|
| unguarded | default | exit 134 (SIGABRT) |
| unguarded | replaced | exit 0 on PyQt6 6.11.0/macOS; crash reports from another machine show `qFatal` reached anyway |
| **guarded** | either | **exit 0, traceback in `DAP_errors.log`** |

The middle row is exactly why the hook is not the safety mechanism. Keep it
for the traceback; rely on the decorator for survival.

One gotcha the decorator introduces: PyQt normally inspects a slot's arity
and passes only as many signal arguments as it accepts. The wrapper takes
`*args`, so every argument is passed instead. A slot wired to `clicked`
(which carries `checked`) or to `Dropdown.changed` (two arguments) must
accept them, or it raises `TypeError` — which the decorator then swallows,
leaving a control that silently does nothing. There is a test that fires
every slot and asserts none of them logged.

## Logging and diagnostics

Three log files are written next to `main.pyw`, **all three unconditionally**.
All three rotate, so they can no longer grow without bound.

| File | When | Rotation | Contents |
|---|---|---|---|
| `DAP_errors.log` | always | 2 MB × 4 | ERROR and above; unhandled exceptions from the app. |
| `DAP_session.log` | always | 2 MB × 4 | INFO lifecycle trail: start (with versions and redacted args), device selection, channel joins, disconnects, profile restore decisions, and shutdown *with its cause*. Also carries discord.py voice diagnostics promoted from DEBUG to WARNING — most importantly `Aborting playback` — plus the `PLAYER ABORT:` verdict described below. |
| `discord.log` | always | 4 MB × 5 | Full DEBUG firehose from the `discord` logger: gateway and voice websocket frames, close codes, handshakes, heartbeats, DAVE/MLS transitions. |

### Why `discord.log` is no longer behind `-v`

Upstream wrote this file only with `-v`. A 26-minute user session was
captured and analysed without the flag, and the capture contained **zero**
gateway evidence — no voice websocket close codes (4006/4014/4015/4017), no
`Disconnected from voice`, no handshake or heartbeat trail. Had the stall
fired during those 26 minutes, nothing would have been learned. Forensic
logging that exists only when someone remembered a flag is forensic logging
nobody has when they need it.

The cost was measured, not guessed. A 45-minute real-audio capture is a
~41 KB connect burst followed by a flat **~5.1 KB/min** steady state
(38–40 lines a minute, almost all heartbeat pairs) — 306 KB/hour. At 4 MB
per file the *active* file holds ~13 hours of continuous connection, so an
ordinary evening session is one contiguous file with no rollover to
reassemble; 4 backups cap the whole set at 20 MB.

It also **appends rather than truncating**, which upstream's `mode="w"` did
not. The stall's signature is that audio dies while the app survives, so the
user's response is to restart — and truncate-on-start would wipe the gateway
trail of the failed session at exactly that moment.

`-v` is therefore no longer a gate on this file, but it is not a no-op
either. It echoes the whole log stream to the console (useful for a CLI run
or a terminal-launched build) and lowers the `dap` namespace to DEBUG, in
both the logger and the session-log filter.

### `PLAYER ABORT:` — telling a real stall from an ordinary disconnect

Seeing `Aborting playback` on its own proves nothing, because a deliberate
`disconnect()` reaches the same line in discord.py as the bug does. The
discriminator is the *gap* between two messages:

```
if not client.is_connected():
    _log.debug('Not connected, waiting for %ss...', client.timeout)
    connected = client.wait_until_connected(client.timeout)   # Event.wait(timeout)
    if self._end.is_set() or not connected:
        _log.debug('Aborting playback')
        return                                                # _end never set
```

- **Real stall** — nothing ever sets the event, so `Event.wait` returns only
  when the full timeout expires. Measured: **10.005 s**.
- **Benign teardown** — `disconnect()` calls `stop()` and then pulses the
  event (`self._connected.set(); self._connected.clear()`, commented *"Flip
  the connected event to unlock any waiters"*), releasing the wait at once.
  Measured: **0.035 s**.

DAP measures that gap and writes an explicit verdict to `DAP_session.log`:

```
ERROR [dap.player] PLAYER ABORT: 10.002s gap vs timeout=10.0s (100%) -> REAL STALL. …
INFO  [dap.player] player abort: 0.028s gap vs timeout=10.0s (0%) -> benign teardown. …
```

The real case is ERROR (so it also lands in `DAP_errors.log`); the benign
case is INFO, so it does not cry wolf. The timeout is **not hardcoded** — it
is read from the argument discord.py logged, which is the value it is
actually about to wait for. The cut sits at half of it: the two populations
are an `Event.set()` apart versus a full timeout expiry, and nothing lands in
between, because a reconnect that succeeds partway through logs `Reconnected,
resuming playback` and never reaches the abort line at all.

### Shutdown cause

The closing line names *why* the app stopped, which the previous
unconditional `=== DAP shutdown (clean) ===` could not:

```
=== DAP shutdown (window-close-button) after 1543.2s ===
=== DAP shutdown (keyboard-interrupt) after 61.4s | exc=KeyboardInterrupt() ===
=== DAP shutdown (unhandled-exception) after 92.1s | exc=RuntimeError(…) ===
```

Reasons: `window-closed`, `window-close-button`, `keyboard-interrupt`,
`asyncio-cancelled`, `discord-client-exit`, `discord-login-failed`,
`no-token-file`, `error-in-main`, `unhandled-exception`, or `unattributed`
if the process unwound through a path nobody instrumented — itself a
finding. The first site to record a cause wins, so the specific trigger (the
user clicked close) is not overwritten by the generic one that follows it.

Every `DAP_session.log` line carries the writing process's pid
(`… pid=12345 INFO …`). The file is opened by path in the working
directory, so two builds launched from the same folder — exactly what
happens when testing a new `.exe` next to the old one — append to the same
file and interleave. Without the pid, a stall recorded there cannot be
attributed to a process, which makes the log unable to answer the question
it exists for.

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

A third ungated probe, `THREAD EXIT`, is attached to playback so the player
thread logs its own death — including the case where it exits with no error
at all, which is the silent bare `return` above. This was behind
`--diagnose` because it changes what gets passed to `VoiceClient.play()`.
That call was reversed: passing a non-`None` `after` costs exactly one
`elif` branch in `AudioPlayer._call_after` (upstream's `_log.exception` for
a failed voice thread), and the callback re-logs that itself with
`exc_info`, so the traceback still reaches `DAP_errors.log` with more
context than before. Nothing is lost, and the probe is the only evidence of
player-thread death that is instantaneous rather than inferred up to five
seconds later by the poller.
