"""Branch a chat: a new chat that continues the conversation through one reply.

A branch is an ordinary owner chat whose transcript starts as a copy of its
source's rows up to and including the chosen reply. The source is never
changed. ``Chat.branch_origin`` records where it came from. When the chosen
reply's provider session still exists, its ``fork`` names that session and the
point where the reply ended, and the branch's first turn starts as an exact
provider fork cut there (``claude_fork_point``/``codex_fork_point``); otherwise
that turn is reseeded from the copied rows exactly like a chat whose provider
session was lost (``chat_context._build_resumed_context``).

What the copied rows need to keep working in their new chat is copied with
them, all addressed by the viewing chat's id: attached upload files
(``/api/chats/{id}/uploads/{name}``), agent deliverables
(``/api/chats/{id}/generated-files/{name}``), screenshot and viewed-image media
(``/api/chats/{id}/media/{name}``), full tool outputs and long thinking, and
full edit diffs, whose sidecar keys embed the chat and are re-keyed for the
branch. Agent scratch is not copied: it expires with its chat by design.
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

from app import chat_writer, generated_files, models, transcript_rows
from app.codex_rollouts import rollout_paths
from app.session_links import resume_retired
from app.tool_edit_preview import edit_diff_sidecar_id
from app.upload_lifecycle import attachment_names

BRANCH_TITLE_PREFIX = "Branch · "
# Model choice an ordinary owner chat snapshots (providers.snapshot_chat_agent_settings).
# App-only settings (embedded scope, visibility, report briefs) stay with the
# source: a branch is the owner's own chat.
_BRANCHED_SETTINGS = ("model", "effort", "effort_by_provider")


class BranchPointError(ValueError):
  """The requested reply cannot start a branch."""


def branch_rows(source: models.Chat, messages: list, through_id: str) -> list:
  """The source rows a branch through the reply with stored id ``through_id`` starts with."""
  for index in range(len(messages) - 1, -1, -1):
    row = messages[index]
    if isinstance(row, dict) and row.get("id") is not None and str(row["id"]) == through_id:
      break
  else:
    raise BranchPointError("That reply is no longer in this chat.")
  if row.get("role") != "assistant":
    raise BranchPointError("Only a reply can start a branch.")
  # A run's rows are its token and, after a mid-turn owner message, its
  # `<token>:assistant:<n>` continuations (chat_event_sink); none is final yet.
  active = source.active_assistant_message_id
  if active is not None and (through_id == str(active) or through_id.startswith(f"{active}:")):
    raise BranchPointError("That reply is still being written.")
  return [_inert_copy(row) for row in messages[: index + 1]]


def _inert_copy(row):
  """A copied row that offers nothing live in the branch.

  Questions, secure inputs and Goal cards are answerable or live only through
  the chat's own durable state (``pending_question_id``, its runs and Goals),
  none of which a branch copies. A paused turn's note is the one row-level
  action: its Resume would continue a run the branch does not have.
  """
  row = copy.deepcopy(row)
  for block in (row.get("blocks") or []) if isinstance(row, dict) else []:
    if isinstance(block, dict) and block.get("type") == "error":
      block.pop("resumable", None)
  return row


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
  config_dir: str | os.PathLike | None, cwd: str, session_id: str,
  started_at: datetime, ended_at: datetime,
) -> str | None:
  """The uuid of the run's last main-conversation assistant entry.

  Claude resumes *at* an entry, so cutting at the reply's final assistant
  entry keeps the whole reply and nothing after it. A run whose last entry is
  a tool call ended mid-step; cutting there would leave an unanswered call, so
  it has no fork point.
  """
  from app.claude_sdk_runner import claude_session_path

  path = claude_session_path(config_dir, cwd, str(uuid.UUID(session_id)))
  if path is None or not path.is_file():
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
  matches = rollout_paths(codex_home, thread_id)
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


def top_level_chat_cwd(data_dir: str) -> str:
  """The working directory an owner's (non-delegated) chat turn runs in."""
  return str(data_dir) if Path(data_dir).exists() else str(Path.cwd())


def _top_level_provider_dir(provider_id: str, data_dir: str) -> str | None:
  """Where an owner's turn of ``provider_id`` keeps its sessions, from its own env.

  ``CLAUDE_CONFIG_DIR`` (None: the CLI default) or ``CODEX_HOME``, exactly as
  the provider's ``build_env`` sets them for a turn.
  """
  from app.providers import registered_provider

  key = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}[provider_id]
  return registered_provider(provider_id).build_env({}, data_dir).get(key)


def reply_fork(
  db: Session, source: models.Chat, messages: list, reply_id: str, *, data_dir: str,
) -> dict | None:
  """The provider fork a branch through ``reply_id`` can start from, if any.

  A reply's id is its run's token, and the run records the provider session
  it ended in plus when it ran. The fork point is where the run ended, so it
  is exact only for the run's last row: a reply that a mid-turn owner message
  cut short is continued in a ``<token>:assistant:<n>`` row (chat_event_sink)
  the fork would wrongly include. Anything missing or unreadable (a
  continuation, an older or failed run, an expired session, another provider)
  means no fork, and the branch is reseeded as text.
  """
  if any(isinstance(row, dict) and str(row.get("id")).startswith(f"{reply_id}:") for row in messages):
    return None
  run = db.get(models.ChatRun, reply_id)
  if (
    run is None or run.chat_id != source.id or run.provider != source.provider
    or not run.provider_session_id or run.started_at is None or run.ended_at is None
    # A retired session's history teaches withdrawn instructions (chat.py
    # never resumes one), so it is never forked either.
    or resume_retired(db, run.provider_session_id)
  ):
    return None
  started_at = run.started_at.replace(tzinfo=None) if run.started_at.tzinfo else run.started_at
  ended_at = run.ended_at.replace(tzinfo=None) if run.ended_at.tzinfo else run.ended_at
  try:
    if run.provider == "claude":
      at = claude_fork_point(
        _top_level_provider_dir("claude", data_dir), top_level_chat_cwd(data_dir),
        run.provider_session_id, started_at, ended_at,
      )
    elif run.provider == "codex":
      codex_home = _top_level_provider_dir("codex", data_dir)
      if not codex_home:
        return None
      at = codex_fork_point(Path(codex_home), run.provider_session_id, started_at, ended_at)
    else:
      return None
  except (OSError, ValueError):
    return None
  if at is None:
    return None
  return {"provider": run.provider, "session_id": run.provider_session_id, "at": at}


def branch_first_turn(db: Session, chat: models.Chat) -> bool:
  """Whether this turn of ``chat`` is a branch's first: it has never had a session.

  Derived from the append-only records of the chat's own sessions, never from
  ``Chat.session_id``, which compaction and a provider switch clear: after
  those, the branch's own turns live on in its transcript and compaction
  brief, and neither the source fork nor the copied rows may come back.
  """
  if not isinstance(chat.branch_origin, dict) or chat.session_id:
    return False
  return (
    db.query(models.ChatSessionLink.session_id).filter(
      models.ChatSessionLink.chat_id == chat.id,
    ).first() is None
    and db.query(models.ChatRun.id).filter(
      models.ChatRun.chat_id == chat.id, models.ChatRun.provider_session_id.isnot(None),
    ).first() is None
  )


def first_turn_fork(db: Session, chat: models.Chat, provider_id: str, data_dir: str) -> dict | None:
  """The exact provider fork a branch's first turn starts from, if still possible.

  ``Chat.branch_origin["fork"]`` (``reply_fork``) names the source session and
  the point the chosen reply ended. Only the same provider can fork it, the
  session must not have been retired since the branch was made, and a Claude
  transcript must still exist where an owner's Claude turn looks for it; a
  Codex fork that fails falls back to the text reseed inside its runner.
  """
  origin = chat.branch_origin
  fork = origin.get("fork") if isinstance(origin, dict) else None
  if not isinstance(fork, dict) or fork.get("provider") != provider_id:
    return None
  if not isinstance(fork.get("session_id"), str) or not isinstance(fork.get("at"), str):
    return None
  if resume_retired(db, fork["session_id"]):
    return None
  if provider_id == "claude":
    from app.claude_sdk_runner import _resumable

    if not _resumable(
      fork["session_id"], top_level_chat_cwd(data_dir), _top_level_provider_dir("claude", data_dir),
    ):
      return None
  return {"session_id": fork["session_id"], "at": fork["at"]}


def forget_fork(chat_id: str) -> None:
  """Drop a branch's fork once Claude refused it, so no later attempt retries it.

  Runs on its own session in a worker thread (``run_settled``) and touches only
  ``branch_origin``, a column no chat writer command owns.
  """
  from app.database import SessionLocal

  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    if chat is not None and isinstance(chat.branch_origin, dict) and chat.branch_origin.get("fork"):
      chat.branch_origin = {**chat.branch_origin, "fork": None}
      db.commit()


def with_resumed_context(user_message: str, resumed_context: str | None) -> str:
  """A fresh session's request, behind the chat's reseeded history when it has one.

  The one place a ``chat_context._build_resumed_context`` block joins a
  request: a lost or retired session, a branch's first turn, a refused fork.
  """
  return f"{resumed_context}\n\n{user_message}" if resumed_context else user_message


def _named_values(value, key: str, found: set[str]) -> set[str]:
  """Every non-empty string stored under ``key`` anywhere in ``value``."""
  if isinstance(value, dict):
    named = value.get(key)
    if isinstance(named, str) and named:
      found.add(named)
    for child in value.values():
      _named_values(child, key, found)
  elif isinstance(value, list):
    for child in value:
      _named_values(child, key, found)
  return found


def _copy_bare_files(source_dir: Path, target_dir: Path, names) -> set[str]:
  """Plain-copy each bare-named file of ``names`` that exists; returns those copied.

  Only a bare name may become a path, and a copy (never a link) means editing
  a file in the branch cannot change the source's.
  """
  copied = set()
  for name in names:
    if (
      not isinstance(name, str) or name in ("", "..") or Path(name).name != name
      or not (source_dir / name).is_file()
    ):
      continue
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / name, target_dir / name)
    copied.add(name)
  return copied


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


def _generated_file_names(rows: list) -> set[str]:
  """Deliverables the rows show (``generated_files`` blocks, by display name)."""
  return {
    item["name"]
    for row in rows if isinstance(row, dict)
    for block in row.get("blocks") or [] if isinstance(block, dict) and block.get("type") == "generated_files"
    for item in block.get("files") or [] if isinstance(item, dict) and isinstance(item.get("name"), str)
  }


def _rekey_edit_diffs(value, branch_id: str, renamed: dict[str, str]) -> dict[str, str]:
  """Point each copied full edit diff at the branch's own sidecar key.

  A sidecar key embeds its chat (``edit_diff_sidecar_id``); returns
  ``{source key: branch key}`` for the sidecars to copy.
  """
  if isinstance(value, dict):
    preview = value.get("edit_preview")
    tool_use_id = value.get("tool_use_id")
    if isinstance(preview, dict) and isinstance(preview.get("full_id"), str) and isinstance(tool_use_id, str):
      branch_key = edit_diff_sidecar_id(branch_id, tool_use_id)
      renamed[preview["full_id"]] = branch_key
      preview["full_id"] = branch_key
    for child in value.values():
      _rekey_edit_diffs(child, branch_id, renamed)
  elif isinstance(value, list):
    for child in value:
      _rekey_edit_diffs(child, branch_id, renamed)
  return renamed


def _copy_tool_outputs(db: Session, source_id: str, branch_id: str, keys: dict[str, str]) -> None:
  """Copy the source's stored tool outputs under ``{source key: branch key}``."""
  ordered = sorted(keys)
  # Chunked to stay far below SQLite's bound-parameter limit on long chats.
  for start in range(0, len(ordered), 500):
    for output in db.query(models.ToolOutput).filter(
      models.ToolOutput.chat_id == source_id,
      models.ToolOutput.tool_use_id.in_(ordered[start:start + 500]),
    ).all():
      db.add(models.ToolOutput(
        chat_id=branch_id, tool_use_id=keys[output.tool_use_id], output=output.output,
      ))


def branch_chat(
  db: Session, source: models.Chat, through_id: str, *, data_dir: str,
) -> models.Chat:
  """Create and commit the branch of ``source`` through one reply.

  Files are copied (never linked, so editing one in the branch cannot change
  the source) before the commit, and the branch's folder is removed if
  copying or the commit fails, so a failed branch leaves nothing.
  """
  if source.created_by_app_id is not None or db.query(models.Delegation.id).filter(
    models.Delegation.child_chat_id == source.id,
  ).first() is not None:
    # App and helper chats run under their app's or task's own settings and
    # policy, which an owner chat copied from them would silently drop.
    raise BranchPointError("Only your own chats can be branched.")
  messages = transcript_rows.read_all(db, source)
  rows = branch_rows(source, messages, through_id)
  fork = reply_fork(db, source, messages, through_id, data_dir=data_dir)
  branch_id = str(uuid.uuid4())
  edit_diffs = _rekey_edit_diffs(rows, branch_id, {})
  settings = source.agent_settings_json if isinstance(source.agent_settings_json, dict) else {}
  branched_settings = {k: copy.deepcopy(settings[k]) for k in _BRANCHED_SETTINGS if k in settings}
  title = f"{BRANCH_TITLE_PREFIX}{source.title or 'New chat'}"[:256]
  branch = chat_writer.create_chat(
    id=branch_id,
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

  source_folder = Path(data_dir) / "chats" / source.id
  branch_folder = Path(data_dir) / "chats" / branch.id
  try:
    attached = _attachment_names(rows)
    entries = [
      entry for entry in source.uploads or []
      if isinstance(entry, dict) and entry.get("name") in attached
    ]
    copied = _copy_bare_files(
      source_folder / "uploads", branch_folder / "uploads", [entry["name"] for entry in entries],
    )
    branch.uploads = [copy.deepcopy(entry) for entry in entries if entry["name"] in copied]
    # Screenshots and viewed-image snapshots a step shows (chat media).
    _copy_bare_files(
      source_folder / "media", branch_folder / "media",
      _named_values(rows, "saved_image", set()) | _named_values(rows, "viewed_image_media", set()),
    )
    db.add(branch)
    deliverables = _generated_file_names(rows)
    for record in db.query(models.GeneratedFile).filter(
      models.GeneratedFile.chat_id == source.id,
    ).all() if deliverables else []:
      # The download route serves only a bare name inside the chat's store.
      if (
        record.name not in deliverables or record.path in ("", "..")
        or Path(record.path).name != record.path
      ):
        continue
      try:
        source_file = generated_files.stored_dir(data_dir, source.id) / record.path
      except OSError:
        continue
      if not source_file.is_file():
        continue
      shutil.copy2(source_file, generated_files.stored_dir(data_dir, branch.id, create=True) / record.path)
      db.add(models.GeneratedFile(
        chat_id=branch.id, name=record.name, path=record.path,
        size=record.size, mime_type=record.mime_type,
      ))
    tool_ids = _named_values(rows, "tool_use_id", set())
    _copy_tool_outputs(db, source.id, branch.id, {key: key for key in tool_ids} | edit_diffs)
    thinking_ids = sorted(_named_values(rows, "thinking_id", set()))
    for start in range(0, len(thinking_ids), 500):
      for trace in db.query(models.ThinkingTrace).filter(
        models.ThinkingTrace.chat_id == source.id,
        models.ThinkingTrace.thinking_id.in_(thinking_ids[start:start + 500]),
      ).all():
        db.add(models.ThinkingTrace(
          chat_id=branch.id, thinking_id=trace.thinking_id, content=trace.content,
          revision=trace.revision, complete=trace.complete,
        ))
    db.commit()
  except Exception:
    db.rollback()
    shutil.rmtree(branch_folder, ignore_errors=True)
    raise
  return branch
