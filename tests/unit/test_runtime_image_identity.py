import hashlib
import json
from copy import deepcopy

import pytest

from adapters.aws.image_identity import verify_runtime_image
from application.certificate import deployment_certificate


def fixture():
    account, region, attempt = '123456789012', 'ap-northeast-2', 'a' * 16 + '-a1'
    service = 'sky-' + attempt
    image = f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:' + attempt
    definition = f'arn:aws:ecs:{region}:{account}:task-definition/{service}:1'
    task = f'arn:aws:ecs:{region}:{account}:task/default/' + 'b' * 32
    local = 'sha256:' + 'c' * 64
    raw = json.dumps({'schemaVersion': 2, 'config': {'digest': local}})
    digest = 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()
    responses = {
        'batch-get-image': {'images': [{'imageManifest': raw, 'imageId': {'imageDigest': digest}}]},
        'list-tasks': {'taskArns': [task]},
        'describe-tasks': {'tasks': [{'taskArn': task, 'group': 'service:' + service,
            'clusterArn': f'arn:aws:ecs:{region}:{account}:cluster/default',
            'taskDefinitionArn': definition, 'lastStatus': 'RUNNING', 'desiredStatus': 'RUNNING',
            'containers': [{'image': image, 'imageDigest': digest, 'lastStatus': 'RUNNING'}]}]},
    }
    arguments = {'account': account, 'region': region, 'service': service, 'task_definition': definition,
                 'image': image, 'manifest_digest': digest, 'local_image_id': local}
    return responses, arguments


def verify(responses, arguments):
    return verify_runtime_image(lambda args, **kwargs: json.dumps(responses[args[1]]), **arguments)


def test_runtime_proof_connects_config_manifest_and_certificate():
    responses, args = fixture()
    evidence = verify(responses, args)
    job = {'status': 'succeeded', 'target': 'aws-ecs-express', 'result': {
        'url': 'https://example.com', 'account': args['account'], 'region': args['region'],
        'service': args['service'], 'service_arn': evidence['service_arn'],
        'task_definition_arn': args['task_definition'], 'image': args['image'],
        'image_digest': args['manifest_digest'], 'image_identity': evidence,
        'rehearsal': {'status': 'passed', 'image_id': args['local_image_id'], 'platform': 'linux/amd64'}}}

    def status(record):
        return next(item['status'] for item in deployment_certificate(record)['verification']
                    if item['name'] == 'image_identity')

    assert status(job) == 'passed'
    for key in evidence:
        modified = deepcopy(job)
        modified['result']['image_identity'][key] = None
        assert status(modified) == 'unverified', key
    job['status'] = 'failed'
    assert status(job) == 'unverified'


@pytest.mark.parametrize('change', ['config', 'manifest', 'empty', 'digest', 'group', 'definition',
                                   'stopped', 'failures', 'foreign', 'duplicate'])
def test_changed_or_missing_runtime_evidence_fails_closed(change):
    responses, args = fixture()
    task = responses['describe-tasks']['tasks'][0]
    if change == 'config':
        args['local_image_id'] = 'sha256:' + 'd' * 64
    elif change == 'manifest':
        responses['batch-get-image']['images'][0]['imageManifest'] += ' '
    elif change == 'empty':
        responses['list-tasks']['taskArns'] = []
    elif change == 'digest':
        task['containers'][0]['imageDigest'] = 'sha256:' + 'd' * 64
    elif change == 'group':
        task['group'] = 'service:foreign'
    elif change == 'definition':
        task['taskDefinitionArn'] += '2'
    elif change == 'stopped':
        task['lastStatus'] = 'STOPPED'
    elif change == 'failures':
        responses['describe-tasks']['failures'] = [{'reason': 'MISSING'}]
    elif change == 'foreign':
        responses['list-tasks']['taskArns'][0] = 'arn:foreign'
    elif change == 'duplicate':
        responses['describe-tasks']['tasks'].append(deepcopy(task))
    with pytest.raises(ValueError):
        verify(responses, args)


@pytest.mark.parametrize('local_kind', ['config', 'platform', 'index'])
def test_oci_index_resolves_one_amd64_image_and_ignores_attestation(local_kind):
    responses, args = fixture()
    child_digest = args['manifest_digest']
    child_response = responses['batch-get-image']
    raw = json.dumps({'schemaVersion': 2, 'manifests': [
        {'digest': child_digest, 'platform': {'os': 'linux', 'architecture': 'amd64'}},
        {'digest': 'sha256:' + 'f' * 64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}}]})
    root_digest = 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()
    args['manifest_digest'] = root_digest
    args['local_image_id'] = {'config': args['local_image_id'], 'platform': child_digest,
                             'index': root_digest}[local_kind]
    responses['describe-tasks']['tasks'][0]['containers'][0]['imageDigest'] = root_digest
    root_response = {'images': [{'imageManifest': raw, 'imageId': {'imageDigest': root_digest}}]}

    def aws(argv, **kwargs):
        if argv[1] == 'batch-get-image':
            return json.dumps(root_response if argv[-1] == 'imageDigest=' + root_digest else child_response)
        return json.dumps(responses[argv[1]])

    result = verify_runtime_image(aws, **args)
    assert result['platform_manifest_digest'] == child_digest
    assert result['manifest_digest'] == root_digest
    assert result['runtime_digests'] == [root_digest]
