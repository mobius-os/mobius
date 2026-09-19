"""Owner-confirmed publication of reviewed changes through the Möbius bot."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import contribution_config, contribution_runtime, fs_locks
from app.config import get_settings
from app.contribution_broker import (
  CONTRIBUTION_PREFIX,
  MAX_REQUEST_BYTES,
  ContributionBrokerError,
  canonical_body,
  contribution_broker,
)
from app.contribution_records import (
  now_iso,
  read_record,
  record_paths,
  write_record,
)
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  reject_cross_site,
  require_nondelegated_owner_or_app_control,
)
from app.github_contribution_git import (
  _assert_clean_worktree,
  _assert_coauthor_trailer,
  _assert_fresh,
  _assert_merges_with_upstream,
  _git_env,
  _validate_branch,
  _validate_repo_slug,
)
from app.github_contributions import (
  ContributionSubmitError,
  _assert_pending_equivalence_preflight,
  _claim_record,
  _equivalence_source_repo,
  _record_pending_equivalence_locked,
  _recheck_submit_app,
  _safe_repo_path,
  _settle_equivalence,
  _validate_submit_app,
)
from app.storage_io import atomic_write


router = APIRouter(prefix="/api/contribution-relay", tags=["contribution-relay"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger(__name__)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTRIBUTION_ID = re.compile(r"^ctr_[0-9a-f]{32}$")
_RELAY_TERMINAL_FAILURES = {"error", "failed", "rejected"}
_RELAY_TERMINAL_CLOSED = {"closed", "merged", "withdrawn"}
_RELAY_TITLE_MAX_CHARS = 256
_RELAY_BODY_MAX_BYTES = 65_536
_RELAY_SIGNED_ATTEMPT_FIELDS = (
  "relay_payload_sha256",
  "relay_attempt_input_sha256",
  "relay_owner_claim_sha256",
  "relay_attempt_witness_sha256",
  "relay_result_witness_sha256",
  "relay_attempt_settlement",
  "relay_terminal_status",
)
_RELAY_PUBLICATION_FIELDS = (
  "submission_mode",
  "public_identity",
  "relay_idempotency_key",
  "relay_request_sha256",
  "relay_payload_sha256",
  "relay_attempt_input_sha256",
  "relay_owner_claim_sha256",
  "relay_attempt_witness_sha256",
  "relay_result_witness_sha256",
  "relay_attempt_settlement",
  "relay_contribution_id",
  "relay_status",
  "relay_publication_repo",
  "relay_retryable",
  "relay_target_repo",
  "relay_source_repo",
  "relay_branch",
  "relay_head_sha",
  "merge_commit_sha",
  "relay_terminal_status",
  "last_submit_upstream_branch",
  "last_submit_upstream_sha",
  "url",
  "number",
  "submitted_at",
  "withdrawn_at",
)

ANONYMOUS_CONTRIBUTION_OWNER = "mobius-os"


class RelaySubmitIn(BaseModel):
  confirm_publication: Literal[True]
  public_identity: Literal["anonymous"] = "anonymous"
  submitter: Literal["contribute-button", "chat-review-card"] = (
    "contribute-button"
  )


class RelayWithdrawIn(BaseModel):
  confirm_withdrawal: Literal[True]


def _run_git_bytes(repo: Path, *args: str) -> bytes:
  result = subprocess.run(
    ["git", "-C", str(repo), *args],
    cwd=str(repo),
    env=_git_env(repo),
    capture_output=True,
    text=False,
    timeout=60,
    check=False,
  )
  if result.returncode:
    detail = (result.stderr or result.stdout or b"Git command failed.")[:600]
    raise ContributionSubmitError(
      detail.decode("utf-8", errors="replace").strip()
    )
  return result.stdout


def _configured_target_repo(source_repo: str) -> str:
  configured = contribution_config.target_repo()
  test_repositories = contribution_config.test_repositories()
  if not configured:
    raise ContributionSubmitError(
      "Choose an explicit contribution target before using the Möbius bot. "
      "Nothing was published.",
      code="relay_target_not_configured",
    )
  repo = _validate_repo_slug(configured)
  source = _validate_repo_slug(source_repo)
  is_test_repo = repo.casefold() in test_repositories
  if (
    repo.split("/", 1)[0].casefold() != ANONYMOUS_CONTRIBUTION_OWNER
    and not is_test_repo
  ):
    raise ContributionSubmitError(
      "Anonymous Möbius contributions are available only for mobius-os "
      "repositories. Connect GitHub to contribute elsewhere.",
      code="anonymous_repo_not_allowed",
    )
  if repo.casefold() != source.casefold() and not is_test_repo:
    raise ContributionSubmitError(
      "The contribution target does not match the reviewed repository. "
      "Nothing was published.",
      code="relay_target_mismatch",
    )
  return repo


def _tree_entry(repo: Path, tree: str, path: str) -> tuple[str, str]:
  raw = _run_git_bytes(repo, "ls-tree", "-z", tree, "--", path)
  entries = [item for item in raw.split(b"\0") if item]
  if len(entries) != 1 or b"\t" not in entries[0]:
    raise ContributionSubmitError(
      "The reviewed merge tree contains an unsupported file entry."
    )
  metadata, raw_path = entries[0].split(b"\t", 1)
  parts = metadata.split()
  if len(parts) != 3 or parts[1] != b"blob":
    raise ContributionSubmitError(
      "Only regular files can be submitted through the Möbius bot."
    )
  try:
    resolved_path = raw_path.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise ContributionSubmitError(
      "File names must be valid UTF-8 before this contribution can be sent."
    ) from exc
  if resolved_path != path:
    raise ContributionSubmitError("The reviewed file path could not be verified.")
  mode = parts[0].decode("ascii")
  if mode not in {"100644", "100755"}:
    raise ContributionSubmitError(
      "Symlinks and special files cannot be submitted through the Möbius bot."
    )
  return mode, parts[2].decode("ascii")


def _merged_snapshot(record: dict, diff_path: Path) -> tuple[dict, list[dict]]:
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo = _safe_repo_path(plan.get("repo_path"))
  source_repo = _validate_repo_slug(plan.get("repo") or record.get("repo"))
  repo_slug = _configured_target_repo(source_repo)
  branch = _validate_branch(plan.get("branch") or record.get("branch"))
  _assert_clean_worktree(repo)
  _base_sha, head_sha, _diff_hash = _assert_fresh(
    record, diff_path, repo, branch,
  )
  _assert_coauthor_trailer(repo, branch)
  upstream = _assert_merges_with_upstream(repo, repo_slug, branch)
  upstream_sha = str(upstream.get("last_submit_upstream_sha") or "")
  base_ref = str(upstream.get("last_submit_upstream_branch") or "")
  if not _SHA.fullmatch(upstream_sha) or not base_ref:
    raise ContributionSubmitError(
      "The current upstream branch could not be verified."
    )
  merged = _run_git_bytes(
    repo, "merge-tree", "--write-tree", upstream_sha, head_sha,
  ).decode("ascii", errors="strict").splitlines()
  expected_tree_sha = merged[0].strip() if merged else ""
  if not _SHA.fullmatch(expected_tree_sha):
    raise ContributionSubmitError(
      "The exact reviewed merge tree could not be constructed."
    )
  raw_changes = _run_git_bytes(
    repo,
    "diff",
    "--name-status",
    "-z",
    "--no-renames",
    upstream_sha,
    expected_tree_sha,
  )
  tokens = raw_changes.split(b"\0")
  if tokens and tokens[-1] == b"":
    tokens.pop()
  if len(tokens) % 2 or len(tokens) > 160:
    raise ContributionSubmitError(
      "This contribution is too large to send safely as one pull request. "
      "Ask your agent to split it into smaller reviewed changes.",
      code="review_changed_large_diff",
    )
  files = []
  for index in range(0, len(tokens), 2):
    status = tokens[index].decode("ascii", errors="strict")
    try:
      path = tokens[index + 1].decode("utf-8")
    except UnicodeDecodeError as exc:
      raise ContributionSubmitError(
        "File names must be valid UTF-8 before this contribution can be sent."
      ) from exc
    if status not in {"A", "M", "D"}:
      raise ContributionSubmitError(
        "Renames and special Git changes must be reviewed as ordinary files."
      )
    source_tree = upstream_sha if status == "D" else expected_tree_sha
    mode, _blob_sha = _tree_entry(repo, source_tree, path)
    content = b"" if status == "D" else _run_git_bytes(
      repo, "show", f"{expected_tree_sha}:{path}",
    )
    files.append({
      "path": path,
      "operation": {"A": "add", "M": "modify", "D": "delete"}[status],
      "mode": mode,
      "content_base64": base64.b64encode(content).decode("ascii") if content else "",
    })
  if not files:
    raise ContributionSubmitError("This contribution no longer changes any files.")
  return {
    "repo": repo_slug,
    "source_repo": source_repo,
    "base_ref": base_ref,
    "base_sha": upstream_sha,
    "expected_tree_sha": expected_tree_sha,
    **upstream,
  }, files


def _relay_result_patch(
  result: object,
  *,
  contribution_id: str = "",
  merge: dict | None = None,
  expected_revision: int | None = None,
) -> dict:
  """Project a relay create/status response onto the durable local record.

  Creation may return before GitHub has opened the draft. In that case the
  local record stays in the existing ``submitting`` state and the app polls the
  exact contribution id. A later status response adds the public URL without
  inventing a second submission.
  """
  if not isinstance(result, dict):
    raise ContributionBrokerError(
      502, "The contribution relay returned an invalid result.",
      "invalid_relay_response",
    )
  reported_id = str(result.get("id") or "")
  if reported_id and contribution_id and reported_id != contribution_id:
    raise ContributionBrokerError(
      502,
      "The contribution relay returned a different contribution identity. "
      "Retry will reconcile the saved request.",
      "invalid_relay_response",
    )
  relay_id = reported_id or contribution_id
  if not _CONTRIBUTION_ID.fullmatch(relay_id):
    raise ContributionBrokerError(
      502,
      "The contribution relay returned an invalid result. Retry will reconcile the same request.",
      "invalid_relay_response",
    )
  relay_status = str(result.get("status") or "").strip().lower()
  pr = result.get("pr")
  patch = {
    "relay_contribution_id": relay_id,
    "relay_status": relay_status or "submitted",
  }
  revision = result.get("revision")
  if revision is not None:
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
      raise ContributionBrokerError(
        502,
        "The contribution relay returned an invalid revision. Retry will "
        "reconcile the saved request.",
        "invalid_relay_response",
      )
    if expected_revision is not None and revision != expected_revision:
      raise ContributionBrokerError(
        502,
        "The contribution relay returned a different revision. Retry will "
        "reconcile the saved request.",
        "invalid_relay_response",
      )
    patch["relay_revision"] = revision
  publication_repo = str(result.get("publication_repo") or "")
  if publication_repo:
    try:
      patch["relay_publication_repo"] = _validate_repo_slug(publication_repo)
    except ContributionSubmitError as exc:
      raise ContributionBrokerError(
        502,
        "The contribution relay returned an invalid publication repository.",
        "invalid_relay_response",
      ) from exc
  if isinstance(result.get("retryable"), bool):
    patch["relay_retryable"] = result["retryable"]
  merge_commit_sha = result.get("merge_commit_sha")
  if relay_status == "merged" and merge_commit_sha is None:
    raise ContributionBrokerError(
      502,
      "The contribution relay did not identify the merged commit. Retry will "
      "reconcile the same contribution.",
      "invalid_relay_response",
    )
  if merge_commit_sha is not None:
    candidate = str(merge_commit_sha).strip().lower()
    if relay_status != "merged" or not _SHA.fullmatch(candidate):
      raise ContributionBrokerError(
        502,
        "The contribution relay returned an invalid merge commit.",
        "invalid_relay_response",
      )
    patch["merge_commit_sha"] = candidate
  if merge:
    patch.update({
      "last_submit_upstream_branch": merge["base_ref"],
      "last_submit_upstream_sha": merge["base_sha"],
      "relay_target_repo": merge["repo"],
      "relay_source_repo": merge.get("source_repo") or merge["repo"],
    })
  has_pr = isinstance(pr, dict) and bool(str(pr.get("url") or ""))
  if has_pr:
    url = str(pr.get("url") or "")
    draft_state = pr.get("draft")
    terminal_pr = relay_status in _RELAY_TERMINAL_CLOSED
    valid_draft_state = (
      draft_state is False
      if relay_status == "merged"
      else isinstance(draft_state, bool)
      if terminal_pr
      else draft_state is True
    )
    if not url.startswith("https://github.com/") or not valid_draft_state:
      raise ContributionBrokerError(
        502,
        "The contribution relay returned an invalid pull request state. "
        "Retry will reconcile the same request.",
        "invalid_relay_response",
      )
    patch.update({
      "url": url,
      "number": pr.get("number"),
      "relay_branch": pr.get("branch"),
      "relay_head_sha": pr.get("head_sha"),
    })
  if relay_status in _RELAY_TERMINAL_FAILURES:
    error = result.get("error")
    message = (
      str(error.get("message") or "")
      if isinstance(error, dict)
      else str(error or "")
    ).strip()
    patch.update({
      "status": "prepared",
      "last_submit_error": message or "The Möbius relay could not open this draft pull request.",
      "last_submit_error_code": relay_status,
      # A terminal attempt must use a new monotonic revision if the owner
      # retries the same reviewed snapshot. Replaying its old idempotency key
      # would only replay the same terminal result forever.
      "relay_request_sha256": "",
    })
  elif relay_status in _RELAY_TERMINAL_CLOSED:
    patch["status"] = "merged" if relay_status == "merged" else "closed"
    patch["relay_terminal_status"] = relay_status
  elif has_pr:
    patch["status"] = relay_status or "draft"
  else:
    patch["status"] = "submitting"
  return patch


def _idempotency_key(app_id: int, record_id: str, revision: int) -> str:
  material = "\0".join((
    str(app_id), record_id, str(revision),
  )).encode()
  return "mobius-pr:" + hashlib.sha256(material).hexdigest()


_RELAY_GUARD_FIELDS = (
  "status",
  "relay_status",
  "relay_contribution_id",
  "relay_revision",
  "relay_request_sha256",
  "relay_idempotency_key",
  "relay_payload_sha256",
  "relay_attempt_input_sha256",
  "relay_owner_claim_sha256",
  "relay_attempt_witness_sha256",
  "relay_result_witness_sha256",
  "relay_attempt_settlement",
  "merge_commit_sha",
  "relay_terminal_status",
)


def _relay_input_fingerprint(record: dict) -> str:
  """Hash the complete reviewed record state bound to one owner claim."""
  material = {
    "id": record.get("id"),
    "type": record.get("type"),
    "repo": record.get("repo"),
    "branch": record.get("branch"),
    "title": record.get("title"),
    "description": record.get("description"),
    "summary": record.get("summary"),
    "plan": record.get("plan"),
    "quality_review": record.get("quality_review"),
    "submission_mode": record.get("submission_mode"),
    "public_identity": record.get("public_identity"),
  }
  return hashlib.sha256(canonical_body(material)).hexdigest()


def _server_witness(label: str, material: dict) -> str:
  key = get_settings().secret_key.encode("utf-8")
  return hmac.new(
    key,
    label.encode("ascii") + b"\0" + canonical_body(material),
    hashlib.sha256,
  ).hexdigest()


def _owner_claim_witness(
  app_id: int, record_id: str, input_sha256: str,
) -> str:
  """Bind one owner-confirmed relay claim to its exact reviewed inputs."""
  return _server_witness("mobius-relay-owner-claim-v1", {
    "app_id": app_id,
    "record_id": record_id,
    "input_sha256": input_sha256,
  })


def _attempt_witness(app_id: int, record_id: str, record: dict) -> str:
  """Bind a durable retry capability to one exact private relay request."""
  return _server_witness("mobius-relay-attempt-v1", {
    "app_id": app_id,
    "record_id": record_id,
    "input_sha256": record.get("relay_attempt_input_sha256"),
    "revision": record.get("relay_revision"),
    "request_sha256": record.get("relay_request_sha256"),
    "idempotency_key": record.get("relay_idempotency_key"),
    "payload_sha256": record.get("relay_payload_sha256"),
    "owner_claim_sha256": record.get("relay_owner_claim_sha256"),
  })


def _result_witness(app_id: int, record_id: str, record: dict) -> str:
  """Bind a broker-validated top-level identity to its signed attempt."""
  material = {
    "app_id": app_id,
    "record_id": record_id,
    "contribution_id": record.get("relay_contribution_id"),
    "revision": record.get("relay_revision"),
    "request_sha256": record.get("relay_request_sha256"),
    "payload_sha256": record.get("relay_payload_sha256"),
    "attempt_witness_sha256": record.get("relay_attempt_witness_sha256"),
  }
  # Preserve witnesses already issued before the broker contract carried a
  # merge commit while binding every newly accepted merge identity exactly.
  if "merge_commit_sha" in record:
    material["merge_commit_sha"] = record.get("merge_commit_sha")
  if "relay_terminal_status" in record:
    material["relay_terminal_status"] = record.get("relay_terminal_status")
  return _server_witness("mobius-relay-result-v1", material)


def _signed_attempt_is_valid(app_id: int, record_id: str, record: dict) -> bool:
  input_sha = str(record.get("relay_attempt_input_sha256") or "")
  owner_claim = str(record.get("relay_owner_claim_sha256") or "")
  attempt_witness = str(record.get("relay_attempt_witness_sha256") or "")
  attempt_valid = (
    bool(re.fullmatch(r"[0-9a-f]{64}", input_sha))
    and bool(re.fullmatch(r"[0-9a-f]{64}", owner_claim))
    and bool(re.fullmatch(r"[0-9a-f]{64}", attempt_witness))
    and hmac.compare_digest(
      owner_claim, _owner_claim_witness(app_id, record_id, input_sha),
    )
    and hmac.compare_digest(
      attempt_witness, _attempt_witness(app_id, record_id, record),
    )
  )
  contribution_id = str(record.get("relay_contribution_id") or "")
  if not attempt_valid or not contribution_id:
    return attempt_valid
  result_witness = str(record.get("relay_result_witness_sha256") or "")
  return (
    bool(re.fullmatch(r"[0-9a-f]{64}", result_witness))
    and hmac.compare_digest(
      result_witness, _result_witness(app_id, record_id, record),
    )
  )


def _legacy_relay_identity(
  app_id: int, record_id: str, record: dict,
) -> dict | None:
  """Describe one pre-witness accepted relay attempt, or fail it closed.

  Released relay records have a deterministic request identity and the target
  chosen from the reviewed merge, but predate server HMAC witnesses.  A partial
  or malformed new-format attempt is never treated as legacy.
  """
  if any(field in record for field in _RELAY_SIGNED_ATTEMPT_FIELDS):
    return None
  contribution_id = str(record.get("relay_contribution_id") or "")
  request_sha = str(record.get("relay_request_sha256") or "")
  idempotency_key = str(record.get("relay_idempotency_key") or "")
  target_repo = str(record.get("relay_target_repo") or "")
  revision = record.get("relay_revision")
  if (
    record.get("id") != record_id
    or record.get("submission_mode") != "mobius-bot"
    or record.get("public_identity") != "anonymous"
    or not _CONTRIBUTION_ID.fullmatch(contribution_id)
    or not isinstance(revision, int)
    or isinstance(revision, bool)
    or revision < 1
    or not re.fullmatch(r"[0-9a-f]{64}", request_sha)
    or idempotency_key != _idempotency_key(app_id, record_id, revision)
  ):
    return None
  try:
    target_repo = _validate_repo_slug(target_repo)
  except ContributionSubmitError:
    return None
  return {
    "app_id": app_id,
    "record_id": record_id,
    "contribution_id": contribution_id,
    "revision": revision,
    "request_sha256": request_sha,
    "idempotency_key": idempotency_key,
    "target_repo": target_repo,
  }


def _require_legacy_broker_identity(result: object, identity: dict) -> None:
  """Require the broker to authoritatively bind every legacy control key."""
  if not isinstance(result, dict) or any((
    result.get("id") != identity["contribution_id"],
    result.get("local_record_id") != identity["record_id"],
    result.get("revision") != identity["revision"],
    result.get("repo") != identity["target_repo"],
  )):
    raise ContributionBrokerError(
      502,
      "The contribution relay could not prove that this older attempt belongs "
      "to the exact local review, revision, and target. Nothing was changed.",
      "invalid_relay_response",
    )


def _adopt_legacy_relay_attempt(
  app_id: int,
  record_id: str,
  record: dict,
  identity: dict,
  broker_result: dict,
) -> dict:
  """Mint a signed, detached control capability from broker-owned identity.

  The old request bytes are unavailable, so the broker proof deliberately does
  not claim that current app-writable title/body inputs equal the old request.
  The caller stores the result as a detached settlement; it can be checked or
  withdrawn without authorizing a fresh publication.
  """
  authority = {
    "version": 1,
    "identity": identity,
    "broker_result": broker_result,
  }
  adopted = {
    **record,
    "relay_attempt_input_sha256": hashlib.sha256(
      canonical_body({"legacy_relay_identity": identity})
    ).hexdigest(),
    "relay_payload_sha256": hashlib.sha256(canonical_body(authority)).hexdigest(),
  }
  adopted["relay_owner_claim_sha256"] = _owner_claim_witness(
    app_id, record_id, adopted["relay_attempt_input_sha256"],
  )
  adopted["relay_attempt_witness_sha256"] = _attempt_witness(
    app_id, record_id, adopted,
  )
  return adopted


def _settlement_witness(
  app_id: int, record_id: str, settlement: dict,
) -> str:
  material = {key: value for key, value in settlement.items() if key != "witness"}
  return _server_witness("mobius-relay-settlement-v1", {
    "app_id": app_id,
    "record_id": record_id,
    "settlement": material,
  })


def _attempt_settlement(
  app_id: int,
  record_id: str,
  record: dict,
  relay_patch: dict,
) -> dict:
  """Represent an accepted old attempt without applying it to changed input."""
  settlement = {
    "attempt_input_sha256": record.get("relay_attempt_input_sha256"),
    "attempt_witness_sha256": record.get("relay_attempt_witness_sha256"),
    "request_sha256": record.get("relay_request_sha256"),
    "idempotency_key": record.get("relay_idempotency_key"),
    "payload_sha256": record.get("relay_payload_sha256"),
    "revision": record.get("relay_revision"),
    "relay_patch": relay_patch,
    "updated_at": now_iso(),
  }
  settlement["witness"] = _settlement_witness(
    app_id, record_id, settlement,
  )
  return settlement


def _validated_attempt_settlement(
  app_id: int, record_id: str, record: dict,
) -> dict | None:
  settlement = record.get("relay_attempt_settlement")
  if settlement is None:
    return None
  if not isinstance(settlement, dict):
    raise HTTPException(409, "The saved relay settlement is invalid.")
  witness = str(settlement.get("witness") or "")
  relay_patch = settlement.get("relay_patch")
  identity_matches = (
    settlement.get("attempt_input_sha256")
    == record.get("relay_attempt_input_sha256")
    and settlement.get("attempt_witness_sha256")
    == record.get("relay_attempt_witness_sha256")
    and settlement.get("request_sha256")
    == record.get("relay_request_sha256")
    and settlement.get("idempotency_key")
    == record.get("relay_idempotency_key")
    and settlement.get("payload_sha256")
    == record.get("relay_payload_sha256")
    and settlement.get("revision") == record.get("relay_revision")
  )
  if (
    not identity_matches
    or not isinstance(relay_patch, dict)
    or not re.fullmatch(r"[0-9a-f]{64}", witness)
    or not hmac.compare_digest(
      witness, _settlement_witness(app_id, record_id, settlement),
    )
  ):
    raise HTTPException(409, "The saved relay settlement is invalid.")
  return settlement


def _acknowledge_detached_attempt(record: dict) -> dict:
  """Clear a terminal detached attempt while preserving changed review input."""
  acknowledged = {
    **record,
    "status": "prepared",
    "relay_detached_acknowledged_at": now_iso(),
    "updated_at": now_iso(),
  }
  for key in (
    "submission_mode",
    "public_identity",
    "relay_idempotency_key",
    "relay_request_sha256",
    "relay_payload_sha256",
    "relay_attempt_input_sha256",
    "relay_owner_claim_sha256",
    "relay_attempt_witness_sha256",
    "relay_result_witness_sha256",
    "relay_attempt_settlement",
    "last_submit_error",
    "last_submit_error_code",
    "last_submit_error_detail",
  ):
    acknowledged.pop(key, None)
  return acknowledged


def _store_detached_result(
  app_id: int,
  record_id: str,
  record: dict,
  relay_patch: dict,
  *,
  message: str,
  error_code: str,
) -> dict:
  """Detach one exact relay result from public inputs changed after approval."""
  settlement = _attempt_settlement(
    app_id, record_id, record, relay_patch,
  )
  detached = {
    **record,
    "status": "prepared",
    "relay_attempt_settlement": settlement,
    "last_submit_error": message,
    "last_submit_error_code": error_code,
    "updated_at": now_iso(),
  }
  for key in (
    "relay_contribution_id",
    "relay_status",
    "relay_publication_repo",
    "relay_retryable",
    "relay_target_repo",
    "relay_source_repo",
    "relay_branch",
    "relay_head_sha",
    "merge_commit_sha",
    "relay_terminal_status",
    "relay_result_witness_sha256",
    "last_submit_upstream_branch",
    "last_submit_upstream_sha",
    "url",
    "number",
    "submitted_at",
    "withdrawn_at",
    "last_submit_error_detail",
  ):
    detached.pop(key, None)
  return detached


def _relay_guard(record: dict) -> tuple:
  """Identity of the exact local relay attempt an outbound call represents."""
  return (
    *(record.get(field) for field in _RELAY_GUARD_FIELDS),
    _relay_input_fingerprint(record),
  )


def _require_relay_guard(current: dict, expected: tuple, message: str) -> None:
  if _relay_guard(current) != expected:
    raise HTTPException(409, message)


def _request_revision(record: dict, payload: dict) -> tuple[int, str]:
  """Choose one monotonic revision for the exact body-independent snapshot.

  ``revision`` itself is excluded from the digest so a byte-identical retry
  reuses the same revision and capability-bound request. If current upstream
  moved and produced a different reviewed merge snapshot, the next request is
  a new revision rather than an idempotency-key/body contradiction.
  """
  snapshot_sha = hashlib.sha256(canonical_body(payload)).hexdigest()
  try:
    previous = int(record.get("relay_revision") or 0)
  except (TypeError, ValueError):
    previous = 0
  if previous >= 1 and record.get("relay_request_sha256") == snapshot_sha:
    return previous, snapshot_sha
  return max(1, previous + 1), snapshot_sha


def _validated_reviewed_title(value: object) -> str:
  """Return one GitHub-compatible reviewed title without normalizing it."""
  if (
    not isinstance(value, str)
    or not value
    or value != value.strip()
    or any(character in value for character in ("\r", "\n", "\x00"))
  ):
    raise ContributionSubmitError(
      "The reviewed relay title must be a nonblank single line without "
      "surrounding whitespace. Nothing was published.",
      code="relay_review_text_invalid",
    )
  try:
    value.encode("utf-8")
  except UnicodeEncodeError as exc:
    raise ContributionSubmitError(
      "The reviewed relay title must be valid UTF-8. Nothing was published.",
      code="relay_review_text_invalid",
    ) from exc
  if len(value) > _RELAY_TITLE_MAX_CHARS:
    raise ContributionSubmitError(
      "The reviewed relay title exceeds the supported "
      f"{_RELAY_TITLE_MAX_CHARS:,}-character "
      "limit. Nothing was published.",
      code="review_changed_large_diff",
    )
  return value


def _validated_reviewed_body(value: object) -> str:
  """Return reviewed body text byte-for-byte within GitHub's UTF-8 limit."""
  if not isinstance(value, str) or not value.strip() or "\x00" in value:
    raise ContributionSubmitError(
      "The reviewed relay body must contain visible text without NUL bytes. "
      "Nothing was published.",
      code="relay_review_text_invalid",
    )
  try:
    encoded = value.encode("utf-8")
  except UnicodeEncodeError as exc:
    raise ContributionSubmitError(
      "The reviewed relay body must be valid UTF-8. Nothing was published.",
      code="relay_review_text_invalid",
    ) from exc
  if len(encoded) > _RELAY_BODY_MAX_BYTES:
    raise ContributionSubmitError(
      "The reviewed relay body exceeds the supported "
      f"{_RELAY_BODY_MAX_BYTES:,}-byte UTF-8 limit. Nothing was published.",
      code="review_changed_large_diff",
    )
  return value


def _reviewed_public_metadata(record: dict) -> tuple[str, str]:
  """Return only the canonical public text approved in the review plan."""
  plan = record.get("plan")
  if not isinstance(plan, dict):
    raise ContributionSubmitError(
      "This contribution has no reviewed relay title or body. Nothing was "
      "published.",
      code="relay_review_text_invalid",
    )
  return (
    _validated_reviewed_title(plan.get("title")),
    _validated_reviewed_body(plan.get("body_draft")),
  )


def _record_after_definitive_relay_rejection(
  record: dict,
  *,
  revision_high_water: int,
  message: str,
  error_code: str,
) -> dict:
  """Prepare the unchanged review after a broker-proven non-public outcome."""
  prepared = {
    **record,
    "status": "prepared",
    "relay_revision": max(
      revision_high_water, _relay_revision_high_water(record),
    ),
    "last_submit_error": message,
    "last_submit_error_code": error_code,
    "updated_at": now_iso(),
  }
  prepared.pop("last_submit_error_detail", None)
  for key in _RELAY_PUBLICATION_FIELDS:
    prepared.pop(key, None)
  return prepared


def _relay_failure(
  *,
  app_id: int,
  record_id: str,
  record_path: Path,
  exc: ContributionBrokerError,
) -> dict | None:
  current = read_record(record_path)
  retryable = exc.status_code in {502, 503, 504} or exc.code in {
    "submission_in_progress", "github_error", "relay_unavailable",
  }
  if retryable and current.get("status") == "submitting":
    next_record = {
      **current,
      "last_submit_error": exc.detail,
      "last_submit_error_code": exc.code,
      "updated_at": now_iso(),
    }
    write_record(record_path, next_record)
    return next_record
  _mark_relay_claim_rejected(
    app_id=app_id, record_id=record_id, claimed=current,
  )
  failed = _record_after_definitive_relay_rejection(
    current,
    revision_high_water=_relay_revision_high_water(current),
    message=exc.detail,
    error_code=exc.code,
  )
  write_record(record_path, failed)
  _retire_relay_request(app_id, record_id, context="definitive rejection")
  _retire_claim_receipt(app_id, record_id)
  return failed


def _relay_revision_high_water(*records: dict) -> int:
  """Return the greatest assigned relay revision without trusting booleans."""
  revisions = []
  for record in records:
    revision = record.get("relay_revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
      revisions.append(revision)
  return max(revisions, default=0)


def _restore_prejournal_drift(
  *,
  app_id: int,
  record_id: str,
  record_path: Path,
  claimed: dict,
  changed: dict,
  message: str | None = None,
  error_code: str = "relay_prejournal_changed",
  expected_receipt_witness: str | None = None,
) -> dict:
  """Retire a provably pre-broker attempt while preserving changed review.

  The private receipt is first advanced to a signed ``retired`` phase.  A crash
  before the record write can therefore be resumed safely: the durable phase
  proves that no broker call was authorized, while an app-writable replay of
  the old claim cannot turn the receipt back into ``armed``.
  """
  _retire_unused_claim_receipt(
    app_id=app_id,
    record_id=record_id,
    claimed=claimed,
    expected_witness=expected_receipt_witness,
  )
  high_water = _relay_revision_high_water(claimed, changed)
  restored = {
    **changed,
    "status": "prepared",
    "last_submit_error": message or (
      "This review changed before its relay request reached the relay. Nothing "
      "was published; review the changed inputs before sending again."
    ),
    "last_submit_error_code": error_code,
    "updated_at": now_iso(),
  }
  if high_water:
    restored["relay_revision"] = high_water
  for key in (
    "submission_mode",
    "public_identity",
    "relay_idempotency_key",
    "relay_request_sha256",
    "relay_payload_sha256",
    "relay_attempt_input_sha256",
    "relay_owner_claim_sha256",
    "relay_attempt_witness_sha256",
    "relay_result_witness_sha256",
    "relay_contribution_id",
    "relay_status",
    "relay_publication_repo",
    "relay_retryable",
    "relay_target_repo",
    "relay_source_repo",
    "relay_branch",
    "relay_head_sha",
    "merge_commit_sha",
    "relay_terminal_status",
    "relay_attempt_settlement",
    "url",
    "number",
    "submitted_at",
    "withdrawn_at",
    "last_submit_error_detail",
  ):
    restored.pop(key, None)
  write_record(record_path, restored)
  # Once the prepared record is durable, stale private artifacts are
  # unreferenced capabilities. Their deletion is best effort: a crash here is
  # recovered by the next fresh claim before it creates a new journal.
  _retire_relay_request(app_id, record_id, context="unused pre-broker")
  _retire_claim_receipt(app_id, record_id)
  return restored


async def _record_relay_equivalence(record: dict) -> None:
  """Best-effort provenance after the relay accepts a reviewed snapshot.

  Personal-GitHub publication records the same pending witness after its PR is
  opened. Keeping the relay path symmetric is what lets an app-publication PR
  later prove that its exact reviewed package landed, without making this
  conflict-avoidance metadata a reason to misreport a successful public action.
  """
  try:
    await _record_pending_equivalence_locked(record)
  except Exception:
    log.warning(
      "relay contribution equivalence witness failed %s",
      record.get("id"),
      exc_info=True,
    )


async def _settle_relay_equivalence(record: dict) -> None:
  """Promote or discard a relay witness after a terminal status result."""
  if record.get("status") not in {"merged", "closed"}:
    return
  try:
    repos = await asyncio.to_thread(_equivalence_source_repo, record)
    if repos is None:
      return
    source_repo, _review_repo = repos
    upstream_sha = None
    if record.get("status") == "merged":
      candidate = str(record.get("merge_commit_sha") or "")
      upstream_sha = candidate if _SHA.fullmatch(candidate) else None
      # A landed witness without the authoritative merge commit cannot later
      # prove that the installed source contains this reviewed change. Keep
      # the pending witness intact so a later broker status refresh can finish
      # settlement once the immutable commit identity is available.
      if upstream_sha is None:
        return
    async with fs_locks.source_dir_lock(str(source_repo)):
      await asyncio.to_thread(_settle_equivalence, record, upstream_sha)
  except Exception:
    log.warning(
      "terminal relay contribution equivalence settlement failed %s",
      record.get("id"),
      exc_info=True,
    )


def _relay_snapshot_payload(
  record: dict, diff_path: Path, record_id: str,
) -> tuple[dict, dict]:
  """Build the exact body-independent snapshot bound to one relay attempt."""
  title, body = _reviewed_public_metadata(record)
  merge, files = _merged_snapshot(record, diff_path)
  return merge, {
    "contract_version": 1,
    "repo": merge["repo"],
    "base_ref": merge["base_ref"],
    "base_sha": merge["base_sha"],
    "expected_tree_sha": merge["expected_tree_sha"],
    "title": title,
    "body": body,
    "commit_message": title,
    "local_record_id": record_id,
    "public_identity": "anonymous",
    "draft": True,
    "files": files,
  }


def _relay_source_lock_paths(
  record: dict, *, include_installed_source: bool,
) -> tuple[str, ...]:
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  paths = {str(_safe_repo_path(plan.get("repo_path")))}
  if include_installed_source:
    equivalence_repos = _equivalence_source_repo(record)
    if equivalence_repos is not None:
      paths.add(str(equivalence_repos[0]))
  return tuple(sorted(paths))


def _relay_request_path(
  app_id: int, record_id: str, *, create_parent: bool = False,
) -> Path:
  """Server-private request bytes retained only for ambiguous retries."""
  return contribution_runtime.relay_request_path(
    app_id, record_id, create_parent=create_parent,
  )


def _relay_claim_path(
  app_id: int, record_id: str, *, create_parent: bool = False,
) -> Path:
  """Server-owned one-shot authorization for creating one relay journal."""
  return contribution_runtime.relay_claim_path(
    app_id, record_id, create_parent=create_parent,
  )


def _cleanup_runtime_dirs(app_id: int, record_id: str) -> None:
  try:
    contribution_runtime.cleanup_empty_runtime_dirs(app_id, record_id)
  except (OSError, ValueError):
    log.warning(
      "could not clean private contribution runtime directories %s",
      record_id,
      exc_info=True,
    )


def _retire_relay_request(
  app_id: int, record_id: str, *, context: str,
) -> None:
  try:
    _relay_request_path(app_id, record_id).unlink(missing_ok=True)
  except (OSError, ValueError):
    log.warning(
      "could not remove %s private relay request %s",
      context,
      record_id,
      exc_info=True,
    )
  _cleanup_runtime_dirs(app_id, record_id)


def _signed_claim_receipt(receipt: dict) -> dict:
  unsigned = {key: value for key, value in receipt.items() if key != "witness"}
  return {
    **unsigned,
    "witness": _server_witness("mobius-relay-claim-receipt-v1", unsigned),
  }


def _read_claim_receipt(
  *, app_id: int, record_id: str,
) -> dict | None:
  try:
    path = _relay_claim_path(app_id, record_id)
  except (OSError, ValueError):
    return None
  try:
    if path.is_symlink():
      return None
    receipt = json.loads(path.read_bytes())
  except (OSError, ValueError, UnicodeDecodeError):
    return None
  if not isinstance(receipt, dict):
    return None
  witness = str(receipt.get("witness") or "")
  expected = _signed_claim_receipt(receipt).get("witness")
  if (
    receipt.get("version") != 1
    or receipt.get("app_id") != app_id
    or receipt.get("record_id") != record_id
    or not isinstance(expected, str)
    or not re.fullmatch(r"[0-9a-f]{64}", witness)
    or not hmac.compare_digest(witness, expected)
  ):
    return None
  return receipt


def _claim_receipt_material(
  app_id: int, record_id: str, claimed: dict, *, phase: str = "armed",
) -> dict:
  try:
    previous_revision = int(claimed.get("relay_revision") or 0)
  except (TypeError, ValueError):
    previous_revision = -1
  return {
    "version": 1,
    "app_id": app_id,
    "record_id": record_id,
    "phase": phase,
    "input_sha256": claimed.get("relay_attempt_input_sha256"),
    "owner_claim_sha256": claimed.get("relay_owner_claim_sha256"),
    "previous_revision": previous_revision,
    "next_revision": previous_revision + 1,
  }


def _claim_receipt_matches_claim(
  receipt: dict, *, app_id: int, record_id: str, claimed: dict,
) -> bool:
  """Bind a phased receipt to either its pre- or post-journal record."""
  expected = _claim_receipt_material(app_id, record_id, claimed)
  if all(
    receipt.get(key) == expected.get(key)
    for key in (
      "version", "app_id", "record_id", "input_sha256",
      "owner_claim_sha256",
    )
  ):
    revision = claimed.get("relay_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
      revision = 0
    if receipt.get("phase") in {"journaled", "broker_ready"}:
      return revision == receipt.get("next_revision")
    return revision in {
      receipt.get("previous_revision"), receipt.get("next_revision"),
    }
  return False


def _write_claim_receipt(app_id: int, record_id: str, receipt: dict) -> dict:
  signed = _signed_claim_receipt(receipt)
  try:
    path = _relay_claim_path(app_id, record_id, create_parent=True)
    atomic_write(path, canonical_body(signed))
  except (OSError, ValueError) as exc:
    raise ContributionSubmitError(
      "The private relay claim store is unavailable. Nothing was published.",
      code="relay_claim_receipt_invalid",
    ) from exc
  return signed


def _advance_claim_receipt(
  *,
  app_id: int,
  record_id: str,
  claimed: dict,
  from_phases: set[str],
  to_phase: str,
) -> dict:
  """Atomically advance one exact server-owned claim phase."""
  receipt = _read_claim_receipt(app_id=app_id, record_id=record_id)
  if receipt is None or not _claim_receipt_matches_claim(
    receipt, app_id=app_id, record_id=record_id, claimed=claimed,
  ):
    raise ContributionSubmitError(
      "This relay claim has no valid one-shot receipt. Nothing was published.",
      code="relay_claim_receipt_invalid",
    )
  phase = str(receipt.get("phase") or "")
  if phase == to_phase:
    return receipt
  if phase not in from_phases:
    raise ContributionSubmitError(
      "This relay claim is in an incompatible durable phase. Nothing was "
      "published.",
      code="relay_claim_receipt_invalid",
    )
  return _write_claim_receipt(app_id, record_id, {
    **{key: value for key, value in receipt.items() if key != "witness"},
    "phase": to_phase,
    "updated_at": now_iso(),
  })


def _mark_relay_claim_rejected(
  *, app_id: int, record_id: str, claimed: dict,
) -> dict | None:
  """Make one broker-proven rejection permanently non-replayable.

  The signed phase is durable before the app record is changed. A process exit
  on either side of that record write therefore leaves one honest recovery:
  prepare the same reviewed inputs at the assigned revision high-water, discard
  the old request, and require a new owner-confirmed revision.
  """
  try:
    receipt_path = _relay_claim_path(app_id, record_id)
  except (OSError, ValueError) as exc:
    raise ContributionSubmitError(
      "The private relay claim store is unavailable after the relay rejected "
      "this request. Nothing new was published.",
      code="relay_claim_receipt_invalid",
    ) from exc
  if not receipt_path.exists():
    # Rolling-upgrade attempts created before private claim receipts have no
    # replayable server capability to tombstone.
    return None
  return _advance_claim_receipt(
    app_id=app_id,
    record_id=record_id,
    claimed=claimed,
    from_phases={"broker_ready"},
    to_phase="rejected",
  )


def _recover_rejected_relay_claim(
  *, app_id: int, record_id: str, record_path: Path, record: dict,
) -> dict:
  """Consume a durable rejection left across record/private cleanup."""
  receipt = _read_claim_receipt(app_id=app_id, record_id=record_id)
  if receipt is None or receipt.get("phase") != "rejected":
    return record
  revision = receipt.get("next_revision")
  if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
    raise ContributionSubmitError(
      "The saved relay rejection has an invalid revision. Nothing was "
      "published.",
      code="relay_claim_receipt_invalid",
    )
  recovered = _record_after_definitive_relay_rejection(
    record,
    revision_high_water=revision,
    message=str(record.get("last_submit_error") or (
      "The earlier relay request was definitively rejected. Nothing was "
      "published; this exact review can be sent as a new revision."
    )),
    error_code=str(
      record.get("last_submit_error_code")
      or "relay_definitive_rejection_recovered"
    ),
  )
  write_record(record_path, recovered)
  # Cleanup follows the durable prepared record. If either unlink fails or the
  # process exits here, the signed rejection remains safe to consume again.
  _retire_relay_request(app_id, record_id, context="rejected attempt")
  _retire_claim_receipt(app_id, record_id)
  return recovered


def _retire_unused_claim_receipt(
  *,
  app_id: int,
  record_id: str,
  claimed: dict,
  expected_witness: str | None = None,
) -> dict | None:
  """Tombstone an unused claim without an unlink-before-record crash window."""
  receipt = _read_claim_receipt(app_id=app_id, record_id=record_id)
  if receipt is None:
    return None
  phase = str(receipt.get("phase") or "")
  witness_matches = (
    expected_witness is not None
    and hmac.compare_digest(
      str(receipt.get("witness") or ""), expected_witness,
    )
  )
  if not witness_matches and not _claim_receipt_matches_claim(
    receipt, app_id=app_id, record_id=record_id, claimed=claimed,
  ):
    raise ContributionSubmitError(
      "Another exact relay claim still needs reconciliation.",
      code="relay_claim_receipt_invalid",
    )
  if phase == "retired":
    return receipt
  if phase not in {"armed", "journaled"}:
    raise ContributionSubmitError(
      "This relay claim may already have reached the broker.",
      code="relay_resume_invalid",
    )
  # Both source phases prove that no broker call was authorized. Even if the
  # app-writable record removed the old signed fields, advancing this valid
  # server receipt can only cancel an unpublished attempt; it cannot grant one.
  return _write_claim_receipt(app_id, record_id, {
    **{key: value for key, value in receipt.items() if key != "witness"},
    "phase": "retired",
    "updated_at": now_iso(),
  })


def _arm_claim_receipt(
  *, app_id: int, record_id: str, claimed: dict,
) -> None:
  expected = _claim_receipt_material(app_id, record_id, claimed)
  existing = _read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  if existing is not None:
    if all(existing.get(key) == value for key, value in expected.items()):
      return
    # ``armed`` is written before the app record. If that record write crashes,
    # the receipt is provably pre-public and a later owner-confirmed claim may
    # atomically replace it. ``retired`` is the same safe tombstone after a
    # pre-broker abort; ``rejected`` is the broker-proven non-public equivalent.
    # Journaled/broker-ready identities must reconcile.
    if existing.get("phase") not in {"armed", "retired", "rejected"}:
      raise ContributionSubmitError(
        "Another exact relay claim still needs reconciliation.",
        code="relay_claim_receipt_invalid",
      )
    _write_claim_receipt(app_id, record_id, expected)
    return
  try:
    path = _relay_claim_path(app_id, record_id)
  except (OSError, ValueError) as exc:
    raise ContributionSubmitError(
      "The private relay claim store is unavailable. Nothing was published.",
      code="relay_claim_receipt_invalid",
    ) from exc
  if path.exists() or path.is_symlink():
    raise ContributionSubmitError(
      "The saved relay claim receipt is invalid. Nothing was published.",
      code="relay_claim_receipt_invalid",
    )
  _write_claim_receipt(app_id, record_id, expected)


def _require_claim_receipt(
  *,
  app_id: int,
  record_id: str,
  claimed: dict,
  phases: set[str] | None = None,
) -> dict:
  receipt = _read_claim_receipt(
    app_id=app_id, record_id=record_id,
  )
  if (
    receipt is None
    or not _claim_receipt_matches_claim(
      receipt, app_id=app_id, record_id=record_id, claimed=claimed,
    )
    or str(receipt.get("phase") or "") not in (phases or {"armed"})
  ):
    raise ContributionSubmitError(
      "This relay claim has no valid one-shot receipt. Nothing was published.",
      code="relay_claim_receipt_invalid",
    )
  return receipt


def _retire_claim_receipt(
  app_id: int,
  record_id: str,
  *,
  require_absent: bool = False,
) -> None:
  """Remove an armed claim, failing closed before any broker request.

  Cleanup after a settled result is best effort, but publication cannot begin
  while the one-shot receipt might still be replayable.  The two pre-broker
  callers therefore require a successful unlink (or an already-absent file).
  """
  try:
    _relay_claim_path(app_id, record_id).unlink(missing_ok=True)
  except (OSError, ValueError) as exc:
    if require_absent:
      raise ContributionSubmitError(
        "The relay could not consume its one-shot claim receipt. Nothing "
        "was published.",
        code="relay_claim_receipt_invalid",
      ) from exc
    log.warning(
      "could not retire relay claim receipt %s", record_id, exc_info=True,
    )
  _cleanup_runtime_dirs(app_id, record_id)


def _relay_request_sha(envelope: dict) -> str:
  return hashlib.sha256(canonical_body(envelope)).hexdigest()


def _write_relay_request(
  app_id: int, record_id: str, merge: dict, payload: dict,
) -> str:
  if len(canonical_body(payload)) > MAX_REQUEST_BYTES:
    raise ContributionSubmitError(
      "The exact reviewed relay request exceeds the supported size limit. "
      "Nothing was published; split it into smaller reviewed changes.",
      code="review_changed_large_diff",
    )
  envelope = {"version": 1, "merge": merge, "payload": payload}
  encoded = canonical_body(envelope)
  if len(encoded) > MAX_REQUEST_BYTES + 16_384:
    raise ContributionSubmitError(
      "The reviewed relay request is too large to retain safely.",
      code="review_changed_large_diff",
    )
  try:
    path = _relay_request_path(app_id, record_id, create_parent=True)
  except (OSError, ValueError) as exc:
    raise ContributionSubmitError(
      "The private relay request store is unavailable. Nothing was published.",
      code="relay_request_store_unavailable",
    ) from exc
  atomic_write(path, encoded)
  return hashlib.sha256(encoded).hexdigest()


def _read_relay_request(
  app_id: int, record_id: str, expected_sha: str,
) -> tuple[dict, dict]:
  try:
    path = _relay_request_path(app_id, record_id)
    raw = path.read_bytes()
  except (OSError, ValueError) as exc:
    raise ContributionSubmitError(
      "The saved relay attempt is missing its private request. Nothing new "
      "was published.",
      code="relay_resume_invalid",
    ) from exc
  if len(raw) > MAX_REQUEST_BYTES + 16_384:
    raise ContributionSubmitError(
      "The saved relay request is invalid. Nothing new was published.",
      code="relay_resume_invalid",
    )
  try:
    envelope = json.loads(raw)
  except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise ContributionSubmitError(
      "The saved relay request is invalid. Nothing new was published.",
      code="relay_resume_invalid",
    ) from exc
  if (
    not isinstance(envelope, dict)
    or envelope.get("version") != 1
    or not isinstance(envelope.get("merge"), dict)
    or not isinstance(envelope.get("payload"), dict)
    or not hmac.compare_digest(_relay_request_sha(envelope), expected_sha)
  ):
    raise ContributionSubmitError(
      "The saved relay request is invalid. Nothing new was published.",
      code="relay_resume_invalid",
    )
  return envelope["merge"], envelope["payload"]


async def _adopt_legacy_control_attempt_if_needed(
  *,
  app_id: int,
  record_id: str,
  record_path: Path,
  expected_nonce: str | None,
  db: Session,
) -> tuple[dict, dict] | None:
  """Authoritatively adopt one released pre-witness relay attempt.

  App-writable legacy fields only nominate the relay identity.  The broker must
  echo the exact local record, revision, and reviewed target before the server
  signs a detached status/withdrawal capability.
  """
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    settlement = _validated_attempt_settlement(app_id, record_id, current)
    already_trusted = settlement is not None or _signed_attempt_is_valid(
      app_id, record_id, current,
    )
    identity = (
      None if already_trusted
      else _legacy_relay_identity(app_id, record_id, current)
    )
    if not already_trusted and identity is None:
      raise HTTPException(409, "The saved relay attempt is invalid.")
    attempt_guard = _relay_guard(current)
  db.close()
  if already_trusted:
    return None

  try:
    payload, _status, _headers = await contribution_broker.request(
      "GET", CONTRIBUTION_PREFIX + "/" + identity["contribution_id"],
    )
    _require_legacy_broker_identity(payload, identity)
    relay_patch = _relay_result_patch(
      payload,
      contribution_id=identity["contribution_id"],
      expected_revision=identity["revision"],
    )
  except ContributionBrokerError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail}
    ) from exc

  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    _require_relay_guard(
      current,
      attempt_guard,
      "This contribution changed while its older relay attempt was checked.",
    )
    adopted = _adopt_legacy_relay_attempt(
      app_id, record_id, current, identity, payload,
    )
    migrated = _store_detached_result(
      app_id,
      record_id,
      adopted,
      relay_patch,
      message=(
        "This older relay attempt was verified for status and withdrawal. Its "
        "result is kept separate because its original private request predates "
        "signed retry journals."
      ),
      error_code="relay_legacy_attempt_adopted",
    )
    write_record(record_path, migrated)
    _retire_relay_request(app_id, record_id, context="adopted legacy")
    _retire_claim_receipt(app_id, record_id)
  return migrated, payload


@router.post(
  "/{app_id}/{record_id}/submit",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
@_limiter.limit("5/minute")
async def submit_through_mobius(
  request: Request,
  app_id: int,
  record_id: str,
  body: RelaySubmitIn,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  expected_nonce = _validate_submit_app(app_id, principal, db)
  db.close()
  resume_error = None
  saved_request = None
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    record_path, diff_path = record_paths(app_id, record_id)
    current = read_record(record_path)
    if current.get("relay_attempt_settlement") is not None:
      _validated_attempt_settlement(app_id, record_id, current)
      raise HTTPException(
        409,
        "The earlier relay attempt is still attached separately. Withdraw "
        "or acknowledge it before publishing the changed review.",
      )
    contribution_id = str(current.get("relay_contribution_id") or "")
    accepted_attempt = (
      bool(_CONTRIBUTION_ID.fullmatch(contribution_id))
      and current.get("relay_status") not in _RELAY_TERMINAL_FAILURES
      and (
        _signed_attempt_is_valid(app_id, record_id, current)
        or _legacy_relay_identity(app_id, record_id, current) is not None
      )
    )
    if accepted_attempt:
      raise HTTPException(
        409,
        "This exact review already reached the relay. Check its status or "
        "withdraw its draft instead of publishing it again.",
      )
    try:
      current = _recover_rejected_relay_claim(
        app_id=app_id,
        record_id=record_id,
        record_path=record_path,
        record=current,
      )
    except ContributionSubmitError as exc:
      raise HTTPException(
        exc.status_code,
        {"code": exc.code or "relay_claim_receipt_invalid", "message": exc.message},
      ) from exc
    resuming_relay_claim = (
      current.get("submission_mode") == "mobius-bot"
      and (
        current.get("status") == "submitting"
        or bool(current.get("relay_request_sha256"))
      )
    )
    if not resuming_relay_claim:
      try:
        _reviewed_public_metadata(current)
      except ContributionSubmitError as exc:
        raise HTTPException(
          exc.status_code,
          {
            "code": exc.code or "relay_review_text_invalid",
            "message": exc.message,
          },
        ) from exc
    if resuming_relay_claim:
      claimed = current
    else:
      def own_relay_claim(
        claimed_record: dict,
        _claimed_path: Path,
        _resumed: bool,
        _original_record: dict,
      ) -> None:
        for key in (
          "relay_idempotency_key",
          "relay_request_sha256",
          "relay_payload_sha256",
          "relay_attempt_witness_sha256",
          "relay_result_witness_sha256",
          "relay_contribution_id",
          "relay_status",
          "relay_publication_repo",
          "relay_retryable",
          "relay_target_repo",
          "relay_source_repo",
          "relay_branch",
          "relay_head_sha",
          "merge_commit_sha",
          "relay_terminal_status",
          "relay_attempt_settlement",
          "url",
          "number",
          "submitted_at",
          "withdrawn_at",
        ):
          claimed_record.pop(key, None)
        claimed_record.update({
          "submission_mode": "mobius-bot",
          "public_identity": "anonymous",
        })
        input_sha = _relay_input_fingerprint(claimed_record)
        claimed_record["relay_attempt_input_sha256"] = input_sha
        claimed_record["relay_owner_claim_sha256"] = _owner_claim_witness(
          app_id, record_id, input_sha,
        )
        _retire_relay_request(
          app_id, record_id, context="before fresh claim",
        )
        _arm_claim_receipt(
          app_id=app_id,
          record_id=record_id,
          claimed=claimed_record,
        )

      claimed, record_path, diff_path = _claim_record(
        app_id=app_id,
        record_id=record_id,
        db=db,
        expected_nonce=expected_nonce,
        submitter=body.submitter,
        before_claim_write=own_relay_claim,
      )
    db.close()
    claim_guard = _relay_guard(claimed)
    attempt_input_sha = str(
      claimed.get("relay_attempt_input_sha256") or ""
    )
    owner_claim = str(claimed.get("relay_owner_claim_sha256") or "")
    owner_claim_valid = (
      bool(re.fullmatch(r"[0-9a-f]{64}", attempt_input_sha))
      and bool(re.fullmatch(r"[0-9a-f]{64}", owner_claim))
      and hmac.compare_digest(
        owner_claim,
        _owner_claim_witness(app_id, record_id, attempt_input_sha),
      )
    )
    prior_revision = claimed.get("relay_revision")
    prior_request_sha = str(claimed.get("relay_request_sha256") or "")
    prior_idempotency_key = str(claimed.get("relay_idempotency_key") or "")
    prior_payload_sha = str(claimed.get("relay_payload_sha256") or "")
    prior_attempt_witness = str(
      claimed.get("relay_attempt_witness_sha256") or ""
    )
    saved_attempt_present = any((
      bool(prior_request_sha),
      bool(prior_idempotency_key),
      bool(prior_payload_sha),
      bool(prior_attempt_witness),
    ))
    complete_attempt = (
      isinstance(prior_revision, int)
      and not isinstance(prior_revision, bool)
      and prior_revision >= 1
      and bool(re.fullmatch(r"[0-9a-f]{64}", prior_request_sha))
      and prior_idempotency_key
      == _idempotency_key(app_id, record_id, prior_revision)
      and bool(re.fullmatch(r"[0-9a-f]{64}", prior_payload_sha))
      and bool(re.fullmatch(r"[0-9a-f]{64}", attempt_input_sha))
      and bool(re.fullmatch(r"[0-9a-f]{64}", prior_attempt_witness))
    )
    attempt_witness_valid = (
      complete_attempt
      and hmac.compare_digest(
        prior_attempt_witness,
        _attempt_witness(app_id, record_id, claimed),
      )
    )
    claim_receipt = _read_claim_receipt(
      app_id=app_id, record_id=record_id,
    )
    claim_receipt_witness = (
      str(claim_receipt.get("witness") or "")
      if claim_receipt is not None
      else None
    )
    try:
      claim_receipt_present = _relay_claim_path(app_id, record_id).exists()
    except (OSError, ValueError):
      claim_receipt_present = True
    claim_receipt_matches = (
      claim_receipt is not None
      and _claim_receipt_matches_claim(
        claim_receipt,
        app_id=app_id,
        record_id=record_id,
        claimed=claimed,
      )
    )
    claim_phase = (
      str(claim_receipt.get("phase") or "") if claim_receipt_matches else ""
    )
    safe_claim_phase = (
      str(claim_receipt.get("phase") or "")
      if claim_receipt is not None
      and claim_receipt.get("phase") in {"armed", "journaled", "retired"}
      else ""
    )
    if resuming_relay_claim:
      if not owner_claim_valid or (saved_attempt_present and not attempt_witness_valid):
        resume_error = ContributionSubmitError(
          "The saved relay attempt journal is incomplete or invalid. Nothing "
          "new was published.",
          code="relay_resume_invalid",
        )
      elif complete_attempt:
        if safe_claim_phase:
          restored = _restore_prejournal_drift(
            app_id=app_id,
            record_id=record_id,
            record_path=record_path,
            claimed=claimed,
            changed=claimed,
            message=(
              "The saved relay request stopped before its required source "
              "proof completed. Nothing was published; review it before "
              "sending again."
            ),
            error_code="relay_prebroker_recovered",
            expected_receipt_witness=claim_receipt_witness,
          )
          raise HTTPException(409, {
            "code": "relay_prebroker_recovered",
            "message": restored["last_submit_error"],
            "record": restored,
          })
        if claim_receipt_present and claim_phase != "broker_ready":
          resume_error = ContributionSubmitError(
            "The saved relay attempt has an invalid durable phase. Nothing "
            "new was published.",
            code="relay_resume_invalid",
          )
        else:
          try:
            saved_request = _read_relay_request(
              app_id, record_id, prior_payload_sha,
            )
          except ContributionSubmitError as exc:
            resume_error = exc
      elif _relay_input_fingerprint(claimed) != attempt_input_sha:
        if safe_claim_phase:
          restored = _restore_prejournal_drift(
            app_id=app_id,
            record_id=record_id,
            record_path=record_path,
            claimed=claimed,
            changed=claimed,
            expected_receipt_witness=claim_receipt_witness,
          )
          raise HTTPException(409, {
            "code": "relay_prejournal_changed",
            "message": restored["last_submit_error"],
            "record": restored,
          })
        resume_error = ContributionSubmitError(
          "This review changed after its relay claim began. The changed "
          "inputs were not treated as a new publication approval.",
          code="relay_resume_changed",
        )
      elif claim_phase == "armed":
        _retire_relay_request(
          app_id, record_id, context="orphaned pre-journal",
        )
    if resume_error is None and not complete_attempt:
      try:
        _require_claim_receipt(
          app_id=app_id,
          record_id=record_id,
          claimed=claimed,
          phases={"armed"},
        )
      except ContributionSubmitError as exc:
        resume_error = exc

  retrying_exact_attempt = saved_request is not None
  broker_may_have_started = bool(
    attempt_witness_valid
    and complete_attempt
    and (claim_phase == "broker_ready" or not claim_receipt_present)
  )
  definitely_prebroker_owned = (
    not resuming_relay_claim
    or bool(safe_claim_phase)
  )
  attempt_guard = None
  submitted = None
  result = None

  try:
    if resume_error is not None:
      raise resume_error
    if retrying_exact_attempt:
      merge, payload = saved_request
      if payload.get("revision") != prior_revision:
        raise ContributionSubmitError(
          "The saved relay attempt no longer matches its private request. "
          "Nothing new was published.",
          code="relay_resume_invalid",
        )
      attempt_guard = claim_guard
      result, _status, _headers = await contribution_broker.request(
        "POST",
        CONTRIBUTION_PREFIX,
        body=payload,
        idempotency_key=prior_idempotency_key,
      )
    else:
      # First build proves the owner-reviewed source and chooses the durable
      # request identity. After the short journal write, app -> source lock
      # acquisition binds the paths from that exact journal while releasing
      # app storage before either the second proof or the broker request.
      lock_paths = await asyncio.to_thread(
        _relay_source_lock_paths,
        claimed,
        include_installed_source=True,
      )
      async with AsyncExitStack() as source_locks:
        for lock_path in lock_paths:
          await source_locks.enter_async_context(
            fs_locks.source_dir_lock(lock_path)
          )
        await asyncio.to_thread(_assert_pending_equivalence_preflight, claimed)
        _initial_merge, initial_payload = await asyncio.to_thread(
          _relay_snapshot_payload, claimed, diff_path, record_id,
        )

      async with AsyncExitStack() as publication_locks:
        async with fs_locks.app_storage_lock(app_id):
          _recheck_submit_app(db, app_id, expected_nonce)
          db.close()
          current = read_record(record_path)
          if _relay_guard(current) != claim_guard:
            restored = _restore_prejournal_drift(
              app_id=app_id,
              record_id=record_id,
              record_path=record_path,
              claimed=claimed,
              changed=current,
              expected_receipt_witness=claim_receipt_witness,
            )
            raise HTTPException(409, {
              "code": "relay_prejournal_changed",
              "message": restored["last_submit_error"],
              "record": restored,
            })
          _require_claim_receipt(
            app_id=app_id,
            record_id=record_id,
            claimed=current,
          )
          revision, request_sha = _request_revision(current, initial_payload)
          idempotency_key = _idempotency_key(app_id, record_id, revision)
          payload = {**initial_payload, "revision": revision}
          payload_sha = _write_relay_request(
            app_id, record_id, _initial_merge, payload,
          )
          claimed = {
            **current,
            "relay_revision": revision,
            "relay_request_sha256": request_sha,
            "relay_idempotency_key": idempotency_key,
            "relay_payload_sha256": payload_sha,
            "updated_at": now_iso(),
          }
          claimed["relay_attempt_witness_sha256"] = _attempt_witness(
            app_id, record_id, claimed,
          )
          attempt_guard = _relay_guard(claimed)
          write_record(record_path, claimed)
          journaled = read_record(record_path)
          if _relay_guard(journaled) != attempt_guard:
            restored = _restore_prejournal_drift(
              app_id=app_id,
              record_id=record_id,
              record_path=record_path,
              claimed=claimed,
              changed=journaled,
              message=(
                "This review changed before its journaled relay request "
                "completed the required source proof. Nothing was published."
              ),
              error_code="relay_prebroker_changed",
              expected_receipt_witness=claim_receipt_witness,
            )
            raise HTTPException(409, {
              "code": "relay_prebroker_changed",
              "message": restored["last_submit_error"],
              "record": restored,
            })
          claim_receipt = _advance_claim_receipt(
            app_id=app_id,
            record_id=record_id,
            claimed=journaled,
            from_phases={"armed"},
            to_phase="journaled",
          )
          claim_receipt_witness = str(claim_receipt.get("witness") or "")
          claimed = journaled
          lock_paths = await asyncio.to_thread(
            _relay_source_lock_paths,
            claimed,
            include_installed_source=True,
          )
          for lock_path in lock_paths:
            await publication_locks.enter_async_context(
              fs_locks.source_dir_lock(lock_path)
            )

        await asyncio.to_thread(_assert_pending_equivalence_preflight, claimed)
        merge, payload = await asyncio.to_thread(
          _relay_snapshot_payload, claimed, diff_path, record_id,
        )
        rebuilt_revision, rebuilt_request_sha = _request_revision(
          claimed, payload,
        )
        if (
          rebuilt_revision != revision
          or rebuilt_request_sha != request_sha
        ):
          raise ContributionSubmitError(
            "The reviewed relay snapshot changed before publication. "
            "Nothing was published.",
            code="relay_snapshot_changed",
          )
        payload["revision"] = revision
        if _relay_request_sha({
          "version": 1, "merge": merge, "payload": payload,
        }) != payload_sha:
          raise ContributionSubmitError(
            "The reviewed relay snapshot changed before publication. "
            "Nothing was published.",
            code="relay_snapshot_changed",
          )
        _advance_claim_receipt(
          app_id=app_id,
          record_id=record_id,
          claimed=claimed,
          from_phases={"journaled"},
          to_phase="broker_ready",
        )
        # From this durable phase onward, a crash cannot prove whether the POST
        # began. Only an exact byte-for-byte retry may reconcile the attempt.
        broker_may_have_started = True
        result, _status, _headers = await contribution_broker.request(
          "POST",
          CONTRIBUTION_PREFIX,
          body=payload,
          idempotency_key=idempotency_key,
        )

    relay_patch = _relay_result_patch(
      result,
      contribution_id=str(claimed.get("relay_contribution_id") or ""),
      merge=merge,
      expected_revision=int(claimed["relay_revision"]),
    )
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      db.close()
      current = read_record(record_path)
      _require_relay_guard(
        current, attempt_guard,
        "This contribution changed while it was sent.",
      )
      if (
        relay_patch.get("status") == "prepared"
        and relay_patch.get("relay_status") in _RELAY_TERMINAL_FAILURES
      ):
        _mark_relay_claim_rejected(
          app_id=app_id,
          record_id=record_id,
          claimed=current,
        )
      if _relay_input_fingerprint(current) != attempt_input_sha:
        submitted = _store_detached_result(
          app_id,
          record_id,
          current,
          relay_patch,
          message=(
            "The earlier reviewed relay attempt was accepted, but this "
            "record's public inputs changed before it settled. Its result "
            "was saved separately and was not applied to the changed review."
          ),
          error_code="relay_inputs_changed_after_submit",
        )
      else:
        submitted = {
          **current,
          **relay_patch,
          "submitted_at": now_iso(),
          "updated_at": now_iso(),
        }
        submitted["relay_result_witness_sha256"] = _result_witness(
          app_id, record_id, submitted,
        )
        if submitted.get("status") != "prepared":
          submitted.pop("last_submit_error", None)
          submitted.pop("last_submit_error_code", None)
          submitted.pop("last_submit_error_detail", None)
      write_record(record_path, submitted)
      _retire_relay_request(app_id, record_id, context="result-settled")
      _retire_claim_receipt(app_id, record_id)
  except ContributionSubmitError as exc:
    response_code = exc.code or "review_changed"
    response_message = exc.message
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      db.close()
      current = read_record(record_path)
      if not broker_may_have_started and definitely_prebroker_owned:
        expected_guard = attempt_guard or claim_guard
        guard_changed = _relay_guard(current) != expected_guard
        error_code = (
          "relay_prebroker_changed" if attempt_guard is not None
          else "relay_prejournal_changed"
        ) if guard_changed else (exc.code or "review_changed")
        message = (
          "This review changed before its relay request reached the broker. "
          "Nothing was published; review the changed inputs before sending "
          "again."
        ) if guard_changed else exc.message
        response_code = error_code
        response_message = message
        record = _restore_prejournal_drift(
          app_id=app_id,
          record_id=record_id,
          record_path=record_path,
          claimed=claimed,
          changed=current,
          message=message,
          error_code=error_code,
          expected_receipt_witness=claim_receipt_witness,
        )
      else:
        _require_relay_guard(
          current, attempt_guard or claim_guard,
          "This contribution changed while it was sent.",
        )
      if broker_may_have_started or not definitely_prebroker_owned:
        # Any failure after ``broker_ready`` is ambiguous unless the signed
        # receipt was durably advanced to ``rejected``. In particular, failure
        # to write that tombstone after a terminal broker response must retain
        # the exact request rather than manufacture a fresh publication claim.
        record = {
          **current,
          "status": "submitting",
          "last_submit_error": exc.message,
          "last_submit_error_code": exc.code,
          "updated_at": now_iso(),
        }
        if exc.detail:
          record["last_submit_error_detail"] = exc.detail
        else:
          record.pop("last_submit_error_detail", None)
        write_record(record_path, record)
    raise HTTPException(
      exc.status_code,
      {
        "code": response_code,
        "message": response_message,
        "record": record,
      },
    ) from exc
  except ContributionBrokerError as exc:
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      db.close()
      _require_relay_guard(
        read_record(record_path), attempt_guard or claim_guard,
        "This contribution changed while it was sent.",
      )
      record = _relay_failure(
        app_id=app_id,
        record_id=record_id,
        record_path=record_path,
        exc=exc,
      )
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    raise HTTPException(
      exc.status_code,
      {"code": exc.code, "message": exc.detail, "record": record},
      headers=headers,
    ) from exc

  if submitted is None or result is None:
    raise HTTPException(500, "The contribution relay did not settle its attempt.")
  if (
    submitted.get("status") != "prepared"
    and not submitted.get("relay_attempt_settlement")
  ):
    await _record_relay_equivalence(submitted)
    await _settle_relay_equivalence(submitted)
  return {"record": submitted, "contribution": result}


@router.get("/{app_id}/{record_id}/status")
@_limiter.limit("30/minute")
async def relay_contribution_status(
  request: Request,
  app_id: int,
  record_id: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  expected_nonce = _validate_submit_app(app_id, principal, db)
  record_path, _diff_path = record_paths(app_id, record_id)
  adopted = await _adopt_legacy_control_attempt_if_needed(
    app_id=app_id,
    record_id=record_id,
    record_path=record_path,
    expected_nonce=expected_nonce,
    db=db,
  )
  if adopted is not None:
    migrated, payload = adopted
    return {"record": migrated, "contribution": payload}
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    record = read_record(record_path)
    settlement = _validated_attempt_settlement(app_id, record_id, record)
    if settlement is None and not _signed_attempt_is_valid(
      app_id, record_id, record,
    ):
      raise HTTPException(409, "The saved relay attempt is invalid.")
    attempt_input_sha = str(
      record.get("relay_attempt_input_sha256") or ""
    )
    detaching_changed_attempt = (
      settlement is None
      and bool(re.fullmatch(r"[0-9a-f]{64}", attempt_input_sha))
      and _relay_input_fingerprint(record) != attempt_input_sha
    )
    settlement_patch = (
      settlement.get("relay_patch") if settlement is not None else None
    )
    contribution_id = str(
      (
        settlement_patch.get("relay_contribution_id")
        if isinstance(settlement_patch, dict)
        else record.get("relay_contribution_id")
      )
      or ""
    )
    if not contribution_id:
      raise HTTPException(404, "This contribution has not reached the relay yet.")
    expected_revision = (
      settlement.get("revision")
      if settlement is not None
      else record.get("relay_revision")
    )
    if not isinstance(expected_revision, int) or isinstance(
      expected_revision, bool
    ):
      expected_revision = None
    attempt_guard = _relay_guard(record)
  db.close()
  try:
    payload, _status, _headers = await contribution_broker.request(
      "GET", CONTRIBUTION_PREFIX + "/" + contribution_id,
    )
  except ContributionBrokerError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail}
    ) from exc
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    _require_relay_guard(
      current, attempt_guard,
      "This contribution changed while status was checked.",
    )
    try:
      relay_patch = _relay_result_patch(
        payload,
        contribution_id=contribution_id,
        expected_revision=expected_revision,
      )
    except ContributionBrokerError as exc:
      raise HTTPException(
        exc.status_code, {"code": exc.code, "message": exc.detail}
      ) from exc
    if settlement is not None:
      current = {
        **current,
        "relay_attempt_settlement": _attempt_settlement(
          app_id, record_id, current, relay_patch,
        ),
        "updated_at": now_iso(),
      }
    elif detaching_changed_attempt:
      current = _store_detached_result(
        app_id,
        record_id,
        current,
        relay_patch,
        message=(
          "The relay attempt belongs to earlier reviewed inputs. Its result "
          "was saved separately and was not applied to the changed review."
        ),
        error_code="relay_inputs_changed_after_submit",
      )
    else:
      current = {
        **current,
        **relay_patch,
        "updated_at": now_iso(),
      }
      current["relay_result_witness_sha256"] = _result_witness(
        app_id, record_id, current,
      )
      if current.get("status") != "prepared":
        current.pop("last_submit_error", None)
        current.pop("last_submit_error_code", None)
        current.pop("last_submit_error_detail", None)
    write_record(record_path, current)
    _retire_relay_request(app_id, record_id, context="status-settled")
    _retire_claim_receipt(app_id, record_id)
  if settlement is None and not detaching_changed_attempt:
    await _settle_relay_equivalence(current)
  return {"record": current, "contribution": payload}


@router.post(
  "/{app_id}/{record_id}/withdraw",
  dependencies=[
    Depends(reject_cross_site),
    Depends(require_nondelegated_owner_or_app_control),
  ],
)
@_limiter.limit("5/minute")
async def withdraw_mobius_contribution(
  request: Request,
  app_id: int,
  record_id: str,
  body: RelayWithdrawIn,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Withdraw one bot-published draft after the owner confirms that action."""
  expected_nonce = _validate_submit_app(app_id, principal, db)
  record_path, _diff_path = record_paths(app_id, record_id)
  await _adopt_legacy_control_attempt_if_needed(
    app_id=app_id,
    record_id=record_id,
    record_path=record_path,
    expected_nonce=expected_nonce,
    db=db,
  )
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    settlement = _validated_attempt_settlement(app_id, record_id, current)
    if settlement is None and not _signed_attempt_is_valid(
      app_id, record_id, current,
    ):
      raise HTTPException(409, "The saved relay attempt is invalid.")
    attempt_input_sha = str(
      current.get("relay_attempt_input_sha256") or ""
    )
    detaching_changed_attempt = (
      settlement is None
      and _relay_input_fingerprint(current) != attempt_input_sha
    )
    settlement_patch = (
      settlement.get("relay_patch") if settlement is not None else None
    )
    contribution_id = str(
      (
        settlement_patch.get("relay_contribution_id")
        if isinstance(settlement_patch, dict)
        else current.get("relay_contribution_id")
      )
      or ""
    )
    if not _CONTRIBUTION_ID.fullmatch(contribution_id):
      raise HTTPException(409, "This contribution has no Möbius draft to withdraw.")
    saved_status = (
      str(settlement_patch.get("status") or "")
      if isinstance(settlement_patch, dict)
      else str(current.get("status") or "")
    )
    saved_relay_status = (
      str(settlement_patch.get("relay_status") or "")
      if isinstance(settlement_patch, dict)
      else str(current.get("relay_status") or "")
    )
    if settlement is not None and (
      saved_status in {"merged", "closed"}
      or saved_relay_status in _RELAY_TERMINAL_FAILURES
      or saved_relay_status in _RELAY_TERMINAL_CLOSED
    ):
      acknowledged = _acknowledge_detached_attempt(current)
      write_record(record_path, acknowledged)
      db.close()
      return {"record": acknowledged, "contribution": {
        "id": contribution_id,
        "status": saved_relay_status or saved_status,
        "revision": settlement.get("revision"),
      }}
    if settlement is None and saved_status in {"merged", "closed", "abandoned"}:
      return {"record": current, "contribution": {
        "id": contribution_id,
        "status": saved_relay_status or saved_status,
      }}
    try:
      revision = int(
        settlement.get("revision")
        if settlement is not None
        else current.get("relay_revision") or 1
      )
    except (TypeError, ValueError):
      raise HTTPException(
        409, "This contribution has an invalid saved relay revision."
      ) from None
    if revision < 1:
      raise HTTPException(
        409, "This contribution has an invalid saved relay revision."
      )
    payload = {
      "contract_version": 1,
      "revision": revision,
      "reason": "owner_withdrawn",
    }
    idempotency_key = "mobius-withdraw:" + hashlib.sha256(
      f"{app_id}\0{record_id}\0{contribution_id}\0{revision}".encode()
    ).hexdigest()
    attempt_guard = _relay_guard(current)
  db.close()

  try:
    result, _status, _headers = await contribution_broker.request(
      "POST",
      f"{CONTRIBUTION_PREFIX}/{contribution_id}/withdraw",
      body=payload,
      idempotency_key=idempotency_key,
    )
    relay_patch = _relay_result_patch(
      result,
      contribution_id=contribution_id,
      expected_revision=revision,
    )
    if relay_patch.get("status") not in {"closed", "merged"}:
      raise ContributionBrokerError(
        502,
        "The contribution relay did not confirm that the draft was closed. "
        "Retry will reconcile the same withdrawal request.",
        "invalid_relay_response",
      )
  except ContributionBrokerError as exc:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    raise HTTPException(
      exc.status_code,
      {"code": exc.code, "message": exc.detail, "record": current},
      headers=headers,
    ) from exc

  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    latest = read_record(record_path)
    _require_relay_guard(
      latest, attempt_guard,
      "This contribution changed while it was withdrawn.",
    )
    if settlement is not None or detaching_changed_attempt:
      detached_patch = {**relay_patch}
      if detached_patch.get("status") == "closed":
        detached_patch["withdrawn_at"] = now_iso()
      withdrawn = _store_detached_result(
        app_id,
        record_id,
        latest,
        detached_patch,
        message=(
          "The detached relay attempt is closed. Acknowledge it to prepare "
          "the changed review for a new publication."
        ),
        error_code="relay_detached_terminal",
      )
    else:
      withdrawn = {
        **latest,
        **relay_patch,
        "updated_at": now_iso(),
      }
      withdrawn["relay_result_witness_sha256"] = _result_witness(
        app_id, record_id, withdrawn,
      )
      if withdrawn.get("status") == "closed":
        withdrawn["withdrawn_at"] = now_iso()
      withdrawn.pop("last_submit_error", None)
      withdrawn.pop("last_submit_error_code", None)
      withdrawn.pop("last_submit_error_detail", None)
    write_record(record_path, withdrawn)
    _retire_relay_request(app_id, record_id, context="withdrawn")
    _retire_claim_receipt(app_id, record_id)
  if settlement is None and not detaching_changed_attempt:
    await _settle_relay_equivalence(withdrawn)
  return {"record": withdrawn, "contribution": result}
