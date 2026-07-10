#!/usr/bin/env python3
"""Report and optionally prune agent-browser Chrome profiles.

Agent-browser profiles are per-chat Chromium user-data dirs. They are a cache
and auth/session mirror for the agent's own browser, not the partner's chat
transcript, but they can still contain cookies/localStorage and they are large.

Default mode is deliberately read-only. To delete anything, pass both a target
set (for example --include-existing-chats) and --delete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


CHAT_PROFILE_RE = re.compile(r"^chat-([0-9a-fA-F-]{36})$")


@dataclass
class ProfileRow:
  name: str
  path: str
  category: str
  size_bytes: int
  age_days: float
  chat_id: str | None = None
  chat_inactive_days: float | None = None
  run_status: str | None = None
  selected: bool = False
  reason: str = ""


def _du_bytes(path: Path) -> int:
  try:
    out = subprocess.check_output(
      ["du", "-sk", str(path)],
      stderr=subprocess.DEVNULL,
      text=True,
    )
    return int(out.split()[0]) * 1024
  except Exception:
    total = 0
    for root, _dirs, files in os.walk(path):
      for name in files:
        try:
          total += (Path(root) / name).stat().st_size
        except OSError:
          pass
    return total


def _load_chats(db_path: Path) -> dict[str, dict[str, Any]]:
  if not db_path.exists():
    return {}
  conn = sqlite3.connect(str(db_path))
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      "select id, deleted_at, updated_at, activity_at, run_status from chats"
    ).fetchall()
  except sqlite3.Error:
    return {}
  finally:
    conn.close()
  return {str(row["id"]): dict(row) for row in rows}


def _parse_sqlite_datetime(value: Any) -> float | None:
  if value in (None, ""):
    return None
  if isinstance(value, (int, float)):
    return float(value)
  text = str(value)
  for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
    try:
      return datetime.strptime(text, fmt).timestamp()
    except ValueError:
      pass
  try:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
  except ValueError:
    return None


def _process_rss() -> tuple[int, list[dict[str, Any]]]:
  try:
    out = subprocess.check_output(
      ["ps", "-eo", "pid,ppid,rss,comm,args"],
      text=True,
    )
  except Exception:
    return 0, []
  rows: list[dict[str, Any]] = []
  total = 0
  for line in out.splitlines()[1:]:
    low = line.lower()
    if (
      "agent-browser" not in low
      and "chrome" not in low
      and "chromium" not in low
    ):
      continue
    parts = line.split(None, 4)
    if len(parts) < 5:
      continue
    pid, ppid, rss_kib, comm, args = parts
    try:
      rss_bytes = int(rss_kib) * 1024
    except ValueError:
      continue
    total += rss_bytes
    rows.append({
      "pid": int(pid),
      "ppid": int(ppid),
      "rss_bytes": rss_bytes,
      "comm": comm,
      "args": args,
    })
  rows.sort(key=lambda row: int(row["rss_bytes"]), reverse=True)
  return total, rows


def _fmt_bytes(n: int) -> str:
  value = float(n)
  for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
    if value < 1024 or unit == "TiB":
      return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
    value /= 1024
  return f"{n} B"


def collect(args: argparse.Namespace) -> tuple[list[ProfileRow], dict[str, Any]]:
  root = Path(args.root)
  chats = _load_chats(Path(args.db))
  now = time.time()
  rows: list[ProfileRow] = []
  if root.exists():
    for path in sorted(root.iterdir(), key=lambda p: p.name):
      if not path.is_dir():
        continue
      name = path.name
      stat = path.stat()
      age_days = max(0.0, (now - stat.st_mtime) / 86400)
      size = _du_bytes(path)
      chat_id: str | None = None
      category = "non_chat_named"
      inactive_days: float | None = None
      run_status: str | None = None
      match = CHAT_PROFILE_RE.fullmatch(name)
      if match:
        chat_id = match.group(1)
        chat = chats.get(chat_id)
        if not chat:
          category = "orphan_chat_id"
        elif chat.get("deleted_at"):
          category = "soft_deleted_chat"
        else:
          category = "existing_chat"
          run_status = chat.get("run_status")
          activity_ts = (
            _parse_sqlite_datetime(chat.get("activity_at"))
            or _parse_sqlite_datetime(chat.get("updated_at"))
          )
          if activity_ts is not None:
            inactive_days = max(0.0, (now - activity_ts) / 86400)
      rows.append(ProfileRow(
        name=name,
        path=str(path),
        category=category,
        size_bytes=size,
        age_days=age_days,
        chat_id=chat_id,
        chat_inactive_days=inactive_days,
        run_status=run_status,
      ))

  for row in rows:
    old_enough = row.age_days >= args.older_than_days
    inactive_enough = (
      row.chat_inactive_days is None
      or row.chat_inactive_days >= args.older_than_days
    )
    if row.category == "orphan_chat_id" and old_enough:
      row.selected = True
      row.reason = f"orphan chat profile older than {args.older_than_days:g}d"
    elif row.category == "soft_deleted_chat" and old_enough:
      row.selected = True
      row.reason = f"soft-deleted chat profile older than {args.older_than_days:g}d"
    elif (
      row.category == "existing_chat"
      and args.include_existing_chats
      and old_enough
      and inactive_enough
      and row.run_status != "running"
    ):
      row.selected = True
      row.reason = (
        f"existing chat inactive/profile untouched for at least "
        f"{args.older_than_days:g}d"
      )
    elif (
      row.category == "non_chat_named"
      and args.include_non_chat
      and old_enough
    ):
      row.selected = True
      row.reason = f"non-chat profile older than {args.older_than_days:g}d"

  rss_total, processes = _process_rss()
  meta = {
    "root": str(root),
    "db": str(args.db),
    "profile_count": len(rows),
    "profile_bytes": sum(row.size_bytes for row in rows),
    "selected_count": sum(1 for row in rows if row.selected),
    "selected_bytes": sum(row.size_bytes for row in rows if row.selected),
    "browser_process_rss_bytes": rss_total,
    "browser_process_count": len(processes),
    "browser_processes": processes[: args.process_limit],
  }
  return rows, meta


def print_text(rows: list[ProfileRow], meta: dict[str, Any], args) -> None:
  print("Agent-browser profile report")
  print(f"root: {meta['root']}")
  print(
    "profiles: "
    f"{meta['profile_count']} dirs, {_fmt_bytes(meta['profile_bytes'])}"
  )
  print(
    "live browser processes: "
    f"{meta['browser_process_count']}, "
    f"RSS {_fmt_bytes(meta['browser_process_rss_bytes'])}"
  )
  print(
    "selection: "
    f"{meta['selected_count']} dirs, {_fmt_bytes(meta['selected_bytes'])}"
    + (" (delete mode)" if args.delete else " (dry run)")
  )
  print()

  categories = sorted({row.category for row in rows})
  for category in categories:
    group = [row for row in rows if row.category == category]
    selected = [row for row in group if row.selected]
    print(
      f"{category}: {len(group)} dirs, "
      f"{_fmt_bytes(sum(row.size_bytes for row in group))}; "
      f"selected {len(selected)}, "
      f"{_fmt_bytes(sum(row.size_bytes for row in selected))}"
    )

  chosen = [row for row in rows if row.selected]
  if chosen:
    print("\nSelected profiles:")
    for row in sorted(chosen, key=lambda r: r.size_bytes, reverse=True):
      inactive = (
        "" if row.chat_inactive_days is None
        else f", chat inactive {row.chat_inactive_days:.1f}d"
      )
      print(
        f"  {_fmt_bytes(row.size_bytes):>10}  "
        f"{row.age_days:5.1f}d old{inactive}  {row.name}"
      )

  processes = meta.get("browser_processes") or []
  if processes:
    print("\nLargest live browser processes:")
    for proc in processes:
      print(
        f"  {_fmt_bytes(proc['rss_bytes']):>10}  "
        f"pid={proc['pid']} {proc['comm']} {proc['args'][:140]}"
      )

  if not args.delete:
    print(
      "\nDry run only. Re-run with --delete plus the same selection flags "
      "to remove the selected profile directories."
    )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "--root",
    default="/data/agent-browser-profiles",
    help="agent-browser profile root",
  )
  parser.add_argument(
    "--db",
    default="/data/db/ultimate.db",
    help="Möbius SQLite database path",
  )
  parser.add_argument(
    "--older-than-days",
    type=float,
    default=14.0,
    help="minimum profile age for cleanup selection",
  )
  parser.add_argument(
    "--include-existing-chats",
    action="store_true",
    help=(
      "also select profiles for existing, non-running chats when both the "
      "profile and chat activity are older than --older-than-days"
    ),
  )
  parser.add_argument(
    "--include-non-chat",
    action="store_true",
    help="also select non chat-UUID profile directories",
  )
  parser.add_argument(
    "--delete",
    action="store_true",
    help="delete selected profile directories; default is dry-run",
  )
  parser.add_argument(
    "--json",
    action="store_true",
    help="print machine-readable JSON",
  )
  parser.add_argument(
    "--process-limit",
    type=int,
    default=12,
    help="number of live browser processes to include in text/json output",
  )
  args = parser.parse_args(argv)

  if args.older_than_days < 0:
    parser.error("--older-than-days must be non-negative")

  rows, meta = collect(args)

  deleted: list[str] = []
  errors: list[dict[str, str]] = []
  if args.delete:
    for row in rows:
      if not row.selected:
        continue
      try:
        shutil.rmtree(row.path)
        deleted.append(row.path)
      except Exception as exc:  # noqa: BLE001 - report and continue cleanup
        errors.append({"path": row.path, "error": str(exc)})
    meta["deleted"] = deleted
    meta["errors"] = errors

  if args.json:
    print(json.dumps({
      "meta": meta,
      "profiles": [asdict(row) for row in rows],
    }, indent=2))
  else:
    print_text(rows, meta, args)
    if errors:
      print("\nDelete errors:", file=sys.stderr)
      for err in errors:
        print(f"  {err['path']}: {err['error']}", file=sys.stderr)
  return 1 if errors else 0


if __name__ == "__main__":
  raise SystemExit(main())
