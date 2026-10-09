"""Exercise the Docker fixture's source-install/manual-host-handoff boundary."""

import json
import os
from pathlib import Path
import subprocess

import pytest
from app import platform_activation


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test-upgrade-path.sh"
SOURCE = SCRIPT.read_text()
HELPERS = SOURCE[SOURCE.index("body() {"):SOURCE.index('previous=$(image_sha')]
FAIL = SOURCE[SOURCE.index("fail() {"):SOURCE.index("as_mobius() {")]
# Exercise the real failure reporter without accessing Docker or a live instance.
SHELL_HELPERS = HELPERS + FAIL + '\nname=fixture\ndocker() { echo "fixture docker: $*" >&2; }\n'
APPLY_REPLY = '  reply=$(api POST /api/platform/apply "$plan")\n'
APPLY_START = SOURCE.index(APPLY_REPLY) + len(APPLY_REPLY)
APPLY_RESPONSE = SOURCE[APPLY_START:SOURCE.index('  echo "5. the owner restarts', APPLY_START)]
HANDOFF = {
  "state": "activation_needed",
  "activation": {
    "required_actions": ["host_maintenance"],
    "reasons": [{
      "code": "host_helper_migration",
      "paths": ["deployment/self-hosted-helper.required"],
    }],
  },
}


def _shell(fragment, payload, *, expected=True, status=200, response_body=None):
  env = {k: v for k, v in os.environ.items() if k not in {"BASH_ENV", "ENV"}}
  if response_body is None:
    response_body = json.dumps(payload)
  return subprocess.run(
    ["bash", "-eu", "-c", SHELL_HELPERS
     + 'reply="$1"; needs_helper="$2"\n' + fragment,
     "fixture", response_body + f"\n{status}", str(expected).lower()],
    env=env, text=True, capture_output=True, timeout=5,
  )


def _apply(payload, **kwargs):
  return _shell(APPLY_RESPONSE, payload, **kwargs)


def _preview_apply(preview, applied):
  """Run the fixture's actual preview classification and source Apply branch."""
  start = SOURCE.index('needs_image=$(field "$preview"')
  end = SOURCE.index('if [ "$local_edits" = true ]', start)
  fragment = (
    'preview="$1"; reply="$2"; local_edits=false\n'
    + SOURCE[start:end]
    + 'reply="$3"\n'
    + APPLY_RESPONSE
  )
  env = {k: v for k, v in os.environ.items() if k not in {"BASH_ENV", "ENV"}}
  return subprocess.run(
    ["bash", "-eu", "-c", SHELL_HELPERS
     + fragment, "fixture", json.dumps(preview), "", json.dumps(applied) + "\n200"],
    env=env, text=True, capture_output=True, timeout=5,
  )


@pytest.mark.parametrize("actions", [
  ["host_maintenance"],
  ["server_restart", "host_maintenance"],
])
def test_reviewed_preview_routes_supported_helper_migration_to_handoff(actions):
  preview = {"activation": {**HANDOFF["activation"], "required_actions": actions}}
  applied = {**HANDOFF, "activation": preview["activation"]}
  assert _preview_apply(preview, applied).returncode == 0
  assert _preview_apply(preview, {**HANDOFF, "state": "restart_needed"}).returncode != 0


def test_production_classifier_mixed_preview_and_apply_take_source_handoff_branch():
  activation = platform_activation.classify_activation(
    ["backend/app/main.py", "deployment/self-hosted-helper.required"],
    deployment="self_hosted",
  )
  assert activation["required_actions"] == ["server_restart", "host_maintenance"]
  assert _preview_apply({"activation": activation},
                        {"state": "activation_needed", "activation": activation}).returncode == 0


@pytest.mark.parametrize("actions", [
  ["proxy_reload", "host_maintenance"],
  ["container_recreate", "host_maintenance"],
  ["image_rebuild", "host_maintenance"],
  ["server_restart", "proxy_reload", "host_maintenance"],
])
def test_preview_rejects_external_work_outside_supported_helper_handoff(actions):
  preview = {"activation": {**HANDOFF["activation"], "required_actions": actions}}
  assert _preview_apply(preview, HANDOFF).returncode != 0


def test_preview_requires_exact_helper_marker_not_merely_host_maintenance():
  preview = {"activation": {"required_actions": ["server_restart", "host_maintenance"],
                            "reasons": [{"code": "host_helper_migration", "paths": ["docker-compose.yml"]}]}}
  assert _preview_apply(preview, HANDOFF).returncode != 0


def test_ordinary_restart_preview_still_takes_ordinary_source_path():
  preview = {"activation": {"required_actions": ["server_restart"], "reasons": []}}
  assert _preview_apply(preview, {"state": "restart_needed"}).returncode == 0
  assert _preview_apply(preview, HANDOFF).returncode != 0


@pytest.mark.parametrize("extra_reason", [
  {"code": "proxy_reload", "paths": ["proxy.conf"]},
  {"code": "self_hosted_topology_migration", "paths": ["deployment/self-hosted-topology.required"]},
])
def test_residual_must_contain_only_the_helper_reason(extra_reason):
  extra = {**HANDOFF, "activation": {**HANDOFF["activation"], "reasons": [
    *HANDOFF["activation"]["reasons"], extra_reason,
  ]}}
  start = SOURCE.rindex('if [ "$needs_helper" = true ]; then')
  end = SOURCE.index('echo "upgrade path: ${previous', start)
  assert _shell('api() { printf "%s\\n" "$reply"; }\n' + SOURCE[start:end], extra).returncode != 0


def test_reviewed_helper_migration_installs_source_with_an_explicit_handoff():
  assert _apply(HANDOFF).returncode == 0


@pytest.mark.parametrize("state", ["updated", "up_to_date", "restart_needed", "rolled_back", "conflict"])
def test_expected_migration_cannot_silently_lose_its_handoff(state):
  assert _apply({**HANDOFF, "state": state}).returncode != 0


@pytest.mark.parametrize("activation", [
  {},
  {"required_actions": []},
  {"required_actions": ["host_maintenance"], "reasons": []},
  {**HANDOFF["activation"], "required_actions": ["host_maintenance", "proxy_reload"]},
  {**HANDOFF["activation"], "reasons": [{"code": "host_helper_migration", "paths": ["docker-compose.yml"]}]},
])
def test_unrelated_or_unproved_external_work_is_not_accepted(activation):
  assert _apply({**HANDOFF, "activation": activation}).returncode != 0


def test_unreviewed_helper_work_still_fails():
  assert _apply(HANDOFF, expected=False).returncode != 0


@pytest.mark.parametrize("expected", [False, True])
@pytest.mark.parametrize("status", [409, 500])
def test_apply_http_refusal_reports_status_body_and_container_logs(status, expected):
  payload = {"detail": "apply refusal diagnostic"}
  result = _apply(payload, status=status, expected=expected)
  assert result.returncode == 1
  assert (f"upgrade path: the previous release refused to apply the candidate ({status}): "
          f"{json.dumps(payload)}") in result.stderr
  assert "fixture docker: logs fixture --tail 80" in result.stderr


@pytest.mark.parametrize("expected", [False, True])
@pytest.mark.parametrize("response_body", ["<html>invalid apply response</html>", ""])
def test_apply_malformed_success_reports_body_and_container_logs(response_body, expected):
  result = _apply(None, response_body=response_body, expected=expected)
  assert result.returncode == 1
  assert ("upgrade path: the previous release returned an invalid apply response (200): "
          f"{response_body}") in result.stderr
  assert "fixture docker: logs fixture --tail 80" in result.stderr


@pytest.mark.parametrize("state", ["updated", "up_to_date", "restart_needed"])
def test_ordinary_source_install_outcomes_remain_accepted(state):
  assert _apply({"state": state}, expected=False).returncode == 0


@pytest.mark.parametrize("retained", [False, True])
def test_healthy_restart_does_not_discharge_helper_maintenance(retained):
  start = SOURCE.rindex('if [ "$needs_helper" = true ]; then')
  end = SOURCE.index('echo "upgrade path: ${previous', start)
  payload = HANDOFF if retained else {"state": "up_to_date", "activation": {"required_actions": []}}
  result = _shell('api() { printf "%s\\n" "$reply"; }\n' + SOURCE[start:end], payload)
  assert (result.returncode == 0) is retained


def test_restart_must_discharge_the_routine_server_restart_requirement():
  activation = platform_activation.classify_activation(
    ["backend/app/main.py", "deployment/self-hosted-helper.required"],
    deployment="self_hosted",
  )
  start = SOURCE.rindex('if [ "$needs_helper" = true ]; then')
  end = SOURCE.index('echo "upgrade path: ${previous', start)
  result = _shell('api() { printf "%s\\n" "$reply"; }\n' + SOURCE[start:end],
                  {"state": "activation_needed", "activation": activation})
  assert result.returncode != 0
