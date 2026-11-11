"""The PR machine: where a pull request stands for the owner, and what may happen next.

Contribute renders the same table (its pr-machine.js) on every screen; the
platform asks ``action_blocker`` before every effect it performs on a PR. Both
are held to one shared case corpus
(tests/fixtures/contribution_pr_machine_cases.json), kept byte-identical with
the app's copy, so the two evaluators cannot drift. Change a rule in both
places and in that corpus together.

Facts use the app's keys (role, author, head, lifecycle, checks, mergeable,
permission, reviews[{by, state, commit, bot, sameWork}], reviewers[{by, state, commit, bot}], myReview,
openThreads, job, result, outbox, readComplete). A fact that could not be read is 'unknown'
(or readComplete is False) and never enables anything that needs it. `reviewers` is the platform's
own list of everyone's newest review, writers or not: it only widens who the Ask actions may name, and is
absent in the app's facts and in the shared corpus (then the askable pool is the people in `reviews`).
A review's state is APPROVED or CHANGES_REQUESTED (a person's standing opinion)
or COMMENTED (their newest review, only when it is a plain comment: evidence
that they looked, never an opinion). `sameWork` is the fact reader's proof, on
an approval of an older commit, that the PR's own change is the same at that
commit and at the head (contribution_same_work); absent or None is "unknown".

The rules, in plain words:
  - Your role decides everything: your PR (author) or theirs (reviewer). An
    author never gets a public review action; an agent's check of your PR is private.
  - Ready to merge is an approval of this exact version by someone other than the
    author (bots never count), no change request still standing, checks that
    pass (or none exist), no conflicts, and nothing unconfirmed on its way out.
    An approval of an older commit still counts when the fact reader proved the
    PR's own change the same: a merge of the base is a new commit, not new work.
    That words the PR's standing and its highlighted next step (not_ready_to_merge);
    it does not switch your own Merge off: GitHub's merge rules refuse what cannot
    land. The agent's own unattended merge has no tap behind it, so it still asks it.
  - Only four things stop an action of yours, each where it matters: the PR cannot be
    read and it is closed or merged (every action); an agent is already working on it
    (an agent job, Take over, Merge, Open for review, Back to draft, Close); something
    sent to GitHub is not confirmed (an agent job, Take over, Merge). Ask for re-review,
    Ask for approval and Request review check only the first, since a note or a review
    request races nothing. An action tied to a thing waits for that thing (a draft to
    send, a person to ask, a result to set aside). Asking works for anyone who reviewed or commented
    and has not approved this version; the sheet ticks the people each ask is meant for.
    Request review is the one ask that waits for nobody: it names anyone, reviewed or not.
    Taking over someone else's PR stays for maintainers: it is Contribute's own rule,
    GitHub would let any writer push.
  - Only new commits bring their PR back for re-review; replies never do. A
    reviewer who only commented has not approved, however long they looked: a
    plain comment never lifts a change request on GitHub, and a PR that needs an
    approval does not get one from it. Anyone who commented on this version can
    be asked to approve.
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
  changes_at_head = _names(people, lambda r: r.get("state") == "CHANGES_REQUESTED" and at_head(r))
  # Whose change request is of an older version, but who has looked at this one since and only commented.
  looked_again = [name for name in changes_older if any(_same(other, name) for other in commented_at_head)]
  # Who commented on this version without approving it, and holds no change request: they looked and gave no verdict.
  holding_or_approved = [*approved_at_head, *changes_at_head, *changes_older]
  commented_only = [name for name in commented_at_head if not any(_same(other, name) for other in holding_or_approved)]
  return {
    "approved_at_head": approved_at_head,
    "approved_older": approved_older,
    "carried": carried,
    "carried_only": bool(approved_at_head) and all(
      any(_same(item["by"], name) for item in carried) for name in approved_at_head),
    "changes_at_head": changes_at_head,
    "changes_older": changes_older,
    "looked_again": looked_again,
    "commented_only": commented_only,
    # Who Ask for approval can ask: everyone who commented on this version and has not approved it.
    "approval_from": [*looked_again, *commented_only],
    # Who a re-review request can still ask: an older review of theirs that they have not looked past.
    "rereview": [name for name in dict.fromkeys([*changes_older, *approved_older])
                 if name not in looked_again and name not in commented_only],
  }


def _others_who_reviewed(facts: dict, people: list[dict]) -> list[dict]:
  """Reviewers the standing opinions leave out (no write access): everyone's newest review, minus bots, the author
  and anyone already among ``people``. They can be asked; what they said never counts toward merging."""
  known = _names(people, lambda review: True)
  return [r for r in facts.get("reviewers") or []
          if not r.get("bot") and not _same(r.get("by"), facts.get("author"))
          and not any(_same(other, r.get("by")) for other in known)]


def _other_reason(review: dict, head) -> str:
  """What is true of a reviewer who holds no standing opinion here, in the words of the standing ones."""
  here = review.get("commit") == head
  if review.get("state") == "CHANGES_REQUESTED":
    return "changes_here" if here else "changes_older"
  if review.get("state") == "APPROVED":
    return "approved_older"
  return "commented_here" if here else "commented_older"


def _askable(people: list[dict], standing: dict, others: list[dict], head) -> list[dict]:
  """Who could be asked to look at this version: everyone who reviewed or commented, except those whose approval
  already counts for it. ``reason`` is what is true of them (it words the public note), ``commit`` the version
  their review was of."""
  def has(names: list[str], name: str) -> bool:
    return any(_same(other, name) for other in names)

  reasons = (("changes_here", "CHANGES_REQUESTED", standing["changes_at_head"]),
             ("looked_again", "CHANGES_REQUESTED", standing["looked_again"]),
             ("commented_here", "COMMENTED", standing["commented_only"]),
             ("changes_older", "CHANGES_REQUESTED", standing["changes_older"]),
             ("approved_older", "APPROVED", standing["approved_older"]))
  askable = []
  for name in _names(people, lambda review: True):
    if has(standing["approved_at_head"], name):
      continue
    reason, state = next(((why, kind) for why, kind, names in reasons if has(names, name)), ("commented_older", "COMMENTED"))
    theirs = [r for r in people if _same(r.get("by"), name)]
    review = next((r for r in theirs if r.get("state") == state), theirs[0] if theirs else None)  # a state beyond the three (a dismissed one) still names its commit
    askable.append({"by": name, "reason": reason, "commit": (review or {}).get("commit") or ""})
  for review in others:
    if review.get("state") == "APPROVED" and review.get("commit") == head:
      continue  # approved this very version: nothing left to ask, though it never counted toward merging
    askable.append({"by": review.get("by"), "reason": _other_reason(review, head), "commit": review.get("commit") or ""})
  return askable


def derive(facts: dict) -> dict:
  """The few values every rule below is written in."""
  head = facts.get("head")
  reviews = facts.get("reviews") or []
  people = [r for r in reviews if not r.get("bot") and not _same(r.get("by"), facts.get("author"))]
  mine = _record(facts.get("myReview"))
  result = _for_this_head(facts.get("result"), head)
  standing = _review_standing(people, head)
  others = _others_who_reviewed(facts, people)
  return {
    **standing,
    "askable": _askable(people, standing, others, head),
    "approved_by_others": any(r.get("state") == "APPROVED" and r.get("commit") == head for r in others),
    "mine": mine,
    "mine_at_head": mine is not None and mine.get("commit") == head,
    "result": result,
    "open_findings": result is not None and result.get("outcome") == "findings" and not result.get("setAside"),
    "outbox": _for_this_head(facts.get("outbox"), head),
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
  "author.commented": _state(
    "you", "Commented · not approved",
    "Your PR · {who} left a comment but hasn’t approved. Ask for their approval", "ask_approval"),
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
  if d["commented_only"]:
    return "author.commented"
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
    return "reviewer.reviewed" if not_ready_to_merge(facts, d) else "reviewer.mergeable"
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


def not_ready_to_merge(facts: dict, d: dict | None = None) -> str:
  """Why this PR is not ready to merge, in order; '' when it is.

  It words the PR's standing (Ready to merge, the highlighted next step). Merge itself is
  offered whenever the stops allow, whatever this says; the agent's own merge still asks it.
  """
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


def _outbox_is(d: dict, state: str) -> str:
  return "" if (d["outbox"] or {}).get("state") == state else "Nothing like that is waiting."


def _agent_stops(facts: dict, d: dict) -> str:
  """What stops an agent job whatever the PR's review standing: unreadable, closed, one already working, a send unconfirmed."""
  return _first_reason(_readable(facts), _still_open(facts), _job_idle(facts), _nothing_in_flight(d))


def _take_over_stops(facts: dict, d: dict) -> str:
  maintainer = "" if facts.get("permission") in MAINTAINER else "Only maintainers can take over someone else’s PR."
  return _first_reason(_readable(facts), _still_open(facts), maintainer, _job_idle(facts), _nothing_in_flight(d))


def _merge_stops(facts: dict, d: dict) -> str:
  return _first_reason(_readable(facts), _still_open(facts), _nothing_in_flight(d), _job_idle(facts))


def _status_stops(facts: dict, d: dict) -> str:
  return _first_reason(_readable(facts), _still_open(facts), _job_idle(facts))


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


def _ask_stops(facts: dict, d: dict) -> str:
  """The two Ask actions need someone to name: anyone who reviewed or commented, except who already approved this version."""
  nobody = "Everyone who reviewed it has already approved this version." if d["approved_at_head"] or d["approved_by_others"] else "Nobody has reviewed this PR yet."
  return _first_reason(_readable(facts), _still_open(facts), "" if d["askable"] else nobody)


def _request_stops(facts: dict, d: dict) -> str:
  """Request review asks anyone to review, so unlike the Ask actions it never waits for someone to have reviewed first."""
  return _first_reason(_readable(facts), _still_open(facts))


def _ask_approval_title(d: dict) -> str:
  return f"Ask {', '.join(d['approval_from'])} to approve" if d["approval_from"] else "Ask for approval"


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
  Action("check_my_pr", "agent", ("author",), "Check my PR", _agent_stops, "Private. Nothing is posted."),
  Action("respond", "agent", ("author",), "Respond to feedback", _agent_stops,
         "Answers every point, then one approval sheet for the pushes and replies."),
  Action("finish_it", "agent", ("author",), "Finish it for me", _agent_stops,
         "Responds, checks and fixes. Stops for your approval before each push."),
  Action("review", "agent", ("reviewer",), lambda d: "Re-review" if d["mine"] is not None or d["result"] is not None else "Review",
         _agent_stops, "Private, drafted for you, or posted by the agent if you allow it."),
  Action("take_over", "agent", ("reviewer",), "Take over", _take_over_stops,
         "Maintainers only. Needs a reason, and says so on the PR. Each push needs your approval."),
  Action("send_draft", "you", BOTH, "Send…", lambda facts, d: _outbox_is(d, "drafted")),
  Action("check_github", "you", BOTH, "Check GitHub", lambda facts, d: _outbox_is(d, "unconfirmed")),
  Action("dismiss", "you", BOTH, "Dismiss", _dismiss_guard),
  Action("draft_verdict", "you", ("reviewer",), "Approve, request changes or comment…", _draft_verdict_guard),
  Action("set_aside", "you", BOTH, "Set aside…", _set_aside_guard),
  Action("open_for_review", "you", ("author",), "Open for review", _status_stops),
  Action("back_to_draft", "you", ("author",), "Back to draft", _status_stops),
  Action("request_review", "you", ("author",), "Request review", _request_stops,
         "Asks the people you pick to review it on GitHub."),
  Action("ask_rereview", "you", ("author",), "Ask for re-review", _ask_stops),
  Action("ask_approval", "you", ("author",), _ask_approval_title, _ask_stops,
         "Posts one short note on the PR."),
  Action("merge", "you", BOTH, "Merge", _merge_stops),
  Action("close", "you", BOTH, "Close", _status_stops),
  Action("retry", "you", BOTH, "Try again", lambda facts, d: "" if _readable(facts) else "Already up to date."),
  Action("open_job", "you", BOTH, "Answer the agent", _job_is("needs_answer", "No question is waiting.")),
  Action("rerun_job", "you", BOTH, "Run it again", _job_is("stalled", "Nothing has stalled.")),
)


def _headline_who(key: str, d: dict) -> list[str]:
  """The people a headline names: who approved this version, or whose review is of an older one."""
  return {"author.waiting_rereview": d["changes_older"], "author.looked_again": d["looked_again"],
          "author.commented": d["commented_only"], "author.approval_stale": d["approved_older"], "author.approved": d["approved_at_head"]}.get(key, [])


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
  changes_from who holds a change request, of this version or an older one;
  approval_from everyone who only commented on it and has not approved (looked_again included);
  rereview_from whose review is of an older version that they have not looked
  past (the people Ask for re-review may ask); askable everyone the two Ask actions may name, as
  {by, reason, commit} (approval_from and rereview_from are the ones ticked at first).
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
          "changes_from": [*d["changes_at_head"], *d["changes_older"]],
          "looked_again": d["looked_again"], "approval_from": d["approval_from"],
          "rereview_from": d["rereview"], "askable": d["askable"]}


def action_blocker(facts: dict, action_id: str) -> str:
  """What the platform asks before an effect: '' when allowed, otherwise the reason."""
  action = next((item for item in decide(facts)["actions"] if item["id"] == action_id), None)
  if action is None:
    role = "author" if facts.get("role") == "author" else "reviewer"
    return f"You can’t do that as the {role} of this PR."
  return action["reason"]
