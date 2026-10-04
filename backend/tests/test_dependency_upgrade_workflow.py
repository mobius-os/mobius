from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow():
  return yaml.load(
    (ROOT / ".github/workflows/test.yml").read_text(),
    Loader=yaml.BaseLoader,
  )


def test_dependency_replacement_runs_only_when_manually_requested_on_a_hosted_runner():
  workflow = _workflow()
  option = workflow["on"]["workflow_dispatch"]["inputs"]["dependency_replay"]
  assert option == {
    "description": "Test actual Python and locked-package replacement on a disposable runner (up to 90 minutes)",
    "type": "boolean", "default": "false",
  }
  assert workflow["permissions"] == {"contents": "read"}
  job = workflow["jobs"]["dependency-replay"]
  assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.dependency_replay"
  assert job["runs-on"] == "ubuntu-24.04"
  assert job["timeout-minutes"] == "90"
  assert "secrets." not in str(job)


def test_dependency_fixture_uses_an_exact_public_base_without_retaining_checkout_credentials():
  workflow = _workflow()
  base = workflow["on"]["workflow_dispatch"]["inputs"]["dependency_base"]
  assert base["type"] == "string"
  assert base["default"] == "3c8401779f7635783cbe6e5e54f4c54f585311a6"
  job = workflow["jobs"]["dependency-replay"]
  checkout = job["steps"][0]
  assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
  assert checkout["with"] == {"persist-credentials": "false", "fetch-depth": "0"}
  replay = job["steps"][-1]
  assert replay["env"] == {"BASE_SHA": "${{ inputs.dependency_base }}"}
  assert replay["run"] == 'scripts/test-dependency-upgrade-path.sh "$BASE_SHA" .'


def test_dependency_replay_does_not_change_the_normal_pr_and_merge_queue_triggers():
  triggers = _workflow()["on"]
  assert triggers["pull_request"]["branches"] == ["main", "stack/**"]
  assert "merge_group" in triggers
