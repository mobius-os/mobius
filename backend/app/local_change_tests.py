"""Run the tests that own an installation's local backend edits across an update.

A platform update carries the owner's local edits onto a new release. Text-clean
merges and the import probe still miss semantic breaks: a local edit can keep
importing while calling something the release removed, and fail only when a
chat turn runs it. When local edits carry their own tests, those tests are the
cheapest evidence that the edits still work on the new release.

``select`` is a best-effort filename convention, not proof of full coverage.
Runs must use frozen isolated checkouts and the same selected files. Only an
observed pass before and failure after is a regression; missing, skipped or
incomplete runs are retained as incomplete evidence, never an all-clear.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

RUNNER = "scripts/wt-pytest.sh"
TIMEOUT_SECONDS = 600
_SCRUBBED_ENV = (
  "PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  "DATABASE_URL", "DATA_DIR", "SECRET_KEY", "BASH_ENV", "ENV", "PYTEST_ADDOPTS",
)


@dataclass(frozen=True)
class LocalTestRun:
  """Executed outcomes from one run, or why the run produced no evidence."""

  passed: frozenset[str] = frozenset()
  failed: frozenset[str] = frozenset()
  skipped: frozenset[str] = frozenset()
  unavailable: str | None = None


def _changed_backend_paths(repo: Path, release: str, candidate: str) -> list[str]:
  proc = subprocess.run(
    ["git", "-C", str(repo), "diff", "--name-only", "--no-renames",
     "--diff-filter=d", release, candidate, "--", "backend/"],
    capture_output=True, text=True, check=True,
  )
  return [line for line in proc.stdout.splitlines() if line.endswith(".py")]


def _in_tree(repo: Path, rev: str, path: str) -> bool:
  return subprocess.run(
    ["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"],
    capture_output=True, check=False,
  ).returncode == 0


def select(repo: Path, release: str, candidate: str) -> list[str]:
  """Backend-relative test files owned by the local delta ``release..candidate``."""
  owned: set[str] = set()
  for path in _changed_backend_paths(repo, release, candidate):
    name = Path(path).name
    if path.startswith("backend/tests/") and name.startswith("test_"):
      owned.add(path)
    elif path.startswith("backend/app/") and name != "__init__.py":
      sibling = f"backend/tests/test_{Path(path).stem}.py"
      if _in_tree(repo, candidate, sibling):
        owned.add(sibling)
  return sorted(path.removeprefix("backend/") for path in owned)


def _read_report(report: Path, exit_code: int) -> LocalTestRun:
  # Pytest's collection/internal/usage exits are not executed test failures.
  if exit_code not in (0, 1):
    return LocalTestRun(unavailable=f"local tests did not complete (exit {exit_code})")
  root = ET.parse(report).getroot()
  cases = list(root.iter("testcase"))
  suites = list(root.iter("testsuite"))
  try:
    accounted = bool(suites) and all(
      sum(int(suite.get(attribute, "-1")) for suite in suites) == count
      for attribute, count in (
        ("tests", len(cases)),
        ("failures", sum(case.find("failure") is not None for case in cases)),
        ("errors", sum(case.find("error") is not None for case in cases)),
        ("skipped", sum(case.find("skipped") is not None for case in cases)),
      )
    )
  except ValueError:
    accounted = False
  if not accounted:
    return LocalTestRun(unavailable="local test report totals do not account for its test cases")
  passed, failed, skipped = set(), set(), set()
  for case in cases:
    ident = f"{case.get('classname', '')}::{case.get('name', '')}"
    if not case.get("classname") or not case.get("name") or ident in passed | failed | skipped:
      return LocalTestRun(unavailable="local test report has missing or duplicate test identities")
    if case.find("error") is not None:
      return LocalTestRun(unavailable="local tests had collection, setup or teardown errors")
    if case.find("failure") is not None:
      failed.add(ident)
    elif case.find("skipped") is not None:
      skipped.add(ident)
    else:
      passed.add(ident)
  if not passed | failed | skipped or bool(failed) != (exit_code == 1):
    return LocalTestRun(unavailable="local test report does not account for the runner exit status")
  return LocalTestRun(frozenset(passed), frozenset(failed), frozenset(skipped))


def run(repo: Path, tests: list[str], *, timeout: int = TIMEOUT_SECONDS) -> LocalTestRun:
  """Execute exactly the selection; never silently drop missing test files."""
  if not tests:
    return LocalTestRun(unavailable="no local tests selected by filename convention")
  missing = [test for test in tests if not (repo / "backend" / test).is_file()]
  if missing:
    return LocalTestRun(unavailable="selected test files are missing: " + ", ".join(missing))
  if not (repo / RUNNER).is_file():
    return LocalTestRun(unavailable=f"{RUNNER} is missing")
  env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
  with tempfile.TemporaryDirectory(prefix="mobius-local-tests-") as tmp:
    report = Path(tmp) / "report.xml"
    # Contain the runner's runtime directories too: SIGKILL cannot run its trap.
    env["TMPDIR"] = tmp
    try:
      proc = subprocess.Popen(
        ["bash", RUNNER, *tests, "-q", f"--junitxml={report}"],
        cwd=str(repo), env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
      )
    except OSError:
      return LocalTestRun(unavailable="local test runner could not start")
    try:
      proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
      return LocalTestRun(unavailable=f"local tests timed out after {timeout}s")
    finally:
      # Also reap descendants left behind by a runner that exited early.
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      proc.wait()
    if not report.is_file():
      # Runner output may contain private local code or environment values.
      return LocalTestRun(unavailable=f"local tests produced no report (exit {proc.returncode})")
    try:
      return _read_report(report, proc.returncode)
    except (ET.ParseError, OSError):
      return LocalTestRun(unavailable="local test report was unreadable")


def compare(tests: list[str], before: LocalTestRun, after: LocalTestRun) -> dict:
  """Comparable proof and its limits, suitable for retained prepare diagnostics."""
  incomplete = []
  for label, result in (("baseline", before), ("candidate", after)):
    if result.unavailable:
      incomplete.append(f"{label}: {result.unavailable}")
  if not incomplete:
    before_ids = before.passed | before.failed | before.skipped
    after_ids = after.passed | after.failed | after.skipped
    if before_ids != after_ids:
      incomplete.append("test identities differ between baseline and candidate")
    if before.skipped or after.skipped:
      incomplete.append("selected tests were skipped")
  broken = newly_failing(before, after)
  return {
    "status": "regression" if broken else "incomplete" if incomplete else "compared",
    "selected": tests,
    "regressions": broken or [],
    "incomplete": incomplete,
    "baseline": {"passed": sorted(before.passed), "failed": sorted(before.failed),
                 "skipped": sorted(before.skipped)},
    "candidate": {"passed": sorted(after.passed), "failed": sorted(after.failed),
                  "skipped": sorted(after.skipped)},
  }


def newly_failing(before: LocalTestRun, after: LocalTestRun) -> list[str] | None:
  """Only passing-before/failing-after proves a regression."""
  if before.unavailable or after.unavailable:
    return None
  return sorted(after.failed & before.passed)


def describe(broken: list[str]) -> str:
  shown = ", ".join(broken[:5])
  more = f" (+{len(broken) - 5} more)" if len(broken) > 5 else ""
  return (
    "Your local changes fail their own tests on this release: "
    f"{shown}{more}. The previous version keeps running; repair the local "
    "changes for this release, then update again."
  )
