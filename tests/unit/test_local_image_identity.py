from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import analyze


@pytest.mark.parametrize('changed', [False, True])
def test_native_platform_deployment_checks_running_container_image(changed):
    project = Path('tests/fixtures/apps/hello-node')
    plan = analyze(project)
    image_id = 'sha256:' + 'b' * 64
    adapter = LocalDockerAdapter(lambda *_: None)

    def command(args, **kwargs):
        if args[1:3] == ['image', 'inspect']:
            return image_id
        if args[1] == 'port':
            return '127.0.0.1:49152'
        return ''

    response = MagicMock()
    response.__enter__.return_value.status = 200
    opener = MagicMock()
    opener.open.return_value = response
    with patch('adapters.local.docker.ImageBuilder.build'), \
            patch.object(adapter, 'command', side_effect=command), \
            patch.object(adapter, 'inspect_resource', return_value={
                'Image': 'sha256:' + 'c' * 64 if changed else image_id}), \
            patch('adapters.local.docker.urllib.request.build_opener', return_value=opener):
        if changed:
            with pytest.raises(RuntimeError, match='변경'):
                adapter.deploy(project, plan, 'a' * 16 + '-a1')
        else:
            result = adapter.deploy(project, plan, 'a' * 16 + '-a1')
            assert result['image_id'] == image_id
            assert 'platform' not in result
