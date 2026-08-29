# ==========================================
# 檔案名稱：speed_presets_store.py
# 說明：巡邏「常用時速」清單存讀，單純的 JSON 存讀模組。
# ==========================================
from __future__ import annotations

import json
import os

SPEED_PRESETS_FILE_NAME = "speed_presets.json"
DEFAULT_PRESETS = [5.0, 10.0, 20.0, 30.0, 50.0]


def _file_path(app_dir: str) -> str:
    return os.path.join(app_dir, SPEED_PRESETS_FILE_NAME)


def load_speed_presets(app_dir: str) -> list[float]:
    path = _file_path(app_dir)
    if not os.path.exists(path):
        return list(DEFAULT_PRESETS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return sorted(float(x) for x in data)
    except Exception:
        pass
    return list(DEFAULT_PRESETS)


def save_speed_presets(app_dir: str, presets: list[float]) -> None:
    try:
        with open(_file_path(app_dir), "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
