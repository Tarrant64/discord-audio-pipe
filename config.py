"""Persistent user settings for Discord Audio Pipe.

One small JSON file (``DAP_config.json``) sitting next to ``main.pyw`` /
the ``.exe``, holding user preferences that should survive a restart.

Design constraints
------------------

**It must never be able to stop the app from starting.** A missing file, a
truncated file, a file full of garbage, a read-only directory, a file owned
by another user -- every one of those resolves to "use the defaults, log one
WARNING, carry on". There is nothing in here worth crashing over.

**It must be safe to extend.** It now holds the auto-recover and
auto-connect flags plus the saved profile -- the last-used device / server /
channel / mute state for each connection row -- and older builds must not
choke on a file written by a newer one. Hence:

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
:data:`DEFAULTS` plus preserved unknown keys, :func:`Config.set` refuses to
write a key listed in :data:`FORBIDDEN_KEYS`, and -- because the profile
introduced the first *nested* value in the file -- that refusal now searches
dicts and lists recursively, so a secret cannot ride in inside a container.
Profile rows are additionally rebuilt field by field from a whitelist
(:data:`ROW_FIELDS`), so an unexpected key in a row is dropped rather than
round-tripped.

**It exists on disk from the first launch.** ``load()`` used to return
defaults without writing anything when the file was missing, so the file
only appeared once the user happened to change a setting -- which meant a
user who changed nothing had no file to find, inspect or hand-edit, and no
way to tell "not written yet" from "written somewhere I did not expect".
:func:`load` now writes the defaults out on first run (see
``create_missing``). A failure to do so is still only a WARNING.
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

    # Re-join the saved channel(s) on launch without being asked. Default
    # OFF, and deliberately separate from the profile itself: the profile is
    # always saved and always restored *into the dropdowns*, but turning the
    # restored selection into an actual voice connection is opt-in. Joining a
    # voice channel is a visible, audible act in someone else's server; it is
    # not something an app should do because it was double-clicked.
    "auto_connect": False,

    # Last-used setup, one entry per connection row, in row order. Each entry
    # is a dict with the keys in ROW_FIELDS and nothing else -- see
    # normalize_row(). An empty list means "nothing saved yet".
    "profile": [],
}

#: Keys that must never be persisted, whatever a caller asks for. Secrets do
#: not belong in a settings file that users copy around and paste into issue
#: reports. Matched case-insensitively, at any nesting depth.
FORBIDDEN_KEYS = frozenset({"token", "bot_token", "auth", "password", "secret"})

# ---------------------------------------------------------------------------
# profile rows
# ---------------------------------------------------------------------------

#: The only keys a profile row may contain. A row is rebuilt from this list
#: on both load and save, so anything else in the file is dropped rather than
#: preserved -- rows are the one place where round-tripping unknown keys
#: would let arbitrary content through the token denylist.
ROW_FIELDS = ("device_name", "guild_id", "channel_id", "muted")

#: Sanity cap on how many rows are read or written. The GUI grows one row per
#: "+" click with no upper bound of its own; this stops a hand-edited or
#: corrupt file from making the app build thousands of widgets on launch.
MAX_PROFILE_ROWS = 32

#: Discord snowflakes are 64-bit, so at most 20 decimal digits. Stored as
#: *strings*, not JSON numbers: a snowflake exceeds 2**53 and would be
#: mangled by any JSON reader that parses numbers as doubles (every browser,
#: jq's default, a lot of tooling people paste settings files into). Python
#: would round-trip it fine; the file should not depend on that.
_MAX_SNOWFLAKE_DIGITS = 20


def _clean_snowflake(value):
    """Coerce a Discord id to its canonical decimal string, or None.

    Accepts an int or a str because a hand-edited file will plausibly hold
    either. Rejects anything that is not a plain positive decimal integer --
    no signs, no whitespace, no ``0x``, no floats.
    """
    if isinstance(value, bool):          # bool is an int subclass; not an id
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value.isdigit() or not 1 <= len(value) <= _MAX_SNOWFLAKE_DIGITS:
        return None

    # Drop leading zeros so "0000123" and "123" compare equal.
    return str(int(value))


def normalize_row(raw):
    """Rebuild one profile row from a whitelist. Never raises.

    Returns a dict with exactly the keys in :data:`ROW_FIELDS`. Every field
    that is missing, the wrong type, or unparseable comes back as ``None``
    (``False`` for ``muted``) *individually*, so one bad id does not discard
    the device name sitting next to it.

    Anything not in :data:`ROW_FIELDS` -- including a hand-added ``token`` --
    is dropped on the floor and never written back.
    """
    row = {
        "device_name": None,
        "guild_id": None,
        "channel_id": None,
        "muted": False,
    }

    if not isinstance(raw, dict):
        return row

    name = raw.get("device_name")
    if isinstance(name, str) and name.strip():
        # Stored byte-for-byte, NOT stripped. This string has to compare
        # equal to what PortAudio reports, and PortAudio's MME backend
        # truncates names to exactly 31 characters -- which lands mid-word
        # and very often leaves a trailing space, as in
        # "CABLE Output (VB-Audio Virtual ". Calling .strip() here looks
        # like tidying and is actually corruption: it produces a name that
        # can never match a real device again, so every restore of an
        # affected device would silently fall back to "nothing selected".
        # ``name.strip()`` above is only the emptiness test.
        row["device_name"] = name

    row["guild_id"] = _clean_snowflake(raw.get("guild_id"))
    row["channel_id"] = _clean_snowflake(raw.get("channel_id"))
    row["muted"] = raw.get("muted") is True

    # A channel id without a guild id is unusable -- the restore walks
    # guild -> channel -- so treat it as absent rather than carrying a
    # dangling reference around.
    if row["guild_id"] is None:
        row["channel_id"] = None

    return row


def row_is_empty(row):
    """True for a row that would restore nothing."""
    return (
        row.get("device_name") is None
        and row.get("guild_id") is None
        and row.get("channel_id") is None
    )


def normalize_profile(raw):
    """Rebuild the whole profile list. Never raises.

    Trailing empty rows are trimmed so that a user who cleared their second
    connection row does not get a phantom empty row recreated on every
    launch. Empty rows *between* populated ones are kept, because row order
    is what maps a saved entry back onto a GUI row.
    """
    if not isinstance(raw, (list, tuple)):
        return []

    rows = [normalize_row(item) for item in raw[:MAX_PROFILE_ROWS]]

    while rows and row_is_empty(rows[-1]):
        rows.pop()

    return rows


def resolve_device(name, devices):
    """Map a saved device *name* back to a PortAudio index, or ``None``.

    ``devices`` is the ``{name: index}`` mapping from
    :func:`sound.query_devices`.

    This function is the whole reason the profile stores a name instead of
    the index the GUI actually needs. PortAudio's enumeration order is not
    stable: it changes when a device is plugged in or removed, when a driver
    updates, and sometimes just across a reboot. Saving index 2 and restoring
    it blindly would eventually select *a different device* -- and since the
    next thing the app does with that index is stream it into a voice
    channel, "a different device" can mean broadcasting a live microphone
    into a public server. There is no acceptable fuzzy match here.

    So: exact string equality, or nothing. In particular there is no
    "closest name" fallback and no prefix match. The MME 31-character
    truncation is not a problem to work around because it is symmetric --
    the name was saved from the same ``query_devices()`` call that is being
    searched now, so both sides are truncated identically.

    A miss is logged at INFO (it is expected and harmless: unplugged
    headset, different machine) and returns ``None``. The caller must leave
    the row's device unselected rather than pick a neighbour.
    """
    if not name or not isinstance(devices, dict):
        return None

    if name in devices:
        return devices[name]

    log.info(
        "saved audio device %r is not present (%d device(s) available)"
        " -- leaving the device unselected",
        name,
        len(devices),
    )
    return None


def _contains_forbidden(value, _depth=0):
    """True if a forbidden key hides anywhere inside ``value``.

    The token denylist used to only have to look at one level, because every
    value in the file was a scalar. The profile made values nested, so a
    caller passing ``{"note": {"token": "..."}}`` would previously have had
    it stored and written out. Depth-limited so a self-referential structure
    cannot spin here.
    """
    if _depth > 8:
        return False

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
                return True
            if _contains_forbidden(item, _depth + 1):
                return True
        return False

    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, _depth + 1) for item in value)

    return False


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

    @property
    def auto_connect(self):
        return bool(self._values.get("auto_connect", DEFAULTS["auto_connect"]))

    @property
    def profile(self):
        """The saved rows, normalised, as a fresh list of fresh dicts.

        A copy every time, deliberately: the GUI holds on to what it gets
        back and a shared list would let a caller mutate stored state without
        going through :meth:`set_profile` (and therefore without validation
        or a save).
        """
        return normalize_profile(self._values.get("profile", []))

    # -- writes -------------------------------------------------------------

    def set_profile(self, rows, save=True):
        """Replace the saved profile. Returns True if written.

        Rows are normalised through :func:`normalize_profile` first, so the
        stored value is always well-formed regardless of what the caller
        assembled, and a no-op change is not written at all -- the GUI calls
        this on every dropdown change and most of those leave the file
        identical.
        """
        cleaned = normalize_profile(rows)

        if cleaned == self._values.get("profile"):
            return True

        self._values["profile"] = cleaned

        if save:
            return self.save()
        return True

    def set(self, key, value, save=True):
        """Set one key and (by default) persist the whole file.

        Returns True only if the value was both accepted *and* written.
        A rejected value returns False and changes nothing; a value that
        was accepted but could not be written returns False and is still
        live in memory for the rest of the session, because failing to
        persist a preference is an annoyance, not a reason to ignore it or
        to interrupt audio.
        """
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
            log.warning("refusing to store %r in the settings file", key)
            return False

        if _contains_forbidden(value):
            log.warning(
                "refusing to store %r: the value contains a key from the"
                " secrets denylist",
                key,
            )
            return False

        if key == "profile":
            return self.set_profile(value, save=save)

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
            if key == "version":
                continue
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
                continue
            if _contains_forbidden(value):
                # Belt and braces: load() and set() both reject these, so
                # reaching here means something bypassed both. Drop it
                # rather than serialise a secret.
                log.warning(
                    "dropping %r from the settings file: it contains a key"
                    " from the secrets denylist",
                    key,
                )
                continue
            out[key] = value

        out.update(self._values)
        out["profile"] = normalize_profile(self._values.get("profile", []))
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


def load(path=None, create_missing=True):
    """Read the settings file. Always returns a usable :class:`Config`.

    Failure modes and their outcomes:

    ==========================  ==========================================
    file missing                defaults, written out, one INFO
    file unreadable / denied    defaults, one WARNING
    not valid JSON              defaults, one WARNING
    JSON but not an object      defaults, one WARNING
    unknown ``version``         values still read, one WARNING
    wrong type for a key        that key falls back, one WARNING
    bad profile row             that field falls back, no warning
    unknown keys                kept verbatim, no warning
    ==========================  ==========================================

    :param create_missing: write the defaults out when the file does not
        exist. On by default. This is what makes the file discoverable: it
        used to be created lazily, on the first setting change, so a user who
        changed nothing had no file to find and no way to tell "not written
        yet" from "written somewhere unexpected". Set False in tests that
        want to assert on a read without a side effect.
    """
    target = default_path() if path is None else path

    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        # First run. Write the defaults so the file exists and can be found,
        # inspected and hand-edited. Failing to write is not fatal -- save()
        # logs it and the session carries on with in-memory defaults.
        fresh = Config(path=target)

        if create_missing and fresh.save():
            log.info("created settings file %s with default values", target)

        return fresh
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
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
            # Somebody hand-edited a secret in. Do not load it and do not
            # write it back out.
            log.warning("ignoring %r found in %s -- secrets do not belong"
                        " in the settings file", key, target)
            continue
        if key != "profile" and _contains_forbidden(value):
            # Same, but buried inside a container. Profile rows are exempt
            # from this check only because normalize_row() rebuilds them
            # from a whitelist, which is a stronger guarantee.
            log.warning("ignoring %r found in %s -- it contains a key from"
                        " the secrets denylist", key, target)
            continue
        if key == "profile":
            values[key] = normalize_profile(value)
            if not isinstance(value, (list, tuple)):
                log.warning(
                    "setting 'profile' in %s is %r, expected a list --"
                    " using the default (no saved rows)",
                    target, value,
                )
        elif key in DEFAULTS:
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
