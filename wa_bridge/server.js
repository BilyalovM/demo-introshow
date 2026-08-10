/**
 * Intro Show CRM — WhatsApp Web bridge (whatsapp-web.js).
 *
 * Runs on a VPS (NOT on Vercel serverless). Persists LocalAuth session on disk.
 * Inbound messages → POST {CRM_URL}/api/webhooks/whatsapp-web
 * Outbound replies ← POST /send from CRM
 *
 * Env:
 *   PORT              default 3001
 *   CRM_URL           e.g. https://demo-introshow.vercel.app
 *   CRM_API_KEY       shared secret (X-API-Key) for CRM webhook
 *   BRIDGE_API_KEY    same secret for CRM → bridge calls (optional; falls back to CRM_API_KEY)
 *   SESSION_PATH      default ./session
 */
"use strict";

const path = require("path");
const fs = require("fs");
const express = require("express");
const qrcode = require("qrcode");
const { Client, LocalAuth } = require("whatsapp-web.js");

const PORT = Number(process.env.PORT || 3001);
const CRM_URL = (process.env.CRM_URL || "").replace(/\/$/, "");
const CRM_API_KEY = process.env.CRM_API_KEY || process.env.WA_WEB_API_KEY || "";
const BRIDGE_API_KEY =
  process.env.BRIDGE_API_KEY || process.env.WA_WEB_API_KEY || CRM_API_KEY || "";
const SESSION_PATH = process.env.SESSION_PATH || path.join(__dirname, "session");

fs.mkdirSync(SESSION_PATH, { recursive: true });

const state = {
  status: "disconnected", // disconnected | initializing | wait_qr | connected | auth_failure
  qrDataUrl: null,
  lastError: null,
  phone: null,
  startedAt: null,
};

let client = null;
let starting = false;

function authMiddleware(req, res, next) {
  if (!BRIDGE_API_KEY) return next();
  const key =
    req.get("x-api-key") ||
    (req.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (key !== BRIDGE_API_KEY) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  return next();
}

async function pushToCrm(payload) {
  if (!CRM_URL) {
    console.warn("[wa-bridge] CRM_URL not set — skip inbound webhook");
    return;
  }
  const url = `${CRM_URL}/api/webhooks/whatsapp-web`;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(CRM_API_KEY ? { "X-API-Key": CRM_API_KEY } : {}),
      },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const body = await r.text();
      console.error("[wa-bridge] CRM webhook failed", r.status, body.slice(0, 300));
    }
  } catch (err) {
    console.error("[wa-bridge] CRM webhook error:", err.message);
  }
}

function destroyClient() {
  if (client) {
    try {
      client.removeAllListeners();
      client.destroy().catch(() => {});
    } catch (_) {
      /* ignore */
    }
  }
  client = null;
}

function createClient() {
  const puppeteerOpts = {
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  };
  if (process.env.PUPPETEER_EXECUTABLE_PATH) {
    puppeteerOpts.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  }
  return new Client({
    authStrategy: new LocalAuth({
      clientId: "introshow",
      dataPath: SESSION_PATH,
    }),
    puppeteer: puppeteerOpts,
  });
}

async function startClient() {
  if (starting) return state;
  starting = true;
  destroyClient();
  state.status = "initializing";
  state.qrDataUrl = null;
  state.lastError = null;
  state.phone = null;
  state.startedAt = new Date().toISOString();

  client = createClient();

  client.on("qr", async (qr) => {
    try {
      state.qrDataUrl = await qrcode.toDataURL(qr, { margin: 2, width: 320 });
      state.status = "wait_qr";
      console.log("[wa-bridge] QR ready — scan in WhatsApp → Linked devices");
    } catch (err) {
      state.lastError = err.message;
      console.error("[wa-bridge] QR encode error:", err.message);
    }
  });

  client.on("authenticated", () => {
    console.log("[wa-bridge] authenticated");
  });

  client.on("ready", async () => {
    state.status = "connected";
    state.qrDataUrl = null;
    try {
      const info = client.info;
      state.phone = info?.wid?.user || null;
    } catch (_) {
      /* ignore */
    }
    console.log("[wa-bridge] connected", state.phone || "");
    await pushToCrm({
      event: "status",
      status: "connected",
      phone: state.phone,
    });
  });

  client.on("auth_failure", (msg) => {
    state.status = "auth_failure";
    state.lastError = String(msg || "auth_failure");
    console.error("[wa-bridge] auth_failure", msg);
  });

  client.on("disconnected", async (reason) => {
    state.status = "disconnected";
    state.qrDataUrl = null;
    state.phone = null;
    console.warn("[wa-bridge] disconnected:", reason);
    await pushToCrm({
      event: "status",
      status: "disconnected",
      reason: String(reason || ""),
    });
  });

  client.on("message", async (msg) => {
    try {
      if (msg.fromMe) return;
      if (msg.isStatus) return;
      const chatId = msg.from || "";
      const text = (msg.body || "").trim();
      if (!chatId || !text) return;
      let senderName = chatId.split("@")[0];
      try {
        const contact = await msg.getContact();
        senderName = contact.pushname || contact.name || senderName;
      } catch (_) {
        /* ignore */
      }
      await pushToCrm({
        event: "message",
        chat_id: chatId,
        text,
        sender_name: senderName,
        timestamp: msg.timestamp || Math.floor(Date.now() / 1000),
      });
    } catch (err) {
      console.error("[wa-bridge] inbound handler error:", err.message);
    }
  });

  try {
    await client.initialize();
  } catch (err) {
    state.status = "disconnected";
    state.lastError = err.message;
    console.error("[wa-bridge] initialize error:", err.message);
  } finally {
    starting = false;
  }
  return state;
}

async function logoutClient() {
  try {
    if (client) await client.logout();
  } catch (_) {
    /* ignore */
  }
  destroyClient();
  // Wipe LocalAuth folder so next connect gets a fresh QR
  try {
    fs.rmSync(SESSION_PATH, { recursive: true, force: true });
    fs.mkdirSync(SESSION_PATH, { recursive: true });
  } catch (err) {
    console.warn("[wa-bridge] session wipe:", err.message);
  }
  state.status = "disconnected";
  state.qrDataUrl = null;
  state.phone = null;
  state.lastError = null;
}

const app = express();
app.use(express.json({ limit: "1mb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "introshow-wa-bridge" });
});

app.get("/status", authMiddleware, (_req, res) => {
  res.json({
    status: state.status,
    phone: state.phone,
    last_error: state.lastError,
    started_at: state.startedAt,
    has_qr: Boolean(state.qrDataUrl),
    crm_url: CRM_URL || null,
  });
});

app.get("/qr", authMiddleware, (_req, res) => {
  if (!state.qrDataUrl) {
    return res.status(404).json({ error: "QR ещё не готов или уже отсканирован" });
  }
  // data:image/png;base64,....
  const b64 = state.qrDataUrl.split(",")[1] || "";
  const buf = Buffer.from(b64, "base64");
  res.set("Content-Type", "image/png");
  res.set("Cache-Control", "no-store");
  return res.send(buf);
});

app.get("/qr.json", authMiddleware, (_req, res) => {
  if (!state.qrDataUrl) {
    return res.status(404).json({ error: "QR ещё не готов или уже отсканирован" });
  }
  return res.json({ data_url: state.qrDataUrl });
});

app.post("/connect", authMiddleware, async (_req, res) => {
  startClient().catch((err) => console.error(err));
  res.json({ status: "starting" });
});

app.post("/logout", authMiddleware, async (_req, res) => {
  await logoutClient();
  res.json({ status: "disconnected" });
});

app.post("/send", authMiddleware, async (req, res) => {
  const chatId = String(req.body?.chat_id || req.body?.chatId || "").trim();
  const text = String(req.body?.text || "").trim();
  if (!chatId || !text) {
    return res.status(400).json({ error: "chat_id и text обязательны" });
  }
  if (!client || state.status !== "connected") {
    return res.status(503).json({ error: "WhatsApp Web не подключён" });
  }
  try {
    let target = chatId;
    if (!target.includes("@")) {
      const digits = target.replace(/\D/g, "");
      target = `${digits}@c.us`;
    }
    await client.sendMessage(target, text);
    return res.json({ status: "ok" });
  } catch (err) {
    console.error("[wa-bridge] send error:", err.message);
    return res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`[wa-bridge] listening on :${PORT}`);
  console.log(`[wa-bridge] session path: ${SESSION_PATH}`);
  console.log(`[wa-bridge] CRM_URL: ${CRM_URL || "(not set)"}`);
  // Auto-start if session already exists (reconnect after reboot)
  const hasSession = fs.existsSync(path.join(SESSION_PATH, "session-introshow"));
  if (hasSession || process.env.WA_BRIDGE_AUTOSTART === "1") {
    console.log("[wa-bridge] autostart client…");
    startClient().catch((err) => console.error(err));
  }
});
