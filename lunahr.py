import sys
import argparse
import asyncio
import time
import logging
import traceback
import json
from importlib.metadata import version as package_version, PackageNotFoundError
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QWidget, QMessageBox,
    QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox,
    QSystemTrayIcon, QMenu
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QAction

from bleak import BleakScanner, BleakClient
from pythonosc.udp_client import SimpleUDPClient

import pyqtgraph as pg
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem

# Pulsoid source (explicitly use the modern asyncio API; websockets 17 switched top-level aliases)
from websockets.asyncio.client import connect as websocket_connect


# -----------------------
# Constants
# -----------------------
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
PULSOID_WS_URL = "wss://dev.pulsoid.net/api/v1/data/real_time?access_token={token}"

# Reconnect / DBus churn controls
RECONNECT_COOLDOWN_SECONDS = 15       # prevents rapid retry storms
STREAMING_ICON_TIMEOUT_SECONDS = 5     # tray turns inactive if HR packets stop


# -----------------------
# Paths / Config
# -----------------------
APP_CONFIG_DIR = Path.home() / ".config" / "lunahr"
APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_CONFIG_DIR / "config.json"

LOG_DIR = Path.home() / ".local/share/LunaHR/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "lunahr.log"

DEFAULT_CONFIG = {
    "source": "polar",        # "polar" or "pulsoid"
    "pulsoid_token": "",
    "osc_host": "127.0.0.1",
    "osc_ports": [9000],      # list of ints
    "theme": "dark",          # "dark" or "light"
}


def resource_path(filename: str) -> Path:
    """Resolve bundled/source/package assets across PyInstaller, AppImage, and native installs."""
    candidates = []

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / filename)

    try:
        candidates.append(Path(__file__).resolve().parent / filename)
    except NameError:
        pass

    candidates.extend([
        Path("/usr/share/lunahr") / filename,
        Path("/usr/share/icons/hicolor/128x128/apps") / filename,
    ])

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0] if candidates else Path(filename)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(data or {})

        ports = cfg.get("osc_ports", [9000])
        if isinstance(ports, int):
            ports = [ports]
        if isinstance(ports, str):
            ports = [int(p.strip()) for p in ports.split(",") if p.strip().isdigit()]
        cfg["osc_ports"] = ports if ports else [9000]

        if cfg.get("theme") not in ("dark", "light"):
            cfg["theme"] = "dark"
        if cfg.get("source") not in ("polar", "pulsoid"):
            cfg["source"] = "polar"
        if not isinstance(cfg.get("pulsoid_token", ""), str):
            cfg["pulsoid_token"] = ""

        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    safe = dict(DEFAULT_CONFIG)
    safe.update(cfg or {})
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(safe, indent=2), encoding="utf-8")


# -----------------------
# Logging (rotating logs)
# -----------------------
logger = logging.getLogger("lunahr")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=2,             # keep 3 total logs: lunahr.log, .1, .2
        encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

logger.propagate = False


# -----------------------
# Graph Axis: HH:MM:SS
# -----------------------
class TimeAxis(DateAxisItem):
    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try:
                out.append(datetime.fromtimestamp(v).strftime("%H:%M:%S"))
            except Exception:
                out.append("")
        return out


# -----------------------
# ViewBox with "user interacted" hook
# -----------------------
class LiveViewBox(pg.ViewBox):
    def __init__(self, on_user_interaction=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_user_interaction = on_user_interaction

    def _touch(self):
        if callable(self._on_user_interaction):
            self._on_user_interaction()

    def mouseDragEvent(self, ev, axis=None):
        self._touch()
        super().mouseDragEvent(ev, axis=axis)

    def wheelEvent(self, ev, axis=None):
        self._touch()
        super().wheelEvent(ev, axis=axis)

    def mouseClickEvent(self, ev):
        self._touch()
        super().mouseClickEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        self._touch()
        super().mouseDoubleClickEvent(ev)


# -----------------------
# Worker thread: Polar BLE
# -----------------------
class PolarWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)
    device_address_signal = Signal(str)
    discovery_rssi_signal = Signal(object)  # int or None; RSSI captured during discovery only

    def __init__(self):
        super().__init__()
        self.loop = None
        self.task = None
        self.running = True

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self.run_ble())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            # Expected when the GUI stops/restarts the worker.
            logger.info("Polar worker task cancelled.")
        finally:
            try:
                pending = asyncio.all_tasks(loop=self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.task = None
            try:
                self.loop.close()
            except Exception:
                pass
            self.loop = None

    async def run_ble(self):
        self.status_signal.emit("Searching for Polar H10...")
        logger.info("Searching for Polar H10...")

        device, discovery_rssi = await self.find_polar()
        if not device:
            self.status_signal.emit("Polar H10 not found.")
            logger.error("Polar H10 not found.")
            return

        self.device_address_signal.emit(device.address)
        self.discovery_rssi_signal.emit(discovery_rssi)

        self.status_signal.emit(f"Connecting to {device.name}...")
        logger.info(f"Connecting to {device.name} ({device.address})")

        disconnected = asyncio.Event()
        running_loop = asyncio.get_running_loop()

        def handle_disconnect(_client):
            # Bleak may invoke this from backend callback code; schedule safely onto this worker loop.
            try:
                running_loop.call_soon_threadsafe(disconnected.set)
            except RuntimeError:
                pass

        client = BleakClient(device, timeout=20.0, disconnected_callback=handle_disconnect)
        try:
            await client.connect()
            self.status_signal.emit("Connected. Streaming heart rate...")
            logger.info("Connected. Starting HR notifications.")

            def handle_hr(_, data: bytearray):
                # Bluetooth Heart Rate Measurement characteristic, per the GATT flags byte.
                # Bit 0 = 0 -> uint8 HR; bit 0 = 1 -> uint16 little-endian HR.
                if len(data) < 2:
                    return
                flags = data[0]
                if flags & 0x01:
                    if len(data) < 3:
                        return
                    hr_value = int.from_bytes(data[1:3], byteorder="little", signed=False)
                else:
                    hr_value = int(data[1])
                self.heart_rate_signal.emit(hr_value)

            await client.start_notify(HR_CHAR_UUID, handle_hr)

            # Cancellation from stop() interrupts this immediately. A real BLE disconnect wakes it too.
            await disconnected.wait()
            if self.running:
                logger.warning("Polar H10 disconnected unexpectedly.")
                self.status_signal.emit("Connection error: Polar H10 disconnected")

        except asyncio.CancelledError:
            logger.info("Polar worker cancelled (reconnect/shutdown).")
            raise
        except TimeoutError:
            logger.exception("Timed out connecting to Polar H10.")
            self.status_signal.emit("Connection error: timed out connecting to Polar H10")
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Connection error: {e}\n{tb}")
            self.status_signal.emit(f"Connection error: {e}")
        finally:
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    logger.exception("Error while disconnecting Polar H10 during worker cleanup.")

    async def find_polar(self):
        matched_rssi = None

        def polar_filter(device, advertisement_data):
            nonlocal matched_rssi
            name = advertisement_data.local_name or device.name or ""
            if name.startswith("Polar H10"):
                matched_rssi = advertisement_data.rssi
                return True
            return False

        device = await BleakScanner.find_device_by_filter(polar_filter, timeout=10.0)
        return device, matched_rssi

    def stop(self):
        self.running = False
        loop = self.loop
        task = self.task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass


# -----------------------
# Worker thread: Pulsoid (WebSocket)
# -----------------------
class PulsoidWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)

    def __init__(self, token: str):
        super().__init__()
        self.loop = None
        self.task = None
        self.running = True
        self.token = token.strip()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self.run_ws())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            logger.info("Pulsoid worker task cancelled.")
        finally:
            try:
                pending = asyncio.all_tasks(loop=self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.task = None
            try:
                self.loop.close()
            except Exception:
                pass
            self.loop = None

    def stop(self):
        self.running = False
        loop = self.loop
        task = self.task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    async def run_ws(self):
        if not self.token:
            self.status_signal.emit("Pulsoid token not set (open Settings).")
            logger.error("Pulsoid token not set.")
            return

        url = PULSOID_WS_URL.format(token=self.token)
        self.status_signal.emit("Connecting to Pulsoid...")
        logger.info("Connecting to Pulsoid WebSocket...")

        try:
            async with websocket_connect(url, ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                self.status_signal.emit("Connected. Streaming heart rate (Pulsoid)...")
                logger.info("Pulsoid connected. Listening for HR...")

                while self.running:
                    msg = await ws.recv()

                    bpm = None
                    try:
                        data = json.loads(msg)
                        bpm = data.get("data", {}).get("heart_rate", None)
                        if bpm is None and "heart_rate" in data:
                            bpm = data.get("heart_rate")
                    except Exception:
                        try:
                            bpm = int(str(msg).strip())
                        except Exception:
                            bpm = None

                    if bpm is not None:
                        try:
                            self.heart_rate_signal.emit(int(bpm))
                        except (TypeError, ValueError):
                            logger.warning("Pulsoid returned a non-integer heart-rate value: %r", bpm)

        except asyncio.CancelledError:
            logger.info("Pulsoid worker cancelled (reconnect/shutdown).")
            raise
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Pulsoid connection error: {e}\n{tb}")
            self.status_signal.emit(f"Connection error: {e}")


# -----------------------
# Settings dialog
# -----------------------
class SettingsDialog(QDialog):
    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.setWindowTitle("LunaHR Settings")
        self.resize(520, 240)
        self.cfg = dict(cfg)

        form = QFormLayout()

        self.source_combo = QComboBox()
        self.source_combo.addItem("Polar H10 (BLE)", "polar")
        self.source_combo.addItem("Pulsoid (WebSocket)", "pulsoid")
        self.source_combo.setCurrentIndex(0 if self.cfg.get("source") == "polar" else 1)

        self.pulsoid_token = QLineEdit()
        self.pulsoid_token.setPlaceholderText("Pulsoid access token (data:heart_rate:read)")
        self.pulsoid_token.setText(self.cfg.get("pulsoid_token", ""))
        self.pulsoid_token.setEchoMode(QLineEdit.Password)

        self.osc_host = QLineEdit()
        self.osc_host.setText(self.cfg.get("osc_host", "127.0.0.1"))

        self.osc_ports = QLineEdit()
        ports = self.cfg.get("osc_ports", [9000])
        ports_str = ",".join(str(p) for p in ports) if isinstance(ports, list) else str(ports)
        self.osc_ports.setText(ports_str)
        self.osc_ports.setPlaceholderText("9000  (or 9000,9001,9002)")

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.setCurrentIndex(0 if self.cfg.get("theme") == "dark" else 1)

        form.addRow("Data source", self.source_combo)
        form.addRow("Pulsoid token", self.pulsoid_token)
        form.addRow("OSC host", self.osc_host)
        form.addRow("OSC port", self.osc_ports)
        form.addRow("Theme", self.theme_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self.source_combo.currentIndexChanged.connect(self._update_enabled)
        self._update_enabled()

    def _update_enabled(self):
        src = self.source_combo.currentData()
        self.pulsoid_token.setEnabled(src == "pulsoid")

    def get_config(self) -> dict:
        cfg = dict(self.cfg)
        cfg["source"] = self.source_combo.currentData()
        cfg["pulsoid_token"] = self.pulsoid_token.text().strip()
        cfg["osc_host"] = self.osc_host.text().strip() or "127.0.0.1"

        ports_text = self.osc_ports.text().strip()
        ports = []
        for part in ports_text.split(","):
            part = part.strip()
            if part.isdigit():
                ports.append(int(part))
        cfg["osc_ports"] = ports if ports else [9000]

        cfg["theme"] = self.theme_combo.currentData()
        return cfg


# -----------------------
# Main Window
# -----------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LunaHR - HR to OSC")
        self.resize(900, 600)

        self.cfg = load_config()
        logger.info("LunaHR started.")

        # Theme
        self.dark_mode = (self.cfg.get("theme") == "dark")

        # OSC clients
        self.osc_clients = []
        self._build_osc_clients()

        # Worker state
        self.worker = None
        self.current_source = self.cfg.get("source", "polar")

        # HR / watchdog state
        self.last_hr_time = None
        self.last_hr_value = None
        self.reconnect_timeout = 30
        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_delay_seconds = 5
        self.reconnect_max_seconds = 180
        self.reconnect_started_at = None

        # reconnect cooldown bookkeeping
        self.last_reconnect_attempt_at = None

        # Polar discovery metadata. We deliberately do not run extra scans while connected;
        # concurrent discovery was a likely source of BlueZ/D-Bus churn.
        self.device_address = None
        self.discovery_rssi = None

        # Tray state
        self.tray_icon = None
        self.tray_active_icon = QIcon(str(resource_path("lunahr-tray-active.png")))
        self.tray_inactive_icon = QIcon(str(resource_path("lunahr-tray-inactive.png")))
        self.tray_show_hide_action = None
        self.tray_connect_action = None
        self._last_status_text = "Idle"
        self._tray_streaming = False

        # Graph live-follow behavior
        self.window_seconds = 30 * 60
        self.follow_live = True
        self.last_user_interaction = None
        self.snap_back_seconds = 30
        self._programmatic_range_change = False

        # Buttons (top row)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect_clicked)

        self.settings_btn = QPushButton("⚙ Settings")
        self.settings_btn.clicked.connect(self.open_settings)

        self.open_logs_btn = QPushButton("📂 Open Logs")
        self.open_logs_btn.setToolTip(str(LOG_DIR))
        self.open_logs_btn.clicked.connect(self.open_log_dir)

        top_row = QHBoxLayout()
        top_row.addWidget(self.connect_btn)
        top_row.addStretch(1)
        top_row.addWidget(self.settings_btn)
        top_row.addWidget(self.open_logs_btn)

        # Live button (same row as HR, right aligned)
        self.live_btn = QPushButton("🔴 Live")
        self.live_btn.setToolTip("Snap back to live tracking")
        self.live_btn.clicked.connect(self.snap_to_live)

        # Status
        self.status_label = QLabel("Status: Idle")

        # Heart rate label
        self.hr_label = QLabel("Heart Rate: -- bpm")
        self.hr_label.setAlignment(Qt.AlignLeft)
        self.hr_label.setStyleSheet("font-size: 24px; font-weight: bold;")

        hr_row = QHBoxLayout()
        hr_row.addWidget(self.hr_label)
        hr_row.addStretch(1)
        hr_row.addWidget(self.live_btn)

        # Graph
        time_axis = TimeAxis(orientation="bottom")
        self.viewbox = LiveViewBox(on_user_interaction=self.on_user_interaction)
        self.plot = pg.PlotWidget(viewBox=self.viewbox, axisItems={"bottom": time_axis})
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("left", "BPM")
        self.plot.setLabel("bottom", "Time (HH:MM:SS)")
        self.viewbox.setMouseEnabled(x=True, y=False)

        self.x_data = []
        self.y_data = []
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2))

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addLayout(hr_row)
        layout.addWidget(self.plot)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.apply_theme()
        self.update_live_button()
        self._setup_tray()

        # Timers
        self.watchdog = QTimer()
        self.watchdog.timeout.connect(self.check_heartbeat_timeout)
        self.watchdog.start(5000)

        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.log_heartbeat_status)
        self.heartbeat_timer.start(60000)

        self.snapback_timer = QTimer()
        self.snapback_timer.timeout.connect(self.check_snapback)
        self.snapback_timer.start(1000)

        self.tray_state_timer = QTimer()
        self.tray_state_timer.timeout.connect(self._refresh_tray_state)
        self.tray_state_timer.start(1000)
        self._refresh_tray_state()

    # ---------------------------
    # Tray
    # ---------------------------
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("No system tray is available; tray controls disabled.")
            return

        self.tray_icon = QSystemTrayIcon(self)
        if not self.tray_inactive_icon.isNull():
            self.tray_icon.setIcon(self.tray_inactive_icon)
        else:
            logger.warning("Inactive tray icon asset was not found.")

        menu = QMenu(self)

        self.tray_show_hide_action = QAction("Hide LunaHR", self)
        self.tray_show_hide_action.triggered.connect(self.toggle_window_visibility)
        menu.addAction(self.tray_show_hide_action)

        self.tray_connect_action = QAction("Connect", self)
        self.tray_connect_action.triggered.connect(self.on_connect_clicked)
        menu.addAction(self.tray_connect_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.setToolTip("LunaHR — Idle")
        self.tray_icon.show()
        logger.info("System tray icon initialized.")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window_visibility()

    def toggle_window_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()
        self._update_tray_menu_text()

    def _update_tray_menu_text(self):
        if self.tray_show_hide_action is not None:
            self.tray_show_hide_action.setText("Hide LunaHR" if self.isVisible() else "Show LunaHR")

    def _is_streaming(self) -> bool:
        return (
            self.last_hr_time is not None
            and (time.time() - self.last_hr_time) <= STREAMING_ICON_TIMEOUT_SECONDS
        )

    def _refresh_tray_state(self):
        if self.tray_icon is None:
            return

        streaming = self._is_streaming()
        if streaming != self._tray_streaming:
            self._tray_streaming = streaming
            icon = self.tray_active_icon if streaming else self.tray_inactive_icon
            if not icon.isNull():
                self.tray_icon.setIcon(icon)

        if streaming and self.last_hr_value is not None:
            tooltip = f"LunaHR — Streaming • {self.last_hr_value} bpm"
        else:
            tooltip = f"LunaHR — {self._last_status_text}"
        self.tray_icon.setToolTip(tooltip)

        if self.tray_connect_action is not None:
            self.tray_connect_action.setEnabled(not streaming)
            self.tray_connect_action.setText("Reconnect" if self.worker else "Connect")

        self._update_tray_menu_text()

    def quit_application(self):
        logger.info("Quit requested from tray.")
        self.shutdown()
        QApplication.instance().quit()

    def shutdown(self):
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        logger.info("LunaHR shutting down.")
        for timer_name in ("watchdog", "heartbeat_timer", "snapback_timer", "tray_state_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        self.stop_worker()
        if self.tray_icon is not None:
            self.tray_icon.hide()

    # ---------------------------
    # Live button state
    # ---------------------------
    def update_live_button(self):
        self.live_btn.setText("🔴 Live" if self.follow_live else "🟡 Live")

    # ---------------------------
    # Config / Settings
    # ---------------------------
    def _build_osc_clients(self):
        host = self.cfg.get("osc_host", "127.0.0.1")
        ports = self.cfg.get("osc_ports", [9000])
        if isinstance(ports, int):
            ports = [ports]
        self.osc_clients = [SimpleUDPClient(host, int(p)) for p in ports]
        logger.info(f"OSC target: {host}:{','.join(str(p) for p in ports)}")

    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec() == QDialog.Accepted:
            self.cfg = dlg.get_config()
            save_config(self.cfg)

            self.dark_mode = (self.cfg.get("theme") == "dark")
            self.apply_theme()

            self._build_osc_clients()
            self.current_source = self.cfg.get("source", "polar")

            self.status_label.setText(f"Status: Settings saved (source: {self.current_source}).")
            logger.info(f"Settings saved. Source: {self.current_source}")

    # ---------------------------
    # Helpers: reconnect cycle
    # ---------------------------
    def _enter_reconnect_cycle(self):
        if not self.reconnect_started_at:
            self.reconnect_started_at = time.time()
            logger.info(f"Reconnect cycle started (max {self.reconnect_max_seconds}s).")

    def _reconnect_time_exceeded(self) -> bool:
        if not self.reconnect_started_at:
            return False
        return (time.time() - self.reconnect_started_at) >= self.reconnect_max_seconds

    def _set_connect_button(self, text: str, enabled: bool):
        """Update the Connect button immediately so Bluetooth work never feels like a dead click."""
        self.connect_btn.setText(text)
        self.connect_btn.setEnabled(enabled)

    def _exit_reconnect_cycle_to_idle(self, reason: str):
        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None
        self.last_hr_time = None
        self.last_reconnect_attempt_at = None

        self.status_label.setText(f"Status: Idle ({reason})")
        logger.warning(f"Reconnect cycle ended → Idle ({reason})")
        self._set_connect_button("Connect", True)

        self.stop_worker()

    def _can_attempt_reconnect_now(self) -> bool:
        if self.last_reconnect_attempt_at is None:
            return True
        return (time.time() - self.last_reconnect_attempt_at) >= RECONNECT_COOLDOWN_SECONDS

    def _remaining_reconnect_cooldown(self) -> float:
        if self.last_reconnect_attempt_at is None:
            return 0.0
        rem = RECONNECT_COOLDOWN_SECONDS - (time.time() - self.last_reconnect_attempt_at)
        return rem if rem > 0 else 0.0

    # ---------------------------
    # Open logs dir
    # ---------------------------
    def open_log_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    # ---------------------------
    # Explore/live tracking
    # ---------------------------
    def on_user_interaction(self):
        if self._programmatic_range_change:
            return
        if self.follow_live:
            self.follow_live = False
            self.update_live_button()
            logger.info("Graph set to explore mode (live-follow paused).")
        self.last_user_interaction = time.time()

    def snap_to_live(self):
        self.follow_live = True
        self.last_user_interaction = None
        self.update_live_button()
        self.update_live_view()

    def check_snapback(self):
        if self.follow_live:
            return
        if not self.last_user_interaction:
            return
        if (time.time() - self.last_user_interaction) >= self.snap_back_seconds:
            logger.info("Snap-back timer triggered; returning to live-follow.")
            self.snap_to_live()

    def update_live_view(self):
        if not self.follow_live or not self.x_data:
            return
        now = self.x_data[-1]
        start = now - self.window_seconds
        self._programmatic_range_change = True
        try:
            self.plot.setXRange(start, now, padding=0.0)
        finally:
            self._programmatic_range_change = False

    # ---------------------------
    # Theme
    # ---------------------------
    def apply_theme(self):
        pink = "#ff4fd8"

        if self.dark_mode:
            self.setStyleSheet("""
                QWidget { background-color: #121212; color: #EAEAEA; }
                QPushButton { background-color: #2A2A2A; border: 1px solid #444; padding: 6px 10px; }
                QPushButton:hover { background-color: #333; }
            """)
            self.plot.setBackground("#121212")
            self.plot.getAxis("left").setPen(pg.mkPen("#EAEAEA"))
            self.plot.getAxis("bottom").setPen(pg.mkPen("#EAEAEA"))
            self.plot.getAxis("left").setTextPen(pg.mkPen("#EAEAEA"))
            self.plot.getAxis("bottom").setTextPen(pg.mkPen("#EAEAEA"))
        else:
            self.setStyleSheet("""
                QWidget { background-color: #FFFFFF; color: #111111; }
                QPushButton { background-color: #F0F0F0; border: 1px solid #CCC; padding: 6px 10px; }
                QPushButton:hover { background-color: #E6E6E6; }
            """)
            self.plot.setBackground("w")
            self.plot.getAxis("left").setPen(pg.mkPen("#111111"))
            self.plot.getAxis("bottom").setPen(pg.mkPen("#111111"))
            self.plot.getAxis("left").setTextPen(pg.mkPen("#111111"))
            self.plot.getAxis("bottom").setTextPen(pg.mkPen("#111111"))

        self.curve.setPen(pg.mkPen(pink, width=2))

    # ---------------------------
    # Worker start/stop
    # ---------------------------
    def stop_worker(self):
        worker = self.worker
        if not worker:
            return True

        # stop() cancels the worker's asyncio task; QThread.quit() is intentionally not used
        # because these workers override run() and don't run Qt's thread event loop.
        try:
            worker.stop()
            if not worker.wait(5000):
                logger.error("Worker did not stop within 5 seconds; refusing to start overlapping BLE work.")
                return False
        except Exception:
            logger.exception("Error while stopping worker.")
            return False

        if self.worker is worker:
            self.worker = None
        return True

    def on_connect_clicked(self):
        # Give instant feedback before scanner startup; discovery itself may take several seconds.
        self._set_connect_button("Searching…", False)
        self.status_label.setText("Status: Searching for source…")
        QApplication.processEvents()

        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None
        self.last_reconnect_attempt_at = None

        self.start_worker()

    def start_worker(self):
        if self.worker and not self.stop_worker():
            self.status_label.setText("Status: Waiting for previous worker to stop…")
            self._set_connect_button("Connect", True)
            return

        src = self.cfg.get("source", "polar")
        self.current_source = src

        if src == "polar":
            self.worker = PolarWorker()
            self.worker.heart_rate_signal.connect(self.on_hr_update)
            self.worker.status_signal.connect(self.on_status_update)
            self.worker.device_address_signal.connect(self.on_device_address)
            self.worker.discovery_rssi_signal.connect(self.on_discovery_rssi)
            logger.info("Starting Polar worker.")
        else:
            token = self.cfg.get("pulsoid_token", "")
            self.worker = PulsoidWorker(token=token)
            self.worker.heart_rate_signal.connect(self.on_hr_update)
            self.worker.status_signal.connect(self.on_status_update)
            logger.info("Starting Pulsoid worker.")

        self.worker.start()
        logger.info(f"Worker started (source={src}).")

    def on_device_address(self, addr: str):
        self.device_address = addr
        logger.info(f"Polar device address: {addr}")

    def on_discovery_rssi(self, rssi):
        self.discovery_rssi = rssi if isinstance(rssi, int) else None
        if self.discovery_rssi is None:
            logger.info("Polar discovery RSSI: n/a")
        else:
            logger.info(f"Polar discovery RSSI: {self.discovery_rssi} dBm")

    # ---------------------------
    # HR updates
    # ---------------------------
    def on_hr_update(self, bpm: int):
        if self.reconnecting:
            self.reconnecting = False
            self._reconnect_scheduled = False
            self.reconnect_started_at = None
            self.last_reconnect_attempt_at = None
            logger.info("Reconnected successfully (HR received).")

        self.last_hr_time = time.time()
        self.last_hr_value = bpm

        self.hr_label.setText(f"Heart Rate: {bpm} bpm")

        now_ts = time.time()
        self.x_data.append(now_ts)
        self.y_data.append(bpm)
        self.curve.setData(self.x_data, self.y_data)

        logger.info(f"HR {bpm} bpm (source={self.current_source})")

        self.update_live_view()
        self.send_heart_rate_osc(bpm)
        self._refresh_tray_state()

    # ---------------------------
    # OSC
    # ---------------------------
    def send_heart_rate_osc(self, heart_rate: int):
        try:
            ones_hr = heart_rate % 10
            tens_hr = (heart_rate // 10) % 10
            hundreds_hr = (heart_rate // 100) % 10

            for client in self.osc_clients:
                client.send_message("/avatar/parameters/hr/ones_hr", ones_hr)
                client.send_message("/avatar/parameters/hr/tens_hr", tens_hr)
                client.send_message("/avatar/parameters/hr/hundreds_hr", hundreds_hr)
                client.send_message("/avatar/parameters/hr/heart_rate", heart_rate)
        except Exception as e:
            logger.error(f"Error sending OSC: {e}")
            print(f"Error sending OSC: {e}")

    # ---------------------------
    # Status updates
    # ---------------------------
    def on_status_update(self, text: str):
        self.status_label.setText(f"Status: {text}")
        logger.info(f"Status: {text}")
        self._last_status_text = text.splitlines()[0].strip() or "Idle"
        self._refresh_tray_state()

        lower = text.lower()

        # Mirror the asynchronous BLE/WebSocket phase on the button itself.
        if lower.startswith("searching for polar"):
            self._set_connect_button("Searching…", False)
        elif lower.startswith("connecting to") or lower.startswith("connecting to pulsoid"):
            self._set_connect_button("Connecting…", False)
        elif lower.startswith("connected."):
            self._set_connect_button("Connected", False)

        if "connection error" in lower:
            if not self.reconnecting:
                self.reconnecting = True
                self._enter_reconnect_cycle()

            if self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
                return

            self.last_hr_time = None
            self._refresh_tray_state()
            logger.warning("Connection error reported; initiating reconnect.")
            self.reconnect()
            return

        if ("error" in lower or "not found" in lower or "failed" in lower) and not self.reconnecting:
            QMessageBox.warning(self, "Source", text)
            self._set_connect_button("Connect", True)

    # ---------------------------
    # Watchdog reconnect
    # ---------------------------
    def check_heartbeat_timeout(self):
        # If we're already reconnecting/scheduled, just check timeout cutoff
        if self.reconnecting or self._reconnect_scheduled:
            if self.reconnecting and self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        if not self.worker or not self.last_hr_time:
            return

        elapsed = time.time() - self.last_hr_time
        if elapsed > self.reconnect_timeout:
            self.reconnecting = True
            self._enter_reconnect_cycle()

            self.last_hr_time = None
            self._refresh_tray_state()

            msg = f"No HR received for {int(elapsed)}s → Reconnecting..."
            self.status_label.setText(f"Status: {msg}")
            self._set_connect_button("Reconnecting…", False)
            logger.warning(msg)

            if self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
                return

            self.reconnect()

    def reconnect(self):
        if self._reconnect_scheduled:
            return
        if self.reconnecting and self._reconnect_time_exceeded():
            self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        # Cooldown to avoid thrashing DBus and triggering dbus_fast cleanup races
        if not self._can_attempt_reconnect_now():
            rem = self._remaining_reconnect_cooldown()
            self._reconnect_scheduled = True
            logger.info(f"Reconnect cooldown active; retrying in {rem:.1f}s...")
            QTimer.singleShot(int(max(rem, 0.5) * 1000), self._restart_connection)
            return

        self.last_reconnect_attempt_at = time.time()

        self._reconnect_scheduled = True
        if not self.stop_worker():
            self._reconnect_scheduled = False
            logger.warning("Previous worker is still stopping; reconnect will be retried shortly.")
            QTimer.singleShot(1000, self.reconnect)
            return

        logger.info(f"Attempting reconnect in {self.reconnect_delay_seconds} seconds...")
        QTimer.singleShot(int(self.reconnect_delay_seconds * 1000), self._restart_connection)

    def _restart_connection(self):
        self._reconnect_scheduled = False

        if self.reconnecting and self._reconnect_time_exceeded():
            self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        self.status_label.setText("Status: Reconnecting…")
        self._set_connect_button("Reconnecting…", False)
        self.start_worker()

    # ---------------------------
    # Heartbeat log
    # ---------------------------
    def log_heartbeat_status(self):
        if self.last_hr_time and self.last_hr_value is not None:
            last_hr_ts = datetime.fromtimestamp(self.last_hr_time).strftime("%H:%M:%S")
            rssi_part = ""
            if self.current_source == "polar" and self.discovery_rssi is not None:
                rssi_part = f", discovery RSSI: {self.discovery_rssi} dBm"
            logger.info(
                f"Still connected, last HR at {last_hr_ts} ({self.last_hr_value} bpm)"
                f"{rssi_part}, source={self.current_source}"
            )
        elif self.worker:
            logger.warning(f"Worker is running, but no heart rate received yet. source={self.current_source}")

    def closeEvent(self, event):
        self.shutdown()
        event.accept()
        QApplication.instance().quit()


def log_runtime_versions():
    logger.info("Python %s", sys.version.split()[0])
    for package in ("bleak", "dbus-fast", "PySide6", "pyqtgraph", "python-osc", "websockets"):
        try:
            logger.info("Dependency %s=%s", package, package_version(package))
        except PackageNotFoundError:
            logger.warning("Dependency %s version unavailable", package)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="LunaHR — heart rate to VRChat OSC")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Automatically start the configured heart-rate connection after launch.",
    )
    parser.add_argument(
        "--minimized", "--tray",
        dest="minimized",
        action="store_true",
        help="Start hidden in the system tray. --tray is an alias for --minimized.",
    )
    return parser.parse_known_args(argv)


def main():
    args, qt_args = parse_args(sys.argv[1:])
    log_runtime_versions()

    app = QApplication([sys.argv[0], *qt_args])
    app.setQuitOnLastWindowClosed(False)

    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)

    if args.minimized and win.tray_icon is not None:
        win.hide()
        win._update_tray_menu_text()
        logger.info("Started minimized to system tray.")
    else:
        if args.minimized and win.tray_icon is None:
            logger.warning("--minimized requested but no system tray is available; showing window instead.")
        win.show()

    if args.connect:
        logger.info("--connect requested; scheduling automatic connection.")
        QTimer.singleShot(250, win.on_connect_clicked)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
