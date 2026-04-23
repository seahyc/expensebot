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
  CREATE TABLE IF NOT EXISTS known_jids (
    session_id TEXT NOT NULL,
    jid TEXT NOT NULL,
    PRIMARY KEY (session_id, jid)
  );
  CREATE TABLE IF NOT EXISTS chats (
    session_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    name TEXT,
    archived INTEGER DEFAULT 0,
    muted_until INTEGER DEFAULT 0,  -- 0 = not muted, -1 = forever, else unix ts
    pinned INTEGER DEFAULT 0,
    unread_count INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT (unixepoch()),
    PRIMARY KEY (session_id, chat_jid)
  );
`);

// Prepared statements
const insertMsg = db.prepare(`
  INSERT INTO messages (session_id, chat_jid, sender_jid, timestamp, text)
  VALUES (?, ?, ?, ?, ?)
`);

const upsertJid = db.prepare(`
  INSERT OR IGNORE INTO known_jids (session_id, jid) VALUES (?, ?)
`);

const getKnownJids = db.prepare(`
  SELECT jid FROM known_jids WHERE session_id = ?
`);

const queryMsgs = db.prepare(`
  SELECT chat_jid, sender_jid, timestamp, text
  FROM messages
  WHERE session_id = ? AND timestamp >= ?
  ORDER BY timestamp ASC
`);

// Upsert chat metadata. COALESCE preserves existing values when the incoming
// Baileys event omits a field (chats.update only includes changed fields).
const upsertChat = db.prepare(`
  INSERT INTO chats (session_id, chat_jid, name, archived, muted_until, pinned, unread_count, updated_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch())
  ON CONFLICT(session_id, chat_jid) DO UPDATE SET
    name         = COALESCE(excluded.name, chats.name),
    archived     = COALESCE(excluded.archived, chats.archived),
    muted_until  = COALESCE(excluded.muted_until, chats.muted_until),
    pinned       = COALESCE(excluded.pinned, chats.pinned),
    unread_count = COALESCE(excluded.unread_count, chats.unread_count),
    updated_at   = unixepoch()
`);

const queryChats = db.prepare(`
  SELECT chat_jid, name, archived, muted_until, pinned, unread_count, updated_at
  FROM chats
  WHERE session_id = ?
`);

// Fill in a name only when the row has none. Used for low-priority sources
// like pushName (the sender's self-set profile name on incoming messages),
// which should never overwrite a saved-contact name from contacts.upsert.
// Note the COALESCE order: chats.name first → existing wins.
const seedChatName = db.prepare(`
  INSERT INTO chats (session_id, chat_jid, name, updated_at)
  VALUES (?, ?, ?, unixepoch())
  ON CONFLICT(session_id, chat_jid) DO UPDATE SET
    name = COALESCE(chats.name, excluded.name),
    updated_at = unixepoch()
`);

// Translate a Baileys Chat object into the columns we store. Returns null if
// the chat is unusable (e.g. broadcast/status). Each field is null when absent
// so the COALESCE in upsertChat preserves prior state.
function chatToRow(c) {
  if (!c?.id) return null;
  if (c.id === "status@broadcast") return null;
  if (typeof isJidBroadcast === "function" && isJidBroadcast(c.id)) return null;

  let mutedUntil = null;
  // Baileys uses `muteEndTime` (number, ms or s depending on version) or
  // legacy `mute` (boolean). Normalize to: 0 = not muted, -1 = forever, else seconds.
  if (c.muteEndTime !== undefined && c.muteEndTime !== null) {
    const n = Number(c.muteEndTime);
    if (!Number.isFinite(n) || n === 0) mutedUntil = 0;
    else if (n < 0) mutedUntil = -1;
    else mutedUntil = n > 1e12 ? Math.floor(n / 1000) : n; // ms → s if needed
  } else if (typeof c.mute === "boolean") {
    mutedUntil = c.mute ? -1 : 0;
  }

  const archived = c.archived === undefined || c.archived === null
    ? null
    : (c.archived ? 1 : 0);
  // Baileys `pin` is a timestamp when pinned, 0/undefined otherwise.
  const pinnedRaw = c.pin ?? c.pinned;
  const pinned = pinnedRaw === undefined || pinnedRaw === null
    ? null
    : (Number(pinnedRaw) > 0 ? 1 : 0);
  const unread = c.unreadCount === undefined || c.unreadCount === null
    ? null
    : Math.max(0, Number(c.unreadCount));

  return {
    name: c.name ?? c.subject ?? null,
    archived,
    mutedUntil,
    pinned,
    unread,
  };
}

function persistChat(sessionId, c) {
  const row = chatToRow(c);
  if (!row) return;
  try {
    upsertChat.run(
      sessionId,
      c.id,
      row.name,
      row.archived,
      row.mutedUntil,
      row.pinned,
      row.unread,
    );
  } catch (e) {
    log.warn({ sessionId, jid: c.id, err: e.message }, "upsertChat failed");
  }
}

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
    sessions.set(sessionId, {
      socket: null,
      knownJids: new Set(), // accumulate chat JIDs from chats.upsert
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
        // Same pushName fallback as in storeMessages — backfill historic
        // messages also let us recover names for unsaved DM contacts.
        if (
          !msg.key.fromMe &&
          msg.pushName &&
          jid.endsWith("@s.whatsapp.net")
        ) {
          try { seedChatName.run(sessionId, jid, msg.pushName); } catch (_) {}
        }
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
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
    syncFullHistory: true, // request full message history on connect
  });

  state.socket = sock;
  state.reconnecting = false;

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
      // Backfill: first from persisted JIDs, then try fetching groups
      setTimeout(async () => {
        const persistedJids = getKnownJids.all(sessionId).map((r) => r.jid);
        // Always try to get groups — this works on reconnect
        let groupJids = [];
        try {
          const groups = await sock.groupFetchAllParticipating();
          groupJids = Object.keys(groups || {});
          for (const jid of groupJids) {
            state.knownJids.add(jid);
            try { upsertJid.run(sessionId, jid); } catch (_) {}
            // Persist group name so list_whatsapp_chats can show "Family WhatsApp"
            // instead of a raw 12345@g.us. Mute/archive/pin still come from
            // chats events when those fire.
            const meta = groups[jid] || {};
            persistChat(sessionId, { id: jid, subject: meta.subject || meta.name });
          }
        } catch (e) {
          log.warn({ sessionId, err: e.message }, "groupFetchAllParticipating failed");
        }
        const allJids = [...new Set([...persistedJids, ...groupJids])];
        if (allJids.length > 0) {
          log.info({ sessionId, count: allJids.length }, "backfilling from known JIDs");
          syncChatsHistory(sessionId, allJids).catch((e) =>
            log.warn({ sessionId, err: e.message }, "syncChatsHistory error")
          );
        } else {
          log.info({ sessionId }, "no known JIDs for backfill — waiting for contacts.upsert");
        }
      }, 5000);
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

      // pushName fallback for unsaved DM contacts. msg.pushName is the name
      // the sender set on their own WA profile. Only meaningful for DMs (in
      // groups, pushName is the participant who sent the msg, not the group
      // name). seedChatName never overwrites an existing name, so a saved
      // contact name from contacts.upsert always wins.
      if (
        chatJid &&
        !msg.key.fromMe &&
        msg.pushName &&
        chatJid.endsWith("@s.whatsapp.net")
      ) {
        try { seedChatName.run(sessionId, chatJid, msg.pushName); } catch (_) {}
      }

      try {
        insertMsg.run(sessionId, chatJid, senderJid, timestamp, text);
        count++;
      } catch (e) {
        log.error({ sessionId, err: e.message }, "failed to insert message");
      }
    }
    if (count > 0) log.info({ sessionId, source, count }, "stored messages");
  }

  sock.ev.on("messaging-history.set", ({ messages: historyMsgs, chats: historyChats }) => {
    storeMessages(historyMsgs || [], "history-sync");
    // Seed the chats table from history sync — this is the only source of
    // chat metadata when reconnecting on cached auth (chats.upsert won't
    // re-fire for already-known chats).
    if (Array.isArray(historyChats) && historyChats.length) {
      let seeded = 0;
      for (const c of historyChats) {
        if (!c?.id) continue;
        persistChat(sessionId, c);
        seeded++;
      }
      log.info({ sessionId, seeded }, "seeded chats from history-sync");
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    storeMessages(messages, "notify");
  });

  // contacts.upsert fires with full contact list on connect — use JIDs for backfill
  // chats.upsert also accumulated for completeness
  let backfillTimer = null;
  function scheduleBackfill() {
    if (backfillTimer) clearTimeout(backfillTimer);
    backfillTimer = setTimeout(() => {
      const jids = [...state.knownJids];
      if (!jids.length) return;
      log.info({ sessionId, count: jids.length }, "triggering history backfill");
      syncChatsHistory(sessionId, jids).catch((e) =>
        log.warn({ sessionId, err: e.message }, "syncChatsHistory error")
      );
    }, 8000);
  }

  sock.ev.on("contacts.upsert", (contacts) => {
    for (const c of contacts) {
      if (c.id && !c.id.endsWith("@broadcast") && !isJidBroadcast(c.id)) {
        state.knownJids.add(c.id);
        try { upsertJid.run(sessionId, c.id); } catch (_) {}
        // Persist contact display name for DMs. Baileys exposes either `name`
        // (saved contact name) or `notify` (the name they set on their own
        // profile). Prefer `name`. This gives list_whatsapp_chats real labels.
        const displayName = c.name || c.notify || null;
        if (displayName) {
          persistChat(sessionId, { id: c.id, subject: displayName });
        }
      }
    }
    scheduleBackfill();
  });

  sock.ev.on("chats.upsert", (chats) => {
    for (const c of chats) {
      if (c.id && !c.id.endsWith("@broadcast") && !isJidBroadcast(c.id)) {
        state.knownJids.add(c.id);
        try { upsertJid.run(sessionId, c.id); } catch (_) {}
        persistChat(sessionId, c);
      }
    }
    scheduleBackfill();
  });

  // chats.update fires for state changes (mute toggle, archive, pin, unread
  // count changes). Baileys sends partial chat objects — persistChat handles
  // that via COALESCE in the upsert.
  sock.ev.on("chats.update", (updates) => {
    for (const c of updates || []) {
      if (c?.id) persistChat(sessionId, c);
    }
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

// POST /session/:session_id/reset — tear down existing session, clear auth, start fresh (for re-pairing)
app.post("/session/:session_id/reset", async (req, res) => {
  const { session_id } = req.params;
  log.info({ session_id }, "resetting session for re-pair");

  const state = sessions.get(session_id);
  if (state?.socket) {
    try { await state.socket.logout(); } catch (_) {}
    try { state.socket.end(); } catch (_) {}
  }
  clearAuthState(session_id);
  sessions.delete(session_id);

  // Small delay so socket close events settle before we start fresh
  await new Promise((r) => setTimeout(r, 500));

  try {
    await startSession(session_id);
    res.json({ ok: true });
  } catch (e) {
    log.error({ session_id, err: e.message }, "startSession after reset failed");
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

// GET /chats/:session_id — return chat metadata (name, archived, muted, pinned, unread)
app.get("/chats/:session_id", (req, res) => {
  const { session_id } = req.params;
  try {
    const now = Math.floor(Date.now() / 1000);
    const rows = queryChats.all(session_id).map((r) => ({
      chat_jid: r.chat_jid,
      name: r.name,
      archived: !!r.archived,
      pinned: !!r.pinned,
      unread_count: r.unread_count || 0,
      // muted_until: 0 = not muted, -1 = muted forever, >0 = muted until that ts
      muted: r.muted_until === -1 || (r.muted_until > 0 && r.muted_until > now),
      muted_until: r.muted_until || 0,
      updated_at: r.updated_at || 0,
    }));
    res.json({ chats: rows });
  } catch (e) {
    log.error({ session_id, err: e.message }, "query chats failed");
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
