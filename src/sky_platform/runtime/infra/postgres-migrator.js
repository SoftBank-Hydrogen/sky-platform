'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const NAME = /^[0-9]{4}_[a-z0-9_]+\.sql$/;
const CHECKSUM = /^[a-f0-9]{64}$/;
const TRANSACTION_CONTROL = /\b(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b/i;

async function applyMigrations(client, entries, directory) {
  if (!Array.isArray(entries) || entries.length < 1 || entries.length > 32) {
    throw new Error('Expected 1–32 SQL migrations');
  }
  const names = new Set();
  const versions = new Set();
  for (const entry of entries) {
    if (!entry || !NAME.test(entry.name) || !CHECKSUM.test(entry.sha256)
        || names.has(entry.name) || versions.has(entry.name.slice(0, 4))) {
      throw new Error('Invalid or duplicate SQL migration');
    }
    names.add(entry.name);
    versions.add(entry.name.slice(0, 4));
  }
  if (entries.map(item => item.name).join('\0')
      !== [...entries].sort((a, b) => a.name.localeCompare(b.name)).map(item => item.name).join('\0')) {
    throw new Error('SQL migrations must be ordered');
  }

  for (const entry of entries) {
    const filename = path.join(directory, entry.name);
    const content = fs.readFileSync(filename);
    if (content.length < 1 || content.length > 65536 || content.includes(0)) {
      throw new Error('Invalid SQL migration content');
    }
    const checksum = crypto.createHash('sha256').update(content).digest('hex');
    if (checksum !== entry.sha256 || TRANSACTION_CONTROL.test(content.toString('utf8'))) {
      throw new Error('SQL migration changed or contains transaction control');
    }
    await client.query('BEGIN');
    try {
      await client.query("SELECT pg_advisory_xact_lock(hashtext('sky_schema_migrations'))");
      await client.query('CREATE TABLE IF NOT EXISTS sky_schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())');
      const previous = await client.query('SELECT sha256 FROM sky_schema_migrations WHERE name = $1', [entry.name]);
      if (previous.rows.length === 0) {
        await client.query(content.toString('utf8'));
        await client.query('INSERT INTO sky_schema_migrations (name, sha256) VALUES ($1, $2)',
          [entry.name, checksum]);
      } else if (previous.rows.length !== 1 || previous.rows[0].sha256 !== checksum) {
        throw new Error(`SQL migration checksum drift: ${entry.name}`);
      }
      await client.query('COMMIT');
    } catch (error) {
      await client.query('ROLLBACK');
      throw error;
    }
  }
}

if (require.main === module) {
  const {Client} = require('pg');
  const directory = '/app/migrations';
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  const client = new Client({connectionTimeoutMillis: 10000});
  (async () => {
    await client.connect();
    try {
      await applyMigrations(client, manifest.migrations, directory);
      process.stdout.write(JSON.stringify({applied: manifest.migrations.length}) + '\n');
    } finally {
      await client.end();
    }
  })().catch(error => {
    process.stderr.write(error.message + '\n');
    process.exitCode = 1;
  });
}

module.exports = {applyMigrations};
