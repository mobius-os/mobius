"""The owner's confirm sheet, server side: describe an action, then mint its grant.

The shell sends identifiers only (app, action, the route's path parameters and
its request body). The words the owner reads are built here from facts the
server holds (stored review rows, a live GitHub read, the chat's title) or
from a body that is itself the payload (a note, the people asked). Mint
rebuilds the same words and refuses, 409 owner_grant_changed, when they differ
from what the owner saw. Only the owner's own browser session can preview or
mint: an app, an agent or a shared-browser guest is refused.

The preview only reads the facts it shows. The route does its own checks and
may still refuse after the owner confirmed: the grant then expires unused, or
is burned when the route had already spent it, and the owner confirms again.

``GUARDED`` is the one list of actions a grant can cover. Each entry names the
route's own request model, so preview, mint and the route that spends the
grant (owner_grants.consume_owner_grant) parse one request the same way.
"""

import asyncio
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app import contribution_direct_merge as direct_merge
from app import contribution_pr_machine as machine
from app import contribution_pull_status as pull_status
from app import contribution_review_runs as reviews
from app import models, owner_grants
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, get_principal, reject_cross_site
from app.github_contributions import _validate_submit_app
from app.routes import contribution_reviews as routes
from app.routes import github as publication

router = APIRouter(prefix="/api/owner-grants", tags=["owner-grants"])

MERGE_VERBS = {"squash": "Squash and merge", "merge": "Merge", "rebase": "Rebase and merge",
               None: "Add to the merge queue"}
REVIEW_EVENTS = {"APPROVE": ("Approve", "an approval"), "REQUEST_CHANGES": ("Request changes", "a request for changes"),
                 "COMMENT": ("Post comment", "a comment")}
# The words for the verdict decisions that send nothing new to GitHub: headline, verb, tone, result.
QUIET_VERDICTS = {
  "discard": ("Discard the proposed verdict on {pr}", "Discard", "plain",
              "The proposed review is thrown away. Nothing is sent to GitHub."),
  "dismiss": ("Dismiss the unconfirmed verdict on {pr}", "Dismiss", "plain",
              "Stops waiting for GitHub to confirm it. Nothing is sent to GitHub."),
  "continue_merge": ("Continue the merge of {pr}", "Continue merge", "caution",
                     "The review agent continues the guarded merge of the approval GitHub already holds. "
                     "Merging still checks GitHub first."),
}


# What each push mode of a follow-up grant lets an agent do without asking.
FOLLOW_MODES = {
  "auto": "When reviews, failing checks or conflicts arrive, an agent pushes its fix to this branch without asking.",
  "ask": "An agent only prepares each fix. Nothing is pushed until you press Update PR.",
}


class RespondPostResult(BaseModel):
  """The prepared response a Post covers, as the Post helper names it; the chat is the caller's own."""
  model_config = ConfigDict(extra="forbid")
  repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256)
  number: int = Field(ge=1)
  result_id: str = Field(min_length=1, max_length=64)
  digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RespondPost(RespondPostResult):
  chat_id: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class Ask:
  """One request to describe: who asks, for which app, the route's path parameters and parsed body."""
  db: Session
  principal: Principal
  app_id: int
  scope: dict[str, str]
  body: Any


@dataclass(frozen=True)
class Guarded:
  model: type[BaseModel]  # the guarded route's own request model
  describe: Callable[[Ask], Awaitable[dict]]  # the sheet: headline, facts, verb, tone
  scope: tuple[str, ...] = ()  # the route's path parameters besides the app id


def _fact(label: str, value) -> dict:
  return {"label": label, "value": str(value)}


def _sheet(headline: str, facts: list[dict], verb: str, tone: Literal["plain", "caution"]) -> dict:
  return {"headline": headline, "facts": facts, "verb": verb, "tone": tone}


async def _read(fn, *args):
  """Run one of the effect modules' own GitHub reads, which refuse in the route's own words."""
  return await asyncio.to_thread(fn, routes._gh, Path(get_settings().data_dir) / "platform", *args)


def _pull_facts(body, pull: dict) -> list[dict]:
  return [
    _fact("Pull request", f"{body.repo}#{body.number}: {pull.get('title')}"),
    _fact("Author", (pull.get("user") or {}).get("login")),
    _fact("Merges into", (pull.get("base") or {}).get("ref")),
    _fact("Version", body.head_sha[:7]),
  ]


def _standing(facts: dict) -> dict:
  return _fact("Where it stands", machine.decide(facts)["headline"])


async def _describe_merge(ask: Ask) -> dict:
  body = ask.body
  _target, repo, pull, checks, facts = await _read(direct_merge._read_mergeable_pull, body)
  verb = MERGE_VERBS[direct_merge._method_to_send(body.method, repo, checks)]
  return _sheet(f"Merge {body.repo}#{body.number}", [
    *_pull_facts(body, pull), _fact("Method", verb),
    _fact("Result", "Merges on GitHub with your connected account. This cannot be undone."),
    _standing(facts),
  ], verb, "caution")


def _asked_of(body) -> list[dict]:
  """People and note of a status change whose body is itself the payload, shown exactly as sent."""
  if isinstance(body, routes.ReviewerRequest):
    return [_fact("Asking", ", ".join(body.reviewers))]
  if isinstance(body, routes.ApprovalRequest):
    return [_fact("Asking", ", ".join(body.people)), _fact("Note, exactly as posted", body.note)]
  return []


async def _describe_status(ask: Ask, name: str) -> dict:
  """A status change on a pull request, in the PR machine's own words for that action."""
  change, body = pull_status.CHANGES[name], ask.body
  _target, pull, facts = await _read(pull_status._read_pull_facts, body)
  action = next((a for a in machine.decide(facts)["actions"] if a["id"] == change.machine_action), None)
  if action is None:  # the machine has no words for an action this role does not have
    raise HTTPException(409, machine.action_blocker(facts, change.machine_action))
  detail = [_fact("What happens", action["detail"])] if action["detail"] else []
  return _sheet(f"{action['title']}: {body.repo}#{body.number}",
                [*_pull_facts(body, pull), *detail, *_asked_of(body), _standing(facts)], action["title"],
                "caution" if name in ("close", "ask-approval") else "plain")


def _stored_proposal(ask: Ask) -> dict:
  """The run's stored verdict proposal, only when it is the exact one the request names: the sheet quotes its text."""
  body = ask.body
  row = routes._row(ask.db, ask.app_id, ask.scope["run_id"], ask.principal)
  proposal = ((row.outcomes_json or {}).get(reviews.key(body.model_dump())) or {}).get("verdict_proposal") or {}
  if proposal.get("id") != body.proposal_id or proposal.get("head_sha") != body.head_sha:
    raise HTTPException(409, routes.VERDICT_CHANGED)
  return proposal


async def _describe_verdict(ask: Ask) -> dict:
  body = ask.body
  pr = f"{body.repo}#{body.number}"
  facts = [_fact("Pull request", pr), _fact("Version", body.head_sha[:7])]
  if body.decision != "submit":
    headline, verb, tone, result = QUIET_VERDICTS[body.decision]
    return _sheet(headline.format(pr=pr), [*facts, _fact("Result", result)], verb, tone)
  proposal = _stored_proposal(ask)
  event = body.event or proposal.get("event")
  text = proposal.get("comment_body" if event == "COMMENT" else "body")
  if event not in REVIEW_EVENTS or not text:
    raise HTTPException(409, routes.VERDICT_CHANGED)
  verb, words = REVIEW_EVENTS[event]
  return _sheet(f"Post {words} on {pr}", [*facts, _fact("Posted exactly as", text)], verb, "caution")


async def _describe_repair(ask: Ask) -> dict:
  body = ask.body
  pr = f"{body.repo}#{body.number}"
  if body.decision == "discard":
    return _sheet(f"Discard the proposed repair of {pr}", [
      _fact("Pull request", pr), _fact("Result", "The repair is thrown away. Nothing is pushed.")],
      "Discard", "plain")
  row = routes._row(ask.db, ask.app_id, ask.scope["run_id"], ask.principal)
  _original, _target, attempt = routes._proposed_repair(row, body)
  return _sheet(f"Push the proposed repair to {pr}", [
    _fact("Pull request", pr), _fact("Pushes to", f"{attempt.get('head_repo')}, branch {attempt.get('head_ref')}"),
    _fact("Version change", f"{attempt['from_sha'][:7]} to {attempt['head_sha'][:7]}"),
    _fact("Changes", attempt.get("diff_stat") or "not available"),
    _fact("Files", ", ".join(attempt.get("files") or []) or "none"),
    _fact("Result", "A new commit lands on the pull request's branch on GitHub."),
  ], "Push this fix", "caution")


async def _read_pull(repo: str, number: int) -> dict:
  try:
    return await _read(direct_merge._read_object, f"repos/{repo}/pulls/{number}")
  except direct_merge._READ_FAILURES as exc:
    raise HTTPException(404, direct_merge.UNREADABLE) from exc


async def _describe_respond_post(ask: Ask) -> dict:
  body = ask.body
  chat = ask.db.query(models.Chat).filter(
    models.Chat.id == body.chat_id, models.Chat.deleted_at.is_(None),
    models.Chat.created_by_app_id == ask.app_id).first()
  if chat is None:
    raise HTTPException(404, "That conversation was not found.")
  pull = await _read_pull(body.repo, body.number)
  return _sheet(f"Post your prepared response on {body.repo}#{body.number}", [
    _fact("Pull request", f"{body.repo}#{body.number}: {pull.get('title')}"),
    _fact("Author", (pull.get("user") or {}).get("login")),
    _fact("Conversation", chat.title),
    _fact("Prepared response", body.result_id),
    _fact("What is posted", "The replies and changes you read in Contribute. They are not shown here."),
  ], "Post to GitHub", "caution")


async def _describe_follow(ask: Ask) -> dict:
  """Following switched on for a sent pull request, or an existing grant's push mode changed."""
  change = await publication.autopilot_follow_change(ask.db, ask.app_id, ask.scope["record_id"], ask.body)
  pr = f"{change['repo']}#{change['number']}"
  pull = await _read_pull(change["repo"], change["number"])
  if pull.get("state") != "open":
    raise HTTPException(409, "This pull request is no longer open, so it cannot be followed.")
  starts = change["grant"] is not None
  return _sheet(f"Follow {pr}" if starts else f"Change how {pr} is followed", [
    _fact("Pull request", f"{pr}: {pull.get('title')}"),
    _fact("Author", (pull.get("user") or {}).get("login")),
    _fact("Branch", change["branch"]),
    _fact("Pushes", FOLLOW_MODES[change["push_mode"]]),
    _fact("Replies", "An agent may answer review comments on GitHub with your connected account."),
    _fact("Result", "Stays on until you pause it or the pull request is merged or closed."),
  ], "Follow" if starts else "Change", "caution")


# The one list of actions a grant can cover: the owner-tap routes of
# contribution_reviews.py (Merge, the six status changes, a verdict decision
# and a repair decision), Post, and Follow (a first follow-up grant for a sent
# pull request, or another push mode for an existing one). Review start is not here: who may start a
# publishing review is for the platform design to settle, because a start
# approved in chat conflicts with a grant rule.
#
# Which PUBLIC writes need the owner's tap is not one rule today, and this list
# is not it. The publication routes in routes/github.py that write to GitHub
# under the owner's name (submit, ready, update-existing, submit-stack,
# update-stack, land-stack) use require_account_owner_control and
# _validate_submit_app, so they accept github_access app tokens and top-level
# agents, and land-stack fast-forwards an upstream branch. They are NOT covered
# here. Whether they need the owner's tap is an open policy decision for the
# platform; no grant check may claim that gap closed while they stand.
GUARDED = {
  "merge": Guarded(routes.DirectMerge, _describe_merge),
  "open_for_review": Guarded(routes.PullStatusChange, partial(_describe_status, name="ready")),
  "back_to_draft": Guarded(routes.PullStatusChange, partial(_describe_status, name="draft")),
  "close": Guarded(routes.PullStatusChange, partial(_describe_status, name="close")),
  "request_review": Guarded(routes.ReviewerRequest, partial(_describe_status, name="request-review")),
  "ask_rereview": Guarded(routes.ReviewerRequest, partial(_describe_status, name="request-reviewers")),
  "ask_approval": Guarded(routes.ApprovalRequest, partial(_describe_status, name="ask-approval")),
  "verdict": Guarded(routes.VerdictDecision, _describe_verdict, ("run_id",)),
  "repair": Guarded(routes.RepairDecision, _describe_repair, ("run_id",)),
  "respond_post": Guarded(RespondPost, _describe_respond_post),
  "follow": Guarded(publication.AutopilotToggleBody, _describe_follow, ("record_id",)),
}


class GrantAsk(BaseModel):
  model_config = ConfigDict(extra="forbid")
  app_id: int
  action: str = Field(max_length=32)
  scope: dict[str, str] = Field(default_factory=dict)
  body: dict[str, Any]


class GrantMint(GrantAsk):
  digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConsumePost(BaseModel):
  model_config = ConfigDict(extra="forbid")
  app_id: int
  action: Literal["respond_post"]
  body: RespondPostResult


def owner_hands(principal: Principal = Depends(get_principal)) -> Principal:
  """The installation owner's own browser session: never an app, an agent, a helper or a guest."""
  if not owner_grants.is_owner_hands(principal):
    raise HTTPException(403, "Only you, in your own Möbius session, can confirm this.")
  return principal


async def _described(ask: GrantAsk, principal: Principal, db: Session) -> tuple[str, str, dict, str]:
  """(app nonce, binding, sheet, digest) for one request, built the same way for preview and mint."""
  guarded = GUARDED.get(ask.action)
  if guarded is None or set(ask.scope) != set(guarded.scope):
    raise HTTPException(422, "This action cannot be confirmed here.")
  try:
    body = guarded.model.model_validate(ask.body)
  except ValidationError as exc:
    raise HTTPException(422, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
  # Checked as the asking app's own token is at the route, which needs github_access.
  nonce = _validate_submit_app(ask.app_id, Principal(owner=principal.owner, app_id=ask.app_id, scope="app"), db)
  sheet = await guarded.describe(Ask(db, principal, ask.app_id, ask.scope, body))
  binding = owner_grants.binding_sha256(ask.action, ask.app_id, ask.scope, body)
  return nonce, binding, sheet, owner_grants.sha256_json({"binding": binding, "sheet": sheet})


@router.post("/preview", dependencies=[Depends(reject_cross_site)])
async def preview_grant(ask: GrantAsk, principal: Principal = Depends(owner_hands), db: Session = Depends(get_db)):
  """What the owner is about to confirm, in plain words, and the digest that pins those words."""
  _nonce, _binding, sheet, digest = await _described(ask, principal, db)
  return {**sheet, "digest": digest}


@router.post("", dependencies=[Depends(reject_cross_site)])
async def mint_grant(ask: GrantMint, principal: Principal = Depends(owner_hands), db: Session = Depends(get_db)):
  """Mint the one grant for what the owner just confirmed. No grant id is returned: nothing to pass around."""
  nonce, binding, _sheet_words, digest = await _described(ask, principal, db)
  if digest != ask.digest:
    raise HTTPException(409, {"code": "owner_grant_changed",
                              "message": "This changed after you looked. Review it again."})
  grant = owner_grants.mint(db, principal.owner, ask.app_id, nonce, ask.action, binding, digest)
  return {"granted": True, "expires_at": grant.expires_at.isoformat() + "Z"}


@router.post("/consume", dependencies=[Depends(reject_cross_site)])
def consume_post_grant(ask: ConsumePost, principal: Principal = Depends(get_agent_run_principal),
                       db: Session = Depends(get_db)):
  """The Post helper spends the owner's grant before it reads or posts anything. 403 without one.

  The chat is the caller's own, so a grant minted for another chat's agent never matches.
  """
  body = RespondPost(chat_id=principal.chat_id, **ask.body.model_dump())
  owner_grants.consume_owner_grant(db, principal, ask.app_id, ask.action, body)
  return {"consumed": True}
