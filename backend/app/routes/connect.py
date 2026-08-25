"""Pair external machines and dispatch owner-approved commands to them.

A small runner on the target machine dials out to this owner-trusted Möbius
instance and holds an HTTPS event stream open. Each machine authenticates with
a per-host bearer token minted through a short-lived, one-time pairing code. A
command belongs to the paired host, not to one stream or HTTP caller, so a
reconnect can resume control without repeating work.

Protocol v3 intentionally rotates its event stream before common hosting
response caps while keeping command execution alive. Older SSE behavior remains
as an upgrade bridge for already-installed runners; the provisional WebSocket
v3 route remains only long enough to offer its installed runner an in-place
update:

  POST /api/connect/pair    {code}          -> {host_id, token}   (one-time)
  WS   /api/connect/socket  (host bearer)   <-> provisional-v3 bridge
  GET  /api/connect/stream  (host bearer)   -> SSE stream of {exec} commands
  POST /api/connect/state   (host bearer)   -> {request_id, state=started}
  POST /api/connect/result  (host bearer)   -> {request_id, stdout, ...}
  POST /api/connect/disconnect (host bearer) -> revoke this runner

Owner/app surface:

  POST   /api/connect/hosts               create a host + pairing code
  GET    /api/connect/hosts               list hosts + live status
  PATCH  /api/connect/hosts/{id}          rename a host
  GET    /api/connect/hosts/{id}/pairing  re-show/refresh the install command
  DELETE /api/connect/hosts/{id}          remove a host
  POST   /api/connect/hosts/{id}/exec     run a command on that host
  POST   /api/connect/hosts/{id}/commands/{request_id}/cancel
                                             stop that exact command
  GET    /api/connect/runner              download the runner script

Live sockets and waiting callers are in-process — safe because the backend runs
a single uvicorn worker, the same assumption broadcast.py already relies on.
The active command and most recent result are also written into the host's
registry record, so transport loss or a backend restart cannot duplicate work.
Ordinary exec requests never queue behind one another.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from hashlib import sha256
from pathlib import Path

from fastapi import (
  APIRouter,
  Depends,
  HTTPException,
  Request,
  WebSocket,
  WebSocketDisconnect,
)
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import models
from app.config import get_settings
from app.deps import get_owner_or_app_with_connect_manage, reject_cross_site

router = APIRouter(
  prefix="/api/connect",
  tags=["connect"],
  dependencies=[Depends(reject_cross_site)],
)
_pair_limiter = Limiter(key_func=get_remote_address)

# How long a freshly minted pairing code stays redeemable.
_PAIRING_TTL_SECONDS = 15 * 60
# SSE heartbeat cadence; also the granularity at which we notice a dropped
# runner connection.
_HEARTBEAT_SECONDS = 15
# Default ceiling for a single remote command.
_DEFAULT_EXEC_TIMEOUT = 60
# Keep each returned stream bounded so one remote command cannot flood the
# caller's context. Preserve both ends because diagnostics commonly put the
# error at the tail after a large body.
_MAX_EXEC_STREAM = 60_000
_DISCONNECT_COMMAND = "python3 ~/.mobius-connect/runner.py --uninstall"
# Older runners only understand exec events. The uninstall command can return
# without stopping a fallback runner, so terminate its parent runner too.
_LEGACY_DISCONNECT_COMMAND = f'{_DISCONNECT_COMMAND}; kill -TERM "$PPID"'
_DISCONNECT_ACK_TIMEOUT = 4
_LEGACY_DISCONNECT_TIMEOUT = 4
_RUNNER_PROTOCOL_VERSION = 3
_SSE_PROTOCOL_VERSION = 3
# Railway permits active HTTP responses for 15 minutes. Rotate current-runner
# streams well inside that bound; the host-owned command continues separately.
_STREAM_ROTATION_SECONDS = 10 * 60
_START_ACK_TIMEOUT = 10
_RESULT_GRACE_SECONDS = 15
_RESULT_RETENTION_SECONDS = 15 * 60


# --------------------------------------------------------------------------- #
# Registry persistence (file-backed JSON under shared storage)
# --------------------------------------------------------------------------- #
def _hosts_dir() -> Path:
  d = Path(get_settings().data_dir) / "shared" / "connect" / "hosts"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _host_path(host_id: str) -> Path:
  # host ids are minted by _new_id() (alnum + underscore) so they are safe as
  # a filename, but guard against traversal from any other caller.
  if not host_id or "/" in host_id or "\\" in host_id or host_id.startswith("."):
    raise HTTPException(status_code=400, detail="Invalid host id.")
  return _hosts_dir() / f"{host_id}.json"


def _load_host(host_id: str) -> dict | None:
  p = _host_path(host_id)
  if not p.exists():
    return None
  try:
    return json.loads(p.read_text("utf-8"))
  except (OSError, ValueError):
    return None


def _save_host(host: dict) -> None:
  p = _host_path(host["id"])
  tmp = p.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(host, indent=2), "utf-8")
  tmp.replace(p)


def _list_hosts() -> list[dict]:
  out: list[dict] = []
  for p in sorted(_hosts_dir().glob("*.json")):
    try:
      out.append(json.loads(p.read_text("utf-8")))
    except (OSError, ValueError):
      continue
  return out


def _new_id() -> str:
  return "h_" + secrets.token_hex(8)


def _new_code() -> str:
  # Human-friendly, unambiguous alphabet (no 0/O/1/I), grouped for readability.
  alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  raw = "".join(secrets.choice(alphabet) for _ in range(8))
  return f"{raw[:4]}-{raw[4:]}"


def _hash(token: str) -> str:
  return sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
  return time.time()


def _command_fingerprint(cmd: str, cwd: str | None, timeout: int) -> str:
  payload = json.dumps(
    [cmd, cwd, timeout], ensure_ascii=False, separators=(",", ":"),
  )
  return sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# In-memory live channels (one per connected runner)
# --------------------------------------------------------------------------- #
class _Channel:
  """One replaceable transport for a host-owned command lifecycle."""

  def __init__(
    self,
    protocol_version: int = 1,
    *,
    transport: str = "sse",
  ) -> None:
    self.queue: asyncio.Queue[dict] = asyncio.Queue()
    self.control_pending: dict[str, asyncio.Future] = {}
    self.closed = asyncio.Event()
    self.connected_at = _now()
    self.protocol_version = protocol_version
    self.transport = transport


class _ActiveCommand:
  """The one command owned by a paired host across transport reconnects."""

  def __init__(
    self,
    request_id: str,
    timeout: int,
    *,
    cmd: str = "",
    cwd: str | None = None,
    created_at: float | None = None,
    started_at: float | None = None,
    state: str = "dispatching",
    not_after: float | None = None,
    fingerprint: str | None = None,
  ) -> None:
    loop = asyncio.get_running_loop()
    self.request_id = request_id
    self.timeout = timeout
    self.cmd = cmd
    self.cwd = cwd
    self.created_at = created_at if created_at is not None else _now()
    self.started_at = started_at
    self.state = state
    self.not_after = not_after
    self.fingerprint = fingerprint or _command_fingerprint(cmd, cwd, timeout)
    self.started = asyncio.Event()
    if started_at is not None:
      self.started.set()
    self.result: asyncio.Future = loop.create_future()

  @classmethod
  def from_record(cls, record: dict) -> _ActiveCommand:
    return cls(
      str(record["id"]),
      max(1, int(record.get("timeout") or _DEFAULT_EXEC_TIMEOUT)),
      cmd=str(record.get("cmd") or ""),
      cwd=record.get("cwd"),
      created_at=float(record.get("created_at") or _now()),
      started_at=(
        float(record["started_at"])
        if record.get("started_at") is not None else None
      ),
      state=str(record.get("state") or "dispatching"),
      not_after=(
        float(record["not_after"])
        if record.get("not_after") is not None else None
      ),
      fingerprint=str(record.get("fingerprint") or ""),
    )

  def record(self) -> dict:
    record = {
      "id": self.request_id,
      "timeout": self.timeout,
      "created_at": self.created_at,
      "started_at": self.started_at,
      "state": self.state,
      "not_after": self.not_after,
      "fingerprint": self.fingerprint,
    }
    # Replay needs the command only during the short pre-start dispatch window.
    # Do not retain command text (which may contain sensitive arguments) for the
    # remainder of a long-running command.
    if self.state == "dispatching":
      record["cmd"] = self.cmd
      record["cwd"] = self.cwd
    return record

  def event(self) -> dict:
    return {
      "type": "exec",
      "request_id": self.request_id,
      "cmd": self.cmd,
      "cwd": self.cwd,
      "timeout": self.timeout,
      "not_after": self.not_after,
    }


def _active_public(command: _ActiveCommand | None) -> dict | None:
  if command is None:
    return None
  return {
    "id": command.request_id,
    "state": command.state,
    "created_at": command.created_at,
    "started_at": command.started_at,
    "timeout": command.timeout,
  }


def _active_record_public(record: object) -> dict | None:
  if not isinstance(record, dict) or not record.get("id"):
    return None
  return {
    "id": record["id"],
    "state": record.get("state") or "dispatching",
    "created_at": record.get("created_at"),
    "started_at": record.get("started_at"),
    "timeout": record.get("timeout") or _DEFAULT_EXEC_TIMEOUT,
  }


def _persist_command(host_id: str, command: _ActiveCommand | None) -> None:
  host = _load_host(host_id)
  if host is None:
    return
  host["active_command"] = command.record() if command is not None else None
  _save_host(host)


def _ensure_command(host_id: str) -> _ActiveCommand | None:
  command = _commands.get(host_id)
  if command is not None:
    return command
  host = _load_host(host_id)
  record = host.get("active_command") if host is not None else None
  if not isinstance(record, dict) or not record.get("id"):
    return None
  command = _ActiveCommand.from_record(record)
  _commands[host_id] = command
  return command


def _mark_command_started(host_id: str, request_id: str) -> bool:
  command = _ensure_command(host_id)
  if command is None or command.request_id != request_id:
    return False
  if command.started_at is None:
    command.started_at = _now()
    command.started.set()
  if command.state != "canceling":
    command.state = "running"
  _persist_command(host_id, command)
  return True


def _finish_command(host_id: str, request_id: str, result: dict) -> bool:
  command = _ensure_command(host_id)
  if command is None or command.request_id != request_id:
    return False
  public_result = _format_result(request_id, result)
  if not command.result.done():
    command.result.set_result(public_result)
  _commands.pop(host_id, None)
  host = _load_host(host_id)
  if host is not None:
    finished_at = _now()
    host["active_command"] = None
    host["last_command"] = {
      "id": request_id,
      "fingerprint": command.fingerprint,
      "finished_at": finished_at,
      "result": public_result,
    }
    _save_host(host)
    try:
      asyncio.get_running_loop().call_later(
        _RESULT_RETENTION_SECONDS + 1,
        _expire_last_command,
        host_id,
        request_id,
        finished_at,
      )
    except RuntimeError:
      pass
  return True


async def _request_command_cancel(
  host_id: str,
  command: _ActiveCommand,
) -> bool:
  ch = _channels.get(host_id)
  host = _load_host(host_id)
  known_protocol = (
    ch.protocol_version if ch is not None
    else int((host or {}).get("runner_protocol") or 1)
  )
  if known_protocol < 2 or (ch is None and known_protocol < 3):
    return False
  if command.state != "canceling":
    command.state = "canceling"
    _persist_command(host_id, command)
  if ch is not None:
    await ch.queue.put({"type": "cancel", "request_id": command.request_id})
  return True


_channels: dict[str, _Channel] = {}
_commands: dict[str, _ActiveCommand] = {}


def _touch(host_id: str) -> None:
  host = _load_host(host_id)
  if host is not None:
    _prune_last_command(host)
    host["last_seen"] = _now()
    _save_host(host)


# --------------------------------------------------------------------------- #
# Host-token auth (runner side) — separate from owner/app JWT auth
# --------------------------------------------------------------------------- #
def _auth_host(request: Request | WebSocket) -> dict:
  header = request.headers.get("authorization", "")
  if not header.lower().startswith("bearer "):
    raise HTTPException(status_code=401, detail="Missing host token.")
  token = header[7:].strip()
  wanted = _hash(token)
  for host in _list_hosts():
    stored = host.get("token_sha256")
    if stored and secrets.compare_digest(stored, wanted):
      return host
  raise HTTPException(status_code=401, detail="Unknown or revoked host token.")


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class CreateHostBody(BaseModel):
  name: str = Field(default="My machine", max_length=80)

  @field_validator("name")
  @classmethod
  def normalize_name(cls, value: str) -> str:
    return value.strip() or "My machine"


class PairBody(BaseModel):
  code: str = Field(min_length=1, max_length=16)


class ResultBody(BaseModel):
  request_id: str = Field(min_length=1, max_length=64)
  stdout: str = Field(default="", max_length=8 * 1024 * 1024)
  stderr: str = Field(default="", max_length=8 * 1024 * 1024)
  exit_code: int = 0
  timed_out: bool = False
  outcome: str | None = Field(default=None, max_length=16)


class ExecBody(BaseModel):
  cmd: str = Field(min_length=1, max_length=64 * 1024)
  cwd: str | None = Field(default=None, max_length=4096)
  timeout: int = Field(default=_DEFAULT_EXEC_TIMEOUT, ge=1, le=3600)
  request_id: str | None = Field(
    default=None, min_length=16, max_length=64, pattern=r"^[a-f0-9]+$",
  )


class CommandStateBody(BaseModel):
  request_id: str = Field(
    min_length=16, max_length=64, pattern=r"^[a-f0-9]+$",
  )
  state: str = Field(min_length=1, max_length=16)


class RenameHostBody(BaseModel):
  name: str = Field(min_length=1, max_length=80)


# --------------------------------------------------------------------------- #
# Owner/app surface
# --------------------------------------------------------------------------- #
def _base_url() -> str:
  return get_settings().frontend_origin.rstrip("/")


def _install_command(base: str, code: str) -> str:
  # --install sets up a service that survives reboot; drop it to run in the
  # foreground for a quick try.
  return (
    f'curl -fsSL "{base}/api/connect/runner" | python3 - '
    f'--pair {code} --url "{base}" --install'
  )


def _update_command(base: str) -> str:
  return f'curl -fsSL "{base}/api/connect/runner" | python3 - --install'


def _public_host(host: dict) -> dict:
  """Registry view safe to hand to the owner/app (no token hash)."""
  ch = _channels.get(host["id"])
  _prune_last_command(host)
  active = _commands.get(host["id"])
  active_public = (
    _active_public(active)
    if active is not None else _active_record_public(host.get("active_command"))
  )
  runner_protocol = (
    ch.protocol_version if ch is not None
    else host.get("runner_protocol")
  )
  runner_transport = (
    ch.transport if ch is not None else host.get("runner_transport")
  )
  # Protocol 3 existed briefly as the provisional WebSocket runner. Records
  # written before transport persistence can therefore be identified safely:
  # older SSE runners are protocol 1/2 and current SSE writes this field.
  if runner_transport is None and runner_protocol == 3:
    runner_transport = "websocket"
  paired = bool(host.get("token_sha256"))
  runner_update_available = bool(
    paired and runner_protocol is not None and (
      int(runner_protocol or 1) < _RUNNER_PROTOCOL_VERSION
      or runner_transport != "sse"
    )
  )
  return {
    "id": host["id"],
    "name": host.get("name") or "Machine",
    "paired": paired,
    "online": ch is not None,
    "busy": active_public is not None,
    "active_command": active_public,
    "runner_protocol": runner_protocol,
    "runner_update_available": runner_update_available,
    "update_command": (
      _update_command(_base_url()) if runner_update_available else None
    ),
    "last_seen": host.get("last_seen"),
    "created_at": host.get("created_at"),
    "platform": host.get("platform"),
    "disconnect_command": _DISCONNECT_COMMAND,
  }


def _prune_last_command(host: dict) -> None:
  last = host.get("last_command")
  if not isinstance(last, dict):
    return
  finished_at = float(last.get("finished_at") or 0)
  if _now() - finished_at <= _RESULT_RETENTION_SECONDS:
    return
  host["last_command"] = None
  _save_host(host)


def _expire_last_command(
  host_id: str,
  request_id: str,
  finished_at: float,
) -> None:
  host = _load_host(host_id)
  last = host.get("last_command") if host is not None else None
  if not isinstance(last, dict):
    return
  if last.get("id") != request_id or last.get("finished_at") != finished_at:
    return
  if _now() - finished_at < _RESULT_RETENTION_SECONDS:
    return
  host["last_command"] = None
  _save_host(host)


def _forget_host(host_id: str) -> None:
  ch = _channels.pop(host_id, None)
  if ch is not None:
    for fut in ch.control_pending.values():
      if not fut.done():
        fut.cancel()
  command = _commands.pop(host_id, None)
  if command is not None and not command.result.done():
    command.result.cancel()
  _host_path(host_id).unlink(missing_ok=True)


async def _ask_runner_to_disconnect(ch: _Channel) -> str:
  request_id = secrets.token_hex(8)
  loop = asyncio.get_running_loop()
  fut: asyncio.Future = loop.create_future()
  ch.control_pending[request_id] = fut
  await ch.queue.put({"type": "disconnect", "request_id": request_id})
  try:
    result = await asyncio.wait_for(
      asyncio.shield(fut), timeout=_DISCONNECT_ACK_TIMEOUT,
    )
  except asyncio.TimeoutError:
    # Runners installed before the disconnect protocol ignore the event. Their
    # exec channel can still uninstall the service, then explicitly terminate
    # the runner if that older uninstaller returns without doing so.
    legacy_id = secrets.token_hex(8)
    await ch.queue.put({
      "type": "exec",
      "request_id": legacy_id,
      "cmd": _LEGACY_DISCONNECT_COMMAND,
      "cwd": None,
      "timeout": _LEGACY_DISCONNECT_TIMEOUT,
    })
    try:
      await asyncio.wait_for(
        ch.closed.wait(), timeout=_LEGACY_DISCONNECT_TIMEOUT,
      )
    except asyncio.TimeoutError:
      raise HTTPException(
        status_code=504,
        detail=(
          "The machine did not confirm that its daemon stopped. "
          "The saved connection was kept."
        ),
      )
    return "legacy-stopped"
  finally:
    ch.control_pending.pop(request_id, None)
    if not fut.done():
      fut.cancel()
  if int(result.get("exit_code", 1)) != 0:
    raise HTTPException(
      status_code=502,
      detail=result.get("stderr") or "The machine could not stop its daemon.",
    )
  return "acknowledged"


async def _await_command_result(
  host_id: str,
  command: _ActiveCommand,
) -> dict:
  begins_at = command.started_at or command.created_at
  remaining = max(
    0.01,
    begins_at + command.timeout + _RESULT_GRACE_SECONDS - _now(),
  )
  try:
    return await asyncio.wait_for(
      asyncio.shield(command.result), timeout=remaining,
    )
  except asyncio.TimeoutError:
    cancel_sent = await _request_command_cancel(host_id, command)
    detail = "The command timed out and Connect asked the machine to stop it."
    if not cancel_sent:
      detail = (
        "The command timed out, but this machine’s runner is too old to stop "
        "it remotely. Update the runner in Connect."
      )
    raise HTTPException(status_code=504, detail=detail)


@router.post("/hosts")
async def create_host(
  body: CreateHostBody,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host_id = _new_id()
  code = _new_code()
  host = {
    "id": host_id,
    "name": body.name,
    "created_at": _now(),
    "pairing_code": code,
    "pairing_expires_at": _now() + _PAIRING_TTL_SECONDS,
    "token_sha256": None,
    "paired_at": None,
    "last_seen": None,
    "platform": None,
    "runner_protocol": None,
    "runner_transport": None,
    "active_command": None,
    "last_command": None,
  }
  _save_host(host)
  base = _base_url()
  return {
    "id": host_id,
    "name": host["name"],
    "pairing_code": code,
    "install_command": _install_command(base, code),
    "expires_at": host["pairing_expires_at"],
  }


@router.get("/hosts")
async def list_hosts(
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  return {"hosts": [_public_host(h) for h in _list_hosts()]}


@router.patch("/hosts/{host_id}")
async def rename_host(
  host_id: str,
  body: RenameHostBody,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  """Rename a paired machine without requiring it to be online."""
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  name = body.name.strip()
  if not name:
    raise HTTPException(status_code=400, detail="A machine name can’t be empty.")
  host["name"] = name
  _save_host(host)
  return _public_host(host)


@router.get("/hosts/{host_id}/pairing")
async def host_pairing(
  host_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  """Re-show the install command for an unpaired host (refreshes the code so it
  is always valid), so the owner never has to remove + re-add just to copy it."""
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  if host.get("token_sha256"):
    raise HTTPException(status_code=409, detail="This machine is already paired.")
  code = _new_code()
  host["pairing_code"] = code
  host["pairing_expires_at"] = _now() + _PAIRING_TTL_SECONDS
  _save_host(host)
  base = _base_url()
  return {
    "id": host_id,
    "name": host["name"],
    "pairing_code": code,
    "install_command": _install_command(base, code),
    "expires_at": host["pairing_expires_at"],
  }


@router.delete("/hosts/{host_id}")
async def delete_host(
  host_id: str,
  force: bool = False,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  ch = _channels.get(host_id)
  daemon = "not-installed"
  if host.get("token_sha256"):
    if ch is None:
      if not force:
        raise HTTPException(
          status_code=409,
          detail=(
            "This machine is offline, so Möbius cannot stop its daemon. "
            f"Run `{_DISCONNECT_COMMAND}` on it, then remove the connection."
          ),
        )
      daemon = "offline-manual"
    else:
      daemon = await _ask_runner_to_disconnect(ch)
  _forget_host(host_id)
  return {"ok": True, "daemon": daemon}


@router.post("/hosts/{host_id}/exec")
async def exec_on_host(
  host_id: str,
  body: ExecBody,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  ch = _channels.get(host_id)
  request_id = body.request_id or secrets.token_hex(8)
  fingerprint = _command_fingerprint(body.cmd, body.cwd, body.timeout)
  _prune_last_command(host)
  last = host.get("last_command")
  if isinstance(last, dict) and last.get("id") == request_id:
    if last.get("fingerprint") != fingerprint:
      raise HTTPException(
        status_code=409,
        detail="That request id already belongs to a different command.",
      )
    result = last.get("result")
    if isinstance(result, dict):
      return result

  command = _ensure_command(host_id)
  if command is not None and command.request_id == request_id:
    if command.fingerprint != fingerprint:
      raise HTTPException(
        status_code=409,
        detail="That request id already belongs to a different command.",
      )
    # A caller can safely retry after losing its own HTTP connection. It joins
    # the one host-owned command instead of dispatching the same work twice.
    return await _await_command_result(host_id, command)
  if command is not None:
    raise HTTPException(
      status_code=409,
      detail=(
        f"{host.get('name') or 'That machine'} is busy with another command. "
        "Wait for it to finish or stop it in Connect."
      ),
    )
  if ch is None:
    raise HTTPException(
      status_code=409,
      detail=f"{host.get('name') or 'That machine'} is offline right now.",
    )
  timeout = body.timeout
  not_after = _now() + _START_ACK_TIMEOUT
  command = _ActiveCommand(
    request_id,
    timeout,
    cmd=body.cmd,
    cwd=body.cwd,
    not_after=not_after,
    fingerprint=fingerprint,
  )
  _commands[host_id] = command
  _persist_command(host_id, command)
  if ch.protocol_version < _RUNNER_PROTOCOL_VERSION:
    _mark_command_started(host_id, request_id)
  await ch.queue.put(command.event())
  try:
    if ch.protocol_version >= _RUNNER_PROTOCOL_VERSION:
      try:
        await asyncio.wait_for(
          command.started.wait(), timeout=_START_ACK_TIMEOUT,
        )
      except asyncio.TimeoutError:
        await _request_command_cancel(host_id, command)
        _finish_command(host_id, request_id, {
          "stdout": "",
          "stderr": "command expired before the runner confirmed it started",
          "exit_code": 124,
          "outcome": "expired",
        })
        raise HTTPException(
          status_code=504,
          detail="The machine did not start the command before it expired.",
        )
    # Execution time belongs to the runner and begins only after its start ack.
    return await _await_command_result(host_id, command)
  except asyncio.CancelledError:
    # HTTP caller lifetime and command lifetime are deliberately independent.
    # `mach` sends an explicit cancel request on Ctrl-C; an edge timeout or
    # backend shutdown must not silently kill remote work.
    raise


@router.post("/hosts/{host_id}/commands/{request_id}/cancel")
async def cancel_host_command(
  host_id: str,
  request_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  ch = _channels.get(host_id)
  command = _ensure_command(host_id)
  if command is None or command.request_id != request_id:
    raise HTTPException(status_code=404, detail="That command is no longer running.")
  if not await _request_command_cancel(host_id, command):
    raise HTTPException(
      status_code=409,
      detail=(
        "This machine’s runner must be updated before commands can be stopped "
        "remotely. The current command is still running."
      ),
    )
  return {"ok": True, "request_id": request_id, "state": command.state}


def _cap_stream(text: str) -> tuple[str, bool]:
  if len(text) <= _MAX_EXEC_STREAM:
    return text, False
  marker = "\n…[output truncated]…\n"
  kept = _MAX_EXEC_STREAM - len(marker)
  head = (kept + 1) // 2
  tail = kept // 2
  return f"{text[:head]}{marker}{text[-tail:]}", True


def _format_result(request_id: str, result: dict) -> dict:
  stdout, stdout_truncated = _cap_stream(str(result.get("stdout") or ""))
  stderr, stderr_truncated = _cap_stream(str(result.get("stderr") or ""))
  exit_code = int(result.get("exit_code", 0))
  outcome = result.get("outcome") or (
    "timed_out" if result.get("timed_out") else "completed"
  )
  return {
    "request_id": request_id,
    "stdout": stdout,
    "stderr": stderr,
    "exit_code": exit_code,
    "outcome": outcome,
    "truncated": stdout_truncated or stderr_truncated,
    "timed_out": outcome in ("timed_out", "expired"),
    "canceled": outcome == "canceled",
  }


def _replace_channel(host_id: str, ch: _Channel) -> None:
  old = _channels.get(host_id)
  if old is not None:
    for fut in old.control_pending.values():
      if not fut.done():
        fut.cancel()
    old.closed.set()
  _channels[host_id] = ch


def _runner_result(host_id: str, body: ResultBody) -> None:
  ch = _channels.get(host_id)
  control = ch.control_pending.get(body.request_id) if ch is not None else None
  if control is not None and not control.done():
    control.set_result({
      "stdout": body.stdout,
      "stderr": body.stderr,
      "exit_code": body.exit_code,
    })
    return
  outcome = body.outcome if body.outcome in {
    "completed", "canceled", "timed_out", "expired", "lost",
  } else None
  _finish_command(host_id, body.request_id, {
    "stdout": body.stdout,
    "stderr": body.stderr,
    "exit_code": body.exit_code,
    "timed_out": body.timed_out,
    "outcome": outcome,
  })


async def _reconcile_runner(
  host_id: str,
  ch: _Channel,
  hello: dict,
) -> None:
  """Join one runner's local state to the durable host-owned command."""
  runner_active = str(hello.get("active_request_id") or "")
  pending_ids = {
    str(item) for item in (hello.get("pending_result_ids") or []) if item
  }
  command = _ensure_command(host_id)
  if command is None:
    if runner_active:
      await ch.queue.put({"type": "cancel", "request_id": runner_active})
    return

  if command.request_id == runner_active:
    _mark_command_started(host_id, command.request_id)
    if command.state == "canceling":
      await ch.queue.put({"type": "cancel", "request_id": command.request_id})
    return
  if command.request_id in pending_ids:
    # The result follows the hello on this connection. Keeping the command here
    # lets that late result resolve a waiting or retried caller exactly once.
    return
  if command.state == "dispatching" and (
    command.not_after is None or _now() <= command.not_after
  ):
    await ch.queue.put(command.event())
    return

  # The backend remembered running work that this restarted runner no longer
  # owns. Clear it honestly rather than either duplicating it or blocking the
  # host forever.
  _finish_command(host_id, command.request_id, {
    "stdout": "",
    "stderr": "runner restarted before the command result was reported",
    "exit_code": 125,
    "outcome": "lost",
  })
  if runner_active:
    await ch.queue.put({"type": "cancel", "request_id": runner_active})


# --------------------------------------------------------------------------- #
# Runner surface (host-token authenticated)
# --------------------------------------------------------------------------- #
@router.post("/pair")
@_pair_limiter.limit("10/minute")
async def pair(request: Request, body: PairBody) -> dict:
  code = (body.code or "").strip().upper()
  if not code:
    raise HTTPException(status_code=400, detail="Missing pairing code.")
  for host in _list_hosts():
    stored = host.get("pairing_code")
    if not stored:
      continue
    if not secrets.compare_digest(stored.upper(), code):
      continue
    if _now() > float(host.get("pairing_expires_at") or 0):
      raise HTTPException(status_code=400, detail="That pairing code has expired.")
    token = secrets.token_urlsafe(32)
    host["token_sha256"] = _hash(token)
    host["pairing_code"] = None
    host["pairing_expires_at"] = None
    host["paired_at"] = _now()
    _save_host(host)
    return {"host_id": host["id"], "token": token, "name": host["name"]}
  raise HTTPException(status_code=400, detail="Invalid pairing code.")


@router.websocket("/socket")
async def legacy_command_socket(websocket: WebSocket) -> None:
  """Upgrade bridge for the provisional WebSocket protocol-v3 runner.

  New runners never use this route. Keeping the already-installed v3 runner
  online lets Connect show its in-place update command instead of stranding the
  paired machine when rotating SSE becomes active.
  """
  try:
    host = _auth_host(websocket)
  except HTTPException:
    await websocket.close(code=1008, reason="Unknown or revoked host token.")
    return
  await websocket.accept()
  try:
    hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
  except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, TypeError):
    await websocket.close(code=1002, reason="Runner hello required.")
    return
  if not isinstance(hello, dict) or hello.get("type") != "hello":
    await websocket.close(code=1002, reason="Runner hello required.")
    return

  host_id = host["id"]
  ch = _Channel(
    protocol_version=_RUNNER_PROTOCOL_VERSION,
    transport="websocket",
  )
  platform_name = str(hello.get("platform") or "")[:80]
  if platform_name:
    host["platform"] = platform_name
  host["runner_protocol"] = _RUNNER_PROTOCOL_VERSION
  host["runner_transport"] = "websocket"
  _save_host(host)
  _replace_channel(host_id, ch)
  _touch(host_id)
  await _reconcile_runner(host_id, ch, hello)
  await websocket.send_json({"type": "welcome", "protocol": 3})

  async def send_events() -> None:
    while True:
      await websocket.send_json(await ch.queue.get())

  async def receive_events() -> None:
    while True:
      message = await websocket.receive_json()
      if not isinstance(message, dict):
        continue
      kind = message.get("type")
      if kind == "started":
        request_id = str(message.get("request_id") or "")
        if not _mark_command_started(host_id, request_id):
          await ch.queue.put({"type": "cancel", "request_id": request_id})
      elif kind == "result":
        try:
          body = ResultBody.model_validate(message)
        except ValueError:
          continue
        _runner_result(host_id, body)
        await ch.queue.put({
          "type": "ack", "kind": "result", "request_id": body.request_id,
        })
      _touch(host_id)

  sender = asyncio.create_task(send_events())
  receiver = asyncio.create_task(receive_events())
  superseded = asyncio.create_task(ch.closed.wait())
  try:
    done, pending = await asyncio.wait(
      {sender, receiver, superseded}, return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
      task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
      try:
        task.result()
      except WebSocketDisconnect:
        pass
  except WebSocketDisconnect:
    pass
  finally:
    if _channels.get(host_id) is ch:
      del _channels[host_id]
    for fut in ch.control_pending.values():
      if not fut.done():
        fut.cancel()
    ch.closed.set()
    _touch(host_id)


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
  host = _auth_host(request)
  host_id = host["id"]
  # The runner reports its OS on connect so the app can label the machine.
  plat = request.query_params.get("platform")
  if plat:
    host["platform"] = plat[:80]
    _save_host(host)
  try:
    protocol_version = max(1, min(
      int(request.query_params.get("protocol") or 1), _SSE_PROTOCOL_VERSION,
    ))
  except ValueError:
    protocol_version = 1
  ch = _Channel(protocol_version=protocol_version)
  host["runner_protocol"] = protocol_version
  host["runner_transport"] = "sse"
  _save_host(host)
  # A reconnecting runner replaces any stale channel.
  _replace_channel(host_id, ch)
  _touch(host_id)
  if protocol_version >= 3:
    await _reconcile_runner(host_id, ch, {
      "active_request_id": request.query_params.get("active_request_id"),
      "pending_result_ids": request.query_params.getlist("pending_result_id"),
    })

  async def gen():
    loop = asyncio.get_running_loop()
    rotation_at = (
      loop.time() + _STREAM_ROTATION_SECONDS
      if protocol_version >= 3 else None
    )
    try:
      yield ": connected\n\n"
      while True:
        if ch.closed.is_set() or await request.is_disconnected():
          break
        if rotation_at is not None and loop.time() >= rotation_at:
          break
        wait_seconds = _HEARTBEAT_SECONDS
        if rotation_at is not None:
          wait_seconds = min(wait_seconds, max(0.01, rotation_at - loop.time()))
        try:
          evt = await asyncio.wait_for(ch.queue.get(), timeout=wait_seconds)
          yield f"data: {json.dumps(evt)}\n\n"
        except asyncio.TimeoutError:
          if ch.closed.is_set() or (
            rotation_at is not None and loop.time() >= rotation_at
          ):
            break
          yield ": ping\n\n"
    finally:
      if _channels.get(host_id) is ch:
        del _channels[host_id]
      for fut in ch.control_pending.values():
        if not fut.done():
          fut.cancel()
      ch.closed.set()
      _touch(host_id)

  return StreamingResponse(
    gen(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
  )


@router.post("/result")
async def result(body: ResultBody, request: Request) -> dict:
  host = _auth_host(request)
  _runner_result(host["id"], body)
  _touch(host["id"])
  return {"ok": True}


@router.post("/state")
async def command_state(body: CommandStateBody, request: Request) -> dict:
  host = _auth_host(request)
  if body.state != "started":
    raise HTTPException(status_code=400, detail="Unknown command state.")
  if not _mark_command_started(host["id"], body.request_id):
    raise HTTPException(status_code=409, detail="That command is no longer active.")
  _touch(host["id"])
  return {"ok": True}


@router.post("/disconnect")
async def runner_disconnect(request: Request) -> dict:
  """Let a locally run --uninstall command revoke its own host token."""
  host = _auth_host(request)
  _forget_host(host["id"])
  return {"ok": True}


# --------------------------------------------------------------------------- #
# The runner script (served for download; pure Python stdlib)
# --------------------------------------------------------------------------- #
_RUNNER_PATH = Path(__file__).resolve().parents[1] / "connect_runner.py"


@router.get("/runner")
async def runner_script() -> PlainTextResponse:
  return PlainTextResponse(
    _RUNNER_PATH.read_text("utf-8"), media_type="text/x-python",
  )
