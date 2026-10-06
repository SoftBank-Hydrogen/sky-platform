'use strict';

const fs = require('node:fs');

const NAME = /^[0-9]{4}_[a-z0-9_]+\.sql$/;
const CHECKSUM = /^[a-f0-9]{64}$/;
const MARKER = /^[a-f0-9]{32}$/;

function checkedManifest(value) {
  if (!value || !Array.isArray(value.migrations) ||
      value.migrations.length < 1 || value.migrations.length > 32) {
    throw new Error('Expected 1–32 migration checksums');
  }
  let previous = '';
  const versions = new Set();
  for (const item of value.migrations) {
    if (!item || !NAME.test(item.name) || !CHECKSUM.test(item.sha256) ||
        item.name <= previous || versions.has(item.name.slice(0, 4))) {
      throw new Error('Invalid migration manifest');
    }
    previous = item.name;
    versions.add(item.name.slice(0, 4));
  }
  return value.migrations;
}

async function verifyRestoredData(client, manifest, markerId = null) {
  const expected = checkedManifest(manifest);
  if (markerId !== null && !MARKER.test(markerId)) {
    throw new Error('Invalid restore marker ID');
  }
  await client.query('BEGIN READ ONLY');
  try {
    const found = await client.query(
      'SELECT name, sha256 FROM sky_schema_migrations ORDER BY name');
    if (!Array.isArray(found.rows) || found.rows.length !== expected.length ||
        found.rows.some((row, index) => row.name !== expected[index].name ||
          row.sha256 !== expected[index].sha256)) {
      throw new Error('Restored migration ledger does not match the source bundle');
    }
    if (markerId !== null) {
      const marker = await client.query(
        'SELECT value FROM sky_probe_migrated WHERE id = $1', [markerId]);
      if (!Array.isArray(marker.rows) || marker.rows.length !== 1 ||
          marker.rows[0].value !== markerId) {
        throw new Error('Restored data marker does not match');
      }
    }
    await client.query('ROLLBACK');
    return {migration_count: expected.length, marker_checked: markerId !== null};
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  }
}

if (require.main === module) {
  const {Client} = require('pg');
  const manifest = JSON.parse(fs.readFileSync('/app/migrations/manifest.json', 'utf8'));
  const markerId = process.env.SKY_RESTORE_MARKER_ID || null;
  const client = new Client({connectionTimeoutMillis: 10000});
  (async () => {
    await client.connect();
    try {
      const result = await verifyRestoredData(client, manifest, markerId);
      process.stdout.write(JSON.stringify({status: 'passed', ...result}) + '\n');
    } finally {
      await client.end();
    }
  })().catch(error => {
    process.stderr.write(error.message + '\n');
    process.exitCode = 1;
  });
}

module.exports = {checkedManifest, verifyRestoredData};
