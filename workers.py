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

        # 路線執行模式："once"(跑完就停，預設值＝原本行為) / "reverse"(跑到底再反向
        # 走回起點算一輪) / "loop"(持續循環，忽略 repeat_count，直到使用者按停止)。
        # 這幾個屬性只有 GPX 頁面的「一般路線」會實際傳非預設值進來；Z字巡邏(_start_patrol)
        # 呼叫 start() 時完全不傳這些新參數，永遠維持 once/repeat=1，行為不受影響。
        self.run_mode = "once"
        self.repeat_count = 1
        self._laps_done = 0
        self._reverse_pending = False

        # 種花／跳躍路徑（RouteStep 序列）專用的暫存路徑清單，跟 self.segments 是分開的
        # 執行路徑，set_segments()/start() 那條既有路線完全不會用到這個屬性
        self._timed_steps: list = []

        # ---- 團體種花（房主）用的掛鉤，預設 None＝完全不影響單機執行 ----
        # on_step(step_idx, lat, lng, lap)：每送出一個座標成功後呼叫，房主用來廣播進度。
        # wait_barrier(step_idx, lap) -> bool：走到同步點時呼叫，會一直擋住直到所有團員
        #   跟上（回傳 True 繼續、False 代表要中止）。兩個都在巡邏執行緒裡被呼叫，
        #   實作端只能發 Qt signal／設 Event，不可以直接碰 UI 元件。
        self.on_step = None
        self.wait_barrier = None
        # 團體模式強制走逐點模式：同步屏障需要「每一點送出後確認」才能等人，
        # iOS 的連續 GPX 播放是一次播完整條、中途沒有逐點回報，沒辦法配合同步，
        # 所以團體種花時即使是 iOS 也走 tick 模式。單機執行不受影響。
        self.force_tick_mode = False

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

    def start(self, speed_kmh: float, run_mode: str = "once", repeat_count: int = 1) -> None:
        """
        run_mode/repeat_count 只有 GPX 頁面「一般路線」的執行模式選單會實際傳非預設值——
        Z字巡邏(_start_patrol)呼叫這個方法時完全不帶這兩個參數，永遠是 once/1，
        跟這個功能加進來之前的行為完全一樣。
        """
        if self._is_patrolling:
            return
        if not self.patrol_path:
            self.logMessage.emit("⚠️ 請先產生弓字型軌道！")
            return
        self.speed_kmh = speed_kmh
        self.run_mode = run_mode
        self.repeat_count = max(1, repeat_count)
        self._laps_done = 0
        self._reverse_pending = False
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

    def start_timed_steps(
        self,
        steps: list[gpx_tools.RouteStep],
        speed_kmh: float,
        run_mode: str = "once",
        repeat_count: int = 1,
    ) -> None:
        """
        種花路徑／跳躍路徑專用的獨立進入點，吃 gpx_tools.RouteStep（帶等待秒數）序列，
        跟 set_segments()/start() 那組既有進入點完全分開，互不影響——Z字巡邏跟「一般
        路線」都還是走 set_segments()/start()，這個方法只有種花／跳躍面板會呼叫。
        """
        if self._is_patrolling:
            return
        if not steps:
            self.logMessage.emit("⚠️ 路線是空的，無法開始！")
            return
        self.speed_kmh = speed_kmh
        self.run_mode = run_mode
        self.repeat_count = max(1, repeat_count)
        self._laps_done = 0
        self._reverse_pending = False
        self._timed_steps = list(steps)
        self._is_patrolling = True
        self._is_paused = False
        use_ios_playback = self._is_ios_transport() and not self.force_tick_mode
        run_target = self._run_timed_ios if use_ios_playback else self._run_timed_android
        self._thread = threading.Thread(target=run_target, daemon=True, name=f"Patrol-{self.label}")
        self._thread.start()

    def _handle_timed_completion(self) -> bool:
        """跟 _handle_route_completion() 同樣的 run_mode 邏輯，只是操作對象是
        self._timed_steps（RouteStep 序列）而不是 self.segments。"""
        if self.run_mode == "once":
            return False
        if self.run_mode == "reverse":
            if not self._reverse_pending:
                self._timed_steps = list(reversed(self._timed_steps))
                self._reverse_pending = True
                self.logMessage.emit(f"↩️ [{self.label}] 已到終點，反向走回起點")
                return True
            self._timed_steps = list(reversed(self._timed_steps))
            self._reverse_pending = False
            self._laps_done += 1
            if self._laps_done >= self.repeat_count:
                return False
            self.logMessage.emit(f"🔁 [{self.label}] 第 {self._laps_done + 1}/{self.repeat_count} 輪開始")
            return True
        if self.run_mode == "loop":
            self.logMessage.emit(f"🔁 [{self.label}] 持續循環，重新開始")
            return True
        return False

    def _sleep_pausable(self, duration: float) -> bool:
        """睡滿 duration 秒，但持續檢查暫停/停止狀態；暫停時延長等待、停止時提前返回。
        回傳 True 代表正常睡完，False 代表中途被停止（呼叫端應該直接結束迴圈）。"""
        remaining = duration
        while remaining > 0:
            if not self._is_patrolling:
                return False
            if self._is_paused:
                time.sleep(0.2)
                continue
            chunk = min(0.2, remaining)
            time.sleep(chunk)
            remaining -= chunk
        return True

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

    def _handle_route_completion(self) -> bool:
        """
        全部 segments 都跑完之後，依 run_mode 決定要不要繼續下一輪。
        回傳 True 代表已經重設好 segment_idx/node_idx，呼叫端應該 continue 主迴圈；
        False 代表真的結束，呼叫端應該 break。

        run_mode == "once" 時一定回傳 False——這正是這個功能加進來之前，唯一存在過
        的行為，Z字巡邏永遠只會走到這個分支。
        """
        if self.run_mode == "once":
            return False

        if self.run_mode == "reverse":
            if not self._reverse_pending:
                # 正向跑完，反轉整條路線再跑一次「回到起點」
                self.segments = [list(reversed(seg)) for seg in reversed(self.segments)]
                self.segment_idx = 0
                self.node_idx = 0
                self._reverse_pending = True
                self.logMessage.emit(f"↩️ [{self.label}] 已到終點，反向走回起點")
                return True
            # 反向也跑完了，這樣才算一輪；轉回正向準備開始下一輪（如果還有的話）
            self.segments = [list(reversed(seg)) for seg in reversed(self.segments)]
            self._reverse_pending = False
            self._laps_done += 1
            if self._laps_done >= self.repeat_count:
                return False
            self.segment_idx = 0
            self.node_idx = 0
            self.logMessage.emit(f"🔁 [{self.label}] 第 {self._laps_done + 1}/{self.repeat_count} 輪開始")
            return True

        if self.run_mode == "loop":
            self.segment_idx = 0
            self.node_idx = 0
            self.logMessage.emit(f"🔁 [{self.label}] 持續循環，重新開始")
            return True

        return False

    def _remaining_distance_m(self) -> float:
        path = self.patrol_path
        total = 0.0
        if path and self.node_idx < len(path) and self.curr_lat is not None:
            lat, lng = self.curr_lat, self.curr_lng
            for i in range(self.node_idx, len(path)):
                tlat, tlng = path[i]
                dy = (tlat - lat) * 111000.0
                dx = gpx_tools.lng_delta(lng, tlng) * 100000.0 * math.cos(math.radians(lat))
                total += math.hypot(dx, dy)
                lat, lng = tlat, tlng
        for seg_i in range(self.segment_idx + 1, len(self.segments)):
            seg = self.segments[seg_i]
            for i in range(len(seg) - 1):
                lat1, lng1 = seg[i]
                lat2, lng2 = seg[i + 1]
                dy = (lat2 - lat1) * 111000.0
                dx = gpx_tools.lng_delta(lng1, lng2) * 100000.0 * math.cos(math.radians(lat1))
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
                if self._handle_route_completion():
                    continue
                break

            target_lat, target_lng = path[self.node_idx]
            dy = (target_lat - self.curr_lat) * 111000.0
            dx = gpx_tools.lng_delta(self.curr_lng, target_lng) * 100000.0 * math.cos(math.radians(self.curr_lat))
            dist = math.hypot(dx, dy)

            if dist <= step_distance:
                self.curr_lat, self.curr_lng = target_lat, target_lng
                self.node_idx += 1
            else:
                angle = math.atan2(dy, dx)
                self.curr_lat += (step_distance * math.sin(angle)) / 111000.0
                self.curr_lng += (step_distance * math.cos(angle)) / (100000.0 * math.cos(math.radians(self.curr_lat)))

            self.curr_lat = round(self.curr_lat, 6)
            self.curr_lng = round(gpx_tools.normalize_lng(self.curr_lng), 6)

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

            # 沒有被暫停打斷，代表整條路線正常播完；依 run_mode 決定要不要跑下一輪
            # （once 時 _handle_route_completion 一定回傳 False，維持原本「播完就停」的行為）
            if self._is_patrolling and self._handle_route_completion():
                full_route = []
                for seg in self.segments:
                    full_route.extend(seg)
                remaining = list(full_route) if len(full_route) >= 2 else []
                if remaining:
                    self.curr_lat, self.curr_lng = remaining[0]
                    distance_done = 0.0
                continue

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

    # ---------------- 種花／跳躍路徑（RouteStep）專用執行迴圈 ----------------

    def _run_timed_android(self) -> None:
        """RouteStep 序列的 Android 版執行迴圈：逐點送座標，抵達後依 wait_s 停留。"""
        self.logMessage.emit(f"🏃 [{self.label}] 自動巡邏啟動（自訂路線模式）！目標時速: {self.speed_kmh} km/h")
        tick_rate = 0.5

        if not self._timed_steps:
            self._is_patrolling = False
            self.finished.emit()
            return

        idx = 0
        self.curr_lat, self.curr_lng = self._timed_steps[0].lat, self._timed_steps[0].lng

        while self._is_patrolling:
            if self._is_paused:
                time.sleep(0.2)
                continue

            steps = self._timed_steps
            if idx >= len(steps):
                if self._handle_timed_completion():
                    idx = 0
                    if self._timed_steps:
                        self.curr_lat, self.curr_lng = self._timed_steps[0].lat, self._timed_steps[0].lng
                    continue
                break

            target = steps[idx]
            speed_mps = self.speed_kmh / 3.6
            step_distance = speed_mps * tick_rate
            dy = (target.lat - self.curr_lat) * 111000.0
            dx = gpx_tools.lng_delta(self.curr_lng, target.lng) * 100000.0 * math.cos(math.radians(self.curr_lat))
            dist = math.hypot(dx, dy)

            if dist <= step_distance:
                self.curr_lat = round(target.lat, 6)
                self.curr_lng = round(gpx_tools.normalize_lng(target.lng), 6)
                self.engine.set_location(self.curr_lat, self.curr_lng)
                self.positionUpdated.emit(self.curr_lat, self.curr_lng, idx + 1, len(steps))
                # 團體種花：座標真的送出去之後才廣播進度／等團員跟上，
                # 順序跟規格書 §11.6「房主只在本機裝置定位成功後發布」一致
                if self.on_step is not None:
                    self.on_step(idx, self.curr_lat, self.curr_lng, self._laps_done)
                if self.wait_barrier is not None and not self.wait_barrier(idx, self._laps_done):
                    break
                wait_s = target.wait_s
                idx += 1
                if wait_s > 0:
                    self.remainingTimeUpdated.emit(f"停留中（{wait_s:.0f}秒）")
                    if not self._sleep_pausable(wait_s):
                        break
                else:
                    time.sleep(tick_rate)
            else:
                angle = math.atan2(dy, dx)
                self.curr_lat += (step_distance * math.sin(angle)) / 111000.0
                self.curr_lng += (step_distance * math.cos(angle)) / (100000.0 * math.cos(math.radians(self.curr_lat)))
                self.curr_lat = round(self.curr_lat, 6)
                self.curr_lng = round(gpx_tools.normalize_lng(self.curr_lng), 6)
                self.engine.set_location(self.curr_lat, self.curr_lng)
                self.positionUpdated.emit(self.curr_lat, self.curr_lng, idx, len(steps))
                time.sleep(tick_rate)

        self._is_patrolling = False
        self.logMessage.emit(f"⏹️ [{self.label}] 巡邏迴圈結束。")
        self.finished.emit()

    def _find_resume_step_index(self, steps: list[gpx_tools.RouteStep], elapsed: float) -> int:
        """暫停時，依目前已經過的秒數(elapsed)算出走到 steps 的第幾個索引，
        用跟 gpx_tools.route_step_timeline() 完全同一套時間累加公式，確保跟實際
        播放進度一致。回傳的索引「已經走完」，續播時要從這個索引之後接續。"""
        speed_mps = max(self.speed_kmh, 0.1) / 3.6
        t = steps[0].wait_s if steps[0].wait_s > 0 else 0.0
        if elapsed <= t:
            return 0
        for i in range(1, len(steps)):
            prev, cur = steps[i - 1], steps[i]
            t += gpx_tools.distance_m((prev.lat, prev.lng), (cur.lat, cur.lng)) / speed_mps
            if cur.wait_s > 0:
                t += cur.wait_s
            if elapsed <= t:
                return i
        return len(steps) - 1

    def _run_timed_ios(self) -> None:
        """
        RouteStep 序列的 iOS 連續播放版本，架構跟 _run_ios_gpx() 相同（一次連線播完
        整段路線，避免逐點重連造成的延遲跟跳動），差別是時間軸改用
        gpx_tools.route_step_timeline()/write_gpx_timed_from_steps()，讓每個點的
        wait_s 停留時間真的反映在播放進度裡。

        種花／跳躍路徑產生器本身在轉折/繞圈處已經有夠密的取樣點，不需要像
        _run_ios_gpx() 那樣額外呼叫 resample_route() 加密。
        """
        self.logMessage.emit(f"🏃 [{self.label}] 自動巡邏啟動（自訂路線 iOS 連續播放模式）！目標時速: {self.speed_kmh} km/h")

        remaining = list(self._timed_steps)
        if not remaining:
            self._is_patrolling = False
            self.finished.emit()
            return

        self.curr_lat, self.curr_lng = remaining[0].lat, remaining[0].lng

        while self._is_patrolling and remaining:
            if self._is_paused:
                time.sleep(0.2)
                continue

            if len(remaining) < 2 and remaining[0].wait_s <= 0:
                break

            try:
                gpx_tools.write_gpx_timed_from_steps(remaining, self._ios_gpx_path, self.speed_kmh)
            except Exception as e:
                self.logMessage.emit(f"⚠️ [{self.label}] 寫入巡邏 GPX 檔案失敗，改用逐點送座標模式：{e}")
                self._run_timed_android()
                return

            if not self.engine.start_ios_gpx_playback(self._ios_gpx_path):
                self.logMessage.emit(f"⚠️ [{self.label}] iOS 連續播放啟動失敗，改用逐點送座標模式")
                self._run_timed_android()
                return

            timeline = gpx_tools.route_step_timeline(remaining, self.speed_kmh)
            total_duration = timeline[-1][0]
            route_start = time.time()
            elapsed = 0.0

            while self._is_patrolling and not self._is_paused:
                elapsed = time.time() - route_start
                if elapsed >= total_duration:
                    self.curr_lat, self.curr_lng = timeline[-1][1], timeline[-1][2]
                    elapsed = total_duration
                    break

                lat, lng, local_idx = self._interpolate_timeline(timeline, elapsed)
                self.curr_lat, self.curr_lng = lat, lng

                remaining_seconds = max(0.0, total_duration - elapsed)
                h, m, s = int(remaining_seconds) // 3600, (int(remaining_seconds) % 3600) // 60, int(remaining_seconds) % 60
                self.remainingTimeUpdated.emit(f"{h:02d}時{m:02d}分{s:02d}秒")
                self.positionUpdated.emit(lat, lng, local_idx, len(timeline))

                time.sleep(0.3)

            self.engine.stop_ios_gpx_playback()

            if not self._is_patrolling:
                break

            if self._is_paused:
                resume_index = self._find_resume_step_index(remaining, elapsed)
                remaining = [gpx_tools.RouteStep(self.curr_lat, self.curr_lng)] + remaining[resume_index + 1:]
                while self._is_patrolling and self._is_paused:
                    time.sleep(0.2)
                continue

            # 正常播完，依 run_mode 決定要不要繼續下一輪
            if self._handle_timed_completion():
                remaining = list(self._timed_steps)
                if remaining:
                    self.curr_lat, self.curr_lng = remaining[0].lat, remaining[0].lng
                continue

            remaining = []

        self.engine.stop_ios_gpx_playback()
        self._is_patrolling = False
        self.logMessage.emit(f"⏹️ [{self.label}] 巡邏迴圈結束。")
        self.finished.emit()
