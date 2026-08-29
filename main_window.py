# ==========================================
# 檔案名稱：main_window.py（精簡版）
# 說明：PySide6 版主視窗，整合地圖、雙裝置定位、Z字巡邏、GPX工具。
#
# 精簡版拿掉了：螢幕投影蘑菇偵測、攻擊人數辨識、授權黑名單機制、樣本庫管理、
# 投影視窗選取/置頂/兩指縮放、掃菇歷史紀錄、待匯出清單、Discord匯出、推送網頁App。
# 只保留：Android/iOS 假GPS連線、手動搖桿、弓字型巡邏、GPX路徑工具、雙裝置設定、
# 以及 iOS 開發者模式啟用（因為手機有鎖定密碼時一定要靠這個才能開發者模式生效）。
# ==========================================
from __future__ import annotations

import math
import os
import threading
import time
from shutil import which
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox, QRadioButton, QButtonGroup,
    QGroupBox, QScrollArea, QToolBar, QPlainTextEdit, QListWidget,
    QMessageBox, QFileDialog, QSlider, QInputDialog,
)

from gps_core import GpsEngine, AndroidDeviceScanner, IosDeviceScanner, resolve_app_dir
import favorites_store
import gpx_tools
import region_store
import settings_store
import speed_presets_store
from joystick_widget import JoystickWidget
from map_view import MapView
from tile_cache_server import TileCacheServer
from workers import PatrolWorker

APP_DIR = resolve_app_dir()
USER_SETTINGS_PATH = os.path.join(APP_DIR, "user_settings.json")


def _default_adb_path() -> str:
    bundled = os.path.join(APP_DIR, "adb", "adb.exe")
    if os.path.exists(bundled):
        return bundled
    on_path = which("adb")
    if on_path:
        return on_path
    return r"C:\adb\adb.exe"


class MainWindow(QMainWindow):
    nativeToolMessage = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎯 雙系統定位神盾 Pro（精簡版） (PySide6)")
        self.resize(1400, 900)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._adb_path = _default_adb_path()
        self._ios_scanner = IosDeviceScanner()
        self.engines: dict[str, GpsEngine] = {
            "A": GpsEngine(adb_path=self._adb_path),
            "B": GpsEngine(adb_path=self._adb_path),
        }
        self.curr_pos: dict[str, tuple[float, float]] = {
            "A": (25.033964, 121.564468),
            "B": (25.033964, 121.564468),
        }
        self._last_logged_error: dict[str, Optional[str]] = {"A": None, "B": None}

        # 雙裝置各自獨立的畫圖狀態
        self.polygon_vertices: dict[str, list[tuple[float, float]]] = {"A": [], "B": []}
        self.finished_polygons: dict[str, list[list[tuple[float, float]]]] = {"A": [], "B": []}
        self.patrol_segments: dict[str, list[list[tuple[float, float]]]] = {"A": [], "B": []}
        self.active_draw_device = "A"

        self.patrol_workers = {
            "A": PatrolWorker(self.engines["A"], "A"),
            "B": PatrolWorker(self.engines["B"], "B"),
        }

        self.gpx_points: list[tuple[float, float]] = []
        self.joystick_enabled = False
        self.joystick_step_m = 10.0

        self.favorites: list[dict] = favorites_store.load_favorites(APP_DIR)
        self.speed_presets: list[float] = speed_presets_store.load_speed_presets(APP_DIR)

        # ---------------- 圖磚快取代理伺服器 ----------------
        self.tile_cache = TileCacheServer(os.path.join(APP_DIR, "map_tile_cache.db"))
        port = self.tile_cache.start()
        tile_url = f"http://127.0.0.1:{port}/tile/{{z}}/{{x}}/{{y}}.png"

        # ---------------- 地圖（central widget） ----------------
        self.map_view = MapView(tile_url)
        self.map_view.bridge.contextAction.connect(self._on_map_context_action)
        self.map_view.bridge.gpxPointMoved.connect(self._on_gpx_point_moved)
        self.map_view.bridge.gpxPointDeleted.connect(self._on_gpx_point_deleted)
        self.setCentralWidget(self.map_view)

        self._build_top_hud()
        self._build_docks()
        self._build_toolbar()

        self.nativeToolMessage.connect(self._log)

        for slot in ("A", "B"):
            self.patrol_workers[slot].positionUpdated.connect(
                lambda lat, lng, idx, total, s=slot: self._on_patrol_position(s, lat, lng, idx, total)
            )
            self.patrol_workers[slot].segmentAdvanced.connect(
                lambda idx, total, s=slot: self._log(f"[{s}] ➡️ 已切換到第 {idx + 1}/{total} 個區域")
            )
            self.patrol_workers[slot].remainingTimeUpdated.connect(
                lambda text, s=slot: self.remaining_time_lbl[s].setText(f"預估剩餘時間: {text}")
            )
            self.patrol_workers[slot].logMessage.connect(self._log)

        # 所有欄位都建立好之後，才套用上次關閉時存的設定值
        settings_store.apply(self, settings_store.load_file(USER_SETTINGS_PATH))

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.timeout.connect(self._poll_connection_status)
        self._watchdog_timer.start(1000)

        self.statusBar().showMessage(f"ADB路徑: {self._adb_path}")

    # ==================== 頂部 HUD ====================
    def _build_top_hud(self) -> None:
        hud = QToolBar("HUD")
        hud.setMovable(False)
        hud.setFloatable(False)
        self.hud_label = QLabel("🎯 FAKEGPS CONSOLE")
        self.hud_label.setProperty("role", "title")
        hud.addWidget(self.hud_label)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, hud)

    # ==================== 右側工具列（每個按鈕對應一個懸浮面板 dock） ====================
    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Panels")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.RightToolBarArea, toolbar)

        def add(dock: QDockWidget, icon: str, tip: str):
            # 故意不用 dock.toggleViewAction()：那個是「打勾=加入分頁群組」的核取語意，
            # 面板一旦顯示過，再點同一顆圖示會變成「取消勾選=關掉面板」，而不是「切到這個面板」，
            # 使用者點慣了同一顆圖示反而會把面板關掉，體驗很怪。這裡改成單純的「點一下＝顯示並切到這個面板」，
            # 要關閉面板的話，用面板自己標題列上的關閉按鈕。
            act = QAction(icon, self)
            act.setToolTip(tip)
            act.triggered.connect(lambda: (dock.setVisible(True), dock.raise_()))
            toolbar.addAction(act)

        add(self.dock_manual, "🎮", "手動搖桿神盾")
        add(self.dock_patrol, "🧭", "巡邏設定")
        add(self.dock_gpx, "🗺️", "GPX路徑工具")
        add(self.dock_favorites, "⭐", "收藏夾")
        add(self.dock_dual, "📱", "雙裝置設定")

    def _make_dock(self, key: str, title: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(key)
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.setVisible(False)
        return dock

    def _build_docks(self) -> None:
        self.dock_manual = self._make_dock("manual", "🎮 手動搖桿神盾", self._build_manual_panel())
        self.dock_patrol = self._make_dock("patrol", "🧭 巡邏設定", self._build_patrol_panel())
        self.dock_gpx = self._make_dock("gpx", "🗺️ GPX路徑工具", self._build_gpx_panel())
        self.dock_favorites = self._make_dock("favorites", "⭐ 收藏夾", self._build_favorites_panel())
        self.dock_dual = self._make_dock("dual", "📱 雙裝置設定", self._build_dual_device_panel())
        self.tabifyDockWidget(self.dock_manual, self.dock_patrol)
        self.tabifyDockWidget(self.dock_patrol, self.dock_gpx)
        self.tabifyDockWidget(self.dock_gpx, self.dock_favorites)
        self.tabifyDockWidget(self.dock_favorites, self.dock_dual)

        self.dock_manual.setVisible(True)
        self.dock_manual.raise_()
        self.resizeDocks([self.dock_manual], [580], Qt.Orientation.Horizontal)

    # ==================== 分頁 1：手動搖桿神盾 ====================
    def _build_manual_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        self.transport_combo: dict[str, QComboBox] = {}
        self.status_lbl: dict[str, QLabel] = {}
        self.coord_lbl: dict[str, QLabel] = {}
        self.wifi_entry: dict[str, QLineEdit] = {}
        self.rsd_ip_entry: dict[str, QLineEdit] = {}
        self.rsd_port_entry: dict[str, QLineEdit] = {}

        for slot in ("A", "B"):
            box = QGroupBox(f"📡 手機{slot} 連線")
            form = QVBoxLayout(box)

            combo = QComboBox()
            combo.addItems(["Android USB", "Android WiFi", "iOS 免通道", "iOS RSD完整通道"])
            combo.setObjectName(f"transport_combo_{slot}")
            self.transport_combo[slot] = combo
            form.addWidget(combo)

            wifi_row = QHBoxLayout()
            wifi_row.addWidget(QLabel("WiFi IP:Port"))
            wifi_entry = QLineEdit()
            wifi_entry.setPlaceholderText("192.168.1.23:5555")
            wifi_entry.setObjectName(f"wifi_entry_{slot}")
            self.wifi_entry[slot] = wifi_entry
            wifi_row.addWidget(wifi_entry)
            form.addLayout(wifi_row)

            rsd_row = QHBoxLayout()
            rsd_ip = QLineEdit()
            rsd_ip.setPlaceholderText("RSD IP（留空=用tunneld自動配）")
            rsd_ip.setObjectName(f"rsd_ip_entry_{slot}")
            rsd_port = QLineEdit()
            rsd_port.setPlaceholderText("RSD Port")
            rsd_port.setObjectName(f"rsd_port_entry_{slot}")
            self.rsd_ip_entry[slot] = rsd_ip
            self.rsd_port_entry[slot] = rsd_port
            rsd_row.addWidget(rsd_ip)
            rsd_row.addWidget(rsd_port)
            form.addLayout(rsd_row)

            connect_btn = QPushButton("🔌 連線")
            connect_btn.clicked.connect(lambda _=False, s=slot: self._connect_slot(s))
            form.addWidget(connect_btn)

            status = QLabel("尚未連線")
            status.setProperty("role", "hint")
            self.status_lbl[slot] = status
            form.addWidget(status)

            coord = QLabel(f"緯度: {self.curr_pos[slot][0]:.6f}\n經度: {self.curr_pos[slot][1]:.6f}")
            self.coord_lbl[slot] = coord
            form.addWidget(coord)

            btn_row = QHBoxLayout()
            send_btn = QPushButton("🎯 傳送目前定位")
            send_btn.clicked.connect(lambda _=False, s=slot: self._send_current(s))
            clear_btn = QPushButton("🔄 還原真實定位")
            clear_btn.clicked.connect(lambda _=False, s=slot: self.engines[s].clear_location())
            btn_row.addWidget(send_btn)
            btn_row.addWidget(clear_btn)
            form.addLayout(btn_row)

            layout.addWidget(box)

        # iOS 開發者模式啟用。實測＋查 pymobiledevice3 原始碼確認：AMFI 的「直接自動
        # 啟用」動作(enable)才會檢查密碼狀態，手機有密碼就直接拒絕；「顯示選項」動作
        # (reveal)沒有這個限制，可以在有密碼的手機上用——之後由使用者自己到手機
        # 「設定 > 隱私權與安全性」手動點開，不用先關掉密碼。優先推薦這個方式。
        ios_box = QGroupBox("🍏 iOS 工具")
        ios_layout = QVBoxLayout(ios_box)

        reveal_btn = QPushButton("👁️ 顯示開發者模式選項（推薦，手機有密碼也能用）")
        reveal_btn.clicked.connect(self._reveal_ios_developer_mode)
        ios_layout.addWidget(reveal_btn)
        reveal_hint = QLabel(
            "💡 按下後不會自動開啟，只會讓「開發者模式」出現在手機「設定 > 隱私權\n"
            "　　與安全性」裡，需要你自己到那裡手動點開、照畫面提示重新開機，\n"
            "　　重開後再次確認即可。不用先關掉手機密碼。"
        )
        reveal_hint.setProperty("role", "hint")
        reveal_hint.setWordWrap(True)
        ios_layout.addWidget(reveal_hint)

        dev_mode_btn = QPushButton("⚡ 直接自動啟用（僅限手機沒有設定密碼）")
        dev_mode_btn.clicked.connect(self._enable_ios_developer_mode)
        ios_layout.addWidget(dev_mode_btn)
        ios_hint = QLabel(
            "💡 手機「有」設定螢幕鎖定密碼：這顆按鈕會直接失敗（Apple 的限制，無法\n"
            "　　繞過），請改用上面「顯示開發者模式選項」。\n"
            "💡 手機「沒有」設定密碼：會觸發手機重新開機（約需1-2分鐘），重開後還\n"
            "　　需要在手機畫面上親自點一下確認，這一步沒辦法自動化。"
        )
        ios_hint.setProperty("role", "hint")
        ios_hint.setWordWrap(True)
        ios_layout.addWidget(ios_hint)
        layout.addWidget(ios_box)

        # 手動輸入座標飛過去
        fly_box = QGroupBox("✈️ 手動輸入座標")
        fly_layout = QHBoxLayout(fly_box)
        self.manual_coord_entry = QLineEdit("25.033964, 121.564468")
        self.manual_fly_target = QComboBox()
        self.manual_fly_target.addItems(["A", "B", "A+B"])
        fly_btn = QPushButton("🚀 飛過去")
        fly_btn.clicked.connect(self._fly_to_manual_coord)
        fly_layout.addWidget(self.manual_coord_entry)
        fly_layout.addWidget(self.manual_fly_target)
        fly_layout.addWidget(fly_btn)
        layout.addWidget(fly_box)

        # WASD 搖桿
        joy_box = QGroupBox("🎮 鍵盤搖桿 (WASD，先點視窗任一處取得焦點)")
        joy_layout = QVBoxLayout(joy_box)
        joy_row = QHBoxLayout()
        self.joystick_toggle_btn = QPushButton("🚫 點擊啟用鍵盤搖桿")
        self.joystick_toggle_btn.setCheckable(True)
        self.joystick_toggle_btn.clicked.connect(self._toggle_joystick)
        self.joystick_target_combo = QComboBox()
        self.joystick_target_combo.addItems(["A", "B"])
        self.joystick_target_combo.setObjectName("joystick_target_combo")
        joy_row.addWidget(self.joystick_toggle_btn)
        joy_row.addWidget(self.joystick_target_combo)
        joy_layout.addLayout(joy_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("每步距離(公尺)"))
        self.joy_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.joy_speed_slider.setRange(1, 100)
        self.joy_speed_slider.setValue(10)
        self.joy_speed_slider.setObjectName("joy_speed_slider")
        self.joy_speed_slider.valueChanged.connect(self._update_joystick_speed)
        self.joy_speed_lbl = QLabel("10 公尺")
        speed_row.addWidget(self.joy_speed_slider)
        speed_row.addWidget(self.joy_speed_lbl)
        joy_layout.addLayout(speed_row)

        # 滑鼠拖曳式搖桿：跟上面 WASD 共用同一個「目標裝置」下拉選單跟「每步距離」
        # 滑桿——拖曳這顆搖桿時，每步距離的意義變成「拉到底時的最大速度(公尺/秒)」
        mouse_joy_row = QHBoxLayout()
        mouse_joy_row.addStretch(1)
        self.mouse_joystick = JoystickWidget()
        self.mouse_joystick.moved.connect(self._on_mouse_joystick_moved)
        mouse_joy_row.addWidget(self.mouse_joystick)
        mouse_joy_row.addStretch(1)
        joy_layout.addLayout(mouse_joy_row)
        mouse_joy_hint = QLabel("💡 滑鼠拖曳式搖桿：拖得越遠移動越快，放開自動停止（跟上面共用目標裝置/每步距離設定）")
        mouse_joy_hint.setProperty("role", "hint")
        mouse_joy_hint.setWordWrap(True)
        joy_layout.addWidget(mouse_joy_hint)

        layout.addWidget(joy_box)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(500)
        layout.addWidget(self.log_text, stretch=1)

        return self._wrap_scroll(root)

    def _wrap_scroll(self, inner: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    # ---------------- 手動控制邏輯 ----------------
    def _connect_slot(self, slot: str) -> None:
        engine = self.engines[slot]
        mode = self.transport_combo[slot].currentText()
        if mode == "Android USB":
            engine.start_android_usb()
        elif mode == "Android WiFi":
            ip = self.wifi_entry[slot].text().strip()
            if ip:
                scanner = AndroidDeviceScanner(self._adb_path)
                scanner.try_connect_wifi(ip)
                engine.android_serial = ip
            engine.start_android_usb()
        elif mode == "iOS 免通道":
            engine.start_ios_userspace()
        else:
            ip = self.rsd_ip_entry[slot].text().strip() or None
            port = self.rsd_port_entry[slot].text().strip() or None
            engine.start_ios_rsd(ip, port)
        self._log(f"[{slot}] 正在連線（{mode}）...")

    def _poll_connection_status(self) -> None:
        for slot in ("A", "B"):
            engine = self.engines[slot]
            if engine.is_connecting:
                text = "連線中..."
            elif engine.is_alive:
                text = f"已連線（{engine.active_transport.value if engine.active_transport else '?'}）"
            elif engine.last_error:
                text = f"❌ {engine.last_error}"
            else:
                text = "尚未連線"
            self.status_lbl[slot].setText(text)

            # engine.is_alive 在「已連線」狀態下不會反映單次送定位失敗（那只是
            # engine.last_error 被更新，連線本身沒斷），狀態列不會自動顯示出來，
            # 使用者會看到「已連線」卻完全不知道剛剛那次送定位其實失敗了。
            # 這裡額外把「新出現的」錯誤內容也印到日誌區，同一則錯誤不重複洗版。
            if engine.last_error and engine.last_error != self._last_logged_error.get(slot):
                self._last_logged_error[slot] = engine.last_error
                self._log(f"[{slot}] ❌ {engine.last_error}")
        # 頂部 HUD 摘要
        a_state = "🟢" if self.engines["A"].is_alive else "⚪"
        b_state = "🟢" if self.engines["B"].is_alive else "⚪"
        a_patrol = "巡邏中" if self.patrol_workers["A"].is_patrolling else "待命"
        b_patrol = "巡邏中" if self.patrol_workers["B"].is_patrolling else "待命"
        self.hud_label.setText(f"🎯 A: {a_state} {a_patrol}　B: {b_state} {b_patrol}")

    def _send_current(self, slot: str) -> None:
        lat, lng = self.curr_pos[slot]
        self.engines[slot].set_location(lat, lng)
        self._log(f"[{slot}] 🎯 傳送定位 ({lat:.6f}, {lng:.6f})")

    def send_location_direct(self, slot: str, lat: float, lng: float) -> None:
        self.curr_pos[slot] = (lat, lng)
        self.engines[slot].set_location(lat, lng)
        self.coord_lbl[slot].setText(f"緯度: {lat:.6f}\n經度: {lng:.6f}")
        color = "#2ecc71" if slot == "A" else "#3498db"
        self.map_view.set_marker(f"pos_{slot}", lat, lng, color=color, label=f"手機{slot}")

    def _fly_to_manual_coord(self) -> None:
        text = self.manual_coord_entry.text().strip()
        try:
            lat_s, lng_s = [p.strip() for p in text.split(",")]
            lat, lng = float(lat_s), float(lng_s)
        except Exception:
            QMessageBox.critical(self, "錯誤", "座標格式錯誤，請用「緯度,經度」")
            return
        target = self.manual_fly_target.currentText()
        slots = ["A", "B"] if target == "A+B" else [target]
        for slot in slots:
            self.send_location_direct(slot, lat, lng)
        self.map_view.set_position(lat, lng, 17)

    def _toggle_joystick(self, checked: bool) -> None:
        self.joystick_enabled = checked
        self.joystick_toggle_btn.setText("✅ 鍵盤搖桿已啟用" if checked else "🚫 點擊啟用鍵盤搖桿")
        self.setFocus()

    def _update_joystick_speed(self, value: int) -> None:
        self.joystick_step_m = float(value)
        self.joy_speed_lbl.setText(f"{value} 公尺")

    def _on_mouse_joystick_moved(self, dx: float, dy: float) -> None:
        """
        滑鼠搖桿每個 tick(約50ms)呼叫一次。dx/dy 是搖桿頭偏移量(-1.0~1.0)，
        跟 keyPressEvent 的「按一下走固定距離」語意不同——這裡把 joystick_step_m
        當成「拉到底時的最大速度(公尺/秒)」，乘上 tick 秒數跟目前偏移強度，
        才是這個 tick 該移動的距離，這樣拖曳感覺才會平順，不會用彈的。
        """
        magnitude = math.hypot(dx, dy)
        if magnitude < 0.05:
            return
        slot = self.joystick_target_combo.currentText()
        lat, lng = self.curr_pos[slot]
        tick_seconds = 0.05
        dist = self.joystick_step_m * min(magnitude, 1.0) * tick_seconds
        # 螢幕座標 y 往下為正，緯度往北為正，角度計算時把 dy 取負號翻正
        angle = math.atan2(-dy, dx)
        lat += (dist * math.sin(angle)) / 111000.0
        lng += (dist * math.cos(angle)) / (100000.0 * math.cos(math.radians(lat)))
        self.send_location_direct(slot, round(lat, 6), round(lng, 6))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.joystick_enabled or event.isAutoRepeat():
            super().keyPressEvent(event)
            return
        slot = self.joystick_target_combo.currentText()
        lat, lng = self.curr_pos[slot]
        step = self.joystick_step_m
        key = event.key()
        moved = True
        if key == Qt.Key.Key_W:
            lat += step / 111000.0
        elif key == Qt.Key.Key_S:
            lat -= step / 111000.0
        elif key == Qt.Key.Key_A:
            lng -= step / (100000.0 * math.cos(math.radians(lat)))
        elif key == Qt.Key.Key_D:
            lng += step / (100000.0 * math.cos(math.radians(lat)))
        else:
            moved = False
        if moved:
            self.send_location_direct(slot, round(lat, 6), round(lng, 6))
        super().keyPressEvent(event)

    def _log(self, msg: str) -> None:
        self.log_text.appendPlainText(msg)

    # ---------------- iOS 開發者模式 ----------------
    def _reveal_ios_developer_mode(self) -> None:
        self._log("🍏 正在顯示開發者模式選項（USB 連線）...")

        def worker():
            ok, msg = self._ios_scanner.reveal_developer_mode()
            prefix = "✅" if ok else "❌"
            self.nativeToolMessage.emit(f"🍏 {prefix} {msg}")

        threading.Thread(target=worker, daemon=True).start()

    def _enable_ios_developer_mode(self) -> None:
        self._log("🍏 正在觸發 iOS 開發者模式自動啟用流程（USB 連線）...")

        def worker():
            ok, msg = self._ios_scanner.enable_developer_mode()
            prefix = "✅" if ok else "❌"
            self.nativeToolMessage.emit(f"🍏 {prefix} {msg}")

        threading.Thread(target=worker, daemon=True).start()

    # ==================== 分頁 2：巡邏設定 ====================
    def _build_patrol_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        draw_box = QGroupBox("🖊️ 目前繪製對象（地圖右鍵新增的多邊形頂點會加進這一台的區域）")
        draw_layout = QHBoxLayout(draw_box)
        self.draw_device_group = QButtonGroup(self)
        for slot in ("A", "B"):
            rb = QRadioButton(f"手機{slot}")
            rb.setChecked(slot == "A")
            rb.setObjectName(f"draw_device_radio_{slot}")
            rb.toggled.connect(lambda checked, s=slot: self._set_active_draw_device(s) if checked else None)
            self.draw_device_group.addButton(rb)
            draw_layout.addWidget(rb)
        layout.addWidget(draw_box)

        poly_row = QHBoxLayout()
        finish_btn = QPushButton("✅ 完成此區域")
        finish_btn.clicked.connect(self._finish_current_polygon)
        clear_btn = QPushButton("🧹 清除目前畫的區域")
        clear_btn.clicked.connect(self._clear_active_polygons)
        poly_row.addWidget(finish_btn)
        poly_row.addWidget(clear_btn)
        layout.addLayout(poly_row)

        region_box = QGroupBox("📂 已存區域清單（依「目前繪製對象」各自存成獨立檔案）")
        region_layout = QVBoxLayout(region_box)
        self.region_list_combo = QComboBox()
        region_layout.addWidget(self.region_list_combo)
        region_btn_row = QHBoxLayout()
        save_new_btn = QPushButton("💾 另存新清單")
        save_new_btn.clicked.connect(self._region_save_as_new)
        update_btn = QPushButton("🔄 更新")
        update_btn.clicked.connect(self._region_update_selected)
        load_btn = QPushButton("📂 載入")
        load_btn.clicked.connect(self._region_load_selected)
        delete_btn = QPushButton("🗑️ 刪除")
        delete_btn.setObjectName("DangerButton")
        delete_btn.clicked.connect(self._region_delete_selected)
        for b in (save_new_btn, update_btn, load_btn, delete_btn):
            region_btn_row.addWidget(b)
        region_layout.addLayout(region_btn_row)
        layout.addWidget(region_box)
        self._refresh_region_list_combo()

        spacing_row = QHBoxLayout()
        spacing_row.addWidget(QLabel("軌道間隔(公尺)"))
        self.spacing_entry = QLineEdit("850")
        self.spacing_entry.setObjectName("spacing_entry")
        spacing_row.addWidget(self.spacing_entry)
        gen_btn = QPushButton("⚙️ 產生弓字路徑")
        gen_btn.clicked.connect(self._generate_z_path)
        spacing_row.addWidget(gen_btn)
        layout.addLayout(spacing_row)

        self.patrol_speed_entry: dict[str, QComboBox] = {}
        self.remaining_time_lbl: dict[str, QLabel] = {}
        self.patrol_progress_lbl: dict[str, QLabel] = {}

        for slot in ("A", "B"):
            box = QGroupBox(f"🚀 手機{slot} 巡邏控制")
            v = QVBoxLayout(box)

            speed_row = QHBoxLayout()
            speed_row.addWidget(QLabel("時速(km/h)"))
            entry = QComboBox()
            entry.setEditable(True)
            entry.setObjectName(f"patrol_speed_entry_{slot}")
            for preset in self.speed_presets:
                entry.addItem(str(preset))
            entry.setCurrentText("20")
            self.patrol_speed_entry[slot] = entry
            speed_row.addWidget(entry, stretch=1)
            save_speed_btn = QPushButton("💾")
            save_speed_btn.setToolTip("把目前輸入的時速加入常用速度清單")
            save_speed_btn.setMaximumWidth(32)
            save_speed_btn.clicked.connect(lambda _=False, s=slot: self._save_speed_preset(s))
            speed_row.addWidget(save_speed_btn)
            v.addLayout(speed_row)

            btn_row = QHBoxLayout()
            start_btn = QPushButton("▶️ 開始")
            start_btn.setObjectName("SuccessButton")
            start_btn.clicked.connect(lambda _=False, s=slot: self._start_patrol(s))
            pause_btn = QPushButton("⏸️ 暫停/恢復")
            pause_btn.clicked.connect(lambda _=False, s=slot: self._pause_resume_patrol(s))
            stop_btn = QPushButton("⏹️ 終止")
            stop_btn.setObjectName("DangerButton")
            stop_btn.clicked.connect(lambda _=False, s=slot: self._stop_patrol(s))
            btn_row.addWidget(start_btn)
            btn_row.addWidget(pause_btn)
            btn_row.addWidget(stop_btn)
            v.addLayout(btn_row)

            progress = QLabel("尚未開始")
            progress.setProperty("role", "hint")
            self.patrol_progress_lbl[slot] = progress
            v.addWidget(progress)

            remaining = QLabel("預估剩餘時間: --")
            self.remaining_time_lbl[slot] = remaining
            v.addWidget(remaining)

            layout.addWidget(box)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _set_active_draw_device(self, slot: str) -> None:
        self.active_draw_device = slot
        if hasattr(self, "region_list_combo"):
            self._refresh_region_list_combo()

    # ---------------- 已存區域清單（存/讀多組多邊形組合） ----------------
    def _refresh_region_list_combo(self) -> None:
        data = region_store.load_regions(APP_DIR, self.active_draw_device)
        names = sorted(data.keys())
        self.region_list_combo.clear()
        self.region_list_combo.addItems(names)

    def _region_save_as_new(self) -> None:
        slot = self.active_draw_device
        if not self.finished_polygons[slot]:
            QMessageBox.warning(self, "提示", "目前沒有任何「已完成」的區域可以儲存（記得先按「完成此區域」）。")
            return
        name, ok = QInputDialog.getText(self, "另存為新清單", "請輸入這組區域的清單名稱：")
        name = name.strip() if ok and name else ""
        if not name:
            return
        data = region_store.load_regions(APP_DIR, slot)
        if name in data and QMessageBox.question(
            self, "確認覆蓋", f"清單「{name}」已經存在，確定要覆蓋掉原本的內容嗎？"
        ) != QMessageBox.StandardButton.Yes:
            return
        data[name] = [list(polygon) for polygon in self.finished_polygons[slot]]
        region_store.save_regions(APP_DIR, slot, data)
        self._refresh_region_list_combo()
        self.region_list_combo.setCurrentText(name)
        self._log(f"[{slot}] 💾 已把目前 {len(self.finished_polygons[slot])} 個區域存成清單「{name}」")

    def _region_update_selected(self) -> None:
        slot = self.active_draw_device
        name = self.region_list_combo.currentText()
        if not name:
            QMessageBox.warning(self, "提示", "請先選擇一個要更新的清單。")
            return
        if not self.finished_polygons[slot]:
            QMessageBox.warning(self, "提示", "目前沒有任何「已完成」的區域可以用來更新。")
            return
        if QMessageBox.question(
            self, "確認覆蓋", f"確定要用目前畫面上的區域，覆蓋掉清單「{name}」原本的內容嗎？"
        ) != QMessageBox.StandardButton.Yes:
            return
        data = region_store.load_regions(APP_DIR, slot)
        data[name] = [list(polygon) for polygon in self.finished_polygons[slot]]
        region_store.save_regions(APP_DIR, slot, data)
        self._log(f"[{slot}] 🔄 已用目前的 {len(self.finished_polygons[slot])} 個區域更新清單「{name}」")

    def _region_load_selected(self) -> None:
        slot = self.active_draw_device
        name = self.region_list_combo.currentText()
        if not name:
            QMessageBox.warning(self, "提示", "請先選擇一個要載入的清單。")
            return
        data = region_store.load_regions(APP_DIR, slot)
        if name not in data:
            QMessageBox.critical(self, "錯誤", f"找不到清單「{name}」，可能已經被刪除。")
            self._refresh_region_list_combo()
            return
        if QMessageBox.question(
            self, "確認載入",
            f"載入清單「{name}」會取代目前畫面上所有已完成的區域跟正在畫的多邊形，\n"
            f"目前沒存起來的東西會消失，確定要繼續嗎？",
        ) != QMessageBox.StandardButton.Yes:
            return

        self.polygon_vertices[slot].clear()
        self.finished_polygons[slot].clear()
        self.map_view.clear_polygons_by_prefix(f"poly_{slot}_")
        self.map_view.clear_paths_by_prefix(f"poly_progress_{slot}")

        color = "#2ecc71" if slot == "A" else "#3498db"
        for idx, polygon in enumerate(data[name]):
            polygon_tuples = [tuple(pt) for pt in polygon]
            self.finished_polygons[slot].append(polygon_tuples)
            self.map_view.set_polygon(f"poly_{slot}_{idx}", polygon_tuples, color=color)

        self._log(f"[{slot}] 📂 已載入清單「{name}」，共 {len(self.finished_polygons[slot])} 個區域")

    def _region_delete_selected(self) -> None:
        slot = self.active_draw_device
        name = self.region_list_combo.currentText()
        if not name:
            QMessageBox.warning(self, "提示", "請先選擇一個要刪除的清單。")
            return
        if QMessageBox.question(self, "確認刪除", f"確定要刪除清單「{name}」嗎？這個動作沒辦法復原。") != QMessageBox.StandardButton.Yes:
            return
        data = region_store.load_regions(APP_DIR, slot)
        data.pop(name, None)
        region_store.save_regions(APP_DIR, slot, data)
        self._refresh_region_list_combo()
        self._log(f"[{slot}] 🗑️ 已刪除清單「{name}」")

    def _finish_current_polygon(self) -> None:
        slot = self.active_draw_device
        pts = self.polygon_vertices[slot]
        if len(pts) < 3:
            QMessageBox.warning(self, "提示", "至少需要 3 個頂點才能完成一個區域！")
            return
        self.finished_polygons[slot].append(list(pts))
        idx = len(self.finished_polygons[slot]) - 1
        color = "#2ecc71" if slot == "A" else "#3498db"
        self.map_view.set_polygon(f"poly_{slot}_{idx}", pts, color=color)
        self.map_view.remove_path(f"poly_progress_{slot}")
        # 區域已經畫成實體多邊形了，逐點的臨時標記留著沒意義，反而會擋住底下的地圖
        self.map_view.clear_markers_by_prefix(f"poly_vertex_{slot}_")
        pts.clear()
        self._log(f"[{slot}] ✅ 已完成第 {idx + 1} 個區域")

    def _clear_active_polygons(self) -> None:
        slot = self.active_draw_device
        self.polygon_vertices[slot].clear()
        self.finished_polygons[slot].clear()
        self.patrol_segments[slot].clear()
        self.map_view.clear_polygons_by_prefix(f"poly_{slot}_")
        self.map_view.clear_paths_by_prefix(f"poly_progress_{slot}")
        self.map_view.clear_paths_by_prefix(f"patrol_{slot}_")
        self.map_view.clear_markers_by_prefix(f"poly_vertex_{slot}_")
        self._log(f"[{slot}] 🧹 已清除所有已畫區域")

    @staticmethod
    def compute_boustrophedon_path(
        polygon_coords: list[tuple[float, float]], spacing_m: float
    ) -> list[tuple[float, float]]:
        """弓字型掃描路徑演算法，逐行移植自 Tkinter 版 compute_boustrophedon_path()"""
        lat0, lng0 = polygon_coords[0]

        def to_meters(lat, lng):
            y = (lat - lat0) * 111000.0
            x = (lng - lng0) * 100000.0 * math.cos(math.radians(lat0))
            return x, y

        def to_coords(x, y):
            lat = lat0 + (y / 111000.0)
            lng = lng0 + (x / (100000.0 * math.cos(math.radians(lat0))))
            return lat, lng

        poly_pts = [to_meters(p[0], p[1]) for p in polygon_coords]
        xs = [p[0] for p in poly_pts]
        ys = [p[1] for p in poly_pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        path_pts = []
        current_y = max_y - (spacing_m / 2.0)
        direction = 1
        n_verts = len(poly_pts)
        poly_edges = [(poly_pts[i], poly_pts[(i + 1) % n_verts]) for i in range(n_verts)]

        def scanline_intersections(y, edges):
            xs_ = []
            for (p1x, p1y), (p2x, p2y) in edges:
                if p1y == p2y:
                    continue
                if y > min(p1y, p2y) and y <= max(p1y, p2y):
                    x_int = p1x + (y - p1y) * (p2x - p1x) / (p2y - p1y)
                    xs_.append(x_int)
            return xs_

        last_x = None
        while current_y >= min_y:
            xs_ = scanline_intersections(current_y, poly_edges)
            if xs_:
                left_x, right_x = min(xs_), max(xs_)
                if direction == 1:
                    if last_x is not None:
                        path_pts.append((left_x, current_y))
                    path_pts.append((right_x, current_y))
                    last_x = right_x
                else:
                    if last_x is not None:
                        path_pts.append((right_x, current_y))
                    path_pts.append((left_x, current_y))
                    last_x = left_x
                direction *= -1
            current_y -= spacing_m

        return [to_coords(x, y) for x, y in path_pts]

    def _generate_z_path(self) -> None:
        slot = self.active_draw_device
        if len(self.polygon_vertices[slot]) >= 3:
            self._finish_current_polygon()
        if not self.finished_polygons[slot]:
            QMessageBox.warning(self, "提示", "請至少畫一個多邊形區域（至少3個頂點）！")
            return
        try:
            spacing = float(self.spacing_entry.text())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "軌道間隔請填入數字！")
            return

        self.map_view.clear_paths_by_prefix(f"patrol_{slot}_")
        segments = []
        colors = ["#e74c3c", "#ff9800", "#2196f3", "#e91e63", "#4caf50", "#9c27b0", "#00bcd4"]
        for i, polygon in enumerate(self.finished_polygons[slot]):
            path = self.compute_boustrophedon_path(polygon, spacing)
            if not path:
                continue
            segments.append(path)
            self.map_view.set_path(f"patrol_{slot}_{i}", path, color=colors[i % len(colors)])
            self.map_view.set_marker(f"patrol_start_{slot}_{i}", path[0][0], path[0][1], color="#2ecc71", label=f"區{i+1}起點")

        if not segments:
            QMessageBox.warning(self, "提示", "所有區域都計算失敗，請確認多邊形大小與間隔比例！")
            return

        self.patrol_segments[slot] = segments
        self.patrol_workers[slot].set_segments(segments)
        total_points = sum(len(p) for p in segments)
        self._log(f"[{slot}] ✅ 弓字型軌道規劃成功！共 {len(segments)} 個區域，總計 {total_points} 個轉折點")
        mid_lat = sum(p[0] for p in segments[0]) / len(segments[0])
        mid_lng = sum(p[1] for p in segments[0]) / len(segments[0])
        self.map_view.set_position(mid_lat, mid_lng, 17)

    def _start_patrol(self, slot: str) -> None:
        try:
            speed = float(self.patrol_speed_entry[slot].currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "時速必須為數字！")
            return
        self.patrol_workers[slot].start(speed)
        self.patrol_progress_lbl[slot].setText("巡邏中...")

    def _save_speed_preset(self, slot: str) -> None:
        try:
            value = float(self.patrol_speed_entry[slot].currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "時速必須為數字才能存成常用速度！")
            return
        if value in self.speed_presets:
            self._log(f"💡 時速 {value} km/h 已經在常用速度清單裡了")
            return
        self.speed_presets.append(value)
        self.speed_presets.sort()
        speed_presets_store.save_speed_presets(APP_DIR, self.speed_presets)
        # 兩台裝置的清單是共用的，存一次兩邊下拉選單都要一起更新，
        # 更新時保留使用者目前輸入/選到的文字，不要被重建清單洗掉
        for s in ("A", "B"):
            combo = self.patrol_speed_entry[s]
            current_text = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(str(p) for p in self.speed_presets)
            combo.setCurrentText(current_text)
            combo.blockSignals(False)
        self._log(f"💾 已把時速 {value} km/h 加入常用速度清單")

    def _pause_resume_patrol(self, slot: str) -> None:
        worker = self.patrol_workers[slot]
        if not worker.is_patrolling:
            return
        if worker.is_paused:
            worker.resume()
            self.patrol_progress_lbl[slot].setText("巡邏中...")
        else:
            worker.pause()
            self.patrol_progress_lbl[slot].setText("已暫停")

    def _stop_patrol(self, slot: str) -> None:
        self.patrol_workers[slot].stop()
        self.patrol_progress_lbl[slot].setText("已終止")
        self.remaining_time_lbl[slot].setText("預估剩餘時間: --")

    def _on_patrol_position(self, slot: str, lat: float, lng: float, node_idx: int, total: int) -> None:
        self.curr_pos[slot] = (lat, lng)
        self.coord_lbl[slot].setText(f"緯度: {lat:.6f}\n經度: {lng:.6f}")
        color = "#2ecc71" if slot == "A" else "#3498db"
        self.map_view.set_marker(f"pos_{slot}", lat, lng, color=color, label=f"手機{slot}")
        self.patrol_progress_lbl[slot].setText(f"巡邏中：第 {node_idx}/{total} 個節點")

    # ==================== 分頁：收藏夾 ====================
    def _build_favorites_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        hint = QLabel("💡 雙擊清單項目可直接飛過去；地圖右鍵選單也有「⭐ 加入收藏」")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.favorites_list = QListWidget()
        self.favorites_list.itemDoubleClicked.connect(lambda _item: self._fly_to_selected_favorite())
        layout.addWidget(self.favorites_list, stretch=1)

        add_btn = QPushButton("➕ 把目前位置加入收藏")
        add_btn.clicked.connect(self._add_current_position_to_favorites)
        layout.addWidget(add_btn)

        btn_row = QHBoxLayout()
        fly_btn = QPushButton("🚀 飛過去")
        fly_btn.clicked.connect(self._fly_to_selected_favorite)
        delete_btn = QPushButton("🗑️ 刪除選取")
        delete_btn.setObjectName("DangerButton")
        delete_btn.clicked.connect(self._delete_selected_favorite)
        btn_row.addWidget(fly_btn)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

        io_row = QHBoxLayout()
        export_btn = QPushButton("📤 全部匯出")
        export_btn.clicked.connect(self._export_favorites)
        import_btn = QPushButton("📥 全部匯入")
        import_btn.clicked.connect(self._import_favorites)
        io_row.addWidget(export_btn)
        io_row.addWidget(import_btn)
        layout.addLayout(io_row)

        self._refresh_favorites_list()
        return root

    def _refresh_favorites_list(self) -> None:
        self.favorites_list.clear()
        for fav in self.favorites:
            self.favorites_list.addItem(f"{fav['name']}　({fav['lat']:.6f}, {fav['lng']:.6f})")

    def _add_favorite(self, name: str, lat: float, lng: float) -> None:
        self.favorites.append({"name": name, "lat": lat, "lng": lng})
        favorites_store.save_favorites(APP_DIR, self.favorites)
        self._refresh_favorites_list()
        self._log(f"⭐ 已加入收藏「{name}」({lat:.6f}, {lng:.6f})")

    def _add_current_position_to_favorites(self) -> None:
        slot = self.active_draw_device
        lat, lng = self.curr_pos[slot]
        name, ok = QInputDialog.getText(
            self, "加入收藏", "請輸入這個座標的名稱：", text=f"收藏 {len(self.favorites) + 1}"
        )
        name = name.strip() if ok and name else ""
        if name:
            self._add_favorite(name, lat, lng)

    def _fly_to_selected_favorite(self) -> None:
        row = self.favorites_list.currentRow()
        if row < 0 or row >= len(self.favorites):
            QMessageBox.warning(self, "提示", "請先選擇一筆收藏")
            return
        fav = self.favorites[row]
        slot = self.active_draw_device
        self.send_location_direct(slot, fav["lat"], fav["lng"])
        self.map_view.set_position(fav["lat"], fav["lng"], 17)
        self._log(f"[{slot}] 🚀 已飛往收藏「{fav['name']}」")

    def _delete_selected_favorite(self) -> None:
        row = self.favorites_list.currentRow()
        if row < 0 or row >= len(self.favorites):
            QMessageBox.warning(self, "提示", "請先選擇一筆收藏")
            return
        removed = self.favorites.pop(row)
        favorites_store.save_favorites(APP_DIR, self.favorites)
        self._refresh_favorites_list()
        self._log(f"🗑️ 已刪除收藏「{removed['name']}」")

    def _export_favorites(self) -> None:
        if not self.favorites:
            QMessageBox.warning(self, "提示", "目前沒有收藏可以匯出")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出收藏", os.path.join(APP_DIR, "favorites_export.json"), "JSON Files (*.json)"
        )
        if not path:
            return
        favorites_store.save_favorites_to(path, self.favorites)
        self._log(f"📤 已匯出 {len(self.favorites)} 筆收藏到: {path}")

    def _import_favorites(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入收藏", APP_DIR, "JSON Files (*.json)")
        if not path:
            return
        try:
            imported = favorites_store.load_favorites_from(path)
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"匯入失敗：{e}")
            return
        if not imported:
            QMessageBox.warning(self, "提示", "這個檔案裡沒有有效的收藏資料")
            return
        self.favorites.extend(imported)
        favorites_store.save_favorites(APP_DIR, self.favorites)
        self._refresh_favorites_list()
        self._log(f"📥 已匯入 {len(imported)} 筆收藏")

    # ==================== 分頁 3：雙裝置設定 ====================
    def _build_dual_device_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel("💡 兩支手機都用 USB 接上電腦後，按「掃描裝置」，再分別指定手機A/B"))

        scan_btn = QPushButton("🔍 掃描已連接裝置")
        scan_btn.clicked.connect(self._scan_dual_devices)
        layout.addWidget(scan_btn)

        form = QFormLayout()
        self.device_combo_a = QComboBox()
        self.device_combo_b = QComboBox()
        form.addRow("手機A 裝置序號", self.device_combo_a)
        form.addRow("手機B 裝置序號", self.device_combo_b)
        layout.addLayout(form)

        apply_btn = QPushButton("✅ 套用裝置指定")
        apply_btn.clicked.connect(self._apply_dual_device_assignment)
        layout.addWidget(apply_btn)

        self.dual_device_status_lbl = QLabel("尚未套用裝置指定")
        self.dual_device_status_lbl.setProperty("role", "hint")
        self.dual_device_status_lbl.setWordWrap(True)
        layout.addWidget(self.dual_device_status_lbl)
        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _scan_dual_devices(self) -> None:
        scanner = AndroidDeviceScanner(self._adb_path)
        devices = scanner.list_usb_devices()
        self.device_combo_a.clear()
        self.device_combo_b.clear()
        for d in devices:
            self.device_combo_a.addItem(d.display_name, d.identifier)
            self.device_combo_b.addItem(d.display_name, d.identifier)
        self._log(f"🔍 掃描到 {len(devices)} 台裝置")

    def _apply_dual_device_assignment(self) -> None:
        serial_a = self.device_combo_a.currentData()
        serial_b = self.device_combo_b.currentData()
        if serial_a:
            self.engines["A"].android_serial = serial_a
        if serial_b:
            self.engines["B"].android_serial = serial_b
        self.dual_device_status_lbl.setText(f"手機A: {serial_a or '未指定'}\n手機B: {serial_b or '未指定'}")

    # ==================== 分頁 4：GPX 路徑工具 ====================
    def _build_gpx_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("座標文字（每行一組，支援多種格式與 Plus Code）"))
        self.gpx_text_edit = QPlainTextEdit()
        self.gpx_text_edit.setMaximumHeight(120)
        layout.addWidget(self.gpx_text_edit)

        parse_btn = QPushButton("📥 解析文字座標")
        parse_btn.clicked.connect(self._gpx_parse_text)
        layout.addWidget(parse_btn)

        self.gpx_list = QListWidget()
        self.gpx_list.setMaximumHeight(140)
        layout.addWidget(self.gpx_list)

        file_row = QHBoxLayout()
        import_btn = QPushButton("📂 匯入GPX檔案")
        import_btn.clicked.connect(self._gpx_import_file)
        export_gpx_btn = QPushButton("💾 匯出GPX")
        export_gpx_btn.clicked.connect(self._gpx_export_gpx)
        export_txt_btn = QPushButton("💾 匯出TXT")
        export_txt_btn.clicked.connect(self._gpx_export_txt)
        file_row.addWidget(import_btn)
        file_row.addWidget(export_gpx_btn)
        file_row.addWidget(export_txt_btn)
        layout.addLayout(file_row)

        sort_row = QHBoxLayout()
        sort_btn = QPushButton("🔀 最短路徑排序")
        sort_btn.clicked.connect(self._gpx_sort)
        reverse_btn = QPushButton("↩️ 反轉順序")
        reverse_btn.clicked.connect(self._gpx_reverse)
        clear_btn = QPushButton("🧹 清空")
        clear_btn.clicked.connect(self._gpx_clear)
        sort_row.addWidget(sort_btn)
        sort_row.addWidget(reverse_btn)
        sort_row.addWidget(clear_btn)
        layout.addLayout(sort_row)

        endpoint_row = QHBoxLayout()
        endpoint_row.addWidget(QLabel("終點模式"))
        self.gpx_endpoint_combo = QComboBox()
        self.gpx_endpoint_combo.addItems(["last", "centroid", "loop"])
        self.gpx_endpoint_combo.setObjectName("gpx_endpoint_combo")
        endpoint_row.addWidget(self.gpx_endpoint_combo)
        endpoint_apply = QPushButton("套用")
        endpoint_apply.clicked.connect(self._gpx_apply_endpoint)
        endpoint_row.addWidget(endpoint_apply)
        layout.addLayout(endpoint_row)

        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("座標偏移"))
        self.gpx_offset_combo = QComboBox()
        self.gpx_offset_combo.addItems(["none", "standard", "large"])
        self.gpx_offset_combo.setObjectName("gpx_offset_combo")
        offset_row.addWidget(self.gpx_offset_combo)
        offset_apply = QPushButton("套用")
        offset_apply.clicked.connect(self._gpx_apply_offset)
        offset_row.addWidget(offset_apply)
        layout.addLayout(offset_row)

        circle_box = QGroupBox("⭕ 繞圈路徑產生器（用地圖右鍵「GPX工具：新增此點」設定中心點的最後一點）")
        circle_layout = QHBoxLayout(circle_box)
        self.circle_radius = QLineEdit("100")
        self.circle_radius.setObjectName("circle_radius")
        self.circle_points = QLineEdit("12")
        self.circle_points.setObjectName("circle_points")
        self.circle_laps = QLineEdit("1")
        self.circle_laps.setObjectName("circle_laps")
        circle_layout.addWidget(QLabel("半徑(m)"))
        circle_layout.addWidget(self.circle_radius)
        circle_layout.addWidget(QLabel("點數"))
        circle_layout.addWidget(self.circle_points)
        circle_layout.addWidget(QLabel("圈數"))
        circle_layout.addWidget(self.circle_laps)
        circle_btn = QPushButton("產生")
        circle_btn.clicked.connect(self._gpx_generate_circle)
        circle_layout.addWidget(circle_btn)
        layout.addWidget(circle_box)

        self.gpx_stats_lbl = QLabel("尚無座標")
        self.gpx_stats_lbl.setProperty("role", "hint")
        layout.addWidget(self.gpx_stats_lbl)

        preview_btn = QPushButton("🗺️ 在地圖上預覽")
        preview_btn.clicked.connect(self._gpx_preview)
        layout.addWidget(preview_btn)

        use_btn = QPushButton("🚀 用這條路線開始巡邏（套用到目前繪製對象）")
        use_btn.clicked.connect(self._gpx_start_patrol_with_route)
        layout.addWidget(use_btn)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    # GPX 點在地圖上畫成可拖曳/可刪除的數字標記；點數太多時（例如匯入了很大的
    # GPX 檔）畫出上千個可拖曳標記會讓地圖變得很卡，超過這個數量就不畫地圖標記，
    # 但清單本身還是照常可以編輯，不受影響
    _MAX_GPX_MAP_MARKERS = 300

    def _refresh_gpx_list(self) -> None:
        self.gpx_list.clear()
        for lat, lng in self.gpx_points:
            self.gpx_list.addItem(f"{lat:.6f}, {lng:.6f}")
        self._gpx_update_stats()
        if len(self.gpx_points) <= self._MAX_GPX_MAP_MARKERS:
            self.map_view.set_gpx_points(self.gpx_points)
        else:
            self.map_view.clear_gpx_points()

    def _on_gpx_point_moved(self, index: int, lat: float, lng: float) -> None:
        if 0 <= index < len(self.gpx_points):
            self.gpx_points[index] = (lat, lng)
            self._refresh_gpx_list()

    def _on_gpx_point_deleted(self, index: int) -> None:
        if 0 <= index < len(self.gpx_points):
            removed = self.gpx_points.pop(index)
            self._refresh_gpx_list()
            self._log(f"🗑️ 已刪除 GPX 座標點 ({removed[0]:.6f}, {removed[1]:.6f})")

    def _gpx_update_stats(self) -> None:
        stats = gpx_tools.route_stats(self.gpx_points)
        self.gpx_stats_lbl.setText(
            f"點數: {stats.point_count}　總距離: {stats.total_distance_m:.0f}m　預估時間: {stats.estimated_time_str}"
        )

    def _gpx_parse_text(self) -> None:
        pts = gpx_tools.parse_coordinate_text(self.gpx_text_edit.toPlainText())
        self.gpx_points.extend(pts)
        self._refresh_gpx_list()
        self._log(f"📥 解析出 {len(pts)} 個座標點")

    def _gpx_import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入GPX檔案", APP_DIR, "GPX Files (*.gpx)")
        if not path:
            return
        try:
            pts = gpx_tools.read_gpx(path)
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"匯入失敗：{e}")
            return
        self.gpx_points.extend(pts)
        self._refresh_gpx_list()

    def _gpx_export_gpx(self) -> None:
        if not self.gpx_points:
            QMessageBox.warning(self, "提示", "目前沒有座標可以匯出")
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出GPX", os.path.join(APP_DIR, "route.gpx"), "GPX Files (*.gpx)")
        if path:
            gpx_tools.write_gpx(self.gpx_points, path)
            self._log(f"💾 已匯出 GPX: {path}")

    def _gpx_export_txt(self) -> None:
        if not self.gpx_points:
            QMessageBox.warning(self, "提示", "目前沒有座標可以匯出")
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出TXT", os.path.join(APP_DIR, "route.txt"), "Text Files (*.txt)")
        if path:
            gpx_tools.write_txt(self.gpx_points, path)
            self._log(f"💾 已匯出 TXT: {path}")

    def _gpx_sort(self) -> None:
        self.gpx_points = gpx_tools.nearest_neighbor_order(self.gpx_points)
        self._refresh_gpx_list()

    def _gpx_reverse(self) -> None:
        self.gpx_points.reverse()
        self._refresh_gpx_list()

    def _gpx_clear(self) -> None:
        self.gpx_points.clear()
        self._refresh_gpx_list()

    def _gpx_apply_endpoint(self) -> None:
        self.gpx_points = gpx_tools.apply_endpoint_mode(self.gpx_points, self.gpx_endpoint_combo.currentText())
        self._refresh_gpx_list()

    def _gpx_apply_offset(self) -> None:
        self.gpx_points = gpx_tools.apply_coordinate_offset(self.gpx_points, self.gpx_offset_combo.currentText())
        self._refresh_gpx_list()

    def _gpx_generate_circle(self) -> None:
        if not self.gpx_points:
            QMessageBox.warning(self, "提示", "請先用地圖右鍵「GPX工具：新增此點」設定中心點")
            return
        center = self.gpx_points[-1]
        try:
            radius = float(self.circle_radius.text())
            num_points = int(self.circle_points.text())
            num_laps = int(self.circle_laps.text())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "半徑/點數/圈數請填數字")
            return
        pts = gpx_tools.generate_circle_path(center, radius, num_points=num_points, num_laps=num_laps)
        self.gpx_points.extend(pts)
        self._refresh_gpx_list()

    def _gpx_preview(self) -> None:
        if not self.gpx_points:
            return
        self.map_view.set_path("gpx_preview", self.gpx_points, color="#9c27b0")
        mid = self.gpx_points[len(self.gpx_points) // 2]
        self.map_view.set_position(mid[0], mid[1], 16)

    def _gpx_start_patrol_with_route(self) -> None:
        if not self.gpx_points:
            QMessageBox.warning(self, "提示", "目前沒有座標")
            return
        slot = self.active_draw_device
        segments = [list(self.gpx_points)]
        self.patrol_segments[slot] = segments
        self.patrol_workers[slot].set_segments(segments)
        self.map_view.set_path(f"patrol_{slot}_0", self.gpx_points, color="#ffd700")
        self._log(f"[{slot}] 🚀 已套用 GPX 路線為巡邏路徑（{len(self.gpx_points)} 個點）")

    def _gpx_add_point_from_map(self, lat: float, lng: float) -> None:
        self.gpx_points.append((lat, lng))
        self._refresh_gpx_list()

    # ==================== 地圖右鍵選單事件 ====================
    def _on_map_context_action(self, action: str, lat: float, lng: float) -> None:
        if action == "set_location":
            slot = self.active_draw_device
            self.send_location_direct(slot, lat, lng)
            self._log(f"[{slot}] 📍 已設定定位點 ({lat:.6f}, {lng:.6f})")
        elif action == "add_polygon_vertex":
            slot = self.active_draw_device
            self.polygon_vertices[slot].append((lat, lng))
            color = "#2ecc71" if slot == "A" else "#3498db"
            self.map_view.set_path(f"poly_progress_{slot}", self.polygon_vertices[slot], color=color)
            # 每新增一個頂點都放一個標記，讓使用者看得到自己點過哪些點（不然只有一條連線，
            # 點太密集時完全分不出來到底點了幾個、點在哪）
            region_no = len(self.finished_polygons[slot]) + 1
            vertex_idx = len(self.polygon_vertices[slot])
            self.map_view.set_marker(
                f"poly_vertex_{slot}_{vertex_idx}", lat, lng, color=color, label=f"區{region_no}-P{vertex_idx}"
            )
            self._log(f"[{slot}] 📌 [區域{region_no}] 新增頂點: {lat:.6f}, {lng:.6f}")
        elif action == "gpx_add_point":
            self._gpx_add_point_from_map(lat, lng)
        elif action == "add_favorite":
            name, ok = QInputDialog.getText(
                self, "加入收藏", "請輸入這個座標的名稱：", text=f"收藏 {len(self.favorites) + 1}"
            )
            name = name.strip() if ok and name else ""
            if name:
                self._add_favorite(name, lat, lng)

    # ==================== 關閉程式 ====================
    def closeEvent(self, event) -> None:
        settings_store.save_file(USER_SETTINGS_PATH, settings_store.collect(self))
        for worker in self.patrol_workers.values():
            worker.stop()
        for engine in self.engines.values():
            engine.force_stop()
        self.tile_cache.stop()
        super().closeEvent(event)
