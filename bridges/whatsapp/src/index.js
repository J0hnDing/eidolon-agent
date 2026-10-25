import * as baileys from '@whiskeysockets/baileys';
import qr from 'qrcode';
import { Bridge } from './bridge.js';
import { BridgeError } from './store.js';

const MAX_FRAME = 4 * 1024 * 1024;
const write = value => {
  const frame = JSON.stringify(value);
  if (Buffer.byteLength(frame) + 1 > MAX_FRAME) throw new BridgeError('response_too_large');
  process.stdout.write(`${frame}\n`);
};
const bridge = new Bridge({baileys, qr, emit: write});
let buffer = Buffer.alloc(0);
let initialized = false;
let initializing = false;
let requests = 0;
let closing = false;
const shutdown = () => { if (closing) return; closing = true; bridge.close(); process.exit(0); };
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
process.stdin.on('end', shutdown);
process.stdin.on('error', shutdown);
process.stdout.on('error', shutdown);
process.on('uncaughtException', () => { process.stderr.write('bridge_internal_failure\n'); shutdown(); });
process.on('unhandledRejection', () => { process.stderr.write('bridge_internal_failure\n'); shutdown(); });
process.stdin.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  for (;;) {
    const newline = buffer.indexOf(10);
    if (newline < 0) break;
    const frame = buffer.subarray(0, newline); buffer = buffer.subarray(newline + 1);
    if (frame.length > MAX_FRAME || requests >= 32) { shutdown(); return; }
    let request;
    try {
      request = JSON.parse(frame.toString('utf8'));
      if (!request || !['string', 'number'].includes(typeof request.id) || String(request.id).length > 128
        || typeof request.method !== 'string' || !request.input || typeof request.input !== 'object' || Array.isArray(request.input)) throw new Error();
      if ((!initialized && request.method !== 'initialize') || (request.method === 'initialize' && (initialized || initializing))) throw new Error();
    } catch { write({id: null, error: {code: 'invalid_request'}}); continue; }
    requests++;
    if (request.method === 'initialize') initializing = true;
    Promise.resolve().then(() => bridge.request(request.method, request.input)).then(result => {
      if (request.method === 'initialize') initialized = true;
      write({id: request.id, result});
    }).catch(error => write({id: request.id, error: {code: error instanceof BridgeError ? error.code : 'internal_failure'}}))
      .finally(() => { requests--; if (request.method === 'initialize') initializing = false; });
  }
  if (buffer.length > MAX_FRAME) shutdown();
});
