# ==========================================================
# 檔案名稱：group_client.py
# 說明：團體種花的房間連線客戶端。
#
# 用 PySide6 內建的 QWebSocket（PySide6-Addons 就有，不用另外裝套件），
# 它本身就掛在 Qt 事件迴圈上，訊息進來直接發 Qt signal，不需要自己開執行緒、
# 也不需要把訊息從別的執行緒搬回 UI 執行緒。
#
# 這一層只負責「講協定」：建房/加入房、連線、收送訊息、心跳、斷線重連，
# 並把結果轉成 Qt signal 往上丟。實際要拿座標做什麼（送去手機、跑路線）
# 由 main_window.py 決定，這個檔案不碰 GpsEngine，方便單獨測試。
# ==========================================================
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

HEARTBEAT_MS = 20000
HTTP_TIMEOUT = 8


class GroupClientError(Exception):
    pass


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "FakeGpsPatrol-Group/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 伺服器用 4xx 回傳「房號重複」「密碼錯」這種可以講給使用者聽的原因，
        # 這裡要把 body 讀出來，不能只丟狀態碼
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        message = detail.get("detail") or detail.get("error") or f"HTTP {e.code}"
        raise GroupClientError(message) from e
    except urllib.error.URLError as e:
        raise GroupClientError(f"連不到伺服器：{e.reason}") from e
    except Exception as e:
        raise GroupClientError(str(e)) from e


def normalize_base_url(raw: str) -> str:
    """使用者可能貼 https://xxx.workers.dev、wss://...、或結尾多一個斜線，
    這裡統一整理成沒有結尾斜線的 https 網址。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class GroupClient(QObject):
    """一次連一個房間。斷線會自動重連（除非是自己主動離開）。"""

    connectionChanged = Signal(bool)          # 是否已連上房間
    snapshotReceived = Signal(dict)           # 房間完整狀態
    progressReceived = Signal(dict)           # 房主座標（團員才會收到）
    barrierRequested = Signal(int)            # 房主到同步點，團員該回報
    barrierReleased = Signal(int)             # 同步屏障放行
    commandAck = Signal(dict)                 # 指令回覆
    logMessage = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.base_url = ""
        self.room_id = ""
        self.token = ""
        self.role = ""
        self._leaving = False

        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.textMessageReceived.connect(self._on_text_message)
        self._socket.errorOccurred.connect(self._on_error)

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_MS)
        self._heartbeat.timeout.connect(lambda: self.send({"type": "ping"}))

    # ---------- 房間 ----------

    @property
    def is_connected(self) -> bool:
        return self._socket.state() == QAbstractSocket.SocketState.ConnectedState

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    def create_room(self, base_url: str, room_id: str, password: str, name: str, max_members: int) -> None:
        self._begin(base_url, room_id)
        result = _post_json(f"{self.base_url}/api/group/create", {
            "roomId": self.room_id,
            "password": password,
            "name": name,
            "maxMembers": max_members,
        })
        self._finish_handshake(result)

    def join_room(self, base_url: str, room_id: str, password: str, name: str) -> None:
        self._begin(base_url, room_id)
        result = _post_json(f"{self.base_url}/api/group/join", {
            "roomId": self.room_id,
            "password": password,
            "name": name,
        })
        self._finish_handshake(result)

    def _begin(self, base_url: str, room_id: str) -> None:
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise GroupClientError("請先填伺服器網址")
        self.room_id = (room_id or "").strip().upper()
        if not self.room_id:
            raise GroupClientError("請填房號")
        self._leaving = False

    def _finish_handshake(self, result: dict) -> None:
        self.token = result.get("token") or ""
        self.role = result.get("role") or ""
        if not self.token:
            raise GroupClientError("伺服器沒有回傳連線 token")
        self._open_socket()

    def _socket_url(self) -> QUrl:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        host_part = self.base_url.split("://", 1)[1]
        query = urllib.parse.urlencode({"room": self.room_id, "token": self.token})
        return QUrl(f"{scheme}://{host_part}/api/group/socket?{query}")

    def _open_socket(self) -> None:
        if self._leaving or not self.token:
            return
        self._socket.open(self._socket_url())

    def leave(self) -> None:
        self._leaving = True
        self._heartbeat.stop()
        self._socket.close()
        self.token = ""
        self.role = ""

    # ---------- 送訊息 ----------

    def send(self, message: dict) -> None:
        if not self.is_connected:
            return
        self._socket.sendTextMessage(json.dumps(message))

    def set_ready(self, ready: bool) -> None:
        self.send({"type": "ready", "ready": bool(ready)})

    def sync_settings(self, settings: dict) -> None:
        self.send({"type": "settings", "settings": settings})

    def send_command(self, command: str) -> None:
        self.send({"type": "command", "command": command})

    def publish_progress(self, payload: dict) -> None:
        self.send({"type": "progress", "payload": payload})

    def announce_barrier(self, boundary: int) -> None:
        self.send({"type": "barrier", "boundary": int(boundary)})

    def ack_barrier(self, boundary: int) -> None:
        self.send({"type": "barrier_ack", "boundary": int(boundary)})

    # ---------- 事件 ----------

    def _on_connected(self) -> None:
        self._heartbeat.start()
        self.send({"type": "hello"})
        self.connectionChanged.emit(True)
        self.logMessage.emit(f"👥 已連上房間 {self.room_id}（身分：{'房主' if self.is_host else '團員'}）")

    def _on_disconnected(self) -> None:
        self._heartbeat.stop()
        self.connectionChanged.emit(False)
        if self._leaving:
            self.logMessage.emit("👥 已離開房間")
            return
        # 連線 token 是一次性的，重連要重新拿一組；這裡先通知使用者，
        # 由 UI 決定要不要重新加入（避免在背景一直用失效 token 敲伺服器）
        self.logMessage.emit("⚠️ 與房間的連線中斷了，請重新加入房間")

    def _on_error(self, _error) -> None:
        self.logMessage.emit(f"⚠️ 房間連線錯誤：{self._socket.errorString()}")

    def _on_text_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = msg.get("type")
        if kind == "snapshot":
            self.snapshotReceived.emit(msg.get("room") or {})
        elif kind == "progress":
            self.progressReceived.emit(msg.get("payload") or {})
        elif kind == "barrier":
            self.barrierRequested.emit(int(msg.get("boundary") or 0))
        elif kind == "release":
            self.barrierReleased.emit(int(msg.get("boundary") or 0))
        elif kind == "command_ack":
            self.commandAck.emit(msg)
        elif kind == "error":
            self.logMessage.emit(f"⚠️ 伺服器回報：{msg.get('message')}")


def boundary_index(repeat: int, flower_count: int, ring_count: int, ring: int, center: int) -> int:
    """規格書 §11.8 的同步屏障編號公式，用來標記「跑完第幾個花點」這個同步點。

    boundary = repeat × flowerCount × ringCount
             + (ring - 1) × flowerCount
             + center
             + 1
    repeat 從 0 起算（第幾輪），ring 從 1 起算（第幾圈），center 從 0 起算（第幾個花點）。
    """
    return repeat * flower_count * ring_count + (ring - 1) * flower_count + center + 1
