"""Branch a chat: a new chat that continues the conversation through one reply.

A branch is an ordinary owner chat whose transcript starts as a copy of its
source's rows up to and including the chosen reply. The source is never
changed. ``Chat.branch_origin`` records where it came from. When the chosen
reply's provider session still exists, its ``fork`` names that session and the
point where the reply ended, and the branch's first turn starts as an exact
provider fork cut there (``claude_fork_point``/``codex_fork_point``); otherwise
that turn is seeded with the copied turns as text
(``chat_context._build_branch_history``).

What the copied rows need to keep working in their new chat is copied with
them: attached upload files (``/api/chats/{id}/uploads/{name}``) and full tool
outputs (``/api/chats/{id}/tool-output/{tool_use_id}``), both addressed by the
viewing chat's id.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import chat_writer, models, transcript_rows
from app.upload_lifecycle import attachment_names

BRANCH_TITLE_PREFIX = "Branch · "
# Model choice an ordinary owner chat snapshots (providers.snapshot_chat_agent_settings).
# App-only settings (embedded scope, visibility, report briefs) stay with the
# source: a branch is the owner's own chat.
_BRANCHED_SETTINGS = ("model", "effort", "effort_by_provider")


class BranchPointError(ValueError):
  """The requested reply cannot start a branch."""


def message_key(message: dict, index: int) -> str:
  """A row's display key; mirrors ``messageKey`` in frontend chatDetailCache.js."""
  if message.get("id") is not None:
    return str(message["id"])
  if message.get("cid") is not None:
    return str(message["cid"])
  ts = message.get("ts")
  return f"{message.get('role')}-{ts if ts is not None else index}"


def branch_rows(source: models.Chat, messages: list, through_key: str) -> list:
  """The source rows a branch through ``through_key`` starts with."""
  for index in range(len(messages) - 1, -1, -1):
    row = messages[index]
    if isinstance(row, dict) and message_key(row, index) == through_key:
      break
  else:
    raise BranchPointError("That reply is no longer in this chat.")
  if row.get("role") != "assistant":
    raise BranchPointError("Only a reply can start a branch.")
  if source.active_assistant_message_id is not None and str(
    source.active_assistant_message_id
  ) == str(row.get("id")):
    raise BranchPointError("That reply is still being written.")
  return copy.deepcopy(messages[: index + 1])


def _naive_utc(value) -> datetime | None:
  if not isinstance(value, str) or not value:
    return None
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.tzinfo is not None:
    parsed = parsed.astimezone(UTC).replace(tzinfo=None)
  return parsed


def _jsonl(path: Path):
  with path.open(encoding="utf-8") as handle:
    for line in handle:
      try:
        entry = json.loads(line)
      except ValueError:
        continue
      if isinstance(entry, dict):
        yield entry


def _within(entry: dict, started_at: datetime, ended_at: datetime) -> bool:
  at = _naive_utc(entry.get("timestamp"))
  return at is not None and started_at <= at <= ended_at


def claude_fork_point(
  config_dir: Path, cwd: str, session_id: str,
  started_at: datetime, ended_at: datetime,
) -> str | None:
  """The uuid of the run's last main-conversation assistant entry.

  Claude resumes *at* an entry, so cutting at the reply's final assistant
  entry keeps the whole reply and nothing after it. A run whose last entry is
  a tool call ended mid-step; cutting there would leave an unanswered call, so
  it has no fork point.
  """
  path = (
    config_dir / "projects" / ("-" + cwd.strip("/").replace("/", "-"))
    / f"{uuid.UUID(session_id)}.jsonl"
  )
  if not path.is_file():
    return None
  last = None
  for entry in _jsonl(path):
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
      continue
    if not _within(entry, started_at, ended_at):
      continue
    content = (entry.get("message") or {}).get("content")
    calls_tool = isinstance(content, list) and any(
      isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    )
    last = None if calls_tool else entry.get("uuid")
  return last if isinstance(last, str) and last else None


def codex_fork_point(
  codex_home: Path, thread_id: str, started_at: datetime, ended_at: datetime,
) -> str | None:
  """The id of the last Codex turn the run started; forks keep whole turns."""
  matches = sorted((codex_home / "sessions").glob(f"**/rollout-*-{uuid.UUID(thread_id)}.jsonl"))
  if len(matches) != 1:
    return None
  last = None
  for entry in _jsonl(matches[0]):
    payload = entry.get("payload")
    if (
      entry.get("type") == "event_msg" and isinstance(payload, dict)
      and payload.get("type") == "task_started" and _within(entry, started_at, ended_at)
    ):
      last = payload.get("turn_id")
  return last if isinstance(last, str) and last else None


def reply_fork(db: Session, source: models.Chat, reply_id, *, data_dir: str, cwd: str) -> dict | None:
  """The provider fork a branch through ``reply_id`` can start from, if any.

  A reply's id is its run's token, and the run records the provider session
  it ended in plus when it ran. Anything missing or unreadable (an older or
  failed run, an expired session, another provider) means no fork, and the
  branch falls back to text history.
  """
  run = db.get(models.ChatRun, str(reply_id)) if reply_id is not None else None
  if (
    run is None or run.chat_id != source.id or run.provider != source.provider
    or not run.provider_session_id or run.started_at is None or run.ended_at is None
  ):
    return None
  started_at = run.started_at.replace(tzinfo=None) if run.started_at.tzinfo else run.started_at
  ended_at = run.ended_at.replace(tzinfo=None) if run.ended_at.tzinfo else run.ended_at
  auth = Path(data_dir) / "cli-auth"
  try:
    if run.provider == "claude":
      at = claude_fork_point(auth / "claude", cwd, run.provider_session_id, started_at, ended_at)
    elif run.provider == "codex":
      at = codex_fork_point(auth / "codex", run.provider_session_id, started_at, ended_at)
    else:
      return None
  except (OSError, ValueError):
    return None
  if at is None:
    return None
  return {"provider": run.provider, "session_id": run.provider_session_id, "at": at}


def _tool_use_ids(value, found: set[str]) -> set[str]:
  if isinstance(value, dict):
    tool_use_id = value.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id:
      found.add(tool_use_id)
    for child in value.values():
      _tool_use_ids(child, found)
  elif isinstance(value, list):
    for child in value:
      _tool_use_ids(child, found)
  return found


def _attachment_names(rows: list) -> set[str]:
  names: set[str] = set()
  for row in rows:
    if not isinstance(row, dict):
      continue
    names |= attachment_names(row.get("attachments"))
    for block in row.get("blocks") or []:
      if isinstance(block, dict):
        names |= attachment_names(block.get("attachments"))
  return names


def _link_or_copy(source: Path, target: Path) -> None:
  try:
    os.link(source, target)
  except OSError:
    shutil.copy2(source, target)


def branch_chat(
  db: Session, source: models.Chat, through_key: str, *, data_dir: str,
) -> models.Chat:
  """Create and commit the branch of ``source`` through one reply.

  Upload files are linked into the branch's folder before the commit and the
  folder is removed if the commit fails, so a failed branch leaves nothing.
  """
  rows = branch_rows(source, transcript_rows.read_all(db, source), through_key)
  # Top-level chats run in the data directory (chat.py), which is also where
  # Claude keeps the session transcripts a fork resumes from.
  fork = reply_fork(db, source, rows[-1].get("id"), data_dir=data_dir, cwd=str(Path(data_dir)))
  settings = source.agent_settings_json if isinstance(source.agent_settings_json, dict) else {}
  branched_settings = {k: copy.deepcopy(settings[k]) for k in _BRANCHED_SETTINGS if k in settings}
  title = f"{BRANCH_TITLE_PREFIX}{source.title or 'New chat'}"[:256]
  branch = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title=title,
    messages=rows,
    provider=source.provider,
    agent_settings_json=branched_settings or None,
    auto_resume_on_limit=bool(source.auto_resume_on_limit),
    auto_resume_on_restart=True,
    project_id=source.project_id,
    branch_origin={
      "chat_id": source.id,
      "title": source.title,
      "through_count": len(rows),
      "fork": fork,
    },
  )

  names = _attachment_names(rows)
  source_uploads = Path(data_dir) / "chats" / source.id / "uploads"
  branch_uploads = Path(data_dir) / "chats" / branch.id / "uploads"
  uploads = []
  for entry in source.uploads or []:
    name = entry.get("name") if isinstance(entry, dict) else None
    if name not in names or not (source_uploads / name).is_file():
      continue
    branch_uploads.mkdir(parents=True, exist_ok=True)
    _link_or_copy(source_uploads / name, branch_uploads / name)
    uploads.append(copy.deepcopy(entry))
  branch.uploads = uploads

  tool_ids = _tool_use_ids(rows, set())
  try:
    db.add(branch)
    ordered_ids = sorted(tool_ids)
    # Chunked to stay far below SQLite's bound-parameter limit on long chats.
    for start in range(0, len(ordered_ids), 500):
      for output in db.query(models.ToolOutput).filter(
        models.ToolOutput.chat_id == source.id,
        models.ToolOutput.tool_use_id.in_(ordered_ids[start:start + 500]),
      ).all():
        db.add(models.ToolOutput(
          chat_id=branch.id, tool_use_id=output.tool_use_id, output=output.output,
        ))
    db.commit()
  except Exception:
    db.rollback()
    shutil.rmtree(Path(data_dir) / "chats" / branch.id, ignore_errors=True)
    raise
  return branch
