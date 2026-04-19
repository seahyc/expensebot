"use strict";

const express = require("express");
const path = require("path");
const fs = require("fs");
const pino = require("pino");
const QRCode = require("qrcode");
const Database = require("better-sqlite3");

const {
  default: makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  isJidBroadcast,
  makeInMemoryStore,
} = require("@whiskeysockets/baileys");

const PORT = parseInt(process.env.PORT || "3001", 10);
const AUTH_DIR = process.env.AUTH_DIR || path.join(__dirname, "auth");
const DB_PATH = process.env.DB_PATH || path.join(__dirname, "messages.db");
const NINETY_DAYS_SECS = 90 * 24 * 60 * 60;

const log = pino({ level: process.env.LOG_LEVEL || "info" });

// Ensure directories exist
fs.mkdirSync(AUTH_DIR, { recursive: true });
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

// ---------------------------------------------------------------------------
// SQLite setup
// ---------------------------------------------------------------------------

const db = new Database(DB_PATH);

db.exec(`
  CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_jid TEXT,
    sender_jid TEXT,
    timestamp INTEGER,
    text TEXT,
    created_at INTEGER DEFAULT (unixepoch())
  );
  CREATE INDEX IF NOT EXISTS idx_session_ts ON messages(session_id, timestamp);
`);

// Prepared statements
const insertMsg = db.prepare(`
  INSERT INTO messages (session_id, chat_jid, sender_jid, timestamp, text)
  VALUES (?, ?, ?, ?, ?)
`);

const queryMsgs = db.prepare(`
  SELECT chat_jid, sender_jid, timestamp, text
  FROM messages
  WHERE session_id = ? AND timestamp >= ?
  ORDER BY timestamp ASC
`);

// Prune messages older than 90 days
function pruneOldMessages() {
  const cutoff = Math.floor(Date.now() / 1000) - NINETY_DAYS_SECS;
  db.prepare("DELETE FROM messages WHERE created_at < ?").run(cutoff);
}

// Run prune on startup and once a day
pruneOldMessages();
setInterval(pruneOldMessages, 24 * 60 * 60 * 1000);

// ---------------------------------------------------------------------------
// Session state
// ---------------------------------------------------------------------------

// session_id (string) -> { socket, store, qr, connected, phone, reconnecting }
const sessions = new Map();

function getOrCreateSessionState(sessionId) {
  if (!sessions.has(sessionId)) {
    const store = makeInMemoryStore({ logger: pino({ level: "silent" }) });
    sessions.set(sessionId, {
      socket: null,
      store,
      qr: null,
      connected: false,
      phone: null,
      reconnecting: false,
    });
  }
  return sessions.get(sessionId);
}

function clearAuthState(sessionId) {
  const authDir = path.join(AUTH_DIR, sessionId);
  if (fs.existsSync(authDir)) {
    fs.rmSync(authDir, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------
// Baileys session management
// ---------------------------------------------------------------------------

async function syncChatsHistory(sessionId, chatJids) {
  const state = sessions.get(sessionId);
  if (!state?.socket || !state.connected) return;

  const sock = state.socket;
  const cutoffTs = Math.floor(Date.now() / 1000) - 30 * 24 * 60 * 60;
  let total = 0;

  for (const jid of chatJids.slice(0, 40)) {
    if (!jid || jid === "status@broadcast") continue;
    if (isJidBroadcast(jid)) continue;
    try {
      const result = await sock.loadMessages(jid, 50);
      for (const msg of result?.messages || []) {
        if (!msg.message) continue;
        const ts = Number(msg.messageTimestamp || 0);
        if (ts < cutoffTs) continue;
        const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text || null;
        if (!text) continue;
        try {
          insertMsg.run(sessionId, jid, msg.key.participant || msg.key.remoteJid || jid, ts, text);
          total++;
        } catch (_) {}
      }
    } catch (_) {}
  }
  log.info({ sessionId, total, chats: chatJids.length }, "syncChatsHistory: done");
}

async function startSession(sessionId) {
  const state = getOrCreateSessionState(sessionId);

  // If already connected, skip
  if (state.connected && state.socket) {
    log.info({ sessionId }, "session already connected");
    return;
  }

  // Prevent double-start
  if (state.reconnecting) {
    log.info({ sessionId }, "session already reconnecting");
    return;
  }
  state.reconnecting = true;

  const { version } = await fetchLatestBaileysVersion();
  log.info({ sessionId, version }, "starting WA session");

  const authPath = path.join(AUTH_DIR, sessionId);
  fs.mkdirSync(authPath, { recursive: true });

  const { state: authState, saveCreds } = await useMultiFileAuthState(authPath);

  const sock = makeWASocket({
    version,
    auth: authState,
    logger: pino({ level: "silent" }), // suppress noisy baileys logs
    printQRInTerminal: false,
  });

  state.socket = sock;
  state.reconnecting = false;

  // Bind in-memory store so it tracks chats, contacts, messages
  state.store.bind(sock.ev);

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        const qrDataUrl = await QRCode.toDataURL(qr);
        state.qr = qrDataUrl;
        log.info({ sessionId }, "QR code updated");
      } catch (e) {
        log.error({ sessionId, err: e.message }, "QR generation failed");
      }
    }

    if (connection === "open") {
      state.connected = true;
      state.qr = null;
      // Extract phone number from JID (format: "1234567890@s.whatsapp.net")
      try {
        const jid = sock.user?.id || "";
        const phoneMatch = jid.match(/^(\d+)/);
        state.phone = phoneMatch ? `+${phoneMatch[1]}` : null;
      } catch (_) {
        state.phone = null;
      }
      log.info({ sessionId, phone: state.phone }, "WA session connected");
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;

      log.info({ sessionId, statusCode, loggedOut }, "WA connection closed");

      state.connected = false;
      state.socket = null;

      if (loggedOut) {
        // Auth is dead — clear creds and don't reconnect automatically
        log.warn({ sessionId }, "logged out, clearing auth state");
        clearAuthState(sessionId);
        state.qr = null;
        state.phone = null;
      } else {
        // Reconnect after a short delay
        setTimeout(() => startSession(sessionId), 5000);
      }
    }
  });

  function storeMessages(msgs, source) {
    const now = Math.floor(Date.now() / 1000);
    let count = 0;
    for (const msg of msgs) {
      if (!msg.message) continue;
      if (msg.key.remoteJid === "status@broadcast") continue;
      if (isJidBroadcast(msg.key.remoteJid || "")) continue;

      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        null;

      if (!text) continue;

      const chatJid = msg.key.remoteJid || null;
      const senderJid = msg.key.participant || msg.key.remoteJid || null;
      const timestamp = msg.messageTimestamp
        ? Number(msg.messageTimestamp)
        : now;

      try {
        insertMsg.run(sessionId, chatJid, senderJid, timestamp, text);
        count++;
      } catch (e) {
        log.error({ sessionId, err: e.message }, "failed to insert message");
      }
    }
    if (count > 0) log.info({ sessionId, source, count }, "stored messages");
  }

  sock.ev.on("messaging-history.set", ({ messages: historyMsgs }) => {
    storeMessages(historyMsgs || [], "history-sync");
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    storeMessages(messages, "notify");
  });

  // chats.upsert fires as chats arrive — trigger backfill once we have a good list
  let backfillScheduled = false;
  sock.ev.on("chats.upsert", (chats) => {
    if (backfillScheduled) return;
    backfillScheduled = true;
    // Wait 8s for more chats to accumulate before backfilling
    setTimeout(() => {
      const allJids = Object.keys(state.store.chats.all ? state.store.chats.all() : {});
      const fromEvent = chats.map((c) => c.id).filter(Boolean);
      const jids = allJids.length > 0 ? allJids : fromEvent;
      log.info({ sessionId, count: jids.length }, "chats.upsert: triggering history backfill");
      syncChatsHistory(sessionId, jids).catch((e) =>
        log.warn({ sessionId, err: e.message }, "syncChatsHistory error")
      );
    }, 8000);
  });
}

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

// POST /session/:session_id — start or reconnect a WA session
app.post("/session/:session_id", async (req, res) => {
  const { session_id } = req.params;
  try {
    await startSession(session_id);
    res.json({ ok: true });
  } catch (e) {
    log.error({ session_id, err: e.message }, "startSession failed");
    res.status(500).json({ error: e.message });
  }
});

// GET /qr/:session_id — return QR code or connected status
app.get("/qr/:session_id", (req, res) => {
  const { session_id } = req.params;
  const state = sessions.get(session_id);

  if (!state) {
    return res.status(404).json({ error: "session not found" });
  }

  if (state.connected) {
    return res.json({ connected: true });
  }

  if (state.qr) {
    return res.json({ qr: state.qr, connected: false });
  }

  // No QR yet — still initialising
  return res.json({ qr: null, connected: false });
});

// GET /status/:session_id — connection status
app.get("/status/:session_id", (req, res) => {
  const { session_id } = req.params;
  const state = sessions.get(session_id);

  if (!state) {
    return res.json({ connected: false, phone: null });
  }

  res.json({ connected: state.connected, phone: state.phone || null });
});

// GET /messages/:session_id?since=<unix_ts> — fetch messages since timestamp
app.get("/messages/:session_id", (req, res) => {
  const { session_id } = req.params;
  const since = parseInt(req.query.since || "0", 10);

  try {
    const rows = queryMsgs.all(session_id, since);
    res.json({ messages: rows });
  } catch (e) {
    log.error({ session_id, err: e.message }, "query messages failed");
    res.status(500).json({ error: e.message });
  }
});

// DELETE /session/:session_id — disconnect and clear auth
app.delete("/session/:session_id", async (req, res) => {
  const { session_id } = req.params;
  const state = sessions.get(session_id);

  if (state?.socket) {
    try {
      await state.socket.logout();
    } catch (_) {}
    try {
      state.socket.end();
    } catch (_) {}
  }

  clearAuthState(session_id);
  sessions.delete(session_id);

  log.info({ session_id }, "session deleted");
  res.json({ ok: true });
});

// Health check
app.get("/healthz", (_req, res) => res.json({ ok: true }));

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, () => {
  log.info({ port: PORT }, "WhatsApp bridge listening");
});
