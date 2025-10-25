import sys
import threading
import time
import traceback
import webbrowser
import ctypes
from typing import Optional, Tuple, List

from PySide6.QtCore import QTimer, Qt, Slot, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMessageBox, QWidget, QDialog, QLabel, QVBoxLayout,
    QHBoxLayout, QPushButton, QSpinBox, QCheckBox, QLineEdit, QListWidget, QListWidgetItem,
    QDialogButtonBox, QStyle, QComboBox, QProgressBar
)

from app.config import Config, Settings, APP_NAME
from app.ntp_client import query_ntp, NtpResult, ping_servers
from app.icons import connected_icon, disconnected_icon, idle_icon, default_app_icon, github_icon, star_icon
from app import time_utils
import requests

# Thread-safe flags and shared state
class SyncState:
    def __init__(self):
        self.last_success_time = 0.0
        self.last_result: Optional[NtpResult] = None
        self.running = False
        self.stop_requested = False
        self.last_best_server: str = ""

class NtpSyncManager:
    """
    Manages background NTP probing, drift detection, and time updates.
    """
    def __init__(self, cfg: Config, state: SyncState, on_status_changed=None, on_best_server_changed=None):
        self.cfg = cfg
        self.state = state
        self.on_status_changed = on_status_changed
        self.on_best_server_changed = on_best_server_changed
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self.state.stop_requested = False
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.state.stop_requested = True

    def _choose_server(self, timeout_ms: int) -> Optional[NtpResult]:
        s = self.cfg.settings
        servers = list(s.ntp_servers)
        if not servers:
            return None
        # Try preferred if manual mode
        if not s.auto_select_server and s.preferred_server:
            res = query_ntp(s.preferred_server, timeout_ms=timeout_ms, samples=3)
            if res.success:
                return res
        # Auto: choose best by lowest RTT (use multiple samples)
        best: Optional[NtpResult] = None
        for host in servers:
            res = query_ntp(host, timeout_ms=timeout_ms, samples=3)
            if res.success:
                if (best is None) or (res.rtt_ms < best.rtt_ms):
                    best = res
        # Announce if best changed
        if best and s.auto_select_server and best.server != self.state.last_best_server:
            self.state.last_best_server = best.server
            if self.on_best_server_changed:
                try:
                    self.on_best_server_changed(best.server, best.rtt_ms)
                except Exception:
                    pass
        return best

    def _verify_and_apply_time(self, selected: NtpResult) -> Tuple[bool, str]:
        threshold_ms = self.cfg.settings.drift_threshold_ms
        drift = selected.offset_ms
        if abs(drift) < threshold_ms:
            return False, f"Drift {drift:.1f} ms within threshold ({threshold_ms} ms)"
        now_epoch = time.time()
        target_epoch_ms = int((now_epoch + (drift / 1000.0)) * 1000)
        ok, msg = time_utils.set_system_time_utc_via_helper(target_epoch_ms)
        return ok, f"Time correction requested ({drift:.1f} ms drift). {msg}"

    def _run_loop(self):
        self.state.running = True
        try:
            while not self.state.stop_requested:
                if not self.cfg.settings.auto_sync_enabled:
                    time.sleep(1.0)
                    continue

                try:
                    selected = self._choose_server(self.cfg.settings.ntp_timeout_ms)
                    if not selected:
                        self.state.last_result = NtpResult(server="(none)", rtt_ms=0, offset_ms=0, success=False, error="No server available")
                        if self.on_status_changed:
                            self.on_status_changed()
                        time.sleep(self.cfg.settings.check_interval_sec)
                        continue

                    self.state.last_result = selected
                    if self.on_status_changed:
                        self.on_status_changed()

                    changed, message = self._verify_and_apply_time(selected)
                    if changed:
                        time.sleep(3.0)
                        recheck = query_ntp(selected.server, timeout_ms=self.cfg.settings.ntp_timeout_ms, samples=2)
                        if recheck.success and abs(recheck.offset_ms) < self.cfg.settings.drift_threshold_ms:
                            self.state.last_success_time = time.time()
                        else:
                            self.state.last_success_time = time.time()
                    else:
                        self.state.last_success_time = time.time()

                except Exception as e:
                    self.state.last_result = NtpResult(server="(error)", rtt_ms=0, offset_ms=0, success=False, error=str(e))

                if self.on_status_changed:
                    self.on_status_changed()

                interval = max(15, int(self.cfg.settings.check_interval_sec))
                slept = 0
                while slept < interval and not self.state.stop_requested:
                    time.sleep(0.5)
                    slept += 0.5
        finally:
            self.state.running = False

class WelcomeDialog(QDialog):
    """
    Aesthetic dialog shown on manual startup to help user find and use the tray icon.
    Includes an option to never show again (for manual launches).
    """
    def __init__(self, cfg: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Welcome to TimeGuardian")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet("""
            QDialog {
                background-color: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 10px;
            }
            QLabel#title {
                color: #93c5fd;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#body {
                font-size: 13px;
                line-height: 1.4em;
            }
            QCheckBox {
                font-size: 12px;
            }
            QPushButton {
                background-color: #2563eb;
                color: white;
                padding: 8px 18px;
                border-radius: 8px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #1d4ed8;
            }
        """)
        v = QVBoxLayout(self)
        title = QLabel("TimeGuardian is running in your tray")
        title.setObjectName("title")
        v.addWidget(title)
        body = QLabel(
            "You can find the TimeGuardian icon in the Windows system tray (near the clock).\n"
            "Right-click the icon for Settings, time sync, and timezone tools.\n\n"
            "Icon states:\n"
            "• Blue (dot) = starting/idle\n"
            "• Green (check) = connected/recently synced\n"
            "• Red (cross) = not recently synced\n\n"
            "Tip: You can disable this welcome dialog for future manual launches in Settings."
        )
        body.setObjectName("body")
        body.setWordWrap(True)
        v.addWidget(body)
        self.chk_dont_show = QCheckBox("Don't show this dialog on manual launch again")
        self.chk_dont_show.setChecked(False)
        v.addWidget(self.chk_dont_show)
        v.addSpacing(6)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        v.addWidget(buttons)
        buttons.accepted.connect(self._on_accept)

    def _on_accept(self):
        if self.chk_dont_show.isChecked():
            self.cfg.settings.show_welcome_on_launch = False
            self.cfg.save()
        self.accept()

class SettingsDialog(QDialog):
    """
    Settings dialog for configuring sync behavior and optional features.
    Includes server pinging with ultra-aesthetic progress, sorting by RTT, and preferred server selection.
    """
    ping_results_ready = Signal(list)   # list of dicts: {'server': str, 'rtt': float or None, 'method': 'NTP'|'ICMP'|'fail'}
    ping_progress = Signal(int, int)    # done, total

    def __init__(self, cfg: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.cfg = cfg

        layout = QVBoxLayout(self)

        # Auto sync
        self.chk_auto = QCheckBox("Enable automatic sync")
        self.chk_auto.setChecked(cfg.settings.auto_sync_enabled)
        layout.addWidget(self.chk_auto)

        # Interval
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Check interval (seconds):"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(30, 86400)
        self.spin_interval.setValue(cfg.settings.check_interval_sec)
        row1.addWidget(self.spin_interval)
        layout.addLayout(row1)

        # Threshold
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Drift threshold (ms):"))
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(10, 60000)
        self.spin_threshold.setValue(cfg.settings.drift_threshold_ms)
        row2.addWidget(self.spin_threshold)
        layout.addLayout(row2)

        # Server list and controls
        layout.addWidget(QLabel("NTP servers (one per line):"))
        self.list_servers = QListWidget()
        for s in cfg.settings.ntp_servers:
            self.list_servers.addItem(QListWidgetItem(s))
        layout.addWidget(self.list_servers)

        # Aesthetic progress bar for pinging
        self.pbar = QProgressBar()
        self.pbar.setMinimum(0)
        self.pbar.setMaximum(100)
        self.pbar.setValue(0)
        self.pbar.setTextVisible(True)
        self.pbar.setVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar {
                background-color: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                            stop:0 #34d399, stop:1 #10b981);
                border-radius: 6px;
            }
        """)
        layout.addWidget(self.pbar)

        row3 = QHBoxLayout()
        self.txt_new_server = QLineEdit()
        self.txt_new_server.setPlaceholderText("Add server (e.g., time.google.com)")
        self.btn_add = QPushButton("Add")
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_ping = QPushButton("Ping servers")
        row3.addWidget(self.txt_new_server)
        row3.addWidget(self.btn_add)
        row3.addWidget(self.btn_remove)
        row3.addWidget(self.btn_ping)
        layout.addLayout(row3)
        self.btn_add.clicked.connect(self._add_server)
        self.btn_remove.clicked.connect(self._remove_selected_servers)
        self.btn_ping.clicked.connect(self._ping_servers)

        # Auto vs Preferred
        self.chk_auto_select = QCheckBox("Auto-select best server (by lowest latency)")
        self.chk_auto_select.setChecked(cfg.settings.auto_select_server)
        layout.addWidget(self.chk_auto_select)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Preferred server (used when Auto-select is off):"))
        self.txt_preferred = QLineEdit()
        self.txt_preferred.setText(cfg.settings.preferred_server or "")
        row4.addWidget(self.txt_preferred)
        self.btn_set_from_selection = QPushButton("Set from selection")
        row4.addWidget(self.btn_set_from_selection)
        layout.addLayout(row4)
        self.btn_set_from_selection.clicked.connect(self._set_preferred_from_selection)

        # Startup
        self.chk_startup = QCheckBox("Launch at Windows startup")
        self.chk_startup.setChecked(cfg.settings.launch_at_startup)
        layout.addWidget(self.chk_startup)

        # Optional features
        self.chk_tz_ip = QCheckBox("Allow timezone set based on public IP (optional)")
        self.chk_tz_ip.setChecked(cfg.settings.allow_timezone_from_ip)
        layout.addWidget(self.chk_tz_ip)

        self.chk_region_ip = QCheckBox("Allow Windows region change based on public IP (optional, may require reboot)")
        self.chk_region_ip.setChecked(cfg.settings.allow_region_from_ip)
        layout.addWidget(self.chk_region_ip)

        # Notifications
        self.chk_notifications = QCheckBox("Enable notifications (tray balloons)")
        self.chk_notifications.setChecked(cfg.settings.notifications_enabled)
        layout.addWidget(self.chk_notifications)

        # Welcome dialog visibility on manual launch
        self.chk_show_welcome = QCheckBox("Show welcome dialog on manual launch")
        self.chk_show_welcome.setChecked(cfg.settings.show_welcome_on_launch)
        layout.addWidget(self.chk_show_welcome)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        # Signals
        self.ping_results_ready.connect(self._apply_ping_results)
        self.ping_progress.connect(self._on_ping_progress)

    def _add_server(self):
        s = self.txt_new_server.text().strip()
        if s:
            self.list_servers.addItem(QListWidgetItem(s))
            self.txt_new_server.clear()

    def _remove_selected_servers(self):
        for item in self.list_servers.selectedItems():
            self.list_servers.takeItem(self.list_servers.row(item))

    def _set_preferred_from_selection(self):
        items = self.list_servers.selectedItems()
        if not items:
            QMessageBox.information(self, APP_NAME, "Select a server first.")
            return
        self.txt_preferred.setText(items[0].text())

    def _on_ping_progress(self, done: int, total: int):
        if total <= 0:
            return
        if not self.pbar.isVisible():
            self.pbar.setVisible(True)
        self.pbar.setMaximum(total)
        self.pbar.setValue(done)
        self.pbar.setFormat(f"Pinging servers... {done}/{total}")

    def _ping_servers(self):
        # Disable only the ping button during the operation
        self.btn_ping.setEnabled(False)
        self.pbar.setVisible(True)
        self.pbar.setValue(0)
        self.pbar.setFormat("Pinging servers... 0/0")
        def work():
            try:
                servers = [self.list_servers.item(i).text().split(" (RTT:")[0].strip()
                           for i in range(self.list_servers.count())
                           if self.list_servers.item(i).text().strip()]
                if not servers:
                    self.ping_results_ready.emit([])
                    return
                def progress(done, total):
                    # Emit from worker thread; Qt will queue it to UI thread
                    self.ping_progress.emit(done, total)
                results = ping_servers(
                    servers, timeout_ms=1000, max_workers=min(8, max(1, len(servers))), samples=5, progress_cb=progress
                )
                payload = []
                for r in results:
                    if r.success:
                        payload.append({"server": r.server, "rtt": r.rtt_ms, "method": "NTP"})
                    elif r.rtt_ms > 0:
                        payload.append({"server": r.server, "rtt": r.rtt_ms, "method": "ICMP"})
                    else:
                        payload.append({"server": r.server, "rtt": None, "method": "fail"})
                self.ping_results_ready.emit(payload)
            finally:
                QTimer.singleShot(0, lambda: self.btn_ping.setEnabled(True))
        threading.Thread(target=work, daemon=True).start()

    @Slot(list)
    def _apply_ping_results(self, payload: List[dict]):
        # Hide progress at the end
        self.pbar.setVisible(False)
        if not payload:
            QMessageBox.information(self, APP_NAME, "No servers to ping.")
            return
        # Sort by numeric RTT (None => inf)
        def rtt_val(entry):
            return entry["rtt"] if isinstance(entry["rtt"], (int, float)) else float("inf")
        sorted_payload = sorted(payload, key=rtt_val)
        # Update list widget texts and reorder based on sorted RTT
        self.list_servers.clear()
        reachable = 0
        for entry in sorted_payload:
            server = entry["server"]
            method = entry["method"]
            if entry["rtt"] is None:
                label = f"{server} (RTT: fail)"
            else:
                label = f"{server} (RTT: {entry['rtt']:.0f} ms {method}, median of 5)"
                reachable += 1
            self.list_servers.addItem(QListWidgetItem(label))
        # Save ping summary and also update stored server order to the sorted order
        self.cfg.settings.last_ping_results = {
            e["server"]: (float(e["rtt"]) if isinstance(e["rtt"], (int, float)) else 0.0)
            for e in sorted_payload
        }
        self.cfg.settings.ntp_servers = [self.list_servers.item(i).text().split(" (RTT:")[0].strip()
                                         for i in range(self.list_servers.count())]
        self.cfg.save()
        # Inform the user which server is currently the best by ping
        best = next((e for e in sorted_payload if isinstance(e["rtt"], (int, float))), None)
        if best:
            QMessageBox.information(self, APP_NAME, f"Best by latency: {best['server']} ({best['rtt']:.0f} ms, {best['method']}, median of 5).\n"
                                                    f"You can set it as Preferred or leave Auto-select enabled.")
        else:
            QMessageBox.information(self, APP_NAME, "Ping complete, but no servers were reachable.")

    def accept(self):
        s = self.cfg.settings
        s.auto_sync_enabled = self.chk_auto.isChecked()
        s.check_interval_sec = int(self.spin_interval.value())
        s.drift_threshold_ms = int(self.spin_threshold.value())
        s.ntp_servers = [self.list_servers.item(i).text().split(" (RTT:")[0].strip()
                         for i in range(self.list_servers.count())
                         if self.list_servers.item(i).text().strip()]
        if not s.ntp_servers:
            QMessageBox.warning(self, "Validation", "You must specify at least one NTP server.")
            return
        s.auto_select_server = self.chk_auto_select.isChecked()
        s.preferred_server = self.txt_preferred.text().strip()
        s.launch_at_startup = self.chk_startup.isChecked()
        s.allow_timezone_from_ip = self.chk_tz_ip.isChecked()
        s.allow_region_from_ip = self.chk_region_ip.isChecked()
        s.show_welcome_on_launch = self.chk_show_welcome.isChecked()
        s.notifications_enabled = self.chk_notifications.isChecked()
        self.cfg.save()
        super().accept()

class TrayApp:
    """
    Main tray application that orchestrates UI and background syncing.
    """
    def __init__(self):
        self.app = QApplication(sys.argv)
        # Brand the app so notifications don't say "Python"
        try:
            self.app.setApplicationName("TG")
            self.app.setApplicationDisplayName("TimeGuardian")
            self.app.setOrganizationName("MixTruS")
            # Set explicit AppUserModelID for Windows 7+
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MixTruS.TG")
        except Exception:
            pass

        self.app.setQuitOnLastWindowClosed(False)

        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(None, APP_NAME, "System tray is not available on this system session.")
            sys.exit(1)

        # Detect startup mode via '--startup' flag
        self.is_startup_launch = any(arg.lower() == "--startup" for arg in sys.argv[1:])

        self.cfg = Config()
        self.state = SyncState()

        # Use default app icon (logo.png if present) for app window icon
        try:
            self.app.setWindowIcon(default_app_icon())
        except Exception:
            pass

        self.sync_manager = NtpSyncManager(
            self.cfg,
            self.state,
            on_status_changed=self._on_status_changed,
            on_best_server_changed=self._on_best_server_changed
        )

        # Tray and menu
        self.tray = QSystemTrayIcon()
        self.tray.setToolTip(f"{APP_NAME} - Time synchronization")

        # Ensure a visible default icon (logo or idle) before showing tray
        try:
            icon = default_app_icon()
            if icon.isNull():
                icon = self.app.style().standardIcon(QStyle.SP_ComputerIcon)
            self.tray.setIcon(icon)
        except Exception:
            self.tray.setIcon(self.app.style().standardIcon(QStyle.SP_ComputerIcon))

        self.menu = QMenu()

        self.action_sync_now = QAction("Sync now")
        self.action_sync_now.triggered.connect(self.sync_now)
        self.menu.addAction(self.action_sync_now)

        self.action_auto = QAction("Enable automatic sync")
        self.action_auto.setCheckable(True)
        self.action_auto.setChecked(self.cfg.settings.auto_sync_enabled)
        self.action_auto.triggered.connect(self.toggle_auto_sync)
        self.menu.addAction(self.action_auto)

        self.menu.addSeparator()

        self.action_startup = QAction("Start at Windows login")
        self.action_startup.setCheckable(True)
        self.action_startup.setChecked(self.cfg.settings.launch_at_startup)
        self.action_startup.triggered.connect(self.toggle_startup)
        self.menu.addAction(self.action_startup)

        self.menu.addSeparator()

        self.action_set_tz = QAction("Set timezone (UTC offset with city/region)...")
        self.action_set_tz.triggered.connect(self.set_timezone_dialog)
        self.menu.addAction(self.action_set_tz)

        self.action_tz_from_ip = QAction("Set timezone from IP (modern, multi-source)")
        self.action_tz_from_ip.triggered.connect(self.set_timezone_from_ip)
        self.menu.addAction(self.action_tz_from_ip)

        self.action_region_from_ip = QAction("Set Windows region from IP (optional)")
        self.action_region_from_ip.triggered.connect(self.set_region_from_ip)
        self.menu.addAction(self.action_region_from_ip)

        self.menu.addSeparator()

        self.action_settings = QAction("Settings")
        self.action_settings.triggered.connect(self.open_settings)
        self.menu.addAction(self.action_settings)

        # New GitHub link (ultra aesthetic)
        self.menu.addSeparator()
        self.action_github = QAction(github_icon(), "Open GitHub (mixtrus)")
        self.action_github.triggered.connect(lambda: webbrowser.open("https://github.com/mixtrus"))
        self.menu.addAction(self.action_github)

        # Disabled invitation action (ultra aesthetic star)
        self.action_invite = QAction(star_icon(), "Please star, support, and report bugs on GitHub ♥")
        self.action_invite.setEnabled(False)
        self.menu.addAction(self.action_invite)

        self.menu.addSeparator()

        self.action_exit = QAction("Exit")
        self.action_exit.triggered.connect(self.exit_app)
        self.menu.addAction(self.action_exit)

        self.tray.setContextMenu(self.menu)

        # Initial icon/tooltip
        self._update_tray_icon()

        # Show tray, then optional welcome dialog (manual launch only, and only if enabled)
        self.tray.show()
        if (not self.is_startup_launch) and self.cfg.settings.show_welcome_on_launch:
            self._show_welcome_dialog()

        if self.cfg.settings.auto_sync_enabled:
            self.sync_manager.start()

        self._apply_startup_flag(self.cfg.settings.launch_at_startup)

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self._update_tray_icon)
        self.ui_timer.start(1000)

    def _notify(self, text: str, icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.NoIcon, timeout_ms: int = 4000):
        """
        Centralized notifications that respect the 'Enable notifications' setting.
        Use NoIcon to avoid the default red X; branding is handled via app identity and tray icon.
        """
        if not self.cfg.settings.notifications_enabled:
            return
        try:
            # Title set to "TG" per request
            self.tray.showMessage("TimeGuardian", text, icon, timeout_ms)
        except Exception:
            pass

    def _show_welcome_dialog(self):
        try:
            dlg = WelcomeDialog(self.cfg)
            dlg.exec()
        except Exception:
            self._notify("TimeGuardian started. Right-click the tray icon for Settings.")

    def _on_status_changed(self):
        QTimer.singleShot(0, self._update_tray_icon)

    def _on_best_server_changed(self, server: str, rtt_ms: float):
        # Announce the new best server selection (auto mode only)
        self._notify(f"Auto-selected best NTP server: {server} ({rtt_ms:.0f} ms)")

    def _is_connected(self) -> bool:
        ttl = self.cfg.settings.connected_status_ttl_sec
        if self.state.last_success_time <= 0:
            return False
        return (time.time() - self.state.last_success_time) < ttl

    def _update_tray_icon(self):
        # Icon priority: Default app icon (logo or idle) when no result yet -> Connected -> Disconnected
        if self.state.last_result is None:
            self.tray.setIcon(default_app_icon())
            tip = f"{APP_NAME} - Starting..."
        else:
            if self._is_connected():
                self.tray.setIcon(connected_icon())
            else:
                self.tray.setIcon(disconnected_icon())
            if self.state.last_result and self.state.last_result.success:
                tip = f"{APP_NAME} - Server: {self.state.last_result.server} | RTT: {self.state.last_result.rtt_ms:.0f} ms | Drift: {self.state.last_result.offset_ms:.0f} ms"
            elif self.state.last_result and not self.state.last_result.success:
                method_hint = f" ({self.state.last_result.error})" if self.state.last_result.error else ""
                tip = f"{APP_NAME} - Last error: {self.state.last_result.error or 'Unknown'}{method_hint}"
            else:
                tip = f"{APP_NAME} - Idle"
        self.tray.setToolTip(tip)

    @Slot()
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.sync_now()

    def toggle_auto_sync(self, checked: bool):
        self.cfg.settings.auto_sync_enabled = checked
        self.cfg.save()
        if checked:
            self.sync_manager.start()
        else:
            self.sync_manager.stop()

    def _apply_startup_flag(self, enable: bool):
        exe = sys.executable
        ok, msg = time_utils.register_startup(APP_NAME, exe, enable)
        if not ok:
            self._notify(f"Startup setting failed: {msg}", QSystemTrayIcon.Information, 5000)

    def toggle_startup(self, checked: bool):
        self.cfg.settings.launch_at_startup = checked
        self.cfg.save()
        ok, msg = time_utils.register_startup(APP_NAME, sys.executable, checked)
        self._notify(msg, QSystemTrayIcon.Information, 3000)

    def sync_now(self):
        def work():
            try:
                res = self.sync_manager._choose_server(self.cfg.settings.ntp_timeout_ms)
                if not res:
                    self.state.last_result = NtpResult(server="(none)", rtt_ms=0, offset_ms=0, success=False, error="All servers failed")
                    self._on_status_changed()
                    return
                self.state.last_result = res
                self._on_status_changed()
                changed, msg = self.sync_manager._verify_and_apply_time(res)
                if changed:
                    time.sleep(2.0)
                self.state.last_success_time = time.time()
                self._on_status_changed()
                self._notify(msg)
            except Exception as e:
                self.state.last_result = NtpResult(server="(error)", rtt_ms=0, offset_ms=0, success=False, error=str(e))
                self._on_status_changed()
                self._notify(f"Sync failed: {e}", QSystemTrayIcon.Critical, 5000)
        threading.Thread(target=work, daemon=True).start()

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, parent=None)
        if dlg.exec() == QDialog.Accepted:
            self._apply_startup_flag(self.cfg.settings.launch_at_startup)
            if self.cfg.settings.auto_sync_enabled and not self.state.running:
                self.sync_manager.start()

    def set_timezone_dialog(self):
        choices = time_utils.list_timezone_choices_labels()
        if not choices:
            QMessageBox.warning(None, APP_NAME, "Failed to enumerate Windows time zones.", QMessageBox.Ok)
            return
        d = QDialog()
        d.setWindowTitle("Select timezone")
        v = QVBoxLayout(d)
        lbl = QLabel("Choose timezone (UTC offset with city/region):")
        v.addWidget(lbl)
        cb = QComboBox()
        for label, key in choices:
            cb.addItem(label, userData=key)
        current = time_utils.get_current_timezone()
        idx = next((i for i in range(cb.count()) if cb.itemData(i) == current), -1)
        if idx >= 0:
            cb.setCurrentIndex(idx)
        v.addWidget(cb)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        v.addWidget(btns)
        btns.accepted.connect(d.accept)
        btns.rejected.connect(d.reject)
        if d.exec() == QDialog.Accepted:
            key = cb.currentData()
            ok, msg = time_utils.set_windows_timezone(key)
            self._notify(msg, QSystemTrayIcon.Information if ok else QSystemTrayIcon.Critical, 4000)

    def set_timezone_from_ip(self):
        if not self.cfg.settings.allow_timezone_from_ip:
            ret = QMessageBox.question(None, APP_NAME, "Timezone-from-IP is disabled in Settings. Enable now?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            self.cfg.settings.allow_timezone_from_ip = True
            self.cfg.save()
        def work():
            try:
                iana, off = time_utils.get_timezone_from_ip_modern()
                applied = False
                msg = ""
                if iana:
                    win_key = None
                    for k, v in time_utils.WIN_TO_IANA.items():
                        if v.lower() == (iana or "").lower():
                            win_key = k
                            break
                    if win_key:
                        ok, msg = time_utils.set_windows_timezone(win_key)
                        applied = ok
                    else:
                        msg = f"No Windows zone mapping for {iana}; attempting by offset."
                if not applied and off is not None:
                    ok2, msg2 = time_utils.set_timezone_by_utc_label(time_utils._format_utc_offset(off, with_space=False))
                    msg = msg or msg2
                    applied = ok2
                if not applied:
                    self._notify(f"Failed to set timezone from IP. {msg}", QSystemTrayIcon.Critical, 5000)
                else:
                    self._notify(f"Timezone set. {msg}", QSystemTrayIcon.Information, 5000)
            except Exception as e:
                self._notify(f"Timezone-from-IP failed: {e}", QSystemTrayIcon.Critical, 5000)
        threading.Thread(target=work, daemon=True).start()

    def set_region_from_ip(self):
        if not self.cfg.settings.allow_region_from_ip:
            ret = QMessageBox.question(None, APP_NAME, "Region-from-IP is disabled in Settings. Enable now?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            self.cfg.settings.allow_region_from_ip = True
            self.cfg.save()
        def work():
            try:
                r = requests.get("https://ipapi.co/json/", timeout=5, headers={"User-Agent": "TimeGuardian/1.0"})
                cc = r.json().get("country") if r.status_code == 200 else None
            except Exception:
                cc = None
            if not cc:
                self._notify("Failed to detect country from IP.", QSystemTrayIcon.Critical, 4000)
                return
            ok, msg = time_utils.set_region_from_country_code(cc)
            self._notify(msg, QSystemTrayIcon.Information if ok else QSystemTrayIcon.Critical, 6000)
        threading.Thread(target=work, daemon=True).start()

    def exit_app(self):
        try:
            self.sync_manager.stop()
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()

    def run(self):
        sys.exit(self.app.exec())

def main():
    try:
        app = TrayApp()
        app.run()
    except Exception as e:
        QMessageBox.critical(None, APP_NAME, f"Fatal error: {e}\n\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()