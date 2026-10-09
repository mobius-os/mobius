"""Installer migration boundaries; never invokes Docker or host systemd."""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install-rebuild-helper.sh"
SOURCE = INSTALLER.read_text()


def _heredoc(tag: str) -> str:
    return SOURCE.split(f"<<'{tag}'\n", 1)[1].split(f"\n{tag}\n", 1)[0]


def _gate_install(tmp_path: Path):
    """Execute the real installer Python, redirecting only absolute host paths."""
    for name in ("var/lib/mobius-rebuild", "usr/local"):
        (tmp_path / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    code = _heredoc("PY_GATE")
    for prefix in ("/var", "/usr"):
        code = code.replace(f'"{prefix}', f'"{tmp_path}{prefix}')
    source = tmp_path / "admission-source.py"
    if not source.exists():
        source.write_text("#!/usr/bin/env python3\nprint('pinned')\n")
    manual = tmp_path / "manual-source.py"
    if not manual.exists():
        manual.write_text("#!/usr/bin/env python3\nprint('manual')\n")
    result = subprocess.run(
        [sys.executable, "-c", code, str(source), str(manual)], capture_output=True, text=True,
    )
    return result, source, tmp_path / "usr/local/libexec/mobius-boot-admission.py", tmp_path / "var/lib/mobius-rebuild/admission"


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_pinned_code_and_private_state_survive_atomic_reinstall(tmp_path):
    result, source, destination, state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == source.read_bytes()
    adapter = destination.with_name("mobius-manual-cutover.py")
    assert adapter.read_bytes() == (tmp_path / "manual-source.py").read_bytes()
    assert adapter.stat().st_uid == adapter.stat().st_gid == 0
    assert stat.S_IMODE(adapter.stat().st_mode) == 0o755
    assert destination.stat().st_uid == destination.stat().st_gid == 0
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert state.stat().st_uid == state.stat().st_gid == 0
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    marker = state / "consumed.json"
    marker.write_text('{"consumed":true}')
    lock = state / "admission.lock"
    lock.touch(mode=0o600)
    inode = lock.stat().st_ino
    with destination.open("rb") as pinned:
        old = pinned.read()
        source.write_text("#!/usr/bin/env python3\nprint('new')\n")
        result, *_ = _gate_install(tmp_path)
        assert result.returncode == 0, result.stderr
        pinned.seek(0)
        assert pinned.read() == old  # existing bind-mount inode is not overwritten
    assert destination.read_bytes() == source.read_bytes()
    assert marker.read_text() == '{"consumed":true}'
    assert lock.stat().st_ino == inode
    assert not list(destination.parent.glob(".mobius-*.py.*"))


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_unprivileged_process_cannot_replace_code_or_forge_admission(tmp_path):
    result, _source, destination, state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    # Make the test sandbox traversable so denial is at the installed boundary,
    # not merely pytest's private parent. No external host paths are modified.
    parent = tmp_path
    while parent.name.startswith("test_") or parent.name.startswith(".pytest-tmp"):
        parent.chmod(0o755)
        parent = parent.parent
    for name in ("var", "var/lib", "var/lib/mobius-rebuild", "usr", "usr/local", "usr/local/libexec"):
        (tmp_path / name).chmod(0o755)
    before = destination.read_bytes()
    program = """
import os, sys
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
for path in sys.argv[1:]:
    try:
        with open(path, 'w') as handle:
            handle.write('forged')
    except PermissionError:
        continue
    raise SystemExit('unprivileged write succeeded')
"""
    attempt = subprocess.run([sys.executable, "-c", program, str(destination), str(state / "forged.json")],
                             capture_output=True, text=True)
    assert attempt.returncode == 0, attempt.stderr
    assert destination.read_bytes() == before
    assert not (state / "forged.json").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
@pytest.mark.parametrize("unsafe", ["state_symlink", "state_writable", "state_owner", "code_symlink", "code_writable", "parent_symlink"])
def test_pin_refuses_untrusted_boundaries_without_replacing_code(tmp_path, unsafe):
    result, source, destination, state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    original = destination.read_bytes()
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    if unsafe == "state_symlink":
        state.rmdir()
        state.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "state_writable":
        state.chmod(0o777)
    elif unsafe == "state_owner":
        os.chown(state, 65534, 65534)
    elif unsafe == "code_symlink":
        destination.unlink()
        destination.symlink_to(victim)
    elif unsafe == "code_writable":
        destination.chmod(0o777)
    else:
        moved = tmp_path / "moved-libexec"
        destination.parent.rename(moved)
        destination.parent.symlink_to(moved, target_is_directory=True)
    source.write_text("print('must not install')\n")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert "root-controlled" in result.stderr
    assert victim.read_text() == "unchanged"
    if unsafe != "code_symlink":
        assert destination.read_bytes() == original


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_invalid_gate_source_does_not_replace_installed_code(tmp_path):
    result, source, destination, _state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    original = destination.read_bytes()
    source.write_text("not valid python !!!")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert destination.read_bytes() == original


def _refusal() -> str:
    return "refuse_unresolved_transaction() {" + SOURCE.split(
        "refuse_unresolved_transaction() {", 1,
    )[1].split("# Never freeze", 1)[0]


@pytest.mark.parametrize("kind", ["legacy", "wrapped", "corrupt", "broken_symlink", "directory"])
def test_existing_transaction_refuses_before_controller_changes(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir()
    tx = state / "transaction.json"
    if kind == "broken_symlink":
        tx.symlink_to(state / "missing")
    elif kind == "directory":
        tx.mkdir()
    else:
        tx.write_text({"legacy": '{"phase":"rollback_started"}',
                       "wrapped": '{"admission_protocol":1}',
                       "corrupt": 'broken'}[kind])
    code = _refusal().replace("/var/lib/mobius-rebuild", str(state))
    published = tmp_path / "controller-changed"
    result = subprocess.run(["bash", "-eu", "-c", code + f'\ntouch "{published}"'],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "Unresolved replacement transaction" in result.stderr
    assert not published.exists()
    assert tx.exists() or tx.is_symlink()
    first_check = SOURCE.index("\nrefuse_unresolved_transaction\n")
    assert first_check < SOURCE.index("systemctl stop")
    assert first_check < SOURCE.index("install -d -m 0700")
    assert SOURCE.index("flock -w 30 8") < SOURCE.index("flock -w 30 9") < SOURCE.index("# BEGIN PINNED")
    assert SOURCE.index("publish-capabilities") < SOURCE.index("flock -u 8")
    assert SOURCE.index("# END PINNED") < SOURCE.index("MOBIUS_REBUILD_LOCK_HELD=1")


def test_transaction_appearing_at_lock_acquisition_refuses_publication(tmp_path):
    state = tmp_path / "state"
    etc = tmp_path / "etc"
    state.mkdir()
    code = _refusal() + SOURCE.split("umask 077\n", 1)[1].split("# BEGIN PINNED", 1)[0]
    code = code.replace("/var/lib/mobius-rebuild", str(state)).replace("/etc/mobius-rebuild", str(etc))
    code = '''systemctl() { if [ "$1" = show ]; then printf 'LoadState=not-found\\nActiveState=inactive\\nJob=\\n'; fi; }
flock() { if [ "${3:-}" = 9 ]; then printf '{"phase":"rollback_started"}' >"$STATE/transaction.json"; fi; }
''' + code + '\ntouch "$PUBLISHED"\n'
    published = tmp_path / "controller-changed"
    result = subprocess.run(["bash", "-eu", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "STATE": str(state), "PUBLISHED": str(published),
                                 "RESOLVED": str(tmp_path / "resolved")})
    assert result.returncode == 1
    assert "Unresolved replacement transaction" in result.stderr
    assert not published.exists()
    assert (state / "transaction.json").exists()


def test_installer_holds_dispatch_fence_while_replacement_lock_is_owned(tmp_path):
    start = SOURCE.index("exec 8>>/var/lib/mobius-rebuild/candidate.lock")
    end = SOURCE.index("\nrefuse_unresolved_transaction\n", start)
    locking = SOURCE[start:end].replace("/var/lib/mobius-rebuild", str(tmp_path))
    process = subprocess.Popen(
        ["bash", "-eu", "-c", locking + "\npython3 -c 'import os; os.fstat(8); os.fstat(9)'\nprintf 'locked\\n'; read -r release"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        for name in ("candidate.lock", "replace.lock"):
            with (tmp_path / name).open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.communicate("release\n", timeout=5)
        assert process.returncode == 0
        for name in ("candidate.lock", "replace.lock"):
            with (tmp_path / name).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_busy_dispatch_refuses_without_waiting_forever_or_stopping_recovery(tmp_path):
    start = SOURCE.index("exec 8>>/var/lib/mobius-rebuild/candidate.lock")
    end = SOURCE.index("\nrefuse_unresolved_transaction\n", start)
    # Keep the real refusal path, shorten only its documented 30-second budget.
    locking = SOURCE[start:end].replace("/var/lib/mobius-rebuild", str(tmp_path)).replace("-w 30", "-w 0")
    with (tmp_path / "candidate.lock").open("a") as active:
        fcntl.flock(active, fcntl.LOCK_EX)
        result = subprocess.run(["bash", "-eu", "-c", locking], capture_output=True, text=True, timeout=5)
        assert result.returncode == 1
        assert "controller dispatch is active" in result.stderr
        assert not (tmp_path / "replace.lock").exists()
        # The installer never terminates the existing lock owner.
        assert active.fileno() >= 0


@pytest.mark.parametrize("app,accepted", [
    ({"image": "original", "entrypoint": ["/usr/bin/tini", "--"]}, True),
    ({"entrypoint": ["python3", "/run/mobius-boot-admission.py"]}, False),
    ({"volumes": [{"target": "/run/mobius-admission"}]}, False),
    ({"labels": {"io.mobius.admission.token": "stale-attempt"}}, False),
])
def test_base_snapshot_cannot_capture_attempt_wrapper(tmp_path, app, accepted):
    config = tmp_path / "compose.json"
    config.write_text(json.dumps({"services": {"app": app}}))
    result = subprocess.run([sys.executable, "-c", _heredoc("PY_BASE"), str(config)],
                            capture_output=True, text=True)
    assert (result.returncode == 0) is accepted
    # Helper-created live containers may be wrapped: copy only their base,
    # never resolve the currently active per-attempt image override into it.
    snapshot = SOURCE.split('SNAPSHOT=$(mktemp', 1)[1].split('OVERRIDE_NEW=', 1)[0]
    assert 'cp "$FROZEN_SOURCE" "$SNAPSHOT"' in snapshot
    assert "image.override.yml" not in snapshot


def test_gate_is_reviewed_input_and_revision_is_five():
    for section in (SOURCE.split("ls-files --error-unmatch", 1)[1].split("# The running", 1)[0],
                    SOURCE.split("diff --quiet HEAD --", 1)[1].split("ARGS=()", 1)[0]):
        assert "scripts/mobius-boot-admission.py" in section
        assert "scripts/mobius-manual-cutover.py" in section
    assert SOURCE.count("scripts/mobius-rebuild-launcher.py scripts/mobius-boot-admission.py") == 3
    assert "# Helper protocol revision: 5 " in SOURCE
    assert (ROOT / "deployment/self-hosted-helper.required").read_text().strip() == "5"


def test_installer_does_not_boot_or_clear_application_state():
    # Inspect executable shell, excluding generated systemd units and Python.
    shell = SOURCE
    for tag in ("EOF", "PY", "PY_GATE", "PY_BASE"):
        shell = re.sub(r"<<'?" + tag + r"'?\n.*?\n" + tag + r"\n", "\n", shell, flags=re.S)
    assert not re.search(r"docker\s+(?:start|restart|stop|rm|kill|run)\b", shell)
    assert not re.search(r"docker compose[^\n]*\b(?:up|down|start|restart)\b", shell)
    assert not re.search(r"^/usr/local/libexec/mobius-rebuild-host (?:reconcile|run)$", shell, re.M)
    assert '/usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities' in shell
    assert ".restart-ledger" not in shell
    assert "-L /var/lib/mobius-rebuild/transaction.json ]]; then" in shell  # refusal, never unlink
    assert not re.search(r"(?:rm|unlink)[^\n]*transaction.json", shell)
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


@pytest.mark.parametrize("legacy,admitted,accepted", [
    ("a" * 64, "", True),
    ("", "b" * 64, True),
    ("a" * 64, "a" * 64, True),
    ("a" * 64, "b" * 64, False),
    ("", "", False),
    ("short-id", "", False),
    ("", "b" * 64 + "\n" + "c" * 64, False),
])
def test_discovery_is_exact_union_of_legacy_and_admission_projects(tmp_path, legacy, admitted, accepted):
    function = "discover_app() {" + SOURCE.split("discover_app() {", 1)[1].split("\nCID=$(discover_app)", 1)[0]
    fake_docker = '''docker() {
  case "$*" in
    *io.mobius.admission.project*) printf '%s\\n' "$ADMITTED" ;;
    *com.docker.compose.project*) printf '%s\\n' "$LEGACY" ;;
    *) return 99 ;;
  esac
}
'''
    result = subprocess.run(["bash", "-eu", "-c", fake_docker + function + "\nCID=$(discover_app)\nprintf '%s' \"$CID\""],
                            env={**os.environ, "PROJECT": "original", "LEGACY": legacy, "ADMITTED": admitted},
                            capture_output=True, text=True)
    assert (result.returncode == 0) is accepted
    if accepted:
        assert result.stdout == (legacy or admitted)


def test_discovery_does_not_treat_failed_query_as_empty_project():
    function = "discover_app() {" + SOURCE.split("discover_app() {", 1)[1].split("\nCID=$(discover_app)", 1)[0]
    result = subprocess.run(["bash", "-eu", "-c", "docker() { return 1; }\n" + function + "\ndiscover_app"],
                            env={**os.environ, "PROJECT": "original"}, capture_output=True, text=True)
    assert result.returncode == 1


def _wrapped_identity(tmp_path):
    frozen = tmp_path / "etc/mobius-rebuild"
    attempts = frozen / "attempts"
    attempts.mkdir(parents=True)
    cid, token, operation = "a" * 64, "b" * 32, "c" * 32
    image = "sha256:" + "d" * 64
    name = "mobius-admission-" + token
    labels = {"io.mobius.admission.project": "original", "io.mobius.admission.operation": operation,
              "io.mobius.admission.role": "rollback", "io.mobius.admission.token": token}
    entrypoint = ["python3", "-I", "-S", "/run/mobius-boot-admission.py", "enter",
                  "--state-dir", "/run/mobius-admission", "--data-dir", "/data",
                  "--token", token, "--", "/usr/bin/tini", "-s", "--"]
    app = {"image": image, "container_name": name, "labels": labels,
           "entrypoint": entrypoint, "command": ["./scripts/entrypoint.sh"]}
    expected = {"name": name, "services": {"app": app}}
    (frozen / "config.json").write_text(json.dumps({"version": 3, "project": "original", "data_dir": "/host/data"}))
    (frozen / "image.override.yml").write_text(json.dumps(expected))
    attempt = attempts / (token + ".json")
    attempt.write_text(json.dumps(expected))
    actual = {"Id": cid, "Image": image, "Name": "/" + name, "Entrypoint": entrypoint,
              "Cmd": app["command"], "Labels": {**labels,
              "com.docker.compose.project": name, "com.docker.compose.service": "app",
              "com.docker.compose.project.working_dir": str(frozen),
              "com.docker.compose.project.config_files": str(attempt)},
              "Mounts": [
                  {"Destination": "/run/mobius-boot-admission.py", "Source": "/usr/local/libexec/mobius-boot-admission.py", "Type": "bind", "RW": False},
                  {"Destination": "/run/mobius-admission", "Source": "/var/lib/mobius-rebuild/admission/" + operation, "Type": "bind", "RW": True},
                  {"Destination": "/data", "Source": "/host/data", "Type": "volume", "RW": True},
              ]}
    code = _heredoc("PY_ADMISSION").replace("/etc/mobius-rebuild", str(frozen))
    return frozen, attempt, actual, code, cid


@pytest.mark.skipif(os.geteuid() != 0, reason="validates root-owned immutable attempt files")
@pytest.mark.parametrize("mutation", [None, "owner", "compose_project", "token", "entrypoint", "command",
                                     "image", "code_writable", "state_readonly", "state_source", "data_source",
                                     "duplicate_mount", "config_files", "immutable_changed", "immutable_symlink",
                                     "immutable_writable", "immutable_directory_symlink"])
def test_wrapped_reinstall_requires_frozen_identity_and_immutable_attempt(tmp_path, mutation):
    frozen, attempt, actual, code, cid = _wrapped_identity(tmp_path)
    if mutation == "owner":
        (frozen / "config.json").write_text(json.dumps({"version": 3, "project": "wrong", "data_dir": "/host/data"}))
    elif mutation == "compose_project":
        actual["Labels"]["com.docker.compose.project"] = "original"
    elif mutation == "token":
        actual["Labels"]["io.mobius.admission.token"] = "e" * 32
    elif mutation == "entrypoint":
        actual["Entrypoint"] = ["./scripts/entrypoint.sh"]
    elif mutation == "command":
        actual["Cmd"] = ["unexpected"]
    elif mutation == "image":
        actual["Image"] = "sha256:" + "e" * 64
    elif mutation == "code_writable":
        actual["Mounts"][0]["RW"] = True
    elif mutation == "state_readonly":
        actual["Mounts"][1]["RW"] = False
    elif mutation == "state_source":
        actual["Mounts"][1]["Source"] = "/data/forged-state"
    elif mutation == "data_source":
        actual["Mounts"][2]["Source"] = "/wrong-data"
    elif mutation == "duplicate_mount":
        actual["Mounts"].append(actual["Mounts"][0])
    elif mutation == "config_files":
        actual["Labels"]["com.docker.compose.project.config_files"] = "/arbitrary/label/path"
    elif mutation == "immutable_changed":
        attempt.write_text("{}")
    elif mutation == "immutable_symlink":
        attempt.unlink()
        attempt.symlink_to(frozen / "image.override.yml")
    elif mutation == "immutable_writable":
        attempt.chmod(0o666)
    elif mutation == "immutable_directory_symlink":
        relocated = frozen / "other"
        attempt.parent.rename(relocated)
        attempt.parent.symlink_to(relocated, target_is_directory=True)
    result = subprocess.run([sys.executable, "-c", code, "original", cid, json.dumps(actual)],
                            capture_output=True, text=True)
    assert (result.returncode == 0) is (mutation is None), result.stderr


def test_wrapped_reinstall_preserves_current_pointer_not_wrapped_base():
    assert 'if [[ $ADMISSION_MANAGED == 0 ]]; then\nOVERRIDE_NEW=' in SOURCE
    assert 'cp "$FROZEN_SOURCE" "$SNAPSHOT"' in SOURCE
    assert '[[ $(discover_app) == "$CID" ]]' in SOURCE
    locked = SOURCE.split("flock -w 30 9", 1)[1]
    assert locked.index("verify_admission_identity") < locked.index("# BEGIN PINNED")
    assert 'MOBIUS_REBUILD_LAUNCHER=2 \\\n  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities' in SOURCE


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises root-pinned helper publication")
def test_invalid_adapter_source_prevents_publishing_either_helper(tmp_path):
    result, gate_source, destination, _state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    adapter = destination.with_name("mobius-manual-cutover.py")
    old_gate, old_adapter = destination.read_bytes(), adapter.read_bytes()
    gate_source.write_text("print('new gate')\n")
    (tmp_path / "manual-source.py").write_text("invalid python !!!")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert destination.read_bytes() == old_gate
    assert adapter.read_bytes() == old_adapter


def test_generated_unit_budgets_cover_pull_full_cutover_and_recovery(tmp_path):
    import ast
    import configparser

    source = (ROOT / "scripts/mobius-rebuild-host.py").read_text()
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    pulls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "docker_command" and node.args
             and isinstance(node.args[0], ast.List)
             and any(isinstance(value, ast.Constant) and value.value == "pull"
                     for value in node.args[0].elts)]
    assert len(pulls) == 1
    pull_budget = next(ast.literal_eval(item.value) for item in pulls[0].keywords if item.arg == "timeout")
    # Initial cutover: remove source; create/start target; fence failed target;
    # create/start rollback. Reconciliation budgets the same full recovery pass.
    mutation_budget = 6 * constants["COMPOSE_MUTATION_SECONDS"]
    readiness_budget = constants["TARGET_HEALTH_SECONDS"] + constants["ROLLBACK_HEALTH_SECONDS"]
    recovery_budget = mutation_budget + readiness_budget + 600
    run_budget = pull_budget + recovery_budget + 1800
    start = SOURCE.index("cat >/etc/systemd/system/mobius-rebuild.service")
    end = SOURCE.index("chmod 0644 /etc/systemd/system/mobius-rebuild.service", start)
    units = SOURCE[start:end].replace("/etc/systemd/system/", f"{tmp_path}/")
    subprocess.run(["bash", "-eu", "-c", units], check=True,
                   env={**os.environ, "DATA_SOURCE": str(tmp_path / "data")})
    main, recovery = configparser.ConfigParser(), configparser.ConfigParser()
    main.read(tmp_path / "mobius-rebuild.service")
    recovery.read(tmp_path / "mobius-rebuild-reconcile.service")
    assert main["Service"].getint("TimeoutStartSec") == run_budget == 9000
    assert main["Service"].getint("TimeoutStopSec") == recovery_budget == 3600
    assert recovery["Service"].getint("TimeoutStartSec") == recovery_budget


def _dispatch_quiescence():
    return SOURCE.split('# BEGIN MIGRATION DISPATCH QUIESCENCE\n', 1)[1].split(
        '# END MIGRATION DISPATCH QUIESCENCE', 1,
    )[0]


def _fake_systemd():
    # Files are isolated unit state, not the host service manager. A selected
    # delayed child marks the oneshot activating until it is explicitly resumed.
    return r'''systemctl() {
  printf '%s\n' "$*" >>"$FIXTURE/calls"
  local verb=$1 unit=$2 active=inactive job= load=loaded
  case "$verb" in
    show)
      [[ ! -f "$FIXTURE/$unit.unreadable" ]] || return 1
      [[ ! -f "$FIXTURE/$unit.state" ]] || active=$(cat "$FIXTURE/$unit.state")
      [[ ! -f "$FIXTURE/$unit.job" ]] || job=$(cat "$FIXTURE/$unit.job")
      [[ ! -f "$FIXTURE/$unit.missing" ]] || load=not-found
      printf 'Job=%s\nActiveState=%s\nLoadState=%s\n' "$job" "$active" "$load"
      ;;
    stop)
      [[ ! -f "$FIXTURE/$unit.stop-fails" ]] || return 1
      printf inactive >"$FIXTURE/$unit.state"
      if [[ $unit == mobius-rebuild-reconcile.timer && -f "$FIXTURE/late-selection" ]]; then
        printf activating >"$FIXTURE/mobius-rebuild.service.state"
      fi
      ;;
    start) printf active >"$FIXTURE/$unit.state" ;;
    *) return 99 ;;
  esac
}
'''


def _run_quiescence(tmp_path, tail='touch "$FIXTURE/published"', extra=''):
    return subprocess.run(
        ['bash', '-eu', '-c', _fake_systemd() + extra + _dispatch_quiescence() + '\n' + tail],
        env={**os.environ, 'FIXTURE': str(tmp_path), 'RESOLVED': str(tmp_path / 'resolved')},
        capture_output=True, text=True, timeout=5,
    )


@pytest.mark.parametrize('service', ['mobius-rebuild.service', 'mobius-rebuild-reconcile.service'])
@pytest.mark.parametrize('state,job', [('active', ''), ('activating', ''), ('deactivating', ''),
                                    ('inactive', '71'), ('failed', '71')])
def test_migration_refuses_active_or_queued_legacy_services_and_restores_dispatch(tmp_path, service, state, job):
    (tmp_path / 'mobius-rebuild.path.state').write_text('active')
    (tmp_path / 'mobius-rebuild-reconcile.timer.state').write_text('inactive')
    (tmp_path / (service + '.state')).write_text(state)
    (tmp_path / (service + '.job')).write_text(job)
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1, result.stderr
    assert 'leave recovery running' in result.stderr
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.path.state').read_text() == 'active'
    assert (tmp_path / 'mobius-rebuild-reconcile.timer.state').read_text() == 'inactive'
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert not any(line.startswith(('stop mobius-rebuild.service', 'stop mobius-rebuild-reconcile.service'))
                   for line in calls)
    assert (tmp_path / (service + '.state')).read_text() == state


def test_selection_just_before_dispatch_stop_is_observed_before_mutation(tmp_path):
    (tmp_path / 'late-selection').touch()
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.service.state').read_text() == 'activating'


@pytest.mark.parametrize('problem', ['stop-fails', 'unreadable'])
def test_dispatch_failure_refuses_publication_and_restores_previous_timer(tmp_path, problem):
    (tmp_path / 'mobius-rebuild.path.state').write_text('inactive')
    (tmp_path / 'mobius-rebuild-reconcile.timer.state').write_text('active')
    (tmp_path / ('mobius-rebuild-reconcile.timer.' + problem)).touch()
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.path.state').read_text() == 'inactive'
    assert (tmp_path / 'mobius-rebuild-reconcile.timer.state').read_text() == 'active'


def test_quiescent_dispatch_is_held_until_publication_and_restored_on_failure(tmp_path):
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        (tmp_path / (unit + '.state')).write_text('active')
    tail = '''[[ $(cat "$FIXTURE/mobius-rebuild.path.state") == inactive ]]
[[ $(cat "$FIXTURE/mobius-rebuild-reconcile.timer.state") == inactive ]]
touch "$FIXTURE/published"
exit 42
'''
    result = _run_quiescence(tmp_path, tail)
    assert result.returncode == 42
    assert (tmp_path / 'published').exists()
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        assert (tmp_path / (unit + '.state')).read_text() == 'active'
    assert SOURCE.index('# END MIGRATION DISPATCH QUIESCENCE') < SOURCE.index('install -d -m 0700')


def test_launcher1_selected_child_before_lock_is_not_fenced_by_lock_order_alone(tmp_path):
    # Deterministic negative control: a selected legacy child is paused BEFORE
    # flock. Neither the new dispatch lock nor a bounded worker flock timeout
    # constrains this pause. It can execute after both installer locks release.
    child = r'''
import fcntl, pathlib, sys
root = pathlib.Path(sys.argv[1])
(root / 'mobius-rebuild.service.state').write_text('activating')
print('selected-before-lock', flush=True)
sys.stdin.readline()
with (root / 'replace.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (root / 'legacy-ran').write_text((root / 'config').read_text())
'''
    process = subprocess.Popen([sys.executable, '-c', child, str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'selected-before-lock'
        with (tmp_path / 'candidate.lock').open('a') as dispatch, (tmp_path / 'replace.lock').open('a') as replacement:
            fcntl.flock(dispatch, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(replacement, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Actual new preflight refuses before changing config or code,
            # despite both cooperative locks being available to the installer.
            result = _run_quiescence(tmp_path)
            assert result.returncode == 1
            assert not (tmp_path / 'published').exists()
            assert process.poll() is None  # installer did not kill active recovery
            (tmp_path / 'config').write_text('new config if lock-only installer published')
        process.communicate('\n', timeout=5)
        assert process.returncode == 0
        assert (tmp_path / 'legacy-ran').read_text() == 'new config if lock-only installer published'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_incomplete_systemd_observation_cannot_prove_quiescence(tmp_path):
    result = _run_quiescence(tmp_path, extra="""
systemctl() { printf 'LoadState=loaded\\nActiveState=inactive\\n'; }
""")
    assert result.returncode == 1
    assert 'Cannot inspect dispatch unit' in result.stderr
    assert not (tmp_path / 'published').exists()


def test_successful_publication_does_not_restore_obsolete_dispatch_state(tmp_path):
    result = _run_quiescence(tmp_path, tail="""
systemctl start mobius-rebuild.path
systemctl start mobius-rebuild-reconcile.timer
MIGRATION_COMPLETE=1
""")
    assert result.returncode == 0, result.stderr
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        assert (tmp_path / (unit + '.state')).read_text() == 'active'
