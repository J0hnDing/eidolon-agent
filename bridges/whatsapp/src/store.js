import { DatabaseSync } from 'node:sqlite';
import { createCipheriv, createDecipheriv, randomBytes, createHash } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';

export const DAY = 86400_000;
export class BridgeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function fail(code) { throw new BridgeError(code); }
export const iso = (n) => new Date(n).toISOString();
const asNumber = (v) => Number(v?.toNumber ? v.toNumber() : v);
export function jid(value) {
  if (typeof value !== 'string') return null;
  if (/^\+[1-9]\d{6,14}$/.test(value)) return `${value.slice(1)}@s.whatsapp.net`;
  if (/^\d{6,20}(?::\d+)?@(s\.whatsapp\.net|lid)$/.test(value)) return value.replace(/:\d+@/, '@');
  return /^\d{6,20}(?:-\d{6,20})?@g\.us$/.test(value) ? value : null;
}

export class Store {
  constructor(directory, key, generation, { now = Date.now, codec = JSON } = {}) {
    if (!isAbsolute(directory) || key.length !== 32 || !generation) fail('invalid_input');
    this.now = now;
    this.codec = codec;
    this.key = key;
    mkdirSync(directory, { recursive: true });
    this.db = new DatabaseSync(join(directory, 'whatsapp.sqlite'));
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA secure_delete=ON;
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS auth (key TEXT PRIMARY KEY,value BLOB NOT NULL);
      CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY,canonical TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT);
      CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,last_message_at TEXT);
      CREATE TABLE IF NOT EXISTS messages (chat_id TEXT NOT NULL,id TEXT NOT NULL,sender_id TEXT NOT NULL,
        from_me INTEGER NOT NULL,timestamp TEXT NOT NULL,text TEXT,type TEXT NOT NULL,reply_to_message_id TEXT,
        expires_at INTEGER,quoted TEXT,PRIMARY KEY(chat_id,id));
      CREATE INDEX IF NOT EXISTS messages_timestamp ON messages(timestamp,chat_id,id);
      CREATE TABLE IF NOT EXISTS dispatches (id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,result TEXT);`);
    const saved = this.meta('generation');
    if (saved && saved !== generation) { this.db.close(); fail('connection_changed'); }
    this.setMeta('generation', generation);
    this.prune();
  }
  meta(key) { return this.db.prepare('SELECT value FROM metadata WHERE key=?').get(key)?.value ?? null; }
  setMeta(key, value) {
    this.db.prepare('INSERT OR REPLACE INTO metadata VALUES(?,?)').run(key, String(value));
  }
  transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = fn(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  authGet(key) {
    const value = this.db.prepare('SELECT value FROM auth WHERE key=?').get(key)?.value;
    if (!value) return null;
    try {
      const blob = Buffer.from(value);
      const decipher = createDecipheriv('aes-256-gcm', this.key, blob.subarray(0, 12));
      decipher.setAAD(Buffer.from(key));
      decipher.setAuthTag(blob.subarray(12, 28));
      return this.codec.parse(Buffer.concat([decipher.update(blob.subarray(28)), decipher.final()]).toString());
    } catch { fail('auth_storage_invalid'); }
  }
  authSet(key, value) {
    if (value == null) { this.db.prepare('DELETE FROM auth WHERE key=?').run(key); return; }
    const nonce = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.key, nonce);
    cipher.setAAD(Buffer.from(key));
    const data = Buffer.concat([cipher.update(this.codec.stringify(value)), cipher.final()]);
    this.db.prepare('INSERT OR REPLACE INTO auth VALUES(?,?)').run(key, Buffer.concat([nonce, cipher.getAuthTag(), data]));
  }
  canonical(id) {
    const normalized = jid(id);
    if (!normalized) return null;
    return this.db.prepare('SELECT canonical FROM aliases WHERE id=?').get(normalized)?.canonical ?? normalized;
  }
  mapAlias(lid, pn) {
    const source = jid(lid), target = jid(pn);
    if (!source?.endsWith('@lid') || !target?.endsWith('@s.whatsapp.net')) return;
    this.transaction(() => {
      this.db.prepare('INSERT OR REPLACE INTO aliases VALUES(?,?)').run(source, target);
      this.db.prepare('INSERT OR IGNORE INTO contacts SELECT ?,name,phone FROM contacts WHERE id=?').run(target, source);
      this.db.prepare('INSERT OR IGNORE INTO chats SELECT ?,name,type,last_message_at FROM chats WHERE id=?').run(target, source);
      this.db.prepare('INSERT OR IGNORE INTO messages SELECT ?,id,sender_id,from_me,timestamp,text,type,reply_to_message_id,expires_at,quoted FROM messages WHERE chat_id=?').run(target, source);
      this.db.prepare('UPDATE messages SET sender_id=? WHERE sender_id=?').run(target, source);
      for (const table of ['contacts', 'chats']) this.db.prepare(`DELETE FROM ${table} WHERE id=?`).run(source);
      this.db.prepare('DELETE FROM messages WHERE chat_id=?').run(source);
    });
  }
  contact(contact) {
    const id = this.canonical(contact.id);
    if (!id || id.endsWith('@g.us')) return;
    const existing = this.db.prepare('SELECT name FROM contacts WHERE id=?').get(id);
    const name = String(contact.name || contact.notify || contact.verifiedName || existing?.name || id).slice(0, 256);
    const phone = /^\d{6,15}@s\.whatsapp\.net$/.test(id) ? `+${id.split('@')[0]}` : null;
    this.db.prepare('INSERT OR REPLACE INTO contacts VALUES(?,?,?)').run(id, name, phone);
  }
  chat(chat) {
    const id = this.canonical(chat.id);
    if (!id) return;
    const existing = this.db.prepare('SELECT * FROM chats WHERE id=?').get(id);
    const timestamp = asNumber(chat.conversationTimestamp);
    this.db.prepare('INSERT OR REPLACE INTO chats VALUES(?,?,?,?)').run(id,
      String(chat.name || chat.subject || existing?.name || id).slice(0, 256),
      id.endsWith('@g.us') ? 'group' : 'direct',
      timestamp > 0 ? iso(timestamp * 1000) : existing?.last_message_at ?? null);
  }
  prune() {
    this.db.prepare('DELETE FROM messages WHERE timestamp<? OR (expires_at IS NOT NULL AND expires_at<=?)')
      .run(iso(this.now() - 30 * DAY), this.now());
  }
  message(message, { history = false } = {}) {
    const chatId = this.canonical(message.key?.remoteJid);
    const id = message.key?.id;
    const seconds = asNumber(message.messageTimestamp);
    if (!chatId || typeof id !== 'string' || !id || id.length > 256 || !Number.isFinite(seconds)) return;
    const timestamp = seconds * 1000;
    if (timestamp < 0 || timestamp > this.now() + DAY) return;
    const cleared = Number(this.meta('cleared_at') || 0);
    if (timestamp < this.now() - 30 * DAY || (cleared && timestamp <= cleared)) return;
    if (history && cleared) return;
    let content = message.message;
    if (!content || content.viewOnceMessage || content.viewOnceMessageV2 || content.viewOnceMessageV2Extension) return;
    const ephemeral = content.ephemeralMessage;
    if (ephemeral) content = ephemeral.message;
    if (!content || content.viewOnceMessage || content.viewOnceMessageV2 || content.viewOnceMessageV2Extension) return;
    if (content.protocolMessage) {
      const protocol = content.protocolMessage;
      if (protocol.type === 0 && protocol.key?.id) this.deleteMessage(chatId, protocol.key.id);
      if (protocol.editedMessage && protocol.key?.id) {
        this.editMessage(chatId, protocol.key.id, protocol.editedMessage);
      }
      return;
    }
    const body = content.extendedTextMessage;
    let text = typeof content.conversation === 'string' ? content.conversation
      : typeof body?.text === 'string' ? body.text : null;
    const oversized = text !== null && text.length > 20_000;
    if (oversized) text = null;
    const media = content.imageMessage || content.videoMessage || content.audioMessage || content.documentMessage || content.stickerMessage;
    const context = body?.contextInfo || media?.contextInfo;
    const expiration = asNumber(context?.expiration);
    // An ephemeral wrapper without a usable expiry cannot safely be archived.
    if (ephemeral && !(expiration > 0)) return;
    const expiresAt = expiration > 0 ? timestamp + expiration * 1000 : null;
    if (expiresAt !== null && expiresAt <= this.now()) return;
    const sender = this.canonical(message.key.participant || (message.key.fromMe ? this.meta('account_id') : chatId)) || chatId;
    const type = oversized ? 'unsupported_oversize' : text !== null ? 'text' : media ? 'media' : 'unsupported';
    // Quote only text. Retain no raw media URLs, keys, thumbnails, or protobuf payloads.
    const quoted = text !== null ? JSON.stringify({key: {id, remoteJid: chatId, fromMe: !!message.key.fromMe,
      ...(message.key.participant ? {participant: sender} : {})}, message: {conversation: text}}) : null;
    this.db.prepare('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)').run(chatId, id, sender,
      message.key.fromMe ? 1 : 0, iso(timestamp), text, type,
      typeof context?.stanzaId === 'string' && context.stanzaId.length <= 256 ? context.stanzaId : null, expiresAt, quoted);
    this.chat({id: chatId});
    this.db.prepare('UPDATE chats SET last_message_at=CASE WHEN last_message_at IS NULL OR last_message_at<? THEN ? ELSE last_message_at END WHERE id=?')
      .run(iso(timestamp), iso(timestamp), chatId);
  }
  editMessage(chatId, id, content) {
    let text = content.conversation ?? content.extendedTextMessage?.text;
    if (typeof text !== 'string') return;
    const row = this.db.prepare('SELECT quoted FROM messages WHERE chat_id=? AND id=?').get(this.canonical(chatId), id);
    if (!row) return;
    const quoted = row.quoted ? JSON.parse(row.quoted) : null;
    const oversized = text.length > 20_000;
    if (oversized) text = null;
    if (quoted) quoted.message = {conversation: text};
    this.db.prepare('UPDATE messages SET text=?,quoted=?,type=? WHERE chat_id=? AND id=?').run(text,
      quoted && !oversized ? JSON.stringify(quoted) : null, oversized ? 'unsupported_oversize' : 'text', this.canonical(chatId), id);
  }
  deleteMessage(chatId, id) {
    this.db.prepare('DELETE FROM messages WHERE chat_id=? AND id=?').run(this.canonical(chatId), id);
  }
  clear() {
    this.transaction(() => {
      this.db.exec('DELETE FROM messages; DELETE FROM chats; DELETE FROM contacts');
      this.setMeta('cleared_at', this.now());
      this.setMeta('sync_status', 'cleared');
    });
    this.db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
  }
  coverage() {
    this.prune();
    return {retention_cutoff: iso(this.now() - 30 * DAY),
      oldest_message_at: this.db.prepare('SELECT MIN(timestamp) AS oldest FROM messages').get().oldest,
      last_synced_at: this.meta('last_synced_at'), sync_status: this.meta('sync_status') || 'pending'};
  }
  read(operation, input) {
    this.prune();
    const limit = input.page_size ?? 50;
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) fail('invalid_input');
    const filter = {...input}; delete filter.cursor;
    const signature = createHash('sha256').update(JSON.stringify([operation, filter])).digest('hex');
    let offset = 0;
    if (input.cursor) {
      try {
        const cursor = JSON.parse(Buffer.from(input.cursor, 'base64url').toString());
        if (cursor.signature !== signature || !Number.isSafeInteger(cursor.offset) || cursor.offset < 0) fail('invalid_input');
        offset = cursor.offset;
      } catch { fail('invalid_input'); }
    }
    let sql, params = [];
    const query = String(input.query || '').toLocaleLowerCase();
    if (operation === 'whatsapp.contact.search') {
      sql = 'SELECT id,name,phone FROM contacts WHERE instr(lower(name),?)>0 OR instr(id,?)>0 OR instr(COALESCE(phone,\'\'),?)>0 ORDER BY name,id';
      params = [query, query, query];
    } else if (operation === 'whatsapp.chat.list') {
      sql = 'SELECT * FROM chats WHERE (instr(lower(name),?)>0 OR instr(id,?)>0)'; params = [query, query];
      if (input.type) { if (!['direct', 'group'].includes(input.type)) fail('invalid_input'); sql += ' AND type=?'; params.push(input.type); }
      sql += ' ORDER BY last_message_at DESC,id';
    } else if (operation === 'whatsapp.message.list' || operation === 'whatsapp.message.search') {
      if (operation.endsWith('.list') && !input.chat_id) fail('invalid_input');
      if (operation.endsWith('.search') && !query.trim()) fail('invalid_input');
      sql = 'SELECT id,chat_id,sender_id,from_me,timestamp,text,type,reply_to_message_id FROM messages WHERE 1=1';
      if (input.chat_id) {
        const chatId = this.canonical(input.chat_id); if (!chatId) fail('invalid_input');
        sql += ' AND chat_id=?'; params.push(chatId);
      }
      if (query) { sql += ' AND instr(lower(text),?)>0'; params.push(query); }
      for (const [field, sign] of [['since', '>='], ['until', '<=']]) {
        if (input[field]) {
          const time = Date.parse(input[field]); if (!Number.isFinite(time)) fail('invalid_input');
          sql += ` AND timestamp${sign}?`; params.push(iso(time));
        }
      }
      sql += ' ORDER BY timestamp DESC,chat_id,id';
    } else fail('operation_undeclared');
    const rows = this.db.prepare(`${sql} LIMIT ? OFFSET ?`).all(...params, limit + 1, offset);
    let items = rows.slice(0, limit).map(row => ({...row, ...('from_me' in row ? {from_me: !!row.from_me} : {})}));
    // Reduce pages without truncating individual message bodies.
    while (items.length && Buffer.byteLength(JSON.stringify(items)) > 3 * 1024 * 1024) items.pop();
    if (!items.length && rows.length) fail('response_too_large');
    return {items, next_cursor: rows.length > items.length
      ? Buffer.from(JSON.stringify({offset: offset + items.length, signature})).toString('base64url') : null,
      coverage: this.coverage()};
  }
  quote(chatId, id) {
    this.prune();
    const row = this.db.prepare('SELECT quoted FROM messages WHERE chat_id=? AND id=?').get(chatId, id);
    if (!row?.quoted) fail('not_found');
    const quoted = JSON.parse(row.quoted);
    quoted.key.remoteJid = chatId;
    if (quoted.key.participant) quoted.key.participant = this.canonical(quoted.key.participant) || quoted.key.participant;
    return quoted;
  }
  dispatch(id, digest) {
    const saved = this.db.prepare('SELECT * FROM dispatches WHERE id=?').get(id);
    if (saved) {
      if (saved.digest !== digest) fail('dispatch_conflict');
      if (saved.state !== 'accepted') fail('outcome_unknown');
      return JSON.parse(saved.result);
    }
    this.db.prepare('INSERT INTO dispatches VALUES(?,?,?,NULL)').run(id, digest, 'started');
    return null;
  }
  accepted(id, result) {
    this.db.prepare('UPDATE dispatches SET state=?,result=? WHERE id=?').run('accepted', JSON.stringify(result), id);
  }
  close() { this.db.close(); this.key.fill(0); }
}
