"""Explicit recipe for the audited TUG ZIP; unsupported sources fail closed.

No AI, package-manager execution or database calls occur in the worker.
"""
import hashlib
import json

from assets import ASSET_ROOT
from application.deployment_core import source_digest

TUG_SOURCE_DIGEST = "c925c00901ac9691a1d404ecbccb41e9067e5298b46b8a386f1d9a2012f38b86"
PROFILE = "tug-postgres-v1"
REQUIRED_ENV = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLROOTCERT", "SKY_APP_DB_CONNECTION_LIMIT"]


def validate_tug_source(original):
    if source_digest(original) != TUG_SOURCE_DIGEST:
        raise ValueError("TUG PostgreSQL recipe supports only the reviewed ZIP source")


def convert_tug_postgres(work):
    """Called only after source validation and SQLite migration staging in a private copy."""
    path = work / "server.js"
    server = path.read_text(encoding="utf-8")
    def replace(old, new):
        nonlocal server
        if server.count(old) != 1:
            raise ValueError("TUG server differs from reviewed conversion recipe")
        server = server.replace(old, new, 1)
    replace("const server = http.createServer((req, res) => {", "const server = http.createServer(async (req, res) => {\n    try {")
    replace("if (req.method === 'GET' && url.pathname === '/health') return sendJson(res, 200, { status: 'ok', storage: 'sqlite', replicas: 1 });",
            "if (req.method === 'GET' && url.pathname === '/health') { if (!scores) throw new Error('Scoreboard unavailable'); await scores.ready(); return sendJson(res, 200, { status: 'ok', storage: 'postgresql', replicas: 1 }); }")
    replace("{ ...scores.summary(), recent: scores.recentRounds(5) }", "{ ...await scores.summary(), recent: await scores.recentRounds(5) }")
    replace("{ rounds: scores.recentRounds(Number(url.searchParams.get('limit')) || 10) }", "{ rounds: await scores.recentRounds(Number(url.searchParams.get('limit')) || 10) }")
    replace("sendJson(res, 404, { error: 'not_found' });\n  });", "sendJson(res, 404, { error: 'not_found' });\n    } catch { if (!res.headersSent) sendJson(res, 503, { error: 'scoreboard_unavailable' }); else res.end(); }\n  });")
    replace("function onMessage(ws, client, data, isBinary) {", "async function onMessage(ws, client, data, isBinary) {\n    try {")
    replace("if (scores) send(ws, { type: 'scoreboard', ...scores.summary() });", "if (scores) send(ws, { type: 'scoreboard', ...await scores.summary() });")
    replace("counters.rejected++;\n  }\n\n  function broadcast() {", "counters.rejected++;\n    } catch { send(ws, { type: 'storage.error', error: 'scoreboard_unavailable' }); }\n  }\n\n  async function broadcast() {")
    replace("scores.saveRound(result);", "await scores.saveRound(result);")
    replace("everyone({ type: 'scoreboard', ...scores.summary() });", "everyone({ type: 'scoreboard', ...await scores.summary() });")
    replace("console.error('Could not save round', e.message);", "console.error('Could not save round: database unavailable');")
    replace("const timer = setInterval(broadcast, BROADCAST_MS);", "const writes = new Set();\n  const timer = setInterval(() => { const write = broadcast(); writes.add(write); write.finally(() => writes.delete(write)).catch(() => {}); }, BROADCAST_MS);")
    replace("server.on('close', () => { clearInterval(timer); clearInterval(heartbeat); if (scores) scores.close(); });", "server.on('close', () => { clearInterval(timer); clearInterval(heartbeat); });")
    replace("server.closeAll = () => {\n    for (const ws of clients.keys()) ws.terminate();\n    return new Promise(resolve => server.close(resolve));\n  };", "server.closeAll = async () => {\n    clearInterval(timer); clearInterval(heartbeat);\n    for (const ws of clients.keys()) ws.terminate();\n    await new Promise(resolve => server.close(resolve));\n    await Promise.allSettled([...writes]);\n    if (scores) await scores.close();\n  };")
    replace("(SQLite scores, single instance)", "(PostgreSQL scores, single instance)")
    path.write_text(server, encoding="utf-8")
    assets = ASSET_ROOT / "infra"
    (work / "db.js").write_bytes((assets / "tug-postgres-db.cjs").read_bytes())
    manifest = json.loads((work / "package.json").read_text())
    manifest["dependencies"]["pg"] = "8.23.0"
    (work / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (work / "package-lock.json").write_bytes((assets / "tug-postgres-package-lock.json").read_bytes())
    from adapters.database.migrations import trusted_rds_ca_bundle
    (work / "rds-global-bundle.pem").write_bytes(trusted_rds_ca_bundle().read_bytes())
    changes = ["server.js", "db.js", "package.json", "package-lock.json", "rds-global-bundle.pem"]
    return {"profile": PROFILE, "source_digest": source_digest(work), "required_env": REQUIRED_ENV,
            "changed_files": {name: hashlib.sha256((work / name).read_bytes()).hexdigest() for name in changes}}
