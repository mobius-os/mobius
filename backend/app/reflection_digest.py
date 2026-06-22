"""Pure helpers for the nightly Reflection per-app digest.

Kept side-effect-free and importable so the error-classification logic is
unit-testable, while `core-apps/reflection/fetch.sh` stays the HTTP/IO layer
that fetches the inputs and writes `per-app-digest.json`. The fetch.sh
heredoc runs with `PYTHONPATH=/app`, so it imports this as
`from app import reflection_digest`.

This summarizes the `app_error` events the platform records in
`/data/logs/activity.jsonl` (uncaught JS errors POSTed to
`/api/client-error`). That channel is SEPARATE from each app's
`signals.jsonl` (explicit `window.mobius.signal('error', ...)` calls,
which feed the digest's `last_5_errors`): an app that simply throws never
calls `signal('error')`, so its crashes only show up here. Keeping the two
channels as distinct digest fields lets the agent tell "the app explicitly
reported this" from "the browser caught an uncaught throw."
"""

from __future__ import annotations

from typing import Any, Iterable


def summarize_app_errors(
  events: Iterable[dict], recent_per_app: int = 5, message_cap: int = 500,
) -> dict[str, Any]:
  """Bucket `app_error` activity events into per-app and shell summaries.

  `events` is an iterable of parsed activity.jsonl dicts (mixed event types;
  non-`app_error` rows are ignored). Returns::

    {
      "by_app": {"<app_id>": {"count": int, "recent": [{ts, message, where}]}},
      "shell":  {"count": int, "recent": [{ts, message, where}]},
    }

  - Stacks are deliberately DROPPED. An activity `app_error` can carry an
    ~8 KB stack; the digest must stay compact, and message + where is enough
    to triage. Messages are capped at `message_cap` chars.
  - Shell errors are `app_error` rows with NO `app_id` (the owner/shell JWT
    carries none) — they bucket under "shell", never under an app. A present
    `app_id` (a real installed app) buckets under that id.
  - `recent` keeps the most recent `recent_per_app` entries in arrival order
    (the log is chronological, so that is oldest→newest of the kept window).
  """
  by_app: dict[str, dict[str, Any]] = {}
  shell: dict[str, Any] = {"count": 0, "recent": []}
  for ev in events:
    if not isinstance(ev, dict) or ev.get("ev") != "app_error":
      continue
    entry = {
      "ts": ev.get("ts"),
      "message": str(ev.get("message", ""))[:message_cap],
      "where": ev.get("where"),
    }
    app_id = ev.get("app_id")
    # No app_id == a shell error (owner JWT). A present id is an installed app.
    if app_id is None:
      bucket = shell
    else:
      bucket = by_app.setdefault(str(app_id), {"count": 0, "recent": []})
    bucket["count"] += 1
    bucket["recent"].append(entry)
    if len(bucket["recent"]) > recent_per_app:
      del bucket["recent"][0]
  return {"by_app": by_app, "shell": shell}
