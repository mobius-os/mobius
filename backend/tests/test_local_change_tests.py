"""Which tests cover an owner's local frontend edits, and how their runs read."""

import json
import subprocess
from pathlib import Path

import pytest

from app import local_change_tests as lt

REPO = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
  ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
  for rel, text in files.items():
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
  _git(repo, "add", "-A")
  _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "tree", "--allow-empty")
  return _git(repo, "rev-parse", "HEAD")


_SOURCES = {
  "backend/app/__init__.py": "",
  "frontend/src/components/Reply.jsx": "import Sources from './Sources.jsx'\nexport default Sources\n",
  "frontend/src/components/Sources.jsx": "export default function Sources() {}\n",
  "frontend/src/components/Other.jsx": "export default function Other() {}\n",
  "frontend/src/components/__tests__/reply.test.js": "import Reply from '../Reply.jsx'\n",
  "frontend/src/components/__tests__/loaded.test.js": (
    "const mod = await vite.ssrLoadModule('/src/components/Reply.jsx')\n"
  ),
  "frontend/src/components/__tests__/text.test.js": (
    "readFileSync(new URL('../Sources.jsx', import.meta.url))\n"
  ),
  "frontend/src/components/__tests__/other.test.js": "import Other from '../Other.jsx'\n",
}


@pytest.fixture
def history(tmp_path):
  _git(tmp_path, "init", "-q")
  baseline = _commit(tmp_path, _SOURCES)
  release = _commit(tmp_path, {
    "frontend/src/components/__tests__/added.test.js": "import Reply from '../Reply.jsx'\n",
  })
  return tmp_path, baseline, release


def test_frontend_tests_that_reach_a_changed_file_are_selected(history):
  repo, baseline, release = history
  candidate = _commit(repo, {
    "frontend/src/components/Sources.jsx": "export default function Sources({ groups }) {}\n",
  })
  assert lt.select(repo, release, candidate, baseline) == [
    # Through Reply.jsx, by Vite path, and by reading the source as text.
    "frontend/src/components/__tests__/loaded.test.js",
    "frontend/src/components/__tests__/reply.test.js",
    "frontend/src/components/__tests__/text.test.js",
  ]


def test_tests_the_release_added_are_not_selected_without_an_earlier_run(history):
  repo, baseline, release = history
  candidate = _commit(repo, {"frontend/src/components/Reply.jsx": "export default 1\n"})
  selected = lt.select(repo, release, candidate, baseline)
  assert "frontend/src/components/__tests__/added.test.js" not in selected
  assert "frontend/src/components/__tests__/reply.test.js" in selected


def test_a_change_outside_frontend_source_selects_no_frontend_tests(history):
  repo, baseline, release = history
  candidate = _commit(repo, {"frontend/README.md": "notes\n"})
  assert lt.select(repo, release, candidate, baseline) == []


def test_every_frontend_test_belongs_to_exactly_one_npm_suite():
  suites = lt._frontend_suites(REPO / "frontend")
  assert isinstance(suites, dict), suites
  assert set(suites) == set(lt.FRONTEND_SUITES)
  owners = {}
  for name, (loader, members) in suites.items():
    assert (REPO / "frontend" / loader).is_file()
    for member in members:
      owners.setdefault(member, []).append(name)
  tests = {
    path.relative_to(REPO / "frontend").as_posix()
    for suffix in ("*.test.js", "*.hooktest.js")
    for path in (REPO / "frontend/src").rglob(suffix)
  }
  assert tests == set(owners)
  assert all(len(names) == 1 for names in owners.values())


def _node_report(cases: str, **totals) -> str:
  counts = {"tests": 0, "suites": 0, "pass": 0, "fail": 0, "cancelled": 0,
            "skipped": 0, "todo": 0, **totals}
  comments = "".join(f"<!-- {key} {value} -->" for key, value in counts.items())
  return f'<?xml version="1.0"?><testsuites>{cases}{comments}<!-- duration_ms 1.5 --></testsuites>'


def _case(frontend: Path, name: str, outcome: str = "", classname: str = "test") -> str:
  file = frontend / "src/a.test.js"
  return f'<testcase name="{name}" classname="{classname}" file="{file}">{outcome}</testcase>'


def test_node_report_outcomes_are_keyed_by_file_suite_and_name(tmp_path):
  report = tmp_path / "report.xml"
  report.write_text(_node_report(
    _case(tmp_path, "passes")
    + _case(tmp_path, "fails", '<failure type="testCodeFailure"/>')
    + _case(tmp_path, "skipped", '<skipped type="skipped"/>')
    + _case(tmp_path, "later", '<skipped type="todo"/>')
    + f'<testsuite name="group">{_case(tmp_path, "inner", classname="group")}</testsuite>',
    tests=5, suites=1, **{"pass": 2, "fail": 1, "skipped": 1, "todo": 1},
  ))
  run = lt._read_node_report(report, 1, tmp_path)
  assert run.unavailable is None
  assert run.passed == {
    "frontend/src/a.test.js::test::passes", "frontend/src/a.test.js::group::inner",
  }
  assert run.failed == {"frontend/src/a.test.js::test::fails"}
  assert run.skipped == {"frontend/src/a.test.js::test::skipped", "frontend/src/a.test.js::test::later"}


@pytest.mark.parametrize("cases,totals,exit_code", [
  ("passes", {"tests": 2, "pass": 2}, 0),  # totals disagree with the cases
  ("passes", {"tests": 1, "pass": 1}, 1),  # failing exit without a failed case
  ("passes", {"tests": 1, "pass": 1, "cancelled": 1}, 0),
  ("passes", {"tests": 1, "pass": 1}, 2),  # the runner itself did not finish
])
def test_node_reports_without_complete_evidence_are_unavailable(tmp_path, cases, totals, exit_code):
  report = tmp_path / "report.xml"
  report.write_text(_node_report(_case(tmp_path, cases), **totals))
  assert lt._read_node_report(report, exit_code, tmp_path).unavailable


def _node_run(tmp_path: Path, name: str, cases: str, exit_code: int, **totals):
  report = tmp_path / f"{name}.xml"
  report.write_text(_node_report(cases, **totals))
  return lt._read_node_report(report, exit_code, tmp_path)


def test_a_frontend_file_that_no_longer_loads_fails_the_tests_that_passed_before(tmp_path):
  # node --test reports a file that cannot load as one failure named after it.
  before = _node_run(tmp_path, "before", _case(tmp_path, "uses lib"), 0, tests=1, **{"pass": 1})
  after = _node_run(
    tmp_path, "after", _case(tmp_path, "a.test.js", '<failure type="testCodeFailure"/>'), 1,
    tests=1, fail=1,
  )
  report = lt.compare(["frontend/src/a.test.js"], before, after)
  assert report["status"] == "regression"
  assert report["regressions"] == ["frontend/src/a.test.js::test::uses lib"]


def test_a_new_failure_beside_tests_that_still_ran_is_not_a_load_failure(tmp_path):
  before = _node_run(
    tmp_path, "before", _case(tmp_path, "kept") + _case(tmp_path, "renamed"), 0,
    tests=2, **{"pass": 2},
  )
  after = _node_run(
    tmp_path, "after",
    _case(tmp_path, "kept") + _case(tmp_path, "new name", '<failure type="testCodeFailure"/>'), 1,
    tests=2, **{"pass": 1, "fail": 1},
  )
  report = lt.compare(["frontend/src/a.test.js"], before, after)
  assert report["status"] == "incomplete"
  assert report["regressions"] == []


_FAKE_NPM = """#!/bin/bash
printf '%s\\n' "$@" > "$(dirname "$0")/../args-$$"
for arg in "$@"; do
  case "$arg" in --test-reporter-destination=*) report="${arg#*=}" ;; esac
done
file="$PWD/$(dirname "$0")/../frontend/${@: -1}"
printf '<testsuites><testcase name="t" classname="test" file="%s"/>\
<!-- tests 1 --><!-- pass 1 --><!-- fail 0 --><!-- cancelled 0 -->\
<!-- skipped 0 --><!-- todo 0 --></testsuites>' "$file" > "$report"
"""


def test_frontend_tests_run_with_their_own_suites_loader(tmp_path):
  (tmp_path / "scripts").mkdir()
  (tmp_path / "scripts/wt-npm.sh").write_text(_FAKE_NPM)
  frontend = tmp_path / "frontend"
  for rel in ("src/lib/a.test.js", "src/hooks/b.hooktest.js"):
    (frontend / rel).parent.mkdir(parents=True, exist_ok=True)
    (frontend / rel).write_text("")
  (frontend / "package.json").write_text(json.dumps({"scripts": {
    "test:lib": "node --loader=./lib-loader.mjs --test $(find src/lib -name '*.test.js')",
    "test:hooks": "node --loader=./hooks-loader.mjs --test $(find src/hooks -name '*.hooktest.js')",
  }}))

  run = lt.run(tmp_path, ["frontend/src/hooks/b.hooktest.js", "frontend/src/lib/a.test.js"])

  assert run.unavailable is None
  assert run.passed == {
    "frontend/src/lib/a.test.js::test::t", "frontend/src/hooks/b.hooktest.js::test::t",
  }
  invocations = [path.read_text().split("\n") for path in tmp_path.glob("args-*")]
  loaders = {next(arg for arg in args if arg.startswith("--loader=")): args[-2] for args in invocations}
  assert loaders == {
    "--loader=./lib-loader.mjs": "src/lib/a.test.js",
    "--loader=./hooks-loader.mjs": "src/hooks/b.hooktest.js",
  }


def test_frontend_evidence_missing_does_not_hide_backend_evidence():
  backend = lt.LocalTestRun(passed=frozenset({"tests.test_a::test_a"}))
  frontend = lt.LocalTestRun(unavailable="frontend tests produced no report (exit 3)")
  before = lt._combine([backend, frontend])
  after = lt._combine([lt.LocalTestRun(failed=frozenset({"tests.test_a::test_a"})), frontend])
  report = lt.compare(["backend/tests/test_a.py", "frontend/src/a.test.js"], before, after)
  assert report["regressions"] == ["tests.test_a::test_a"]
  assert report["status"] == "regression"
