"""Central logging configuration for Discord Audio Pipe.

This module owns *all* log handler setup so that upstream files stay
merge-clean. Nothing in here changes application behaviour -- it only
changes what gets recorded and where.

Three log files, **all three always on**:

  DAP_errors.log   ERROR and above on the root logger. Upstream behaviour,
                   now size-bounded by a RotatingFileHandler.
  discord.log      full DEBUG firehose from the ``discord`` logger. Upstream
                   gated this behind ``-v``; we do not. See "Why the gateway
                   log is unconditional" below.
  DAP_session.log  NEW. INFO-level lifecycle log: start/stop (with the
                   *cause* of the stop), device selection, guild/channel
                   joins, connects, disconnects, settings/profile changes,
                   the ``--diagnose`` instrumentation output, plus the
                   *promoted* discord.py diagnostics and the abort verdict
                   described below. Every line carries the writing process's
                   pid, because two builds launched from the same directory
                   share this file.

Why the gateway log is unconditional
------------------------------------

A 26-minute user session was captured and analysed with ``-v`` absent.
``discord.log`` was stale, so the capture contained **zero** gateway
evidence: no voice websocket close codes (4006/4014/4015/4017), no
``Disconnected from voice``, no handshake or heartbeat trail, no DAVE/MLS
transitions. If the stall had fired during those 26 minutes we would have
learned nothing. Forensic logging that only exists when someone remembered
a flag is forensic logging nobody has when they need it.

Cost was measured, not guessed. A 45-minute real-audio capture from the
test VM (``vm_healthy_realaudio_discord.log``, 271 221 bytes over 46 wall
minutes) breaks down as a ~41 KB connect/handshake burst in the first
minute and then a flat **~5.1 KB/min** steady state -- 38-40 lines a
minute, almost all voice/gateway heartbeat pairs. That is 306 KB/hour, or
7.3 MB/day of continuous connection. See :data:`DEBUG_MAX_BYTES`.

The abort discriminator
-----------------------

discord.py logs its two most diagnostic voice lines at DEBUG:

    discord/player.py:814   'Not connected, waiting for %ss...'
    discord/player.py:818   'Aborting playback'

The second sits immediately before a bare ``return`` that leaves
``AudioPlayer._end`` unset, so ``VoiceClient.is_playing()`` keeps
returning True on a dead thread -- the exact silent-stall signature we are
hunting. Both are promoted into the session log at WARNING.

But seeing 'Aborting playback' is **not** on its own proof of the bug,
because a deliberate ``disconnect()`` reaches the same line. The
discriminator is the *gap* between the two messages -- see
:class:`AbortDiscriminator`, which turns it into an explicit verdict line.
"""

import logging
import logging.handlers
import os
import sys
import threading
import time

# ---------------------------------------------------------------------------
# file names / rotation policy
# ---------------------------------------------------------------------------

ERROR_LOG = "DAP_errors.log"
DEBUG_LOG = "discord.log"
SESSION_LOG = "DAP_session.log"

MAX_BYTES = 2 * 1024 * 1024  # 2 MB
BACKUP_COUNT = 3

#: Rotation policy for the now-always-on gateway log, sized from the
#: measured 5.1 KB/min steady state (see the module docstring).
#:
#:   4 MB per file    ~13 hours of continuous connection in the *active*
#:                    file alone, so an ordinary evening session -- and the
#:                    15-20 minute stall inside it -- is one contiguous
#:                    file with no rollover to reassemble.
#:   4 backups        ~65 hours retained, 20 MB hard ceiling.
#:
#: The headroom matters because a stall episode is not the idle case: a
#: voice reconnect storm logs far faster than 5 KB/min. Even at a 10x burst
#: rate the active file still holds 80 minutes, which comfortably brackets
#: the 15-20 minute failure window. 20 MB is a rounding error on any disk
#: this app runs on, and it buys a capture that cannot silently lose the
#: minutes before the abort.
DEBUG_MAX_BYTES = 4 * 1024 * 1024
DEBUG_BACKUP_COUNT = 4

SESSION_LOGGER_NAME = "dap"

# Substrings of discord.py log messages that we promote to WARNING on the
# session log. Matched against the raw ``record.msg`` format string (not the
# interpolated message) so the check is a cheap ``in`` against a constant.
WAIT_NEEDLE = "Not connected, waiting for"      # player.py:814
ABORT_NEEDLE = "Aborting playback"              # player.py:818
RESUME_NEEDLE = "Reconnected, resuming playback"  # player.py:820

PROMOTED_SUBSTRINGS = (
    ABORT_NEEDLE,                    # silent-abort path (c)
    WAIT_NEEDLE,                     # reconnect wait begins
    RESUME_NEEDLE,                   # recovery
    "A packet has been dropped",     # voice_client.py:608 - UDP send gap
)

#: ``client.timeout`` to assume when the wait record does not carry it as an
#: argument. ``gui.py`` passes ``connect(timeout=10)``; the discriminator
#: prefers the value discord.py actually logged (``record.args[0]``) and only
#: falls back to this if discord.py changes that call's signature.
CONNECT_TIMEOUT_FALLBACK = 10.0

#: Fraction of ``client.timeout`` above which an abort is judged a real
#: stall. See :class:`AbortDiscriminator` for why any value in the wide
#: empty band between the two populations works, and why half is the one
#: that needs no maintenance.
ABORT_REAL_FRACTION = 0.5

# Loggers whose records we inspect for promotion. Their level is forced to
# DEBUG so the records are emitted at all; the filter drops everything else,
# so the session log stays quiet.
PROMOTED_LOGGERS = ("discord.player", "discord.voice_client", "discord.voice_state")

_configured = False
_lock = threading.Lock()


class SessionFilter(logging.Filter):
    """Gate for the single shared session-log handler.

    Two classes of record reach that handler:

    * anything logged on the ``dap`` logger at INFO or above -- passes as-is;
    * the specific discord.py voice diagnostics listed in
      :data:`PROMOTED_SUBSTRINGS` -- passes regardless of level.

    Everything else is dropped.

    ``min_level`` is the floor applied to the ``dap`` namespace; ``-v``
    lowers it to DEBUG so our own debug chatter reaches the session log too.
    It is deliberately *not* applied to promoted records, which pass at
    whatever level discord.py logged them.
    """

    def __init__(self, min_level=logging.INFO):
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == SESSION_LOGGER_NAME or record.name.startswith(
            SESSION_LOGGER_NAME + "."
        ):
            return record.levelno >= self.min_level

        msg = record.msg
        if not isinstance(msg, str):
            return False

        for needle in PROMOTED_SUBSTRINGS:
            if needle in msg:
                return True

        return False


class AbortDiscriminator:
    """Turn 'Aborting playback' into a verdict: real stall, or teardown?

    Why a bare 'Aborting playback' proves nothing
    ---------------------------------------------

    Both the bug and an ordinary disconnect reach ``player.py:818``::

        if not client.is_connected():
            _log.debug('Not connected, waiting for %ss...', client.timeout)
            connected = client.wait_until_connected(client.timeout)
            if self._end.is_set() or not connected:
                _log.debug('Aborting playback')
                return

    ``wait_until_connected`` is ``threading.Event.wait(timeout)``. That is
    the whole discriminator:

    * **Real stall.** Nothing ever sets the event. ``Event.wait`` returns
      False only when the full timeout has elapsed, so the two records land
      ``client.timeout`` apart -- 10.0s with what ``gui.py`` passes today.
    * **Benign teardown.** ``VoiceClient.disconnect()`` calls ``stop()``
      (which sets ``_end``) and then ``VoiceConnectionState.disconnect()``,
      which does::

            # Flip the connected event to unlock any waiters
            self._connected.set()
            self._connected.clear()

      That pulse releases ``Event.wait`` immediately, so the two records
      land *sub-second* apart -- an in-process ``Event.set()`` on an
      already-waiting thread.

    Choosing the threshold
    ----------------------

    The timeout is **not** hardcoded. It is read from the wait record's own
    argument (``_log.debug('Not connected, waiting for %ss...',
    client.timeout)``), which is the value discord.py is actually about to
    wait for -- strictly better than reading ``gui.py``'s ``timeout=10``
    literal, and automatically correct if that literal ever changes or if a
    ``move_to`` uses a different one.

    The cut sits at :data:`ABORT_REAL_FRACTION` (half) of that timeout.
    Half is not tuning; it is the midpoint of an empty band. The two
    populations are an ``Event.set()`` away (microseconds to a few tens of
    milliseconds, dominated by GIL handoff) and a full timeout expiry
    (10s). Nothing produces a value in between: the one path that *would*
    -- a reconnect that succeeds partway through the wait -- logs
    'Reconnected, resuming playback' instead and never reaches the abort
    line at all. So any threshold in roughly (0.2s, timeout) classifies
    identically, and half is the choice that stays correct without
    maintenance for any timeout discord.py hands us.

    Robustness
    ----------

    Keyed by ``record.thread``, so two connection rows aborting on their own
    player threads never cross-talk. An abort with no pending wait, a wait
    with no abort, a resume that cancels a pending wait, and repeated
    wait/abort cycles across reconnects are all handled -- the unmatched
    cases produce an explicit UNKNOWN verdict rather than a wrong one, and
    the pending map is bounded so a leak cannot grow it.
    """

    #: Hard cap on the pending map. A pending entry exists only between a
    #: wait and its abort/resume on one player thread, so the real ceiling
    #: is the number of connection rows. This only bounds a pathological
    #: leak (e.g. discord.py stops logging the abort line entirely).
    MAX_PENDING = 64

    def __init__(self, fraction=ABORT_REAL_FRACTION, clock=None,
                 logger_name=SESSION_LOGGER_NAME + ".player"):
        self.fraction = fraction
        #: Monotonic by design: a wall-clock step (NTP, DST, VM resume) mid
        #: wait must not be able to manufacture or erase a 10-second gap.
        self.clock = clock if clock is not None else time.monotonic
        self.logger_name = logger_name
        self._pending = {}
        self._lock = threading.Lock()

    # -- pure classification; no logging, fully unit-testable ---------------

    def observe(self, record):
        """Feed one discord.py record in; get a verdict out, or ``None``.

        :returns: ``None``, or ``(levelno, msg, args)`` ready to hand to
            ``Logger.log``.
        """
        msg = record.msg
        if not isinstance(msg, str):
            return None

        key = getattr(record, "thread", None)

        if WAIT_NEEDLE in msg:
            timeout = CONNECT_TIMEOUT_FALLBACK
            args = getattr(record, "args", None)
            if isinstance(args, tuple) and args:
                try:
                    candidate = float(args[0])
                except (TypeError, ValueError):
                    candidate = 0.0
                if candidate > 0:
                    timeout = candidate

            with self._lock:
                if len(self._pending) >= self.MAX_PENDING:
                    self._pending.clear()
                # A second wait without an intervening abort simply
                # restarts the clock: only the most recent wait can be the
                # one this thread is sitting in.
                self._pending[key] = (self.clock(), timeout)
            return None

        if RESUME_NEEDLE in msg:
            # Reconnect succeeded. There is no abort coming for this wait.
            with self._lock:
                self._pending.pop(key, None)
            return None

        if ABORT_NEEDLE not in msg:
            return None

        with self._lock:
            entry = self._pending.pop(key, None)

        if entry is None:
            return (
                logging.WARNING,
                "PLAYER ABORT: no preceding %r record on thread %s -- cannot"
                " tell a real stall from a teardown. discord.py's player"
                " logging may have changed; check discord/player.py.",
                (WAIT_NEEDLE, key),
            )

        started, timeout = entry
        gap = self.clock() - started
        if gap < 0:
            gap = 0.0
        pct = (gap / timeout * 100.0) if timeout > 0 else 0.0

        if gap >= self.fraction * timeout:
            return (
                logging.ERROR,
                "PLAYER ABORT: %.3fs gap vs timeout=%.1fs (%.0f%%) -> REAL"
                " STALL. discord.py waited out the entire reconnect timeout"
                " and gave up at discord/player.py:818. That bare return"
                " never sets AudioPlayer._end, so VoiceClient.is_playing()"
                " stays True on a dead player thread: audio is gone and"
                " discord.py will not notice. thread=%s",
                (gap, timeout, pct, key),
            )

        return (
            logging.INFO,
            "player abort: %.3fs gap vs timeout=%.1fs (%.0f%%) -> benign"
            " teardown. The connected event was pulsed immediately"
            " (VoiceConnectionState.disconnect: 'Flip the connected event to"
            " unlock any waiters'), so this abort followed a deliberate"
            " stop/disconnect, not a failed reconnect. thread=%s",
            (gap, timeout, pct, key),
        )

    # -- side-effecting wrapper --------------------------------------------

    def note(self, record):
        """:meth:`observe` the record and emit any verdict. Never raises."""
        try:
            verdict = self.observe(record)
        except Exception:
            return None

        if verdict is None:
            return None

        level, msg, args = verdict
        try:
            logging.getLogger(self.logger_name).log(level, msg, *args)
        except Exception:
            pass
        return verdict


class PromotingHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler that re-badges promoted discord.py records at WARNING.

    The re-badge happens on a *copy* of the record, so the ``discord.log``
    handler (which is offered the same record object further up the logger
    hierarchy) still sees the original DEBUG level. Mutating the shared
    record in place would silently corrupt discord.log.

    This is also where :class:`AbortDiscriminator` is fed. Doing it in
    ``emit`` rather than in a filter means the verdict is emitted *after*
    the record that triggered it, so the session log reads in causal order.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.abort = AbortDiscriminator()
        # The verdict is logged from inside emit(), which re-enters this
        # handler through the dap logger. That is safe -- the verdict text
        # matches none of the needles, so it terminates -- but the guard
        # makes termination structural rather than a property of the
        # wording, which is the sort of thing a later edit breaks silently.
        self._local = threading.local()

    def emit(self, record):
        own = record.name == SESSION_LOGGER_NAME or record.name.startswith(
            SESSION_LOGGER_NAME + "."
        )

        if not own and record.levelno < logging.WARNING:
            record = logging.makeLogRecord(record.__dict__)
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            record.dap_promoted = True

        super().emit(record)

        if own or getattr(self._local, "busy", False):
            return

        self._local.busy = True
        try:
            self.abort.note(record)
        finally:
            self._local.busy = False


def get_logger():
    """The DAP session logger. Safe to call before :func:`configure`."""
    return logging.getLogger(SESSION_LOGGER_NAME)


def configure(verbose=False, log_dir=None, max_bytes=None, backup_count=None,
              debug_max_bytes=None, debug_backup_count=None):
    """Install every handler. Idempotent for normal (``log_dir is None``) use.

    :param verbose: mirrors upstream ``-v``. No longer gates ``discord.log``
        -- see :func:`enable_verbose` for what it does now.
    :param log_dir: write logs here instead of the CWD (tests only).
    :param max_bytes: rotation threshold override (tests only).
    :param backup_count: rotation backup count override (tests only).
    :param debug_max_bytes: ``discord.log`` rotation override (tests only).
    :param debug_backup_count: ``discord.log`` backups override (tests only).
    :returns: the ``dap`` session logger.
    """
    global _configured, _session_filter

    with _lock:
        if _configured and log_dir is None:
            return get_logger()

        maxb = MAX_BYTES if max_bytes is None else max_bytes
        backups = BACKUP_COUNT if backup_count is None else backup_count

        def path(name):
            return name if log_dir is None else os.path.join(log_dir, name)

        # -- DAP_errors.log (upstream, root logger, now rotating) -----------
        error_formatter = logging.Formatter(
            fmt="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        error_handler = logging.handlers.RotatingFileHandler(
            path(ERROR_LOG),
            maxBytes=maxb,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(error_formatter)
        logging.getLogger().addHandler(error_handler)

        # -- DAP_session.log (new) -----------------------------------------
        # ONE handler instance, shared by the ``dap`` logger and the discord
        # voice loggers. Two RotatingFileHandlers on the same path would race
        # each other during rollover, so this must stay a single object.
        # ``pid=`` is not decoration. DAP_session.log is opened by path, in
        # the working directory, with no per-process suffix, so two copies
        # launched from the same folder -- which is exactly what happens
        # while testing a new build next to the old one -- append to the same
        # file and interleave. Without the pid, a stall recorded in that file
        # cannot be attributed to a process, and the "which build did this?"
        # question the log exists to answer becomes unanswerable.
        session_formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d pid=%(process)-6d %(levelname)-7s"
                " [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        session_handler = PromotingHandler(
            path(SESSION_LOG),
            maxBytes=maxb,
            backupCount=backups,
            encoding="utf-8",
        )
        session_handler.setLevel(logging.DEBUG)  # gating is done by the filter
        session_handler.setFormatter(session_formatter)
        _session_filter = SessionFilter()
        session_handler.addFilter(_session_filter)

        session_logger = get_logger()
        session_logger.setLevel(logging.INFO)
        session_logger.addHandler(session_handler)
        session_logger.propagate = True  # ERRORs still reach DAP_errors.log

        for name in PROMOTED_LOGGERS:
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            lg.addHandler(session_handler)

        # -- discord.log (ALWAYS ON now, and rotating) ----------------------
        # Upstream hid this behind -v. See the module docstring: a stall
        # capture without gateway evidence is not a capture. ~5.1 KB/min,
        # 20 MB ceiling.
        install_gateway_log(
            path(DEBUG_LOG),
            debug_max_bytes,
            debug_backup_count,
            force=log_dir is not None,
        )

        if verbose:
            enable_verbose()

        _configured = True

    return get_logger()


# ---------------------------------------------------------------------------
# unhandled-exception safety net
# ---------------------------------------------------------------------------

_excepthook_installed = False


def install_excepthook():
    """Capture the traceback of an exception that escaped a Qt slot.

    **This records the crash. It does not prevent it.** Read that again
    before relying on it for anything.

    Under PyQt5, an exception that escaped a Python slot back into C++ was
    printed and execution continued. PyQt6 hands it to
    ``pyqt6_err_print()``, which prints it -- that is where this hook gets
    its turn -- and then calls ``qFatal()`` -> ``abort()``. On the observed
    stacks, ``qFatal`` is reached from inside ``err_print`` regardless of
    what the hook did, so a hook that "logs and returns" does not
    necessarily get to return anywhere.

    Measurements do not agree across builds, which is exactly why this is
    not the safety mechanism. A slot raising RuntimeError from
    ``QComboBox.setCurrentIndex()``:

        PyQt6 6.11.0 / Qt 6.11.0, macOS   default hook  -> exit 134 (SIGABRT)
                                          replaced hook -> exit 0, continued
        another machine's crash reports   replaced hook -> qFatal reached

    So: the hook is worth having, because a crash whose traceback reached
    DAP_errors.log is diagnosable and a native "Abort trap: 6" is not. It is
    NOT what keeps the app alive.

    What keeps the app alive is ``gui.guarded_slot``: the exception never
    escaping the slot in the first place. That works on every build, and it
    is the mechanism the profile restore depends on -- restore drives
    ``setCurrentIndex()`` on a device that may have been unplugged, a guild
    the bot may have been removed from and a channel that may have been
    deleted.

    Only covers the main thread -- worker threads go through
    ``threading.excepthook``, which is untouched.
    """
    global _excepthook_installed

    with _lock:
        if _excepthook_installed:
            return

        previous = sys.excepthook

        def handler(exc_type, value, traceback_obj):
            try:
                logging.getLogger("dap").error(
                    "unhandled exception (%s) -- caught by the safety net,"
                    " continuing; see DAP_errors.log for the traceback",
                    exc_type.__name__,
                )
                logging.getLogger().error(
                    "unhandled exception",
                    exc_info=(exc_type, value, traceback_obj),
                )
            except Exception:
                # Logging itself failed. Fall back to stderr; never re-raise
                # out of an excepthook.
                pass

            try:
                previous(exc_type, value, traceback_obj)
            except Exception:
                pass

        sys.excepthook = handler
        _excepthook_installed = True


_verbose_enabled = False
_gateway_installed = False
_session_filter = None


def install_gateway_log(filename=None, max_bytes=None, backup_count=None,
                        force=False):
    """Attach the always-on DEBUG firehose handler to the ``discord`` logger.

    Two deliberate departures from upstream:

    **Always installed, not just under ``-v``.** Rationale and measured cost
    are in the module docstring.

    **Appends; does not truncate.** Upstream opened this ``mode="w"``, which
    was defensible when the file only existed because you asked for it. Now
    that it is always on, truncating would destroy exactly the evidence we
    are collecting: the stall's signature is that audio dies while the app
    survives, so the user's response is to restart the app -- and a
    truncate-on-start handler would wipe the gateway trail of the failed
    session the moment they did. Append keeps it. Bounding is
    :class:`~logging.handlers.RotatingFileHandler`'s job, not truncation's,
    and a banner line marks each session boundary so multiple runs stay
    separable in one file.
    """
    global _gateway_installed
    if _gateway_installed and not force:
        return None

    debug_formatter = logging.Formatter(
        fmt="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
    )
    debug_handler = logging.handlers.RotatingFileHandler(
        DEBUG_LOG if filename is None else filename,
        maxBytes=DEBUG_MAX_BYTES if max_bytes is None else max_bytes,
        backupCount=(
            DEBUG_BACKUP_COUNT if backup_count is None else backup_count
        ),
        encoding="utf-8",
        mode="a",
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(debug_formatter)

    debug_logger = logging.getLogger("discord")
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.addHandler(debug_handler)

    # Session banner. Emitted straight at the handler rather than through a
    # logger, so it cannot be affected by anyone's level or propagation and
    # cannot leak into the other two files.
    try:
        debug_handler.emit(
            logging.makeLogRecord(
                {
                    "name": "dap.gateway",
                    "levelno": logging.INFO,
                    "levelname": "INFO",
                    "msg": "=== gateway log opened | pid=%d | cwd=%s ===",
                    "args": (os.getpid(), os.getcwd()),
                }
            )
        )
    except Exception:
        pass

    _gateway_installed = True
    return debug_handler


def enable_verbose(*_a, **_kw):
    """``-v`` / ``--verbose``. **No longer gates ``discord.log``.**

    ``discord.log`` is now written on every run (see
    :func:`install_gateway_log`), so the flag would have been a no-op if
    left as it was. It is not; it still does two things, both of which are
    about watching a run rather than capturing one:

    1. **Echoes everything to the console** -- the ``discord`` firehose and
       our own ``dap`` lines -- so a CLI run or a build launched from a
       terminal shows the gateway trail live instead of only in a file.
       Skipped when there is no usable stderr, which is the normal case for
       a ``pythonw``-launched GUI.
    2. **Lowers the ``dap`` namespace to DEBUG**, in the logger *and* in the
       session-log filter, so DEBUG-level diagnostics we write reach
       ``DAP_session.log``. Without both halves the flag would raise the
       logger only for records the filter then dropped.

    Positional arguments are accepted and ignored for compatibility with the
    old ``enable_verbose(filename, max_bytes, backup_count)`` signature.
    """
    global _verbose_enabled
    if _verbose_enabled:
        return

    session_logger = get_logger()
    session_logger.setLevel(logging.DEBUG)
    if _session_filter is not None:
        _session_filter.min_level = logging.DEBUG

    stream = getattr(sys, "stderr", None)
    if stream is not None:
        try:
            console = logging.StreamHandler(stream)
            console.setLevel(logging.DEBUG)
            console.setFormatter(
                logging.Formatter(fmt="%(levelname)s:%(name)s: %(message)s")
            )
            logging.getLogger("discord").addHandler(console)
            session_logger.addHandler(console)
        except Exception:
            pass

    _verbose_enabled = True


# ---------------------------------------------------------------------------
# lifecycle helpers -- thin wrappers so call sites stay one-liners
# ---------------------------------------------------------------------------


def _redact(argv):
    """Never let a bot token reach a log file."""
    out = []
    skip = False
    for a in argv:
        if skip:
            out.append("<redacted>")
            skip = False
            continue
        if a in ("-t", "--token"):
            out.append(a)
            skip = True
            continue
        if a.startswith("--token="):
            out.append("--token=<redacted>")
            continue
        out.append(a)
    return out


def log_start(args=None, diagnose=False):
    log = get_logger()

    try:
        import discord

        dpy = discord.__version__
    except Exception:
        dpy = "?"

    try:
        import sounddevice

        sdv = sounddevice.__version__
    except Exception:
        sdv = "?"

    log.info(
        "=== DAP start | python=%s discord.py=%s sounddevice=%s | diagnose=%s"
        " | argv=%s",
        sys.version.split()[0],
        dpy,
        sdv,
        diagnose,
        " ".join(_redact(sys.argv[1:])),
    )

    if args is not None:
        log.info(
            "args: verbose=%s device=%s channel=%s query=%s online=%s",
            getattr(args, "verbose", None),
            getattr(args, "device", None),
            getattr(args, "channel", None),
            getattr(args, "query", None),
            getattr(args, "online", None),
        )


def log_device(index, name):
    get_logger().info("device selected: index=%s name=%r", index, name)


def log_event(fmt, *a):
    get_logger().info(fmt, *a)


def log_warn(fmt, *a):
    get_logger().warning(fmt, *a)


# ---------------------------------------------------------------------------
# shutdown attribution
# ---------------------------------------------------------------------------
#
# Upstream's ``finally: log_shutdown()`` wrote "=== DAP shutdown (clean) ==="
# for every exit, which meant the log could not answer the first question
# asked of it after a bad session: did the app stop because the user closed
# it, or because the thing we are hunting killed it? Those need different
# investigations and the log conflated them.
#
# The cause is only knowable where it happens, so call sites record it and
# the ``finally`` reports whatever was recorded.

_start_monotonic = time.monotonic()
_shutdown_reason = None
_shutdown_detail = None

#: Exception types mapped to a stable reason string, so the log is greppable
#: rather than needing the reader to know Python's exception hierarchy.
_EXC_REASONS = {
    "KeyboardInterrupt": "keyboard-interrupt",
    "CancelledError": "asyncio-cancelled",
    "SystemExit": "sys-exit",
    "ConnectionClosed": "discord-connection-closed",
    "LoginFailure": "discord-login-failed",
    "FileNotFoundError": "no-token-file",
}


def note_shutdown(reason=None, exc=None, force=False):
    """Record *why* the app is stopping. First writer wins. Never raises.

    First-writer-wins is the point: the outermost handler always sees
    something generic (``asyncio.run`` returned, a CancelledError arrived),
    while the innermost site knows the actual trigger -- the user clicked
    the close button. Letting a later, vaguer caller overwrite an earlier,
    specific one would throw away the only useful part.

    :param reason: short stable slug. Derived from ``exc`` when omitted.
    :param exc: the exception that triggered the shutdown, if any. Its type
        name and ``str()`` go into the detail field.
    :param force: overwrite an already-recorded reason.
    """
    global _shutdown_reason, _shutdown_detail

    try:
        exc_name = type(exc).__name__ if exc is not None else None

        if reason is None:
            reason = _EXC_REASONS.get(exc_name, "unhandled-exception"
                                      if exc is not None else "unknown")

        detail = None
        if exc is not None:
            text = ""
            try:
                text = str(exc)
            except Exception:
                text = "<unprintable>"
            detail = "exc=%s(%s)" % (exc_name, text[:200])

        with _lock:
            if _shutdown_reason is not None and not force:
                return
            _shutdown_reason = str(reason)
            _shutdown_detail = detail
    except Exception:
        # A shutdown path that can itself raise is worse than no attribution.
        pass


def shutdown_reason():
    """The recorded reason, or ``None``. Test/introspection helper."""
    with _lock:
        return _shutdown_reason


def reset_shutdown_reason():
    """Test helper: forget the recorded cause."""
    global _shutdown_reason, _shutdown_detail
    with _lock:
        _shutdown_reason = None
        _shutdown_detail = None


def log_shutdown(reason=None):
    """Write the closing line, naming the cause. Never raises.

    ``reason=None`` (the normal ``finally`` call) reports whatever
    :func:`note_shutdown` recorded, or ``unattributed`` if nothing did --
    which is itself a finding worth seeing, because it means the process
    unwound through a path nobody instrumented.
    """
    try:
        with _lock:
            recorded, detail = _shutdown_reason, _shutdown_detail

        if reason is None:
            reason = recorded if recorded is not None else "unattributed"

        get_logger().info(
            "=== DAP shutdown (%s) after %.1fs%s ===",
            reason,
            time.monotonic() - _start_monotonic,
            " | " + detail if detail else "",
        )
    except Exception:
        pass
