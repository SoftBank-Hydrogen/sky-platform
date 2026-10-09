import ws from 'k6/ws';
import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, fail } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const mode = __ENV.GAME_MODE || 'probe';
const endpoint = __ENV.GAME_WS_URL || 'ws://127.0.0.1:8080/ws';
const base = (__ENV.GAME_HTTP_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
if (!/^wss?:\/\/[^/?#@]+\/[^?#]*$/.test(endpoint) || !/^https?:\/\/[^/?#@]+$/.test(base)) {
  throw new Error('Set GAME_WS_URL and GAME_HTTP_URL without credentials or query');
}
if (!['probe', 'play'].includes(mode)) throw new Error('GAME_MODE must be probe or play');
if (mode === 'play' && __ENV.ALLOW_GAME_INPUT !== 'true') throw new Error('Play changes the game/DB; set ALLOW_GAME_INPUT=true for an isolated test game');
if (__ENV.ALLOW_INSECURE_HTTP !== 'true' &&
    ((!endpoint.startsWith('wss://') && !/^ws:\/\/(localhost|127\.0\.0\.1)(:\d+)?\//.test(endpoint)) ||
     (!base.startsWith('https://') && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base)))) {
  throw new Error('Use HTTPS/WSS; isolated HTTP/WS requires ALLOW_INSECURE_HTTP=true');
}
const taps = Number(__ENV.TAPS_PER_SECOND || 5);
if (!Number.isInteger(taps) || taps < 1 || taps > 10) throw new Error('TAPS_PER_SECOND must be 1..10');
const failures = new Rate('sky_game_session_failures');
const rtt = new Trend('sky_game_probe_rtt', true);
const headers = __ENV.GAME_ORIGIN ? { Origin: __ENV.GAME_ORIGIN } : {};
export const options = {
  scenarios: { game: { executor: 'shared-iterations', vus: mode === 'play' ? 2 : 1,
    iterations: mode === 'play' ? 2 : 1, maxDuration: '60s' } },
  thresholds: { sky_game_session_failures: ['rate==0'], sky_game_probe_rtt: ['p(95)<5000'], checks: ['rate==1'] },
};
export function setup() {
  const response = http.get(`${base}/stats`, { redirects: 0, timeout: '5s' });
  let stats;
  try { stats = response.json(); } catch { fail('Game /stats returned invalid JSON'); }
  if (response.status !== 200 || typeof stats?.connections?.players !== 'number') fail('Game stats preflight failed');
  if (mode === 'play' && stats.connections.players !== 0) fail('Play test requires an empty dedicated game');
  if (mode === 'play') {
    const scores = http.get(`${base}/api/scoreboard`, { redirects: 0, timeout: '5s' });
    let body;
    try { body = scores.json(); } catch { fail('Scoreboard preflight failed'); }
    if (scores.status !== 200 || typeof body?.rounds !== 'number') fail('Play requires a working scoreboard');
    return { beforeRounds: body.rounds };
  }
  return {};
}
export default function () {
  let failed = false, ack = false, welcome = false, result = false, phase = '';
  const pending = new Map();
  const response = ws.connect(endpoint, { headers, timeout: '5s' }, socket => {
    let expectedClose = false;
    const sendProbe = () => {
      const nonce = Array.from(new Uint8Array(crypto.randomBytes(16)), n => n.toString(16).padStart(2, '0')).join('');
      pending.set(nonce, Date.now());
      socket.send(JSON.stringify({ type: 'sky.probe', nonce }));
      socket.setTimeout(() => {
        if (pending.has(nonce)) { pending.delete(nonce); failed = true; socket.close(); }
      }, 5000);
    };
    socket.on('open', () => {
      sendProbe();
      if (mode === 'play') {
        socket.send(JSON.stringify({ type: 'join' }));
        socket.setInterval(() => { if (phase === 'playing') socket.send(JSON.stringify({ type: 'tap', n: 1 })); }, 1000 / taps);
        socket.setTimeout(() => { expectedClose = true; socket.close(); }, 45000);
      }
    });
    socket.on('message', raw => {
      let msg;
      try { msg = JSON.parse(raw); } catch { failed = true; return; }
      if (!msg || typeof msg.type !== 'string') { failed = true; socket.close(); return; }
      if (msg.type === 'sky.probe.ack' && pending.has(msg.nonce)) {
        const elapsed = Date.now() - pending.get(msg.nonce);
        pending.delete(msg.nonce); rtt.add(elapsed); ack = elapsed <= 5000;
        if (mode === 'probe') socket.close();
      }
      if (msg.type === 'welcome') welcome = true;
      if (msg.type === 'welcome' || msg.type === 'state') phase = msg.phase;
      if (msg.type === 'result') result = true;
    });
    socket.on('error', () => { failed = true; });
    socket.on('close', () => { if (mode === 'play' && !expectedClose) failed = true; });
    socket.setTimeout(() => { if (!ack) { failed = true; socket.close(); } }, 10000);
  });
  const ok = response?.status === 101 && !failed && ack && (mode !== 'play' || (welcome && result));
  failures.add(!ok);
  check(response, { 'Game handshake and app exchange verified': () => ok });
}
export function teardown(data) {
  if (mode !== 'play') return;
  const response = http.get(`${base}/api/scoreboard`, { redirects: 0, timeout: '5s' });
  let scores;
  try { scores = response.json(); } catch { scores = null; }
  check(response, { 'A completed round was stored': () => response.status === 200 && scores?.rounds > data.beforeRounds });
}
export function handleSummary(data) {
  return { stdout: JSON.stringify({ mode, note: 'Play uses one shared room; scoreboard readability is not restart durability', summary: data }, null, 2) + '\n' };
}
