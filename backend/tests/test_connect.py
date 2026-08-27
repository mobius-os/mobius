"""Pairing, daemon shutdown, and capability boundaries for Möbius Connect."""

import asyncio
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

from app import connect_runner, models
from app.connect_runner import _run_command
from app.database import SessionLocal
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.routes import connect as connect_routes
from app.schema_migrations import run_migrations, schema_migration_history


@pytest.fixture(autouse=True)
def _clear_connect_channels():
  connect_routes._channels.clear()
  yield
  connect_routes._channels.clear()


def _app_auth(client, auth, *, granted: bool) -> dict[str, str]:
  from test_app_fixtures import create_local_app

  app_id = create_local_app(
    client, auth, name="connect-test", description="test app",
  )["id"]
  session = SessionLocal()
  try:
    app = session.query(models.App).filter(models.App.id == app_id).first()
    app.connect_manage = granted
    session.commit()
  finally:
    session.close()
  minted = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  )
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


def _paired_host(client, auth, name="Workstation"):
  created = client.post(
    "/api/connect/hosts", headers=auth, json={"name": name},
  )
  assert created.status_code == 200, created.text
  pairing = created.json()
  paired = client.post(
    "/api/connect/pair", json={"code": pairing["pairing_code"]},
  )
  assert paired.status_code == 200, paired.text
  return pairing, paired.json()["token"]


def test_app_token_requires_connect_manage(client, auth):
  denied = _app_auth(client, auth, granted=False)
  response = client.get("/api/connect/hosts", headers=denied)
  assert response.status_code == 403
  assert "permissions.connect_manage=true" in response.json()["detail"]

  granted = _app_auth(client, auth, granted=True)
  assert client.get("/api/connect/hosts", headers=granted).json() == {"hosts": []}
  created = client.post(
    "/api/connect/hosts", headers=granted, json={"name": "Workstation"},
  )
  assert created.status_code == 200
  assert created.json()["name"] == "Workstation"


def test_host_rename_updates_and_trims_the_name(client, auth):
  pairing, _ = _paired_host(client, auth, name="Old name")

  renamed = client.patch(
    f"/api/connect/hosts/{pairing['id']}",
    headers=auth,
    json={"name": "  New name  "},
  )

  assert renamed.status_code == 200, renamed.text
  assert renamed.json()["name"] == "New name"
  assert client.get(
    "/api/connect/hosts", headers=auth,
  ).json()["hosts"][0]["name"] == "New name"


def test_host_rename_rejects_empty_or_unknown_hosts(client, auth):
  pairing, _ = _paired_host(client, auth)

  empty = client.patch(
    f"/api/connect/hosts/{pairing['id']}",
    headers=auth,
    json={"name": "   "},
  )
  missing = client.patch(
    "/api/connect/hosts/h_does_not_exist",
    headers=auth,
    json={"name": "New name"},
  )

  assert empty.status_code == 400, empty.text
  assert missing.status_code == 404, missing.text


def test_pairing_is_one_time_and_delete_revokes_runner(client, auth):
  runner = client.get("/api/connect/runner")
  assert runner.status_code == 200
  assert runner.text.startswith("#!/usr/bin/env python3")

  created = client.post(
    "/api/connect/hosts", headers=auth, json={"name": "   "},
  )
  assert created.status_code == 200, created.text
  pairing = created.json()
  assert pairing["name"] == "My machine"
  assert pairing["pairing_code"] in pairing["install_command"]

  before = client.get("/api/connect/hosts", headers=auth).json()["hosts"]
  assert len(before) == 1
  public_host = before[0]
  assert public_host["id"] == pairing["id"]
  assert public_host["name"] == "My machine"
  assert public_host["paired"] is False
  assert public_host["online"] is False
  assert public_host["disconnect_command"] == (
    "python3 ~/.mobius-connect/runner.py --uninstall"
  )
  assert "pairing_code" not in public_host
  assert "token_sha256" not in public_host

  paired = client.post(
    "/api/connect/pair", json={"code": pairing["pairing_code"]},
  )
  assert paired.status_code == 200, paired.text
  runner_token = paired.json()["token"]
  assert client.post(
    "/api/connect/pair", json={"code": pairing["pairing_code"]},
  ).status_code == 400

  runner_auth = {"Authorization": f"Bearer {runner_token}"}
  assert client.post(
    "/api/connect/result", headers=runner_auth,
    json={"request_id": "not-pending"},
  ).status_code == 200

  blocked = client.delete(f"/api/connect/hosts/{pairing['id']}", headers=auth)
  assert blocked.status_code == 409
  assert "cannot stop its daemon" in blocked.json()["detail"]

  removed = client.delete(
    f"/api/connect/hosts/{pairing['id']}?force=true", headers=auth,
  )
  assert removed.status_code == 200
  assert removed.json()["daemon"] == "offline-manual"
  assert client.post(
    "/api/connect/result", headers=runner_auth,
    json={"request_id": "not-pending"},
  ).status_code == 401


@pytest.mark.asyncio
async def test_online_disconnect_waits_for_daemon_ack_before_revoking(client, auth):
  pairing, runner_token = _paired_host(client, auth)
  host_id = pairing["id"]
  channel = connect_routes._Channel()
  connect_routes._channels[host_id] = channel

  removing = asyncio.create_task(
    connect_routes.delete_host(host_id, _owner=object()),
  )
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert event["type"] == "disconnect"
  channel.pending[event["request_id"]].set_result({
    "stdout": "Connect daemon removed.", "stderr": "", "exit_code": 0,
  })

  assert await removing == {"ok": True, "daemon": "acknowledged"}
  assert connect_routes._load_host(host_id) is None
  runner_auth = {"Authorization": f"Bearer {runner_token}"}
  assert client.post(
    "/api/connect/result", headers=runner_auth,
    json={"request_id": "revoked"},
  ).status_code == 401


@pytest.mark.asyncio
async def test_failed_daemon_cleanup_keeps_the_connection(client, auth):
  pairing, _ = _paired_host(client, auth)
  host_id = pairing["id"]
  channel = connect_routes._Channel()
  connect_routes._channels[host_id] = channel

  removing = asyncio.create_task(
    connect_routes.delete_host(host_id, _owner=object()),
  )
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  channel.pending[event["request_id"]].set_result({
    "stdout": "", "stderr": "systemd disable failed", "exit_code": 1,
  })

  with pytest.raises(connect_routes.HTTPException) as raised:
    await removing
  assert raised.value.status_code == 502
  assert connect_routes._load_host(host_id) is not None
  assert host_id in connect_routes._channels


@pytest.mark.asyncio
async def test_old_runner_must_close_after_compatibility_uninstall(
  client, auth, monkeypatch,
):
  pairing, _ = _paired_host(client, auth)
  host_id = pairing["id"]
  channel = connect_routes._Channel()
  connect_routes._channels[host_id] = channel
  monkeypatch.setattr(connect_routes, "_DISCONNECT_ACK_TIMEOUT", 0.01)
  monkeypatch.setattr(connect_routes, "_LEGACY_DISCONNECT_TIMEOUT", 0.2)

  removing = asyncio.create_task(
    connect_routes.delete_host(host_id, _owner=object()),
  )
  assert (await asyncio.wait_for(channel.queue.get(), timeout=1))["type"] == (
    "disconnect"
  )
  fallback = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert fallback["type"] == "exec"
  assert "--uninstall" in fallback["cmd"]
  assert 'kill -TERM "$PPID"' in fallback["cmd"]
  channel.closed.set()

  assert await removing == {"ok": True, "daemon": "legacy-stopped"}
  assert connect_routes._load_host(host_id) is None


@pytest.mark.asyncio
async def test_unconfirmed_legacy_shutdown_keeps_the_connection(
  client, auth, monkeypatch,
):
  pairing, _ = _paired_host(client, auth)
  host_id = pairing["id"]
  channel = connect_routes._Channel()
  connect_routes._channels[host_id] = channel
  monkeypatch.setattr(connect_routes, "_DISCONNECT_ACK_TIMEOUT", 0.01)
  monkeypatch.setattr(connect_routes, "_LEGACY_DISCONNECT_TIMEOUT", 0.01)

  with pytest.raises(connect_routes.HTTPException) as raised:
    await connect_routes.delete_host(host_id, _owner=object())

  assert raised.value.status_code == 504
  assert connect_routes._load_host(host_id) is not None
  assert host_id in connect_routes._channels


def test_runner_remote_disconnect_removes_restart_registration(
  tmp_path: Path, monkeypatch,
):
  config = tmp_path / "config.json"
  runner = tmp_path / "runner.py"
  pid = tmp_path / "runner.pid"
  unit = tmp_path / "mobius-connect.service"
  for path in (config, runner, pid, unit):
    path.write_text("fixture", encoding="utf-8")
  monkeypatch.setattr(connect_runner, "CONFIG_PATH", str(config))
  monkeypatch.setattr(connect_runner, "RUNNER_PATH", str(runner))
  monkeypatch.setattr(connect_runner, "PID_PATH", str(pid))
  monkeypatch.setattr(connect_runner, "SYSTEMD_UNIT", str(unit))
  monkeypatch.setattr(connect_runner.platform, "system", lambda: "Linux")
  commands = []
  def record(command):
    commands.append(command)
    return SimpleNamespace(returncode=0, stdout="", stderr="")
  monkeypatch.setattr(connect_runner, "_run", record)

  connect_runner._uninstall_service(stop_running=False)

  assert ["systemctl", "--user", "disable", "mobius-connect.service"] in commands
  assert ["systemctl", "--user", "daemon-reload"] in commands
  assert not any("stop" in command for command in commands)
  assert all(not path.exists() for path in (config, runner, pid, unit))


def test_local_uninstall_can_revoke_its_server_connection(client, auth):
  pairing, runner_token = _paired_host(client, auth)
  runner_auth = {"Authorization": f"Bearer {runner_token}"}

  response = client.post("/api/connect/disconnect", headers=runner_auth, json={})

  assert response.status_code == 200
  assert client.get("/api/connect/hosts", headers=auth).json() == {"hosts": []}
  assert connect_routes._load_host(pairing["id"]) is None


def test_exec_rejects_an_offline_machine(client, auth):
  pairing, _ = _paired_host(client, auth)

  response = client.post(
    f"/api/connect/hosts/{pairing['id']}/exec",
    headers=auth,
    json={"cmd": "printf ready"},
  )

  assert response.status_code == 409
  assert response.json()["detail"] == "Workstation is offline right now."


@pytest.mark.asyncio
async def test_exec_correlates_the_runner_result(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel()
  connect_routes._channels[pairing["id"]] = channel

  request = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="printf ready", cwd="/tmp", timeout=7),
    _owner=object(),
  ))
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert event == {
    "type": "exec",
    "request_id": event["request_id"],
    "cmd": "printf ready",
    "cwd": "/tmp",
    "timeout": 7,
  }
  channel.pending[event["request_id"]].set_result({
    "stdout": "ready", "stderr": "", "exit_code": 0,
  })

  assert await request == {
    "stdout": "ready",
    "stderr": "",
    "exit_code": 0,
    "truncated": False,
    "timed_out": False,
  }
  assert channel.pending == {}


@pytest.mark.asyncio
async def test_exec_caps_large_output_and_reports_runner_timeout(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel()
  connect_routes._channels[pairing["id"]] = channel
  request = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="large command"),
    _owner=object(),
  ))
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  oversized = "head-" + ("x" * connect_routes._MAX_EXEC_STREAM) + "-tail"
  channel.pending[event["request_id"]].set_result({
    "stdout": oversized,
    "stderr": "runner timed out",
    "exit_code": 124,
    "timed_out": True,
  })

  result = await request

  assert result["stdout"].startswith("head-")
  assert result["stdout"].endswith("-tail")
  assert "output truncated" in result["stdout"]
  assert len(result["stdout"]) == connect_routes._MAX_EXEC_STREAM
  assert result["stderr"] == "runner timed out"
  assert result["exit_code"] == 124
  assert result["truncated"] is True
  assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_exec_timeout_clears_its_pending_request(client, auth, monkeypatch):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel()
  connect_routes._channels[pairing["id"]] = channel

  async def timeout(_future, timeout):
    assert timeout == 16
    _future.cancel()
    raise asyncio.TimeoutError

  monkeypatch.setattr(connect_routes, "asyncio", SimpleNamespace(
    CancelledError=asyncio.CancelledError,
    TimeoutError=asyncio.TimeoutError,
    get_running_loop=asyncio.get_running_loop,
    wait_for=timeout,
  ))

  with pytest.raises(connect_routes.HTTPException) as raised:
    await connect_routes.exec_on_host(
      pairing["id"], connect_routes.ExecBody(cmd="sleep 60", timeout=1),
      _owner=object(),
    )

  assert raised.value.status_code == 504
  assert channel.pending == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_runner_timeout_terminates_the_entire_command_tree(tmp_path: Path):
  """A timed-out command must not leave descendants running on the machine."""
  marker = tmp_path / "descendant-finished"
  descendant = (
    "import pathlib,time; time.sleep(0.5); "
    f"pathlib.Path({str(marker)!r}).touch()"
  )
  launcher = (
    "import subprocess,sys; "
    f"subprocess.Popen([sys.executable, '-c', {descendant!r}])"
  )
  # The launcher and its shell both exit immediately. The descendant retains
  # their captured pipes, reproducing the case where checking only the shell's
  # status would mistake a still-running command tree for completed work.
  cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(launcher)}"

  stdout, stderr, exit_code, timed_out = _run_command(cmd, None, 0.05)

  assert stdout == ""
  assert "timed out" in stderr
  assert exit_code == 124
  assert timed_out is True
  time.sleep(0.6)
  assert not marker.exists()


def test_runner_does_not_mislabel_command_exit_124_as_timeout():
  stdout, stderr, exit_code, timed_out = _run_command("exit 124", None, 1)

  assert stdout == ""
  assert stderr == ""
  assert exit_code == 124
  assert timed_out is False


def test_manifest_requires_boolean_connect_permission():
  manifest = {
    "id": "connect",
    "name": "Connect",
    "version": "0.1.0",
    "description": "Pair an external machine.",
    "entry": "index.jsx",
    "permissions": {"connect_manage": "yes"},
  }
  with pytest.raises(ManifestContractError, match="connect_manage"):
    validate_manifest_contract(manifest)


def test_connect_manage_reaches_a_ledgered_database(tmp_path: Path):
  eng = create_engine(f"sqlite:///{tmp_path / 'ledgered-apps.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), capability_contract JSON)"
    ))
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in (
      "0001_legacy_schema_convergence",
      "0002_chat_run_goal_objective",
      "0003_chat_run_root_identity",
      "0004_app_identity_required",
      "0005_connectors",
      "0006_connector_capability_identity",
      "0007_chat_has_messages",
      "0008_chat_search_documents",
      "0009_app_connections_manage",
      "0010_chat_pending_question_id",
      "0011_delegation_parent_wake",
      "0012_connector_oauth_gcloud",
      "0013_app_hosted_publication",
      "0014_chat_run_goal_plan",
      "0015_chat_run_goal_identity",
    ):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-22 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  columns = {column["name"] for column in inspect(eng).get_columns("apps")}
  assert "connect_manage" in columns
  assert "0016_app_connect_manage" in {
    entry["version"] for entry in schema_migration_history(eng)
  }
