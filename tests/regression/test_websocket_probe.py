import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from application.analysis import AISettings
from application.certificate import deployment_certificate
from application.websocket_probe import WebSocketProbeError, probe_sky_game
from interfaces.http.server import App, handler_for


class FakeWebSocket:
    def __init__(self, *, wrong_nonce=False, bad_accept=False):
        self.output = io.BytesIO()
        self.wrong_nonce = wrong_nonce
        self.bad_accept = bad_accept
        self.request = b''
        self.masked = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, _):
        pass

    def makefile(self, _):
        return self

    def read(self, size):
        return self.output.read(size)

    def readline(self, size):
        return self.output.readline(size)

    def respond(self, data):
        position = self.output.tell()
        self.output.seek(0, 2)
        self.output.write(data)
        self.output.seek(position)

    def sendall(self, data):
        if data.startswith(b'GET '):
            self.request = data
            key = data.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
            accept = base64.b64encode(hashlib.sha1(
                key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            if self.bad_accept:
                accept = b'invalid'
            self.respond(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                         b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        elif data[0] & 0x0F == 1:
            self.masked = bool(data[1] & 0x80)
            length = data[1] & 0x7F
            mask = data[2:6]
            payload = bytes(value ^ mask[index % 4]
                            for index, value in enumerate(data[6:6 + length]))
            message = json.loads(payload)
            nonce = 'wrong' if self.wrong_nonce else message['nonce']
            response = json.dumps({'type': 'sky.probe.ack', 'nonce': nonce}).encode()
            self.respond(bytes((0x81, len(response))) + response)


class WebSocketProbeTests(unittest.TestCase):
    def test_game_round_trip_checks_handshake_masking_and_nonce(self):
        socket = FakeWebSocket()
        with patch('application.websocket_probe.socket.create_connection', return_value=socket):
            result = probe_sky_game('http://127.0.0.1:8080')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['protocol'], 'sky.probe.v1')
        self.assertTrue(socket.masked)
        self.assertIn(b'GET /ws HTTP/1.1', socket.request)

    def test_wrong_nonce_or_handshake_never_passes(self):
        for options in ({'wrong_nonce': True}, {'bad_accept': True}):
            with self.subTest(options=options):
                with patch('application.websocket_probe.socket.create_connection',
                           return_value=FakeWebSocket(**options)):
                    with self.assertRaises(WebSocketProbeError):
                        probe_sky_game('http://127.0.0.1:8080')

    def test_remote_plain_http_is_rejected_before_connect(self):
        with patch('application.websocket_probe.socket.create_connection') as connect:
            with self.assertRaisesRegex(WebSocketProbeError, 'TLS'):
                probe_sky_game('http://example.com')
            connect.assert_not_called()

    def test_job_probe_requires_contract_and_live_owned_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings(), monitor_interval=0)
            job_id = 'a' * 16
            (app.root / job_id).mkdir()
            app.jobs[job_id] = {
                'id': job_id, 'status': 'succeeded', 'deployment_state': 'active',
                'result': {'url': 'http://127.0.0.1:8080'},
                'application_ir': {'hypotheses': [
                    {'kind': 'websocket', 'status': 'inferred'},
                    {'kind': 'sky-probe-protocol', 'status': 'inferred'}]},
            }
            with patch('interfaces.http.server.check_deployment', return_value={'healthy': True}), \
                    patch('interfaces.http.server.probe_sky_game',
                          return_value={'status': 'passed', 'protocol': 'sky.probe.v1',
                                        'checked_at': '2026-10-09T00:00:00+00:00'}):
                handler_type = handler_for(app)
                handler = handler_type.__new__(handler_type)
                handler.path = f'/api/jobs/{job_id}/websocket-probe'
                handler.headers = {'X-Sky-Token': app.token, 'Content-Length': '0'}
                handler.json_response = Mock()
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 200)
            self.assertEqual(app.jobs[job_id]['websocket_verification']['status'], 'passed')
            self.assertEqual(deployment_certificate(app.jobs[job_id])['verification'][-1]['status'], 'passed')
            with patch('interfaces.http.server.check_deployment', return_value={'healthy': False}), \
                    patch('interfaces.http.server.probe_sky_game') as probe:
                failed = app.check_and_record_websocket(job_id)
            self.assertEqual(failed['status'], 'failed')
            probe.assert_not_called()
            self.assertEqual(deployment_certificate(app.jobs[job_id])['verification'][-1]['status'], 'failed')
            app.jobs[job_id]['application_ir']['hypotheses'] = []
            with self.assertRaisesRegex(ValueError, '계약'):
                app.check_and_record_websocket(job_id)
            app.jobs[job_id]['deployment_state'] = 'deleted'
            with self.assertRaisesRegex(ValueError, '실행 중'):
                app.check_and_record_websocket(job_id)


if __name__ == '__main__':
    unittest.main()
