"""Revocable process-group leases for scheduled and on-demand app jobs."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest
from jose import jwt

from app import app_jobs, models
from app.config import get_settings
from app.install import _crontab_command_path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_pins_landlock_capable_setpriv_release():
  dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

  assert "FROM node:24-trixie-slim AS node-runtime" in dockerfile
  assert "FROM python:3.12-slim-trixie" in dockerfile
  assert "setpriv --help 2>&1 | grep -q -- '--landlock-access'" in dockerfile


def test_cron_parser_resolves_supervised_command_to_real_job():
  line = (
    "30 5 * * * python3 /app/scripts/app-job-runner.py "
    "57 /data/apps/memory/memory-job.sh"
  )
  assert _crontab_command_path(line) == (
    "/data/apps/memory/memory-job.sh"
  )


def test_only_bootstrap_commands_request_a_readiness_wait():
  job = Path("/data/apps/memory/memory-job.sh")

  assert app_jobs.runner_command(57, job, wait_for_ready=True)[-3:] == [
    "--wait-for-ready", "57", str(job),
  ]
  assert app_jobs.runner_command(57, job)[-2:] == ["57", str(job)]


def test_direct_launch_passes_the_configured_backend_address(monkeypatch, tmp_path):
  source = tmp_path / "memory"
  source.mkdir()
  calls = []
  monkeypatch.setattr(
    app_jobs.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)),
  )

  app_jobs.launch_app_job(57, source / "fetch.sh", source)

  assert calls[0][1]["env"]["API_BASE_URL"] == get_settings().api_base_url


def test_terminate_verifies_start_ticks_before_signalling(monkeypatch):
  data_dir = Path(get_settings().data_dir)
  leases = data_dir / "run" / "app-jobs" / "57"
  leases.mkdir(parents=True)
  (leases / "live.json").write_text(json.dumps({
    "pid": 123,
    "start_ticks": 999,
  }))
  (leases / "reused.json").write_text(json.dumps({
    "pid": 456,
    "start_ticks": 111,
  }))
  ticks = {123: 999, 456: 222}
  monkeypatch.setattr(app_jobs, "_start_ticks", lambda pid: ticks.get(pid))
  signals = []
  monkeypatch.setattr(
    app_jobs.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
  )

  assert app_jobs.terminate_app_jobs(57, grace_seconds=0) == 1
  assert signals == [(123, signal.SIGTERM), (123, signal.SIGKILL)]
  assert not (leases / "reused.json").exists()


def test_terminate_does_not_kill_reused_pid_after_term(monkeypatch):
  data_dir = Path(get_settings().data_dir)
  leases = data_dir / "run" / "app-jobs" / "58"
  leases.mkdir(parents=True)
  lease = leases / "job.json"
  lease.write_text(json.dumps({"pid": 789, "start_ticks": 10}))
  observed = iter((10, 20, 20))
  monkeypatch.setattr(app_jobs, "_start_ticks", lambda _pid: next(observed, 20))
  signals = []
  monkeypatch.setattr(
    app_jobs.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
  )

  assert app_jobs.terminate_app_jobs(58, grace_seconds=0.1) == 1

  assert signals == [(789, signal.SIGTERM)]
  assert not lease.exists()


def _load_runner():
  path = Path(__file__).resolve().parent.parent / "scripts" / "app-job-runner.py"
  spec = importlib.util.spec_from_file_location("app_job_runner", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_live_check_calls_real_app_endpoint(monkeypatch):
  runner = _load_runner()
  seen = {}

  class Response:
    status = 200

    def __enter__(self):
      return self

    def __exit__(self, *_args):
      return False

  def urlopen(request, timeout):
    seen["url"] = request.full_url
    seen["auth"] = request.headers["Authorization"]
    seen["timeout"] = timeout
    return Response()

  monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)

  assert runner._app_is_live(57, "app-token") is True
  assert seen == {
    "url": f"{runner.API_BASE_URL}/api/apps/57",
    "auth": "Bearer app-token",
    "timeout": 10,
  }


def test_bootstrap_waits_for_ready_before_minting_a_job_token(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "memory-job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  events = []
  monkeypatch.setattr(
    runner, "_wait_for_ready", lambda: events.append("ready") or True,
  )
  monkeypatch.setattr(
    runner, "_mint_app_token", lambda _app_id: events.append("mint") or "token",
  )
  monkeypatch.setattr(runner, "_app_is_live", lambda *_args: True)
  monkeypatch.setattr(
    runner, "_job_context", lambda *_args: {"source_dir": str(source)},
  )
  monkeypatch.setattr(
    runner.subprocess, "Popen", lambda *_args, **_kwargs: types.SimpleNamespace(wait=lambda: 0),
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "--wait-for-ready", "57", str(job),
  ])

  assert runner.run() == 0
  assert events == ["ready", "mint"]


def test_bootstrap_readiness_timeout_never_mints_a_job_token(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "memory-job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  monkeypatch.setattr(runner, "_wait_for_ready", lambda: False)
  minted = []
  monkeypatch.setattr(runner, "_mint_app_token", lambda _app_id: minted.append(True))
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "--wait-for-ready", "57", str(job),
  ])

  assert runner.run() == 4
  assert minted == []


def test_wrapper_publishes_lease_before_live_check_and_cleans_it(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "memory-job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "_mint_app_token", lambda app_id: "app-token")
  monkeypatch.setattr(runner, "_app_is_live", lambda app_id, token=None: False)
  monkeypatch.setattr(runner, "_job_context", lambda app_id, token: {})
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  seen_lease = []
  original = runner._app_is_live

  def check_after_publication(app_id, token=None):
    seen_lease.extend(
      (data_dir / "run" / "app-jobs" / str(app_id)).glob("*.json")
    )
    return original(app_id, token)

  monkeypatch.setattr(runner, "_app_is_live", check_after_publication)
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "57", str(job),
  ])

  assert runner.run() == 4
  assert len(seen_lease) == 1
  assert not seen_lease[0].exists()


def test_wrapper_runs_job_only_after_live_check(tmp_path, monkeypatch):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "memory-job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "_mint_app_token", lambda app_id: "app-token")
  monkeypatch.setattr(
    runner, "_app_is_live", lambda app_id, token=None: True,
  )
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  monkeypatch.setattr(
    runner,
    "_job_context",
    lambda app_id, token: {"source_dir": str(source)},
  )
  popen = types.SimpleNamespace(wait=lambda: 0)
  calls = []
  monkeypatch.setattr(
    runner.subprocess,
    "Popen",
    lambda *args, **kwargs: calls.append((args, kwargs)) or popen,
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "57", str(job),
  ])

  assert runner.run() == 0
  assert calls[0][0][0] == ["bash", str(job.resolve()), "57"]
  child_env = calls[0][1]["env"]
  assert child_env["APP_TOKEN"] == "app-token"
  assert child_env["APP_JOB_STATE_DIR"].endswith("/apps/57/job-state")
  assert "SERVICE_TOKEN" not in child_env
  assert "AGENT_TOKEN" not in child_env


def test_wrapper_rejects_a_job_from_another_live_app(tmp_path, monkeypatch):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  memory_source = data_dir / "apps" / "memory"
  reflection_source = data_dir / "apps" / "reflection"
  memory_source.mkdir(parents=True)
  reflection_source.mkdir()
  job = memory_source / "fetch.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "_mint_app_token", lambda app_id: "app-token")
  monkeypatch.setattr(
    runner, "_app_is_live", lambda app_id, token=None: True,
  )
  monkeypatch.setattr(
    runner,
    "_job_context",
    lambda app_id, token: {"source_dir": str(reflection_source)},
  )
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  calls = []
  monkeypatch.setattr(
    runner.subprocess,
    "Popen",
    lambda *args, **kwargs: calls.append((args, kwargs)),
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "56", str(job),
  ])

  assert runner.run() == 4
  assert calls == []


def test_wrapper_rejects_job_context_without_exact_app_identity(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  source = tmp_path / "data" / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "fetch.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", tmp_path / "data")
  monkeypatch.setattr(runner, "_mint_app_token", lambda app_id: "app-token")
  monkeypatch.setattr(
    runner, "_app_is_live", lambda app_id, token=None: True,
  )
  monkeypatch.setattr(runner, "_job_context", lambda app_id, token: {})
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  calls = []
  monkeypatch.setattr(
    runner.subprocess,
    "Popen",
    lambda *args, **kwargs: calls.append((args, kwargs)),
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "56", str(job),
  ])

  assert runner.run() == 4
  assert calls == []


def test_background_agent_policy_contains_only_declared_data_scope(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "fetch.sh"
  job.write_text("#!/bin/sh\n")
  (data_dir / "cli-auth" / "claude").mkdir(parents=True)
  (data_dir / "cli-auth" / "codex").mkdir(parents=True)
  (data_dir / "cli-auth" / "unreviewed-provider").mkdir(parents=True)
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  context = {
    "primary": {"provider": "claude"},
    "fallback": None,
    "capability_contract": {
      "background": {"agent": True},
      "data": {"shared_memory": "write"},
    },
  }

  policy = runner._job_access(57, job.resolve(), context)

  assert policy.source_read == source
  assert policy.storage_write == data_dir / "apps" / "57"
  assert set(policy.extra_write) == {
    data_dir / "shared" / "memory",
    data_dir / "cli-auth" / "claude",
    data_dir / "cli-auth" / "codex",
  }
  assert data_dir / "cli-auth" / "unreviewed-provider" not in policy.extra_write
  assert data_dir / "service-token.txt" not in policy.extra_write
  assert data_dir / "db" not in policy.extra_write


def test_runner_records_executor_and_cleans_job_home(tmp_path, monkeypatch):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "fetch.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "_mint_app_token", lambda _app_id: "app-token")
  monkeypatch.setattr(runner, "_app_is_live", lambda *_args: True)
  monkeypatch.setattr(
    runner, "_job_context", lambda *_args: {
      "source_dir": str(source),
      "capability_contract": {
        "background": {"agent": True},
        "data": {"shared_memory": "none"},
      },
    },
  )
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  homes = []

  def select(_policy, command, env, home):
    homes.append(home)
    probe = types.SimpleNamespace(
      executor="bubblewrap", available=True, detail="passed",
    )
    return types.SimpleNamespace(
      executor="bubblewrap", command=command, env=env,
    ), (probe,)

  monkeypatch.setattr(runner, "select_executor", select)

  class Child:
    def wait(self):
      leases = list(
        (data_dir / "run" / "app-jobs" / "57").glob("*.json")
      )
      assert len(leases) == 1
      assert json.loads(leases[0].read_text())["executor"] == "bubblewrap"
      assert homes[0].is_dir()
      return 0

  monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: Child())
  monkeypatch.setattr(
    runner.sys, "argv", ["app-job-runner.py", "57", str(job)],
  )

  assert runner.run() == 0
  assert len(homes) == 1
  assert not homes[0].exists()


@pytest.mark.parametrize("executor", ["bubblewrap", "landlock"])
def test_secure_executors_enforce_the_same_data_contract(executor, monkeypatch):
  runner = _load_runner()
  sandbox = importlib.import_module("app_job_sandbox")
  executor_probe = (
    sandbox.probe_bubblewrap()
    if executor == "bubblewrap"
    else sandbox.probe_landlock()
  )
  if not executor_probe.available:
    pytest.skip(executor_probe.detail)
  # Keep the fake owner data outside the helper's read-only runtime roots.
  temp_root = "/var/tmp" if executor == "bubblewrap" else "/tmp"
  with tempfile.TemporaryDirectory(dir=temp_root) as raw:
    data_dir = Path(raw) / "data"
    source = data_dir / "apps" / "memory"
    source.mkdir(parents=True)
    storage = data_dir / "apps" / "57"
    storage.mkdir()
    shared = data_dir / "shared" / "memory"
    shared.mkdir(parents=True)
    (shared / "fact.txt").write_text("visible", encoding="utf-8")
    (data_dir / "db").mkdir()
    (data_dir / "service-token.txt").write_text("owner-secret", encoding="utf-8")
    job = source / "fetch.sh"
    job.write_text(
      "#!/bin/sh\n"
      "cat \"$DATA_DIR/service-token.txt\" >/dev/null 2>&1 && exit 21\n"
      "ls \"$DATA_DIR/db\" >/dev/null 2>&1 && exit 22\n"
      "test \"$(cat \"$DATA_DIR/shared/memory/fact.txt\")\" = visible || exit 23\n"
      "printf escaped >\"$DATA_DIR/outside.txt\" 2>/dev/null && exit 24\n"
      "printf temporary >\"$HOME/temp-proof.txt\" || exit 25\n"
      "printf confined >\"$DATA_DIR/apps/57/proof.txt\"\n",
      encoding="utf-8",
    )
    if os.geteuid() == 0:
      # Match production's /data ownership before the sandbox drops root.
      for path in (
        Path(raw), data_dir, data_dir / "apps", source, storage,
        data_dir / "shared", shared,
      ):
        os.chown(path, 1000, 1000)
    monkeypatch.setattr(runner, "DATA_DIR", data_dir)
    context = {
      "primary": None,
      "fallback": None,
      "capability_contract": {
        "background": {"agent": True},
        "data": {"shared_memory": "write"},
      },
    }

    policy = runner._job_access(57, job.resolve(), context)
    sandbox_home = Path(tempfile.mkdtemp(prefix="mobius-job-test-"))
    if os.geteuid() == 0:
      os.chown(sandbox_home, 1000, 1000)
    if executor == "bubblewrap":
      monkeypatch.setattr(sandbox, "probe_bubblewrap", lambda: executor_probe)
    else:
      monkeypatch.setattr(
        sandbox, "probe_bubblewrap",
        lambda: sandbox.ExecutorProbe("bubblewrap", False, "test fallback"),
      )
    launch, probes = sandbox.select_executor(
      policy,
      ["bash", str(job.resolve()), "57"],
      {"PATH": os.environ.get("PATH", ""), "DATA_DIR": str(data_dir)},
      sandbox_home,
    )
    assert launch is not None
    assert launch.executor == executor
    try:
      result = subprocess.run(
        launch.command,
        env=launch.env,
        capture_output=True,
        text=True,
        timeout=20,
      )
      assert result.returncode == 0, result.stderr
      assert (storage / "proof.txt").read_text(encoding="utf-8") == "confined"
      assert (storage / "proof.txt").stat().st_uid == 1000
      assert Path(launch.env["HOME"]) == sandbox_home
    finally:
      shutil.rmtree(sandbox_home, ignore_errors=True)


def test_landlock_fallback_scopes_processes_and_unix_sockets(monkeypatch):
  runner = _load_runner()
  sandbox = importlib.import_module("app_job_sandbox")
  landlock_probe = sandbox.probe_landlock()
  if not landlock_probe.available:
    pytest.skip(landlock_probe.detail)
  with tempfile.TemporaryDirectory(dir="/tmp") as raw:
    data_dir = Path(raw) / "data"
    source = data_dir / "apps" / "memory"
    storage = data_dir / "apps" / "57"
    source.mkdir(parents=True)
    storage.mkdir()
    probe = source / "probe.py"
    probe.write_text(
      "import os, socket, sys\n"
      "try:\n"
      "  os.kill(int(os.environ['TARGET_PID']), 0)\n"
      "except PermissionError:\n"
      "  pass\n"
      "else:\n"
      "  raise SystemExit(31)\n"
      "try:\n"
      "  open(f\"/proc/{os.environ['TARGET_PID']}/environ\", 'rb').read()\n"
      "except PermissionError:\n"
      "  pass\n"
      "else:\n"
      "  raise SystemExit(32)\n"
      "try:\n"
      "  open(f\"/proc/{os.environ['TARGET_PID']}/cmdline\", 'rb').read()\n"
      "except PermissionError:\n"
      "  pass\n"
      "else:\n"
      "  raise SystemExit(34)\n"
      "try:\n"
      "  socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
      "except PermissionError:\n"
      "  pass\n"
      "else:\n"
      "  raise SystemExit(33)\n"
      "open(os.environ['PROOF'], 'w').write('scoped')\n",
      encoding="utf-8",
    )
    if os.geteuid() == 0:
      for path in (Path(raw), data_dir, data_dir / "apps", source, storage):
        os.chown(path, 1000, 1000)
    target_env = dict(os.environ)
    target_env["LANDLOCK_PARENT_SECRET"] = "not-readable"
    target = subprocess.Popen(["sleep", "20"], env=target_env)
    sandbox_home = Path(tempfile.mkdtemp(prefix="mobius-job-test-"))
    if os.geteuid() == 0:
      os.chown(sandbox_home, 1000, 1000)
    monkeypatch.setattr(runner, "DATA_DIR", data_dir)
    policy = runner._job_access(57, probe.resolve(), {
      "capability_contract": {
        "background": {"agent": True},
        "data": {"shared_memory": "none"},
      },
    })
    monkeypatch.setattr(
      sandbox, "probe_bubblewrap",
      lambda: sandbox.ExecutorProbe("bubblewrap", False, "test fallback"),
    )
    launch, _probes = sandbox.select_executor(
      policy,
      ["python3", str(probe.resolve())],
      {
        "PATH": os.environ.get("PATH", ""),
        "DATA_DIR": str(data_dir),
        "TARGET_PID": str(target.pid),
        "PROOF": str(storage / "process-proof.txt"),
      },
      sandbox_home,
    )
    assert launch is not None
    try:
      result = subprocess.run(
        launch.command,
        env=launch.env,
        capture_output=True,
        text=True,
        timeout=10,
      )
      assert result.returncode == 0, result.stderr
      assert (storage / "process-proof.txt").read_text() == "scoped"
      assert target.poll() is None
    finally:
      target.terminate()
      target.wait(timeout=5)
      shutil.rmtree(sandbox_home, ignore_errors=True)


def test_landlock_child_dies_when_its_supervisor_exits():
  sandbox = importlib.import_module("app_job_sandbox")
  landlock_probe = sandbox.probe_landlock()
  if not landlock_probe.available:
    pytest.skip(landlock_probe.detail)
  with tempfile.TemporaryDirectory(dir="/tmp") as raw:
    root = Path(raw)
    source = root / "source"
    storage = root / "storage"
    home = root / "home"
    source.mkdir()
    storage.mkdir()
    home.mkdir()
    pidfile = root / "child.pid"
    launcher = (
      "import os, subprocess\n"
      "from pathlib import Path\n"
      "from app_job_sandbox import JobAccess, _landlock_plan\n"
      f"source=Path({str(source)!r})\n"
      f"storage=Path({str(storage)!r})\n"
      f"home=Path({str(home)!r})\n"
      "plan=_landlock_plan(JobAccess(source, storage), ['sleep', '30'], "
      "dict(os.environ), home)\n"
      "child=subprocess.Popen(plan.command, env=plan.env)\n"
      f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
    )
    env = dict(os.environ)
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    env["PYTHONPATH"] = scripts
    parent = subprocess.run(
      [sys.executable, "-c", launcher],
      env=env,
      capture_output=True,
      text=True,
      timeout=10,
    )
    assert parent.returncode == 0, parent.stderr
    child_pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
      stat = Path(f"/proc/{child_pid}/stat")
      if not stat.exists():
        break
      if stat.read_text().split()[2] == "Z":
        break
      time.sleep(0.05)
    else:
      os.kill(child_pid, signal.SIGKILL)
      pytest.fail("Landlock child survived its supervisor")


def test_executor_selection_is_capability_based(tmp_path, monkeypatch):
  sandbox = importlib.import_module("app_job_sandbox")
  source = tmp_path / "source"
  storage = tmp_path / "storage"
  home = storage / "home"
  source.mkdir()
  home.mkdir(parents=True)
  policy = sandbox.JobAccess(source_read=source, storage_write=storage)

  unavailable_bwrap = sandbox.ExecutorProbe("bubblewrap", False, "blocked")
  unavailable_landlock = sandbox.ExecutorProbe("landlock", False, "old kernel")
  available_landlock = sandbox.ExecutorProbe("landlock", True, "ABI 7")
  available_bwrap = sandbox.ExecutorProbe("bubblewrap", True, "passed")
  monkeypatch.setattr(sandbox, "probe_bubblewrap", lambda: unavailable_bwrap)
  monkeypatch.setattr(sandbox, "probe_landlock", lambda: unavailable_landlock)
  launch, probes = sandbox.select_executor(policy, ["true"], {}, home)
  assert launch is None
  assert probes == (unavailable_bwrap, unavailable_landlock)

  monkeypatch.setattr(sandbox, "probe_landlock", lambda: available_landlock)
  monkeypatch.setattr(
    sandbox, "_landlock_plan",
    lambda *_args: sandbox.LaunchPlan("landlock", ["true"], {}),
  )
  launch, probes = sandbox.select_executor(policy, ["true"], {}, home)
  assert launch.executor == "landlock"
  assert probes == (unavailable_bwrap, available_landlock)

  monkeypatch.setattr(sandbox, "probe_bubblewrap", lambda: available_bwrap)
  monkeypatch.setattr(
    sandbox, "_bubblewrap_plan",
    lambda *_args: sandbox.LaunchPlan("bubblewrap", ["true"], {}),
  )
  launch, probes = sandbox.select_executor(policy, ["true"], {}, home)
  assert launch.executor == "bubblewrap"
  assert probes == (available_bwrap,)


def _db_app(db, name):
  app = models.App(
    name=name, description="", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _token(client, owner_token, app_id):
  response = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  return response.json()["token"]


def test_job_context_is_nonsecret_and_self_scoped(client, owner_token, db):
  own = _db_app(db, "memory")
  other = _db_app(db, "other")
  token = _token(client, owner_token, own.id)
  headers = {"Authorization": f"Bearer {token}"}

  response = client.get(f"/api/apps/{own.id}/job-context", headers=headers)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["app_id"] == own.id
  assert body["source_dir"] == own.source_dir
  serialized = json.dumps(body).lower()
  assert "token" not in serialized
  assert "credential" not in serialized
  assert client.get(
    f"/api/apps/{other.id}/job-context", headers=headers,
  ).status_code == 403


def test_job_token_is_app_scoped_and_expires_within_two_hours(
  client, owner_token, db,
):
  app = _db_app(db, "memory")

  response = client.post(
    "/api/auth/app-job-token",
    json={"app_id": app.id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 200, response.text
  claims = jwt.get_unverified_claims(response.json()["token"])
  assert claims["scope"] == "app"
  assert claims["app_id"] == app.id
  assert 0 < claims["exp"] - time.time() <= 2 * 60 * 60 + 5
