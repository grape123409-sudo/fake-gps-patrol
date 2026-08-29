# ==========================================
# 檔案名稱：favorites_store.py
# 說明：收藏夾（常用座標點）存讀，跟 region_store.py 一樣是單純的 JSON 存讀，
#       刻意獨立於 Tkinter/PySide6，方便單獨測試。
#
# 資料格式：list[dict]，每筆 {"name": str, "lat": float, "lng": float}
# ==========================================
from __future__ import annotations

import json
import os

FAVORITES_FILE_NAME = "favorites.json"


def _favorites_file_path(app_dir: str) -> str:
    return os.path.join(app_dir, FAVORITES_FILE_NAME)


def load_favorites_from(path: str) -> list[dict]:
    """從指定路徑載入收藏清單，檔案不存在或格式錯誤都安全回傳空清單"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict) and "lat" in d and "lng" in d]
    except Exception:
        return []


def save_favorites_to(path: str, favorites: list[dict]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(favorites, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 存檔失敗不影響當次執行期間的收藏清單，只是下次重開會遺失


def load_favorites(app_dir: str) -> list[dict]:
    return load_favorites_from(_favorites_file_path(app_dir))


def save_favorites(app_dir: str, favorites: list[dict]) -> None:
    save_favorites_to(_favorites_file_path(app_dir), favorites)
