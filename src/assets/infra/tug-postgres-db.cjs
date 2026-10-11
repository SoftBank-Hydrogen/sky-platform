"use strict";
// Sky's reviewed TUG PostgreSQL recipe. Credentials are runtime settings only.
const fs = require("node:fs");
const { Pool, types } = require("pg");

function safeInteger(value) {
  const n = Number(value);
  if (!/^-?[0-9]+$/.test(value) || !Number.isSafeInteger(n)) throw new Error("Unsafe database integer");
  return n;
}

function openScores() {
  for (const name of ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLROOTCERT", "SKY_APP_DB_CONNECTION_LIMIT"]) {
    if (!process.env[name]) throw new Error("Missing application database setting: " + name);
  }
  if (process.env.DATABASE_URL) throw new Error("Use reviewed application database bindings");
  const max = Number(process.env.SKY_APP_DB_CONNECTION_LIMIT);
  const port = Number(process.env.PGPORT);
  if (!Number.isInteger(max) || max < 1 || max > 50 || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Invalid application database limits");
  }
  const pool = new Pool({
    host: process.env.PGHOST, port, database: process.env.PGDATABASE, user: process.env.PGUSER,
    password: process.env.PGPASSWORD, max, connectionTimeoutMillis: 5000, idleTimeoutMillis: 10000,
    statement_timeout: 5000, query_timeout: 6000,
    ssl: { rejectUnauthorized: true, ca: fs.readFileSync(process.env.PGSSLROOTCERT, "utf8") },
    types: { getTypeParser: (oid, format) => oid === 20 && format !== "binary" ? safeInteger : types.getTypeParser(oid, format) },
  });
  pool.on("error", () => console.error("Scoreboard database connection unavailable"));
  return {
    async ready() { await pool.query("SELECT 1 FROM rounds LIMIT 1"); },
    async saveRound(r) {
      await pool.query("INSERT INTO rounds(started_at,ended_at,winner,taps_a,taps_b,players_a,players_b) VALUES($1,$2,$3,$4,$5,$6,$7)",
        [r.startedAt,r.endedAt,r.winner,r.taps.A,r.taps.B,r.players.A,r.players.B]);
    },
    async recentRounds(limit = 10) {
      const count = Number(limit);
      const bounded = Number.isFinite(count) ? Math.max(1, Math.min(100, Math.trunc(count))) : 10;
      const result = await pool.query("SELECT id,started_at,ended_at,winner,taps_a,taps_b,players_a,players_b FROM rounds ORDER BY id DESC LIMIT $1", [bounded]);
      return result.rows.map(row => ({ id: row.id, startedAt: row.started_at, endedAt: row.ended_at, winner: row.winner,
        taps: { A: row.taps_a, B: row.taps_b }, players: { A: row.players_a, B: row.players_b } }));
    },
    async summary() {
      const result = await pool.query(`SELECT COUNT(*) AS rounds,
        SUM(CASE WHEN winner='A' THEN 1 ELSE 0 END) AS wins_a,
        SUM(CASE WHEN winner='B' THEN 1 ELSE 0 END) AS wins_b,
        SUM(CASE WHEN winner='DRAW' THEN 1 ELSE 0 END) AS draws,
        SUM(taps_a+taps_b) AS taps, MAX(taps_a+taps_b) AS best_round_taps FROM rounds`);
      const t = result.rows[0];
      return { rounds: t.rounds, wins: { A: t.wins_a || 0, B: t.wins_b || 0 }, draws: t.draws || 0,
        totalTaps: t.taps || 0, bestRoundTaps: t.best_round_taps || 0 };
    },
    async close() { await pool.end(); },
  };
}
module.exports = { openScores, available: () => true, safeInteger };
