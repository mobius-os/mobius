"""Recovery resumes an existing grant, never grants or publishes anything."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import contribution_autopilot as autopilot
from app import contribution_autopilot_recovery as recovery
from app import models
from app.config import get_settings
from app.contribution_records import read_record, record_paths, write_record
from app.database import SessionLocal
from app.timeutil import now_naive_utc


HEAD = "a" * 40
BASE = "b" * 40
REPO = "mobius-os/app-demo"
HEAD_REPO = "octocat/app-demo"
BRANCH = "fix/recovery"
PR_NUMBER = 7
PRIOR_EVENT = "2026-07-01T00:00:00.000000Z"
BLOCKED_EVENT = "2026-07-02T00:00:00.000000Z"
NEXT_EVENT = "2026-07-03T00:00:00.000000Z"


@pytest.fixture(autouse=True)
def _contribute_owned_recovery_policy(monkeypatch):
  """Core tests verify the CAS adapter; Contribute tests own record policy."""
  async def policy(_app, _owner, path, body):
    assert path == "autopilot/reviewed-resolution"
    row = body["grant"]
    record = body["record"]
    plan = record.get("plan")
    review = record.get("quality_review")
    exact_target = isinstance(plan, dict) and (
      plan.get("repo") or record.get("repo"), record.get("number"),
      record.get("head_repository") or plan.get("head_repository"),
      plan.get("branch") or record.get("branch"), plan.get("repo_path"),
    ) == (
      row.get("target_repo"), row.get("target_pr_number"),
      row.get("target_head_repository"), row.get("target_branch"),
      row.get("target_repo_path"),
    )
    try:
      reviewed_at = datetime.fromisoformat(
        str(review.get("reviewed_at") or "").replace("Z", "+00:00"),
      )
      blocked_at = datetime.fromisoformat(row["blocked_at"])
      now = datetime.fromisoformat(body["now"])
    except (AttributeError, KeyError, TypeError, ValueError):
      reviewed_at = blocked_at = now = None
    eligible = bool(
      record.get("id") == row.get("record_id")
      and record.get("repo") == row.get("target_repo")
      and record.get("type") == "pr"
      and record.get("status") in {"open", "draft"}
      and not record.get("needs_attention")
      and not record.get("attention")
      and exact_target
      and isinstance(review, dict)
      and review.get("state") == "all_clear"
      and review.get("reviewed_head_sha") in {
        row.get("granted_head_sha"),
        plan.get("attribution_normalized_from") if isinstance(plan, dict) else None,
      }
      and plan.get("head_sha") == row.get("granted_head_sha")
      and reviewed_at is not None and blocked_at <= reviewed_at <= now + timedelta(minutes=5)
    )
    return {"eligible": eligible}

  monkeypatch.setattr(recovery.app_services, "invoke_policy", policy)


def _iso(value):
  return value.isoformat() + "Z"


@pytest.fixture
def blocked(db, auth, monkeypatch):
  app = models.App(
    name="Recovery test", slug="recovery-test", source_dir="recovery-test",
    github_access=True,
  )
  chat = models.Chat(
    id="autopilot-recovery-chat", title="Autopilot recovery",
    agent_settings_json={"drawer_hidden": True},
  )
  db.add_all([app, chat])
  db.commit()
  record_id = "recoverable-pr"
  repo_path = (
    Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  )
  (repo_path / ".git").mkdir(parents=True, exist_ok=True)
  row = autopilot.stamp_grant(
    db, app.id, record_id, head_sha=HEAD,
    target_repo=REPO, target_pr_number=PR_NUMBER,
    target_head_repository=HEAD_REPO, target_branch=BRANCH,
    target_repo_path=str(repo_path.resolve()),
  )
  row.followup_chat_id = chat.id
  row.rounds_used = 4
  row.last_handled_attention_key = "prior-review"
  row.last_handled_event_at = PRIOR_EVENT
  row.rounds_json = [{"outcome": "handled", "summary": "Earlier review settled."}]
  db.commit()
  claimed = autopilot.claim_for_round(
    db, app.id, record_id,
    attention_key="review-needing-help", event_at=BLOCKED_EVENT,
  )
  assert claimed["status"] == "granted"
  blocked_at = now_naive_utc() - timedelta(minutes=1)
  with monkeypatch.context() as clock:
    clock.setattr(autopilot, "now_naive_utc", lambda: blocked_at)
    assert autopilot.escalate(db, app.id, record_id)
  db.expire_all()
  row = autopilot.get_row(db, app.id, record_id)
  assert row.enabled is True and row.state == "blocked"
  assert row.blocked_at == blocked_at
  record = {
    "id": record_id, "type": "pr", "status": "open", "repo": REPO,
    "number": PR_NUMBER, "head_repository": HEAD_REPO,
    "url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
    "needs_attention": False, "attention": None,
    "plan": {
      "action": "pr", "repo": REPO, "branch": BRANCH,
      "repo_path": str(repo_path.resolve()), "base_sha": BASE,
      "head_sha": HEAD, "diff_sha256": "d" * 64,
    },
    "quality_review": {
      "state": "all_clear", "reviewed_head_sha": HEAD,
      "reviewed_at": _iso(blocked_at + timedelta(seconds=1)),
    },
  }
  record_path, _ = record_paths(app.id, record_id)
  write_record(record_path, record)
  return SimpleNamespace(
    db=db, app_id=app.id, record_id=record_id, chat_id=chat.id,
    record_path=record_path, record=record, blocked_at=blocked_at,
    run_id=claimed["run_id"],
  )


def _recover():
  return asyncio.run(recovery.recover_resolved_blocks())


def _row(case):
  case.db.expire_all()
  return autopilot.get_row(case.db, case.app_id, case.record_id)


def test_repaired_warning_resumes_once_with_fresh_claim_and_durable_state(blocked):
  history = list(_row(blocked).rounds_json)
  assert _recover() == 1
  assert _recover() == 0

  # Read through a separate session: supervisor recovery owns its commit.
  with SessionLocal() as reader:
    row = autopilot.get_row(reader, blocked.app_id, blocked.record_id)
    assert row.enabled is True and row.state == "idle"
    assert row.blocked_at is None
    assert row.run_id is None
    assert row.rounds_used == 0 and row.consecutive_failures == 0
    assert row.rounds_json == history
    assert row.granted_head_sha == HEAD
    assert not autopilot.verify_claim(row, blocked.run_id)
    chat = reader.get(models.Chat, blocked.chat_id)
    assert chat.agent_settings_json["drawer_hidden"] is True

  mirrored = read_record(blocked.record_path)["autopilot"]
  assert mirrored["enabled"] is True and mirrored["state"] == "idle"
  assert autopilot.claim_for_round(
    blocked.db, blocked.app_id, blocked.record_id,
    attention_key="review-needing-help", event_at=BLOCKED_EVENT,
  )["status"] == "duplicate"
  assert _row(blocked).last_handled_event_at == BLOCKED_EVENT
  verdict = autopilot.claim_for_round(
    blocked.db, blocked.app_id, blocked.record_id,
    attention_key="new-review", event_at=NEXT_EVENT,
  )
  assert verdict["status"] == "granted"
  assert verdict["run_id"] != blocked.run_id
  assert autopilot.verify_claim(_row(blocked), verdict["run_id"])
  assert not autopilot.verify_claim(_row(blocked), blocked.run_id)
  assert autopilot.complete_round(
    blocked.db, blocked.app_id, blocked.record_id,
    run_id=verdict["run_id"], outcome="handled", summary="New review settled.",
  )["productive"] is True
  assert _row(blocked).last_handled_event_at == NEXT_EVENT


@pytest.mark.parametrize("status", ["prepared", "submitting", "merged", "closed"])
def test_nonpublic_or_terminal_record_does_not_resume(blocked, status):
  blocked.record["status"] = status
  write_record(blocked.record_path, blocked.record)
  assert _recover() == 0
  assert _row(blocked).state == "blocked"


@pytest.mark.parametrize("change", [
  "missing_review", "stale_review", "review_not_clear", "review_head",
  "plan_head", "needs_attention", "attention", "malformed_reviewed_at",
])
def test_cleared_warning_alone_is_not_a_fresh_exact_review(blocked, change):
  record = blocked.record
  if change == "missing_review":
    record.pop("quality_review")
  elif change == "stale_review":
    record["quality_review"]["reviewed_at"] = _iso(
      blocked.blocked_at - timedelta(seconds=1),
    )
  elif change == "review_not_clear":
    record["quality_review"]["state"] = "changes_needed"
  elif change == "review_head":
    record["quality_review"]["reviewed_head_sha"] = "e" * 40
  elif change == "plan_head":
    # A reviewed PRIVATE fix is not yet the trusted, approved public head.
    record["plan"]["head_sha"] = "e" * 40
    record["quality_review"]["reviewed_head_sha"] = "e" * 40
  elif change == "needs_attention":
    record["needs_attention"] = True
  elif change == "attention":
    record["attention"] = {"type": "human_required", "message": "Still blocked."}
  elif change == "malformed_reviewed_at":
    record["quality_review"]["reviewed_at"] = "not-a-time"
  write_record(blocked.record_path, record)
  assert _recover() == 0
  row = _row(blocked)
  assert row.enabled is True and row.state == "blocked"
  assert not autopilot.verify_claim(row, blocked.run_id)


@pytest.mark.parametrize("field", [
  "repo", "plan_repo", "number", "head_repository", "branch", "repo_path",
])
def test_recovery_cannot_retarget_existing_grant(blocked, field):
  record = blocked.record
  if field == "plan_repo":
    record["repo"] = record["plan"]["repo"] = "another/repository"
  elif field == "repo":
    record["repo"] = "another/repository"
  elif field == "number":
    record["number"] = PR_NUMBER + 1
  elif field == "head_repository":
    record["head_repository"] = "another/app-demo"
  elif field == "branch":
    record["plan"]["branch"] = "another-branch"
  else:
    record["plan"]["repo_path"] = str(Path(record["plan"]["repo_path"]).parent)
  write_record(blocked.record_path, record)
  assert _recover() == 0
  assert _row(blocked).state == "blocked"


@pytest.mark.parametrize("pause", ["manual", "legacy", "terminal"])
def test_recovery_never_undoes_disabled_owner_or_legacy_grant(blocked, pause):
  if pause == "manual":
    autopilot.set_enabled(blocked.db, blocked.app_id, blocked.record_id, False)
  elif pause == "terminal":
    autopilot.close_out(blocked.db, blocked.app_id, blocked.record_id)
  else:
    row = _row(blocked)
    row.enabled = False
    row.state = "idle"
    row.blocked_at = None
    blocked.db.commit()
  # Display values are not authority, even with otherwise valid repair data.
  blocked.record["autopilot"] = {"enabled": True, "state": "blocked"}
  write_record(blocked.record_path, blocked.record)
  assert _recover() == 0
  assert _row(blocked).enabled is False


def test_forged_ledger_without_db_grant_cannot_resume(blocked):
  blocked.db.delete(_row(blocked))
  blocked.db.commit()
  blocked.record["autopilot"] = {"enabled": True, "state": "blocked"}
  write_record(blocked.record_path, blocked.record)
  assert _recover() == 0
  assert _row(blocked) is None


def test_unanswered_owner_question_stays_visible_and_blocks_recovery(blocked):
  chat = blocked.db.get(models.Chat, blocked.chat_id)
  chat.pending_question_id = "choose-resolution"
  blocked.db.commit()
  assert _recover() == 0
  assert _row(blocked).state == "blocked"
  blocked.db.expire_all()
  chat = blocked.db.get(models.Chat, blocked.chat_id)
  assert chat.pending_question_id == "choose-resolution"
  assert chat.agent_settings_json["drawer_hidden"] is False


def test_approved_head_refresh_does_not_move_escalation_freshness_barrier(blocked):
  reviewed_at = blocked.record["quality_review"]["reviewed_at"]
  blocked.record["plan"]["head_sha"] = "f" * 40
  blocked.record["quality_review"]["reviewed_head_sha"] = "f" * 40
  write_record(blocked.record_path, blocked.record)
  assert autopilot.refresh_granted_head(
    blocked.db, blocked.app_id, blocked.record_id, head_sha="f" * 40,
  )
  row = _row(blocked)
  assert row.blocked_at == blocked.blocked_at
  assert _iso(row.updated_at) > reviewed_at
  assert _recover() == 1
  assert _row(blocked).granted_head_sha == "f" * 40


@pytest.mark.parametrize("published", [True, False])
def test_attribution_only_normalization_requires_confirmed_public_head(blocked, published):
  blocked.record["plan"]["head_sha"] = "f" * 40
  blocked.record["plan"]["attribution_normalized_from"] = HEAD
  write_record(blocked.record_path, blocked.record)
  if published:
    autopilot.refresh_granted_head(
      blocked.db, blocked.app_id, blocked.record_id, head_sha="f" * 40,
    )
  assert _recover() == int(published)
  assert _row(blocked).state == ("idle" if published else "blocked")


@pytest.mark.parametrize("interruption", ["manual_pause", "terminal_close", "owner_question"])
def test_owner_interruption_wins_between_recovery_read_and_compare_swap(
  blocked, monkeypatch, interruption,
):
  original_update = recovery.update
  raced = False

  def update_after_owner_interruption(model):
    nonlocal raced
    # Recovery has already read the grant, review and question state. Commit
    # the owner's newer action immediately before its conditional UPDATE.
    if model is models.ContributionAutopilot and not raced:
      raced = True
      with SessionLocal() as owner:
        if interruption == "manual_pause":
          autopilot.set_enabled(owner, blocked.app_id, blocked.record_id, False)
        elif interruption == "terminal_close":
          autopilot.close_out(owner, blocked.app_id, blocked.record_id)
        else:
          chat = owner.get(models.Chat, blocked.chat_id)
          chat.pending_question_id = "new-owner-question"
          owner.commit()
    return original_update(model)

  monkeypatch.setattr(recovery, "update", update_after_owner_interruption)
  assert _recover() == 0
  assert raced
  row = _row(blocked)
  assert not autopilot.verify_claim(row, blocked.run_id)
  if interruption == "owner_question":
    assert row.enabled is True and row.state == "blocked"
    chat = blocked.db.get(models.Chat, blocked.chat_id)
    assert chat.pending_question_id == "new-owner-question"
    assert chat.agent_settings_json["drawer_hidden"] is False
  else:
    assert row.enabled is False
    assert row.state == "idle"


def test_retargeting_the_grant_while_app_policy_runs_blocks_recovery(
  blocked, monkeypatch,
):
  async def retarget_then_approve(*_args, **_kwargs):
    with SessionLocal() as owner:
      row = autopilot.get_row(owner, blocked.app_id, blocked.record_id)
      row.target_branch = "fix/new-target"
      owner.commit()
    return {"eligible": True}

  monkeypatch.setattr(recovery.app_services, "invoke_policy", retarget_then_approve)
  assert _recover() == 0
  assert _row(blocked).state == "blocked"
  assert _row(blocked).target_branch == "fix/new-target"


def test_failed_drawer_commit_rolls_back_recovery_and_can_retry(blocked, monkeypatch):
  original_hide = autopilot.stage_followup_drawer_hidden

  def stage_hiding_then_fail_commit(db, row, hidden):
    original_hide(db, row, hidden)
    if hidden:
      def failed_commit():
        raise RuntimeError("injected recovery commit failure")
      monkeypatch.setattr(db, "commit", failed_commit)

  with monkeypatch.context() as failure:
    failure.setattr(autopilot, "stage_followup_drawer_hidden", stage_hiding_then_fail_commit)
    assert _recover() == 0
  row = _row(blocked)
  assert row.enabled is True and row.state == "blocked"
  assert row.blocked_at == blocked.blocked_at
  chat = blocked.db.get(models.Chat, blocked.chat_id)
  assert chat.agent_settings_json["drawer_hidden"] is False
  assert _recover() == 1
  assert _row(blocked).state == "idle"
  assert blocked.db.get(models.Chat, blocked.chat_id).agent_settings_json["drawer_hidden"] is True
