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

from app import github_contributions
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
_REVIEWED_BASE = "a" * 40
_REVIEWED_HEAD = "c" * 40


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
      "title": "Reviewed relay change",
      "body_draft": "## What\n\nA reviewed relay change.",
      "base_sha": _REVIEWED_BASE,
      "head_sha": _REVIEWED_HEAD,
    },
    "quality_review": {
      "state": "all_clear",
      "reviewed_head_sha": _REVIEWED_HEAD,
      "reviewed_at": "2026-08-20T12:00:00Z",
    },
  })
  return app_id, record_path


def _sign_relay_attempt(app_id, record_id, record):
  """Give a synthetic top-level relay attempt its real server witnesses."""
  signed = {**record}
  revision = int(signed.get("relay_revision") or 1)
  signed.setdefault("relay_revision", revision)
  signed.setdefault("relay_request_sha256", "a" * 64)
  signed.setdefault(
    "relay_idempotency_key",
    relay_route._idempotency_key(app_id, record_id, revision),
  )
  signed.setdefault("relay_payload_sha256", "b" * 64)
  signed["relay_attempt_input_sha256"] = relay_route._relay_input_fingerprint(
    signed
  )
  signed["relay_owner_claim_sha256"] = relay_route._owner_claim_witness(
    app_id, record_id, signed["relay_attempt_input_sha256"],
  )
  signed["relay_attempt_witness_sha256"] = relay_route._attempt_witness(
    app_id, record_id, signed,
  )
  if signed.get("relay_contribution_id"):
    signed["relay_result_witness_sha256"] = relay_route._result_witness(
      app_id, record_id, signed,
    )
  return signed


def _legacy_accepted_relay_record(app_id, record_id, record_path):
  """Write the exact accepted-record shape released before HMAC journals."""
  legacy = json.loads(record_path.read_text())
  legacy.update({
    "status": "draft",
    "submission_mode": "mobius-bot",
    "public_identity": "anonymous",
    "relay_contribution_id": _RELAY_ID,
    "relay_status": "draft",
    "relay_revision": 1,
    "relay_request_sha256": "a" * 64,
    "relay_idempotency_key": relay_route._idempotency_key(
      app_id, record_id, 1,
    ),
    "relay_target_repo": "mobius-os/mobius",
    "relay_source_repo": "mobius-os/mobius",
  })
  atomic_write(record_path, json.dumps(legacy))
  return legacy


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


def _allow_synthetic_source_provenance(monkeypatch, tmp_path):
  """Give deliberately fake relay fixtures an explicit source-proof seam."""
  source = tmp_path / "installed-source"
  monkeypatch.setattr(
    relay_route,
    "_equivalence_source_repo",
    lambda _record: (source, tmp_path),
  )
  monkeypatch.setattr(
    relay_route,
    "_assert_pending_equivalence_preflight",
    lambda _record: "exact_tree",
  )


def _create_detached_relay_attempt(
  client, owner_token, tmp_path, monkeypatch, record_id,
):
  """Exercise the real drift/retry path and return its signed settlement."""
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)

  async def accept_while_inputs_drift(
    method, path, *, body=None, idempotency_key=None,
  ):
    assert method == "POST"
    changed = json.loads(record_path.read_text())
    changed["title"] = "Changed detached review"
    changed["plan"]["head_sha"] = "d" * 40
    atomic_write(record_path, json.dumps(changed))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", accept_while_inputs_drift,
  )
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  first = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert first.status_code == 409, first.text
  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  assert "relay_attempt_settlement" in retry.json()["record"]
  return app_id, record_path


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


def test_anonymous_relay_rejects_a_different_mobius_target(monkeypatch):
  monkeypatch.setenv(
    "MOBIUS_CONTRIBUTION_TARGET_REPO", "mobius-os/app-other",
  )
  monkeypatch.delenv(
    "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES", raising=False,
  )

  with pytest.raises(relay_route.ContributionSubmitError) as caught:
    relay_route._configured_target_repo("mobius-os/mobius")

  assert caught.value.code == "relay_target_mismatch"


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
  with pytest.raises(ContributionBrokerError, match="invalid pull request state"):
    relay_route._relay_result_patch({
      "id": "ctr_1234567890abcdef1234567890abcdef",
      "status": "open",
      "pr": {
        "url": "https://github.com/example/mobius/pull/123",
        "draft": False,
      },
    })


def test_relay_result_requires_positive_draft_confirmation():
  with pytest.raises(ContributionBrokerError, match="invalid pull request state"):
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


def test_relay_snapshot_uses_plan_metadata_exactly_and_ignores_card_text(
  tmp_path, monkeypatch,
):
  exact_files = [{
    "path": "notes.txt",
    "operation": "modify",
    "mode": "100644",
    "content_base64": base64.b64encode(b"exact\x00bytes\n").decode(),
  }]
  monkeypatch.setattr(
    relay_route,
    "_merged_snapshot",
    lambda *_args: ({
      "repo": "mobius-os/mobius",
      "source_repo": "mobius-os/mobius",
      "base_ref": "main",
      "base_sha": "a" * 40,
      "expected_tree_sha": "b" * 40,
    }, exact_files),
  )
  title = "Tačan pregled – Živio 🧪"
  body = "Tačno tijelo 🧪\r\n\nTrailing reviewed space: "
  _merge, payload = relay_route._relay_snapshot_payload({
    "title": "Private card title",
    "description": "Private card description",
    "summary": "Private card summary",
    "plan": {"title": title, "body_draft": body},
  }, tmp_path / "review.diff", "exact-review")

  assert payload["title"] == title
  assert payload["title"].encode("utf-8") == title.encode("utf-8")
  assert payload["commit_message"] == title
  assert payload["body"] == body
  assert payload["body"].encode("utf-8") == body.encode("utf-8")
  assert payload["files"] is exact_files


@pytest.mark.parametrize(
  ("record", "code"),
  [
    ({"plan": {"title": "   ", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": " title", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title ", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title\nsecond", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title\rsecond", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title\x00second", "body_draft": "body"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "x" * 257, "body_draft": "body"}},
     "review_changed_large_diff"),
    ({"plan": {"title": "title", "body_draft": "\n\t"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title", "body_draft": "body\x00tail"}},
     "relay_review_text_invalid"),
    ({"plan": {"title": "title", "body_draft": "x" * 65_537}},
     "review_changed_large_diff"),
    ({"plan": {"title": "title", "body_draft": "🧪" * 16_385}},
     "review_changed_large_diff"),
  ],
)
def test_relay_snapshot_rejects_invalid_or_oversized_reviewed_text(
  tmp_path, monkeypatch, record, code,
):
  monkeypatch.setattr(
    relay_route,
    "_merged_snapshot",
    lambda *_args: ({
      "repo": "mobius-os/mobius",
      "source_repo": "mobius-os/mobius",
      "base_ref": "main",
      "base_sha": "a" * 40,
      "expected_tree_sha": "b" * 40,
    }, [{
      "path": "notes.txt",
      "operation": "modify",
      "mode": "100644",
      "content_base64": "eA==",
    }]),
  )

  with pytest.raises(relay_route.ContributionSubmitError) as caught:
    relay_route._relay_snapshot_payload(
      record, tmp_path / "review.diff", "invalid-review",
    )
  assert caught.value.code == code


@pytest.mark.parametrize(
  "missing",
  ["plan", "title", "body_draft"],
)
def test_submit_rejects_missing_plan_metadata_before_claim_or_broker(
  client, owner_token, tmp_path, monkeypatch, missing,
):
  record_id = f"relay-missing-reviewed-{missing.replace('_', '-')}"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  record = json.loads(record_path.read_text())
  if missing == "plan":
    record.pop("plan")
  else:
    record["plan"].pop(missing)
  atomic_write(record_path, json.dumps(record))
  broker_calls = []

  async def broker_must_not_run(*args, **kwargs):
    broker_calls.append((args, kwargs))
    raise AssertionError("broker called for incomplete reviewed metadata")

  def snapshot_must_not_run(*_args):
    raise AssertionError("snapshot built before reviewed metadata validation")

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", broker_must_not_run,
  )
  monkeypatch.setattr(relay_route, "_merged_snapshot", snapshot_must_not_run)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "relay_review_text_invalid"
  assert json.loads(record_path.read_text()) == record
  assert broker_calls == []
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_oversized_exact_relay_request_is_rejected_before_private_write():
  payload = {
    "repo": "mobius-os/mobius",
    "files": [{"content_base64": "x" * MAX_REQUEST_BYTES}],
  }

  with pytest.raises(relay_route.ContributionSubmitError) as caught:
    relay_route._write_relay_request(
      7, "oversized-request", {"repo": "mobius-os/mobius"}, payload,
    )
  assert caught.value.code == "review_changed_large_diff"
  assert not relay_route._relay_request_path(7, "oversized-request").exists()


def test_hidden_claim_receipt_rejects_cross_record_app_forgery_and_deletion(
  tmp_path,
):
  record_path = tmp_path / "receipt-a.json"
  claimed = {
    "id": "receipt-a",
    "type": "pr",
    "status": "submitting",
    "repo": "mobius-os/mobius",
    "branch": "fix/receipt",
    "plan": {"action": "pr", "repo": "mobius-os/mobius"},
    "quality_review": {"state": "all_clear"},
    "submission_mode": "mobius-bot",
    "public_identity": "anonymous",
  }
  claimed["relay_attempt_input_sha256"] = relay_route._relay_input_fingerprint(
    claimed
  )
  claimed["relay_owner_claim_sha256"] = relay_route._owner_claim_witness(
    7, "receipt-a", claimed["relay_attempt_input_sha256"],
  )
  relay_route._arm_claim_receipt(
    app_id=7, record_id="receipt-a", claimed=claimed,
  )
  receipt_path = relay_route._relay_claim_path(7, "receipt-a")
  signed_bytes = receipt_path.read_bytes()
  assert relay_route._read_claim_receipt(
    app_id=7, record_id="receipt-a",
  ) is not None
  assert relay_route._read_claim_receipt(
    app_id=8, record_id="receipt-a",
  ) is None
  assert relay_route._read_claim_receipt(
    app_id=7, record_id="receipt-b",
  ) is None

  atomic_write(
    relay_route._relay_claim_path(7, "receipt-b", create_parent=True),
    signed_bytes,
  )
  assert relay_route._read_claim_receipt(
    app_id=7, record_id="receipt-b",
  ) is None

  forged = json.loads(signed_bytes)
  forged["next_revision"] = 99
  atomic_write(receipt_path, json.dumps(forged))
  assert relay_route._read_claim_receipt(
    app_id=7, record_id="receipt-a",
  ) is None

  atomic_write(receipt_path, signed_bytes)
  receipt_path.unlink()
  with pytest.raises(
    relay_route.ContributionSubmitError,
    match="no valid one-shot receipt",
  ):
    relay_route._require_claim_receipt(
      app_id=7, record_id="receipt-a", claimed=claimed,
    )

  # The old record-adjacent location is app-writable and is never a fallback.
  adjacent = record_path.with_name(f".{record_path.stem}.relay-claim.json")
  atomic_write(adjacent, signed_bytes)
  assert relay_route._read_claim_receipt(
    app_id=7, record_id="receipt-a",
  ) is None


def test_record_adjacent_request_blob_is_never_a_private_retry_capability(
  client, owner_token, tmp_path,
):
  record_id = "relay-adjacent-request-forgery"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  envelope = {
    "version": 1,
    "merge": {"repo": "mobius-os/mobius"},
    "payload": {"revision": 1},
  }
  adjacent = record_path.with_name(f".{record_path.stem}.relay-request.json")
  atomic_write(adjacent, canonical_body(envelope))

  with pytest.raises(
    relay_route.ContributionSubmitError,
    match="missing its private request",
  ):
    relay_route._read_relay_request(
      app_id, record_id, relay_route._relay_request_sha(envelope),
    )
  assert adjacent.is_file()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_claim_crash_before_single_write_retries_without_broker_duplication(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-claim-crash-before-write"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  broker_calls = []

  async def accept_once(method, path, *, body=None, idempotency_key=None):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_once)
  real_write = github_contributions._write_record

  def crash_before_write(_path, _record):
    raise RuntimeError("crash before claim write")

  monkeypatch.setattr(
    github_contributions, "_write_record", crash_before_write,
  )
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  with pytest.raises(RuntimeError, match="crash before claim write"):
    client.post(url, headers=headers, json={"confirm_publication": True})

  unchanged = json.loads(record_path.read_text())
  assert unchanged["status"] == "prepared"
  assert "submission_mode" not in unchanged
  assert "relay_owner_claim_sha256" not in unchanged
  assert relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  ) is not None
  assert broker_calls == []

  monkeypatch.setattr(github_contributions, "_write_record", real_write)
  retry = client.post(url, headers=headers, json={"confirm_publication": True})
  assert retry.status_code == 200, retry.text
  assert len(broker_calls) == 1
  assert not relay_route._relay_claim_path(app_id, record_id).exists()


def test_changed_review_replaces_only_an_orphaned_armed_claim_receipt(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-orphaned-armed-claim"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  real_write = github_contributions._write_record

  def crash_before_claim_record(_path, _record):
    raise RuntimeError("claim record was not durable")

  monkeypatch.setattr(
    github_contributions, "_write_record", crash_before_claim_record,
  )
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  with pytest.raises(RuntimeError, match="claim record was not durable"):
    client.post(url, headers=headers, json={"confirm_publication": True})

  orphan = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert orphan is not None
  assert orphan["phase"] == "armed"
  unchanged = json.loads(record_path.read_text())
  assert unchanged["status"] == "prepared"

  changed = {
    **unchanged,
    "title": "Changed after the pre-record crash",
    "plan": {
      **unchanged["plan"],
      "title": "Changed reviewed public title after the crash",
      "body_draft": "Changed reviewed body after the crash.",
    },
  }
  atomic_write(record_path, json.dumps(changed))
  calls = []

  async def accept_changed(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(github_contributions, "_write_record", real_write)
  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_changed)
  retried = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )

  assert retried.status_code == 200, retried.text
  assert len(calls) == 1
  assert calls[0][2]["title"] == changed["plan"]["title"]
  assert calls[0][2]["body"] == changed["plan"]["body_draft"]
  assert calls[0][2]["revision"] == 1
  assert not relay_route._relay_claim_path(app_id, record_id).exists()


def test_claim_crash_after_single_write_resumes_without_broker_duplication(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-claim-crash-after-write"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  broker_calls = []

  async def accept_once(method, path, *, body=None, idempotency_key=None):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_once)
  real_claim = relay_route._claim_record

  def crash_after_write(**kwargs):
    real_claim(**kwargs)
    raise RuntimeError("crash after claim write")

  monkeypatch.setattr(relay_route, "_claim_record", crash_after_write)
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  with pytest.raises(RuntimeError, match="crash after claim write"):
    client.post(url, headers=headers, json={"confirm_publication": True})

  claimed = json.loads(record_path.read_text())
  assert claimed["status"] == "submitting"
  assert claimed["submission_mode"] == "mobius-bot"
  assert claimed["public_identity"] == "anonymous"
  assert claimed["relay_attempt_input_sha256"] == (
    relay_route._relay_input_fingerprint(claimed)
  )
  assert claimed["relay_owner_claim_sha256"] == relay_route._owner_claim_witness(
    app_id, record_id, claimed["relay_attempt_input_sha256"],
  )
  assert relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  ) is not None
  assert broker_calls == []

  monkeypatch.setattr(relay_route, "_claim_record", real_claim)
  retry = client.post(url, headers=headers, json={"confirm_publication": True})
  assert retry.status_code == 200, retry.text
  assert len(broker_calls) == 1
  assert not relay_route._relay_claim_path(app_id, record_id).exists()


def test_settled_claim_json_replay_without_hidden_receipt_cannot_publish(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-settled-claim-replay"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  real_claim = relay_route._claim_record
  captured_claim = {}

  def capture_then_crash(**kwargs):
    real_claim(**kwargs)
    captured_claim.update(json.loads(record_path.read_text()))
    raise RuntimeError("capture signed pre-journal claim")

  monkeypatch.setattr(relay_route, "_claim_record", capture_then_crash)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  with pytest.raises(RuntimeError, match="capture signed pre-journal claim"):
    client.post(url, headers=headers, json={"confirm_publication": True})
  assert relay_route._relay_claim_path(app_id, record_id).is_file()
  assert "relay_attempt_witness_sha256" not in captured_claim

  broker_calls = []

  async def accept_once(method, path, *, body=None, idempotency_key=None):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route, "_claim_record", real_claim)
  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_once)
  settled = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert settled.status_code == 200, settled.text
  assert len(broker_calls) == 1
  assert not relay_route._relay_claim_path(app_id, record_id).exists()

  # Replaying the still-valid app JSON claim cannot recreate the consumed
  # server-owned one-shot capability.
  atomic_write(record_path, json.dumps(captured_claim))

  async def fail_replayed_broker(*_args, **_kwargs):
    pytest.fail("a consumed claim receipt must never mint another request")

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", fail_replayed_broker,
  )
  replay = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert replay.status_code == 409, replay.text
  assert replay.json()["detail"]["code"] == "relay_claim_receipt_invalid"


def test_claim_receipt_must_reach_broker_ready_before_the_broker_request(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-claim-retirement-fails-closed"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  broker_calls = []

  async def accept_once(method, path, *, body=None, idempotency_key=None):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_once)
  receipt_path = relay_route._relay_claim_path(app_id, record_id)
  real_write_receipt = relay_route._write_claim_receipt

  def refuse_broker_ready(app_id, record_id, receipt):
    if receipt.get("phase") == "broker_ready":
      raise relay_route.ContributionSubmitError(
        "broker-ready phase is not durable",
        code="relay_claim_receipt_invalid",
      )
    return real_write_receipt(app_id, record_id, receipt)

  monkeypatch.setattr(
    relay_route, "_write_claim_receipt", refuse_broker_ready,
  )
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  blocked = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })

  assert blocked.status_code == 409, blocked.text
  assert blocked.json()["detail"]["code"] == "relay_claim_receipt_invalid"
  assert broker_calls == []
  assert json.loads(record_path.read_text())["status"] == "prepared"
  assert not receipt_path.exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()

  # Once the private phase transition works, a new owner-confirmed claim can
  # proceed; the failed pre-broker journal is never replayed.
  monkeypatch.setattr(
    relay_route, "_write_claim_receipt", real_write_receipt,
  )
  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  assert len(broker_calls) == 1
  assert not receipt_path.exists()


def test_journaled_crash_requires_a_new_two_proof_attempt_before_broker(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-journaled-before-second-proof"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  proofs = []
  broker_calls = []

  def prove_source(_record):
    proofs.append("proof")

  async def accept_once(method, path, *, body=None, idempotency_key=None):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route, "_assert_pending_equivalence_preflight", prove_source,
  )
  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_once)
  real_advance = relay_route._advance_claim_receipt
  crashed = False

  def crash_after_journaled_phase(**kwargs):
    nonlocal crashed
    receipt = real_advance(**kwargs)
    if kwargs["to_phase"] == "journaled" and not crashed:
      crashed = True
      raise RuntimeError("crash before the second source proof")
    return receipt

  monkeypatch.setattr(
    relay_route, "_advance_claim_receipt", crash_after_journaled_phase,
  )
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  with pytest.raises(RuntimeError, match="before the second source proof"):
    client.post(url, headers=headers, json={"confirm_publication": True})

  journaled = json.loads(record_path.read_text())
  receipt = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert journaled["status"] == "submitting"
  assert journaled["relay_revision"] == 1
  assert receipt is not None
  assert receipt["phase"] == "journaled"
  assert relay_route._relay_request_path(app_id, record_id).is_file()
  assert proofs == ["proof"]
  assert broker_calls == []

  monkeypatch.setattr(relay_route, "_advance_claim_receipt", real_advance)
  recovered = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )
  assert recovered.status_code == 409, recovered.text
  assert recovered.json()["detail"]["code"] == "relay_prebroker_recovered"
  prepared = json.loads(record_path.read_text())
  assert prepared["status"] == "prepared"
  assert prepared["relay_revision"] == 1
  assert not relay_route._relay_request_path(app_id, record_id).exists()
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert proofs == ["proof"]
  assert broker_calls == []

  retried = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )
  assert retried.status_code == 200, retried.text
  assert retried.json()["record"]["relay_revision"] == 2
  assert proofs == ["proof", "proof", "proof"]
  assert len(broker_calls) == 1
  assert broker_calls[0][2]["revision"] == 2


def test_terminal_retry_crash_after_claim_resumes_next_exact_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-terminal-retry-claim-crash"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def terminal_then_accept(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((body, idempotency_key))
    if len(calls) == 1:
      return ({
        "id": _RELAY_ID,
        "status": "failed",
        "revision": body["revision"],
        "error": {"message": "terminal"},
      }, 200, {})
    return ({
      "id": _OTHER_RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", terminal_then_accept,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  first = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert first.status_code == 200, first.text
  assert first.json()["record"]["status"] == "prepared"
  assert calls[0][0]["revision"] == 1

  real_claim = relay_route._claim_record

  def crash_after_terminal_retry_claim(**kwargs):
    real_claim(**kwargs)
    raise RuntimeError("crash after terminal retry claim")

  monkeypatch.setattr(
    relay_route, "_claim_record", crash_after_terminal_retry_claim,
  )
  with pytest.raises(RuntimeError, match="crash after terminal retry claim"):
    client.post(url, headers=headers, json={"confirm_publication": True})
  claimed = json.loads(record_path.read_text())
  assert claimed["status"] == "submitting"
  assert claimed["relay_revision"] == 1
  assert "relay_request_sha256" not in claimed
  receipt = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert receipt is not None
  assert receipt["next_revision"] == 2
  assert len(calls) == 1

  monkeypatch.setattr(relay_route, "_claim_record", real_claim)
  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  assert retry.json()["record"]["relay_revision"] == 2
  assert calls[1][0]["revision"] == 2
  assert not relay_route._relay_claim_path(app_id, record_id).exists()


def test_submit_route_rejects_missing_source_before_snapshot_or_broker(
  client, owner_token, tmp_path, monkeypatch,
):
  """The relay cannot bypass the authoritative installed-source guard."""
  record_id = "relay-missing-source"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  monkeypatch.setattr(relay_route, "_safe_repo_path", lambda _raw: tmp_path)
  monkeypatch.setattr(
    relay_route, "_equivalence_source_repo", lambda _record: None,
  )

  def reject_missing_source(_record):
    raise relay_route.ContributionSubmitError(
      "This review is missing its installed source provenance.",
      code="missing_source_provenance",
    )

  monkeypatch.setattr(
    relay_route, "_assert_pending_equivalence_preflight", reject_missing_source,
  )
  monkeypatch.setattr(
    relay_route,
    "_merged_snapshot",
    lambda *_args: pytest.fail("missing provenance must stop before snapshot"),
  )

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("missing provenance must stop before the broker request")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "missing_source_provenance"
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "prepared"
  assert "submission_mode" not in stored
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_prejournal_record_drift_retires_unused_claim_and_restores_review(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-prejournal-record-drift"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  proof_calls = []

  def drift_during_first_source_proof(_record):
    proof_calls.append("proof")
    changed = json.loads(record_path.read_text())
    changed["title"] = "Changed before relay journaling"
    changed["plan"]["body_draft"] = "Changed reviewed body"
    atomic_write(record_path, json.dumps(changed))
    return "exact_tree"

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("pre-journal drift must stop before any broker request")

  monkeypatch.setattr(
    relay_route,
    "_assert_pending_equivalence_preflight",
    drift_during_first_source_proof,
  )
  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "relay_prejournal_changed"
  assert proof_calls == ["proof"]
  restored = json.loads(record_path.read_text())
  assert restored["status"] == "prepared"
  assert restored["title"] == "Changed before relay journaling"
  assert restored["plan"]["body_draft"] == "Changed reviewed body"
  assert restored["last_submit_error_code"] == "relay_prejournal_changed"
  assert "submission_mode" not in restored
  assert "relay_attempt_input_sha256" not in restored
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_failing_first_source_proof_restores_concurrent_review_drift(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-prejournal-proof-failure-drift"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  monkeypatch.setattr(
    relay_route,
    "_equivalence_source_repo",
    lambda _record: (tmp_path / "installed-source", tmp_path),
  )

  def drift_then_fail(_record):
    changed = json.loads(record_path.read_text())
    changed["title"] = "Changed while the first proof failed"
    changed["plan"]["body_draft"] = "Changed while proving source."
    atomic_write(record_path, json.dumps(changed))
    raise relay_route.ContributionSubmitError(
      "The installed source changed during proof.",
      code="source_provenance_changed",
    )

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a failed first source proof cannot reach the broker")

  monkeypatch.setattr(
    relay_route, "_assert_pending_equivalence_preflight", drift_then_fail,
  )
  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "relay_prejournal_changed"
  restored = json.loads(record_path.read_text())
  assert restored["status"] == "prepared"
  assert restored["title"] == "Changed while the first proof failed"
  assert restored["plan"]["body_draft"] == "Changed while proving source."
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_retired_prejournal_phase_recovers_a_crash_before_record_restore(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-retired-restore-crash"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  real_claim = relay_route._claim_record

  def crash_after_claim_write(**kwargs):
    real_claim(**kwargs)
    raise RuntimeError("capture prejournal claim")

  monkeypatch.setattr(relay_route, "_claim_record", crash_after_claim_write)
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  headers = {"Authorization": f"Bearer {owner_token}"}
  with pytest.raises(RuntimeError, match="capture prejournal claim"):
    client.post(url, headers=headers, json={"confirm_publication": True})

  changed = json.loads(record_path.read_text())
  changed["title"] = "Changed before restore crashed"
  atomic_write(record_path, json.dumps(changed))
  claimed = json.loads(record_path.read_text())
  real_route_write = relay_route.write_record

  def crash_on_prepared_restore(path, record):
    if (
      record.get("status") == "prepared"
      and record.get("last_submit_error_code") == "relay_prejournal_changed"
    ):
      raise RuntimeError("restore record was not durable")
    real_route_write(path, record)

  monkeypatch.setattr(relay_route, "write_record", crash_on_prepared_restore)
  with pytest.raises(RuntimeError, match="restore record was not durable"):
    relay_route._restore_prejournal_drift(
      app_id=app_id,
      record_id=record_id,
      record_path=record_path,
      claimed=claimed,
      changed=claimed,
    )

  retired = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert retired is not None
  assert retired["phase"] == "retired"
  assert json.loads(record_path.read_text())["status"] == "submitting"

  monkeypatch.setattr(relay_route, "_claim_record", real_claim)
  monkeypatch.setattr(relay_route, "write_record", real_route_write)

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a retired prejournal claim cannot reach the broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  recovered = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )

  assert recovered.status_code == 409, recovered.text
  assert recovered.json()["detail"]["code"] == "relay_prejournal_changed"
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "prepared"
  assert stored["title"] == "Changed before restore crashed"
  assert not relay_route._relay_claim_path(app_id, record_id).exists()


def test_submit_route_releases_storage_lock_during_source_proof_and_broker(
  client, owner_token, tmp_path, monkeypatch,
):
  """The relay re-proves source without blocking unrelated app storage I/O."""
  record_id = "relay-publication-locks"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  source = tmp_path / "installed-source"
  app_lock = relay_route.fs_locks.app_storage_lock(app_id)
  review_lock = relay_route.fs_locks.source_dir_lock(str(tmp_path))
  source_lock = relay_route.fs_locks.source_dir_lock(str(source))
  phases = []

  def assert_provenance(_record):
    assert not app_lock.locked()
    assert review_lock.locked()
    assert source_lock.locked()
    phases.append("provenance")
    return "exact_tree"

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    assert method == "POST"
    assert path == relay_route.CONTRIBUTION_PREFIX
    assert not app_lock.locked()
    assert review_lock.locked()
    assert source_lock.locked()
    attempted = json.loads(record_path.read_text())
    assert attempted["relay_revision"] == body["revision"]
    assert attempted["relay_idempotency_key"] == idempotency_key
    phases.append("broker")
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  async def ignore_witness(_record):
    return None

  monkeypatch.setattr(
    relay_route, "_assert_pending_equivalence_preflight", assert_provenance,
  )
  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  monkeypatch.setattr(
    relay_route, "_record_relay_equivalence", ignore_witness,
  )
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 200, response.text
  assert phases == ["provenance", "provenance", "broker"]
  assert not app_lock.locked()
  assert not review_lock.locked()
  assert not source_lock.locked()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_submit_route_records_nonretryable_broker_failure(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-nonretryable"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((body, idempotency_key))
    if len(calls) == 1:
      raise ContributionBrokerError(400, "Rejected payload.", "invalid_payload")
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

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
  assert stored["relay_revision"] == 1
  assert not relay_route._relay_request_path(app_id, record_id).exists()

  retried = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )
  assert retried.status_code == 200, retried.text
  assert calls[0][0]["revision"] == 1
  assert calls[1][0]["revision"] == 2
  assert calls[0][1] != calls[1][1]


def test_definitive_broker_failure_write_crash_recovers_at_next_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-definitive-failure-write-crash"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def reject_then_accept(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((body, idempotency_key))
    if len(calls) == 1:
      raise ContributionBrokerError(
        400, "Rejected payload.", "invalid_payload",
      )
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", reject_then_accept,
  )
  real_write = relay_route.write_record
  crashed = False

  def crash_after_definitive_failure_write(path, record):
    nonlocal crashed
    real_write(path, record)
    if (
      not crashed
      and record.get("status") == "prepared"
      and record.get("last_submit_error_code") == "invalid_payload"
    ):
      crashed = True
      raise RuntimeError("crash after definitive failure record write")

  monkeypatch.setattr(
    relay_route, "write_record", crash_after_definitive_failure_write,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  with pytest.raises(
    RuntimeError, match="crash after definitive failure record write",
  ):
    client.post(url, headers=headers, json={"confirm_publication": True})

  interrupted = json.loads(record_path.read_text())
  receipt = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert interrupted["status"] == "prepared"
  assert interrupted["relay_revision"] == 1
  assert "submission_mode" not in interrupted
  assert receipt is not None
  assert receipt["phase"] == "rejected"
  assert relay_route._relay_request_path(app_id, record_id).is_file()
  assert len(calls) == 1

  monkeypatch.setattr(relay_route, "write_record", real_write)
  retried = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )

  assert retried.status_code == 200, retried.text
  assert retried.json()["record"]["relay_revision"] == 2
  assert [call[0]["revision"] for call in calls] == [1, 2]
  assert calls[0][1] != calls[1][1]
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_submit_route_retries_terminal_result_as_a_new_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-terminal-retry"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
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
  assert not relay_route._relay_request_path(app_id, record_id).exists()

  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert retry.status_code == 200, retry.text
  assert retry.json()["record"]["status"] == "submitting"
  assert calls[0][0]["revision"] == 1
  assert calls[1][0]["revision"] == 2
  assert calls[0][1] != calls[1][1]
  assert json.loads(record_path.read_text())["relay_revision"] == 2


def test_terminal_result_write_crash_recovers_at_next_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-terminal-result-write-crash"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def fail_then_accept(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((body, idempotency_key))
    if len(calls) == 1:
      return ({
        "id": _RELAY_ID,
        "status": "failed",
        "revision": body["revision"],
        "error": {"message": "GitHub rejected the draft."},
      }, 200, {})
    return ({
      "id": _OTHER_RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", fail_then_accept,
  )
  real_write = relay_route.write_record
  crashed = False

  def crash_after_terminal_result_write(path, record):
    nonlocal crashed
    real_write(path, record)
    if (
      not crashed
      and record.get("status") == "prepared"
      and record.get("relay_status") == "failed"
    ):
      crashed = True
      raise RuntimeError("crash after terminal result record write")

  monkeypatch.setattr(
    relay_route, "write_record", crash_after_terminal_result_write,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  with pytest.raises(
    RuntimeError, match="crash after terminal result record write",
  ):
    client.post(url, headers=headers, json={"confirm_publication": True})

  interrupted = json.loads(record_path.read_text())
  receipt = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert interrupted["status"] == "prepared"
  assert interrupted["relay_status"] == "failed"
  assert interrupted["relay_revision"] == 1
  assert receipt is not None
  assert receipt["phase"] == "rejected"
  assert relay_route._relay_request_path(app_id, record_id).is_file()
  assert len(calls) == 1

  monkeypatch.setattr(relay_route, "write_record", real_write)
  retried = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )

  assert retried.status_code == 200, retried.text
  assert retried.json()["record"]["relay_revision"] == 2
  assert [call[0]["revision"] for call in calls] == [1, 2]
  assert calls[0][1] != calls[1][1]
  assert not relay_route._relay_claim_path(app_id, record_id).exists()
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_terminal_result_tombstone_failure_retains_exact_retry_capability(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-terminal-tombstone-failure"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def terminal_until_new_revision(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((body, idempotency_key))
    if body["revision"] == 1:
      return ({
        "id": _RELAY_ID,
        "status": "failed",
        "revision": 1,
        "error": {"message": "GitHub rejected the draft."},
      }, 200, {})
    return ({
      "id": _OTHER_RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker,
    "request",
    terminal_until_new_revision,
  )
  real_mark_rejected = relay_route._mark_relay_claim_rejected
  tombstone_attempts = 0

  def fail_first_tombstone(**kwargs):
    nonlocal tombstone_attempts
    tombstone_attempts += 1
    if tombstone_attempts == 1:
      raise relay_route.ContributionSubmitError(
        "The private tombstone write failed.",
        code="relay_claim_receipt_invalid",
      )
    return real_mark_rejected(**kwargs)

  monkeypatch.setattr(
    relay_route, "_mark_relay_claim_rejected", fail_first_tombstone,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"

  interrupted = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )
  assert interrupted.status_code == 409, interrupted.text
  ambiguous = json.loads(record_path.read_text())
  receipt = relay_route._read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  assert ambiguous["status"] == "submitting"
  assert ambiguous["relay_revision"] == 1
  assert relay_route._relay_request_path(app_id, record_id).is_file()
  assert receipt is not None
  assert receipt["phase"] == "broker_ready"

  settled = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )
  assert settled.status_code == 200, settled.text
  assert settled.json()["record"]["status"] == "prepared"
  assert [call[0]["revision"] for call in calls] == [1, 1]
  assert calls[0][1] == calls[1][1]

  retried = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )
  assert retried.status_code == 200, retried.text
  assert retried.json()["record"]["relay_revision"] == 2
  assert [call[0]["revision"] for call in calls] == [1, 1, 2]
  assert calls[2][1] != calls[1][1]


def test_submit_route_retries_one_lost_response_with_the_same_request(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-lost-response"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []
  witnessed = []
  provenance_checks = []

  def assert_first_publication_source(_record):
    provenance_checks.append("checked")
    return "exact_tree"

  monkeypatch.setattr(
    relay_route,
    "_assert_pending_equivalence_preflight",
    assert_first_publication_source,
  )

  async def record_equivalence(record):
    witnessed.append(record)

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    assert not relay_route.fs_locks.app_storage_lock(app_id).locked()
    calls.append((method, path, body, idempotency_key))
    if method == "GET":
      return ({
        "id": _RELAY_ID,
        "status": "draft",
        "revision": 1,
      }, 200, {})
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
  assert relay_route._relay_request_path(app_id, record_id).is_file()

  def fail_if_rechecked(_record):
    pytest.fail("an exact saved relay retry must reconcile, not republish")

  monkeypatch.setattr(
    relay_route,
    "_assert_pending_equivalence_preflight",
    fail_if_rechecked,
  )

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
  assert provenance_checks == ["checked", "checked"]
  assert not relay_route._relay_request_path(app_id, record_id).exists()

  status = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers=headers,
  )
  assert status.status_code == 200, status.text
  assert status.json()["record"]["relay_status"] == "draft"
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_exact_relay_retry_replays_private_request_after_upstream_moves(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-lost-response-upstream-moved"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((body, idempotency_key))
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
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  first = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })
  assert first.status_code == 503, first.text
  monkeypatch.setattr(
    relay_route,
    "_merged_snapshot",
    lambda *_args: pytest.fail(
      "exact retry must replay private bytes, not rebuild against upstream"
    ),
  )
  monkeypatch.setattr(
    relay_route,
    "_assert_pending_equivalence_preflight",
    lambda _record: pytest.fail("exact retry must not recheck current source"),
  )
  retry = client.post(url, headers=headers, json={
    "confirm_publication": True,
  })

  assert retry.status_code == 200, retry.text
  assert calls[0] == calls[1]
  assert retry.json()["record"]["relay_contribution_id"] == _RELAY_ID


def test_malformed_ambiguous_relay_journal_never_starts_a_fresh_attempt(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-malformed-ambiguous-journal"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  record = json.loads(record_path.read_text())
  record.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "relay_revision": 1,
  })
  atomic_write(record_path, json.dumps(record))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a malformed ambiguous journal must never reach the broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "relay_resume_invalid"
  after = json.loads(record_path.read_text())
  assert after["status"] == "submitting"
  assert after["relay_revision"] == 1
  assert "relay_request_sha256" not in after
  assert "relay_idempotency_key" not in after
  assert after["last_submit_error_code"] == "relay_resume_invalid"


def test_forged_complete_relay_journal_without_exact_review_fails_closed(
  client, owner_token, tmp_path, monkeypatch,
):
  """Ledger syntax alone is not an owner-confirmed retry capability."""
  record_id = "relay-forged-complete-journal"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  record = json.loads(record_path.read_text())
  record.pop("quality_review")
  record.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "public_identity": "anonymous",
    "relay_revision": 1,
    "relay_request_sha256": "a" * 64,
    "relay_idempotency_key": relay_route._idempotency_key(
      app_id, record_id, 1,
    ),
    "relay_payload_sha256": "b" * 64,
    "relay_attempt_input_sha256": "e" * 64,
    "relay_owner_claim_sha256": "c" * 64,
    "relay_attempt_witness_sha256": "d" * 64,
  })
  atomic_write(record_path, json.dumps(record))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a forged retry journal must never reach the broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "relay_resume_invalid"
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "submitting"
  assert "quality_review" not in stored


def test_submit_revalidates_journaled_source_path_before_second_proof(
  client, owner_token, tmp_path, monkeypatch,
):
  """A path changed by the journal write is rejected before an unlocked read."""
  record_id = "relay-journal-path-change"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  monkeypatch.setattr(
    relay_route, "_safe_repo_path", lambda raw: Path(str(raw)).resolve(),
  )
  replacement = tmp_path / "different-review"
  replacement.mkdir()
  real_write = relay_route.write_record
  checks = []
  journal_mutated = False

  def mutate_journal_path(path, record):
    nonlocal journal_mutated
    if record.get("relay_payload_sha256") and not journal_mutated:
      changed = json.loads(json.dumps(record))
      changed["plan"]["repo_path"] = str(replacement)
      checks.append("journal-mutated")
      journal_mutated = True
      real_write(path, changed)
      return
    real_write(path, record)

  def assert_provenance(_record):
    checks.append("proof")
    return "exact_tree"

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a changed journal path must stop before the broker")

  monkeypatch.setattr(relay_route, "write_record", mutate_journal_path)
  monkeypatch.setattr(
    relay_route, "_assert_pending_equivalence_preflight", assert_provenance,
  )
  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  assert checks == ["proof", "journal-mutated"]
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "prepared"
  assert stored["plan"]["repo_path"] == str(replacement)
  assert stored["last_submit_error_code"] == "relay_prebroker_changed"
  assert not relay_route._relay_request_path(app_id, record_id).exists()
  assert not relay_route._relay_claim_path(app_id, record_id).exists()

  broker_calls = []

  async def accept_after_two_proofs(
    method, path, *, body=None, idempotency_key=None,
  ):
    broker_calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", accept_after_two_proofs,
  )
  retried = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert retried.status_code == 200, retried.text
  assert checks == ["proof", "journal-mutated", "proof", "proof"]
  assert len(broker_calls) == 1
  assert broker_calls[0][2]["revision"] == 2


def test_submit_rejects_stale_response_after_public_inputs_change_in_broker(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-public-input-race"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)

  calls = []

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    calls.append((method, body, idempotency_key))
    if method == "GET":
      return ({
        "id": _RELAY_ID,
        "status": "draft",
        "revision": 1,
      }, 200, {})
    changed = json.loads(record_path.read_text())
    changed["title"] = "Different public title"
    changed["plan"]["head_sha"] = "d" * 40
    changed["plan"]["title"] = "Different reviewed public title"
    changed["plan"]["body_draft"] = "Different public body"
    atomic_write(record_path, json.dumps(changed))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(record_path.read_text())
  assert stored["title"] == "Different public title"
  assert stored["plan"]["head_sha"] == "d" * 40
  assert "relay_contribution_id" not in stored
  assert relay_route._relay_request_path(app_id, record_id).is_file()

  # The current record no longer matches the owner-approved input, but the
  # server-signed attempt can still reconcile the exact accepted request.
  retry = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )
  assert retry.status_code == 200, retry.text
  retried = retry.json()["record"]
  assert calls[0][1:] == calls[1][1:]
  assert retried["title"] == "Different public title"
  assert retried["plan"]["head_sha"] == "d" * 40
  assert "relay_contribution_id" not in retried
  settlement = retried["relay_attempt_settlement"]
  assert settlement["attempt_input_sha256"] != relay_route._relay_input_fingerprint(
    retried
  )
  assert settlement["relay_patch"]["relay_contribution_id"] == _RELAY_ID
  assert retried["last_submit_error_code"] == (
    "relay_inputs_changed_after_submit"
  )
  assert not relay_route._relay_request_path(app_id, record_id).exists()

  status = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert status.status_code == 200, status.text
  settled = status.json()["record"]
  assert settled["title"] == "Different public title"
  assert "relay_contribution_id" not in settled
  assert (
    settled["relay_attempt_settlement"]["relay_patch"]["relay_status"]
    == "draft"
  )
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_submit_route_never_applies_an_old_response_to_a_newer_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-submit-race"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    newer = json.loads(record_path.read_text())
    newer.update({
      "status": "submitting",
      "relay_revision": body["revision"] + 1,
      "relay_request_sha256": "newer-request",
      "relay_idempotency_key": "mobius-pr:newer",
    })
    atomic_write(record_path, json.dumps(newer))
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_publication": True},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(record_path.read_text())
  assert stored["relay_revision"] == 2
  assert stored["relay_request_sha256"] == "newer-request"
  assert "relay_contribution_id" not in stored


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
    relay_route, "_settle_equivalence",
    lambda record, upstream: settled.append((record["status"], upstream)),
  )

  asyncio.run(relay_route._settle_relay_equivalence({
    "id": "merged-relay", "status": "merged",
    "merge_commit_sha": merge_sha,
    "last_land_head_sha": "d" * 40,
    "checks": {"merge_commit_sha": "e" * 40},
  }))
  asyncio.run(relay_route._settle_relay_equivalence({
    "id": "merged-relay-awaiting-sha", "status": "merged",
  }))
  asyncio.run(relay_route._settle_relay_equivalence({
    "id": "closed-relay", "status": "closed",
  }))

  assert settled == [("merged", merge_sha), ("closed", None)]


def test_relay_result_accepts_only_a_terminal_merge_commit():
  merge_sha = "C" * 40
  patch = relay_route._relay_result_patch({
    "id": _RELAY_ID,
    "status": "merged",
    "revision": 3,
    "merge_commit_sha": merge_sha,
    "pr": {
      "url": "https://github.com/mobius-os/mobius/pull/123",
      "number": 123,
      "branch": "mobius/contribution-123",
      "head_sha": "b" * 40,
      "draft": False,
    },
  }, contribution_id=_RELAY_ID, expected_revision=3)

  assert patch["status"] == "merged"
  assert patch["merge_commit_sha"] == merge_sha.lower()
  assert patch["url"].endswith("/pull/123")

  for result in ({
    "id": _RELAY_ID,
    "status": "merged",
    "merge_commit_sha": "not-a-commit",
  }, {
    "id": _RELAY_ID,
    "status": "merged",
  }, {
    "id": _RELAY_ID,
    "status": "draft",
    "merge_commit_sha": "d" * 40,
  }, {
    "id": _RELAY_ID,
    "status": "merged",
    "merge_commit_sha": "d" * 40,
    "pr": {
      "url": "https://github.com/mobius-os/mobius/pull/123",
      "draft": True,
    },
  }):
    with pytest.raises(ContributionBrokerError) as exc_info:
      relay_route._relay_result_patch(result, contribution_id=_RELAY_ID)
    assert exc_info.value.code == "invalid_relay_response"


def test_merge_commit_is_bound_to_the_signed_relay_result():
  app_id = 7
  record_id = "relay-merge-witness"
  signed = _sign_relay_attempt(app_id, record_id, {
    "id": record_id,
    "submission_mode": "mobius-bot",
    "public_identity": "anonymous",
    "relay_contribution_id": _RELAY_ID,
    "merge_commit_sha": "e" * 40,
  })

  assert relay_route._signed_attempt_is_valid(app_id, record_id, signed)
  assert not relay_route._signed_attempt_is_valid(app_id, record_id, {
    **signed,
    "merge_commit_sha": "f" * 40,
  })


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
  original = _sign_relay_attempt(app_id, record_id, original)
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


def test_real_submit_to_draft_then_status_transition_uses_signed_attempt(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-real-draft-status"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def real_lifecycle(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path))
    if method == "POST":
      return ({
        "id": _RELAY_ID,
        "status": "draft",
        "revision": body["revision"],
        "pr": {
          "url": "https://github.com/mobius-os/mobius/pull/123",
          "number": 123,
          "branch": "mobius/contribution-123",
          "head_sha": "b" * 40,
          "draft": True,
        },
      }, 201, {})
    return ({
      "id": _RELAY_ID,
      "status": "closed",
      "revision": 1,
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", real_lifecycle)
  headers = {"Authorization": f"Bearer {owner_token}"}
  submitted = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert submitted.status_code == 200, submitted.text
  draft = submitted.json()["record"]
  assert draft["status"] == "draft"
  assert relay_route._signed_attempt_is_valid(app_id, record_id, draft)

  status = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers=headers,
  )
  assert status.status_code == 200, status.text
  closed = status.json()["record"]
  assert closed["status"] == "closed"
  assert closed["relay_status"] == "closed"
  assert closed["relay_terminal_status"] == "closed"
  assert relay_route._signed_attempt_is_valid(app_id, record_id, closed)
  assert "relay_attempt_settlement" not in closed
  assert calls == [
    ("POST", relay_route.CONTRIBUTION_PREFIX),
    ("GET", f"{relay_route.CONTRIBUTION_PREFIX}/{_RELAY_ID}"),
  ]
  assert json.loads(record_path.read_text())["status"] == "closed"


def test_successful_draft_submit_retry_never_reposts_without_request_blob(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-draft-submit-idempotency"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def accept_draft(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    return ({
      "id": _RELAY_ID,
      "status": "draft",
      "revision": body["revision"],
      "pr": {
        "url": "https://github.com/mobius-os/mobius/pull/321",
        "number": 321,
        "branch": "mobius/contribution-321",
        "head_sha": "b" * 40,
        "draft": True,
      },
    }, 201, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_draft)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/submit"
  first = client.post(url, headers=headers, json={"confirm_publication": True})
  assert first.status_code == 200, first.text
  assert first.json()["record"]["status"] == "draft"
  assert len(calls) == 1
  assert not relay_route._relay_request_path(app_id, record_id).exists()
  settled_bytes = record_path.read_bytes()

  async def fail_duplicate(*_args, **_kwargs):
    pytest.fail("a settled draft must not make another POST request")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_duplicate)
  duplicate = client.post(
    url, headers=headers, json={"confirm_publication": True},
  )

  assert duplicate.status_code == 409, duplicate.text
  assert "already reached the relay" in duplicate.json()["detail"]
  assert record_path.read_bytes() == settled_bytes
  assert not relay_route._relay_request_path(app_id, record_id).exists()


def test_legacy_accepted_relay_status_is_adopted_only_from_exact_broker_proof(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-legacy-status-adoption"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _legacy_accepted_relay_record(app_id, record_id, record_path)
  calls = []

  async def prove_legacy_identity(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((method, path, body, idempotency_key))
    assert method == "GET"
    return ({
      "id": _RELAY_ID,
      "status": "draft",
      "revision": 1,
      "local_record_id": record_id,
      "repo": "mobius-os/mobius",
      "pr": {
        "url": "https://github.com/mobius-os/mobius/pull/400",
        "number": 400,
        "branch": "mobius/contribution-400",
        "head_sha": "b" * 40,
        "draft": True,
      },
    }, 200, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", prove_legacy_identity,
  )
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 200, response.text
  assert len(calls) == 1
  migrated = response.json()["record"]
  assert migrated["status"] == "prepared"
  assert migrated["last_submit_error_code"] == "relay_legacy_attempt_adopted"
  assert "relay_contribution_id" not in migrated
  settlement = relay_route._validated_attempt_settlement(
    app_id, record_id, migrated,
  )
  assert settlement is not None
  assert settlement["relay_patch"]["relay_contribution_id"] == _RELAY_ID
  assert settlement["relay_patch"]["url"].endswith("/pull/400")
  assert migrated["relay_attempt_input_sha256"] != (
    relay_route._relay_input_fingerprint(migrated)
  )


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("local_record_id", "some-other-review"),
    ("revision", 2),
    ("repo", "mobius-os/other"),
  ],
)
def test_legacy_relay_adoption_rejects_each_mismatched_broker_binding(
  client, owner_token, tmp_path, monkeypatch, field, value,
):
  record_id = f"relay-legacy-binding-{field}"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _legacy_accepted_relay_record(app_id, record_id, record_path)
  original = record_path.read_bytes()

  async def mismatched_identity(
    method, path, *, body=None, idempotency_key=None,
  ):
    result = {
      "id": _RELAY_ID,
      "status": "draft",
      "revision": 1,
      "local_record_id": record_id,
      "repo": "mobius-os/mobius",
    }
    result[field] = value
    return result, 200, {}

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", mismatched_identity,
  )
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 502, response.text
  assert response.json()["detail"]["code"] == "invalid_relay_response"
  assert record_path.read_bytes() == original
  assert "relay_attempt_settlement" not in json.loads(original)


def test_legacy_accepted_relay_withdraws_after_authoritative_adoption(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-legacy-withdraw-adoption"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _legacy_accepted_relay_record(app_id, record_id, record_path)
  calls = []

  async def adopt_then_withdraw(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((method, path, body, idempotency_key))
    if method == "GET":
      return ({
        "id": _RELAY_ID,
        "status": "draft",
        "revision": 1,
        "local_record_id": record_id,
        "repo": "mobius-os/mobius",
        "pr": {
          "url": "https://github.com/mobius-os/mobius/pull/401",
          "number": 401,
          "branch": "mobius/contribution-401",
          "head_sha": "b" * 40,
          "draft": True,
        },
      }, 200, {})
    assert path.endswith(f"/{_RELAY_ID}/withdraw")
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": 1,
    }, 200, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", adopt_then_withdraw,
  )
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/withdraw",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_withdrawal": True},
  )

  assert response.status_code == 200, response.text
  assert [call[0] for call in calls] == ["GET", "POST"]
  withdrawn = response.json()["record"]
  assert withdrawn["status"] == "prepared"
  settlement = relay_route._validated_attempt_settlement(
    app_id, record_id, withdrawn,
  )
  assert settlement is not None
  assert settlement["relay_patch"]["relay_status"] == "withdrawn"
  assert settlement["relay_patch"]["status"] == "closed"


@pytest.mark.parametrize("action", ["status", "withdraw"])
@pytest.mark.parametrize("forged_witness", [False, True])
def test_top_level_relay_routes_reject_unsigned_or_forged_unchanged_identity(
  client, owner_token, tmp_path, monkeypatch, action, forged_witness,
):
  record_id = f"relay-{action}-untrusted-{int(forged_witness)}"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "draft",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
    "relay_request_sha256": "a" * 64,
    "relay_idempotency_key": relay_route._idempotency_key(
      app_id, record_id, 1,
    ),
    "relay_payload_sha256": "b" * 64,
    "relay_status": "draft",
  })
  if forged_witness:
    original["relay_attempt_input_sha256"] = (
      relay_route._relay_input_fingerprint(original)
    )
    original["relay_owner_claim_sha256"] = "c" * 64
    original["relay_attempt_witness_sha256"] = "d" * 64
  atomic_write(record_path, json.dumps(original))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("untrusted top-level relay identity must stop before broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/{action}"
  if action == "status":
    response = client.get(url, headers=headers)
  else:
    response = client.post(
      url, headers=headers, json={"confirm_withdrawal": True},
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == "The saved relay attempt is invalid."


@pytest.mark.parametrize("action", ["status", "withdraw"])
@pytest.mark.parametrize("forged_field", ["relay_contribution_id", "relay_revision"])
def test_top_level_relay_routes_reject_forged_signed_identity_fields(
  client, owner_token, tmp_path, monkeypatch, action, forged_field,
):
  record_id = f"relay-{action}-forged-{forged_field}"
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
  forged = _sign_relay_attempt(app_id, record_id, original)
  forged[forged_field] = (
    _OTHER_RELAY_ID if forged_field == "relay_contribution_id" else 2
  )
  atomic_write(record_path, json.dumps(forged))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a forged signed relay identity must stop before broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/contribution-relay/{app_id}/{record_id}/{action}"
  if action == "status":
    response = client.get(url, headers=headers)
  else:
    response = client.post(
      url, headers=headers, json={"confirm_withdrawal": True},
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == "The saved relay attempt is invalid."


def test_status_route_rejects_forged_attempt_input_digest_and_witness(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-status-forged-attempt"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
    "relay_request_sha256": "a" * 64,
    "relay_idempotency_key": relay_route._idempotency_key(
      app_id, record_id, 1,
    ),
    "relay_payload_sha256": "b" * 64,
    "relay_attempt_input_sha256": "c" * 64,
    "relay_owner_claim_sha256": "d" * 64,
    "relay_attempt_witness_sha256": "e" * 64,
  })
  atomic_write(record_path, json.dumps(original))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("forged status reconciliation must stop before the broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == "The saved relay attempt is invalid."


def test_status_route_rejects_a_forged_detached_settlement(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-status-forged-detached-settlement"
  app_id, record_path = _create_detached_relay_attempt(
    client, owner_token, tmp_path, monkeypatch, record_id,
  )
  forged = json.loads(record_path.read_text())
  forged["relay_attempt_settlement"]["relay_patch"][
    "relay_contribution_id"
  ] = _OTHER_RELAY_ID
  atomic_write(record_path, json.dumps(forged))

  async def fail_broker(*_args, **_kwargs):
    pytest.fail("a forged settlement must stop before the broker")

  monkeypatch.setattr(relay_route.contribution_broker, "request", fail_broker)
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == "The saved relay settlement is invalid."


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
  original = _sign_relay_attempt(app_id, record_id, original)
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


def test_status_route_retains_private_request_when_local_record_changes(
  client, owner_token, tmp_path, monkeypatch,
):
  """Only a durably written, guard-matched status can remove retry bytes."""
  record_id = "relay-status-retains-raced-request"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    if method == "POST":
      return ({
        "id": _RELAY_ID,
        "status": "queued",
        "revision": body["revision"],
      }, 202, {})
    changed = json.loads(record_path.read_text())
    changed["title"] = "Changed during status"
    atomic_write(record_path, json.dumps(changed))
    return ({
      "id": _RELAY_ID,
      "status": "draft",
      "revision": 1,
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  headers = {"Authorization": f"Bearer {owner_token}"}
  submitted = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert submitted.status_code == 200, submitted.text
  request_path = relay_route._relay_request_path(app_id, record_id)
  assert not request_path.exists()
  atomic_write(request_path, b"retained ambiguous request")
  assert request_path.is_file()

  status = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers=headers,
  )
  assert status.status_code == 409, status.text
  assert request_path.is_file()
  stored = json.loads(record_path.read_text())
  assert stored["title"] == "Changed during status"
  assert stored["relay_status"] == "queued"


def test_status_route_never_overwrites_a_newer_revision_of_the_same_identity(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-status-revision-race"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "submitting",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 1,
    "relay_request_sha256": "revision-one",
  })
  original = _sign_relay_attempt(app_id, record_id, original)
  atomic_write(record_path, json.dumps(original))

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    newer = json.loads(record_path.read_text())
    newer["relay_revision"] = 2
    newer["relay_request_sha256"] = "revision-two"
    atomic_write(record_path, json.dumps(newer))
    return ({"id": _RELAY_ID, "status": "draft", "revision": 1}, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.get(
    f"/api/contribution-relay/{app_id}/{record_id}/status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(record_path.read_text())
  assert stored["relay_contribution_id"] == _RELAY_ID
  assert stored["relay_revision"] == 2
  assert stored["relay_request_sha256"] == "revision-two"


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
  original = _sign_relay_attempt(app_id, record_id, original)
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


def test_real_submit_to_draft_then_withdraw_uses_signed_attempt(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-real-draft-withdraw"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)
  calls = []

  async def real_lifecycle(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    if path == relay_route.CONTRIBUTION_PREFIX:
      return ({
        "id": _RELAY_ID,
        "status": "draft",
        "revision": body["revision"],
        "pr": {
          "url": "https://github.com/mobius-os/mobius/pull/124",
          "number": 124,
          "branch": "mobius/contribution-124",
          "head_sha": "b" * 40,
          "draft": True,
        },
      }, 201, {})
    assert path.endswith(f"/{_RELAY_ID}/withdraw")
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": body["revision"],
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", real_lifecycle)
  headers = {"Authorization": f"Bearer {owner_token}"}
  submitted = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert submitted.status_code == 200, submitted.text
  draft = submitted.json()["record"]
  assert draft["status"] == "draft"
  assert relay_route._signed_attempt_is_valid(app_id, record_id, draft)

  withdrawn = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/withdraw",
    headers=headers,
    json={"confirm_withdrawal": True},
  )
  assert withdrawn.status_code == 200, withdrawn.text
  closed = withdrawn.json()["record"]
  assert closed["status"] == "closed"
  assert closed["relay_status"] == "withdrawn"
  assert "relay_attempt_settlement" not in closed
  assert len(calls) == 2
  assert json.loads(record_path.read_text())["status"] == "closed"


def test_detached_withdraw_then_acknowledgement_allows_a_new_claim(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-detached-withdraw-lifecycle"
  app_id, record_path = _create_detached_relay_attempt(
    client, owner_token, tmp_path, monkeypatch, record_id,
  )
  calls = []

  async def withdraw_detached(
    method, path, *, body=None, idempotency_key=None,
  ):
    calls.append((method, path, body, idempotency_key))
    assert method == "POST"
    assert path.endswith(f"/{_RELAY_ID}/withdraw")
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": body["revision"],
    }, 200, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", withdraw_detached,
  )
  headers = {"Authorization": f"Bearer {owner_token}"}
  withdraw_url = f"/api/contribution-relay/{app_id}/{record_id}/withdraw"
  withdrawn = client.post(withdraw_url, headers=headers, json={
    "confirm_withdrawal": True,
  })

  assert withdrawn.status_code == 200, withdrawn.text
  closed = withdrawn.json()["record"]
  assert closed["title"] == "Changed detached review"
  assert closed["status"] == "prepared"
  assert "relay_contribution_id" not in closed
  closed_patch = closed["relay_attempt_settlement"]["relay_patch"]
  first_attempt_key = closed["relay_attempt_settlement"]["idempotency_key"]
  assert closed_patch["relay_contribution_id"] == _RELAY_ID
  assert closed_patch["relay_status"] == "withdrawn"
  assert closed_patch["status"] == "closed"
  assert closed["last_submit_error_code"] == "relay_detached_terminal"
  assert len(calls) == 1

  async def fail_unresolved_submit(*_args, **_kwargs):
    pytest.fail("an unresolved detached attempt must block a new submit")

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", fail_unresolved_submit,
  )
  blocked = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert blocked.status_code == 409, blocked.text

  # A second explicit withdrawal confirmation acknowledges the already closed
  # detached attempt locally; it must not make another public request.
  acknowledged = client.post(withdraw_url, headers=headers, json={
    "confirm_withdrawal": True,
  })
  assert acknowledged.status_code == 200, acknowledged.text
  prepared = acknowledged.json()["record"]
  assert prepared["status"] == "prepared"
  assert prepared["title"] == "Changed detached review"
  assert prepared["plan"]["head_sha"] == "d" * 40
  assert "relay_attempt_settlement" not in prepared
  assert "relay_attempt_input_sha256" not in prepared
  assert "submission_mode" not in prepared
  assert prepared["relay_revision"] == 1

  reviewed_again = json.loads(record_path.read_text())
  reviewed_again["quality_review"] = {
    "state": "all_clear",
    "reviewed_head_sha": "d" * 40,
    "reviewed_at": "2026-08-21T12:00:00Z",
  }
  atomic_write(record_path, json.dumps(reviewed_again))

  async def accept_new_claim(
    method, path, *, body=None, idempotency_key=None,
  ):
    assert method == "POST"
    assert path == relay_route.CONTRIBUTION_PREFIX
    assert body["revision"] == 2
    assert idempotency_key == relay_route._idempotency_key(
      app_id, record_id, 2,
    )
    assert idempotency_key != first_attempt_key
    return ({
      "id": _OTHER_RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(
    relay_route.contribution_broker, "request", accept_new_claim,
  )
  resubmitted = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert resubmitted.status_code == 200, resubmitted.text
  new_record = resubmitted.json()["record"]
  assert new_record["relay_contribution_id"] == _OTHER_RELAY_ID
  assert new_record["title"] == "Changed detached review"
  assert "relay_attempt_settlement" not in new_record


def test_changed_input_withdraw_detaches_without_prior_status_poll(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-withdraw-detaches-changed-top-level"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  _stub_reviewed_snapshot(monkeypatch, tmp_path)
  _allow_synthetic_source_provenance(monkeypatch, tmp_path)

  async def accept_submit(method, path, *, body=None, idempotency_key=None):
    assert method == "POST"
    return ({
      "id": _RELAY_ID,
      "status": "queued",
      "revision": body["revision"],
    }, 202, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", accept_submit)
  headers = {"Authorization": f"Bearer {owner_token}"}
  submitted = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/submit",
    headers=headers,
    json={"confirm_publication": True},
  )
  assert submitted.status_code == 200, submitted.text
  assert submitted.json()["record"]["relay_contribution_id"] == _RELAY_ID

  changed = json.loads(record_path.read_text())
  changed["title"] = "Changed after relay acceptance"
  changed["plan"]["head_sha"] = "d" * 40
  atomic_write(record_path, json.dumps(changed))
  calls = []

  async def withdraw_exact(method, path, *, body=None, idempotency_key=None):
    calls.append((method, path, body, idempotency_key))
    assert method == "POST"
    assert path.endswith(f"/{_RELAY_ID}/withdraw")
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": body["revision"],
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", withdraw_exact)
  withdrawn = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/withdraw",
    headers=headers,
    json={"confirm_withdrawal": True},
  )

  assert withdrawn.status_code == 200, withdrawn.text
  detached = withdrawn.json()["record"]
  assert len(calls) == 1
  assert detached["status"] == "prepared"
  assert detached["title"] == "Changed after relay acceptance"
  assert detached["plan"]["head_sha"] == "d" * 40
  assert "relay_contribution_id" not in detached
  assert "relay_status" not in detached
  patch = detached["relay_attempt_settlement"]["relay_patch"]
  assert patch["relay_contribution_id"] == _RELAY_ID
  assert patch["relay_status"] == "withdrawn"
  assert patch["status"] == "closed"


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
  original = _sign_relay_attempt(app_id, record_id, original)
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


def test_withdraw_route_never_applies_an_old_response_to_a_newer_revision(
  client, owner_token, tmp_path, monkeypatch,
):
  record_id = "relay-withdraw-race"
  app_id, record_path = _prepared_relay_record(
    client, owner_token, tmp_path, record_id,
  )
  original = json.loads(record_path.read_text())
  original.update({
    "status": "draft",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": _RELAY_ID,
    "relay_revision": 3,
    "relay_request_sha256": "revision-three",
    "relay_status": "draft",
  })
  original = _sign_relay_attempt(app_id, record_id, original)
  atomic_write(record_path, json.dumps(original))

  async def fake_request(method, path, *, body=None, idempotency_key=None):
    newer = json.loads(record_path.read_text())
    newer["relay_revision"] = 4
    newer["relay_request_sha256"] = "revision-four"
    atomic_write(record_path, json.dumps(newer))
    return ({
      "id": _RELAY_ID,
      "status": "withdrawn",
      "revision": 3,
    }, 200, {})

  monkeypatch.setattr(relay_route.contribution_broker, "request", fake_request)
  response = client.post(
    f"/api/contribution-relay/{app_id}/{record_id}/withdraw",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"confirm_withdrawal": True},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(record_path.read_text())
  assert stored["status"] == "draft"
  assert stored["relay_revision"] == 4
  assert stored["relay_request_sha256"] == "revision-four"
  assert "withdrawn_at" not in stored
