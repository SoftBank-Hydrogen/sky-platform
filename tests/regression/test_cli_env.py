import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.cli import load_local_env, main


class LocalEnvironmentTests(unittest.TestCase):
    def test_loads_private_file_and_keeps_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENAI_API_KEY="file-key=123"\nSKY_AI_MODEL=fixture-model\n')
            path.chmod(0o600)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'shell-key'}, clear=True):
                load_local_env(path)
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'shell-key')
                self.assertEqual(os.environ['SKY_AI_MODEL'], 'fixture-model')

    def test_rejects_readable_by_others_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENAI_API_KEY=private\n')
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'chmod 600'):
                load_local_env(path)
            path.chmod(0o600)
            alias = Path(directory) / 'linked.env'
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, '심볼릭 링크'):
                load_local_env(alias)
            alias.unlink()
            alias.symlink_to(Path(directory) / 'missing')
            with self.assertRaisesRegex(ValueError, '심볼릭 링크'):
                load_local_env(alias)

    def test_invalid_file_does_not_partially_set_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENAI_API_KEY=private\nPATH=/unexpected\n')
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, '2행'):
                    load_local_env(path)
                self.assertNotIn('OPENAI_API_KEY', os.environ)

    def test_entry_point_loads_file_before_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OPENAI_API_KEY=fixture-key\n')
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True), \
                 patch('interfaces.cli.Path.cwd', return_value=Path(directory)), \
                 patch('interfaces.cli.serve') as serve:
                main()
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'fixture-key')
                serve.assert_called_once_with(product_name='Sky', default_state_dir='.sky')


if __name__ == '__main__':
    unittest.main()
