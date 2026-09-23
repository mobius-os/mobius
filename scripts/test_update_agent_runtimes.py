"""Offline contracts for paired, review-only runtime update proposals."""

import json
from pathlib import Path
import runpy
import tempfile
import unittest

UPDATER = runpy.run_path(str(Path(__file__).with_name("update-agent-runtimes.py")))
ROOT = Path(__file__).resolve().parents[1]


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "backend").mkdir()
        (self.root / "Dockerfile").write_text(
            "ARG CODEX_VERSION=1.2.3\n"
            "ARG CODEX_SDK_VERSION=1.2.3\n"
            "pip install --no-deps \"openai-codex==${CODEX_SDK_VERSION}\"\n"
            "pip install \"openai-codex-cli-bin==${CODEX_SDK_VERSION}\"\n"
        )
        (self.root / "backend/requirements.txt").write_text("claude-agent-sdk==1.2.3\n")
        self.codex = "1.2.4"
        self.codex_sdk = "1.2.4"
        self.claude = "1.2.4"
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if "registry.npmjs.org" in url:
            return json.dumps({"version": self.codex})
        if "pypi.org/pypi/openai-codex/" in url:
            return json.dumps({"info": {
                "version": self.codex_sdk,
                "requires_dist": [f"openai-codex-cli-bin=={self.codex_sdk}"],
            }})
        if "pypi.org/pypi/claude-agent-sdk/" in url:
            return json.dumps({"info": {"version": self.claude}})
        self.fail(f"Unexpected registry URL: {url}")

    def propose(self):
        return UPDATER["propose"](self.root, self.fetch)

    def test_matches_independent_codex_cli_and_sdk_releases_without_writing(self):
        files, changes = self.propose()
        self.assertIn("ARG CODEX_VERSION=1.2.4", files["Dockerfile"])
        self.assertIn("ARG CODEX_SDK_VERSION=1.2.4", files["Dockerfile"])
        self.assertIn('openai-codex==${CODEX_SDK_VERSION}', files["Dockerfile"])
        self.assertIn('openai-codex-cli-bin==${CODEX_SDK_VERSION}', files["Dockerfile"])
        self.assertEqual(files["backend/requirements.txt"], "claude-agent-sdk==1.2.4\n")
        self.assertEqual(len(changes), 3)
        self.assertIn("ARG CODEX_VERSION=1.2.3", (self.root / "Dockerfile").read_text())

    def test_no_new_release_does_not_resolve_tags_or_create_churn(self):
        self.codex = self.codex_sdk = self.claude = "1.2.3"
        self.assertEqual(self.propose(), ({}, []))
        self.assertEqual(len(self.urls), 3)

    def test_claude_only_leaves_codex_untouched(self):
        self.codex = self.codex_sdk = "1.2.3"
        files, _ = self.propose()
        self.assertEqual(set(files), {"backend/requirements.txt"})
        self.assertEqual(len(self.urls), 3)

    def test_codex_only_leaves_python_lock_input_untouched(self):
        self.claude = "1.2.3"
        files, _ = self.propose()
        self.assertEqual(set(files), {"Dockerfile"})

    def test_prerelease_and_downgrade_are_not_proposed(self):
        for version in ("1.2.4-alpha.1", "1.2.2", "bad\nvalue"):
            with self.subTest(version=version):
                self.codex = version
                with self.assertRaises(ValueError):
                    self.propose()

        self.codex = "1.2.4"
        self.codex_sdk = "1.2.2"
        with self.assertRaises(ValueError):
            self.propose()

    def test_missing_release_or_network_error_writes_nothing(self):
        def unavailable(url):
            raise OSError("Unavailable")
        before = (self.root / "Dockerfile").read_bytes()
        with self.assertRaises(OSError):
            UPDATER["propose"](self.root, unavailable)
        self.assertEqual(before, (self.root / "Dockerfile").read_bytes())

    def test_source_drift_is_a_visible_failure_not_a_partial_pin_update(self):
        for pattern, text in [(r"(pin=)\S+", "none"), (r"(pin=)\S+", "pin=one pin=two")]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                UPDATER["replace_pin"](text, pattern, "new")

    def test_sdk_runtime_mismatch_requires_review(self):
        self.codex_sdk = "1.2.4"
        with self.assertRaises(ValueError):
            UPDATER["propose"](self.root, lambda url: json.dumps(
                {"version": self.codex} if "registry.npmjs.org" in url else
                {"info": {"version": self.codex_sdk, "requires_dist": ["openai-codex-cli-bin==1.1.0"]}}
                if "openai-codex/" in url else {"info": {"version": self.claude}}
            ))

    def test_workflow_is_upstream_only_review_gated_and_dispatches_real_suite(self):
        workflow = (ROOT / ".github/workflows/agent-runtime-updates.yml").read_text()
        self.assertIn("github.repository == 'mobius-os/mobius'", workflow)
        self.assertIn("draft: always-true", workflow)
        self.assertIn("gh workflow run test.yml --ref automation/agent-runtimes", workflow)
        self.assertNotIn("gh pr merge", workflow)
        self.assertNotIn("enable-pull-request-automerge", workflow)
        self.assertNotIn("/data/", workflow)


if __name__ == "__main__":
    unittest.main()
