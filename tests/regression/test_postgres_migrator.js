'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {applyMigrations} = require('../../src/assets/infra/postgres-migrator.js');

test('applies a migration once and rejects checksum drift', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sky-migration-'));
  try {
    const sql = 'CREATE TABLE demo (id integer);';
    fs.writeFileSync(path.join(directory, '0001_demo.sql'), sql);
    const entry = {name: '0001_demo.sql', sha256: crypto.createHash('sha256').update(sql).digest('hex')};
    const applied = new Map();
    const calls = [];
    const client = {async query(statement, params = []) {
      calls.push(statement);
      if (statement.startsWith('SELECT sha256')) {
        return {rows: applied.has(params[0]) ? [{sha256: applied.get(params[0])}] : []};
      }
      if (statement.startsWith('INSERT INTO')) applied.set(params[0], params[1]);
      return {rows: []};
    }};
    await applyMigrations(client, [entry], directory);
    await applyMigrations(client, [entry], directory);
    assert.equal(calls.filter(item => item === sql).length, 1);
    applied.set(entry.name, 'f'.repeat(64));
    await assert.rejects(applyMigrations(client, [entry], directory), /checksum drift/);
    assert.equal(calls.at(-1), 'ROLLBACK');
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('rejects embedded transaction control before touching the database', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sky-migration-'));
  try {
    const sql = 'COMMIT;';
    fs.writeFileSync(path.join(directory, '0001_bad.sql'), sql);
    const entry = {name: '0001_bad.sql', sha256: crypto.createHash('sha256').update(sql).digest('hex')};
    const client = {query() {throw new Error('Database should not be contacted');}};
    await assert.rejects(applyMigrations(client, [entry], directory), /transaction control/);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
