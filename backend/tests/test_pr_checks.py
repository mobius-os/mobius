"""Hermetic contract tests for the GitHub wait checker (no network or auth)."""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SHA = "a" * 40
OTHER = "b" * 40


def run_item(n, status="completed", conclusion="success"):
    return {"id": n, "head_sha": SHA, "status": status, "conclusion": conclusion}


def fixture_run(tmp_path, replies, *, shell=False):
    fixture = tmp_path / "replies.json"
    fixture.write_text(json.dumps(replies))
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['GH_FIXTURE']) as stream:
    replies = json.load(stream)
path = sys.argv[2]
value = replies.get(path)
if value is None:
    print('secret auth diagnostic: fixture path missing', file=sys.stderr)
    sys.exit(1)
if value == 'BAD_JSON':
    print('{bad')
else:
    print(json.dumps(value))
""")
    gh.chmod(0o755)
    env = {**os.environ, "GH_FIXTURE": str(fixture), "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    command = ([str(SCRIPTS / "pr-checks.sh")] if shell else [sys.executable, str(SCRIPTS / "pr-checks.py"), "--json"])
    process = subprocess.run(command + ["owner/repo", "7", SHA[:12]], text=True, capture_output=True, env=env, timeout=10)
    return process if shell else (process, json.loads(process.stdout))


def base(runs, statuses=None):
    replies = {f"repos/owner/repo/pulls/7": {"head": {"sha": SHA}}}
    replies[f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1"] = {
        "total_count": 1 if runs else 0,
        "check_suites": [{"id": 1, "head_sha": SHA}] if runs else [],
    }
    for page, chunk in enumerate([runs[i:i + 100] for i in range(0, len(runs), 100)] or [[]], 1):
        replies[f"repos/owner/repo/commits/{SHA}/check-runs?per_page=100&page={page}"] = {"total_count": len(runs), "check_runs": chunk}
    replies[f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"] = statuses or []
    return replies


def test_later_page_pending_and_shell_silent(tmp_path):
    runs = [run_item(i) for i in range(150)]
    runs[-1]["status"] = "in_progress"
    replies = base(runs)
    process, value = fixture_run(tmp_path, replies)
    assert process.returncode == 0
    assert value == {"state": "pending", "summary": "149 of 150 checks have finished; waiting for the rest.", "completed": 149, "total": 150, "failed_count": 0}
    shell = fixture_run(tmp_path, replies, shell=True)
    assert (shell.returncode, shell.stdout, shell.stderr) == (1, "", "")


def test_failed_checks_are_terminal_not_broken(tmp_path):
    replies = base([run_item(1, conclusion="failure"), run_item(2)], [{"context": "legacy", "state": "error"}])
    process, value = fixture_run(tmp_path, replies)
    assert process.returncode == 0
    assert (value["state"], value["completed"], value["total"], value["failed_count"]) == ("met", 3, 3, 2)
    assert "2 reported failure" in value["summary"]
    assert fixture_run(tmp_path, replies, shell=True).returncode == 0


def test_status_pages_use_latest_per_context(tmp_path):
    statuses = [{"context": f"ci-{i}", "state": "success"} for i in range(99)]
    statuses += [{"context": "repeat", "state": "pending"}, {"context": "repeat", "state": "success"}]
    replies = base([run_item(1)])
    replies[f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"] = statuses[:100]
    replies[f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=2"] = statuses[100:]
    _, value = fixture_run(tmp_path, replies)
    assert (value["state"], value["completed"], value["total"]) == ("pending", 100, 101)


def test_1000_runs_in_one_suite_still_use_paginated_ref_endpoint(tmp_path):
    runs = [run_item(i) for i in range(1000)]
    runs[-1]["status"] = "queued"
    replies = base(runs)
    _, value = fixture_run(tmp_path, replies)
    assert (value["state"], value["completed"], value["total"]) == ("pending", 999, 1000)


def test_more_than_1000_suites_fall_back_and_reuse_first_page(monkeypatch):
    spec = importlib.util.spec_from_file_location("pr_checks", SCRIPTS / "pr-checks.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    suites = [{"id": i + 1, "head_sha": SHA} for i in range(1001)]
    calls = []

    def fake_api(path, deadline):
        calls.append(path)
        assert "filter=all" not in path
        if "/check-suites?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {"total_count": len(suites), "check_suites": suites[(page - 1) * 100:page * 100]}
        if "/check-suites/" in path:
            suite_id = int(path.split("/check-suites/", 1)[1].split("/", 1)[0])
            status = "queued" if suite_id == 1001 else "completed"
            return {"total_count": 1, "check_runs": [run_item(suite_id, status=status)]}
        pytest.fail(f"unexpected ref check-run call: {path}")

    monkeypatch.setattr(checker, "api", fake_api)
    runs = checker.list_runs("owner/repo", SHA, 9999999999)
    assert len(runs) == 1001 and runs[-1]["status"] == "queued"
    assert calls.count(f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1") == 1


def test_replaced_head_is_diagnostic(tmp_path):
    replies = {"repos/owner/repo/pulls/7": {"head": {"sha": OTHER}}}
    _, value = fixture_run(tmp_path, replies)
    assert value["state"] == "failed" and "error" not in value
    shell = fixture_run(tmp_path, replies, shell=True)
    assert shell.returncode == 2 and "not the published" in shell.stderr


@pytest.mark.parametrize("change", [
    lambda replies: replies.update({f"repos/owner/repo/commits/{SHA}/check-runs?per_page=100&page=1": "BAD_JSON"}),
    lambda replies: replies.update({f"repos/owner/repo/commits/{SHA}/check-runs?per_page=100&page=1": {"check_runs": []}}),
    lambda replies: replies.update({f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1": "BAD_JSON"}),
    lambda replies: replies.update({f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1": [{"context": "ci", "state": []}]}),
    lambda replies: replies.update({f"repos/owner/repo/commits/{SHA}/check-runs?per_page=100&page=1": {"total_count": 1, "check_runs": [run_item(1, conclusion=[])]}}),
])
def test_bad_evidence_is_safe_and_visible(tmp_path, change):
    replies = base([run_item(1)])
    change(replies)
    process, value = fixture_run(tmp_path, replies)
    assert process.returncode == 0 and value["state"] == "failed" and "error" not in value
    assert "secret" not in value["summary"] and "fixture" not in value["summary"]
    assert len(value["summary"]) <= 500
    shell = fixture_run(tmp_path, replies, shell=True)
    assert shell.returncode == 2 and "secret" not in shell.stderr


def test_no_checks_remains_pending(tmp_path):
    _, value = fixture_run(tmp_path, base([]))
    assert (value["state"], value["completed"], value["total"]) == ("pending", 0, 0)


def test_head_changed_during_observation_never_reports_finished(monkeypatch):
    spec = importlib.util.spec_from_file_location("pr_checks", SCRIPTS / "pr-checks.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    replies = base([run_item(1)])
    pull_reads = 0

    def fake_api(path, deadline):
        nonlocal pull_reads
        if path == "repos/owner/repo/pulls/7":
            pull_reads += 1
            return {"head": {"sha": SHA if pull_reads == 1 else OTHER}}
        return replies[path]

    monkeypatch.setattr(checker, "api", fake_api)
    value = checker.observe("owner/repo", "7", SHA)
    assert value["state"] == "failed"
    assert "changed during" in value["summary"]
