"""Same-origin BFF for the GitHub-backed Möbius community catalog."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import re
from typing import Any, Literal
from urllib.parse import quote
from weakref import WeakValueDictionary

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import fs_locks, github_auth, models
from app.community_broker import (
  COMMUNITY_PREFIX,
  CommunityBrokerError,
  community_broker,
)
from app.community_publish import (
  CommunityPublicationJournal,
  CommunityPublicationError,
  build_public_snapshot,
  delete_publication_journal,
  list_publication_journals,
  new_publication_journal,
  read_publication_journal,
  write_publication_journal,
)
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_github_access,
  get_owner_or_app_with_manage_apps,
  reject_cross_site,
)


router = APIRouter(prefix="/api/community", tags=["community"])
_PUBLIC_IDENTITY = Literal["anonymous", "github"]
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_:-]{8,200}$")
_RETRYABLE_ADMISSION_CODES = {
  "community_unavailable",
  "request_in_progress",
  "rate_limited",
  "temporarily_unavailable",
}
_publication_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()


def _publication_lock(local_app_id: str) -> asyncio.Lock:
  lock = _publication_locks.get(local_app_id)
  if lock is None:
    lock = asyncio.Lock()
    _publication_locks[local_app_id] = lock
  return lock


def _store_github_owner(
  owner: models.Owner = Depends(get_owner_or_app_with_manage_apps),
  _github: models.Owner = Depends(get_owner_or_app_with_github_access),
) -> models.Owner:
  """Require both Store authority and the separately revocable GitHub grant."""
  return owner


class PublishLocalGitHubAppIn(BaseModel):
  app_id: int = Field(gt=0)
  repository_name: str = Field(
    min_length=1, max_length=100,
    pattern=r"^[A-Za-z0-9_.-]+$",
  )
  confirm_source_public: Literal[True]
  public_identity: Literal["github"] = "github"


class ExistingGitHubRevisionIn(BaseModel):
  repository: str = Field(
    pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$",
  )
  commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
  manifest_path: str = Field(
    default="mobius.json", min_length=1, max_length=256,
    pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
  )
  public_identity: _PUBLIC_IDENTITY = "anonymous"
  contribution_id: str = Field(default="", max_length=200)


class InstallReceiptIn(BaseModel):
  local_app_id: str = Field(
    min_length=1, max_length=128,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
  )


def _idempotency(value: str | None) -> str:
  key = str(value or "")
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", key):
    raise HTTPException(400, "A valid Idempotency-Key is required.")
  return key


def _safe_public_id(value: str, label: str) -> str:
  if not _PUBLIC_ID.fullmatch(value):
    raise HTTPException(400, f"{label} is invalid.")
  return value


def _broker_error(exc: CommunityBrokerError) -> HTTPException:
  headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
  return HTTPException(
    status_code=exc.status_code,
    detail={"code": exc.code, "message": exc.detail},
    headers=headers,
  )


async def _broker_result(*args, **kwargs) -> tuple[Any, int, dict[str, str]]:
  try:
    return await community_broker.request(*args, **kwargs)
  except CommunityBrokerError as exc:
    raise _broker_error(exc) from exc


def _response(
  payload: Any, status: int, headers: dict[str, str],
) -> JSONResponse:
  normalized_headers = {
    str(key).lower(): value for key, value in headers.items()
  }
  outgoing = {
    key.title(): value for key, value in normalized_headers.items()
    if key in {"etag", "last-modified", "retry-after"}
  }
  outgoing["Cache-Control"] = "no-store"
  return JSONResponse(payload, status_code=status, headers=outgoing)


async def _request(*args, **kwargs) -> JSONResponse:
  return _response(*(await _broker_result(*args, **kwargs)))


def _error_detail(
  exc: HTTPException, *, default_code: str,
) -> tuple[str, str]:
  detail = exc.detail
  if isinstance(detail, dict):
    code = str(detail.get("code") or default_code)[:128]
    message = str(detail.get("message") or "The Store listing request failed.")
  else:
    code = default_code
    message = str(detail or "The Store listing request failed.")
  return code, message[:400]


def _retryable_admission(status_code: int, code: str) -> bool:
  return (
    code in _RETRYABLE_ADMISSION_CODES
    or status_code in {408, 425, 429}
    or status_code >= 500
  )


def _journal_projection(
  journal: CommunityPublicationJournal,
) -> dict[str, Any]:
  return {
    "id": f"local-publication:{journal.id}",
    "local_app_id": journal.local_app_id,
    "status": journal.state,
    "repository": journal.repository,
    "repository_url": f"https://github.com/{quote(journal.repository, safe='/')}",
    "accepted_commit": journal.accepted_commit,
    "commit_sha": journal.source_commit_sha,
    "admission_commit_sha": journal.admission_commit_sha,
    "admission": {
      "code": journal.admission_code,
      "message": journal.admission_message,
      "status_code": journal.admission_status_code,
      "retryable": bool(journal.admission_retryable),
    },
    "created_at": journal.created_at,
    "updated_at": journal.updated_at,
  }


def _latest_local_publications() -> list[dict[str, Any]]:
  rows = sorted(
    list_publication_journals(), key=lambda row: row.updated_at, reverse=True,
  )
  return [_journal_projection(row) for row in rows]


def _merge_publication_state(
  payload: Any, local_items: list[dict[str, Any]],
) -> Any:
  if not local_items:
    return payload
  if isinstance(payload, list):
    remote_items = payload
    container: dict[str, Any] | None = None
  elif isinstance(payload, dict):
    remote_items = payload.get("items")
    if not isinstance(remote_items, list):
      remote_items = []
    container = dict(payload)
  else:
    remote_items = []
    container = {}

  local_by_app = {item["local_app_id"]: item for item in local_items}
  merged = []
  for item in remote_items:
    if not isinstance(item, dict):
      merged.append(item)
      continue
    local = local_by_app.pop(str(item.get("local_app_id") or ""), None)
    if local and _remote_publication_is_current(item, local):
      delete_publication_journal(local["local_app_id"])
      merged.append(item)
    else:
      merged.append({**item, **local} if local else item)
  local_only_count = len(local_by_app)
  merged.extend(local_by_app.values())
  if container is None:
    return merged
  container["items"] = merged
  if isinstance(container.get("total"), int):
    container["total"] += local_only_count
  return container


def _publication_timestamp(value: Any) -> float | None:
  try:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
      parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
  except (OSError, OverflowError, ValueError):
    return None


def _remote_publication_is_current(
  remote: dict[str, Any], local: dict[str, Any],
) -> bool:
  if str(remote.get("status") or "").casefold() != "live":
    return False
  same_repository = (
    str(remote.get("repository") or "").casefold()
    == str(local.get("repository") or "").casefold()
  )
  remote_commit = str(remote.get("commit_sha") or "").casefold()
  if same_repository and remote_commit in {
    str(local.get("commit_sha") or "").casefold(),
    str(local.get("admission_commit_sha") or "").casefold(),
  }:
    return bool(remote_commit)
  remote_updated = _publication_timestamp(remote.get("updated_at"))
  local_updated = _publication_timestamp(local.get("updated_at"))
  return (
    remote_updated is not None
    and local_updated is not None
    and remote_updated >= local_updated
  )


def _publication_journal(
  *,
  local_app_id: str,
  accepted_commit: str,
  repository_name: str,
) -> CommunityPublicationJournal | None:
  journal = read_publication_journal(local_app_id)
  if journal is None or (
    journal.accepted_commit != accepted_commit
    or journal.repository_name != repository_name
  ):
    return None
  return journal


def _record_source_public(
  journal: CommunityPublicationJournal,
) -> None:
  journal.state = "listing_pending"
  journal.admission_code = "admission_pending"
  journal.admission_message = "The public source is waiting for Store admission."
  journal.admission_status_code = None
  journal.admission_retryable = True
  journal.updated_at = datetime.now(UTC).isoformat()
  write_publication_journal(journal)


def _record_admission_failure(
  journal: CommunityPublicationJournal,
  exc: HTTPException,
) -> tuple[str, str, bool]:
  code, message = _error_detail(exc, default_code="host_admission_failed")
  retryable = _retryable_admission(exc.status_code, code)
  journal.state = "listing_pending" if retryable else "failed"
  journal.admission_code = code
  journal.admission_message = message
  journal.admission_status_code = exc.status_code
  journal.admission_retryable = retryable
  journal.updated_at = datetime.now(UTC).isoformat()
  write_publication_journal(journal)
  return code, message, retryable


def _record_admission_success(
  journal: CommunityPublicationJournal,
) -> None:
  journal.state = "live"
  journal.admission_code = ""
  journal.admission_message = ""
  journal.admission_status_code = None
  journal.admission_retryable = False
  journal.updated_at = datetime.now(UTC).isoformat()
  write_publication_journal(journal)


def _release_proof_marker(
  *, issuer: str, subject: str, repository: str,
  parent_sha: str, manifest_path: str,
) -> str:
  material = "\n".join((
    issuer, subject, "publish", repository.casefold(), parent_sha, manifest_path,
  ))
  return "[mobius-store-proof:" + hashlib.sha256(
    material.encode("utf-8"),
  ).hexdigest()[:32] + "]"


def _local_repository_marker(
  *, issuer: str, subject: str, repository: str, local_app_id: str,
) -> str:
  material = "\n".join((
    issuer, subject, "local-app", repository.casefold(), local_app_id,
  ))
  return "[mobius-store-repository:" + hashlib.sha256(
    material.encode("utf-8"),
  ).hexdigest()[:32] + "]"


def _local_release_marker(
  *, repository_marker: str, accepted_commit: str,
) -> str:
  return "[mobius-store-source:" + hashlib.sha256(
    f"{repository_marker}\n{accepted_commit}".encode("utf-8"),
  ).hexdigest()[:32] + "]"


def _local_admission_key(
  *,
  issuer: str,
  subject: str,
  local_app_id: str,
  accepted_commit: str,
  repository: str,
  source_commit_sha: str,
  manifest_path: str,
  public_identity: str,
) -> str:
  """Bind Host idempotency to the exact public-source admission intent."""
  material = "\n".join((
    issuer,
    subject,
    local_app_id,
    accepted_commit,
    repository.casefold(),
    source_commit_sha.casefold(),
    manifest_path,
    public_identity,
  ))
  return "store:publish-local:" + hashlib.sha256(
    material.encode("utf-8"),
  ).hexdigest()


async def _github_json(
  client: httpx.AsyncClient, method: str, path: str,
  *, body: dict[str, Any] | None = None, allow_not_found: bool = False,
) -> tuple[dict[str, Any] | None, int]:
  response = await client.request(method, path, json=body)
  if allow_not_found and response.status_code == 404:
    return None, 404
  if response.is_error:
    try:
      detail = str(response.json().get("message") or "GitHub request failed.")
    except (ValueError, AttributeError):
      detail = "GitHub request failed."
    raise HTTPException(response.status_code, detail[:400])
  try:
    payload = response.json()
  except ValueError as exc:
    raise HTTPException(502, "GitHub returned an invalid response.") from exc
  if not isinstance(payload, dict):
    raise HTTPException(502, "GitHub returned an invalid response.")
  return payload, response.status_code


@router.get("/identity")
async def community_identity(
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> JSONResponse:
  return await _request("GET", "/identity")


@router.get("/github-status")
async def community_github_status(
  _: models.Owner = Depends(_store_github_owner),
) -> dict[str, str | bool]:
  """Expose only the local GitHub identity needed by Store journeys.

  Connection setup and credential details remain owned by Contribute. The
  Store inherits the already-reviewed local authority without receiving the
  token, OAuth controls, scopes, or resumable device-flow state.
  """
  state = github_auth.read_state() or {}
  connected = bool(state.get("token"))
  return {
    "connected": connected,
    "login": str(state.get("login") or "") if connected else "",
  }


@router.get("/apps")
async def list_community_apps(
  q: str = Query(default="", max_length=160),
  limit: int = Query(default=50, ge=1, le=100),
  offset: int = Query(default=0, ge=0, le=10_000),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> JSONResponse:
  return await _request(
    "GET", f"{COMMUNITY_PREFIX}/apps",
    params={"q": q, "limit": limit, "offset": offset},
  )


@router.get("/publications")
async def list_community_publications(
  limit: int = Query(default=100, ge=1, le=100),
  offset: int = Query(default=0, ge=0, le=10_000),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> JSONResponse:
  """Return the current identity's durable publication lifecycle records."""
  try:
    local_items = _latest_local_publications()
  except CommunityPublicationError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail},
    ) from exc
  try:
    payload, status, headers = await _broker_result(
      "GET", f"{COMMUNITY_PREFIX}/publications",
      params={"limit": limit, "offset": offset},
    )
  except HTTPException as exc:
    if not local_items:
      raise
    code, message = _error_detail(exc, default_code="community_unavailable")
    return _response(
      {
        "items": local_items,
        "registry_unavailable": {
          "code": code,
          "message": message,
          "retryable": True,
        },
      },
      200,
      exc.headers or {},
    )
  return _response(
    _merge_publication_state(payload, local_items), status, headers,
  )


@router.get("/apps/{app_id}")
async def get_community_app(
  app_id: str,
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> JSONResponse:
  return await _request(
    "GET", f"{COMMUNITY_PREFIX}/apps/{_safe_public_id(app_id, 'App id')}",
  )


@router.get("/apps/{app_id}/revisions/{revision_id}")
async def get_community_revision(
  app_id: str,
  revision_id: str,
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> JSONResponse:
  return await _request(
    "GET",
    f"{COMMUNITY_PREFIX}/apps/{_safe_public_id(app_id, 'App id')}"
    f"/revisions/{_safe_public_id(revision_id, 'Revision id')}",
  )


@router.post(
  "/publications/github",
  dependencies=[Depends(reject_cross_site)],
)
async def publish_local_app_to_github(
  body: PublishLocalGitHubAppIn,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(_store_github_owner),
  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
  """Publish one accepted local app revision through the inherited GitHub account.

  GitHub remains the public source of truth. The partial-success journal is
  committed after that exact source becomes public and before Host admission,
  so a reload or retry can finish the listing without repeating publication.
  """
  _idempotency(idempotency_key)
  app = (
    db.query(models.App)
    .filter(models.App.id == body.app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if app is None:
    raise HTTPException(404, "App not found.")
  token = github_auth.get_token()
  if not token:
    raise HTTPException(401, "Connect GitHub in Contribute before publishing.")
  try:
    identity, _, _ = await community_broker.request("GET", "/identity")
  except CommunityBrokerError as exc:
    raise _broker_error(exc) from exc
  issuer = str(identity.get("issuer") or "") if isinstance(identity, dict) else ""
  subject = str(identity.get("subject") or "") if isinstance(identity, dict) else ""
  if not issuer or not subject:
    raise HTTPException(409, "Link a Möbius identity before publishing.")

  try:
    async with fs_locks.source_dir_lock(str(app.source_dir)):
      accepted_commit, files = await asyncio.to_thread(build_public_snapshot, app)
  except CommunityPublicationError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail},
    ) from exc

  local_app_id = f"app:{app.id}:{app.slug}"
  async with _publication_lock(local_app_id):
    repository_name = body.repository_name.casefold()
    journal = _publication_journal(
      local_app_id=local_app_id,
      accepted_commit=accepted_commit,
      repository_name=repository_name,
    )
    if journal is not None:
      repository = journal.repository
      source_commit_sha = journal.source_commit_sha
      if (
        not re.fullmatch(r"[0-9a-f]{40}", source_commit_sha)
        or not re.fullmatch(
          r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository,
        )
      ):
        raise HTTPException(
          409,
          {
            "code": "publication_journal_invalid",
            "message": "The saved publication state is incomplete. Repair it before retrying.",
          },
        )
      if journal.state == "live":
        return _response(_journal_projection(journal), 200, {})
      if journal.state == "failed" and not journal.admission_retryable:
        raise HTTPException(
          journal.admission_status_code or 409,
          {
            "code": journal.admission_code or "host_admission_failed",
            "message": journal.admission_message,
            "retryable": False,
            "publication": _journal_projection(journal),
          },
        )
    else:
      repository, source_commit_sha = await _publish_local_source(
        app=app,
        token=token,
        issuer=issuer,
        subject=subject,
        local_app_id=local_app_id,
        repository_name=body.repository_name,
        accepted_commit=accepted_commit,
        files=files,
      )
      journal = new_publication_journal(
        local_app_id=local_app_id,
        accepted_commit=accepted_commit,
        repository_name=repository_name,
        repository=repository,
        source_commit_sha=source_commit_sha,
      )

    host_key = _local_admission_key(
      issuer=issuer,
      subject=subject,
      local_app_id=local_app_id,
      accepted_commit=accepted_commit,
      repository=repository,
      source_commit_sha=source_commit_sha,
      manifest_path="mobius.json",
      public_identity="github",
    )
    _record_source_public(journal)
    try:
      admission_payload, admission_key = await _prepare_existing_github_revision(
        ExistingGitHubRevisionIn(
          repository=repository,
          commit_sha=source_commit_sha,
          manifest_path="mobius.json",
          public_identity="github",
        ),
        host_key,
        local_app_id=local_app_id,
      )
      journal.admission_commit_sha = str(
        (admission_payload.get("github") or {}).get("commit_sha") or "",
      ).casefold()
      _record_source_public(journal)
      response = await _request(
        "POST", f"{COMMUNITY_PREFIX}/apps",
        body=admission_payload,
        idempotency_key=admission_key,
      )
    except HTTPException as exc:
      code, message, retryable = _record_admission_failure(journal, exc)
      raise HTTPException(
        exc.status_code,
        {
          "code": code,
          "message": message,
          "retryable": retryable,
          "publication": _journal_projection(journal),
        },
        headers=exc.headers,
      ) from exc
    _record_admission_success(journal)

  response.headers["X-Mobius-Accepted-Source-Commit"] = accepted_commit
  response.headers["X-Mobius-GitHub-Repository"] = repository
  return response


async def _publish_local_source(
  *,
  app: models.App,
  token: str,
  issuer: str,
  subject: str,
  local_app_id: str,
  repository_name: str,
  accepted_commit: str,
  files: list[dict[str, str]],
) -> tuple[str, str]:
  """Publish one accepted source tree, or reuse its managed GitHub commit."""
  headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "mobius-app-store",
  }
  async with httpx.AsyncClient(
    base_url="https://api.github.com", headers=headers,
    follow_redirects=False, timeout=30,
  ) as github:
    user, _ = await _github_json(github, "GET", "/user")
    login = str(user.get("login") or "")
    if not login or not isinstance(user.get("id"), int):
      raise HTTPException(502, "GitHub did not return the connected account.")
    repository = f"{login}/{repository_name}"
    encoded_repo = quote(repository, safe="/")
    repo, repo_status = await _github_json(
      github, "GET", f"/repos/{encoded_repo}", allow_not_found=True,
    )
    if repo_status == 404:
      repo, _ = await _github_json(
        github, "POST", "/user/repos",
        body={
          "name": repository_name,
          "description": str(app.description or f"{app.name} for Möbius")[:350],
          "private": False,
          "auto_init": False,
        },
      )
    if (
      str(repo.get("full_name") or "").casefold() != repository.casefold()
      or repo.get("private") is True
      or (repo.get("owner") or {}).get("id") != user.get("id")
    ):
      raise HTTPException(409, "That GitHub repository is not reusable for this app.")

    repository_marker = _local_repository_marker(
      issuer=issuer, subject=subject, repository=repository,
      local_app_id=local_app_id,
    )
    release_marker = _local_release_marker(
      repository_marker=repository_marker, accepted_commit=accepted_commit,
    )
    ref_path = f"/repos/{encoded_repo}/git/ref/heads/main"
    ref, ref_status = await _github_json(
      github, "GET", ref_path, allow_not_found=True,
    )
    parent_sha = ""
    parent_message = ""
    if ref_status != 404:
      parent_sha = str((ref.get("object") or {}).get("sha") or "").lower()
      if not re.fullmatch(r"[0-9a-f]{40}", parent_sha):
        raise HTTPException(409, "GitHub could not resolve the repository main branch.")
      parent, _ = await _github_json(
        github, "GET", f"/repos/{encoded_repo}/commits/{parent_sha}",
      )
      parent_message = str((parent.get("commit") or {}).get("message") or "")
      if repository_marker not in parent_message:
        raise HTTPException(
          409,
          "That repository already exists and was not created for this local app. Choose another name.",
        )

    if release_marker in parent_message:
      return repository, parent_sha

    tree_items = []
    for item in files:
      blob, _ = await _github_json(
        github, "POST", f"/repos/{encoded_repo}/git/blobs",
        body={
          "content": item["content_base64"],
          "encoding": "base64",
        },
      )
      blob_sha = str(blob.get("sha") or "").lower()
      if not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
        raise HTTPException(502, "GitHub did not return a source blob.")
      tree_items.append({
        "path": item["path"],
        "mode": item["mode"],
        "type": "blob",
        "sha": blob_sha,
      })
    tree, _ = await _github_json(
      github, "POST", f"/repos/{encoded_repo}/git/trees",
      body={"tree": tree_items},
    )
    tree_sha = str(tree.get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
      raise HTTPException(502, "GitHub did not return the source tree.")
    commit, _ = await _github_json(
      github, "POST", f"/repos/{encoded_repo}/git/commits",
      body={
        "message": (
          f"Publish {app.name} from Möbius\n\n"
          f"{repository_marker}\n{release_marker}"
        ),
        "tree": tree_sha,
        "parents": [parent_sha] if parent_sha else [],
      },
    )
    source_commit_sha = str(commit.get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit_sha):
      raise HTTPException(502, "GitHub did not return the source commit.")
    if ref_status == 404:
      await _github_json(
        github, "POST", f"/repos/{encoded_repo}/git/refs",
        body={"ref": "refs/heads/main", "sha": source_commit_sha},
      )
    else:
      await _github_json(
        github, "PATCH", f"/repos/{encoded_repo}/git/refs/heads/main",
        body={"sha": source_commit_sha, "force": False},
      )
    return repository, source_commit_sha


async def _prepare_existing_github_revision(
  body: ExistingGitHubRevisionIn,
  idempotency_key: str,
  *,
  local_app_id: str = "",
) -> tuple[dict[str, Any], str]:
  key = _idempotency(idempotency_key)
  if body.contribution_id:
    return (
      {
        "github": {
          "repository": body.repository,
          "commit_sha": body.commit_sha.lower(),
          "manifest_path": body.manifest_path,
        },
        "public_identity": body.public_identity,
        "contribution_id": body.contribution_id,
      },
      key,
    )
  if body.public_identity != "github":
    raise HTTPException(
      400, "Inherited GitHub publication uses your public GitHub identity.",
    )

  token = github_auth.get_token()
  if not token:
    raise HTTPException(401, "Connect GitHub in Contribute before publishing.")
  try:
    identity, _, _ = await community_broker.request("GET", "/identity")
  except CommunityBrokerError as exc:
    raise _broker_error(exc) from exc
  issuer = str(identity.get("issuer") or "") if isinstance(identity, dict) else ""
  subject = str(identity.get("subject") or "") if isinstance(identity, dict) else ""
  if not issuer or not subject:
    raise HTTPException(409, "Link a Möbius identity before publishing.")

  requested_repo = body.repository
  parent_sha = body.commit_sha.lower()
  headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "mobius-app-store",
  }
  async with httpx.AsyncClient(
    base_url="https://api.github.com", headers=headers,
    follow_redirects=False, timeout=20,
  ) as client:
    user, _ = await _github_json(client, "GET", "/user")
    encoded_repo = quote(requested_repo, safe="/")
    repo, _ = await _github_json(client, "GET", f"/repos/{encoded_repo}")
    canonical_repo = str(repo.get("full_name") or "")
    if repo.get("private") is True:
      raise HTTPException(400, "The App Store can list only public repositories.")
    if (
      canonical_repo.casefold() != requested_repo.casefold()
      or (repo.get("owner") or {}).get("id") != user.get("id")
    ):
      raise HTTPException(
        403, "The connected GitHub account must own this repository.",
      )
    parent, _ = await _github_json(
      client, "GET", f"/repos/{encoded_repo}/git/commits/{parent_sha}",
    )
    tree_sha = str((parent.get("tree") or {}).get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
      raise HTTPException(409, "GitHub could not resolve that exact commit.")
    marker = _release_proof_marker(
      issuer=issuer, subject=subject, repository=canonical_repo,
      parent_sha=parent_sha, manifest_path=body.manifest_path,
    )
    branch = "mobius-release-" + marker[-17:-1]
    ref_path = f"/repos/{encoded_repo}/git/ref/heads/{quote(branch, safe='')}"
    ref, ref_status = await _github_json(
      client, "GET", ref_path, allow_not_found=True,
    )
    if ref_status == 404:
      proof_commit, _ = await _github_json(
        client, "POST", f"/repos/{encoded_repo}/git/commits",
        body={
          "message": f"Möbius App Store release proof\n\n{marker}",
          "tree": tree_sha,
          "parents": [parent_sha],
        },
      )
      proof_sha = str(proof_commit.get("sha") or "").lower()
      if not re.fullmatch(r"[0-9a-f]{40}", proof_sha):
        raise HTTPException(502, "GitHub did not return the release proof commit.")
      await _github_json(
        client, "POST", f"/repos/{encoded_repo}/git/refs",
        body={"ref": f"refs/heads/{branch}", "sha": proof_sha},
      )
    else:
      proof_sha = str((ref.get("object") or {}).get("sha") or "").lower()
      proof_commit, _ = await _github_json(
        client, "GET", f"/repos/{encoded_repo}/commits/{proof_sha}",
      )
      proof_message = str((proof_commit.get("commit") or {}).get("message") or "")
      proof_parents = {
        str(item.get("sha") or "").lower()
        for item in (proof_commit.get("parents") or []) if isinstance(item, dict)
      }
      if marker not in proof_message or parent_sha not in proof_parents:
        raise HTTPException(
          409, "The existing Möbius release proof branch does not match this revision.",
        )

  payload = {
    "github": {
      "repository": canonical_repo,
      "commit_sha": proof_sha,
      "manifest_path": body.manifest_path,
    },
    "public_identity": "github",
    "ownership_proof": {
      "kind": "github_commit_v1",
      "parent_sha": parent_sha,
    },
  }
  if local_app_id:
    payload["local_app_id"] = local_app_id
  return payload, key


async def _publish_existing_github_revision(
  body: ExistingGitHubRevisionIn,
  idempotency_key: str,
  *,
  local_app_id: str = "",
) -> JSONResponse:
  payload, key = await _prepare_existing_github_revision(
    body, idempotency_key, local_app_id=local_app_id,
  )
  return await _request(
    "POST", f"{COMMUNITY_PREFIX}/apps",
    body=payload,
    idempotency_key=key,
  )


@router.post(
  "/apps",
  dependencies=[Depends(reject_cross_site)],
)
async def publish_existing_github_revision(
  body: ExistingGitHubRevisionIn,
  _: models.Owner = Depends(_store_github_owner),
  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
  return await _publish_existing_github_revision(body, _idempotency(idempotency_key))


@router.post(
  "/apps/{app_id}/revisions/{revision_id}/installs",
  dependencies=[Depends(reject_cross_site)],
)
async def record_community_install(
  app_id: str,
  revision_id: str,
  body: InstallReceiptIn,
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
  """Keep one exact installed revision available in the Host release cache."""
  safe_app_id = _safe_public_id(app_id, "App id")
  safe_revision_id = _safe_public_id(revision_id, "Revision id")
  return await _request(
    "POST",
    f"{COMMUNITY_PREFIX}/apps/{safe_app_id}/revisions/"
    f"{safe_revision_id}/installs",
    body=body.model_dump(), idempotency_key=_idempotency(idempotency_key),
  )
