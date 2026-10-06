const http = require('node:http');
const crypto = require('node:crypto');
const {Pool} = require('pg');

const pool = new Pool({max: 2, connectionTimeoutMillis: 5000});
const token = Buffer.from(process.env.PROBE_KEY || '');
if (token.length < 32) throw new Error('PROBE_KEY must be configured');

function authorized(request) {
  const supplied = Buffer.from(request.headers['x-probe-key'] || '');
  return supplied.length === token.length && crypto.timingSafeEqual(supplied, token);
}

function reply(response, status, body) {
  response.writeHead(status, {'content-type': 'application/json', 'cache-control': 'no-store'});
  response.end(JSON.stringify(body));
}

http.createServer(async (request, response) => {
  if (request.method === 'GET' && request.url === '/health') {
    reply(response, 200, {ok: true});
    return;
  }
  if (!authorized(request)) {
    reply(response, 403, {error: 'forbidden'});
    return;
  }
  const match = /^\/records\/([a-f0-9]{32})$/.exec(request.url || '');
  if (!match || !['GET', 'POST', 'DELETE'].includes(request.method)) {
    reply(response, 404, {error: 'not found'});
    return;
  }
  try {
    if (request.method === 'DELETE') {
      const deleted = await pool.query('DELETE FROM sky_probe_migrated WHERE id = $1', [match[1]]);
      reply(response, 200, {deleted: deleted.rowCount === 1});
      return;
    }
    if (request.method === 'POST') {
      await pool.query('INSERT INTO sky_probe_migrated (id, value) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value',
        [match[1], match[1]]);
    }
    const result = await pool.query('SELECT value FROM sky_probe_migrated WHERE id = $1', [match[1]]);
    reply(response, result.rows.length ? 200 : 404,
      {value: result.rows[0]?.value || null});
  } catch (_error) {
    reply(response, 503, {error: 'database unavailable'});
  }
}).listen(Number(process.env.PORT || 3000), '0.0.0.0');
