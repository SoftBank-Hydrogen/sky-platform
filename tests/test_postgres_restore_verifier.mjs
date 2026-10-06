import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const {verifyRestoredData} = require('../src/sky_platform/runtime/infra/postgres-restore-verifier.js');
const checksum = 'a'.repeat(64);
const manifest = {migrations: [{name: '0001_init.sql', sha256: checksum}]};

test('checks immutable migration rows in a read-only transaction', async () => {
  const calls = [];
  const client = {query: async (sql, params) => {
    calls.push([sql, params]);
    if (sql.startsWith('SELECT name')) return {rows: [{name: '0001_init.sql', sha256: checksum}]};
    return {rows: []};
  }};
  assert.deepEqual(await verifyRestoredData(client, manifest),
    {migration_count: 1, marker_checked: false});
  assert.equal(calls[0][0], 'BEGIN READ ONLY');
  assert.equal(calls.at(-1)[0], 'ROLLBACK');
  assert.equal(calls.filter(([sql]) => sql.startsWith('SELECT')).length, 1);
});

test('checks a supplied data marker without logging its contents', async () => {
  const marker = 'b'.repeat(32);
  const client = {query: async (sql, params) => {
    if (sql.startsWith('SELECT name')) return {rows: [{name: '0001_init.sql', sha256: checksum}]};
    if (sql.startsWith('SELECT value')) {
      assert.deepEqual(params, [marker]);
      return {rows: [{value: marker}]};
    }
    return {rows: []};
  }};
  assert.deepEqual(await verifyRestoredData(client, manifest, marker),
    {migration_count: 1, marker_checked: true});
});

test('rejects ledger drift and always rolls back', async () => {
  const calls = [];
  const client = {query: async (sql) => {
    calls.push(sql);
    return {rows: sql.startsWith('SELECT') ? [{name: '0001_init.sql', sha256: 'b'.repeat(64)}] : []};
  }};
  await assert.rejects(verifyRestoredData(client, manifest), /ledger/);
  assert.deepEqual(calls, ['BEGIN READ ONLY',
    'SELECT name, sha256 FROM sky_schema_migrations ORDER BY name', 'ROLLBACK']);
});
