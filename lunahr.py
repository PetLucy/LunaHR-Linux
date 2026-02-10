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
from PySide6.QtCore import QThread, Signal, Qt, QTimer

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

# Avoid duplicate handlers if reloaded
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
        # values are epoch timestamps (float)
        out = []
        for v in values:
            try:
                out.append(datetime.fromtimestamp(v).strftime("%H:%M:%S"))
            except Exception:
                out.append("")
        return out


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

        # Let MainWindow know which address to use for RSSI scans
        self.device_address_signal.emit(device.address)

        self.status_signal.emit(f"Connecting to {device.name}...")
        logger.info(f"Connecting to {device.name} ({device.address})")

        try:
            async with BleakClient(device) as client:
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
# Main Window (UI + OSC + Logging + Reconnect)
# -----------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LunaHR - Polar H10")
        self.resize(900, 600)

        # OSC setup
        self.osc_client = SimpleUDPClient("127.0.0.1", 9000)

        # Logging enabled by default (file logging)
        self.logging_enabled = True
        logger.disabled = False
        logger.info("LunaHR started.")

        # Theme state
        self.dark_mode = True  # default dark mode

        # BLE / watchdog state
        self.worker = None
        self.last_hr_time = None
        self.last_hr_value = None

        self.reconnect_timeout = 30  # seconds without HR => reconnect
        self.reconnecting = False

        # RSSI tracking
        self.device_address = None
        self.last_rssi_value = None
        self.last_rssi_time = None
        self.rssi_worker = None

        # Top controls
        self.connect_btn = QPushButton("Connect to Polar H10")
        self.connect_btn.clicked.connect(self.on_connect_clicked)

        self.log_toggle_btn = QPushButton("📝 Logging: ON")
        self.log_toggle_btn.setToolTip("Enable/Disable file logging")
        self.log_toggle_btn.clicked.connect(self.toggle_logging)

        self.theme_btn = QPushButton("🌙 Dark")
        self.theme_btn.setToolTip("Toggle light/dark theme")
        self.theme_btn.clicked.connect(self.toggle_theme)

        top_row = QHBoxLayout()
        top_row.addWidget(self.connect_btn)
        top_row.addStretch(1)
        top_row.addWidget(self.log_toggle_btn)
        top_row.addWidget(self.theme_btn)

        # Status + current HR
        self.status_label = QLabel("Status: Idle")
        self.hr_label = QLabel("Heart Rate: -- bpm")
        self.hr_label.setAlignment(Qt.AlignLeft)
        self.hr_label.setStyleSheet("font-size: 24px; font-weight: bold;")

        # Graph with time axis HH:MM:SS
        time_axis = TimeAxis(orientation="bottom")
        self.plot = pg.PlotWidget(axisItems={"bottom": time_axis})
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("left", "BPM")
        self.plot.setLabel("bottom", "Time (HH:MM:SS)")

        self.x_data = []  # epoch timestamps
        self.y_data = []  # bpm values
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2))

        # Layout
        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hr_label)
        layout.addWidget(self.plot)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.apply_theme()

        # Watchdog: check if HR has stopped
        self.watchdog = QTimer()
        self.watchdog.timeout.connect(self.check_heartbeat_timeout)
        self.watchdog.start(5000)  # every 5 seconds

        # Heartbeat logger: once per minute
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.log_heartbeat_status)
        self.heartbeat_timer.start(60000)  # 60 seconds

    # ---------------------------
    # Logging toggle
    # ---------------------------
    def toggle_logging(self):
        self.logging_enabled = not self.logging_enabled
        if self.logging_enabled:
            self.log_toggle_btn.setText("📝 Logging: ON")
            logger.disabled = False
            logger.info("Logging enabled by user.")
        else:
            # Log this before disabling
            logger.info("Logging disabled by user.")
            self.log_toggle_btn.setText("📝 Logging: OFF")
            logger.disabled = True

    # ---------------------------
    # Theme
    # ---------------------------
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.apply_theme()

    def apply_theme(self):
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
            self.curve.setPen(pg.mkPen("#00D1FF", width=2))
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
            self.curve.setPen(pg.mkPen("#D12C2C", width=2))

    # ---------------------------
    # Connect / worker start
    # ---------------------------
    def on_connect_clicked(self):
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Status: Connecting…")
        self.start_worker()

    def start_worker(self):
        # Stop existing worker if any
        if self.worker:
            try:
                self.worker.stop()
                self.worker.quit()
                self.worker.wait(1500)
            except Exception:
                pass

        self.worker = HRWorker()
        self.worker.heart_rate_signal.connect(self.on_hr_update)
        self.worker.status_signal.connect(self.on_status_update)
        self.worker.device_address_signal.connect(self.on_device_address)
        self.worker.start()

        if self.logging_enabled:
            logger.info("HRWorker started.")

    def on_device_address(self, addr: str):
        self.device_address = addr
        if self.logging_enabled:
            logger.info(f"Device address set for RSSI scans: {addr}")

    # ---------------------------
    # HR updates
    # ---------------------------
    def on_hr_update(self, bpm: int):
        self.last_hr_time = time.time()
        self.last_hr_value = bpm

        self.hr_label.setText(f"Heart Rate: {bpm} bpm")

        # Graph update uses real epoch timestamps
        now_ts = time.time()
        self.x_data.append(now_ts)
        self.y_data.append(bpm)
        self.curve.setData(self.x_data, self.y_data)

        # Log HR line to file
        if self.logging_enabled:
            logger.info(f"HR {bpm} bpm")

        # Send OSC
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
            if self.logging_enabled:
                logger.error(f"Error sending OSC: {e}")
            # keep stdout print for dev/testing
            print(f"Error sending OSC: {e}")

    # ---------------------------
    # Status updates
    # ---------------------------
    def on_status_update(self, text: str):
        self.status_label.setText(f"Status: {text}")
        if self.logging_enabled:
            logger.info(f"Status: {text}")

        # Only show a popup if we aren't in a reconnect loop
        if ("error" in text.lower() or "not found" in text.lower() or "failed" in text.lower()) and not self.reconnecting:
            QMessageBox.warning(self, "BLE", text)
            self.connect_btn.setEnabled(True)

    # ---------------------------
    # Watchdog: reconnect if HR stalls
    # ---------------------------
    def check_heartbeat_timeout(self):
        if not self.worker or not self.last_hr_time:
            return

        elapsed = time.time() - self.last_hr_time
        if elapsed > self.reconnect_timeout and not self.reconnecting:
            self.reconnecting = True
            msg = f"No HR received for {int(elapsed)}s → Reconnecting..."
            self.status_label.setText(f"Status: {msg}")
            if self.logging_enabled:
                logger.warning(msg)
            self.reconnect()

    def reconnect(self):
        try:
            if self.worker:
                self.worker.stop()
                self.worker.quit()
                self.worker.wait(1500)
        except Exception:
            pass

        if self.logging_enabled:
            logger.info("Attempting BLE reconnect in 5 seconds...")

        QTimer.singleShot(5000, self._restart_connection)

    def _restart_connection(self):
        self.reconnecting = False
        self.status_label.setText("Status: Reconnecting…")
        self.start_worker()

    # ---------------------------
    # Heartbeat log once per minute + RSSI
    # ---------------------------
    def log_heartbeat_status(self):
        if not self.logging_enabled:
            return

        # Kick an RSSI update in the background (best effort)
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

        # Avoid overlapping scans
        if self.rssi_worker and self.rssi_worker.isRunning():
            return

        self.rssi_worker = RSSIWorker(self.device_address, timeout=2.0)
        self.rssi_worker.rssi_signal.connect(self.on_rssi_update)
        self.rssi_worker.start()

    def on_rssi_update(self, rssi):
        self.last_rssi_time = time.time()
        self.last_rssi_value = rssi if isinstance(rssi, int) else None

        if self.logging_enabled:
            if self.last_rssi_value is None:
                logger.info("RSSI scan: n/a")
            else:
                logger.info(f"RSSI scan: {self.last_rssi_value} dBm")


# ---------------------------
# Entry point
# ---------------------------
def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
