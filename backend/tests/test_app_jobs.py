"""Revocable process-group leases for scheduled and on-demand app jobs."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import types
from pathlib import Path

import pytest
from jose import jwt

from app import app_jobs, models
from app.config import get_settings
from app.install import _crontab_command_path


def test_cron_parser_resolves_supervised_command_to_real_job():
  line = (
    "30 5 * * * python3 /app/scripts/app-job-runner.py "
    "57 /data/apps/memory/memory-job.sh"
  )
  assert _crontab_command_path(line) == (
    "/data/apps/memory/memory-job.sh"
  )


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
  monkeypatch.setattr(runner, "_job_context", lambda app_id, token: {})
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


def test_background_agent_command_masks_platform_data_and_mounts_declared_scope(
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
  monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
  context = {
    "primary": {"provider": "claude"},
    "fallback": None,
    "capability_contract": {
      "background": {"agent": True},
      "data": {"shared_memory": "write"},
    },
  }

  command = runner._sandboxed_command(57, job.resolve(), context)

  assert command[:7] == [
    "/usr/bin/setpriv",
    "--reuid", "1000", "--regid", "1000", "--clear-groups",
    "/usr/bin/bwrap",
  ]
  joined = " ".join(command)
  assert "--unshare-user" not in command
  assert f"--tmpfs {data_dir}" in joined
  assert f"--ro-bind {source} {source}" in joined
  assert f"--bind {data_dir / 'apps' / '57'} {data_dir / 'apps' / '57'}" in joined
  assert f"--bind {data_dir / 'shared' / 'memory'}" in joined
  assert f"--bind {data_dir / 'cli-auth' / 'claude'}" in joined
  assert f"--bind {data_dir / 'cli-auth' / 'codex'}" in joined
  assert str(data_dir / "cli-auth" / "unreviewed-provider") not in joined
  assert "service-token" not in joined
  assert str(data_dir / "db") not in joined

  monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)
  monkeypatch.setattr(runner.os, "getegid", lambda: 1000)
  unprivileged_command = runner._sandboxed_command(57, job.resolve(), context)
  assert unprivileged_command[0] == "/usr/bin/bwrap"
  assert "/usr/bin/setpriv" not in unprivileged_command


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_background_agent_sandbox_enforces_reviewed_mounts(monkeypatch):
  runner = _load_runner()
  with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
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
      "test ! -e \"$DATA_DIR/service-token.txt\" || exit 21\n"
      "test ! -e \"$DATA_DIR/db\" || exit 22\n"
      "test \"$(cat \"$DATA_DIR/shared/memory/fact.txt\")\" = visible || exit 23\n"
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

    command = runner._sandboxed_command(57, job.resolve(), context)
    result = subprocess.run(
      command,
      env={"PATH": os.environ.get("PATH", ""), "DATA_DIR": str(data_dir)},
      capture_output=True,
      text=True,
      timeout=20,
    )
    namespace_denied = (
      "Operation not permitted" in result.stderr
      or "No permissions to create new namespace" in result.stderr
    )
    if result.returncode != 0 and namespace_denied:
      pytest.skip("host kernel disables unprivileged bubblewrap")

    assert result.returncode == 0, result.stderr
    assert (storage / "proof.txt").read_text(encoding="utf-8") == "confined"
    assert (storage / "proof.txt").stat().st_uid == 1000


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
