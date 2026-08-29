from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.contribution_broker import (
  MAX_REQUEST_BYTES,
  MAX_RESPONSE_BYTES,
  ContributionBrokerClient,
  ContributionBrokerError,
  bound_request_id,
  canonical_body,
)
from app.config import get_settings
from app.github_contribution_git import _reviewed_branch_diff
from app.routes import contribution_relay as relay_route
from app.storage_io import atomic_write
from test_app_fixtures import create_local_app


relay_route._limiter.enabled = False

_RELAY_ID = "ctr_1234567890abcdef1234567890abcdef"
_OTHER_RELAY_ID = "ctr_fedcba0987654321fedcba0987654321"


def _git(repo, *args, input_bytes=None):
  result = subprocess.run(
    ["git", "-C", str(repo), *args],
    input=input_bytes,
    capture_output=True,
    check=True,
  )
  return result.stdout.decode().strip()


def _write_relay_record(app_id, record_id, record):
  base = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  )
  base.mkdir(parents=True, exist_ok=True)
  atomic_write(base / f"{record_id}.json", json.dumps(record))
  atomic_write(base / f"{record_id}.diff", "reviewed diff\n")
  return base / f"{record_id}.json"


def _prepared_relay_record(client, owner_token, tmp_path, record_id):
  app_id = create_local_app(
    client,
    {"Authorization": f"Bearer {owner_token}"},
    name=f"relay-{record_id}",
    description="relay lifecycle test",
  )["id"]
  record_path = _write_relay_record(app_id, record_id, {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/mobius",
    "status": "prepared",
    "title": "Reviewed relay change",
    "branch": "fix/relay-review",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/mobius",
      "repo_path": str(tmp_path),
      "branch": "fix/relay-review",
      "body_draft": "## What\n\nA reviewed relay change.",
    },
  })
  return app_id, record_path


def _stub_reviewed_snapshot(monkeypatch, tmp_path):
  monkeypatch.setattr(relay_route, "_safe_repo_path", lambda _raw: tmp_path)
  monkeypatch.setattr(
    relay_route,
    "_merged_snapshot",
    lambda _record, _diff_path: ({
      "repo": "mobius-os/mobius",
      "source_repo": "mobius-os/mobius",
      "base_ref": "main",
      "base_sha": "a" * 40,
      "expected_tree_sha": "b" * 40,
    }, [{
      "path": "backend/app/example.py",
      "operation": "modify",
      "mode": "100644",
      "content_base64": base64.b64encode(b"reviewed\n").decode(),
    }]),
  )


def test_merged_snapshot_preserves_upstream_and_reviewed_changes(tmp_path, monkeypatch):
  repo = tmp_path / "review"
  repo.mkdir()
  _git(repo, "init", "-q")
  _git(repo, "config", "user.name", "Möbius")
  _git(repo, "config", "user.email", "mobius@example.test")
  (repo / "notes.txt").write_text("first\nmiddle\nlast\n")
  _git(repo, "add", "notes.txt")
  _git(repo, "commit", "-qm", "Base")
  base = _git(repo, "rev-parse", "HEAD")

  _git(repo, "checkout", "-qb", "feature")
  (repo / "notes.txt").write_text("reviewed\nmiddle\nlast\n")
  _git(repo, "add", "notes.txt")
  _git(
    repo,
    "commit",
    "-qm",
    "Reviewed change\n\nCo-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>",
  )
  head = _git(repo, "rev-parse", "HEAD")
  reviewed_diff = _reviewed_branch_diff(repo, base, head)
  diff_path = tmp_path / "review.diff"
  diff_path.write_bytes(reviewed_diff)

  _git(repo, "checkout", "-q", "-b", "upstream", base)
  (repo / "notes.txt").write_text("first\nmiddle\nupstream\n")
  _git(repo, "add", "notes.txt")
  _git(repo, "commit", "-qm", "Upstream change")
  upstream = _git(repo, "rev-parse", "HEAD")
  _git(repo, "checkout", "-q", "feature")

  monkeypatch.setattr(relay_route, "_safe_repo_path", lambda _raw: repo)
  monkeypatch.setenv("MOBIUS_CONTRIBUTION_TARGET_REPO", "mobius-os/mobius")
  monkeypatch.setattr(
    relay_route,
    "_assert_merges_with_upstream",
    lambda *_args: {
      "last_submit_upstream_branch": "main",
      "last_submit_upstream_sha": upstream,
    },
  )
  record = {
    "id": "review-1",
    "type": "pr",
    "repo": "mobius-os/mobius",
    "branch": "feature",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/mobius",
      "repo_path": str(repo),
      "branch": "feature",
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(reviewed_diff).hexdigest(),
    },
  }

  merge, files = relay_route._merged_snapshot(record, diff_path)

  assert merge["base_sha"] == upstream
  assert merge["repo"] == "mobius-os/mobius"
  assert merge["source_repo"] == "mobius-os/mobius"
  assert len(files) == 1
  assert files[0]["path"] == "notes.txt"
  assert base64.b64decode(files[0]["content_base64"]) == (
    b"reviewed\nmiddle\nupstream\n"
  )
  assert _git(repo, "show", f"{merge['expected_tree_sha']}:notes.txt") == (
    "reviewed\nmiddle\nupstream"
  )


def test_contribution_broker_binds_body_and_request_id():
  seen = []

  async def handler(request: httpx.Request):
    seen.append(request)
    return httpx.Response(201, json={"id": "ctr_1234567890abcdef1234567890abcdef"})

  client = ContributionBrokerClient(transport=httpx.MockTransport(handler))
  body = {"repo": "mobius-os/mobius", "files": []}
  key = "mobius-pr:1234567890abcdef"

  async def run():
    created = await client.request(
      "POST", "/v1/contributions", body=body, idempotency_key=key,
    )
    withdrawn = await client.request(
      "POST", "/v1/contributions/ctr_1234567890abcdef1234567890abcdef/withdraw",
      body={"contract_version": 1, "revision": 1},
      idempotency_key="mobius-withdraw:1234567890abcdef",
    )
    return created, withdrawn

  created, withdrawn = asyncio.run(run())
  assert created[0]["id"] == "ctr_1234567890abcdef1234567890abcdef"
  assert withdrawn[0]["id"] == "ctr_1234567890abcdef1234567890abcdef"
  encoded = canonical_body(body)
  assert seen[0].headers["Idempotency-Key"] == key
  assert seen[0].headers["X-Mobius-Request-Id"] == bound_request_id(
    "POST", "/v1/contributions", encoded, key,
  )
  assert b"user_" not in seen[0].content


def test_contribution_broker_rejects_route_expansion_and_surfaces_quota():
  async def handler(_request: httpx.Request):
    return httpx.Response(
      429,
      headers={"Retry-After": "120"},
      json={"error": {"code": "quota", "message": "Daily limit reached."}},
    )

  client = ContributionBrokerClient(transport=httpx.MockTransport(handler))

  async def run():
    with pytest.raises(ValueError):
      await client.request("POST", "/v1/contributions/other", body={})
    with pytest.raises(ValueError):
      await client.request(
        "GET",
        "/v1/contributions/ctr_1234567890abcdef1234567890abcdef"
        "?subject=other",
      )
    with pytest.raises(ValueError):
      await client.request("GET", "/v1/contributions/github/status")
    with pytest.raises(ValueError):
      await client.request("DELETE", "/v1/contributions/github")
    with pytest.raises(ValueError):
      await client.request(
        "POST", "/v1/contributions/ctr_1234567890abcdef1234567890abcdef/withdraw/again",
        body={}, idempotency_key="mobius-withdraw:1234567890abcdef",
      )
    with pytest.raises(ContributionBrokerError) as caught:
      await client.request(
        "POST", "/v1/contributions", body={},
        idempotency_key="mobius-pr:1234567890abcdef",
      )
    return caught.value

  error = asyncio.run(run())
  assert error.status_code == 429
  assert error.code == "quota"
  assert error.retry_after == 120
  assert "Daily limit" in error.detail


def test_contribution_broker_bounds_streamed_responses_before_buffering():
  async def handler(_request: httpx.Request):
    return httpx.Response(
      200,
      content=b"x" * (MAX_RESPONSE_BYTES + 1),
    )

  client = ContributionBrokerClient(transport=httpx.MockTransport(handler))

  async def run():
    with pytest.raises(ContributionBrokerError) as caught:
      await client.request(
        "GET", "/v1/contributions/ctr_1234567890abcdef1234567890abcdef",
      )
    return caught.value

  error = asyncio.run(run())
  assert error.status_code == 502
  assert "too large" in error.detail


def test_contribution_broker_rejects_oversized_requests_before_transport():
  touched = False

  async def handler(_request: httpx.Request):
    nonlocal touched
    touched = True
    return httpx.Response(200, json={})

  client = ContributionBrokerClient(transport=httpx.MockTransport(handler))

  async def run():
    with pytest.raises(ContributionBrokerError) as caught:
      await client.request(
        "POST", "/v1/contributions",
        body={"content": "x" * MAX_REQUEST_BYTES},
        idempotency_key="mobius-pr:1234567890abcdef",
      )
    return caught.value

  error = asyncio.run(run())
  assert error.status_code == 413
  assert error.code == "contribution_too_large"
  assert touched is False


def test_anonymous_relay_requires_an_explicit_target(monkeypatch):
  monkeypatch.delenv("MOBIUS_CONTRIBUTION_TARGET_REPO", raising=False)
  monkeypatch.delenv(
    "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES", raising=False,
  )

  with pytest.raises(relay_route.ContributionSubmitError) as caught:
    relay_route._configured_target_repo("mobius-os/mobius")
  assert caught.value.code == "relay_target_not_configured"


def test_anonymous_relay_rejects_non_mobius_repositories(monkeypatch):
  monkeypatch.setenv("MOBIUS_CONTRIBUTION_TARGET_REPO", "example/project")
  monkeypatch.delenv(
    "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES", raising=False,
  )

  with pytest.raises(relay_route.ContributionSubmitError) as caught:
    relay_route._configured_target_repo("mobius-os/mobius")

  assert caught.value.code == "anonymous_repo_not_allowed"


def test_explicit_test_repository_allows_safe_relay_proof(monkeypatch):
  monkeypatch.setenv("MOBIUS_CONTRIBUTION_TARGET_REPO", "example/safe-fork")
  monkeypatch.setenv(
    "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES",
    "another/repo, example/safe-fork",
  )

  assert relay_route._configured_target_repo("mobius-os/mobius") == (
    "example/safe-fork"
  )


def test_mobius_relay_only_accepts_anonymous_public_identity():
  assert relay_route.RelaySubmitIn.model_validate({
    "confirm_publication": True,
  }).public_identity == "anonymous"
  with pytest.raises(ValidationError):
    relay_route.RelaySubmitIn.model_validate({
      "confirm_publication": True,
      "public_identity": "github",
    })


def test_relay_result_stays_submitting_until_the_draft_url_arrives():
  merge = {
    "repo": "example/mobius",
    "source_repo": "mobius-os/mobius",
    "base_ref": "main",
    "base_sha": "a" * 40,
  }
  pending = relay_route._relay_result_patch(
    {"id": "ctr_1234567890abcdef1234567890abcdef", "status": "queued"}, merge=merge,
  )
  assert pending == {
    "relay_contribution_id": "ctr_1234567890abcdef1234567890abcdef",
    "relay_status": "queued",
    "relay_target_repo": "example/mobius",
    "relay_source_repo": "mobius-os/mobius",
    "last_submit_upstream_branch": "main",
    "last_submit_upstream_sha": "a" * 40,
    "status": "submitting",
  }

  opened = relay_route._relay_result_patch({
    "id": "ctr_1234567890abcdef1234567890abcdef",
    "status": "draft",
    "pr": {
      "url": "https://github.com/example/mobius/pull/123",
      "number": 123,
      "branch": "mobius/contribution-123",
      "head_sha": "b" * 40,
      "draft": True,
    },
  })
  assert opened["status"] == "draft"
  assert opened["url"].endswith("/pull/123")
  assert opened["relay_branch"] == "mobius/contribution-123"


def test_relay_result_tolerates_unpublished_pr_shape_and_tracks_revision():
  pending = relay_route._relay_result_patch({
    "id": "ctr_1234567890abcdef1234567890abcdef",
    "status": "publishing",
    "revision": 2,
    "publication_repo": "mobius-bot/mobius",
    "retryable": True,
    "pr": {"url": "", "number": None, "repo": "mobius-os/mobius"},
  })
  assert pending["status"] == "submitting"
  assert pending["relay_revision"] == 2
  assert pending["relay_publication_repo"] == "mobius-bot/mobius"
  assert pending["relay_retryable"] is True


def test_relay_result_rejects_a_different_revision():
  with pytest.raises(ContributionBrokerError, match="different revision"):
    relay_route._relay_result_patch({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": 2,
    }, expected_revision=1)


def test_relay_result_preserves_contribution_identity_across_revisions():
  with pytest.raises(ContributionBrokerError, match="different contribution identity"):
    relay_route._relay_result_patch(
      {"id": "ctr_other000", "status": "publishing"},
      contribution_id="ctr_1234567890abcdef1234567890abcdef",
    )


def test_request_revision_replays_exact_snapshot_and_advances_changed_snapshot():
  first_payload = {"contract_version": 1, "repo": "example/mobius"}
  first_revision, first_sha = relay_route._request_revision({}, first_payload)
  assert first_revision == 1

  exact_revision, exact_sha = relay_route._request_revision({
    "relay_revision": 1,
    "relay_request_sha256": first_sha,
  }, first_payload)
  assert (exact_revision, exact_sha) == (1, first_sha)

  changed_revision, changed_sha = relay_route._request_revision({
    "relay_revision": 1,
    "relay_request_sha256": first_sha,
  }, {**first_payload, "base_sha": "a" * 40})
  assert changed_revision == 2
  assert changed_sha != first_sha
  assert relay_route._idempotency_key(80, "change-1", 1) != (
    relay_route._idempotency_key(80, "change-1", 2)
  )


def test_relay_result_rejects_a_non_draft_pr():
  with pytest.raises(ContributionBrokerError, match="invalid draft"):
    relay_route._relay_result_patch({
      "id": "ctr_1234567890abcdef1234567890abcdef",
      "status": "open",
      "pr": {
        "url": "https://github.com/example/mobius/pull/123",
        "draft": False,
      },
    })


def test_relay_result_requires_positive_draft_confirmation():
  with pytest.raises(ContributionBrokerError, match="invalid draft"):
    relay_route._relay_result_patch({
      "id": _RELAY_ID,
      "status": "open",
      "pr": {"url": "https://github.com/example/mobius/pull/123"},
    })


def test_terminal_relay_result_advances_the_next_identical_revision():
  payload = {"contract_version": 1, "repo": "mobius-os/mobius"}
  revision, request_sha = relay_route._request_revision({}, payload)
  failed = relay_route._relay_result_patch({
    "id": _RELAY_ID,
    "status": "failed",
    "revision": revision,
    "error": {"message": "GitHub rejected the draft."},
  })

  assert failed["status"] == "prepared"
  assert failed["last_submit_error"] == "GitHub rejected the draft."
  assert failed["relay_request_sha256"] == ""
  next_revision, _next_sha = relay_route._request_revision({
    "relay_revision": revision,
    "relay_request_sha256": failed["relay_request_sha256"],
  }, payload)
  assert request_sha
  assert next_revision == revision + 1


def test_submit_route_records_nonretryable_broker_failure(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-nonretryable"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    raise ContributionBrokerError(400, "Rejected payload.", "invalid_payload")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 400, response.text
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "prepared"
  assert stored["last_submit_error"] == "Rejected payload."
  assert stored["last_submit_error_code"] == "invalid_payload"
  assert "submission_mode" not in stored


def test_submit_route_retries_terminal_result_as_a_new_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-terminal-retry"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  calls = []

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((body, idempotency_key))
    if len(calls) == 1:
      return ({
        "id": _RELAY_ID,
        "status": "failed",
        "revision": body["revision"],
        "error": {"message": "GitHub rejected the draft."},
      }, 200, {})
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"

  failed = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert failed.status_code == 200, failed.text
  first_record = failed.json()["record"]
  assert first_record["status"] == "prepared"
  assert first_record["last_submit_error"] == "GitHub rejected the draft."
  assert first_record["last_submit_error_code"] == "failed"

  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  assert retry.json()["record"]["status"] == "submitting"
  assert calls[0][0]["revision"] == 1
  assert calls[1][0]["revision"] == 2
  assert calls[0][1] != calls[1][1]
  assert json.loads(record_path.read_text())["relay_revision"] == 2


def test_submit_route_retries_one_lost_response_with_the_same_request(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-lost-response"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  calls = []
  witnessed = []

  async def record_equivalence(record):
    witnessed.append(record)

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    if len(calls) == 1:
      raise ContributionBrokerError(
        503, "The relay response was lost.", "relay_unavailable",
      )
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  monkeypatch.setattr(
    relay_route, "_record_relay_equivalence", record_equivalence,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"

  first = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert first.status_code == 503, first.text
  after_loss = json.loads(record_path.read_text())
  assert after_loss["status"] == "submitting"
  assert after_loss["submission_mode"] == "mobius-bot"
  assert after_loss["relay_revision"] == 1
  assert after_loss["last_submit_error_code"] == "relay_unavailable"

  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  submitted = retry.json()["record"]
  assert submitted["status"] == "submitting"
  assert submitted["relay_contribution_id"] == _RELAY_ID
  assert submitted["relay_revision"] == 1
  assert calls[0][2] == calls[1][2]
  assert calls[0][3] == calls[1][3]
  assert [record["id"] for record in witnessed] == [record_id]


def test_terminal_relay_equivalence_uses_the_verified_merge_commit(
  tmp_path, monkeypatch,
):
  source = tmp_path / "source"
  review = tmp_path / "review"
  source.mkdir()
  review.mkdir()
  settled = []
  merge_sha = "c" * 40
  monkeypatch.setattr(
    relay_route, "_equivalence_source_repo", lambda _record: (source, review),
  )
  monkeypatch.setattr(
    relay_route, "_merged_upstream_sha", lambda record, repo: (
      merge_sha if record["status"] == "merged" and repo == review else None
    ),
  )
  monkeypatch.setattr(
    relay_route, "_settle_equivalence",
    lambda record, upstream: settled.append((record["status"], upstream)),
  )

  asyncio.run(relay_route._settle_relay_equivalence({
    "id": "merged-relay", "status": "merged",
  }))
  asyncio.run(relay_route._settle_relay_equivalence({
    "id": "closed-relay", "status": "closed",
  }))

  assert settled == [("merged", merge_sha), ("closed", None)]


def test_status_route_rejects_a_different_relay_identity(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-status-identity"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
  })
  atomic_write(record_path, json.dumps(original))

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    assert method == "GET"
    assert path.endswith(_RELAY_ID)
    return ({"id": _OTHER_RELAY_ID, "status": "draft"}, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 502, response.text
  assert response.json()["detail"]["code"] == "invalid_relay_response"
  stored = json.loads(record_path.read_text())
  assert stored["relay_contribution_id"] == _RELAY_ID
  assert stored["status"] == "submitting"


def test_status_route_never_overwrites_a_newer_local_relay_identity(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-status-race"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
  })
  atomic_write(record_path, json.dumps(original))

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    assert method == "GET"
    assert path.endswith(_RELAY_ID)
    newer = json.loads(record_path.read_text())
    newer["relay_contribution_id"] = _OTHER_RELAY_ID
    newer["relay_revision"] = 2
    atomic_write(record_path, json.dumps(newer))
    return ({"id": _RELAY_ID, "status": "draft"}, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(record_path.read_text())
  assert stored["relay_contribution_id"] == _OTHER_RELAY_ID
  assert stored["relay_revision"] == 2


def test_withdraw_route_requires_confirmation_and_is_locally_idempotent(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-withdraw"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "draft",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 3,
    "relay_status": "draft",
  })
  atomic_write(record_path, json.dumps(original))
  calls = []
  settled = []

  async def settle_equivalence(record):
    settled.append(record["status"])

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": 3,
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  monkeypatch.setattr(
    relay_route, "_settle_relay_equivalence", settle_equivalence,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/withdraw"

  missing_confirmation = client.post(url, headers=headers, json={})
  assert missing_confirmation.status_code == 422
  assert calls == []

  withdrawn = client.post(url, headers=headers, json={
    "confirm_withdrawal": True,
  })
  assert withdrawn.status_code == 200, withdrawn.text
  assert withdrawn.json()["record"]["status"] == "closed"
  assert withdrawn.json()["record"]["relay_status"] == "withdrawn"
  assert settled == ["closed"]
  assert calls[0][2] == {
    "contract_version": 1,
    "revision": 3,
    "reason": "owner_withdrawn",
  }
  assert calls[0][3].startswith("mobius-withdraw:")

  duplicate = client.post(url, headers=headers, json={
    "confirm_withdrawal": True,
  })
  assert duplicate.status_code == 200, duplicate.text
  assert duplicate.json()["record"]["status"] == "closed"
  assert len(calls) == 1


def test_withdraw_route_never_reports_closed_before_relay_confirmation(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-withdraw-pending"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "draft",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
    "relay_status": "draft",
  })
  atomic_write(record_path, json.dumps(original))

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    return ({
      "id": _RELAY_ID,
      "status": "withdrawing",
      "revision": 1,
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/withdraw",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_withdrawal": True},
  )

  assert response.status_code == 502, response.text
  assert response.json()["detail"]["code"] == "invalid_relay_response"
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "draft"
  assert "withdrawn_at" not in stored
