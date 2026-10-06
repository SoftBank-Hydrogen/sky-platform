"""Small managed-PostgreSQL probe used by local and AWS deployment drills."""
import hmac
import os
import re

import psycopg
from flask import Flask, jsonify, request


app = Flask(__name__)
probe_key = os.environ.get('PROBE_KEY', '').encode()
if len(probe_key) < 32:
    raise RuntimeError('PROBE_KEY must be configured')


@app.get('/health')
def health():
    return jsonify(ok=True)


@app.route('/records/<record_id>', methods=['GET', 'POST', 'DELETE'])
def record(record_id):
    if not hmac.compare_digest(request.headers.get('X-Probe-Key', '').encode(), probe_key):
        return jsonify(error='forbidden'), 403
    if re.fullmatch(r'[a-f0-9]{32}', record_id) is None:
        return jsonify(error='not found'), 404
    try:
        with psycopg.connect(connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                if request.method == 'DELETE':
                    cursor.execute('DELETE FROM sky_probe_migrated WHERE id = %s', (record_id,))
                    return jsonify(deleted=cursor.rowcount == 1)
                if request.method == 'POST':
                    cursor.execute('''INSERT INTO sky_probe_migrated (id, value)
                                      VALUES (%s, %s) ON CONFLICT (id)
                                      DO UPDATE SET value = EXCLUDED.value''', (record_id, record_id))
                cursor.execute('SELECT value FROM sky_probe_migrated WHERE id = %s', (record_id,))
                row = cursor.fetchone()
        return (jsonify(value=row[0]), 200) if row else (jsonify(value=None), 404)
    except psycopg.Error:
        return jsonify(error='database unavailable'), 503
