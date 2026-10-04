"""The installer's bridge that finishes an update waiting for the Host helper.

scripts/finish-helper-update.py runs inside an OLDER release's container,
against that release's own updater. These tests run it against a small stand-in
of that updater's interface to pin what it may and may not do; the real old
release is exercised by scripts/test-upgrade-path.sh.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "scripts" / "finish-helper-update.py"
INSTALLER = ROOT / "scripts" / "install-rebuild-helper.sh"

STAND_IN = {
  "__init__.py": "",
  "platform_activation.py": """
    RULES = {"deployment/self-hosted-helper.required": ["host_maintenance"],
             "deployment/other-host-work": ["host_maintenance"],
             "Dockerfile": ["image_rebuild"],
             "docker-compose.yml": ["container_recreate"]}
    def classify_activation(paths):
        actions = sorted({a for p in paths for a in RULES.get(p, [])})
        return {"required_actions": actions}
  """,
  "platform_update.py": """
    import json, os
    from pathlib import Path
    PLATFORM_REPO = Path(os.environ["BRIDGE_REPO"])
    CALLS = Path(os.environ["BRIDGE_CALLS"])
    def _record(name, value):
        calls = json.loads(CALLS.read_text()) if CALLS.exists() else []
        CALLS.write_text(json.dumps(calls + [[name, value]]))
    def platform_update_preview(target_sha=None):
        _record("preview", target_sha)
        return json.loads(os.environ["BRIDGE_PREVIEW"])
    def activation_changes_python_dependencies(impact):
        return "python_dependencies" in json.dumps(impact)
    def image_activates_updates():
        return os.environ.get("BRIDGE_IMAGE_ACTIVATES", "1") == "1"
    def prepare_reviewed_update(**plan):
        _record("prepare", plan)
        if os.environ.get("BRIDGE_PREPARE_RAISES"):
            raise RuntimeError(os.environ["BRIDGE_PREPARE_RAISES"])
        return json.loads(os.environ.get("BRIDGE_PREPARED", '{"state": "prepared", "requires_image": true}'))
  """,
  "deployment_control.py": """
    from app.platform_update import _record
    class DeploymentControlError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.message = message
    async def request_reviewed_rebuild(*, db, **plan):
        _record("request", plan)
        return {"state": "queued", "request_nonce": "n" * 32}
  """,
  "auth.py": """
    from app.platform_update import _record
    def write_service_token(username, token_epoch):
        _record("token", [username, token_epoch])
  """,
  "models.py": """
    class Owner:
        username = "owner"
        token_epoch = 7
  """,
  "database.py": """
    from app import models
    class _Query:
        def first(self):
            return models.Owner()
    class _Session:
        def query(self, _model):
            return _Query()
        def close(self):
            pass
    def SessionLocal():
        return _Session()
  """,
}


@pytest.fixture
def old_release(tmp_path):
  backend = tmp_path / "backend"
  (backend / "app").mkdir(parents=True)
  for name, source in STAND_IN.items():
    (backend / "app" / name).write_text(textwrap.dedent(source))
  repo = tmp_path / "platform"
  repo.mkdir()
  marker = repo / "deployment" / "self-hosted-helper.required"
  marker.parent.mkdir()
  marker.write_text("2\n")
  git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
  subprocess.run([*git, "init", "-q"], check=True)
  subprocess.run([*git, "add", "-A"], check=True)
  subprocess.run([*git, "commit", "-qm", "release"], check=True)
  target = subprocess.run([*git, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
  return backend, repo, target, tmp_path / "calls.json"


def _preview(target, actions, reasons, **extra):
  return {"available": True, "plan_id": "p", "current_sha": "c" * 40,
          "target_sha": target, "image_digest": None, "conflict_paths": [],
          "activation": {"required_actions": actions, "reasons": reasons}, **extra}


HELPER = {"code": "host_helper_migration", "paths": ["deployment/self-hosted-helper.required"]}
IMAGE = {"code": "image_inputs", "paths": ["Dockerfile"]}


def _run(old_release, preview, *, protocol="2", prepared=None, **extra_env):
  backend, repo, target, calls = old_release
  env = {"BRIDGE_REPO": str(repo), "BRIDGE_CALLS": str(calls),
         "BRIDGE_PREVIEW": json.dumps(preview), "PATH": "/usr/bin:/bin", **extra_env}
  if prepared is not None:
    env["BRIDGE_PREPARED"] = json.dumps(prepared)
  result = subprocess.run(
    [sys.executable, "-I", "-", target, protocol], input=BRIDGE.read_text(),
    cwd=backend, env=env, capture_output=True, text=True,
  )
  output = json.loads(result.stdout.strip().splitlines()[-1])
  recorded = json.loads(calls.read_text()) if calls.exists() else []
  return result.returncode, output, [name for name, _ in recorded], recorded


def test_queues_the_waiting_update_through_the_old_release_updater(old_release):
  target = old_release[2]
  code, out, names, recorded = _run(old_release, _preview(
    target, ["host_maintenance", "image_rebuild"], [HELPER, IMAGE]))
  assert code == 0 and out == {"state": "queued", "target_sha": target,
                               "request_nonce": "n" * 32}
  assert names == ["preview", "prepare", "token", "request"]
  plan = {"plan_id": "p", "current_sha": "c" * 40, "target_sha": target, "image_digest": None}
  assert recorded[1][1] == plan and recorded[3][1] == plan
  assert recorded[2][1] == ["owner", 7]


@pytest.mark.parametrize("preview_change", [
  {"available": False},
  {"activation": {"required_actions": ["image_rebuild"], "reasons": [IMAGE]}},
])
def test_nothing_waiting_for_the_helper_changes_nothing(old_release, preview_change):
  preview = {**_preview(old_release[2], ["host_maintenance", "image_rebuild"], [HELPER, IMAGE]),
             **preview_change}
  code, out, names, _ = _run(old_release, preview)
  assert code == 0 and out["state"] == "none"
  assert names == ["preview"]


@pytest.mark.parametrize("actions,reasons,extra", [
  (["container_recreate", "host_maintenance", "image_rebuild"],
   [HELPER, IMAGE, {"code": "self_hosted_topology", "paths": ["docker-compose.yml"]}], {}),
  (["host_maintenance", "image_rebuild"],
   [HELPER, {"code": "other_host_work", "paths": ["deployment/other-host-work"]}], {}),
  (["host_maintenance", "image_rebuild"], [HELPER, IMAGE], {"conflict_paths": ["backend/app/x.py"]}),
])
def test_refuses_without_preparing_when_more_than_the_helper_is_owed(
    old_release, actions, reasons, extra):
  code, out, names, _ = _run(old_release, _preview(old_release[2], actions, reasons, **extra))
  assert code == 1 and out["state"] == "refused"
  assert names == ["preview"]


def test_refuses_a_release_needing_a_newer_helper_than_installed(old_release):
  code, out, names, _ = _run(old_release, _preview(
    old_release[2], ["host_maintenance", "image_rebuild"], [HELPER, IMAGE]), protocol="1")
  assert code == 1 and "protocol 2" in out["message"]
  assert names == ["preview"]


def test_a_conflicting_prepare_is_left_for_settings_and_never_queued(old_release):
  code, out, names, _ = _run(old_release, _preview(
    old_release[2], ["host_maintenance", "image_rebuild"], [HELPER, IMAGE]),
    prepared={"state": "conflict"})
  assert code == 1 and out["state"] == "refused"
  assert names == ["preview", "prepare"]


def test_installer_finishes_the_update_only_after_the_active_worker_verifies():
  installer = INSTALLER.read_text()
  assert installer.index("verify-active") < installer.index("finish-helper-update.py\")")
  assert installer.index("systemctl enable --now mobius-rebuild.path") \
    < installer.index("finish-helper-update.py\")")
  assert "--no-update) FINISH_UPDATE=0" in installer
  # The bridge runs as the app user inside the app, never as host root.
  assert 'docker exec -i -u mobius -w /data/platform/backend "$CID"' in installer


def test_refuses_package_changes_an_old_image_cannot_hand_over(old_release):
  deps = {"code": "python_dependencies", "paths": ["Dockerfile"]}
  code, out, names, _ = _run(old_release, _preview(
    old_release[2], ["host_maintenance", "image_rebuild"], [HELPER, deps]),
    BRIDGE_IMAGE_ACTIVATES="0")
  assert code == 1 and "Python packages" in out["message"]
  assert names == ["preview"]


def test_a_failing_prepare_is_a_refusal_not_a_traceback(old_release):
  code, out, names, _ = _run(old_release, _preview(
    old_release[2], ["host_maintenance", "image_rebuild"], [HELPER, IMAGE]),
    BRIDGE_PREPARE_RAISES="finish_update_first")
  assert code == 1 and out["state"] == "refused" and "finish_update_first" in out["message"]
  assert names == ["preview", "prepare"]
