import sys
import asyncio
import time
import logging
import traceback
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QWidget, QMessageBox
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices

from bleak import BleakScanner, BleakClient
from pythonosc.udp_client import SimpleUDPClient

import pyqtgraph as pg
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem


# -----------------------
# Constants
# -----------------------
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


# -----------------------
# Logging (rotating logs)
# -----------------------
LOG_DIR = Path.home() / ".local/share/LunaHR/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "lunahr.log"

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
    """
    A ViewBox that calls a callback whenever the user pans/zooms.
    This makes "explore mode" reliable across pyqtgraph versions.
    """
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
        try:
            rssi = asyncio.run(self._scan_rssi())
            self.rssi_signal.emit(rssi)
        except Exception:
            self.rssi_signal.emit(None)

    async def _scan_rssi(self):
        devices = await BleakScanner.discover(timeout=self.timeout)
        for d in devices:
            if getattr(d, "address", None) == self.address:
                return getattr(d, "rssi", None)
        return None


# -----------------------
# Worker thread for BLE
# -----------------------
class HRWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)
    device_address_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.running = True

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.run_ble())

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
            # Increased timeout helps with BlueZ service discovery stalls
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
# Main Window
# -----------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LunaHR - Polar H10")
        self.resize(900, 600)

        self.osc_client = SimpleUDPClient("127.0.0.1", 9000)
        logger.info("LunaHR started.")

        # Theme
        self.dark_mode = True

        # BLE / watchdog state
        self.worker = None
        self.last_hr_time = None
        self.last_hr_value = None
        self.reconnect_timeout = 30
        self.reconnecting = False

        # Reconnect scheduling guard (prevents "reconnect storms")
        self._reconnect_scheduled = False
        self.reconnect_delay_seconds = 5

        # Reconnect overall limit (no backoff, just give up after X seconds)
        self.reconnect_max_seconds = 180  # 3 minutes
        self.reconnect_started_at = None  # epoch time when reconnect cycle began

        # RSSI state
        self.device_address = None
        self.last_rssi_value = None
        self.last_rssi_time = None
        self.rssi_worker = None

        # Graph live-follow behavior
        self.window_seconds = 30 * 60      # 30 minutes visible when live
        self.follow_live = True
        self.last_user_interaction = None
        self.snap_back_seconds = 30        # auto return to live after 30s
        self._programmatic_range_change = False

        # Buttons (top row)
        self.connect_btn = QPushButton("Connect to Polar H10")
        self.connect_btn.clicked.connect(self.on_connect_clicked)

        self.open_logs_btn = QPushButton("📂 Open Logs")
        self.open_logs_btn.setToolTip(str(LOG_DIR))
        self.open_logs_btn.clicked.connect(self.open_log_dir)

        self.theme_btn = QPushButton("🌙 Dark")
        self.theme_btn.setToolTip("Toggle light/dark theme")
        self.theme_btn.clicked.connect(self.toggle_theme)

        top_row = QHBoxLayout()
        top_row.addWidget(self.connect_btn)
        top_row.addStretch(1)
        top_row.addWidget(self.open_logs_btn)
        top_row.addWidget(self.theme_btn)

        # Live button row (right-aligned, closer to graph)
        self.live_btn = QPushButton("🔴 Live")
        self.live_btn.setToolTip("Snap back to live tracking")
        self.live_btn.clicked.connect(self.snap_to_live)

        live_row = QHBoxLayout()
        live_row.addStretch(1)
        live_row.addWidget(self.live_btn)

        # Status + HR label
        self.status_label = QLabel("Status: Idle")
        self.hr_label = QLabel("Heart Rate: -- bpm")
        self.hr_label.setAlignment(Qt.AlignLeft)
        self.hr_label.setStyleSheet("font-size: 24px; font-weight: bold;")

        # Graph
        time_axis = TimeAxis(orientation="bottom")
        self.viewbox = LiveViewBox(on_user_interaction=self.on_user_interaction)
        self.plot = pg.PlotWidget(viewBox=self.viewbox, axisItems={"bottom": time_axis})
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("left", "BPM")
        self.plot.setLabel("bottom", "Time (HH:MM:SS)")
        self.viewbox.setMouseEnabled(x=True, y=False)  # pan/zoom in X, keep Y stable-ish

        self.x_data = []  # epoch timestamps
        self.y_data = []  # bpm
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2))

        # Layout
        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hr_label)
        layout.addLayout(live_row)      # moved live button closer to the graph
        layout.addWidget(self.plot)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.apply_theme()

        # Watchdog timer for HR stall reconnect
        self.watchdog = QTimer()
        self.watchdog.timeout.connect(self.check_heartbeat_timeout)
        self.watchdog.start(5000)

        # Heartbeat logger
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.log_heartbeat_status)
        self.heartbeat_timer.start(60000)

        # Snap-back timer (checks explore idle time)
        self.snapback_timer = QTimer()
        self.snapback_timer.timeout.connect(self.check_snapback)
        self.snapback_timer.start(1000)

    # ---------------------------
    # Helpers: entering/leaving reconnect cycle
    # ---------------------------
    def _enter_reconnect_cycle(self):
        if not self.reconnect_started_at:
            self.reconnect_started_at = time.time()
            logger.info(f"Reconnect cycle started (max {self.reconnect_max_seconds}s).")

    def _exit_reconnect_cycle_to_idle(self, reason: str):
        # Stop trying, go back to idle, let user click Connect again
        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None
        self.last_hr_time = None

        self.status_label.setText(f"Status: Idle ({reason})")
        logger.warning(f"Reconnect cycle ended → Idle ({reason})")

        self.connect_btn.setEnabled(True)

    def _reconnect_time_exceeded(self) -> bool:
        if not self.reconnect_started_at:
            return False
        return (time.time() - self.reconnect_started_at) >= self.reconnect_max_seconds

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
            logger.info("Graph set to explore mode (live-follow paused).")

        self.last_user_interaction = time.time()

    def snap_to_live(self):
        self.follow_live = True
        self.last_user_interaction = None
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
        if not self.follow_live:
            return
        if not self.x_data:
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
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.apply_theme()

    def apply_theme(self):
        pink = "#ff4fd8"

        if self.dark_mode:
            self.theme_btn.setText("🌙 Dark")
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
            self.theme_btn.setText("☀️ Light")
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
    # BLE wiring
    # ---------------------------
    def on_connect_clicked(self):
        self.connect_btn.setEnabled(False)

        # User initiated connection: reset reconnect cycle state
        self.reconnecting = False
        self._reconnect_scheduled = False
        self.reconnect_started_at = None

        self.status_label.setText("Status: Connecting…")
        self.start_worker()

    def start_worker(self):
        if self.worker:
            try:
                self.worker.stop()
                self.worker.quit()
                self.worker.wait(3000)
            except Exception:
                pass

        # Safety guard: don't start a new worker if the old one is still running
        if self.worker and self.worker.isRunning():
            logger.warning("Old HRWorker still running; delaying restart by 1 second.")
            QTimer.singleShot(1000, self.start_worker)
            return

        self.worker = HRWorker()
        self.worker.heart_rate_signal.connect(self.on_hr_update)
        self.worker.status_signal.connect(self.on_status_update)
        self.worker.device_address_signal.connect(self.on_device_address)
        self.worker.start()

        logger.info("HRWorker started.")

    def on_device_address(self, addr: str):
        self.device_address = addr
        logger.info(f"Device address set for RSSI scans: {addr}")

    # ---------------------------
    # HR updates
    # ---------------------------
    def on_hr_update(self, bpm: int):
        # Reconnect is considered successful once HR resumes
        if self.reconnecting:
            self.reconnecting = False
            self._reconnect_scheduled = False
            self.reconnect_started_at = None
            logger.info("Reconnected successfully (HR received).")

        self.last_hr_time = time.time()
        self.last_hr_value = bpm

        self.hr_label.setText(f"Heart Rate: {bpm} bpm")

        now_ts = time.time()
        self.x_data.append(now_ts)
        self.y_data.append(bpm)
        self.curve.setData(self.x_data, self.y_data)

        logger.info(f"HR {bpm} bpm")

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

            self.osc_client.send_message("/avatar/parameters/hr/ones_hr", ones_hr)
            self.osc_client.send_message("/avatar/parameters/hr/tens_hr", tens_hr)
            self.osc_client.send_message("/avatar/parameters/hr/hundreds_hr", hundreds_hr)
            self.osc_client.send_message("/avatar/parameters/hr/heart_rate", heart_rate)
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

        # If the worker reports a connection error, proactively reconnect (within the 3-min window)
        if "connection error" in lower:
            if not self.reconnecting:
                self.reconnecting = True
                self._enter_reconnect_cycle()

            if self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
                return

            # Prevent watchdog cascade
            self.last_hr_time = None
            logger.warning("Connection error reported; initiating reconnect.")
            self.reconnect()
            return

        if ("error" in lower or "not found" in lower or "failed" in lower) and not self.reconnecting:
            QMessageBox.warning(self, "BLE", text)
            self.connect_btn.setEnabled(True)

    # ---------------------------
    # Watchdog reconnect
    # ---------------------------
    def check_heartbeat_timeout(self):
        # If we are already reconnecting or have one scheduled, don't trigger again
        if self.reconnecting or self._reconnect_scheduled:
            # If reconnecting, check if we should give up and go idle
            if self.reconnecting and self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        if not self.worker or not self.last_hr_time:
            return

        elapsed = time.time() - self.last_hr_time
        if elapsed > self.reconnect_timeout:
            self.reconnecting = True
            self._enter_reconnect_cycle()

            # IMPORTANT: clear so watchdog doesn't repeatedly trigger while reconnecting
            self.last_hr_time = None

            msg = f"No HR received for {int(elapsed)}s → Reconnecting..."
            self.status_label.setText(f"Status: {msg}")
            logger.warning(msg)

            if self._reconnect_time_exceeded():
                self._exit_reconnect_cycle_to_idle("reconnect timeout")
                return

            self.reconnect()

    def reconnect(self):
        # Ensure only one reconnect attempt is scheduled at a time
        if self._reconnect_scheduled:
            return

        # If we’re reconnecting too long, stop.
        if self.reconnecting and self._reconnect_time_exceeded():
            self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        self._reconnect_scheduled = True

        try:
            if self.worker:
                self.worker.stop()
                self.worker.quit()
                self.worker.wait(3000)
        except Exception:
            pass

        logger.info(f"Attempting BLE reconnect in {self.reconnect_delay_seconds} seconds...")
        QTimer.singleShot(int(self.reconnect_delay_seconds * 1000), self._restart_connection)

    def _restart_connection(self):
        # Keep reconnecting=True until HR resumes
        self._reconnect_scheduled = False

        if self.reconnecting and self._reconnect_time_exceeded():
            self._exit_reconnect_cycle_to_idle("reconnect timeout")
            return

        self.status_label.setText("Status: Reconnecting…")
        self.start_worker()

    # ---------------------------
    # Heartbeat log + RSSI
    # ---------------------------
    def log_heartbeat_status(self):
        self.request_rssi_update()

        if self.last_hr_time and self.last_hr_value is not None:
            last_hr_ts = datetime.fromtimestamp(self.last_hr_time).strftime("%H:%M:%S")

            rssi_part = "RSSI: n/a"
            if self.last_rssi_time and (time.time() - self.last_rssi_time) < 90:
                if self.last_rssi_value is not None:
                    rssi_part = f"RSSI: {self.last_rssi_value} dBm"

            logger.info(f"Still connected, last HR at {last_hr_ts} ({self.last_hr_value} bpm), {rssi_part}")
        else:
            logger.warning("Still running, but no heart rate received yet.")

    def request_rssi_update(self):
        if not self.device_address:
            return
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
