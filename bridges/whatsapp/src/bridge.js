import { createHash } from 'node:crypto';
import { rmSync } from 'node:fs';
import { join } from 'node:path';
import { Store, BridgeError, fail, iso, jid } from './store.js';

const noop = () => {};
const logger = {level: 'silent', trace: noop, debug: noop, info: noop, warn: noop, error: noop, fatal: noop};
logger.child = () => logger;
export function bounded(promise, milliseconds = 30_000) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new BridgeError('provider_timeout')), milliseconds);
  })]).finally(() => clearTimeout(timer));
}

export class Bridge {
  constructor({ baileys, qr, now = Date.now, emit = noop, sendTimeout = 10_000, pruneInterval = 60_000 }) {
    this.baileys = baileys; this.qr = qr; this.now = now; this.emit = emit; this.sendTimeout = sendTimeout;
    this.store = null; this.socket = null; this.timer = null; this.socketEpoch = 0;
    this.connectionStatus = 'disconnected'; this.qrData = null; this.error = null;
    this.backoff = 0; this.closed = false; this.sendInFlight = false; this.pruneInterval = pruneInterval; this.maintenance = null;
  }
  initialize({key, directory, generation}) {
    if (this.store) fail('already_initialized');
    if (typeof key !== 'string' || !/^[A-Za-z0-9+/]{43}=$/.test(key)) fail('invalid_input');
    const bytes = Buffer.from(key, 'base64');
    this.directory = directory;
    const {BufferJSON} = this.baileys;
    this.store = new Store(directory, bytes, generation, {now: this.now, codec: {
      stringify: v => JSON.stringify(v, BufferJSON.replacer),
      parse: v => JSON.parse(v, BufferJSON.reviver),
    }});
    // Validate saved encrypted state before opening a network connection.
    // WhatsApp Web pairing persists `me`; `registered` is a mobile-registration
    // flag and may remain false after a successful linked-device QR pairing.
    if (this.store.authGet('creds')?.me?.id) this.connect();
    this.maintenance = setInterval(() => {
      try { this.store?.prune(); } catch { this.onError(); }
    }, this.pruneInterval);
    this.maintenance.unref?.();
    return this.status();
  }
  status() {
    const coverage = this.store?.coverage() ?? {retention_cutoff: iso(this.now() - 30 * 86400_000),
      oldest_message_at: null, last_synced_at: null, sync_status: 'pending'};
    return {connected: this.connectionStatus === 'connected', status: this.connectionStatus,
      account_id: this.store?.meta('account_id') ?? null,
      account_login: this.store?.meta('account_login') ?? null,
      qr_data_url: this.qrData, error_type: this.error, ...coverage};
  }
  notify() { this.emit({event: 'status', data: this.status()}); }
  onError() { this.error = 'provider_unavailable'; this.notify(); }
  pair() {
    if (!this.store) fail('not_initialized');
    if (this.connectionStatus === 'connected' || this.connectionStatus === 'pairing') return this.status();
    if (this.connectionStatus === 'logged_out') {
      this.store.transaction(() => this.store.db.exec('DELETE FROM auth'));
      this.store.clear();
    }
    this.connect();
    return this.status();
  }
  connect() {
    if (this.closed || !this.store) return;
    clearTimeout(this.timer); this.timer = null;
    this.stopSocket();
    const epoch = ++this.socketEpoch;
    const {initAuthCreds, proto, Browsers} = this.baileys;
    const creds = this.store.authGet('creds') || initAuthCreds();
    const keys = {
      get: async (type, ids) => {
        const result = {};
        for (const id of ids) {
          let value = this.store.authGet(`${type}:${id}`);
          if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value);
          if (value != null) result[id] = value;
        }
        return result;
      },
      set: async (data) => this.store.transaction(() => {
        for (const [type, values] of Object.entries(data)) {
          for (const [id, value] of Object.entries(values)) this.store.authSet(`${type}:${id}`, value);
        }
      }),
    };
    this.connectionStatus = creds.me?.id ? 'connecting' : 'pairing';
    this.error = null; this.qrData = null;
    let socket;
    try {
      socket = this.baileys.default({auth: {creds, keys}, logger,
        browser: Browsers.ubuntu('Eidolon'), syncFullHistory: true,
        markOnlineOnConnect: false, generateHighQualityLinkPreview: false,
        shouldSyncHistoryMessage: () => !this.store.meta('cleared_at'),
        enableAutoSessionRecreation: false,
        getMessage: async key => {
          try { return this.store.quote(this.store.canonical(key.remoteJid), key.id)?.message; }
          catch { return undefined; }
        }});
    } catch { this.connectionStatus = 'unavailable'; this.error = 'provider_unavailable'; this.notify(); return; }
    this.socket = socket;
    const bind = (event, handler) => socket.ev.on(event, (...args) => {
      if (this.closed || epoch !== this.socketEpoch) return;
      try {
        const pending = handler(...args);
        if (pending?.catch) pending.catch(() => { if (epoch === this.socketEpoch) this.onError(); });
      } catch { this.onError(); }
    });
    bind('creds.update', update => {
      Object.assign(creds, update);
      this.store.transaction(() => this.store.authSet('creds', creds));
    });
    bind('connection.update', async update => {
      if (update.qr) {
        const data = await this.qr.toDataURL(update.qr, {errorCorrectionLevel: 'M', margin: 2, width: 280});
        if (epoch !== this.socketEpoch) return;
        this.qrData = data; this.connectionStatus = 'pairing'; this.notify();
      }
      if (update.connection === 'open') {
        const account = jid(socket.user?.id || creds.me?.id);
        if (!account) { this.error = 'invalid_account'; this.stopSocket(); this.connectionStatus = 'unavailable'; this.notify(); return; }
        const previous = this.store.meta('account_id');
        if (previous && previous !== account) {
          this.store.transaction(() => {
            this.store.db.exec('DELETE FROM messages; DELETE FROM chats; DELETE FROM contacts; DELETE FROM aliases; DELETE FROM dispatches');
          });
        }
        this.store.setMeta('account_id', account);
        this.store.setMeta('account_login', String(socket.user?.name || creds.me?.name || account).slice(0, 128));
        this.connectionStatus = 'connected'; this.qrData = null; this.error = null; this.backoff = 0;
        this.notify();
      } else if (update.connection === 'close') {
        this.qrData = null;
        const reason = update.lastDisconnect?.error?.output?.statusCode;
        this.stopSocket();
        if (reason === this.baileys.DisconnectReason.loggedOut || reason === this.baileys.DisconnectReason.badSession
          || reason === this.baileys.DisconnectReason.connectionReplaced || reason === this.baileys.DisconnectReason.multideviceMismatch
          || reason === this.baileys.DisconnectReason.forbidden) {
          this.connectionStatus = 'logged_out'; this.error = 'relink_required';
        } else {
          this.connectionStatus = 'reconnecting'; this.error = null;
          const delay = reason === this.baileys.DisconnectReason.restartRequired ? 50 : Math.min(30_000, 1000 * 2 ** Math.min(this.backoff++, 5));
          this.timer = setTimeout(() => this.connect(), delay); this.timer.unref?.();
        }
        this.notify();
      }
    });
    bind('contacts.upsert', contacts => contacts.forEach(contact => this.store.contact(contact)));
    bind('contacts.update', contacts => contacts.forEach(contact => this.store.contact(contact)));
    bind('chats.upsert', chats => chats.forEach(chat => this.store.chat(chat)));
    bind('chats.update', chats => chats.forEach(chat => this.store.chat(chat)));
    bind('groups.upsert', groups => groups.forEach(group => this.store.chat(group)));
    bind('groups.update', groups => groups.forEach(group => this.store.chat(group)));
    bind('lid-mapping.update', mapping => this.store.mapAlias(mapping.lid, mapping.pn));
    bind('messaging-history.set', batch => {
      for (const mapping of batch.lidPnMappings || []) this.store.mapAlias(mapping.lid, mapping.pn);
      this.store.transaction(() => {
        if (!this.store.meta('cleared_at')) {
          for (const contact of batch.contacts || []) this.store.contact(contact);
          for (const chat of batch.chats || []) this.store.chat(chat);
        }
        for (const message of batch.messages || []) this.store.message(message, {history: true});
        this.store.setMeta('last_synced_at', iso(this.now()));
        this.store.setMeta('sync_status', this.store.meta('cleared_at') ? 'cleared' : batch.isLatest ? 'complete' : 'syncing');
      });
      this.notify();
    });
    bind('messaging-history.status', state => {
      this.store.setMeta('sync_status', this.store.meta('cleared_at') ? 'cleared' : state.status === 'complete' ? 'complete' : 'paused'); this.notify();
    });
    bind('messages.upsert', batch => {
      this.store.transaction(() => {
        for (const message of batch.messages || []) this.store.message(message);
        this.store.setMeta('last_synced_at', iso(this.now()));
      });
    });
    bind('messages.update', batch => {
      for (const {key, update} of batch) {
        if (update.message === null) this.store.deleteMessage(key.remoteJid, key.id);
        else if (update.message) this.store.editMessage(key.remoteJid, key.id, update.message.editedMessage?.message || update.message);
      }
    });
    bind('messages.delete', batch => {
      if (batch.all && batch.jid) this.store.db.prepare('DELETE FROM messages WHERE chat_id=?').run(this.store.canonical(batch.jid));
      for (const key of batch.keys || []) this.store.deleteMessage(key.remoteJid, key.id);
    });
    bind('chats.delete', ids => {
      for (const id of ids) {
        this.store.db.prepare('DELETE FROM chats WHERE id=?').run(this.store.canonical(id));
        this.store.db.prepare('DELETE FROM messages WHERE chat_id=?').run(this.store.canonical(id));
      }
    });
    this.notify();
  }
  stopSocket() {
    ++this.socketEpoch;
    if (this.socket) {
      this.socket.ev.removeAllListeners?.();
      try { this.socket.end(new Error('bridge_stopped')); } catch { /* no provider error content */ }
      this.socket = null;
    }
  }
  reconnect() {
    if (!this.store?.authGet('creds')?.me?.id || this.connectionStatus === 'logged_out') fail('relink_required');
    this.connect(); return this.status();
  }
  async execute({operation, input = {}, dispatch_id: dispatchId, account_id: expectedAccount}) {
    if (!this.store) fail('not_initialized');
    const account = this.store.meta('account_id');
    if (!account || expectedAccount !== account) fail('connection_changed');
    if (operation !== 'whatsapp.message.send') return this.store.read(operation, input);
    if (this.sendInFlight) fail('operation_in_progress');
    if (this.connectionStatus !== 'connected' || !this.socket) fail('connection_unavailable');
    if (typeof input.text !== 'string' || !input.text.trim() || [...input.text].length > 4000) fail('invalid_input');
    if (typeof dispatchId !== 'string' || !/^[a-zA-Z0-9:_-]{1,200}$/.test(dispatchId)) fail('invalid_input');
    let chatId = this.store.canonical(input.recipient);
    if (!chatId) fail('invalid_input');
    const digest = createHash('sha256').update(JSON.stringify([account, input])).digest('hex');
    const saved = this.store.dispatch(dispatchId, digest);
    if (saved) return saved;
    this.sendInFlight = true;
    try {
      const socket = this.socket;
      if (input.recipient.startsWith('+')) {
        const matches = await bounded(socket.onWhatsApp(input.recipient.slice(1)), this.sendTimeout);
        const match = matches?.find(value => value.exists && jid(value.jid));
        if (!match) fail('not_found');
        chatId = this.store.canonical(match.jid);
      }
      const quoted = input.reply_to_message_id ? this.store.quote(chatId, input.reply_to_message_id) : undefined;
      if (socket !== this.socket || this.connectionStatus !== 'connected' || account !== this.store.meta('account_id')) fail('connection_changed');
      let sent;
      try {
        sent = await bounded(socket.sendMessage(chatId, {text: input.text}, {quoted, linkPreview: null}), this.sendTimeout);
      } catch { fail('outcome_unknown'); }
      if (typeof sent?.key?.id !== 'string' || !sent.key.id || sent.key.id.length > 256) fail('outcome_unknown');
      const result = {accepted: true, message_id: sent.key.id, chat_id: chatId};
      this.store.accepted(dispatchId, result);
      this.store.message({...sent, key: {...sent.key, remoteJid: chatId, fromMe: true},
        messageTimestamp: sent.messageTimestamp || Math.floor(this.now() / 1000), message: {conversation: input.text}});
      return result;
    } finally { this.sendInFlight = false; }
  }
  async disconnect() {
    if (this.sendInFlight) fail('operation_in_progress');
    clearTimeout(this.timer); this.timer = null;
    // Detach before remote logout: logout itself emits a close event which must
    // never schedule a reconnect after this explicit disconnect.
    const socket = this.socket;
    ++this.socketEpoch;
    socket?.ev.removeAllListeners?.();
    this.socket = null;
    const wasConnected = this.connectionStatus === 'connected';
    this.connectionStatus = 'disconnecting'; this.qrData = null;
    let remoteLogoutFailed = false;
    if (this.store?.authGet('creds')?.me?.id) {
      try {
        if (!socket || !wasConnected) throw new Error('offline');
        await bounded(socket.logout(), 5000);
      } catch { remoteLogoutFailed = true; }
    }
    try { socket?.end(new Error('bridge_stopped')); } catch { /* close after logout */ }
    clearInterval(this.maintenance); this.maintenance = null;
    if (this.store) { this.store.close(); this.store = null; }
    for (const suffix of ['', '-wal', '-shm']) rmSync(join(this.directory, `whatsapp.sqlite${suffix}`), {force: true});
    this.connectionStatus = 'disconnected'; this.qrData = null; this.error = null;
    return {remote_logout_failed: remoteLogoutFailed};
  }
  async request(method, input) {
    if (method === 'initialize') return this.initialize(input);
    if (!this.store) fail('not_initialized');
    if (method === 'status') return this.status();
    if (method === 'pair') return this.pair();
    if (method === 'reconnect') return this.reconnect();
    if (method === 'clear_cache') { this.store.clear(); return this.status(); }
    if (method === 'disconnect') return this.disconnect();
    if (method === 'execute') return this.execute(input);
    fail('invalid_method');
  }
  close() {
    this.closed = true; clearTimeout(this.timer); clearInterval(this.maintenance); this.stopSocket();
    this.store?.close(); this.store = null;
  }
}
