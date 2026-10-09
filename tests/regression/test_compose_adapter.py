import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adapters.local.compose import LocalComposeAdapter


class ComposeAdapterTests(unittest.TestCase):
    def test_compose_config_keeps_environment_values_out_of_persistent_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = LocalComposeAdapter(lambda *_: None, Path(temporary))
            attempt = "a" * 16 + "-a1"
            adapter._write_compose(
                attempt,
                "sky/" + attempt + ":latest",
                SimpleNamespace(port=8080),
                12345,
                {"SESSION_SECRET": "synthetic-private-value"},
            )
            content = adapter.compose_file.read_text()
            service = json.loads(content)["services"]["app"]
            self.assertEqual(service["environment"]["SESSION_SECRET"], "${SESSION_SECRET:?}")
            self.assertEqual(service["ports"], ["127.0.0.1:12345:8080"])
            self.assertNotIn("synthetic-private-value", content)
            self.assertEqual(stat.S_IMODE(adapter.compose_file.stat().st_mode), 0o600)
            adapter._checked_compose_file(attempt, adapter.compose_digest)
            adapter.compose_file.write_text(content + "\n")
            with self.assertRaisesRegex(ValueError, "무결성"):
                adapter._checked_compose_file(attempt, adapter.compose_digest)

    def test_interrupted_cleanup_rejects_foreign_compose_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = LocalComposeAdapter(lambda *_: None, Path(temporary))
            foreign = {
                "Config": {
                    "Image": "sky/" + "a" * 16 + "-a1:latest",
                    "Labels": {
                        "app": "sky",
                        "sky-attempt": "a" * 16 + "-a1",
                        "com.docker.compose.project": "someone-else",
                    },
                }
            }
            with (
                patch.object(adapter, "inspect_resource", return_value=foreign),
                patch.object(adapter, "command") as command,
            ):
                with self.assertRaisesRegex(ValueError, "소유권"):
                    adapter.retire_orphan("a" * 16 + "-a1")
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
