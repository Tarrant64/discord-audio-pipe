import os
import sys
import sound
import asyncio
import instrumentation
import logging_setup
import logging
import discord
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtGui import QFontDatabase, QFontMetrics, QIcon, QCursor
from PyQt6.QtCore import Qt, QCoreApplication, QEventLoop, QDir, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow,
    QPushButton,
    QWidget,
    QFrame,
    QGridLayout,
    QComboBox,
    QLabel,
    QHBoxLayout,
    QStyledItemDelegate,
    QListView
)

if getattr(sys, "frozen", False):
    bundle_dir = sys._MEIPASS
else:
    bundle_dir = os.path.dirname(os.path.abspath(__file__))


class Dropdown(QComboBox):
    changed = pyqtSignal(object, object)

    def __init__(self):
        super(Dropdown, self).__init__()

        self.setItemDelegate(QStyledItemDelegate())
        self.setPlaceholderText("None")
        self.setView(QListView())

        self.deselected = None
        self.currentIndexChanged.connect(self.changed_signal)

    def changed_signal(self, selected):
        self.changed.emit(self.deselected, selected)
        self.deselected = selected

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

        # dropdowns
        self.devices = Dropdown()
        self.servers = Dropdown()
        self.channels = Dropdown()

        for device, idx in parent.devices.items():
            self.devices.addItem(device + "   ", idx)

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
        self.devices.changed.connect(self.change_device)
        self.servers.changed.connect(
            lambda deselected, selected: asyncio.create_task(
                self.change_server(deselected, selected)
            )
        )
        self.channels.changed.connect(
            lambda: asyncio.create_task(self.change_channel())
        )
        self.mute.clicked.connect(self.toggle_mute)

    @staticmethod
    def resize_combobox(combobox):
        font = combobox.property("font")
        metrics = QFontMetrics(font)
        min_width = 0

        for i in range(combobox.count()):
            size = metrics.horizontalAdvance(combobox.itemText(i))
            if size > min_width:
                min_width = size

        combobox.setMinimumWidth(min_width + 30)

    def setEnabled(self, enabled):
        self.devices.setEnabled(enabled)
        self.servers.setEnabled(enabled)
        self.channels.setEnabled(enabled)

        self.mute.setEnabled(enabled)
        self.mute.setText("Mute" if enabled else "")

    def set_servers(self, guilds):
        for guild in guilds:
            self.servers.addItem(guild.name, guild)

    def change_device(self):
        try:
            selection = self.devices.currentData()
            logging_setup.log_device(selection, self.devices.currentText().strip())
            self.mute.setText("Mute")

            if self.voice is not None:
                self.voice.stop()
                self.stream.change_device(selection)

                if self.voice.is_connected():
                    self.voice.play(self.stream, fec=False, signal_type='music', after=instrumentation.make_after())

            else:
                self.stream.change_device(selection)

        except Exception:
            logging.exception("Error on change_device")

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

    async def change_channel(self):
        try:
            selection = self.channels.currentData()
            self.mute.setText("Mute")
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

    def toggle_mute(self):
        try:
            if self.voice is not None:
                if self.voice.is_playing():
                    self.voice.pause()
                    self.mute.setText("Resume")
                else:
                    self.voice.resume()
                    self.mute.setText("Mute")

        except Exception:
            logging.exception("Error on toggle_mute")


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
        close_button.clicked.connect(lambda: asyncio.create_task(self.close()))

    async def close(self):
        await self.bot.close()
        self.parent.close()

    def minimize(self):
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

        # layout
        central = QWidget()
        self.layout = QGridLayout()
        self.layout.setSpacing(10)
        self.layout.setContentsMargins(20, 16, 20, 20)
        central.setLayout(self.layout)

        # labels
        self.info = QLabel("Connecting...")
        self.info.setObjectName("info")
        device_lb = QLabel("Devices")
        device_lb.setObjectName("label")
        server_lb = QLabel("Servers     ")
        server_lb.setObjectName("label")
        channel_lb = QLabel("Channels  ")
        channel_lb.setObjectName("label")

        # connections
        self.devices = sound.query_devices()
        self.connections = [Connection(2, self)]
        self.connected_servers = set()

        # new connections
        self.connection_btn = QPushButton("＋", self)
        self.connection_btn.setObjectName("connection_btn")
        self.connection_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # add widgets
        self.layout.addWidget(self.info, 0, 0, 1, 3)
        self.layout.addWidget(device_lb, 1, 0)
        self.layout.addWidget(server_lb, 1, 1)
        self.layout.addWidget(channel_lb, 1, 2)
        self.layout.addWidget(self.connection_btn, 2, 4)

        # events
        self.connection_btn.clicked.connect(self.add_connection)

        # build window
        titlebar = TitleBar(self)
        titlebar.setFixedHeight(36)
        self.setMenuWidget(titlebar)
        self.setCentralWidget(central)
        self.setEnabled(False)

        # load styles
        QFontDatabase.addApplicationFont("./assets/Roboto-Black.ttf")
        with open("./assets/style.qss", "r") as qss:
            self.app.setStyleSheet(qss.read())

        # show window
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.position = event.position().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.position is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(QCursor.pos() - self.position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.position = None
        event.accept()

    def setEnabled(self, enabled):
        self.connection_btn.setEnabled(enabled)
        for connection in self.connections:
            connection.setEnabled(enabled)

    def add_connection(self):
        layer = len(self.connections) + 2

        new_connection = Connection(layer, self)
        new_connection.set_servers(self.bot.guilds)

        for idx in range(new_connection.servers.count()):
            if idx in self.connected_servers:
                new_connection.servers.setRowHidden(idx, True)

        self.layout.removeWidget(self.connection_btn)
        self.layout.addWidget(self.connection_btn, layer, 4)

        self.connections.append(new_connection)

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
        self.connections[0].set_servers(self.bot.guilds)
        Connection.resize_combobox(self.connections[0].servers)
        self.setEnabled(True)
