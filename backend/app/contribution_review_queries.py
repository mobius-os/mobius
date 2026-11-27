"""Which review runs hold items in a given saved state, asked of the database instead of every row.

A run keeps one entry per PR in `outcomes_json`, under `repo#number` with the repo lowercased. A PR whose
agent has not reported has no entry at all, and `contribution_review_runs.view` reads that as `reviewing`, so
an absent entry counts here too. The questions are answered with SQLite's JSON functions, the same way
`contribution_review_runs._transition_candidates` asks which grants name a PR. They only narrow the rows to
read: the caller still decides from the run itself.
"""
from sqlalchemy import text

# One target's saved entry, or a field of it. `t` is the target being looked at, `r` the run.
_KEY = "'$.\"' || lower(json_extract(t.value, '$.repo')) || '#' || json_extract(t.value, '$.number') || '\"{path}'"


def _saved(path: str = "") -> str:
  return f"json_extract(r.outcomes_json, {_KEY.format(path=path)})"


def _word() -> str:
  return f"COALESCE({_saved('.state')}, 'reviewing')"


# Still the agent's to finish: no result saved, and the review is not stalled, stopped or settled yet.
# A saved vector the word has outrun is listed too, so the next tick catches it up (a late result after a
# stall, or another round after a result). A verdict still posting belongs here as well: only the clock
# can end it when the request died.
OPEN_REVIEW = (
  f"({_word()} = 'reviewing' AND COALESCE({_saved('.lifecycle.regions.review.state')}, 'REVIEWING')"
  f" IN ('REVIEWING', 'ASKS', 'CLEAR', 'PROBLEMS', 'NOT_FINISHED'))"
  f" OR ({_word()} != 'reviewing' AND {_saved('.lifecycle.regions.review.state')} IN ('REVIEWING', 'ASKS', 'STALLED'))"
  f" OR {_saved('.verdict_proposal.state')} = 'posting'")

# Anything `view` can read as other than complete. Wider than that on purpose, so no run is missed.
# Every overlay `view` adds to a finished word has a line here: a verdict (an approval GitHub confirmed whose
# merge never ran is one), a readiness attempt, a public review receipt, and a repair successor.
UNFINISHED = (
  f"{_word()} NOT IN ('all_clear', 'merged')"
  f" OR {_saved('.verdict_proposal.state')} IN ('proposed', 'posting', 'unknown', 'not_dispatched')"
  f" OR ({_saved('.verdict_proposal.state')} = 'discarded' AND r.mode = 'review_merge')"
  f" OR ({_word()} = 'all_clear' AND {_saved('.verdict_proposal.state')} = 'posted'"
  f" AND {_saved('.verdict_proposal.event')} = 'APPROVE' AND r.mode = 'review_merge')"
  f" OR {_saved('.ready_attempt.state')} IN ('attempting', 'unknown', 'unattributable')"
  f" OR {_saved('.public_review.state')} IN ('posting', 'unknown', 'not_dispatched')"
  f" OR {_saved('.successor')} IS NOT NULL")


def run_ids(db, condition: str, owner_id: int | None = None) -> list[str]:
  """Ids of the runs (of one owner, or all) with at least one target that meets `condition`."""
  owned = "r.owner_id = :owner AND " if owner_id is not None else ""
  sql = text(f"SELECT r.id FROM contribution_review_runs r WHERE {owned}"
             f"EXISTS (SELECT 1 FROM json_each(r.targets_json) t WHERE {condition})")
  params = {"owner": owner_id} if owner_id is not None else {}
  return list(db.execute(sql, params).scalars())
