import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import * as realBaileys from '@whiskeysockets/baileys';
import { Bridge } from '../src/bridge.js';
import { Store, DAY, jid } from '../src/store.js';

const ACCOUNT = '15551234567@s.whatsapp.net';
const PEER = '15557654321@s.whatsapp.net';
const GROUP = '120363000000000000@g.us';
const NOW = Date.parse('2026-10-24T16:00:00Z');
const tick = () => new Promise(resolve => setImmediate(resolve));
const key = () => Buffer.alloc(32, 7);
function setup(t, options = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'eidolon-whatsapp-'));
  let currentTime = NOW;
  const sockets = [], events = [], additional = [];
  const baileys = {...realBaileys, default: config => {
    const socket = {ev: new EventEmitter(), user: {id: ACCOUNT, name: 'Test account'}, config,
      sendCount: 0, resolveCount: 0,
      end() {}, async logout() {},
      async onWhatsApp() { this.resolveCount++; return [{exists: true, jid: PEER}]; },
      async sendMessage(chatId, content, sendOptions) {
        this.sendCount++; this.lastSend = {chatId, content, sendOptions};
        return {key: {id: 'sent-1', remoteJid: chatId}, messageTimestamp: Math.floor(currentTime / 1000)};
      }};
    sockets.push(socket); return socket;
  }};
  const bridge = new Bridge({baileys, qr: {toDataURL: async value => `data:image/png;base64,${value}`},
    now: () => currentTime, emit: event => events.push(event), ...options});
  bridge.initialize({directory, generation: 'generation1', key: key().toString('base64')});
  t.after(() => { additional.forEach(other => other.close()); bridge.close(); rmSync(directory, {recursive: true, force: true}); });
  const pair = () => {
    bridge.pair();
    const socket = sockets.at(-1);
    socket.ev.emit('creds.update', {registered: false, me: {id: ACCOUNT}});
    socket.ev.emit('connection.update', {connection: 'open'});
    return socket;
  };
  const message = (id, text = 'hello', extra = {}) => ({key: {remoteJid: PEER, id, fromMe: false},
    messageTimestamp: Math.floor(currentTime / 1000), message: {conversation: text}, ...extra});
  return {bridge, sockets, directory, events, pair, message, additional, advance: n => { currentTime += n; }};
}

test('initialization stays offline until pairing and QR is status-only', async t => {
  const {bridge, sockets, events} = setup(t);
  assert.equal(sockets.length, 0);
  bridge.pair();
  sockets[0].ev.emit('connection.update', {qr: 'test-code'});
  await tick();
  assert.equal(bridge.status().status, 'pairing');
  assert.equal(bridge.status().qr_data_url, 'data:image/png;base64,test-code');
  assert.equal(events.at(-1).event, 'status');
  assert.equal('key' in events.at(-1).data, false);
});

test('encrypted credentials and all Signal key categories survive restart', async t => {
  const {bridge, directory, pair, additional} = setup(t);
  const socket = pair();
  await socket.config.auth.keys.set({session: {'peer.0': Buffer.from('secret-signal-session')},
    'app-state-sync-key': {one: {keyData: Buffer.from('sync-secret')}}});
  assert.match(socket.config.auth.creds.me.id, /s.whatsapp.net$/);
  assert.equal(socket.config.auth.creds.registered, false);
  const disk = Buffer.concat(['', '-wal'].map(suffix => readFileSync(join(directory, `whatsapp.sqlite${suffix}`))));
  assert.equal(disk.includes(Buffer.from('secret-signal-session')), false);
  bridge.close();
  const restored = new Bridge({baileys: {...realBaileys, default: config => ({config, ev: new EventEmitter(), end() {}})},
    qr: {}, now: () => NOW});
  additional.push(restored);
  restored.initialize({directory, generation: 'generation1', key: key().toString('base64')});
  assert.equal(restored.connectionStatus, 'connecting');
  const values = await restored.socket.config.auth.keys.get('session', ['peer.0']);
  assert.equal(Buffer.from(values['peer.0']).toString(), 'secret-signal-session');
  const sync = await restored.socket.config.auth.keys.get('app-state-sync-key', ['one']);
  assert.equal(Buffer.from(sync.one.keyData).toString(), 'sync-secret');
  await restored.socket.config.auth.keys.set({session: {'peer.0': null}});
  assert.deepEqual(await restored.socket.config.auth.keys.get('session', ['peer.0']), {});
  assert.equal(restored.reconnect().status, 'connecting');
  assert.equal(restored.socket.config.auth.creds.me.id, ACCOUNT);
  assert.equal(restored.status().qr_data_url, null);
});

test('wrong encryption key and connection generation fail without creating a socket', t => {
  const {bridge, directory, pair} = setup(t);
  pair(); bridge.close();
  assert.throws(() => new Store(directory, key(), 'other'), {code: 'connection_changed'});
  const wrong = new Store(directory, Buffer.alloc(32, 8), 'generation1');
  assert.throws(() => wrong.authGet('creds'), {code: 'auth_storage_invalid'});
  wrong.close();
});

test('history ingestion deduplicates and migrates LID aliases', t => {
  const {bridge, pair, message} = setup(t);
  const socket = pair();
  socket.ev.emit('messages.upsert', {messages: [message('one', 'from LID', {key: {remoteJid: '123456@lid', id: 'one'}})]});
  socket.ev.emit('messaging-history.set', {lidPnMappings: [{lid: '123456@lid', pn: PEER}],
    contacts: [{id: PEER, name: 'Friend'}], chats: [{id: PEER, name: 'Friend'}],
    messages: [message('one', 'updated'), message('two', 'second')], isLatest: true});
  const result = bridge.store.read('whatsapp.message.list', {chat_id: '123456@lid'});
  assert.equal(result.items.length, 2);
  assert(result.items.every(item => item.chat_id === PEER));
  assert.equal(result.items.find(item => item.id === 'one').text, 'updated');
  assert.equal(result.coverage.sync_status, 'complete');
  assert.equal(bridge.store.read('whatsapp.contact.search', {query: 'friend'}).items[0].phone, '+15557654321');
});

test('only 30 days are retained and view-once and media secrets are excluded', t => {
  const {bridge, pair, message, directory, advance} = setup(t);
  const socket = pair();
  socket.ev.emit('messages.upsert', {messages: [message('current'),
    message('old', 'old message', {messageTimestamp: (NOW - 31 * DAY) / 1000}),
    message('viewonce', '', {message: {viewOnceMessageV2: {message: {imageMessage: {caption: 'private'}}}}}),
    message('media', '', {message: {imageMessage: {url: 'secret-media-url', mediaKey: 'secret-media-key'}}})]});
  const rows = bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items;
  assert.deepEqual(rows.map(row => row.id).sort(), ['current', 'media']);
  assert.equal(rows.find(row => row.id === 'media').text, null);
  const data = readFileSync(join(directory, 'whatsapp.sqlite-wal'));
  assert.equal(data.includes(Buffer.from('secret-media-url')), false);
  advance(31 * DAY);
  assert.equal(bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items.length, 0);
});

test('disappearing messages expire and edits/deletions update cached text', t => {
  const {bridge, pair, message, advance} = setup(t);
  const socket = pair();
  socket.ev.emit('messages.upsert', {messages: [message('edit'),
    message('expires', '', {message: {ephemeralMessage: {message: {extendedTextMessage: {
      text: 'gone soon', contextInfo: {expiration: 10}}}}}})]});
  socket.ev.emit('messages.update', [{key: {remoteJid: PEER, id: 'edit'}, update: {message: {conversation: 'changed'}}}]);
  assert.equal(bridge.store.quote(PEER, 'edit').message.conversation, 'changed');
  advance(11_000);
  assert.deepEqual(bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items.map(row => row.text), ['changed']);
  socket.ev.emit('messages.delete', {keys: [{remoteJid: PEER, id: 'edit'}]});
  assert.equal(bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items.length, 0);
});

test('clearing messages preserves pairing and rejects historical replay', t => {
  const {bridge, pair, message, advance} = setup(t);
  const socket = pair();
  socket.ev.emit('messages.upsert', {messages: [message('old')]});
  bridge.store.clear();
  socket.ev.emit('messaging-history.set', {contacts: [{id: PEER, name: 'Cleared friend'}],
    chats: [{id: PEER, name: 'Cleared friend'}], messages: [message('old')], isLatest: true});
  assert.equal(bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items.length, 0);
  assert.equal(bridge.store.read('whatsapp.chat.list', {}).items.length, 0);
  assert.equal(bridge.store.read('whatsapp.contact.search', {}).items.length, 0);
  assert.equal(bridge.store.authGet('creds').me.id, ACCOUNT);
  advance(2000);
  socket.ev.emit('messages.upsert', {messages: [message('new')]});
  assert.deepEqual(bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items.map(row => row.id), ['new']);
});

test('oversize text and edited text cannot poison bounded read responses', t => {
  const {bridge, pair, message} = setup(t);
  const socket = pair();
  socket.ev.emit('messages.upsert', {messages: [message('huge', 'x'.repeat(20_001)),
    message('edited', 'small'), message('stanza', '', {message: {extendedTextMessage: {text: 'fine', contextInfo: {stanzaId: 'x'.repeat(300)}}}})]});
  socket.ev.emit('messages.update', [{key: {remoteJid: PEER, id: 'edited'}, update: {message: {conversation: 'z'.repeat(20_001)}}}]);
  const items = bridge.store.read('whatsapp.message.list', {chat_id: PEER}).items;
  assert.equal(items.length, 3);
  assert(items.filter(item => ['huge', 'edited'].includes(item.id)).every(item => item.type === 'unsupported_oversize' && item.text === null));
  assert.equal(items.find(item => item.id === 'stanza').reply_to_message_id, null);
});

test('idle retention maintenance removes expired messages without a read', async t => {
  const {bridge, pair, message, advance} = setup(t, {pruneInterval: 10});
  pair(); bridge.store.message(message('expires'));
  advance(31 * DAY);
  await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(bridge.store.db.prepare('SELECT COUNT(*) AS count FROM messages').get().count, 0);
});

test('disconnect detaches logout callbacks before remote close can schedule reconnect', async t => {
  const {bridge, pair} = setup(t);
  const socket = pair();
  socket.logout = async () => socket.ev.emit('connection.update', {connection: 'close', lastDisconnect: {error: {output: {statusCode: 500}}}});
  assert.deepEqual(await bridge.disconnect(), {remote_logout_failed: false});
  assert.equal(bridge.timer, null);
  assert.equal(bridge.status().status, 'disconnected');
});

test('pagination is bounded and cursor filters cannot be changed', t => {
  const {bridge, pair, message} = setup(t);
  pair();
  for (let i = 0; i < 5; i++) bridge.store.message(message(`message-${i}`));
  const first = bridge.store.read('whatsapp.message.search', {query: 'hello', page_size: 2});
  const next = bridge.store.read('whatsapp.message.search', {query: 'hello', page_size: 2, cursor: first.next_cursor});
  assert.equal(next.items.length, 2);
  assert.equal(new Set([...first.items, ...next.items].map(row => row.id)).size, 4);
  assert.throws(() => bridge.store.read('whatsapp.message.search', {query: 'changed', page_size: 2, cursor: first.next_cursor}), {code: 'invalid_input'});
  assert.throws(() => bridge.store.read('whatsapp.chat.list', {page_size: 101}), {code: 'invalid_input'});
  assert.throws(() => bridge.store.read('whatsapp.message.search', {query: ''}), {code: 'invalid_input'});
});

test('execute enforces account identity and sends approved text without marking read', async t => {
  const {bridge, pair} = setup(t);
  const socket = pair();
  await assert.rejects(bridge.execute({operation: 'whatsapp.message.send', input: {recipient: PEER, text: 'hello'},
    account_id: 'wrong', dispatch_id: 'approval:1'}), {code: 'connection_changed'});
  assert.equal(socket.sendCount, 0);
  const result = await bridge.execute({operation: 'whatsapp.message.send', input: {recipient: '+15557654321', text: 'hello'},
    account_id: ACCOUNT, dispatch_id: 'approval:1'});
  assert.deepEqual(result, {accepted: true, message_id: 'sent-1', chat_id: PEER});
  assert.equal(socket.resolveCount, 1);
  assert.deepEqual(socket.lastSend.content, {text: 'hello'});
  assert.equal(socket.lastSend.sendOptions.linkPreview, null);
});

test('accepted dispatches are idempotent and cannot change payload', async t => {
  const {bridge, pair} = setup(t);
  const socket = pair();
  const request = {operation: 'whatsapp.message.send', input: {recipient: GROUP, text: 'hello'}, account_id: ACCOUNT, dispatch_id: 'approval:5'};
  const first = await bridge.execute(request);
  assert.deepEqual(await bridge.execute(request), first);
  assert.equal(socket.sendCount, 1);
  await assert.rejects(bridge.execute({...request, input: {...request.input, text: 'changed'}}), {code: 'dispatch_conflict'});
});

test('send timeout is outcome_unknown and never retried', async t => {
  const {bridge, pair} = setup(t, {sendTimeout: 20});
  const socket = pair();
  socket.sendMessage = async () => { socket.sendCount++; return new Promise(() => {}); };
  const request = {operation: 'whatsapp.message.send', input: {recipient: GROUP, text: 'hello'}, account_id: ACCOUNT, dispatch_id: 'approval:6'};
  await assert.rejects(bridge.execute(request), {code: 'outcome_unknown'});
  await assert.rejects(bridge.execute(request), {code: 'outcome_unknown'});
  assert.equal(socket.sendCount, 1);
});

test('quoted replies must reference a retained text message in that chat', async t => {
  const {bridge, pair, message} = setup(t);
  const socket = pair(); bridge.store.message(message('quote', 'original'));
  await bridge.execute({operation: 'whatsapp.message.send', input: {recipient: PEER, text: 'reply', reply_to_message_id: 'quote'},
    account_id: ACCOUNT, dispatch_id: 'approval:7'});
  assert.equal(socket.lastSend.sendOptions.quoted.message.conversation, 'original');
  await assert.rejects(bridge.execute({operation: 'whatsapp.message.send', input: {recipient: GROUP, text: 'reply', reply_to_message_id: 'quote'},
    account_id: ACCOUNT, dispatch_id: 'approval:8'}), {code: 'not_found'});
  assert.equal(socket.sendCount, 1);
});

test('logout requires explicit relinking and disconnect removes local data even offline', async t => {
  const {bridge, pair, directory} = setup(t);
  const socket = pair();
  socket.ev.emit('connection.update', {connection: 'close', lastDisconnect: {error: {output: {statusCode: realBaileys.DisconnectReason.loggedOut}}}});
  assert.equal(bridge.status().status, 'logged_out');
  assert.equal(bridge.timer, null);
  assert.throws(() => bridge.reconnect(), {code: 'relink_required'});
  assert.deepEqual(await bridge.disconnect(), {remote_logout_failed: true});
  assert.throws(() => readFileSync(join(directory, 'whatsapp.sqlite')), {code: 'ENOENT'});
});

test('JID validation supports modern/legacy groups and rejects other surfaces', () => {
  assert.equal(jid(GROUP), GROUP);
  assert.equal(jid('123456-456789@g.us'), '123456-456789@g.us');
  assert.equal(jid('123456:8@lid'), '123456@lid');
  assert.equal(jid('status@broadcast'), null);
  assert.equal(jid('123@newsletter'), null);
});

test('stdio framing handles fragmented input and emits only sanitized error codes', async t => {
  const cwd = dirname(dirname(fileURLToPath(import.meta.url)));
  const child = spawn(process.execPath, ['src/index.js'], {cwd, stdio: ['pipe', 'pipe', 'pipe']});
  t.after(() => child.kill());
  const pending = [], lines = []; let buffer = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    buffer += chunk;
    while (buffer.includes('\n')) {
      const index = buffer.indexOf('\n');
      const line = JSON.parse(buffer.slice(0, index)); buffer = buffer.slice(index + 1);
      if (pending.length) pending.shift()(line); else lines.push(line);
    }
  });
  const next = () => lines.length ? Promise.resolve(lines.shift()) : new Promise(resolve => pending.push(resolve));
  child.stdin.write('{"id":1,"method":"sta');
  child.stdin.write('tus","input":{"secret":"not-in-output"}}\n');
  const result = await next();
  assert.deepEqual(result, {id: null, error: {code: 'invalid_request'}});
  assert.equal(JSON.stringify(result).includes('not-in-output'), false);
  const exited = new Promise(resolve => child.on('exit', resolve));
  child.stdin.end(); await exited;
});
