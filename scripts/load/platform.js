import http from 'k6/http';
import { check, fail } from 'k6';
import { Rate } from 'k6/metrics';

// Read-only service traffic. Never upload, analyze, build, deploy or probe workloads here.
const profile = __ENV.PROFILE || 'smoke';
const base = (__ENV.BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
if (!/^https?:\/\/[^/?#@]+$/.test(base)) throw new Error('BASE_URL must be an origin without credentials, path or query');
if (base.startsWith('http://') && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base)
    && __ENV.ALLOW_INSECURE_HTTP !== 'true') {
  throw new Error('Use HTTPS; isolated HTTP testing requires ALLOW_INSECURE_HTTP=true');
}
const authMode = __ENV.AUTH_MODE || 'local';
if (!['local', 'hosted'].includes(authMode)) throw new Error('AUTH_MODE must be local or hosted');
if (authMode === 'hosted' && (!__ENV.SKY_COOKIE || __ENV.SKY_API_TOKEN)) {
  throw new Error('Hosted mode requires SKY_COOKIE and forbids SKY_API_TOKEN');
}
const cursor = __ENV.JOB_CURSOR || '';
if (cursor.length > 1024) throw new Error('JOB_CURSOR is too long');
const jobsPath = '/api/jobs' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : '');
const jobId = __ENV.JOB_ID || '';
if (jobId && !/^[A-Za-z0-9_-]+$/.test(jobId)) throw new Error('Invalid JOB_ID');
const duration = __ENV.DURATION || '5m';
if (!/^\d+(s|m)$/.test(duration)) throw new Error('DURATION must use seconds or minutes');
const seconds = parseInt(duration, 10) * (duration.endsWith('m') ? 60 : 1);
if (seconds < 5 || seconds > 1800) throw new Error('DURATION must be between 5s and 30m');
const rate = Number(__ENV.RATE || 10);
if (!Number.isInteger(rate) || rate < 1 || rate > 50) throw new Error('RATE must be an integer from 1 to 50');
const arrival = { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration,
  preAllocatedVUs: 20, maxVUs: 100, exec: 'readTraffic' };
const profiles = {
  smoke: { executor: 'shared-iterations', vus: 1, iterations: 5, maxDuration: '30s', exec: 'readTraffic' },
  baseline: arrival,
  ramp: { executor: 'ramping-arrival-rate', startRate: 1, timeUnit: '1s',
    preAllocatedVUs: 20, maxVUs: 100, exec: 'readTraffic',
    stages: [{ target: 5, duration: '2m' }, { target: 10, duration: '3m' },
      { target: 20, duration: '3m' }, { target: 1, duration: '2m' }] },
  soak: { ...arrival, rate: Number(__ENV.RATE || 5), duration: __ENV.DURATION || '15m' },
};
if (!Object.prototype.hasOwnProperty.call(profiles, profile)) throw new Error('PROFILE must be smoke, baseline, ramp or soak');
const readFailures = new Rate('sky_read_failures');
export const options = {
  scenarios: { reads: profiles[profile] },
  discardResponseBodies: false,
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
  thresholds: {
    sky_read_failures: [{ threshold: 'rate<0.01', abortOnFail: true, delayAbortEval: '30s' }],
    'http_req_duration{phase:load}': ['p(95)<1000', 'p(99)<2000'],
    ...(profile === 'smoke' ? {} : { dropped_iterations: ['count==0'] }),
  },
};

function request(path, token, phase) {
  const headers = {};
  if (authMode === 'local' && token) headers['X-Sky-Token'] = token;
  if (__ENV.SKY_COOKIE) headers.Cookie = __ENV.SKY_COOKIE;
  return http.get(`${base}${path}`, { headers, redirects: 0, timeout: '5s',
    tags: { name: path.startsWith('/api/jobs/') ? '/api/jobs/:id' : path.startsWith('/api/jobs?') ? '/api/jobs' : path, phase } });
}
function json(response) {
  try { return response.json(); } catch (_) { return null; }
}
function valid(response, path) {
  if (response.status !== 200) return false;
  if (path === '/') return (authMode === 'hosted' ? /const token='';/ : /const token='[A-Za-z0-9_-]{32,128}';/).test(response.body || '');
  const body = json(response);
  if (path === '/health') return body && body.status === 'ok';
  if (path === '/api/config') return body && typeof body.ai_available === 'boolean' && Array.isArray(body.targets);
  if (path === jobsPath) {
    const items = Array.isArray(body) ? body : body && body.items;
    const next = Array.isArray(body) ? null : body && body.next_cursor;
    return Array.isArray(items) && items.length <= 10000 && items.every(job =>
      job && typeof job.id === 'string' && typeof job.status === 'string') &&
      (next === null || (typeof next === 'string' && next.length > 0 && next.length <= 1024));
  }
  return body && body.id === jobId && typeof body.status === 'string';
}

export function setup() {
  const health = request('/health', '', 'preflight');
  if (!valid(health, '/health')) fail('Sky health preflight failed; check target and authentication');
  const page = request('/', '', 'preflight');
  if (!valid(page, '/')) fail('Sky page preflight failed; nginx/login page is not a valid Sky target');
  const token = authMode === 'hosted' ? '' :
    __ENV.SKY_API_TOKEN || page.body.match(/const token='([A-Za-z0-9_-]{32,128})';/)[1];
  if (authMode === 'hosted' && !valid(request('/api/config', '', 'preflight'), '/api/config')) {
    fail('Sky config preflight failed; check authenticated membership');
  }
  if (!valid(request(jobsPath, token, 'preflight'), jobsPath)) fail('Jobs preflight failed; check session token');
  if (jobId && !valid(request(`/api/jobs/${jobId}`, token, 'preflight'), `/api/jobs/${jobId}`)) {
    fail('JOB_ID preflight failed');
  }
  // This token stays in memory; never print responses, cookies or request headers.
  return { token };
}

export function readTraffic(data) {
  const choice = Math.random();
  const path = choice < 0.2 ? '/' : choice < 0.9 || !jobId ? jobsPath : `/api/jobs/${jobId}`;
  const response = request(path, data.token, 'load');
  const ok = valid(response, path);
  readFailures.add(!ok);
  check(response, { 'Sky read response is valid': () => ok });
}

export function handleSummary(data) {
  // No raw HTTP payloads or auth values in the report.
  return { stdout: JSON.stringify({ profile, authMode, jobListScope: cursor ? 'selected-page' : 'first-page', jobDetailEnabled: Boolean(jobId),
    note: 'Read-only HTTP test; no deployment or capacity guarantee', summary: data }, null, 2) + '\n' };
}
