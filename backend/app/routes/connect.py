"""Pair external machines and dispatch owner-approved commands to them.

A small runner on the target machine dials out to this owner-trusted Möbius
instance and holds an SSE command channel open. Each machine authenticates with
a per-host bearer token minted through a short-lived, one-time pairing code.

Transport is plain HTTP so the runner needs only the Python stdlib:

  POST /api/connect/pair    {code}          -> {host_id, token}   (one-time)
  GET  /api/connect/stream  (host bearer)   -> SSE stream of {exec} commands
  POST /api/connect/result  (host bearer)   -> {request_id, stdout, ...}
  POST /api/connect/disconnect (host bearer) -> revoke this runner

Owner/app surface:

  POST   /api/connect/hosts               create a host + pairing code
  GET    /api/connect/hosts               list hosts + live status
  PATCH  /api/connect/hosts/{id}          rename a host
  GET    /api/connect/hosts/{id}/pairing  re-show/refresh the install command
  DELETE /api/connect/hosts/{id}          remove a host
  POST   /api/connect/hosts/{id}/exec     run a command on that host
  GET    /api/connect/runner              download the runner script

State is in-process — safe because the backend runs a single uvicorn worker,
the same assumption broadcast.py already relies on. A live SSE channel per
connected host holds an outbound command queue plus the pending result futures
that /exec awaits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app import models
from app.config import get_settings
from app.deps import get_owner_or_app_with_connect_manage, reject_cross_site

log = logging.getLogger("moebius.connect")

router = APIRouter(
  prefix="/api/connect",
  tags=["connect"],
  dependencies=[Depends(reject_cross_site)],
)

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


# --------------------------------------------------------------------------- #
# In-memory live channels (one per connected runner)
# --------------------------------------------------------------------------- #
class _Channel:
  """A live runner connection: an outbound command queue plus the futures the
  exec endpoint is waiting on."""

  def __init__(self) -> None:
    self.queue: asyncio.Queue[dict] = asyncio.Queue()
    self.pending: dict[str, asyncio.Future] = {}
    self.closed = asyncio.Event()
    self.connected_at = _now()


_channels: dict[str, _Channel] = {}


def _touch(host_id: str) -> None:
  host = _load_host(host_id)
  if host is not None:
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


class ExecBody(BaseModel):
  cmd: str = Field(min_length=1, max_length=64 * 1024)
  cwd: str | None = Field(default=None, max_length=4096)
  timeout: int = Field(default=_DEFAULT_EXEC_TIMEOUT, ge=1, le=3600)


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


def _public_host(host: dict) -> dict:
  """Registry view safe to hand to the owner/app (no token hash)."""
  return {
    "id": host["id"],
    "name": host.get("name") or "Machine",
    "paired": bool(host.get("token_sha256")),
    "online": host["id"] in _channels,
    "last_seen": host.get("last_seen"),
    "created_at": host.get("created_at"),
    "platform": host.get("platform"),
    "disconnect_command": _DISCONNECT_COMMAND,
  }


def _forget_host(host_id: str) -> None:
  ch = _channels.pop(host_id, None)
  if ch is not None:
    for fut in ch.pending.values():
      if not fut.done():
        fut.cancel()
  _host_path(host_id).unlink(missing_ok=True)


async def _ask_runner_to_disconnect(ch: _Channel) -> str:
  request_id = secrets.token_hex(8)
  loop = asyncio.get_running_loop()
  fut: asyncio.Future = loop.create_future()
  ch.pending[request_id] = fut
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
    ch.pending.pop(request_id, None)
    if not fut.done():
      fut.cancel()
  if int(result.get("exit_code", 1)) != 0:
    raise HTTPException(
      status_code=502,
      detail=result.get("stderr") or "The machine could not stop its daemon.",
    )
  return "acknowledged"


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
  if ch is None:
    raise HTTPException(
      status_code=409,
      detail=f"{host.get('name') or 'That machine'} is offline right now.",
    )
  request_id = secrets.token_hex(8)
  loop = asyncio.get_running_loop()
  fut: asyncio.Future = loop.create_future()
  ch.pending[request_id] = fut
  timeout = max(1, min(int(body.timeout or _DEFAULT_EXEC_TIMEOUT), 3600))
  await ch.queue.put(
    {
      "type": "exec",
      "request_id": request_id,
      "cmd": body.cmd,
      "cwd": body.cwd,
      "timeout": timeout,
    }
  )
  try:
    # Give the runner its own command timeout plus a network grace margin.
    result = await asyncio.wait_for(fut, timeout=timeout + 15)
  except asyncio.TimeoutError:
    raise HTTPException(status_code=504, detail="The command timed out.")
  except asyncio.CancelledError:
    raise HTTPException(status_code=409, detail="The machine disconnected.")
  finally:
    ch.pending.pop(request_id, None)
  stdout, stdout_truncated = _cap_stream(result.get("stdout", ""))
  stderr, stderr_truncated = _cap_stream(result.get("stderr", ""))
  exit_code = int(result.get("exit_code", 0))
  return {
    "stdout": stdout,
    "stderr": stderr,
    "exit_code": exit_code,
    "truncated": stdout_truncated or stderr_truncated,
    "timed_out": bool(result.get("timed_out", False)),
  }


def _cap_stream(text: str) -> tuple[str, bool]:
  if len(text) <= _MAX_EXEC_STREAM:
    return text, False
  marker = "\n…[output truncated]…\n"
  kept = _MAX_EXEC_STREAM - len(marker)
  head = (kept + 1) // 2
  tail = kept // 2
  return f"{text[:head]}{marker}{text[-tail:]}", True


# --------------------------------------------------------------------------- #
# Runner surface (host-token authenticated)
# --------------------------------------------------------------------------- #
@router.post("/pair")
async def pair(body: PairBody) -> dict:
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
  # The runner reports its OS on connect so the app can label the machine.
  plat = request.query_params.get("platform")
  if plat:
    host["platform"] = plat[:80]
    _save_host(host)
  ch = _Channel()
  # A reconnecting runner replaces any stale channel.
  old = _channels.get(host_id)
  if old is not None:
    for fut in old.pending.values():
      if not fut.done():
        fut.cancel()
  _channels[host_id] = ch
  _touch(host_id)

  async def gen():
    try:
      yield ": connected\n\n"
      while True:
        if await request.is_disconnected():
          break
        try:
          evt = await asyncio.wait_for(ch.queue.get(), timeout=_HEARTBEAT_SECONDS)
          yield f"data: {json.dumps(evt)}\n\n"
        except asyncio.TimeoutError:
          yield ": ping\n\n"
    finally:
      if _channels.get(host_id) is ch:
        del _channels[host_id]
      for fut in ch.pending.values():
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
  ch = _channels.get(host["id"])
  if ch is not None:
    fut = ch.pending.get(body.request_id)
    if fut is not None and not fut.done():
      fut.set_result(
        {
          "stdout": body.stdout,
          "stderr": body.stderr,
          "exit_code": body.exit_code,
          "timed_out": body.timed_out,
        }
      )
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
