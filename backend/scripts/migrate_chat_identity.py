"""Forward-migration for prod chats (card-221, B1 + B2). One-shot, owner-triggered.

Walks every chat's transcript once and does BOTH:

- B2 (cid backfill): every legacy user row in `Chat.messages` /
  `Chat.pending_messages` lacking a `cid` gets EXACTLY `legacy-<ts>` —
  byte-identical to `chat_writer.cid_of` / the frontend `cidOf`, so warm clients
  and migrated rows derive the same message identity. A row with neither cid nor
  ts is left untouched and REPORTED (never guessed).
- B1 (tool-output extraction): every fat inline tool block (`output` over the
  inline threshold, not already reduced, no `tool_use_id`) has its full text
  stashed in the `tool_outputs` side table under a minted `legacy-<ts>-<i>` id
  and its block rewritten to a bounded excerpt. This is the write-side twin of
  the live funnel's `_reduce_tool_output`; migrating every chat is what let the
  legacy dual-read shims be deleted.
- B3 (thinking extraction): adjacent legacy reasoning fragments are coalesced
  exactly as the renderer displayed them; runs over 1KB move to the
  `thinking_traces` table and leave only lazy-fetch metadata in the transcript.

Both mutations run through the single-writer `chat_writer` actor's `MigrateChat`
command — the whole per-chat read-modify-write happens on the actor thread under
an optimistic compare-and-swap (`WHERE run_status IS NULL AND updated_at =
<snapshot>`), so a chat with a live turn in flight is DEFERRED, not clobbered,
even though this script runs as a second writer process alongside the live
server. Idle-gate the run to keep deferrals rare; deferred chats are reported
for a re-run.

Idempotent (re-runnable — already-migrated rows/blocks are no-ops), with a
`--dry-run` that only reports counts and a `--chat-id` for single-chat testing.

Run inside the container, as the mobius user, NOT as root (a root-run write
poisons /data ownership):

    docker exec -u mobius -w /app <container> \
      python -m scripts.migrate_chat_identity --dry-run
    docker exec -u mobius -w /app <container> \
      python -m scripts.migrate_chat_identity            # apply

Take a DB backup first — /data git safety-net does NOT cover db/ (it is
gitignored). The rollback is restoring that backup (see the card-221 runbook).
"""

from __future__ import annotations

import pathlib
import sys
import time

# Make `app` / `scripts` importable whether launched as `-m scripts.…` from
# /app or as a bare file path. A no-op under the supported `-m` invocation.
_BACKEND = str(pathlib.Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
  sys.path.insert(0, _BACKEND)

import argparse  # noqa: E402
from collections import Counter  # noqa: E402

from app import models  # noqa: E402
from app.chat_writer import (  # noqa: E402
  MigrateChat,
  get_writer,
  start_writer,
  stop_writer,
  writer_readiness,
)
from app.database import SessionLocal  # noqa: E402

# A chat that neither migrated content nor raced a live turn.
_INERT = {"noop", "dry_run", "missing"}


def _chat_ids(chat_id: str | None) -> list[str]:
  """All chat ids (INCLUDING soft-deleted, so recovered chats are migrated
  too), oldest first — or just `chat_id` when given."""
  db = SessionLocal()
  try:
    query = db.query(models.Chat.id).order_by(
      models.Chat.created_at, models.Chat.id
    )
    if chat_id:
      query = query.filter(models.Chat.id == chat_id)
    return [row[0] for row in query.all()]
  finally:
    db.close()


def _migrate_one(chat_id: str, dry_run: bool, timeout: float) -> dict:
  """Submit one `MigrateChat` and return its report, mapping any raised ack
  (a round-trip verify failure or actor error) to a `failed` report."""
  try:
    fut = get_writer().submit(MigrateChat(chat_id=chat_id, dry_run=dry_run))
    return fut.result(timeout=timeout)
  except Exception as exc:  # noqa: BLE001 - report, never abort the whole run
    return {"chat_id": chat_id, "status": "failed", "error": repr(exc)}


def run(
  chat_id: str | None = None,
  dry_run: bool = False,
  timeout: float = 60.0,
  verbose: bool = False,
) -> list[dict]:
  """Migrate every chat (or `chat_id`) and return the per-chat reports.

  Assumes a writer is already started (main() starts one; tests reuse the
  conftest writer). Re-checks chats deferred as active ONCE at the end — an
  idle-window run should then find them finished."""
  ids = _chat_ids(chat_id)
  results: list[dict] = []
  for cid in ids:
    res = _migrate_one(cid, dry_run, timeout)
    results.append(res)
    if verbose:
      print(
        f"  {res['status']:18} {cid}  "
        f"backfilled={res.get('backfilled', 0)} "
        f"extracted={res.get('extracted', 0)}"
        f" thinking={res.get('thinking_extracted', 0)}"
      )

  # Re-check the deferred (active-at-first-pass) chats once. A real run only:
  # a dry-run never writes, so nothing was deferred for content reasons.
  if not dry_run:
    deferred = [
      r["chat_id"] for r in results
      if r.get("status") in ("skipped_active", "deferred_locked")
    ]
    if deferred:
      recheck = {cid: _migrate_one(cid, dry_run, timeout) for cid in deferred}
      results = [
        recheck.get(r["chat_id"], r)
        if r.get("status") in ("skipped_active", "deferred_locked")
        else r
        for r in results
      ]
      if verbose:
        for cid, res in recheck.items():
          print(f"  recheck {res['status']:10} {cid}")
  return results


def summarize(results: list[dict], dry_run: bool) -> int:
  """Print the run summary; return a process exit code (non-zero iff a chat
  failed round-trip verification — a loud, investigate-me outcome)."""
  counts = Counter(r["status"] for r in results)
  backfilled = sum(r.get("backfilled", 0) for r in results)
  extracted = sum(r.get("extracted", 0) for r in results)
  thinking_extracted = sum(r.get("thinking_extracted", 0) for r in results)
  bytes_moved = sum(r.get("bytes_moved", 0) for r in results)
  unfixable = [
    (r["chat_id"], u) for r in results for u in r.get("unfixable", [])
  ]
  failed = [r for r in results if r.get("status") == "failed"]
  deferred = [
    r for r in results
    if r.get("status") in ("skipped_active", "deferred_locked")
  ]

  mode = "DRY-RUN (no writes)" if dry_run else "APPLY"
  print(f"\n=== migrate_chat_identity — {mode} ===")
  print(f"chats seen:        {len(results)}")
  for status in sorted(counts):
    print(f"  {status:18} {counts[status]}")
  print(f"rows backfilled:   {backfilled}")
  print(f"blocks extracted:  {extracted}")
  print(f"thoughts extracted:{thinking_extracted:>5}")
  print(f"bytes moved:       {bytes_moved} ({bytes_moved / 1e6:.1f} MB)")

  if deferred:
    print(f"\nDEFERRED (active/locked — re-run to migrate) [{len(deferred)}]:")
    for r in deferred:
      print(f"  {r['status']:16} {r['chat_id']}")
  if unfixable:
    print(f"\nUNFIXABLE user rows (no cid AND no ts — left untouched) "
          f"[{len(unfixable)}]:")
    for chat_id, u in unfixable:
      print(f"  chat={chat_id} {u}")
  if failed:
    print(f"\nFAILED (round-trip verify / actor error — NOT migrated) "
          f"[{len(failed)}]:")
    for r in failed:
      print(f"  chat={r['chat_id']} {r.get('error', '')}")

  return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "--dry-run", action="store_true",
    help="Report the plan (counts, unfixable rows) without any DB write.",
  )
  parser.add_argument(
    "--chat-id", default=None,
    help="Migrate only this chat id (single-chat testing).",
  )
  parser.add_argument(
    "--timeout", type=float, default=60.0,
    help="Per-chat actor ack timeout, seconds (default 60).",
  )
  parser.add_argument(
    "--verbose", action="store_true", help="Print a line per chat.",
  )
  return parser.parse_args()


def _await_writer_ready(deadline_secs: float = 15.0) -> tuple[bool, str | None]:
  """Poll `writer_readiness` until ready or the deadline.

  `start_writer` publishes the actor BEFORE its thread opens the DB session
  (proving it with a `SELECT 1`), so a readiness check immediately after start
  races that open. Poll briefly instead of failing on the transient window.
  """
  end = time.monotonic() + deadline_secs
  ready, reason = writer_readiness()
  while not ready and time.monotonic() < end:
    time.sleep(0.1)
    ready, reason = writer_readiness()
  return ready, reason


def main() -> int:
  args = _parse_args()
  start_writer(SessionLocal)
  try:
    ready, reason = _await_writer_ready()
    if not ready:
      print(f"chat writer not ready: {reason}", file=sys.stderr)
      return 3
    results = run(
      chat_id=args.chat_id,
      dry_run=args.dry_run,
      timeout=args.timeout,
      verbose=args.verbose,
    )
  finally:
    stop_writer(timeout=10)
  return summarize(results, args.dry_run)


if __name__ == "__main__":
  raise SystemExit(main())
