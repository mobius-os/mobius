"""The PR machine: where a pull request stands for the owner, and what may happen next.

Contribute renders the same table (its pr-machine.js) on every screen; the
platform asks ``action_blocker`` before every effect it performs on a PR. Both
are held to one shared case corpus
(tests/fixtures/contribution_pr_machine_cases.json), kept byte-identical with
the app's copy, so the two evaluators cannot drift. Change a rule in both
places and in that corpus together.

Facts use the app's keys (role, author, head, lifecycle, checks, mergeable,
permission, reviews[{by, state, commit, bot, sameWork}], myReview, openThreads,
job, result, outbox, readComplete). A fact that could not be read is 'unknown'
(or readComplete is False) and never enables anything that needs it.
A review's state is APPROVED or CHANGES_REQUESTED (a person's standing opinion)
or COMMENTED (their newest review, only when it is a plain comment: evidence
that they looked, never an opinion). `sameWork` is the fact reader's proof, on
an approval of an older commit, that the PR's own change is the same at that
commit and at the head (contribution_same_work); absent or None is "unknown".

The rules, in plain words:
  - Your role decides everything: your PR (author) or theirs (reviewer). An
    author never gets a public review action; an agent's check of your PR is private.
  - Merging needs an approval of this exact version by someone other than the
    author (bots never count), no change request still standing, checks that
    pass (or none exist), no conflicts, and nothing unconfirmed on its way out.
    An approval of an older commit still counts when the fact reader proved the
    PR's own change the same: a merge of the base is a new commit, not new work.
  - Only new commits bring their PR back for re-review; replies never do. A
    reviewer who looked again and only commented has not approved: a plain
    comment never lifts a change request on GitHub.
  - One agent job per PR at a time.
Pure: no I/O. Reason strings are shown to the owner verbatim (the server's 409).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

WRITE = ("WRITE", "MAINTAIN", "ADMIN")
MAINTAINER = ("MAINTAIN", "ADMIN")


def may_write(permission) -> bool:
  return permission in WRITE


def _same(a, b) -> bool:
  """GitHub logins compare without case."""
  return str(a or "").lower() == str(b or "").lower()


def _names(people: list[dict], test: Callable[[dict], bool]) -> list[str]:
  """Distinct reviewer names, without case, in first-seen order, spelled as last seen (the app's Map)."""
  names: dict[str, str] = {}
  for review in people:
    if test(review):
      names[str(review.get("by")).lower()] = review.get("by")
  return list(names.values())


def _record(value) -> dict | None:
  """A present fact record; like the app, an empty record still counts as present."""
  return value if isinstance(value, dict) else None


def _for_this_head(value, head) -> dict | None:
  value = _record(value)
  return value if value is not None and value.get("head") == head else None


def _review_standing(people: list[dict], head) -> dict:
  """Who stands where on this head: approvals (the carried ones counted), change requests, comments."""
  def at_head(review: dict) -> bool:
    return review.get("commit") == head

  def approved_here(review: dict) -> bool:
    # Of this version when of this commit, or of the same work (the reader's proof, never guessed here).
    return review.get("state") == "APPROVED" and (at_head(review) or review.get("sameWork") is True)

  approved_at_head = _names(people, approved_here)
  approved_older = _names(people, lambda r: r.get("state") == "APPROVED" and not approved_here(r))
  carried = [{"by": r.get("by"), "commit": r.get("commit")} for r in people if approved_here(r) and not at_head(r)]
  changes_older = _names(people, lambda r: r.get("state") == "CHANGES_REQUESTED" and not at_head(r))
  commented_at_head = _names(people, lambda r: r.get("state") == "COMMENTED" and at_head(r))
  # Whose change request is of an older version, but who has looked at this one since and only commented.
  looked_again = [name for name in changes_older if any(_same(other, name) for other in commented_at_head)]
  return {
    "approved_at_head": approved_at_head,
    "approved_older": approved_older,
    "carried": carried,
    "carried_only": bool(approved_at_head) and all(
      any(_same(item["by"], name) for item in carried) for name in approved_at_head),
    "changes_at_head": _names(people, lambda r: r.get("state") == "CHANGES_REQUESTED" and at_head(r)),
    "changes_older": changes_older,
    "looked_again": looked_again,
    # Who a re-review request can still ask: an older review of theirs that they have not looked past.
    "rereview": [name for name in dict.fromkeys([*changes_older, *approved_older]) if name not in looked_again],
  }


def derive(facts: dict) -> dict:
  """The few values every rule below is written in."""
  head = facts.get("head")
  reviews = facts.get("reviews") or []
  people = [r for r in reviews if not r.get("bot") and not _same(r.get("by"), facts.get("author"))]
  mine = _record(facts.get("myReview"))
  result = _for_this_head(facts.get("result"), head)
  return {
    **_review_standing(people, head),
    "mine": mine,
    "mine_at_head": mine is not None and mine.get("commit") == head,
    "result": result,
    "open_findings": result is not None and result.get("outcome") == "findings" and not result.get("setAside"),
    "outbox": _for_this_head(facts.get("outbox"), head),
    # A bot's change request is something to answer; its plain comment is only conversation.
    "bot_findings": any(r.get("bot") and r.get("state") not in ("APPROVED", "COMMENTED") for r in reviews),
  }


def _state(turn: str, label: str, headline: str, next_action: str = "") -> dict:
  return {"turn": turn, "label": label, "headline": headline, "next": next_action}


# Every state. turn: you | agent | other | none. `label` is the short chip on a
# row, `headline` the line under a PR's title, `next` the highlighted action.
STATES = {
  "merged": _state("none", "Merged", "Merged"),
  "closed": _state("none", "Closed", "Closed"),
  "unknown": _state("you", "Couldn’t read", "Couldn’t read this PR from GitHub. Try again", "retry"),
  "unconfirmed": _state("you", "Not confirmed", "GitHub didn’t confirm what was sent. Check GitHub before trying again", "check_github"),
  "sending": _state("agent", "Sending", "Sending to GitHub…"),
  "to_confirm": _state("you", "Draft to send", "A draft is ready. Read it, then send it or throw it away", "send_draft"),
  "working": _state("agent", "Agent working", "An agent is working on it"),
  "needs_answer": _state("you", "Agent asks you", "The agent has a question for you", "open_job"),
  "stalled": _state("you", "Agent stopped", "The agent stopped before it finished. Run it again", "rerun_job"),

  "author.draft": _state("you", "Draft", "Your PR · a draft. Check it first", "check_my_pr"),
  "author.draft_ready": _state("you", "Ready to open", "Your PR · checked and clean. Open it for review", "open_for_review"),
  "author.findings": _state("you", "Findings", "Your PR · your check found problems. Fix them or set them aside", "respond"),
  "author.blocked": _state("you", "Checks or conflicts", "Your PR · checks fail or it conflicts. Fix it", "respond"),
  "author.feedback": _state("you", "Feedback to answer", "Your PR · feedback is waiting. Answer every point", "respond"),
  "author.waiting_rereview": _state("other", "Waiting on re-review", "Your PR · fixes pushed. Waiting on {who} to re-review"),
  "author.looked_again": _state(
    "you", "Looked again · not approved",
    "Your PR · {who} looked again and left a comment, but GitHub still holds their change request. "
    "Ask for their approval", "ask_approval"),
  "author.approval_stale": _state("other", "Approval is old", "Your PR · approved on an older version. Waiting on {who} to look again"),
  "author.waiting_review": _state("other", "Waiting on reviewers", "Your PR · waiting on reviewers"),
  "author.approved_checks": _state("other", "Approved", "Your PR · approved. {checks}"),
  "author.approved": _state("you", "Ready to merge", "Your PR · approved by {who} at this version, {green}. Merge it", "merge"),
  "author.approved_no_rights": _state("other", "Approved", "Your PR · approved, {green}. Waiting on someone who can merge"),

  "reviewer.draft": _state("other", "Draft", "Their PR · still a draft"),
  "reviewer.to_review": _state("you", "To review", "Their PR · your turn: review it", "review"),
  "reviewer.result_ready": _state("you", "Review ready", "Their PR · your review is ready. Send a verdict or keep it private", "draft_verdict"),
  "reviewer.waiting_author": _state("other", "Waiting on {author}", "Their PR · you asked for changes. Waiting on {author}"),
  "reviewer.re_review": _state("you", "New commits · review again", "Their PR · new commits since your review. Re-review", "review"),
  "reviewer.reviewed": _state("none", "Reviewed", "Their PR · you reviewed this version"),
  "reviewer.kept_private": _state("none", "Kept private", "Their PR · you kept your review of this version private"),
  "reviewer.mergeable": _state("you", "Ready to merge", "Their PR · approved at this version, {green}. Merge it", "merge"),
}

# An approval that only carries over says so: "at this version" would be untrue of it.
# The comparison only reads added and removed lines, so it claims no more than that.
CARRIED_APPROVAL_HEADLINE = ("Your PR · approved by {who}, {green}. The lines you added and removed are the "
                             "same as when they approved. Merge it")

OUTBOX_STATES = {"unconfirmed": "unconfirmed", "sending": "sending", "drafted": "to_confirm"}
JOB_STATES = {"running": "working", "needs_answer": "needs_answer", "stalled": "stalled"}


def _state_of(facts: dict, d: dict) -> str:
  """The state, by a fixed priority: the first match wins."""
  lifecycle = facts.get("lifecycle")
  if lifecycle in ("merged", "closed"):
    return lifecycle
  if lifecycle == "unknown" or facts.get("readComplete") is False:
    return "unknown"
  outbox_state = OUTBOX_STATES.get((d["outbox"] or {}).get("state"))
  if outbox_state:
    return outbox_state
  job = _record(facts.get("job"))
  if job is not None:
    return JOB_STATES.get(job.get("state"), "working")
  if facts.get("role") == "author":
    return _author_standing(facts, d)
  return _reviewer_standing(facts, d)


def _author_standing(facts: dict, d: dict) -> str:
  if facts.get("lifecycle") == "draft":
    return _author_draft_standing(d)
  if d["open_findings"]:
    return "author.findings"
  if facts.get("checks") == "fail" or facts.get("mergeable") == "conflict":
    return "author.blocked"
  if d["changes_at_head"] or _open_threads(facts) > 0:
    return "author.feedback"
  if d["changes_older"]:
    return "author.looked_again" if d["looked_again"] else "author.waiting_rereview"
  if d["approved_at_head"]:
    return _approved_standing(facts)
  if d["approved_older"]:
    return "author.approval_stale"
  return "author.waiting_review"


def _author_draft_standing(d: dict) -> str:
  if d["open_findings"]:
    return "author.findings"
  if (d["result"] or {}).get("outcome") == "clear":
    return "author.draft_ready"
  return "author.draft"


def _approved_standing(facts: dict) -> str:
  if facts.get("checks") not in ("pass", "none") or facts.get("mergeable") != "yes":
    return "author.approved_checks"
  return "author.approved" if may_write(facts.get("permission")) else "author.approved_no_rights"


def _reviewer_standing(facts: dict, d: dict) -> str:
  """Your verdict on this version first, then a finished review of this version (a
  re-review that is done is no longer "new commits"), then an older review of yours."""
  if d["mine_at_head"] and d["mine"].get("state") == "CHANGES_REQUESTED":
    return "reviewer.waiting_author"
  if d["mine_at_head"]:
    return "reviewer.reviewed" if merge_blocker(facts, d) else "reviewer.mergeable"
  if d["result"] is not None:
    return "reviewer.kept_private" if d["result"].get("setAside") else "reviewer.result_ready"
  if d["mine"] is not None:
    return "reviewer.re_review"
  return "reviewer.draft" if facts.get("lifecycle") == "draft" else "reviewer.to_review"


CHECK_WORDS = {"pass": "", "none": "", "fail": "Checks are failing.", "pending": "Checks are still running.",
               "unknown": "Checks could not be read. Try again."}
CONFLICT_WORDS = {"yes": "", "conflict": "It has merge conflicts.",
                  "unknown": "GitHub hasn’t worked out conflicts yet. Try again."}
JOB_BUSY = "An agent is already working on this PR."


def _first_reason(*reasons: str) -> str:
  return next((reason for reason in reasons if reason), "")


def _open_threads(facts: dict) -> int:
  return facts.get("openThreads") or 0


def _job_idle(facts: dict) -> str:
  # A stalled job holds nothing: starting again (or another job) is the way out of it.
  job = _record(facts.get("job"))
  return JOB_BUSY if job is not None and job.get("state") != "stalled" else ""


def _nothing_in_flight(d: dict) -> str:
  # Something sent to GitHub but not yet confirmed: no new agent job may race it.
  outbox = d["outbox"]
  return "Something sent to GitHub is not confirmed yet." if outbox and outbox.get("state") != "drafted" else ""


def _still_open(facts: dict) -> str:
  return "" if facts.get("lifecycle") in ("open", "draft") else "This PR is no longer open."


def _readable(facts: dict) -> str:
  unread = facts.get("lifecycle") == "unknown" or facts.get("readComplete") is False
  return "This PR could not be read. Try again." if unread else ""


def _approval_reason(d: dict) -> str:
  if d["approved_at_head"]:
    return ""
  if d["approved_older"]:
    return "The approval is for an older version."
  return "Nobody but the author has approved it yet."


def merge_blocker(facts: dict, d: dict | None = None) -> str:
  """Why Merge cannot run, in order; '' when it may."""
  d = d if d is not None else derive(facts)
  standing = [*d["changes_at_head"], *d["changes_older"]]
  return _first_reason(
    _readable(facts),
    "Open it for review first." if facts.get("lifecycle") == "draft" else _still_open(facts),
    "" if may_write(facts.get("permission")) else "Needs merge rights.",
    _approval_reason(d),
    f"{', '.join(standing)} asked for changes and hasn’t approved since." if standing else "",
    CHECK_WORDS.get(facts.get("checks"), CHECK_WORDS["unknown"]),
    CONFLICT_WORDS.get(facts.get("mergeable"), CONFLICT_WORDS["unknown"]),
    _nothing_in_flight(d),
    _job_idle(facts),
  )


def _something_to_answer(facts: dict, d: dict) -> bool:
  return bool(d["open_findings"] or d["changes_at_head"] or _open_threads(facts) > 0
              or facts.get("checks") == "fail" or facts.get("mergeable") == "conflict" or d["bot_findings"])


def _outbox_is(d: dict, state: str) -> str:
  return "" if (d["outbox"] or {}).get("state") == state else "Nothing like that is waiting."


def _agent_job_guard(facts: dict, d: dict) -> str:
  return _first_reason(_readable(facts), _still_open(facts), _job_idle(facts), _nothing_in_flight(d))


def _respond_guard(facts: dict, d: dict) -> str:
  answer = "" if _something_to_answer(facts, d) else "Nothing to respond to."
  return _first_reason(_readable(facts), _still_open(facts), _job_idle(facts), _nothing_in_flight(d), answer)


def _finish_it_guard(facts: dict, d: dict) -> str:
  writer = "" if may_write(facts.get("permission")) else "Needs write access to this repository."
  return _first_reason(_readable(facts), _still_open(facts), writer, _job_idle(facts), _nothing_in_flight(d))


def _take_over_guard(facts: dict, d: dict) -> str:
  maintainer = "" if facts.get("permission") in MAINTAINER else "Only maintainers can take over someone else’s PR."
  return _first_reason(_readable(facts), _still_open(facts), maintainer, _job_idle(facts), _nothing_in_flight(d))


def _dismiss_guard(facts: dict, d: dict) -> str:
  # Only an unconfirmed verdict can be set aside here; other outcomes settle by checking GitHub.
  waiting = _outbox_is(d, "unconfirmed")
  if waiting:
    return waiting
  return "" if d["outbox"].get("kind") == "verdict" else "Only a verdict can be dismissed."


def _draft_verdict_guard(facts: dict, d: dict) -> str:
  reviewed_here = d["result"] is not None and not d["mine_at_head"] and d["outbox"] is None
  return _first_reason(_readable(facts), "" if reviewed_here else "Review this version first.")


def _set_aside_guard(facts: dict, d: dict) -> str:
  result = d["result"]
  open_result = result is not None and not result.get("setAside")
  allowed = open_result and (facts.get("role") == "reviewer" or d["open_findings"])
  return _first_reason(_readable(facts), "" if allowed else "Nothing to set aside.")


def _open_for_review_guard(facts: dict, d: dict) -> str:
  draft = _job_idle(facts) if facts.get("lifecycle") == "draft" else "It is already open for review."
  return _first_reason(_readable(facts), draft)


def _back_to_draft_guard(facts: dict, d: dict) -> str:
  is_open = _job_idle(facts) if facts.get("lifecycle") == "open" else "It is not open for review."
  return _first_reason(_readable(facts), is_open)


def _ask_rereview_guard(facts: dict, d: dict) -> str:
  if d["rereview"]:
    return _first_reason(_readable(facts), _still_open(facts))
  if d["looked_again"]:
    return "They already looked at this version. Ask them to approve."
  return "Nobody reviewed an older version."


def _ask_approval_guard(facts: dict, d: dict) -> str:
  if not d["looked_again"]:
    return "Nobody has looked again without approving."
  return _first_reason(_readable(facts), _still_open(facts))


def _ask_approval_title(d: dict) -> str:
  return f"Ask {', '.join(d['looked_again'])} to approve" if d["looked_again"] else "Ask for approval"


def _close_guard(facts: dict, d: dict) -> str:
  rights = "" if facts.get("role") == "author" or may_write(facts.get("permission")) else "Needs write access."
  return _first_reason(_readable(facts), _still_open(facts), rights, _job_idle(facts))


def _job_is(state: str, refusal: str) -> Callable[[dict, dict], str]:
  return lambda facts, d: "" if (_record(facts.get("job")) or {}).get("state") == state else refusal


@dataclass(frozen=True)
class Action:
  id: str
  by: str  # 'agent' (a job) or 'you' (the owner's own tap, behind a confirm sheet)
  roles: tuple[str, ...]
  title: str | Callable[[dict], str]
  when: Callable[[dict, dict], str]  # '' when allowed, otherwise why not
  detail: str = ""


BOTH = ("author", "reviewer")
ACTIONS = (
  Action("check_my_pr", "agent", ("author",), "Check my PR", _agent_job_guard, "Private. Nothing is posted."),
  Action("respond", "agent", ("author",), "Respond to feedback", _respond_guard,
         "Answers every point, then one approval sheet for the pushes and replies."),
  Action("finish_it", "agent", ("author",), "Finish it for me", _finish_it_guard,
         "Responds, checks and fixes. Stops for your approval before each push."),
  Action("review", "agent", ("reviewer",), lambda d: "Re-review" if d["mine"] is not None or d["result"] is not None else "Review",
         _agent_job_guard, "Private, drafted for you, or posted by the agent if you allow it."),
  Action("take_over", "agent", ("reviewer",), "Take over", _take_over_guard,
         "Maintainers only. Needs a reason, and says so on the PR. Each push needs your approval."),
  Action("send_draft", "you", BOTH, "Send…", lambda facts, d: _outbox_is(d, "drafted")),
  Action("check_github", "you", BOTH, "Check GitHub", lambda facts, d: _outbox_is(d, "unconfirmed")),
  Action("dismiss", "you", BOTH, "Dismiss", _dismiss_guard),
  Action("draft_verdict", "you", ("reviewer",), "Approve, request changes or comment…", _draft_verdict_guard),
  Action("set_aside", "you", BOTH, "Set aside…", _set_aside_guard),
  Action("open_for_review", "you", ("author",), "Open for review", _open_for_review_guard),
  Action("back_to_draft", "you", ("author",), "Back to draft", _back_to_draft_guard),
  Action("ask_rereview", "you", ("author",), "Ask for re-review", _ask_rereview_guard),
  Action("ask_approval", "you", ("author",), _ask_approval_title, _ask_approval_guard,
         "Posts one short note on the PR."),
  Action("merge", "you", BOTH, "Merge", merge_blocker),
  Action("close", "you", BOTH, "Close", _close_guard),
  Action("retry", "you", BOTH, "Try again", lambda facts, d: "" if _readable(facts) else "Already up to date."),
  Action("open_job", "you", BOTH, "Answer the agent", _job_is("needs_answer", "No question is waiting.")),
  Action("rerun_job", "you", BOTH, "Run it again", _job_is("stalled", "Nothing has stalled.")),
)


def _headline_who(key: str, d: dict) -> list[str]:
  """The people a headline names: who approved this version, or whose review is of an older one."""
  return {"author.waiting_rereview": d["changes_older"], "author.looked_again": d["looked_again"],
          "author.approval_stale": d["approved_older"], "author.approved": d["approved_at_head"]}.get(key, [])


def _headline(key: str, facts: dict, d: dict) -> str:
  if facts.get("mergeable") == "yes":
    checks = CHECK_WORDS.get(facts.get("checks")) or "Checks are still running."
  else:
    checks = CONFLICT_WORDS.get(facts.get("mergeable")) or CONFLICT_WORDS["unknown"]
  green = "no checks" if facts.get("checks") == "none" else "checks green"
  template = CARRIED_APPROVAL_HEADLINE if key == "author.approved" and d["carried_only"] else STATES[key]["headline"]
  return (template.replace("{who}", ", ".join(_headline_who(key, d)) or "reviewers")
          .replace("{author}", facts.get("author") or "the author").replace("{checks}", checks)
          .replace("{green}", green))


def _waiting_on(key: str, facts: dict, d: dict) -> str:
  if STATES[key]["turn"] != "other":
    return ""
  if facts.get("role") == "reviewer":
    return facts.get("author") or "the author"
  return ", ".join(_headline_who(key, d)) or "reviewers"


def _action_view(action: Action, facts: dict, d: dict, next_action: str) -> dict:
  reason = action.when(facts, d)
  title = action.title(d) if callable(action.title) else action.title
  return {"id": action.id, "by": action.by, "title": title, "detail": action.detail,
          "enabled": not reason, "reason": reason, "highlighted": action.id == next_action and not reason}


def decide(facts: dict) -> dict:
  """The one entry point: facts in, where the PR stands and every action for this role out.

  approved_by names who approved this version (carried_from: those of them whose
  approval is of an older commit with the same work, as {by, commit}); looked_again
  who looked at this version and only commented while their change request stands;
  rereview_from whose review is of an older version that they have not looked
  past (the people Ask for re-review may ask).
  """
  d = derive(facts)
  key = _state_of(facts, d)
  state = STATES[key]
  actions = [_action_view(action, facts, d, state["next"])
             for action in ACTIONS if facts.get("role") in action.roles]
  return {"role": facts.get("role"), "state": key, "turn": state["turn"],
          "label": state["label"].replace("{author}", facts.get("author") or "the author"),
          "headline": _headline(key, facts, d), "next": state["next"],
          "waiting_on": _waiting_on(key, facts, d), "actions": actions,
          "approved_by": d["approved_at_head"], "carried_from": d["carried"],
          "looked_again": d["looked_again"], "rereview_from": d["rereview"]}


def action_blocker(facts: dict, action_id: str) -> str:
  """What the platform asks before an effect: '' when allowed, otherwise the reason."""
  action = next((item for item in decide(facts)["actions"] if item["id"] == action_id), None)
  if action is None:
    role = "author" if facts.get("role") == "author" else "reviewer"
    return f"You can’t do that as the {role} of this PR."
  return action["reason"]
