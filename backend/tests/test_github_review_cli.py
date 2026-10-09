"""Core GitHub client freezes consent without an optional-app dependency."""
import contextlib
import importlib.util
import io
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("github_review_cli", Path(__file__).parents[1] / "scripts/github_review.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
ITEM = {"repo": "example/project", "number": 7, "head_sha": "a" * 40,
        "base_ref": "main", "base_sha": "b" * 40, "title": "Change", "url": "https://github.com/example/project/pull/7"}
SNAPSHOT = {"review_prompt": "Review full diff", "fix_prompt": "Fix findings", "merge_prompt": "Guarded merge",
            "max_rounds": 3, "autopilot": True, "provider": "codex", "model": "test-model", "reasoning_effort": "high"}
PREVIEW = {"options": SNAPSHOT, "preview_sha256": hashlib.sha256(json.dumps(SNAPSHOT, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}


def run(args, call):
  output = io.StringIO()
  with patch.dict(cli.os.environ, {"CHAT_ID": "owner-chat"}), patch.object(cli, "api", side_effect=call), contextlib.redirect_stdout(output):
    cli.main(args)
  return json.loads(output.getvalue())


def selection():
  with patch.object(cli, "inspect_pr", return_value=ITEM), patch.object(cli, "api", return_value=PREVIEW):
    return cli.freeze_preview(cli.prepare_selection(["example/project#7"], "review_fix_merge", "owner-chat"))


def test_preview_is_private_frozen_and_uses_no_app_api(tmp_path):
  calls = []
  def api(path, **kwargs):
    calls.append((path, kwargs))
    return PREVIEW
  with patch.object(cli, "inspect_pr", return_value=ITEM):
    result = run(["preview", "example/project#7", "--mode", "review_fix_merge", "--output", str(tmp_path / "preview.json")], api)
  assert [path for path, _ in calls] == ["/api/github/review-preview"]
  assert not result["approved"]
  saved = cli.read_object(result["preview_file"])
  assert saved["resolved_snapshot"] == SNAPSHOT
  assert saved["confirmation_scope"] == cli.TAKEOVER_SCOPE
  assert (tmp_path / "preview.json").stat().st_mode & 0o777 == 0o600


def test_start_reuses_frozen_heads_model_and_options_without_refresh(tmp_path):
  path = tmp_path / "preview.json"
  selected = selection()
  cli.save_preview(path, selected)
  calls = []
  with patch.object(cli, "inspect_pr", side_effect=AssertionError("Saved identity advanced")):
    result = run(["start", "--preview", str(path), "--approved-in-chat", "--approval-context", "Owner named these scoped repairs and reviewed successors"],
      lambda p, **kw: calls.append((p, kw)) or {"run": {"chat_id": "owner-chat"}})
  assert result["approved"]
  assert len(calls) == 1
  endpoint, kw = calls[0]
  assert endpoint == "/api/github/review-runs"
  assert kw["data"] == {**cli.start_body(selected), "chat_approval": {"context": "Owner named these scoped repairs and reviewed successors"}}


def test_other_chat_cannot_borrow_preview_consent(tmp_path):
  path = tmp_path / "preview.json"
  cli.save_preview(path, {**selection(), "source_chat_id": "different-owner-chat"})
  with pytest.raises(ValueError, match="another source chat"):
    run(["start", "--preview", str(path), "--approved-in-chat", "--approval-context", "yes"],
        lambda *a, **kw: pytest.fail("Approval I/O must not happen"))


@pytest.mark.parametrize("args", [[], ["--approved-in-chat", "--approval-context", " "]])
def test_start_requires_explicit_consent_before_reading_preview_or_api(args):
  with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
    run(["start", "--preview", "nonexistent.json", *args], lambda *a, **kw: pytest.fail("No API before approval"))


def test_preview_write_cannot_replace_existing_consent(tmp_path):
  path = tmp_path / "preview.json"
  selected = selection()
  cli.save_preview(path, selected)
  with pytest.raises(FileExistsError):
    cli.save_preview(path, {**selected, "mode": "review"})
  assert cli.read_object(path) == selected


def test_changed_preview_is_blocked_not_silently_repreviewed_or_retried(tmp_path):
  from urllib.error import HTTPError
  path = tmp_path / "preview.json"
  cli.save_preview(path, selection())
  calls = []
  def api(p, **kw):
    calls.append((p, kw))
    raise HTTPError(p, 409, "Conflict", {}, io.BytesIO(json.dumps({"detail": "The resolved prompts or model changed"}).encode()))
  with pytest.raises(ValueError, match="prompts or model changed"):
    run(["start", "--preview", str(path), "--approved-in-chat", "--approval-context", "yes"], api)
  assert len(calls) == 1


def test_already_owned_returns_existing_conversation_without_starting_again(tmp_path):
  from urllib.error import HTTPError
  path = tmp_path / "preview.json"
  cli.save_preview(path, selection())
  calls = []
  def api(p, **kw):
    calls.append((p, kw))
    raise HTTPError(p, 409, "Conflict", {}, io.BytesIO(json.dumps({"detail": {"chat_id": "existing-chat", "message": "Already owned"}}).encode()))
  result = run(["start", "--preview", str(path), "--approved-in-chat", "--approval-context", "yes"], api)
  assert result["already_owned"] and not result["approved"]
  assert result["review_url"] == "/shell/?chat=existing-chat"
  assert len(calls) == 1


def test_observation_only_uses_read_only_reconciliation_route():
  calls = []
  run(["observe", "review-run-123"], lambda p, **kw: calls.append((p, kw)) or {"run": {"state": "queued"}})
  assert calls == [("/api/github/review-runs/review-run-123/observe", {"method": "POST", "data": {}})]


@pytest.mark.parametrize("field,value", [("mode", "review_merge"), ("source_chat_id", "owner-chat")])
def test_edited_saved_identity_requires_new_preview(tmp_path, field, value):
  path = tmp_path / "preview.json"
  selected = selection()
  selected[field] = value
  selected["items"] = [{**ITEM, "head_sha": "c" * 40}]
  cli.save_preview(path, selected)
  with pytest.raises(ValueError, match="identity changed"):
    run(["start", "--preview", str(path), "--approved-in-chat", "--approval-context", "yes"],
        lambda *a, **kw: pytest.fail("No start for edited saved identity"))


def test_edited_resolved_snapshot_cannot_misrepresent_frozen_start_options(tmp_path):
  path = tmp_path / "preview.json"
  selected = selection()
  selected["resolved_snapshot"] = {**selected["resolved_snapshot"], "review_prompt": "different instructions"}
  cli.save_preview(path, selected)
  with pytest.raises(ValueError, match="frozen preview changed"):
    cli.load_preview(path, "owner-chat")


def test_draft_ready_permission_is_explicit_frozen_and_never_added_to_legacy_selection():
  with patch.object(cli, "inspect_pr", return_value={**ITEM, "is_draft": True}), patch.object(cli, "api", return_value=PREVIEW):
    draft = cli.prepare_selection(["example/project#7"], "review_fix_merge", "owner-chat")
    with pytest.raises(ValueError, match="allow-mark-ready"):
      cli.freeze_preview(draft)
    new = cli.freeze_preview(draft, allow_mark_ready=True)
    assert new["confirmation_scope"] == cli.DRAFT_TAKEOVER_SCOPE
    assert cli.start_body(new)["confirmation_scope"] == cli.DRAFT_TAKEOVER_SCOPE
    assert "is_draft" not in cli.start_body(new)["items"][0]
    with pytest.raises(ValueError, match="identity changed"):
      cli.verify_selection({**new, "confirmation_scope": cli.TAKEOVER_SCOPE})
  assert selection()["confirmation_scope"] == cli.TAKEOVER_SCOPE


POSTING = {**SNAPSHOT, "post_review": True}
POSTING_PREVIEW = {"options": POSTING, "preview_sha256": hashlib.sha256(json.dumps(POSTING, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}


def test_review_preview_can_freeze_posting_the_verdict_on_github(tmp_path):
  calls = []
  def api(path, **kwargs):
    calls.append((path, kwargs))
    return POSTING_PREVIEW
  with patch.object(cli, "inspect_pr", return_value=ITEM):
    result = run(["preview", "example/project#7", "--post-review", "--output", str(tmp_path / "preview.json")], api)
  assert calls == [("/api/github/review-preview", {"method": "POST", "data": {"options": {"post_review": True}, "agent": None}})]
  saved = cli.load_preview(result["preview_file"], "owner-chat")
  assert saved["mode"] == "review" and saved["options"]["post_review"] is True
  assert cli.start_body(saved)["options"]["post_review"] is True


def test_post_review_flag_merges_with_edited_options(tmp_path):
  options = tmp_path / "options.json"
  options.write_text(json.dumps({"review_prompt": "Review full diff"}))
  calls = []
  with patch.object(cli, "inspect_pr", return_value=ITEM):
    run(["preview", "example/project#7", "--post-review", "--options", str(options), "--output", str(tmp_path / "preview.json")],
        lambda path, **kw: calls.append(kw["data"]["options"]) or POSTING_PREVIEW)
  assert calls == [{"review_prompt": "Review full diff", "post_review": True}]


@pytest.mark.parametrize("mode", ["review_merge", "review_fix_merge"])
def test_post_review_is_review_mode_only(tmp_path, mode):
  with patch.object(cli, "inspect_pr", return_value=ITEM), pytest.raises(ValueError, match="review mode only"):
    run(["preview", "example/project#7", "--mode", mode, "--post-review", "--output", str(tmp_path / "preview.json")],
        lambda *a, **kw: pytest.fail("No preview for an unsupported posting choice"))


def test_post_review_requires_the_platform_to_freeze_it(tmp_path):
  with patch.object(cli, "inspect_pr", return_value=ITEM), pytest.raises(ValueError, match="posting choice"):
    run(["preview", "example/project#7", "--post-review", "--output", str(tmp_path / "preview.json")],
        lambda *a, **kw: PREVIEW)


def test_review_preview_without_flag_freezes_no_posting_choice():
  with patch.object(cli, "inspect_pr", return_value=ITEM), patch.object(cli, "api", return_value=PREVIEW):
    selected = cli.freeze_preview(cli.prepare_selection(["example/project#7"], "review", "owner-chat"))
  assert "post_review" not in selected["options"]


@pytest.mark.parametrize("edit", ["drop", "add"])
def test_edited_posting_choice_requires_new_preview(tmp_path, edit):
  with patch.object(cli, "inspect_pr", return_value=ITEM), patch.object(cli, "api", return_value=POSTING_PREVIEW if edit == "drop" else PREVIEW):
    selected = cli.freeze_preview(cli.prepare_selection(["example/project#7"], "review", "owner-chat"),
                                  post_review=edit == "drop")
  if edit == "drop":
    selected["options"].pop("post_review")
  else:
    selected["options"]["post_review"] = True
  path = tmp_path / "preview.json"
  cli.save_preview(path, selected)
  with pytest.raises(ValueError, match="frozen preview changed"):
    cli.load_preview(path, "owner-chat")
