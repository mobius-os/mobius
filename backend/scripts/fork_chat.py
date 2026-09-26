#!/usr/bin/env python3
"""Resolve a Möbius chat to an exact provider session and coach its fork.

A *call moment* narrows the fork to one app tool call (fork_session.py says
where each provider's fork ends). It is the JSON object recorded when an agent
called an app tool::

  {"chat_id": str, "run_id": str, "provider": "claude"|"codex", "call_id": str}

``run_id`` is the ``chat_runs`` row of the turn that made the call and
``call_id`` is the provider's own id for the model's tool call. A moment
resolves only through that run's recorded provider session; it never falls back
to the chat's current session or to stored messages.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable

from fork_session import ForkError, ForkResult, fork_session


_MOMENT_FIELDS = ("chat_id", "run_id", "provider", "call_id")


def _live_chat(con: sqlite3.Connection, chat_id: str) -> tuple[str, str]:
  """Return the chat's (provider, session id); deleted chats are refused."""
  columns = {row[1] for row in con.execute("pragma table_info(chats)")}
  deleted_expr = "deleted_at" if "deleted_at" in columns else "null"
  row = con.execute(
    "select coalesce(provider,''), coalesce(session_id,''), "
    f"{deleted_expr} from chats where id=?",
    (chat_id,),
  ).fetchone()
  if row is None:
    raise ForkError(f"chat not found: {chat_id}")
  provider, session_id, deleted_at = row
  if deleted_at is not None:
    raise ForkError("deleted chats cannot be forked for coaching")
  return (provider or "claude").strip().lower(), session_id


def _chat_session(db_path: Path, chat_id: str) -> tuple[str, str]:
  try:
    with sqlite3.connect(db_path) as con:
      provider, session_id = _live_chat(con, chat_id)

      # A chat may clear its current session id after a settled turn while its
      # append-only provider link remains authoritative. Recover only an exact
      # session of the same provider; never cross providers or use messages.
      if not session_id:
        tables = {
          item[0]
          for item in con.execute(
            "select name from sqlite_master where type='table'"
          )
        }
        if "chat_session_links" in tables:
          linked = con.execute(
            "select session_id from chat_session_links "
            "where chat_id=? and provider=? "
            "order by last_seen_at desc, session_id desc limit 1",
            (chat_id, provider),
          ).fetchone()
          session_id = linked[0] if linked else ""
  except sqlite3.Error as exc:
    raise ForkError(f"could not read chat metadata: {exc}") from exc

  if provider not in {"claude", "codex"}:
    raise ForkError(f"unsupported coaching provider: {provider or '(empty)'}")
  if not session_id:
    raise ForkError("chat has no exact provider session to fork")
  return provider, session_id


def _moment_session(
  db_path: Path, chat_id: str, moment: Any
) -> tuple[str, str, str]:
  """Resolve a call moment to (provider, run session id, call id)."""
  if not isinstance(moment, dict) or any(
    not isinstance(moment.get(field), str) or not moment[field].strip()
    for field in _MOMENT_FIELDS
  ):
    raise ForkError(
      "a call moment needs string chat_id, run_id, provider, and call_id"
    )
  if moment["chat_id"] != chat_id:
    raise ForkError("call moment belongs to a different chat")
  provider = moment["provider"].strip().lower()
  try:
    with sqlite3.connect(db_path) as con:
      _live_chat(con, chat_id)
      run = con.execute(
        "select coalesce(provider,''), coalesce(provider_session_id,'') "
        "from chat_runs where id=? and chat_id=?",
        (moment["run_id"], chat_id),
      ).fetchone()
  except sqlite3.Error as exc:
    raise ForkError(f"could not read chat metadata: {exc}") from exc
  if run is None:
    raise ForkError(f"call moment run not found in this chat: {moment['run_id']}")
  run_provider, session_id = run
  if run_provider.strip().lower() != provider:
    raise ForkError(
      f"call moment provider {provider} does not match its run "
      f"({run_provider or 'unrecorded'})"
    )
  if not session_id:
    raise ForkError("call moment run has no exact provider session to fork")
  return provider, session_id, moment["call_id"]


def coach_chat(
  chat_id: str,
  prompt: str,
  *,
  moment: dict[str, Any] | None = None,
  data_dir: Path = Path("/data"),
  driver: Callable[..., ForkResult] = fork_session,
) -> dict[str, object]:
  db_path = data_dir / "db" / "ultimate.db"
  if moment is None:
    provider, session_id = _chat_session(db_path, chat_id)
    after_call_id = None
  else:
    provider, session_id, after_call_id = _moment_session(
      db_path, chat_id, moment
    )
  result = driver(
    provider, session_id, str(data_dir), prompt, after_call_id=after_call_id
  )
  return {"chat_id": chat_id, **asdict(result)}


def moment_json(text: str) -> Any:
  return json.loads(text)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Fork and coach a chat's exact provider session"
  )
  parser.add_argument("--json", action="store_true", dest="as_json")
  parser.add_argument(
    "--after-call",
    type=moment_json,
    dest="moment",
    metavar="MOMENT_JSON",
    help="fork at this recorded call moment instead of the session end",
  )
  parser.add_argument("chat_id")
  parser.add_argument("prompt")
  return parser


def main(argv: list[str] | None = None) -> int:
  args = _parser().parse_args(argv)
  try:
    payload = coach_chat(
      args.chat_id,
      args.prompt,
      moment=args.moment,
      data_dir=Path(os.environ.get("DATA_DIR", "/data")),
    )
  except ForkError as exc:
    print(f"fork-chat: {exc}", file=sys.stderr)
    return 1
  print(
    json.dumps(payload, ensure_ascii=False)
    if args.as_json
    else str(payload["answer"])
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
