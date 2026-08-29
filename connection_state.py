"""The one source of truth for whether a connection row is in voice.

Why this module exists
----------------------

Before this, "are we connected?" was answered independently in four places,
by three different means, and they disagreed:

* ``Connection.voice is not None`` -- true from the first successful join
  until the object is replaced, including for the whole time the client is
  disconnected.
* ``voice.is_connected()`` -- the websocket's opinion, which says nothing
  about whether audio is flowing.
* ``voice.is_playing()`` -- **which lies.** ``AudioPlayer`` takes the silent
  abort path at ``discord/player.py:818``, the thread dies, and
  ``is_playing()`` keeps returning ``True`` for the rest of the process's
  life. Anything that trusts it is reading a value that cannot go false.
* The status strip, which inferred a fifth answer from instrumentation
  sample counts.

None of those is a *state*: they are observations of a system that has one,
and each is wrong in a different direction. This module holds the state
explicitly, as a small, boring, Qt-free, Discord-free finite state machine,
and everything else -- the button label, the button colour, which dropdowns
are enabled, what the status strip says, whether auto-connect may fire --
reads it rather than guessing.

Being Qt-free is deliberate: it means the transition rules can be unit
tested directly, without a QApplication, an event loop, a token or a
network.

The states
----------

::

    IDLE ──connect──▶ CONNECTING ──joined───▶ LIVE
      ▲                    │                   │
      │                    │ error/timeout     │ disconnect
      │                    ▼                   ▼
      │                 FAILED           DISCONNECTING
      │                    │                   │
      └──reset─────────────┘◀──fail────────────┤
      │                                        │
      └────────────────────left────────────────┘

    FAILED ──connect──▶ CONNECTING            (retry)

* ``IDLE`` -- not in voice. Selections may be changed freely. This is also
  where a row sits after a *successful* disconnect, with its dropdowns
  untouched, so pressing Connect again rejoins in one click.
* ``CONNECTING`` -- a join is in flight. Nothing may interrupt it: the
  controls are disabled and every request is refused, so a second click
  cannot start a second join.
* ``LIVE`` -- in voice and playing.
* ``DISCONNECTING`` -- a leave is in flight. Always ends in ``IDLE``; see
  below.
* ``FAILED`` -- the last join raised or timed out. A terminal-until-asked
  state, not a hang: the controls come back, the button says "Retry", and
  changing any selection clears it back to ``IDLE``.

Two rules that the table encodes and that are worth stating in words:

**Disconnect always ends in IDLE.** There is deliberately no
``DISCONNECTING -> FAILED`` edge. If leaving the channel raises, the useful
answer for the user is still "you are not in voice, press Connect to try
again" -- routing that to ``FAILED`` would only add a state they have to
clear before they can do the obvious thing.

**CONNECTING cannot be cancelled.** There is deliberately no
``CONNECTING -> DISCONNECTING`` edge either. ``discord.py``'s connect
already carries its own timeout, so the state is bounded in time, and
allowing a mid-flight cancel means racing the join against the leave for the
sake of at most ten seconds.

Illegal transitions are *rejected*, not raised
----------------------------------------------

:meth:`LinkState.to` returns ``False`` and logs rather than raising. The
callers are Qt slots, and under PyQt6 an exception escaping a slot reaches
``qFatal()`` and aborts the process (see ``gui.guarded_slot``). A state
machine whose job is to make illegal things impossible must not itself be a
way to kill the app. The UI is the first line of defence -- it disables what
cannot legally be pressed -- and this is the second, for the races the UI
cannot see.
"""

import logging

log = logging.getLogger("dap.link")

#: Not in voice. Selections are editable; Connect is the offered action.
IDLE = "idle"

#: A join is in flight.
CONNECTING = "connecting"

#: In voice, playing.
LIVE = "live"

#: A leave is in flight.
DISCONNECTING = "disconnecting"

#: The last join raised or timed out.
FAILED = "failed"

#: Every state, in lifecycle order. Also the set of ``[link="..."]`` selector
#: values in ``assets/style.qss`` -- changing one means changing the other.
STATES = (IDLE, CONNECTING, LIVE, DISCONNECTING, FAILED)

#: The whole transition table. Anything not listed here is illegal, and
#: "illegal" means the request is refused, not that it crashes.
TRANSITIONS = {
    IDLE: frozenset({CONNECTING}),
    CONNECTING: frozenset({LIVE, FAILED}),
    LIVE: frozenset({DISCONNECTING, FAILED}),
    DISCONNECTING: frozenset({IDLE}),
    FAILED: frozenset({CONNECTING, IDLE}),
}

#: States in which a request is in flight and the row must be left alone.
BUSY = frozenset({CONNECTING, DISCONNECTING})

#: Priority order for summarising several independent rows into one word for
#: the status strip. ``FAILED`` outranks everything because it is the only
#: one that needs the user; ``LIVE`` outranks ``CONNECTING`` so that a live
#: row's health metrics stay on screen while a *different* row is joining.
PRECEDENCE = (FAILED, LIVE, CONNECTING, DISCONNECTING, IDLE)


def summarise(states):
    """Reduce several rows' states to the one the status strip should show."""
    present = set(states)

    for state in PRECEDENCE:
        if state in present:
            return state

    return IDLE


class LinkState:
    """One connection row's position in the lifecycle above.

    :param name: short label used in log lines, e.g. ``"row 1"``.
    :param ready: zero-argument callable returning ``True`` when the row has
        a complete selection (device *and* server *and* channel). A callable
        rather than a flag on purpose -- a mirrored boolean is one more thing
        that can drift out of step with the dropdowns it claims to describe.
    :param on_change: called as ``on_change(old, new)`` after every accepted
        transition. Exceptions from it are logged and swallowed: an observer
        that fails must not corrupt the state it was observing.
    """

    def __init__(self, name="", ready=None, on_change=None):
        self.name = name or "link"
        self._state = IDLE
        self._ready = ready
        self._on_change = on_change

        #: Why the last ``FAILED`` happened, for the tooltip. ``None`` unless
        #: the current state is ``FAILED``.
        self.error = None

        #: How many requests have been refused. Purely diagnostic, but it is
        #: what a test asserts on to prove a double-click did nothing.
        self.rejected = 0

    # -- reading ------------------------------------------------------------

    def __repr__(self):
        return "<LinkState %s=%s>" % (self.name, self._state)

    @property
    def state(self):
        return self._state

    @property
    def is_busy(self):
        """A request is in flight; the row must not be touched."""
        return self._state in BUSY

    @property
    def is_live(self):
        return self._state == LIVE

    @property
    def is_ready(self):
        """The row has a complete selection, so a join is even meaningful.

        Never raises: this is consulted from paint-adjacent code, and the
        conservative answer to "is the selection complete?" is "no".
        """
        if self._ready is None:
            return True

        try:
            return bool(self._ready())
        except Exception:
            log.exception("%s: readiness check failed", self.name)
            return False

    def can(self, target):
        """Is ``state -> target`` in the table?"""
        return target in TRANSITIONS.get(self._state, frozenset())

    # -- writing ------------------------------------------------------------

    def to(self, target, reason=None):
        """Attempt a transition. Returns whether it happened.

        Refuses, logs and returns ``False`` for an unknown target, for a
        self-loop, and for anything the table does not allow. It does not
        raise -- see the module docstring.
        """
        if target not in TRANSITIONS:
            self.rejected += 1
            log.error("%s: refusing transition to unknown state %r",
                      self.name, target)
            return False

        if not self.can(target):
            self.rejected += 1
            log.debug("%s: refusing %s -> %s%s", self.name, self._state,
                      target, (" (%s)" % reason) if reason else "")
            return False

        old = self._state
        self._state = target
        self.error = reason if target == FAILED else None

        log.info("%s: %s -> %s%s", self.name, old, target,
                 (" (%s)" % reason) if reason else "")

        if self._on_change is not None:
            try:
                self._on_change(old, target)
            except Exception:
                log.exception("%s: state observer failed on %s -> %s",
                              self.name, old, target)

        return True

    # -- the intents the GUI actually expresses ------------------------------
    #
    # These exist so that no caller has to know which transitions implement
    # "the user pressed Connect". They are also the synchronisation point:
    # begin_connect() flips the state *before* the coroutine that does the
    # joining is even scheduled, which is what makes a double-click
    # impossible rather than merely unlikely.

    def begin_connect(self):
        """Claim the row for a join. ``False`` means do not start one."""
        if not self.is_ready:
            self.rejected += 1
            log.debug("%s: connect refused, selection incomplete", self.name)
            return False

        return self.to(CONNECTING, "connect requested")

    def finish_connect(self):
        """The join succeeded."""
        return self.to(LIVE, "joined")

    def fail(self, reason="connect failed"):
        """The join (or a live row) broke."""
        return self.to(FAILED, reason)

    def begin_disconnect(self):
        """Claim the row for a leave. ``False`` means do not start one."""
        return self.to(DISCONNECTING, "disconnect requested")

    def finish_disconnect(self):
        """The leave finished, however it went. Always lands in IDLE."""
        return self.to(IDLE, "left voice")

    def clear_failure(self):
        """Drop a ``FAILED`` back to ``IDLE``; a no-op in any other state.

        Called when the user changes a selection: the failure described the
        *old* selection, and leaving the button reading "Retry" for a channel
        they have since changed their mind about is a stale claim.
        """
        if self._state != FAILED:
            return False

        return self.to(IDLE, "selection changed")
