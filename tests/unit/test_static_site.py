"""A static candidate must not silently discard a backend or data requirement."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from application.deployment_core import extract_project
from application.infrastructure import inspect_infrastructure
from engine.static_site import assess_static_site


class StaticSiteAssessmentTests(unittest.TestCase):
    def assess(self, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            return assess_static_site(root, inspect_infrastructure(root))

    def test_html_and_client_assets_are_static_candidate(self):
        result = self.assess({"index.html": '<script src="assets/app.js"></script>',
                              "assets/app.js": "document.body.dataset.ready = 'yes';"})
        self.assertEqual(result.status, "eligible")
        self.assertIn("index.html", result.evidence_files)

    def test_client_call_to_same_origin_api_requires_review(self):
        result = self.assess({"index.html": '<script src="app.js"></script>',
                              "app.js": "fetch('/api/posts').then(console.log)"})
        self.assertEqual(result.status, "needs_review")
        self.assertIn("app.js", result.evidence_files)

    def test_frontend_source_requires_verified_build_output(self):
        result = self.assess({"index.html": '<div id="root"></div>',
                              "package.json": '{"scripts":{"build":"vite build"},'
                                              '"devDependencies":{"vite":"1.0.0"}}'})
        self.assertEqual(result.status, "needs_build")

    def test_game_frontend_does_not_hide_server(self):
        result = self.assess({"index.html": "<h1>Game</h1>",
                              "server.js": "const http = require('node:http');"})
        self.assertEqual(result.status, "server_or_mixed")
        self.assertIn("server.js", result.evidence_files)

    def test_nested_server_is_not_hidden_by_root_index(self):
        result = self.assess({"index.html": "<h1>Game</h1>",
                              "backend/server.js": "const http = require('node:http');"})
        self.assertEqual(result.status, "server_or_mixed")
        self.assertIn("backend/server.js", result.evidence_files)

    def test_sqlite_dependent_site_is_not_static_candidate(self):
        result = self.assess({"index.html": "<h1>Scores</h1>",
                              "db.js": "const Database = require('better-sqlite3');"})
        self.assertEqual(result.status, "server_or_mixed")

    def test_pure_static_zip_can_enter_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "site.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("site/index.html", "<h1>Hello</h1>")
                bundle.writestr("site/style.css", "body { color: navy; }")
            project = extract_project(archive, root / "source")
            self.assertEqual(project.name, "site")
            self.assertEqual(assess_static_site(project, inspect_infrastructure(project)).status,
                             "eligible")

    def test_top_level_static_folder_cannot_hide_sibling_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "mixed.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("site/index.html", "<h1>Hello</h1>")
                bundle.writestr("backend/server.js", "const http = require('node:http');")
            with self.assertRaisesRegex(ValueError, "outside the selected top-level app folder"):
                extract_project(archive, root / "source")


if __name__ == "__main__":
    unittest.main()
