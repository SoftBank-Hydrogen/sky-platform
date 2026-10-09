'use strict';
const http = require('node:http');
const fs = require('node:fs');
const { randomBytes } = require('node:crypto');
const { performance } = require('node:perf_hooks');
const WebSocket = require('ws');
const { discover, validateDiscovery } = require('./discovery.cjs');
const JOBS = Symbol('discoveryJobs');

class ObservationError extends Error {
  constructor(reason) { super('Observation failed'); this.reason = reason; }
}
function reasonFor(error) {
  if (error instanceof ObservationError) return error.reason;
  if (error instanceof SyntaxError) return 'schema';
  if (['AbortError', 'TimeoutError'].includes(error?.name)) return 'timeout';
  if (error instanceof TypeError) return 'network';
  return 'other';
}

function validateTargets(targets) {
  if (!Array.isArray(targets) || targets.length > 20) throw new Error('Expected at most 20 targets');
  const names = new Set();
  for (const target of targets) {
    if (!target || !/^[a-zA-Z0-9_-]{1,48}$/.test(target.name) || names.has(target.name)) throw new Error('Invalid/duplicate target name');
    names.add(target.name);
    if (!['sky', 'game'].includes(target.kind)) throw new Error('Target kind must be sky or game');
    validateDiscovery(target);
    const url = new URL(target.httpUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new Error('httpUrl must be an HTTP(S) origin without credentials');
    }
    if (target.kind === 'game') {
      const ws = new URL(target.wsUrl);
      if (!['ws:', 'wss:'].includes(ws.protocol) || ws.username || ws.password || ws.search || ws.hash) throw new Error('Invalid wsUrl');
    }
  }
  return targets;
}
function credentials(target) {
  const headers = {};
  if (target.cookieFile) {
    try {
      if (fs.statSync(target.cookieFile).size > 65536) throw new Error();
      const cookie = fs.readFileSync(target.cookieFile, 'utf8').trim();
      if (!cookie || /[\r\n\0]/.test(cookie)) throw new Error();
      headers.Cookie = cookie;
    } catch { throw new ObservationError('credentials'); }
  }
  return headers;
}
async function get(target, path, headers = {}) {
  const response = await fetch(new URL(path, target.httpUrl), {
    headers: { ...credentials(target), ...headers }, redirect: 'manual', signal: AbortSignal.timeout(5000),
  });
  if (response.status !== 200) {
    await response.body?.cancel();
    throw new ObservationError([401, 403].includes(response.status) ? 'auth' : 'http');
  }
  // Current endpoints can grow with jobs; bound the collector's response memory.
  let bytes = 0;
  const chunks = [];
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > 4 * 1024 * 1024) throw new ObservationError('schema');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}
function probe(target) {
  return new Promise(resolve => {
    const nonce = randomBytes(16).toString('hex');
    let sentAt;
    let done = false;
    const ws = new WebSocket(target.wsUrl, { headers: credentials(target),
      ...(target.origin ? { origin: target.origin } : {}), handshakeTimeout: 5000, maxPayload: 65536 });
    const finish = result => {
      if (done) return;
      done = true; clearTimeout(timer); ws.terminate(); resolve(result);
    };
    // Total handshake + exchange budget, and exchange itself must be <= 5 seconds.
    const timer = setTimeout(() => finish({ ok: false }), 10000);
    ws.on('open', () => {
      sentAt = performance.now();
      ws.send(JSON.stringify({ type: 'sky.probe', nonce }));
    });
    ws.on('message', raw => {
      let msg;
      try { msg = JSON.parse(raw); } catch { return; }
      const elapsed = (performance.now() - sentAt) / 1000;
      if (msg && msg.type === 'sky.probe.ack' && msg.nonce === nonce && elapsed <= 5) finish({ ok: true, seconds: elapsed });
    });
    ws.on('error', () => finish({ ok: false }));
    ws.on('close', () => finish({ ok: false }));
  });
}
const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
async function collect(target) {
  const observations = {};
  const started = performance.now();
  try {
    if (target.kind === 'sky') {
      const health = JSON.parse(await get(target, '/health'));
      if (health?.status !== 'ok') throw new ObservationError('schema');
      observations.sky_observed_http_up = 1;
      const page = await get(target, '/');
      const token = page.match(/const token='([A-Za-z0-9_-]{32,128})';/)?.[1];
      if (!token) throw new ObservationError('schema');
      const jobs = JSON.parse(await get(target, '/api/jobs', { 'X-Sky-Token': token }));
      if (!Array.isArray(jobs) || jobs.some(job => !job || typeof job.status !== 'string')) throw new ObservationError('schema');
      const counts = { succeeded: 0, failed: 0, other: 0 };
      for (const job of jobs) counts[Object.hasOwn(counts, job.status) ? job.status : 'other']++;
      observations.sky_observed_jobs = counts;
      if (target.discovery) observations[JOBS] = { jobs, token };
    } else {
      const stats = JSON.parse(await get(target, '/stats'));
      if (!finite(stats?.connections?.players) || !finite(stats?.connections?.others)) throw new ObservationError('schema');
      observations.sky_observed_http_up = 1;
      observations.sky_game_players = stats.connections.players;
      observations.sky_game_nonplayer_connections = stats.connections.others;
      for (const [key, field] of [['sky_game_messages', 'messages'], ['sky_game_messages_rejected', 'messagesRejected'],
        ['sky_game_taps_accepted', 'tapsAccepted'], ['sky_game_taps_limited', 'tapsLimited']]) {
        if (!finite(stats.totals?.[field])) throw new ObservationError('schema');
        observations[key] = stats.totals[field];
      }
      try {
        const scores = JSON.parse(await get(target, '/api/scoreboard'));
        observations.sky_game_scoreboard_up = finite(scores.rounds) ? 1 : 0;
        if (finite(scores.rounds)) observations.sky_game_stored_rounds = scores.rounds;
      } catch { observations.sky_game_scoreboard_up = 0; }
      const exchange = await probe(target);
      observations.sky_observed_websocket_up = Number(exchange.ok);
      if (exchange.ok) observations.sky_observed_websocket_rtt_seconds = exchange.seconds;
    }
    observations.sky_observed_collection_up = 1;
    observations.sky_observed_collection_error = 'none';
    observations.sky_observed_last_success_timestamp_seconds = Date.now() / 1000;
  } catch (error) {
    // Clear stale application samples; never repeat a previous "healthy" observation.
    return { sky_observed_http_up: observations.sky_observed_http_up || 0, sky_observed_collection_up: 0,
      sky_observed_collection_error: reasonFor(error),
      ...(target.kind === 'game' ? { sky_observed_websocket_up: 0, sky_game_scoreboard_up: 0 } : {}) };
  }
  observations.sky_observed_collection_duration_seconds = (performance.now() - started) / 1000;
  return observations;
}
function render(targets, snapshots) {
  const lines = ['# HELP sky_observer_targets Number of configured observation targets', '# TYPE sky_observer_targets gauge',
    `sky_observer_targets ${targets.length}`];
  const declared = new Set();
  function sample(name, labels, value) {
    if (!declared.has(name)) {
      declared.add(name);
      lines.push(`# HELP ${name} Observer measurement; see monitoring README`,
        `# TYPE ${name} ${/^sky_game_(messages|messages_rejected|taps_accepted|taps_limited)$/.test(name) ? 'counter' : 'gauge'}`);
    }
    lines.push(`${name}{${labels}} ${value}`);
  }
  for (const target of targets) {
    const labels = `target="${target.name}",kind="${target.kind}"`;
    const snapshot = snapshots.get(target.name) || { sky_observed_collection_up: 0 };
    for (const [name, value] of Object.entries(snapshot)) {
      if (name === 'sky_observed_jobs') {
        for (const [status, count] of Object.entries(value)) sample(name, `${labels},status="${status}"`, count);
      } else if (name === 'sky_observed_collection_error') {
        for (const reason of ['auth', 'http', 'schema', 'timeout', 'network', 'credentials', 'other']) {
          sample(name, `${labels},reason="${reason}"`, Number(value === reason));
        }
      } else sample(name, labels, value);
    }
  }
  return lines.join('\n') + '\n';
}
function main() {
  const targets = validateTargets(JSON.parse(fs.readFileSync(process.env.TARGETS_FILE || '/config/targets.json', 'utf8')));
  const interval = Number(process.env.POLL_SECONDS || 30);
  if (!Number.isInteger(interval) || interval < 15 || interval > 300) throw new Error('POLL_SECONDS must be 15..300');
  const snapshots = new Map();
  const discovered = new Map();
  let currentTargets = targets;
  let active = false;
  async function poll() {
    if (active) return;
    active = true;
    try {
      await Promise.all(targets.map(async target => {
        const snapshot = await collect(target);
        if (target.discovery) {
          try {
            if (!snapshot[JOBS]) throw new Error();
            const { jobs, token } = snapshot[JOBS];
            const result = await discover(target, jobs, async id => JSON.parse(await get(target, `/api/jobs/${id}`, { 'X-Sky-Token': token })));
            const capacity = Math.floor(Math.max(0,20-targets.length) / targets.filter(item => item.discovery).length);
            discovered.set(target.name, result.targets.slice(0, capacity));
            snapshot.sky_discovery_up = 1;
            snapshot.sky_discovery_rejected = result.rejected + Math.max(0, result.targets.length - capacity);
          } catch { snapshot.sky_discovery_up = 0; }
        }
        delete snapshot[JOBS];
        snapshots.set(target.name, snapshot);
      }));
      const dynamic = [...discovered.values()].flat().filter(item => !targets.some(target => target.name === item.name)).slice(0, Math.max(0,20-targets.length));
      currentTargets = [...targets, ...dynamic];
      await Promise.all(dynamic.map(async target => snapshots.set(target.name, await collect(target))));
      const activeNames = new Set(currentTargets.map(target => target.name));
      for (const name of snapshots.keys()) if (!activeNames.has(name)) snapshots.delete(name);
    }
    finally { active = false; }
  }
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/metrics') {
      res.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' });
      return res.end(render(currentTargets, snapshots));
    }
    if (req.method === 'GET' && req.url === '/health') { res.writeHead(200); return res.end('ok'); }
    res.writeHead(404); res.end();
  });
  server.listen(9108, process.env.BIND_HOST || '127.0.0.1');
  const timer = setInterval(poll, interval * 1000);
  void poll();
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => { clearInterval(timer); server.close(); });
}
if (require.main === module) main();
module.exports = { validateTargets, collect, render };
