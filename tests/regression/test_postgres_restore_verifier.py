import json
import tempfile
import unittest
from pathlib import Path

from adapters.database.migrations import collect_sql_migrations
from adapters.aws.postgres_restore_verifier import stage_restore_verifier_context


class RestoreVerifierContextTests(unittest.TestCase):
    def test_stages_only_checked_manifest_and_verifier_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'app'
            (project / 'migrations').mkdir(parents=True)
            (project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo (id int);')
            (project / 'secret.txt').write_text('never copy')
            bundle = collect_sql_migrations(project)
            staged = stage_restore_verifier_context(bundle, root / 'context')
            self.assertEqual(json.loads((staged / 'migrations' / 'manifest.json').read_text()),
                             {'migrations': [{'name': '0001_init.sql',
                                              'sha256': bundle.migrations[0].sha256}]})
            self.assertFalse((staged / 'secret.txt').exists())
            self.assertFalse((staged / 'migrations' / '0001_init.sql').exists())
            self.assertTrue((staged / 'rds-global-bundle.pem').exists())
            with self.assertRaisesRegex(ValueError, '새 경로'):
                stage_restore_verifier_context(bundle, staged)

    def test_refuses_bundle_changed_after_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'app'
            (project / 'migrations').mkdir(parents=True)
            migration = project / 'migrations' / '0001_init.sql'
            migration.write_text('CREATE TABLE demo (id int);')
            bundle = collect_sql_migrations(project)
            migration.write_text('CREATE TABLE changed (id int);')
            with self.assertRaisesRegex(ValueError, '변경'):
                stage_restore_verifier_context(bundle, root / 'context')


if __name__ == '__main__':
    unittest.main()
