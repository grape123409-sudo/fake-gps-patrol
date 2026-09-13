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

import json
import math
import os
import platform
import sys
import threading
import time
import urllib.parse
import urllib.request
from shutil import which
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox, QRadioButton, QButtonGroup,
    QGroupBox, QScrollArea, QToolBar, QPlainTextEdit, QListWidget, QListWidgetItem,
    QMessageBox, QFileDialog, QSlider, QInputDialog, QTabWidget, QMenu, QAbstractItemView,
    QApplication,
)

from gps_core import GpsEngine, AndroidDeviceScanner, IosDeviceScanner, resolve_app_dir
import favorites_store
import gpx_tools
import group_client
import history_store
import region_store
import routes_store
import settings_store
import speed_presets_store
from joystick_widget import JoystickWidget
from map_view import MapView
from tile_cache_server import TileCacheServer
from workers import PatrolWorker

APP_DIR = resolve_app_dir()
USER_SETTINGS_PATH = os.path.join(APP_DIR, "user_settings.json")

APP_VERSION = "2.0.0"
# 「檢查更新」去看這個 GitHub 倉庫最新的 Release 版本號。之後如果換倉庫，改這一行就好。
UPDATE_CHECK_REPO = "grape123409-sudo/fake-gps-patrol"


def _version_tuple(version: str) -> tuple[int, ...]:
    """把 "2.0.1" 這種版本號轉成可以比大小的 tuple；看不懂的片段一律當 0，
    避免 GitHub 上的 tag 命名跟預期不同時直接炸掉（頂多是判斷成不用更新）。"""
    parts = []
    for chunk in version.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


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
    updateCheckFinished = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎯 雙系統定位神盾 Pro（精簡版） (PySide6)")
        self.resize(1400, 900)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 字體大小設定是「在原本的 theme.qss 後面追加覆寫規則」來實作的，
        # 這裡先記住原始樣式表，之後每次調整字體都從這份重新疊加，
        # 不會一次比一次累積、也不會把原本的主題洗掉
        app_instance = QApplication.instance()
        self._base_stylesheet = app_instance.styleSheet() if app_instance else ""

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
        self.gpx_resume_index = 0
        self.gpx_flower_steps: list = []
        self.gpx_jump_steps: list = []
        self.joystick_enabled = False
        self.joystick_step_m = 10.0

        self.favorites: list[dict] = favorites_store.load_favorites(APP_DIR)
        self.speed_presets: list[float] = speed_presets_store.load_speed_presets(APP_DIR)
        self.saved_routes: list[dict] = routes_store.load_routes(APP_DIR)
        self.history_entries: list[dict] = history_store.load_history(APP_DIR)

        # ---------------- 團體種花 ----------------
        self.group = group_client.GroupClient(self)
        self.group_room: dict = {}
        self.group_boundaries: dict[int, tuple[int, int]] = {}   # steps 索引 -> (第幾圈, 第幾個花點)
        self.group_flower_count = 0
        self.group_ring_count = 1
        self._group_release = threading.Event()
        self._group_pending_boundary: Optional[int] = None
        self._group_follower_last_sent = 0.0
        self._group_follower_pending: Optional[tuple[float, float]] = None
        self.gpx_last_concentric_points: list[tuple[float, float]] = []
        self.gpx_last_concentric_settings: dict = {}

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
        self.updateCheckFinished.connect(self._on_update_check_finished)

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
        self._apply_font_scale()

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
        add(self.dock_group, "👥", "團體種花")
        add(self.dock_tools, "🛠️", "工具與設定")

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
        self.dock_group = self._make_dock("group", "👥 團體種花", self._build_group_panel())
        self.dock_tools = self._make_dock("tools", "🛠️ 工具與設定", self._build_tools_panel())
        self.tabifyDockWidget(self.dock_manual, self.dock_patrol)
        self.tabifyDockWidget(self.dock_patrol, self.dock_gpx)
        self.tabifyDockWidget(self.dock_gpx, self.dock_favorites)
        self.tabifyDockWidget(self.dock_favorites, self.dock_dual)
        self.tabifyDockWidget(self.dock_dual, self.dock_group)
        self.tabifyDockWidget(self.dock_group, self.dock_tools)

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

        # 手動輸入座標飛過去（也支援直接輸入地名，自動判斷後其中一種呼叫地理編碼）
        fly_box = QGroupBox("✈️ 手動輸入座標或地名")
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
        if not text:
            return
        # 先當座標解析（沿用 GPX 頁面同一套多格式判斷邏輯）；解析不出兩個數字就
        # 當地名，改呼叫 Nominatim 地理編碼，兩種輸入用同一個欄位/按鈕就好，
        # 不用另外切換模式。
        coords = gpx_tools.parse_coordinate_text(text)
        if coords:
            lat, lng = coords[0]
        else:
            result = self._geocode_place_name(text)
            if result is None:
                QMessageBox.critical(self, "錯誤", f"找不到「{text}」對應的地點，也不是有效座標格式")
                return
            lat, lng = result
            self._log(f"🔍 地名「{text}」搜尋結果：({lat:.6f}, {lng:.6f})")
        target = self.manual_fly_target.currentText()
        slots = ["A", "B"] if target == "A+B" else [target]
        for slot in slots:
            self.send_location_direct(slot, lat, lng)
        self.map_view.set_position(lat, lng, 17)

    def _geocode_place_name(self, query: str) -> Optional[tuple[float, float]]:
        """呼叫 OpenStreetMap Nominatim 免費地理編碼服務，把地名轉成座標。
        免費公開服務規範要求帶合規的 User-Agent、且每秒最多一次請求——這裡只是
        使用者手動按一次按鈕才觸發一次請求，天然符合限制，不需要額外做節流。"""
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": query, "format": "json", "limit": 1,
        })
        req = urllib.request.Request(url, headers={"User-Agent": "FakeGpsPatrol/2.0 (personal use)"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self._log(f"⚠️ 地名搜尋失敗：{e}")
            return None
        if not data:
            return None
        try:
            return float(data[0]["lat"]), float(data[0]["lon"])
        except (KeyError, ValueError, TypeError):
            return None

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
        lng = gpx_tools.normalize_lng(lng + (dist * math.cos(angle)) / (100000.0 * math.cos(math.radians(lat))))
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
            lng = gpx_tools.normalize_lng(lng - step / (100000.0 * math.cos(math.radians(lat))))
        elif key == Qt.Key.Key_D:
            lng = gpx_tools.normalize_lng(lng + step / (100000.0 * math.cos(math.radians(lat))))
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
            # 用 lng_delta 而不是直接相減：多邊形如果跨過 ±180 換日線，直接相減會把
            # 對面的頂點算成繞地球一圈那麼遠，整個掃描範圍會爆掉
            x = gpx_tools.lng_delta(lng0, lng) * 100000.0 * math.cos(math.radians(lat0))
            return x, y

        def to_coords(x, y):
            lat = lat0 + (y / 111000.0)
            lng = gpx_tools.normalize_lng(lng0 + (x / (100000.0 * math.cos(math.radians(lat0)))))
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

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("分類篩選"))
        self.favorites_category_filter = QComboBox()
        self.favorites_category_filter.currentTextChanged.connect(lambda _t: self._refresh_favorites_list())
        filter_row.addWidget(self.favorites_category_filter, stretch=1)
        manage_cat_btn = QPushButton("🗂️ 分類管理")
        manage_cat_btn.clicked.connect(lambda: self._manage_categories_for(
            self.favorites, lambda: favorites_store.save_favorites(APP_DIR, self.favorites),
            self._refresh_favorites_list, "收藏"
        ))
        filter_row.addWidget(manage_cat_btn)
        layout.addLayout(filter_row)

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

        history_box = QGroupBox("🕘 執行歷史")
        history_layout = QVBoxLayout(history_box)
        hist_filter_row = QHBoxLayout()
        hist_filter_row.addWidget(QLabel("類型"))
        self.history_type_filter = QComboBox()
        self.history_type_filter.addItems(["全部", "manual", "flower", "jump", "concentric", "favorite"])
        self.history_type_filter.currentTextChanged.connect(lambda _t: self._refresh_history_list())
        hist_filter_row.addWidget(self.history_type_filter)
        self.history_search_entry = QLineEdit()
        self.history_search_entry.setPlaceholderText("搜尋名稱...")
        self.history_search_entry.textChanged.connect(lambda _t: self._refresh_history_list())
        hist_filter_row.addWidget(self.history_search_entry, stretch=1)
        history_layout.addLayout(hist_filter_row)

        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(150)
        self.history_list.itemDoubleClicked.connect(lambda _item: self._history_fly_to_selected())
        history_layout.addWidget(self.history_list)

        hist_btn_row = QHBoxLayout()
        hist_fly_btn = QPushButton("🚀 飛過去")
        hist_fly_btn.clicked.connect(self._history_fly_to_selected)
        hist_clear_btn = QPushButton("🧹 清空全部")
        hist_clear_btn.setObjectName("DangerButton")
        hist_clear_btn.clicked.connect(self._history_clear_all)
        hist_btn_row.addWidget(hist_fly_btn)
        hist_btn_row.addWidget(hist_clear_btn)
        history_layout.addLayout(hist_btn_row)

        layout.addWidget(history_box)
        self._refresh_history_list()
        return root

    def _refresh_category_filter_combo(self, combo: QComboBox, items: list[dict]) -> None:
        """收藏夾／已存路線共用：把 combo 重建成「全部」+ 目前存在的所有分類，
        並盡量保留使用者原本選到的那個分類（找不到就退回「全部」）。"""
        current = combo.currentText() or "全部"
        categories = {it.get("category", "未分類") for it in items}
        categories.add("未分類")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("全部")
        for c in sorted(categories):
            combo.addItem(c)
        idx = combo.findText(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _manage_categories_for(self, items: list[dict], save_fn, refresh_fn, log_prefix: str) -> None:
        """收藏夾／已存路線共用的分類管理：選一個既有分類，重新命名或刪除
        （刪除＝該分類底下所有項目退回「未分類」，不刪除資料本身）。"""
        categories = sorted({it.get("category", "未分類") for it in items if it.get("category", "未分類") != "未分類"})
        if not categories:
            QMessageBox.information(self, "分類管理", "目前沒有自訂分類（除了「未分類」以外）")
            return
        cat, ok = QInputDialog.getItem(self, "分類管理", "選擇要處理的分類：", categories, 0, False)
        if not ok or not cat:
            return
        action, ok = QInputDialog.getItem(
            self, "分類管理", f"對分類「{cat}」要做什麼？", ["重新命名", "刪除（項目退回未分類）"], 0, False
        )
        if not ok:
            return
        if action == "重新命名":
            new_name, ok2 = QInputDialog.getText(self, "重新命名分類", "新的分類名稱：", text=cat)
            new_name = new_name.strip() if ok2 and new_name else ""
            if not new_name:
                return
            for it in items:
                if it.get("category", "未分類") == cat:
                    it["category"] = new_name
            save_fn()
            refresh_fn()
            self._log(f"🗂️ {log_prefix}分類「{cat}」已重新命名為「{new_name}」")
        else:
            for it in items:
                if it.get("category", "未分類") == cat:
                    it["category"] = "未分類"
            save_fn()
            refresh_fn()
            self._log(f"🗂️ {log_prefix}分類「{cat}」已刪除，項目已退回未分類")

    def _refresh_favorites_list(self) -> None:
        self._refresh_category_filter_combo(self.favorites_category_filter, self.favorites)
        selected_cat = self.favorites_category_filter.currentText()
        self.favorites_list.clear()
        self._favorites_list_row_map: list[int] = []
        for idx, fav in enumerate(self.favorites):
            cat = fav.get("category", "未分類")
            if selected_cat not in ("全部", "") and cat != selected_cat:
                continue
            self.favorites_list.addItem(f"[{cat}] {fav['name']}　({fav['lat']:.6f}, {fav['lng']:.6f})")
            self._favorites_list_row_map.append(idx)

    def _prompt_favorite_details(self, default_name: str) -> Optional[tuple[str, str]]:
        name, ok = QInputDialog.getText(self, "加入收藏", "請輸入這個座標的名稱：", text=default_name)
        name = name.strip() if ok and name else ""
        if not name:
            return None
        category, ok2 = QInputDialog.getText(self, "加入收藏", "分類（留空 = 未分類）：", text="未分類")
        category = category.strip() if ok2 and category else "未分類"
        return name, category

    def _add_favorite(self, name: str, lat: float, lng: float, category: str = "未分類") -> None:
        self.favorites.append({"name": name, "lat": lat, "lng": lng, "category": category or "未分類"})
        favorites_store.save_favorites(APP_DIR, self.favorites)
        self._refresh_favorites_list()
        self._log(f"⭐ 已加入收藏「{name}」({lat:.6f}, {lng:.6f})　分類：{category or '未分類'}")

    def _add_current_position_to_favorites(self) -> None:
        slot = self.active_draw_device
        lat, lng = self.curr_pos[slot]
        details = self._prompt_favorite_details(f"收藏 {len(self.favorites) + 1}")
        if details:
            name, category = details
            self._add_favorite(name, lat, lng, category)

    def _fly_to_selected_favorite(self) -> None:
        row = self.favorites_list.currentRow()
        if row < 0 or row >= len(self._favorites_list_row_map):
            QMessageBox.warning(self, "提示", "請先選擇一筆收藏")
            return
        fav = self.favorites[self._favorites_list_row_map[row]]
        slot = self.active_draw_device
        self.send_location_direct(slot, fav["lat"], fav["lng"])
        self.map_view.set_position(fav["lat"], fav["lng"], 17)
        self._log(f"[{slot}] 🚀 已飛往收藏「{fav['name']}」")
        self._history_add(fav["name"], "favorite", fav["lat"], fav["lng"])

    def _delete_selected_favorite(self) -> None:
        row = self.favorites_list.currentRow()
        if row < 0 or row >= len(self._favorites_list_row_map):
            QMessageBox.warning(self, "提示", "請先選擇一筆收藏")
            return
        removed = self.favorites.pop(self._favorites_list_row_map[row])
        favorites_store.save_favorites(APP_DIR, self.favorites)
        self._refresh_favorites_list()
        self._log(f"🗑️ 已刪除收藏「{removed['name']}」")

    # ==================== 執行歷史 ====================
    def _history_add(self, name: str, kind: str, lat: Optional[float] = None, lng: Optional[float] = None) -> None:
        entry = {"name": name, "kind": kind, "lat": lat, "lng": lng, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.history_entries.insert(0, entry)
        del self.history_entries[200:]
        history_store.save_history(APP_DIR, self.history_entries)
        if hasattr(self, "history_list"):
            self._refresh_history_list()

    def _refresh_history_list(self) -> None:
        self.history_list.clear()
        self._history_list_row_map: list[int] = []
        type_filter = self.history_type_filter.currentText()
        search = self.history_search_entry.text().strip().lower()
        for idx, h in enumerate(self.history_entries):
            if type_filter != "全部" and h.get("kind") != type_filter:
                continue
            if search and search not in h.get("name", "").lower():
                continue
            self.history_list.addItem(f"[{h.get('timestamp', '')}] ({h.get('kind', '')}) {h.get('name', '')}")
            self._history_list_row_map.append(idx)

    def _history_fly_to_selected(self) -> None:
        row = self.history_list.currentRow()
        if row < 0 or row >= len(self._history_list_row_map):
            return
        h = self.history_entries[self._history_list_row_map[row]]
        if h.get("lat") is not None and h.get("lng") is not None:
            self.map_view.set_position(h["lat"], h["lng"], 17)

    def _history_clear_all(self) -> None:
        if not self.history_entries:
            return
        self.history_entries.clear()
        history_store.save_history(APP_DIR, self.history_entries)
        self._refresh_history_list()
        self._log("🧹 已清空執行歷史")

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
        tabs = QTabWidget()
        tabs.addTab(self._build_gpx_general_tab(), "一般路線")
        tabs.addTab(self._build_gpx_flower_tab(), "🌸種花路徑")
        tabs.addTab(self._build_gpx_jump_tab(), "🦘跳躍路徑")
        tabs.addTab(self._build_gpx_concentric_tab(), "⭕同心圓")
        tabs.addTab(self._build_gpx_routes_tab(), "📁已存路線")
        layout.addWidget(tabs)
        return root

    def _build_gpx_general_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("座標文字（每行一組，支援多種格式與 Plus Code）"))
        self.gpx_text_edit = QPlainTextEdit()
        self.gpx_text_edit.setMaximumHeight(100)
        layout.addWidget(self.gpx_text_edit)

        parse_btn = QPushButton("📥 解析文字座標")
        parse_btn.clicked.connect(self._gpx_parse_text)
        layout.addWidget(parse_btn)

        self.gpx_list = QListWidget()
        self.gpx_list.setMaximumHeight(160)
        self.gpx_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        # QListWidget 的 InternalMove 在不同 Qt 版本上，有的是發 rowsMoved、有的是
        # 「先插入新的再移除舊的」只發 rowsInserted/rowsRemoved，只接 rowsMoved 的話
        # 拖曳完可能完全不會同步。兩個都接，配合 _gpx_list_rebuilding 旗標避開我們
        # 自己重建清單時的雜訊，兩種實作都能正確同步。
        self.gpx_list.model().rowsMoved.connect(self._gpx_list_reordered)
        self.gpx_list.model().rowsRemoved.connect(self._gpx_list_reordered)
        self.gpx_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gpx_list.customContextMenuRequested.connect(self._gpx_show_context_menu)
        layout.addWidget(self.gpx_list)

        node_row = QHBoxLayout()
        fly_btn = QPushButton("🚀飛到")
        fly_btn.clicked.connect(self._gpx_fly_to_selected)
        resume_btn = QPushButton("▶️續跑起點")
        resume_btn.clicked.connect(self._gpx_set_resume_start)
        fav_btn = QPushButton("⭐存收藏")
        fav_btn.clicked.connect(self._gpx_save_selected_as_favorite)
        copy_btn = QPushButton("📋複製")
        copy_btn.clicked.connect(self._gpx_copy_selected)
        del_btn = QPushButton("🗑️刪除")
        del_btn.clicked.connect(self._gpx_delete_selected)
        for b in (fly_btn, resume_btn, fav_btn, copy_btn, del_btn):
            node_row.addWidget(b)
        layout.addLayout(node_row)

        self.gpx_resume_lbl = QLabel("續跑起點：從頭開始")
        self.gpx_resume_lbl.setProperty("role", "hint")
        layout.addWidget(self.gpx_resume_lbl)

        file_row = QHBoxLayout()
        import_btn = QPushButton("📂 匯入檔案(GPX/CSV/JSON)")
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
        sort_btn = QPushButton("🧭 最短路徑排序")
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

        self.gpx_stats_lbl = QLabel("尚無座標")
        self.gpx_stats_lbl.setProperty("role", "hint")
        layout.addWidget(self.gpx_stats_lbl)

        preview_btn = QPushButton("🗺️ 在地圖上預覽")
        preview_btn.clicked.connect(self._gpx_preview)
        layout.addWidget(preview_btn)

        run_box, self.gpx_run_mode_combo, self.gpx_repeat_count_entry = self._build_run_mode_box("gpx")
        layout.addWidget(run_box)

        use_btn = QPushButton("🚀 用這條路線開始巡邏（套用到目前繪製對象）")
        use_btn.clicked.connect(self._gpx_start_patrol_with_route)
        layout.addWidget(use_btn)

        save_route_btn = QPushButton("💾 存為已存路線")
        save_route_btn.clicked.connect(self._gpx_save_general_as_route)
        layout.addWidget(save_route_btn)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _build_run_mode_box(self, object_prefix: str) -> tuple[QGroupBox, QComboBox, QLineEdit]:
        """一般路線／種花路徑／跳躍路徑三個分頁共用的「執行模式」小面板：跑一次／往返
        折返／持續循環 + 執行次數。這個模式只套用在按下各自「開始」按鈕送出的路線，
        不影響 Z字巡邏(dock_patrol)的行為（Z字巡邏呼叫 PatrolWorker.start() 時不帶這兩個
        參數，永遠是 once/1）。"""
        box = QGroupBox("執行模式")
        row = QHBoxLayout(box)
        combo = QComboBox()
        combo.addItem("跑一次", "once")
        combo.addItem("往返折返", "reverse")
        combo.addItem("持續循環", "loop")
        combo.setObjectName(f"{object_prefix}_run_mode_combo")
        row.addWidget(QLabel("模式"))
        row.addWidget(combo)
        entry = QLineEdit("1")
        entry.setObjectName(f"{object_prefix}_repeat_count_entry")
        row.addWidget(QLabel("執行次數(1-999)"))
        row.addWidget(entry)
        return box, combo, entry

    def _clamped_float(self, text: str, lo: float, hi: float, default: float) -> float:
        try:
            value = float(text)
        except ValueError:
            value = default
        return max(lo, min(hi, value))

    # GPX 點在地圖上畫成可拖曳/可刪除的數字標記；點數太多時（例如匯入了很大的
    # GPX 檔）畫出上千個可拖曳標記會讓地圖變得很卡，超過這個數量就不畫地圖標記，
    # 但清單本身還是照常可以編輯，不受影響
    _MAX_GPX_MAP_MARKERS = 300

    def _refresh_gpx_list(self) -> None:
        self._gpx_list_rebuilding = True
        try:
            self.gpx_list.clear()
            for lat, lng in self.gpx_points:
                item = QListWidgetItem(f"{lat:.6f}, {lng:.6f}")
                item.setData(Qt.ItemDataRole.UserRole, (lat, lng))
                self.gpx_list.addItem(item)
        finally:
            self._gpx_list_rebuilding = False
        self._gpx_update_stats()
        if len(self.gpx_points) <= self._MAX_GPX_MAP_MARKERS:
            self.map_view.set_gpx_points(self.gpx_points)
        else:
            self.map_view.clear_gpx_points()
        if self.gpx_resume_index >= len(self.gpx_points):
            self.gpx_resume_index = 0
            self.gpx_resume_lbl.setText("續跑起點：從頭開始")

    def _gpx_list_reordered(self, *_args) -> None:
        """拖曳清單重新排序後，把 self.gpx_points 同步成清單目前的視覺順序
        （每個 QListWidgetItem 在 _refresh_gpx_list 時都用 UserRole 存了自己的座標，
        拖曳只會搬動 item，不會自動更新 self.gpx_points，要手動同步回去）。

        點數對不上時（例如清單重建到一半）直接不動作，避免把資料弄壞。"""
        if getattr(self, "_gpx_list_rebuilding", False):
            return
        reordered: list[tuple[float, float]] = []
        for row in range(self.gpx_list.count()):
            data = self.gpx_list.item(row).data(Qt.ItemDataRole.UserRole)
            if data is not None:
                reordered.append((data[0], data[1]))
        if len(reordered) == len(self.gpx_points):
            self.gpx_points = reordered
            self.gpx_resume_index = 0
            self.gpx_resume_lbl.setText("續跑起點：從頭開始")
            self._gpx_update_stats()
            if len(self.gpx_points) <= self._MAX_GPX_MAP_MARKERS:
                self.map_view.set_gpx_points(self.gpx_points)
            self._log("🔀 已依拖曳順序重新排列座標點")

    def _gpx_show_context_menu(self, pos) -> None:
        if not self.gpx_points:
            return
        menu = QMenu(self)
        optimize_action = menu.addAction("🧭 優化路線（最短路徑）")
        action = menu.exec(self.gpx_list.mapToGlobal(pos))
        if action == optimize_action:
            self._gpx_sort()

    def _gpx_selected_index(self) -> int:
        return self.gpx_list.currentRow()

    def _gpx_fly_to_selected(self) -> None:
        idx = self._gpx_selected_index()
        if not (0 <= idx < len(self.gpx_points)):
            QMessageBox.warning(self, "提示", "請先在清單中選一個點")
            return
        lat, lng = self.gpx_points[idx]
        self.map_view.set_position(lat, lng, 17)

    def _gpx_set_resume_start(self) -> None:
        idx = self._gpx_selected_index()
        if not (0 <= idx < len(self.gpx_points)):
            QMessageBox.warning(self, "提示", "請先在清單中選一個點")
            return
        self.gpx_resume_index = idx
        lat, lng = self.gpx_points[idx]
        self.gpx_resume_lbl.setText(f"續跑起點：第 {idx + 1} 點 ({lat:.6f}, {lng:.6f})")

    def _gpx_save_selected_as_favorite(self) -> None:
        idx = self._gpx_selected_index()
        if not (0 <= idx < len(self.gpx_points)):
            QMessageBox.warning(self, "提示", "請先在清單中選一個點")
            return
        lat, lng = self.gpx_points[idx]
        details = self._prompt_favorite_details(f"收藏 {len(self.favorites) + 1}")
        if details:
            name, category = details
            self._add_favorite(name, lat, lng, category)

    def _gpx_copy_selected(self) -> None:
        idx = self._gpx_selected_index()
        if not (0 <= idx < len(self.gpx_points)):
            QMessageBox.warning(self, "提示", "請先在清單中選一個點")
            return
        lat, lng = self.gpx_points[idx]
        QApplication.clipboard().setText(f"{lat:.6f}, {lng:.6f}")
        self._log(f"📋 已複製座標 ({lat:.6f}, {lng:.6f})")

    def _gpx_delete_selected(self) -> None:
        idx = self._gpx_selected_index()
        if not (0 <= idx < len(self.gpx_points)):
            QMessageBox.warning(self, "提示", "請先在清單中選一個點")
            return
        removed = self.gpx_points.pop(idx)
        if self.gpx_resume_index > idx:
            self.gpx_resume_index -= 1
        self._refresh_gpx_list()
        self._log(f"🗑️ 已刪除 GPX 座標點 ({removed[0]:.6f}, {removed[1]:.6f})")

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
        path, _ = QFileDialog.getOpenFileName(
            self, "匯入路徑檔案", APP_DIR,
            "路徑檔案 (*.gpx *.csv *.json);;GPX Files (*.gpx);;CSV Files (*.csv);;JSON Files (*.json)"
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".csv":
                pts = gpx_tools.read_csv(path)
            elif ext == ".json":
                pts = gpx_tools.read_json(path)
            else:
                pts = gpx_tools.read_gpx(path)
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"匯入失敗：{e}")
            return
        if not pts:
            QMessageBox.warning(self, "提示", "檔案裡沒有找到有效座標")
            return
        self.gpx_points.extend(pts)
        self._refresh_gpx_list()
        self._log(f"📂 已匯入 {len(pts)} 個座標點（{path}）")

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
        self.gpx_points = gpx_tools.optimize_route(self.gpx_points)
        self._refresh_gpx_list()
        self._log(f"🧭 已依最短路徑重新排序（{len(self.gpx_points)} 點）")

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
        points = self.gpx_points[self.gpx_resume_index:] if self.gpx_resume_index else list(self.gpx_points)
        if not points:
            QMessageBox.warning(self, "提示", "續跑起點超出範圍，請重新設定")
            return
        segments = [points]
        self.patrol_segments[slot] = segments
        self.patrol_workers[slot].set_segments(segments)
        self.map_view.set_path(f"patrol_{slot}_0", points, color="#ffd700")
        try:
            speed = float(self.patrol_speed_entry[slot].currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "時速必須為數字！（請到「巡邏設定」分頁設定時速）")
            return
        repeat_count = int(self._clamped_float(self.gpx_repeat_count_entry.text(), 1, 999, 1))
        run_mode = self.gpx_run_mode_combo.currentData()
        self.patrol_workers[slot].start(speed, run_mode=run_mode, repeat_count=repeat_count)
        self.patrol_progress_lbl[slot].setText("巡邏中...")
        self._log(
            f"[{slot}] 🚀 已套用 GPX 路線並開始巡邏（{len(points)} 個點，"
            f"模式：{self.gpx_run_mode_combo.currentText()}）"
        )
        self._history_add(f"一般路線（{len(points)}點）", "manual", points[0][0], points[0][1])

    def _gpx_save_general_as_route(self) -> None:
        if not self.gpx_points:
            QMessageBox.warning(self, "提示", "目前沒有座標")
            return
        points = [[p[0], p[1]] for p in self.gpx_points]
        run_mode = self.gpx_run_mode_combo.currentData()
        repeat_count = int(self._clamped_float(self.gpx_repeat_count_entry.text(), 1, 999, 1))
        try:
            speed = float(self.patrol_speed_entry[self.active_draw_device].currentText())
        except ValueError:
            speed = 19.0
        self._save_route_entry("manual", "points", points, run_mode, repeat_count, speed, {})

    def _gpx_add_point_from_map(self, lat: float, lng: float) -> None:
        self.gpx_points.append((lat, lng))
        self._refresh_gpx_list()

    # ==================== 種花路徑生成 ====================
    def _build_ring_settings_box(
        self, title: str, prefix: str,
        default_radius: float, default_turns: float, default_arrival: float, default_departure: float,
    ) -> tuple[QGroupBox, dict]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        radius_entry = QLineEdit(str(default_radius))
        radius_entry.setObjectName(f"flower_{prefix}_radius")
        turns_entry = QLineEdit(str(default_turns))
        turns_entry.setObjectName(f"flower_{prefix}_turns")
        arrival_entry = QLineEdit(str(default_arrival))
        arrival_entry.setObjectName(f"flower_{prefix}_arrival_wait")
        departure_entry = QLineEdit(str(default_departure))
        departure_entry.setObjectName(f"flower_{prefix}_departure_wait")
        form.addRow("半徑(m) 5-100", radius_entry)
        form.addRow("圈數 0.05-10", turns_entry)
        form.addRow("抵達後等待(s) 0-300", arrival_entry)
        form.addRow("出發前等待(s) 0-300", departure_entry)
        return box, {
            "radius": radius_entry, "turns": turns_entry,
            "arrival_wait": arrival_entry, "departure_wait": departure_entry,
        }

    def _gpx_flower_ring_settings(self, fields: dict) -> dict:
        return {
            "radius": self._clamped_float(fields["radius"].text(), 5.0, 100.0, 28.0),
            "turns": self._clamped_float(fields["turns"].text(), 0.05, 10.0, 1.0),
            "arrival_wait": self._clamped_float(fields["arrival_wait"].text(), 0.0, 300.0, 1.0),
            "departure_wait": self._clamped_float(fields["departure_wait"].text(), 0.0, 300.0, 3.0),
        }

    def _build_gpx_flower_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("花點座標（每行一組，可到「一般路線」分頁用地圖右鍵新增後複製過來）"))
        self.flower_centers_text = QPlainTextEdit()
        self.flower_centers_text.setMaximumHeight(90)
        layout.addWidget(self.flower_centers_text)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("花點順序"))
        self.flower_sort_combo = QComboBox()
        self.flower_sort_combo.addItem("貼上原順序", "paste")
        self.flower_sort_combo.addItem("最短路徑", "shortest")
        self.flower_sort_combo.setObjectName("flower_sort_combo")
        opt_row.addWidget(self.flower_sort_combo)
        opt_row.addWidget(QLabel("花點間移動"))
        self.flower_plant_combo = QComboBox()
        self.flower_plant_combo.addItem("瞬移", "teleport")
        self.flower_plant_combo.addItem("直線走過去", "walk")
        self.flower_plant_combo.setObjectName("flower_plant_combo")
        opt_row.addWidget(self.flower_plant_combo)
        layout.addLayout(opt_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("團體速度(km/h)"))
        self.flower_speed_combo = QComboBox()
        self.flower_speed_combo.setEditable(True)
        self.flower_speed_combo.setObjectName("flower_speed_combo")
        preset_texts = [str(v) for v in self.speed_presets]
        if "19" not in preset_texts:
            preset_texts.append("19")
        self.flower_speed_combo.addItems(preset_texts)
        self.flower_speed_combo.setCurrentText("19")
        speed_row.addWidget(self.flower_speed_combo)
        layout.addLayout(speed_row)

        ring1_box, self.flower_ring1_fields = self._build_ring_settings_box(
            "第一圈", "ring1", default_radius=28, default_turns=1, default_arrival=1, default_departure=3
        )
        layout.addWidget(ring1_box)

        self.flower_ring2_enable = QCheckBox("啟用第二圈")
        self.flower_ring2_enable.setObjectName("flower_ring2_enable")
        layout.addWidget(self.flower_ring2_enable)
        ring2_box, self.flower_ring2_fields = self._build_ring_settings_box(
            "第二圈", "ring2", default_radius=24, default_turns=1, default_arrival=1, default_departure=3
        )
        layout.addWidget(ring2_box)

        gen_btn = QPushButton("🌸 產生種花路徑")
        gen_btn.clicked.connect(self._gpx_flower_generate)
        layout.addWidget(gen_btn)

        self.flower_stats_lbl = QLabel("尚未產生路線")
        self.flower_stats_lbl.setProperty("role", "hint")
        layout.addWidget(self.flower_stats_lbl)

        preview_btn = QPushButton("🗺️ 在地圖上預覽")
        preview_btn.clicked.connect(self._gpx_flower_preview)
        layout.addWidget(preview_btn)

        run_box, self.flower_run_mode_combo, self.flower_repeat_count_entry = self._build_run_mode_box("flower")
        layout.addWidget(run_box)

        start_btn = QPushButton("🚀 開始這條路線（套用到目前繪製對象）")
        start_btn.clicked.connect(self._gpx_flower_start)
        layout.addWidget(start_btn)

        save_route_btn = QPushButton("💾 存為已存路線")
        save_route_btn.clicked.connect(self._gpx_save_flower_as_route)
        layout.addWidget(save_route_btn)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _gpx_flower_generate(self) -> None:
        centers = gpx_tools.parse_coordinate_text(self.flower_centers_text.toPlainText())
        if not centers:
            QMessageBox.warning(self, "提示", "請先輸入至少一個花點座標")
            return
        try:
            speed = float(self.flower_speed_combo.currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "團體速度必須為數字")
            return
        sort_mode = self.flower_sort_combo.currentData()
        plant_mode = self.flower_plant_combo.currentData()
        ring1 = self._gpx_flower_ring_settings(self.flower_ring1_fields)
        ring2 = self._gpx_flower_ring_settings(self.flower_ring2_fields) if self.flower_ring2_enable.isChecked() else None
        self.gpx_flower_steps = gpx_tools.build_flower_route(centers, sort_mode, plant_mode, ring1, ring2, speed)
        if not self.gpx_flower_steps:
            QMessageBox.warning(self, "提示", "路線產生失敗（無有效步驟）")
            return
        total_wait = sum(s.wait_s for s in self.gpx_flower_steps)
        self.flower_stats_lbl.setText(
            f"共 {len(centers)} 個花點，{len(self.gpx_flower_steps)} 個路徑點，總等待 {total_wait:.0f}s"
        )
        self._log(f"🌸 已產生種花路徑：{len(centers)} 個花點、{len(self.gpx_flower_steps)} 個路徑點")

    def _gpx_flower_preview(self) -> None:
        if not self.gpx_flower_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        pts = [(s.lat, s.lng) for s in self.gpx_flower_steps]
        self.map_view.set_path("gpx_flower_preview", pts, color="#e91e63")
        mid = pts[len(pts) // 2]
        self.map_view.set_position(mid[0], mid[1], 16)

    def _gpx_flower_start(self) -> None:
        if not self.gpx_flower_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        slot = self.active_draw_device
        try:
            speed = float(self.flower_speed_combo.currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "團體速度必須為數字")
            return
        run_mode = self.flower_run_mode_combo.currentData()
        repeat_count = int(self._clamped_float(self.flower_repeat_count_entry.text(), 1, 999, 1))
        self.patrol_workers[slot].start_timed_steps(self.gpx_flower_steps, speed, run_mode=run_mode, repeat_count=repeat_count)
        self.patrol_progress_lbl[slot].setText("巡邏中...")
        self._log(f"[{slot}] 🌸 已開始種花路徑（{len(self.gpx_flower_steps)} 個路徑點）")
        self._history_add("種花路徑", "flower", self.gpx_flower_steps[0].lat, self.gpx_flower_steps[0].lng)

    def _gpx_save_flower_as_route(self) -> None:
        if not self.gpx_flower_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        points = [[s.lat, s.lng, s.wait_s] for s in self.gpx_flower_steps]
        run_mode = self.flower_run_mode_combo.currentData()
        repeat_count = int(self._clamped_float(self.flower_repeat_count_entry.text(), 1, 999, 1))
        try:
            speed = float(self.flower_speed_combo.currentText())
        except ValueError:
            speed = 19.0
        settings = {
            "centers_text": self.flower_centers_text.toPlainText(),
            "sort_mode": self.flower_sort_combo.currentData(),
            "plant_mode": self.flower_plant_combo.currentData(),
            "ring1": self._gpx_flower_ring_settings(self.flower_ring1_fields),
            "ring2": self._gpx_flower_ring_settings(self.flower_ring2_fields) if self.flower_ring2_enable.isChecked() else None,
        }
        self._save_route_entry("flower", "steps", points, run_mode, repeat_count, speed, settings)

    # ==================== 跳躍路徑生成 ====================
    def _build_gpx_jump_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("跳躍座標（每行一組）"))
        self.jump_points_text = QPlainTextEdit()
        self.jump_points_text.setMaximumHeight(100)
        layout.addWidget(self.jump_points_text)

        form = QFormLayout()
        self.jump_before_wait_entry = QLineEdit("1")
        self.jump_before_wait_entry.setObjectName("jump_before_wait_entry")
        self.jump_after_wait_entry = QLineEdit("1")
        self.jump_after_wait_entry.setObjectName("jump_after_wait_entry")
        self.jump_forward_m_entry = QLineEdit("0")
        self.jump_forward_m_entry.setObjectName("jump_forward_m_entry")
        self.jump_speed_entry = QLineEdit("19")
        self.jump_speed_entry.setObjectName("jump_speed_entry")
        form.addRow("跳躍前等待(s)", self.jump_before_wait_entry)
        form.addRow("到達後等待(s)", self.jump_after_wait_entry)
        form.addRow("跳躍後走路公尺數", self.jump_forward_m_entry)
        form.addRow("走路速度(km/h)", self.jump_speed_entry)
        layout.addLayout(form)

        gen_btn = QPushButton("🦘 產生跳躍路徑")
        gen_btn.clicked.connect(self._gpx_jump_generate)
        layout.addWidget(gen_btn)

        self.jump_stats_lbl = QLabel("尚未產生路線")
        self.jump_stats_lbl.setProperty("role", "hint")
        layout.addWidget(self.jump_stats_lbl)

        preview_btn = QPushButton("🗺️ 在地圖上預覽")
        preview_btn.clicked.connect(self._gpx_jump_preview)
        layout.addWidget(preview_btn)

        run_box, self.jump_run_mode_combo, self.jump_repeat_count_entry = self._build_run_mode_box("jump")
        layout.addWidget(run_box)

        start_btn = QPushButton("🚀 開始這條路線（套用到目前繪製對象）")
        start_btn.clicked.connect(self._gpx_jump_start)
        layout.addWidget(start_btn)

        save_route_btn = QPushButton("💾 存為已存路線")
        save_route_btn.clicked.connect(self._gpx_save_jump_as_route)
        layout.addWidget(save_route_btn)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _gpx_jump_generate(self) -> None:
        points = gpx_tools.parse_coordinate_text(self.jump_points_text.toPlainText())
        if not points:
            QMessageBox.warning(self, "提示", "請先輸入至少一個跳躍座標")
            return
        try:
            before_wait = float(self.jump_before_wait_entry.text())
            after_wait = float(self.jump_after_wait_entry.text())
            forward_m = float(self.jump_forward_m_entry.text())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "等待秒數/走路公尺數請填數字")
            return
        self.gpx_jump_steps = gpx_tools.build_jump_route(points, before_wait, after_wait, forward_m)
        if not self.gpx_jump_steps:
            QMessageBox.warning(self, "提示", "路線產生失敗（無有效步驟）")
            return
        self.jump_stats_lbl.setText(f"共 {len(points)} 個跳躍點，{len(self.gpx_jump_steps)} 個路徑點")
        self._log(f"🦘 已產生跳躍路徑：{len(points)} 個跳躍點、{len(self.gpx_jump_steps)} 個路徑點")

    def _gpx_jump_preview(self) -> None:
        if not self.gpx_jump_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        pts = [(s.lat, s.lng) for s in self.gpx_jump_steps]
        self.map_view.set_path("gpx_jump_preview", pts, color="#ff9800")
        mid = pts[len(pts) // 2]
        self.map_view.set_position(mid[0], mid[1], 16)

    def _gpx_jump_start(self) -> None:
        if not self.gpx_jump_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        slot = self.active_draw_device
        try:
            speed = float(self.jump_speed_entry.text())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "走路速度必須為數字")
            return
        run_mode = self.jump_run_mode_combo.currentData()
        repeat_count = int(self._clamped_float(self.jump_repeat_count_entry.text(), 1, 999, 1))
        self.patrol_workers[slot].start_timed_steps(self.gpx_jump_steps, speed, run_mode=run_mode, repeat_count=repeat_count)
        self.patrol_progress_lbl[slot].setText("巡邏中...")
        self._log(f"[{slot}] 🦘 已開始跳躍路徑（{len(self.gpx_jump_steps)} 個路徑點）")
        self._history_add("跳躍路徑", "jump", self.gpx_jump_steps[0].lat, self.gpx_jump_steps[0].lng)

    def _gpx_save_jump_as_route(self) -> None:
        if not self.gpx_jump_steps:
            QMessageBox.warning(self, "提示", "請先產生路線")
            return
        points = [[s.lat, s.lng, s.wait_s] for s in self.gpx_jump_steps]
        run_mode = self.jump_run_mode_combo.currentData()
        repeat_count = int(self._clamped_float(self.jump_repeat_count_entry.text(), 1, 999, 1))
        try:
            speed = float(self.jump_speed_entry.text())
        except ValueError:
            speed = 19.0
        settings = {
            "points_text": self.jump_points_text.toPlainText(),
            "before_wait": self.jump_before_wait_entry.text(),
            "after_wait": self.jump_after_wait_entry.text(),
            "forward_m": self.jump_forward_m_entry.text(),
        }
        self._save_route_entry("jump", "steps", points, run_mode, repeat_count, speed, settings)

    # ==================== 同心圓生成 ====================
    def _build_gpx_concentric_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel(
            "中心座標（留空則用「一般路線」清單裡的最後一個點，也可到那邊用地圖右鍵「GPX工具：新增此點」設定）"
        ))
        self.concentric_center_entry = QLineEdit()
        self.concentric_center_entry.setPlaceholderText("留空 = 使用一般路線清單最後一點")
        layout.addWidget(self.concentric_center_entry)

        form = QFormLayout()
        self.concentric_start_radius_entry = QLineEdit("35")
        self.concentric_start_radius_entry.setObjectName("concentric_start_radius_entry")
        self.concentric_ring_count_entry = QLineEdit("3")
        self.concentric_ring_count_entry.setObjectName("concentric_ring_count_entry")
        self.concentric_radius_step_entry = QLineEdit("35")
        self.concentric_radius_step_entry.setObjectName("concentric_radius_step_entry")
        self.concentric_points_per_ring_entry = QLineEdit("12")
        self.concentric_points_per_ring_entry.setObjectName("concentric_points_per_ring_entry")
        form.addRow("起始半徑(m) 最低25", self.concentric_start_radius_entry)
        form.addRow("圓圈數量 1-100", self.concentric_ring_count_entry)
        form.addRow("每圈增加(m) 最低25", self.concentric_radius_step_entry)
        form.addRow("每圈節點 最低4", self.concentric_points_per_ring_entry)
        layout.addLayout(form)

        gen_btn = QPushButton("⭕ 產生同心圓（加入到「一般路線」清單）")
        gen_btn.clicked.connect(self._gpx_concentric_generate)
        layout.addWidget(gen_btn)

        hint = QLabel("產生後的座標會加入「一般路線」分頁的清單裡，可以在那邊排序、預覽、設定執行模式後開始巡邏。")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        layout.addWidget(hint)

        save_route_btn = QPushButton("💾 存為已存路線（存這次產生的同心圓設定）")
        save_route_btn.clicked.connect(self._gpx_save_concentric_as_route)
        layout.addWidget(save_route_btn)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _gpx_concentric_generate(self) -> None:
        center_text = self.concentric_center_entry.text().strip()
        if center_text:
            centers = gpx_tools.parse_coordinate_text(center_text)
            if not centers:
                QMessageBox.critical(self, "錯誤", "中心座標格式看不懂，請輸入「緯度, 經度」")
                return
            center = centers[0]
        elif self.gpx_points:
            center = self.gpx_points[-1]
        else:
            QMessageBox.warning(self, "提示", "請輸入中心座標，或先在「一般路線」分頁用地圖右鍵新增一個點當中心")
            return
        try:
            start_radius = float(self.concentric_start_radius_entry.text())
            ring_count = int(self.concentric_ring_count_entry.text())
            radius_step = float(self.concentric_radius_step_entry.text())
            points_per_ring = int(self.concentric_points_per_ring_entry.text())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "半徑/圈數/節點數請填數字")
            return
        ring_count = max(1, min(100, ring_count))
        pts = gpx_tools.generate_concentric_circles(center, start_radius, ring_count, radius_step, points_per_ring)
        self.gpx_points.extend(pts)
        self._refresh_gpx_list()
        self._log(f"⭕ 已產生同心圓路徑，共 {len(pts)} 個座標點（已加入一般路線清單）")
        self.gpx_last_concentric_points = pts
        self.gpx_last_concentric_settings = {
            "center_text": center_text,
            "start_radius": start_radius,
            "ring_count": ring_count,
            "radius_step": radius_step,
            "points_per_ring": points_per_ring,
        }

    def _gpx_save_concentric_as_route(self) -> None:
        if not self.gpx_last_concentric_points:
            QMessageBox.warning(self, "提示", "請先產生同心圓路線")
            return
        points = [[p[0], p[1]] for p in self.gpx_last_concentric_points]
        self._save_route_entry("concentric", "points", points, "once", 1, 19.0, dict(self.gpx_last_concentric_settings))

    # ==================== 已存路線 ====================
    def _prompt_route_save_details(self, default_name: str) -> Optional[tuple[str, str]]:
        name, ok = QInputDialog.getText(self, "存為已存路線", "請輸入路線名稱：", text=default_name)
        name = name.strip() if ok and name else ""
        if not name:
            return None
        category, ok2 = QInputDialog.getText(self, "存為已存路線", "分類（留空 = 未分類）：", text="未分類")
        category = category.strip() if ok2 and category else "未分類"
        return name, category

    def _save_route_entry(
        self, generator_type: str, kind: str, points: list, run_mode: str, repeat_count: int,
        speed_kmh: float, settings: dict,
    ) -> None:
        details = self._prompt_route_save_details(f"{generator_type} 路線 {len(self.saved_routes) + 1}")
        if details is None:
            return
        name, category = details
        self.saved_routes.append({
            "name": name, "category": category, "generator_type": generator_type, "kind": kind,
            "points": points, "run_mode": run_mode, "repeat_count": repeat_count, "speed_kmh": speed_kmh,
            "settings": settings,
        })
        routes_store.save_routes(APP_DIR, self.saved_routes)
        self._refresh_routes_list()
        self._log(f"💾 已存路線「{name}」（分類：{category}）")

    def _build_gpx_routes_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("分類篩選"))
        self.routes_category_filter = QComboBox()
        self.routes_category_filter.currentTextChanged.connect(lambda _t: self._refresh_routes_list())
        filter_row.addWidget(self.routes_category_filter, stretch=1)
        manage_cat_btn = QPushButton("🗂️ 分類管理")
        manage_cat_btn.clicked.connect(lambda: self._manage_categories_for(
            self.saved_routes, lambda: routes_store.save_routes(APP_DIR, self.saved_routes),
            self._refresh_routes_list, "已存路線"
        ))
        filter_row.addWidget(manage_cat_btn)
        layout.addLayout(filter_row)

        self.routes_list = QListWidget()
        layout.addWidget(self.routes_list, stretch=1)

        btn_row = QHBoxLayout()
        start_btn = QPushButton("🚀 直接執行")
        start_btn.clicked.connect(self._routes_start_selected)
        load_btn = QPushButton("📂 載入回產生器面板")
        load_btn.clicked.connect(self._routes_load_selected)
        delete_btn = QPushButton("🗑️ 刪除")
        delete_btn.setObjectName("DangerButton")
        delete_btn.clicked.connect(self._routes_delete_selected)
        btn_row.addWidget(start_btn)
        btn_row.addWidget(load_btn)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

        hint = QLabel("💡 在「一般路線／種花路徑／跳躍路徑／同心圓」各分頁按「💾 存為已存路線」即可存進這裡")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        layout.addWidget(hint)

        layout.addStretch(1)
        self._refresh_routes_list()
        return self._wrap_scroll(root)

    def _refresh_routes_list(self) -> None:
        self._refresh_category_filter_combo(self.routes_category_filter, self.saved_routes)
        selected_cat = self.routes_category_filter.currentText()
        self.routes_list.clear()
        self._routes_list_row_map: list[int] = []
        type_label = {"manual": "一般", "flower": "種花", "jump": "跳躍", "concentric": "同心圓"}
        for idx, r in enumerate(self.saved_routes):
            cat = r.get("category", "未分類")
            if selected_cat not in ("全部", "") and cat != selected_cat:
                continue
            kind = type_label.get(r.get("generator_type"), r.get("generator_type", "?"))
            self.routes_list.addItem(f"[{cat}][{kind}] {r['name']}　({len(r.get('points', []))} 點)")
            self._routes_list_row_map.append(idx)

    def _routes_selected_route(self) -> Optional[dict]:
        row = self.routes_list.currentRow()
        if row < 0 or row >= len(self._routes_list_row_map):
            QMessageBox.warning(self, "提示", "請先選擇一筆已存路線")
            return None
        return self.saved_routes[self._routes_list_row_map[row]]

    def _routes_start_selected(self) -> None:
        r = self._routes_selected_route()
        if r is None:
            return
        points_raw = r.get("points", [])
        if not points_raw:
            QMessageBox.warning(self, "提示", "這筆已存路線沒有座標")
            return
        slot = self.active_draw_device
        speed = float(r.get("speed_kmh", 19.0))
        run_mode = r.get("run_mode", "once")
        repeat_count = max(1, min(999, int(r.get("repeat_count", 1))))
        if r.get("kind") == "steps":
            steps = [gpx_tools.RouteStep(lat=p[0], lng=p[1], wait_s=p[2] if len(p) > 2 else 0.0) for p in points_raw]
            self.patrol_workers[slot].start_timed_steps(steps, speed, run_mode=run_mode, repeat_count=repeat_count)
        else:
            points = [(p[0], p[1]) for p in points_raw]
            segments = [points]
            self.patrol_segments[slot] = segments
            self.patrol_workers[slot].set_segments(segments)
            self.map_view.set_path(f"patrol_{slot}_0", points, color="#ffd700")
            self.patrol_workers[slot].start(speed, run_mode=run_mode, repeat_count=repeat_count)
        self.patrol_progress_lbl[slot].setText("巡邏中...")
        self._log(f"[{slot}] 🚀 已開始執行已存路線「{r['name']}」")
        self._history_add(r["name"], r.get("generator_type", "manual"), points_raw[0][0], points_raw[0][1])

    def _routes_load_selected(self) -> None:
        r = self._routes_selected_route()
        if r is None:
            return
        gtype = r.get("generator_type")
        settings = r.get("settings", {}) or {}
        points_raw = r.get("points", [])
        if gtype == "manual":
            self.gpx_points = [(p[0], p[1]) for p in points_raw]
            self._refresh_gpx_list()
            idx = self.gpx_run_mode_combo.findData(r.get("run_mode", "once"))
            self.gpx_run_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.gpx_repeat_count_entry.setText(str(r.get("repeat_count", 1)))
        elif gtype == "flower":
            self.flower_centers_text.setPlainText(settings.get("centers_text", ""))
            idx = self.flower_sort_combo.findData(settings.get("sort_mode", "paste"))
            self.flower_sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
            idx = self.flower_plant_combo.findData(settings.get("plant_mode", "teleport"))
            self.flower_plant_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.flower_speed_combo.setCurrentText(str(r.get("speed_kmh", 19)))
            ring1 = settings.get("ring1", {}) or {}
            for key, field in self.flower_ring1_fields.items():
                if key in ring1:
                    field.setText(str(ring1[key]))
            ring2 = settings.get("ring2")
            self.flower_ring2_enable.setChecked(bool(ring2))
            if ring2:
                for key, field in self.flower_ring2_fields.items():
                    if key in ring2:
                        field.setText(str(ring2[key]))
            idx = self.flower_run_mode_combo.findData(r.get("run_mode", "once"))
            self.flower_run_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.flower_repeat_count_entry.setText(str(r.get("repeat_count", 1)))
            self.gpx_flower_steps = [
                gpx_tools.RouteStep(lat=p[0], lng=p[1], wait_s=p[2] if len(p) > 2 else 0.0) for p in points_raw
            ]
        elif gtype == "jump":
            self.jump_points_text.setPlainText(settings.get("points_text", ""))
            self.jump_before_wait_entry.setText(str(settings.get("before_wait", 1)))
            self.jump_after_wait_entry.setText(str(settings.get("after_wait", 1)))
            self.jump_forward_m_entry.setText(str(settings.get("forward_m", 0)))
            self.jump_speed_entry.setText(str(r.get("speed_kmh", 19)))
            idx = self.jump_run_mode_combo.findData(r.get("run_mode", "once"))
            self.jump_run_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.jump_repeat_count_entry.setText(str(r.get("repeat_count", 1)))
            self.gpx_jump_steps = [
                gpx_tools.RouteStep(lat=p[0], lng=p[1], wait_s=p[2] if len(p) > 2 else 0.0) for p in points_raw
            ]
        elif gtype == "concentric":
            self.concentric_center_entry.setText(settings.get("center_text", ""))
            self.concentric_start_radius_entry.setText(str(settings.get("start_radius", 35)))
            self.concentric_ring_count_entry.setText(str(settings.get("ring_count", 3)))
            self.concentric_radius_step_entry.setText(str(settings.get("radius_step", 35)))
            self.concentric_points_per_ring_entry.setText(str(settings.get("points_per_ring", 12)))
            self.gpx_last_concentric_points = [(p[0], p[1]) for p in points_raw]
            self.gpx_last_concentric_settings = dict(settings)
        self._log(f"📂 已載入已存路線「{r['name']}」回產生器面板")

    def _routes_delete_selected(self) -> None:
        row = self.routes_list.currentRow()
        if row < 0 or row >= len(self._routes_list_row_map):
            QMessageBox.warning(self, "提示", "請先選擇一筆已存路線")
            return
        removed = self.saved_routes.pop(self._routes_list_row_map[row])
        routes_store.save_routes(APP_DIR, self.saved_routes)
        self._refresh_routes_list()
        self._log(f"🗑️ 已刪除已存路線「{removed['name']}」")

    # ==================== 分頁：團體種花 ====================
    def _build_group_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        hint = QLabel(
            "同一條路線、同一個座標一起走：房主設定花點與圈數，團員跟著房主的座標移動，"
            "每跑完一個花點會等所有人跟上再繼續。需要先架好房間伺服器（見 cloudflare_worker 資料夾）。"
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        conn_box = QGroupBox("🌐 房間連線")
        form = QFormLayout(conn_box)
        self.group_server_entry = QLineEdit()
        self.group_server_entry.setObjectName("group_server_entry")
        self.group_server_entry.setPlaceholderText("https://group-planting.你的帳號.workers.dev")
        self.group_room_entry = QLineEdit()
        self.group_room_entry.setObjectName("group_room_entry")
        self.group_room_entry.setPlaceholderText("房號：3-16 位大寫英數字")
        self.group_password_entry = QLineEdit()
        self.group_password_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.group_name_entry = QLineEdit()
        self.group_name_entry.setObjectName("group_name_entry")
        self.group_name_entry.setPlaceholderText("顯示名稱")
        self.group_max_members_entry = QLineEdit("20")
        self.group_max_members_entry.setObjectName("group_max_members_entry")
        form.addRow("伺服器網址", self.group_server_entry)
        form.addRow("房號", self.group_room_entry)
        form.addRow("房間密碼", self.group_password_entry)
        form.addRow("你的名稱", self.group_name_entry)
        form.addRow("人數上限(2-20)", self.group_max_members_entry)
        layout.addWidget(conn_box)

        conn_row = QHBoxLayout()
        create_btn = QPushButton("➕ 建立房間（當房主）")
        create_btn.clicked.connect(self._group_create_room)
        join_btn = QPushButton("🚪 加入房間（當團員）")
        join_btn.clicked.connect(self._group_join_room)
        leave_btn = QPushButton("👋 離開房間")
        leave_btn.clicked.connect(self._group_leave_room)
        conn_row.addWidget(create_btn)
        conn_row.addWidget(join_btn)
        conn_row.addWidget(leave_btn)
        layout.addLayout(conn_row)

        self.group_status_lbl = QLabel("尚未連線")
        self.group_status_lbl.setProperty("role", "title")
        self.group_status_lbl.setWordWrap(True)
        layout.addWidget(self.group_status_lbl)

        self.group_members_list = QListWidget()
        self.group_members_list.setMaximumHeight(140)
        layout.addWidget(self.group_members_list)

        self.group_ready_check = QCheckBox("✅ 我準備好了（團員）")
        self.group_ready_check.toggled.connect(lambda v: self.group.set_ready(v))
        layout.addWidget(self.group_ready_check)

        host_box = QGroupBox("👑 房主控制")
        host_layout = QVBoxLayout(host_box)
        host_hint = QLabel("花點與圈數設定沿用「🌸種花路徑」分頁；按下開始前會先把設定同步給團員。")
        host_hint.setProperty("role", "hint")
        host_hint.setWordWrap(True)
        host_layout.addWidget(host_hint)
        host_row = QHBoxLayout()
        sync_btn = QPushButton("🔄 同步設定")
        sync_btn.clicked.connect(self._group_sync_settings)
        start_btn = QPushButton("▶️ 開始")
        start_btn.clicked.connect(lambda: self._group_command("start"))
        pause_btn = QPushButton("⏸️ 暫停")
        pause_btn.clicked.connect(lambda: self._group_command("pause"))
        resume_btn = QPushButton("⏵ 繼續")
        resume_btn.clicked.connect(lambda: self._group_command("resume"))
        stop_btn = QPushButton("⏹️ 停止")
        stop_btn.setObjectName("DangerButton")
        stop_btn.clicked.connect(lambda: self._group_command("stop"))
        for b in (sync_btn, start_btn, pause_btn, resume_btn, stop_btn):
            host_row.addWidget(b)
        host_layout.addLayout(host_row)
        layout.addWidget(host_box)

        layout.addStretch(1)

        self.group.connectionChanged.connect(self._group_on_connection_changed)
        self.group.snapshotReceived.connect(self._group_on_snapshot)
        self.group.progressReceived.connect(self._group_on_progress)
        self.group.barrierRequested.connect(self._group_on_barrier_requested)
        self.group.barrierReleased.connect(self._group_on_barrier_released)
        self.group.commandAck.connect(self._group_on_command_ack)
        self.group.logMessage.connect(self._log)

        return self._wrap_scroll(root)

    # ---------------- 團體種花：連線 ----------------
    def _group_create_room(self) -> None:
        try:
            max_members = int(self._clamped_float(self.group_max_members_entry.text(), 2, 20, 20))
            self.group.create_room(
                self.group_server_entry.text(), self.group_room_entry.text(),
                self.group_password_entry.text(), self.group_name_entry.text() or "房主", max_members,
            )
        except group_client.GroupClientError as e:
            QMessageBox.critical(self, "建立房間失敗", str(e))
            return
        self._log(f"👥 已建立房間 {self.group.room_id}，等待團員加入")

    def _group_join_room(self) -> None:
        try:
            self.group.join_room(
                self.group_server_entry.text(), self.group_room_entry.text(),
                self.group_password_entry.text(), self.group_name_entry.text() or "團員",
            )
        except group_client.GroupClientError as e:
            QMessageBox.critical(self, "加入房間失敗", str(e))
            return
        self._log(f"👥 已加入房間 {self.group.room_id}")

    def _group_leave_room(self) -> None:
        self._group_stop_local_run()
        self.group.leave()
        self.group_room = {}
        self.group_members_list.clear()
        self.group_status_lbl.setText("尚未連線")

    def _group_on_connection_changed(self, connected: bool) -> None:
        if not connected:
            self.group_status_lbl.setText("已斷線")
            self._group_stop_local_run()

    def _group_on_snapshot(self, room: dict) -> None:
        self.group_room = room
        phase_text = {
            "idle": "待機中", "countdown": "倒數中", "running": "執行中", "paused": "已暫停",
        }.get(room.get("phase"), room.get("phase", "?"))
        role = "房主" if self.group.is_host else "團員"
        self.group_status_lbl.setText(
            f"房間 {room.get('roomId')}｜身分：{role}｜狀態：{phase_text}｜"
            f"{len(room.get('members', []))}/{room.get('maxMembers')} 人"
        )
        self.group_members_list.clear()
        for m in room.get("members", []):
            mark = "👑" if m.get("isHost") else ("✅" if m.get("ready") else "⏳")
            self.group_members_list.addItem(f"{mark} {m.get('name')}")

        phase = room.get("phase")
        if phase == "running":
            self._group_begin_local_run()
        elif phase in ("idle", "paused"):
            if phase == "idle":
                self._group_stop_local_run()
            elif self.group.is_host:
                self.patrol_workers[self.active_draw_device].pause()

    def _group_on_command_ack(self, ack: dict) -> None:
        if ack.get("ok"):
            self._log(f"👥 指令 {ack.get('command')} 已被伺服器接受")
            return
        reason = {
            "members_not_ready": "還有團員沒按準備", "no_flowers": "還沒同步花點設定",
            "busy": "目前狀態不允許（可能已經在跑了）", "not_running": "目前不是執行中",
            "not_paused": "目前不是暫停中", "no_host": "房間裡沒有房主",
        }.get(ack.get("reason"), ack.get("reason"))
        QMessageBox.warning(self, "指令被拒絕", f"{ack.get('command')}：{reason}")

    # ---------------- 團體種花：房主 ----------------
    def _group_sync_settings(self) -> None:
        centers = gpx_tools.parse_coordinate_text(self.flower_centers_text.toPlainText())
        if not centers:
            QMessageBox.warning(self, "提示", "請先到「🌸種花路徑」分頁填花點座標")
            return
        try:
            speed = float(self.flower_speed_combo.currentText())
        except ValueError:
            QMessageBox.critical(self, "錯誤", "團體速度必須為數字")
            return
        ring1 = self._gpx_flower_ring_settings(self.flower_ring1_fields)
        ring2 = self._gpx_flower_ring_settings(self.flower_ring2_fields) if self.flower_ring2_enable.isChecked() else None
        self.group.sync_settings({
            "centers": [[c[0], c[1]] for c in centers],
            "sortMode": self.flower_sort_combo.currentData(),
            "plantMode": self.flower_plant_combo.currentData(),
            "ring1": ring1,
            "ring2": ring2,
            "speed": speed,
            "flowerCount": len(centers),
            "ringCount": 2 if ring2 else 1,
        })
        self._log(f"👥 已把設定送給團員：{len(centers)} 個花點、{2 if ring2 else 1} 圈")

    def _group_command(self, command: str) -> None:
        if not self.group.is_host:
            QMessageBox.warning(self, "提示", "只有房主可以控制執行")
            return
        self.group.send_command(command)

    def _group_build_route_from_settings(self) -> list:
        """房主與團員都用房間裡同一份設定產生路線，確保兩邊步驟數與座標完全一致。"""
        settings = self.group_room.get("settings") or {}
        centers = [(c[0], c[1]) for c in settings.get("centers", [])]
        if not centers:
            return []
        steps, boundaries = gpx_tools.build_flower_route_detailed(
            centers, settings.get("sortMode", "paste"), settings.get("plantMode", "teleport"),
            settings.get("ring1") or {}, settings.get("ring2"), float(settings.get("speed", 19)),
        )
        self.group_boundaries = {idx: (ring, center) for idx, ring, center in boundaries}
        self.group_flower_count = int(settings.get("flowerCount", len(centers)))
        self.group_ring_count = int(settings.get("ringCount", 1))
        return steps

    def _group_begin_local_run(self) -> None:
        slot = self.active_draw_device
        worker = self.patrol_workers[slot]
        if worker.is_patrolling:
            if worker.is_paused:
                worker.resume()
            return
        if not self.group.is_host:
            # 團員不自己跑路線，只跟著房主廣播過來的座標移動
            self._log("👥 開始跟隨房主移動")
            return
        steps = self._group_build_route_from_settings()
        if not steps:
            QMessageBox.warning(self, "提示", "房間裡還沒有有效的花點設定")
            return
        settings = self.group_room.get("settings") or {}
        speed = float(settings.get("speed", 19))
        self._group_release.clear()
        self._group_pending_boundary = None
        worker.on_step = self._group_host_on_step
        worker.wait_barrier = self._group_host_wait_barrier
        worker.force_tick_mode = True
        worker.start_timed_steps(steps, speed)
        self.patrol_progress_lbl[slot].setText("團體種花執行中...")
        self._log(f"👥 房主開始跑團體路線（{len(steps)} 個路徑點）")
        self._history_add("團體種花", "flower", steps[0].lat, steps[0].lng)

    def _group_stop_local_run(self) -> None:
        slot = self.active_draw_device
        worker = self.patrol_workers[slot]
        self._group_release.set()   # 讓卡在同步點的巡邏執行緒能夠離開
        if worker.is_patrolling:
            worker.stop()
        worker.on_step = None
        worker.wait_barrier = None
        worker.force_tick_mode = False
        self._group_pending_boundary = None

    def _group_host_on_step(self, idx: int, lat: float, lng: float, lap: int) -> None:
        """在巡邏執行緒裡被呼叫：只做「發訊息」這種執行緒安全的事，不碰 UI 元件。"""
        ring, center = self.group_boundaries.get(idx, (0, -1))
        self.group.publish_progress({
            "sequence": idx, "lat": lat, "lon": lng, "round": lap,
            "ring": ring, "center": center,
        })

    def _group_host_wait_barrier(self, idx: int, lap: int) -> bool:
        mark = self.group_boundaries.get(idx)
        if mark is None:
            return True
        ring, center = mark
        boundary = group_client.boundary_index(lap, self.group_flower_count, self.group_ring_count, ring, center)
        self._group_release.clear()
        self._group_pending_boundary = boundary
        self.group.announce_barrier(boundary)
        # 等伺服器放行；設上限避免有人當掉就永遠卡住
        released = self._group_release.wait(timeout=60.0)
        self._group_pending_boundary = None
        if not released:
            self.nativeToolMessage.emit("⚠️ 等待團員同步超時，先繼續跑下一段")
        return True

    def _group_on_barrier_released(self, _boundary: int) -> None:
        self._group_release.set()

    # ---------------- 團體種花：團員 ----------------
    def _group_on_progress(self, payload: dict) -> None:
        if self.group.is_host:
            return
        lat, lng = payload.get("lat"), payload.get("lon")
        if lat is None or lng is None:
            return
        # 規格書 §11.7：前一筆還在送就只留最新一筆，且兩次發送至少間隔 320ms
        self._group_follower_pending = (float(lat), float(lng))
        now = time.time()
        if now - self._group_follower_last_sent < 0.32:
            QTimer.singleShot(int((0.32 - (now - self._group_follower_last_sent)) * 1000),
                              self._group_flush_follower_sync)
            return
        self._group_flush_follower_sync()

    def _group_flush_follower_sync(self) -> None:
        pending = self._group_follower_pending
        if pending is None:
            return
        self._group_follower_pending = None
        self._group_follower_last_sent = time.time()
        lat, lng = pending
        self.send_location_direct(self.active_draw_device, round(lat, 6), round(lng, 6))

    def _group_on_barrier_requested(self, boundary: int) -> None:
        if self.group.is_host:
            return
        # 團員：把手上最後一筆座標送完再回報跟上了
        self._group_flush_follower_sync()
        self.group.ack_barrier(boundary)

    # ==================== 分頁：工具與設定 ====================
    def _build_tools_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        font_box = QGroupBox("🔤 顯示設定")
        font_layout = QHBoxLayout(font_box)
        font_layout.addWidget(QLabel("字體大小"))
        self.ui_font_size_combo = QComboBox()
        self.ui_font_size_combo.addItem("小 (9)", 9)
        self.ui_font_size_combo.addItem("標準 (10)", 10)
        self.ui_font_size_combo.addItem("大 (12)", 12)
        self.ui_font_size_combo.addItem("特大 (14)", 14)
        self.ui_font_size_combo.setCurrentIndex(1)
        self.ui_font_size_combo.setObjectName("ui_font_size_combo")
        self.ui_font_size_combo.currentTextChanged.connect(lambda _t: self._apply_font_scale())
        font_layout.addWidget(self.ui_font_size_combo, stretch=1)
        layout.addWidget(font_box)

        diag_box = QGroupBox("🩺 診斷日誌")
        diag_layout = QVBoxLayout(diag_box)
        diag_hint = QLabel(
            "遇到連不上、定位沒反應等問題時，按「匯出診斷日誌」會產生一個純文字檔，"
            "裡面有系統環境、外部工具路徑、兩台裝置的連線狀態跟畫面上的日誌，"
            "把這個檔案傳給協助你的人比較好判斷問題。"
        )
        diag_hint.setProperty("role", "hint")
        diag_hint.setWordWrap(True)
        diag_layout.addWidget(diag_hint)
        diag_row = QHBoxLayout()
        export_log_btn = QPushButton("💾 匯出診斷日誌")
        export_log_btn.clicked.connect(self._export_diagnostics)
        clear_log_btn = QPushButton("🧹 清空日誌畫面")
        clear_log_btn.clicked.connect(self._clear_log)
        diag_row.addWidget(export_log_btn)
        diag_row.addWidget(clear_log_btn)
        diag_layout.addLayout(diag_row)
        layout.addWidget(diag_box)

        update_box = QGroupBox("🔄 版本與更新")
        update_layout = QVBoxLayout(update_box)
        update_layout.addWidget(QLabel(f"目前版本：v{APP_VERSION}"))
        self.update_status_lbl = QLabel("尚未檢查更新")
        self.update_status_lbl.setProperty("role", "hint")
        self.update_status_lbl.setWordWrap(True)
        update_layout.addWidget(self.update_status_lbl)
        check_update_btn = QPushButton("🔄 檢查更新")
        check_update_btn.clicked.connect(self._check_for_updates)
        update_layout.addWidget(check_update_btn)
        layout.addWidget(update_box)

        layout.addStretch(1)
        return self._wrap_scroll(root)

    def _apply_font_scale(self) -> None:
        """把選到的字體級距套成追加在 theme.qss 後面的覆寫規則。
        theme.qss 裡面標題/提示文字/工具列圖示都各自寫死了 px 級距，只呼叫
        QApplication.setFont() 那幾種文字不會跟著變大，所以這裡連它們一起換算。"""
        app_instance = QApplication.instance()
        if app_instance is None:
            return
        base_pt = self.ui_font_size_combo.currentData()
        if not isinstance(base_pt, int):
            base_pt = 10
        extra = (
            f"* {{ font-size: {base_pt}pt; }}\n"
            f'QLabel[role="title"] {{ font-size: {base_pt + 1}pt; }}\n'
            f'QLabel[role="hint"] {{ font-size: {max(7, base_pt - 2)}pt; }}\n'
            f"QToolButton {{ font-size: {base_pt + 5}pt; }}\n"
        )
        app_instance.setStyleSheet(self._base_stylesheet + "\n" + extra)

    def _clear_log(self) -> None:
        self.log_text.clear()
        self._log("🧹 已清空日誌畫面")

    def _collect_diagnostics(self) -> str:
        lines = [
            "=== 雙系統定位神盾 診斷日誌 ===",
            f"匯出時間: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"程式版本: v{APP_VERSION}",
            f"作業系統: {platform.platform()}",
            f"Python: {sys.version.split()[0]}",
            f"是否為打包版: {getattr(sys, 'frozen', False)}",
            f"程式資料夾: {APP_DIR}",
            "",
            "--- 外部工具 ---",
            f"ADB 路徑: {self._adb_path}（檔案存在: {os.path.exists(self._adb_path)}）",
        ]
        pmd3 = os.path.join(APP_DIR, "pymobiledevice3", "pymobiledevice3.exe")
        lines.append(f"隨附 pymobiledevice3: {pmd3}（檔案存在: {os.path.exists(pmd3)}）")
        apk_dir = os.path.join(APP_DIR, "安卓定位助手APK")
        lines.append(f"安卓定位助手APK 資料夾: {'存在' if os.path.isdir(apk_dir) else '不存在'}")

        lines += ["", "--- 裝置狀態 ---"]
        for slot in ("A", "B"):
            engine = self.engines[slot]
            if engine.is_connecting:
                status = "連線中"
            elif engine.is_alive:
                status = "已連線"
            else:
                status = "未連線"
            worker = self.patrol_workers[slot]
            transport = engine.active_transport
            lines += [
                f"[{slot}] 連線方式選單: {self.transport_combo[slot].currentText()}",
                f"[{slot}] 目前 transport: {getattr(transport, 'value', None)}",
                f"[{slot}] 連線狀態: {status}",
                f"[{slot}] 最後錯誤: {engine.last_error}",
                f"[{slot}] 目前座標: {self.curr_pos[slot]}",
                f"[{slot}] 巡邏中: {worker.is_patrolling}／暫停: {worker.is_paused}／"
                f"模式: {worker.run_mode} x{worker.repeat_count}",
                f"[{slot}] 巡邏區域數: {len(self.patrol_segments[slot])}",
            ]

        lines += [
            "",
            "--- 資料統計 ---",
            f"收藏: {len(self.favorites)} 筆／已存路線: {len(self.saved_routes)} 筆／"
            f"執行歷史: {len(self.history_entries)} 筆",
            f"GPX 一般路線點數: {len(self.gpx_points)}",
            f"種花路徑步驟數: {len(self.gpx_flower_steps)}／跳躍路徑步驟數: {len(self.gpx_jump_steps)}",
            "",
            "--- 畫面日誌（最近 500 行）---",
            self.log_text.toPlainText(),
        ]
        return "\n".join(lines)

    def _export_diagnostics(self) -> None:
        default_name = f"diagnostics_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出診斷日誌", os.path.join(APP_DIR, default_name), "Text Files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._collect_diagnostics())
        except OSError as e:
            QMessageBox.critical(self, "錯誤", f"匯出失敗：{e}")
            return
        self._log(f"💾 已匯出診斷日誌: {path}")

    def _check_for_updates(self) -> None:
        self.update_status_lbl.setText("檢查中...")

        def worker():
            latest = self._fetch_latest_version()
            if latest is None:
                self.updateCheckFinished.emit("⚠️ 檢查更新失敗（可能是沒有網路，或還沒有發布任何版本）")
                return
            if _version_tuple(latest) > _version_tuple(APP_VERSION):
                self.updateCheckFinished.emit(
                    f"🆕 有新版本 v{latest}（目前 v{APP_VERSION}）\n"
                    f"下載： https://github.com/{UPDATE_CHECK_REPO}/releases/latest"
                )
            else:
                self.updateCheckFinished.emit(f"✅ 已經是最新版本（v{APP_VERSION}）")

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_latest_version(self) -> Optional[str]:
        url = f"https://api.github.com/repos/{UPDATE_CHECK_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={
            "User-Agent": f"FakeGpsPatrol/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.nativeToolMessage.emit(f"⚠️ 檢查更新失敗：{e}")
            return None
        tag = (data.get("tag_name") or "").strip()
        return tag.lstrip("vV") or None

    def _on_update_check_finished(self, message: str) -> None:
        self.update_status_lbl.setText(message)
        self._log(f"🔄 {message}")

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
            details = self._prompt_favorite_details(f"收藏 {len(self.favorites) + 1}")
            if details:
                name, category = details
                self._add_favorite(name, lat, lng, category)

    # ==================== 關閉程式 ====================
    def closeEvent(self, event) -> None:
        settings_store.save_file(USER_SETTINGS_PATH, settings_store.collect(self))
        self._group_release.set()   # 避免巡邏執行緒還卡在團體同步點
        self.group.leave()
        for worker in self.patrol_workers.values():
            worker.stop()
        for engine in self.engines.values():
            engine.force_stop()
        self.tile_cache.stop()
        super().closeEvent(event)
