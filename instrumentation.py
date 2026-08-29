"""Diagnostic probes for the DAP silent-audio-stall bug.

Enabled only by ``--diagnose``. Observation only -- nothing in here
attempts to fix, restart, reconnect or work around the stall. If it ever
starts doing that, it stops being evidence.

What we are hunting
-------------------

After 15-20 minutes audio stops while the process stays alive and the bot
stays visibly connected. Three candidate mechanisms, all currently
invisible:

1. ``AudioPlayer._do_run`` returns bare at player.py:819 when a reconnect
   exceeds ``client.timeout`` (DAP passes ``timeout=10``; discord.py's
   reconnect backoff sleeps 1+3+5+7s). That ``return`` never sets
   ``_end``, so ``VoiceClient.is_playing()`` returns True forever on a
   dead thread. Ground truth is ``voice._player.is_alive()``.
   -> probe: :class:`VoiceStatePoller`, WARNING on is_playing/is_alive
      disagreement.

2. PortAudio stops clocking and ``RawInputStream.read()`` -- an unbounded
   blocking call executed on discord.py's player thread -- parks forever.
   -> probe: :class:`InstrumentedPCMStream` read_block_ms, plus
      ``_player.loops`` failing to advance between polls.

3. Clock drift: player.py:827-830 paces against the wall clock with zero
   back-pressure from PortAudio. Upstream #21 reports ~1296 ppm.
   -> probe: the drift ledger (frames read / 48000 vs elapsed wall time).

Performance contract
--------------------

``PCMStream.read()`` runs 50x/second on discord.py's player thread. The
override here does, per call: two ``perf_counter()`` calls, one
``read_available`` property read, and a fixed set of int/float attribute
updates. No allocations beyond upstream's own ``bytes(buf)``, no
formatting, no logging, no locks, no collections. One summary line is
emitted per ~250 calls (5 s), guarded by a single float comparison.

Private-attribute safety
------------------------

Every access to a discord.py private (``_player``, ``_connection``,
``.loops``) goes through :func:`_probe`, which logs a single WARNING the
first time a probe becomes unavailable and then permanently stops trying.
A discord.py upgrade that renames a private must never crash the app.
"""

import logging
import threading
import time

import sound
import logging_setup

SAMPLE_RATE = 48000
SUMMARY_INTERVAL = 5.0  # seconds

log = logging.getLogger("dap.diag")

# Set by ``main.pyw`` when ``--diagnose`` is passed. Defaults OFF: with this
# False every helper below is a no-op returning exactly what upstream would
# have used (a plain PCMStream, ``after=None``, no poller).
ENABLED = False


def enable():
    global ENABLED
    ENABLED = True
    log.info("diagnostics enabled (--diagnose)")

# ---------------------------------------------------------------------------
# defensive private-attribute access
# ---------------------------------------------------------------------------

_dead_probes = set()
_dead_lock = threading.Lock()


def _probe(name, fn, default=None):
    """Run ``fn`` (a zero-arg closure touching discord.py internals).

    On the first failure, log one WARNING naming the probe and retire it
    permanently. Subsequent calls short-circuit to ``default``.
    """
    if name in _dead_probes:
        return default
    try:
        return fn()
    except Exception as exc:
        with _dead_lock:
            if name not in _dead_probes:
                _dead_probes.add(name)
                log.warning(
                    "probe %r unavailable (%s: %s) -- disabled for this run."
                    " discord.py internals may have changed.",
                    name,
                    type(exc).__name__,
                    exc,
                )
        return default


def reset_probes():
    """Test helper: re-arm every retired probe."""
    with _dead_lock:
        _dead_probes.clear()


# ---------------------------------------------------------------------------
# audio-thread instrumentation
# ---------------------------------------------------------------------------


class InstrumentedPCMStream(sound.PCMStream):
    """``sound.PCMStream`` with a measured ``read()``.

    Subclass only -- ``sound.py`` is upstream and stays untouched. The
    override reproduces upstream's four lines of logic verbatim, with the
    ``overflowed`` flag (which upstream discards via ``[0]``) captured
    instead of thrown away.
    """

    def __init__(self, interval=SUMMARY_INTERVAL, logger=None):
        super().__init__()

        self._log = logger if logger is not None else log
        self._interval = interval

        # cumulative, for the drift ledger
        self._frames_total = 0
        self._wall_start = None

        # per-window accumulators (reset every ``interval`` seconds)
        self._reset_window(time.perf_counter())

        # sticky counters
        self._reads_total = 0
        self._overflow_total = 0
        self._empty_total = 0

        # read_available support is probed once and latched off on failure
        self._ring_ok = True
        self._ring_last = -1
        self._ring_max = -1

    def _reset_window(self, now):
        self._w_start = now
        self._w_n = 0
        self._w_sum = 0.0
        self._w_max = 0.0
        self._w_over = 0
        self._w_bytes = 0

    # -- the hot path -------------------------------------------------------

    def read(self):
        if self.stream is None:
            return

        t0 = time.perf_counter()
        buf, overflowed = self.stream.read(self.frames)
        t1 = time.perf_counter()

        dt = t1 - t0
        self._w_n += 1
        self._w_sum += dt
        if dt > self._w_max:
            self._w_max = dt
        if overflowed:
            self._w_over += 1
            self._overflow_total += 1

        self._reads_total += 1
        self._frames_total += self.frames

        if self._ring_ok:
            try:
                avail = self.stream.read_available
            except Exception:
                self._ring_ok = False
            else:
                self._ring_last = avail
                if avail > self._ring_max:
                    self._ring_max = avail

        data = bytes(buf)
        n = len(data)
        self._w_bytes += n
        if n == 0:
            # discord.py's `if not data` treats this as end-of-source and
            # silently stops the player. Count it; do not intervene.
            self._empty_total += 1

        if self._wall_start is None:
            self._wall_start = t0

        # one float compare per call; the body runs ~once per 250 calls
        if t1 - self._w_start >= self._interval:
            self._emit(t1)

        return data

    # -- 5 s summary --------------------------------------------------------

    def _emit(self, now):
        n = self._w_n or 1
        window = now - self._w_start

        audio_seconds = self._frames_total / SAMPLE_RATE
        wall_seconds = now - self._wall_start
        divergence = audio_seconds - wall_seconds
        ppm = (divergence / wall_seconds * 1e6) if wall_seconds > 0 else 0.0

        self._log.info(
            "audio: reads=%d/%.1fs read_ms max=%.2f mean=%.2f | overflow=%d(%d tot)"
            " | ring=%s max=%s | bytes=%d | drift=%+.3fs (%+.0f ppm)"
            " audio=%.1fs wall=%.1fs | empty_reads=%d",
            self._w_n,
            window,
            self._w_max * 1000.0,
            (self._w_sum / n) * 1000.0,
            self._w_over,
            self._overflow_total,
            self._ring_last if self._ring_ok else "n/a",
            self._ring_max if self._ring_ok else "n/a",
            self._w_bytes,
            divergence,
            ppm,
            audio_seconds,
            wall_seconds,
            self._empty_total,
        )

        self._ring_max = -1
        self._reset_window(now)

    # -- read-only view, for tests and the voice poller ---------------------

    def stats(self):
        wall = (time.perf_counter() - self._wall_start) if self._wall_start else 0.0
        audio = self._frames_total / SAMPLE_RATE
        return {
            "reads_total": self._reads_total,
            "frames_total": self._frames_total,
            "overflow_total": self._overflow_total,
            "empty_total": self._empty_total,
            "audio_seconds": audio,
            "wall_seconds": wall,
            "drift_seconds": audio - wall,
            "drift_ppm": ((audio - wall) / wall * 1e6) if wall > 0 else 0.0,
            "window_reads": self._w_n,
            "window_max_ms": self._w_max * 1000.0,
            "window_mean_ms": (self._w_sum / self._w_n * 1000.0) if self._w_n else 0.0,
            "ring_last": self._ring_last if self._ring_ok else None,
        }

    def change_device(self, num):
        super().change_device(num)
        logging_setup.log_event(
            "diag: stream (re)opened on device index=%s samplerate=%s",
            num,
            _probe("stream.samplerate", lambda: self.stream.samplerate, "?"),
        )


# ---------------------------------------------------------------------------
# voice-state polling
# ---------------------------------------------------------------------------


class VoiceStatePoller:
    """Async 5 s poll of a ``discord.VoiceClient``'s observable state.

    Two conditions are escalated to WARNING because each is a signature of
    a distinct failure mode:

    * ``is_playing() is True`` while ``_player.is_alive() is False``
      -- the player thread returned bare out of ``_do_run`` without
      setting ``_end``. This is the silent-abort path.
    * ``_player.loops`` unchanged across a poll while the thread is still
      alive -- the player thread is parked, almost certainly inside the
      unbounded ``RawInputStream.read()``.
    """

    def __init__(self, voice, label="", interval=SUMMARY_INTERVAL, logger=None):
        self.voice = voice
        self.label = label
        self.interval = interval
        self._log = logger if logger is not None else log

        self._last_loops = None
        self._last_sequence = None
        self._stall_reported = False
        self._park_reported = False
        self._task = None

    # -- one poll, sync and testable ---------------------------------------

    def sample(self):
        v = self.voice

        is_playing = _probe("is_playing", v.is_playing, None)
        is_paused = _probe("is_paused", v.is_paused, None)
        is_connected = _probe("is_connected", v.is_connected, None)

        player = _probe("_player", lambda: v._player, None)
        has_player = player is not None
        alive = _probe("_player.is_alive", player.is_alive, None) if has_player else None
        loops = _probe("_player.loops", lambda: player.loops, None) if has_player else None

        conn = _probe("_connection", lambda: v._connection, None)
        ws_id = _probe("_connection.ws", lambda: id(conn.ws), None) if conn else None
        sock_id = (
            _probe("_connection.socket", lambda: id(conn.socket), None) if conn else None
        )

        return {
            "is_playing": is_playing,
            "is_paused": is_paused,
            "is_connected": is_connected,
            "has_player": has_player,
            "alive": alive,
            "loops": loops,
            "sequence": _probe("sequence", lambda: v.sequence, None),
            "timestamp": _probe("timestamp", lambda: v.timestamp, None),
            "latency": _probe("latency", lambda: v.latency, None),
            "average_latency": _probe("average_latency", lambda: v.average_latency, None),
            "ssrc": _probe("ssrc", lambda: v.ssrc, None),
            "ws_id": ws_id,
            "socket_id": sock_id,
        }

    def poll_once(self):
        s = self.sample()

        def f(x):
            return "n/a" if x is None else (f"{x:.4f}" if isinstance(x, float) else x)

        self._log.info(
            "voice%s: playing=%s paused=%s connected=%s player=%s alive=%s loops=%s"
            " seq=%s ts=%s lat=%s avg_lat=%s ssrc=%s ws=%s sock=%s",
            self.label,
            s["is_playing"],
            s["is_paused"],
            s["is_connected"],
            s["has_player"],
            s["alive"],
            f(s["loops"]),
            f(s["sequence"]),
            f(s["timestamp"]),
            f(s["latency"]),
            f(s["average_latency"]),
            f(s["ssrc"]),
            f(s["ws_id"]),
            f(s["socket_id"]),
        )

        # (1) silent-abort signature: discord.py thinks it is playing, the
        #     thread that would be doing the playing is gone.
        if s["is_playing"] is True and s["alive"] is False:
            if not self._stall_reported:
                self._stall_reported = True
                self._log.warning(
                    "voice%s: SILENT ABORT -- is_playing()=True but"
                    " _player.is_alive()=False. The player thread returned"
                    " without setting _end (discord/player.py:819, reconnect"
                    " exceeded client.timeout). Audio is dead; discord.py will"
                    " not notice. loops=%s seq=%s connected=%s",
                    self.label,
                    s["loops"],
                    s["sequence"],
                    s["is_connected"],
                )
        else:
            self._stall_reported = False

        # (2) park signature: thread alive, loop counter frozen.
        if (
            s["alive"] is True
            and s["is_playing"] is True
            and s["loops"] is not None
            and self._last_loops is not None
            and s["loops"] == self._last_loops
        ):
            if not self._park_reported:
                self._park_reported = True
                self._log.warning(
                    "voice%s: PLAYER PARKED -- _player.loops frozen at %s across"
                    " a %.0fs poll while the thread is alive. The player thread"
                    " is blocked, most likely inside RawInputStream.read()"
                    " (unbounded, no timeout). seq=%s connected=%s",
                    self.label,
                    s["loops"],
                    self.interval,
                    s["sequence"],
                    s["is_connected"],
                )
        else:
            self._park_reported = False

        self._last_loops = s["loops"]
        self._last_sequence = s["sequence"]
        return s

    # -- async driver -------------------------------------------------------

    async def run(self):
        import asyncio

        self._log.info(
            "voice%s: poller started (%.0fs interval)", self.label, self.interval
        )
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    self.poll_once()
                except Exception:
                    self._log.exception("voice%s: poll failed", self.label)
        except asyncio.CancelledError:
            raise
        finally:
            self._log.info("voice%s: poller stopped", self.label)

    def start(self):
        """Schedule :meth:`run` on the running loop. Returns the task."""
        import asyncio

        self.stop()
        self._task = asyncio.create_task(self.run())
        return self._task

    def stop(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None


# ---------------------------------------------------------------------------
# player-thread death callback
# ---------------------------------------------------------------------------


def make_after(label=""):
    """Build an ``after=`` callback for ``VoiceClient.play()``.

    Returns ``None`` when diagnostics are off, so the call site collapses to
    upstream's ``play(source, after=None)`` exactly.

    ``AudioPlayer.run`` calls this from a ``finally``, so it fires on
    *every* exit path -- including the bare ``return`` at player.py:819,
    where ``error`` is None. Right now that death is completely invisible;
    this makes it announce itself with a wall-clock timestamp and the
    thread name.
    """
    if not ENABLED:
        return None

    def after(error):
        try:
            log.warning(
                "player%s: THREAD EXIT at %s error=%r thread=%r",
                label,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                error,
                threading.current_thread().name,
            )
            if error is None:
                log.warning(
                    "player%s: exited with error=None -- consistent with the"
                    " silent paths: empty source read, or the bare return at"
                    " discord/player.py:819 after a reconnect exceeded"
                    " client.timeout.",
                    label,
                )
        except Exception:
            # never let instrumentation raise inside discord.py's finally
            pass

    return after


# ---------------------------------------------------------------------------
# wiring helper used by gui.py / cli.py
# ---------------------------------------------------------------------------


def make_stream():
    """Return an instrumented stream, or upstream's plain ``PCMStream``."""
    return InstrumentedPCMStream() if ENABLED else sound.PCMStream()


def attach(voice, label="", previous=None):
    """Start a :class:`VoiceStatePoller` for ``voice``. No-op when disabled.

    ``previous`` (the poller from an earlier connect on the same UI row) is
    cancelled first, so reconnecting does not accumulate poller tasks.
    """
    detach(previous)

    if not ENABLED or voice is None:
        return None

    poller = VoiceStatePoller(voice, label=label)
    try:
        poller.start()
    except Exception:
        log.exception("failed to start voice poller%s", label)
        return None
    return poller


def detach(poller):
    """Cancel a poller returned by :func:`attach`. Always returns None."""
    if poller is not None:
        try:
            poller.stop()
        except Exception:
            log.exception("failed to stop voice poller")
    return None
