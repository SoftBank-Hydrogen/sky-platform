import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from application.analysis import AISettings
from interfaces.http.server import App, handler_for


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return output.getvalue()


class CompatibilityPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name), AISettings(), monitor_interval=0)
        handler_type = handler_for(self.app)
        self.handler = handler_type.__new__(handler_type)
        self.handler.path = '/api/compatibility'
        self.handler.json_response = Mock()

    def preview(self, content, public='true'):
        self.handler.headers = {'X-Sky-Token': self.app.token,
                                'X-Public-Access': public,
                                'Content-Length': str(len(content)),
                                'Content-Type': 'application/zip'}
        self.handler.rfile = io.BytesIO(content)
        self.handler.do_POST()
        return self.handler.json_response.call_args.args

    def test_preview_compares_all_targets_without_ai_or_job_creation(self):
        status, payload = self.preview(archive({
            'package.json': '{"scripts":{"start":"node server.js"}}',
            'server.js': 'require("node:http").createServer((q,r)=>r.end("ok"))'
        }))
        self.assertEqual(status, 200)
        reports = {item['target']: item for item in payload['reports']}
        self.assertEqual(set(reports), {'local-docker', 'onprem-compose', 'aws-ecs-express', 'cloud-run'})
        self.assertTrue(all(item['compatible'] for item in reports.values()))
        self.assertEqual(reports['local-docker']['access_mode'], 'loopback')
        self.assertEqual(reports['onprem-compose']['access_mode'], 'loopback')
        self.assertEqual(reports['aws-ecs-express']['access_mode'], 'public')
        self.assertTrue(all(item['cost']['estimate'] is None for item in reports.values()))
        self.assertEqual(payload['inspection']['requirements'], [])
        self.assertEqual(payload['inspection']['scanned_files'], 2)
        self.assertEqual(payload['application_ir']['schema_version'], 2)
        self.assertEqual(payload['application_ir']['source_revision'], payload['source_digest'])
        self.assertEqual(payload['application_ir']['unknowns'], ('component_topology', 'statelessness'))
        self.assertEqual(payload['deployment_policy']['selection_mode'], 'auto_target')
        self.assertTrue(payload['deployment_policy']['public_access_allowed'])
        self.assertIn('aws-ecs-express', payload['deployment_policy']['allowed_targets'])
        self.assertIsNone(payload['deployment_policy']['max_monthly_cost_usd'])
        capability_models = payload['capability_models']
        self.assertEqual(set(capability_models), set(reports) | {'aws-s3-cloudfront', 'aws-ecs-standard'})
        self.assertTrue(all(model['schema_version'] == 1 for model in capability_models.values()))
        aws_capabilities = {item['id']: item for item in capability_models['aws-ecs-express']['capabilities']}
        self.assertEqual(aws_capabilities['existing_rds_binding']['display_status'], 'implemented_unverified')
        self.assertEqual(aws_capabilities['new_rds_provisioning']['display_status'], 'implemented_unverified')
        self.assertEqual(aws_capabilities['sqlite_volume']['display_status'], 'unsupported_by_sky')
        self.assertTrue(all(not item['verification_refs'] for item in aws_capabilities.values()))
        candidates = {item['id']: item for item in payload['candidates']}
        self.assertEqual(candidates['local-docker']['status'], 'eligible')
        self.assertEqual(candidates['local-docker']['cost_estimate'], None)
        self.assertTrue(all(not item['selected'] for item in candidates.values()))
        self.assertEqual(self.app.jobs, {})
        self.assertFalse(list(Path(self.temp.name).glob('*/job.json')))

    def test_preview_explains_unsupported_sqlite_on_every_target(self):
        status, payload = self.preview(archive({
            'package.json': '{"dependencies":{"better-sqlite3":"11.0.0"}}',
            'server.js': 'require("better-sqlite3")("app.db")'
        }))
        self.assertEqual(status, 200)
        self.assertTrue(all(not item['compatible'] for item in payload['reports']))
        self.assertTrue(all(any('SQLite' in issue for issue in item['problems'])
                            for item in payload['reports']))
        self.assertIn('sqlite', payload['inspection']['requirements'])
        self.assertIn('package.json', payload['inspection']['evidence_files'])
        ir = payload['application_ir']
        self.assertEqual(ir['topology_status'], 'unresolved')
        self.assertEqual(ir['source_revision'], payload['source_digest'])
        self.assertEqual(ir['components'][0]['kind'], 'unresolved')
        sqlite_requirement = next(item for item in ir['requirements'] if item['kind'] == 'sqlite')
        evidence_ids = set(sqlite_requirement['evidence_ids'])
        self.assertTrue(evidence_ids)
        self.assertEqual({item['path'] for item in ir['evidence'] if item['id'] in evidence_ids},
                         {'package.json', 'server.js'})
        self.assertTrue(all(item['source']['revision'] == ir['source_revision']
                            and item['source']['line'] is None
                            and item['verified_by'] == () for item in ir['evidence']))
        for report in payload['reports']:
            decision = next(item for item in report['constraint_results']
                            if item['rule_id'] == 'DATA-SQLITE-01')
            self.assertEqual(decision['status'], 'violated')
            self.assertEqual(set(decision['evidence_ids']), evidence_ids)
            self.assertIn(decision['reason'], report['problems'])
        for candidate in payload['candidates']:
            self.assertEqual(candidate['status'], 'rejected')
            if candidate['id'] != 'aws-s3-cloudfront':
                self.assertIn('DATA-SQLITE-01', candidate['violated_rule_ids'])
                self.assertEqual(set(candidate['evidence_ids']), evidence_ids)
        self.assertIn('STATIC-SERVER-01', payload['candidates'][-1]['reason_codes'])

    def test_websocket_and_process_local_state_remain_inferred_and_auto_needs_review(self):
        status, payload = self.preview(archive({
            'package.json': '{"scripts":{"start":"node server.js"}}',
            'server.js': 'const {WebSocketServer}=require("ws"); const clients = new Map(); '
                         'const wss = new WebSocketServer({noServer:true}); '
                         'if (message.type === "sky.probe") send("sky.probe.ack");',
        }))
        self.assertEqual(status, 200)
        ir = payload['application_ir']
        hypotheses = {item['kind']: item for item in ir['hypotheses']}
        self.assertEqual(set(hypotheses),
                         {'websocket', 'possible-process-local-state', 'sky-probe-protocol'})
        self.assertTrue(all(item['status'] == 'inferred' for item in hypotheses.values()))
        self.assertTrue(all(item['status'] == 'inferred' for item in ir['evidence']))
        self.assertIn('session_affinity_behavior', ir['unknowns'])
        self.assertIn('target_websocket_round_trip', ir['unknowns'])
        self.assertEqual({item['status'] for item in payload['candidates'] if item['id'] != 'aws-s3-cloudfront'},
                         {'needs_review'})
        self.assertTrue(all('PROTOCOL-WS-01' in item['unknown_rule_ids']
                            and item['evidence_ids'] for item in payload['candidates']
                            if item['id'] != 'aws-s3-cloudfront'))
        self.assertTrue(all(not item['preview_eligible'] for item in payload['reports']))
        self.assertTrue(all(any(rule['rule_id'] == 'PROTOCOL-WS-01' and rule['status'] == 'unknown'
                                for rule in report['constraint_results']) for report in payload['reports']))
        self.assertEqual(self.app.jobs, {})

    def test_ir_is_stable_for_the_same_extracted_source(self):
        files = {
            'package.json': '{"scripts":{"start":"node server.js"}}',
            'server.js': 'require("node:http").createServer((q,r)=>r.end("ok"))',
        }
        first_status, first = self.preview(archive(files))
        second_status, second = self.preview(archive(dict(reversed(list(files.items())))))
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first['source_digest'], second['source_digest'])
        self.assertEqual(first['application_ir'], second['application_ir'])
        self.assertEqual(first['application_ir']['source_revision'], first['source_digest'])
        files['server.js'] += '\n// changed source revision'
        changed_status, changed = self.preview(archive(files))
        self.assertEqual(changed_status, 200)
        self.assertNotEqual(changed['application_ir']['source_revision'], first['application_ir']['source_revision'])
        self.assertEqual(changed['application_ir']['source_revision'], changed['source_digest'])

    def test_access_rule_uses_user_intent_without_inventing_source_evidence(self):
        status, payload = self.preview(archive({
            'package.json': '{"scripts":{"start":"node server.js"}}',
            'server.js': 'console.log("ready")',
        }), public='false')
        self.assertEqual(status, 200)
        aws = next(item for item in payload['reports'] if item['target'] == 'aws-ecs-express')
        access = next(item for item in aws['constraint_results'] if item['rule_id'] == 'ACCESS-01')
        self.assertEqual(access['status'], 'violated')
        self.assertEqual(access['evidence_ids'], [])
        self.assertFalse(aws['compatible'])

    def test_configuration_gap_is_not_reported_as_constraint_failure(self):
        with patch('interfaces.http.server.AwsSettings.unavailable_reason',
                   return_value='AWS credentials missing'):
            status, payload = self.preview(archive({
                'package.json': '{"scripts":{"start":"node server.js"}}',
                'server.js': 'console.log("ready")',
            }))
        self.assertEqual(status, 200)
        candidates = {item['id']: item for item in payload['candidates']}
        self.assertEqual(candidates['aws-ecs-express']['status'], 'requires_setup')
        self.assertEqual(candidates['aws-ecs-express']['violated_rule_ids'], [])

    def test_postgres_binding_is_conditional_only_on_supported_aws_target(self):
        status, payload = self.preview(archive({
            'package.json': '{"dependencies":{"pg":"8.0.0"}}',
            'server.js': 'const database = require("pg");',
        }))
        self.assertEqual(status, 200)
        candidates = {item['id']: item for item in payload['candidates']}
        self.assertEqual(candidates['aws-ecs-express']['status'], 'requires_database_binding')
        self.assertEqual(candidates['aws-ecs-express']['violated_rule_ids'], ['DATA-BINDING-01'])
        self.assertEqual(candidates['local-docker']['status'], 'rejected')
        self.assertEqual(candidates['cloud-run']['status'], 'rejected')

    def test_static_bundle_is_aws_candidate_for_upload_auto_selection(self):
        with patch('interfaces.http.server.AwsStaticSiteAdapter.unavailable_reason', return_value=None):
            status, payload = self.preview(archive({
                'index.html': '<h1>Sky</h1>',
                'assets/app.js': 'document.body.dataset.ready = "yes";',
            }))
        self.assertEqual(status, 200)
        candidates = {item['id']: item for item in payload['candidates']}
        static = candidates['aws-s3-cloudfront']
        self.assertEqual((static['provider'], static['backend']), ('aws', 'static_hosting'))
        self.assertEqual(static['structural_status'], 'compatible')
        self.assertEqual(static['selection_mode'], 'automatic')
        self.assertEqual(static['status'], 'eligible')
        self.assertEqual(payload['static_site']['adapter_status'], 'available')
        self.assertFalse(static['selected'])
        self.assertIn('index.html', static['evidence_files'])
        self.assertNotIn('aws-ecs-standard', candidates)

    def test_frontend_source_needs_build_and_server_keeps_its_requirements(self):
        _, frontend = self.preview(archive({
            'index.html': '<div id="root"></div>',
            'package.json': '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"1.0.0"}}',
        }))
        static = next(item for item in frontend['candidates'] if item['id'] == 'aws-s3-cloudfront')
        self.assertEqual((static['status'], static['structural_status']),
                         ('needs_build', 'potentially_compatible'))
        _, worker = self.preview(archive({
            'index.html': '<h1>worker</h1>',
            'package.json': '{"dependencies":{"bullmq":"1.0.0"}}',
            'server.js': 'require("bullmq")',
        }))
        static = next(item for item in worker['candidates'] if item['id'] == 'aws-s3-cloudfront')
        self.assertEqual(static['status'], 'rejected')
        self.assertIn('background-worker', worker['inspection']['requirements'])


if __name__ == '__main__':
    unittest.main()
