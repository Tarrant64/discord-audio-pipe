"""Diagnostic probes for the DAP silent-audio-stall bug.

Observation only -- nothing in here attempts to fix, restart, reconnect or
work around the stall. If it ever starts doing that, it stops being
evidence.

Collection vs. logging
----------------------

These are two different things and they are gated separately:

* **Collection is always on.** The measured ``read()`` and the voice-state
  poller run on every session, because the GUI status strip needs live
  numbers all the time -- a health readout that only works when you
  remember to pass a flag is a health readout nobody has when they need it.
  Measured cost is 0.22 us per ``read()`` against a 20 000 us budget
  (0.001%), on CPython 3.14 against a fake PortAudio stream.
* **Verbose logging is on only with ``--diagnose``** (:data:`VERBOSE`).
  That flag controls the per-5-second summary lines written to
  ``DAP_session.log``, and nothing else.

The three WARNING conditions -- SILENT ABORT, PLAYER PARKED, and the
THREAD EXIT callback built by :func:`make_after` -- are deliberately *not*
behind the flag. They fire at most once per episode, they are the whole
reason this module exists, and a user who hits the bug without
``--diagnose`` should still end up with the evidence in their log.
``make_after`` was gated until the 26-minute capture that had neither
``-v`` nor ``--diagnose`` proved the point; its docstring records the
reasoning and the one behavioural difference, which is compensated for.

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
formatting, no logging, no locks, no collections.

Two guarded bodies hang off single float comparisons: one snapshot dict
built per ~50 calls (1 s) for the status strip, and one summary line
emitted per ~250 calls (5 s) when ``--diagnose`` is on.

Measured against a fake PortAudio stream on CPython 3.14: 122 ns/call for
``sound.PCMStream.read``, 344 ns/call for this override -- 222 ns of
overhead, 0.001% of the 20 ms budget per frame. Turning ``--diagnose`` on
moves that to 223 ns, i.e. the log line does not register.

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
SUMMARY_INTERVAL = 5.0  # cadence of the verbose diagnostic log lines
PUBLISH_INTERVAL = 1.0  # cadence of the live snapshot the status strip reads

log = logging.getLogger("dap.diag")

# Set by ``main.pyw`` when ``--diagnose`` is passed. Gates *logging verbosity
# only* -- metric collection runs regardless. See the module docstring.
VERBOSE = False


def enable():
    """Turn on the per-5s diagnostic log lines (``--diagnose``)."""
    global VERBOSE
    VERBOSE = True
    log.info("verbose diagnostics enabled (--diagnose)")

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


def dead_probes():
    """Names of probes retired this run. Read-only copy."""
    with _dead_lock:
        return frozenset(_dead_probes)


# ---------------------------------------------------------------------------
# live metric snapshot
# ---------------------------------------------------------------------------
#
# Threading model, because getting this wrong is how you turn a health
# readout into the thing that breaks audio:
#
#   * ``InstrumentedPCMStream.read()`` runs on discord.py's player thread.
#     Once per second it *builds a fresh dict* and rebinds one attribute
#     (``self.published``). It never takes a lock, never blocks, never
#     touches Qt.
#   * ``VoiceStatePoller.poll_once()`` runs on the asyncio event loop and
#     does the same to its own ``published``.
#   * The Qt status strip calls :func:`snapshot`, which copies two short
#     lists and reads those already-built dicts.
#
# A single attribute rebind is atomic under the GIL, so the reader either
# sees the previous complete dict or the next complete dict -- never a
# half-populated one. No lock is needed on the hot path, which is the whole
# point: the audio thread must never wait on the GUI.

_streams = []   # every InstrumentedPCMStream created (one per connection row)
_pollers = []   # VoiceStatePollers currently attached
_registry_lock = threading.Lock()


def _register_stream(stream):
    with _registry_lock:
        _streams.append(stream)


def _register_poller(poller):
    with _registry_lock:
        _pollers.append(poller)


def _unregister_poller(poller):
    with _registry_lock:
        try:
            _pollers.remove(poller)
        except ValueError:
            pass


def reset_registry():
    """Test helper: forget every registered stream and poller."""
    with _registry_lock:
        _streams.clear()
        _pollers.clear()


def _worst(values, key=None):
    """Max of the non-None values, or None if there are none."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return max(vals, key=key) if key else max(vals)


def snapshot():
    """Aggregate every active stream and voice connection into one dict.

    Safe to call from the Qt thread at any cadence; it allocates a couple of
    small lists and does no I/O, no formatting and no probing.

    Every numeric field is ``None`` when it is genuinely unknown -- no
    connection yet, or the underlying probe has been retired because
    discord.py moved its internals. Callers must render ``None`` as "--"
    and must never substitute a previous value, because a stale number
    presented as live is worse than no number at all.
    """
    now = time.perf_counter()

    with _registry_lock:
        streams = list(_streams)
        pollers = list(_pollers)

    audio = [s.published for s in streams if s.published is not None]
    voice = [p.published for p in pollers if p.published is not None]

    out = {
        "now": now,
        "connections": len(pollers),
        "voice_samples": len(voice),
        "audio_samples": len(audio),
        "connected": None,
        "playing": None,
        "paused": None,
        "stalled": False,
        "parked": False,
        "latency_ms": None,
        "avg_latency_ms": None,
        "uptime_s": None,
        "voice_age_s": None,
        "drops": None,
        "drift_ppm": None,
        "drift_s": None,
        "read_ms_max": None,
        "read_ms_mean": None,
        "reads": None,
        "ring": None,
        "audio_age_s": None,
        "dead_probes": dead_probes(),
    }

    if voice:
        # "Worst wins" across rows: a health strip that shows the healthy
        # connection while another one is dead is actively misleading.
        out["connected"] = all(v["connected"] is True for v in voice)
        out["playing"] = all(v["playing"] is True for v in voice)
        out["paused"] = any(v["paused"] is True for v in voice)
        out["stalled"] = any(v["stalled"] for v in voice)
        out["parked"] = any(v["parked"] for v in voice)
        out["latency_ms"] = _worst(v["latency_ms"] for v in voice)
        out["avg_latency_ms"] = _worst(v["avg_latency_ms"] for v in voice)
        out["uptime_s"] = now - min(v["since"] for v in voice)
        out["voice_age_s"] = now - min(v["t"] for v in voice)

    if audio:
        out["drops"] = sum(a["drops"] for a in audio)
        out["reads"] = sum(a["reads"] for a in audio)
        out["drift_ppm"] = _worst((a["drift_ppm"] for a in audio), key=abs)
        out["drift_s"] = _worst((a["drift_s"] for a in audio), key=abs)
        out["read_ms_max"] = _worst(a["read_ms_max"] for a in audio)
        out["read_ms_mean"] = _worst(a["read_ms_mean"] for a in audio)
        out["ring"] = _worst(a["ring"] for a in audio)
        out["audio_age_s"] = now - min(a["t"] for a in audio)

    return out


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

    def __init__(self, interval=SUMMARY_INTERVAL, logger=None,
                 publish_interval=PUBLISH_INTERVAL):
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

        # live snapshot for the GUI status strip. Rebound wholesale, never
        # mutated in place -- see the threading note above.
        self.published = None
        self._pub_interval = publish_interval
        self._pub_next = time.perf_counter() + publish_interval
        self._pub_max = 0.0

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
        if dt > self._pub_max:
            self._pub_max = dt
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

        # two float compares per call; the bodies run ~once per 50 and ~once
        # per 250 calls respectively
        if t1 >= self._pub_next:
            self._publish(t1)
        if t1 - self._w_start >= self._interval:
            self._emit(t1)

        return data

    # -- 1 s live snapshot --------------------------------------------------

    def _publish(self, now):
        """Rebind ``self.published`` to a freshly built dict.

        Runs on the player thread. One dict allocation per second, no
        formatting, no locks, no logging.
        """
        audio_seconds = self._frames_total / SAMPLE_RATE
        wall_seconds = now - self._wall_start if self._wall_start else 0.0
        divergence = audio_seconds - wall_seconds

        self.published = {
            "t": now,
            "reads": self._reads_total,
            # "drops" is the honest input-side dropout count: PortAudio told
            # us the ring overflowed (samples were discarded before we got
            # them), or handed back an empty buffer (which discord.py reads
            # as end-of-source and silently stops the player).
            "drops": self._overflow_total + self._empty_total,
            "overflows": self._overflow_total,
            "empties": self._empty_total,
            "drift_s": divergence,
            "drift_ppm": (
                (divergence / wall_seconds * 1e6) if wall_seconds > 0 else 0.0
            ),
            "audio_s": audio_seconds,
            "wall_s": wall_seconds,
            # max blocking time since the *previous publish*, so the strip
            # reacts within a second rather than inheriting a 5 s window
            "read_ms_max": self._pub_max * 1000.0,
            "read_ms_mean": (
                (self._w_sum / self._w_n * 1000.0) if self._w_n else 0.0
            ),
            "ring": self._ring_last if self._ring_ok else None,
        }

        self._pub_max = 0.0
        self._pub_next = now + self._pub_interval

    # -- 5 s summary --------------------------------------------------------

    def _emit(self, now):
        n = self._w_n or 1
        window = now - self._w_start

        audio_seconds = self._frames_total / SAMPLE_RATE
        wall_seconds = now - self._wall_start
        divergence = audio_seconds - wall_seconds
        ppm = (divergence / wall_seconds * 1e6) if wall_seconds > 0 else 0.0

        if not VERBOSE:
            # collection still happened; only the log line is suppressed
            self._ring_max = -1
            self._reset_window(now)
            return

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
            "audio stream (re)opened on device index=%s samplerate=%s",
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

        # live snapshot for the GUI status strip; see the threading note at
        # the top of this module
        self.published = None
        self.since = time.perf_counter()

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

        # -- classify before logging, so the status strip and the log agree --

        stalled = s["is_playing"] is True and s["alive"] is False
        parked = (
            s["alive"] is True
            and s["is_playing"] is True
            and s["loops"] is not None
            and self._last_loops is not None
            and s["loops"] == self._last_loops
        )

        self.published = {
            "t": time.perf_counter(),
            "since": self.since,
            "label": self.label,
            "connected": s["is_connected"],
            "playing": s["is_playing"],
            "paused": s["is_paused"],
            "alive": s["alive"],
            "loops": s["loops"],
            "stalled": stalled,
            "parked": parked,
            # discord.py reports voice latency in seconds; the strip wants ms
            "latency_ms": (
                s["latency"] * 1000.0 if isinstance(s["latency"], float) else None
            ),
            "avg_latency_ms": (
                s["average_latency"] * 1000.0
                if isinstance(s["average_latency"], float)
                else None
            ),
        }

        if VERBOSE:
            def f(x):
                return "n/a" if x is None else (
                    f"{x:.4f}" if isinstance(x, float) else x
                )

            self._log.info(
                "voice%s: playing=%s paused=%s connected=%s player=%s alive=%s"
                " loops=%s seq=%s ts=%s lat=%s avg_lat=%s ssrc=%s ws=%s sock=%s",
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
        #     NOT gated on VERBOSE -- this is the bug we are hunting, and a
        #     user who hits it without --diagnose should still have proof.
        if stalled:
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
        if parked:
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
    """Build an ``after=`` callback for ``VoiceClient.play()``. Always on.

    ``AudioPlayer.run`` calls this from a ``finally``, so it fires on
    *every* exit path -- including the bare ``return`` at player.py:819,
    where ``error`` is None. That death is otherwise completely invisible:
    it is the single most direct evidence of player-thread termination, and
    the only one that is instantaneous rather than inferred.

    Why this stopped being gated on ``--diagnose``
    ----------------------------------------------

    The earlier call was to keep the connect path byte-identical to upstream
    unless diagnosing, because unlike the passive read/poll probes this one
    changes what gets handed to ``VoiceClient.play()``. Reconsidered, and
    reversed, for three reasons.

    1. **The behavioural delta is one branch, and we cover it.**
       ``AudioPlayer._call_after`` is::

            if self.after is not None:
                try: self.after(error)
                except Exception as exc: _log.exception('Calling the after function failed.', ...)
            elif error:
                _log.exception('Exception in voice thread %s', self.name, exc_info=error)

       Passing a non-None ``after`` costs exactly upstream's ``elif``
       branch. So this callback re-logs the error itself, with ``exc_info``,
       at ERROR -- the traceback still reaches ``DAP_errors.log``, with
       strictly more context than upstream's line carried. Nothing is lost.

    2. **The gated version was evidence we would never have.** The 26-minute
       capture that motivated this hardening pass had neither ``-v`` nor
       ``--diagnose``. A probe that fires only when someone remembered a
       flag is not a probe.

    3. **The poller is not a substitute.** ``VoiceStatePoller`` infers a
       dead thread up to 5 seconds later, and only while the connection
       object is still around to poll. This fires synchronously, in the
       dying thread, with its name -- which is what correlates the death
       against the gateway trail in ``discord.log``.

    Cost is one Python call per player-thread exit, i.e. a handful per
    session, on a thread that is already finished. It is wrapped so it can
    never raise inside discord.py's ``finally``.
    """

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
                    " client.timeout. Check DAP_session.log for a"
                    " 'PLAYER ABORT:' verdict line at the same timestamp,"
                    " which distinguishes the two.",
                    label,
                )
            elif isinstance(error, BaseException):
                # Replaces the _log.exception() in _call_after's elif branch,
                # which passing a non-None after() skips. Same traceback,
                # more context, and it lands in DAP_errors.log too.
                log.error(
                    "player%s: exited with an exception in the voice thread",
                    label,
                    exc_info=error,
                )
        except Exception:
            # never let instrumentation raise inside discord.py's finally
            pass

    return after


# ---------------------------------------------------------------------------
# wiring helper used by gui.py / cli.py
# ---------------------------------------------------------------------------


def make_stream():
    """Return the instrumented stream and register it for :func:`snapshot`.

    Always instrumented now: the status strip needs live numbers on every
    session, and the measured cost of the override is 0.22 us per
    ``read()`` against a 20 000 us budget. ``--diagnose`` only decides
    whether the 5-second summary lines are written.
    """
    stream = InstrumentedPCMStream()
    _register_stream(stream)
    return stream


def attach(voice, label="", previous=None):
    """Start a :class:`VoiceStatePoller` for ``voice``.

    ``previous`` (the poller from an earlier connect on the same UI row) is
    cancelled first, so reconnecting does not accumulate poller tasks.
    Returns None if there is nothing to poll or the task could not start --
    the strip renders that as "no data", never as "healthy".
    """
    detach(previous)

    if voice is None:
        return None

    poller = VoiceStatePoller(voice, label=label)
    try:
        poller.start()
    except Exception:
        log.exception("failed to start voice poller%s", label)
        return None

    _register_poller(poller)
    return poller


def detach(poller):
    """Cancel a poller returned by :func:`attach`. Always returns None."""
    if poller is not None:
        _unregister_poller(poller)
        try:
            poller.stop()
        except Exception:
            log.exception("failed to stop voice poller")
    return None
