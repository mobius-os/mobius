"""Host rollbacks honour the database's compatibility floor.

ONE_WAY_UPGRADES_DESIGN.md, "Host rollbacks": deploy-prod.sh and the rebuild
helper validate a rollback with the bounded /api/ready payload, treat
``below_compatibility_floor`` as terminal, and run a fenced preflight (stop the
failed container, read the rollback image's baked level, then read the real
floor directly and read-only) before replacing anything.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DEPLOY = SCRIPTS / "deploy-prod.sh"


def _load(name: str, path: Path):
  spec = importlib.util.spec_from_file_location(name, path)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


support = _load("deploy_support_floor", SCRIPTS / "deploy_support.py")
host = _load("mobius_rebuild_host_floor", SCRIPTS / "mobius-rebuild-host.py")

DB_RELATIVE = Path("db") / "ultimate.db"
_REAL_RUN = subprocess.run


def _database(data: Path, *, floor: int | None = None, ledger: bool = True) -> Path:
  path = data / DB_RELATIVE
  path.parent.mkdir(parents=True, exist_ok=True)
  con = sqlite3.connect(path)
  con.execute("CREATE TABLE apps (id INTEGER PRIMARY KEY)")
  if ledger:
    con.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
  if floor is not None:
    con.execute("CREATE TABLE platform_compat (id INTEGER PRIMARY KEY, floor INTEGER NOT NULL)")
    con.execute("INSERT INTO platform_compat (id, floor) VALUES (1, ?)", (floor,))
  con.commit()
  con.close()
  return path


def _probe(path: Path) -> tuple[int, str]:
  result = subprocess.run(
    [sys.executable, "-I", "-c", support.FLOOR_PROBE, str(path)],
    capture_output=True, text=True,
  )
  return result.returncode, result.stdout.strip().splitlines()[-1]


# ── the shared probe and level parser ───────────────────────────────────


def test_both_scripts_embed_the_same_probe_and_paths():
  assert host.FLOOR_PROBE == support.FLOOR_PROBE
  assert host.ROLLBACK_DATABASE == support.ROLLBACK_DATABASE
  assert host.BAKED_COMPAT_PATH == support.BAKED_COMPAT_PATH
  deploy = DEPLOY.read_text(encoding="utf-8")
  assert f"ROLLBACK_DATABASE={support.ROLLBACK_DATABASE} " in deploy
  assert support.BAKED_COMPAT_PATH in deploy


def test_rollback_database_is_the_one_every_compose_file_serves():
  root = SCRIPTS.parent
  url = f"DATABASE_URL=sqlite:///{support.ROLLBACK_DATABASE}"
  for compose in (
    "docker-compose.yml", "docker-compose.test.yml",
    "deploy/docker-compose.runtime.yml",
  ):
    assert url in (root / compose).read_text(encoding="utf-8"), compose


@pytest.mark.parametrize("module", [support, host], ids=["deploy", "rebuild"])
@pytest.mark.parametrize(
  ("source", "level"),
  [
    ("COMPAT_LEVEL = 3\nREQUIRED_IMAGE_LEVEL = 1\n", 3),
    ("COMPAT_LEVEL: int = 2\n", 2),
    ("REQUIRED_IMAGE_LEVEL = 4\n", 0),
    ("", 0),
    ("COMPAT_LEVEL = True\n", 0),
    ("COMPAT_LEVEL = -1\n", 0),
    ("COMPAT_LEVEL = '5'\n", 0),
    ("COMPAT_LEVEL = 1 +\n", 0),
    # Python keeps the last assignment; a duplicate is ambiguous.
    ("COMPAT_LEVEL = 1\nCOMPAT_LEVEL = 3\n", 0),
  ],
)
def test_compat_level_parser(module, source, level):
  assert module.compat_level(source) == level


def test_compat_level_reads_the_real_compat_module():
  source = (SCRIPTS.parent / "backend" / "app" / "compat.py").read_text(encoding="utf-8")
  sys.path.insert(0, str(SCRIPTS.parent / "backend"))
  try:
    from app import compat
  finally:
    sys.path.pop(0)
  assert support.compat_level(source) == compat.COMPAT_LEVEL


def test_probe_reads_the_floor(tmp_path):
  assert _probe(_database(tmp_path, floor=2)) == (0, "floor=2")


def test_missing_floor_table_is_zero_only_with_the_ledger(tmp_path):
  assert _probe(_database(tmp_path / "legacy")) == (0, "floor=0")
  unrelated = _database(tmp_path / "other", ledger=False)
  assert _probe(unrelated) == (3, "error=not_a_mobius_database")


def test_probe_fails_closed_on_a_missing_or_foreign_file(tmp_path):
  assert _probe(tmp_path / "absent.db") == (3, "error=database_missing")
  foreign = tmp_path / "foreign.db"
  foreign.write_bytes(b"not a database at all")
  assert _probe(foreign) == (3, "error=database_invalid")


def test_probe_fails_closed_on_wal_without_shm(tmp_path):
  path = _database(tmp_path, floor=0)
  Path(f"{path}-wal").write_bytes(b"\0" * 64)
  assert _probe(path) == (3, "error=wal_without_shm")


def test_probe_fails_closed_on_a_hot_rollback_journal(tmp_path):
  path = _database(tmp_path, floor=0)
  Path(f"{path}-journal").write_bytes(b"\0" * 64)
  assert _probe(path) == (3, "error=hot_journal")


def test_probe_replays_a_committed_wal_with_its_shm(tmp_path):
  path = _database(tmp_path, floor=0)
  con = sqlite3.connect(path)
  con.execute("PRAGMA journal_mode=WAL")
  con.execute("PRAGMA wal_autocheckpoint=0")
  con.execute("UPDATE platform_compat SET floor = 4 WHERE id = 1")
  con.commit()
  try:
    assert Path(f"{path}-wal").exists() and Path(f"{path}-shm").exists()
    assert _probe(path) == (0, "floor=4")
  finally:
    con.close()


# ── deploy-prod.sh ──────────────────────────────────────────────────────


def _deploy_rollback_source() -> str:
  source = DEPLOY.read_text(encoding="utf-8")
  start = source.index("# ── rollback preflight\n")
  end = source.index("# Wait for a live-container probe", start)
  return source[start:end]


def _run_deploy_rollback(
  tmp_path: Path, *, data: Path, compat: str = "REQUIRED_IMAGE_LEVEL = 0; COMPAT_LEVEL = 0",
  ready: list[str] | None = None, running_after_stop: str = "",
  data_mount: str = "type=volume,src=mobius_app_data,dst=/data,readonly",
  wait: int = 3,
) -> tuple[subprocess.CompletedProcess, list[str]]:
  """Run attempt_rollback against a docker stub.

  The stub executes the real floor probe with this Python against ``data``
  standing in for the read-only /data mount. ``ready`` lists successive
  readiness answers (body, newline, status).
  """
  log = tmp_path / "docker.log"
  answers = tmp_path / "ready"
  answers.mkdir()
  for index, answer in enumerate(ready or ['{"ready":true,"boot_id":"b"}\n200']):
    (answers / f"{index:03d}").write_text(answer, encoding="utf-8")
  harness = _deploy_rollback_source() + textwrap.dedent(f"""\
    warn() {{ printf 'WARN %s\\n' "$1" >&2; }}
    intent() {{ :; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    ok() {{ printf 'OK %s\\n' "$1"; }}
    external_prod_caddy_running() {{ return 1; }}
    rearm_chat_cutover() {{ printf 'rearm\\n' >> {str(log)!r}; }}
    finalize_chat_cutover() {{ printf 'finalize\\n' >> {str(log)!r}; }}
    sleep() {{ :; }}
    docker() {{
      case "$1" in
        stop|start) printf '%s %s\\n' "$1" "$2" >> {str(log)!r} ;;
        ps) printf 'ps %s\\n' "$*" >> {str(log)!r}; printf '%s' {running_after_stop!r} ;;
        run)
          if [ "$6" = cat ]; then
            printf 'run-cat %s %s\\n' "$7" "$8" >> {str(log)!r}
            printf '%s' {compat!r}
            return 0
          fi
          printf 'run-probe %s %s %s\\n' "$5" "$6" "$9" >> {str(log)!r}
          local db="${{13}}"
          {sys.executable!r} -I -c "${{12}}" {str(data)!r}"${{db#/data}}"
          ;;
        exec)
          printf 'exec %s\\n' "$*" >> {str(log)!r}
          local next
          next=$(ls {str(answers)!r} | head -n 1)
          if [ -n "$next" ]; then
            cat {str(answers)!r}/"$next"
            [ "$(ls {str(answers)!r} | wc -l)" -gt 1 ] && rm {str(answers)!r}/"$next"
          fi
          ;;
        *) printf '%s\\n' "$*" >> {str(log)!r} ;;
      esac
      return 0
    }}
    DEPLOY_SUPPORT={str(SCRIPTS / "deploy_support.py")!r}
    DATA_MOUNT={data_mount!r}
    PREV_IMAGE=sha256:old-image
    IMAGE_TAG=mobius:prod
    CONTAINER=mobius
    CUTOVER_WAIT_SECONDS={wait}
    INTERNAL_BASE=http://localhost:8000
    COMPOSE_ARGS=(-p mobius)
    attempt_rollback
  """)
  result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
  calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
  return result, calls


def _index(calls: list[str], prefix: str) -> int:
  return next(i for i, call in enumerate(calls) if call.startswith(prefix))


def test_deploy_rollback_happy_path_is_fenced_then_validated_by_readiness(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=0)
  result, calls = _run_deploy_rollback(tmp_path, data=data)

  assert result.returncode == 0, result.stderr
  assert "rollback preflight: previous image level 0 ≥ database floor 0" in result.stdout
  assert "serviceable on the previous image again" in result.stdout
  # Fence, then the image's level, then the floor, then the replacement.
  stop = _index(calls, "stop mobius")
  probe = _index(calls, "run-probe")
  tag = _index(calls, "tag sha256:old-image mobius:prod")
  assert stop < _index(calls, "ps ") < _index(calls, "run-cat") < probe < tag
  assert _index(calls, "rearm") > probe
  assert calls[tag + 1] == "compose -p mobius up -d --force-recreate"
  assert calls[_index(calls, "run-cat")] == (
    "run-cat sha256:old-image /app/platform-baked/backend/app/compat.py"
  )
  assert calls[probe] == (
    "run-probe --mount type=volume,src=mobius_app_data,dst=/data,readonly sha256:old-image"
  )
  # Success is judged by the readiness payload, not /api/health.
  readiness = [call for call in calls if call.startswith("exec ")]
  assert readiness and all("/api/ready" in call for call in readiness)
  assert not any("/api/health" in call for call in calls)
  assert calls[-1] == "finalize"


def test_deploy_rollback_refuses_an_image_below_the_floor(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=2)
  result, calls = _run_deploy_rollback(tmp_path, data=data, compat="COMPAT_LEVEL = 1")

  assert result.returncode == 1
  assert "a newer version is required" in result.stderr
  assert "level 1; the database floor is 2" in result.stderr
  assert not any(call.startswith(("tag ", "compose ", "rearm")) for call in calls)
  assert calls[-1] == "start mobius"


def test_deploy_rollback_allows_an_image_at_the_floor(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=1)
  result, calls = _run_deploy_rollback(tmp_path, data=data, compat="COMPAT_LEVEL = 1")

  assert result.returncode == 0, result.stderr
  assert "previous image level 1 ≥ database floor 1" in result.stdout
  assert any(call.startswith("tag ") for call in calls)


def test_deploy_rollback_treats_a_missing_baked_compat_as_level_zero(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=1)
  result, calls = _run_deploy_rollback(tmp_path, data=data, compat="")

  assert result.returncode == 1
  assert "level 0; the database floor is 1" in result.stderr
  assert not any(call.startswith("tag ") for call in calls)


def test_deploy_rollback_allows_a_legacy_database_without_the_floor_table(tmp_path):
  data = tmp_path / "data"
  _database(data)
  result, calls = _run_deploy_rollback(tmp_path, data=data)

  assert result.returncode == 0, result.stderr
  assert any(call.startswith("tag ") for call in calls)


@pytest.mark.parametrize("case", ["wal_without_shm", "no_ledger", "no_database", "no_mount"])
def test_deploy_rollback_fails_closed_when_the_floor_is_unreadable(tmp_path, case):
  data = tmp_path / "data"
  mount = "type=volume,src=mobius_app_data,dst=/data,readonly"
  if case == "wal_without_shm":
    path = _database(data, floor=0)
    Path(f"{path}-wal").write_bytes(b"\0" * 64)
  elif case == "no_ledger":
    _database(data, ledger=False)
  elif case == "no_mount":
    _database(data, floor=0)
    mount = ""
  result, calls = _run_deploy_rollback(tmp_path, data=data, data_mount=mount)

  assert result.returncode == 1
  assert "could not be read safely" in result.stderr
  assert not any(call.startswith(("tag ", "compose ")) for call in calls)
  assert calls[-1] == "start mobius"


def test_deploy_rollback_refuses_when_the_failed_container_keeps_running(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=0)
  result, calls = _run_deploy_rollback(tmp_path, data=data, running_after_stop="abc123\n")

  assert result.returncode == 1
  assert "could not stop the failed mobius" in result.stderr
  assert not any(call.startswith(("run-", "tag ", "compose ")) for call in calls)


def test_deploy_rollback_floor_verdict_is_terminal(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=0)
  refused = json.dumps({
    "ready": False, "reason": "below_compatibility_floor", "floor": 3,
    "compat_level": 1, "boot_id": "b",
  }, separators=(",", ":"))
  result, calls = _run_deploy_rollback(
    tmp_path, data=data, ready=[f"{refused}\n503"], wait=30,
  )

  assert result.returncode == 1
  assert "a newer version is required" in result.stderr
  assert '"floor":3' in result.stderr
  assert "rolled back" not in result.stdout
  # One answer suffices; no further polling, no second image, no finalize.
  assert len([call for call in calls if call.startswith("exec ")]) == 1
  assert len([call for call in calls if call.startswith("tag ")]) == 1
  assert "finalize" not in calls


def test_deploy_rollback_other_readiness_failures_keep_waiting(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=0)
  degraded = '{"ready":false,"reason":"schema_mismatch"}\n503'
  result, calls = _run_deploy_rollback(
    tmp_path, data=data, ready=[degraded, degraded, '{"ready":true}\n200'], wait=5,
  )

  assert result.returncode == 0, result.stderr
  assert len([call for call in calls if call.startswith("exec ")]) == 3


def test_deploy_rollback_does_not_count_health_as_serviceable(tmp_path):
  data = tmp_path / "data"
  _database(data, floor=0)
  result, _calls = _run_deploy_rollback(
    tmp_path, data=data, ready=['{"ready":false,"reason":"writer_unavailable"}\n503'],
  )

  assert result.returncode == 1
  assert "did not become serviceable" in result.stderr


def test_deploy_data_mount_is_captured_read_only():
  source = DEPLOY.read_text(encoding="utf-8")
  start = source.index("data_mount_spec() {")
  end = source.index("DATA_MOUNT=$(", start)
  for mounts, expected in [
    ("volume|mobius_app_data|/var/lib/docker/volumes/x", "type=volume,src=mobius_app_data,dst=/data,readonly"),
    ("bind||/srv/mobius", "type=bind,src=/srv/mobius,dst=/data,readonly"),
    ("", ""),
    ("bind||/srv/a,b", ""),
  ]:
    harness = source[start:end] + textwrap.dedent(f"""\
      docker() {{ printf '%s' {mounts!r}; }}
      CONTAINER=mobius
      data_mount_spec || true
    """)
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert result.stdout.strip() == expected
  # Captured before cutover, from the known-good container.
  assert source.index("DATA_MOUNT=$(data_mount_spec") < source.index("attempt_rollback() {")


# ── mobius-rebuild-host.py ──────────────────────────────────────────────


class _Docker:
  """A subprocess.run stand-in: records docker calls, runs the real probe."""

  def __init__(self, data: Path, *, compat: str = "COMPAT_LEVEL = 0\n",
               ready: list[dict] | None = None, running_after_stop: str = ""):
    self.data = data
    self.compat = compat
    self.ready = list(ready or [{"ready": True, "boot_id": "b"}])
    self.running_after_stop = running_after_stop
    self.calls: list[str] = []
    self.running = True

  def __call__(self, args, **_kwargs):
    def done(stdout: str = "", code: int = 0):
      return subprocess.CompletedProcess(args, code, stdout, "")

    if args[:2] == ["docker", "compose"]:
      verb = args[args.index(str(host.OVERRIDE)) + 1:]
      self.calls.append("compose " + " ".join(verb))
      if verb[:1] == ["ps"]:
        return done("cid\n" if self.running else self.running_after_stop)
      if verb[:1] == ["up"]:
        self.running = True
      return done()
    if args[:2] == ["docker", "run"]:
      if "cat" in args:
        self.calls.append(f"run-cat {args[-2]} {args[-1]}")
        return done(self.compat)
      mount = args[args.index("--mount") + 1]
      self.calls.append(f"run-probe {mount}")
      probe = args[args.index("-c") + 1]
      database = str(self.data) + args[-1].removeprefix("/data")
      return _REAL_RUN(
        [sys.executable, "-I", "-c", probe, database], capture_output=True, text=True,
      )
    if args[:2] == ["docker", "exec"]:
      self.calls.append("exec " + " ".join(args[2:]))
      answer = self.ready[0] if len(self.ready) == 1 else self.ready.pop(0)
      return done(json.dumps(answer))
    self.calls.append(" ".join(args))
    if args[:2] == ["docker", "stop"]:
      self.running = False
    if args[:2] == ["docker", "start"]:
      self.running = True
    return done()


def _rebuild_rollback(tmp_path: Path, monkeypatch, docker: _Docker, image: str = "new"):
  data = docker.data
  config = {"project": "mobius", "control_dir": tmp_path / "control", "data_dir": data}
  monkeypatch.setattr(host.subprocess, "run", docker)
  monkeypatch.setattr(host.time, "sleep", lambda _seconds: None)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", image))
  monkeypatch.setattr(host, "clear_transaction", lambda: None)
  ledger = []
  monkeypatch.setattr(
    host, "restart_ledger",
    lambda _config, _cid, command, _operation, **_kwargs:
      ledger.append(command) or docker.calls.append(f"ledger {command}") or True,
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  code = host.rollback(config, "a" * 32, "b" * 40, "health_check_failed", "unhealthy")
  return code, statuses


def test_rebuild_rollback_happy_path_is_fenced_then_validated_by_readiness(
  tmp_path, monkeypatch,
):
  docker = _Docker(tmp_path / "data")
  _database(docker.data, floor=0)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker)

  assert code == 1
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "health_check_failed"
  calls = docker.calls
  up = _index(calls, "compose up")
  assert (
    _index(calls, "docker stop cid") < _index(calls, "compose ps")
    < _index(calls, "run-cat") < _index(calls, "run-probe")
    < _index(calls, "ledger rearm-cutover") < up
  )
  assert calls[_index(calls, "run-cat")] == f"run-cat {host.ROLLBACK_TAG} {host.BAKED_COMPAT_PATH}"
  assert calls[_index(calls, "run-probe")] == (
    f"run-probe type=bind,src={docker.data},dst=/data,readonly"
  )
  readiness = [call for call in calls[up:] if call.startswith("exec ")]
  assert readiness and all(call.endswith(host.READY_URL) for call in readiness)


def test_rebuild_rollback_refuses_an_image_below_the_floor(tmp_path, monkeypatch):
  """The previous image is never started; the new release is settled forward."""
  docker = _Docker(tmp_path / "data", compat="COMPAT_LEVEL = 1\n")
  _database(docker.data, floor=2)
  target = "sha256:" + "9" * 64
  monkeypatch.setattr(host, "read_transaction", lambda: {"target_image": target})
  monkeypatch.setattr(host, "inspect_container_image", lambda cid: target)
  monkeypatch.setattr(host, "verify_served_generation", lambda cid, sha: None)
  monkeypatch.setattr(host, "adopt_from_image", lambda image: "kept")
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker, image=target)

  assert code == 0
  assert statuses[-1]["state"] == "succeeded"
  assert not any(call.startswith("compose up") for call in docker.calls)
  start = docker.calls.index("docker start cid")
  assert docker.calls.index("ledger rearm-cutover") < start
  assert docker.calls.index("ledger finalize-cutover") > start


def test_rebuild_rollback_below_the_floor_starts_nothing_it_cannot_identify(
  tmp_path, monkeypatch,
):
  docker = _Docker(tmp_path / "data", compat="COMPAT_LEVEL = 1\n")
  _database(docker.data, floor=2)
  monkeypatch.setattr(host, "read_transaction", lambda: None)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker)

  assert code == 1
  assert statuses[-1]["state"] == "needs_recovery"
  assert statuses[-1]["code"] == "newer_version_required"
  assert "cannot run on this database" in statuses[-1]["message"]
  assert not any(call.startswith(("compose up", "ledger", "docker start")) for call in docker.calls)


def test_rebuild_rollback_missing_table_is_zero_only_with_the_ledger(
  tmp_path, monkeypatch,
):
  legacy = _Docker(tmp_path / "legacy")
  _database(legacy.data)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, legacy)
  assert statuses[-1]["state"] == "rolled_back"

  foreign = _Docker(tmp_path / "foreign")
  _database(foreign.data, ledger=False)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, foreign)
  assert code == 1
  assert statuses[-1]["state"] == "needs_recovery"
  assert statuses[-1]["code"] == "rollback_preflight_failed"
  assert "not_a_mobius_database" in statuses[-1]["message"]
  assert not any(call.startswith("compose up") for call in foreign.calls)


def test_rebuild_rollback_fails_closed_on_wal_without_shm(tmp_path, monkeypatch):
  docker = _Docker(tmp_path / "data")
  path = _database(docker.data, floor=0)
  Path(f"{path}-wal").write_bytes(b"\0" * 64)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker)

  assert code == 1
  assert statuses[-1]["code"] == "rollback_preflight_failed"
  assert "wal_without_shm" in statuses[-1]["message"]
  assert not any(call.startswith("compose up") for call in docker.calls)


def test_rebuild_rollback_refuses_when_the_failed_container_keeps_running(
  tmp_path, monkeypatch,
):
  docker = _Docker(tmp_path / "data", running_after_stop="cid\n")
  _database(docker.data, floor=0)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker)

  assert code == 1
  assert statuses[-1]["code"] == "rollback_preflight_failed"
  assert not any(call.startswith(("run-", "compose up")) for call in docker.calls)


def test_rebuild_rollback_floor_verdict_is_terminal(tmp_path, monkeypatch):
  docker = _Docker(tmp_path / "data", ready=[{
    "ready": False, "reason": "below_compatibility_floor", "floor": 3,
    "compat_level": 1, "boot_id": "b",
  }])
  _database(docker.data, floor=0)
  code, statuses = _rebuild_rollback(tmp_path, monkeypatch, docker)

  assert code == 1
  assert statuses[-1]["state"] == "needs_recovery"
  assert statuses[-1]["code"] == "newer_version_required"
  assert "(level 3)" in statuses[-1]["message"]
  assert len([call for call in docker.calls if call.startswith("exec ")]) == 1
  assert len([call for call in docker.calls if call.startswith("compose up")]) == 1
  assert "ledger finalize-cutover" not in docker.calls


def test_rebuild_wait_ready_keeps_waiting_through_other_failures(monkeypatch):
  docker = _Docker(Path("/nonexistent"), ready=[
    {"ready": False, "reason": "schema_mismatch"},
    {"ready": True},
  ])
  monkeypatch.setattr(host.subprocess, "run", docker)
  monkeypatch.setattr(host.time, "sleep", lambda _seconds: None)
  config = {"project": "mobius"}
  assert host.wait_ready(config, 60) == ("ready", {"ready": True})


def test_rebuild_wait_ready_times_out_without_a_ready_payload(monkeypatch):
  docker = _Docker(Path("/nonexistent"), ready=[{"ready": False, "reason": "x"}])
  clock = iter(range(0, 1000, 50))
  monkeypatch.setattr(host.subprocess, "run", docker)
  monkeypatch.setattr(host.time, "sleep", lambda _seconds: None)
  monkeypatch.setattr(host.time, "monotonic", lambda: next(clock))
  assert host.wait_ready({"project": "mobius"}, 120) == ("timeout", None)


def test_a_floor_table_without_its_row_fails_closed(tmp_path):
  """The table and its row are created together, so a lone table is damage."""
  path = _database(tmp_path)
  con = sqlite3.connect(path)
  con.execute("CREATE TABLE platform_compat (id INTEGER PRIMARY KEY, floor INTEGER NOT NULL)")
  con.commit()
  con.close()
  assert _probe(path) == (3, "error=floor_row_missing")


def test_an_active_step_is_a_floor_even_without_the_floor_table(tmp_path):
  """A partial restore that lost the floor table must not look safe."""
  path = _database(tmp_path)
  con = sqlite3.connect(path)
  con.execute("CREATE TABLE platform_upgrades (level INTEGER PRIMARY KEY, state TEXT)")
  con.executemany(
    "INSERT INTO platform_upgrades VALUES (?, ?)", [(1, "active"), (2, "active"), (3, "preparing")],
  )
  con.commit()
  con.close()
  assert _probe(path) == (0, "floor=2")


def test_the_higher_of_row_and_active_steps_wins(tmp_path):
  path = _database(tmp_path, floor=4)
  con = sqlite3.connect(path)
  con.execute("CREATE TABLE platform_upgrades (level INTEGER PRIMARY KEY, state TEXT)")
  con.execute("INSERT INTO platform_upgrades VALUES (2, 'active')")
  con.commit()
  con.close()
  assert _probe(path) == (0, "floor=4")
