# ==========================================
# 檔案名稱：workers.py（精簡版）
# 說明：巡邏迴圈，從 Tkinter 版的 run_patrol_loop() 移植邏輯過來。
#       精簡版拿掉了螢幕投影蘑菇偵測（DetectionWorker），只保留巡邏送座標功能。
#
# 執行緒模型（重要，PySide6 版跟 Tkinter 版的關鍵差異）：
#   Tkinter 版背景執行緒常常直接呼叫 widget 方法更新畫面（例如直接 .config(text=...)），
#   這在 Tkinter 是「通常能動但沒保證」的做法。Qt 規定只有主執行緒能碰 UI，背景執行緒
#   一律只能 emit signal，實際的畫面更新永遠在 slot 裡執行（Qt 會自動把 signal 從背景
#   執行緒安全地排進主執行緒事件迴圈，不需要額外處理）。
#
#   這裡沿用原本「背景 threading.Thread 跑迴圈」的架構（沒有改用 QThread），
#   但迴圈內部絕對不碰任何 widget，只透過 Signal 把資料丟出去，由 MainWindow 的
#   slot 負責實際更新畫面 —— 這樣寫法上跟原本最接近，同時符合 Qt 的執行緒規則。
# ==========================================
from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

import gpx_tools


class PatrolWorker(QObject):
    """
    對應原本的 run_patrol_loop()：逐點送座標的巡邏迴圈。
    一個 PatrolWorker 對應一支手機（A 或 B 各自建立一個實例，互不影響）。
    """

    positionUpdated = Signal(float, float, int, int)  # lat, lng, 目前節點, 這個區域總節點數
    segmentAdvanced = Signal(int, int)                 # 新的區域index, 總區域數
    remainingTimeUpdated = Signal(str)
    logMessage = Signal(str)
    finished = Signal()

    def __init__(self, engine, label: str = "A"):
        super().__init__()
        self.engine = engine
        self.label = label

        self.segments: list[list[tuple[float, float]]] = []
        self.segment_idx = 0
        self.node_idx = 0
        self.curr_lat: Optional[float] = None
        self.curr_lng: Optional[float] = None
        self.speed_kmh = 20.0
        # (timestamp, lat, lng)：保留給未來擴充用（原本用來讓偵測迴圈回推「幾秒前的座標」）
        self.location_history: list[tuple[float, float, float]] = []

        self._is_patrolling = False
        self._is_paused = False
        self._thread: Optional[threading.Thread] = None

        # iOS 連續播放模式用的暫存 GPX 檔案路徑（固定檔名，每次巡邏重新覆寫即可）
        self._ios_gpx_path = os.path.join(tempfile.gettempdir(), f"fakegps_patrol_{label}.gpx")

    @property
    def patrol_path(self) -> list[tuple[float, float]]:
        if 0 <= self.segment_idx < len(self.segments):
            return self.segments[self.segment_idx]
        return []

    @property
    def is_patrolling(self) -> bool:
        return self._is_patrolling

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    def set_segments(self, segments: list[list[tuple[float, float]]]) -> None:
        self.segments = segments
        self.segment_idx = 0
        self.node_idx = 0

    def start(self, speed_kmh: float) -> None:
        if self._is_patrolling:
            return
        if not self.patrol_path:
            self.logMessage.emit("⚠️ 請先產生弓字型軌道！")
            return
        self.speed_kmh = speed_kmh
        self._is_patrolling = True
        self._is_paused = False
        if self.node_idx >= len(self.patrol_path):
            self.node_idx = 0
        # iOS 逐點送座標每一點都要重新跟手機建立開發者連線，巡邏這種高頻率送座標的
        # 場景會來不及跟上，手機上的定位變成一段一段用跳的；改成連續 GPX 播放模式，
        # 一次連線播完整條路線，移動才會平順。Android／單次傳送則沿用原本的逐點模式。
        run_target = self._run_ios_gpx if self._is_ios_transport() else self._run
        self._thread = threading.Thread(target=run_target, daemon=True, name=f"Patrol-{self.label}")
        self._thread.start()

    def _is_ios_transport(self) -> bool:
        transport = getattr(self.engine, "active_transport", None)
        value = getattr(transport, "value", "") or ""
        return value.startswith("ios")

    def pause(self) -> None:
        self._is_paused = True

    def resume(self) -> None:
        self._is_paused = False

    def stop(self) -> None:
        """徹底結束巡邏，並將進度重置到起點（對應 stop_patrol_completely）"""
        self._is_patrolling = False
        self._is_paused = False
        self.node_idx = 0
        self.engine.stop_ios_gpx_playback()

    def _advance_segment(self) -> bool:
        self.segment_idx += 1
        if self.segment_idx >= len(self.segments):
            return False
        self.node_idx = 0
        self.curr_lat, self.curr_lng = self.patrol_path[0]
        self.engine.set_location(self.curr_lat, self.curr_lng)
        self.segmentAdvanced.emit(self.segment_idx, len(self.segments))
        return True

    def _remaining_distance_m(self) -> float:
        path = self.patrol_path
        total = 0.0
        if path and self.node_idx < len(path) and self.curr_lat is not None:
            lat, lng = self.curr_lat, self.curr_lng
            for i in range(self.node_idx, len(path)):
                tlat, tlng = path[i]
                dy = (tlat - lat) * 111000.0
                dx = (tlng - lng) * 100000.0 * math.cos(math.radians(lat))
                total += math.hypot(dx, dy)
                lat, lng = tlat, tlng
        for seg_i in range(self.segment_idx + 1, len(self.segments)):
            seg = self.segments[seg_i]
            for i in range(len(seg) - 1):
                lat1, lng1 = seg[i]
                lat2, lng2 = seg[i + 1]
                dy = (lat2 - lat1) * 111000.0
                dx = (lng2 - lng1) * 100000.0 * math.cos(math.radians(lat1))
                total += math.hypot(dx, dy)
        return total

    def _run(self) -> None:
        self.logMessage.emit(f"🏃 [{self.label}] 自動巡邏啟動！目標時速: {self.speed_kmh} km/h")
        tick_rate = 0.5
        speed_mps = self.speed_kmh / 3.6
        step_distance = speed_mps * tick_rate

        if self.node_idx == 0 and self.patrol_path:
            self.curr_lat, self.curr_lng = self.patrol_path[0]

        while self._is_patrolling:
            if self._is_paused:
                time.sleep(0.2)
                continue

            path = self.patrol_path
            if self.node_idx >= len(path):
                if self._advance_segment():
                    continue
                break

            target_lat, target_lng = path[self.node_idx]
            dy = (target_lat - self.curr_lat) * 111000.0
            dx = (target_lng - self.curr_lng) * 100000.0 * math.cos(math.radians(self.curr_lat))
            dist = math.hypot(dx, dy)

            if dist <= step_distance:
                self.curr_lat, self.curr_lng = target_lat, target_lng
                self.node_idx += 1
            else:
                angle = math.atan2(dy, dx)
                self.curr_lat += (step_distance * math.sin(angle)) / 111000.0
                self.curr_lng += (step_distance * math.cos(angle)) / (100000.0 * math.cos(math.radians(self.curr_lat)))

            self.curr_lat = round(self.curr_lat, 6)
            self.curr_lng = round(self.curr_lng, 6)

            self.engine.set_location(self.curr_lat, self.curr_lng)

            now = time.time()
            self.location_history.append((now, self.curr_lat, self.curr_lng))
            if len(self.location_history) > 200:
                self.location_history.pop(0)

            self.positionUpdated.emit(self.curr_lat, self.curr_lng, self.node_idx, len(path))

            speed_mps = self.speed_kmh / 3.6
            if speed_mps > 0:
                seconds = int(self._remaining_distance_m() / speed_mps)
                h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
                self.remainingTimeUpdated.emit(f"{h:02d}時{m:02d}分{s:02d}秒")
            else:
                self.remainingTimeUpdated.emit("停止中")

            time.sleep(tick_rate)

        self._is_patrolling = False
        self.logMessage.emit(f"⏹️ [{self.label}] 巡邏迴圈結束。")
        self.finished.emit()

    # ---------------- iOS 專用：連續 GPX 播放巡邏 ----------------

    def _run_ios_gpx(self) -> None:
        """
        對應 iOS 連線時的巡邏迴圈：不逐點送座標，改把整條路線一次寫成「有時間戳記」
        的 GPX 檔案，交給 GpsEngine.start_ios_gpx_playback() 用同一條連線連續播放，
        避免逐點模式「每點都要重新連線」造成的延遲跟斷續跳動。

        重要：pymobiledevice3 的 GPX 播放器只會在兩個相鄰點之間睡滿對應秒數、然後
        瞬間跳到下一個點，並不會自己在中間補間——如果直接把巡邏路徑稀疏的轉折點寫進
        GPX，手機在兩個轉折點之間還是會「呆著不動一段時間、接著瞬間跳過去」。所以
        送出前一定要先用 gpx_tools.resample_route() 把路徑加密（每約0.3秒一個點），
        播放器逐點推進時，手機上看起來才會是連續平順移動。

        暫停時：先停止播放行程（讓手機停在目前內插到的座標），把「還沒走完的部分」
        記下來；恢復時，從目前位置重新規劃一條新路線、寫新的 GPX 檔案再繼續播放。
        """
        self.logMessage.emit(f"🏃 [{self.label}] 自動巡邏啟動（iOS 連續播放模式）！目標時速: {self.speed_kmh} km/h")

        full_route: list[tuple[float, float]] = []
        for seg in self.segments:
            full_route.extend(seg)

        if len(full_route) < 2:
            self.logMessage.emit(f"⚠️ [{self.label}] 路線點數不足，無法巡邏")
            self._is_patrolling = False
            self.finished.emit()
            return

        total_distance_all = sum(
            gpx_tools.distance_m(full_route[i], full_route[i + 1]) for i in range(len(full_route) - 1)
        )
        total_distance_all = max(total_distance_all, 1.0)

        remaining = list(full_route)
        distance_done = 0.0
        self.curr_lat, self.curr_lng = remaining[0]

        while self._is_patrolling and len(remaining) >= 2:
            if self._is_paused:
                time.sleep(0.2)
                continue

            dense_points = gpx_tools.resample_route(remaining, self.speed_kmh, interval_seconds=0.3)
            if len(dense_points) < 2:
                break

            try:
                gpx_tools.write_gpx_timed(dense_points, self._ios_gpx_path, self.speed_kmh)
            except Exception as e:
                self.logMessage.emit(f"⚠️ [{self.label}] 寫入巡邏 GPX 檔案失敗，改用逐點送座標模式：{e}")
                self._run()
                return

            if not self.engine.start_ios_gpx_playback(self._ios_gpx_path):
                self.logMessage.emit(f"⚠️ [{self.label}] iOS 連續播放啟動失敗，改用逐點送座標模式")
                self._run()
                return

            timeline = gpx_tools.route_timeline(dense_points, self.speed_kmh)
            total_duration = timeline[-1][0]
            speed_mps = max(self.speed_kmh, 0.1) / 3.6
            route_start = time.time()
            local_idx = 0
            elapsed = 0.0

            while self._is_patrolling and not self._is_paused:
                elapsed = time.time() - route_start
                if elapsed >= total_duration:
                    local_idx = len(dense_points) - 1
                    self.curr_lat, self.curr_lng = dense_points[-1]
                    elapsed = total_duration
                    break

                lat, lng, local_idx = self._interpolate_timeline(timeline, elapsed)
                self.curr_lat, self.curr_lng = lat, lng

                now = time.time()
                self.location_history.append((now, lat, lng))
                if len(self.location_history) > 200:
                    self.location_history.pop(0)

                remaining_seconds = max(0.0, total_duration - elapsed)
                h, m, s = int(remaining_seconds) // 3600, (int(remaining_seconds) % 3600) // 60, int(remaining_seconds) % 60
                self.remainingTimeUpdated.emit(f"{h:02d}時{m:02d}分{s:02d}秒")
                travelled_now = distance_done + elapsed * speed_mps
                self.positionUpdated.emit(lat, lng, int(travelled_now), int(total_distance_all))

                time.sleep(0.3)

            self.engine.stop_ios_gpx_playback()
            distance_done += min(elapsed, total_duration) * speed_mps

            if not self._is_patrolling:
                break

            if self._is_paused:
                remaining = [(self.curr_lat, self.curr_lng)] + dense_points[local_idx + 1:]
                while self._is_patrolling and self._is_paused:
                    time.sleep(0.2)
                continue

            # 沒有被暫停打斷，代表整條路線正常播完
            remaining = []

        self.engine.stop_ios_gpx_playback()
        self._is_patrolling = False
        self.logMessage.emit(f"⏹️ [{self.label}] 巡邏迴圈結束。")
        self.finished.emit()

    @staticmethod
    def _interpolate_timeline(
        timeline: list[tuple[float, float, float]], elapsed: float
    ) -> tuple[float, float, int]:
        """在時間軸上內插出 elapsed 秒當下的座標，回傳 (lat, lng, 這個時間點所在的區間起點索引)"""
        if elapsed <= timeline[0][0]:
            return timeline[0][1], timeline[0][2], 0
        for i in range(1, len(timeline)):
            t_prev, lat_prev, lng_prev = timeline[i - 1]
            t_next, lat_next, lng_next = timeline[i]
            if elapsed <= t_next:
                span = t_next - t_prev
                ratio = 0.0 if span <= 0 else (elapsed - t_prev) / span
                lat = lat_prev + (lat_next - lat_prev) * ratio
                lng = lng_prev + (lng_next - lng_prev) * ratio
                return lat, lng, i - 1
        last = timeline[-1]
        return last[1], last[2], len(timeline) - 1
