"""Hermetic contracts for the disposable real-host mixed upgrade proof.

scripts/test-mixed-host-upgrade.sh needs a disposable systemd host with
Docker. These tests run its decision helpers, refusal snapshot, fresh-host
preflight and local ingress checks against shell stubs in a minimal
environment, never a real host. Passing them is preparation, not evidence that
the real upgrade passed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

from app import platform_activation

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/test-mixed-host-upgrade.sh"
SOURCE = SCRIPT.read_text()
UPGRADE_PATH = (ROOT / "scripts/test-upgrade-path.sh").read_text()
UPGRADE_HELPERS = UPGRADE_PATH[UPGRADE_PATH.index("body() {"):UPGRADE_PATH.index("previous=$(image_sha")]
PREVIOUS, TARGET = "f6e61ae4240c10daf7ece40a74bb14eca4d43f87", "6bac85f6834ad0507eae8dc12b2dcbdbcbe65193"
HELPER_REASON = {"code": "host_helper_migration", "paths": ["deployment/self-hosted-helper.required"]}
TOPOLOGY_REASON = {"code": "self_hosted_topology_migration", "paths": ["deployment/self-hosted-topology.required"]}
# Representative paths of the pinned span: server code, a baked bootstrap
# script, the helper marker and host tooling.
MIXED_SPAN = platform_activation.classify_activation([
  "backend/app/main.py", "backend/scripts/init_data_repo.py",
  "deployment/self-hosted-helper.required", "scripts/install-rebuild-helper.sh",
], deployment="self_hosted")
CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _host_proof_workflow():
  # BaseLoader preserves GitHub's `on` key instead of treating it as YAML 1.1 true.
  return yaml.load((ROOT / ".github/workflows/test.yml").read_text(), Loader=yaml.BaseLoader)


def test_host_proof_is_default_off_and_only_runs_on_manual_disposable_runners():
  workflow = _host_proof_workflow()
  option = workflow["on"]["workflow_dispatch"]["inputs"]["mixed_upgrade_proof"]
  assert option["type"] == "boolean" and option["default"] == "false"
  job = workflow["jobs"]["mixed-upgrade-proof"]
  assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.mixed_upgrade_proof"
  assert job["runs-on"] == "ubuntu-24.04"
  assert job["timeout-minutes"] == "120"
  assert workflow["permissions"] == job["permissions"] == {"contents": "read"}


def test_host_proof_checks_out_full_history_without_retained_credentials():
  job = _host_proof_workflow()["jobs"]["mixed-upgrade-proof"]
  checkout = job["steps"][0]
  assert re.fullmatch(r"actions/checkout@[0-9a-fA-F]{40}", checkout["uses"])
  assert checkout["with"] == {"persist-credentials": "false", "fetch-depth": "0"}
  assert "secrets." not in json.dumps(job)


def test_host_proof_contract_accepts_a_checkout_pin_bump(monkeypatch):
  workflow = _host_proof_workflow()
  checkout = workflow["jobs"]["mixed-upgrade-proof"]["steps"][0]
  checkout["uses"] = "actions/checkout@" + "b" * 40
  monkeypatch.setitem(globals(), "_host_proof_workflow", lambda: workflow)
  test_host_proof_checks_out_full_history_without_retained_credentials()


@pytest.mark.parametrize("ref", ["v7", "a" * 39, "a" * 41, "g" * 40])
def test_host_proof_contract_rejects_a_non_commit_checkout_ref(monkeypatch, ref):
  workflow = _host_proof_workflow()
  workflow["jobs"]["mixed-upgrade-proof"]["steps"][0]["uses"] = "actions/checkout@" + ref
  monkeypatch.setitem(globals(), "_host_proof_workflow", lambda: workflow)
  with pytest.raises(AssertionError):
    test_host_proof_checks_out_full_history_without_retained_credentials()


def test_host_proof_uses_root_owned_source_and_the_reviewed_historical_pair():
  job = _host_proof_workflow()["jobs"]["mixed-upgrade-proof"]
  step = job["steps"][-1]
  assert step["env"] == {"PREVIOUS": PREVIOUS, "TARGET": TARGET}
  commands = [line.strip() for line in step["run"].splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
  assert commands == [
    'sudo cp -a "$GITHUB_WORKSPACE" /root/mixed-upgrade-proof',
    'sudo chown -R root:root /root/mixed-upgrade-proof',
    'sudo chmod -R go-w /root/mixed-upgrade-proof',
    'sudo /root/mixed-upgrade-proof/scripts/test-mixed-host-upgrade.sh "$PREVIOUS" "$TARGET"',
  ]
  assert "safe.directory" not in json.dumps(job)


@pytest.mark.parametrize("default_branch", ["master", "trunk"])
def test_operator_clone_checks_out_previous_main_and_fast_forwards_to_target(tmp_path, default_branch):
  home = tmp_path / "home"
  home.mkdir()
  (home / ".gitconfig").write_text(f"[init]\n\tdefaultBranch = {default_branch}\n")
  env = {"PATH": CLEAN_PATH, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
  source = tmp_path / "source"
  source.mkdir()

  def git(*args):
    return subprocess.run(
      ["git", "-C", str(source), *args], env=env, capture_output=True,
      text=True, check=True, timeout=10,
    ).stdout.strip()

  git("init", "-q", "--initial-branch=main")
  (source / ".gitignore").write_text(".env\n")
  (source / "release").write_text("previous\n")
  git("add", ".")
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "previous")
  previous = git("rev-parse", "HEAD")
  (source / "release").write_text("target\n")
  git("add", ".")
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "target")
  target = git("rev-parse", "HEAD")
  variables = {"ROOT": str(source), "RELEASES": str(tmp_path / "releases.git"),
               "CHECKOUT": str(tmp_path / "operator"), "PREVIOUS": previous, "TARGET": target}
  program = "\n".join(f"{key}={shlex.quote(value)}" for key, value in variables.items())
  program += '\nfail() { echo "$*" >&2; exit 71; }\n'
  program += SOURCE[SOURCE.index("checkout_clean() {"):SOURCE.index('fresh_host_preflight "$ROOT"')]
  program += SOURCE[SOURCE.index("git init -q --bare"):SOURCE.index('echo "1. the previous release')]
  program += '\n[[ $(git -C "$CHECKOUT" symbolic-ref --short HEAD) == main ]]\n'
  program += '[[ $(git -C "$CHECKOUT" rev-parse HEAD) == "$PREVIOUS" ]]\n'
  program += '[[ $(cat "$CHECKOUT/release") == previous ]]\n'
  program += 'git -C "$ROOT" push -q "$RELEASES" "$TARGET:refs/heads/main"\n'
  program += 'git -C "$CHECKOUT" pull -q --ff-only\n'
  program += '[[ $(git -C "$CHECKOUT" rev-parse HEAD) == "$TARGET" ]]\n'
  program += '[[ $(cat "$CHECKOUT/release") == target ]]\ncheckout_clean\n'
  result = _bash(program, env=env)
  assert result.returncode == 0, (result.stdout, result.stderr)
  assert "remote HEAD refers to nonexistent ref" not in result.stderr


def _region(name: str) -> str:
  return SOURCE[SOURCE.index(f"# BEGIN {name}\n"):SOURCE.index(f"# END {name}\n")]


PREDICATES = _region("MIXED PREDICATES")


def _bash(program: str, *args: str, env: dict | None = None, cwd: Path | None = None):
  return subprocess.run(
    ["/bin/bash", "-c", "set -euo pipefail\nshopt -s inherit_errexit\nDOMAIN=localhost\n" + program, "fixture", *args],
    env=env or {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}, cwd=cwd,
    capture_output=True, text=True, timeout=20,
  )


def _predicate(name: str, *args: str) -> bool:
  return _bash(PREDICATES + f'\n{name} "$@"', *args).returncode == 0


def _fixture(name: str, *args: str) -> bool:
  return _bash(UPGRADE_HELPERS + f'\n{name} "$@"', *args).returncode == 0


def _preview(actions=None, reasons=None, current=PREVIOUS, target=TARGET) -> str:
  activation = dict(MIXED_SPAN) if actions is None else {"required_actions": actions, "reasons": reasons}
  return json.dumps({"current_sha": current, "target_sha": target, "activation": activation})


# --- inputs ----------------------------------------------------------------

@pytest.mark.parametrize("previous,target", [
  ("main", "b" * 40), ("a" * 40, "b" * 40 + '"'), ("a" * 40, "a" * 40),
])
def test_invalid_release_inputs_stop_before_any_host_command(tmp_path, previous, target):
  # No tools on PATH: an accidental earlier host command would be observable.
  result = subprocess.run(["/bin/bash", str(SCRIPT), previous, target],
                          env={"PATH": str(tmp_path)}, capture_output=True, text=True)
  assert result.returncode == 2
  assert result.stderr.strip() == "two distinct full release SHAs are required"


def test_valid_inputs_without_root_stop_before_any_host_command(tmp_path):
  result = subprocess.run(["/bin/bash", str(SCRIPT), PREVIOUS, TARGET],
                          env={"PATH": str(tmp_path)}, capture_output=True, text=True)
  assert result.returncode == 1
  assert result.stderr.strip() == "run with sudo"


# --- the admitted span -----------------------------------------------------

def test_the_pinned_span_is_admitted_and_the_settings_fixture_still_refuses_it():
  assert MIXED_SPAN["required_actions"] == ["server_restart", "image_rebuild", "host_maintenance"]
  assert _predicate("mixed_review", _preview(), PREVIOUS, TARGET)
  # scripts/test-upgrade-path.sh keeps failing this span on purpose: Settings
  # cannot finish it, and this separate proof must not weaken that refusal.
  assert not _fixture("reviewed_helper_preview", _preview())


@pytest.mark.parametrize("preview", [
  _preview(current="0" * 40),
  _preview(target="1" * 40),
  _preview(["host_maintenance"], [HELPER_REASON]),
  _preview(["server_restart", "host_maintenance"], [HELPER_REASON]),
  _preview(["image_rebuild"], [{"code": "baked_runtime", "paths": ["Dockerfile"]}]),
  _preview(["image_rebuild", "host_maintenance"], [{"code": "host_helper_migration", "paths": ["docker-compose.yml"]}]),
  _preview(["image_rebuild", "host_maintenance", "host_maintenance"], [HELPER_REASON]),
  _preview(["image_rebuild", "host_maintenance", "container_recreate"], [HELPER_REASON, TOPOLOGY_REASON]),
  _preview(["server_restart", "image_rebuild", "host_maintenance", "container_recreate"], [HELPER_REASON]),
  _preview(["image_rebuild", "host_maintenance", "proxy_reload"],
           [HELPER_REASON, {"code": "self_hosted_proxy", "paths": ["Caddyfile"]}]),
  _preview(["image_rebuild", "host_maintenance"], [HELPER_REASON, TOPOLOGY_REASON]),
  _preview(["image_rebuild", "host_maintenance"],
           [HELPER_REASON, {"code": "python_dependencies", "paths": ["backend/requirements.lock"]}]),
  _preview(["image_rebuild", "host_maintenance"], [HELPER_REASON, {"code": "a_future_external_rule", "paths": ["x"]}]),
  _preview(["image_rebuild", "host_maintenance"], [HELPER_REASON, HELPER_REASON]),
  _preview([], []),
  "not json",
  "{}",
])
def test_only_the_exact_pinned_image_plus_helper_span_is_admitted(preview):
  assert not _predicate("mixed_review", preview, PREVIOUS, TARGET)


def test_admitted_reason_codes_are_exactly_routine_work_plus_the_helper_migration():
  allowed = set(PREDICATES.split('MIXED_REASON_CODES="', 1)[1].split('"', 1)[0].split())
  routine = {"live", "server_restart", "image_rebuild"}
  expected = {rule.code for rule in platform_activation._RULES if rule.level.value in routine}
  expected = (expected - {"python_dependencies"}) | {"host_helper_migration", "live_source"}
  assert allowed == expected


# --- refusals, residual and deploy outcome ---------------------------------

@pytest.mark.parametrize("status,payload,expected", [
  (409, {"detail": {"code": "external_activation_required"}}, True),
  (409, {"detail": {"code": "update_plan_stale"}}, False),
  (202, {"state": "queued"}, False),
  (200, {"detail": {"code": "external_activation_required"}}, False),
  (500, {"detail": {"code": "external_activation_required"}}, False),
  (409, {"detail": "external_activation_required"}, False),
])
def test_refusal_must_be_the_exact_settings_refusal(status, payload, expected):
  reply = json.dumps(payload) + f"\n{status}"
  assert _predicate("refused_with", reply, "external_activation_required") is expected


def test_refusal_rejects_an_unparseable_body():
  assert not _predicate("refused_with", "<html>\n409", "external_activation_required")


RESIDUALS = [
  ({"activation": {"required_actions": ["host_maintenance"], "reasons": [HELPER_REASON]}}, True),
  ({"activation": {"required_actions": [], "reasons": []}}, False),
  ({"activation": {"required_actions": ["image_rebuild", "host_maintenance"],
                   "reasons": [{"code": "baked_runtime", "paths": ["Dockerfile"]}, HELPER_REASON]}}, False),
  ({"activation": {"required_actions": ["server_restart", "host_maintenance"],
                   "reasons": [{"code": "server_runtime", "paths": ["backend/app"]}, HELPER_REASON]}}, False),
  ({"activation": {"required_actions": ["host_maintenance"],
                   "reasons": [HELPER_REASON, {"code": "proxy_reload", "paths": ["Caddyfile"]}]}}, False),
  ({"activation": {"required_actions": ["host_maintenance"],
                   "reasons": [{"code": "host_helper_migration", "paths": ["docker-compose.yml"]}]}}, False),
  ({}, False),
]


@pytest.mark.parametrize("status,expected", RESIDUALS)
def test_after_the_deploy_only_the_exact_helper_handoff_remains(status, expected):
  assert _predicate("helper_handoff_only", json.dumps(status)) is expected


@pytest.mark.parametrize("status,_expected", RESIDUALS)
def test_residual_matches_the_settings_fixture_handoff_contract(status, _expected):
  # One contract, two harnesses: they must never drift apart.
  assert _predicate("helper_handoff_only", json.dumps(status)) is _fixture(
    "helper_maintenance_pending", json.dumps(status))


def test_public_probe_host_is_read_from_the_real_deploy_script():
  result = _bash(PREDICATES + '\ndeploy_public_host "$1"', str(ROOT / "scripts/deploy-prod.sh"))
  host = result.stdout.strip()
  assert result.returncode == 0 and host and "." in host and host != "localhost"


DEPLOY_OK = """\
  controller verified the exact admitted container 0123456789ab
  backend sha: 6bac85f6834ad0507e… (matches the commit just built)
  protected runtime: current
  served platform contains reviewed source 6bac85f6834ad0507e… plus local changes
  internal /api/health: 200
  internal /api/ready:  200
"""
PUBLIC_FAILED = (
  "  public  /api/health: 000\n000  (https://example.invalid/api/health)\n"
  "  internal is healthy but public reverse-proxy returned non-200; check Caddy.\n"
)
SHELL_RELOAD = "\n[12:00:00] [4b/4] notify open shells to reload\n"


@pytest.mark.parametrize("status,log,host,expected", [
  # The fixed public host resolves to loopback: only that probe may fail, last.
  (1, DEPLOY_OK + PUBLIC_FAILED, "example.invalid", True),
  # Reaching a real public host is never success on a disposable runner.
  (0, DEPLOY_OK + "  public  /api/health: 200  (https://example.invalid/api/health)\n"
   + SHELL_RELOAD + "deploy complete\n", "example.invalid", False),
  (1, DEPLOY_OK.replace("  protected runtime: current\n", "") + PUBLIC_FAILED, "example.invalid", False),
  (1, "  installed controller could not settle the manual replacement\n", "example.invalid", False),
  (1, DEPLOY_OK + PUBLIC_FAILED + "  something failed afterwards\n", "example.invalid", False),
  (1, DEPLOY_OK + SHELL_RELOAD + PUBLIC_FAILED, "example.invalid", False),
  (1, DEPLOY_OK + "deploy complete\n" + PUBLIC_FAILED, "example.invalid", False),
  (2, DEPLOY_OK + PUBLIC_FAILED, "example.invalid", False),
  # A deploy whose public probe is this host itself must finish completely.
  (0, DEPLOY_OK + SHELL_RELOAD + "deploy complete\n", "localhost", True),
  (0, DEPLOY_OK, "localhost", False),
  (1, DEPLOY_OK, "localhost", False),
])
def test_deploy_counts_only_as_a_partial_cutover_after_every_internal_check(tmp_path, status, log, host, expected):
  path = tmp_path / "deploy.log"
  path.write_text(log)
  assert _predicate("deploy_cutover_observed", str(status), str(path), host) is expected


# --- refusal snapshot fails closed ----------------------------------------

SNAPSHOT_OBSERVERS = ["app_container", "docker_inspect", "docker_exec", "python3", "sha256sum", "find", "docker_image"]


def _snapshot_program(tmp_path: Path) -> str:
  root = tmp_path / "host"
  (root / "var-lib").mkdir(parents=True)
  (root / "etc-rebuild").mkdir()
  (root / "systemd").mkdir()
  (root / "var-lib/workers.json").write_text('{"active": {}}')
  (root / "etc-rebuild/config.json").write_text("{}")
  (root / "systemd/mobius-rebuild.path").write_text("[Path]\n")
  function = (_region("REFUSAL SNAPSHOT")
              .replace("/var/lib/mobius-rebuild", str(root / "var-lib"))
              .replace("/usr/local/libexec", str(root / "libexec"))
              .replace("/etc/mobius-rebuild", str(root / "etc-rebuild"))
              .replace("/etc/systemd/system", str(root / "systemd")))
  assert "/etc/" not in function and "/var/lib" not in function and "/usr/local" not in function
  stubs = '''
STATUS=status.json
fails() { [[ " ${FAIL:-} " == *" $1 "* ]]; }
app_container() { fails app_container && return 1; printf '%064d\\n' 1; }
docker() {
  case "$1" in
    inspect) fails docker_inspect && return 7; echo "cid image ${STARTED:-t0} 0" ;;
    exec) fails docker_exec && return 7; printf 'head\\nrefs\\nprepared: absent\\n' ;;
    image) fails docker_image && return 7; printf '%s' "${IMAGE_IDS:-}" ;;
    *) echo "MUTATION docker $*" >&2; return 0 ;;
  esac
}
python3() { fails python3 && return 8; echo '{"state": "idle"}'; }
sha256sum() {
  fails sha256sum && return 9
  [[ -n ${FAIL_HASH:-} && $* == *"$FAIL_HASH"* ]] && return 9
  command sha256sum "$@"
}
find() { fails find && return 10; command find "$@"; }
'''
  return stubs + function


def test_snapshot_reports_each_fact_when_every_observation_succeeds(tmp_path):
  program = _snapshot_program(tmp_path) + '\nbefore=$(untouched_state) || exit 3\nprintf "%s" "$before"'
  result = _bash(program)
  assert result.returncode == 0, result.stderr
  assert "transaction.json: absent" in result.stdout and "workers.json" in result.stdout
  assert "mobius-rebuild.path" in result.stdout and "config.json" in result.stdout
  assert "MUTATION" not in result.stderr


@pytest.mark.parametrize("observer", SNAPSHOT_OBSERVERS)
def test_snapshot_fails_when_any_observation_fails(tmp_path, observer):
  # The reviewed false pass: a failed observation inside the conditional
  # assignment must never yield a comparable snapshot.
  program = _snapshot_program(tmp_path) + '\nif before=$(untouched_state); then echo SNAPSHOT; fi'
  result = _bash(program, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "FAIL": observer})
  assert "SNAPSHOT" not in result.stdout


@pytest.mark.parametrize("unreadable", ["workers.json", "config.json", "mobius-rebuild.path"])
def test_snapshot_fails_when_one_root_file_cannot_be_hashed(tmp_path, unreadable):
  program = _snapshot_program(tmp_path) + '\nif before=$(untouched_state); then echo SNAPSHOT; fi'
  result = _bash(program, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
                               "FAIL_HASH": unreadable})
  assert "SNAPSHOT" not in result.stdout


def test_snapshot_fails_when_every_observation_fails(tmp_path):
  program = _snapshot_program(tmp_path) + '''
if before=$(untouched_state); then echo SNAPSHOT_SUCCEEDED; fi
if after=$(untouched_state) && [[ $after == "${before:-}" ]]; then echo UNCHANGED; fi'''
  result = _bash(program, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
                               "FAIL": " ".join(SNAPSHOT_OBSERVERS[1:])})
  assert "SNAPSHOT_SUCCEEDED" not in result.stdout and "UNCHANGED" not in result.stdout


@pytest.mark.parametrize("change", ["STARTED=t1", "IMAGE_IDS=sha256:abc", "journal", "adapter"])
def test_snapshot_detects_each_kind_of_change(tmp_path, change):
  program = _snapshot_program(tmp_path) + '\nuntouched_state'
  before = _bash(program)
  assert before.returncode == 0, before.stderr
  env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}
  if change == "journal":
    (tmp_path / "host/var-lib/transaction.json").write_text("{}")
  elif change == "adapter":
    (tmp_path / "host/libexec").mkdir()
    (tmp_path / "host/libexec/mobius-manual-cutover.py").write_text("# adapter\n")
  else:
    key, value = change.split("=")
    env[key] = value
  after = _bash(program, env=env)
  assert after.returncode == 0, after.stderr
  assert after.stdout != before.stdout


# --- fresh-host preflight refuses before any mutation ---------------------

VALID_HOST = {
  "stat_home": "0 700", "stat_root": "0 drwxr-xr-x", "uname": "x86_64", "systemd": "running",
  "context": "default", "endpoint": "unix:///var/run/docker.sock", "info": "linux x86_64",
  "containers": "registry  \n", "volumes": "unrelated\n", "networks": "bridge\nhost\nnone\n",
  "images": "", "ports": "", "free": str(40 * 1024 ** 3),
}


def _preflight(tmp_path: Path, extra_env=None, helper_path=None, fail=None, **host):
  state = {**VALID_HOST, **host}
  calls = tmp_path / "calls.log"
  config = "\n".join(f"HOST_{k}={shlex.quote(v)}" for k, v in state.items())
  helper_paths = f'HELPER_PATHS=("{helper_path}")' if helper_path else "HELPER_PATHS=()"
  stubs = f'''
{config}
log() {{ printf '%s\\n' "$*" >>"{calls}"; }}
fails() {{ [[ " {fail or ''} " == *" $1 "* ]]; }}
stat() {{ log stat "$@"; fails stat && return 1; [[ $3 == "$HOME" ]] && echo "$HOST_stat_home" || echo "$HOST_stat_root"; }}
uname() {{ log uname "$@"; echo "$HOST_uname"; }}
systemctl() {{
  log systemctl "$@"
  [[ $1 == is-system-running ]] || {{ log MUTATION systemctl "$@"; return 0; }}
  echo "$HOST_systemd"; [[ $HOST_systemd == running ]]
}}
ss() {{ log ss "$@"; fails ss && return 1; printf '%s' "$HOST_ports"; }}
df() {{ log df "$@"; printf 'Filesystem 1B-blocks Used Available Use%% Mounted\\n/dev/x 1 1 %s 1%% /\\n' "$HOST_free"; }}
docker() {{
  log docker "$@"
  fails "docker_$1" && return 1
  case "$1 ${{2:-}}" in
    "context show") echo "$HOST_context" ;;
    "context inspect") echo "$HOST_endpoint" ;;
    "info "*) [[ $* == *DockerRootDir* ]] && echo /var/lib/docker || echo "$HOST_info" ;;
    "compose version") echo "Docker Compose version v2" ;;
    "ps "*) printf '%s' "$HOST_containers" ;;
    "volume ls") printf '%s' "$HOST_volumes" ;;
    "network ls") printf '%s' "$HOST_networks" ;;
    "image ls") printf '%s' "$HOST_images" ;;
    *) log MUTATION docker "$@" ;;
  esac
}}
for tripwire in git mktemp rm chmod tee install cp mv mkdir curl getent; do
  eval "$tripwire() {{ log MUTATION $tripwire \\"\\$@\\"; }}"
done
'''
  region = _region("FRESH HOST PREFLIGHT")
  region = region.replace(region[region.index("HELPER_PATHS=("):region.index("\nrefuse()")], helper_paths)
  home = "/tmp/mobius-mixed-home.fixture"
  env = {"PATH": CLEAN_PATH, "HOME": home, "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
         "MOBIUS_MIXED_PROOF_HOME": home, **(extra_env or {})}
  result = _bash(stubs + region + '\nfresh_host_preflight /root/checkout && echo PREFLIGHT_OK', env=env)
  log = calls.read_text() if calls.exists() else ""
  assert "MUTATION" not in log, log
  return result, log


def test_preflight_accepts_a_fresh_local_disposable_host_by_observation_only(tmp_path):
  result, log = _preflight(tmp_path)
  assert result.returncode == 0 and "PREFLIGHT_OK" in result.stdout, result.stderr
  for observed in ("docker context show", "docker volume ls", "docker network ls", "docker image ls", "ss -Hltnu"):
    assert observed in log


@pytest.mark.parametrize("case,message", [
  ({"extra_env": {"HTTPS_PROXY": "http://proxy.invalid:3128"}}, "unexpected environment variable HTTPS_PROXY"),
  ({"extra_env": {"ALLOW_STALE": "1"}}, "unexpected environment variable ALLOW_STALE"),
  ({"extra_env": {"DOCKER_HOST": "tcp://10.0.0.1:2375"}}, "unexpected environment variable DOCKER_HOST"),
  ({"extra_env": {"MOBIUS_IMAGE": "attacker"}}, "unexpected environment variable MOBIUS_IMAGE"),
  ({"extra_env": {"PATH": "/tmp/evil:" + CLEAN_PATH}}, "the environment was not rebuilt"),
  ({"extra_env": {"HOME": "/root"}}, "the environment was not rebuilt"),
  ({"stat_home": "0 755"}, "the isolated HOME is not root-private"),
  ({"stat_root": "1000 drwxr-xr-x"}, "must be a root-owned checkout"),
  ({"stat_root": "0 drwxrwxr-x"}, "must be a root-owned checkout"),
  ({"fail": "stat"}, "cannot inspect"),
  ({"uname": "aarch64"}, "official images are amd64 only"),
  ({"systemd": "offline"}, "systemd is not running"),
  ({"context": "remote"}, "use the local default Docker context"),
  ({"endpoint": "tcp://10.0.0.1:2375"}, "use the local Docker daemon"),
  ({"fail": "docker_info"}, "Docker is unavailable"),
  ({"fail": "docker_compose"}, "Docker Compose v2 is unavailable"),
  ({"fail": "docker_ps"}, "cannot list containers"),
  ({"containers": "mobius mobius \n"}, "existing container mobius"),
  ({"containers": "edge-caddy  \n"}, "existing container edge-caddy"),
  ({"containers": "renamed  mobius\n"}, "existing container renamed"),
  ({"containers": "mobius-caddy-1 mobius \n"}, "existing container mobius-caddy-1"),
  ({"fail": "docker_volume"}, "cannot list volumes"),
  ({"volumes": "mobius_caddy_data\n"}, "existing volume mobius_caddy_data"),
  ({"volumes": "mobius_app_data\n"}, "existing volume mobius_app_data"),
  ({"networks": "bridge\nedge-mobius\n"}, "existing network edge-mobius"),
  ({"networks": "mobius_default\n"}, "existing network mobius_default"),
  ({"images": "sha256:abc\n"}, "an image named mobius already exists"),
  ({"ports": "tcp LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*\n"}, "ports 80/443 are already in use"),
  ({"fail": "ss"}, "cannot list listening ports"),
  ({"free": str(10 * 1024 ** 3)}, "needs 15 GiB free"),
])
def test_preflight_refuses_before_any_mutation(tmp_path, case, message):
  result, _ = _preflight(tmp_path, **case)
  assert result.returncode == 2, (result.stdout, result.stderr)
  assert message in result.stderr and "PREFLIGHT_OK" not in result.stdout


def test_preflight_refuses_an_existing_helper_path(tmp_path):
  existing = tmp_path / "mobius-rebuild"
  existing.mkdir()
  result, _ = _preflight(tmp_path, helper_path=str(existing))
  assert result.returncode == 2 and "existing helper path" in result.stderr


# --- local ingress ---------------------------------------------------------

def _ingress(tmp_path: Path, **responses):
  default = {
    "/api/health": ("{}", 200), "/api/ready": ("{}", 200),
    "/api/version": (json.dumps({"sha": TARGET}), 200),
    "/api/platform/status": (json.dumps(RESIDUALS[0][0]), 200),
  }
  default.update(responses)
  calls = tmp_path / "curl.log"
  cases = "\n".join(
    f"    *{path}) [[ {code} == 0 ]] && return 7; printf '%s\\n%s' {shlex.quote(body)} {code} ;;"
    for path, (body, code) in default.items())
  stub = f'''
curl() {{
  printf '%s\\n' "$*" >>"{calls}"
  case "${{@: -1}}" in
{cases}
    *) return 22 ;;
  esac
}}
'''
  program = PREDICATES + _region("INGRESS CHECKS") + stub + f'\ningress_serves_target ca.pem owner-token {TARGET}'
  result = _bash(program)
  return result, calls.read_text() if calls.exists() else ""


def test_ingress_accepts_the_target_and_old_session_through_the_local_proxy_only(tmp_path):
  result, calls = _ingress(tmp_path)
  assert result.returncode == 0, result.stderr
  lines = calls.splitlines()
  assert [line.rsplit(" ", 1)[1] for line in lines] == [
    "https://localhost/api/health", "https://localhost/api/ready",
    "https://localhost/api/version", "https://localhost/api/platform/status"]
  for line in lines:
    assert line.startswith("-q ") and "--noproxy *" in line and "--cacert ca.pem" in line
    assert "--resolve localhost:443:127.0.0.1" in line and "--max-time 10" in line
  assert "Authorization: Bearer owner-token" in lines[-1]


@pytest.mark.parametrize("responses", [
  {"/api/health": ("bad gateway", 502)},
  {"/api/ready": ("{}", 503)},
  {"/api/health": ("", 0)},
  {"/api/version": (json.dumps({"sha": PREVIOUS}), 200)},
  {"/api/version": ("<html>", 200)},
  {"/api/platform/status": ("{}", 401)},
  {"/api/platform/status": (json.dumps(RESIDUALS[2][0]), 200)},
  {"/api/platform/status": (json.dumps(RESIDUALS[1][0]), 200)},
])
def test_ingress_fails_on_broken_routing_wrong_target_or_lost_session(tmp_path, responses):
  result, _ = _ingress(tmp_path, **responses)
  assert result.returncode != 0


# --- structure -------------------------------------------------------------

def test_operator_steps_run_in_the_order_the_guards_require():
  steps = [
    'exec env -i PATH="$CLEAN_PATH"',
    'fresh_host_preflight "$ROOT"',
    ">>/etc/hosts",
    'git init -q --bare --initial-branch=main "$RELEASES"',
    "up -d --no-build --no-deps app",
    "the previous image is not the official",
    "the previous image records build",
    "probe seed",
    '"$CHECKOUT/scripts/install-rebuild-helper.sh" || fail "the previous helper did not install"',
    'helper_identity "$PREVIOUS"',
    'mixed_review "$preview" "$PREVIOUS" "$TARGET"',
    'before=$(untouched_state)',
    "api POST /api/platform/rebuild",
    'fail "the refused Update changed the installation"',
    "api POST /api/platform/apply",
    'fail "the refused Apply changed the installation"',
    'git -C "$CHECKOUT" pull -q --ff-only',
    '"$CHECKOUT/scripts/deploy-prod.sh" --yes >"$WORK/deploy-early.log"',
    'fail "the refused deploy changed the installation"',
    '"$CHECKOUT/scripts/install-rebuild-helper.sh" || fail "the target helper migration failed"',
    'helper_identity "$TARGET"',
    '"$CHECKOUT/scripts/deploy-prod.sh" --yes >"$WORK/deploy.log"',
    'ingress_serves_target "$WORK/caddy-root.crt"',
    "mixed upgrade: partial deploy",
  ]
  positions = [SOURCE.index(step) for step in steps]
  assert positions == sorted(positions)


def test_the_documented_scripts_run_unmodified_without_overrides():
  # No copied deploy logic, no override flags or broad Git trust.
  for forbidden in ("--allow-stale", "--allow-unpushed", "--allow-low-disk", "--skip-build",
                    "--force-now", "safe.directory", "sudo -E", "docker-compose.override"):
    assert forbidden not in SOURCE, forbidden
  assert "deploy complete" not in SOURCE.split("# END MIXED PREDICATES", 1)[1]


def test_every_snapshot_is_captured_fail_closed_before_comparison():
  # An inline [[ $(untouched_state) == ... ]] suppresses the snapshot's own
  # failure (the reviewed false pass); each capture must fail the run instead.
  import re
  captures = re.findall(r"[^\n]*\$\(untouched_state\)[^\n]*", SOURCE)
  assert len(captures) == 4
  for line in captures:
    assert re.fullmatch(r'(before|after)=\$\(untouched_state\) \|\| fail "[^"]+"', line.strip()), line


# --- execute the real nested shell, not a canned Docker exec answer --------

def _nested_snapshot(tmp_path):
  root = tmp_path / "container"
  inbox = root / "data/mobius-rebuild/inbox"
  inbox.mkdir(parents=True)
  binary = root / "bin"
  binary.mkdir()
  for name, program in {
    "git": 'printf "synthetic git fact\\n"',
    "find": 'case "${INJECT:-}" in find) exit 23;; metadata) case "$*" in *-printf*) exit 23;; esac;; esac\nexec /usr/bin/find "$@"',
    "sha256sum": '[ "${INJECT:-}" != hash ] || exit 24\nexec /usr/bin/sha256sum "$@"',
  }.items():
    path = binary / name
    path.write_text("#!/bin/sh\n" + program + "\n")
    path.chmod(0o755)
  # Keep the existing outer-observer tests, but replace only their Docker
  # exec arm with actual shell execution. All literal paths are retargeted.
  program = _snapshot_program(tmp_path)
  old = "exec) fails docker_exec && return 7; printf 'head\\nrefs\\nprepared: absent\\n' ;;"
  assert old in program
  program = program.replace(old, 'exec) shift 4; PATH="$FIXTURE_BIN:/usr/bin:/bin" "$@" ;;')
  # Rewrite only container paths. The host fixture itself may also live under
  # /data, and a blanket replacement would corrupt its already-mapped paths.
  for path in ("/data/platform", "/data/mobius-rebuild/inbox", "/data/.platform-prepared-update.json"):
    assert path in program
    program = program.replace(path, str(root) + path)
  program += '\nbefore=$(untouched_state) || exit 71\nprintf "%s\\n" "$before"'
  env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root), "FIXTURE_BIN": str(binary)}
  return program, env, root, inbox


@pytest.mark.parametrize("failure", ["find", "metadata", "hash"])
def test_failed_inner_observation_cannot_hide_an_added_request(tmp_path, failure):
  program, env, root, inbox = _nested_snapshot(tmp_path)
  baseline = _bash(program, env=env)
  assert baseline.returncode == 0, baseline.stderr
  request = inbox / "request.json"
  request.write_text('{"unexpected": "mutation"}')
  observed = _bash(program, env=env)
  assert observed.returncode == 0 and observed.stdout != baseline.stdout
  rejected = _bash(program, env={**env, "INJECT": failure})
  assert rejected.returncode == 71, (rejected.stdout, rejected.stderr)
  assert rejected.stdout != baseline.stdout


@pytest.mark.parametrize("kind", ["dangling-link", "readable-link", "directory", "unreadable-file", "hash-failure"])
def test_prepared_record_presence_never_disappears_from_the_snapshot(tmp_path, kind):
  program, env, root, inbox = _nested_snapshot(tmp_path)
  baseline = _bash(program, env=env)
  assert baseline.returncode == 0, baseline.stderr
  record = root / "data/.platform-prepared-update.json"
  if kind.endswith("link"):
    target = root / "target"
    if kind == "readable-link":
      target.write_text("{}")
    record.symlink_to(target)
  elif kind == "directory":
    record.mkdir()
  else:
    record.write_text("{}")
    if kind == "unreadable-file":
      record.chmod(0)
    else:
      env["INJECT"] = "hash"
  try:
    rejected = _bash(program, env=env)
    assert rejected.returncode == 71, (rejected.stdout, rejected.stderr)
  finally:
    if kind == "unreadable-file":
      record.chmod(0o600)


def test_regular_prepared_record_changes_are_observed(tmp_path):
  program, env, root, inbox = _nested_snapshot(tmp_path)
  absent = _bash(program, env=env)
  record = root / "data/.platform-prepared-update.json"
  record.write_text('{"target": "one"}')
  first = _bash(program, env=env)
  record.write_text('{"target": "two"}')
  second = _bash(program, env=env)
  assert absent.returncode == first.returncode == second.returncode == 0
  assert len({absent.stdout, first.stdout, second.stdout}) == 3


def test_prepared_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
  import os
  import signal
  program, env, root, _inbox = _nested_snapshot(tmp_path)
  os.mkfifo(root / "data/.platform-prepared-update.json", 0o600)
  # Real hashing would block on this FIFO. Bound and clean up the whole
  # shell group even if the rejection regresses, not just its outer Bash.
  process = subprocess.Popen(
    ["/bin/bash", "-euo", "pipefail", "-c", program], env=env,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
  )
  timed_out = False
  try:
    stdout, stderr = process.communicate(timeout=2)
  except subprocess.TimeoutExpired:
    timed_out = True
  finally:
    if process.poll() is None:
      os.killpg(process.pid, signal.SIGKILL)
    stdout, stderr = process.communicate()
  assert not timed_out, "prepared FIFO reached a blocking observation"
  assert process.returncode == 71, (stdout, stderr)


# --- provenance guards execute; retaining an error string proves nothing --

def _previous_identity(**changes):
  values = {"image": "sha256:old", "labels": f"{PREVIOUS} https://github.com/mobius-os/mobius",
            "running": "sha256:old", "built": PREVIOUS, "served": f"platform {PREVIOUS}",
            "head": PREVIOUS, "version": PREVIOUS, "fail": ""}
  values.update(changes)
  assignments = "\n".join(f"V_{key}={shlex.quote(value)}" for key, value in values.items())
  stubs = '''
fail() { echo "$*" >&2; exit 79; }
observe() { [[ $V_fail != "$1" ]] || return 23; local key=V_$1; printf '%s\\n' "${!key}"; }
docker() {
  case "$1 $2" in
    "image inspect") [[ $4 == '{{.Id}}' ]] && observe image || observe labels ;;
    "inspect -f") observe running ;;
    "exec app") observe built ;;
    "exec -u") observe head ;;
    *) return 91 ;;
  esac
}
served() { observe served; }
api() { local version; version=$(observe version) || return 23; printf '{"sha":"%s"}\\n200\\n' "$version"; }
'''
  return _bash(assignments + stubs + PREDICATES + f'\nPREVIOUS={PREVIOUS}\nIMAGE=synthetic\napp=app\n'
               + _region("PREVIOUS IDENTITY") + '\necho IDENTITY_OK')


def test_previous_identity_accepts_only_all_agreeing_observations():
  result = _previous_identity()
  assert result.returncode == 0 and "IDENTITY_OK" in result.stdout, result.stderr


@pytest.mark.parametrize("field", ["image", "labels", "running", "built", "served", "head", "version"])
def test_previous_identity_rejects_wrong_or_failed_observations(field):
  wrong = _previous_identity(**{field: "wrong"})
  failed = _previous_identity(fail=field)
  assert wrong.returncode != 0 and "IDENTITY_OK" not in wrong.stdout
  assert failed.returncode != 0 and "IDENTITY_OK" not in failed.stdout


def _helper_identity(tmp_path, *, mutation=None, failed_observation=None):
  import hashlib
  worker_bytes = b"WORKER_REVISION = 5\n# fixture worker\n"
  digest = hashlib.sha256(worker_bytes).hexdigest()
  status = {"launcher_revision": 4}
  index = {"active": {"revision": 5, "sha256": digest}}
  if mutation == "launcher":
    status["launcher_revision"] = 99
  elif mutation == "revision":
    index["active"]["revision"] = 99
  elif mutation == "digest":
    index["active"]["sha256"] = "0" * 64
  elif mutation == "journal":
    (tmp_path / "transaction.json").write_text("{}")
  (tmp_path / "status.json").write_text(json.dumps(status))
  (tmp_path / "workers.json").write_text(json.dumps(index))
  if failed_observation in {"status", "index"}:
    (tmp_path / ("status.json" if failed_observation == "status" else "workers.json")).write_text("invalid JSON")
  worker = tmp_path / "worker.py"
  worker.write_bytes(worker_bytes)
  stubs = f'''
ROOT=fixture
STATUS={shlex.quote(str(tmp_path / 'status.json'))}
FAIL_OBSERVATION={shlex.quote(failed_observation or '')}
git() {{
  case "${{@: -1}}" in
    *:scripts/mobius-rebuild-launcher.py) [[ $FAIL_OBSERVATION != launcher ]] || return 23; echo 'LAUNCHER_REVISION = 4' ;;
    *:scripts/mobius-rebuild-host.py) [[ $FAIL_OBSERVATION != worker ]] || return 24; cat {shlex.quote(str(worker))} ;;
    *) return 91 ;;
  esac
}}
'''
  functions = (_region("REVISION READER") + _region("HELPER IDENTITY"))
  functions = functions.replace("/var/lib/mobius-rebuild", str(tmp_path))
  return _bash(stubs + functions + f'\nhelper_identity {PREVIOUS} || exit 72\necho HELPER_OK')


def test_helper_identity_accepts_matching_release_revisions_and_digest(tmp_path):
  result = _helper_identity(tmp_path)
  assert result.returncode == 0 and "HELPER_OK" in result.stdout, result.stderr


@pytest.mark.parametrize("mutation", ["launcher", "revision", "digest", "journal"])
def test_helper_identity_rejects_wrong_indexed_release_or_open_journal(tmp_path, mutation):
  result = _helper_identity(tmp_path, mutation=mutation)
  assert result.returncode == 72 and "HELPER_OK" not in result.stdout


@pytest.mark.parametrize("observation", ["launcher", "worker", "status", "index"])
def test_helper_identity_rejects_failed_observations(tmp_path, observation):
  result = _helper_identity(tmp_path, failed_observation=observation)
  assert result.returncode == 72 and "HELPER_OK" not in result.stdout
