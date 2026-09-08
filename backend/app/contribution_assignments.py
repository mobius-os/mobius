"""GitHub-native assignment: repository access owns eligibility, never org membership.

Assignment adds one person without replacing other assignees. GitHub can silently
ignore an ineligible assignee, so a successful HTTP response alone is not proof.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path


class AssignmentError(Exception):
  def __init__(self, status: int, message: str):
    self.status = status
    self.message = message
    super().__init__(message)


def _read(gh: Callable, cwd: Path, endpoint: str):
  return json.loads(gh(cwd, "api", endpoint).stdout)


def repository_access(gh: Callable, cwd: Path, repo: str) -> dict:
  repository = _read(gh, cwd, f"repos/{repo}")
  permissions = repository.get("permissions") or {}
  admin = permissions.get("admin") is True
  writable = any(permissions.get(role) is True for role in ("push", "maintain", "admin"))
  active = not repository.get("archived") and not repository.get("disabled")
  return {
    "can_assign": active and (writable or permissions.get("triage") is True),
    # This is repository authority, not proof a particular PR satisfies branch rules.
    "can_merge": active and writable,
    "can_manage_access": admin,
    "access_url": f"https://github.com/{repo}/settings/access" if admin else None,
  }


def list_assignees(gh: Callable, cwd: Path, repo: str, page: int = 1) -> dict:
  access = repository_access(gh, cwd, repo)
  people = _read(gh, cwd, f"repos/{repo}/assignees?per_page=100&page={page}")
  return {
    "repo": repo, **access,
    "assignees": [
      {"login": person["login"], "avatar_url": person.get("avatar_url"),
       "html_url": person.get("html_url")}
      for person in people
    ],
    "page": page,
    "next_page": page + 1 if len(people) == 100 else None,
  }


def assign_pull_request(gh: Callable, cwd: Path, repo: str, number: int,
                        login: str, expected_head_sha: str | None = None,
                        before_write: Callable | None = None) -> dict:
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
    raise AssignmentError(422, "Choose a valid GitHub account.")
  if not repository_access(gh, cwd, repo)["can_assign"]:
    raise AssignmentError(403, "You do not have permission to assign work in this repository.")
  pull = _read(gh, cwd, f"repos/{repo}/pulls/{number}")
  if pull.get("state") != "open" or pull.get("merged"):
    raise AssignmentError(409, "This pull request is no longer open. Refresh the project.")
  if expected_head_sha is not None and (pull.get("head") or {}).get("sha") != expected_head_sha:
    raise AssignmentError(409, "This pull request changed. Refresh it before assigning.")
  eligible = gh(cwd, "api", f"repos/{repo}/assignees/{login}", check=False)
  if eligible.returncode:
    # GitHub owns assignability; no local maintainer roster can override it.
    raise AssignmentError(409, "GitHub could not confirm that this person can be assigned. Refresh or check repository access.")
  if before_write is not None:
    before_write()
  assigned = json.loads(gh(
    cwd, "api", "--method", "POST", f"repos/{repo}/issues/{number}/assignees",
    "-f", f"assignees[]={login}",
  ).stdout)
  if not any(str(person.get("login", "")).lower() == login.lower()
             for person in assigned.get("assignees", [])):
    raise AssignmentError(409, "GitHub did not assign this person. Refresh the pull request and check repository access.")
  return {"assigned": True, "login": login, "repo": repo, "number": number}
