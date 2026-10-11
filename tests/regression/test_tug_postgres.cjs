const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let config;
let unavailable = false;
let sum = "100";
const queries = [];
class Pool {
  constructor(options) { config = options; }
  on() {}
  async query(sql, args) {
    queries.push({sql,args});
    if (unavailable) throw new Error("Database unavailable");
    if (sql.includes('COUNT')) return { rows: [{ rounds: 13, wins_a: 4, wins_b: 5, draws: 4, taps: sum, best_round_taps: 30 }] };
    if (sql.includes('ORDER BY')) return { rows: [{ id: 13, started_at: 10, ended_at: 20, winner: 'A', taps_a: 2, taps_b: 1, players_a: 1, players_b: 1 }] };
    return {rows: []};
  }
  async end() {}
}
const context = {module: {exports: {}}, console, process: {env: {
  PGHOST: 'db.example', PGPORT: '5432', PGDATABASE: 'app', PGUSER: 'login', PGPASSWORD: 'test-secret',
  PGSSLROOTCERT: '/public-ca.pem', SKY_APP_DB_CONNECTION_LIMIT: '5'}}, require(name) {
    if (name === 'pg') return { Pool, types: { getTypeParser: () => x => x } };
    if (name === 'node:fs') return {readFileSync: () => 'CA'};
    throw new Error('Unexpected import');
  }};
vm.runInNewContext(fs.readFileSync('src/assets/infra/tug-postgres-db.cjs','utf8'),context);
(async () => {
  const {openScores, safeInteger} = context.module.exports;
  assert.equal(safeInteger('9007199254740991'), Number.MAX_SAFE_INTEGER);
  for (const value of ['9007199254740992','1.5','NaN']) assert.throws(() => safeInteger(value));
  const scores = openScores();
  assert.equal(config.ssl.rejectUnauthorized,true);
  assert.equal(config.max,5);
  assert.equal(config.types.getTypeParser(20,'text')('13'),13);
  await scores.ready();
  assert.ok(queries.at(-1).sql.includes("FROM rounds"));
  unavailable = true;
  await assert.rejects(scores.ready());
  unavailable = false;
  await scores.saveRound({startedAt:1,endedAt:2,winner:"O'Reilly",taps:{A:2,B:1},players:{A:1,B:1}});
  const insert = queries.find(q => q.sql.startsWith('INSERT'));
  assert.ok(!insert.sql.includes("O'Reilly"));
  assert.equal(insert.args[2],"O'Reilly");
  assert.equal((await scores.summary()).rounds,13);
  assert.equal((await scores.summary()).totalTaps,100);
  sum = "9007199254740992";
  await assert.rejects(scores.summary());
  sum = null;
  assert.equal((await scores.summary()).totalTaps,0);
  assert.equal((await scores.recentRounds(-1))[0].id,13);
  assert.equal(queries.at(-1).args[0],1);
  await scores.close();
  context.process.env.DATABASE_URL = 'ignored-unsafe-url';
  assert.throws(() => openScores());
  delete context.process.env.DATABASE_URL;
  delete context.process.env.PGPASSWORD;
  assert.throws(() => openScores());
  console.log('TUG PostgreSQL template checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});
