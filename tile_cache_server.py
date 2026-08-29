# ==========================================
# 檔案名稱：tile_cache_server.py
# 說明：本機圖磚快取代理伺服器。取代 Tkinter 版 my_fake_gps.py 裡對 tkintermapview
#       做的 monkeypatch（那個是直接接管套件內部的 request_image 方法）。
#       QWebEngineView 裡的 Leaflet 沒有那種內部掛勾點可以攔截，改用「起一個
#       loopback-only 的本機 HTTP 伺服器」的方式：Leaflet 的 tileLayer 指向
#       http://127.0.0.1:<port>/tile/{z}/{x}/{y}.png，伺服器收到請求後：
#         1. 先查 SQLite 有沒有存過 -> 有就直接回傳，不連網路
#         2. 沒有 -> 向 Google 圖磚伺服器下載，回傳給瀏覽器的同時也寫回資料庫
#
# 資料庫 schema 跟 Tkinter 版共用（tiles/server 兩張表），兩邊的快取檔案理論上
# 可以互通，但預設各自存在自己資料夾下的 map_tile_cache.db，不共用同一個檔案，
# 避免兩個版本同時寫入同一份 SQLite 檔案造成鎖定衝突。
# ==========================================
from __future__ import annotations

import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TILE_SERVER_TEMPLATE = "https://mt0.google.com/vt/lyrs=m&hl=zh-TW&x={x}&y={y}&z={z}"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TileCacheServer"


class TileCacheServer:
    """
    只綁定 127.0.0.1，不對外開放，純粹給同一台電腦上的 QWebEngineView 讀取用。
    """

    def __init__(self, db_path: str, tile_server_template: str = _TILE_SERVER_TEMPLATE):
        self.db_path = db_path
        self.tile_server_template = tile_server_template
        self._lock = threading.Lock()
        self._init_db()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS server (
                    url VARCHAR(300) PRIMARY KEY NOT NULL, max_zoom INTEGER NOT NULL)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tiles (
                    zoom INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
                    server VARCHAR(300) NOT NULL, tile_image BLOB NOT NULL,
                    CONSTRAINT pk_tiles PRIMARY KEY (zoom, x, y, server))"""
            )
            conn.execute(
                "INSERT OR IGNORE INTO server (url, max_zoom) VALUES (?, ?)",
                (self.tile_server_template, 22),
            )
            conn.commit()
        finally:
            conn.close()

    def _read_cached(self, zoom: int, x: int, y: int) -> bytes | None:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute(
                    "SELECT tile_image FROM tiles WHERE zoom=? AND x=? AND y=? AND server=?",
                    (zoom, x, y, self.tile_server_template),
                )
                row = cur.fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    def _write_cache(self, zoom: int, x: int, y: int, data: bytes) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO tiles (zoom, x, y, server, tile_image) VALUES (?, ?, ?, ?, ?)",
                    (zoom, x, y, self.tile_server_template, data),
                )
                conn.commit()
            except Exception:
                pass  # 寫快取失敗不該影響圖磚照樣顯示，安全忽略
            finally:
                conn.close()

    def fetch_tile(self, zoom: int, x: int, y: int) -> bytes | None:
        cached = self._read_cached(zoom, x, y)
        if cached is not None:
            return cached
        url = self.tile_server_template.format(x=x, y=y, z=zoom)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
        except Exception:
            return None
        self._write_cache(zoom, x, y, data)
        return data

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """啟動背景伺服器，回傳實際監聽的 port（傳 0 代表讓系統自動選一個空的）"""
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 安靜模式，不要洗掉主程式的 log 輸出
                pass

            def handle_one_request(self):
                # 地圖快速拖曳/縮放時，瀏覽器常常會主動中止「已經不需要了」的圖磚請求，
                # 這是正常現象，不是伺服器出錯。BaseHTTPRequestHandler 預設會把這種連線
                # 中斷的例外印成一長串 traceback 到終端機，這裡整個攔截掉，安靜地忽略即可，
                # 不能讓某一次連線中斷就把整個背景伺服器執行緒帶掛掉。
                try:
                    super().handle_one_request()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass

            def do_GET(self):
                try:
                    self._serve_tile()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass

            def _serve_tile(self):
                parts = self.path.strip("/").split("/")
                # 預期路徑格式：tile/<z>/<x>/<y>.png
                if len(parts) != 4 or parts[0] != "tile":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    z = int(parts[1])
                    x = int(parts[2])
                    y = int(parts[3].split(".")[0])
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return

                data = server_self.fetch_tile(z, x, y)
                if data is None:
                    self.send_response(502)
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
