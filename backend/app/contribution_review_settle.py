"""Write the lifecycle's moves back to a review run's items, so a stuck review does not stay "reviewing".

A review's agent can die without ever reporting, and nothing reports for it. The clock and the run's chat
are the only evidence, so something has to look: every read of a run does (the lazy tick), and so does the
periodic sweep, which needs no screen open. Both go through `settle_safely`, so a write that fails (a lost
race, a locked database) never fails the read or the rest of the batch.

An accepted race: a tick that writes changes the item and bumps the run's revision, so an owner's arm of a
merge, a ready or a verdict, or an agent's report, that read the item just before the write is refused with
"This review changed. Refresh before continuing." and the caller repeats it. The window is the moment of a
transition (a stall, an answer, a verdict timing out), never a read that changes nothing; no write is lost
and no public action repeats. It is worst for a report that arrives as the hour ends, so the hour does not
end while the chat is working (see `contribution_review_lifecycle.advance`).

The write is the module's own: one revision-fenced write of one item through
`contribution_review_runs.save_effect_outcome`, built from the item as it is after the refresh, so a
result the agent reported meanwhile is never overwritten. Only a move the clock or the chat caused is
written, and so is catching a saved vector up with the `state` word (the agent reported, or another round
began), once. Seeding a vector for an item that has none is recomputed on every read and writes nothing.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import HTTPException

from app import contribution_review_lifecycle as review_lifecycle
from app import contribution_review_queries as queries
from app import contribution_review_runs as reviews
from app import contribution_review_verdicts as verdicts
from app import models

_log = logging.getLogger(__name__)


def now() -> int:
  return int(time.time())


def started_at(row) -> int:
  """When the run, and so every review in it that has not reported, began."""
  return review_lifecycle.epoch(row.created_at) or 0


def has_live(row, at: int) -> bool:
  """Whether any review in this run could still be moved by its chat or the clock."""
  saved, started = row.outcomes_json or {}, started_at(row)
  return any(review_lifecycle.state_of(review_lifecycle.review_vector(saved.get(reviews.key(target), {}), started, at))
             in review_lifecycle.LIVE
             for target in row.targets_json)


def run_vectors(row, facts: dict, at: int) -> dict[str, dict]:
  """Each target's review vector as it stands at `at`, by `repo#number`. Reads only; nothing is written."""
  saved, started = row.outcomes_json or {}, started_at(row)
  return {reviews.key(target): review_lifecycle.advance(
            review_lifecycle.review_vector(saved.get(reviews.key(target), {}), started, at), facts, at)
          for target in row.targets_json}


def item_change(current: dict, started: int, facts: dict, at: int) -> dict | None:
  """The saved item once the chat and the clock have moved it; None when they have not."""
  base = review_lifecycle.review_vector(current, started, at)
  moved = review_lifecycle.advance(base, facts, at)
  overdue = verdicts.answer_overdue(current.get("verdict_proposal"), at)
  # A vector that was saved is compared as saved, so a result the word reported (or a new round) is caught up.
  held = current[review_lifecycle.KEY] if review_lifecycle.readable(current.get(review_lifecycle.KEY)) else base
  if moved == held and overdue is None:
    return None
  item = {**current, "state": current.get("state", "reviewing")}
  if moved != held:
    item[review_lifecycle.KEY] = moved
  if overdue is not None:
    item["verdict_proposal"] = overdue
  return item


def settle_run(db, row, facts: dict, at: int) -> int:
  """Write every item of this run that the chat or the clock moved. Returns how many were written.

  Raises HTTPException(409) when the run kept changing under the write; the next look tries again.
  """
  started, written = started_at(row), 0
  for target in row.targets_json:
    item_key = reviews.key(target)
    if item_change((row.outcomes_json or {}).get(item_key, {}), started, facts, at) is None:
      continue
    reviews.save_effect_outcome(db, row, item_key, lambda current: item_change(current, started, facts, at))
    written += 1
  return written


def settle_safely(db, row, facts: dict, at: int) -> int:
  """`settle_run` for a caller that must go on whatever the write meets; a failed write is logged, never raised.

  Losing a race (409) and a database that is locked or full are the same here: nothing was written, the
  view is computed from the same facts anyway, and the next look or tick tries again. The rollback leaves
  the session usable for the next row.
  """
  try:
    return settle_run(db, row, facts, at)
  except HTTPException as exc:
    _log.warning("review run %s changed while its lifecycle was written: %s", row.id, exc.detail)
  except Exception:  # noqa: BLE001 - nothing the lazy write meets may fail a read or end a sweep
    db.rollback()
    _log.warning("review run %s: its lifecycle could not be written", row.id, exc_info=True)
  return 0


def sweep(db, facts_of: Callable, at: int | None = None) -> int:
  """The periodic tick: settle every run that still has a review or a verdict only the clock can end.

  `facts_of(row)` is the run's chat as {stopped, asking, ended}. A run that fails is logged and skipped,
  so one bad row never holds the others. Returns how many items were written.
  """
  at = now() if at is None else at
  ids = queries.run_ids(db, queries.OPEN_REVIEW)
  rows = db.query(models.ContributionReviewRun).filter(models.ContributionReviewRun.id.in_(ids)).all() if ids else []
  written = 0
  for row in rows:
    try:
      facts = facts_of(row)
    except Exception:  # noqa: BLE001 - one run whose chat cannot be read must not stop the others
      _log.warning("review run %s: its chat could not be read for the lifecycle sweep", row.id, exc_info=True)
      continue
    written += settle_safely(db, row, facts, at)
  return written
