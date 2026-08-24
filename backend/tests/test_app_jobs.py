"""Revocable process-group leases for scheduled and on-demand app jobs."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
import jwt

from app import app_cron, app_jobs, models
from app.config import get_settings


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cron_parser_resolves_supervised_command_to_real_job():
  line = (
    "30 5 * * * python3 /app/scripts/app-job-runner.py "
    "--scheduled 57 /data/apps/memory/memory-job.sh"
  )
  assert app_cron.crontab_command_path(line) == (
    "/data/apps/memory/memory-job.sh"
  )


def test_cron_parser_resolves_wall_clock_gate_to_real_job():
  line = (
    "* * * * * python3 /app/scripts/app-job-runner.py "
    "--scheduled --wall-clock Europe/Belgrade 30\\ 2\\ \\*\\ \\*\\ \\* "
    "57 /data/apps/memory/memory-job.sh"
  )
  assert app_cron.crontab_command_path(line) == (
    "/data/apps/memory/memory-job.sh"
  )


def test_initialization_commands_request_a_readiness_wait():
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


def test_wall_clock_claim_is_once_per_identity_and_local_date(
  monkeypatch, tmp_path,
):
  runner = _load_runner()
  monkeypatch.setattr(runner, "WALL_CLOCK_STATE_DIR", tmp_path / "state")
  monkeypatch.setattr(
    runner.cron_tz, "due_wall_clock_date",
    lambda zone_cron, tz_name: date(2026, 10, 25),
  )
  job = tmp_path / "memory" / "fetch.sh"

  def claim():
    return runner._claim_wall_clock_run(
      57, job, "Europe/Belgrade", "30 2 * * *",
    )

  with ThreadPoolExecutor(max_workers=8) as pool:
    claims = list(pool.map(lambda _index: claim(), range(8)))

  assert claims.count(True) == 1
  assert claims.count(False) == 7
  # An explicit schedule change is a new identity, not a duplicate tick.
  assert runner._claim_wall_clock_run(
    57, job, "Europe/Belgrade", "45 2 * * *",
  ) is True


def test_wall_clock_tick_that_is_not_due_stops_before_job_launch(
  monkeypatch, tmp_path,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  job = data_dir / "apps" / "memory" / "fetch.sh"
  job.parent.mkdir(parents=True)
  job.write_text("#!/bin/sh\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "_claim_wall_clock_run", lambda *_args: False)
  monkeypatch.setattr(
    runner, "_emit_cron_outcome",
    lambda *_args: pytest.fail("a non-due tick must not emit cron telemetry"),
  )
  mint = pytest.fail
  monkeypatch.setattr(
    runner, "_mint_app_token",
    lambda *_args: mint("a non-due tick must not mint an app token"),
  )
  monkeypatch.setattr(
    runner.sys, "argv",
    [
      "app-job-runner.py", "--wall-clock", "Europe/Belgrade", "30 2 * * *",
      "57", str(job),
    ],
  )

  assert runner.run() == 0


def test_wrapper_skips_an_overlapping_job_for_the_same_app(
  monkeypatch, tmp_path,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "memory-job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(
    runner, "_emit_cron_outcome",
    lambda *_args: pytest.fail("an overlapping run must not emit cron telemetry"),
  )
  events = []
  monkeypatch.setattr(runner, "_log", lambda app_id, message: events.append(
    (app_id, message),
  ))
  monkeypatch.setattr(
    runner, "_mint_app_token",
    lambda _app_id: pytest.fail("an overlapping job must stop before auth"),
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "57", str(job),
  ])

  held = runner._acquire_app_run_lock(57)
  assert held is not None
  try:
    assert runner.run() == 0
  finally:
    held.close()

  assert events == [(57, "skipped: another job for this app is still running")]
  released = runner._acquire_app_run_lock(57)
  assert released is not None
  released.close()


def test_app_job_runtime_root_is_ignored_by_the_data_repo():
  entrypoint = (REPO_ROOT / "backend/scripts/entrypoint.sh").read_text()
  data_gitignore = entrypoint.split(
    "cat > /data/.gitignore <<'EOF'\n", 1,
  )[1].split("\nEOF", 1)[0]

  assert "/run/" in data_gitignore.splitlines()


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


def test_initialization_uses_the_token_returned_by_readiness(
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
    runner, "_wait_for_ready", lambda app_id: events.append(("ready", app_id)) or "token",
  )
  monkeypatch.setattr(
    runner, "_mint_app_token",
    lambda _app_id: pytest.fail("readiness already owns token minting"),
  )
  monkeypatch.setattr(runner, "_app_is_live", lambda *_args: True)
  monkeypatch.setattr(
    runner, "_job_context", lambda *_args: {
      "source_dir": str(source),
      "capability_contract": None,
    },
  )
  monkeypatch.setattr(
    runner.subprocess, "Popen", lambda *_args, **_kwargs: types.SimpleNamespace(wait=lambda: 0),
  )
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "--wait-for-ready", "57", str(job),
  ])

  assert runner.run() == 0
  assert events == [("ready", 57)]


def test_initialization_readiness_timeout_does_not_retry_outside_its_owner(
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
  monkeypatch.setattr(runner, "_wait_for_ready", lambda _app_id: None)
  monkeypatch.setattr(
    runner, "_emit_cron_outcome",
    lambda *_args: pytest.fail("a preflight failure must not emit cron telemetry"),
  )
  minted = []
  monkeypatch.setattr(runner, "_mint_app_token", lambda _app_id: minted.append(True))
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "--wait-for-ready", "57", str(job),
  ])

  assert runner.run() == 4
  assert minted == []


def test_readiness_waits_until_the_service_credential_can_mint(monkeypatch):
  runner = _load_runner()
  attempts = []

  class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_args): return False

  monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
  monkeypatch.setattr(
    runner,
    "_mint_app_token",
    lambda app_id: attempts.append(app_id) or ("scoped" if len(attempts) == 2 else None),
  )
  ticks = iter((0.0, 0.0, 0.1, 0.1))
  monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks, 0.1))
  monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

  assert runner._wait_for_ready(57, timeout_seconds=1) == "scoped"
  assert attempts == [57, 57]


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
    lambda app_id, token: {
      "source_dir": str(source),
      "capability_contract": None,
    },
  )
  popen = types.SimpleNamespace(wait=lambda: 0)
  calls = []
  prior_sigterm = signal.getsignal(signal.SIGTERM)

  def capture_popen(*args, **kwargs):
    assert signal.getsignal(signal.SIGTERM) is not prior_sigterm
    calls.append((args, kwargs))
    return popen

  monkeypatch.setattr(
    runner.subprocess,
    "Popen",
    capture_popen,
  )
  monkeypatch.setattr(
    runner,
    "_emit_cron_outcome",
    lambda *_args: pytest.fail("a manual run must not emit cron telemetry"),
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
  assert len(calls[0][1]["pass_fds"]) == 1
  assert signal.getsignal(signal.SIGTERM) is prior_sigterm


def test_scheduled_job_emits_owner_authenticated_outcome_after_child_exit(
  tmp_path, monkeypatch,
):
  runner = _load_runner()
  data_dir = tmp_path / "data"
  source = data_dir / "apps" / "memory"
  source.mkdir(parents=True)
  job = source / "fetch.sh"
  job.write_text("#!/bin/sh\nexit 7\n")
  token_file = data_dir / "service-token.txt"
  token_file.write_text("owner-token\n")
  monkeypatch.setattr(runner, "DATA_DIR", data_dir)
  monkeypatch.setattr(runner, "TOKEN_FILE", token_file)
  monkeypatch.setattr(runner, "API_BASE_URL", "http://mobius.test:8000")
  monkeypatch.setattr(runner, "_mint_app_token", lambda _app_id: "app-token")
  monkeypatch.setattr(runner, "_app_is_live", lambda *_args: True)
  monkeypatch.setattr(
    runner, "_job_context", lambda *_args: {"source_dir": str(source)},
  )
  monkeypatch.setattr(runner.os, "getsid", lambda _pid: os.getpid())
  lifecycle = []

  def wait():
    lifecycle.append("child-exit")
    return 7

  monkeypatch.setattr(
    runner.subprocess, "Popen",
    lambda *_args, **_kwargs: types.SimpleNamespace(wait=wait),
  )
  emitted = {}

  class Response:
    def __enter__(self):
      return self

    def __exit__(self, *_args):
      return False

  def urlopen(request, timeout):
    lifecycle.append("emit")
    emitted["url"] = request.full_url
    emitted["auth"] = request.headers["Authorization"]
    emitted["body"] = json.loads(request.data)
    emitted["timeout"] = timeout
    return Response()

  monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)
  monkeypatch.setattr(runner.sys, "argv", [
    "app-job-runner.py", "--scheduled", "57", str(job),
  ])

  assert runner.run() == 7
  assert lifecycle == ["child-exit", "emit"]
  duration_ms = emitted["body"].pop("duration_ms")
  assert isinstance(duration_ms, int)
  assert duration_ms >= 0
  assert emitted == {
    "url": "http://mobius.test:8000/api/admin/activity/emit",
    "auth": "Bearer owner-token",
    "body": {
      "ev": "cron_outcome",
      "app_id": 57,
      "job": "fetch.sh",
      "exit_code": 7,
    },
    "timeout": 5,
  }


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


def _db_app(db, name):
  app = models.App(
    slug=name,
    source_dir=f"/tmp/mobius-tests/{name}",
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
  claims = jwt.decode(
    response.json()["token"],
    options={"verify_signature": False},
    algorithms=["HS256"],
  )
  assert claims["scope"] == "app"
  assert claims["app_id"] == app.id
  assert 0 < claims["exp"] - time.time() <= 2 * 60 * 60 + 5
