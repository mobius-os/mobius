"""The shared case files the platform's PR machine, same-work reader and record readers answer to are pinned by hash.

Contribute carries byte-identical copies (test/pr-machine.cases.json, test/pr-machine.wording.json,
test/same-work.cases.json, test/lifecycle-record.cases.json, test/lifecycle-unreadable.cases.json) and its own
evaluators of them. The wording file is what the app's machine says for each shared case; the app's suite
regenerates it from pr-machine.js, so a changed word fails there until the file, then this copy and the lock,
follow. Two repositories cannot share one CI run, so each side pins the files here. Changing a rule means
changing the case file in both repositories, then this lock, together.

The copies are compared byte for byte only when CONTRIBUTE_APP_TEST_DIR names an app checkout's test folder.
There is no default: an installed app is another version's files, and a test that reads it goes red whenever
the installed app and this checkout are out of step, which is no fault of either.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LOCK = json.loads((FIXTURES / "contribution_machine_cases_lock.json").read_text("utf-8"))
APP_TEST_DIR = os.environ.get("CONTRIBUTE_APP_TEST_DIR")
# (lock key, this repository's file, the app's file)
PAIRS = [("pr_machine_cases_sha256", "contribution_pr_machine_cases.json", "pr-machine.cases.json"),
         ("pr_machine_wording_sha256", "contribution_pr_machine_wording.json", "pr-machine.wording.json"),
         ("same_work_cases_sha256", "contribution_same_work_cases.json", "same-work.cases.json"),
         ("lifecycle_record_cases_sha256", "contribution_lifecycle_record_cases.json", "lifecycle-record.cases.json"),
         ("lifecycle_unreadable_cases_sha256", "contribution_lifecycle_unreadable_cases.json", "lifecycle-unreadable.cases.json")]


def _sha256(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("key, ours, theirs", PAIRS, ids=[pair[1] for pair in PAIRS])
def test_the_lock_pins_each_shared_case_file(key, ours, theirs):
  assert _sha256((FIXTURES / ours).read_bytes()) == LOCK[key]


@pytest.mark.parametrize("key, ours, theirs", PAIRS, ids=[pair[1] for pair in PAIRS])
def test_the_contribute_app_copy_is_byte_identical_when_a_checkout_is_named(key, ours, theirs):
  if not APP_TEST_DIR:
    pytest.skip("CONTRIBUTE_APP_TEST_DIR is not set: no app checkout to compare with")
  sibling = Path(APP_TEST_DIR) / theirs
  assert sibling.exists(), f"CONTRIBUTE_APP_TEST_DIR is set but {sibling} is not there"
  assert sibling.read_bytes() == (FIXTURES / ours).read_bytes(), (
    f"{sibling} differs from {ours}: change the shared cases in both repositories together, then the lock")


def test_the_lock_names_only_the_files_it_pins():
  assert set(LOCK) == {key for key, _ours, _theirs in PAIRS}
