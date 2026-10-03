#!/usr/bin/env python3
"""Read-only, bounded GitHub PR-head check observation.

GitHub REST documentation: /rest/checks/runs, /rest/checks/suites, and
/rest/commits/statuses. No API error text is included in output.
"""

import argparse
import json
import re
import subprocess
import sys
import time

SHA = re.compile(r"[0-9a-fA-F]{7,40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PAGE_SIZE = 100
DEADLINE_SECONDS = 90
CALL_TIMEOUT_SECONDS = 15
FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
VALID_CONCLUSIONS = FAILED_CONCLUSIONS | {"success", "neutral", "skipped", "stale"}


class CheckError(Exception):
    """An observation cannot be trusted; the message is safe for display."""


def api(path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CheckError("GitHub check lookup timed out; try again later.")
    try:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True,
            timeout=min(CALL_TIMEOUT_SECONDS, remaining), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError("Could not read GitHub checks; try again later.") from exc
    if result.returncode:
        raise CheckError("Could not read GitHub checks; verify access and try again later.")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise CheckError("GitHub returned an unreadable check response; try again later.") from exc


def object_page(path, key, deadline, first=None):
    """Paginate count-bearing GitHub check collections, rejecting partial evidence."""
    items = []
    expected = None
    page = 1
    while True:
        data = first if page == 1 and first is not None else api(
            f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", deadline
        )
        if not isinstance(data, dict) or type(data.get("total_count")) is not int or data["total_count"] < 0 or not isinstance(data.get(key), list):
            raise CheckError("GitHub returned incomplete check data; try again later.")
        if expected is None:
            expected = data["total_count"]
        elif expected != data["total_count"]:
            raise CheckError("GitHub checks changed during lookup; try again later.")
        chunk = data[key]
        if len(chunk) > PAGE_SIZE or not all(isinstance(item, dict) for item in chunk):
            raise CheckError("GitHub returned incomplete check data; try again later.")
        items.extend(chunk)
        if len(items) >= expected:
            if len(items) != expected:
                raise CheckError("GitHub returned inconsistent check totals; try again later.")
            return items
        if len(chunk) != PAGE_SIZE:
            raise CheckError("GitHub returned an incomplete check page; try again later.")
        page += 1


def list_runs(repo, head, deadline):
    suites_path = f"repos/{repo}/commits/{head}/check-suites"
    first = api(f"{suites_path}?per_page=100&page=1", deadline)
    # Validate even when the ordinary ref endpoint suffices; incomplete suite
    # evidence cannot establish that the ref endpoint is below GitHub's cap.
    if (not isinstance(first, dict) or type(first.get("total_count")) is not int
            or first["total_count"] < 0 or not isinstance(first.get("check_suites"), list)
            or len(first["check_suites"]) != min(PAGE_SIZE, first["total_count"])
            or not all(isinstance(suite, dict) for suite in first["check_suites"])):
        raise CheckError("GitHub returned incomplete check suites; try again later.")
    if first["total_count"] < 1000:
        # GitHub's default filter=latest ignores historical reruns.
        return object_page(f"repos/{repo}/commits/{head}/check-runs", "check_runs", deadline)
    # The commit-ref endpoint can truncate when a SHA has >1,000 suites.
    # Enumerate every suite, then its latest check runs instead.
    suites = object_page(suites_path, "check_suites", deadline, first=first)
    all_runs = []
    for suite in suites:
        suite_id = suite.get("id")
        if type(suite_id) is not int or suite_id <= 0 or suite.get("head_sha") != head:
            raise CheckError("GitHub returned inconsistent check suites; try again later.")
        all_runs.extend(object_page(f"repos/{repo}/check-suites/{suite_id}/check-runs", "check_runs", deadline))
    return all_runs


def list_statuses(repo, head, deadline):
    latest = {}
    page = 1
    while True:
        data = api(f"repos/{repo}/commits/{head}/statuses?per_page=100&page={page}", deadline)
        if not isinstance(data, list) or len(data) > PAGE_SIZE:
            raise CheckError("GitHub returned incomplete commit statuses; try again later.")
        for status in data:
            if (not isinstance(status, dict) or not isinstance(status.get("context"), str)
                    or not status["context"] or not isinstance(status.get("state"), str)
                    or status["state"] not in {"pending", "success", "failure", "error"}):
                raise CheckError("GitHub returned an unreadable commit status; try again later.")
            # GitHub documents reverse chronological order; first per context is newest.
            latest.setdefault(status["context"], status["state"])
        if len(data) < PAGE_SIZE:
            return latest
        page += 1


def observe(repo, pr, expected_sha):
    deadline = time.monotonic() + DEADLINE_SECONDS
    pull = api(f"repos/{repo}/pulls/{pr}", deadline)
    head = pull.get("head", {}).get("sha") if isinstance(pull, dict) and isinstance(pull.get("head"), dict) else None
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise CheckError("GitHub did not provide a valid PR head; try again later.")
    if not head.lower().startswith(expected_sha.lower()):
        return result("failed", "The requested commit is not the published pull-request head.", 0, 0)
    runs = list_runs(repo, head, deadline)
    statuses = list_statuses(repo, head, deadline)
    final_pull = api(f"repos/{repo}/pulls/{pr}", deadline)
    final_head = final_pull.get("head", {}).get("sha") if isinstance(final_pull, dict) and isinstance(final_pull.get("head"), dict) else None
    if final_head != head:
        return result("failed", "The PR head changed during this check; the wait targets an older commit.", 0, 0)
    ids = set()
    completed = failed = 0
    for run in runs:
        run_id = run.get("id")
        if type(run_id) is not int or run_id in ids or run.get("head_sha") != head or not isinstance(run.get("status"), str):
            raise CheckError("GitHub returned inconsistent check runs; try again later.")
        ids.add(run_id)
        status = run["status"]
        if status == "completed":
            conclusion = run.get("conclusion")
            if not isinstance(conclusion, str) or conclusion not in VALID_CONCLUSIONS:
                raise CheckError("GitHub returned an incomplete check result; try again later.")
            completed += 1
            failed += conclusion in FAILED_CONCLUSIONS
        elif status not in {"queued", "in_progress", "waiting", "requested", "pending"}:
            raise CheckError("GitHub returned an unknown check state; try again later.")
    for status in statuses.values():
        if status != "pending":
            completed += 1
            failed += status in {"failure", "error"}
    total = len(runs) + len(statuses)
    if total == 0:
        return result("pending", "No checks have appeared for this commit yet.", 0, 0)
    if completed < total:
        return result("pending", f"{completed} of {total} checks have finished; waiting for the rest.", completed, total, failed)
    if failed:
        return result("met", f"All {total} checks finished; {failed} reported failure.", completed, total, failed)
    return result("met", f"All {total} checks finished.", completed, total, 0)


def result(state, summary, completed, total, failed_count=None):
    value = {"state": state, "summary": summary, "completed": completed, "total": total}
    if failed_count is not None:
        value["failed_count"] = failed_count
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print one bounded JSON result")
    parser.add_argument("repo")
    parser.add_argument("pr")
    parser.add_argument("sha")
    args = parser.parse_args()
    if not REPO.fullmatch(args.repo) or not args.pr.isdecimal() or int(args.pr) <= 0 or not SHA.fullmatch(args.sha):
        parser.error("expected owner/repo, positive PR number, and 7-40 hexadecimal SHA characters")
    try:
        value = observe(args.repo, args.pr, args.sha)
    except CheckError as exc:
        value = result("failed", str(exc), 0, 0)
    if args.json:
        print(json.dumps(value, separators=(",", ":")))
        return 0
    if value["state"] == "pending":
        return 1
    if value["state"] == "failed":
        print("pr-checks: " + value["summary"], file=sys.stderr)
        return 2
    print(value["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
