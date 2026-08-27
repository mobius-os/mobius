"""Same-origin BFF for the GitHub-backed Möbius community catalog."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from typing import Any, Literal
from urllib.parse import quote

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
  CommunityPublicationError,
  build_public_snapshot,
  public_store_listing,
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


async def _request(*args, **kwargs) -> JSONResponse:
  try:
    payload, status, headers = await community_broker.request(*args, **kwargs)
  except CommunityBrokerError as exc:
    raise _broker_error(exc) from exc
  outgoing = {
    key.title(): value for key, value in headers.items()
    if key in {"etag", "last-modified", "retry-after"}
  }
  outgoing["Cache-Control"] = "no-store"
  return JSONResponse(payload, status_code=status, headers=outgoing)


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
  return await _request(
    "GET", f"{COMMUNITY_PREFIX}/publications",
    params={"limit": limit, "offset": offset},
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

  GitHub remains the public source of truth. The Host receives only the same
  exact-revision proof used for an existing public repository; the local token
  and unpublished worktree never cross either boundary.
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
      await asyncio.to_thread(public_store_listing, files)
  except CommunityPublicationError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail},
    ) from exc

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
    repository = f"{login}/{body.repository_name}"
    encoded_repo = quote(repository, safe="/")
    repo, repo_status = await _github_json(
      github, "GET", f"/repos/{encoded_repo}", allow_not_found=True,
    )
    if repo_status == 404:
      repo, _ = await _github_json(
        github, "POST", "/user/repos",
        body={
          "name": body.repository_name,
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

    local_app_id = f"app:{app.id}:{app.slug}"
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
      source_commit_sha = parent_sha
    else:
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

  host_key = "store:publish-local:" + hashlib.sha256(
    f"{issuer}\n{subject}\n{local_app_id}\n{accepted_commit}".encode("utf-8"),
  ).hexdigest()
  try:
    response = await _publish_existing_github_revision(
      ExistingGitHubRevisionIn(
        repository=repository,
        commit_sha=source_commit_sha,
        manifest_path="mobius.json",
        public_identity="github",
      ),
      host_key,
      local_app_id=local_app_id,
    )
  except HTTPException as exc:
    raise HTTPException(
      502,
      {
        "code": "listing_pending",
        "message": (
          "The source is public on GitHub, but the Store listing is not live yet. "
          "Publish again to finish it."
        ),
        "repository": repository,
        "commit_sha": source_commit_sha,
      },
    ) from exc
  response.headers["X-Mobius-Accepted-Source-Commit"] = accepted_commit
  response.headers["X-Mobius-GitHub-Repository"] = repository
  return response


@router.get("/publications/github/preview")
async def preview_local_app_publication(
  app_id: int = Query(gt=0),
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
) -> dict[str, Any]:
  """Return the exact accepted listing that the next publish would use."""
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if app is None:
    raise HTTPException(404, "App not found.")
  try:
    async with fs_locks.source_dir_lock(str(app.source_dir)):
      accepted_commit, files = await asyncio.to_thread(build_public_snapshot, app)
      listing = await asyncio.to_thread(public_store_listing, files)
  except CommunityPublicationError as exc:
    raise HTTPException(
      exc.status_code, {"code": exc.code, "message": exc.detail},
    ) from exc
  return {
    "app_id": app.id,
    "name": app.name,
    "slug": app.slug,
    "accepted_commit": accepted_commit,
    "repository_name": app.slug,
    "icon_url": f"/api/apps/{app.id}/icon?size=128&v={quote(str(app.updated_at), safe='')}",
    "asset_base": f"/app-assets/by-id/{app.id}/",
    "listing": listing,
  }


async def _publish_existing_github_revision(
  body: ExistingGitHubRevisionIn,
  idempotency_key: str,
  *,
  local_app_id: str = "",
) -> JSONResponse:
  key = _idempotency(idempotency_key)
  if body.contribution_id:
    return await _request(
      "POST", f"{COMMUNITY_PREFIX}/apps",
      body={
        "github": {
          "repository": body.repository,
          "commit_sha": body.commit_sha.lower(),
          "manifest_path": body.manifest_path,
        },
        "public_identity": body.public_identity,
        "contribution_id": body.contribution_id,
      },
      idempotency_key=key,
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
