#!/usr/bin/env python3
"""Resolve a Möbius chat to an exact provider session and coach its fork."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable

from fork_session import ForkError, ForkResult, fork_session


def _chat_session(db_path: Path, chat_id: str) -> tuple[str, str]:
  try:
    with sqlite3.connect(db_path) as con:
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
      provider = (provider or "claude").strip().lower()

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


def coach_chat(
  chat_id: str,
  prompt: str,
  *,
  data_dir: Path = Path("/data"),
  driver: Callable[[str, str, str, str], ForkResult] = fork_session,
) -> dict[str, object]:
  provider, session_id = _chat_session(
    data_dir / "db" / "ultimate.db", chat_id
  )
  result = driver(provider, session_id, str(data_dir), prompt)
  return {"chat_id": chat_id, **asdict(result)}


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Fork and coach a chat's exact provider session"
  )
  parser.add_argument("--json", action="store_true", dest="as_json")
  parser.add_argument("chat_id")
  parser.add_argument("prompt")
  return parser


def main(argv: list[str] | None = None) -> int:
  args = _parser().parse_args(argv)
  try:
    payload = coach_chat(
      args.chat_id,
      args.prompt,
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
