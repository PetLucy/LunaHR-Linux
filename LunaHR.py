import sys
import asyncio
import time
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QWidget, QPlainTextEdit, QMessageBox
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QIcon
from bleak import BleakScanner, BleakClient
from pythonosc.udp_client import SimpleUDPClient
import pyqtgraph as pg
import traceback

HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


# -----------------------
# Worker thread for BLE
# -----------------------
class HRWorker(QThread):
    heart_rate_signal = Signal(int)
    status_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.run_ble())

    async def run_ble(self):
        self.status_signal.emit("Searching for Polar H10...")
        device = await self.find_polar()
        if not device:
            self.status_signal.emit("Polar H10 not found.")
            return

        self.status_signal.emit(f"Connecting to {device.name}...")
        try:
            async with BleakClient(device) as client:
                self.status_signal.emit("Connected. Streaming heart rate...")

                def handle_hr(_, data: bytearray):
                    if len(data) > 1:
                        hr_value = data[1]
                        self.heart_rate_signal.emit(hr_value)

                await client.start_notify(HR_CHAR_UUID, handle_hr)
                while True:
                    await asyncio.sleep(1)
        except Exception as e:
            tb = traceback.format_exc()
            self.status_signal.emit(f"Connection error: {e}\n{tb}")

    async def find_polar(self):
        devices = await BleakScanner.discover()
        for d in devices:
            if d.name and d.name.startswith("Polar H10"):
                return d
        return None


# -----------------------
# Main Window (UI + OSC)
# -----------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LunaHR - Polar H10")
        self.resize(800, 600)

        # OSC setup
        self.osc_client = SimpleUDPClient("127.0.0.1", 9000)

        # Theme state
        self.dark_mode = True  # default dark mode

        # Top controls
        self.connect_btn = QPushButton("Connect to Polar H10")
        self.connect_btn.clicked.connect(self.on_connect_clicked)

        self.theme_btn = QPushButton("🌙 Dark")  # toggles to light when clicked
        self.theme_btn.setToolTip("Toggle light/dark theme")
        self.theme_btn.clicked.connect(self.toggle_theme)

        top_row = QHBoxLayout()
        top_row.addWidget(self.connect_btn)
        top_row.addStretch(1)
        top_row.addWidget(self.theme_btn)

        # Status + current HR
        self.status_label = QLabel("Status: Idle")
        self.hr_label = QLabel("Heart Rate: -- bpm")
        self.hr_label.setAlignment(Qt.AlignLeft)
        self.hr_label.setStyleSheet("font-size: 24px; font-weight: bold;")

        # Rolling log (unlimited until app closes)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("Rolling heart-rate log will appear here…")

        # Graph
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel('left', 'BPM')
        self.plot.setLabel('bottom', 'Time (s)')
        self.t0 = time.time()
        self.x_data = []
        self.y_data = []
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2))

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hr_label)
        layout.addWidget(self.log_box)
        layout.addWidget(self.plot)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Apply initial theme
        self.apply_theme()

        # Worker placeholder
        self.worker = None

    # ---- OSC sending ----
    def send_heart_rate_osc(self, heart_rate: int):
        try:
            ones_hr = heart_rate % 10
            tens_hr = (heart_rate // 10) % 10
            hundreds_hr = (heart_rate // 100) % 10

            # Preserve parameter names exactly
            self.osc_client.send_message("/avatar/parameters/hr/ones_hr", ones_hr)
            self.osc_client.send_message("/avatar/parameters/hr/tens_hr", tens_hr)
            self.osc_client.send_message("/avatar/parameters/hr/hundreds_hr", hundreds_hr)
            self.osc_client.send_message("/avatar/parameters/hr/heart_rate", heart_rate)
        except Exception as e:
            print(f"Error sending heart rate OSC: {e}")
            traceback.print_exc()

    # ---- Theme handling ----
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.apply_theme()

    def apply_theme(self):
        if self.dark_mode:
            self.theme_btn.setText("🌙 Dark")
            self.setStyleSheet("""
                QWidget { background-color: #121212; color: #EAEAEA; }
                QPlainTextEdit { background-color: #1E1E1E; color: #EAEAEA; border: 1px solid #333; }
                QPushButton { background-color: #2A2A2A; border: 1px solid #444; padding: 6px 10px; }
                QPushButton:hover { background-color: #333; }
            """)
            self.plot.setBackground('#121212')
            self.plot.getAxis('left').setPen(pg.mkPen('#EAEAEA'))
            self.plot.getAxis('bottom').setPen(pg.mkPen('#EAEAEA'))
            self.plot.getAxis('left').setTextPen(pg.mkPen('#EAEAEA'))
            self.plot.getAxis('bottom').setTextPen(pg.mkPen('#EAEAEA'))
            self.curve.setPen(pg.mkPen('#00D1FF', width=2))
        else:
            self.theme_btn.setText("☀️ Light")
            self.setStyleSheet("""
                QWidget { background-color: #FFFFFF; color: #111111; }
                QPlainTextEdit { background-color: #FAFAFA; color: #111111; border: 1px solid #CCC; }
                QPushButton { background-color: #F0F0F0; border: 1px solid #CCC; padding: 6px 10px; }
                QPushButton:hover { background-color: #E6E6E6; }
            """)
            self.plot.setBackground('w')
            self.plot.getAxis('left').setPen(pg.mkPen('#111111'))
            self.plot.getAxis('bottom').setPen(pg.mkPen('#111111'))
            self.plot.getAxis('left').setTextPen(pg.mkPen('#111111'))
            self.plot.getAxis('bottom').setTextPen(pg.mkPen('#111111'))
            self.curve.setPen(pg.mkPen('#D12C2C', width=2))

    # ---- BLE wiring ----
    def on_connect_clicked(self):
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Status: Connecting…")
        self.worker = HRWorker()
        self.worker.heart_rate_signal.connect(self.on_hr_update)
        self.worker.status_signal.connect(self.on_status_update)
        self.worker.start()

    # ---- Update UI on incoming HR ----
    def on_hr_update(self, bpm: int):
        self.hr_label.setText(f"Heart Rate: {bpm} bpm")

        # Timestamped log
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_box.appendPlainText(f"{ts} {bpm} bpm")
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

        # Graph update
        t = time.time() - self.t0
        self.x_data.append(t)
        self.y_data.append(bpm)
        self.curve.setData(self.x_data, self.y_data)

        # Send OSC
        self.send_heart_rate_osc(bpm)

    def on_status_update(self, text: str):
        self.status_label.setText(f"Status: {text}")
        if "error" in text.lower() or "not found" in text.lower() or "failed" in text.lower():
            QMessageBox.warning(self, "BLE", text)
            self.connect_btn.setEnabled(True)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

