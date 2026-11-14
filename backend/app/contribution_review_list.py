"""What a review-runs list carries of each run: the facts a row needs, never the run's documents.

Findings, drafted verdicts, prompts, frozen evidence and test output stay in the run itself
(`GET .../review-runs/{run_id}`). Every projection here is an allow-list, so a field added to runs
later stays out of a list until someone decides a row needs it.
"""

from collections.abc import Iterable

# Where a run's PR stands, which version it covers, and the links that open it. This is the projection
# of a core run an asking app reads through, so widening it widens what core runs show other apps: a field
# only the asking app's own rows need belongs in `summary_item`. The summary of an owned run builds on it.
NAVIGATION_ITEM_FIELDS = ("repo", "number", "head_sha", "base_ref", "base_sha", "state", "url", "title",
  "review_chat_id", "review_run_id", "merge_sha", "queue_entry_id",
  "successor", "approved_head_sha", "approved_base_sha", "current_base_sha")
NAVIGATION_OPTIONS = ("provider", "model", "reasoning_effort", "post_review", "confirmation_scope")
SUMMARY_CHARS = 240
SUMMARY_OMISSION = "… open this run for the full findings"
# What a row needs to know of a verdict or a fix without reading it: whether it waits, went out, or was dropped.
VERDICT_STATUS_FIELDS = ("id", "state", "event", "head_sha", "review_id")
REPAIR_STATUS_FIELDS = ("id", "state", "head_sha", "from_sha", "decided_at")
# Run-level facts a row reads. Left out on purpose: `options` beyond NAVIGATION_OPTIONS (prompts and
# instructions), `takeover_notice` (the takeover comment's text and receipt) and `stop_semantics`
# (the same sentence on every run); the single run still carries all three.
SUMMARY_RUN_FIELDS = ("id", "request_id", "mode", "chat_id", "source_chat_id", "app_id", "state",
  "execution_state", "created_at", "chat_created_by_app_id", "can_stop", "summary")


def _pick(source: dict, keys: Iterable[str]) -> dict:
  return {key: source[key] for key in keys if key in source}


def navigation_item(item: dict) -> dict:
  brief = _pick(item, NAVIGATION_ITEM_FIELDS)
  summary = item.get("summary")
  if summary and len(summary) > SUMMARY_CHARS:
    cut = SUMMARY_CHARS - len(SUMMARY_OMISSION)
    brief.update(summary=summary[:cut] + SUMMARY_OMISSION, summary_truncated=True)
  elif summary:
    brief["summary"] = summary
  if item.get("public_review"):
    brief["public_review"] = _pick(item["public_review"], ("state", "url", "id"))
  return brief


def navigation_options(options: dict | None) -> dict:
  return _pick(options or {}, NAVIGATION_OPTIONS)


def summary_item(item: dict) -> dict:
  brief = navigation_item(item)
  if item.get("verdict_proposal"):
    brief["verdict_proposal"] = _pick(item["verdict_proposal"], VERDICT_STATUS_FIELDS)
  if item.get("repair_attempts"):
    brief["repair_attempts"] = [_pick(attempt, REPAIR_STATUS_FIELDS) for attempt in item["repair_attempts"]]
  return brief


def summary_run(run: dict) -> dict:
  """A run the asking app owns: its full view's row facts, without documents, option text or notices."""
  light = _pick(run, SUMMARY_RUN_FIELDS)
  light["items"] = [summary_item(item) for item in run["items"]]
  if "options" in run:
    light["options"] = navigation_options(run["options"])
  return light
