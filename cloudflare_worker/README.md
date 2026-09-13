# 團體種花房間伺服器（Cloudflare Worker）

這個資料夾是「團體種花」用的房間伺服器原始碼。**目前還沒有部署**，
桌面程式那邊做好了但要填入伺服器網址才能用。

## 這是什麼

一個房間 = 一個 Durable Object 實例。房主把自己的座標送上來，伺服器廣播給
房間裡所有團員，讓大家的手機停在同一個座標上；每跑完一輪還會等所有人跟上
（同步屏障）才繼續，避免有人落後。

## 費用

用的是 **SQLite-backed Durable Object**，可以跑在 Workers **免費方案**上
（免費額度：每天 10 萬次請求、13,000 GB-s 執行時間）。幾個朋友自己用完全夠。

程式有兩個地方特別為了省額度設計：

- 用 WebSocket Hibernation API，房間閒置時 Durable Object 會休眠不計時，
  連線本身不會斷。
- 房主的即時座標只做轉發、完全不寫進儲存空間，不會吃掉每天的寫入額度。

## 部署步驟

1. 安裝 wrangler（需要 Node.js）：

```bash
npm install -g wrangler
```

2. 登入你的 Cloudflare 帳號：

```bash
wrangler login
```

3. 在這個資料夾裡部署：

```bash
wrangler deploy
```

4. 部署完成後 wrangler 會印出網址，長得像
   `https://group-planting.<你的帳號名>.workers.dev`。
   把這個網址填進桌面程式「👥 團體種花」分頁的「伺服器網址」欄位即可
   （程式會自己把 `https://` 換成 `wss://` 去接 WebSocket）。

5. 確認有活著：瀏覽器打開 `https://<你的網址>/api/group/health`，
   看到 `{"ok":true,...}` 就是好了。

如果你想掛在自己的網域底下（例如已經在用的那個網域），在 Cloudflare 後台
Workers Routes 綁一個路由指到這個 Worker 就行，桌面程式那邊填你自己的網域。

## API

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/api/group/health` | 健康檢查 |
| POST | `/api/group/create` | 建立房間，回傳一次性連線 token |
| POST | `/api/group/join` | 加入房間（驗房間密碼），回傳一次性連線 token |
| GET | `/api/group/socket?room=&token=` | WebSocket 連線 |

房號限制 3–16 位大寫英數字 / `_` / `-`，房間人數上限 2–20 人，
房間密碼至少 4 個字、用 PBKDF2-SHA256 加鹽雜湊後才存（不存明文）。

### WebSocket 訊息

送給伺服器：`hello`、`ping`、`ready`、`settings`(房主)、`command`(房主：
start/pause/resume/stop)、`progress`(房主)、`barrier`(房主)、`barrier_ack`(團員)

伺服器送出：`snapshot`（房間完整狀態）、`progress`（房主座標）、`barrier`、
`release`（同步屏障放行）、`command_ack`、`pong`、`error`

## 已知取捨

- 規格書寫房間密碼用 Argon2id，但 Cloudflare Workers 的 WebCrypto 沒有內建
  Argon2，這裡改用 PBKDF2-SHA256（12 萬次迭代）。對「朋友之間的房間密碼」
  這個用途足夠。
- 房間的「執行階段/準備狀態」是輕量狀態：Durable Object 如果在**閒置**時被
  回收，重連後房間會回到待機、團員要重新按一次準備。執行中因為座標訊息一直
  在流動不會被回收，所以不影響跑路線。
- 沒有做帳號系統，進房間只靠房號 + 房間密碼。房號請不要用容易猜到的字。
