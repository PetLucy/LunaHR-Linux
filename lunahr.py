import sys
import asyncio
import time
import logging
import traceback
import json
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QWidget, QMessageBox,
    QDialog, QFormLayout, QLineEdit, QComboBox, QDialogButtonBox
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices

from bleak import BleakScanner, BleakClient
from pythonosc.udp_client import SimpleUDPClient

import pyqtgraph as pg
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem

# Pulsoid source
import websockets


# -----------------------
# Constants
# -----------------------
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
PULSOID_WS_URL = "wss://dev.pulsoid.net/api/v1/data/real_time?access_token={token}"

# Reconnect / DBus churn controls
RECONNECT_COOLDOWN_SECONDS = 15       # prevents rapid retry storms
RSSI_MIN_INTERVAL_SECONDS = 60        # rate-limit RSSI scans


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
# RSSI scan worker (best effort)
# -----------------------
class RSSIWorker(QThread):
    rssi_signal = Signal(object)  # int or None

    def __init__(self, address: str, timeout: float = 2.0):
        super().__init__()
        self.address = address
        self.timeout = timeout

    def run(self):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            rssi = loop.run_until_complete(self._scan_rssi())
            self.rssi_signal.emit(rssi)
        except Exception:
            self.rssi_signal.emit(None)
        finally:
            try:
                loop.stop()
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    async def _scan_rssi(self):
        devices = await BleakScanner.discover(timeout=self.timeout)
        for d in devices:
            if getattr(d, "address", None) == self.address:
                return getattr(d, "rssi", None)
        return None


# -----------------------
# Worker thread: Polar BLE
# -----------------------
class PolarWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)
    device_address_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.running = True

    def run(self):
        try:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.run_ble())
        finally:
            # Ensure loop teardown is clean
            try:
                pending = asyncio.all_tasks(loop=self.loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.loop.stop()
            except Exception:
                pass
            try:
                self.loop.close()
            except Exception:
                pass

    async def run_ble(self):
        self.status_signal.emit("Searching for Polar H10...")
        logger.info("Searching for Polar H10...")
        device = await self.find_polar()
        if not device:
            self.status_signal.emit("Polar H10 not found.")
            logger.error("Polar H10 not found.")
            return

        self.device_address_signal.emit(device.address)

        self.status_signal.emit(f"Connecting to {device.name}...")
        logger.info(f"Connecting to {device.name} ({device.address})")

        try:
            async with BleakClient(device, timeout=20.0) as client:
                self.status_signal.emit("Connected. Streaming heart rate...")
                logger.info("Connected. Starting HR notifications.")

                def handle_hr(_, data: bytearray):
                    if len(data) > 1:
                        hr_value = int(data[1])
                        self.heart_rate_signal.emit(hr_value)

                await client.start_notify(HR_CHAR_UUID, handle_hr)

                while self.running:
                    await asyncio.sleep(1)

                logger.info("Stopping BLE worker loop.")

        except asyncio.CancelledError:
            # Normal during shutdown / restart; don't treat as a scary failure.
            logger.info("Polar worker cancelled (likely due to reconnect/shutdown).")
            self.status_signal.emit("Connection error: cancelled")
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Connection error: {e}\n{tb}")
            self.status_signal.emit(f"Connection error: {e}\n{tb}")

    async def find_polar(self):
        devices = await BleakScanner.discover()
        for d in devices:
            if d.name and d.name.startswith("Polar H10"):
                return d
        return None

    def stop(self):
        self.running = False


# -----------------------
# Worker thread: Pulsoid (WebSocket)
# -----------------------
class PulsoidWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)

    def __init__(self, token: str):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.running = True
        self.token = token.strip()

    def run(self):
        try:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.run_ws())
        finally:
            try:
                pending = asyncio.all_tasks(loop=self.loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.loop.stop()
            except Exception:
                pass
            try:
                self.loop.close()
            except Exception:
                pass

    def stop(self):
        self.running = False

    async def run_ws(self):
        if not self.token:
            self.status_signal.emit("Pulsoid token not set (open Settings).")
            logger.error("Pulsoid token not set.")
            return

        url = PULSOID_WS_URL.format(token=self.token)

        self.status_signal.emit("Connecting to Pulsoid...")
        logger.info("Connecting to Pulsoid WebSocket...")

        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
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
                        except Exception:
                            pass

        except asyncio.CancelledError:
            logger.info("Pulsoid worker cancelled (likely due to reconnect/shutdown).")
            self.status_signal.emit("Connection error: cancelled")
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Pulsoid connection error: {e}\n{tb}")
            self.status_signal.emit(f"Connection error: {e}\n{tb}")


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

        # RSSI state (Polar only)
        self.device_address = None
        self.last_rssi_value = None
        self.last_rssi_time = None
        self.rssi_worker = None
        self._last_rssi_request_at = None

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

    def _exit_reconnect_cycle_to_idle(self, reason: str):
        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None
        self.last_hr_time = None
        self.last_reconnect_attempt_at = None

        self.status_label.setText(f"Status: Idle ({reason})")
        logger.warning(f"Reconnect cycle ended → Idle ({reason})")
        self.connect_btn.setEnabled(True)

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
        if not self.worker:
            return
        try:
            if hasattr(self.worker, "stop"):
                self.worker.stop()
            self.worker.quit()
            self.worker.wait(3000)
        except Exception:
            pass
        self.worker = None

    def on_connect_clicked(self):
        self.connect_btn.setEnabled(False)

        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None
        self.last_reconnect_attempt_at = None

        self.status_label.setText("Status: Connecting…")
        self.start_worker()

    def start_worker(self):
        self.stop_worker()

        src = self.cfg.get("source", "polar")
        self.current_source = src

        if src == "polar":
            self.worker = PolarWorker()
            self.worker.heart_rate_signal.connect(self.on_hr_update)
            self.worker.status_signal.connect(self.on_status_update)
            self.worker.device_address_signal.connect(self.on_device_address)
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
        logger.info(f"Device address set for RSSI scans: {addr}")

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

        lower = text.lower()

        if "connection error" in lower:
            if not self.reconnecting:
                self.reconnecting = True
                self._enter_reconnect_cycle()

            if self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
                return

            self.last_hr_time = None
            logger.warning("Connection error reported; initiating reconnect.")
            self.reconnect()
            return

        if ("error" in lower or "not found" in lower or "failed" in lower) and not self.reconnecting:
            QMessageBox.warning(self, "Source", text)
            self.connect_btn.setEnabled(True)

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

            msg = f"No HR received for {int(elapsed)}s → Reconnecting..."
            self.status_label.setText(f"Status: {msg}")
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
        self.stop_worker()

        logger.info(f"Attempting reconnect in {self.reconnect_delay_seconds} seconds...")
        QTimer.singleShot(int(self.reconnect_delay_seconds * 1000), self._restart_connection)

    def _restart_connection(self):
        self._reconnect_scheduled = False

        if self.reconnecting and self._reconnect_time_exceeded():
            self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        self.status_label.setText("Status: Reconnecting…")
        self.start_worker()

    # ---------------------------
    # Heartbeat log + RSSI (Polar only)
    # ---------------------------
    def log_heartbeat_status(self):
        self.request_rssi_update()

        if self.last_hr_time and self.last_hr_value is not None:
            last_hr_ts = datetime.fromtimestamp(self.last_hr_time).strftime("%H:%M:%S")

            rssi_part = "RSSI: n/a"
            if self.current_source == "polar":
                if self.last_rssi_time and (time.time() - self.last_rssi_time) < 90:
                    if self.last_rssi_value is not None:
                        rssi_part = f"RSSI: {self.last_rssi_value} dBm"

            logger.info(
                f"Still connected, last HR at {last_hr_ts} ({self.last_hr_value} bpm), "
                f"{rssi_part}, source={self.current_source}"
            )
        else:
            logger.warning(f"Still running, but no heart rate received yet. source={self.current_source}")

    def request_rssi_update(self):
        # Polar only
        if self.current_source != "polar":
            return
        if not self.device_address:
            return

        # Don't add DBus load while reconnecting (this is where the socket/EOF spam usually comes from)
        if self.reconnecting or self._reconnect_scheduled:
            return

        # Rate limit (even if some future code path calls it more often)
        now = time.time()
        if self._last_rssi_request_at and (now - self._last_rssi_request_at) < RSSI_MIN_INTERVAL_SECONDS:
            return
        self._last_rssi_request_at = now

        if self.rssi_worker and self.rssi_worker.isRunning():
            return

        self.rssi_worker = RSSIWorker(self.device_address, timeout=2.0)
        self.rssi_worker.rssi_signal.connect(self.on_rssi_update)
        self.rssi_worker.start()

    def on_rssi_update(self, rssi):
        self.last_rssi_time = time.time()
        self.last_rssi_value = rssi if isinstance(rssi, int) else None

        if self.last_rssi_value is None:
            logger.info("RSSI scan: n/a")
        else:
            logger.info(f"RSSI scan: {self.last_rssi_value} dBm")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
