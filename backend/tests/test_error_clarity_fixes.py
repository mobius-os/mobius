"""Opaque failures from the Reflection backlog now say what actually happened.

Each test pins one previously misleading outcome: a signal-killed compile that
read as broken JSX, a correct PR body rejected by a bytes-for-characters limit,
a shallow checkout whose merge preview failed as generically "unavailable", and
a finish preview that hid the prepared release's own image requirement.
"""

import subprocess
from pathlib import Path

import pytest

from app import app_git, platform_update
from app.compiler import _compile_failure_detail
from app.github_contributions import ContributionSubmitError, _exact_reviewed_pr_text
from app.platform_activation import PlatformActivationImpact


def test_signal_killed_compile_names_the_signal_and_likely_memory_cause():
  detail = _compile_failure_detail(-9, b"")
  assert "SIGKILL" in detail
  assert "memory" in detail


def test_signal_kill_with_real_stderr_keeps_rolldowns_own_error():
  assert _compile_failure_detail(-9, b"real diagnostics") == "real diagnostics"


def test_ordinary_compile_failure_passes_stderr_through_unchanged():
  assert _compile_failure_detail(1, b"Unexpected token") == "Unexpected token"


def test_non_kill_signal_is_named_without_the_memory_claim():
  detail = _compile_failure_detail(-11, b"")
  assert "SIGSEGV" in detail
  assert "memory" not in detail


def _pr_record(body: str) -> dict:
  return {"plan": {"title": "A title", "body_draft": body}}


def test_pr_body_limit_counts_characters_not_utf8_bytes():
  # Dependabot truncates to exactly 65,536 CHARACTERS; a non-ASCII character
  # must not push a publishable body over a byte-counted limit.
  body = "é" * 65_536
  assert len(body.encode("utf-8")) > 65_536
  title, returned = _exact_reviewed_pr_text(_pr_record(body))
  assert returned == body


def test_pr_body_over_githubs_character_limit_is_still_rejected():
  with pytest.raises(ContributionSubmitError):
    _exact_reviewed_pr_text(_pr_record("x" * 65_537))


def _git(repo: Path, *args: str) -> None:
  subprocess.run(
    ["git", "-C", str(repo), *args], check=True, capture_output=True,
  )


def test_merge_preview_survives_a_shallow_checkout(tmp_path):
  # A shallow clone grafts away parents; the preview's proof repo must carry
  # the shallow boundary or merge-tree fails reading a missing parent and the
  # verdict collapses into a generic "review unavailable".
  origin = tmp_path / "origin"
  origin.mkdir()
  _git(origin, "init", "-q", "-b", "main")
  _git(origin, "config", "user.email", "t@example.com")
  _git(origin, "config", "user.name", "t")
  for n in (1, 2, 3):
    (origin / "file.txt").write_text(f"line {n}\n")
    _git(origin, "add", "file.txt")
    _git(origin, "commit", "-q", "-m", f"c{n}")
  clone = tmp_path / "shallow"
  subprocess.run(
    ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)],
    check=True, capture_output=True,
  )
  assert (clone / ".git" / "shallow").is_file()
  (clone / "file.txt").write_text("line 3\nlocal\n")
  _git(clone, "config", "user.email", "t@example.com")
  _git(clone, "config", "user.name", "t")
  _git(clone, "add", "file.txt")
  _git(clone, "commit", "-q", "-m", "local edit")
  result = app_git.preview_merge_refs(clone, "HEAD", "HEAD~1")
  assert result is not None
  assert result.status in ("clean", "conflict")


def _diff_only_restart_impact() -> PlatformActivationImpact:
  return PlatformActivationImpact(
    level="server_restart",
    required_actions=["server_restart"],
    deployment="railway",
    reasons=[],
    guidance=["Restart Möbius after Apply so the running server loads the new source."],
  )


def test_prepared_image_requirement_surfaces_in_finish_preview_activation():
  record = {"requires_image": True, "image_digest": "sha256:abc123"}
  impact = platform_update._with_prepared_image_requirement(
    _diff_only_restart_impact(), record,
  )
  assert impact["level"] == "image_rebuild"
  assert impact["required_actions"][-1] == "image_rebuild"
  assert any(
    reason["code"] == "prepared_image_requirement"
    and "sha256:abc123" in reason["summary"]
    for reason in impact["reasons"]
  )
  assert len(impact["guidance"]) == 2


def test_prepared_image_requirement_names_an_unresolved_digest():
  impact = platform_update._with_prepared_image_requirement(
    _diff_only_restart_impact(), {"requires_image": True, "image_digest": None},
  )
  assert any(
    reason["code"] == "prepared_image_requirement"
    and "not resolved" in reason["summary"]
    for reason in impact["reasons"]
  )


def test_diff_only_activation_is_untouched_without_an_image_requirement():
  impact = _diff_only_restart_impact()
  assert platform_update._with_prepared_image_requirement(
    impact, {"requires_image": False},
  ) is impact


def test_activation_already_naming_image_rebuild_is_not_duplicated():
  impact = PlatformActivationImpact(
    level="image_rebuild",
    required_actions=["server_restart", "image_rebuild"],
    deployment="railway",
    reasons=[],
    guidance=[],
  )
  assert platform_update._with_prepared_image_requirement(
    impact, {"requires_image": True, "image_digest": "sha256:x"},
  ) is impact
