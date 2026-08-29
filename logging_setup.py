"""Central logging configuration for Discord Audio Pipe.

This module owns *all* log handler setup so that upstream files stay
merge-clean. Nothing in here changes application behaviour -- it only
changes what gets recorded and where.

Three log files:

  DAP_errors.log   ERROR and above on the root logger. Upstream behaviour,
                   now size-bounded by a RotatingFileHandler.
  discord.log      full DEBUG firehose from the ``discord`` logger, only
                   when ``-v`` / ``--verbose`` is passed. Upstream
                   behaviour, now size-bounded.
  DAP_session.log  NEW. INFO-level lifecycle log: start/stop, device
                   selection, guild/channel joins, connects, disconnects,
                   settings/profile changes, the ``--diagnose``
                   instrumentation output, plus the *promoted* discord.py
                   diagnostics described below. Every line carries the
                   writing process's pid, because two builds launched from
                   the same directory share this file.

The promotion filter is the highest-value piece here. discord.py logs its
two most diagnostic voice lines at DEBUG:

    discord/player.py:814   'Not connected, waiting for %ss...'
    discord/player.py:818   'Aborting playback'

The second sits immediately before a bare ``return`` that leaves
``AudioPlayer._end`` unset, so ``VoiceClient.is_playing()`` keeps
returning True on a dead thread -- the exact silent-stall signature we are
hunting. Seeing that line once proves the hypothesis outright, so we force
it into the session log at WARNING regardless of ``-v``.
"""

import logging
import logging.handlers
import os
import sys
import threading

# ---------------------------------------------------------------------------
# file names / rotation policy
# ---------------------------------------------------------------------------

ERROR_LOG = "DAP_errors.log"
DEBUG_LOG = "discord.log"
SESSION_LOG = "DAP_session.log"

MAX_BYTES = 2 * 1024 * 1024  # 2 MB
BACKUP_COUNT = 3

SESSION_LOGGER_NAME = "dap"

# Substrings of discord.py log messages that we promote to WARNING on the
# session log. Matched against the raw ``record.msg`` format string (not the
# interpolated message) so the check is a cheap ``in`` against a constant.
PROMOTED_SUBSTRINGS = (
    "Aborting playback",             # player.py:818  - silent-abort path (c)
    "Not connected, waiting for",    # player.py:814  - reconnect wait begins
    "Reconnected, resuming playback",  # player.py:820 - recovery
    "A packet has been dropped",     # voice_client.py:608 - UDP send gap
)

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
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == SESSION_LOGGER_NAME or record.name.startswith(
            SESSION_LOGGER_NAME + "."
        ):
            return record.levelno >= logging.INFO

        msg = record.msg
        if not isinstance(msg, str):
            return False

        for needle in PROMOTED_SUBSTRINGS:
            if needle in msg:
                return True

        return False


class PromotingHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler that re-badges promoted discord.py records at WARNING.

    The re-badge happens on a *copy* of the record, so the ``-v``
    ``discord.log`` handler (which is offered the same record object further
    up the logger hierarchy) still sees the original DEBUG level. Mutating the
    shared record in place would silently corrupt discord.log.
    """

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


def get_logger():
    """The DAP session logger. Safe to call before :func:`configure`."""
    return logging.getLogger(SESSION_LOGGER_NAME)


def configure(verbose=False, log_dir=None, max_bytes=None, backup_count=None):
    """Install every handler. Idempotent for normal (``log_dir is None``) use.

    :param verbose: mirrors upstream ``-v``; enables the full DEBUG
        ``discord.log`` firehose.
    :param log_dir: write logs here instead of the CWD (tests only).
    :param max_bytes: rotation threshold override (tests only).
    :param backup_count: rotation backup count override (tests only).
    :returns: the ``dap`` session logger.
    """
    global _configured

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
        session_handler.addFilter(SessionFilter())

        session_logger = get_logger()
        session_logger.setLevel(logging.INFO)
        session_logger.addHandler(session_handler)
        session_logger.propagate = True  # ERRORs still reach DAP_errors.log

        for name in PROMOTED_LOGGERS:
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            lg.addHandler(session_handler)

        # -- discord.log (upstream -v behaviour, now rotating) --------------
        if verbose:
            enable_verbose(path(DEBUG_LOG), maxb, backups)

        _configured = True

    return get_logger()


# ---------------------------------------------------------------------------
# unhandled-exception safety net
# ---------------------------------------------------------------------------

_excepthook_installed = False


def install_excepthook():
    """Stop an unhandled exception in a Qt slot from killing the process.

    This is not belt-and-braces. It is load-bearing, and it is a regression
    the PyQt5 -> PyQt6 migration introduced.

    Under PyQt5, an exception that escaped a Python slot back into C++ was
    printed and execution continued. PyQt6 calls ``qFatal()`` instead, which
    calls ``abort()``: the whole application dies instantly, with a native
    "Abort trap: 6" crash report and *nothing at all* in DAP_errors.log,
    because the process is gone before anything can be flushed.

    PyQt makes exactly one exception to that: if ``sys.excepthook`` has been
    replaced, it hands the exception to the replacement and does **not**
    abort. So installing any non-default hook is what keeps the app alive.

    Measured, not assumed -- a slot raising RuntimeError, triggered by
    ``QComboBox.setCurrentIndex()``:

        default sys.excepthook  -> exit 134 (SIGABRT), slot never returns
        replaced sys.excepthook -> exit 0, hook runs, execution continues

    This matters most for anything that sets a widget's value in code rather
    than in response to a click -- the saved-profile restore, above all. A
    restore touches a device that may have been unplugged, a guild the bot
    may have been removed from and a channel that may have been deleted, and
    every one of those paths runs inside a slot.

    Individual slot bodies still catch their own exceptions; this is the net
    under the ones we missed. Only covers the main thread -- worker threads
    go through ``threading.excepthook``, which is untouched.
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


def enable_verbose(filename=None, max_bytes=None, backup_count=None):
    """Upstream's ``-v`` behaviour: full DEBUG firehose to ``discord.log``.

    Split out from :func:`configure` because ``main.pyw`` installs the error
    handler before argument parsing (so that import-time failures are still
    captured) and only learns about ``-v`` afterwards.
    """
    global _verbose_enabled
    if _verbose_enabled:
        return

    debug_formatter = logging.Formatter(
        fmt="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
    )
    # Upstream used mode="w"; RotatingFileHandler honours mode on the initial
    # open, so verbose runs still start from a clean file.
    debug_handler = logging.handlers.RotatingFileHandler(
        DEBUG_LOG if filename is None else filename,
        maxBytes=MAX_BYTES if max_bytes is None else max_bytes,
        backupCount=BACKUP_COUNT if backup_count is None else backup_count,
        encoding="utf-8",
        mode="w",
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(debug_formatter)

    debug_logger = logging.getLogger("discord")
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.addHandler(debug_handler)

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


def log_shutdown(reason="clean"):
    get_logger().info("=== DAP shutdown (%s) ===", reason)
