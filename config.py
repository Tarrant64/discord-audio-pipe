"""Persistent user settings for Discord Audio Pipe.

One small JSON file (``DAP_config.json``) sitting next to ``main.pyw`` /
the ``.exe``, holding user preferences that should survive a restart.

Design constraints
------------------

**It must never be able to stop the app from starting.** A missing file, a
truncated file, a file full of garbage, a read-only directory, a file owned
by another user -- every one of those resolves to "use the defaults, log one
WARNING, carry on". There is nothing in here worth crashing over.

**It must be safe to extend.** Today it holds exactly one setting. A later
phase adds last-used device / server / channel / mute state for a "remember
my settings" feature, and older builds must not choke on a file written by a
newer one. Hence:

* a ``version`` integer at the top level, checked on load;
* unknown keys are preserved on load and written back out on save, so a
  downgrade-then-upgrade round trip does not silently destroy settings the
  older build did not understand;
* every known key is read with a type check and falls back to its default
  individually, so one bad value does not discard the rest of the file.

**It must not be corruptible by a crash.** Saves go to a temporary file in
the same directory, are flushed to disk, and are then :func:`os.replace`\\ d
over the real path. ``os.replace`` is atomic on POSIX and on Windows, so a
crash mid-save leaves either the complete old file or the complete new one,
never a half-written one.

**It never, ever stores the bot token.** The token lives in ``token.txt``
and nowhere else. :func:`Config.to_dict` builds its output from
:data:`DEFAULTS` plus preserved unknown keys, and :func:`Config.set` refuses
to write a key listed in :data:`FORBIDDEN_KEYS`.
"""

import json
import logging
import os
import sys
import tempfile
import threading

log = logging.getLogger("dap.config")

FILENAME = "DAP_config.json"

#: Schema version written into every saved file. Bump only for a change that
#: an older build could *misread* -- adding a new optional key does not need
#: a bump, because unknown keys are ignored and defaults fill the gaps.
VERSION = 1

#: Every setting this build understands, with its default and expected type.
#: Adding a setting means adding one line here and nothing else.
DEFAULTS = {
    # Reconnect/restart the stream automatically when the health readout says
    # it has stalled. NOT IMPLEMENTED YET -- the setting and its checkbox
    # exist so the preference can be captured and persisted; the recovery
    # behaviour itself is a separate change. Default OFF, and it must stay
    # OFF by default until the behaviour actually exists and has been proven.
    "auto_recover": False,
}

#: Keys that must never be persisted, whatever a caller asks for. Secrets do
#: not belong in a settings file that users copy around and paste into issue
#: reports.
FORBIDDEN_KEYS = frozenset({"token", "bot_token", "auth", "password", "secret"})


def _base_dir():
    """Directory the config file lives in.

    Mirrors how ``main.pyw`` finds ``token.txt``: relative to the process
    working directory for a source checkout, and next to the executable for
    a PyInstaller build (where ``sys._MEIPASS`` is a throwaway temp dir and
    would lose the file on every run).

    Resolved once at import time, deliberately. ``gui.GUI.__init__`` calls
    ``QDir.setCurrent(bundle_dir)``, which changes the process working
    directory; capturing the path before that runs keeps the config file in
    the same place ``token.txt`` was read from.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.getcwd()


BASE_DIR = _base_dir()


def default_path():
    return os.path.join(BASE_DIR, FILENAME)


class Config:
    """A loaded settings file. Construct via :func:`load`.

    Values are read through :meth:`get` or as attributes for the known keys
    (``cfg.auto_recover``). Writes go through :meth:`set`, which persists
    immediately -- there is no explicit save step to forget, and the file is
    small enough that writing it on every change costs nothing.
    """

    def __init__(self, path=None, values=None, unknown=None, readable=True):
        self.path = default_path() if path is None else path
        self._values = dict(DEFAULTS)
        if values:
            self._values.update(values)
        #: Keys present in the file that this build does not know about.
        #: Kept verbatim and written back on save so a newer build's settings
        #: survive being opened by an older one.
        self._unknown = dict(unknown or {})
        #: False when the file existed but could not be parsed or read. The
        #: GUI can use this to explain why settings look reset.
        self.readable = readable
        self._lock = threading.Lock()
        self._save_failed = False

    # -- reads --------------------------------------------------------------

    def get(self, key, default=None):
        if key in self._values:
            return self._values[key]
        if key in self._unknown:
            return self._unknown[key]
        return default

    @property
    def auto_recover(self):
        return bool(self._values.get("auto_recover", DEFAULTS["auto_recover"]))

    # -- writes -------------------------------------------------------------

    def set(self, key, value, save=True):
        """Set one key and (by default) persist the whole file.

        Returns True only if the value was both accepted *and* written.
        A rejected value returns False and changes nothing; a value that
        was accepted but could not be written returns False and is still
        live in memory for the rest of the session, because failing to
        persist a preference is an annoyance, not a reason to ignore it or
        to interrupt audio.
        """
        if key in FORBIDDEN_KEYS:
            log.warning("refusing to store %r in the settings file", key)
            return False

        if key in DEFAULTS:
            expected = type(DEFAULTS[key])
            if expected is bool:
                value = bool(value)
            elif not isinstance(value, expected):
                log.warning(
                    "ignoring %r for setting %r (expected %s)",
                    value,
                    key,
                    expected.__name__,
                )
                return False
            self._values[key] = value
        else:
            self._unknown[key] = value

        if save:
            return self.save()
        return True

    def to_dict(self):
        """The exact object that gets serialised.

        Built from the known settings plus preserved unknown keys -- never
        from arbitrary caller state -- so nothing can leak in sideways.
        """
        out = {"version": VERSION}
        for key, value in self._unknown.items():
            if key not in FORBIDDEN_KEYS and key != "version":
                out[key] = value
        out.update(self._values)
        return out

    def save(self):
        """Atomically write the file. Never raises.

        Writes ``DAP_config.json.<random>.tmp`` in the target directory,
        fsyncs it, then :func:`os.replace`\\ s it over the real path. Same
        filesystem by construction, so the replace is a rename, which is
        atomic on both POSIX and Windows.
        """
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            tmp_name = None
            try:
                payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)

                fd, tmp_name = tempfile.mkstemp(
                    prefix=FILENAME + ".", suffix=".tmp", dir=directory
                )
                # fdopen takes ownership of fd, including on close.
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(tmp_name, self.path)
                tmp_name = None
                self._save_failed = False
                return True

            except Exception as exc:
                # One WARNING per failure *transition*, so a read-only
                # directory does not fill the log on every checkbox click.
                if not self._save_failed:
                    self._save_failed = True
                    log.warning(
                        "could not save settings to %s (%s: %s) --"
                        " changes will not persist across restarts",
                        self.path,
                        type(exc).__name__,
                        exc,
                    )
                return False

            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass


def load(path=None):
    """Read the settings file. Always returns a usable :class:`Config`.

    Failure modes and their outcomes:

    ==========================  ==========================================
    file missing                defaults, no warning (first run is normal)
    file unreadable / denied    defaults, one WARNING
    not valid JSON              defaults, one WARNING
    JSON but not an object      defaults, one WARNING
    unknown ``version``         values still read, one WARNING
    wrong type for a key        that key falls back, one WARNING
    unknown keys                kept verbatim, no warning
    ==========================  ==========================================
    """
    target = default_path() if path is None else path

    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        # First run. Not an error, and deliberately not written eagerly --
        # the file appears the first time the user changes something.
        return Config(path=target)
    except OSError as exc:
        log.warning(
            "could not read settings from %s (%s: %s) -- using defaults",
            target,
            type(exc).__name__,
            exc,
        )
        return Config(path=target, readable=False)

    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning(
            "settings file %s is not valid JSON (%s) -- using defaults."
            " It will be overwritten the next time a setting changes.",
            target,
            exc,
        )
        return Config(path=target, readable=False)

    if not isinstance(data, dict):
        log.warning(
            "settings file %s does not contain a JSON object (found %s) --"
            " using defaults",
            target,
            type(data).__name__,
        )
        return Config(path=target, readable=False)

    version = data.get("version")
    if version != VERSION:
        log.warning(
            "settings file %s has version %r, this build writes version %d --"
            " reading what is recognisable and keeping the rest",
            target,
            version,
            VERSION,
        )

    values = {}
    unknown = {}
    for key, value in data.items():
        if key == "version":
            continue
        if key in FORBIDDEN_KEYS:
            # Somebody hand-edited a secret in. Do not load it and do not
            # write it back out.
            log.warning("ignoring %r found in %s -- secrets do not belong"
                        " in the settings file", key, target)
            continue
        if key in DEFAULTS:
            expected = type(DEFAULTS[key])
            if expected is bool:
                if isinstance(value, bool):
                    values[key] = value
                else:
                    log.warning(
                        "setting %r in %s is %r, expected a boolean --"
                        " using the default (%r)",
                        key, target, value, DEFAULTS[key],
                    )
            elif isinstance(value, expected):
                values[key] = value
            else:
                log.warning(
                    "setting %r in %s is %r, expected %s -- using the"
                    " default (%r)",
                    key, target, value, expected.__name__, DEFAULTS[key],
                )
        else:
            unknown[key] = value

    return Config(path=target, values=values, unknown=unknown)
