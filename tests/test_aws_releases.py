import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sky_platform.planning.analysis import AISettings
from sky_platform.deployment.aws import AwsSettings
from sky_platform.deployment.core import DeploymentPlan
from sky_platform.api.server import App, handler_for


REGION = 'ap-northeast-2'
ACCOUNT = '123456789012'
FIRST_ID = 'a' * 16
FIRST_ATTEMPT = FIRST_ID + '-a1'
SERVICE = 'sky-' + FIRST_ATTEMPT
REPOSITORY = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed'


class AwsReleaseTests(unittest.TestCase):
    def reconcile_aws(self, status, deployment_arn, active_image, current_deployment=None,
                      task_arn=None, extra_active=False):
        def aws(args, **_kwargs):
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceDeploymentArn': deployment_arn,
                    'status': status, 'createdAt': '2026-09-28T01:00:00Z'}]})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                task_revision = '1' if active_image == REPOSITORY + ':' + FIRST_ATTEMPT else '2'
                return json.dumps({'service': {'serviceArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}',
                    'status': {'statusCode': 'ACTIVE'}, 'currentDeployment': current_deployment,
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': FIRST_ATTEMPT}],
                    'activeConfigurations': [{'primaryContainer': {'image': active_image},
                        'taskDefinitionArn': task_arn or f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:{task_revision}'},
                        *([{'primaryContainer': {'image': active_image},
                            'taskDefinitionArn': task_arn or f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:{task_revision}'}]
                          if extra_active else [])]}})
            raise AssertionError(args)
        return aws

    def pending_update(self, root):
        app = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
        old_id, new_id = FIRST_ID, 'b' * 16
        old_source = root / old_id / 'source' / 'app'
        new_source = root / new_id / 'source' / 'app'
        old_source.mkdir(parents=True)
        new_source.mkdir(parents=True)
        prior = {'service': SERVICE, 'service_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}',
                 'image': REPOSITORY + ':' + FIRST_ATTEMPT, 'images': [REPOSITORY + ':' + FIRST_ATTEMPT],
                 'owner_attempt': FIRST_ATTEMPT, 'url': f'https://{SERVICE}.ecs.{REGION}.on.aws',
                 'target': 'aws-ecs-express', 'account': ACCOUNT, 'region': REGION}
        common = {'mode': 'agent', 'target': 'aws-ecs-express', 'application_id': 'my-app',
                  'events': [], 'created_at': '2026-09-28T00:00:00+00:00', 'aws': {'region': REGION}}
        app.jobs[old_id] = {**common, 'id': old_id, 'status': 'succeeded', 'result': prior,
                            'deployment_state': 'needs_attention', 'project': str(old_source), 'plan': None}
        app.jobs[new_id] = {**common, 'id': new_id, 'status': 'failed', 'project': str(new_source),
                            'plan': asdict(DeploymentPlan('nodejs', 'npm start', None, 3000, '',
                                                          target='aws-ecs-express')), 'attempts': 1,
                            'replaces_job_id': old_id, 'aws_update_submitted': True,
                            'aws_previous_deployment_arn': 'old-deployment',
                            'aws_candidate_image': REPOSITORY + ':' + new_id + '-a1'}
        app.save(old_id)
        app.save(new_id)
        return app, old_id, new_id

    def test_reconcile_promotes_verified_new_release(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            aws = self.reconcile_aws('SUCCESSFUL', 'new-deployment', REPOSITORY + ':' + new_id + '-a1')
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                outcome = app.reconcile_aws_update(new_id)
            self.assertEqual(outcome['release'], 'new')
            self.assertEqual(app.jobs[new_id]['status'], 'succeeded')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'superseded')
            self.assertEqual(app.jobs[new_id]['result']['images'], [REPOSITORY + ':' + FIRST_ATTEMPT,
                                                                    REPOSITORY + ':' + new_id + '-a1'])

    def test_reconcile_restores_previous_release_after_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            aws = self.reconcile_aws('ROLLBACK_SUCCESSFUL', 'new-deployment', REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                outcome = app.reconcile_aws_update(new_id)
            self.assertEqual(outcome['release'], 'previous')
            self.assertEqual(app.jobs[new_id]['status'], 'failed')
            self.assertEqual(app.jobs[new_id]['aws_reconciled'], 'previous')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'active')
            restarted = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                            aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'active')

    def test_reconcile_waits_for_new_deployment_and_restart_preserves_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.pending_update(root)
            app.jobs[old_id]['deployment_state'] = 'active'
            app.save(old_id)
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'needs_attention')
            with self.assertRaises(ValueError):
                restarted.ensure_application_available('my-app', 'aws-ecs-express')
            aws = self.reconcile_aws('SUCCESSFUL', 'old-deployment', REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment') as health:
                self.assertFalse(restarted.reconcile_aws_update(new_id)['reconciled'])
            health.assert_not_called()

    def test_reconcile_restores_previous_when_aws_never_started_update(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            app.jobs[new_id]['aws_update_failed_at'] = (
                datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            app.save(new_id)
            aws = self.reconcile_aws('SUCCESSFUL', 'old-deployment', REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                outcome = app.reconcile_aws_update(new_id)
            self.assertEqual(outcome['release'], 'previous')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'active')

    def test_reconcile_restores_manually_reverted_image_but_rejects_active_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            aws = self.reconcile_aws('SUCCESSFUL', 'new-deployment', REPOSITORY + ':' + FIRST_ATTEMPT,
                                     current_deployment='in-progress')
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment') as health:
                self.assertFalse(app.reconcile_aws_update(new_id)['reconciled'])
            health.assert_not_called()
            aws = self.reconcile_aws('SUCCESSFUL', 'new-deployment', REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                outcome = app.reconcile_aws_update(new_id)
            self.assertEqual(outcome['release'], 'previous')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'active')

    def test_interrupted_update_can_be_reconciled_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.pending_update(root)
            app.jobs[new_id]['status'] = 'running'
            app.save(new_id)
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[new_id]['status'], 'interrupted')
            aws = self.reconcile_aws('SUCCESSFUL', 'new-deployment', REPOSITORY + ':' + new_id + '-a1')
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                outcome = restarted.reconcile_aws_update(new_id)
            self.assertEqual(outcome['release'], 'new')
            self.assertEqual(restarted.jobs[new_id]['status'], 'succeeded')
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'superseded')

    def test_failed_accepted_update_marks_previous_release_for_review(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            app.jobs[old_id]['deployment_state'] = 'active'
            app.jobs[new_id]['status'] = 'running'
            app.jobs[new_id].pop('aws_update_submitted')
            app.save(old_id)
            app.save(new_id)
            def fail(_agent):
                app.jobs[new_id]['aws_update_submitted'] = True
                raise RuntimeError('AWS update outcome unknown')
            with patch('sky_platform.api.server.DeploymentAgent.run', autospec=True, side_effect=fail):
                app.run_agent(new_id)
            self.assertEqual(app.jobs[new_id]['status'], 'failed')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'needs_attention')

    def test_abandoned_image_cleanup_is_explicit_and_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            app.jobs[old_id]['deployment_state'] = 'active'
            app.jobs[new_id]['aws_reconciled'] = 'previous'
            app.save(old_id)
            app.save(new_id)
            with patch('sky_platform.api.server.check_deployment', return_value={'healthy': True}), \
                    patch('sky_platform.api.server.AwsExpressAdapter.cleanup_abandoned_image',
                          side_effect=RuntimeError('ECR unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'ECR unavailable'):
                    app.cleanup_abandoned_aws_image(new_id)
            self.assertEqual(app.jobs[new_id]['aws_image_cleanup_state'], 'failed')
            deleted = {'state': 'deleted', 'image': app.jobs[new_id]['aws_candidate_image']}
            with patch('sky_platform.api.server.check_deployment', return_value={'healthy': True}), \
                    patch('sky_platform.api.server.AwsExpressAdapter.cleanup_abandoned_image', return_value=deleted) as cleanup:
                self.assertEqual(app.cleanup_abandoned_aws_image(new_id), deleted)
            cleanup.assert_called_once()
            self.assertEqual(app.jobs[new_id]['aws_image_cleanup_state'], 'done')
            with self.assertRaises(ValueError):
                app.cleanup_abandoned_aws_image(new_id)
            restarted = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                            aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[new_id]['aws_image_cleanup_state'], 'done')
            restarted.jobs[new_id]['aws_image_cleanup_state'] = 'running'
            restarted.save(new_id)
            recovered = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                            aws_settings=AwsSettings(REGION))
            self.assertEqual(recovered.jobs[new_id]['aws_image_cleanup_state'], 'failed')

    def test_ongoing_update_rollback_request_keeps_reconciliation_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.pending_update(Path(directory))
            deployment_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service-deployment/default/{SERVICE}/new'
            with patch('sky_platform.api.server.AwsExpressAdapter.request_update_rollback',
                       return_value={'service_deployment_arn': deployment_arn, 'state': 'requested'}) as rollback:
                outcome = app.request_aws_update_rollback(new_id)
            self.assertEqual(outcome['state'], 'requested')
            rollback.assert_called_once()
            self.assertEqual(app.jobs[new_id]['aws_rollback_deployment_arn'], deployment_arn)
            self.assertTrue(app.jobs[new_id]['aws_rollback_requested'])
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'needs_attention')
            restarted = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                            aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'needs_attention')
            aws = self.reconcile_aws('ROLLBACK_SUCCESSFUL', deployment_arn,
                                     REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                self.assertEqual(restarted.reconcile_aws_update(new_id)['release'], 'previous')
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'active')

    def completed_release(self, root):
        app, old_id, new_id = self.pending_update(root)
        old_result = app.jobs[old_id]['result']
        old_task = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1'
        new_task = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:2'
        old_result['task_definition_arn'] = old_task
        app.jobs[old_id]['deployment_state'] = 'superseded'
        app.jobs[new_id]['status'] = 'succeeded'
        app.jobs[new_id]['deployment_state'] = 'active'
        app.jobs[new_id]['result'] = {**old_result,
            'image': REPOSITORY + ':' + new_id + '-a1',
            'images': [old_result['image'], REPOSITORY + ':' + new_id + '-a1'],
            'task_definition_arn': new_task, 'previous_task_definition_arn': old_task}
        app.save(old_id)
        app.save(new_id)
        return app, old_id, new_id

    def three_releases(self, root):
        app, first_id, second_id = self.completed_release(root)
        third_id = 'c' * 16
        third_source = root / third_id / 'source' / 'app'
        third_source.mkdir(parents=True)
        second = app.jobs[second_id]
        third_image = REPOSITORY + ':' + third_id + '-a1'
        app.jobs[third_id] = {**second, 'id': third_id, 'project': str(third_source),
                              'created_at': '2026-09-29T00:00:00+00:00',
                              'replaces_job_id': second_id, 'deployment_state': 'active',
                              'result': {**second['result'], 'image': third_image,
                                         'images': [*second['result']['images'], third_image],
                                         'task_definition_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:3',
                                         'previous_task_definition_arn': second['result']['task_definition_arn']}}
        second['deployment_state'] = 'superseded'
        app.save(second_id)
        app.save(third_id)
        return app, first_id, second_id, third_id

    def test_can_restore_older_release_without_changing_service_or_losing_image_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, first_id, second_id, third_id = self.three_releases(root)
            with patch('sky_platform.api.server.threading.Thread'):
                app.start_release_rollback(third_id, first_id)
            self.assertEqual(app.jobs[third_id]['release_rollback_target_id'], first_id)
            with patch('sky_platform.api.server.AwsExpressAdapter.rollback_release', return_value={'state': 'successful'}):
                app.run_release_rollback(third_id)
            self.assertEqual(app.jobs[first_id]['deployment_state'], 'active')
            self.assertEqual(app.jobs[second_id]['deployment_state'], 'superseded')
            self.assertEqual(app.jobs[third_id]['deployment_state'], 'superseded')
            self.assertEqual(app.jobs[first_id]['result']['images'], app.jobs[third_id]['result']['images'])
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[first_id]['deployment_state'], 'active')
            self.assertEqual(restarted.jobs[second_id]['deployment_state'], 'superseded')

    def test_old_release_selection_requires_same_service_and_saved_task_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            app, first_id, _second_id, third_id = self.three_releases(Path(directory))
            first_result = app.jobs[first_id]['result']
            for key, value in [('service_arn', 'another-service'), ('task_definition_arn', None),
                               ('image', REPOSITORY + ':untracked-a1')]:
                original = first_result[key]
                first_result[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    app.start_release_rollback(third_id, first_id)
                first_result[key] = original
            self.assertNotIn('release_rollback_state', app.jobs[third_id])

    def test_rollback_api_accepts_selected_historical_release(self):
        with tempfile.TemporaryDirectory() as directory:
            app, first_id, _second_id, third_id = self.three_releases(Path(directory))
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = f'/api/jobs/{third_id}/rollback-release'
            payload = json.dumps({'target_job_id': first_id}).encode()
            handler.headers = {'X-Sky-Token': app.token, 'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            with patch('sky_platform.api.server.threading.Thread'):
                handler.do_POST()
            handler.json_response.assert_called_once_with(202,
                {'id': third_id, 'release_rollback_state': 'running'})
            self.assertEqual(app.jobs[third_id]['release_rollback_target_id'], first_id)

    def test_reconcile_rollback_to_older_release_uses_selected_task_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            app, first_id, second_id, third_id = self.three_releases(Path(directory))
            app.jobs[third_id].update(release_rollback_state='needs_attention',
                release_rollback_submitted=True, release_rollback_target_id=first_id,
                release_rollback_previous_deployment_arn='old-deployment',
                deployment_state='needs_attention')
            app.save(third_id)
            aws = self.reconcile_aws('SUCCESSFUL', 'restored-deployment', app.jobs[first_id]['result']['image'])
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                self.assertEqual(app.reconcile_release_rollback(third_id)['release'], 'previous')
            self.assertEqual(app.jobs[first_id]['deployment_state'], 'active')
            self.assertEqual(app.jobs[second_id]['deployment_state'], 'superseded')

    def test_reactivating_previously_rolled_back_release_does_not_revive_older_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, first_id, second_id, third_id = self.three_releases(root)
            app.jobs[second_id]['release_rollback_state'] = 'succeeded'
            app.jobs[second_id]['release_rollback_target_id'] = first_id
            app.jobs[second_id]['release_rollback_restore_pending'] = False
            app.save(second_id)
            app.finish_release_rollback(third_id, second_id)
            fourth_id = 'd' * 16
            fourth_source = root / fourth_id / 'source' / 'app'
            fourth_source.mkdir(parents=True)
            app.jobs[fourth_id] = {**app.jobs[third_id], 'id': fourth_id,
                'project': str(fourth_source), 'created_at': '2026-09-30T00:00:00+00:00',
                'replaces_job_id': second_id, 'deployment_state': 'active',
                'result': {**app.jobs[third_id]['result'],
                           'image': REPOSITORY + ':' + fourth_id + '-a1'}}
            app.jobs[fourth_id].pop('release_rollback_state', None)
            app.jobs[fourth_id].pop('release_rollback_target_id', None)
            app.jobs[second_id]['deployment_state'] = 'superseded'
            app.save(second_id)
            app.save(fourth_id)
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[first_id]['deployment_state'], 'superseded')
            self.assertEqual(restarted.jobs[second_id]['deployment_state'], 'superseded')
            self.assertEqual(restarted.jobs[fourth_id]['deployment_state'], 'active')

    def test_completed_release_rollback_activates_previous_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.completed_release(root)
            with patch('sky_platform.api.server.threading.Thread'):
                app.start_release_rollback(new_id)
            self.assertEqual(app.jobs[new_id]['release_rollback_state'], 'running')
            with self.assertRaises(ValueError):
                app.ensure_application_available('my-app', 'aws-ecs-express')
            def rollback(_adapter, _current, _previous, _health_path, checkpoint):
                checkpoint(release_rollback_submitted=True,
                           release_rollback_previous_deployment_arn='old-deployment')
                return {'state': 'successful'}
            with patch('sky_platform.api.server.AwsExpressAdapter.rollback_release', autospec=True, side_effect=rollback):
                app.run_release_rollback(new_id)
            self.assertEqual(app.jobs[new_id]['release_rollback_state'], 'succeeded')
            self.assertEqual(app.jobs[new_id]['deployment_state'], 'superseded')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'active')
            self.assertEqual(app.jobs[old_id]['result']['images'], app.jobs[new_id]['result']['images'])
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'active')

    def test_restart_preserves_release_after_prior_rollback_and_redeploy(self):
        for successor_id, successor_state in [('0' * 16, 'active'), ('f' * 16, 'deleted')]:
            with self.subTest(successor_id=successor_id, successor_state=successor_state):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    app, old_id, rolled_back_id = self.completed_release(root)
                    app.finish_release_rollback(rolled_back_id, old_id)
                    app.jobs[old_id]['deployment_state'] = 'superseded'
                    successor_source = root / successor_id / 'source' / 'app'
                    successor_source.mkdir(parents=True)
                    app.jobs[successor_id] = {
                        **app.jobs[rolled_back_id], 'id': successor_id,
                        'project': str(successor_source),
                        'created_at': '2026-09-29T00:00:00+00:00',
                        'replaces_job_id': old_id, 'deployment_state': successor_state,
                        'result': {**app.jobs[rolled_back_id]['result'],
                                   'image': REPOSITORY + ':' + successor_id + '-a1'},
                    }
                    app.jobs[successor_id].pop('release_rollback_state', None)
                    app.jobs[successor_id].pop('release_rollback_target_id', None)
                    app.save(old_id)
                    app.save(successor_id)
                    restarted = App(root, AISettings('fixture-key', 'fixture-model'),
                                    aws_settings=AwsSettings(REGION))
                    self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'superseded')
                    self.assertEqual(restarted.jobs[successor_id]['deployment_state'], successor_state)

    def test_restart_finishes_partially_saved_rollback_without_successor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.completed_release(root)
            app.jobs[new_id]['release_rollback_state'] = 'succeeded'
            app.jobs[new_id]['deployment_state'] = 'superseded'
            app.save(new_id)
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'active')

    def test_upload_after_release_rollback_replaces_restored_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, rolled_back_id = self.completed_release(root)
            app.finish_release_rollback(rolled_back_id, old_id)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
                bundle.writestr('server.js', 'require("node:http").createServer((_,res)=>res.end("ok")).listen(3000)')
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true',
                               'X-Application-Id': 'my-app'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('sky_platform.api.server.AwsSettings.unavailable_reason', return_value=None), \
                    patch('sky_platform.api.server.threading.Thread'):
                handler.do_POST()
            third_id = handler.json_response.call_args.args[1]['id']
            self.assertEqual(app.jobs[third_id]['replaces_job_id'], old_id)
            self.assertEqual(app.jobs[third_id]['prior_result']['image'], app.jobs[old_id]['result']['image'])
            third_result = {**app.jobs[old_id]['result'],
                            'image': REPOSITORY + ':' + third_id + '-a1',
                            'task_definition_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:3',
                            'previous_task_definition_arn': app.jobs[old_id]['result']['task_definition_arn']}
            with patch('sky_platform.api.server.DeploymentAgent.run', return_value=third_result):
                app.run_agent(third_id)
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'superseded')
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[third_id]['deployment_state'], 'active')
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'superseded')

    def test_completed_release_rollback_failure_can_be_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.completed_release(root)
            with patch('sky_platform.api.server.threading.Thread'):
                app.start_release_rollback(new_id)
            def rollback(_adapter, _current, _previous, _health_path, checkpoint):
                checkpoint(release_rollback_submitted=True,
                           release_rollback_previous_deployment_arn='old-deployment')
                raise RuntimeError('HTTP verification failed')
            with patch('sky_platform.api.server.AwsExpressAdapter.rollback_release', autospec=True, side_effect=rollback):
                app.run_release_rollback(new_id)
            self.assertEqual(app.jobs[new_id]['release_rollback_state'], 'needs_attention')
            self.assertEqual(app.jobs[new_id]['deployment_state'], 'needs_attention')
            aws = self.reconcile_aws('SUCCESSFUL', 'rollback-deployment', REPOSITORY + ':' + FIRST_ATTEMPT)
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                self.assertEqual(app.reconcile_release_rollback(new_id)['release'], 'previous')
            self.assertEqual(app.jobs[old_id]['deployment_state'], 'active')

    def test_completed_release_rollback_interruption_and_aws_revert_restore_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, old_id, new_id = self.completed_release(root)
            app.jobs[new_id].update(release_rollback_state='running', release_rollback_submitted=True,
                                    release_rollback_target_id=old_id,
                                    release_rollback_previous_deployment_arn='old-deployment')
            app.save(new_id)
            restarted = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            self.assertEqual(restarted.jobs[new_id]['release_rollback_state'], 'needs_attention')
            with self.assertRaises(ValueError):
                restarted.ensure_application_available('my-app', 'aws-ecs-express')
            aws = self.reconcile_aws('ROLLBACK_SUCCESSFUL', 'rollback-deployment',
                                     REPOSITORY + ':' + new_id + '-a1')
            with patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                    patch('sky_platform.api.server.check_deployment', return_value={'healthy': True, 'reason': 'HTTP 200'}):
                self.assertEqual(restarted.reconcile_release_rollback(new_id)['release'], 'current')
            self.assertEqual(restarted.jobs[new_id]['deployment_state'], 'active')
            self.assertEqual(restarted.jobs[new_id]['release_rollback_state'], 'failed')
            self.assertEqual(restarted.jobs[old_id]['deployment_state'], 'superseded')

    def test_release_rollback_reconciliation_rejects_wrong_task_definition_and_mixed_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            app, old_id, new_id = self.completed_release(Path(directory))
            app.jobs[new_id].update(release_rollback_state='needs_attention',
                                    deployment_state='needs_attention',
                                    release_rollback_submitted=True,
                                    release_rollback_target_id=old_id,
                                    release_rollback_previous_deployment_arn='old-deployment')
            app.save(new_id)
            wrong_task = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:99'
            cases = [
                self.reconcile_aws('SUCCESSFUL', 'rollback-deployment',
                                   app.jobs[old_id]['result']['image'], task_arn=wrong_task),
                self.reconcile_aws('ROLLBACK_SUCCESSFUL', 'rollback-deployment',
                                   app.jobs[new_id]['result']['image'], task_arn=wrong_task),
                self.reconcile_aws('SUCCESSFUL', 'rollback-deployment',
                                   app.jobs[old_id]['result']['image'], extra_active=True),
            ]
            for aws in cases:
                with self.subTest(aws=aws), \
                        patch('sky_platform.api.server.AwsExpressAdapter.aws', side_effect=aws), \
                        patch('sky_platform.api.server.check_deployment') as health:
                    self.assertFalse(app.reconcile_release_rollback(new_id)['reconciled'])
                    health.assert_not_called()
                self.assertEqual(app.jobs[new_id]['deployment_state'], 'needs_attention')
                self.assertEqual(app.jobs[old_id]['deployment_state'], 'superseded')

    def test_same_app_upload_reuses_prior_service_and_supersedes_old_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), aws_settings=AwsSettings(REGION))
            old_source = root / FIRST_ID / 'source' / 'app'
            old_source.mkdir(parents=True)
            old_result = {'service': SERVICE, 'service_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}',
                          'image': REPOSITORY + ':' + FIRST_ATTEMPT,
                          'url': f'https://{SERVICE}.ecs.{REGION}.on.aws', 'target': 'aws-ecs-express',
                          'account': ACCOUNT, 'region': REGION}
            app.jobs[FIRST_ID] = {'id': FIRST_ID, 'mode': 'agent', 'target': 'aws-ecs-express',
                                  'application_id': 'my-app', 'status': 'succeeded',
                                  'deployment_state': 'active', 'project': str(old_source),
                                  'plan': None, 'result': old_result, 'events': [],
                                  'created_at': '2026-09-27T00:00:00+00:00'}
            app.save(FIRST_ID)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
                bundle.writestr('server.js', 'require("node:http").createServer((_,res)=>res.end("ok")).listen(3000)')
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token, 'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true',
                               'X-Application-Id': 'my-app'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('sky_platform.api.server.AwsSettings.unavailable_reason', return_value=None), \
                    patch('sky_platform.api.server.threading.Thread'):
                handler.do_POST()
            new_id = handler.json_response.call_args.args[1]['id']
            new_job = app.jobs[new_id]
            self.assertEqual(new_job['prior_result'], old_result)
            self.assertEqual(new_job['replaces_job_id'], FIRST_ID)
            new_result = {**old_result, 'image': REPOSITORY + ':' + new_id + '-a1',
                          'owner_attempt': FIRST_ATTEMPT, 'images': [old_result['image'], REPOSITORY + ':' + new_id + '-a1']}
            with patch('sky_platform.api.server.DeploymentAgent.run', return_value=new_result):
                app.run_agent(new_id)
            self.assertEqual(app.jobs[new_id]['status'], 'succeeded')
            self.assertEqual(app.jobs[FIRST_ID]['deployment_state'], 'superseded')
            self.assertEqual(app.jobs[new_id]['result']['url'], old_result['url'])


if __name__ == '__main__':
    unittest.main()
