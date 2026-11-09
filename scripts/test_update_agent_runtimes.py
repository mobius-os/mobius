"""Offline contracts for paired, review-only runtime update proposals."""

import json
from pathlib import Path
import runpy
import tempfile
import unittest
from urllib.error import HTTPError

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
            "ARG CHROME_VERSION=1.0.0.1\n"
            f"ARG CHROME_SHA256={'0' * 64}\n"
            f"ARG CHROME_HEADLESS_SHELL_SHA256={'0' * 64}\n"
            "pip install --no-deps \"openai-codex==${CODEX_SDK_VERSION}\"\n"
            "pip install \"openai-codex-cli-bin==${CODEX_SDK_VERSION}\"\n"
        )
        (self.root / "backend/requirements.txt").write_text("claude-agent-sdk==1.2.3\n")
        # npm and PyPI latest tags, every version each publishes, and the CLI
        # each SDK release declares.
        self.codex = "1.2.4"
        self.codex_sdk = "1.2.4"
        self.npm_versions = {"1.2.3", "1.2.4"}
        self.sdk_releases = {"1.2.3": "1.2.3", "1.2.4": "1.2.4"}
        self.yanked = set()
        self.claude = "1.2.4"
        self.chrome = "1.0.0.1"
        self.urls = []
        self.hashed = []

    def fetch(self, url):
        self.urls.append(url)
        if url == f"{UPDATER['NPM_CODEX']}/latest":
            return json.dumps({"version": self.codex})
        if url.startswith(f"{UPDATER['NPM_CODEX']}/"):
            version = url.rsplit("/", 1)[1]
            if version not in self.npm_versions:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps({"version": version})
        if url == f"{UPDATER['PYPI_CODEX']}/json":
            return json.dumps({
                "info": self.sdk_info(self.codex_sdk),
                "releases": {version: [{"yanked": version in self.yanked}]
                             for version in self.sdk_releases},
            })
        if url.startswith(f"{UPDATER['PYPI_CODEX']}/"):
            return json.dumps({"info": self.sdk_info(url.split("/")[-2])})
        if "pypi.org/pypi/claude-agent-sdk/" in url:
            return json.dumps({"info": {"version": self.claude}})
        if url == UPDATER["CHROME_FEED"]:
            return json.dumps({"channels": {"Stable": {
                "version": self.chrome,
                "downloads": {name: [
                    {"platform": "mac-arm64", "url": f"https://example.invalid/{name}.zip"},
                    {"platform": "linux64", "url": self.chrome_url(name)},
                ] for name in ("chrome", "chrome-headless-shell")},
            }}})
        self.fail(f"Unexpected registry URL: {url}")

    def sdk_info(self, version):
        return {"version": version, "requires_dist": [
            "pydantic>=2", f"openai-codex-cli-bin=={self.sdk_releases.get(version, version)}"]}

    def chrome_url(self, name):
        return (f"https://storage.googleapis.com/chrome-for-testing-public/"
                f"{self.chrome}/linux64/{name}-linux64.zip")

    def digest(self, url):
        self.hashed.append(url)
        return ("a" if "headless-shell" in url else "b") * 64

    def propose(self):
        return UPDATER["propose"](self.root, self.fetch, self.digest)

    def test_matches_codex_cli_and_sdk_releases_without_writing(self):
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
        self.assertEqual(len(self.urls), 4)
        self.assertEqual(self.hashed, [])

    def test_claude_only_leaves_codex_untouched(self):
        self.codex = self.codex_sdk = "1.2.3"
        files, _ = self.propose()
        self.assertEqual(set(files), {"backend/requirements.txt"})
        self.assertEqual(len(self.urls), 4)

    def test_new_chrome_pins_version_with_hashes_of_the_urls_the_build_fetches(self):
        self.codex = self.codex_sdk = self.claude = "1.2.3"
        self.chrome = "1.0.0.12"
        files, changes = self.propose()
        self.assertEqual(changes, ["Chrome for Testing: 1.0.0.1 → 1.0.0.12"])
        self.assertEqual(set(files), {"Dockerfile"})
        self.assertIn("ARG CHROME_VERSION=1.0.0.12\n", files["Dockerfile"])
        self.assertIn(f"ARG CHROME_SHA256={'b' * 64}\n", files["Dockerfile"])
        self.assertIn(f"ARG CHROME_HEADLESS_SHELL_SHA256={'a' * 64}\n", files["Dockerfile"])
        self.assertEqual(self.hashed, [self.chrome_url("chrome"),
                                       self.chrome_url("chrome-headless-shell")])

    def test_chrome_downgrade_or_unexpected_download_is_refused(self):
        self.codex = self.codex_sdk = self.claude = "1.2.3"
        self.chrome = "1.0.0.0"
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.propose()
        self.chrome = "1.0.0.2"
        self.chrome_url = lambda name: f"https://example.invalid/{name}-linux64.zip"
        with self.assertRaisesRegex(ValueError, "does not list"):
            self.propose()
        self.assertEqual(self.hashed, [])

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

    def test_newest_release_on_both_registries_is_paired_when_latest_tags_differ(self):
        cases = {
            "npm ahead": ("1.2.5", "1.2.4", {"1.2.4", "1.2.5"}, {}, set()),
            "PyPI ahead": ("1.2.4", "1.2.5", {"1.2.4"}, {"1.2.5": "1.2.5"}, set()),
            "newer SDK missing on npm": ("1.2.6", "1.2.5", {"1.2.4", "1.2.6"},
                                         {"1.2.5": "1.2.5"}, set()),
            "newer SDK yanked": ("1.2.5", "1.2.5", {"1.2.4", "1.2.5"},
                                 {"1.2.5": "1.2.5"}, {"1.2.5"}),
        }
        for case, (cli, sdk, npm, extra_sdk, yanked) in cases.items():
            with self.subTest(case):
                self.codex, self.codex_sdk, self.yanked = cli, sdk, yanked
                self.npm_versions = {"1.2.3"} | npm
                self.sdk_releases = {"1.2.3": "1.2.3", "1.2.4": "1.2.4", **extra_sdk}
                files, changes = self.propose()
                self.assertIn("ARG CODEX_VERSION=1.2.4\n", files["Dockerfile"])
                self.assertIn("ARG CODEX_SDK_VERSION=1.2.4\n", files["Dockerfile"])
                self.assertEqual(changes, ["Codex: 1.2.3 → 1.2.4",
                                           "Codex Python SDK: 1.2.3 → 1.2.4",
                                           "Claude SDK + bundled CLI: 1.2.3 → 1.2.4"])

    def test_no_newer_matching_codex_pair_skips_only_codex(self):
        cases = {
            "PyPI behind": ("1.2.4", "1.2.3", {"1.2.4"}, {}),
            "npm skipped the SDK release": ("1.2.5", "1.2.4", {"1.2.5"}, {"1.2.4": "1.2.4"}),
        }
        for case, (cli, sdk, npm, sdk_releases) in cases.items():
            with self.subTest(case):
                self.codex, self.codex_sdk = cli, sdk
                self.npm_versions = {"1.2.3"} | npm
                self.sdk_releases = {"1.2.3": "1.2.3", **sdk_releases}
                files, changes = self.propose()
                self.assertEqual(files, {"backend/requirements.txt": "claude-agent-sdk==1.2.4\n"})
                self.assertEqual(changes, [
                    f"Codex: kept 1.2.3; no newer release is on both npm ({cli}) and PyPI ({sdk})",
                    "Claude SDK + bundled CLI: 1.2.3 → 1.2.4",
                ])

    def test_mismatched_codex_pins_are_refused(self):
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace(
            "CODEX_SDK_VERSION=1.2.3", "CODEX_SDK_VERSION=1.2.2"))
        with self.assertRaisesRegex(ValueError, "pins differ"):
            self.propose()

    def test_missing_release_or_network_error_writes_nothing(self):
        def unavailable(url):
            raise OSError("Unavailable")
        before = (self.root / "Dockerfile").read_bytes()
        with self.assertRaises(OSError):
            UPDATER["propose"](self.root, unavailable, self.digest)
        self.assertEqual(before, (self.root / "Dockerfile").read_bytes())

    def test_source_drift_is_a_visible_failure_not_a_partial_pin_update(self):
        for pattern, text in [(r"(pin=)\S+", "none"), (r"(pin=)\S+", "pin=one pin=two")]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                UPDATER["replace_pin"](text, pattern, "new")

    def test_sdk_runtime_mismatch_requires_review(self):
        self.sdk_releases["1.2.4"] = "1.1.0"
        with self.assertRaisesRegex(ValueError, "does not declare its matching CLI"):
            self.propose()

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
