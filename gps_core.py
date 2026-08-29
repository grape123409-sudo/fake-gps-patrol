# ==========================================
# 檔案名稱：gps_core.py
# 說明：合併版核心定位模組，取代 my_fake_gps_core.py + async_gps_engine.py。
#
# 內容：
#   - ConnectionState      連線狀態列舉
#   - DeviceInfo            裝置資訊資料類別
#   - GpsEngine             非同步定位引擎（沿用 async_gps_engine.py 的事件迴圈架構，
#                            並移植 FakeGPSCore 裡「Android App 在背景就自動改前景重試」的邏輯）
#   - AndroidDeviceScanner  Android 裝置探索：USB 列表 + ADB over WiFi 探索 + 背景健康檢查
#   - IosDeviceScanner      iOS 裝置探索：USB usbmux 列表 + RSD 服務是否可連
#
# 設計原則：全面 asyncio、狀態用 Enum、資料結構用 dataclass、一律 type hints、
#           用 logging 取代 print()。
# ==========================================
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger("gps_core")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def resolve_app_dir() -> str:
    """
    平常回傳這支程式所在的資料夾路徑（用 __file__ 算）。

    但打包成 exe（PyInstaller）之後，改回傳 exe「實際、持續存在」的所在資料夾
    （sys.executable 所在目錄）：
      - onedir 模式：sys.executable 就是發布資料夾裡的那支 exe，天生正確。
      - onefile 模式：exe 執行時會先解壓縮到一個每次啟動都不同、程式關掉就消失
        的暫存資料夾，這時候如果繼續用 __file__ 算路徑，會指向那個暫存資料夾，
        使用者的設定值/存檔/圖磚快取每次重開程式都會「消失」；sys.executable
        則仍然正確指向使用者實際雙擊的那個 exe 檔案本身的位置，不會跑掉。

    main.py / main_window.py 都改呼叫這個函式來算 APP_DIR，統一成同一套邏輯。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _python_exe() -> str:
    """
    平常回傳 sys.executable（目前這個Python直譯器路徑）就好。
    但如果程式被打包成 exe（PyInstaller 等工具），sys.executable 會變成打包出來的
    exe 自己，不是真正的 python.exe——這時候呼叫「sys.executable -m pymobiledevice3」
    實際上會變成不斷重新啟動我們自己的程式，不是真的在跟 pymobiledevice3 溝通。
    打包後改成呼叫系統 PATH 裡真正安裝的 python，讓作業系統自己去找。

    這個函式只在「找不到隨附打包的 pymobiledevice3.exe」時才會真的被用到
    （見 _pymobiledevice3_cmd_prefix()）——正常打包發布的版本應該都帶有隨附
    版本，走不到這裡；這個 fallback 主要是給「還沒打包、直接跑 python main.py」
    的開發階段用。
    """
    if getattr(sys, "frozen", False):
        return "python"
    return sys.executable


def _pymobiledevice3_cmd_prefix() -> list[str]:
    """
    組出呼叫 pymobiledevice3 指令的開頭部分。

    優先找「跟這支程式放在同一個資料夾下的 pymobiledevice3/pymobiledevice3.exe」
    ——那是另一支獨立的 PyInstaller 打包腳本，把 pymobiledevice3 自己也凍結成
    一支獨立執行檔（原因：它依賴一大堆 C 擴充套件，沒辦法簡單併進主程式的 exe
    裡，所以做成隨附的第二支 exe，概念上跟隨附的 adb.exe 一樣）。
    找到就直接呼叫它（不需要 "-m" 也不需要系統 python）；找不到（例如開發階段
    直接跑 python main.py，還沒跑過打包腳本）就照舊退回「用系統安裝的 python
    去 -m pymobiledevice3」。
    """
    bundled = os.path.join(resolve_app_dir(), "pymobiledevice3", "pymobiledevice3.exe")
    if os.path.exists(bundled):
        return [bundled]
    return [_python_exe(), "-m", "pymobiledevice3"]


# Windows 專用：呼叫外部指令（adb、pymobiledevice3）預設會幫子程序另外彈出一個主控台視窗，
# 跑完馬上關掉，變成「秒開秒關的小黑窗」。這裡統一在 Windows 上加這個旗標抑制掉，
# 非 Windows 環境（例如開發時在其他系統測試）沒有這個常數，安全地退回不加任何旗標。
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd, **kwargs):
    kwargs.setdefault("creationflags", _NO_WINDOW_FLAGS)
    return subprocess.run(cmd, **kwargs)


def _popen(cmd, **kwargs):
    kwargs.setdefault("creationflags", _NO_WINDOW_FLAGS)
    return subprocess.Popen(cmd, **kwargs)


def _check_output(cmd, **kwargs):
    kwargs.setdefault("creationflags", _NO_WINDOW_FLAGS)
    return subprocess.check_output(cmd, **kwargs)


class ConnectionState(Enum):
    IDLE = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    ERROR = auto()
    STOPPED = auto()


class Transport(Enum):
    ANDROID_USB = "android_usb"
    ANDROID_WIFI = "android_wifi"
    IOS_USERSPACE = "ios_userspace"
    IOS_RSD = "ios_rsd"


@dataclass
class DeviceInfo:
    """描述一台被探索到的裝置"""
    identifier: str                 # 序號 / IP:Port，作為唯一識別
    display_name: str               # 顯示用名稱
    transport: Transport
    extra: dict = field(default_factory=dict)  # 額外資訊（例如 rsd_ip / rsd_port）


# ============================================================
# GpsEngine：非同步定位引擎
# ============================================================
class GpsEngine:
    """
    常駐一條 asyncio 事件迴圈的背景執行緒。
    外部（GUI / 巡邏執行緒 / 搖桿執行緒）一律透過 set_location() 這個
    執行緒安全的入口把座標交給事件迴圈，不直接碰 asyncio 物件。
    """

    def __init__(self, adb_path: str = r"C:\adb\adb.exe", android_serial: Optional[str] = None) -> None:
        self.adb_path = adb_path
        # 指定這個引擎要對哪一支 Android 裝置下指令（adb -s <序號>）。
        # 只有一支手機接電腦時可以留 None（adb 自己就知道要對誰），
        # 兩支以上同時接電腦時一定要指定，不然 adb 會直接報錯「有多台裝置」。
        self.android_serial = android_serial

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[__import__("threading").Thread] = None
        self._loop_ready = __import__("threading").Event()

        self._cmd_queue: Optional[asyncio.Queue] = None
        self._worker_future = None

        self._lock = __import__("threading").Lock()
        self._busy = False
        self._gpx_process: Optional[subprocess.Popen] = None
        self._state = ConnectionState.IDLE
        self._active_transport: Optional[Transport] = None
        self._last_error: Optional[str] = None

    # ---------------- 事件迴圈執行緒管理 ----------------

    def _adb_base_cmd(self) -> list[str]:
        """組合 adb 指令的開頭部分：有指定裝置序號就帶 -s，沒有就跟原本行為一樣（適用單裝置場景）"""
        if self.android_serial:
            return [self.adb_path, "-s", self.android_serial]
        return [self.adb_path]

    def _on_loop_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._cmd_queue = asyncio.Queue()
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _ensure_loop(self) -> None:
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        import threading
        self._loop_ready.clear()
        self._loop_thread = threading.Thread(target=self._on_loop_thread, daemon=True, name="GpsEngineLoop")
        self._loop_thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError("GpsEngine 事件迴圈啟動逾時")

    def _set_state(self, state: ConnectionState) -> None:
        with self._lock:
            self._state = state
        logger.info("狀態變更 -> %s", state.name)

    # ---------------- 啟動連線 ----------------

    def _cancel_current_worker(self) -> None:
        """
        啟動新連線前，先確實中止上一個還在跑的 worker。

        沒有這一步的話，使用者只要按第二次「連線」（例如覺得第一次沒反應、或是斷線後
        重連），舊的 worker coroutine 不會被換掉，會跟新啟動的 worker 同時活著、
        搶同一條命令佇列（self._cmd_queue）。兩個 worker 各自收到座標後都會分別
        呼叫 pymobiledevice3 對同一支手機重新建立開發者連線，兩個行程互搶同一個
        USB lockdown 連線名額會卡住，最後雙雙撞上 30 秒逾時——巡邏時因為持續送新
        座標，這個「重複 worker」的機率跟影響都比只點一次「傳送目前定位」大很多，
        這正是巡邏時大量出現逾時警告、但單次傳送正常的原因。
        """
        if self._worker_future is not None and not self._worker_future.done():
            self._worker_future.cancel()
        self._worker_future = None

    def start_android_usb(self) -> None:
        self._ensure_loop()
        self._cancel_current_worker()
        self._set_state(ConnectionState.CONNECTING)
        self._active_transport = Transport.ANDROID_USB
        self._worker_future = asyncio.run_coroutine_threadsafe(
            self._run_worker(self._worker_android()), self._loop
        )

    def start_ios_userspace(self) -> None:
        self._ensure_loop()
        self._cancel_current_worker()
        self._set_state(ConnectionState.CONNECTING)
        self._active_transport = Transport.IOS_USERSPACE
        self._worker_future = asyncio.run_coroutine_threadsafe(
            self._run_worker(self._worker_ios_userspace()), self._loop
        )

    def start_ios_rsd(self, rsd_ip: Optional[str] = None, rsd_port: Optional[str] = None) -> None:
        """
        iOS 完整通道模式。
        - 有給 rsd_ip/rsd_port：用手動抄下來的 --rsd IP PORT 連線
        - rsd_ip 留 None：改用 --tunnel ''，讓 pymobiledevice3 向背景執行的 tunneld 服務自動要一條可用通道
          （這個模式要先另外啟動 tunneld，見 IosDeviceScanner.start_tunneld()）
        """
        self._ensure_loop()
        self._cancel_current_worker()
        self._set_state(ConnectionState.CONNECTING)
        self._active_transport = Transport.IOS_RSD
        self._rsd_ip = rsd_ip
        self._rsd_port = rsd_port
        self._worker_future = asyncio.run_coroutine_threadsafe(
            self._run_worker(self._worker_ios_rsd(rsd_ip, rsd_port)), self._loop
        )

    async def _run_worker(self, worker_coro) -> None:
        try:
            await worker_coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 這裡刻意攔截所有例外，轉成友善訊息回報給 GUI
            self._last_error = self._friendly_error(e)
            logger.error("worker 發生錯誤: %s", self._last_error)
            self._set_state(ConnectionState.ERROR)

    # ---------------- Android worker ----------------

    async def _worker_android(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            ok, msg = await loop.run_in_executor(None, self._check_android_status)
            if ok:
                self._set_state(ConnectionState.CONNECTED)
                break
            self._last_error = msg
            await asyncio.sleep(2)

        assert self._cmd_queue is not None
        while True:
            cmd = await self._cmd_queue.get()
            if cmd is None:
                break
            lat, lng = cmd
            self._busy = True
            try:
                await loop.run_in_executor(None, self._send_location_android, lat, lng)
            finally:
                self._busy = False

    def _check_android_status(self) -> tuple[bool, str]:
        try:
            output = _check_output(self._adb_base_cmd() + ["devices"], text=True, encoding="utf-8", errors="replace", stderr=subprocess.STDOUT)
            lines = output.strip().split("\n")[1:]
            if self.android_serial:
                # 有指定序號：要確認「這一支」有在清單裡且狀態正常，不能只看「隨便一支有連線」，
                # 不然雙裝置情境下，明明是另一支手機連線正常，卻誤判成自己這支也連線正常
                for line in lines:
                    if line.startswith(self.android_serial) and "device" in line and "offline" not in line:
                        return True, f"Android 已連線 ({self.android_serial})"
                return False, f"未連線 ({self.android_serial})"
            for line in lines:
                if "device" in line and "offline" not in line:
                    return True, "Android 已連線"
            return False, "未連線"
        except Exception:
            return False, "找不到 ADB"

    def _send_location_android(self, lat: float, lng: float) -> None:
        """對應原本 FakeGPSCore.set_location_android：保留「App在背景就自動改前景重試」的邏輯"""
        cmd = self._adb_base_cmd() + [
            "shell", "am", "startservice",
            "-a", "com.example.mymocklocation.UPDATE",
            "--es", "lat", str(lat),
            "--es", "lng", str(lng),
            "com.example.mymocklocation/.MockLocationService",
        ]
        result = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        used_foreground_retry = False
        if result.stderr and "Error:" in result.stderr:
            if "background" in result.stderr.lower():
                used_foreground_retry = True
                logger.warning("Android App 目前在背景，正在改用 start-foreground-service 重試...")
                cmd_fg = cmd.copy()
                # 用「找到 startservice 這個字的位置再替換」取代寫死索引位置，
                # 因為前面加上 -s 序號 之後，指令陣列的元素位置會往後移，寫死索引會抓錯
                startservice_idx = cmd_fg.index("startservice")
                cmd_fg[startservice_idx] = "start-foreground-service"
                result_fg = _run(cmd_fg, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if result_fg.stderr and "Error:" in result_fg.stderr:
                    raise RuntimeError("app is in background")
            else:
                raise RuntimeError(result.stderr.strip())
        # 注意：這裡的「成功」只代表 adb 指令本身沒有回報錯誤（component 有收到 intent），
        # 不能保證 App 內部真的正確處理、實際套用了新座標——這一層 PC 端沒辦法驗證，
        # 要靠手機畫面上直接觀察定位有沒有動來最終確認
        tag = "（走了背景轉前景重試）" if used_foreground_retry else ""
        logger.info("✅ Android 定位指令執行成功%s：(%s, %s)", tag, lat, lng)

    # ---------------- iOS worker（免通道） ----------------

    async def _worker_ios_userspace(self) -> None:
        """
        目前用每次重新連線的方式送座標（透過命令列，確認在各版本 pymobiledevice3 都能動，
        缺點是速度較慢）。曾經嘗試改成建立一條持久連線重複使用，但 pymobiledevice3 內部
        DVT 連線的模組路徑在不同版本間變動很大（例如 9.36.0 版就沒有 dvt_secure_socket_proxy
        這個模組了），與其每次都用猜的去對版本，先用這個穩定可動的版本。
        """
        loop = asyncio.get_running_loop()
        self._set_state(ConnectionState.CONNECTED)
        assert self._cmd_queue is not None
        while True:
            cmd = await self._cmd_queue.get()
            if cmd is None:
                break
            # 佇列堆積時（上一個指令還沒送完，巡邏又排了新座標進來），
            # 只送最新的一個，中間過時的座標直接丟棄，避免越堆越多、巡邏卡住不動
            while not self._cmd_queue.empty():
                nxt = self._cmd_queue.get_nowait()
                if nxt is None:
                    cmd = None
                    break
                cmd = nxt
            if cmd is None:
                break
            lat, lng = cmd
            self._busy = True
            try:
                await loop.run_in_executor(None, self._send_location_ios_userspace_subprocess, lat, lng)
            except Exception as e:
                self._last_error = self._friendly_error(e)
                logger.warning("iOS 免通道送定位失敗: %s", self._last_error)
                # 失敗（尤其是逾時）代表裝置端的連線可能還沒完全釋放，緊接著馬上重連
                # 很容易再次卡住撞逾時；巡邏會不斷排新座標進來，這裡刻意先讓連線緩一下，
                # 比起立刻重試更容易恢復正常
                await asyncio.sleep(1.5)
            finally:
                self._busy = False

    def _send_location_ios_userspace_subprocess(self, lat: float, lng: float) -> None:
        cmd = [
            *_pymobiledevice3_cmd_prefix(), "developer", "dvt", "simulate-location", "set",
            "--userspace", "--", str(lat), str(lng),
        ]
        try:
            result = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError("iOS 免通道定位指令逾時（30秒內沒有回應）")
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        # 實測發現：手機沒開啟開發者模式時，這個指令 exit code 還是 0（看起來像成功），
        # 但實際上手機的定位完全沒有變化——只看 returncode 會誤判成功，改成連內容
        # 一起檢查，跟 enable_developer_mode() 用同一套「印出 ERROR 字樣就當失敗」的判斷
        if result.returncode != 0 or "ERROR" in output:
            raise RuntimeError(output or "iOS 免通道定位指令失敗")
        logger.info("✅ iOS 免通道定位指令執行成功：(%s, %s)。指令輸出：%s", lat, lng, output[:200] or "（無輸出）")

    # ---------------- iOS worker（RSD 完整通道） ----------------

    async def _worker_ios_rsd(self, rsd_ip: Optional[str], rsd_port: Optional[str]) -> None:
        loop = asyncio.get_running_loop()
        self._set_state(ConnectionState.CONNECTED)
        assert self._cmd_queue is not None
        while True:
            cmd = await self._cmd_queue.get()
            if cmd is None:
                break
            while not self._cmd_queue.empty():
                nxt = self._cmd_queue.get_nowait()
                if nxt is None:
                    cmd = None
                    break
                cmd = nxt
            if cmd is None:
                break
            lat, lng = cmd
            self._busy = True
            try:
                await loop.run_in_executor(None, self._send_location_ios_rsd, rsd_ip, rsd_port, lat, lng)
            except Exception as e:
                self._last_error = self._friendly_error(e)
                logger.warning("iOS RSD 送定位失敗: %s", self._last_error)
                # 同上：失敗後先緩一下再讓迴圈撈下一筆，避免連線還沒釋放就立刻重試又卡住
                await asyncio.sleep(1.5)
            finally:
                self._busy = False

    def _send_location_ios_rsd(self, rsd_ip: Optional[str], rsd_port: Optional[str], lat: float, lng: float) -> None:
        base = [
            *_pymobiledevice3_cmd_prefix(), "developer", "dvt", "simulate-location", "set",
        ]
        if rsd_ip and rsd_port:
            base += ["--rsd", str(rsd_ip), str(rsd_port)]
        else:
            base += ["--tunnel", ""]  # 交給背景執行的 tunneld 自動選用通道
        base += ["--", str(lat), str(lng)]
        try:
            result = _run(base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError("RSD 定位指令逾時（30秒內沒有回應，可能是 tunneld 通道還沒準備好，或需要重新建立通道）")
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        # 同上：exit code 0 不代表真的成功，手機沒開發者模式時常常看起來成功但沒作用
        if result.returncode != 0 or "ERROR" in output:
            raise RuntimeError(output or "RSD 定位指令失敗")
        logger.info("✅ RSD 定位指令執行成功 (exit code 0)：(%s, %s)。指令輸出：%s", lat, lng, output[:200] or "（無輸出）")

    # ---------------- 對外：座標 / 清除 / 停止 ----------------

    def set_location(self, lat: float, lng: float) -> bool:
        """執行緒安全：GUI/巡邏/搖桿執行緒都可以直接呼叫"""
        if self._loop is None or self._cmd_queue is None:
            self._last_error = "engine not started"
            return False
        self._loop.call_soon_threadsafe(self._cmd_queue.put_nowait, (lat, lng))
        return True

    def clear_location(self) -> None:
        import threading
        transport = self._active_transport
        if transport == Transport.ANDROID_USB:
            threading.Thread(target=self._clear_android, daemon=True).start()
        elif transport == Transport.IOS_USERSPACE:
            threading.Thread(target=self._clear_ios_userspace, daemon=True).start()
        elif transport == Transport.IOS_RSD:
            threading.Thread(target=self._clear_ios_rsd, daemon=True).start()

    def _clear_android(self) -> None:
        cmd = self._adb_base_cmd() + ["shell", "am", "stopservice", "com.example.mymocklocation/.MockLocationService"]
        _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    def _clear_ios_userspace(self) -> None:
        cmd = [*_pymobiledevice3_cmd_prefix(), "developer", "dvt", "simulate-location", "clear", "--userspace"]
        _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    def _clear_ios_rsd(self) -> None:
        cmd = [*_pymobiledevice3_cmd_prefix(), "developer", "dvt", "simulate-location", "clear"]
        _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # ---------------- iOS GPX 路線播放（比逐點送座標快很多，適合連續巡邏移動） ----------------

    def start_ios_gpx_playback(self, gpx_path: str) -> bool:
        """
        啟動 pymobiledevice3 內建的 GPX 路線播放：整條路線一次性交給它連續播放，
        只需要一次連線，不用像逐點送座標那樣每一步都重新開行程、重新連線。
        對應指令：pymobiledevice3 developer dvt simulate-location play <gpx> --userspace

        注意：一定要持續讀取子行程的 stdout/stderr，不能放著不管。
        Popen 的管線緩衝區大小有限，子行程如果持續輸出（play 這種長時間執行的指令很可能會），
        沒人讀取，緩衝區塞滿後子行程的 write() 會直接卡住，導致整個播放看起來「卡住不動」，
        但其實是被我們自己晾在一旁沒去讀輸出造成的，不是 pymobiledevice3 本身壞掉。
        """
        self.stop_ios_gpx_playback()
        try:
            cmd = [
                *_pymobiledevice3_cmd_prefix(), "developer", "dvt", "simulate-location", "play",
                gpx_path, "--userspace",
            ]
            self._gpx_process = _popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
            )
            self._start_gpx_output_drain_threads()
            return True
        except Exception as e:
            self._last_error = f"GPX 播放啟動失敗: {e}"
            logger.error(self._last_error)
            return False

    def _start_gpx_output_drain_threads(self) -> None:
        """背景持續讀取 GPX 播放行程的 stdout/stderr 並記錄下來，避免管線塞滿卡死子行程"""
        import threading

        proc = self._gpx_process
        if proc is None:
            return

        def _drain(stream, log_fn, label):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    log_fn("GPX播放[%s]: %s", label, line.rstrip())
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc.stdout, logger.info, "out"), daemon=True).start()
        threading.Thread(target=_drain, args=(proc.stderr, logger.warning, "err"), daemon=True).start()

    def stop_ios_gpx_playback(self) -> None:
        """中止目前的 GPX 播放行程（暫停/停止巡邏時呼叫，才能真正讓手機停下來）"""
        if self._gpx_process is not None:
            try:
                self._gpx_process.terminate()
                self._gpx_process.wait(timeout=3)
            except Exception:
                try:
                    self._gpx_process.kill()
                except Exception:
                    pass
            self._gpx_process = None

    @property
    def gpx_playback_running(self) -> bool:
        return self._gpx_process is not None and self._gpx_process.poll() is None

    def stop(self) -> None:
        """優雅停止：讓 worker 處理完佇列裡剩下的指令再結束"""
        if self._loop is not None and self._cmd_queue is not None:
            self._loop.call_soon_threadsafe(self._cmd_queue.put_nowait, None)
        self._set_state(ConnectionState.STOPPED)
        self._active_transport = None

    def force_stop(self) -> None:
        """強制停止：直接取消 worker task，並中止還在跑的 GPX 播放行程（如果有的話）"""
        if self._worker_future is not None:
            self._worker_future.cancel()
            self._worker_future = None
        self.stop_ios_gpx_playback()
        self._set_state(ConnectionState.STOPPED)
        self._active_transport = None

    # ---------------- 狀態查詢 ----------------

    @property
    def is_alive(self) -> bool:
        return self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING)

    @property
    def is_connecting(self) -> bool:
        return self._state == ConnectionState.CONNECTING

    @property
    def active_transport(self) -> Optional[Transport]:
        return self._active_transport

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def busy(self) -> bool:
        """目前是不是正在實際送出一筆定位（還沒送完）。巡邏迴圈可以用這個判斷要不要先等一下再推進下一步。"""
        return self._busy

    @staticmethod
    def _friendly_error(e: Exception) -> str:
        msg = str(e)
        if "background" in msg.lower():
            return "【手機 App 未開啟】請把手機上的 Mock Location App 打開並保持在畫面最前台！"
        return msg


# ============================================================
# AndroidDeviceScanner：Android 裝置探索
# ============================================================
class AndroidDeviceScanner:
    """負責「找到裝置、決定用哪種方式連」，不負責送座標（那是 GpsEngine 的事）"""

    def __init__(self, adb_path: str = r"C:\adb\adb.exe") -> None:
        self.adb_path = adb_path

    def list_usb_devices(self) -> list[DeviceInfo]:
        try:
            output = _check_output([self.adb_path, "devices"], text=True, encoding="utf-8", errors="replace", stderr=subprocess.STDOUT)
        except Exception as e:
            logger.warning("ADB 裝置列表查詢失敗: %s", e)
            return []

        devices = []
        for line in output.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                devices.append(DeviceInfo(
                    identifier=serial,
                    display_name=self._friendly_device_name(serial),
                    transport=Transport.ANDROID_WIFI if ":" in serial else Transport.ANDROID_USB,
                ))
        return devices

    def _friendly_device_name(self, serial: str) -> str:
        """
        查這台裝置的品牌+型號，組成「品牌 型號 (序號)」這種好分辨的顯示名稱，
        例如「samsung SM-S928B (R58N12345678)」，不然光看序號完全分不出是哪一支實體手機。
        查詢失敗（例如裝置剛拔線）就退回單純顯示序號，不影響清單本身。
        """
        try:
            manufacturer = _check_output(
                [self.adb_path, "-s", serial, "shell", "getprop", "ro.product.manufacturer"],
                text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=3,
            ).strip()
            model = _check_output(
                [self.adb_path, "-s", serial, "shell", "getprop", "ro.product.model"],
                text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=3,
            ).strip()
            if manufacturer or model:
                return f"{manufacturer} {model} ({serial})".strip()
        except Exception:
            pass
        return serial

    def try_connect_wifi(self, ip_port: str, timeout: float = 5.0) -> bool:
        """嘗試用 `adb connect ip:port` 連上 ADB over WiFi 的裝置"""
        try:
            result = _run(
                [self.adb_path, "connect", ip_port],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
            return "connected" in result.stdout.lower()
        except Exception as e:
            logger.warning("ADB WiFi 連線失敗 (%s): %s", ip_port, e)
            return False

    async def health_check_loop(self, on_change, interval: float = 5.0) -> None:
        """
        背景健康檢查：定期確認裝置是否還連著，狀態改變時呼叫 on_change(bool, str)。
        用法：asyncio.create_task(scanner.health_check_loop(callback))
        """
        last_ok = None
        while True:
            devices = await asyncio.get_running_loop().run_in_executor(None, self.list_usb_devices)
            ok = len(devices) > 0
            if ok != last_ok:
                on_change(ok, f"偵測到 {len(devices)} 台裝置" if ok else "裝置已斷線")
                last_ok = ok
            await asyncio.sleep(interval)


# ============================================================
# IosDeviceScanner：iOS 裝置探索
# ============================================================
class IosDeviceScanner:
    def __init__(self) -> None:
        self._tunneld_process: Optional[subprocess.Popen] = None

    def list_usb_devices(self) -> list[DeviceInfo]:
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "usbmux", "list"]
            output = _check_output(cmd, text=True, encoding="utf-8", errors="replace", stderr=subprocess.STDOUT)
        except Exception as e:
            logger.warning("iOS usbmux 查詢失敗: %s", e)
            return []

        if "UniqueDeviceID" not in output:
            return []
        return [DeviceInfo(identifier="ios-usb-0", display_name="iOS (USB)", transport=Transport.IOS_USERSPACE)]

    def check_rsd_reachable(self, rsd_ip: str, rsd_port: str, timeout: float = 3.0) -> bool:
        """簡易確認 RSD 服務是否可連（開一個 socket 測試，不做完整 handshake）"""
        import socket
        try:
            with socket.create_connection((rsd_ip, int(rsd_port)), timeout=timeout):
                return True
        except Exception as e:
            logger.warning("RSD 服務無法連線 (%s:%s): %s", rsd_ip, rsd_port, e)
            return False

    def reveal_developer_mode(self) -> tuple[bool, str]:
        """
        只讓「開發者模式」這個選項在手機的「設定 > 隱私權與安全性」裡出現，
        不會嘗試真的打開它，也不會檢查/受手機密碼狀態影響。
        對應指令：pymobiledevice3 amfi reveal-developer-mode

        這是有設定密碼的手機唯一可行的路線：AMFI 只在「直接打開」(enable) 這個
        動作上檢查密碼狀態，「顯示選項」(reveal) 沒有這個限制。跑完這個指令後，
        還是要使用者自己到手機「設定 > 隱私權與安全性 > 開發者模式」手動點開，
        照畫面提示重新開機、重開後再次確認——這一段沒辦法自動化，但至少不用先
        關掉手機密碼。
        """
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "amfi", "reveal-developer-mode"]
            result = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.returncode == 0 and "ERROR" not in output:
                return True, (
                    "✅ 已在手機的「設定 > 隱私權與安全性」裡顯示開發者模式選項。\n"
                    "請到手機上手動點開「開發者模式」，照畫面提示重新開機，\n"
                    "重開機後再次確認開啟即可（這一步需要你本人在手機上操作）。"
                )
            return False, output or "顯示開發者模式選項失敗，原因不明"
        except subprocess.TimeoutExpired:
            return False, "指令逾時（30秒內沒有回應）"
        except Exception as e:
            return False, str(e)

    def enable_developer_mode(self) -> tuple[bool, str]:
        """
        觸發 iOS 開發者模式「完整自動」啟用流程（打開＋等重開機＋自動確認）。
        對應指令：pymobiledevice3 amfi enable-developer-mode

        重要限制（Apple 刻意設計在 AMFI 服務裡的檢查，沒辦法繞過，實測過會直接
        擋下來，不是「先跳過去、重開機後再要求輸入」）：
        - 手機目前「有」設定螢幕鎖定密碼：AMFI 直接拒絕，回報
          "Device has a passcode set"，指令連重開機都不會觸發。這種情況請改用
          reveal_developer_mode()，不用關密碼也能繼續。
        - 手機「沒有」設定密碼：指令才會真的觸發裝置重新開機，pymobiledevice3
          會自動等待手機重新連上再送出確認，整個過程含重開機通常要一兩分鐘，
          過程中手機畫面上還是會跳出開發者模式確認提示，需要使用者本人在手機上
          點一下確認——這一步是 Apple 要求必須有實體人員操作的安全機制，任何
          工具都無法自動化。
        """
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "amfi", "enable-developer-mode"]
            # 沒設密碼的情況下，這個指令要等裝置重開機、重新連上 USB 才會回來，
            # 實測含重開機通常要一兩分鐘，30秒太短，拉長到180秒給足夠的緩衝時間
            result = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            # 注意：pymobiledevice3 有些失敗情況會把錯誤訊息印成 ERROR 等級的日誌，
            # 但整個指令的 exit code 還是 0（正常結束），不能只看 exit code 判斷成功與否
            looks_like_error = "ERROR" in output
            if result.returncode == 0 and not looks_like_error:
                detail = output if output else "（指令沒有印出任何內容）"
                return True, f"指令執行完成 (exit code 0)。實際輸出內容：\n{detail}"
            if "Device has a passcode set" in output or "DeviceHasPasscodeSetError" in output:
                return False, (
                    "❌ 手機目前有設定螢幕鎖定密碼，Apple 不允許在有密碼的狀態下\n"
                    "用這個「自動啟用」指令開發者模式（這是 AMFI 服務本身的限制，不是本程式的問題）。\n"
                    "請改用「顯示開發者模式選項」按鈕，不用關密碼也能繼續，\n"
                    "之後自己到手機「設定 > 隱私權與安全性」手動點開即可。"
                )
            return False, output or "啟用失敗，原因不明"
        except subprocess.TimeoutExpired:
            return False, "指令逾時（180秒內沒有回應），手機可能正在重開機，請稍後在手機上確認開發者模式提示，或直接到手機設定裡確認開發者模式的狀態"
        except Exception as e:
            return False, str(e)

    # ---------------- WiFi / mDNS 探索（透過 pymobiledevice3 內建功能） ----------------

    def enable_wifi_connections(self) -> tuple[bool, str]:
        """
        一次性設定：允許這台裝置之後可以透過 WiFi 被連線（需要先用 USB 接上裝置執行一次）。
        對應手動指令：pymobiledevice3 lockdown wifi-connections --state on

        注意：早期版本的 pymobiledevice3 這個子指令吃「位置參數」(... wifi-connections on)，
        目前安裝的版本（實測 10.2.3）已經改成 --state 選項，用舊語法會直接被拒絕
        （「Got unexpected extra argument(s) (on)」），這裡照目前版本的語法呼叫。
        """
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "lockdown", "wifi-connections", "--state", "on"]
            result = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            if result.returncode == 0:
                return True, "已允許此裝置透過 WiFi 連線"
            return False, (result.stderr or result.stdout).strip()
        except Exception as e:
            return False, str(e)

    def browse_devices(self, timeout: float = 5.0) -> dict:
        """
        用 pymobiledevice3 內建的 mDNS/Bonjour 掃描找 USB 跟 WiFi 上的 iOS 裝置。
        對應手動指令：pymobiledevice3 remote browse
        回傳格式：{"usb": [...], "wifi": [...]}，失敗回傳兩個空清單。
        """
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "remote", "browse",
                   "--timeout", str(timeout)]
            output = _check_output(cmd, text=True, encoding="utf-8", errors="replace", stderr=subprocess.STDOUT, timeout=timeout + 10)
            import json
            return json.loads(output)
        except Exception as e:
            logger.warning("mDNS 裝置探索失敗: %s", e)
            return {"usb": [], "wifi": []}

    def start_tunneld(self, host: str = "127.0.0.1", port: int = 49151) -> tuple[bool, str]:
        """
        啟動 tunneld 背景常駐服務：會持續透過 USB/WiFi/mDNS 自動偵測裝置並自動建立通道。
        啟動後，其他 pymobiledevice3 指令可以直接用 --tunnel '' 自動選用，不用手動抄 RSD IP/Port。
        注意：這個服務需要系統管理員權限才能建立 TUN 網路介面，Windows 上要「以系統管理員身分執行」。
        """
        if self._tunneld_process is not None and self._tunneld_process.poll() is None:
            return True, "tunneld 已經在執行中"
        try:
            cmd = [*_pymobiledevice3_cmd_prefix(), "remote", "tunneld",
                   "--host", host, "--port", str(port)]
            self._tunneld_process = _popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
            )
            return True, f"tunneld 已啟動於 {host}:{port}（需要用系統管理員權限執行本程式才會成功）"
        except Exception as e:
            return False, str(e)

    def stop_tunneld(self) -> None:
        if self._tunneld_process is not None:
            self._tunneld_process.terminate()
            self._tunneld_process = None

    @property
    def tunneld_running(self) -> bool:
        return self._tunneld_process is not None and self._tunneld_process.poll() is None


class GoIosBridge:
    """
    選用的 go-ios 橋接：go-ios 是另一套獨立的 Go 語言工具（執行檔通常叫 ios / ios.exe），
    不是 Python 套件，這支程式不會幫你安裝它。
    如果你電腦上已經有裝好 go-ios 並加進 PATH，這裡的方法才會真的動作；
    沒裝的話 is_available 會是 False，其他方法回傳空結果，不會假裝有找到裝置。
    """

    def __init__(self, ios_binary: str = "ios") -> None:
        self.ios_binary = ios_binary

    @property
    def is_available(self) -> bool:
        try:
            _run([self.ios_binary, "version"], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def list_devices(self) -> list[DeviceInfo]:
        if not self.is_available:
            logger.warning("go-ios 未安裝或不在 PATH 裡，略過")
            return []
        try:
            import json
            output = _check_output(
                [self.ios_binary, "list", "--details"], text=True, encoding="utf-8", errors="replace", timeout=10
            )
            data = json.loads(output)
            devices = []
            for entry in data.get("deviceList", []):
                udid = entry.get("Udid") or entry.get("udid", "unknown")
                devices.append(DeviceInfo(
                    identifier=udid, display_name=f"iOS ({udid}) via go-ios",
                    transport=Transport.IOS_USERSPACE, extra={"source": "go-ios"},
                ))
            return devices
        except Exception as e:
            logger.warning("go-ios 裝置列表查詢失敗: %s", e)
            return []