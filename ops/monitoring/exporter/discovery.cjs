'use strict';
const { createHash } = require('node:crypto');
function validateDiscovery(source) {
  if (!source.discovery) return;
  const { allowedHosts, applicationIds } = source.discovery;
  if (source.kind !== 'sky' || !Array.isArray(allowedHosts) || allowedHosts.length < 1 || allowedHosts.length > 20 ||
      allowedHosts.some(host => typeof host !== 'string' || !/^(\*\.)?[a-z0-9]+(?:[.-][a-z0-9]+)*$/.test(host))) {
    throw new Error('Discovery needs explicit lowercase allowedHosts on a Sky target');
  }
  if (applicationIds !== undefined && (!Array.isArray(applicationIds) || applicationIds.length > 50 ||
      applicationIds.some(id => typeof id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9-]{2,63}$/.test(id)))) {
    throw new Error('Invalid discovery applicationIds');
  }
}
async function discover(source, jobs, loadJob) {
  validateDiscovery(source);
  const candidates = jobs.filter(job => job?.status === 'succeeded' && job.deployment_state === 'active' &&
    typeof job.id === 'string' && /^[a-f0-9]{16}$/.test(job.id) &&
    (!source.discovery.applicationIds || source.discovery.applicationIds.includes(job.application_id)));
  let rejected = Math.max(0, candidates.length - 20);
  // Load failures abort the reconciliation: do not delete old targets because Sky is unreachable.
  const details = await Promise.all(candidates.slice(0, 20).map(async job => {
    const detail = await loadJob(job.id);
    if (!detail || detail.id !== job.id) throw new Error('Discovery job identity mismatch');
    return detail;
  }));
  const targets = new Map();
  for (const detail of details) {
    if (detail?.status !== 'succeeded' || detail.deployment_state !== 'active' ||
        !detail.application_ir?.hypotheses?.some(item => item?.kind === 'sky-probe-protocol')) continue;
    try {
      const applicationId = detail.application_id || detail.id;
      if (!/^[a-zA-Z0-9][a-zA-Z0-9-]{2,63}$/.test(applicationId) || typeof detail.target !== 'string') throw new Error();
      const url = new URL(detail.result?.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error();
      const allowed = source.discovery.allowedHosts.some(host => host.startsWith('*.') ?
        url.hostname.endsWith(host.slice(1)) && url.hostname !== host.slice(2) : url.hostname === host);
      if (!allowed) throw new Error();
      const key = `${source.name}\0${applicationId}\0${detail.target}`;
      const name = `game-${applicationId.slice(0,20)}-${createHash('sha256').update(key).digest('hex').slice(0,12)}`;
      const ws = new URL('/ws', url); ws.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      // Keep stable names across releases; never inherit Sky's Cognito cookie to a workload.
      if (!targets.has(name)) targets.set(name, { name, kind: 'game', httpUrl: url.origin, wsUrl: ws.href });
    } catch { rejected++; }
  }
  return { targets: [...targets.values()], rejected };
}
module.exports = { discover, validateDiscovery };
