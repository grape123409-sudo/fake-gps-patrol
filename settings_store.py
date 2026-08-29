# ==========================================
# 檔案名稱：settings_store.py
# 說明：設定值存/讀，取代 Tkinter 版 my_fake_gps.py 的 PERSISTED_ENTRY_FIELDS +
#       load_saved_settings() / save_current_settings()。
#
# 跟 Tkinter 版不同的地方：Tkinter 版要手動列一份「哪些欄位要存」的清單，
# PySide6 版改成「只要幫欄位設定 objectName()，這裡就會自動掃到、自動存讀」，
# 不用另外維護一份清單，新增欄位只要記得設 objectName 就會自動生效
# （這也是使用者這次要求的「所有能key值的跟打勾的設定值都要保留」——包含
# Tkinter 版原本沒做的核取方塊/單選鈕，這裡一併處理）。
#
# 支援的元件類型：QLineEdit（文字）、QComboBox（目前選到的文字）、
# QCheckBox / QRadioButton（勾選狀態）、QSpinBox / QDoubleSpinBox（數值）、
# QSlider（數值）。沒有設定 objectName 的元件會被忽略，不會被存到檔案裡
# ——一次性動作用的欄位（例如手動輸入座標飛過去那個欄位）刻意不設定
# objectName，這樣就自動被排除，不用另外维護排除清單。
# ==========================================
from __future__ import annotations

import json
import os
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QRadioButton, QSlider, QSpinBox, QWidget,
)


def _is_own_name(name: str) -> bool:
    """
    排除 Qt 內部元件自己的預設 objectName（例如 QSpinBox/QDoubleSpinBox 內部
    都自帶一個子 QLineEdit，預設 objectName 固定是 "qt_spinbox_lineedit"——
    不是我們自己設的，而且每一個 spinbox 的這個內部子元件都同名，掃描到的話
    會互相蓋掉、還原時也會被誤套用。Qt 內部元件的命名一律以 "qt_" 開頭，直接排除。
    """
    return bool(name) and not name.startswith("qt_")


def collect(root: QWidget) -> dict[str, Any]:
    """掃描 root 底下所有有設定 objectName 的可設定元件，收集成一份 dict"""
    data: dict[str, Any] = {}

    for w in root.findChildren(QLineEdit):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.text()
    for w in root.findChildren(QComboBox):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.currentText()
    for w in root.findChildren(QCheckBox):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.isChecked()
    for w in root.findChildren(QRadioButton):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.isChecked()
    for w in root.findChildren(QSpinBox):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.value()
    for w in root.findChildren(QDoubleSpinBox):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.value()
    for w in root.findChildren(QSlider):
        if _is_own_name(w.objectName()):
            data[w.objectName()] = w.value()

    return data


def apply(root: QWidget, data: dict[str, Any]) -> None:
    """把之前存的 dict 套用回對應 objectName 的元件。單一元件還原失敗不影響其他元件。"""
    for w in root.findChildren(QLineEdit):
        name = w.objectName()
        if name in data:
            try:
                w.setText(str(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QComboBox):
        name = w.objectName()
        if name in data:
            try:
                w.setCurrentText(str(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QCheckBox):
        name = w.objectName()
        if name in data:
            try:
                w.setChecked(bool(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QRadioButton):
        name = w.objectName()
        if name in data:
            try:
                w.setChecked(bool(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QSpinBox):
        name = w.objectName()
        if name in data:
            try:
                w.setValue(int(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QDoubleSpinBox):
        name = w.objectName()
        if name in data:
            try:
                w.setValue(float(data[name]))
            except Exception:
                pass
    for w in root.findChildren(QSlider):
        name = w.objectName()
        if name in data:
            try:
                w.setValue(int(data[name]))
            except Exception:
                pass


def load_file(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取上次設定值失敗（略過，使用預設值）: {e}")
        return {}


def save_file(path: str, data: dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存設定值失敗: {e}")
