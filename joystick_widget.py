# ==========================================
# 檔案名稱：joystick_widget.py
# 說明：滑鼠拖曳式搖桿元件，補足原本鍵盤 WASD 搖桿之外的操作方式。
#
# 用法：拖曳期間持續發射 moved(dx, dy)，dx/dy 各自正規化到 -1.0~1.0，
# 代表搖桿頭目前偏移方向與強度；放開時搖桿頭彈回中心並發射 released()。
# 呼叫端拿到 (dx, dy) 後自行決定每個 tick 要移動多少距離，這裡不涉及
# 任何座標系統轉換，純粹是一個輸入元件。
# ==========================================
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPaintEvent, QPainter
from PySide6.QtWidgets import QWidget


class JoystickWidget(QWidget):
    moved = Signal(float, float)
    released = Signal()

    def __init__(self, parent=None, diameter: int = 140, emit_interval_ms: int = 50):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self._dragging = False
        self._knob_pos = self._center()
        self._current_dx = 0.0
        self._current_dy = 0.0

        self._emit_timer = QTimer(self)
        self._emit_timer.setInterval(emit_interval_ms)
        self._emit_timer.timeout.connect(self._emit_current)

    def _center(self) -> QPointF:
        return QPointF(self._diameter / 2, self._diameter / 2)

    def _max_radius(self) -> float:
        return self._diameter / 2 - 18  # 扣掉搖桿頭半徑，避免搖桿頭畫到外框上

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._update_knob(event.position())
            self._emit_timer.start()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self._update_knob(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._stop_dragging()

    def leaveEvent(self, event) -> None:
        # 拖曳中滑鼠離開元件範圍（例如拖太快甩出去）也要停止，避免搖桿頭卡在
        # 偏移位置、繼續無窮盡地發射移動事件
        if self._dragging:
            self._stop_dragging()
        super().leaveEvent(event)

    def _stop_dragging(self) -> None:
        self._dragging = False
        self._emit_timer.stop()
        self._knob_pos = self._center()
        self._current_dx = 0.0
        self._current_dy = 0.0
        self.update()
        self.released.emit()

    def _update_knob(self, pos: QPointF) -> None:
        center = self._center()
        vec = pos - center
        dist = math.hypot(vec.x(), vec.y())
        max_r = self._max_radius()
        if dist > max_r and dist > 0:
            vec = vec * (max_r / dist)
        self._knob_pos = center + vec
        self._current_dx = (vec.x() / max_r) if max_r > 0 else 0.0
        self._current_dy = (vec.y() / max_r) if max_r > 0 else 0.0
        self.update()

    def _emit_current(self) -> None:
        if self._dragging:
            self.moved.emit(self._current_dx, self._current_dy)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self._center()
        max_r = self._max_radius()

        painter.setBrush(QColor("#1e1e1e"))
        painter.setPen(QColor("#3a3a3a"))
        painter.drawEllipse(center, self._diameter / 2 - 2, self._diameter / 2 - 2)

        painter.setBrush(QColor("#2a2a2a"))
        painter.setPen(QColor("#444444"))
        painter.drawEllipse(center, max_r, max_r)

        knob_color = QColor("#ffd700") if self._dragging else QColor("#c5a059")
        painter.setBrush(knob_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self._knob_pos, 16, 16)
