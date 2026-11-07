"""Hermetic read-only proofs for exact signed personal publication effects."""
import json
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import github_contributions as proof

HEAD = "a" * 40
OLD = "b" * 40
BASE = "c" * 40


def response(value, code=0):
  return SimpleNamespace(returncode=code, stdout=json.dumps(value), stderr="")


def fixture(action="pr", phase="pr_ambiguous", labels=None):
  plan = {
    "action": action, "repo": "owner/repo", "repo_path": "/unused",
    "branch": "topic", "base_branch": "main", "head_sha": HEAD,
    "title": "Exact title", "body_draft": "Exact body",
    "labels": labels or [],
  }
  claim = {
    "id": "one", "type": "pr", "repo": "owner/repo", "branch": "topic",
    "head_repository": "owner/repo", "number": 7 if action == "pr_update" else None,
    "plan": plan,
  }
  if action == "pr_update":
    plan["pr_metadata"] = {"old_title": "Exact title", "old_body": "Exact body"}
  request = {
    "action": "create_pr" if action == "pr" else "push",
    "repo": "owner/repo", "head_repository": "owner/repo", "branch": "topic",
    "head_sha": HEAD, "base_branch": "main",
  }
  if action == "pr":
    request.update(title="Exact title", body="Exact body")
  return claim, {"claim_input": claim, "record_patch": {}, "phase": phase,
                 "effective_request": request}


def github_reads(monkeypatch, *, head=HEAD, base="main", title="Exact title", labels=(),
                 branch_code=0, pr_code=0):
  calls = []
  live = {
    "number": 7, "html_url": "https://github.com/owner/repo/pull/7",
    "state": "open", "draft": False, "title": title, "body": "Exact body",
    "head": {"repo": {"full_name": "owner/repo"}, "ref": "topic", "sha": head},
    "base": {"ref": base, "repo": {"full_name": "owner/repo"}},
  }
  def gh(_repo, *args, **_kwargs):
    assert args[0] == "api" and "--method" not in args and "PATCH" not in args
    calls.append(args[1])
    if "/git/ref/heads/topic" in args[1]:
      return response({"object": {"sha": head}}, branch_code)
    if "/pulls?" in args[1]:
      return response([live])
    if "/pulls/7" in args[1]:
      return response(live, pr_code)
    if "/issues/7/labels" in args[1]:
      return response([{"name": name} for name in labels])
    raise AssertionError(args)
  monkeypatch.setattr(proof, "_safe_repo_path", lambda _value: Path("/unused"))
  monkeypatch.setattr(proof._git_ops, "_gh", gh)
  return calls


def test_no_number_create_needs_exact_remote_pr_but_not_its_labels(monkeypatch):
  record, attempt = fixture(labels=["ready"])
  calls = github_reads(monkeypatch, labels=["ready"])
  result = proof.observe_publication_effect(record, attempt)
  assert result["outcome"] == "settled"
  assert result["result"]["number"] == 7
  assert result["result"]["status"] == "open"
  assert len(calls) == 4
  github_reads(monkeypatch, labels=[])
  result = proof.observe_publication_effect(record, attempt)
  assert result["outcome"] == "settled", result
  assert result["result"]["labels_applied"] == []
  assert result["result"]["labels_missing"] == ["ready"]


def test_known_update_requires_signed_head_not_old_public_head(monkeypatch):
  record, attempt = fixture("pr_update", "push_pending")
  github_reads(monkeypatch, head=OLD)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"
  github_reads(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  attempt["effective_request"]["head_sha"] = OLD
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"


def test_no_pre_effect_or_unavailable_read_is_success(monkeypatch):
  record, attempt = fixture()
  attempt["phase"] = "armed"
  github_reads(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"
  attempt["phase"] = "pr_ambiguous"
  github_reads(monkeypatch, branch_code=1)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"
  github_reads(monkeypatch, pr_code=1)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"


def test_wrong_base_or_text_cannot_settle(monkeypatch):
  record, attempt = fixture()
  github_reads(monkeypatch, base="old")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"
  github_reads(monkeypatch, title="Changed")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"


def test_successor_requires_new_head_target_base_and_target_tip(monkeypatch):
  record, attempt = fixture("pr_update", "pr_ambiguous")
  successor = {
    "old_head_sha": OLD, "old_base_sha": "d" * 40,
    "old_base_branch": "old-parent", "base_branch": "main",
  }
  record["plan"]["successor"] = successor
  record["plan"]["base_sha"] = BASE
  attempt["record_patch"] = {"last_submit_push_sha": HEAD}
  attempt["effective_request"].update(
    action="advance_successor", number=7, successor=successor)
  def reads(base):
    def gh(_repo, *args, **_kwargs):
      assert args[0] == "api"
      if args[1].endswith("/git/ref/heads/topic"):
        return response({"object": {"sha": HEAD}})
      if args[1].endswith("/git/ref/heads/main"):
        return response({"object": {"sha": BASE}})
      if args[1].endswith("/pulls/7"):
        return response({
          "number": 7, "html_url": "https://github.com/owner/repo/pull/7",
          "title": "Exact title", "body": "Exact body", "state": "open", "draft": False,
          "head": {"repo": {"full_name": "owner/repo"}, "ref": "topic", "sha": HEAD},
          "base": {"repo": {"full_name": "owner/repo"}, "ref": base},
        })
      raise AssertionError(args)
    monkeypatch.setattr(proof, "_safe_repo_path", lambda _value: Path("/unused"))
    monkeypatch.setattr(proof._git_ops, "_gh", gh)
  reads("old-parent")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "pending"
  reads("main")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  attempt["record_patch"] = {}
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"


def proof_remote(monkeypatch, *, live_patch=None, branch=HEAD, list_rows=None,
                 pr_code=0, branch_code=0, labels=(), labels_code=0,
                 parent_sha=OLD, parent_ref="main", raises=None, malformed=None):
  """Only `gh api` reads; no staged worktree or source command is available."""
  live = {
    "number": 7, "html_url": "https://github.com/owner/repo/pull/7",
    "state": "open", "draft": False, "title": "Exact title", "body": "Exact body",
    "head": {"repo": {"full_name": "owner/repo"}, "ref": "topic", "sha": HEAD},
    "base": {"ref": parent_ref, "repo": {"full_name": "owner/repo"}},
  }
  if live_patch:
    for key, value in live_patch.items():
      live[key] = value
  calls = []
  def gh(cwd, *args, **_kwargs):
    assert cwd == Path(proof.get_settings().data_dir)
    assert args[0] == "api" and len(args) == 2
    calls.append(args[1])
    if raises and raises in args[1]:
      raise __import__("subprocess").TimeoutExpired("gh", 1)
    if "/git/ref/heads/topic" in args[1]:
      return response({"object": {"sha": branch}}, branch_code)
    if f"/git/ref/heads/{parent_ref}" in args[1]:
      return response({"object": {"sha": parent_sha}})
    if "/pulls?" in args[1]:
      return response([live] if list_rows is None else list_rows)
    if "/pulls/7" in args[1]:
      return SimpleNamespace(returncode=pr_code,
                             stdout="{bad" if malformed == "pr" else json.dumps(live), stderr="")
    if "/issues/7/labels" in args[1]:
      return response([{"name": x} for x in labels], labels_code)
    raise AssertionError(args)
  monkeypatch.setattr(proof, "_safe_repo_path", lambda *_: pytest.fail("source path inspected"))
  monkeypatch.setattr(proof._git_ops, "_gh", gh)
  return calls


@pytest.mark.parametrize("action,phase,effect", [
  ("pr", "pr_ambiguous", "create_pr"),
  ("pr", "complete", "create_pr"),
  ("pr", "complete", "reconcile_pr"),
  ("pr_update", "push_pending", "push"),
  ("pr_update", "branch_published", "push"),
  ("pr_update", "complete", "update_pr"),
  ("pr_update", "complete", "reconcile_pr"),
])
def test_actual_publication_emitter_phase_pairs(monkeypatch, action, phase, effect):
  record, attempt = fixture(action, phase)
  request = attempt["effective_request"]
  request["action"] = effect
  if action == "pr":
    request["number"] = 7
  if effect == "reconcile_pr":
    request.pop("title", None)
    request.pop("body", None)
  if effect in {"reconcile_pr", "update_pr"}:
    request["url"] = "https://github.com/owner/repo/pull/7"
    request["number"] = 7
  proof_remote(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"


def test_normalized_signed_patch_head_not_old_claim_head(monkeypatch):
  record, attempt = fixture("pr_update", "complete")
  old_plan = copy.deepcopy(record["plan"])
  old_plan["head_sha"] = OLD
  record["plan"] = old_plan
  attempt["record_patch"] = {"head_sha": HEAD, "plan": {
    **old_plan, "head_sha": HEAD, "attribution_normalized_from": OLD}}
  attempt["effective_request"]["action"] = "update_pr"
  attempt["effective_request"]["number"] = 7
  proof_remote(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  attempt["effective_request"]["head_sha"] = OLD
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"


@pytest.mark.parametrize("field,value", [
  ("number", 8), ("html_url", "https://github.com/owner/repo/pull/8"),
  ("title", "other"), ("body", "other"),
  ("head", {"repo": {"full_name": "wrong/repo"}, "ref": "topic", "sha": HEAD}),
  ("head", {"repo": {"full_name": "owner/repo"}, "ref": "wrong", "sha": HEAD}),
  ("head", {"repo": {"full_name": "owner/repo"}, "ref": "topic", "sha": OLD}),
  ("base", {"repo": {"full_name": "wrong/repo"}, "ref": "main"}),
])
def test_wrong_remote_pr_identity_never_settles(monkeypatch, field, value):
  record, attempt = fixture("pr_update", "push_pending")
  proof_remote(monkeypatch, live_patch={field: value})
  assert proof.observe_publication_effect(record, attempt)["outcome"] != "settled"


@pytest.mark.parametrize("change", [
  {"repo": "wrong/repo"}, {"head_repository": "wrong/repo"},
  {"head_sha": OLD}, {"branch": "wrong"}, {"base_branch": "wrong"},
  {"number": 8}, {"url": "https://github.com/owner/repo/pull/8"},
])
def test_wrong_signed_effect_identity_never_settles(monkeypatch, change):
  record, attempt = fixture("pr_update", "complete")
  attempt["effective_request"].update(action="update_pr", **change)
  proof_remote(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] != "settled"


@pytest.mark.parametrize("live,expected", [
  ({"state": "closed"}, "unavailable"),
  ({"state": "closed", "merged_at": "2026-01-01T00:00:00Z"}, "unavailable"),
  ({"state": "closed", "merged_at": "2026-01-01T00:00:00Z", "merge_commit_sha": "bad"}, "unavailable"),
  ({"state": "closed", "closed_at": "2026-01-01T00:00:00Z"}, "settled"),
  ({"state": "closed", "merged_at": "2026-01-01T00:00:00Z", "merge_commit_sha": BASE}, "settled"),
])
def test_terminal_deleted_branch_requires_exact_terminal_identity(monkeypatch, live, expected):
  record, attempt = fixture("pr_update", "complete")
  attempt["effective_request"].update(action="update_pr", number=7)
  proof_remote(monkeypatch, live_patch=live, branch_code=1)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == expected


@pytest.mark.parametrize("variant,expected", [
  ("branch403", "unavailable"), ("branch404", "unavailable"),
  ("branch429", "unavailable"), ("timeout", "unavailable"),
  ("pr403", "unavailable"), ("pr404", "unavailable"),
  ("pr429", "unavailable"), ("malformed", "unavailable"),
  ("emptylist", "pending"), ("otherhead", "pending"),
  ("emptylist-branch404", "unavailable"), ("emptylist-branchmoved", "changed"),
  ("badlist", "unavailable"), ("twoprs", "unavailable"),
])
def test_remote_unavailable_and_branch_only_never_settle(monkeypatch, variant, expected):
  record, attempt = fixture()
  kwargs = {}
  if variant.startswith("branch"):
    kwargs["branch_code"] = int(variant[-3:])
  elif variant.startswith("pr"):
    kwargs["pr_code"] = int(variant[-3:])
  elif variant == "timeout":
    kwargs["raises"] = "/pulls/7"
    attempt["effective_request"]["number"] = 7
  elif variant == "malformed":
    kwargs["malformed"] = "pr"
    attempt["effective_request"]["number"] = 7
  elif variant.startswith("emptylist"):
    kwargs["list_rows"] = []
    kwargs.update({"-branch404": {"branch_code": 404},
                   "-branchmoved": {"branch": OLD}}.get(variant[9:], {}))
  elif variant == "otherhead":
    stale = {"number": 3, "html_url": "https://github.com/owner/repo/pull/3",
             "head": {"ref": "topic", "sha": OLD}}
    kwargs["list_rows"] = [stale]
  elif variant == "badlist":
    kwargs["list_rows"] = {"message": "not a list"}
  elif variant == "twoprs":
    row = {"html_url": "https://github.com/owner/repo/pull/{}", "head": {"ref": "topic", "sha": HEAD}}
    kwargs["list_rows"] = [{**row, "number": n, "html_url": row["html_url"].format(n)} for n in (7, 8)]
  proof_remote(monkeypatch, **kwargs)
  result = proof.observe_publication_effect(record, attempt)
  assert result["outcome"] == expected
  # Only an exact pushed branch with no PR is the branch-only phase; a pushed
  # branch is never reported as a sent PR, and nothing here creates one.
  assert (result.get("effect") == "branch_published") is (expected == "pending")
  assert "result" not in result


def test_linked_child_requires_current_exact_reviewed_parent_sha(monkeypatch):
  record, attempt = fixture("pr_update", "branch_published")
  record["plan"]["stack"] = {"parent_record_id": "parent", "base_branch": "parent-branch"}
  record["plan"]["base_sha"] = OLD
  attempt["effective_request"]["base_branch"] = "parent-branch"
  proof_remote(monkeypatch, parent_sha=OLD, parent_ref="parent-branch")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  proof_remote(monkeypatch, parent_sha=BASE, parent_ref="parent-branch")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"
  record["plan"].pop("base_sha")
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"


@pytest.mark.parametrize("labels,code,applied,missing,note", [
  (["Ready", "reviewed"], 0, ["Ready", "reviewed"], None, False),
  (["ready"], 0, ["ready"], ["reviewed"], True),
  ((), 403, [], None, True),
  ((), 0, [], ["ready", "reviewed"], True),
])
def test_labels_are_nonfatal_metadata_like_send(monkeypatch, labels, code, applied, missing, note):
  """A missing or unreadable label never strands an exact published PR."""
  record, attempt = fixture(labels=["ready", "reviewed"])
  calls = proof_remote(monkeypatch, labels=labels, labels_code=code)
  result = proof.observe_publication_effect(record, attempt)
  assert result["outcome"] == "settled", result
  assert result["result"]["labels_requested"] == ["ready", "reviewed"]
  assert result["result"]["labels_applied"] == applied
  assert result["result"].get("labels_missing") == missing
  assert ("labels_note" in result["result"]) is note
  assert sum("/labels" in call for call in calls) == 1  # read only, never reapplied


@pytest.mark.parametrize("action,phase,effect", [
  ("pr", "branch_published", "create_pr"),
  ("pr_update", "push_ambiguous", "push"),
])
def test_remaining_regular_emitter_pairs(monkeypatch, action, phase, effect):
  record, attempt = fixture(action, phase)
  attempt["effective_request"]["action"] = effect
  proof_remote(monkeypatch)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"


@pytest.mark.parametrize("phase,effect", [
  ("push_pending", "advance_successor_branch"),
  ("branch_published", "advance_successor_branch"),
  ("pr_ambiguous", "retarget_successor_pr"),
  ("pr_ambiguous", "advance_successor"),
  ("complete", "advance_successor"),
])
def test_successor_actual_emitter_pairs(monkeypatch, phase, effect):
  record, attempt = fixture("pr_update", phase)
  successor = {"old_head_sha": OLD, "old_base_sha": "d" * 40,
               "old_base_branch": "old-parent", "base_branch": "main"}
  record["plan"].update(successor=successor, base_sha=BASE)
  request = attempt["effective_request"]
  request.update(action=effect, number=7)
  if effect == "advance_successor_branch":
    request.update(previous_head_sha=OLD, target_base_branch="main",
                   base_branch="old-parent")
  elif effect == "retarget_successor_pr":
    request["base_branch"] = "main"
  else:
    request["successor"] = successor
    attempt["record_patch"] = {"last_submit_push_sha": HEAD}
  proof_remote(monkeypatch, parent_sha=BASE)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  if effect == "advance_successor":
    attempt["record_patch"] = {}  # outer catch can emit this before a push
    assert proof.observe_publication_effect(record, attempt)["outcome"] == "unavailable"


def test_linked_child_wrong_signed_parent_ref_rejected(monkeypatch):
  record, attempt = fixture("pr_update", "branch_published")
  record["plan"].update(base_sha=OLD, stack={
    "parent_record_id": "parent", "base_branch": "parent-branch"})
  proof_remote(monkeypatch, parent_ref="parent-branch", parent_sha=OLD)
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "changed"


@pytest.mark.parametrize("live,status", [
  ({}, "open"),
  ({"draft": True}, "draft"),
  ({"state": "closed", "closed_at": "2026-01-01T00:00:00Z"}, "closed"),
  ({"state": "closed", "merged_at": "2026-01-01T00:00:00Z", "merge_commit_sha": BASE}, "merged"),
])
def test_exact_update_after_lost_push_reply_settles_its_live_lifecycle(monkeypatch, live, status):
  """A disconnect after an accepted push settles to GitHub's exact lifecycle."""
  record, attempt = fixture("pr_update", "push_ambiguous")
  proof_remote(monkeypatch, live_patch=live)
  result = proof.observe_publication_effect(record, attempt)
  assert result["outcome"] == "settled", result
  assert result["result"]["status"] == status and result["result"]["number"] == 7


SHOT = {"name": "after.png", "sha256": "f" * 64, "alt": "After"}
RAW_BODY = "Exact body\n\n![After](screenshot:after.png)"


def _screenshot_fixture(phase="pr_ambiguous", witness=True):
  record, attempt = fixture(phase=phase)
  record["plan"].update(body_draft=RAW_BODY, screenshots=[SHOT])
  resolved = proof.contribution_screenshots.resolve_body(
    RAW_BODY, [SHOT], assets_repo="owner/repo", commit="e" * 40, branch="topic",
  )
  attempt["effective_request"]["body"] = resolved
  if witness:
    attempt["record_patch"] = {"last_submit_screenshots": {"repo": "owner/repo", "commit": "e" * 40}}
  return record, attempt, resolved


def test_screenshot_pr_settles_against_its_signed_resolved_body(monkeypatch):
  """Send publishes pinned image links, not the reviewed placeholders."""
  record, attempt, resolved = _screenshot_fixture()
  assert "screenshot:after.png" not in resolved
  proof_remote(monkeypatch, live_patch={"body": resolved})
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"
  # A complete receipt saved before its ledger write is the same exact proof.
  record, attempt, resolved = _screenshot_fixture("complete")
  attempt["effective_request"].update(number=7, url="https://github.com/owner/repo/pull/7")
  proof_remote(monkeypatch, live_patch={"body": resolved})
  assert proof.observe_publication_effect(record, attempt)["outcome"] == "settled"


@pytest.mark.parametrize("damage,expected", [
  ("live-raw", "changed"), ("live-other-commit", "changed"),
  ("request-raw", "changed"), ("no-witness", "unavailable"), ("bad-commit", "unavailable"),
])
def test_screenshot_body_mismatch_or_missing_witness_never_settles(monkeypatch, damage, expected):
  record, attempt, resolved = _screenshot_fixture(witness=damage != "no-witness")
  live_body = resolved
  if damage == "live-raw":
    live_body = RAW_BODY
  elif damage == "live-other-commit":
    live_body = resolved.replace("e" * 40, "d" * 40)
  elif damage == "request-raw":
    attempt["effective_request"]["body"] = RAW_BODY
  elif damage == "bad-commit":
    attempt["record_patch"]["last_submit_screenshots"]["commit"] = "not-a-commit"
  proof_remote(monkeypatch, live_patch={"body": live_body})
  assert proof.observe_publication_effect(record, attempt)["outcome"] == expected
