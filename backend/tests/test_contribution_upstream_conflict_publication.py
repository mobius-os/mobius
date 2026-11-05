"""A new standalone PR may open while it overlaps newer upstream.

The owner approved the exact reviewed change. GitHub shows a new PR as
merge-base..head, which equals that reviewed diff while the reviewed base is
the sole merge base with today's upstream, so the overlap becomes the PR's
next update instead of a send-time refusal. Stacks and existing-PR updates
keep the clean-merge gate, and failing to learn the merge fact is never
relabelled as a conflict.
"""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app import github_contributions
from app import github_contribution_git as git_ops
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.github_contributions import _submit_prepared_pr
from app.routes import github as github_routes
from tests.test_github_routes import (  # noqa: F401  (autouse fixtures re-registered here)
  _UPSTREAM_SHA, _allow_synthetic_source_provenance, _app_token, _commit_metadata, _cp,
  _conflicting_upstream_commit, _github_state, _prepared_real_review,
  _prepared_review_publication_unavailable, _record_upstream, _submit_preflight_response,
  _synthetic_repos_answer_the_canonical_diff, _write_contribution, _write_token,
)

BASE = "b" * 40
HEAD = "a" * 40
DIFF = "diff --git a/index.jsx b/index.jsx\n+hello\n"


# -- the owning git seam ----------------------------------------------------------

def run(repo, *args):
  return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                        capture_output=True).stdout.strip()


@pytest.fixture
def histories(tmp_path, monkeypatch):
  """A reviewed topic branch plus an upstream tip that edits the same line."""
  repo = tmp_path / "staged"
  repo.mkdir()
  run(repo, "init", "-q", "-b", "main")
  run(repo, "config", "user.name", "Test")
  run(repo, "config", "user.email", "test@example.com")
  (repo / "index.jsx").write_text("export default 1\n")
  run(repo, "add", "index.jsx")
  run(repo, "commit", "-qm", "base")
  base = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "-b", "fix/topic")
  (repo / "index.jsx").write_text("export default 2\n")
  run(repo, "commit", "-qam", "reviewed fix")
  head = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "main")
  (repo / "index.jsx").write_text("export default 9\n")
  run(repo, "commit", "-qam", "upstream overlaps")
  conflicting = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "--orphan", "rewritten")
  (repo / "index.jsx").write_text("export default 7\n")
  run(repo, "commit", "-qam", "rewritten upstream")
  rewritten = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "fix/topic")
  upstream = {"sha": conflicting}
  monkeypatch.setattr(git_ops, "_upstream_default_branch", lambda *a: "main")
  monkeypatch.setattr(git_ops, "fetch_upstream_head", lambda *a: upstream["sha"])
  return repo, upstream, {"base": base, "head": head, "conflicting": conflicting, "rewritten": rewritten}


def test_conflict_is_reported_as_a_fact_with_the_exact_upstream_tip(histories):
  repo, _upstream, shas = histories
  merge = git_ops.inspect_upstream_merge(repo, "owner/repo", "fix/topic")
  assert (merge.branch, merge.sha, merge.conflicts) == ("main", shas["conflicting"], True)


def test_new_standalone_pr_conflict_returns_the_exact_upstream_tip(histories):
  repo, _upstream, shas = histories
  patch = git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/topic", shas["base"], shas["head"])
  assert patch == {
    "last_submit_upstream_branch": "main",
    "last_submit_upstream_sha": shas["conflicting"],
    # GitHub runs no PR workflows on a conflicting PR, so the record says so.
    "last_submit_upstream_conflict": {
      "upstream_branch": "main", "upstream_sha": shas["conflicting"],
      "head_sha": shas["head"], "next_pr_action": "merge_upstream_into_pr",
    },
  }


def test_clean_new_pr_preflight_keeps_its_exact_shape(histories):
  repo, upstream, shas = histories
  upstream["sha"] = shas["base"]
  assert git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/topic", shas["base"], shas["head"]) == {
    "last_submit_upstream_branch": "main", "last_submit_upstream_sha": shas["base"]}


def test_reviewed_base_absent_from_upstream_still_refuses(histories):
  """Otherwise GitHub would show a different diff than the one reviewed."""
  repo, upstream, shas = histories
  upstream["sha"] = shas["rewritten"]
  with pytest.raises(ContributionSubmitError) as error:
    git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/topic", shas["base"], shas["head"])
  assert error.value.code == "upstream_conflict"
  assert error.value.record_patch["last_submit_upstream_sha"] == shas["rewritten"]


def test_stack_and_update_gate_still_raises_with_a_structured_code(histories):
  repo, _upstream, shas = histories
  with pytest.raises(ContributionSubmitError) as error:
    git_ops._assert_merges_with_upstream(repo, "owner/repo", "fix/topic")
  assert error.value.code == "upstream_conflict"
  assert "no longer merges cleanly" in error.value.message
  assert error.value.record_patch == {
    "last_submit_upstream_branch": "main", "last_submit_upstream_sha": shas["conflicting"]}


@pytest.mark.parametrize("operation", ["new_pr", "gate"])
@pytest.mark.parametrize(("failure", "code"), [
  ("upstream_fetch_unavailable", "upstream_fetch_unavailable"),
  ("upstream_fetch_failed", "upstream_fetch_failed"),
  ("upstream_unresolved", None),
])
def test_unknown_merge_facts_are_never_labelled_conflicts(tmp_path, monkeypatch, operation, failure, code):
  monkeypatch.setattr(git_ops, "_upstream_default_branch", lambda *a: "main")
  def unavailable(*a):
    raise ContributionSubmitError("GitHub could not provide upstream main.", code=failure)
  monkeypatch.setattr(git_ops, "fetch_upstream_head", unavailable)
  monkeypatch.setattr(git_ops, "_git", lambda *a, **k: pytest.fail("no merge without an upstream tip"))
  with pytest.raises(ContributionSubmitError) as error:
    if operation == "new_pr":
      git_ops.upstream_merge_for_new_pr(tmp_path, "owner/repo", "fix/topic", BASE, HEAD)
    else:
      git_ops._assert_merges_with_upstream(tmp_path, "owner/repo", "fix/topic")
  assert error.value.code == code
  assert error.value.code != "upstream_conflict"
  assert error.value.record_patch == {"last_submit_upstream_branch": "main"}


def test_an_unreadable_ancestry_comparison_is_not_a_conflict_or_a_pass(tmp_path, monkeypatch):
  monkeypatch.setattr(git_ops, "_upstream_default_branch", lambda *a: "main")
  monkeypatch.setattr(git_ops, "fetch_upstream_head", lambda *a: _UPSTREAM_SHA)
  def fake_git(_repo, *args, check=True):
    if args[:1] == ("merge-tree",):
      return _cp(returncode=1)
    if args[:2] == ("merge-base", "--all"):
      return _cp(stderr="fatal: bad object", returncode=128)
    pytest.fail(f"unexpected git {args}")
  monkeypatch.setattr(git_ops, "_git", fake_git)
  with pytest.raises(ContributionSubmitError) as error:
    git_ops.upstream_merge_for_new_pr(tmp_path, "owner/repo", "fix/topic", BASE, HEAD)
  assert error.value.code == "upstream_merge_unavailable" and "Could not compare" in error.value.message
  assert "fatal: bad object" in error.value.detail


@pytest.mark.parametrize("operation", ["inspect", "new_pr", "gate"])
@pytest.mark.parametrize(("returncode", "stderr"), [(128, "fatal: unable to read tree 0123"),
                                                    (129, "error: unknown option `write-tree'"),
                                                    (2, "")])
@pytest.mark.parametrize("related", ["related", "unreadable"])
def test_a_merge_tree_that_could_not_run_is_never_a_conflict_or_a_publishable_fact(
    tmp_path, monkeypatch, operation, returncode, stderr, related):
  """Only exit 0 (clean) and exit 1 (conflict) are merge verdicts.

  Anything else is an unknown verdict: it raises its own code with git's own
  diagnostics and the exact upstream tip, and never opens a PR as an overlap.
  """
  monkeypatch.setattr(git_ops, "_upstream_default_branch", lambda *a: "main")
  monkeypatch.setattr(git_ops, "fetch_upstream_head", lambda *a: _UPSTREAM_SHA)
  def fake_git(_repo, *args, check=True):
    if args[:1] == ("merge-tree",):
      return _cp(stderr=stderr, returncode=returncode)
    if args == ("merge-base", _UPSTREAM_SHA, "fix/topic"):
      # Related histories (or an unreadable answer): never proof of a conflict.
      return _cp(BASE + "\n") if related == "related" else _cp(stderr="fatal: bad object", returncode=128)
    pytest.fail(f"no further git after an unknown merge verdict: {args}")
  monkeypatch.setattr(git_ops, "_git", fake_git)
  with pytest.raises(ContributionSubmitError) as error:
    if operation == "inspect":
      git_ops.inspect_upstream_merge(tmp_path, "owner/repo", "fix/topic")
    elif operation == "new_pr":
      git_ops.upstream_merge_for_new_pr(tmp_path, "owner/repo", "fix/topic", BASE, HEAD)
    else:
      git_ops._assert_merges_with_upstream(tmp_path, "owner/repo", "fix/topic")
  assert error.value.code == "upstream_merge_unavailable"
  assert "Nothing was published" in error.value.message
  assert error.value.detail == (stderr or f"git merge-tree exited {returncode} without diagnostic output.")
  assert error.value.record_patch == {
    "last_submit_upstream_branch": "main", "last_submit_upstream_sha": _UPSTREAM_SHA}


def test_upstream_already_holding_part_of_the_topic_is_not_the_reviewed_diff(histories):
  """Ancestry is not enough: a shared commit moves GitHub's merge base.

  The reviewed base is still an ancestor of upstream, but upstream already
  merged the topic's first commit. GitHub would show only the later commit,
  not the reviewed diff, so the conflict exemption must refuse.
  """
  repo, upstream, shas = histories
  run(repo, "checkout", "-q", "-b", "fix/two-step", shas["base"])
  (repo / "first.jsx").write_text("first\n")
  run(repo, "add", "first.jsx")
  run(repo, "commit", "-qm", "first reviewed step")
  shared = run(repo, "rev-parse", "HEAD")
  (repo / "index.jsx").write_text("export default 2\n")
  run(repo, "commit", "-qam", "second reviewed step")
  run(repo, "checkout", "-q", "main")
  run(repo, "merge", "-q", "--no-ff", "-m", "upstream takes the first step", shared)
  upstream["sha"] = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "fix/two-step")
  assert run(repo, "merge-base", "--is-ancestor", shas["base"], upstream["sha"]) == ""
  assert run(repo, "merge-base", upstream["sha"], "fix/two-step") == shared
  assert git_ops.inspect_upstream_merge(repo, "owner/repo", "fix/two-step").conflicts
  with pytest.raises(ContributionSubmitError) as error:
    git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/two-step", shas["base"], HEAD)
  assert error.value.code == "upstream_conflict"


def test_a_clean_merge_after_upstream_took_part_of_the_topic_still_refuses(histories):
  """A clean merge is not proof of the reviewed diff either.

  Upstream merged the topic's first commit and nothing else, so the branch
  merges cleanly, yet GitHub would show only the second commit.
  """
  repo, upstream, shas = histories
  run(repo, "checkout", "-q", "-b", "fix/partly-landed", shas["base"])
  (repo / "first.jsx").write_text("first\n")
  run(repo, "add", "first.jsx")
  run(repo, "commit", "-qm", "first reviewed step")
  shared = run(repo, "rev-parse", "HEAD")
  (repo / "second.jsx").write_text("second\n")
  run(repo, "add", "second.jsx")
  run(repo, "commit", "-qm", "second reviewed step")
  run(repo, "checkout", "-q", "-b", "upstream-took-first", shas["base"])
  run(repo, "merge", "-q", "--no-ff", "-m", "upstream takes the first step", shared)
  upstream["sha"] = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "fix/partly-landed")
  assert not git_ops.inspect_upstream_merge(repo, "owner/repo", "fix/partly-landed").conflicts
  with pytest.raises(ContributionSubmitError) as error:
    git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/partly-landed", shas["base"], HEAD)
  assert error.value.code == "upstream_conflict"
  assert "already contains part of this PR" in error.value.message
  assert error.value.record_patch == {
    "last_submit_upstream_branch": "main", "last_submit_upstream_sha": upstream["sha"]}
  # A stack layer or an existing-PR update keeps its clean-merge-only gate.
  assert git_ops._assert_merges_with_upstream(repo, "owner/repo", "fix/partly-landed") == {
    "last_submit_upstream_branch": "main", "last_submit_upstream_sha": upstream["sha"]}


def test_reviewed_base_as_one_of_several_merge_bases_is_not_the_reviewed_diff(histories):
  """A criss-cross history has more than one merge base.

  The reviewed base is one of them, but GitHub picks a single merge base for
  ``merge-base..head``, so the published diff is not provably the reviewed
  one. The conflict exemption requires the SOLE merge base and must refuse.
  """
  repo, upstream, shas = histories
  run(repo, "checkout", "-q", "-b", "reviewed-base", shas["base"])
  (repo / "base.txt").write_text("reviewed base\n")
  run(repo, "add", "base.txt")
  run(repo, "commit", "-qm", "reviewed base")
  reviewed_base = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "-b", "side", shas["base"])
  (repo / "side.txt").write_text("side\n")
  run(repo, "add", "side.txt")
  run(repo, "commit", "-qm", "side history")
  side = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "-b", "fix/criss-cross", reviewed_base)
  (repo / "index.jsx").write_text("export default 2\n")
  run(repo, "commit", "-qam", "reviewed fix")
  run(repo, "merge", "-q", "--no-ff", "-m", "topic takes side", side)
  run(repo, "checkout", "-q", "-b", "upstream-criss-cross", reviewed_base)
  (repo / "index.jsx").write_text("export default 9\n")
  run(repo, "commit", "-qam", "upstream overlaps")
  run(repo, "merge", "-q", "--no-ff", "-m", "upstream takes side", side)
  upstream["sha"] = run(repo, "rev-parse", "HEAD")
  run(repo, "checkout", "-q", "fix/criss-cross")
  bases = set(run(repo, "merge-base", "--all", upstream["sha"], "fix/criss-cross").split())
  assert bases == {reviewed_base, side}
  assert git_ops.inspect_upstream_merge(repo, "owner/repo", "fix/criss-cross").conflicts
  with pytest.raises(ContributionSubmitError) as error:
    git_ops.upstream_merge_for_new_pr(repo, "owner/repo", "fix/criss-cross", reviewed_base, HEAD)
  assert error.value.code == "upstream_conflict"


# -- the owner-approved Send path ---------------------------------------------------

def standalone_record(record_id, repo):
  return {
    "id": record_id, "type": "pr", "repo": "mobius-os/app-demo", "status": "prepared",
    "title": "Polish demo", "branch": "fix/demo-polish",
    "created_at": "2026-07-09T00:00:00Z", "updated_at": "2026-07-09T00:00:00Z",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo", "title": "Polish demo",
             "body_draft": "## What\n\nPolishes the demo.", "branch": "fix/demo-polish",
             "repo_path": str(repo), "base_sha": BASE, "head_sha": HEAD,
             "diff_sha256": hashlib.sha256(DIFF.encode()).hexdigest()},
  }


def send_fakes(monkeypatch, *, base_on_upstream=True, merge_conflict=True):
  git_calls, gh_calls = [], []
  fork_ready = {"value": False}
  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args, merge_conflict=merge_conflict, merge_base=None)) is not None:
      return preflight
    if args == ("merge-base", "--all", _UPSTREAM_SHA, "fix/demo-polish"):
      return _cp(BASE + "\n") if base_on_upstream else _cp(returncode=1)
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("develop\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args in {("rev-parse", "fix/demo-polish"), ("rev-parse", "--verify", "fix/demo-polish^{commit}")}:
      return _cp(HEAD + "\n")
    if args == ("rev-parse", "--verify", f"{BASE}^{{commit}}"):
      return _cp(BASE + "\n")
    if args == ("rev-parse", "--verify", f"{HEAD}^{{commit}}"):
      return _cp(HEAD + "\n")
    if "diff" in args and args[-1:] == (f"{BASE}..{HEAD}",):
      return _cp(DIFF)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp("Polish demo\n\nCo-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n")
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(HEAD)
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/mobius-os/app-demo.git\n")
    if args == ("remote", "get-url", "fork"):
      return _cp("https://github.com/octocat/app-demo-1.git\n") if fork_ready["value"] else _cp(returncode=1)
    return _cp("")
  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
    if args[:2] == ("repo", "fork"):
      fork_ready["value"] = True
      return _cp("")
    if args[:2] == ("repo", "view"):
      return _cp("main\n")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/42\n")
    if args == ("api", "repos/mobius-os/app-demo/pulls/42"):
      return _cp(json.dumps({"state": "open", "draft": False,
        "html_url": "https://github.com/mobius-os/app-demo/pull/42",
        "head": {"ref": "fix/demo-polish", "sha": HEAD, "repo": {"full_name": "octocat/app-demo-1"}},
        "base": {"ref": "main"}}))
    if args[:2] == ("api", "--paginate"):
      return _cp("")
    return _cp("")
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  return git_calls, gh_calls


def stored(app_id, record_id):
  return json.loads((Path(get_settings().data_dir) / "apps" / str(app_id) /
                     "contributions" / f"{record_id}.json").read_text())


def test_send_opens_the_exact_reviewed_pr_over_an_upstream_overlap(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo = Path(get_settings().data_dir) / "contrib" / "rec-overlap" / "repo"
  (repo / ".git").mkdir(parents=True)
  _write_contribution(app_id, "rec-overlap", standalone_record("rec-overlap", repo), DIFF)
  git_calls, gh_calls = send_fakes(monkeypatch)
  response = client.post(f"/api/github/contributions/{app_id}/rec-overlap/submit",
                         headers={"Authorization": f"Bearer {app_token}"},
                         json={"publication_stage": "ready"})
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert record["status"] == "open" and record["number"] == 42
  assert record["last_submit_upstream_sha"] == _UPSTREAM_SHA
  assert "last_submit_error" not in record and "last_submit_error_code" not in record
  # GitHub runs no PR workflows on a conflicting PR: the record says it needs
  # upstream merged in, so the PR cannot sit there silently.
  signal = {"upstream_branch": "main", "upstream_sha": _UPSTREAM_SHA,
            "head_sha": HEAD, "next_pr_action": "merge_upstream_into_pr"}
  assert record["last_submit_upstream_conflict"] == signal
  assert stored(app_id, "rec-overlap")["last_submit_upstream_conflict"] == signal
  # The reviewed diff was proven to be what GitHub shows, and published as-is.
  assert ("merge-base", "--all", _UPSTREAM_SHA, "fix/demo-polish") in git_calls
  assert ("push", "--force-with-lease=refs/heads/fix/demo-polish:", "fork",
          "HEAD:refs/heads/fix/demo-polish") in git_calls
  assert not any(call[:1] in {("rebase",), ("merge",), ("reset",)} for call in git_calls)
  create = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert create[-2:] == ("--base", "main") and "--draft" not in create
  assert stored(app_id, "rec-overlap")["status"] == "open"


def test_send_without_a_body_still_publishes_a_draft_without_autopilot(client, owner_token, monkeypatch):
  """Conflict handling never changes the explicit stage or grants autopilot."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo = Path(get_settings().data_dir) / "contrib" / "rec-overlap-draft" / "repo"
  (repo / ".git").mkdir(parents=True)
  _write_contribution(app_id, "rec-overlap-draft", standalone_record("rec-overlap-draft", repo), DIFF)
  _git_calls, gh_calls = send_fakes(monkeypatch)
  response = client.post(f"/api/github/contributions/{app_id}/rec-overlap-draft/submit",
                         headers={"Authorization": f"Bearer {app_token}"})
  assert response.status_code == 200, response.text
  create = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert "--draft" in create
  assert "autopilot" not in response.json()["record"] or not response.json()["record"]["autopilot"].get("enabled")


def test_send_refuses_when_the_reviewed_base_left_upstream(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo = Path(get_settings().data_dir) / "contrib" / "rec-rewritten" / "repo"
  (repo / ".git").mkdir(parents=True)
  _write_contribution(app_id, "rec-rewritten", standalone_record("rec-rewritten", repo), DIFF)
  git_calls, gh_calls = send_fakes(monkeypatch, base_on_upstream=False)
  response = client.post(f"/api/github/contributions/{app_id}/rec-rewritten/submit",
                         headers={"Authorization": f"Bearer {app_token}"})
  assert response.status_code == 409
  detail = response.json()["detail"]
  assert detail["code"] == "upstream_conflict"
  assert detail["record"]["status"] == "prepared"
  assert detail["record"]["last_submit_error_code"] == "upstream_conflict"
  assert detail["record"]["last_submit_upstream_sha"] == _UPSTREAM_SHA
  assert not any(call[:1] == ("push",) for call in git_calls)
  assert not any(call[:2] == ("pr", "create") for call in gh_calls)


def test_send_refuses_a_clean_merge_whose_reviewed_base_is_no_longer_the_merge_base(
    client, owner_token, monkeypatch):
  """Upstream took part of the topic: it merges cleanly, but GitHub would show
  a smaller diff than the one reviewed, so nothing is pushed."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo = Path(get_settings().data_dir) / "contrib" / "rec-partly-landed" / "repo"
  (repo / ".git").mkdir(parents=True)
  _write_contribution(app_id, "rec-partly-landed", standalone_record("rec-partly-landed", repo), DIFF)
  git_calls, gh_calls = send_fakes(monkeypatch, base_on_upstream=False, merge_conflict=False)
  response = client.post(f"/api/github/contributions/{app_id}/rec-partly-landed/submit",
                         headers={"Authorization": f"Bearer {app_token}"})
  assert response.status_code == 409
  detail = response.json()["detail"]
  assert detail["code"] == "upstream_conflict"
  assert detail["record"]["status"] == "prepared"
  assert "last_submit_upstream_conflict" not in detail["record"]
  assert not any(call[:1] == ("push",) for call in git_calls)
  assert not any(call[:2] == ("pr", "create") for call in gh_calls)


def direct_submit(monkeypatch, tmp_path, *, plan=None, **kwargs):
  _write_token(login="octocat")
  repo = Path(get_settings().data_dir) / "contrib" / f"rec-direct-{tmp_path.name}" / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_path = tmp_path / "reviewed.diff"
  diff_path.write_text(DIFF)
  record = standalone_record("rec-direct", repo)
  record["plan"].update(plan or {})
  git_calls, gh_calls = send_fakes(monkeypatch)
  monkeypatch.setattr(git_ops, "_upstream_default_branch", lambda *a: "main")
  monkeypatch.setattr(git_ops, "_assert_fresh", lambda *a, **k: (BASE, HEAD, record["plan"]["diff_sha256"]))
  monkeypatch.setattr(git_ops, "_assert_coauthor_trailer", lambda *a: None)
  monkeypatch.setattr(git_ops, "_assert_head_attribution", lambda *a, **k: None)
  monkeypatch.setattr(github_contributions, "_authoritative_public_reconciliation", lambda *a, **k: None)
  monkeypatch.setattr(git_ops, "upstream_merge_for_new_pr",
                      lambda *a: pytest.fail("only a new standalone PR may publish over a conflict"))
  with pytest.raises(ContributionSubmitError) as error:
    _submit_prepared_pr(record, diff_path, **kwargs)
  assert error.value.code == "upstream_conflict"
  assert not any(call[:1] == ("push",) for call in git_calls)
  assert not any(call[:2] == ("pr", "create") for call in gh_calls)


def test_stack_layer_keeps_the_clean_merge_gate(monkeypatch, tmp_path):
  direct_submit(monkeypatch, tmp_path, direct_base_branch="main",
                plan={"stack": {"id": "demo", "position": 1, "total": 2, "base_branch": "main"}})


def test_record_carrying_a_stack_keeps_the_gate_even_without_a_base_override(monkeypatch, tmp_path):
  direct_submit(monkeypatch, tmp_path,
                plan={"stack": {"id": "demo", "position": 1, "total": 2, "base_branch": "main"}})


def test_existing_pr_update_keeps_the_clean_merge_gate(monkeypatch, tmp_path):
  direct_submit(monkeypatch, tmp_path, direct_base_branch="main",
                expected_existing_pr_number=42, expected_existing_head_repository="octocat/app-demo",
                expected_existing_head_sha="c" * 40,
                plan={"action": "pr_update", "pr_metadata": {
                  "old_title": "Polish demo", "old_body": "## What\n\nPolishes the demo."}})


# -- the advisory review-status projection mirrors Send -----------------------------

def review_status(client, app_id, app_token):
  response = client.get(f"/api/github/contributions/{app_id}/review-status",
                        headers={"Authorization": f"Bearer {app_token}"})
  assert response.status_code == 200, response.text
  return response.json()["records"][0]


def test_review_status_keeps_confirm_available_for_a_standalone_overlap(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "overlap-ready")
  upstream = _conflicting_upstream_commit(repo, "export default 9\n")
  _record_upstream(app_id, "overlap-ready", upstream)
  verdict = review_status(client, app_id, app_token)
  assert verdict["state"] == "ready" and verdict["code"] == "ready"


def test_review_status_still_blocks_an_overlapping_existing_pr_update(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "overlap-update")
  _record_upstream(app_id, "overlap-update", _conflicting_upstream_commit(repo, "export default 9\n"))
  path = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions" / "overlap-update.json"
  record = json.loads(path.read_text())
  record["plan"]["action"] = "pr_update"
  path.write_text(json.dumps(record))
  verdict = review_status(client, app_id, app_token)
  assert verdict["state"] == "needs_refresh", verdict
  assert verdict["code"] == "upstream_conflict"
  assert verdict["message"] == github_routes._REVIEW_STATUS_MESSAGES["upstream_conflict"]


def test_review_status_blocks_a_clean_standalone_pr_upstream_already_took(client, owner_token):
  """Mirror Send on the clean path: upstream holding the topic merges cleanly
  but moves the merge base, so the reviewed diff is not what GitHub shows."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "already-landed")
  head = subprocess.check_output(["git", "rev-parse", "fix/demo-review"], cwd=repo, text=True).strip()
  _record_upstream(app_id, "already-landed", head)
  verdict = review_status(client, app_id, app_token)
  assert verdict["state"] == "needs_refresh" and verdict["code"] == "upstream_conflict", verdict


def test_review_status_blocks_an_overlap_whose_reviewed_base_left_upstream(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "overlap-rewritten")
  subprocess.run(["git", "checkout", "-q", "--orphan", "rewritten"], cwd=repo, check=True)
  (repo / "index.jsx").write_text("export default 7\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=repo, check=True)
  subprocess.run(["git", "commit", "-qm", "rewritten upstream"], cwd=repo, check=True)
  rewritten = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
  subprocess.run(["git", "checkout", "-q", "fix/demo-review"], cwd=repo, check=True)
  _record_upstream(app_id, "overlap-rewritten", rewritten)
  verdict = review_status(client, app_id, app_token)
  # An unrelated rewritten history cannot even be merged locally; either way
  # Send never becomes available for a diff GitHub would show differently.
  assert verdict["state"] != "ready", verdict
  assert verdict["code"] in {"upstream_conflict", "review_unavailable"}


def test_an_existing_pr_update_keeps_the_gate_even_if_its_action_witness_regressed(monkeypatch, tmp_path):
  """Defense in depth at the owning seam: the conflict exemption is keyed on
  "no existing PR", not only on the plan's action. Even if the separate
  pr_update text-witness invariant were relaxed, an update of an existing PR
  with no explicit base still refuses to publish over an upstream conflict."""
  monkeypatch.setattr(github_contributions, "_reviewed_existing_pr_metadata",
                      lambda record: ("Polish demo", "## What\n\nPolishes the demo."))
  direct_submit(monkeypatch, tmp_path, expected_existing_pr_number=42,
                expected_existing_head_repository="octocat/app-demo",
                expected_existing_head_sha="c" * 40)


@pytest.mark.parametrize(("answer", "code"), [
  ("moved", "upstream_conflict"), ("several", "upstream_conflict"),
  ("unreadable", "upstream_merge_unavailable"),
])
def test_review_status_requires_the_reviewed_base_as_the_sole_merge_base(client, owner_token, monkeypatch, answer, code):
  """The advisory projection mirrors Send's exact merge-base proof.

  ``several`` keeps the reviewed base but adds a second (criss-cross) merge
  base: GitHub picks one, so the reviewed diff is not proven either.
  """
  from app import app_git
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, f"overlap-base-{answer}")
  _record_upstream(app_id, f"overlap-base-{answer}", _conflicting_upstream_commit(repo, "export default 9\n"))
  real_run = app_git._run
  def run_git(path, *args, **kwargs):
    if args[:2] == ("merge-base", "--all") and answer == "several":
      sole = real_run(path, *args, **kwargs)
      assert sole.returncode == 0 and len(sole.stdout.split()) == 1
      return subprocess.CompletedProcess(args, 0, sole.stdout + "c" * 40 + "\n", "")
    if args[:2] == ("merge-base", "--all"):
      return subprocess.CompletedProcess(args, 0, "c" * 40 + "\n", "") if answer == "moved" else (
        subprocess.CompletedProcess(args, 128, "", "fatal: bad object"))
    return real_run(path, *args, **kwargs)
  monkeypatch.setattr(app_git, "_run", run_git)
  verdict = review_status(client, app_id, app_token)
  assert verdict["state"] != "ready" and verdict["code"] == code
