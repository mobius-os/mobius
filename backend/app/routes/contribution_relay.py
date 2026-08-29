"""Owner-confirmed publication of reviewed changes through the Möbius bot."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import contribution_config, fs_locks
from app.contribution_broker import (
  CONTRIBUTION_PREFIX,
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
from app.deps import Principal, get_principal, reject_cross_site
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
  _claim_record,
  _equivalence_source_repo,
  _mark_submit_failure,
  _merged_upstream_sha,
  _record_pending_equivalence_locked,
  _recheck_submit_app,
  _safe_repo_path,
  _settle_equivalence,
  _validate_submit_app,
)


router = APIRouter(prefix="/api/contribution-relay", tags=["contribution-relay"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger(__name__)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTRIBUTION_ID = re.compile(r"^ctr_[0-9a-f]{32}$")
_RELAY_TERMINAL_FAILURES = {"error", "failed", "rejected"}
_RELAY_TERMINAL_CLOSED = {"closed", "merged", "withdrawn"}

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
  try:
    configured = contribution_config.target_repo()
    test_repositories = contribution_config.test_repositories()
  except contribution_config.ContributionConfigError as exc:
    raise ContributionSubmitError(
      "The contribution relay settings are invalid. Nothing was published.",
      code="relay_config_invalid",
    ) from exc
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
  if merge:
    patch.update({
      "last_submit_upstream_branch": merge["base_ref"],
      "last_submit_upstream_sha": merge["base_sha"],
      "relay_target_repo": merge["repo"],
      "relay_source_repo": merge.get("source_repo") or merge["repo"],
    })
  if isinstance(pr, dict) and str(pr.get("url") or ""):
    url = str(pr.get("url") or "")
    if not url.startswith("https://github.com/") or pr.get("draft") is not True:
      raise ContributionBrokerError(
        502,
        "The contribution relay returned an invalid draft pull request. "
        "Retry will reconcile the same request.",
        "invalid_relay_response",
      )
    patch.update({
      "status": relay_status or "draft",
      "url": url,
      "number": pr.get("number"),
      "relay_branch": pr.get("branch"),
      "relay_head_sha": pr.get("head_sha"),
    })
  elif relay_status in _RELAY_TERMINAL_FAILURES:
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
  else:
    patch["status"] = "submitting"
  return patch


def _idempotency_key(app_id: int, record_id: str, revision: int) -> str:
  material = "\0".join((
    str(app_id), record_id, str(revision),
  )).encode()
  return "mobius-pr:" + hashlib.sha256(material).hexdigest()


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


def _reviewed_description(record: dict) -> str:
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  return str(
    plan.get("body_draft")
    or record.get("description")
    or record.get("summary")
    or "Reviewed in Möbius."
  ).strip()


def _relay_failure(
  *, app_id: int, record_path: Path, exc: ContributionBrokerError,
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
  failed = _mark_submit_failure(
    app_id=app_id,
    record_path=record_path,
    message=exc.detail,
    record_patch={"last_submit_error_code": exc.code},
  )
  return _clear_unpublished_relay_attempt(record_path, failed)


def _clear_unpublished_relay_attempt(
  record_path: Path, record: dict | None,
) -> dict | None:
  """Remove bot-attribution from an attempt that never reached the relay."""
  if record is not None and not record.get("relay_contribution_id"):
    for key in (
      "submission_mode", "public_identity", "relay_idempotency_key",
      "relay_request_sha256", "relay_revision",
    ):
      record.pop(key, None)
    write_record(record_path, record)
  return record


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
    source_repo, review_repo = repos
    upstream_sha = None
    if record.get("status") == "merged":
      upstream_sha = await asyncio.to_thread(
        _merged_upstream_sha, record, review_repo,
      )
    async with fs_locks.source_dir_lock(str(source_repo)):
      await asyncio.to_thread(_settle_equivalence, record, upstream_sha)
  except Exception:
    log.warning(
      "terminal relay contribution equivalence settlement failed %s",
      record.get("id"),
      exc_info=True,
    )


@router.post(
  "/{app_id}/{record_id}/submit",
  dependencies=[Depends(reject_cross_site)],
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
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    record_path, diff_path = record_paths(app_id, record_id)
    current = read_record(record_path)
    if (
      current.get("status") == "submitting"
      and current.get("submission_mode") == "mobius-bot"
    ):
      claimed = current
    else:
      claimed, record_path, diff_path = _claim_record(
        app_id=app_id,
        record_id=record_id,
        db=db,
        expected_nonce=expected_nonce,
        submitter=body.submitter,
      )
      claimed = {
        **claimed,
        "submission_mode": "mobius-bot",
        "public_identity": "anonymous",
      }
      write_record(record_path, claimed)
  db.close()

  try:
    async with fs_locks.source_dir_lock(
      str(_safe_repo_path((claimed.get("plan") or {}).get("repo_path")))
    ):
      merge, files = await asyncio.to_thread(
        _merged_snapshot, claimed, diff_path,
      )
    title = str(claimed.get("title") or "Reviewed Möbius contribution").strip()
    description = _reviewed_description(claimed)
    payload = {
      "contract_version": 1,
      "repo": merge["repo"],
      "base_ref": merge["base_ref"],
      "base_sha": merge["base_sha"],
      "expected_tree_sha": merge["expected_tree_sha"],
      "title": title[:256],
      "body": description[:65_536],
      "commit_message": title[:512],
      "local_record_id": record_id,
      "public_identity": "anonymous",
      "draft": True,
      "files": files,
    }
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      current = read_record(record_path)
      if (
        current.get("status") != "submitting"
        or current.get("submission_mode") != "mobius-bot"
      ):
        raise HTTPException(409, "This contribution changed while it was sent.")
      revision, request_sha = _request_revision(current, payload)
      payload["revision"] = revision
      claimed = {
        **current,
        "relay_revision": revision,
        "relay_request_sha256": request_sha,
        "relay_idempotency_key": _idempotency_key(
          app_id, record_id, revision,
        ),
        "updated_at": now_iso(),
      }
      write_record(record_path, claimed)
    result, _status, _headers = await contribution_broker.request(
      "POST",
      CONTRIBUTION_PREFIX,
      body=payload,
      idempotency_key=str(claimed["relay_idempotency_key"]),
    )
  except ContributionSubmitError as exc:
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      record = _mark_submit_failure(
        app_id=app_id,
        record_path=record_path,
        message=exc.message,
        record_patch={
          **(exc.record_patch or {}),
          "last_submit_error_code": exc.code or "review_changed",
        },
        detail=exc.detail,
      )
      record = _clear_unpublished_relay_attempt(record_path, record)
    raise HTTPException(
      exc.status_code,
      {"code": exc.code or "review_changed", "message": exc.message, "record": record},
    ) from exc
  except ContributionBrokerError as exc:
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      record = _relay_failure(app_id=app_id, record_path=record_path, exc=exc)
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    raise HTTPException(
      exc.status_code,
      {"code": exc.code, "message": exc.detail, "record": record},
      headers=headers,
    ) from exc

  try:
    relay_patch = _relay_result_patch(
      result,
      contribution_id=str(claimed.get("relay_contribution_id") or ""),
      merge=merge,
      expected_revision=int(claimed["relay_revision"]),
    )
  except ContributionBrokerError as invalid:
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      record = _relay_failure(app_id=app_id, record_path=record_path, exc=invalid)
    raise HTTPException(
      502,
      {"code": invalid.code, "message": invalid.detail, "record": record},
    )
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    if current.get("status") != "submitting":
      raise HTTPException(409, "This contribution changed while it was sent.")
    submitted = {
      **current,
      **relay_patch,
      "submitted_at": now_iso(),
      "updated_at": now_iso(),
    }
    if submitted.get("status") != "prepared":
      submitted.pop("last_submit_error", None)
      submitted.pop("last_submit_error_code", None)
      submitted.pop("last_submit_error_detail", None)
    write_record(record_path, submitted)
  if submitted.get("status") != "prepared":
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
  record = read_record(record_path)
  contribution_id = str(record.get("relay_contribution_id") or "")
  if not contribution_id:
    raise HTTPException(404, "This contribution has not reached the relay yet.")
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
    if str(current.get("relay_contribution_id") or "") != contribution_id:
      raise HTTPException(409, "This contribution changed while status was checked.")
    try:
      relay_patch = _relay_result_patch(
        payload,
        contribution_id=contribution_id,
        expected_revision=(
          current.get("relay_revision")
          if isinstance(current.get("relay_revision"), int)
          and not isinstance(current.get("relay_revision"), bool)
          else None
        ),
      )
    except ContributionBrokerError as exc:
      raise HTTPException(
        exc.status_code, {"code": exc.code, "message": exc.detail}
      ) from exc
    current = {
      **current,
      **relay_patch,
      "updated_at": now_iso(),
    }
    if current.get("status") != "prepared":
      current.pop("last_submit_error", None)
      current.pop("last_submit_error_code", None)
      current.pop("last_submit_error_detail", None)
    write_record(record_path, current)
  await _settle_relay_equivalence(current)
  return {"record": current, "contribution": payload}


@router.post(
  "/{app_id}/{record_id}/withdraw",
  dependencies=[Depends(reject_cross_site)],
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
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = read_record(record_path)
    contribution_id = str(current.get("relay_contribution_id") or "")
    if not _CONTRIBUTION_ID.fullmatch(contribution_id):
      raise HTTPException(409, "This contribution has no Möbius draft to withdraw.")
    if current.get("status") in {"merged", "closed", "abandoned"}:
      return {"record": current, "contribution": {
        "id": contribution_id,
        "status": current.get("relay_status") or current.get("status"),
      }}
    try:
      revision = int(current.get("relay_revision") or 1)
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
    if str(latest.get("relay_contribution_id") or "") != contribution_id:
      raise HTTPException(409, "This contribution changed while it was withdrawn.")
    withdrawn = {
      **latest,
      **relay_patch,
      "updated_at": now_iso(),
    }
    if withdrawn.get("status") == "closed":
      withdrawn["withdrawn_at"] = now_iso()
    withdrawn.pop("last_submit_error", None)
    withdrawn.pop("last_submit_error_code", None)
    withdrawn.pop("last_submit_error_detail", None)
    write_record(record_path, withdrawn)
  await _settle_relay_equivalence(withdrawn)
  return {"record": withdrawn, "contribution": result}
