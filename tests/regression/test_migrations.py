import json
import tempfile
import unittest
from pathlib import Path

from database.migrations import collect_sql_migrations, stage_migrator_context


class MigrationBundleTests(unittest.TestCase):
    def test_collects_ordered_checksums_and_rejects_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            migrations = project / 'migrations'
            migrations.mkdir()
            (migrations / '0002_index.sql').write_text('CREATE INDEX idx_name ON users (name);\n')
            (migrations / '0001_users.sql').write_text('CREATE TABLE users (name text);\n')
            bundle = collect_sql_migrations(project)
            self.assertEqual([item.name for item in bundle.migrations],
                             ['0001_users.sql', '0002_index.sql'])
            self.assertEqual(len(bundle.digest), 64)
            self.assertIn('0001_users.sql', bundle.manifest())
            staged = stage_migrator_context(bundle, project / 'build-context')
            self.assertTrue((staged / 'Dockerfile').is_file())
            dockerfile = (staged / 'Dockerfile').read_text()
            self.assertIn('npm ci', dockerfile)
            self.assertIn('FROM node:22-alpine@sha256:', dockerfile)
            self.assertIn('NODE_EXTRA_CA_CERTS=/app/rds-global-bundle.pem', dockerfile)
            self.assertGreater((staged / 'rds-global-bundle.pem').read_text().count(
                '-----BEGIN CERTIFICATE-----'), 10)
            lock = json.loads((staged / 'package-lock.json').read_text())
            package = json.loads((staged / 'package.json').read_text())
            self.assertEqual(lock['packages']['']['dependencies'], package['dependencies'])
            self.assertEqual(lock['packages']['node_modules/pg']['version'], '8.23.0')
            self.assertTrue(all(item.get('integrity', '').startswith('sha512-')
                                for name, item in lock['packages'].items() if name))
            self.assertEqual((staged / 'migrations' / '0001_users.sql').read_text(),
                             'CREATE TABLE users (name text);\n')
            self.assertEqual((staged / 'migrations' / 'manifest.json').read_text(), bundle.manifest())
            (migrations / '0001_users.sql').write_text('CREATE TABLE users (name text, id int);\n')
            changed = collect_sql_migrations(project)
            self.assertNotEqual(bundle.digest, changed.digest)
            with self.assertRaisesRegex(ValueError, '계획 뒤 변경'):
                stage_migrator_context(bundle, project / 'another-build-context')

    def test_rejects_duplicate_versions_links_and_transaction_control(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            migrations = project / 'migrations'
            migrations.mkdir()
            first = migrations / '0001_users.sql'
            first.write_text('CREATE TABLE users (id int);')
            second = migrations / '0001_other.sql'
            second.write_text('CREATE TABLE other (id int);')
            with self.assertRaisesRegex(ValueError, '중복'):
                collect_sql_migrations(project)
            second.unlink()
            first.write_text('BEGIN; CREATE TABLE users (id int); COMMIT;')
            with self.assertRaisesRegex(ValueError, '트랜잭션'):
                collect_sql_migrations(project)
            first.write_text('CREATE TABLE users (id int);')
            second.symlink_to(first)
            with self.assertRaisesRegex(ValueError, '일반 파일'):
                collect_sql_migrations(project)


if __name__ == '__main__':
    unittest.main()
