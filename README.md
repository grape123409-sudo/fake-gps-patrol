# Fake GPS Patrol（雙系統定位神盾 - 精簡版）

Windows 桌面工具，透過 USB / WiFi 對 Android、iOS 手機發送虛擬定位（假 GPS），
支援手動搖桿、弓字型自動巡邏、GPX 路徑匯入匯出、收藏夾等功能。

## 下載使用（不用裝 Python，直接執行）

到本倉庫的 **[Releases](../../releases)** 頁面下載最新版的 zip 檔，解壓縮後
雙擊 `FakeGpsPatrol.exe` 即可執行。

使用前請務必先閱讀壓縮包內的
[`使用前說明與免責聲明.txt`](./使用前說明與免責聲明.txt)，
裡面有 Android / iOS 各自的連線設定步驟與常見問題排解。

## 從原始碼執行 / 自行打包

```bash
pip install -r requirements.txt
python main.py
```

若要打包成給別人使用的獨立執行檔（不需要對方安裝 Python），執行：

```bash
build_release.bat
```

打包腳本會另外把 `pymobiledevice3` 也凍結成一支獨立執行檔一起附上，讓
iOS 功能在沒有安裝 Python 的電腦上也能使用；第一次執行會比較久（約
5-15 分鐘），之後若沒有升級 `pymobiledevice3` 版本，可以設定環境變數
`SKIP_PMD3=1` 跳過這一步。

Android 功能另外需要手動安裝定位助手 APK 並設為模擬定位 App，詳見
使用說明文件第五章。

## 授權與第三方套件

本專案程式碼僅供個人研究、測試與學習用途，免費使用，禁止轉賣或包裝成付費服務。

打包後的執行檔內附以下第三方工具，各自維持原本的授權條款：

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)（GPL-3.0）——
  用於 iOS 裝置的定位模擬與開發者模式控制。
- [Android Debug Bridge (adb)](https://developer.android.com/tools/adb)
  （Android Open Source Project）——用於 Android 裝置連線。

完整免責聲明見
[`使用前說明與免責聲明.txt`](./使用前說明與免責聲明.txt)。
