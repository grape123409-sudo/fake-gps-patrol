# ==========================================
# 檔案名稱：routes_store.py
# 說明：已存路線（含產生器設定、座標、執行模式）存讀，跟 favorites_store.py
#       一樣是單純的 JSON 存讀，刻意獨立於 Tkinter/PySide6，方便單獨測試。
#
# 資料格式：list[dict]，每筆：
#   name: str, category: str（預設「未分類」）,
#   generator_type: "manual"/"flower"/"jump"/"concentric",
#   kind: "points"（純座標）/"steps"（帶等待秒數的 RouteStep 序列）,
#   points: list[[lat,lng]]（kind=points）或 list[[lat,lng,wait_s]]（kind=steps）,
#   run_mode: str, repeat_count: int, speed_kmh: float,
#   settings: dict（各產生器自己的設定，用於「載入回產生器面板」時還原欄位）
# ==========================================
from __future__ import annotations

import json
import os

ROUTES_FILE_NAME = "routes.json"


def _routes_file_path(app_dir: str) -> str:
    return os.path.join(app_dir, ROUTES_FILE_NAME)


def load_routes_from(path: str) -> list[dict]:
    """從指定路徑載入已存路線清單，檔案不存在或格式錯誤都安全回傳空清單"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict) and "name" in d and "points" in d]
    except Exception:
        return []


def save_routes_to(path: str, routes: list[dict]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 存檔失敗不影響當次執行期間的清單，只是下次重開會遺失


def load_routes(app_dir: str) -> list[dict]:
    return load_routes_from(_routes_file_path(app_dir))


def save_routes(app_dir: str, routes: list[dict]) -> None:
    save_routes_to(_routes_file_path(app_dir), routes)
