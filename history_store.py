# ==========================================
# 檔案名稱：history_store.py
# 說明：路線/單點執行歷史存讀，只記錄「按下開始/直飛」這個動作本身，
#       最新的排最前面，上限 200 筆（超過自動裁切最舊的）。
#
# 資料格式：list[dict]，每筆 {"name": str, "kind": str, "lat": float|None,
#           "lng": float|None, "timestamp": "YYYY-MM-DD HH:MM:SS"}
#           list 本身就是「新到舊」排序，index 0 是最新一筆。
# ==========================================
from __future__ import annotations

import json
import os

HISTORY_FILE_NAME = "history.json"
MAX_ENTRIES = 200


def _history_file_path(app_dir: str) -> str:
    return os.path.join(app_dir, HISTORY_FILE_NAME)


def load_history(app_dir: str) -> list[dict]:
    path = _history_file_path(app_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict) and "name" in d][:MAX_ENTRIES]
    except Exception:
        return []


def save_history(app_dir: str, entries: list[dict]) -> None:
    try:
        with open(_history_file_path(app_dir), "w", encoding="utf-8") as f:
            json.dump(entries[:MAX_ENTRIES], f, ensure_ascii=False, indent=2)
    except Exception:
        pass
