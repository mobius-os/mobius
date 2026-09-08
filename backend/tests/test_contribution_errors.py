"""Submit failures must read as one sentence, never as a terminal transcript."""

import re
from pathlib import Path

from app.contribution_errors import ContributionSubmitError, push_rejected
from app.terminal_output import readable_output

# The exact bytes a rejected Send stored on a real contribution record: the
# local gate's colour codes, npm's lifecycle banner, and the one line that
# actually explains the refusal.
REAL_PRE_PUSH_REJECTION = (
  "\x1b[1;31m[pre-push]\x1b[0m frontend-unit FAILED:\r\n"
  "    \r\n"
  "    > moebius@0.1.0 test\r\n"
  "    > npm run test:lib && npm run test:hooks\r\n"
  "    \r\n"
  "    \r\n"
  "    > moebius@0.1.0 pretest:lib\r\n"
  "    > npm run runtime:check && npm run test:structure\r\n"
  "    \r\n"
  "    Structural-test debt grew:\r\n"
  "      - 781 source-reading cases exceeds 780\r\n"
  "    Replace implementation-text assertions with behavioral coverage.\r\n"
  "\x1b[1;31m[pre-push]\x1b[0m push blocked. Fix the above.\r\n"
  "[pre-push] verdict=blocked cause=checks checks=frontend-unit\r\n"
)


def test_terminal_colour_never_survives_into_stored_text():
  cleaned = readable_output(REAL_PRE_PUSH_REJECTION)

  assert "\x1b" not in cleaned
  assert "[1;31m" not in cleaned
  assert "[pre-push] frontend-unit FAILED:" in cleaned
  assert "781 source-reading cases exceeds 780" in cleaned


def test_blank_runs_collapse_and_line_endings_normalize():
  cleaned = readable_output("first\r\n\r\n\r\n\r\nsecond   \r\n\r\n")

  assert cleaned == "first\n\nsecond"


def test_overflow_keeps_the_end_where_the_reason_is():
  raw = "\n".join(["banner line"] * 400 + ["the actual reason"])

  cleaned = readable_output(raw, limit=200)

  assert cleaned.startswith("…\n")
  assert cleaned.endswith("the actual reason")
  assert len(cleaned) <= 210


def test_stray_control_bytes_are_removed_but_text_is_kept():
  cleaned = readable_output("before\x00\x07after\ttabbed")

  assert cleaned == "beforeafter\ttabbed"


def test_local_gate_rejection_names_the_check_and_hides_the_transcript():
  error = push_rejected(REAL_PRE_PUSH_REJECTION)

  assert isinstance(error, ContributionSubmitError)
  assert error.message == (
    "This did not pass the checks Möbius runs before publishing "
    "(frontend-unit). Nothing was published — fix it locally, then send again."
  )
  assert "\x1b" not in error.message
  assert "npm run" not in error.message
  # The evidence is still reachable, just not the headline.
  assert "781 source-reading cases exceeds 780" in error.detail


def test_every_failing_check_the_gate_named_reaches_the_owner():
  error = push_rejected(
    "[pre-push] frontend-unit FAILED:\n"
    "    boom\n"
    "[pre-push] backend pytest FAILED:\n"
    "    boom\n"
    "[pre-push] push blocked. Fix the above.\n"
    "[pre-push] verdict=blocked cause=checks checks=frontend-unit,backend-pytest\n"
  )

  assert "(frontend-unit, backend-pytest)" in error.message


def test_privacy_rejection_gets_its_own_sentence():
  error = push_rejected(
    "[pre-push] private workspace path staged: .claude/settings.json\n"
    "[pre-push] push blocked by the privacy gate. Do not use --no-verify.\n"
    "[pre-push] verdict=blocked cause=privacy checks=\n"
  )

  assert "privacy gate" in error.message
  assert "Nothing was published." in error.message


def test_an_unrecognized_refusal_is_attributed_to_github_not_to_us():
  error = push_rejected(
    "remote: Permission to mobius-os/mobius.git denied.\n"
    "fatal: unable to access 'https://github.com/...': 403\n"
  )

  assert error.message == (
    "GitHub would not accept this branch, so nothing was published."
  )
  assert "403" in error.detail


def test_moved_remote_ref_routes_supported_git_rejections_to_a_fresh_review():
  for reason in ("non-fast-forward", "fetch first", "stale info"):
    error = push_rejected(
      f" ! [rejected] HEAD -> feat/existing-review ({reason})\n"
      "error: failed to push some refs\n"
    )

    assert error.code == "review_refresh_needed"
    assert error.message == (
      "GitHub's copy of this branch changed after it was reviewed. Nothing was "
      "pushed. Ask the agent to refresh and review it against the current branch."
    )
    assert reason in error.detail


def test_check_output_that_mentions_fetch_first_remains_a_check_failure():
  error = push_rejected(
    "[pre-push] frontend-unit FAILED:\n"
    "    ! [rejected] HEAD -> test-fixture (fetch first)\n"
    "[pre-push] verdict=blocked cause=checks checks=frontend-unit\n"
  )

  assert error.code is None
  assert "(frontend-unit)" in error.message


def test_gate_output_without_a_verdict_is_not_claimed_as_a_local_failure():
  # An older installed hook, or a push refused before the gate ran, has no
  # verdict line. Reporting it as a local check failure would send the owner
  # looking for a test to fix that never ran.
  error = push_rejected("[pre-push] some older wording\nerror: failed to push\n")

  assert error.message == (
    "GitHub would not accept this branch, so nothing was published."
  )


def test_the_record_patch_survives_classification():
  error = push_rejected("boom", record_patch={"head_repository": "owner/fork"})

  assert error.record_patch == {"head_repository": "owner/fork"}


def test_the_gate_and_this_parser_agree_on_the_verdict_line():
  """The one place the hook and this module have to stay in step.

  Everything else the gate prints is prose for a human and may be reworded
  freely. If someone changes the verdict line's shape in the hook without
  changing the pattern here, classification silently degrades to the generic
  remote-refusal message — so assert the two really do match, rather than
  testing this parser against a hand-copied fixture that can drift.
  """
  hook = (Path(__file__).resolve().parents[2] / "scripts/githooks/pre-push").read_text()

  emitted = re.search(
    r"printf '(\[pre-push\] verdict=blocked cause=%s checks=%s)\\n'", hook,
  )
  assert emitted, "the pre-push gate no longer prints the verdict line this parses"

  template = emitted.group(1)
  privacy = template % ("privacy", "")
  checks = template % ("checks", "frontend-unit,backend-pytest")

  assert "privacy gate" in push_rejected(privacy).message
  assert "(frontend-unit, backend-pytest)" in push_rejected(checks).message


def test_every_named_check_in_the_gate_is_shell_safe_and_readable():
  """Check names travel through a shell variable into an owner-facing sentence."""
  hook = (Path(__file__).resolve().parents[2] / "scripts/githooks/pre-push").read_text()

  names = re.findall(r"^\s*fail_check ([\w-]+)$", hook, re.MULTILINE)

  assert len(names) >= 5, f"expected the gate's named checks, found {names}"
  for name in names:
    assert re.fullmatch(r"[a-z0-9-]+", name), f"{name!r} is not a safe check name"
