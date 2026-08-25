"""Pairing, daemon shutdown, and capability boundaries for Möbius Connect."""

import asyncio
import json
import shlex
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from starlette.requests import Request

from app import connect_runner, models
from app.connect_runner import _run_command
from app.database import SessionLocal
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.routes import connect as connect_routes
from app.schema_migrations import run_migrations, schema_migration_history


@pytest.fixture(autouse=True)
def _clear_connect_channels():
  connect_routes._channels.clear()
  connect_routes._commands.clear()
  connect_routes._pair_limiter.reset()
  yield
  connect_routes._channels.clear()
  connect_routes._commands.clear()
  connect_routes._pair_limiter.reset()


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


def test_pairing_code_exchange_is_rate_limited(client):
  for _ in range(10):
    response = client.post("/api/connect/pair", json={"code": "AAAA-AAAA"})
    assert response.status_code == 400

  blocked = client.post("/api/connect/pair", json={"code": "AAAA-AAAA"})

  assert blocked.status_code == 429


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
  channel.control_pending[event["request_id"]].set_result({
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
  channel.control_pending[event["request_id"]].set_result({
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


def test_provisional_websocket_runner_stays_online_for_in_place_update(
  client, auth,
):
  pairing, runner_token = _paired_host(client, auth)
  with client.websocket_connect(
    "/api/connect/socket",
    headers={"Authorization": f"Bearer {runner_token}"},
  ) as websocket:
    websocket.send_json({
      "type": "hello",
      "protocol": 3,
      "platform": "ProvisionalOS 1",
      "active_request_id": None,
      "pending_result_ids": [],
    })
    assert websocket.receive_json() == {"type": "welcome", "protocol": 3}
    host = client.get("/api/connect/hosts", headers=auth).json()["hosts"][0]
    assert host["online"] is True
    assert host["runner_protocol"] == 3
    assert host["runner_update_available"] is True
    assert "--install" in host["update_command"]
    assert host["platform"] == "ProvisionalOS 1"

  host = client.get("/api/connect/hosts", headers=auth).json()["hosts"][0]
  assert host["online"] is False
  assert host["runner_update_available"] is True
  assert "--install" in host["update_command"]


def test_pre_transport_protocol_three_record_offers_offline_update(client, auth):
  pairing, _ = _paired_host(client, auth)
  host = connect_routes._load_host(pairing["id"])
  host["runner_protocol"] = 3
  host.pop("runner_transport", None)
  connect_routes._save_host(host)

  public = client.get("/api/connect/hosts", headers=auth).json()["hosts"][0]

  assert public["online"] is False
  assert public["runner_update_available"] is True
  assert "--install" in public["update_command"]


@pytest.mark.asyncio
async def test_protocol_three_stream_rotates_without_losing_running_command(
  client, auth, monkeypatch,
):
  pairing, runner_token = _paired_host(client, auth)
  request_id = "0" * 16
  first = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = first
  caller = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(
      cmd="printf rotating", timeout=30, request_id=request_id,
    ),
    _owner=object(),
  ))
  assert (await first.queue.get())["request_id"] == request_id
  connect_routes._mark_command_started(pairing["id"], request_id)

  monkeypatch.setattr(connect_routes, "_STREAM_ROTATION_SECONDS", 0.01)
  monkeypatch.setattr(connect_routes, "_HEARTBEAT_SECONDS", 0.01)

  async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}

  request = Request({
    "type": "http",
    "method": "GET",
    "path": "/api/connect/stream",
    "query_string": (
      f"protocol=3&platform=TestOS%201&active_request_id={request_id}"
    ).encode(),
    "headers": [
      (b"authorization", f"Bearer {runner_token}".encode()),
    ],
  }, receive)
  response = await connect_routes.stream(request)
  current = connect_routes._channels[pairing["id"]]
  assert current is not first
  assert current.queue.empty()
  host = connect_routes._load_host(pairing["id"])
  assert host["runner_protocol"] == 3
  assert host["platform"] == "TestOS 1"
  assert connect_routes._public_host(host)["runner_update_available"] is False

  assert await response.body_iterator.__anext__() == ": connected\n\n"
  with pytest.raises(StopAsyncIteration):
    await response.body_iterator.__anext__()

  assert pairing["id"] not in connect_routes._channels
  assert connect_routes._ensure_command(pairing["id"]).request_id == request_id
  assert not caller.done()
  host = connect_routes._load_host(pairing["id"])
  assert host["runner_transport"] == "sse"
  assert connect_routes._public_host(host)["runner_update_available"] is False
  connect_routes._runner_result(pairing["id"], connect_routes.ResultBody(
    request_id=request_id,
    stdout="rotating",
    exit_code=0,
    outcome="completed",
  ))
  assert (await caller)["stdout"] == "rotating"


@pytest.mark.asyncio
async def test_reconnect_keeps_one_command_and_returns_its_result(client, auth):
  pairing, _ = _paired_host(client, auth)
  request_id = "7" * 16
  first = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = first
  caller = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(
      cmd="printf durable", timeout=30, request_id=request_id,
    ),
    _owner=object(),
  ))
  event = await first.queue.get()
  assert event["request_id"] == request_id
  connect_routes._mark_command_started(pairing["id"], request_id)

  connect_routes._channels.pop(pairing["id"])
  second = connect_routes._Channel(protocol_version=3)
  connect_routes._replace_channel(pairing["id"], second)
  await connect_routes._reconcile_runner(pairing["id"], second, {
    "active_request_id": request_id, "pending_result_ids": [],
  })
  assert second.queue.empty()
  connect_routes._runner_result(pairing["id"], connect_routes.ResultBody(
    request_id=request_id,
    stdout="durable",
    exit_code=0,
    outcome="completed",
  ))
  response = await caller
  assert response["stdout"] == "durable"
  # The same stable id is an idempotent read of the retained result, not work.
  retry = await connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(
      cmd="printf durable", timeout=30, request_id=request_id,
    ),
    _owner=object(),
  )
  assert retry == response
  with pytest.raises(connect_routes.HTTPException) as reused:
    await connect_routes.exec_on_host(
      pairing["id"],
      connect_routes.ExecBody(
        cmd="different work", timeout=30, request_id=request_id,
      ),
      _owner=object(),
    )
  assert reused.value.status_code == 409
  assert "different command" in reused.value.detail


@pytest.mark.asyncio
async def test_exec_correlates_the_runner_result(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "a" * 16

  request = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(
      cmd="printf ready", cwd="/tmp", timeout=7, request_id=request_id,
    ),
    _owner=object(),
  ))
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert event["type"] == "exec"
  assert event["request_id"] == request_id
  assert event["cmd"] == "printf ready"
  assert event["cwd"] == "/tmp"
  assert event["timeout"] == 7
  assert event["not_after"] > time.time()
  connect_routes._mark_command_started(pairing["id"], request_id)
  connect_routes._finish_command(pairing["id"], request_id, {
    "stdout": "ready", "stderr": "", "exit_code": 0,
    "outcome": "completed",
  })

  assert await request == {
    "request_id": request_id,
    "stdout": "ready",
    "stderr": "",
    "exit_code": 0,
    "outcome": "completed",
    "truncated": False,
    "timed_out": False,
    "canceled": False,
  }
  assert connect_routes._ensure_command(pairing["id"]) is None


@pytest.mark.asyncio
async def test_persisted_running_command_accepts_result_after_runtime_restart(
  client, auth, monkeypatch,
):
  pairing, _ = _paired_host(client, auth)
  request_id = "8" * 16
  command = connect_routes._ActiveCommand(
    request_id,
    30,
    cmd="sensitive argument",
    started_at=time.time(),
    state="running",
  )
  connect_routes._commands[pairing["id"]] = command
  connect_routes._persist_command(pairing["id"], command)
  record = connect_routes._load_host(pairing["id"])["active_command"]
  assert "cmd" not in record

  # A backend restart drops futures and sockets, but the host record survives.
  connect_routes._commands.clear()
  channel = connect_routes._Channel(protocol_version=3)
  await connect_routes._reconcile_runner(pairing["id"], channel, {
    "active_request_id": None,
    "pending_result_ids": [request_id],
  })
  assert channel.queue.empty()
  restored = connect_routes._ensure_command(pairing["id"])
  assert restored.request_id == request_id

  connect_routes._runner_result(pairing["id"], connect_routes.ResultBody(
    request_id=request_id,
    stdout="finished after restart",
    exit_code=0,
    outcome="completed",
  ))
  assert connect_routes._ensure_command(pairing["id"]) is None
  last = connect_routes._load_host(pairing["id"])["last_command"]
  assert last["id"] == request_id
  assert last["result"]["stdout"] == "finished after restart"
  monkeypatch.setattr(
    connect_routes,
    "_now",
    lambda: last["finished_at"] + connect_routes._RESULT_RETENTION_SECONDS + 1,
  )
  connect_routes._expire_last_command(
    pairing["id"], request_id, last["finished_at"],
  )
  assert connect_routes._load_host(pairing["id"])["last_command"] is None


@pytest.mark.asyncio
async def test_offline_cancel_is_delivered_when_protocol_three_reconnects(
  client, auth,
):
  pairing, _ = _paired_host(client, auth)
  host = connect_routes._load_host(pairing["id"])
  host["runner_protocol"] = 3
  connect_routes._save_host(host)
  request_id = "6" * 16
  command = connect_routes._ActiveCommand(
    request_id, 30, started_at=time.time(), state="running",
  )
  connect_routes._commands[pairing["id"]] = command
  connect_routes._persist_command(pairing["id"], command)

  response = await connect_routes.cancel_host_command(
    pairing["id"], request_id, _owner=object(),
  )
  assert response["state"] == "canceling"
  channel = connect_routes._Channel(protocol_version=3)
  await connect_routes._reconcile_runner(pairing["id"], channel, {
    "active_request_id": request_id,
    "pending_result_ids": [],
  })
  assert await channel.queue.get() == {
    "type": "cancel", "request_id": request_id,
  }


@pytest.mark.asyncio
async def test_exec_caps_large_output_and_reports_runner_timeout(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "b" * 16
  request = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="large command", request_id=request_id),
    _owner=object(),
  ))
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  connect_routes._mark_command_started(pairing["id"], request_id)
  oversized = "head-" + ("x" * connect_routes._MAX_EXEC_STREAM) + "-tail"
  connect_routes._finish_command(pairing["id"], event["request_id"], {
    "stdout": oversized,
    "stderr": "runner timed out",
    "exit_code": 124,
    "timed_out": True,
    "outcome": "timed_out",
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
  assert result["outcome"] == "timed_out"


@pytest.mark.asyncio
async def test_busy_host_rejects_work_instead_of_queueing_it(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "c" * 16
  first = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="first", request_id=request_id),
    _owner=object(),
  ))
  await asyncio.wait_for(channel.queue.get(), timeout=1)

  with pytest.raises(connect_routes.HTTPException) as blocked:
    await connect_routes.exec_on_host(
      pairing["id"],
      connect_routes.ExecBody(cmd="must not queue", request_id="d" * 16),
      _owner=object(),
    )
  assert blocked.value.status_code == 409
  assert "busy" in blocked.value.detail
  assert channel.queue.empty()
  assert connect_routes._public_host(
    connect_routes._load_host(pairing["id"]),
  )["busy"] is True

  connect_routes._mark_command_started(pairing["id"], request_id)
  connect_routes._finish_command(pairing["id"], request_id, {
    "stdout": "", "stderr": "", "exit_code": 0,
    "outcome": "completed",
  })
  await first


@pytest.mark.asyncio
async def test_cancel_keeps_host_busy_until_runner_confirms_exit(client, auth):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "e" * 16
  running = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="long task", request_id=request_id),
    _owner=object(),
  ))
  await asyncio.wait_for(channel.queue.get(), timeout=1)
  connect_routes._mark_command_started(pairing["id"], request_id)

  canceled = await connect_routes.cancel_host_command(
    pairing["id"], request_id, _owner=object(),
  )
  assert canceled["state"] == "canceling"
  assert await asyncio.wait_for(channel.queue.get(), timeout=1) == {
    "type": "cancel", "request_id": request_id,
  }
  assert connect_routes._ensure_command(pairing["id"]).state == "canceling"

  connect_routes._finish_command(pairing["id"], request_id, {
    "stdout": "", "stderr": "command canceled", "exit_code": 130,
    "outcome": "canceled",
  })
  assert (await running)["canceled"] is True
  assert connect_routes._ensure_command(pairing["id"]) is None


@pytest.mark.asyncio
async def test_old_runner_is_single_flight_but_does_not_claim_cancellation(
  client, auth,
):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=1)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "9" * 16
  running = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="legacy task", request_id=request_id),
    _owner=object(),
  ))
  await asyncio.wait_for(channel.queue.get(), timeout=1)

  public = connect_routes._public_host(
    connect_routes._load_host(pairing["id"]),
  )
  assert public["busy"] is True
  assert public["active_command"]["state"] == "running"
  assert public["runner_update_available"] is True
  assert public["update_command"].endswith("python3 - --install")

  with pytest.raises(connect_routes.HTTPException) as raised:
    await connect_routes.cancel_host_command(
      pairing["id"], request_id, _owner=object(),
    )
  assert raised.value.status_code == 409
  assert "updated" in raised.value.detail
  assert connect_routes._ensure_command(pairing["id"]).request_id == request_id

  connect_routes._finish_command(pairing["id"], request_id, {
    "stdout": "", "stderr": "", "exit_code": 0,
    "outcome": "completed",
  })
  await running


@pytest.mark.asyncio
async def test_protocol_two_does_not_claim_offline_cancellation(client, auth):
  pairing, _ = _paired_host(client, auth)
  host = connect_routes._load_host(pairing["id"])
  host["runner_protocol"] = 2
  connect_routes._save_host(host)
  command = connect_routes._ActiveCommand(
    "5" * 16, 30, started_at=time.time(), state="running",
  )
  connect_routes._commands[pairing["id"]] = command
  connect_routes._persist_command(pairing["id"], command)

  with pytest.raises(connect_routes.HTTPException) as raised:
    await connect_routes.cancel_host_command(
      pairing["id"], command.request_id, _owner=object(),
    )
  assert raised.value.status_code == 409
  assert command.state == "running"


@pytest.mark.asyncio
async def test_unacknowledged_dispatch_expires_and_sends_cancel(
  client, auth, monkeypatch,
):
  pairing, _ = _paired_host(client, auth)
  channel = connect_routes._Channel(protocol_version=3)
  connect_routes._channels[pairing["id"]] = channel
  request_id = "f" * 16
  monkeypatch.setattr(connect_routes, "_START_ACK_TIMEOUT", 0.01)
  request = asyncio.create_task(connect_routes.exec_on_host(
    pairing["id"],
    connect_routes.ExecBody(cmd="must expire", request_id=request_id),
    _owner=object(),
  ))
  dispatched = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert dispatched["not_after"] > time.time()

  with pytest.raises(connect_routes.HTTPException) as raised:
    await request
  assert raised.value.status_code == 504
  assert "did not start" in raised.value.detail
  assert await asyncio.wait_for(channel.queue.get(), timeout=1) == {
    "type": "cancel", "request_id": request_id,
  }
  assert connect_routes._ensure_command(pairing["id"]) is None
  assert connect_routes._load_host(
    pairing["id"],
  )["last_command"]["result"]["outcome"] == "expired"


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


def test_runner_uses_standard_urllib_for_protocol_three_stream(monkeypatch):
  opened = []
  posted = []

  class Stream:
    def __enter__(self):
      return self

    def __exit__(self, *_args):
      return False

    def __iter__(self):
      return iter([
        b'data: {"type":"disconnect","request_id":"abcd"}\n\n',
      ])

  def fake_urlopen(request, **kwargs):
    opened.append((request, kwargs))
    return Stream()

  monkeypatch.setattr(connect_runner.urllib.request, "urlopen", fake_urlopen)
  monkeypatch.setattr(
    connect_runner, "_uninstall_service", lambda stop_running: None,
  )
  monkeypatch.setattr(
    connect_runner, "_post",
    lambda url, payload, token=None: posted.append((url, payload, token)),
  )

  connect_runner._serve({"url": "https://mobius.test", "token": "secret"})

  request, kwargs = opened[0]
  assert request.full_url.startswith(
    "https://mobius.test/api/connect/stream?protocol=3&platform=",
  )
  assert request.get_header("Authorization") == "Bearer secret"
  assert request.get_header("Accept") == "text/event-stream"
  assert kwargs["timeout"] is None
  assert posted == [(
    "https://mobius.test/api/connect/result",
    {
      "request_id": "abcd",
      "stdout": "Connect daemon removed.",
      "stderr": "",
      "exit_code": 0,
    },
    "secret",
  )]


def test_runner_refuses_expired_command_without_spawning(
  monkeypatch,
):
  monkeypatch.setattr(
    connect_runner, "_spawn_command",
    lambda *args, **kwargs: pytest.fail("expired command was spawned"),
  )
  monkeypatch.setattr(
    connect_runner,
    "_post",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      urllib.error.URLError("offline"),
    ),
  )
  runner = connect_runner._CommandRunner("https://mobius.test", "token")

  runner.start({
    "request_id": "1" * 16,
    "cmd": "must not run",
    "timeout": 30,
    "not_after": time.time() - 1,
  })

  messages = list(runner.outbox)
  assert messages[-1]["outcome"] == "expired"
  assert messages[-1]["exit_code"] == 124
  assert runner.active is None


def test_runner_retries_a_result_until_ordinary_https_succeeds(monkeypatch):
  attempts = []

  def post(_url, payload, token=None):
    attempts.append((payload, token))
    if len(attempts) == 1:
      raise urllib.error.URLError("rotating")
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", post)
  runner = connect_runner._CommandRunner("https://mobius.test", "token")
  request_id = "4" * 16
  runner._post_result(request_id, "ready", "", 0, "completed")

  active_id, pending_ids = runner.snapshot()
  assert active_id is None
  assert pending_ids == [request_id]
  assert len(runner.pending_messages()) == 1

  assert runner.flush_pending_results() is True
  assert runner.pending_messages() == []
  assert len(attempts) == 2


def test_runner_rechecks_expiry_after_start_ack_before_spawning(monkeypatch):
  clock = iter((100.0, 102.0))
  # Replace this module's clock reference rather than mutating the process-wide
  # time module that pytest and database teardown also use.
  monkeypatch.setattr(
    connect_runner, "time", SimpleNamespace(time=lambda: next(clock)),
  )
  monkeypatch.setattr(
    connect_runner, "_spawn_command",
    lambda *args, **kwargs: pytest.fail("late command was spawned"),
  )

  def post(url, _payload, token=None):
    if url.endswith("/result"):
      raise urllib.error.URLError("offline")
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", post)
  runner = connect_runner._CommandRunner("https://mobius.test", "token")

  runner.start({
    "request_id": "3" * 16,
    "cmd": "must not run after a slow acknowledgement",
    "timeout": 30,
    "not_after": 101.0,
  })

  messages = list(runner.outbox)
  assert [message["type"] for message in messages] == ["result"]
  assert messages[-1]["outcome"] == "expired"
  assert runner.active is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cancellation timing")
def test_runner_cancel_stops_process_tree_and_reports_once(monkeypatch):
  def post(url, _payload, token=None):
    if url.endswith("/result"):
      raise urllib.error.URLError("offline")
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", post)
  runner = connect_runner._CommandRunner("https://mobius.test", "token")
  request_id = "2" * 16
  started = time.monotonic()
  runner.start({
    "request_id": request_id,
    "cmd": f'"{sys.executable}" -c "import time; time.sleep(30)"',
    "timeout": 30,
    "not_after": time.time() + 5,
  })

  assert runner.cancel(request_id) is True
  deadline = time.monotonic() + 3
  results = []
  while time.monotonic() < deadline:
    results = [message for message in runner.outbox if message["type"] == "result"]
    if results:
      break
    time.sleep(0.025)

  assert len(results) == 1
  assert results[0]["outcome"] == "canceled"
  assert results[0]["exit_code"] == 130
  assert time.monotonic() - started < 3
  assert runner.active is None


def test_runner_systemd_update_restarts_the_existing_service(
  tmp_path, monkeypatch,
):
  commands = []
  monkeypatch.setattr(
    connect_runner, "SYSTEMD_UNIT", str(tmp_path / "mobius-connect.service"),
  )
  monkeypatch.setattr(connect_runner, "RUNNER_PATH", "/tmp/runner.py")

  def fake_run(command):
    commands.append(command)
    return SimpleNamespace(returncode=0, stderr="")

  monkeypatch.setattr(connect_runner, "_run", fake_run)

  assert connect_runner._install_systemd("/usr/bin/python3") is True
  assert ["systemctl", "--user", "enable", "mobius-connect.service"] in commands
  assert ["systemctl", "--user", "restart", "mobius-connect.service"] in commands


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
