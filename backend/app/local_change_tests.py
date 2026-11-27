"""Run the tests that cover an installation's local edits across an update.

A platform update carries the owner's local edits onto a new release. Text-clean
merges, the import probe and the frontend build still miss semantic breaks: a
local edit can keep importing and bundling while calling something the release
moved or reshaped, and fail only when a chat turn or a render runs it. The
tests that exercise the code those edits touch are the cheapest evidence that
the edits still work on the new release.

``select`` is best-effort coverage, not proof. Backend tests follow a filename
convention: a changed test file, or ``tests/test_x.py`` for a changed
``app/x.py``. Frontend tests are chosen when they refer to a changed source file
directly or through other source files. Only test files present on both sides
can be compared. A whole backend family is deliberately not selected: on a
large local delta it costs tens of minutes per side, so ``removed_references``
covers the commonest semantic break statically instead. For the same reason
frontend tests are taken nearest-first and capped: an edit to a module nearly
everything imports would otherwise select the whole suite.

The comparison has one time budget (``TIMEOUT_SECONDS``) for both sides
together. Each suite gets a fair share of what is left, so one slow suite
cannot starve the others, and the candidate reruns only the suites that passed
something on the baseline: without an earlier pass there is no regression to
observe.

Runs must use frozen isolated checkouts and the same selected files. Only an
observed pass before and failure after is a regression; missing, skipped,
limited or incomplete runs are retained as incomplete evidence, never an
all-clear.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import posixpath
import re
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from app import app_git

RUNNER = "scripts/wt-pytest.sh"
# Borrows the served checkout's exact-lock frontend dependencies, so a release
# that changes them yields no frontend evidence rather than a false verdict.
FRONTEND_RUNNER = "scripts/wt-npm.sh"
# The npm scripts that define which files each frontend suite runs and with
# which module loader. Reading them keeps package.json the one definition.
FRONTEND_SUITES = ("test:lib", "test:hooks")
# The whole baseline-plus-candidate comparison, not each side.
TIMEOUT_SECONDS = 600
# Frontend test files run per comparison, nearest to the changed sources first.
FRONTEND_TEST_LIMIT = 40
_SCRUBBED_ENV = (
  "PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  "DATABASE_URL", "DATA_DIR", "SECRET_KEY", "BASH_ENV", "ENV", "PYTEST_ADDOPTS",
  "NODE_OPTIONS",
)
_FRONTEND_SOURCE_SUFFIXES = (".js", ".jsx", ".mjs")
_FRONTEND_TEST_SUFFIXES = (".test.js", ".hooktest.js")
# Any quoted relative or ``/src/`` path: imports, dynamic imports, Vite
# ``ssrLoadModule`` paths and source files read as text all count as a
# reference. Over-selecting costs test time; missing a reference costs evidence.
_PATH_LITERAL = re.compile(r"""['"`]((?:\.{1,2}/|/src/)[^'"`\s]+)['"`]""")


# The run marker (``process_groups.RUN_MARKER_ENV``) every runner started in
# this context carries. Runners lead their own sessions, so a dead owner never
# takes them down; the marker lets the operation that proves its owner gone
# end exactly them (``process_groups.terminate_run_processes``). Unset, a
# runner keeps the marker it inherited (an agent turn's, ending with its turn).
_PROCESS_MARKER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
  "local_change_tests_process_marker", default=None,
)


@contextlib.contextmanager
def process_marker(marker: str):
  """Mark every runner started inside this block with ``marker``."""
  token = _PROCESS_MARKER.set(marker)
  try:
    yield
  finally:
    _PROCESS_MARKER.reset(token)


@dataclass(frozen=True)
class LocalTestRun:
  """Executed outcomes from one run, or why the run produced no evidence."""

  passed: frozenset[str] = frozenset()
  failed: frozenset[str] = frozenset()
  skipped: frozenset[str] = frozenset()
  unavailable: str | None = None
  # Wall time the run took, for diagnosing slow checks; never compared.
  seconds: float = 0.0


@dataclass(frozen=True)
class Selection:
  """The test files to compare, and how many covering files the cap left out."""

  tests: list[str]
  omitted: int = 0


def _git_lines(repo: Path, *args: str) -> list[str]:
  proc = subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
  )
  return proc.stdout.splitlines()


def _changed_paths(repo: Path, release: str, candidate: str) -> list[str]:
  return _git_lines(
    repo, "diff", "--name-only", "--no-renames", "--diff-filter=d",
    release, candidate, "--", "backend/", "frontend/src/",
  )


def _tree(repo: Path, rev: str) -> set[str]:
  return set(_git_lines(repo, "ls-tree", "-r", "--name-only", rev, "--", "backend/", "frontend/src/"))


def _backend_tests(changed: list[str], tree: set[str]) -> set[str]:
  owned: set[str] = set()
  for path in changed:
    name = Path(path).name
    if not name.endswith(".py"):
      continue
    if path.startswith("backend/tests/") and name.startswith("test_"):
      owned.add(path)
    elif path.startswith("backend/app/") and name != "__init__.py":
      sibling = f"backend/tests/test_{Path(path).stem}.py"
      if sibling in tree:
        owned.add(sibling)
  return owned


def _resolve_frontend_reference(source: str, reference: str, files: set[str]) -> str | None:
  reference = reference.split("?", 1)[0].split("#", 1)[0]
  if reference.startswith("/src/"):
    base = "frontend" + reference
  else:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), reference))
  for path in (base, *(base + suffix for suffix in _FRONTEND_SOURCE_SUFFIXES),
               *(f"{base}/index{suffix}" for suffix in _FRONTEND_SOURCE_SUFFIXES)):
    if path in files:
      return path
  return None


def _frontend_tests(repo: Path, candidate: str, changed: list[str], tree: set[str]) -> list[str]:
  """Tests reaching a changed source, nearest first (fewest import hops)."""
  changed_sources = sorted(path for path in changed if path.startswith("frontend/src/"))
  if not changed_sources:
    return []
  referrers: dict[str, set[str]] = {}
  for source, data in app_git.read_ref_subtree(repo, candidate, "frontend/src").items():
    if not source.endswith(_FRONTEND_SOURCE_SUFFIXES):
      continue
    for reference in _PATH_LITERAL.findall(data.decode("utf-8", "replace")):
      target = _resolve_frontend_reference(source, reference, tree)
      if target and target != source:
        referrers.setdefault(target, set()).add(source)
  distance = {path: 0 for path in changed_sources}
  pending = deque(changed_sources)
  while pending:
    source = pending.popleft()
    for referrer in sorted(referrers.get(source, ())):
      if referrer not in distance:
        distance[referrer] = distance[source] + 1
        pending.append(referrer)
  return sorted(
    (path for path in distance if path.endswith(_FRONTEND_TEST_SUFFIXES)),
    key=lambda path: (distance[path], path),
  )


def select(repo: Path, release: str, candidate: str, baseline: str) -> Selection:
  """Repo-relative test files covering the local delta ``release..candidate``.

  Only files that also exist at ``baseline`` are kept: a test the release adds
  has no earlier pass to compare, and one missing file would leave the whole
  run without evidence. Frontend tests beyond ``FRONTEND_TEST_LIMIT`` (the
  farthest from the change) are counted, not run.
  """
  changed = _changed_paths(repo, release, candidate)
  if not changed:
    return Selection([])
  tree = _tree(repo, candidate)
  backend = _backend_tests(changed, tree)
  frontend = _frontend_tests(repo, candidate, changed, tree)
  comparable = _tree(repo, baseline) if backend or frontend else set()
  frontend = [path for path in frontend if path in comparable]
  kept = frontend[:FRONTEND_TEST_LIMIT]
  return Selection(sorted((backend & comparable) | set(kept)), len(frontend) - len(kept))


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


def _run_process(repo: Path, argv: list[str], runtime: Path, deadline: float) -> int | str:
  """Run one test command in its own process group; return its exit or why not.

  Its output is discarded: runner output may contain private local code or
  environment values.
  """
  timeout = deadline - time.monotonic()
  if timeout <= 0:
    return "local tests had no time left in the check's budget"
  env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
  # Contain the runner's runtime directories too: SIGKILL cannot run its trap.
  env["TMPDIR"] = str(runtime)
  marker = _PROCESS_MARKER.get()
  if marker:
    from app.process_groups import RUN_MARKER_ENV
    env[RUN_MARKER_ENV] = marker
  try:
    proc = subprocess.Popen(
      argv, cwd=str(repo), env=env, start_new_session=True,
      stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
  except OSError:
    return "local test runner could not start"
  try:
    proc.wait(timeout=timeout)
  except subprocess.TimeoutExpired:
    return f"local tests timed out after {round(timeout)}s"
  finally:
    # Also reap descendants left behind by a runner that exited early.
    try:
      os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    proc.wait()
  return proc.returncode


def _run_backend(repo: Path, tests: list[str], deadline: float) -> LocalTestRun:
  if not (repo / RUNNER).is_file():
    return LocalTestRun(unavailable=f"{RUNNER} is missing")
  with tempfile.TemporaryDirectory(prefix="mobius-local-tests-") as tmp:
    report = Path(tmp) / "report.xml"
    exit_code = _run_process(
      repo, ["bash", RUNNER, *tests, "-q", f"--junitxml={report}"], Path(tmp), deadline,
    )
    if isinstance(exit_code, str):
      return LocalTestRun(unavailable=exit_code)
    if not report.is_file():
      return LocalTestRun(unavailable=f"local tests produced no report (exit {exit_code})")
    try:
      return _read_report(report, exit_code)
    except (ET.ParseError, OSError):
      return LocalTestRun(unavailable="local test report was unreadable")


def _frontend_suites(
  frontend: Path, deadline: float | None = None,
) -> dict[str, tuple[str, frozenset[str]]] | str:
  """Each suite's module loader and test files, as its npm script defines them.

  Listing counts against ``deadline`` when one is given.
  """
  try:
    scripts = json.loads((frontend / "package.json").read_text())["scripts"]
  except (OSError, ValueError, KeyError, TypeError):
    return "frontend test suites are not readable from package.json"
  suites = {}
  for name in FRONTEND_SUITES:
    command = scripts.get(name) if isinstance(scripts, dict) else None
    loader = re.search(r"--loader=(\S+)", command or "")
    listings = re.findall(r"\$\((find\s[^)]*)\)", command or "")
    if not loader or not listings:
      return f"frontend test suite {name} is not in the expected form"
    members: set[str] = set()
    for listing in listings:
      limit = 60.0 if deadline is None else min(60.0, deadline - time.monotonic())
      if limit <= 0:
        return "local tests had no time left in the check's budget"
      try:
        proc = subprocess.run(
          ["sh", "-c", listing], cwd=str(frontend), capture_output=True, text=True, timeout=limit,
        )
      except (OSError, subprocess.TimeoutExpired):
        return f"frontend test suite {name} could not be listed"
      if proc.returncode:
        return f"frontend test suite {name} could not be listed"
      members.update(line.removeprefix("./") for line in proc.stdout.splitlines() if line)
    suites[name] = (loader.group(1), frozenset(members))
  return suites


def _frontend_plan(
  repo: Path, tests: list[str], deadline: float,
) -> dict[str, tuple[str, list[str]]] | str:
  """Frontend-relative test files grouped by owning suite, with its loader."""
  if not (repo / FRONTEND_RUNNER).is_file():
    return f"{FRONTEND_RUNNER} is missing"
  suites = _frontend_suites(repo / "frontend", deadline)
  if isinstance(suites, str):
    return suites
  chosen: dict[str, tuple[str, list[str]]] = {}
  for test in tests:
    owners = [name for name, (_, members) in suites.items() if test in members]
    if len(owners) != 1:
      return f"frontend test is not in exactly one suite: {test}"
    chosen.setdefault(owners[0], (suites[owners[0]][0], []))[1].append(test)
  return chosen


def _run_frontend_suite(repo: Path, loader: str, files: list[str], deadline: float) -> LocalTestRun:
  with tempfile.TemporaryDirectory(prefix="mobius-local-tests-") as tmp:
    report = Path(tmp) / "report.xml"
    exit_code = _run_process(repo, [
      "bash", FRONTEND_RUNNER, "exec", "--no", "--",
      "node", "--disable-warning=ExperimentalWarning", f"--loader={loader}",
      "--test", "--test-concurrency=1", "--test-reporter=junit",
      f"--test-reporter-destination={report}", *sorted(files),
    ], Path(tmp), deadline)
    if isinstance(exit_code, str):
      return LocalTestRun(unavailable=exit_code)
    if not report.is_file():
      return LocalTestRun(unavailable=f"frontend tests produced no report (exit {exit_code})")
    try:
      return _read_node_report(report, exit_code, repo / "frontend")
    except (ET.ParseError, OSError):
      return LocalTestRun(unavailable="frontend test report was unreadable")


def _read_node_report(report: Path, exit_code: int, frontend: Path) -> LocalTestRun:
  """Outcomes from ``node --test``'s JUnit report, checked against its totals."""
  if exit_code not in (0, 1):
    return LocalTestRun(unavailable=f"frontend tests did not complete (exit {exit_code})")
  root = ET.parse(report, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))).getroot()
  totals = {}
  for comment in root.iter(ET.Comment):
    match = re.fullmatch(r"\s*(\w+) (\d+)(?:\.\d+)?\s*", comment.text or "")
    if match:
      totals[match.group(1)] = int(match.group(2))
  cases = list(root.iter("testcase"))
  failures = sum(case.find("failure") is not None for case in cases)
  skips = sum(case.find("skipped") is not None for case in cases)
  if (
    any(key not in totals for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"))
    or totals["tests"] != len(cases)
    or totals["fail"] != failures
    or totals["skipped"] + totals["todo"] != skips
    or totals["pass"] != len(cases) - failures - skips
  ):
    return LocalTestRun(unavailable="frontend test report totals do not account for its test cases")
  if totals["cancelled"]:
    return LocalTestRun(unavailable="frontend tests were cancelled")
  passed, failed, skipped = set(), set(), set()
  for case in cases:
    file = case.get("file")
    if not file or not case.get("name"):
      return LocalTestRun(unavailable="frontend test report has missing test identities")
    try:
      relative = Path(file).resolve().relative_to(frontend.resolve())
    except ValueError:
      return LocalTestRun(unavailable="frontend test report names a file outside the checkout")
    ident = f"frontend/{relative.as_posix()}::{case.get('classname', '')}::{case.get('name')}"
    if ident in passed | failed | skipped:
      return LocalTestRun(unavailable="frontend test report has duplicate test identities")
    if case.find("failure") is not None:
      failed.add(ident)
    elif case.find("skipped") is not None:
      skipped.add(ident)
    else:
      passed.add(ident)
  if not cases or bool(failed) != (exit_code == 1):
    return LocalTestRun(unavailable="frontend test report does not account for the runner exit status")
  return LocalTestRun(frozenset(passed), frozenset(failed), frozenset(skipped))


def run(repo: Path, tests: list[str], *, timeout: int = TIMEOUT_SECONDS) -> LocalTestRun:
  """Execute exactly the selection within ``timeout``, as one run's evidence."""
  return combine(list(run_suites(repo, tests, deadline=time.monotonic() + timeout).values()))


def run_suites(
  repo: Path, tests: list[str], *, deadline: float,
  only: set[str] | None = None, reserve: bool = False,
) -> dict[str, LocalTestRun]:
  """Each suite's evidence for the selection, keyed ``backend`` or by npm suite.

  Never silently drops missing test files. Each suite runs with a fair share
  of the time left before ``deadline``; ``reserve`` keeps an equal share per
  suite for a later run (the candidate after the baseline). Suites outside
  ``only`` are reported as not run.
  """
  if not tests:
    return {"selection": LocalTestRun(unavailable="no local tests selected for the changed files")}
  missing = [test for test in tests if not (repo / test).is_file()]
  if missing:
    return {"selection": LocalTestRun(unavailable="selected test files are missing: " + ", ".join(missing))}
  backend = [test.removeprefix("backend/") for test in tests if test.startswith("backend/")]
  frontend = [test.removeprefix("frontend/") for test in tests if test.startswith("frontend/")]
  planned: dict[str, Callable[[float], LocalTestRun]] = {}
  results: dict[str, LocalTestRun] = {}
  if backend:
    planned["backend"] = lambda until: _run_backend(repo, backend, until)
  if frontend and only is not None and not only & set(FRONTEND_SUITES):
    results["frontend"] = LocalTestRun(unavailable="frontend not rerun: nothing in it passed on the baseline")
  elif frontend:
    plan = _frontend_plan(repo, frontend, deadline)
    if isinstance(plan, str):
      results["frontend"] = LocalTestRun(unavailable=plan)
    else:
      for name, (loader, files) in plan.items():
        planned[name] = lambda until, loader=loader, files=files: _run_frontend_suite(
          repo, loader, files, until,
        )
  runnable = [name for name in planned if only is None or name in only]
  for name in planned:
    if name not in runnable:
      results[name] = LocalTestRun(unavailable=f"{name} not rerun: nothing in it passed on the baseline")
  for index, name in enumerate(runnable):
    started = time.monotonic()
    slots = len(runnable) - index + (len(runnable) if reserve else 0)
    outcome = planned[name](started + max(0.0, deadline - started) / slots)
    results[name] = replace(outcome, seconds=time.monotonic() - started)
  return results


def describe_timing(results: dict[str, LocalTestRun]) -> str:
  """Per-suite wall time and outcome, for the update log."""
  return ", ".join(
    f"{name} {run.seconds:.0f}s{' (' + run.unavailable + ')' if run.unavailable else ''}"
    for name, run in results.items()
  )


def combine(parts: list[LocalTestRun]) -> LocalTestRun:
  """One run's evidence from independent suites; a suite without evidence adds none."""
  if len(parts) == 1:
    return parts[0]
  reasons = [part.unavailable for part in parts if part.unavailable]
  return LocalTestRun(
    frozenset().union(*(part.passed for part in parts)),
    frozenset().union(*(part.failed for part in parts)),
    frozenset().union(*(part.skipped for part in parts)),
    "; ".join(reasons) or None,
  )


def compare(tests: list[str], before: LocalTestRun, after: LocalTestRun, *, omitted: int = 0) -> dict:
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
  if omitted:
    incomplete.append(
      f"{omitted} more frontend test file(s) cover the change but were not run "
      f"(nearest {FRONTEND_TEST_LIMIT} only)"
    )
  broken = newly_failing(before, after)
  return {
    "status": "regression" if broken else "incomplete" if incomplete else "compared",
    "selected": tests,
    "selection_omitted": omitted,
    "regressions": broken or [],
    "incomplete": incomplete,
    "baseline": {"passed": sorted(before.passed), "failed": sorted(before.failed),
                 "skipped": sorted(before.skipped)},
    "candidate": {"passed": sorted(after.passed), "failed": sorted(after.failed),
                  "skipped": sorted(after.skipped)},
  }


def newly_failing(before: LocalTestRun, after: LocalTestRun) -> list[str]:
  """Only passing-before/failing-after proves a regression.

  A test identity exists only for a test that executed and reported, so a
  suite that produced no evidence on either side adds nothing here while the
  suites that did run still count.

  A frontend test file that cannot load on the candidate (the usual break
  after a release removes an export it uses) reports one failure under its
  own name instead of its tests. When a file fails and none of its baseline
  tests ran on the candidate, each test that passed before counts as failing.
  """
  broken = set(after.failed & before.passed)
  ran = after.passed | after.failed | after.skipped
  for file in {_frontend_file(ident) for ident in after.failed} - {None}:
    baseline = {
      ident for ident in before.passed | before.failed | before.skipped
      if _frontend_file(ident) == file
    }
    if baseline and not baseline & ran:
      broken |= {ident for ident in before.passed if _frontend_file(ident) == file}
  return sorted(broken)


def _frontend_file(ident: str) -> str | None:
  """The test file of a frontend identity (``frontend/<file>::<suite>::<name>``)."""
  return ident.split("::", 1)[0] if ident.startswith("frontend/") else None


def describe(broken: list[str]) -> str:
  shown = ", ".join(broken[:5])
  more = f" (+{len(broken) - 5} more)" if len(broken) > 5 else ""
  return (
    "Tests covering your local changes fail on this release: "
    f"{shown}{more}. The previous version keeps running; repair the local "
    "changes for this release, then update again."
  )
