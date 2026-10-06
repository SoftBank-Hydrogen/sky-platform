"""Opt-in Chrome UI -> app network stack smoke with owned probe cleanup."""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from sky_platform.runtime.analysis import AISettings
from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from sky_platform.runtime.server import App, handler_for
from tests.smoke_aws_network import retire_probe


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_network_cdp.mjs')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify network creation through real Chrome')
    parser.add_argument('--apply', action='store_true', help='Create and retire a temporary security-group stack')
    parser.add_argument('--application', default='netprobe-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'netprobe-[a-f0-9]{8}', args.application):
        parser.error('--application must be a unique netprobe-<8 hex> ID')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    discovered = discover_default_network(settings)
    request = ServiceNetworkRequest(args.application, args.account, args.region, discovered['vpc_id'])
    provisioner = AwsServiceNetworkProvisioner(request)
    preview = provisioner.preflight()
    print('Read-only browser preflight:', preview['stack_name'], flush=True)
    if not args.apply:
        print('읽기 전용 검증 완료. --apply 없이는 브라우저나 스택 생성을 시작하지 않습니다.', flush=True)
        return
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    state = Path(tempfile.mkdtemp(prefix='sky-network-browser-smoke-'))
    app = App(state, AISettings('fixture-only', 'scripted'),
              aws_settings=settings, monitor_interval=0)
    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    profile = state / 'chrome-profile'
    chrome_log = (state / 'chrome.log').open('w')
    chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
        '--no-default-browser-check', '--disable-gpu', '--disable-background-networking',
        '--no-proxy-server', '--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0', '--remote-allow-origins=*',
        '--user-data-dir=' + str(profile), 'about:blank'],
        stdout=chrome_log, stderr=subprocess.STDOUT)
    retired = False
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 30
        while not port_file.is_file():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome debugging endpoint did not start; see ' + str(state / 'chrome.log'))
            time.sleep(.2)
        port = port_file.read_text().splitlines()[0]
        subprocess.run(['node', str(DRIVER), url, port, args.application],
                       check=True, timeout=20 * 60)
        operation = app.network_operations.get(args.application)
        inspected = provisioner.inspect_current()
        if (operation['status'] != 'succeeded'
                or operation['stack_id'] != inspected['stack_id']
                or operation['service_security_group'] != inspected['service_security_group']):
            raise AssertionError('브라우저 생성 기록과 AWS 소유 스택이 다릅니다.')
        if app.jobs:
            raise AssertionError('네트워크 smoke가 ECS 배포 작업을 만들었습니다.')
        print('PASS: browser network operation matches the owned AWS stack', flush=True)
    finally:
        try:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                operation = app.network_operations.operations.get(args.application)
                if not operation or operation['status'] != 'running':
                    break
                time.sleep(3)
            operation = app.network_operations.operations.get(args.application)
            if operation and operation['status'] == 'succeeded' and operation.get('stack_id'):
                retire_probe(provisioner, operation['stack_id'],
                             operation['service_security_group'])
                retired = True
                print('PASS: browser-created temporary stack and group retired', flush=True)
            elif operation:
                print('네트워크 결과가 불확실해 자동 정리하지 않았습니다. AWS 상태를 확인하세요:',
                      request.stack_name, flush=True)
            else:
                retired = True
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
            chrome_log.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if retired:
                shutil.rmtree(state)
            else:
                print('Smoke state retained for diagnosis:', state, flush=True)
    if not retired:
        raise RuntimeError('Temporary network stack was not confirmed retired')


if __name__ == '__main__':
    main()
