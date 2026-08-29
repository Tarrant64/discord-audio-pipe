import os
import sys
import functools
import sound
import asyncio
import config
import instrumentation
import logging_setup
import logging
import discord
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtGui import (
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QCursor,
    QPalette,
)
from PyQt6.QtCore import Qt, QCoreApplication, QEventLoop, QDir, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow,
    QPushButton,
    QWidget,
    QFrame,
    QGridLayout,
    QComboBox,
    QCheckBox,
    QLabel,
    QHBoxLayout,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QStyledItemDelegate,
    QListView
)

if getattr(sys, "frozen", False):
    bundle_dir = sys._MEIPASS
else:
    bundle_dir = os.path.dirname(os.path.abspath(__file__))


# ===========================================================================
# Stream health thresholds
# ===========================================================================
#
# *** PROVISIONAL. PENDING CALIBRATION AGAINST REAL DEGRADATION DATA. ***
#
# We do not yet know what a degrading DAP stream looks like. The entire
# dataset is two clean runs -- one 19-minute silent run and one ~30-minute
# music run -- and zero samples from a stream on its way down. Every number
# below is therefore a guess, chosen deliberately loose.
#
# The bias is intentional and should stay until real numbers replace it:
# a strip that under-warns costs one missed early warning, while a strip
# that cries wolf gets ignored within a day, and an ignored readout is
# worse than none at all -- it is a readout the user has been trained to
# disbelieve.
#
# Recalibrate by capturing a session that actually degrades (--diagnose,
# then read the 5-second lines in DAP_session.log from the last clean
# minute through the failure) and setting WARN to roughly where the metric
# leaves its steady-state band.
#
# All thresholds live here and nowhere else.

#: discord.py voice websocket heartbeat round-trip.
LATENCY_WARN_MS = 300.0
LATENCY_FAIL_MS = 1000.0

#: Frames-read vs. wall-clock divergence. Upstream issue #21 reports ~1296
#: ppm on a stream that is working fine, so WARN sits well above that.
#: 10 000 ppm is 1%, i.e. 600 ms of audio lost per minute.
DRIFT_WARN_PPM = 2000.0
DRIFT_FAIL_PPM = 10000.0

#: Cumulative input-side dropouts: PortAudio ring overflows plus empty
#: reads. A healthy run in our two samples produced zero of either.
DROPS_WARN = 10
DROPS_FAIL = 100

#: Worst blocking time of a single PortAudio read() in the last second.
#: One read covers 20 ms of audio, so anything past ~60 ms means the device
#: is not keeping up; half a second means it has effectively stopped.
READ_BLOCK_WARN_MS = 60.0
READ_BLOCK_FAIL_MS = 500.0

#: Age of the newest metric sample. The audio thread publishes every 1 s and
#: the voice poller every 5 s, so these allow several missed cycles before
#: complaining.
VOICE_STALE_WARN_S = 12.0
VOICE_STALE_FAIL_S = 30.0
AUDIO_STALE_WARN_S = 5.0
AUDIO_STALE_FAIL_S = 15.0

#: How often the strip repaints. Two seconds is fast enough to watch a
#: metric move and slow enough that the numbers are readable.
STATUS_REFRESH_MS = 2000

#: How long after the last dropdown change the profile is written. Every
#: change schedules a save and restarts this timer, so dragging through a
#: combo box with the arrow keys -- which fires currentIndexChanged for each
#: item passed -- costs one write at the end instead of one per item. Short
#: enough that a normal click-then-quit still lands (and closeEvent flushes
#: anything still pending regardless).
PROFILE_SAVE_DEBOUNCE_MS = 750

#: Grid row the strip occupies. Deliberately far below any connection row
#: (which are numbered 2, 3, 4, ... as rows are added); QGridLayout gives
#: the empty rows in between zero height.
STATUS_ROW = 900

# State keys. These are also the QSS ``[state="..."]`` selector values, so
# changing one means changing assets/style.qss too.
STATE_IDLE = "idle"
STATE_OK = "ok"
STATE_WARN = "warn"
STATE_FAIL = "fail"


# ===========================================================================
# Slot safety
# ===========================================================================


def guarded_slot(method):
    """Stop an exception in ``method`` from escaping back into Qt.

    **This is the crash guard.** Under PyQt5 an exception that escaped a
    Python slot was printed and execution continued. PyQt6 routes it through
    ``pyqt6_err_print()``, which prints it and then calls ``qFatal()`` ->
    ``abort()``. The process dies where it stands: no Python traceback, no
    flushed log, just a native "Abort trap: 6" that a user has no reason to
    send us. The PyQt5 -> PyQt6 migration turned every unguarded slot in
    this file into a potential silent crash.

    A replaced ``sys.excepthook`` is *not* a reliable substitute. It does get
    invoked -- which is why we still install one, to capture the traceback --
    but whether control ever returns to Python afterwards is a property of
    the PyQt build. On PyQt6 6.11.0 / Qt 6.11.0 the abort was observed not to
    happen with a hook installed; crash reports from another machine show
    ``qFatal`` being reached anyway. So the hook is treated as diagnostics
    only, and never as the thing that keeps the app alive.

    The only thing that works everywhere is the exception not escaping. This
    decorator is applied to every method connected to a Qt signal, and to the
    reimplemented virtuals Qt calls directly, so the guarantee is structural
    rather than a matter of remembering.

    Returns ``None`` on failure, so it suits void slots. Methods that must
    return a value to Qt (``sizeHint`` and friends) keep a hand-written
    try/except with a meaningful fallback instead.

    One consequence to know about: PyQt normally introspects a slot's
    arity and passes only as many signal arguments as it will accept.
    The wrapper takes ``*args``, so that negotiation stops working and
    every argument is passed. Decorated slots therefore have to accept
    what their signal actually sends -- hence the ``*_`` on the ones
    wired to ``clicked`` (which carries ``checked``) and to
    ``Dropdown.changed`` (which carries two). Getting this wrong is a
    TypeError the decorator itself swallows, i.e. a button that quietly
    stops working, so it is covered by a test.
    """

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            logging.exception(
                "Unhandled exception in slot %s -- contained, continuing",
                getattr(method, "__qualname__", method),
            )
            return None

    return wrapper


class Dropdown(QComboBox):
    """Combo box that fits its contents, within limits, and elides the rest.

    The closed field shows text the user did not choose the length of --
    device names come from PortAudio, server and channel names come from
    whatever the guild owner typed. Sizing therefore has to satisfy three
    things at once:

    * **Fit the content when it reasonably can.** ``AdjustToContents`` makes
      Qt measure the widest item *including* the real style chrome (frame,
      left padding, arrow well). The hand-rolled "text width + 30" this
      class used to rely on under-counted the stylesheet's 12px left pad
      plus its 32px arrow well by roughly 16px, which is precisely how
      "A pack of autism" came out clipped to "A pack of auti" on Windows.
    * **Never let one long name drag the window off the screen.**
      ``MAX_CHARS`` caps what the field will *ask* for.
    * **Stay readable when it is capped or squeezed.** Anything that does
      not fit is elided with an ellipsis and recovered through the tooltip,
      so a clipped name is still discoverable rather than silently lost.
    """

    changed = pyqtSignal(object, object)

    #: Width caps for the closed field, in "average character" widths of the
    #: current font. MAX_CHARS bounds what the field asks for; MIN_CHARS is
    #: the floor the layout may compress it to if the window cannot be given
    #: its full hint, so a cramped window degrades into more elision rather
    #: than into a horizontal scroll or an off-screen mute button.
    MAX_CHARS = 20
    MIN_CHARS = 9

    def __init__(self):
        super(Dropdown, self).__init__()

        self.setItemDelegate(QStyledItemDelegate())
        self.setPlaceholderText("None")
        self.setView(QListView())

        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        self.deselected = None
        self.currentIndexChanged.connect(self.changed_signal)
        self.currentIndexChanged.connect(lambda _: self.sync_tooltip())

    # -- sizing ------------------------------------------------------------

    def chrome_width(self):
        """Width the field needs on top of its text.

        Derived rather than hard-coded: Qt's own content-based size hint
        minus the widest item's text advance is, by definition, everything
        that is not text -- frame, padding and arrow well -- whatever the
        active stylesheet has set those to.
        """
        metrics = QFontMetrics(self.font())
        widest = 0

        for i in range(self.count()):
            widest = max(widest, metrics.horizontalAdvance(self.itemText(i)))

        return max(0, super().sizeHint().width() - widest)

    def width_for(self, chars):
        metrics = QFontMetrics(self.font())
        return self.chrome_width() + metrics.averageCharWidth() * chars

    # These two are called by Qt's layout engine, not by our code, so they
    # carry the same "an exception here aborts the process" hazard as a slot
    # -- and the layout runs constantly during a profile restore. They cannot
    # use @guarded_slot because they must hand a QSize back to C++; instead
    # the custom narrowing is guarded and Qt's own hint is the fallback.

    def sizeHint(self):
        hint = super().sizeHint()
        try:
            hint.setWidth(min(hint.width(), self.width_for(self.MAX_CHARS)))
        except Exception:
            logging.exception("Error computing dropdown size hint")
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        try:
            hint.setWidth(min(hint.width(), self.width_for(self.MIN_CHARS)))
        except Exception:
            logging.exception("Error computing dropdown minimum size hint")
        return hint

    # -- elision -----------------------------------------------------------

    def field_width(self):
        """Pixels available for the current item's text, arrow excluded."""
        option = QStyleOptionComboBox()
        self.initStyleOption(option)

        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        ).width()

    @guarded_slot
    def sync_tooltip(self):
        """Expose the full text through the tooltip iff it is being elided.

        Reached from ``currentIndexChanged`` and from ``resizeEvent``, so it
        is a slot in all but name and must not raise (PyQt6 answers an
        escaping exception with abort()).
        """
        try:
            text = self.currentText()
            available = self.field_width()

            if not text or available <= 0:
                self.setToolTip("")
                return

            metrics = QFontMetrics(self.font())
            self.setToolTip(
                text if metrics.horizontalAdvance(text) > available else ""
            )
        except Exception:
            logging.exception("Error syncing dropdown tooltip")

    @guarded_slot
    def refresh(self):
        """Re-measure after the item list changed."""
        self.updateGeometry()
        self.sync_tooltip()

    @guarded_slot
    def resizeEvent(self, event):
        # The tooltip depends on how much room the layout actually granted,
        # which is only known once the widget has been given its geometry.
        super().resizeEvent(event)
        self.sync_tooltip()

    @guarded_slot
    def paintEvent(self, event):
        # Mirrors QComboBox::paintEvent, with the label text elided to the
        # edit field before it is drawn. Qt's own implementation clips
        # instead, which is what produced a name cut mid-word with no
        # ellipsis to signal that anything was missing.
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))

        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)

        available = self.field_width()
        if available > 0:
            metrics = QFontMetrics(self.font())
            option.currentText = metrics.elidedText(
                option.currentText, Qt.TextElideMode.ElideRight, available
            )

        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)

    @guarded_slot
    def changed_signal(self, selected):
        # Slot. Anything that escapes here goes back into Qt, and PyQt6
        # answers that with abort() -- see
        # logging_setup.install_excepthook(). Nothing in this file that is
        # reachable from a signal may raise.
        try:
            self.changed.emit(self.deselected, selected)
            self.deselected = selected
        except Exception:
            logging.exception("Error emitting dropdown change")

    def setRowHidden(self, idx, hidden):
        self.view().setRowHidden(idx, hidden)


class SVGButton(QPushButton):
    def __init__(self, text=None):
        super(SVGButton, self).__init__(text)

        self.layout = QHBoxLayout()
        self.setLayout(self.layout)

        self.svg = QSvgWidget("./assets/loading.svg", self)
        self.svg.setVisible(False)

        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.addWidget(self.svg)

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.svg.setVisible(not enabled)


class Connection:
    def __init__(self, layer, parent):
        self.stream = instrumentation.make_stream()
        self.parent = parent
        self.voice = None
        self.poller = None

        #: Mirrors the mute button, so the saved profile does not have to
        #: interrogate a VoiceClient that may not exist. ``is_playing()``
        #: cannot stand in for this: it is also False when nothing has been
        #: started at all, and it famously keeps returning True on a dead
        #: player thread (see logging_setup's promotion filter).
        self.muted = False

        # dropdowns
        self.devices = Dropdown()
        self.servers = Dropdown()
        self.channels = Dropdown()

        # No trailing-space padding on the label: it existed only to bully a
        # bit of extra width out of a combo box that was not measuring its
        # own contents. Dropdown does that properly now, and the padding
        # skewed the measurement it feeds.
        for device, idx in parent.devices.items():
            self.devices.addItem(device, idx)

        # mute
        self.mute = SVGButton("Mute")
        self.mute.setObjectName("mute")
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)

        # add widgets
        parent.layout.addWidget(self.devices, layer, 0)
        parent.layout.addWidget(self.servers, layer, 1)
        parent.layout.addWidget(self.channels, layer, 2)
        parent.layout.addWidget(self.mute, layer, 3)

        # events
        #
        # The two async ones go through guarded methods rather than bare
        # lambdas. asyncio.create_task() raises RuntimeError when there is
        # no running loop, and an exception escaping a slot back into Qt is
        # fatal under PyQt6 -- it calls abort(), taking the app down with no
        # traceback in any log file. See
        # logging_setup.install_excepthook().
        self.devices.changed.connect(self.change_device)
        self.servers.changed.connect(self.on_server_changed)
        self.channels.changed.connect(self.on_channel_changed)
        self.mute.clicked.connect(self.toggle_mute)

    # -- slots --------------------------------------------------------------

    @guarded_slot
    def on_server_changed(self, deselected, selected):
        try:
            asyncio.create_task(self.change_server(deselected, selected))
        except Exception:
            logging.exception("Error dispatching change_server")

    @guarded_slot
    def on_channel_changed(self, *_):
        try:
            asyncio.create_task(self.change_channel())
        except Exception:
            logging.exception("Error dispatching change_channel")

    @staticmethod
    def resize_combobox(combobox):
        """Tell a dropdown its item list changed so it can re-measure.

        This used to compute a minimum width by hand as "widest item + 30px"
        and pin it with setMinimumWidth(). That was wrong twice over: 30px
        does not cover the stylesheet's 12px left pad plus 32px arrow well,
        so long names were clipped; and a hard minimum width meant a long
        name could push the window wider without limit -- and could never
        let it back down again.

        Dropdown owns the width policy now, but something still has to make
        the window act on it: the server list arrives after the window has
        already been shown and sized, and a size *hint* alone does not
        resize a window that already has a size. So grow the window to its
        hint here, which the old minimum width did implicitly.

        Growing only, never shrinking, so switching to a shorter server name
        does not make the window jump about mid-session. It stays bounded
        because Dropdown caps every column's hint.
        """
        combobox.refresh()

        window = combobox.window()
        if window is None:
            return

        hint = window.sizeHint().width()
        if hint > window.width():
            window.resize(hint, window.height())

    def setEnabled(self, enabled):
        self.devices.setEnabled(enabled)
        self.servers.setEnabled(enabled)
        self.channels.setEnabled(enabled)

        self.mute.setEnabled(enabled)
        self.mute.setText("Mute" if enabled else "")

    def set_servers(self, guilds):
        for guild in guilds:
            self.servers.addItem(guild.name, guild)

    # -- saved profile ------------------------------------------------------

    def snapshot(self):
        """This row's state, in the shape :mod:`config` persists.

        The audio device is recorded by **name**, never by index. PortAudio
        hands out indices in enumeration order, and that order shifts when
        hardware is plugged in or removed, when a driver updates, and
        sometimes across a plain reboot. A remembered index would therefore
        eventually point at a different device -- and the next thing this app
        does with a device index is stream it into a voice channel, so
        "different device" can mean broadcasting a live microphone. The name
        is stored exactly as PortAudio reported it (see
        ``config.normalize_row``) and re-resolved on the next launch.

        Never raises: a failure here must not break the dropdown change the
        user actually asked for.
        """
        row = {"device_name": None, "guild_id": None,
               "channel_id": None, "muted": bool(self.muted)}

        try:
            if self.devices.currentData() is not None:
                row["device_name"] = self.devices.itemText(
                    self.devices.currentIndex()
                )

            guild = self.servers.currentData()
            row["guild_id"] = getattr(guild, "id", None)

            channel = self.channels.currentData()
            row["channel_id"] = getattr(channel, "id", None)

        except Exception:
            logging.exception("Error building profile snapshot")

        return row

    @guarded_slot
    def change_device(self, *_):
        try:
            selection = self.devices.currentData()
            logging_setup.log_device(selection, self.devices.currentText().strip())
            self.mute.setText("Mute")
            self.muted = False

            if self.voice is not None:
                self.voice.stop()
                self.stream.change_device(selection)

                if self.voice.is_connected():
                    self.voice.play(self.stream, fec=False, signal_type='music', after=instrumentation.make_after())

            else:
                self.stream.change_device(selection)

        except Exception:
            logging.exception("Error on change_device")

        finally:
            self.parent.profile_changed()

    async def change_server(self, deselcted, selected):
        try:
            selection = self.servers.itemData(selected)

            self.parent.exclude(deselcted, selected)
            self.channels.clear()
            self.channels.addItem("None", None)

            for channel in selection.channels:
                if isinstance(channel, discord.VoiceChannel) or isinstance(channel, discord.StageChannel):
                    self.channels.addItem(channel.name, channel)

            Connection.resize_combobox(self.channels)

        except Exception:
            logging.exception("Error on change_server")

        finally:
            self.parent.profile_changed()

    async def change_channel(self):
        try:
            selection = self.channels.currentData()
            self.mute.setText("Mute")
            self.muted = False
            self.setEnabled(False)

            if selection is not None:
                not_connected = (
                    self.voice is None
                    or self.voice is not None
                    and not self.voice.is_connected()
                )

                if not_connected:
                    self.voice = await selection.connect(timeout=10)
                else:
                    await self.voice.move_to(selection)

                logging_setup.log_event("joined %s / %s", selection.guild, selection)
                self.poller = instrumentation.attach(self.voice, f" [{selection}]", self.poller)

                not_playing = (
                    self.devices.currentData() is not None
                    and not self.voice.is_playing()
                )

                if not_playing:
                    self.voice.play(self.stream, fec=False, signal_type='music', after=instrumentation.make_after(f" [{selection}]"))

            else:
                if self.voice is not None:
                    logging_setup.log_event("leaving voice (channel set to None)")
                    self.poller = instrumentation.detach(self.poller)
                    await self.voice.disconnect()

        except asyncio.TimeoutError:
            logging.exception("Timed out connecting to channel. The bot may not have permissions to join the channel due to custom roles.")

        except Exception:
            logging.exception("Error on change_channel")

        finally:
            self.setEnabled(True)
            self.parent.profile_changed()

    @guarded_slot
    def toggle_mute(self, *_):
        try:
            if self.voice is not None:
                if self.voice.is_playing():
                    self.voice.pause()
                    self.mute.setText("Resume")
                    self.muted = True
                else:
                    self.voice.resume()
                    self.mute.setText("Mute")
                    self.muted = False

        except Exception:
            logging.exception("Error on toggle_mute")

        finally:
            self.parent.profile_changed()


def _fmt_uptime(seconds):
    if seconds is None:
        return "--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class StatusStrip(QFrame):
    """One-line live health readout for the audio stream.

    Reads :func:`instrumentation.snapshot`, a pre-built dict that the audio
    thread and the voice poller each rebind on their own cadence. The timer
    callback therefore does no measurement, no probing, no I/O and no
    locking beyond copying two short lists -- it formats numbers that were
    already computed elsewhere. Nothing here can block the player thread or
    the asyncio loop.

    Three rules the rendering obeys:

    * **State is in the text, not only the colour.** The word "Live" /
      "Degraded" / "Stalled" / "Idle" always appears; the dot and the text
      colour are redundant reinforcement, so the strip still works for a
      colour-blind user or in a screenshot pasted into a bug report.
    * **Unknown renders as "--", never as a stale number.** If a probe has
      retired (discord.py renamed an internal) or nothing has been sampled
      yet, the field shows "--". Showing last-known values as if they were
      live is how a health readout starts lying.
    * **It never raises.** A failure inside the timer callback is caught,
      logged once, and turns the strip into an honest "unavailable".
    """

    def __init__(self, parent_gui, cfg):
        super(StatusStrip, self).__init__()
        self.setObjectName("status")

        self.gui = parent_gui
        self.config = cfg
        self._error_logged = False

        layout = QHBoxLayout()
        layout.setContentsMargins(12, 6, 10, 6)
        layout.setSpacing(6)
        self.setLayout(layout)

        self.dot = QLabel("●")
        self.dot.setObjectName("status_dot")

        self.state_lb = QLabel("Idle")
        self.state_lb.setObjectName("status_state")

        self.detail_lb = QLabel("")
        self.detail_lb.setObjectName("status_detail")

        # Re-join the saved channel on launch. This one is wired all the way
        # through -- see GUI.restore_discord() -- so it gets a plain label
        # and no "(soon)" hedge. It sits to the left of Auto-recover so the
        # working control reads first.
        self.autoconnect_cb = QCheckBox("Auto-connect on launch")
        self.autoconnect_cb.setObjectName("autoconnect")
        self.autoconnect_cb.setProperty("role", "toggle")
        self.autoconnect_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.autoconnect_cb.setToolTip(
            "Re-join the server and channel you last used, automatically,\n"
            "as soon as the bot finishes logging in.\n\n"
            "Off by default. Your last setup is always saved and always\n"
            "pre-filled into the dropdowns either way -- this only controls\n"
            "whether the app joins voice without being asked. Joining is\n"
            "audible to everyone in the channel, so it is opt-in."
        )
        self.autoconnect_cb.setChecked(bool(cfg.auto_connect))
        self.autoconnect_cb.toggled.connect(self.on_auto_connect_toggled)

        # Honest label. The setting persists; the behaviour does not exist
        # yet, and the strip must not imply otherwise.
        self.auto_cb = QCheckBox("Auto-recover (soon)")
        self.auto_cb.setObjectName("autorecover")
        self.auto_cb.setProperty("role", "toggle")
        self.auto_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_cb.setToolTip(
            "Not active yet.\n\n"
            "Ticking this only saves the preference to DAP_config.json.\n"
            "Automatic restart of a stalled stream is a later change; until\n"
            "then a stall still needs a manual reconnect."
        )
        self.auto_cb.setChecked(bool(cfg.auto_recover))
        self.auto_cb.toggled.connect(self.on_auto_recover_toggled)

        layout.addWidget(self.dot)
        layout.addWidget(self.state_lb)
        layout.addWidget(self.detail_lb)
        layout.addStretch()
        layout.addWidget(self.autoconnect_cb)
        layout.addWidget(self.auto_cb)

        self._apply_state(STATE_IDLE)

        self.timer = QTimer(self)
        self.timer.setInterval(STATUS_REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    # -- settings -----------------------------------------------------------

    @guarded_slot
    def on_auto_recover_toggled(self, checked):
        try:
            self.config.set("auto_recover", bool(checked))
            logging_setup.log_event("setting auto_recover=%s (not yet active)", bool(checked))
        except Exception:
            logging.exception("Error saving auto_recover setting")

    @guarded_slot
    def on_auto_connect_toggled(self, checked):
        try:
            self.config.set("auto_connect", bool(checked))
            logging_setup.log_event("setting auto_connect=%s", bool(checked))
        except Exception:
            logging.exception("Error saving auto_connect setting")

    # -- classification -----------------------------------------------------

    @staticmethod
    def evaluate(snap):
        """Turn a snapshot into ``(state, word, detail, tooltip_lines)``.

        Pure and side-effect free, which is what makes the four rendered
        states testable without a Discord connection.
        """
        latency = snap.get("latency_ms")
        drops = snap.get("drops")
        drift = snap.get("drift_ppm")
        uptime = snap.get("uptime_s")
        read_max = snap.get("read_ms_max")
        paused = snap.get("paused") is True

        detail = " · ".join((
            f"{latency:.0f}ms" if latency is not None else "--ms",
            f"{drops} drops" if drops is not None else "-- drops",
            f"drift {drift:+.0f}ppm" if drift is not None else "drift --",
            _fmt_uptime(uptime),
        ))

        tip = [
            "Live stream health, refreshed every"
            f" {STATUS_REFRESH_MS // 1000}s.",
            "",
            f"voice latency   {latency:.1f} ms" if latency is not None
            else "voice latency   -- (not sampled)",
            f"dropouts        {drops}" if drops is not None
            else "dropouts        -- (no audio device)",
            f"clock drift     {drift:+.0f} ppm" if drift is not None
            else "clock drift     --",
            f"slowest read    {read_max:.1f} ms" if read_max is not None
            else "slowest read    --",
            f"connections     {snap.get('connections', 0)}",
        ]

        dead = snap.get("dead_probes") or ()
        if dead:
            tip += [
                "",
                "Retired probes (discord.py internals moved; these fields"
                " show -- for the rest of the run):",
                "  " + ", ".join(sorted(dead)),
            ]

        tip += [
            "",
            "Thresholds are provisional and deliberately generous;"
            " they have not been calibrated against a real failure yet.",
        ]

        # -- nothing to report -------------------------------------------
        if not snap.get("connections"):
            return STATE_IDLE, "Idle", "not connected", tip
        if not snap.get("voice_samples"):
            return STATE_IDLE, "Connecting", "waiting for first sample", tip

        # -- hard failures, in order of certainty --------------------------
        if snap.get("connected") is False:
            return STATE_FAIL, "Disconnected", detail, tip
        if snap.get("stalled"):
            return (STATE_FAIL, "Stalled",
                    detail + " · player thread died", tip)
        if snap.get("parked"):
            return (STATE_FAIL, "Stalled",
                    detail + " · player thread blocked", tip)

        voice_age = snap.get("voice_age_s")
        if voice_age is not None and voice_age > VOICE_STALE_FAIL_S:
            return STATE_FAIL, "No data", detail, tip

        if paused:
            # A user-initiated mute stops read() entirely, so audio metrics
            # legitimately go stale here. Say so instead of alarming.
            return STATE_IDLE, "Muted", _fmt_uptime(uptime), tip

        audio_age = snap.get("audio_age_s")
        if snap.get("audio_samples"):
            if audio_age is not None and audio_age > AUDIO_STALE_FAIL_S:
                return STATE_FAIL, "Audio stopped", detail, tip

        if snap.get("playing") is False:
            return STATE_IDLE, "Not playing", detail, tip

        # If every measurable field came back None -- no audio device and a
        # retired latency probe -- we know nothing. Saying "Live" here would
        # be reporting health we have not observed, which is exactly the
        # failure mode this strip exists to prevent.
        if latency is None and drops is None and drift is None and read_max is None:
            return STATE_IDLE, "No metrics", detail, tip

        # -- graded numeric checks -----------------------------------------
        warn = False
        if latency is not None:
            if latency >= LATENCY_FAIL_MS:
                return STATE_FAIL, "Failing", detail, tip
            warn = warn or latency >= LATENCY_WARN_MS
        if drops is not None:
            if drops >= DROPS_FAIL:
                return STATE_FAIL, "Failing", detail, tip
            warn = warn or drops >= DROPS_WARN
        if drift is not None:
            if abs(drift) >= DRIFT_FAIL_PPM:
                return STATE_FAIL, "Failing", detail, tip
            warn = warn or abs(drift) >= DRIFT_WARN_PPM
        if read_max is not None:
            if read_max >= READ_BLOCK_FAIL_MS:
                return STATE_FAIL, "Failing", detail, tip
            warn = warn or read_max >= READ_BLOCK_WARN_MS
        if voice_age is not None:
            warn = warn or voice_age > VOICE_STALE_WARN_S
        if audio_age is not None:
            warn = warn or audio_age > AUDIO_STALE_WARN_S

        if warn:
            return STATE_WARN, "Degrading", detail, tip
        return STATE_OK, "Live", detail, tip

    # -- rendering ----------------------------------------------------------

    def _apply_state(self, state):
        """Set the ``state`` dynamic property and force a restyle.

        Qt does not re-evaluate attribute selectors on a property change by
        itself, hence the unpolish/polish pair.
        """
        for widget in (self.dot, self.state_lb):
            if widget.property("state") != state:
                widget.setProperty("state", state)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    @guarded_slot
    def refresh(self, snap=None):
        try:
            if snap is None:
                snap = instrumentation.snapshot()
            state, word, detail, tip = self.evaluate(snap)
        except Exception:
            # A broken readout must never take the app with it, and must
            # never keep showing the last good numbers as if they were live.
            if not self._error_logged:
                self._error_logged = True
                logging.exception("Error refreshing status strip")
            self._apply_state(STATE_IDLE)
            self.state_lb.setText("Unavailable")
            self.detail_lb.setText("health readout failed — see DAP_errors.log")
            self.gui.grow_to_hint()
            return

        self._apply_state(state)
        self.state_lb.setText(word)
        self.detail_lb.setText("· " + detail if detail else "")
        self.setToolTip("\n".join(tip))

        # The detail text just changed length; make sure the window is wide
        # enough for it rather than letting the label be squeezed.
        self.gui.grow_to_hint()


class TitleBar(QFrame):
    def __init__(self, parent):
        # title bar
        super(TitleBar, self).__init__()
        self.setObjectName("titlebar")

        # discord
        self.parent = parent
        self.bot = parent.bot

        # layout
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        # window title
        title = QLabel("Discord Audio Pipe")

        # minimize
        minimize_button = QPushButton("—")
        minimize_button.setObjectName("minimize")
        minimize_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # close
        close_button = QPushButton("✕")
        close_button.setObjectName("close")
        close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # add widgets
        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(minimize_button)
        layout.addWidget(close_button)

        # events
        minimize_button.clicked.connect(self.minimize)
        close_button.clicked.connect(self.on_close_clicked)

    @guarded_slot
    def on_close_clicked(self, *_):
        # Attribute the shutdown at the only place that knows the user asked
        # for it. First-writer-wins, so this beats the generic outer cause.
        logging_setup.note_shutdown("window-close-button")
        # Same guard as Connection's async slots: create_task() raises
        # RuntimeError with no running loop, and under PyQt6 that would
        # abort the process instead of just failing to close the window.
        try:
            asyncio.create_task(self.close())
        except Exception:
            logging.exception("Error dispatching window close")

    async def close(self):
        await self.bot.close()
        self.parent.close()

    @guarded_slot
    def minimize(self, *_):
        self.parent.showMinimized()


class GUI(QMainWindow):
    def __init__(self, app, bot):
        # app
        super(GUI, self).__init__()
        QDir.setCurrent(bundle_dir)
        self.app = app

        # window info
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        window_icon = QIcon("./assets/favicon.ico")
        self.setWindowTitle("Discord Audio Pipe")
        self.app.setWindowIcon(window_icon)
        self.position = None

        # discord
        self.bot = bot

        # user settings. Loaded before QDir.setCurrent below, but config
        # resolved its own directory at import time anyway, so the file
        # lands next to token.txt either way.
        self.config = config.load()

        # Saved setup, read once. Held rather than re-read because the
        # restore happens in two phases (devices now, servers/channels after
        # the bot logs in) and both must see the same list -- by the time
        # phase two runs, phase one's dropdown changes have already been
        # written back to the file.
        self._profile_rows = self.config.profile

        # True while a restore is driving the dropdowns, so the change
        # handlers do not save a half-restored profile back over the real
        # one. Set before any widget is touched and cleared in a finally.
        self._restoring = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(PROFILE_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.save_profile)

        # layout
        central = QWidget()
        self.layout = QGridLayout()
        self.layout.setSpacing(10)
        self.layout.setContentsMargins(20, 16, 20, 20)
        central.setLayout(self.layout)

        # Deliberately no column stretch factors.
        #
        # QGridLayout has two distribution modes. With stretch factors set it
        # divides the whole width in the stretch ratio, so three equally
        # weighted dropdown columns come out the same width whatever they
        # contain -- Channels sitting on 70px of slack showing "General"
        # while Devices elides a 31-character name. With no stretch factors
        # it gives every column its size hint first and shares only the
        # surplus, which is what we want: each column ends up as wide as its
        # own content needs, up to Dropdown's cap.
        #
        # This works because the window is frameless and has no resize grip,
        # so it is only ever at the width resize_combobox() gave it -- there
        # is essentially no surplus to argue over.

        # labels
        self.info = QLabel("Connecting...")
        self.info.setObjectName("info")
        device_lb = QLabel("Devices")
        device_lb.setObjectName("label")
        server_lb = QLabel("Servers")
        server_lb.setObjectName("label")
        channel_lb = QLabel("Channels")
        channel_lb.setObjectName("label")

        # connections
        self.devices = sound.query_devices()
        self.connections = [Connection(2, self)]
        self.connected_servers = set()

        # new connections
        self.connection_btn = QPushButton("＋", self)
        self.connection_btn.setObjectName("connection_btn")
        self.connection_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # live health readout, pinned below every connection row
        self.status = StatusStrip(self, self.config)

        # add widgets
        self.layout.addWidget(self.info, 0, 0, 1, 3)
        self.layout.addWidget(device_lb, 1, 0)
        self.layout.addWidget(server_lb, 1, 1)
        self.layout.addWidget(channel_lb, 1, 2)
        self.layout.addWidget(self.connection_btn, 2, 4)
        self.layout.addWidget(self.status, STATUS_ROW, 0, 1, 5)

        # events
        self.connection_btn.clicked.connect(self.add_connection)

        # Phase one of the profile restore: rows and audio devices. Done
        # here, not in ready(), because none of it needs the bot -- the
        # user's device selection comes back even if Discord is unreachable
        # or the token is wrong.
        self.restore_devices()

        # build window
        titlebar = TitleBar(self)
        titlebar.setFixedHeight(36)
        self.setMenuWidget(titlebar)
        self.setCentralWidget(central)
        self.setEnabled(False)

        # load fonts
        #
        # Absolute paths, deliberately. QFontDatabase.addApplicationFont()
        # silently returns -1 for a relative path on macOS even with
        # QDir.setCurrent() pointing at the bundle, so the app fell back to
        # the platform UI font there while loading correctly on Windows.
        # That platform split is exactly what hid the weight problem below:
        # the development machine never rendered the bundled face at all.
        #
        # All three faces carry typographic family "Roboto" with styles
        # Regular / Medium / Black, so they register as one family and the
        # stylesheet picks between them with font-weight alone. Registering
        # only Roboto-Black.ttf, as this did before, left 900 as the
        # family's only face, and every weight request -- including the
        # default 400 -- snapped to it. That is why the Windows build came
        # out uniformly heavy.
        #
        # A face that fails to load is not fatal (Qt substitutes), but it is
        # never silent again.
        for face in ("Roboto-Regular.ttf", "Roboto-Medium.ttf", "Roboto-Black.ttf"):
            path = os.path.join(bundle_dir, "assets", face)

            if QFontDatabase.addApplicationFont(path) == -1:
                logging.warning("could not load bundled font: %s", path)

        # load styles
        with open("./assets/style.qss", "r") as qss:
            self.app.setStyleSheet(qss.read())

        # show window
        self.show()

    @guarded_slot
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.position = event.position().toPoint()
            event.accept()

    @guarded_slot
    def mouseMoveEvent(self, event):
        if self.position is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(QCursor.pos() - self.position)
            event.accept()

    @guarded_slot
    def mouseReleaseEvent(self, event):
        self.position = None
        event.accept()

    def setEnabled(self, enabled):
        self.connection_btn.setEnabled(enabled)
        for connection in self.connections:
            connection.setEnabled(enabled)

    @guarded_slot
    def add_connection(self, *_):
        # Slot ("+" button) and also called directly by restore_devices().
        # Guarded because an exception escaping a slot is fatal under PyQt6
        # -- see logging_setup.install_excepthook().
        try:
            layer = len(self.connections) + 2

            new_connection = Connection(layer, self)
            new_connection.set_servers(self.bot.guilds)

            for idx in range(new_connection.servers.count()):
                if idx in self.connected_servers:
                    new_connection.servers.setRowHidden(idx, True)

            self.layout.removeWidget(self.connection_btn)
            self.layout.addWidget(self.connection_btn, layer, 4)

            self.connections.append(new_connection)

        except Exception:
            logging.exception("Error adding a connection row")

    # -- saved profile ------------------------------------------------------
    #
    # The user's last setup is written whenever a dropdown changes and read
    # back on the next launch. Two rules shape all of it:
    #
    # 1. The audio device is stored by NAME and re-resolved to an index at
    #    startup. See Connection.snapshot() and config.resolve_device() for
    #    why an index would be actively dangerous.
    # 2. Restoring the dropdowns is not the same as connecting. In this app
    #    picking a channel *is* the connect action, so the restore only goes
    #    that far when auto_connect is on. Off by default.
    #
    # Anything that has since disappeared -- unplugged device, bot kicked
    # from a server, channel deleted or renamed or now permission-gated --
    # leaves its field blank, logs one line saying which and why, and lets
    # the rest of the row through. Nothing here may raise: a bad profile must
    # not stop the app from starting.

    @guarded_slot
    def grow_to_hint(self):
        """Widen the window if the layout now needs more room.

        Same contract as ``Connection.resize_combobox``: grow only, never
        shrink, so nothing jumps about mid-session. The window is frameless
        with no resize grip, so it is only ever the width something asked
        for -- and two things now ask. The status strip is the second: its
        detail readout is short ("not connected") when the window is first
        sized and reaches its full width ("1234ms · 99999 drops · drift
        -12345ppm · 13h 42m") only once a stream is live. Without this the
        strip's own text is the thing that gets squeezed, which is the one
        part of the window that exists to be read precisely.

        Bounded: the detail string has a fixed maximum length and Dropdown
        caps every column, so this converges after one call and then no-ops.
        """
        try:
            # activate() first. sizeHint() is served from a cache that is
            # only recomputed when the layout next runs, which normally
            # means "after this function has returned" -- so reading it
            # straight after a setText()/addItem() gives the width the
            # window needed a moment ago and the growth silently no-ops.
            self.layout.activate()

            hint = self.sizeHint().width()

            if hint > self.width():
                self.resize(hint, self.height())
        except Exception:
            logging.exception("Error growing the window to its size hint")

    @guarded_slot
    def profile_changed(self):
        """A dropdown changed; schedule a save. Cheap, call it freely."""
        if self._restoring:
            return

        try:
            self._save_timer.start()          # restarts if already pending
        except Exception:
            logging.exception("Error scheduling profile save")

    @guarded_slot
    def save_profile(self):
        """Write every row's current state. Never raises."""
        try:
            self.config.set_profile(
                [connection.snapshot() for connection in self.connections]
            )
        except Exception:
            logging.exception("Error saving profile")

    @guarded_slot
    def flush_profile(self):
        """Write a pending debounced save immediately, if there is one."""
        try:
            if self._save_timer.isActive():
                self._save_timer.stop()
                self.save_profile()
        except Exception:
            logging.exception("Error flushing profile")

    @staticmethod
    def find_by_id(dropdown, snowflake):
        """Index of the item whose data has ``.id == snowflake``, or None.

        Guild and channel ids are Discord snowflakes: stable, unlike device
        indices, and unlike names -- which is why these two are matched on id
        while the device is matched on name. A renamed channel is still found;
        a deleted one is not found at all, which is the correct outcome.
        """
        try:
            wanted = int(snowflake)
        except (TypeError, ValueError):
            return None

        for index in range(dropdown.count()):
            if getattr(dropdown.itemData(index), "id", None) == wanted:
                return index

        return None

    def restore_devices(self):
        """Phase one: recreate the saved rows and re-select their devices.

        Runs during ``__init__``, before the bot has logged in, because
        nothing here needs Discord.
        """
        rows = self._profile_rows
        if not rows:
            return

        self._restoring = True
        try:
            # Bounded, NOT `while len(self.connections) < len(rows)`.
            # add_connection() swallows its own exceptions (it is a slot),
            # so a row that fails to build never increments the count and a
            # while-loop on that condition would spin forever on a corrupt
            # profile. The zip() below truncates to whatever actually got
            # built, so a short-fall degrades to "fewer rows restored".
            for _ in range(max(0, len(rows) - len(self.connections))):
                self.add_connection()

            for position, (connection, row) in enumerate(
                zip(self.connections, rows)
            ):
                # Note: the saved ``muted`` flag is deliberately NOT applied
                # here. It tracks the live mute button, and nothing is
                # playing yet -- setting it now would both contradict the
                # button (which still reads "Mute") and be wiped a moment
                # later by change_device(), which resets it. It is consumed
                # in restore_row(), after a successful auto-connect join,
                # which is the only point at which muting means anything.
                name = row.get("device_name")

                if name is None:
                    continue

                # By name. Never by index. config.resolve_device() logs the
                # miss and returns None rather than guessing at a neighbour.
                index = config.resolve_device(name, self.devices)

                if index is None:
                    logging_setup.log_event(
                        "profile: row %d device %r not available --"
                        " left unselected", position + 1, name,
                    )
                    continue

                item = connection.devices.findText(name)

                if item < 0 or connection.devices.itemData(item) != index:
                    logging_setup.log_event(
                        "profile: row %d device %r resolved to index %s but"
                        " is not in the dropdown -- left unselected",
                        position + 1, name, index,
                    )
                    continue

                # Set the value with signals blocked, then run the handler
                # ourselves. Two reasons, both deliberate: the restore does
                # not depend on slot side effects firing in an order it did
                # not design, and change_device() runs on a normal Python
                # call stack instead of a Qt one -- where PyQt6 would turn
                # any escaping exception into abort().
                self.select_quietly(connection.devices, item)
                connection.change_device()

                logging_setup.log_event(
                    "profile: row %d device restored: %r (index %s)",
                    position + 1, name, index,
                )

        except Exception:
            logging.exception("Error restoring saved devices")

        finally:
            self._restoring = False

    async def restore_discord(self):
        """Phase two: re-select the saved servers and channels.

        Must run after ``bot.wait_until_ready()`` and after ``set_servers``
        has filled the server dropdowns -- there is nothing to match against
        before that.
        """
        rows = self._profile_rows
        if not rows:
            return

        auto_connect = self.config.auto_connect

        self._restoring = True
        try:
            for position, (connection, row) in enumerate(
                zip(self.connections, rows)
            ):
                await self.restore_row(
                    position, connection, row, auto_connect
                )

        except Exception:
            logging.exception("Error restoring saved servers and channels")

        finally:
            self._restoring = False

        # Deliberately no save here. If the saved device was unplugged for
        # one session, the profile must still remember it for the session
        # after -- writing back "what survived" would quietly forget it.

    async def restore_row(self, position, connection, row, auto_connect):
        """Re-select one row's server and channel. Never raises."""
        try:
            guild_id = row.get("guild_id")

            if guild_id is None:
                return

            index = self.find_by_id(connection.servers, guild_id)

            if index is None:
                logging_setup.log_event(
                    "profile: row %d server id %s not available (bot removed"
                    " from it, or no longer visible) -- left blank",
                    position + 1, guild_id,
                )
                return

            if index in self.connected_servers:
                logging_setup.log_event(
                    "profile: row %d server id %s is already in use by an"
                    " earlier row -- left blank", position + 1, guild_id,
                )
                return

            # Select, then run the handler inline. Letting the signal fire
            # would spawn change_server() as a detached task and this
            # coroutine would go looking in a channel dropdown that has not
            # been populated yet.
            previous = connection.servers.deselected
            self.select_quietly(connection.servers, index)
            await connection.change_server(previous, index)

            channel_id = row.get("channel_id")

            if channel_id is None:
                return

            channel = self.find_by_id(connection.channels, channel_id)

            if channel is None:
                logging_setup.log_event(
                    "profile: row %d channel id %s not found in that server"
                    " (deleted, or the bot cannot see it) -- left blank",
                    position + 1, channel_id,
                )
                return

            self.select_quietly(connection.channels, channel)

            if not auto_connect:
                logging_setup.log_event(
                    "profile: row %d pre-filled %s / %s -- not joining,"
                    " auto-connect is off", position + 1,
                    connection.servers.currentText(),
                    connection.channels.currentText(),
                )
                return

            await connection.change_channel()

            # Only meaningful once something is actually playing.
            if row.get("muted") and connection.voice is not None:
                if connection.voice.is_playing():
                    connection.toggle_mute()

        except Exception:
            logging.exception("Error restoring profile row %d", position + 1)

    @staticmethod
    def select_quietly(dropdown, index):
        """Set the current item without firing ``changed``.

        ``Dropdown.deselected`` is kept in step by hand, because it is
        normally advanced by the very handler being suppressed -- and
        ``GUI.exclude`` uses it to decide which server row to un-hide.
        """
        dropdown.blockSignals(True)
        try:
            dropdown.setCurrentIndex(index)
            dropdown.deselected = index
        finally:
            dropdown.blockSignals(False)

        # resize_combobox(), not refresh(): a restored server name can be
        # wider than anything the window was sized for, and a size *hint*
        # does not resize a window that already has a size.
        Connection.resize_combobox(dropdown)

    @guarded_slot
    def closeEvent(self, event):
        # Covers the paths the title-bar button does not: OS window-manager
        # close, Cmd-Q, app quit. note_shutdown never raises, so it is safe
        # inside a slot even before @guarded_slot gets a say.
        logging_setup.note_shutdown("window-closed")
        # A change made in the last few hundred milliseconds still has its
        # save sitting in the debounce timer. Land it before the process goes
        # away, or the setting the user just picked is the one thing the
        # profile forgets.
        self.flush_profile()
        super().closeEvent(event)

    def exclude(self, deselected, selected):
        self.connected_servers.add(selected)
        
        if deselected is not None:
            self.connected_servers.remove(deselected)

        for connection in self.connections:
            connection.servers.setRowHidden(selected, True)

            if deselected is not None:
                connection.servers.setRowHidden(deselected, False)

    async def run_Qt(self, interval=0.01):
        while True:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, int(interval * 1000))
            await asyncio.sleep(interval)

    async def ready(self):
        await self.bot.wait_until_ready()

        self.info.setText(f"Logged in as: {self.bot.user.name}")

        # Every row, not just the first. A restored profile can have built
        # extra rows during __init__, and those were created while
        # bot.guilds was still empty, so this is their only chance to be
        # filled in.
        for connection in self.connections:
            connection.set_servers(self.bot.guilds)
            Connection.resize_combobox(connection.servers)

        self.setEnabled(True)

        # Phase two of the profile restore, at the only point in the
        # lifecycle where it can work: the server list does not exist until
        # the client is ready.
        await self.restore_discord()

        # Restoring can have widened the widest server/channel name and the
        # info label ("Logged in as: ..."). Fit the window now rather than
        # leaving it narrow until the strip's 2-second timer next fires.
        self.grow_to_hint()
