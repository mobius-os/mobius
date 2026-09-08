"""Assignment contracts, with all GitHub calls mocked and no network writes."""
import json
import subprocess
from pathlib import Path

import pytest

from app.contribution_assignments import AssignmentError, assign_pull_request, list_assignees, repository_access


class GitHub:
  def __init__(self, permissions=None, state="open", eligible=True, assigned=True, people=None):
    self.permissions = permissions if permissions is not None else {"triage": True}
    self.state = state
    self.eligible = eligible
    self.assigned = assigned
    self.people = people or []
    self.calls = []

  def __call__(self, cwd, *args, check=True):
    self.calls.append(args)
    if "POST" in args:
      result = {"assignees": [{"login": "existing"}, {"login": "target"}] if self.assigned else []}
    elif args[-1] == "repos/org/repo":
      result = {"permissions": self.permissions}
    elif args[-1] == "repos/org/repo/pulls/7":
      result = {"state": self.state, "head": {"sha": "current"}}
    elif "/assignees?" in args[-1]:
      result = self.people
    else:
      return subprocess.CompletedProcess(args, 0 if self.eligible else 1, "", "")
    return subprocess.CompletedProcess(args, 0, json.dumps(result), "")


@pytest.mark.parametrize("permissions,assign,merge,manage", [
  ({"pull": True}, False, False, False),
  ({"triage": True}, True, False, False),
  ({"push": True}, True, True, False),
  ({"maintain": True}, True, True, False),
  ({"admin": True}, True, True, True),
  ({}, False, False, False),
])
def test_repository_authority_is_not_org_membership(permissions, assign, merge, manage):
  access = repository_access(GitHub(permissions), Path("."), "org/repo")
  assert (access["can_assign"], access["can_merge"], access["can_manage_access"]) == (assign, merge, manage)
  assert bool(access["access_url"]) == manage


def test_people_are_paginated_and_only_public_identity_is_exposed():
  gh = GitHub(people=[{"login": str(i), "secret": "not returned"} for i in range(100)])
  result = list_assignees(gh, Path("."), "org/repo", 2)
  assert result["next_page"] == 3
  assert len(result["assignees"]) == 100
  assert "secret" not in result["assignees"][0]
  assert gh.calls[-1][-1].endswith("per_page=100&page=2")
  assert list_assignees(GitHub(), Path("."), "org/repo")["next_page"] is None


def test_assignment_adds_one_person_without_replacing_existing_assignees():
  gh = GitHub()
  result = assign_pull_request(gh, Path("."), "org/repo", 7, "target", "current")
  assert result["assigned"] is True
  assert result["login"] == "target"
  assert gh.calls[-1] == ("api", "--method", "POST", "repos/org/repo/issues/7/assignees", "-f", "assignees[]=target")


@pytest.mark.parametrize("kwargs,head,login,status", [
  ({"permissions": {"pull": True}}, "current", "target", 403),
  ({"state": "closed"}, "current", "target", 409),
  ({}, "stale", "target", 409),
  ({"eligible": False}, "current", "target", 409),
  ({}, "current", "../target", 422),
])
def test_rejected_assignment_never_writes(kwargs, head, login, status):
  gh = GitHub(**kwargs)
  with pytest.raises(AssignmentError) as error:
    assign_pull_request(gh, Path("."), "org/repo", 7, login, head)
  assert error.value.status == status
  assert not any("POST" in call for call in gh.calls)


def test_github_silent_ignored_assignment_is_not_reported_as_success():
  with pytest.raises(AssignmentError, match="did not assign"):
    assign_pull_request(GitHub(assigned=False), Path("."), "org/repo", 7, "target")


def test_revoked_local_authority_stops_write_after_remote_preflight():
  gh = GitHub()
  def revoked():
    raise AssignmentError(403, "revoked")
  with pytest.raises(AssignmentError, match="revoked"):
    assign_pull_request(gh, Path("."), "org/repo", 7, "target", before_write=revoked)
  assert not any("POST" in call for call in gh.calls)
