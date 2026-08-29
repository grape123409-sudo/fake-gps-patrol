# ==========================================
# 檔案名稱：main.py
# 說明：PySide6 版進入點。對應 Tkinter 版 my_fake_gps.py 檔案最下面的 __main__ 區塊。
#
# 注意：這裡不需要像 Tkinter 版那樣手動呼叫 ctypes 設定 DPI 感知模式。
# PySide6 在 Windows 上啟動時會自動設成 DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
# （比手動呼叫 SetProcessDpiAwareness(2) 更新的版本），行程的 DPI 感知模式一旦設定過
# 就不能重設，我們自己再呼叫只會得到「存取被拒」的警告，沒有任何實際效果，故意不呼叫。
# ==========================================
import os
import sys

from PySide6.QtWidgets import QApplication

from gps_core import resolve_app_dir
from main_window import MainWindow

APP_DIR = resolve_app_dir()


def main() -> None:
    app = QApplication(sys.argv)
    try:
        with open(os.path.join(APP_DIR, "theme.qss"), "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except OSError as e:
        # 找不到 theme.qss 只會讓畫面變回 Qt 預設樣式，不影響功能，
        # 不值得為了這個直接讓整支程式打不開（例如打包時漏帶了這個檔案）
        print(f"⚠️ 找不到 theme.qss，改用預設樣式: {e}")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
