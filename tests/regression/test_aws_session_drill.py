from copy import deepcopy
from unittest.mock import Mock

import pytest

from tests.live.smoke_aws_game_sessions import drill, owned_runtime

ACCOUNT, REGION, SERVICE = '123456789012', 'ap-northeast-2', 'sky-' + 'a' * 16 + '-a1'
ARN = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}'
TASK = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'b' * 32
IMAGE = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:' + SERVICE.removeprefix('sky-')
DEFINITION = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/game:1'


def ecs_fixture():
    ecs = Mock()
    ecs.describe_express_gateway_service.return_value = {'service': {
        'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'}, 'tags': [
            {'key': 'sky-managed', 'value': 'true'},
            {'key': 'sky-attempt', 'value': SERVICE.removeprefix('sky-')}],
        'activeConfigurations': [{'primaryContainer': {'image': IMAGE}, 'taskDefinitionArn': DEFINITION,
            'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': f'sk-demo.ecs.{REGION}.on.aws'}]}]}}
    ecs.list_tasks.return_value = {'taskArns': [TASK]}
    ecs.describe_tasks.return_value = {'tasks': [{'taskArn': TASK, 'group': 'service:' + SERVICE,
        'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default', 'taskDefinitionArn': DEFINITION,
        'lastStatus': 'RUNNING', 'desiredStatus': 'RUNNING',
        'containers': [{'image': IMAGE, 'lastStatus': 'RUNNING', 'imageDigest': 'sha256:' + 'c' * 64}]}]}
    return ecs


@pytest.mark.parametrize('change', ['tag', 'group', 'definition', 'foreign_task', 'image', 'digest', 'pagination'])
def test_ownership_mismatch_prevents_task_stop(change):
    ecs = ecs_fixture()
    service = ecs.describe_express_gateway_service.return_value['service']
    task = ecs.describe_tasks.return_value['tasks'][0]
    if change == 'tag':
        service['tags'] = []
    elif change == 'group':
        task['group'] = 'service:other'
    elif change == 'definition':
        task['taskDefinitionArn'] += '2'
    elif change == 'foreign_task':
        ecs.list_tasks.return_value['taskArns'] = ['arn:foreign']
    elif change == 'image':
        service['activeConfigurations'][0]['primaryContainer']['image'] = 'foreign'
    elif change == 'digest':
        task['containers'][0]['imageDigest'] = None
    else:
        ecs.list_tasks.return_value['nextToken'] = 'incomplete'
    with pytest.raises(ValueError):
        owned_runtime(ecs, ACCOUNT, REGION, SERVICE)
    ecs.stop_task.assert_not_called()


def test_read_only_default_never_stops_task_or_joins_game(monkeypatch):
    ecs = ecs_fixture()
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.game_json', lambda url, path:
        {'rounds': 13} if path == '/api/scoreboard' else {
            'connections': {'players': 0, 'others': 0}, 'phase': 'waiting'})
    popen = Mock()
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.subprocess.Popen', popen)
    assert drill(ecs, ACCOUNT, REGION, SERVICE)['scope'] == 'aws-read-only-preflight'
    ecs.stop_task.assert_not_called()
    popen.assert_not_called()


def test_player_joins_during_preflight_blocks_stop(monkeypatch):
    ecs = ecs_fixture()
    stats = iter([{'players': 0, 'others': 0}, {'players': 2, 'others': 0}])
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.game_json', lambda url, path:
        {'rounds': 13} if path == '/api/scoreboard' else {'connections': next(stats), 'phase': 'waiting'})
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.subprocess.Popen', Mock(return_value=process))
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.event_line', lambda *args: {'event': 'ready'})
    with pytest.raises(ValueError, match='players changed'):
        drill(ecs, ACCOUNT, REGION, SERVICE, apply=True)
    ecs.stop_task.assert_not_called()
    process.terminate.assert_called_once()


@pytest.mark.parametrize('drain_delayed', [False, True])
def test_single_owned_task_replacement_records_loss_and_score_persistence(monkeypatch, drain_delayed):
    ecs = ecs_fixture()
    before = owned_runtime(ecs, ACCOUNT, REGION, SERVICE)
    after = deepcopy(before)
    after['task_arn'] = TASK[:-32] + 'd' * 32
    ecs.stop_task.return_value = {'task': {'taskArn': TASK}}
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.owned_runtime',
                        Mock(side_effect=[before, before, after]))
    stats = iter([{'players': 0, 'others': 0}, {'players': 1, 'others': 0}])
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.game_json', lambda url, path:
        {'rounds': 13, 'wins': {'A': 5}} if path == '/api/scoreboard' else {
            'connections': next(stats), 'phase': 'waiting'})
    process = Mock(returncode=0)
    process.poll.return_value = None
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.subprocess.Popen', Mock(return_value=process))
    events = [{'event': 'ready', 'session_hash': 'old'}]
    if drain_delayed:
        events.append(TimeoutError('Old socket is still draining'))
    events.extend([{'event': 'closed', 'code': 1006, 'clean': False},
                   {'event': 'ready', 'session_hash': 'new'}])
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.event_line', Mock(side_effect=events))
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.time.sleep', lambda seconds: None)
    monkeypatch.setattr('tests.live.smoke_aws_game_sessions.time.monotonic', Mock(side_effect=range(1000)))
    result = drill(ecs, ACCOUNT, REGION, SERVICE, apply=True)
    ecs.stop_task.assert_called_once()
    assert ecs.stop_task.call_args.kwargs['task'] == TASK
    assert result['session_continuity'] == 'lost'
    assert result['persisted_scoreboard'] == 'unchanged'
    assert result['before']['task_arn'] != result['after']['task_arn']
    assert 'playing-round memory state' in result['unverified']
    if drain_delayed:
        assert result['http_recovery_seconds'] < result['close_seconds']
