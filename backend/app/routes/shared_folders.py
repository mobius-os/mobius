"""Routes for folders the owner shares from a browser with their chat agents."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import (
  Principal,
  _owner_principal_or_403,
  get_principal,
  reject_cross_site,
  require_owner_input_principal,
)
from app.shared_folders import DeviceReport, SharedFolder, registry


router = APIRouter(prefix="/api/shared-folders", tags=["shared-folders"])

MAX_READ_BYTES = 4 * 1024 * 1024
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 1024
MAX_PATH_DEPTH = 32
MAX_LIST_LIMIT = 500
# Claude reads at most 2,576 px on the long edge, so photos are shrunk on the
# device to at most this before they travel.
MAX_IMAGE_EDGE = 4096

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_UNSAFE_SEGMENT = re.compile(r"[\x00-\x1f\x7f/\\]")


def _installation_owner(principal: Principal) -> None:
  # Shared-browser guests, and agents those guests started, may use the
  # workspace but never reach the installation owner's own device.
  if principal.browser is not None:
    raise HTTPException(403, "Shared folders belong to the installation owner.")


def browser_owner(principal: Principal = Depends(get_principal)) -> Principal:
  """The owner's own browser: human input, never an agent or a guest."""
  _owner_principal_or_403(principal)
  require_owner_input_principal(principal)
  _installation_owner(principal)
  return principal


def _live_owner_chat(principal: Principal, db: Session) -> None:
  # Only the owner's own chats, which the owner can watch and Stop. A chat or
  # turn an app started (News, Reflection, scheduled jobs, an app's chat
  # panel) runs unattended and reads untrusted content, so it gets no folder.
  chat = db.get(models.Chat, principal.chat_id) if principal.chat_id else None
  run = db.get(models.ChatRun, principal.run_id) if principal.run_id else None
  if chat is None or run is None or chat.created_by_app_id is not None or run.initiated_by_app_id is not None:
    raise HTTPException(
      403, "Shared folders are only for the owner's own chats, not background or scheduled runs.",
    )


def folder_reader(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> Principal:
  """The owner's browser or one of the owner's live chat agents."""
  _owner_principal_or_403(principal)
  _installation_owner(principal)
  if principal.chat_id or principal.run_id:
    _live_owner_chat(principal, db)
  return principal


def folder_agent(principal: Principal = Depends(folder_reader)) -> Principal:
  if not principal.chat_id:
    raise HTTPException(403, "Folder commands require a chat agent token.")
  return principal


def normalized_path(path: str, *, allow_root: bool) -> str:
  """A path inside the shared folder, as plain segments; never escapes it."""
  stripped = path.strip().strip("/")
  if not stripped:
    if allow_root:
      return ""
    raise ValueError("a file path inside the folder is required")
  if len(stripped) > MAX_PATH_CHARS:
    raise ValueError("path is too long")
  segments = stripped.split("/")
  if len(segments) > MAX_PATH_DEPTH:
    raise ValueError("path is nested too deeply")
  for segment in segments:
    if segment in ("", ".", "..") or _UNSAFE_SEGMENT.search(segment):
      raise ValueError("path must be plain folder and file names inside the shared folder")
  return "/".join(segments)


def _content_bytes(content: str, encoding: str | None) -> int:
  if encoding == "base64":
    return len(content) * 3 // 4 - len(content) + len(content.rstrip("="))
  return len(content.encode("utf-8", errors="surrogatepass"))


class FolderBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  id: Identifier
  name: Label
  mode: Literal["read", "edit"]
  access: Literal["remembered", "visit"]
  state: Literal["ready", "needs_permission", "needs_pick", "paused"]


class DeviceReportBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  sequence: int = Field(ge=1)
  deviceId: Identifier
  deviceLabel: Label
  folders: list[FolderBody] = Field(max_length=50)


# Which optional fields each operation may carry. Anything else is refused so a
# command means exactly what its operation name says.
_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
  "list": frozenset({"cursor", "limit"}),
  "info": frozenset(),
  "read": frozenset({"offset", "length", "fromEnd", "encoding"}),
  "image": frozenset({"maxEdge"}),
  "write": frozenset({"content", "encoding", "overwrite"}),
  "move": frozenset({"to"}),
  "mkdir": frozenset(),
  "bin": frozenset(),
}
_ROOT_ALLOWED = frozenset({"list", "info"})
_OPTIONAL_FIELDS = frozenset().union(*_ALLOWED_FIELDS.values())


class FolderCommandBody(BaseModel):
  """Closed command vocabulary: files are read and written, never run.

  A written file can still be run later by other programs (a Git hook, an
  editor task), so the browser refuses changes to hidden names and the bin.
  """

  model_config = ConfigDict(extra="forbid")

  op: Literal["list", "info", "read", "image", "write", "move", "mkdir", "bin"]
  path: str = Field(default="", max_length=MAX_PATH_CHARS + 2)
  to: str | None = Field(default=None, max_length=MAX_PATH_CHARS + 2)
  cursor: int | None = Field(default=None, ge=0)
  limit: int | None = Field(default=None, ge=1, le=MAX_LIST_LIMIT)
  offset: int | None = Field(default=None, ge=0)
  length: int | None = Field(default=None, ge=1, le=MAX_READ_BYTES)
  fromEnd: bool | None = None
  encoding: Literal["text", "base64"] | None = None
  content: str | None = Field(default=None, max_length=(MAX_WRITE_BYTES // 3 + 1) * 4)
  overwrite: bool | None = None
  maxEdge: int | None = Field(default=None, ge=256, le=MAX_IMAGE_EDGE)

  @model_validator(mode="after")
  def confine_to_operation(self) -> "FolderCommandBody":
    supplied = {name for name in _OPTIONAL_FIELDS if getattr(self, name) is not None}
    unexpected = supplied - _ALLOWED_FIELDS[self.op]
    if unexpected:
      raise ValueError(f"{self.op} does not accept: {', '.join(sorted(unexpected))}")
    self.path = normalized_path(self.path, allow_root=self.op in _ROOT_ALLOWED)
    if self.op == "move":
      if self.to is None:
        raise ValueError("move requires a destination path")
      self.to = normalized_path(self.to, allow_root=False)
    if self.op == "write":
      if self.content is None:
        raise ValueError("write requires content")
      # Measure bytes, not characters: 4 MB of letters can be 16 MB of UTF-8.
      if _content_bytes(self.content, self.encoding) > MAX_WRITE_BYTES:
        raise ValueError("write at most 4 MB at a time")
    return self

  def wire(self) -> dict[str, Any]:
    return self.model_dump(exclude_none=True)


class BrowserAnswerBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  subscriptionId: str = Field(min_length=1, max_length=64)
  commandId: str = Field(min_length=1, max_length=64)
  ok: bool
  result: dict[str, Any] | list[Any] | str | int | float | bool | None = None
  code: str | None = Field(default=None, max_length=40)
  error: str | None = Field(default=None, max_length=1000)


@router.get("")
async def list_shared_folders(_: Principal = Depends(folder_reader)) -> dict[str, Any]:
  return {"folders": registry.folders()}


@router.put(
  "/devices/{subscription_id}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def report_device_folders(
  subscription_id: str,
  body: DeviceReportBody,
  _: Principal = Depends(browser_owner),
) -> None:
  """Bind this browser's folders to its live system stream.

  The report vanishes with that stream, like visible-app presence; a 404 tells
  the shell its stream is gone and the next connection must report afresh.
  """
  report = DeviceReport(
    subscription_id=subscription_id,
    device_id=body.deviceId,
    device_label=body.deviceLabel,
    sequence=body.sequence,
    folders=tuple(SharedFolder(**folder.model_dump()) for folder in body.folders),
  )
  if not registry.report(report):
    raise HTTPException(404, "System stream not found.")


class PauseBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  folderIds: list[Identifier] | None = Field(default=None, max_length=MAX_LIST_LIMIT)


@router.post("/pause", dependencies=[Depends(reject_cross_site)])
async def pause_shared_folders(
  body: PauseBody,
  _: Principal = Depends(browser_owner),
) -> dict[str, list[str]]:
  """Stop from any of the owner's devices: every device refuses at once.

  The browsers holding the folders are told to pause them for good; the
  owner resumes a folder in Settings on the device that shares it.
  """
  return {"paused": registry.pause(body.folderIds)}


@router.post("/responses", dependencies=[Depends(reject_cross_site)])
async def answer_folder_command(
  body: BrowserAnswerBody,
  _: Principal = Depends(browser_owner),
) -> dict[str, bool]:
  # Count UTF-8 bytes as sent: ASCII escaping would count "ж" as six bytes.
  encoded = json.dumps(body.result, ensure_ascii=False, separators=(",", ":")).encode()
  if len(encoded) > MAX_RESPONSE_BYTES:
    accepted = registry.answer(body.subscriptionId, body.commandId, {
      "ok": False, "code": "too_large", "error": "The answer was too large. Read a smaller piece.",
    })
    raise HTTPException(413 if accepted else 409, "Folder answer is too large.")
  outcome = {"ok": body.ok, "result": body.result, "code": body.code, "error": body.error}
  if not registry.answer(body.subscriptionId, body.commandId, outcome):
    raise HTTPException(409, "Command is no longer pending.")
  return {"accepted": True}


@router.post("/{folder_id}/commands", dependencies=[Depends(reject_cross_site)])
async def run_folder_command(
  folder_id: Identifier,
  body: FolderCommandBody,
  _: Principal = Depends(folder_agent),
) -> dict[str, Any]:
  """Run one command; a refusal is an ordinary result the agent can relay."""
  outcome = await registry.issue(folder_id, body.wire())
  if outcome.get("ok"):
    return {"ok": True, "result": outcome.get("result")}
  return {
    "ok": False,
    "code": outcome.get("code") or "device_error",
    "error": outcome.get("error") or "The browser could not complete the command.",
  }
