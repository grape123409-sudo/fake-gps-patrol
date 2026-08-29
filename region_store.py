# ==========================================
# 檔案名稱：region_store.py
# 說明：已存區域清單（多組已完成多邊形的存/讀），逐行移植自 Tkinter 版
#       my_fake_gps.py 的 _regions_file_path() / _load_regions_file() / _save_regions_file()。
#       手機A、手機B 各自存成獨立的 JSON 檔案（saved_regions_a.json / saved_regions_b.json），
#       跟 Tkinter 版共用同一套檔案格式，兩邊存的清單檔案可以互相讀取。
# ==========================================
from __future__ import annotations

import json
import os

LatLng = tuple[float, float]


def regions_file_path(app_dir: str, slot: str) -> str:
    suffix = "a" if slot == "A" else "b"
    return os.path.join(app_dir, f"saved_regions_{suffix}.json")


def load_regions(app_dir: str, slot: str) -> dict[str, list[list[LatLng]]]:
    path = regions_file_path(app_dir, slot)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_regions(app_dir: str, slot: str, data: dict[str, list[list[LatLng]]]) -> None:
    path = regions_file_path(app_dir, slot)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
