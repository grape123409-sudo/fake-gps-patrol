// ==========================================================
// 團體種花房間伺服器（Cloudflare Worker + Durable Object）
//
// 一個房間 = 一個 Durable Object 實例，房間裡所有人的 WebSocket 都接在
// 同一個實例上，房主送出的座標由這個實例廣播給全部團員。
//
// 用 SQLite-backed Durable Object（wrangler.toml 裡的 new_sqlite_classes），
// 這是目前唯一能在 Workers 免費方案上使用的 DO 型態。
//
// 使用 WebSocket Hibernation API（ctx.acceptWebSocket）：房間閒置時 DO 可以
// 休眠、不佔用執行時間額度，連線本身不會斷。休眠後記憶體會被清掉，所以
// 「成員身分與準備狀態」存在每條連線自己的 attachment 裡（跟著連線走，
// 休眠也不會掉），房間設定與階段另外寫進 DO storage。
// 房主的即時座標(progress)只做轉發、完全不落地，避免吃掉免費方案的寫入額度。
// ==========================================================

const PHASE_IDLE = "idle";
const PHASE_COUNTDOWN = "countdown";
const PHASE_RUNNING = "running";
const PHASE_PAUSED = "paused";

const COUNTDOWN_MS = 5000;
const ROOM_ID_RE = /^[A-Z0-9_-]{3,16}$/;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function randomHex(bytes) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// 房間密碼用 PBKDF2-SHA256 加鹽雜湊後才存起來，不存明文。
// （規格書寫的是 Argon2id，但 Workers 的 WebCrypto 沒有內建 Argon2，
//  PBKDF2 是這個環境拿得到、又足以保護「朋友間房間密碼」的選擇。）
const PBKDF2_ITERATIONS = 120000;

async function hashPassword(password, saltHex) {
  const enc = new TextEncoder();
  const salt = Uint8Array.from(saltHex.match(/../g).map((h) => parseInt(h, 16)));
  const key = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS },
    key,
    256,
  );
  return [...new Uint8Array(bits)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// 定值時間比較，避免用字串 === 比較雜湊值時洩漏資訊
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/api/group/health") {
      return json({ ok: true, service: "group-planting", time: Date.now() });
    }

    let roomId = null;
    let body = null;
    if (path === "/api/group/create" || path === "/api/group/join") {
      if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
      try {
        body = await request.json();
      } catch {
        return json({ error: "invalid_json" }, 400);
      }
      roomId = String(body.roomId || "").toUpperCase();
    } else if (path === "/api/group/socket") {
      roomId = String(url.searchParams.get("room") || "").toUpperCase();
    } else {
      return json({ error: "not_found" }, 404);
    }

    if (!ROOM_ID_RE.test(roomId)) {
      return json({ error: "invalid_room_id", detail: "房號需為 3-16 位大寫英數字、_ 或 -" }, 400);
    }

    // 同一個房號一定路由到同一個 Durable Object 實例
    const id = env.GROUP_ROOM.idFromName(roomId);
    const stub = env.GROUP_ROOM.get(id);

    const forwarded = new Request(request.url, {
      method: request.method,
      headers: request.headers,
      body: body === null ? null : JSON.stringify({ ...body, roomId }),
    });
    return stub.fetch(forwarded);
  },
};

export class GroupRoom {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    // 記憶體狀態：休眠後會消失，靠 loadState() 從 storage 補回來
    this.state = null;
    // barrier 的 ack 名單是「這一輪執行中」才有意義的短暫狀態，不落地
    this.barrier = null;
  }

  async loadState() {
    if (this.state) return this.state;
    this.state = (await this.ctx.storage.get("room")) || null;
    return this.state;
  }

  async saveState() {
    await this.ctx.storage.put("room", this.state);
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/api/group/create") return this.handleCreate(await request.json());
    if (path === "/api/group/join") return this.handleJoin(await request.json());
    if (path === "/api/group/socket") return this.handleSocket(request, url);
    return json({ error: "not_found" }, 404);
  }

  async handleCreate(body) {
    const roomId = String(body.roomId || "").toUpperCase();
    const password = String(body.password || "");
    const name = String(body.name || "").trim().slice(0, 32) || "房主";
    const maxMembers = Math.max(2, Math.min(20, parseInt(body.maxMembers, 10) || 20));

    if (password.length < 4) {
      return json({ error: "weak_password", detail: "房間密碼至少 4 個字" }, 400);
    }

    await this.loadState();
    // 房間還有人連著就不能被重新建立，避免把別人的房間洗掉
    if (this.state && this.ctx.getWebSockets().length > 0) {
      return json({ error: "room_in_use", detail: "這個房號正在使用中，請換一個或改用加入" }, 409);
    }

    const saltHex = randomHex(16);
    this.state = {
      roomId,
      saltHex,
      passwordHash: await hashPassword(password, saltHex),
      maxMembers,
      hostId: null,
      hostName: name,
      phase: PHASE_IDLE,
      settings: null,
      settingsVersion: 0,
      startAt: null,
      createdAt: Date.now(),
    };
    await this.saveState();

    const token = randomHex(24);
    await this.ctx.storage.put(`token:${token}`, { role: "host", name, issuedAt: Date.now() });
    return json({ ok: true, roomId, token, role: "host", maxMembers });
  }

  async handleJoin(body) {
    const password = String(body.password || "");
    const name = String(body.name || "").trim().slice(0, 32) || "團員";

    await this.loadState();
    if (!this.state) return json({ error: "room_not_found", detail: "找不到這個房間" }, 404);

    const attempt = await hashPassword(password, this.state.saltHex);
    if (!timingSafeEqual(attempt, this.state.passwordHash)) {
      return json({ error: "bad_password", detail: "房間密碼不對" }, 403);
    }
    if (this.ctx.getWebSockets().length >= this.state.maxMembers) {
      return json({ error: "room_full", detail: "房間人數已滿" }, 409);
    }

    const token = randomHex(24);
    await this.ctx.storage.put(`token:${token}`, { role: "member", name, issuedAt: Date.now() });
    return json({ ok: true, roomId: this.state.roomId, token, role: "member", maxMembers: this.state.maxMembers });
  }

  async handleSocket(request, url) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return json({ error: "expected_websocket" }, 426);
    }
    const token = url.searchParams.get("token") || "";
    const record = await this.ctx.storage.get(`token:${token}`);
    if (!record) return json({ error: "bad_token" }, 403);

    await this.loadState();
    if (!this.state) return json({ error: "room_not_found" }, 404);

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    const memberId = randomHex(8);
    const isHost = record.role === "host";
    // Hibernation API：接受連線後就算 DO 休眠，連線仍然存活；
    // 成員資料放 attachment，醒來後還讀得到。
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({
      memberId,
      name: record.name,
      isHost,
      ready: isHost, // 房主本身視為永遠 ready
      joinedAt: Date.now(),
    });

    if (isHost) {
      this.state.hostId = memberId;
      this.state.hostName = record.name;
      await this.saveState();
    }
    // token 一次性使用，避免同一組 token 被重複拿去接第二條連線
    await this.ctx.storage.delete(`token:${token}`);

    this.broadcastSnapshot();
    return new Response(null, { status: 101, webSocket: client });
  }

  // ---------- 房間狀態 ----------

  members() {
    return this.ctx.getWebSockets().map((ws) => {
      const att = ws.deserializeAttachment() || {};
      return { ws, ...att };
    });
  }

  snapshot() {
    const members = this.members().map((m) => ({
      memberId: m.memberId,
      name: m.name,
      isHost: !!m.isHost,
      ready: !!m.ready,
      connected: true,
    }));
    return {
      type: "snapshot",
      room: {
        roomId: this.state?.roomId || null,
        phase: this.state?.phase || PHASE_IDLE,
        maxMembers: this.state?.maxMembers || 20,
        hostId: this.state?.hostId || null,
        hostName: this.state?.hostName || null,
        settings: this.state?.settings || null,
        settingsVersion: this.state?.settingsVersion || 0,
        startAt: this.state?.startAt || null,
        barrier: this.barrier ? this.barrier.boundary : null,
        members,
      },
    };
  }

  broadcast(message, exceptWs = null) {
    const text = JSON.stringify(message);
    for (const ws of this.ctx.getWebSockets()) {
      if (ws === exceptWs) continue;
      try {
        ws.send(text);
      } catch {
        // 送不出去的連線交給 webSocketClose/webSocketError 收拾，這裡不中斷其他人
      }
    }
  }

  broadcastSnapshot() {
    this.broadcast(this.snapshot());
  }

  send(ws, message) {
    try {
      ws.send(JSON.stringify(message));
    } catch {
      /* 忽略：連線已斷 */
    }
  }

  // ---------- WebSocket 事件（Hibernation API 的處理函式）----------

  async webSocketMessage(ws, raw) {
    await this.loadState();
    if (!this.state) return;

    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return this.send(ws, { type: "error", message: "invalid_json" });
    }

    const att = ws.deserializeAttachment() || {};
    const isHost = !!att.isHost;

    switch (msg.type) {
      case "ping":
        return this.send(ws, { type: "pong", time: Date.now() });

      case "hello":
        return this.send(ws, this.snapshot());

      case "ready": {
        if (isHost) return; // 房主不需要按準備
        ws.serializeAttachment({ ...att, ready: !!msg.ready });
        return this.broadcastSnapshot();
      }

      case "settings": {
        if (!isHost) return this.send(ws, { type: "error", message: "only_host" });
        if (this.state.phase !== PHASE_IDLE) {
          return this.send(ws, { type: "command_ack", command: "settings", ok: false, reason: "busy" });
        }
        this.state.settings = msg.settings || null;
        this.state.settingsVersion = (this.state.settingsVersion || 0) + 1;
        await this.saveState();
        this.broadcastSnapshot();
        return this.send(ws, {
          type: "command_ack",
          command: "settings",
          ok: true,
          settingsVersion: this.state.settingsVersion,
        });
      }

      case "command": {
        if (!isHost) return this.send(ws, { type: "error", message: "only_host" });
        return this.handleHostCommand(ws, msg);
      }

      case "progress": {
        if (!isHost) return;
        if (this.state.phase !== PHASE_RUNNING) return;
        // 房主座標純轉發，不落地也不回送給房主自己
        return this.broadcast({ type: "progress", payload: msg.payload || {} }, ws);
      }

      case "barrier": {
        // 房主抵達同步點，開始等所有團員跟上
        if (!isHost) return;
        const boundary = Number(msg.boundary) || 0;
        this.barrier = { boundary, acked: new Set() };
        this.broadcast({ type: "barrier", boundary }, ws);
        return this.maybeRelease();
      }

      case "barrier_ack": {
        if (isHost) return;
        const boundary = Number(msg.boundary) || 0;
        if (!this.barrier || this.barrier.boundary !== boundary) return;
        this.barrier.acked.add(att.memberId);
        return this.maybeRelease();
      }

      default:
        return this.send(ws, { type: "error", message: "unknown_type" });
    }
  }

  async handleHostCommand(ws, msg) {
    const command = String(msg.command || "");

    if (command === "start") {
      const check = this.canStart();
      if (!check.ok) {
        return this.send(ws, { type: "command_ack", command, ok: false, reason: check.reason });
      }
      this.state.phase = PHASE_COUNTDOWN;
      this.state.startAt = Date.now() + COUNTDOWN_MS;
      await this.saveState();
      this.send(ws, { type: "command_ack", command, ok: true, startAt: this.state.startAt });
      this.broadcastSnapshot();
      // 倒數結束就轉成 running；用 alarm 而不是 setTimeout，休眠也叫得醒
      await this.ctx.storage.setAlarm(this.state.startAt);
      return;
    }

    if (command === "pause") {
      if (this.state.phase !== PHASE_RUNNING) {
        return this.send(ws, { type: "command_ack", command, ok: false, reason: "not_running" });
      }
      this.state.phase = PHASE_PAUSED;
      await this.saveState();
      this.send(ws, { type: "command_ack", command, ok: true });
      return this.broadcastSnapshot();
    }

    if (command === "resume") {
      if (this.state.phase !== PHASE_PAUSED) {
        return this.send(ws, { type: "command_ack", command, ok: false, reason: "not_paused" });
      }
      this.state.phase = PHASE_RUNNING;
      await this.saveState();
      this.send(ws, { type: "command_ack", command, ok: true });
      return this.broadcastSnapshot();
    }

    if (command === "stop") {
      this.state.phase = PHASE_IDLE;
      this.state.startAt = null;
      this.barrier = null;
      await this.saveState();
      this.send(ws, { type: "command_ack", command, ok: true });
      return this.broadcastSnapshot();
    }

    return this.send(ws, { type: "command_ack", command, ok: false, reason: "unknown_command" });
  }

  // 規格書 §11.5 的開始條件
  canStart() {
    const members = this.members();
    const followers = members.filter((m) => !m.isHost);
    if (!members.some((m) => m.isHost)) return { ok: false, reason: "no_host" };
    if (this.state.phase !== PHASE_IDLE) return { ok: false, reason: "busy" };
    const flowerCount = this.state.settings?.flowerCount || 0;
    if (flowerCount <= 0) return { ok: false, reason: "no_flowers" };
    if (followers.some((m) => !m.ready)) return { ok: false, reason: "members_not_ready" };
    return { ok: true };
  }

  async alarm() {
    await this.loadState();
    if (!this.state) return;
    if (this.state.phase === PHASE_COUNTDOWN) {
      this.state.phase = PHASE_RUNNING;
      await this.saveState();
      this.broadcastSnapshot();
    }
  }

  maybeRelease() {
    if (!this.barrier) return;
    const followers = this.members().filter((m) => !m.isHost);
    const pending = followers.filter((m) => !this.barrier.acked.has(m.memberId));
    if (pending.length > 0) return;
    const boundary = this.barrier.boundary;
    this.barrier = null;
    this.broadcast({ type: "release", boundary });
  }

  async webSocketClose(ws) {
    await this.loadState();
    const att = ws.deserializeAttachment() || {};
    if (this.state && att.isHost) {
      // 房主離線就把房間收回待機，避免團員卡在 running 卻沒人送座標
      this.state.phase = PHASE_IDLE;
      this.state.startAt = null;
      this.barrier = null;
      await this.saveState();
    }
    // 少一個人之後，原本卡住的 barrier 可能就湊齊了
    this.maybeRelease();
    this.broadcastSnapshot();
  }

  async webSocketError(ws) {
    return this.webSocketClose(ws);
  }
}
