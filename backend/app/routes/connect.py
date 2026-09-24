"""Pair external machines and dispatch owner-approved commands to them.

A small runner on the target machine dials out to this owner-trusted Möbius
instance and holds an HTTPS event stream open. Each machine authenticates with
a per-host bearer token minted through a short-lived, one-time pairing code. A
command belongs to the paired host, not to one stream or HTTP caller, so a
reconnect can resume control without repeating work.

Protocol v4 rotates its event stream before common hosting response caps while
keeping command execution alive, and carries literal scripts as structured data.
Wire-incompatible runners are rejected at the transport boundary. Compatible
older releases remain connected and expose an in-place update command.

  POST /api/connect/pair    {code}          -> {host_id, token}   (one-time)
  GET  /api/connect/stream  (host bearer)   -> SSE stream of {exec} commands
  POST /api/connect/state   (host bearer)   -> {request_id, state=started}
  POST /api/connect/output  (host bearer)   -> live output chunks
  POST /api/connect/result  (host bearer)   -> {request_id, stdout, ...}
  POST /api/connect/disconnect (host bearer) -> revoke this runner

Owner/app surface:

  GET    /api/connect/outbound            list people this Möbius joined
  POST   /api/connect/outbound            paste another Connect command
  DELETE /api/connect/outbound/{id}       revoke that outbound access
  POST   /api/connect/hosts               create a host + pairing code
  GET    /api/connect/hosts               list hosts + live status
  PATCH  /api/connect/hosts/{id}          rename a host
  GET    /api/connect/hosts/{id}/pairing  re-show/refresh the install command
  DELETE /api/connect/hosts/{id}          remove a host
  POST   /api/connect/hosts/{id}/exec     run a command on that host
  GET    /api/connect/hosts/{id}/commands running + recently finished commands
  GET    /api/connect/hosts/{id}/commands/{request_id}/output
                                             long-poll that command's output
  POST   /api/connect/hosts/{id}/commands/{request_id}/cancel
                                             stop that exact command
  GET    /api/connect/runner              download the runner script

Live sockets and waiting callers are in-process — safe because the backend runs
a single uvicorn worker, the same assumption broadcast.py already relies on.
Active commands and recent results are also written into the host's registry
record, so transport loss or a backend restart cannot duplicate work. Live
output is in-memory only; after a backend restart the final result still
carries the head and tail of each stream.

Runners that announce the "parallel" capability run any number of commands at
once, each with its own id, time limit, output, and cancellation. Other
runners stay single-flight: extra work is refused as busy. Nothing ever waits
in a hidden queue. Capabilities, not release numbers, decide this.
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
)
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import connect_outbound, connect_runner, models
from app.config import get_settings
from app.deps import (
  get_owner_or_app_with_connect_manage,
  reject_cross_site,
  require_nondelegated_owner_or_app_control,
)
from app.storage_io import atomic_write

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
# Default time limit for a single remote command. Callers may ask for any
# length up to a year: a long command stays visible, streamable, and stoppable
# for its whole life.
_DEFAULT_EXEC_TIMEOUT = 60
_MAX_EXEC_TIMEOUT = 365 * 24 * 60 * 60
# Keep each returned stream bounded so one remote command cannot flood the
# caller's context. Preserve both ends because diagnostics commonly put the
# error at the tail after a large body.
_MAX_EXEC_STREAM = 60_000
_DISCONNECT_COMMAND = "python3 ~/.mobius-connect/runner.py --uninstall"
_DISCONNECT_ACK_TIMEOUT = 4
_RUNNER_PROTOCOL_VERSION = connect_runner.RUNNER_PROTOCOL_VERSION
_RUNNER_RELEASE = connect_runner.RUNNER_RELEASE
# Railway permits active HTTP responses for 15 minutes. Rotate current-runner
# streams well inside that bound; the host-owned command continues separately.
_STREAM_ROTATION_SECONDS = 10 * 60
_START_ACK_TIMEOUT = 10
_RESULT_GRACE_SECONDS = 15
_RESULT_RETENTION_SECONDS = 15 * 60
_RUNNER_CAPABILITIES = frozenset(connect_runner.RUNNER_CAPABILITIES)
# Live output kept per command for readers that attach late. This is a memory
# bound, not a result limit: readers following along receive every chunk, and
# the final result independently carries each stream's head and tail.
_MAX_LIVE_OUTPUT_CHARS = 2_000_000
# Longest single output long-poll. Stays well inside proxy idle-request cuts.
_MAX_OUTPUT_WAIT_SECONDS = 25
_COMMAND_LABEL_CHARS = 120


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
  atomic_write(p, json.dumps(host, indent=2), mode=0o600)


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


# Human-friendly, unambiguous alphabet (no 0/O/1/I), grouped for readability.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_code() -> str:
  raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
  return f"{raw[:4]}-{raw[4:]}"


def _normalize_code(raw: str) -> str | None:
  """Return the canonical XXXX-XXXX code, or None if it is not well-formed.

  The result only ever contains characters from `_CODE_ALPHABET` and a single
  hyphen, so it is safe to interpolate into the install shell script below.
  """
  body = (raw or "").strip().upper().replace("-", "")
  if len(body) != 8 or any(ch not in _CODE_ALPHABET for ch in body):
    return None
  return f"{body[:4]}-{body[4:]}"


def _hash(token: str) -> str:
  return sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
  return time.time()


def _command_fingerprint(
  cmd: str | None,
  cwd: str | None,
  timeout: int,
  *,
  script: str | None = None,
  shell: str | None = None,
) -> str:
  # Plain commands keep their established list shape so a backend restart does
  # not turn a caller retry into a false request-id conflict. Scripts use a
  # tagged shape that cannot collide with a command containing the same text.
  payload_value = (
    [cmd or "", cwd, timeout]
    if script is None
    else {"script": script, "shell": shell, "cwd": cwd, "timeout": timeout}
  )
  payload = json.dumps(
    payload_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
  )
  return sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# In-memory live channels (one per connected runner)
# --------------------------------------------------------------------------- #
class _Channel:
  """One replaceable transport for a host-owned command lifecycle."""

  def __init__(self) -> None:
    self.queue: asyncio.Queue[dict] = asyncio.Queue()
    self.control_pending: dict[str, asyncio.Future] = {}
    self.closed = asyncio.Event()
    self.connected_at = _now()


class _OutputLog:
  """One command's live output, addressed by the runner's chunk sequence.

  Delivery is idempotent: a retried chunk whose sequence is already present is
  ignored. Sequences missing because of runner-side drops, trimming, or a
  backend restart stay visible to readers as jumps in the sequence.
  """

  def __init__(self) -> None:
    self.chunks: list[dict] = []
    self.end_seq = 0
    self.chars = 0
    self._changed = asyncio.Event()

  def append(self, chunks: list[dict]) -> None:
    added = False
    for chunk in sorted(chunks, key=lambda item: item["seq"]):
      seq = chunk["seq"]
      if seq < self.end_seq:
        continue
      self.chunks.append(chunk)
      self.chars += len(chunk["text"])
      self.end_seq = seq + 1
      added = True
    while self.chars > _MAX_LIVE_OUTPUT_CHARS and len(self.chunks) > 1:
      self.chars -= len(self.chunks.pop(0)["text"])
    if added:
      self.notify()

  def read(self, after: int) -> dict:
    """Chunks from `after` on. A reader detects missing output by comparing
    each chunk's sequence with the one it expected."""
    return {
      "chunks": [chunk for chunk in self.chunks if chunk["seq"] >= after],
      "next": max(after, self.end_seq),
    }

  def notify(self) -> None:
    self._changed.set()
    self._changed = asyncio.Event()

  async def wait_for_change(self, timeout: float) -> None:
    changed = self._changed
    try:
      await asyncio.wait_for(changed.wait(), timeout=timeout)
    except asyncio.TimeoutError:
      pass


def _command_label(cmd: str | None, script: str | None) -> str | None:
  """A short in-memory description; command text is never persisted."""
  lines = [
    line.strip() for line in (cmd if cmd is not None else script or "").splitlines()
    if line.strip()
  ]
  if not lines:
    return None
  label = lines[0] + (" …" if len(lines) > 1 else "")
  if len(label) > _COMMAND_LABEL_CHARS:
    label = label[:_COMMAND_LABEL_CHARS - 1] + "…"
  return label


class _ActiveCommand:
  """One command owned by a paired host across transport reconnects."""

  def __init__(
    self,
    request_id: str,
    timeout: int,
    *,
    cmd: str | None = None,
    script: str | None = None,
    shell: str | None = None,
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
    self.script = script
    self.shell = shell
    self.cwd = cwd
    self.created_at = created_at if created_at is not None else _now()
    self.started_at = started_at
    self.state = state
    self.not_after = not_after
    self.fingerprint = fingerprint or _command_fingerprint(
      cmd, cwd, timeout, script=script, shell=shell,
    )
    self.started = asyncio.Event()
    if started_at is not None:
      self.started.set()
    self.result: asyncio.Future = loop.create_future()
    self.output = _OutputLog()
    self.label = _command_label(cmd, script)

  @classmethod
  def from_record(cls, record: dict) -> _ActiveCommand:
    return cls(
      str(record["id"]),
      max(1, int(record.get("timeout") or _DEFAULT_EXEC_TIMEOUT)),
      cmd=(str(record["cmd"]) if record.get("cmd") is not None else None),
      script=(
        str(record["script"]) if record.get("script") is not None else None
      ),
      shell=(str(record["shell"]) if record.get("shell") is not None else None),
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
      if self.script is not None:
        record["script"] = self.script
        record["shell"] = self.shell
      else:
        record["cmd"] = self.cmd
      record["cwd"] = self.cwd
    return record

  def event(self) -> dict:
    event = {
      "type": "exec",
      "request_id": self.request_id,
      "cwd": self.cwd,
      "timeout": self.timeout,
      "not_after": self.not_after,
    }
    if self.script is not None:
      event["script"] = self.script
      event["shell"] = self.shell
    else:
      event["cmd"] = self.cmd
    return event


def _active_public(command: _ActiveCommand) -> dict:
  return {
    "id": command.request_id,
    "state": command.state,
    "created_at": command.created_at,
    "started_at": command.started_at,
    "timeout": command.timeout,
    "label": command.label,
  }


def _host_commands(host_id: str) -> dict[str, _ActiveCommand]:
  """The host's active commands, restored from its record after a restart."""
  commands = _commands.get(host_id)
  if commands is not None:
    return commands
  commands = {}
  host = _load_host(host_id)
  if host is not None:
    records = list(host.get("active_commands") or [])
    # Records written before parallel commands held a single active command.
    legacy = host.get("active_command")
    if isinstance(legacy, dict):
      records.append(legacy)
    for record in records:
      if isinstance(record, dict) and record.get("id"):
        command = _ActiveCommand.from_record(record)
        commands[command.request_id] = command
  _commands[host_id] = commands
  return commands


def _find_command(host_id: str, request_id: str) -> _ActiveCommand | None:
  return _host_commands(host_id).get(request_id)


def _persist_commands(host_id: str) -> None:
  host = _load_host(host_id)
  if host is None:
    return
  host["active_commands"] = [
    command.record() for command in _host_commands(host_id).values()
  ]
  host.pop("active_command", None)
  _save_host(host)


def _mark_command_started(host_id: str, request_id: str) -> bool:
  command = _find_command(host_id, request_id)
  if command is None:
    return False
  if command.started_at is None:
    command.started_at = _now()
    command.started.set()
  if command.state != "canceling":
    command.state = "running"
  _persist_commands(host_id)
  return True


def _finish_command(host_id: str, request_id: str, result: dict) -> bool:
  command = _host_commands(host_id).pop(request_id, None)
  if command is None:
    return False
  public_result = _format_result(request_id, result)
  if not command.result.done():
    command.result.set_result(public_result)
  finished_at = _now()
  output_seq = result.get("output_seq")
  _finished_output.setdefault(host_id, {})[request_id] = command.output
  host = _load_host(host_id)
  if host is not None:
    host["active_commands"] = [
      active.record() for active in _host_commands(host_id).values()
    ]
    host.pop("active_command", None)
    recent = host.get("recent_commands")
    if not isinstance(recent, dict):
      recent = {}
    recent[request_id] = {
      "fingerprint": command.fingerprint,
      "finished_at": finished_at,
      "output_seq": output_seq,
      "result": public_result,
    }
    host["recent_commands"] = recent
    host.pop("last_command", None)
    _save_host(host)
  command.output.notify()
  return True


def _finish_command_as_lost(
  host_id: str,
  request_id: str,
  stderr: str,
) -> bool:
  return _finish_command(host_id, request_id, {
    "stdout": "",
    "stderr": stderr,
    "exit_code": 125,
    "outcome": "lost",
  })


async def _request_command_cancel(
  host_id: str,
  command: _ActiveCommand,
) -> bool:
  ch = _channels.get(host_id)
  host = _load_host(host_id)
  if ch is None and (
    int((host or {}).get("runner_protocol") or 0) != _RUNNER_PROTOCOL_VERSION
    or (host or {}).get("runner_transport") != "sse"
  ):
    return False
  if command.state != "canceling":
    command.state = "canceling"
    _persist_commands(host_id)
  if ch is not None:
    await ch.queue.put({"type": "cancel", "request_id": command.request_id})
  return True


_channels: dict[str, _Channel] = {}
_commands: dict[str, dict[str, _ActiveCommand]] = {}
# Output logs of finished commands, retained with their results so a caller
# that attaches late still sees what the command printed.
_finished_output: dict[str, dict[str, _OutputLog]] = {}
_host_id_by_token_hash: dict[str, str] = {}


def _runner_capabilities(host: dict | None) -> set[str]:
  raw = (host or {}).get("runner_capabilities")
  return set(raw) & _RUNNER_CAPABILITIES if isinstance(raw, list) else set()


def _runs_in_parallel(host: dict | None) -> bool:
  return "parallel" in _runner_capabilities(host)


def _touch(host_id: str) -> None:
  host = _load_host(host_id)
  if host is not None:
    _prune_recent_commands(host)
    host["last_seen"] = _now()
    _save_host(host)


# --------------------------------------------------------------------------- #
# Host-token auth (runner side) — separate from owner/app JWT auth
# --------------------------------------------------------------------------- #
def _auth_host(request: Request) -> dict:
  header = request.headers.get("authorization", "")
  if not header.lower().startswith("bearer "):
    raise HTTPException(status_code=401, detail="Missing host token.")
  token = header[7:].strip()
  wanted = _hash(token)
  # Live output makes runner requests frequent. Remember which record a token
  # opened, but always re-read and re-compare it so revocation takes effect.
  cached = _host_id_by_token_hash.get(wanted)
  if cached is not None:
    host = _load_host(cached)
    stored = (host or {}).get("token_sha256")
    if stored and secrets.compare_digest(stored, wanted):
      return host
    _host_id_by_token_hash.pop(wanted, None)
  for host in _list_hosts():
    stored = host.get("token_sha256")
    if stored and secrets.compare_digest(stored, wanted):
      _host_id_by_token_hash[wanted] = host["id"]
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
  truncated: bool = False
  output_seq: int | None = Field(default=None, ge=0)


class OutputChunk(BaseModel):
  seq: int = Field(ge=0)
  stream: str = Field(pattern=r"^(stdout|stderr)$")
  text: str = Field(max_length=1024 * 1024)


class OutputBody(BaseModel):
  request_id: str = Field(min_length=1, max_length=64)
  chunks: list[OutputChunk] = Field(max_length=4096)


class ExecBody(BaseModel):
  cmd: str | None = Field(default=None, min_length=1, max_length=64 * 1024)
  script: str | None = Field(default=None, min_length=1, max_length=64 * 1024)
  shell: str | None = Field(default=None, min_length=1, max_length=4096)
  cwd: str | None = Field(default=None, max_length=4096)
  # One year is a validity bound, not a working limit: it keeps deadline
  # arithmetic finite on both sides.
  timeout: int = Field(default=_DEFAULT_EXEC_TIMEOUT, ge=1, le=_MAX_EXEC_TIMEOUT)
  request_id: str | None = Field(
    default=None, min_length=16, max_length=64, pattern=r"^[a-f0-9]+$",
  )
  # Return as soon as the machine starts the command; read its output and
  # result from the output endpoint. Otherwise wait for the final result.
  stream: bool = False

  @model_validator(mode="after")
  def validate_work(self) -> ExecBody:
    if (self.cmd is None) == (self.script is None):
      raise ValueError("Provide exactly one of cmd or script.")
    if self.shell is not None and self.script is None:
      raise ValueError("shell is only valid with script.")
    return self


class CommandStateBody(BaseModel):
  request_id: str = Field(
    min_length=16, max_length=64, pattern=r"^[a-f0-9]+$",
  )
  state: str = Field(min_length=1, max_length=16)


class RenameHostBody(BaseModel):
  name: str = Field(min_length=1, max_length=80)


class CreateOutboundBody(BaseModel):
  label: str = Field(min_length=1, max_length=80)
  command: str = Field(min_length=1, max_length=4096)

  @field_validator("label")
  @classmethod
  def normalize_label(cls, value: str) -> str:
    label = value.strip()
    if not label:
      raise ValueError("Name who will have access.")
    return label


# --------------------------------------------------------------------------- #
# Owner/app surface
# --------------------------------------------------------------------------- #
@router.get("/outbound")
async def list_outbound_access(
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  return {"connections": connect_outbound.list_profiles()}


@router.post(
  "/outbound",
  dependencies=[Depends(require_nondelegated_owner_or_app_control)],
)
async def create_outbound_access(
  body: CreateOutboundBody,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  try:
    return await connect_outbound.create_profile(body.label, body.command)
  except connect_outbound.OutboundConnectError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
  "/outbound/{profile_id}",
  dependencies=[Depends(require_nondelegated_owner_or_app_control)],
)
async def revoke_outbound_access(
  profile_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  try:
    await connect_outbound.revoke_profile(profile_id)
  except LookupError as exc:
    raise HTTPException(status_code=404, detail="No such shared access.") from exc
  except connect_outbound.OutboundConnectError as exc:
    raise HTTPException(status_code=409, detail=str(exc)) from exc
  return {"ok": True}


def _base_url() -> str:
  return get_settings().frontend_origin.rstrip("/")


def _install_command(base: str, code: str) -> str:
  # One fetch with the code + instance URL baked into the path. No pipe-into-
  # python arguments and no quotes, so nothing gets corrupted when the command
  # is copied and pasted into a terminal. The served script pairs and installs
  # a service that survives reboot (see `install_script`).
  return f"curl -fsSL {base}/api/connect/i/{code} | sh"


def _update_command(base: str) -> str:
  # The target can be paired with several Möbius instances. Pin both the
  # download and the install source to the instance whose Connect UI generated
  # this command; otherwise the runner's saved first connection may silently
  # supply a different (older) release.
  return (
    f'curl -fsSL "{base}/api/connect/runner" '
    f'| python3 - --url "{base}" --install'
  )


def _reported_runner_release(value: object) -> int | None:
  try:
    release = int(str(value))
  except (TypeError, ValueError):
    return None
  return release if release >= 0 else None


def _public_host(host: dict) -> dict:
  """Registry view safe to hand to the owner/app (no token hash)."""
  ch = _channels.get(host["id"])
  _prune_recent_commands(host)
  active = [
    _active_public(command)
    for command in sorted(
      _host_commands(host["id"]).values(), key=lambda item: item.created_at,
    )
  ]
  runner_protocol = (
    _RUNNER_PROTOCOL_VERSION if ch is not None
    else host.get("runner_protocol")
  )
  runner_transport = (
    "sse" if ch is not None else host.get("runner_transport")
  )
  runner_release = _reported_runner_release(host.get("runner_release"))
  paired = bool(host.get("token_sha256"))
  runner_update_available = bool(
    paired and (
      int(runner_protocol or 0) != _RUNNER_PROTOCOL_VERSION
      or runner_transport != "sse"
      or runner_release is None
      or runner_release < _RUNNER_RELEASE
      or not _RUNNER_CAPABILITIES <= _runner_capabilities(host)
    )
  )
  return {
    "id": host["id"],
    "name": host.get("name") or "Machine",
    "paired": paired,
    "online": ch is not None,
    "busy": bool(active),
    "active_commands": active,
    # Older Connect app releases read one command; they can still stop it.
    "active_command": active[0] if active else None,
    "parallel_commands": _runs_in_parallel(host),
    "runner_protocol": runner_protocol,
    "runner_release": runner_release,
    "runner_update_available": runner_update_available,
    "update_command": (
      _update_command(_base_url()) if runner_update_available else None
    ),
    "last_seen": host.get("last_seen"),
    "created_at": host.get("created_at"),
    "platform": host.get("platform"),
    "disconnect_command": _DISCONNECT_COMMAND,
  }


def _prune_recent_commands(host: dict) -> None:
  """Expire finished commands past retention; the only expiry path."""
  recent = host.get("recent_commands")
  recent = dict(recent) if isinstance(recent, dict) else {}
  # A record written before parallel commands kept one finished command.
  changed = "last_command" in host
  legacy = host.pop("last_command", None)
  if isinstance(legacy, dict) and legacy.get("id"):
    recent.setdefault(str(legacy["id"]), {
      key: legacy.get(key) for key in ("fingerprint", "finished_at", "result")
    })
  cutoff = _now() - _RESULT_RETENTION_SECONDS
  kept = {
    request_id: entry for request_id, entry in recent.items()
    if isinstance(entry, dict) and float(entry.get("finished_at") or 0) >= cutoff
  }
  if kept != host.get("recent_commands"):
    host["recent_commands"] = kept
    changed = True
  logs = _finished_output.get(host["id"])
  if logs:
    for request_id in [key for key in logs if key not in kept]:
      logs.pop(request_id, None)
  if changed:
    _save_host(host)


def _recent_command(host: dict, request_id: str) -> dict | None:
  recent = host.get("recent_commands")
  entry = recent.get(request_id) if isinstance(recent, dict) else None
  return entry if isinstance(entry, dict) else None


def _forget_host(host_id: str) -> None:
  ch = _channels.pop(host_id, None)
  if ch is not None:
    for fut in ch.control_pending.values():
      if not fut.done():
        fut.cancel()
  for command in (_commands.pop(host_id, None) or {}).values():
    if not command.result.done():
      command.result.cancel()
    command.output.notify()
  _finished_output.pop(host_id, None)
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
    raise HTTPException(
      status_code=504,
      detail=(
        "The machine did not confirm that its daemon stopped. "
        "The saved connection was kept."
      ),
    )
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
      _finish_command_as_lost(
        host_id,
        command.request_id,
        "runner did not report a final result before its reporting grace elapsed",
      )
      detail = (
        "The command timed out, but this machine’s runner is too old to stop "
        "it remotely. Update the runner in Connect."
      )
    raise HTTPException(status_code=504, detail=detail)


@router.post(
  "/hosts",
  dependencies=[Depends(require_nondelegated_owner_or_app_control)],
)
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
    "runner_release": None,
    "runner_transport": None,
    "active_commands": [],
    "recent_commands": {},
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


@router.get(
  "/hosts/{host_id}/pairing",
  dependencies=[Depends(require_nondelegated_owner_or_app_control)],
)
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


@router.delete(
  "/hosts/{host_id}",
  dependencies=[Depends(require_nondelegated_owner_or_app_control)],
)
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


def _started_response(command: _ActiveCommand) -> dict:
  return {"request_id": command.request_id, "state": command.state}


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
  fingerprint = _command_fingerprint(
    body.cmd,
    body.cwd,
    body.timeout,
    script=body.script,
    shell=body.shell,
  )
  _prune_recent_commands(host)
  recent = _recent_command(host, request_id)
  if recent is not None:
    if recent.get("fingerprint") != fingerprint:
      raise HTTPException(
        status_code=409,
        detail="That request id already belongs to a different command.",
      )
    result = recent.get("result")
    if isinstance(result, dict):
      if body.stream:
        return {"request_id": request_id, "state": "finished"}
      return result

  commands = _host_commands(host_id)
  command = commands.get(request_id)
  if command is not None:
    if command.fingerprint != fingerprint:
      raise HTTPException(
        status_code=409,
        detail="That request id already belongs to a different command.",
      )
    # A caller can safely retry after losing its own HTTP connection. It joins
    # the one host-owned command instead of dispatching the same work twice.
    if body.stream:
      return _started_response(command)
    return await _await_command_result(host_id, command)
  if commands and not _runs_in_parallel(host):
    raise HTTPException(
      status_code=409,
      detail=(
        f"{host.get('name') or 'That machine'} is busy with another command, "
        "and its Connect runner runs one command at a time. Wait for it to "
        "finish, stop it in Connect, or update the runner to run commands "
        "in parallel."
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
    script=body.script,
    shell=body.shell,
    cwd=body.cwd,
    not_after=not_after,
    fingerprint=fingerprint,
  )
  commands[request_id] = command
  _persist_commands(host_id)
  await ch.queue.put(command.event())
  try:
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
    if body.stream:
      return _started_response(command)
    return await _await_command_result(host_id, command)
  except asyncio.CancelledError:
    # HTTP caller lifetime and command lifetime are deliberately independent.
    # `mach` sends an explicit cancel request on Ctrl-C; an edge timeout or
    # backend shutdown must not silently kill remote work.
    raise


@router.get("/hosts/{host_id}/commands")
async def list_host_commands(
  host_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  _prune_recent_commands(host)
  running = [
    _active_public(command)
    for command in sorted(
      _host_commands(host_id).values(), key=lambda item: item.created_at,
    )
  ]
  recent = []
  for request_id, entry in (host.get("recent_commands") or {}).items():
    result = entry.get("result") or {}
    recent.append({
      "id": request_id,
      "finished_at": entry.get("finished_at"),
      "outcome": result.get("outcome"),
      "exit_code": result.get("exit_code"),
    })
  recent.sort(key=lambda item: item.get("finished_at") or 0, reverse=True)
  return {"running": running, "recent": recent}


@router.get("/hosts/{host_id}/commands/{request_id}/output")
async def read_command_output(
  host_id: str,
  request_id: str,
  after: int = 0,
  wait: float = 0,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  """Return output chunks from sequence `after`, waiting up to `wait` seconds.

  The response is ready as soon as there is output past `after` or the
  command has finished. A finished command also returns its final result.
  """
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  after = max(0, after)
  deadline = _now() + max(0.0, min(float(wait), _MAX_OUTPUT_WAIT_SECONDS))
  while True:
    command = _find_command(host_id, request_id)
    if command is not None:
      view = command.output.read(after)
      if view["chunks"] or _now() >= deadline:
        return {
          "request_id": request_id,
          "state": command.state,
          **view,
          "result": None,
          "output_seq": None,
        }
      await command.output.wait_for_change(
        max(0.01, min(_HEARTBEAT_SECONDS, deadline - _now())),
      )
      continue
    host = _load_host(host_id)
    if host is not None:
      _prune_recent_commands(host)
    entry = _recent_command(host, request_id) if host is not None else None
    if entry is None:
      raise HTTPException(
        status_code=404,
        detail="Connect has no running or recent command with that id.",
      )
    log = _finished_output.get(host_id, {}).get(request_id) or _OutputLog()
    return {
      "request_id": request_id,
      "state": "finished",
      **log.read(after),
      "result": entry.get("result"),
      "output_seq": entry.get("output_seq"),
    }


@router.post("/hosts/{host_id}/commands/{request_id}/cancel")
async def cancel_host_command(
  host_id: str,
  request_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> dict:
  host = _load_host(host_id)
  if host is None:
    raise HTTPException(status_code=404, detail="No such host.")
  command = _find_command(host_id, request_id)
  if command is None:
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
    "truncated": (
      bool(result.get("truncated")) or stdout_truncated or stderr_truncated
    ),
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
    "truncated": body.truncated,
    "output_seq": body.output_seq,
  })


async def _reconcile_runner(
  host_id: str,
  ch: _Channel,
  hello: dict,
) -> None:
  """Join one runner's local state to the durable host-owned commands."""
  runner_active = {
    str(item) for item in (hello.get("active_request_ids") or []) if item
  }
  pending_ids = {
    str(item) for item in (hello.get("pending_result_ids") or []) if item
  }
  for command in list(_host_commands(host_id).values()):
    if command.request_id in runner_active:
      _mark_command_started(host_id, command.request_id)
      if command.state == "canceling":
        await ch.queue.put({"type": "cancel", "request_id": command.request_id})
      continue
    if command.request_id in pending_ids:
      # The result follows the hello on this connection. Keeping the command
      # here lets that late result resolve a waiting or retried caller once.
      continue
    if command.state == "dispatching" and (
      command.not_after is None or _now() <= command.not_after
    ):
      await ch.queue.put(command.event())
      continue
    # The backend remembered running work that this restarted runner no longer
    # owns. Clear it honestly rather than either duplicating it or blocking.
    _finish_command(host_id, command.request_id, {
      "stdout": "",
      "stderr": "runner restarted before the command result was reported",
      "exit_code": 125,
      "outcome": "lost",
    })
  # Work the runner still runs but Möbius no longer tracks has no caller left.
  for request_id in sorted(runner_active - set(_host_commands(host_id))):
    await ch.queue.put({"type": "cancel", "request_id": request_id})


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


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
  host = _auth_host(request)
  host_id = host["id"]
  try:
    protocol_version = int(request.query_params.get("protocol") or 0)
  except ValueError:
    protocol_version = 0
  runner_release = _reported_runner_release(
    request.query_params.get("release"),
  )
  # Persist transport compatibility and implementation release independently.
  # A protocol-compatible legacy runner may stay connected while Connect still
  # offers the owner the current implementation.
  host["runner_protocol"] = protocol_version or None
  host["runner_release"] = runner_release
  host["runner_capabilities"] = sorted(
    set(request.query_params.getlist("capability")) & _RUNNER_CAPABILITIES
  )
  host["runner_transport"] = "sse"
  plat = request.query_params.get("platform")
  if plat:
    host["platform"] = plat[:80]
  _save_host(host)
  if protocol_version != _RUNNER_PROTOCOL_VERSION:
    raise HTTPException(
      status_code=426,
      detail="This Connect runner is no longer supported. Update it in Connect.",
    )
  ch = _Channel()
  # A reconnecting runner replaces any stale channel.
  _replace_channel(host_id, ch)
  _touch(host_id)
  # Runners that stream output wait for this before sending any; older runners
  # ignore event types they do not know.
  await ch.queue.put({"type": "hello", "live_output": True})
  await _reconcile_runner(host_id, ch, {
    "active_request_ids": request.query_params.getlist("active_request_id"),
    "pending_result_ids": request.query_params.getlist("pending_result_id"),
  })

  async def gen():
    loop = asyncio.get_running_loop()
    rotation_at = loop.time() + _STREAM_ROTATION_SECONDS
    try:
      yield ": connected\n\n"
      while True:
        if ch.closed.is_set() or await request.is_disconnected():
          break
        if loop.time() >= rotation_at:
          break
        wait_seconds = min(
          _HEARTBEAT_SECONDS, max(0.01, rotation_at - loop.time()),
        )
        try:
          evt = await asyncio.wait_for(ch.queue.get(), timeout=wait_seconds)
          yield f"data: {json.dumps(evt)}\n\n"
        except asyncio.TimeoutError:
          if ch.closed.is_set() or (
            loop.time() >= rotation_at
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


@router.post("/output")
async def command_output(body: OutputBody, request: Request) -> dict:
  host = _auth_host(request)
  command = _find_command(host["id"], body.request_id)
  if command is None:
    raise HTTPException(status_code=409, detail="That command is no longer active.")
  command.output.append([chunk.model_dump() for chunk in body.chunks])
  return {"ok": True, "next": command.output.end_seq}


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


# A POSIX shell bootstrap with the pairing code + instance URL baked in. It
# fetches the Python runner and hands it the same flags the pipe form uses, so
# the single-line install command carries no arguments or quotes of its own.
_INSTALL_SCRIPT_TEMPLATE = """\
#!/bin/sh
# Mobius Connect installer -- pairs this machine and installs the background
# runner. It only makes outbound HTTPS requests and runs commands as you.
set -eu
base="{base}"
code="{code}"
runner="$(mktemp)"
trap 'rm -f "$runner"' EXIT INT TERM
curl -fsSL "$base/api/connect/runner" -o "$runner"
python3 "$runner" --pair "$code" --url "$base" --install
"""


@router.get("/i/{code}")
async def install_script(code: str) -> PlainTextResponse:
  """Serve the single-line install bootstrap: `curl .../i/<code> | sh`.

  Collapsing the fetch, pipe, and flags into one URL removes the quotes and
  arguments that get mangled when the command is copied and pasted. The code is
  normalized to its shell-safe canonical form before it is interpolated; an
  unredeemed or expired code still fails clearly at the runner's pairing step.
  """
  normalized = _normalize_code(code)
  if normalized is None:
    raise HTTPException(status_code=404, detail="Unknown pairing code.")
  script = _INSTALL_SCRIPT_TEMPLATE.format(base=_base_url(), code=normalized)
  return PlainTextResponse(script, media_type="text/x-shellscript")
