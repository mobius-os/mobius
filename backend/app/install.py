"""Atomic install + update lifecycle for mini-apps from a manifest.

The store mini-app (and future bootstrap hook) hands the backend a
`mobius.json` URL or inline manifest. This module does the rest:
fetch entry JSX, create/update the App row, compile, write
source_dir for explicit source apply, seed storage, upload icon, register
cron. Wrapped in a single SQLAlchemy transaction with on-failure
filesystem cleanup so partial installs don't land.

Why this is server-side, not in the store mini-app:
  - Mini-apps can only PUT into their OWN storage scope, but install
    seeds another app's scope (target's `/data/apps/<new_id>/`).
  - Mini-apps can't shell out to `init-cron-scaffold.sh`; cron needs
    a subprocess + crontab access that lives only in the container.
  - Mini-apps can't write `/data/apps/<slug>/index.jsx` (source_dir), so they
    cannot prepare source for the explicit apply lifecycle.
  - 4-step client-side flow (POST app, PUT seeds, PUT icon, mark cron)
    can leave the DB row with missing seeds + missing source_dir on a
    mid-flight failure. One transaction here makes that all-or-nothing.

See feature ticket 062 for the design rationale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import (
  activity,
  app_git,
  app_python_env,
  applied_app_runtime,
  drawer_pins,
  fs_locks,
  icon_assets,
  managed_paths,
  models,
  settled,
)
from app import app_consent, app_cron
from app.build_admission import BuildLeaseUnavailable
from app.app_capabilities import contract_and_digest
from app.app_compile_contract import RolldownStalled
from app.app_source_check import SourceCheckResult, check_app_source
from app.compiler import (
  CompileError,
  compile_jsx,
  owned_bundle_path,
  publish_staged_bundle,
  unlink_app_bundle,
)
from app.config import get_settings
from app.manifest_identity import (
  MANIFEST_ID_MARKER as _MANIFEST_ID_MARKER,
  canonical_manifest_base as _canonical_base,
  canonical_manifest_identity_key as _canonical_identity_key,
  parse_root_manifest,
  requested_manifest_source,
  require_bound_manifest as _require_bound_manifest,
  require_permanent_package,
  stored_manifest_fetch_url,
)
from app.manifest_contract import (
  EXECUTABLE_MANIFEST_FIELDS,
  MANIFEST_MAX_BYTES as _CONTRACT_MANIFEST_MAX_BYTES,
  PACKAGE_MAX_BYTES as _CONTRACT_PACKAGE_MAX_BYTES,
  SYSTEM_PROMPT_MAX_BYTES as _CONTRACT_SYSTEM_PROMPT_MAX_BYTES,
  REQUIRED_STRING_FIELDS,
  ManifestContractError,
  package_input_paths,
  package_bytes,
  package_limit_message,
  python_lock,
  static_asset_entries,
  validate_manifest_contract,
  validate_schedule_job,
  validate_storage_destination,
)
# Keep the underscore alias: install._http_get calls _validate_url_safe, and
# the install tests patch `app.install._validate_url_safe`. The canonical
# validator now lives in net_utils (shared with routes/proxy.py) — see
# net_utils.py for why the two SSRF validators were unified.
from app.net_utils import (
  MAX_REDIRECTS as _MAX_REDIRECTS,
  pinned_client,
  validate_url_safe as _validate_url_safe,
)
from app.storage_io import atomic_write
# Moved out of this module and imported by name: tests patch `_stage`,
# `fetch_app_commit`, `_sync_app_skills` and `clear_pending_conflict_update`
# here to reach the install path, and routes call `fetch_app_commit` and
# apply calls `_sync_app_skills` through this module for the same reason.
from app.app_skills import _sync_app_skills
from app.git_transfers import (
  _stage, discard_staged, fetch_app_commit, git_transfer_root,
)
from app.pending_update import (
  clear_pending_conflict_update,
  committed_pending_resolution,
  stage_pending_conflict_update,
)
from app.terminal_output import strip_terminal_noise
from app.app_identity import (
  allocate_unique_slug,
  reject_if_source_dir_taken as _reject_if_source_dir_taken,
)

log = logging.getLogger("mobius.install")


def _publish_install_bundle(
  app,
  staged_bundle: Path,
  rollback_actions: list[Callable[[], None]],
  commit_actions: list[Callable[[], None]],
) -> Path:
  """Publish an install artifact before commit and wire symmetric cleanup.

  The DB row continues to reference ``previous`` until its transaction commits,
  so publishing the immutable content path cannot expose uncommitted code. A
  rollback removes the new orphan; a successful commit removes the superseded
  bundle. A process crash on either side is cleaned by the startup orphan reaper.
  """
  previous = owned_bundle_path(app.id, app.compiled_path)
  try:
    published = publish_staged_bundle(app.id, staged_bundle)
  except Exception:
    staged_bundle.unlink(missing_ok=True)
    raise
  app.compiled_path = str(published)
  if previous != published:
    rollback_actions.append(
      lambda a=app.id, p=published: unlink_app_bundle(a, p)
    )
    commit_actions.append(
      lambda a=app.id, p=previous: unlink_app_bundle(a, p)
    )
  return published

# Manifest fetch cap. A legitimate manifest is < 4 KB. The cap is
# the safety net against malicious URLs streaming GB of data.
_MANIFEST_MAX_BYTES = _CONTRACT_MANIFEST_MAX_BYTES

# Total bytes one install may download for every file the manifest declares.
# See `manifest_contract.PACKAGE_MAX_BYTES`; `_PackageDownload` enforces it.
_PACKAGE_MAX_BYTES = _CONTRACT_PACKAGE_MAX_BYTES

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _manifest_color(value) -> str | None:
  """Return a safe #RRGGBB color from mobius.json, or None."""
  if not isinstance(value, str):
    return None
  value = value.strip()
  if not _HEX_COLOR_RE.match(value):
    return None
  return value.lower()


# Web-manifest `display` values an app may request. Anything else drops to
# None so the served manifest falls back to "standalone" rather than emitting
# a bogus mode. "fullscreen" asks for the browser's most immersive supported
# presentation; iOS can retain its OS status bar, so it never replaces insets.
_VALID_DISPLAY = frozenset(("standalone", "fullscreen", "minimal-ui", "browser"))


def _manifest_display(value) -> str | None:
  """Return a safe web-manifest `display` value from mobius.json, or None."""
  if not isinstance(value, str):
    return None
  value = value.strip().lower()
  return value if value in _VALID_DISPLAY else None


def _compile_error_detail(app_name: str, exc: CompileError) -> str:
  """Return a concise client-safe compile error for a manifest install."""
  cleaned = strip_terminal_noise(exc.stderr or "")
  lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
  for line in lines:
    resolve_idx = line.find("Could not resolve")
    if resolve_idx >= 0:
      line = line[resolve_idx:]
      return (
        f"{app_name} failed to compile: {line} — its manifest likely "
        "omits a source file"
      )
  detail = lines[0] if lines else str(exc)
  return f"{app_name} failed to compile: {detail}"


# Static site assets declared by a manifest. These are for prebuilt apps that
# need durable files below /data/apps/<slug>/static (served at /app-assets/...),
# not one-off files dropped into the platform frontend.
_STATIC_ASSETS_MANIFEST = managed_paths.STATIC_ASSETS_MANIFEST
_STATIC_ASSETS_BACKUP_ASSET_PREFIX = "assets"
_STATIC_ASSETS_BACKUP_METADATA_PREFIX = "metadata"


# A system-prompt fragment is more privileged than a skill because it is read
# every turn. It uses the same root-level markdown and byte-size contract.
_SYSTEM_PROMPT_MAX_BYTES = _CONTRACT_SYSTEM_PROMPT_MAX_BYTES


# Tracked files in a merged tree that are NOT hand-written app source: the
# managed .gitignore, the install-managed static-asset manifest, and the cron
# script. The job script is dropped separately (its name is known only at call
# time). Excluding these keeps the source-write loop from rewriting an
# install-managed artifact a clean merge happened to carry on `main`.
_MERGED_NON_SOURCE = managed_paths.MERGED_NON_SOURCE

_HTTP_TIMEOUT = 15.0
_SIZE_LOOKUP_CONCURRENCY = 16
# The download size is optional metadata on the preview that gates Install, so
# a host that stalls HEAD requests costs this long at most, not a timeout per
# batch of lookups.
_SIZE_LOOKUP_DEADLINE = 3.0
# Reopening a listing sizes the same files again. Remembering each size for a
# few minutes keeps that off the request budget installs share, at the cost of
# a file edited meanwhile showing its earlier size until the TTL ends.
_FILE_SIZE_TTL = 600.0
_FILE_SIZE_CACHE_MAX = 512
# URL -> (monotonic time sized, bytes). Insertion order is oldest first.
_FILE_SIZE_CACHE: dict[str, tuple[float, int]] = {}

# Tests override this module attribute to prevent production cron mutation.
CRON_SCAFFOLD = app_cron.BAKED_CRON_SCAFFOLD
# Moved to `app_cron`; this name stays for callers and tests that patch it here.
_unregister_cron = app_cron.unregister_cron


def _validate_manifest(m: dict) -> None:
  """Raises HTTPException(400) with a precise message on any issue."""
  try:
    validate_manifest_contract(m)
  except ManifestContractError as exc:
    raise HTTPException(400, str(exc)) from exc
  # The dependency-free shared contract above is the complete shape/path
  # contract used by both installation and pre-publication validation. Keep the
  # adapter here limited to translating its exception into the HTTP boundary.
  return


def _validate_discovery_manifest(manifest: dict) -> None:
  """Keep shared identity/source guards without gating on unused metadata.

  Discovery compares source bytes, not installation readiness. Project
  templates, permissions, and other installation-only declarations must not
  hide that comparison, but identity, response metadata, source paths, and
  fetch bounds still use the install contract. Keep the original manifest
  intact for the source review digest; this projection is only validation.
  """
  if not isinstance(manifest, dict):
    raise HTTPException(400, "Manifest must be a JSON object.")
  fields = (
    *REQUIRED_STRING_FIELDS, "package_id", "moved_to", "previous_id",
    "previous_manifest_url", "source_files", "schedule",
  )
  _validate_manifest({key: manifest[key] for key in fields if key in manifest})


def _derive_raw_base(manifest_url: str) -> str:
  """Everything before the trailing filename — entry, icon, and seed
  file references resolve relative to this."""
  if "/" not in manifest_url:
    raise HTTPException(400, "Cannot derive raw_base from manifest_url.")
  return manifest_url.rsplit("/", 1)[0] + "/"


def _derive_repo_ref(manifest_url: str) -> tuple[str, str] | None:
  """The clone URL and ref of a root Git package, or None to fetch it over HTTP.

  Only a root ``mobius.json`` (``parse_root_manifest`` with a file name) is
  cloned, because a Git package is read from its repository root. A manifest
  in a repository subdirectory, at a ref containing a slash, or under another
  file name is imported over HTTP into a local Git repository instead. A
  stored identity key is resolved to its fetch address first.
  """
  root = parse_root_manifest(requested_manifest_source(manifest_url)[0])
  if root is None or root.filename is None:
    return None
  return root.origin_url, root.ref


def _normalize_raw_base(raw_base: str) -> str:
  """Return a fetch base suitable for joining manifest-relative paths."""
  if not isinstance(raw_base, str) or not raw_base.strip():
    raise HTTPException(400, "`raw_base` must be a non-empty URL.")
  base = raw_base.strip()
  parsed = urlparse(base)
  if parsed.scheme not in ("http", "https") or not parsed.hostname:
    raise HTTPException(400, "`raw_base` must be an http(s) URL.")
  if parsed.query or parsed.fragment:
    raise HTTPException(400, "`raw_base` must not include query or fragment.")
  return base if base.endswith("/") else base + "/"


def _trusted_catalog_origin_url(url_or_base: str) -> str | None:
  """The Git origin of a root package in the trusted ``mobius-os`` catalog.

  A ``mobius-os/<repo>`` repository is one app at every ref, so a pinned
  commit and a branch name the same origin.
  """
  root = parse_root_manifest(url_or_base)
  if root is None or root.repository[0] != "mobius-os":
    return None
  return root.origin_url


def _github_root_manifest_identity(url_or_base: str) -> tuple[str, str] | None:
  """Return the lowercased owner/repository of one root raw-GitHub package."""
  root = parse_root_manifest(url_or_base)
  return root.repository if root is not None else None


def _github_origin_url(url_or_base: str) -> str | None:
  root = parse_root_manifest(url_or_base)
  return root.origin_url if root is not None else None


def _reviewed_predecessor_source(
  manifest: dict, source_for_key: str,
) -> tuple[str, tuple[str, str] | None]:
  """Resolve a package rename without allowing cross-owner adoption."""
  previous_source = manifest.get("previous_manifest_url")
  if not previous_source:
    return source_for_key, None
  current_identity = _github_root_manifest_identity(source_for_key)
  previous_identity = _github_root_manifest_identity(previous_source)
  if (
    current_identity is None
    or previous_identity is None
    or current_identity[0] != previous_identity[0]
  ):
    raise HTTPException(
      400,
      "Manifest repository renames must use root GitHub manifests owned by "
      "the same account.",
    )
  old_origin = _github_origin_url(previous_source)
  new_origin = _github_origin_url(source_for_key)
  if old_origin is None or new_origin is None:
    raise HTTPException(400, "Manifest repository rename is invalid.")
  return previous_source, (old_origin, new_origin)


def _trusted_origin_catalog_identity_matches(
  app: models.App,
  source_url: str,
  manifest_id: str,
) -> bool:
  """Whether a local identity-free row has the catalog package's Git origin."""
  if app.manifest_url is not None or app.slug != manifest_id:
    return False
  return _trusted_catalog_origin_matches(app.source_dir, source_url)


def _trusted_catalog_origin_matches(
  source_dir: str | Path,
  source_url: str,
) -> bool:
  """Whether an app checkout has the canonical catalog repository origin."""
  expected_origin = _trusted_catalog_origin_url(source_url)
  if expected_origin is None:
    return False
  return app_git.same_origin(app_git.origin_url(source_dir), expected_origin)


def trusted_catalog_checkout(
  identity: str | None,
  source_dir: str | Path,
  source_url: str,
  manifest_id: str,
) -> bool:
  """Whether an installed package's checkout is its trusted catalog repo."""
  return _catalog_identity_matches(
    identity, source_url, manifest_id,
  ) and _trusted_catalog_origin_matches(source_dir, source_url)


def replaces_migration_bridge(upstream_tree: dict, *, trusted_origin: bool) -> bool:
  """Whether install replaces the recorded upstream instead of continuing it.

  The one-time migration bridge records code but no manifest and shares no
  history with the real repository. Only a trusted origin may replace it
  outright; the Store's update check asks this same question so it never
  offers an update install cannot apply.
  """
  return trusted_origin and "mobius.json" not in upstream_tree


def _find_ref_independent_catalog_row(
  db: Session, canonical_manifest_url: str, manifest_id: str,
) -> models.App | None:
  """An existing row for the same GitHub repo + manifest id at ANY ref.

  Used only when the exact-ref identity lookups miss. Prefers a live row, then
  the lowest id, and re-verifies each candidate in Python because SQL `LIKE`
  treats `_` as a wildcard (a ref may contain one). Tombstone-agnostic like the
  primary identity lookup: an explicit install/update/pin-bump that reaches here
  revives a soft-deleted row in place (keeping its id + storage). Honoring an
  owner's uninstall of a `reinstall_after_uninstall=False` app is the bootstrap
  layer's job — it decides whether to call install at all.
  """
  repository = _github_root_manifest_identity(canonical_manifest_url)
  if repository is None:
    return None
  suffix = f"{_MANIFEST_ID_MARKER}{manifest_id}"
  candidates = (
    db.query(models.App)
    .filter(models.App.manifest_url.like(f"%{suffix}"))
    .order_by(
      case((models.App.deleted_at.is_(None), 0), else_=1),
      models.App.id.asc(),
    )
    .all()
  )
  for cand in candidates:
    url = cand.manifest_url or ""
    if (
      url.endswith(suffix)
      and _github_root_manifest_identity(url) == repository
    ):
      return cand
  return None


def _find_trusted_origin_catalog_row(
  db: Session,
  canonical_manifest_url: str,
  manifest_id: str,
) -> models.App | None:
  """Adopt a legacy local row only when its Git origin proves identity.

  Before catalog identity was persisted, an app could be built from the
  canonical repository yet retain ``manifest_url=NULL``. Slug equality alone
  is never enough to adopt owner data: a catalog package may legitimately use
  the same id as an unrelated local app. For the narrow trusted root-catalog
  shape, an exact canonical ``origin`` URL is the missing durable witness.
  """
  candidate = (
    db.query(models.App)
    .filter(
      models.App.slug == manifest_id,
      models.App.manifest_url.is_(None),
    )
    .first()
  )
  if candidate is None:
    return None
  return candidate if _trusted_origin_catalog_identity_matches(
    candidate, canonical_manifest_url, manifest_id,
  ) else None


def _find_install_identity_row(
  db: Session,
  *,
  source_url: str,
  manifest_id: str,
  package_id: str | None = None,
) -> models.App | None:
  """Resolve permanent package identity, then the bounded legacy witnesses."""
  package_row = None
  if package_id:
    package_row = (
      db.query(models.App)
      .filter(models.App.package_id == package_id)
      .one_or_none()
    )
  canonical = _canonical_identity_key(source_url, manifest_id)
  existing = (
    db.query(models.App)
    .filter(models.App.manifest_url == canonical)
    # Rows can share an address; choose deterministically, as below.
    .order_by(
      case((models.App.deleted_at.is_(None), 0), else_=1),
      models.App.id.asc(),
    )
    .first()
  )
  if existing is None:
    existing = _find_ref_independent_catalog_row(db, canonical, manifest_id)
  if existing is None:
    existing = _find_trusted_origin_catalog_row(
      db, canonical, manifest_id,
    )
  if package_row is not None and existing is not None and package_row.id != existing.id:
    raise HTTPException(
      409, "Permanent package identity conflicts with the installed source.",
    )
  return package_row or existing


def _manifest_service_id(
  manifest: dict, *, app: models.App | None = None, slug: str | None = None,
) -> str | None:
  """Resolve the identity that this accepted manifest will actually store.

  Explicit package identities are stable by declaration. Legacy manifests
  keep the row's existing identity, or fall back to the collision-resolved
  slug for a fresh install, matching the migration backfill.
  """
  service = manifest.get("service")
  if not isinstance(service, dict):
    return None
  if "id" in service:
    return service["id"]
  if app is not None and app.service_id:
    return app.service_id
  return slug or (app.slug if app is not None else manifest["id"])


def _manifest_service_aliases(manifest: dict) -> list[str]:
  service = manifest.get("service")
  if not isinstance(service, dict):
    return []
  return list(service.get("aliases") or [])


def _assert_service_identity_available(
  db: Session, *, service_id: str | None, app_id: int | None,
) -> None:
  if service_id is None:
    return
  owner = (
    db.query(models.App)
    .filter(models.App.service_id == service_id)
    .one_or_none()
  )
  if owner is not None and owner.id != app_id:
    raise HTTPException(409, "App service identity is already installed.")
  alias_owner = db.get(models.AppServiceAlias, service_id)
  if alias_owner is not None and alias_owner.app_id != app_id:
    raise HTTPException(409, "App service identity is already installed.")


def _assert_service_aliases_available(
  db: Session, *, aliases: list[str], service_id: str | None, app_id: int | None,
) -> None:
  for alias in aliases:
    if alias == service_id:
      raise HTTPException(409, "App service alias repeats its stable identity.")
    _assert_service_identity_available(db, service_id=alias, app_id=app_id)


def _assert_service_transition_safe(
  app: models.App | None, *, service_id: str | None, aliases: list[str],
) -> None:
  if (
    app is not None
    and app.service_id is not None
    and service_id is not None
    and app.service_id != service_id
    and app.service_id not in aliases
  ):
    raise HTTPException(
      409,
      "Changing an app service identity requires retaining the previous "
      "identity in `service.aliases` for the reviewed transition.",
    )


def _sync_service_aliases(
  db: Session, *, app: models.App, manifest: dict,
) -> None:
  """Replace only this app's explicit transition routes in the transaction."""
  aliases = _manifest_service_aliases(manifest)
  db.query(models.AppServiceAlias).filter(
    models.AppServiceAlias.app_id == app.id,
  ).delete(synchronize_session=False)
  for alias in aliases:
    db.add(models.AppServiceAlias(service_id=alias, app_id=app.id))


def _contract_with_service_identity(
  contract: dict, *, service_id: str | None, aliases: list[str],
) -> dict:
  """Align the accepted contract with the collision-resolved row identity."""
  if service_id is None or not isinstance(contract.get("service"), dict):
    return contract
  result = dict(contract)
  service = dict(result["service"])
  service["id"] = service_id
  if aliases:
    service["aliases"] = list(aliases)
  else:
    service.pop("aliases", None)
  result["service"] = service
  return result


def _catalog_identity_matches(
  existing_identity: str | None,
  candidate_url: str,
  manifest_id: str,
) -> bool:
  """Whether a reviewed candidate keeps the installed GitHub package."""
  if not existing_identity:
    return False
  candidate_identity = _canonical_identity_key(candidate_url, manifest_id)
  if candidate_identity == existing_identity:
    return True
  suffix = f"{_MANIFEST_ID_MARKER}{manifest_id}"
  existing_repo = _github_root_manifest_identity(existing_identity)
  candidate_repo = _github_root_manifest_identity(candidate_identity)
  return bool(
    existing_identity.endswith(suffix)
    and existing_repo
    and existing_repo == candidate_repo
  )


def pending_conflict_update_matches_app(
  app: models.App,
  receipt: dict,
) -> bool:
  """Bind a pending receipt to the app identity it is allowed to promote.

  A normal Store app is bound by its persisted package identity. During the
  first adoption of a local app, that identity intentionally remains unset
  until source and capabilities are accepted together; the exact trusted Git
  origin is the temporary witness for that one pending package.
  """
  manifest = receipt.get("manifest")
  raw_base = receipt.get("raw_base")
  if (
    receipt.get("app_id") != app.id
    or not app.upstream_commit
    or receipt.get("upstream_commit") != app.upstream_commit
    or not isinstance(manifest, dict)
    or not isinstance(manifest.get("id"), str)
    or not isinstance(raw_base, str)
  ):
    return False
  manifest_id = manifest["id"]
  if app.manifest_url is not None:
    return _catalog_identity_matches(app.manifest_url, raw_base, manifest_id)
  return _trusted_origin_catalog_identity_matches(
    app, raw_base, manifest_id,
  )


def _redirect_target(url: str, r: httpx.Response) -> str | None:
  """The next hop of a redirect, or None for a usable response.

  Error statuses raise here, so every caller refuses them the same way.
  """
  if r.status_code in (301, 302, 303, 307, 308):
    loc = r.headers.get("Location")
    if not loc:
      raise HTTPException(502, f"Redirect from {url} missing Location header.")
    return urljoin(url, loc)
  if r.status_code == 404:
    raise HTTPException(404, f"Not found: {url}")
  if _is_rate_limited(url, r.status_code, r.headers):
    raise _rate_limit_error(url, r.headers)
  if r.status_code >= 400:
    raise HTTPException(502, f"Upstream {r.status_code} fetching {url}")
  return None


@asynccontextmanager
async def _validated_response(
  client: httpx.AsyncClient,
  url: str,
  method: str = "GET",
  headers: Mapping[str, str] | None = None,
  _hops: int = 0,
) -> AsyncIterator[httpx.Response]:
  """Open one request with SSRF validation + manual redirect handling.

  Each hop is re-validated through `_validate_url_safe` so a 302 to
  a private IP gets rejected just like a direct request to one — and the
  connection is PINNED to the validated IP (we fetch `pinned_url`, an
  IP-in-netloc URL, with the real hostname carried as the Host header + TLS
  SNI). That makes the address we checked the address we actually connect to,
  closing the DNS-rebinding gap where httpx would re-resolve at connect time.
  `follow_redirects` is False on the client; we walk the chain ourselves with a
  hop count cap, resolving each Location against the original (hostname) URL.
  Yields the final response with its body still unread.
  """
  if _hops > _MAX_REDIRECTS:
    raise HTTPException(
      502, f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url}",
    )
  # getaddrinfo blocks; resolve off the event loop.
  pinned_url, host_header, sni_host = await asyncio.to_thread(_validate_url_safe, url)
  next_url = None
  try:
    async with client.stream(
      method, pinned_url,
      headers={**(headers or {}), "Host": host_header},
      # httpcore/anyio expect a hostname string here. Passing bytes worked with
      # older httpx but now reaches idna2008_resolve(), which calls `.encode()`
      # and crashes every real HTTPS install/update before the request is sent.
      extensions={"sni_hostname": sni_host},
    ) as r:
      next_url = _redirect_target(url, r)
      if next_url is None:
        yield r
  except httpx.TimeoutException:
    raise HTTPException(504, f"Timeout fetching {url}")
  except httpx.RequestError as exc:
    raise HTTPException(502, f"Failed to fetch {url}: {exc}")
  if next_url is None:
    return
  # Follow outside the stream context so the previous connection is
  # already released by the time we open the next one.
  async with _validated_response(
    client, next_url, method, headers, _hops + 1,
  ) as r:
    yield r


async def _http_get(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
  """GETs a URL through `_validated_response`.

  Reads the body as a stream and aborts as soon as the running byte
  total crosses `max_bytes` — `r.content` would buffer the full
  response before the cap fires, so a hostile upstream could force
  us to allocate `max_bytes` × N pending requests in memory.
  """
  async with _validated_response(client, url) as r:
    chunks: list[bytes] = []
    total = 0
    async for chunk in r.aiter_bytes():
      total += len(chunk)
      if total > max_bytes:
        raise HTTPException(
          413,
          f"{url} exceeds {max_bytes} byte cap ({total}+ received).",
        )
      chunks.append(chunk)
    return b"".join(chunks)


def _remember_file_size(url: str, size: int) -> None:
  _FILE_SIZE_CACHE.pop(url, None)  # a refreshed entry is the newest again
  _FILE_SIZE_CACHE[url] = (time.monotonic(), size)
  if len(_FILE_SIZE_CACHE) > _FILE_SIZE_CACHE_MAX:
    del _FILE_SIZE_CACHE[next(iter(_FILE_SIZE_CACHE))]


async def _remote_file_size(client: httpx.AsyncClient, url: str) -> int | None:
  """A remote file's size from its response headers, or None when unknown.

  Asks for `identity` encoding: httpx advertises gzip by default, and a
  compressed length is not the number of bytes install writes. A known size is
  reused for `_FILE_SIZE_TTL`; an unknown one is looked up again next time.
  """
  cached = _FILE_SIZE_CACHE.get(url)
  if cached is not None and time.monotonic() - cached[0] < _FILE_SIZE_TTL:
    return cached[1]
  try:
    async with _validated_response(
      client, url, "HEAD", {"Accept-Encoding": "identity"},
    ) as r:
      length = r.headers.get("Content-Length", "")
      encoding = r.headers.get("Content-Encoding", "identity")
  except HTTPException:
    return None
  if encoding != "identity" or not length.isdigit():
    return None
  size = int(length)
  _remember_file_size(url, size)
  return size


async def package_download_bytes(manifest: Mapping, raw_base: str) -> int | None:
  """Bytes install would download for `manifest`, or None if any is unknown.

  The pre-install review shows this, so it reads response headers only and
  never fetches app code; install still fetches and binds the bytes itself.
  The sum is `package_bytes`, the same count the package limit uses. Lookups
  still running after `_SIZE_LOOKUP_DEADLINE` are cancelled and the size is
  unknown.
  """
  paths = sorted(set(package_input_paths(manifest)))
  # A manifest may declare many files; keep the burst on one host small.
  gate = asyncio.Semaphore(_SIZE_LOOKUP_CONCURRENCY)

  async with pinned_client(timeout=_HTTP_TIMEOUT) as client:
    async def size_of(rel: str) -> int | None:
      async with gate:
        return await _remote_file_size(client, raw_base + quote(rel))

    try:
      found = await asyncio.wait_for(
        asyncio.gather(*map(size_of, paths)), _SIZE_LOOKUP_DEADLINE,
      )
    except TimeoutError:
      return None
  sizes = dict(zip(paths, found))
  if None in sizes.values():
    return None
  return package_bytes(manifest, sizes.__getitem__)


class _PackageDownload:
  """Read one app package's declared files under `_PACKAGE_MAX_BYTES`.

  Every read is one declaration and is charged in full, exactly as
  `manifest_contract.package_bytes` counts them, because each declaration is
  written separately. A file declared more than once is downloaded once, and
  a first download stops streaming the moment it would cross the bound.
  """

  def __init__(self, client: httpx.AsyncClient, raw_base: str) -> None:
    self._client = client
    self._raw_base = raw_base
    self._files: dict[str, bytes] = {}
    self._total = 0

  def _over_limit(self, rel: str) -> HTTPException:
    return HTTPException(
      413,
      f"This app is larger than the {_PACKAGE_MAX_BYTES // (1024 * 1024)} "
      f"MiB app package limit (reached while downloading {rel}).",
    )

  async def read(self, rel: str) -> bytes:
    remaining = _PACKAGE_MAX_BYTES - self._total
    data = self._files.get(rel)
    if data is None:
      try:
        # Declared names are literal paths; encode them (e.g. `%`, spaces).
        data = await _http_get(
          self._client, self._raw_base + quote(rel), remaining,
        )
      except HTTPException as exc:
        if exc.status_code != 413:
          raise
        raise self._over_limit(rel) from exc
      self._files[rel] = data
    elif len(data) > remaining:
      raise self._over_limit(rel)
    self._total += len(data)
    return data


async def _resolve_source_identity(
  client: httpx.AsyncClient, source_url: str,
) -> tuple[str, str]:
  """Return the provider-owned trust identity and current fetch locator.

  GitHub repository paths are mutable, so resolve their numeric repository id
  only when a package opts into permanent identity. Other HTTPS sources retain
  an exact URL-derived identity and can still move through the explicit
  old-source handoff contract.
  """
  root = parse_root_manifest(source_url)
  if root is None:
    base = _canonical_base(source_url)
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return f"url:{digest}", source_url

  repository_id = _TRUSTED_CATALOG_REPOSITORY_IDS.get(root.repository)
  if repository_id is not None:
    canonical_owner, canonical_repo = root.repository
  else:
    repository_id, canonical_owner, canonical_repo = (
      await _fetch_github_repository(client, root.owner, root.repo)
    )
  canonical = dataclasses.replace(
    root, owner=canonical_owner, repo=canonical_repo,
  )
  return f"github:{repository_id}", canonical.url


async def _fetch_github_repository(
  client: httpx.AsyncClient, owner: str, repo: str,
) -> tuple[int, str, str]:
  """Return GitHub's numeric id and canonical owner/name for one repository."""
  api_url = f"https://api.github.com/repos/{owner}/{repo}"
  try:
    raw = await _http_get(client, api_url, 256 * 1024)
    metadata = json.loads(raw)
  except (json.JSONDecodeError, UnicodeDecodeError) as exc:
    raise HTTPException(502, "GitHub returned invalid repository metadata.") from exc
  repository_id = metadata.get("id") if isinstance(metadata, dict) else None
  full_name = metadata.get("full_name") if isinstance(metadata, dict) else None
  if (
    not isinstance(repository_id, int)
    or repository_id <= 0
    or not isinstance(full_name, str)
    or full_name.count("/") != 1
  ):
    raise HTTPException(502, "GitHub returned incomplete repository metadata.")
  canonical_owner, canonical_repo = full_name.split("/", 1)
  if not canonical_owner or not canonical_repo:
    raise HTTPException(502, "GitHub returned incomplete repository metadata.")
  return repository_id, canonical_owner, canonical_repo


def _header(headers, name: str) -> str | None:
  for key, value in (headers or {}).items():
    if str(key).lower() == name.lower():
      return str(value)
  return None


def _wait_label(seconds: int) -> str:
  seconds = max(1, int(seconds))
  if seconds < 60:
    return f"{seconds} second{'s' if seconds != 1 else ''}"
  minutes = (seconds + 59) // 60
  return f"about {minutes} minute{'s' if minutes != 1 else ''}"


_GITHUB_RATE_LIMIT_HOSTS = frozenset((
  "github.com", "api.github.com", "raw.githubusercontent.com",
))

# GitHub's permanent ids for the platform's own catalog repositories whose
# packages declare `package_id`. The `mobius-os` organization is already the
# trust root for these sources, so their identity needs no anonymous API call,
# which shared egress addresses often have exhausted on first boot. Any other
# repository is still resolved through the API. `tests/test_bootstrap_live.py`
# checks every live default app against this table.
_TRUSTED_CATALOG_REPOSITORY_IDS = {
  ("mobius-os", "app-store"): 1253755424,
  ("mobius-os", "app-social"): 1354986466,
}


def _is_rate_limited(url: str, status_code: int, headers) -> bool:
  """True for 429, and for the 403 GitHub uses for its rate limits.

  GitHub answers an exhausted primary limit with 403 and
  ``x-ratelimit-remaining: 0``, and a secondary limit with 403 and
  ``retry-after``; both are a wait, not a permission problem.
  """
  return status_code == 429 or (
    status_code == 403
    and urlparse(url).hostname in _GITHUB_RATE_LIMIT_HOSTS
    and (
      _header(headers, "x-ratelimit-remaining") == "0"
      or _header(headers, "retry-after") is not None
    )
  )


def _rate_limit_wait_seconds(headers) -> int | None:
  """Seconds the upstream asked us to wait, from retry-after or its reset."""
  retry_after = _header(headers, "retry-after")
  if retry_after:
    try:
      seconds = int(float(retry_after))
      if seconds > 0:
        return seconds
    except ValueError:
      pass
  reset = _header(headers, "x-ratelimit-reset")
  if reset:
    try:
      return max(1, int(reset) - int(time.time()))
    except ValueError:
      pass
  return None


def _rate_limit_error(url: str, headers) -> HTTPException:
  """The 429 for a rate-limited fetch, carrying the wait as Retry-After."""
  host = urlparse(url).hostname or "upstream"
  service = "GitHub" if host in _GITHUB_RATE_LIMIT_HOSTS else host
  detail = f"{service} rate-limited this app request."
  wait = _rate_limit_wait_seconds(headers)
  if wait is None:
    return HTTPException(429, f"{detail} Please wait a minute and try again.")
  return HTTPException(
    429,
    f"{detail} Try again in {_wait_label(wait)}.",
    headers={"Retry-After": str(wait)},
  )


def _seed_value_is_inline(value) -> bool:
  """`storage_seeds` values: a string is a repo-relative path; anything
  else (dict, list, bool, number) is an inline JSON literal."""
  return not isinstance(value, str)


def _assert_within(root: Path, target: Path, field: str) -> None:
  """Reject a write target that escapes `root` once symlinks are resolved.

  `source_files` paths are validated lexically (no `..`, no leading `/`), but a
  nested entry like `lib/cards.js` can still write THROUGH a symlinked parent
  (`lib -> /data/shared`) to clobber a file outside the app. Resolving both
  sides with `os.path.realpath` and requiring containment closes that — the one
  silent-and-catastrophic failure mode on the untrusted-manifest fetch path that
  earns a hard sanitizer.
  """
  real_root = os.path.realpath(root)
  real_target = os.path.realpath(target)
  if real_target != real_root and not real_target.startswith(real_root + os.sep):
    raise HTTPException(
      400, f"Manifest `{field}` resolves outside the app source dir."
    )


def _write_source_file(
  target: Path,
  content: bytes,
  backup: Path,
  created_paths: list[Path],
  rollback_actions: list[Callable[[], None]],
  commit_actions: list[Callable[[], None]],
) -> None:
  """Write one source file with the install's transactional rollback pattern.

  Snapshots an existing `target` to `backup` and registers rollback (restore the
  snapshot) + commit (drop the snapshot) actions; a newly-created file is tracked
  in `created_paths` so a failure deletes it. The bytes land via `atomic_write`
  so a concurrent reader never sees a torn file. Generalizes the single
  `index.jsx` write so every entry in a multi-file app's source set goes through
  the same snapshot-and-restore path.
  """
  if target.exists():
    if backup.exists():
      try:
        backup.unlink()
      except OSError:
        pass
    shutil.copy2(target, backup)
    rollback_actions.append(
      lambda b=backup, o=target: os.replace(b, o) if b.exists() else None
    )
    commit_actions.append(
      lambda b=backup: b.unlink() if b.exists() else None
    )
  else:
    created_paths.append(target)
  atomic_write(target, content)


def _source_path_set(tree: dict[str, bytes]) -> set[str]:
  """Return hand-written source paths from a full git tree."""
  return {rel for rel in tree if rel not in _MERGED_NON_SOURCE}


def _read_upstream_source_paths(source_dir: Path, ref: str | None) -> set[str]:
  """Best-effort source path set for a prior or current upstream ref."""
  if not ref:
    return set()
  try:
    return _source_path_set(app_git.read_ref_tree(source_dir, ref))
  except Exception as exc:
    log.warning(
      "install: failed to read upstream source paths in %s at %s — %r",
      source_dir, ref, exc,
    )
    return set()


def _prune_dropped_source_files(
  source_dir_path: Path,
  dropped_source_paths: set[str],
  rollback_actions: list[Callable[[], None]],
  commit_actions: list[Callable[[], None]],
) -> None:
  """Delete only git-tracked source files the new upstream removed.

  The delete set is the prior upstream source paths minus the new upstream
  source paths. That keeps local-only tracked files and siblings omitted from
  both upstreams on disk because they were never removed by upstream. Each
  deletion snapshots to a `.mobius-drop-bak`, registers rollback restore for a
  later failure, and registers success cleanup so the snapshot is never staged.
  Best-effort on the `ls-files` read: if git can't enumerate, nothing is pruned.
  """
  if not dropped_source_paths:
    return
  try:
    listing = subprocess.run(
      ["git", "-C", str(source_dir_path), "ls-files", "-z"],
      capture_output=True, timeout=30, check=True,
      env=app_git._git_env(source_dir_path),
    )
  except (OSError, subprocess.SubprocessError):
    return
  for rel in listing.stdout.decode().split("\0"):
    if not rel or rel not in dropped_source_paths:
      continue
    target = source_dir_path / rel
    if not target.is_file():
      continue
    backup = target.with_name(target.name + ".mobius-drop-bak")
    if backup.exists():
      # The backup path is already taken — by a leaked `.mobius-drop-bak` from
      # an older install, or (worst case) a real tracked file with that name.
      # Never clobber it: skip pruning this entry. Leaving an upstream-removed
      # file on disk is harmless (a stale module at most) and reversible;
      # destroying a tracked file is neither. `_tracked_source` excludes the
      # backup suffix from staging, so a stale leaked one is cleaned up by the
      # next clean update rather than here.
      log.warning(
        "prune: %s already exists; skipping prune of %s to avoid clobber",
        backup, rel,
      )
      continue
    shutil.copy2(target, backup)
    rollback_actions.append(
      lambda b=backup, o=target: os.replace(b, o) if b.exists() else None
    )
    commit_actions.append(
      lambda b=backup: b.unlink() if b.exists() else None
    )
    target.unlink()


def _write_static_assets(
  source_dir_path: Path,
  assets: dict[str, bytes],
  created_paths: list[Path],
  rollback_actions: list[Callable[[], None]],
  commit_actions: list[Callable[[], None]],
) -> None:
  """Write manifest static assets under source_dir/static with rollback."""
  metadata_path = (source_dir_path / _STATIC_ASSETS_MANIFEST).resolve()
  previous_assets: set[str] = set()
  if metadata_path.exists():
    try:
      previous_raw = json.loads(metadata_path.read_text())
      if isinstance(previous_raw, list):
        previous_assets = {p for p in previous_raw if isinstance(p, str)}
    except (OSError, json.JSONDecodeError):
      previous_assets = set()
  if not assets and not previous_assets and not metadata_path.exists():
    return
  static_root = (source_dir_path / "static").resolve()
  static_root.mkdir(parents=True, exist_ok=True)
  backup_root = (
    source_dir_path.parent / f".{source_dir_path.name}.mobius-static-bak"
  ).resolve()
  backup_root_used = False

  def backup_existing_file(target: Path, backup_rel: str) -> Path | None:
    nonlocal backup_root_used
    if not target.exists():
      return None
    backup = (backup_root / backup_rel).resolve()
    if backup_root not in backup.parents:
      raise HTTPException(400, "Manifest `static_assets` backup path escapes.")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
      try:
        backup.unlink()
      except OSError:
        pass
    shutil.copy2(target, backup)
    if not backup_root_used:
      backup_root_used = True
      # Rollback actions execute in reverse order, so register directory
      # cleanup before file restores; restores run first, cleanup last.
      rollback_actions.append(
        lambda d=backup_root: shutil.rmtree(d, ignore_errors=True)
      )
    rollback_actions.append(
      lambda b=backup, o=target:
        os.replace(b, o) if b.exists() else None
    )
    commit_actions.append(
      lambda b=backup: b.unlink() if b.exists() else None
    )
    return backup

  for rel, content in assets.items():
    # rel was already validated as a simple repo-relative path. Resolve anyway
    # so this helper stays safe if future callers hand it unchecked data.
    target = (static_root / rel).resolve()
    if static_root not in target.parents:
      raise HTTPException(400, "Manifest `static_assets` path escapes static dir.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
      backup_existing_file(
        target, f"{_STATIC_ASSETS_BACKUP_ASSET_PREFIX}/{rel}",
      )
    else:
      created_paths.append(target)
    atomic_write(target, content)

  for rel in sorted(previous_assets - set(assets)):
    target = (static_root / rel).resolve()
    if static_root not in target.parents:
      continue
    if not target.exists() or not target.is_file():
      continue
    backup_existing_file(
      target, f"{_STATIC_ASSETS_BACKUP_ASSET_PREFIX}/{rel}",
    )
    target.unlink()

  if metadata_path.exists():
    backup_existing_file(
      metadata_path,
      f"{_STATIC_ASSETS_BACKUP_METADATA_PREFIX}/{_STATIC_ASSETS_MANIFEST}",
    )
  else:
    created_paths.append(metadata_path)
  atomic_write(metadata_path, json.dumps(sorted(assets), indent=2) + "\n")

  if backup_root_used:
    commit_actions.append(
      lambda d=backup_root: shutil.rmtree(d, ignore_errors=True)
    )


def _update_package_paths(
  source_dir: str | Path, incoming_manifest: dict,
) -> set[str] | None:
  """Protect declarations on either side and everything the app serves.

  A trailing slash protects a whole directory: the asset route serves every
  file below SERVED_STATIC_DIR, declared or not. None protects every path
  when the local package contract cannot be read.
  """
  try:
    raw = app_git.read_blob(source_dir, app_git.LOCAL_BRANCH, "mobius.json") or b""
    if len(raw) > _MANIFEST_MAX_BYTES:
      return None
    local_manifest = json.loads(raw)
    validate_manifest_contract(local_manifest)
  except (
    OSError, subprocess.SubprocessError, UnicodeDecodeError,
    json.JSONDecodeError, ManifestContractError, RecursionError,
  ):
    return None
  paths = {"mobius.json", f"{managed_paths.SERVED_STATIC_DIR}/"}
  for side in (incoming_manifest, local_manifest):
    paths.update(package_input_paths(side))
  return paths


async def _kept_local_bundle_unchanged(
  tree: Mapping[str, bytes], manifest: dict, static_assets: Mapping[str, bytes],
  upstream: Mapping[str, bytes], kept_local: Sequence[str],
) -> bool:
  """Retain ancillary conflicts only when they cannot change the served bundle.

  Compile both alternatives in the same root so generated paths cannot create
  spurious differences. Non-JavaScript runtimes are not covered by this proof.
  Source completeness also protects URL references that are not bundle inputs.
  """
  if EXECUTABLE_MANIFEST_FIELDS.intersection(manifest):
    return False
  source_check = await asyncio.to_thread(
    _source_completeness, tree, manifest, static_assets,
  )
  if source_check.errors:
    return False
  # Creation and removal touch disk; keep both off the event loop.
  temp = await asyncio.to_thread(tempfile.mkdtemp, prefix="mobius-ancillary-check-")
  try:
    root = Path(temp).resolve() / "source"
    output = Path(temp) / "bundle.js"

    def write_kept_tree() -> str:
      inputs = dict(tree)
      inputs.update({
        f"static/{dest}": static_assets[dest]
        for dest in static_asset_entries(manifest.get("static_assets"))
      })
      for rel, data in inputs.items():
        path = root / rel
        _assert_within(root, path, f"package input {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
      return entry.read_text(encoding="utf-8")

    def write_upstream_alternative() -> tuple[bytes, str]:
      kept_bundle = output.read_bytes()
      output.unlink()
      for rel in kept_local:
        path = root / rel
        _assert_within(root, path, f"package input {rel}")
        if rel in upstream:
          path.parent.mkdir(parents=True, exist_ok=True)
          path.write_bytes(upstream[rel])
        else:
          path.unlink(missing_ok=True)
      return kept_bundle, entry.read_text(encoding="utf-8")

    try:
      entry = root / manifest["entry"]
      source = await asyncio.to_thread(write_kept_tree)
      await compile_jsx(
        source, source_path=entry, source_root=root, out_path=output,
      )
      kept_bundle, source = await asyncio.to_thread(write_upstream_alternative)
      await compile_jsx(
        source, source_path=entry, source_root=root, out_path=output,
      )
      return kept_bundle == await asyncio.to_thread(output.read_bytes)
    except BuildLeaseUnavailable as exc:
      raise HTTPException(503, "JavaScript builder is busy; retry the update.") from exc
    except RolldownStalled as exc:
      # A stall says nothing about whether the trees differ.
      raise HTTPException(503, exc.detail()) from exc
    except (RuntimeError, OSError, UnicodeError, KeyError, HTTPException):
      return False
  finally:
    await asyncio.to_thread(shutil.rmtree, temp, ignore_errors=True)


PackageContentBytes = bytes | app_git.GitTreeBlob


def _install_candidate_digest(
  *,
  manifest: dict,
  raw_base: str,
  source_identity: str | None,
  predecessor_source_identity: str | None,
  canonical_source_url: str,
  entry_bytes: PackageContentBytes,
  icon_processed: bytes | None,
  bundled_job: bytes | None,
  static_assets: Mapping[str, PackageContentBytes],
  source_files: Mapping[str, PackageContentBytes],
  seeds: Mapping[str, PackageContentBytes],
) -> str:
  """Bind a deferred conflict resolution to the exact fetched candidate.

  A resolver can finish long after the first install request. Its replay may
  fetch through a moving branch URL, so manifest/version equality alone is not
  enough: publishers can replace static, seed, icon, or source bytes without a
  version bump. Length-prefixed fields make this digest unambiguous while
  streaming large assets instead of base64-encoding them into the receipt.
  """
  digest = hashlib.sha256()

  def add(label: str, value: PackageContentBytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(4, "big"))
    digest.update(label_bytes)
    if isinstance(value, app_git.GitTreeBlob):
      digest.update(value.size.to_bytes(8, "big"))
      for chunk in value.chunks():
        digest.update(chunk)
    else:
      digest.update(len(value).to_bytes(8, "big"))
      digest.update(value)

  add(
    "manifest",
    json.dumps(
      manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"),
  )
  add("raw_base", raw_base.encode("utf-8"))
  add("source-identity", (source_identity or "").encode("utf-8"))
  add(
    "predecessor-source-identity",
    (predecessor_source_identity or "").encode("utf-8"),
  )
  add("canonical-source-url", canonical_source_url.encode("utf-8"))
  add("entry", entry_bytes)
  add("icon-present", b"1" if icon_processed is not None else b"0")
  if icon_processed is not None:
    add("icon", icon_processed)
  add("job-present", b"1" if bundled_job is not None else b"0")
  if bundled_job is not None:
    add("job", bundled_job)
  for group, values in (
    ("source", source_files),
    ("static", static_assets),
    ("seed", seeds),
  ):
    for rel in sorted(values):
      add(f"{group}-path", rel.encode("utf-8"))
      add(f"{group}-bytes", values[rel])
  return digest.hexdigest()


def package_content_digest(
  *,
  manifest: dict,
  entry_bytes: PackageContentBytes,
  icon_processed: bytes | None,
  bundled_job: bytes | None,
  static_assets: Mapping[str, PackageContentBytes],
  source_files: Mapping[str, PackageContentBytes],
  seeds: Mapping[str, PackageContentBytes],
) -> str:
  """Bind every installable package byte without binding its public URL.

  Install replay deliberately includes ``raw_base`` because a moved upstream
  is a different replay input. Publication verification has the opposite
  requirement: compare an accepted local package with the same bytes fetched
  from its eventual public location. Reuse the install digest with one stable
  empty origin so future package fields cannot silently drift between those
  two comparisons.
  """
  return _install_candidate_digest(
    manifest=manifest,
    raw_base="",
    source_identity=None,
    predecessor_source_identity=None,
    canonical_source_url="",
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
  )


class PackageContentError(ValueError):
  """An accepted source tree cannot reproduce its declared install package."""


def _package_input_bytes(value: PackageContentBytes) -> bytes:
  # Parsing manifest/icon/job syntax requires bytes; opaque assets do not.
  return value.read_bytes() if isinstance(value, app_git.GitTreeBlob) else value


def _package_input_size(value: PackageContentBytes | None) -> int:
  if value is None:
    return 0
  return value.size if isinstance(value, app_git.GitTreeBlob) else len(value)


def package_content_digest_from_tree(
  tree: Mapping[str, PackageContentBytes | None],
) -> tuple[str, str]:
  """Return manifest identity + package digest from an immutable source tree.

  This is the local half of publication verification. Keep it beside the
  fetch/install implementation so every declared package input has one owner
  when the manifest contract grows.
  """
  try:
    inputs = _read_git_package_inputs(
      tree, _read_git_package_manifest(tree, strict=True),
    )
  except HTTPException as exc:
    raise PackageContentError(str(exc.detail)) from exc
  except ValueError as exc:
    raise PackageContentError(str(exc)) from exc
  return inputs.manifest["id"], inputs.content_digest()


def package_content_digest_from_git(
  source_dir: str | Path, ref: str,
) -> tuple[str, str] | None:
  """Digest every declared package byte without materializing its full tree.

  Use the same canonical validation/digest as publication and installation.
  None means a real legacy baseline without a manifest, not a broken package;
  an invalid/missing declared input still fails closed with PackageContentError.
  Callers hold the source lock. Blob ranges cannot escape this spool scope.
  """
  with app_git.open_ref_tree(source_dir, ref) as tree:
    if "mobius.json" not in tree:
      return None
    return package_content_digest_from_tree(tree)


def read_recorded_update_source(
  source_dir: str | Path, candidate_tree: Mapping[str, bytes],
) -> dict[str, bytes]:
  """Read installed runtime source without applying incoming-package rules.

  Historical manifests may predate validation and size limits. If one cannot
  be read safely, use the candidate's runtime paths plus the entry. Otherwise
  include prior declared sources too, so previews preserve deletions. Only
  this old/new runtime-path union is materialized, never old assets or other
  repository files. Callers hold the source lock.
  """
  with app_git.open_ref_tree(source_dir, app_git.UPSTREAM_BRANCH) as tree:
    paths = set(candidate_tree) | {"index.jsx"}
    if "mobius.json" not in tree:
      # Pre-manifest imports recorded runtime source directly, not a clone.
      # Keep that source set so a dropped legacy module remains an update.
      paths.update(set(tree) - _MERGED_NON_SOURCE)
    raw_manifest = tree.get("mobius.json")
    manifest = None
    if (
      raw_manifest is not None
      and _package_input_size(raw_manifest) <= _MANIFEST_MAX_BYTES
    ):
      try:
        manifest = json.loads(_package_input_bytes(raw_manifest))
      except (OSError, RuntimeError, ValueError):
        pass
    if isinstance(manifest, dict):
      sources = manifest.get("source_files")
      if isinstance(sources, list):
        paths.update(rel for rel in sources if isinstance(rel, str))
      schedule = manifest.get("schedule")
      if isinstance(schedule, dict) and isinstance(schedule.get("job"), str):
        paths.add(schedule["job"])
    return {
      rel: _package_input_bytes(data)
      for rel, data in tree.items() if rel in paths and data is not None
    }


def _require_git_candidate(
  source_dir: str | Path, commit: str, candidate: InstallCandidate,
) -> None:
  """Prove that a present Git commit holds the package fetched over HTTP.

  A prepared install reads its package from the very commit it adopts; only a
  caller that fetched over HTTP, such as a publication handoff, needs this.
  """
  reason = None
  try:
    actual = read_git_install_candidate(
      source_dir, commit, candidate.raw_base + "mobius.json",
    ).candidate
    if actual.manifest != candidate.manifest:
      reason = "manifest changed"
    elif actual.source_review_digest != candidate.source_review_digest:
      reason = "package files changed"
  except HTTPException as exc:  # An invalid Git manifest.
    reason = f"invalid mobius.json: {exc.detail}"
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
    reason = str(exc)
  if reason is None:
    return
  log.warning("install: Git commit %s does not match its package: %s", commit, reason)
  raise HTTPException(409, detail={
    "code": "git_source_mismatch",
    "message": (
      "The fetched Git commit does not match the package bytes that were "
      "reviewed. Refresh the update and try again."
    ),
    "reason": reason,
    "retryable": True,
  })


def _reconcile_cron_after_install_rollback() -> None:
  """Best-effort safe restoration after an adopted source-dir move fails.

  The prior implementation executed the restored app-owned ``init-cron.sh``
  directly. That made an install failure a supervision bypass. At this point
  the DB transaction and source-dir rename have already been rolled back, so
  the normal reconciler can rediscover the live row, parse its declaration,
  and register the common runner without trusting the shell body.
  """
  try:
    from app.database import SessionLocal
    from app.routes.app_schedules import reconcile_app_cron_supervision
    cron_db = SessionLocal()
    try:
      _count, warnings, _infrastructure_ready = (
        reconcile_app_cron_supervision(cron_db)
      )
    finally:
      cron_db.close()
    for warning in warnings:
      log.warning("install rollback cron supervision skipped: %s", warning)
  except Exception as exc:
    # Rollback is already a best-effort failure path. Log without masking the
    # original install exception that caused it.
    log.warning("install rollback cron reconciliation failed: %s", exc)


def _drop_app_cron(source_dir: Path) -> None:
  """Converge an updated app's cron to "no schedule": drop its live crontab
  entry AND delete the durable init-cron.sh declaration under `source_dir`.

  The update path is otherwise add-only, so an app that migrates from a
  recurring schedule (v1) to on-demand-only (v2, no `schedule.default`) would
  leave the v1 crontab line firing and its init-cron.sh discovered by boot
  reconciliation forever (card 099). Removing the script — not just
  tombstoning it like the soft-delete path — is correct here because an
  in-place update has no recover step to re-arm from; the next update that
  re-declares a schedule rewrites init-cron.sh from scratch via the scaffold.
  Pure-filesystem so it runs via `asyncio.to_thread` (`_unregister_cron`
  shells out to crontab); best-effort, mirroring `_unregister_cron` itself.
  """
  try:
    _unregister_cron(source_dir)
  except OSError:
    pass
  try:
    (source_dir / "init-cron.sh").unlink()
  except OSError:
    pass


def _storage_path(app_id: int, sub: str) -> Path:
  """Mirror of routes/storage.py's per-app path layout."""
  data_dir = Path(get_settings().data_dir)
  # Path validation mirrors routes/storage.py — keep characters safe
  # against traversal. The store mini-app is the primary caller, but
  # community manifests might be careless / hostile.
  try:
    validate_storage_destination(sub)
  except ManifestContractError as exc:
    raise HTTPException(400, str(exc)) from exc
  return data_dir / "apps" / str(app_id) / sub


def _source_completeness(
  tree: Mapping[str, bytes], manifest: dict, static_assets: Mapping[str, bytes],
) -> SourceCheckResult:
  """Check imports using caller-selected static bytes (opaque on publication)."""
  files = {rel: data.decode("utf-8", "replace") for rel, data in tree.items()}
  files.update({
    f"static/{dest}": data.decode("utf-8", "replace")
    for dest, data in static_assets.items()
  })
  return check_app_source(
    files,
    entry=manifest["entry"],
    source_files=manifest.get("source_files") or [],
    job=(manifest.get("schedule") or {}).get("job"),
    static_assets=(f"static/{dest}" for dest in static_assets),
  )


def _check_source_completeness(
  *,
  app_name: str,
  manifest: dict,
  source_tree: dict[str, bytes],
  static_assets: Mapping[str, bytes],
) -> None:
  """Assert the source tree the manifest declares is self-contained.

  Runs the static ``app_source_check`` against the tree about to be compiled:
  every relative sibling import reachable from the entry (and the job) must be
  declared in ``source_files`` (an incomplete list installs fine from a git
  clone but breaks every synthetic-fetch install), and no shipped module may
  reference an off-origin http(s) host the ``connect-src 'self'`` CSP blocks.

  The completeness misses are ERRORS and raise ``HTTPException(422)`` — caught
  by the install's ``except HTTPException`` handler, which rolls the source
  writes back exactly like a compile failure. External-host references are
  logged as warnings (runtime quality, not install-breaking).

  The caller invokes this only on the synthetic-fetch path, where
  ``source_tree`` IS the whole declared tree (entry + every fetched
  ``source_files`` entry + the job script), so it is the sole source of bytes.
  Static-asset dests are recorded below their installer-owned ``static/``
  directory so source checks see the exact path compilation sees. Their bytes
  remain opaque on publication, as generated assets may contain import-like
  strings. For example,
  logical destination ``logo.js`` is importable as ``./static/logo.js``.
  """
  result = _source_completeness(
    source_tree, manifest, {dest: b"" for dest in static_assets},
  )
  for warning in result.warnings:
    log.warning(
      "install: %s external-host reference in %s — %s",
      app_name, warning.path, warning.detail,
    )
  if result.errors:
    detail = "; ".join(f"{e.path}: {e.detail}" for e in result.errors)
    raise HTTPException(
      422,
      f"{app_name} has an incomplete `source_files` manifest — {detail}",
    )


def validate_manifest_address(
  manifest: dict, manifest_url: str | None, *,
  db: Session | None = None, package_id: str | None = None,
) -> None:
  """Bind a stored address to its manifest or installed permanent package."""
  if manifest_url is None:
    return
  _, bound_id = requested_manifest_source(manifest_url)
  if db is not None and bound_id is not None:
    existing = _find_install_identity_row(
      db, source_url=manifest_url, manifest_id=bound_id,
    )
    package_id = existing.package_id if existing is not None else None
  try:
    _require_bound_manifest(manifest, bound_id, package_id)
  except ValueError as exc:
    raise HTTPException(409, str(exc)) from exc


async def _fetch_and_validate_manifest(
  cli: httpx.AsyncClient,
  *,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  db: Session | None = None,
) -> tuple[dict, str]:
  """Load one manifest and return it with its normalized asset base.

  Preview and install intentionally share this exact boundary.  The preview is
  therefore not a second, weaker interpretation that can drift from what the
  installer eventually applies. Stored identity addresses are normalized and
  bound here, before any package assets are fetched.
  """
  if (manifest_url is None) == (manifest is None):
    raise HTTPException(
      400, "Provide exactly one of `manifest_url` or `manifest`.",
    )
  requested_url = manifest_url
  if manifest_url is not None:
    manifest_url = requested_manifest_source(manifest_url)[0]
    raw = await _http_get(cli, manifest_url, _MANIFEST_MAX_BYTES)
    try:
      loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
      raise HTTPException(400, f"Manifest is not valid JSON: {exc}")
    manifest = loaded
    if raw_base is None:
      raw_base = _derive_raw_base(manifest_url)
  elif raw_base is None:
    raise HTTPException(
      400, "When passing inline `manifest`, also pass `raw_base`.",
    )
  if not isinstance(manifest, dict):
    raise HTTPException(400, "Manifest root must be a JSON object.")

  _validate_manifest(manifest)
  validate_manifest_address(manifest, requested_url, db=db)
  return manifest, _normalize_raw_base(raw_base)


async def preview_manifest_capabilities(
  *,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  db: Session | None = None,
) -> tuple[dict, str, dict, str]:
  """Return the validated manifest/base and its canonical review contract."""
  async with pinned_client(timeout=_HTTP_TIMEOUT) as cli:
    loaded, normalized_base = await _fetch_and_validate_manifest(
      cli,
      manifest_url=manifest_url,
      manifest=manifest,
      raw_base=raw_base,
      db=db,
    )
  contract, digest = contract_and_digest(loaded)
  return loaded, normalized_base, contract, digest


@dataclass(frozen=True)
class InstallCandidate:
  """Fully fetched, review-bound inputs for one install attempt.

  Nothing below this boundary performs network I/O. The digests bind the exact
  bytes that materialization will use, so identity selection and filesystem
  work cannot silently reinterpret a reviewed manifest.
  """

  manifest: dict
  raw_base: str
  entry_bytes: bytes
  icon_processed: bytes | None
  icon_warning: str | None
  bundled_job: bytes | None
  static_assets: dict[str, bytes]
  source_files: dict[str, bytes]
  seeds: dict[str, bytes]
  capability_contract: dict
  capability_digest: str
  candidate_digest: str
  source_review_digest: str
  source_identity: str | None = None
  predecessor_source_identity: str | None = None
  canonical_source_url: str = ""


def _new_candidate(
  *,
  manifest: dict,
  raw_base: str,
  entry_bytes: bytes,
  icon_processed: bytes | None,
  icon_warning: str | None,
  bundled_job: bytes | None,
  static_assets: dict[str, bytes],
  source_files: dict[str, bytes],
  seeds: dict[str, bytes],
  source_review_digest: str | None = None,
  source_identity: str | None = None,
  predecessor_source_identity: str | None = None,
  canonical_source_url: str | None = None,
) -> InstallCandidate:
  """Bind one package's bytes and source identities into its digests."""
  package = dict(
    manifest=manifest,
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
  )
  identity = dict(
    source_identity=source_identity,
    predecessor_source_identity=predecessor_source_identity,
    canonical_source_url=canonical_source_url or raw_base,
  )
  capability_contract, capability_digest = contract_and_digest(manifest)
  return InstallCandidate(
    **package,
    **identity,
    raw_base=raw_base,
    icon_warning=icon_warning,
    capability_contract=capability_contract,
    capability_digest=capability_digest,
    candidate_digest=_install_candidate_digest(
      raw_base=raw_base, **package, **identity,
    ),
    source_review_digest=(
      source_review_digest or package_content_digest(**package)
    ),
  )


async def _resolve_package_identity(
  client: httpx.AsyncClient, manifest: dict, source_url: str,
) -> tuple[str | None, str | None, str | None]:
  """Source, predecessor and canonical URL of a permanent-identity package."""
  if not manifest.get("package_id"):
    return None, None, None
  source_identity, canonical_source_url = await _resolve_source_identity(
    client, source_url,
  )
  predecessor_source_identity = None
  if manifest.get("previous_manifest_url"):
    predecessor_source_identity, _ = await _resolve_source_identity(
      client, manifest["previous_manifest_url"],
    )
  return source_identity, predecessor_source_identity, canonical_source_url


class PackageTooLarge(ValueError):
  """A Git package exceeds its manifest or declared-input size bound.

  A `ValueError`, so callers that treat any unusable candidate as unknown keep
  doing so, while preview and apply can tell the owner the actual reason.
  """


@dataclass(frozen=True)
class GitInstallCandidate:
  """One complete install package read from one immutable Git commit."""

  commit: str
  candidate: InstallCandidate

  @property
  def manifest(self) -> dict:
    return self.candidate.manifest

  @property
  def source_digest(self) -> str:
    return self.candidate.source_review_digest

  @property
  def runtime_tree(self) -> dict[str, bytes]:
    tree = {
      "index.jsx": self.candidate.entry_bytes,
      **self.candidate.source_files,
    }
    schedule = self.candidate.manifest.get("schedule")
    if self.candidate.bundled_job is not None and isinstance(schedule, dict):
      tree[schedule["job"]] = self.candidate.bundled_job
    return tree


def _reviewed_commit_has_manifest(source_dir: str | Path, commit: str) -> bool:
  """Check manifest presence without confusing a Git failure with absence."""
  proc = app_git._run(
    Path(source_dir), "ls-tree", "--name-only", f"{commit}^{{commit}}",
    "--", "mobius.json", read_only=True,
  )
  return "mobius.json" in proc.stdout.splitlines()


def _reviewed_git_package_commit(source_dir: str | Path, commit: str) -> str:
  try:
    resolved = app_git.head_sha(source_dir, commit).lower()
  except (OSError, subprocess.SubprocessError, RuntimeError):
    resolved = ""
  if resolved != str(commit).lower():
    raise ValueError("reviewed Git commit is unavailable")
  return resolved


@dataclass(frozen=True)
class _GitPackageInputs:
  """Validated inputs whose opaque bodies may be borrowed from an open spool."""

  manifest: dict
  entry_bytes: PackageContentBytes
  icon_processed: bytes | None
  icon_warning: str | None
  bundled_job: bytes | None
  static_assets: dict[str, PackageContentBytes]
  source_files: dict[str, PackageContentBytes]
  seeds: dict[str, PackageContentBytes]

  def content_digest(self) -> str:
    return package_content_digest(
      manifest=self.manifest,
      entry_bytes=self.entry_bytes,
      icon_processed=self.icon_processed,
      bundled_job=self.bundled_job,
      static_assets=self.static_assets,
      source_files=self.source_files,
      seeds=self.seeds,
    )


def _read_git_package_manifest(
  tree: Mapping[str, PackageContentBytes | None], *, strict: bool,
) -> dict:
  """Parse and validate a Git manifest independently of admission limits."""
  raw_manifest = tree.get("mobius.json")
  if raw_manifest is None:
    raise ValueError("candidate Git tree is missing manifest mobius.json")
  try:
    manifest = json.loads(_package_input_bytes(raw_manifest))
  except (json.JSONDecodeError, UnicodeDecodeError) as exc:
    raise ValueError("candidate Git manifest is invalid") from exc
  if strict:
    _validate_manifest(manifest)
  else:
    _validate_discovery_manifest(manifest)
  return manifest


def _read_git_candidate_inputs(
  tree: Mapping[str, PackageContentBytes | None], *, strict: bool,
) -> _GitPackageInputs:
  """Apply admission limits before parsing or materializing candidate inputs."""
  raw_manifest = tree.get("mobius.json")
  size = _package_input_size(raw_manifest)
  if size > _MANIFEST_MAX_BYTES:
    raise PackageTooLarge(
      f"This app's mobius.json is {size} bytes, more than the "
      f"{_MANIFEST_MAX_BYTES} byte manifest limit.",
    )
  manifest = _read_git_package_manifest(tree, strict=strict)
  size = package_bytes(manifest, lambda rel: _package_input_size(tree.get(rel)))
  if size > _PACKAGE_MAX_BYTES:
    raise PackageTooLarge(package_limit_message(size))
  return _read_git_package_inputs(tree, manifest)


def _read_git_package_inputs(
  tree: Mapping[str, PackageContentBytes | None], manifest: dict,
) -> _GitPackageInputs:
  # Discovery intentionally differs from strict installation validation.
  # Both readers share missing-input, schedule and non-fatal icon semantics.
  def required(relative: str, field: str) -> PackageContentBytes:
    try:
      value = tree[relative]
      if value is None:
        raise KeyError(relative)
      return value
    except KeyError as exc:
      raise ValueError(f"candidate Git tree is missing {field} {relative}") from exc

  entry_bytes = required(manifest["entry"], "entry")
  source_files = {
    relative: required(relative, "source file")
    for relative in manifest.get("source_files") or []
  }
  schedule = manifest.get("schedule")
  job_name = schedule.get("job") if isinstance(schedule, dict) else None
  bundled_job = (
    _package_input_bytes(required(job_name, "schedule job"))
    if job_name else None
  )
  if bundled_job is not None:
    try:
      validate_schedule_job(manifest, bundled_job)
    except ManifestContractError as exc:
      raise ValueError(str(exc)) from exc

  icon_processed = None
  icon_warning = None
  icon = manifest.get("icon")
  if icon and icon not in tree:
    # As over HTTP, an optional icon the package lacks is only a warning. A
    # listed but unreadable object still fails closed like any other input.
    icon_warning = f"icon: candidate Git tree is missing icon {icon}"
  elif icon:
    try:
      icon_processed = icon_assets.normalize_icon(
        _package_input_bytes(required(icon, "icon")),
      )
    except icon_assets.InvalidIcon as exc:
      icon_warning = f"icon: {exc}"

  static_assets = {
    destination: required(relative, "static asset")
    for destination, relative in static_asset_entries(
      manifest.get("static_assets") or {},
    ).items()
  }
  seeds: dict[str, PackageContentBytes] = {}
  for destination, value in (manifest.get("storage_seeds") or {}).items():
    seeds[destination] = (
      json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
      ).encode("utf-8")
      if _seed_value_is_inline(value)
      else required(value, "storage seed")
    )

  return _GitPackageInputs(
    manifest=manifest,
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    icon_warning=icon_warning,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
  )


def _same_repository_predecessor(
  manifest: dict, source_url: str, source_identity: str | None,
) -> str | None:
  """A ``previous_manifest_url`` in the source's own repository shares its identity."""
  previous_source = manifest.get("previous_manifest_url")
  if (
    source_identity is not None
    and previous_source
    and _github_root_manifest_identity(previous_source)
    == _github_root_manifest_identity(source_url)
  ):
    return source_identity
  return None


def read_git_install_candidate(
  source_dir: str | Path,
  commit: str,
  source_url: str,
  *,
  source_identity: str | None = None,
  predecessor_source_identity: str | None = None,
  canonical_source_url: str | None = None,
  strict: bool = True,
) -> GitInstallCandidate:
  """Read every declared install input from one already-fetched commit.

  Without a resolved ``predecessor_source_identity``, a predecessor in the
  same repository shares ``source_identity``.
  """
  repo = Path(source_dir)
  resolved = _reviewed_git_package_commit(repo, commit)
  # Spool the commit rather than reading every file into memory: the package
  # bound is checked first, then only declared inputs are materialized.
  with app_git.open_ref_tree(repo, resolved) as tree:
    inputs = _read_git_candidate_inputs(tree, strict=strict)
    entry_bytes = _package_input_bytes(inputs.entry_bytes)
    static_assets = {
      key: _package_input_bytes(value) for key, value in inputs.static_assets.items()
    }
    source_files = {
      key: _package_input_bytes(value) for key, value in inputs.source_files.items()
    }
    seeds = {
      key: _package_input_bytes(value) for key, value in inputs.seeds.items()
    }
    content_digest = inputs.content_digest()
  manifest = inputs.manifest
  raw_base = _normalize_raw_base(
    source_url.rsplit("/mobius.json", 1)[0]
    if "/mobius.json" in source_url else source_url
  )
  if predecessor_source_identity is None:
    predecessor_source_identity = _same_repository_predecessor(
      manifest, source_url, source_identity,
    )
  return GitInstallCandidate(
    commit=resolved,
    candidate=_new_candidate(
      manifest=manifest,
      raw_base=raw_base,
      entry_bytes=entry_bytes,
      icon_processed=inputs.icon_processed,
      icon_warning=inputs.icon_warning,
      bundled_job=inputs.bundled_job,
      static_assets=static_assets,
      source_files=source_files,
      seeds=seeds,
      source_review_digest=content_digest,
      source_identity=source_identity,
      predecessor_source_identity=predecessor_source_identity,
      canonical_source_url=canonical_source_url or (
        source_url if source_identity else None
      ),
    ),
  )


@dataclass(frozen=True)
class GitPackageSummary:
  """Complete package identity without retained opaque asset/seed payloads.

  No package body or borrowed spool range escapes the read. A real legacy
  comparison reads runtime source from this exact reviewed commit separately;
  preview/install keep their full-byte candidate contract.
  """

  commit: str
  manifest: dict
  source_digest: str
  icon_warning: str | None


def read_git_package_summary(
  source_dir: str | Path, commit: str, *, strict: bool = False,
) -> GitPackageSummary:
  """Read all discovery inputs, streaming opaque bodies into the canonical digest."""
  repo = Path(source_dir)
  resolved = _reviewed_git_package_commit(repo, commit)
  with app_git.open_ref_tree(repo, resolved) as tree:
    inputs = _read_git_candidate_inputs(tree, strict=strict)
    return GitPackageSummary(
      commit=resolved,
      manifest=inputs.manifest,
      source_digest=inputs.content_digest(),
      icon_warning=inputs.icon_warning,
    )


_TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})


def transient_failure(exc: BaseException) -> tuple[bool, int | None]:
  """Whether the same request may later get past ``exc`` unchanged, and any wait.

  The one rule for install, update and bootstrap retries: a Git transfer that
  timed out or lost its connection (``app_git`` classifies its failures),
  incomplete Git history, a resolver that could not answer, and an upstream
  rate limit or gateway failure. A failure the installer has already
  classified carries ``retryable`` in its detail. Anything else, such as a
  missing repository, a full disk or an invalid package, will fail again.
  """
  if isinstance(exc, app_git.GitTransferFailed):
    return exc.transient, None
  if isinstance(exc, app_git.HistoryIncomplete):
    return True, None
  # Only a resolver that could not answer may answer later; a name that does
  # not exist stays missing.
  if isinstance(exc, HTTPException) and isinstance(exc.__cause__, socket.gaierror):
    exc = exc.__cause__
  if isinstance(exc, socket.gaierror):
    return exc.errno == socket.EAI_AGAIN, None
  if not isinstance(exc, HTTPException):
    return False, None
  detail = exc.detail
  if exc.status_code not in _TRANSIENT_STATUS_CODES and not (
    isinstance(detail, dict) and detail.get("retryable") is True
  ):
    return False, None
  try:
    wait = int((exc.headers or {}).get("Retry-After") or 0)
  except ValueError:
    wait = 0
  return True, wait or None


def git_source_error(
  outcome: str, exc: BaseException, *,
  code: str = "git_update_unavailable", failure: str = "",
  retry_hint: str = "",
) -> HTTPException:
  """The owner-facing 409 for a Git source that could not be used.

  A transfer exceeding its wall-clock ceiling is reported as a timeout,
  distinct from a missing or unverifiable source. ``retryable`` marks a
  failure the same request may later get past unchanged (``transient_failure``);
  bootstrap retries it, and only then is ``retry_hint`` added to the message.
  """
  retryable = transient_failure(exc)[0]
  if retryable and retry_hint:
    failure = f"{failure} {retry_hint}"
  if isinstance(exc, app_git.GitTransferTimeout):
    code = "git_transfer_timeout"
    failure = f"The app's Git transfer timed out: {exc}."
    outcome = f"{outcome} Retrying on a steadier connection may help."
  elif isinstance(exc, app_git.HistoryIncomplete):
    log.warning("install: %s", exc)  # Names the refs; the owner text does not.
    failure = (
      "The app's Git history is incomplete, so the update can't be merged "
      "safely; retry when its source is reachable."
    )
  return HTTPException(409, detail={
    "code": code, "message": f"{failure} {outcome}".strip(),
    "retryable": retryable,
  })


def _url_update_commit(
  repo: Path, ref: str, staged_clone: app_git.Staged | None,
) -> str:
  """The URL update's commit, imported from its staged clone or already here."""
  if staged_clone is not None:
    return app_git.import_staged(repo, staged_clone)
  present = app_git.present_commit(repo, ref)
  if present is None:
    raise HTTPException(
      409, "The app's Git update must be fetched before it is installed.",
    )
  return present


@dataclass(frozen=True)
class InstallTarget:
  """Identity decision made before any database or filesystem mutation."""

  existing: models.App | None
  mode: str
  adopting_previous_id: bool
  adopting_trusted_origin: bool
  trusted_catalog_origin: bool
  canonical_manifest_url: str
  origin_migration: tuple[str, str] | None
  package_id: str | None
  source_identity: str | None
  source_handoff_required: bool

  @property
  def trusted_origin(self) -> bool:
    return self.adopting_trusted_origin or self.trusted_catalog_origin


@dataclass
class InstallJournal:
  """Filesystem compensation owned by the pre-commit materialization phase."""

  created_paths: list[Path] = field(default_factory=list)
  rollback_actions: list[Callable[[], None]] = field(default_factory=list)
  commit_actions: list[Callable[[], None]] = field(default_factory=list)
  durable: bool = False

  def mark_durable(self) -> None:
    """Cross the irreversible boundary without retaining failure actions."""
    self.durable = True
    self.rollback_actions.clear()
    self.created_paths.clear()

  def rollback_materialization(self) -> None:
    """Undo pre-commit filesystem work once; never undo a durable install."""
    if self.durable:
      return
    _run_rollback_actions(self.rollback_actions)
    _cleanup(self.created_paths)
    self.rollback_actions.clear()
    self.created_paths.clear()

  def cleanup_superseded(self) -> None:
    """Remove backups/artifacts made obsolete by a successful commit."""
    for action in self.commit_actions:
      try:
        action()
      except OSError as exc:
        log.warning("install: post-commit cleanup failed — %s", exc)


@dataclass
class InstallResult:
  """Stable outcome shared by store, bootstrap, and conflict replay callers."""

  app: models.App
  mode: str
  warnings: list[str]
  manifest: dict
  conflict_paths: list[str]
  divergence: str
  reconciliation: app_git.ReconciliationReceipt


async def _fetch_install_candidate(
  *,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  reviewed_capability_digest: str | None = None,
  db: Session | None = None,
) -> InstallCandidate:
  """Fetch every install input of a package over HTTP, once."""
  async with pinned_client(timeout=_HTTP_TIMEOUT) as cli:
    manifest, raw_base = await _fetch_and_validate_manifest(
      cli,
      manifest_url=manifest_url,
      manifest=manifest,
      raw_base=raw_base,
      db=db,
    )
    _require_reviewed_capabilities(manifest, reviewed_capability_digest)
    source_identity, predecessor_source_identity, canonical_source_url = (
      await _resolve_package_identity(
        cli,
        manifest,
        requested_manifest_source(manifest_url)[0] if manifest_url else raw_base,
      )
    )
    package = _PackageDownload(cli, raw_base)
    entry_bytes = await package.read(manifest["entry"])

    icon_processed: bytes | None = None
    icon_warning: str | None = None
    if manifest.get("icon"):
      try:
        icon_raw = await package.read(manifest["icon"])
        icon_processed = icon_assets.normalize_icon(icon_raw)
      except icon_assets.InvalidIcon as exc:
        icon_warning = f"icon: {exc}"
        log.info("install: icon skipped — %s", exc)
      except HTTPException as exc:
        # A broken optional icon must not block an otherwise valid app, but
        # the package budget binds the icon like every declared file.
        if exc.status_code == 413:
          raise
        icon_warning = f"icon: {exc.detail}"
        log.info("install: icon skipped — %s", exc.detail)

    schedule = manifest.get("schedule")
    bundled_job = None
    if schedule and schedule.get("job"):
      bundled_job = await package.read(schedule["job"])
      try:
        validate_schedule_job(manifest, bundled_job)
      except ManifestContractError as exc:
        raise HTTPException(400, str(exc)) from exc

    static_assets = {
      dest: await package.read(src)
      for dest, src in static_asset_entries(
        manifest.get("static_assets") or {},
      ).items()
    }
    source_files = {
      rel: await package.read(rel)
      for rel in manifest.get("source_files") or []
    }

    # Inline seeds are part of the manifest, which its own cap already bounds.
    seeds: dict[str, bytes] = {}
    for sub, value in (manifest.get("storage_seeds") or {}).items():
      if _seed_value_is_inline(value):
        seeds[sub] = json.dumps(
          value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
      else:
        seeds[sub] = await package.read(value)

  return _new_candidate(
    manifest=manifest,
    raw_base=raw_base,
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    icon_warning=icon_warning,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
    source_identity=source_identity,
    predecessor_source_identity=predecessor_source_identity,
    canonical_source_url=canonical_source_url,
  )


async def fetch_install_candidate(
  manifest_url: str, *, db: Session | None = None,
) -> InstallCandidate:
  """Fetch the exact package the installer would use, without installing it."""
  return await _fetch_install_candidate(
    manifest_url=manifest_url, manifest=None, raw_base=None, db=db,
  )


@dataclass(frozen=True)
class PreparedPackage:
  """A fresh install's package, fetched before the lifecycle lock.

  A root Git package is read from its staged clone, so the installer adopts
  exactly the commit it reviewed and downloads the package only once. The
  caller owns ``staged_clone`` and discards it once the install is over.
  """

  candidate: InstallCandidate
  staged_clone: app_git.Staged | None = None


async def prepare_package(
  manifest_url: str | None,
  manifest: dict | None = None,
  raw_base: str | None = None,
  *,
  reviewed_capability_digest: str | None = None,
  db: Session | None = None,
) -> PreparedPackage:
  """The network half of a fresh install; run it holding no lock.

  A root Git package is cloned and read from its clone. An owner request
  passes ``db``: the manifest is validated first, with the stored address
  checks the preview uses, so a missing or invalid package, or one whose
  capabilities changed since review, is refused before any history is
  transferred, and the connection is released before the transfer.
  """
  if (manifest_url is None) == (manifest is None):
    raise HTTPException(
      400, "Provide exactly one of `manifest_url` or `manifest`.",
    )
  repo_ref = _derive_repo_ref(manifest_url) if manifest_url else None
  if repo_ref is None:
    candidate = await _fetch_install_candidate(
      manifest_url=manifest_url, manifest=manifest, raw_base=raw_base,
      reviewed_capability_digest=reviewed_capability_digest, db=db,
    )
    if db is not None:
      db.rollback()  # No connection is held while the caller waits for a lock.
    return PreparedPackage(candidate)
  if db is not None:
    previewed, *_ = await preview_manifest_capabilities(
      manifest_url=manifest_url, manifest=None, raw_base=None, db=db,
    )
    db.rollback()
    _require_reviewed_capabilities(previewed, reviewed_capability_digest)
  try:
    staged = await _stage(app_git.stage_clone, *repo_ref, git_transfer_root())
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
    raise git_source_error(
      "Nothing was installed.",
      exc,
      code="git_install_unavailable",
      failure="The app's Git repository could not be cloned or verified.",
      retry_hint="Retry when its source is available.",
    ) from exc
  try:
    return PreparedPackage(
      await _staged_clone_candidate(manifest_url, staged, db=db), staged,
    )
  except BaseException:
    await asyncio.to_thread(discard_staged, staged)
    raise


def _read_staged_manifest(staged: app_git.Staged) -> dict | None:
  """The staged commit's bounded, valid root manifest, or None.

  None when it is absent, oversized or invalid, or when Git could not read
  it; the full package read then reports which.
  """
  raw = app_git.read_committed_file(
    staged.path, staged.sha, "mobius.json", max_bytes=_MANIFEST_MAX_BYTES,
  )
  if raw is None:
    return None
  try:
    return _read_git_package_manifest({"mobius.json": raw}, strict=True)
  except (ValueError, HTTPException):
    return None


async def _staged_package_identity(
  manifest: dict, manifest_url: str, source_url: str, db: Session | None,
) -> dict[str, str | None]:
  """Check a staged manifest's address, then resolve a permanent identity.

  The address is checked before any identity request, so a package at the
  wrong address never reaches the network.
  """
  validate_manifest_address(manifest, manifest_url, db=db)
  if db is not None:
    db.rollback()
  if not manifest.get("package_id"):
    return {}
  async with pinned_client(timeout=_HTTP_TIMEOUT) as cli:
    source_identity, predecessor, canonical = await _resolve_package_identity(
      cli, manifest, source_url,
    )
  return dict(
    source_identity=source_identity,
    predecessor_source_identity=predecessor,
    canonical_source_url=canonical,
  )


async def _read_staged_candidate(
  staged: app_git.Staged, source_url: str, identity: dict[str, str | None],
) -> InstallCandidate:
  try:
    return (await asyncio.to_thread(functools.partial(
      read_git_install_candidate, staged.path, staged.sha, source_url,
      **identity,
    ))).candidate
  except PackageTooLarge as exc:
    raise HTTPException(
      413, detail={"code": "package_too_large", "message": str(exc)},
    ) from exc
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
    raise HTTPException(422, f"The app's Git package is unusable: {exc}") from exc


async def _staged_clone_candidate(
  manifest_url: str, staged: app_git.Staged, *, db: Session | None,
) -> InstallCandidate:
  """Read a root Git package from its staged clone, binding its identity.

  Identity comes from the bounded manifest alone, so the package is read
  (and its digests derived) once, already bound to it.
  """
  source_url = requested_manifest_source(manifest_url)[0]
  manifest = await asyncio.to_thread(_read_staged_manifest, staged)
  if manifest is None:
    # The full read reports an absent, oversized or invalid manifest. If it
    # accepts the package, only the bounded read failed (a transient Git read
    # error): bind identity from the manifest the full read accepted instead.
    candidate = await _read_staged_candidate(staged, source_url, {})
    identity = await _staged_package_identity(
      candidate.manifest, manifest_url, source_url, db,
    )
    if not identity:
      return candidate
  else:
    identity = await _staged_package_identity(
      manifest, manifest_url, source_url, db,
    )
  return await _read_staged_candidate(staged, source_url, identity)


def _require_reviewed_capabilities(
  manifest: dict, reviewed_capability_digest: str | None,
) -> None:
  if reviewed_capability_digest is None:
    return
  capability_contract, capability_digest = contract_and_digest(manifest)
  if capability_digest != reviewed_capability_digest:
    raise HTTPException(409, detail={
      "code": "capability_changed",
      "message": (
        "The app's capabilities changed after they were reviewed. "
        "Review the current manifest and try again."
      ),
      "manifest": manifest,
      "capability_contract": capability_contract,
      "capability_digest": capability_digest,
      "access_rows": app_consent.rows_for_manifest(manifest, capability_contract),
    })


def _require_reviewed_candidate(
  candidate: InstallCandidate,
  *,
  manifest: dict | None,
  reviewed_capability_digest: str | None,
  reviewed_source_digest: str | None,
  expected_candidate_digest: str | None,
  is_update: bool,
) -> None:
  """Refuse a candidate that is not the package the owner reviewed."""
  if manifest is not None and manifest != candidate.manifest:
    raise HTTPException(409, "Pending update manifest changed.")
  _require_reviewed_capabilities(candidate.manifest, reviewed_capability_digest)
  if (
    reviewed_source_digest is not None
    and reviewed_source_digest != candidate.source_review_digest
  ):
    raise HTTPException(409, detail={
      "code": "update_changed",
      "message": (
        "The app source changed after it was reviewed. "
        + (
          "Refresh the update preview and try again." if is_update
          else "Review the app again and try again."
        )
      ),
    })
  if (
    expected_candidate_digest is not None
    and expected_candidate_digest != candidate.candidate_digest
  ):
    raise HTTPException(409, detail={
      "code": "pending_update_changed",
      "message": (
        "The pending update changed upstream. "
        "Review the latest update and start again."
      ),
    })


async def _authorize_source_handoff(
  target: InstallTarget, candidate: InstallCandidate,
) -> None:
  """Require the currently trusted source to authorize a new repository.

  Matching package ids select the existing row, but never grant source trust:
  an unrelated repository can copy a public manifest. A different provider
  identity is accepted only when the old installed manifest names the new
  source and both manifests carry the same permanent package id.
  """
  if not target.source_handoff_required:
    return
  existing = target.existing
  package_id = target.package_id
  if existing is None or not package_id or not existing.manifest_url:
    raise HTTPException(409, "App source changed without a trusted handoff.")
  async with pinned_client(timeout=_HTTP_TIMEOUT) as cli:
    # The old row's own address is fetched without the stored id binding:
    # trust here comes from `package_id` + `moved_to`, and a `package_id` app
    # may have changed its manifest id without `previous_id`.
    old_manifest, _ = await _fetch_and_validate_manifest(
      cli,
      manifest_url=stored_manifest_fetch_url(existing.manifest_url),
      manifest=None,
      raw_base=None,
    )
    moved_to = old_manifest.get("moved_to")
    if (
      old_manifest.get("package_id") != package_id
      or not isinstance(moved_to, dict)
    ):
      raise HTTPException(409, "App source changed without a trusted handoff.")
    moved_url = moved_to.get("manifest_url")
    if not isinstance(moved_url, str):
      raise HTTPException(409, "App source changed without a trusted handoff.")
    moved_identity, _ = await _resolve_source_identity(cli, moved_url)
  if moved_identity != candidate.source_identity:
    raise HTTPException(409, "Trusted app handoff names a different source.")


@dataclass(frozen=True)
class PackageRow:
  """The installed row, if any, that one candidate package is."""

  row: models.App | None
  renamed: bool = False  # Found through the candidate's `previous_id`.
  origin_migration: tuple[str, str] | None = None


def resolve_package_row(
  db: Session,
  manifest: dict,
  source_url: str,
  *,
  source_identity: str | None = None,
  predecessor_identity: str | None = None,
  canonical_source_url: str | None = None,
) -> PackageRow:
  """Resolve which installed row a candidate package is, by one rule.

  Install, update-check and the update preview all call this, so discovery
  offers an update exactly when install would apply it to that row:

  1. Its permanent ``package_id`` and its address (the stored identity, any
     ref of the same root GitHub package, or a proven legacy origin); when
     both find a row they must agree.
  2. Its ``previous_manifest_url`` under the same ``id``, for a permanent
     package whose resolved provider identity (``source_identity``) equals the
     predecessor's, proving both addresses name one repository.
  3. A ``previous_id`` rename: under the current address, then (permanent
     package) that proven predecessor address, or (otherwise) a predecessor
     owned by the same GitHub account. A deleted predecessor is not revived.

  A row holding a permanent ``package_id`` is only ever that package.
  Raises HTTPException 409 for a conflicting or changed permanent identity and
  400 for a rename across GitHub accounts without a permanent identity.
  """
  manifest_id = manifest["id"]
  package_id = manifest.get("package_id")
  previous_id = manifest.get("previous_id")
  previous_source = manifest.get("previous_manifest_url")
  key_source = canonical_source_url or source_url
  existing = _find_install_identity_row(
    db, source_url=source_url, manifest_id=manifest_id, package_id=package_id,
  )
  renamed = False
  origin_migration = None
  if existing is None:
    # Only a resolved provider identity proves one repository; two unknown
    # identities (a legacy row's update, an unresolved fetch) prove nothing.
    same_repository = bool(
      package_id and previous_source and source_identity is not None
      and predecessor_identity == source_identity
    )
    # (address, manifest id, whether the row is a `previous_id` rename)
    lookups: list[tuple[str, str, bool]] = []
    if same_repository:
      lookups.append((previous_source, manifest_id, False))
    if previous_id and package_id:
      lookups.append((key_source, previous_id, True))
      if same_repository:
        lookups.append((previous_source, previous_id, True))
    elif previous_id:
      predecessor_source, origin_migration = _reviewed_predecessor_source(
        manifest, key_source,
      )
      lookups.append((predecessor_source, previous_id, True))
    for lookup_source, lookup_id, rename in lookups:
      existing = _find_install_identity_row(
        db, source_url=lookup_source, manifest_id=lookup_id,
      )
      if existing is not None:
        renamed = rename
        break
    if renamed and existing.deleted_at is not None:
      existing, renamed = None, False
    if not renamed:
      origin_migration = None
  if existing is not None:
    try:
      require_permanent_package(manifest, existing.package_id)
    except ValueError as exc:
      raise HTTPException(409, str(exc)) from exc
  return PackageRow(existing, renamed, origin_migration)


def resolve_update_row(
  db: Session, manifest: dict, manifest_url: str, source_identity: str | None,
) -> PackageRow:
  """The row a Store update of this Git candidate would apply to.

  A Store update reads its package from the installed app's repository and
  binds it to that row's own source identity (``install_from_manifest``), so
  update discovery passes install the same inputs and needs no request.
  """
  fetch_url = requested_manifest_source(manifest_url)[0]
  return resolve_package_row(
    db,
    manifest,
    manifest_url,
    source_identity=source_identity,
    predecessor_identity=_same_repository_predecessor(
      manifest, fetch_url, source_identity,
    ),
    canonical_source_url=fetch_url if source_identity else None,
  )


def _select_install_target(
  db: Session,
  *,
  candidate: InstallCandidate,
  manifest_url: str | None,
  source: str,
  expected_app_id: int | None,
  publication_handoff_app_id: int | None = None,
) -> InstallTarget:
  """Resolve install/update/adoption identity without mutating the target row."""
  manifest = candidate.manifest
  manifest_id = manifest["id"]
  package_id = manifest.get("package_id")
  requested_source = (
    manifest_url if manifest_url is not None else candidate.raw_base
  )
  source_for_key = candidate.canonical_source_url or requested_source
  canonical_manifest_url = _canonical_identity_key(
    source_for_key, manifest_id,
  )
  resolved = resolve_package_row(
    db,
    manifest,
    requested_source,
    source_identity=candidate.source_identity,
    predecessor_identity=candidate.predecessor_source_identity,
    canonical_source_url=candidate.canonical_source_url,
  )
  existing = resolved.row
  origin_migration = resolved.origin_migration
  adopting_previous_id = resolved.renamed or bool(
    existing is not None
    and package_id
    and existing.package_id == package_id
    and existing.slug != manifest_id
  )

  required_app_id = (
    expected_app_id
    if expected_app_id is not None
    else publication_handoff_app_id
  )
  if required_app_id is not None and (
    existing is None or existing.id != required_app_id
  ):
    raise HTTPException(409, "Pending update no longer matches this app.")
  source_handoff_required = bool(
    existing is not None
    and existing.source_identity is not None
    and existing.source_identity != candidate.source_identity
  )
  if existing is not None and existing.source_identity and not candidate.source_identity:
    raise HTTPException(
      409, "This installed app requires a verifiable source identity.",
    )

  old_origin = _github_origin_url(existing.manifest_url) if existing else None
  new_origin = _github_origin_url(source_for_key)
  if old_origin and new_origin and not app_git.same_origin(old_origin, new_origin):
    origin_migration = (old_origin, new_origin)

  service = manifest.get("service")
  explicit_service_id = isinstance(service, dict) and "id" in service
  resolved_service_id = _manifest_service_id(manifest, app=existing)
  service_aliases = _manifest_service_aliases(manifest)
  # A fresh implicit service cannot be resolved until its unique slug is
  # allocated below. Explicit identities and every update are known now.
  if existing is not None or explicit_service_id:
    _assert_service_identity_available(
      db, service_id=resolved_service_id,
      app_id=existing.id if existing is not None else None,
    )
    _assert_service_aliases_available(
      db, aliases=service_aliases, service_id=resolved_service_id,
      app_id=existing.id if existing is not None else None,
    )
    _assert_service_transition_safe(
      existing, service_id=resolved_service_id, aliases=service_aliases,
    )

  return InstallTarget(
    existing=existing,
    mode="update" if existing else "install",
    adopting_previous_id=adopting_previous_id,
    adopting_trusted_origin=bool(
      existing is not None and existing.manifest_url is None
    ),
    trusted_catalog_origin=bool(
      existing is not None
      and trusted_catalog_checkout(
        existing.manifest_url, existing.source_dir, source_for_key,
        manifest_id,
      )
    ),
    canonical_manifest_url=canonical_manifest_url,
    origin_migration=origin_migration,
    package_id=package_id,
    source_identity=candidate.source_identity,
    source_handoff_required=source_handoff_required,
  )


def owner_timezone(db: Session) -> str | None:
  """The owner's recorded IANA timezone, when the shell has reported one."""
  from app import cron_tz

  owner = db.query(models.Owner).order_by(models.Owner.id).first()
  zone = owner.timezone if owner else None
  return zone if zone and cron_tz.valid_timezone(zone) else None


def _manifest_default_timezone(
  default: str, owner_zone: str | None,
) -> str | None:
  """The zone a manifest default is registered in; ``None`` is server time.

  A fixed wall-time default (``M H * * *``, ``M H * * 1-5``) means that time
  for the owner, so it is owned in the owner's timezone once known. Other
  cadences, and a zone the server already runs in, stay ordinary server cron.
  """
  from app import cron_tz

  if (
    owner_zone is None
    or cron_tz.parse_wall_clock_cron(default) is None
    or owner_zone == cron_tz.server_timezone_name()
  ):
    return None
  return owner_zone


def _apply_schedule_choice(
  app: models.App,
  choice: app_cron.ScheduleChoice,
  scaffold: Path,
) -> None:
  """Record who chose the schedule, then register it.

  Provenance is written first so a failed or interrupted registration can
  never leave a declaration whose origin a later update must guess. Accepted
  installs keep that intent on live-write failure for startup to retry.
  """
  from app import cron_tz

  app_cron.record_schedule_choice(app.id, choice)
  job_path = Path(app.source_dir) / choice.job
  if choice.timezone is None:
    app_cron.register_cron(
      app.slug, choice.cron, job_path, app.id, scaffold=scaffold,
    )
    return
  app_cron.register_cron(
    app.slug,
    cron_tz.materialize_zone_cron(choice.cron, choice.timezone),
    job_path,
    app.id,
    timezone=choice.timezone,
    zone_cron=choice.cron,
    scaffold=scaffold,
  )


async def converge_manifest_schedule_zones(
  db: Session, owner_zone: str | None,
) -> None:
  """Move every manifest-default schedule into the zone it belongs in.

  Runs when the owner's timezone is reported. Owner-chosen schedules keep
  their own zone; a default already in the right zone is left alone, so
  repeated reports are no-ops. A failure leaves that app for the next report.
  """
  async with fs_locks.install_uninstall_lock():
    db.rollback()
    await _converge_manifest_schedule_zones_unlocked(db, owner_zone)


async def _converge_manifest_schedule_zones_unlocked(
  db: Session, owner_zone: str | None,
) -> None:
  scaffold = app_cron.cron_scaffold(CRON_SCAFFOLD)
  if not scaffold.exists():
    return
  apps = db.query(models.App).filter(models.App.deleted_at.is_(None)).all()
  for app in apps:
    try:
      async with fs_locks.source_dir_lock(app.source_dir):
        choice = app_cron.read_schedule_choice(app.id)
        if choice is None or choice.source != "manifest":
          continue
        zone = _manifest_default_timezone(choice.cron, owner_zone)
        if zone == choice.timezone:
          continue
        await settled.run_settled(
          _move_manifest_schedule_zone, app,
          dataclasses.replace(choice, timezone=zone), scaffold,
        )
    except Exception:
      log.exception("schedule zone: app %s could not be moved", app.id)


def _move_manifest_schedule_zone(
  app: models.App, choice: app_cron.ScheduleChoice, scaffold: Path,
) -> None:
  with app_cron.schedule_choice_rollback(app.id):
    _apply_schedule_choice(app, choice, scaffold)


async def _sync_manifest_cron_unlocked(
  *,
  app: models.App,
  manifest: dict,
  drop_prior_cron: bool,
  bundled_job: bool,
  warnings: list[str],
  owner_zone: str | None,
) -> None:
  """Converge one accepted manifest's cron while its source lock is held.

  Store installs and explicit local-source applies are two acceptance paths for
  the same manifest contract. Keeping cron convergence here prevents either
  path from becoming add-only: an accepted update that drops its schedule must
  retire both the live entry and its replayable declaration.

  A schedule the owner chose survives while the app keeps the same schedule
  contract (``app_cron.owner_schedule_to_keep``); otherwise the manifest
  default is registered, in ``owner_zone`` when it is a fixed wall time.
  """
  await settled.run_settled(
    _sync_manifest_cron, app, manifest, drop_prior_cron,
    bundled_job, warnings, owner_zone,
  )


def _sync_manifest_cron(
  app: models.App, manifest: dict, drop_prior_cron: bool, bundled_job: bool,
  warnings: list[str], owner_zone: str | None,
) -> None:
  schedule = manifest.get("schedule")
  job_name = schedule.get("job") if schedule else None
  cron_job_name = job_name or "job.sh"
  has_cron = bool(schedule and schedule.get("default"))
  if not (bundled_job or has_cron or drop_prior_cron):
    return

  app_data_dir = Path(app.source_dir)
  app_data_dir.mkdir(parents=True, exist_ok=True)
  kept = (
    app_cron.owner_schedule_to_keep(app.id, schedule["default"], cron_job_name)
    if has_cron else None
  )
  if drop_prior_cron and kept is None:
    _drop_app_cron(app_data_dir)
    (app_cron.schedule_state_dir(app.id) / "init-cron.sh").unlink(missing_ok=True)
    app_cron.clear_schedule_choice(app.id)
  active_cron_scaffold = app_cron.cron_scaffold(CRON_SCAFFOLD)
  if has_cron and active_cron_scaffold.exists():
    default = schedule["default"]
    if kept is not None:
      choice = dataclasses.replace(kept, manifest_default=default)
    else:
      choice = app_cron.ScheduleChoice(
        source="manifest",
        cron=default,
        job=cron_job_name,
        timezone=_manifest_default_timezone(default, owner_zone),
        manifest_default=default,
      )
    _apply_schedule_choice(app, choice, active_cron_scaffold)
  elif has_cron:
    sentinel = app_data_dir / ".cron-pending.json"
    sentinel.write_text(
      json.dumps({
        "expr": schedule["default"],
        "job": cron_job_name,
        "status": "pending — init-cron-scaffold.sh not on PATH",
      }),
      encoding="utf-8",
    )
    warnings.append(
      "cron: scaffold script not available — registration pending"
    )


async def _run_post_commit_effects(
  db: Session,
  *,
  app: models.App,
  mode: str,
  candidate: InstallCandidate,
  warnings: list[str],
  was_usable: bool = False,
) -> None:
  """Converge cron, skills, and initialization after durability.

  Every failure here becomes a warning. The app row and selected bundle are
  already durable, so this phase must never enter the pre-commit rollback path.
  """
  from app import app_setup
  app_setup.request_run()
  manifest = candidate.manifest
  schedule = manifest.get("schedule")
  job_name = schedule.get("job") if schedule else None
  has_cron = bool(schedule and schedule.get("default"))
  drop_prior_cron = mode == "update"
  if candidate.bundled_job or has_cron or drop_prior_cron:
    app_data_dir = Path(app.source_dir)
    try:
      async with fs_locks.source_dir_lock(str(app_data_dir)):
        if not db.query(models.App.id).filter(models.App.id == app.id).first():
          raise HTTPException(404, "App removed before cron registration.")
        await _sync_manifest_cron_unlocked(
          app=app,
          manifest=manifest,
          drop_prior_cron=drop_prior_cron,
          bundled_job=bool(candidate.bundled_job),
          warnings=warnings,
          owner_zone=owner_timezone(db),
        )
    except HTTPException as exc:
      log.warning(
        "install: job-script/cron step failed post-commit — %s",
        exc.detail,
      )
      warnings.append(f"cron: registration failed — {exc.detail}")
    except Exception as exc:
      log.exception("install: job-script/cron step failed post-commit")
      warnings.append(f"cron: registration failed — {exc!r}")

  try:
    await _sync_app_skills(db, app, manifest, warnings)
  except Exception as exc:
    log.exception("install: skill sync failed post-commit")
    warnings.append(f"skills: sync failed — {exc!r}")
  if mode == "update" and was_usable and app_consent.dormant(app):
    # Only the owner's answer re-enables it, and an app they rarely open would
    # otherwise stop without a word.
    app_id, app_name = app.id, app.name
    try:
      from app import push
      from app.database import SessionLocal

      # Its own session: the notification's commit must not expire the
      # install's app row, which the caller still reads.
      with SessionLocal() as notify_db:
        owner = notify_db.query(models.Owner).first()
        if owner is not None:
          push.notify_owner(
            notify_db, owner.id, title=f"{app_name} needs your approval",
            body="An update asks for more access. Open the app to review it.",
            source_type="app", source_id=str(app_id),
            target=f"/shell/?app={app_id}", tag=f"app-consent-{app_id}",
          )
    except Exception as exc:
      log.exception("install: approval notification failed post-commit")
      warnings.append(f"notification: not sent — {exc!r}")

  if (
    mode == "install"
    and schedule
    and schedule.get("initialize_on_install") is True
    and job_name
    and app.source_dir
    # A dormant app's first run starts when the owner accepts (routes/app_consent).
    and not app_consent.dormant(app)
  ):
    try:
      from app.app_jobs import launch_app_job

      source_dir = Path(app.source_dir)
      launch_app_job(
        app.id,
        source_dir / job_name,
        source_dir,
        wait_for_ready=True,
      )
      warnings.append("initialization waiting for startup readiness")
    except Exception as exc:
      log.exception("install: initialization job failed to start")
      warnings.append(f"initialization failed to start — {exc!r}")


async def _prepare_app_row(
  db: Session,
  *,
  candidate: InstallCandidate,
  target: InstallTarget,
  source: str,
  journal: InstallJournal,
  warnings: list[str],
) -> models.App:
  """Create/revive/adopt the target row and converge its filesystem identity.

  Source, capability, and offline metadata for an existing app deliberately
  remain untouched here; they are accepted only after source reconciliation
  succeeds. Identity migrations are journaled so a later compile/commit failure
  restores the previous source tree.
  """
  manifest = candidate.manifest
  manifest_id = manifest["id"]
  data_dir = Path(get_settings().data_dir)
  existing = target.existing
  adopting_previous_id = target.adopting_previous_id
  canonical_manifest_url = target.canonical_manifest_url

  if existing:
    app = existing
    app.name = manifest["name"]
    app.description = manifest.get("description", "")
    db.flush()

    if adopting_previous_id:
      old_slug = app.slug
      old_source_dir = app.source_dir
      target_slug = manifest_id
      target_source_dir = str(data_dir / "apps" / target_slug)
      converged = False
      if old_source_dir and Path(old_source_dir).is_dir():
        if old_source_dir == target_source_dir:
          converged = app.slug == target_slug
        else:
          async with fs_locks.source_dir_lock(old_source_dir):
            try:
              _reject_if_source_dir_taken(
                db, target_source_dir, exclude_id=app.id,
              )
              target_taken = False
            except HTTPException:
              target_taken = True
            if not target_taken and not Path(target_source_dir).exists():
              # Unregister is already a pre-commit mutation, even if the
              # source rename never starts. Arm its compensation before the
              # worker can change cron or cancellation can interrupt the await.
              journal.rollback_actions.append(
                _reconcile_cron_after_install_rollback
              )
              try:
                await settled.run_settled(
                  _unregister_cron, Path(old_source_dir),
                )
              except asyncio.CancelledError:
                # The unregister worker has drained. Restore the uncommitted
                # row and filesystem journal while both lifecycle/source locks
                # still exclude competitors, then propagate the cancellation.
                db.rollback()
                await settled.run_settled(
                  journal.rollback_materialization,
                )
                raise
              os.rename(old_source_dir, target_source_dir)
              converged = True
              journal.rollback_actions.append(
                lambda o=old_source_dir, n=target_source_dir:
                  os.rename(n, o)
                  if Path(n).is_dir() and not Path(o).exists()
                  else None
              )
      if converged:
        app.slug = target_slug
        app.source_dir = target_source_dir
        app.manifest_url = canonical_manifest_url
        db.flush()
      elif old_source_dir and Path(old_source_dir).is_dir():
        warnings.append(
          f"could not rename slug {old_slug}->{manifest_id}: target in use"
        )
      else:
        warnings.append(
          f"could not rename slug {old_slug}->{manifest_id}: source missing"
        )

    if target.origin_migration and app_git.has_origin(app.source_dir):
      old_origin, new_origin = target.origin_migration
      current_origin = app_git.origin_url(app.source_dir)
      if current_origin and not (
        app_git.same_origin(current_origin, old_origin)
        or app_git.same_origin(current_origin, new_origin)
      ):
        raise HTTPException(
          409, "Installed app origin no longer matches its trusted source.",
        )
      if current_origin and not app_git.same_origin(current_origin, new_origin):
        app_git.set_origin_url(app.source_dir, new_origin)
        journal.rollback_actions.append(
          lambda p=app.source_dir, u=current_origin:
          app_git.set_origin_url(p, u)
          if Path(p).is_dir() and app_git.has_origin(p)
          else None
        )
    return app

  slug = manifest_id
  if db.query(models.App).filter(models.App.slug == slug).first():
    slug = allocate_unique_slug(db, manifest["name"])
    activity.log_event(
      "slug_collision",
      requested_slug=manifest_id,
      assigned_slug=slug,
      source=source,
    )
  source_dir = str(data_dir / "apps" / slug)
  journal.created_paths.append(Path(source_dir))
  permissions = manifest.get("permissions") or {}
  service_id = _manifest_service_id(manifest, slug=slug)
  service_aliases = _manifest_service_aliases(manifest)
  capability_contract = _contract_with_service_identity(
    candidate.capability_contract,
    service_id=service_id,
    aliases=service_aliases,
  )
  _assert_service_identity_available(db, service_id=service_id, app_id=None)
  _assert_service_aliases_available(
    db, aliases=service_aliases, service_id=service_id, app_id=None,
  )
  app = models.App(
    name=manifest["name"],
    description=manifest.get("description", ""),
    jsx_source=candidate.entry_bytes.decode("utf-8"),
    source_dir=source_dir,
    slug=slug,
    manifest_url=canonical_manifest_url,
    package_id=target.package_id,
    source_identity=target.source_identity,
    service_id=service_id,
    cross_app_access=permissions.get("cross_app_access", "none"),
    share_with_apps=permissions.get("share_with_apps", "none"),
    chat_log_access=permissions.get("chat_log_access", "none"),
    manage_apps=bool(permissions.get("manage_apps", False)),
    manage_skills=bool(permissions.get("manage_skills", False)),
    github_access=bool(permissions.get("github_access", False)),
    github_connect=bool(permissions.get("github_connect", False)),
    filesystem_access=bool(permissions.get("filesystem_access", False)),
    connections_manage=bool(permissions.get("connections_manage", False)),
    connect_manage=bool(permissions.get("connect_manage", False)),
    offline_capable=bool(manifest.get("offline_capable", False)),
    embeds_agent=bool(manifest.get("embeds_agent", False)),
    shell_shortcuts=bool(manifest.get("shell_shortcuts", True)),
    offline_contract=manifest.get("offline") or None,
    system_prompt_file=manifest.get("system_prompt") or None,
    capability_contract=capability_contract,
    project_templates_json=manifest.get("project_templates") or None,
    # Apps that ship with the platform are accepted with it; every other
    # Store install waits for the owner's first-open answer.
    accepted_consent=(
      app_consent.platform_acceptance(
        capability_contract,
        project_builders=bool(manifest.get("project_templates")),
      )
      if source == "bootstrap" else None
    ),
  )
  db.add(app)
  db.flush()
  return app


def _commit_prepared_app(
  db: Session,
  app: models.App,
  journal: InstallJournal,
  *,
  revive_after_commit: bool,
) -> None:
  """Make prepared source durable, then reveal a recovered pinned app.

  Reinstall performs awaited filesystem work and may flush row metadata long
  before its final commit, so it must not hold the drawer's blocking process
  lock for the whole operation. A tombstoned app stays outside the live pinned
  identity set while that work is made durable. Its visibility transition is
  then one short, independently committed drawer mutation.

  If the revival commit fails, the fully prepared app remains safely hidden
  and a retry can recover it; the compensation journal must not remove source
  that the first commit already made authoritative.
  """
  db.commit()
  journal.mark_durable()
  if not revive_after_commit:
    return
  with drawer_pins.serialized_write():
    app.deleted_at = None
    db.commit()


@dataclass(frozen=True)
class ActivationPlan:
  """Source and manifest state selected by reconciliation for activation."""

  source_tree: dict[str, bytes]
  published_source_tree: dict[str, bytes]
  static_assets: dict[str, bytes]
  dropped_source_paths: set[str]
  exec_paths: frozenset[str]
  git_exec_paths: frozenset[str]
  entry_key: str
  job_name: str | None
  cloned_install: bool
  cloned_update: bool
  merge_applied: bool
  updating: bool
  canonical_manifest_url: str
  capability_contract: dict
  package_id: str | None
  source_identity: str | None
  # Set on the second pass of an install whose manifest declares a Python lock.
  python_check: CheckedPythonTree | None = None


@dataclass(frozen=True)
class CheckedPythonTree:
  """A reconciled source tree whose Python entries passed outside the locks."""

  # The source-dir tree both passes snapshot at the same point, before compile
  # and commit; with the same candidate's assets and manifest it fixes every
  # served byte.
  tree_oid: str
  env: app_python_env.StagedEnv


class _UncheckedPythonTree(Exception):
  """The first pass's reconciled tree, handed out once its locks are released.

  Building and smoke-running an env takes minutes at worst, so it must not run
  inside the install's SQLite write transaction or source-dir lock. The first
  pass therefore stops before anything durable: the install rolls its source
  writes back under the source-dir lock, checks this tree, and reconciles again.
  """

  def __init__(
    self,
    tree_oid: str,
    runtime: applied_app_runtime.PreparedRuntime,
    app_id: int | None,
  ) -> None:
    super().__init__("reconciled Python tree needs checking")
    self.tree_oid = tree_oid
    self.runtime = runtime
    self.app_id = app_id

  def discard(self) -> None:
    """Remove the handed-out runtime copy once checked, or once abandoned."""
    shutil.rmtree(self.runtime.root, ignore_errors=True)


async def _check_reconciled_python(pending: _UncheckedPythonTree) -> CheckedPythonTree:
  """Build or reuse the env and smoke-run the exact reconciled tree, unlocked."""
  data_dir = Path(get_settings().data_dir)
  try:
    env = await asyncio.to_thread(
      app_python_env.prepare_env, data_dir, pending.app_id, pending.runtime.root,
    )
  except app_python_env.PythonEnvBuildError as exc:
    raise HTTPException(422, detail={
      "code": "python_env_failed",
      "message": f"Could not build the app's Python environment. {exc}",
    }) from exc
  except Exception as exc:
    log.exception("install: unexpected failure while checking the Python environment")
    raise HTTPException(
      500, "Install failed due to an unexpected server error.",
    ) from exc
  finally:
    pending.discard()
  return CheckedPythonTree(tree_oid=pending.tree_oid, env=env)


def _require_package_python_lock(
  manifest: dict, package_tree: dict[str, bytes], runtime_root: Path,
) -> None:
  """Refuse a reconciled tree whose Python lock is not the reviewed package's."""
  try:
    key = app_python_env.declared_key(runtime_root)
  except app_python_env.PythonEnvUnavailable as exc:
    raise HTTPException(422, detail={
      "code": "python_env_failed", "message": str(exc),
    }) from exc
  package_lock = package_tree.get(python_lock(manifest))
  if package_lock is None or key != app_python_env.env_key(package_lock):
    # Only a local edit to the lock merged into the update can differ here.
    raise HTTPException(409, detail={
      "code": "python_lock_diverged",
      "message": (
        "This app's local source changes its Python lock, so it no longer "
        "matches the update's. The update was not installed; reconcile the "
        "lock in the app source, Apply it, and retry."
      ),
    })


def _apply_manifest_metadata(
  db: Session,
  app: models.App,
  *,
  manifest: dict,
  canonical_manifest_url: str,
  capability_contract: dict,
  package_id: str | None,
  source_identity: str | None,
  entry_source: str | None,
) -> None:
  """Attach one reviewed package identity and its runtime declarations.

  A normal install/update calls this while activating the reviewed source
  tree. The verified publication handoff also calls it when later local edits
  conflict: that action must still connect the original app row to its public
  identity, while deliberately leaving its served source and bundle untouched
  for the ordinary resolver.
  """
  app.version = str(manifest.get("version", "")).strip() or None
  app.theme_color = _manifest_color(manifest.get("theme_color"))
  app.background_color = (
    _manifest_color(manifest.get("background_color")) or app.theme_color
  )
  app.display = _manifest_display(manifest.get("display"))
  if entry_source is not None:
    app.jsx_source = entry_source
  app.manifest_url = canonical_manifest_url
  if package_id:
    app.package_id = package_id
  if source_identity:
    app.source_identity = source_identity
  app.service_id = _manifest_service_id(manifest, app=app)
  _sync_service_aliases(db, app=app, manifest=manifest)
  capability_contract = _contract_with_service_identity(
    capability_contract,
    service_id=app.service_id,
    aliases=_manifest_service_aliases(manifest),
  )
  app.published_manifest_url = None
  permissions = manifest.get("permissions") or {}
  app.cross_app_access = permissions.get(
    "cross_app_access", app.cross_app_access,
  )
  app.share_with_apps = permissions.get(
    "share_with_apps", app.share_with_apps,
  )
  app.chat_log_access = permissions.get(
    "chat_log_access", app.chat_log_access,
  )
  # Privileged grants are opt-in on every version; omission revokes them.
  app.manage_apps = bool(permissions.get("manage_apps", False))
  app.manage_skills = bool(permissions.get("manage_skills", False))
  app.github_access = bool(permissions.get("github_access", False))
  app.github_connect = bool(permissions.get("github_connect", False))
  app.filesystem_access = bool(permissions.get("filesystem_access", False))
  app.connections_manage = bool(permissions.get("connections_manage", False))
  app.connect_manage = bool(permissions.get("connect_manage", False))
  if "offline_capable" in manifest:
    app.offline_capable = bool(manifest["offline_capable"])
  if "embeds_agent" in manifest:
    app.embeds_agent = bool(manifest["embeds_agent"])
  app.shell_shortcuts = bool(manifest.get("shell_shortcuts", True))
  app.offline_contract = manifest.get("offline") or None
  app.system_prompt_file = manifest.get("system_prompt") or None
  app.capability_contract = capability_contract
  # Projects workspace: declared project templates travel with the package
  # identity, so apply them here alongside the other manifest metadata for
  # every install/update and the reviewed-publication handoff.
  app.project_templates_json = manifest.get("project_templates") or None


async def _activate_install_source(
  db: Session,
  *,
  app: models.App,
  manifest: dict,
  plan: ActivationPlan,
  journal: InstallJournal,
  data_dir: Path,
) -> str | None:
  """Publish the reconciled source tree while its source-dir lock is held.

  Returns the upstream equivalence ref that becomes safe to retire only after
  the caller commits the row. Every filesystem mutation is registered with the
  journal before this function returns, so the outer transaction retains one
  rollback boundary.

  A tree that declares a Python lock is published only on a second pass: the
  first pass writes the reconciled source, freezes its exact runtime tree, and
  raises ``_UncheckedPythonTree`` so the install can roll back and check it.
  """
  entry_source = plan.source_tree[plan.entry_key].decode("utf-8")
  _apply_manifest_metadata(
    db,
    app,
    manifest=manifest,
    canonical_manifest_url=plan.canonical_manifest_url,
    capability_contract=plan.capability_contract,
    package_id=plan.package_id,
    source_identity=plan.source_identity,
    entry_source=entry_source,
  )

  staged_bundle = data_dir / "compiled" / f"app-{app.id}.js.staging"
  source_dir = Path(app.source_dir)

  _reject_if_source_dir_taken(db, str(source_dir), exclude_id=app.id)
  source_dir.mkdir(parents=True, exist_ok=True)
  python_declared = python_lock(manifest) is not None
  jsx_file = source_dir / "index.jsx"
  if not plan.cloned_install:
    for rel, content in plan.source_tree.items():
      target = source_dir / rel
      backup = (
        jsx_file.with_suffix(".jsx.bak")
        if rel == "index.jsx"
        else target.with_name(target.name + ".bak")
      )
      target.parent.mkdir(parents=True, exist_ok=True)
      _assert_within(source_dir, target, f"source_files {rel}")
      _write_source_file(
        target,
        content,
        backup,
        journal.created_paths,
        journal.rollback_actions,
        journal.commit_actions,
      )
      if rel in plan.exec_paths or rel in plan.git_exec_paths:
        target.chmod(0o755)
    _prune_dropped_source_files(
      source_dir,
      plan.dropped_source_paths,
      journal.rollback_actions,
      journal.commit_actions,
    )
    if not plan.cloned_update:
      _check_source_completeness(
        app_name=str(manifest.get("name") or app.slug),
        manifest=manifest,
        # Validate the published package against its own manifest, not the
        # reconciled tree.  A clean update merge may legitimately retain
        # locally applied sibling modules which the upstream manifest cannot
        # declare; checking that merged tree makes every later synthetic
        # fallback misdiagnose those preserved edits as a broken release.
        source_tree=plan.published_source_tree,
        static_assets=plan.static_assets,
      )

  _write_static_assets(
    source_dir,
    plan.static_assets,
    journal.created_paths,
    journal.rollback_actions,
    journal.commit_actions,
  )
  # The source dir now holds exactly what the commit below records.
  runtime_manifest = json.dumps(manifest, sort_keys=True).encode()
  if python_declared:
    # Both passes snapshot here, before anything is compiled or committed, so
    # a stale second pass is refused while it has nothing durable to undo.
    snapshot = await asyncio.to_thread(app_git.snapshot_worktree, source_dir)
    if plan.python_check is None:
      runtime = await asyncio.to_thread(
        applied_app_runtime.prepare_runtime, source_dir, snapshot.tree_oid,
        static_assets=plan.static_assets, runtime_manifest=runtime_manifest,
      )
      try:
        _require_package_python_lock(
          manifest, plan.published_source_tree, runtime.root,
        )
      except BaseException:
        shutil.rmtree(runtime.root)
        raise
      raise _UncheckedPythonTree(
        snapshot.tree_oid, runtime, app.id if plan.updating else None,
      )
    if snapshot.tree_oid != plan.python_check.tree_oid:
      raise HTTPException(409, detail={
        "code": "python_check_stale",
        "message": (
          "The app's source changed while its Python code was being checked. "
          "The update was not installed; try again."
        ),
      })

  await compile_jsx(
    entry_source,
    out_path=staged_bundle,
    source_path=source_dir / plan.entry_key,
    source_root=source_dir,
  )
  _publish_install_bundle(
    app, staged_bundle, journal.rollback_actions, journal.commit_actions,
  )

  commit_message = (
    f"install: {manifest.get('name', app.slug)} "
    f"v{manifest.get('version', 'unknown')}"
  )
  equivalence_target = None
  if plan.merge_applied and app.upstream_commit:
    await asyncio.to_thread(
      app_git.commit_replay,
      source_dir,
      app.upstream_commit,
      commit_message,
    )
    equivalence_target = app.upstream_commit
  else:
    await asyncio.to_thread(app_git.commit_local, source_dir, commit_message)
  app.source_commit = await asyncio.to_thread(
    app_git.head_sha, source_dir, app_git.LOCAL_BRANCH,
  )
  runtime_staged = await asyncio.to_thread(
    applied_app_runtime.prepare_runtime, source_dir, app.source_commit,
    static_assets=plan.static_assets, runtime_manifest=runtime_manifest,
  )
  if plan.python_check is not None:
    try:
      published_env = app_python_env.publish_env(
        data_dir, app.id, plan.python_check.env,
      )
    except BaseException:
      shutil.rmtree(runtime_staged.root)
      raise
    journal.rollback_actions.append(
      lambda: app_python_env.unpublish_env(published_env)
    )
  applied_app_runtime.publish_runtime(app, runtime_staged)
  return equivalence_target


async def install_requested_app(
  db: Session,
  *,
  manifest_url: str | None,
  manifest: dict | None = None,
  raw_base: str | None = None,
  reviewed_capability_digest: str | None = None,
  reviewed_source_digest: str | None = None,
  update_app_id: int | None = None,
  reviewed_upstream_commit: str | None = None,
) -> InstallResult:
  """One owner-reviewed Store install or update, the same for every caller.

  The install route and each entry of a server-owned update batch take this
  one path. Network transfers (``fetch_app_commit`` for a reviewed update,
  ``prepare_package`` for an install) finish before the lifecycle lock, so
  one slow package never holds up other apps' installs, updates or
  uninstalls. The local install then runs under that lock
  (``fs_locks.install_uninstall_lock`` has the full rationale), and its
  passes settle before a cancellation.
  """
  prepared = None
  try:
    if update_app_id is not None and reviewed_upstream_commit is not None:
      reviewed = db.get(models.App, update_app_id)
      reviewed_source = reviewed.source_dir if reviewed is not None else None
      # Release the connection for the transfer, as update-check does.
      db.rollback()
      if reviewed_source is not None and app_git.is_repo(reviewed_source):
        try:
          await fetch_app_commit(reviewed_source, reviewed_upstream_commit)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
          raise git_source_error(
            "Refresh the update and try again.",
            exc,
            code="update_changed",
            failure="The reviewed Git update is no longer available.",
          ) from exc
    elif update_app_id is None:
      prepared = await prepare_package(
        manifest_url, manifest, raw_base,
        reviewed_capability_digest=reviewed_capability_digest, db=db,
      )
    async with fs_locks.install_uninstall_lock():
      return await install_from_manifest(
        db,
        manifest_url=manifest_url,
        manifest=manifest,
        raw_base=raw_base,
        source="store",
        reviewed_capability_digest=reviewed_capability_digest,
        reviewed_source_digest=reviewed_source_digest,
        reviewed_app_id=update_app_id,
        reviewed_upstream_commit=reviewed_upstream_commit,
        prepared=prepared,
      )
  finally:
    if prepared is not None:
      await settled.run_settled(discard_staged, prepared.staged_clone)


async def install_from_manifest(
  db: Session,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  source: str = "url",
  reviewed_capability_digest: str | None = None,
  reviewed_source_digest: str | None = None,
  reviewed_app_id: int | None = None,
  reviewed_upstream_commit: str | None = None,
  expected_app_id: int | None = None,
  expected_upstream_commit: str | None = None,
  expected_candidate_digest: str | None = None,
  publication_handoff_app_id: int | None = None,
  prepared: PreparedPackage | None = None,
) -> InstallResult:
  """Return a structured durable install/update/conflict outcome.

  The parsed manifest dict comes back so callers can read fields the
  App row doesn't store (notably `version`) without re-fetching.
  `conflict_paths` is empty except on the 'conflict' mode below.

  Modes:
    - 'install' — created a new App row.
    - 'update' — manifest's id matched an existing app's manifest_url;
      that row's jsx_source + (missing) storage seeds + source_dir got
      refreshed in place. Icon + cron are re-applied to keep the
      end state coherent with the new manifest.
    - 'conflict' — ONLY when a three-way merge of the new upstream into
      the app's local edits conflicted. Nothing is
      clobbered: the on-disk source, the compiled bundle, and the DB
      row's jsx_source all keep the local edits; the new upstream bytes
      are recorded on the `upstream` branch for a later agent-resolution
      pass. `conflict_paths` names the files that need resolving. The
      App row is committed (so the recorded upstream sha persists) but
      the served app is unchanged.

  The per-app git model is unconditional. Every installed app has a canonical
  source directory and repository; updates never overwrite source blindly.
  Callers hold the lifecycle lock, so a fresh install or a publication handoff
  arrives ``prepared`` (by ``prepare_package``, or the handoff's verified
  candidate) and a reviewed update or conflict replay as a commit already made
  present by ``fetch_app_commit``. Only a legacy conflict replay whose commit
  has no manifest still fetches its package here.

  Failure modes:
    - Pre-commit failures (manifest fetch, validation, JSX compile,
      seed write, icon process) all raise HTTPException. The DB
      transaction rolls back, filesystem `_cleanup` removes anything
      we created, and on the update path the old compiled bundle is
      restored from its `.bak` snapshot — caller sees a clean failure.
    - Post-commit failures: cron registration runs AFTER `db.commit()`.
      The app is fully installed at that point; cron failure becomes a
      non-fatal warning appended to the returned `warnings` list. The
      owner can re-register cron manually by editing the schedule.
    - A tombstoned reinstall deliberately commits its prepared row while it is
      still hidden, then revives it in one short drawer-serialized commit. If
      revival fails, the durable prepared source remains hidden and a retry can
      recover it without exposing a half-installed pinned item.
    - A declared Python lock is checked against the exact reconciled tree
      between two passes, outside the transaction. A failed check (422) or a
      tree that changed meanwhile (409) leaves the previous revision live.
    - FastAPI surfaces each HTTPException with its proper status code;
      we never catch + swallow anything that would land the DB or
      filesystem in a half state.
  """
  reviewed_update_fields = (
    reviewed_app_id,
    reviewed_upstream_commit,
    reviewed_source_digest,
  )
  if (
    reviewed_app_id is not None or reviewed_upstream_commit is not None
  ) and not all(
    value is not None for value in reviewed_update_fields
  ):
    raise ValueError("reviewed update identity is incomplete")
  replay_fields = (
    expected_app_id, expected_upstream_commit, expected_candidate_digest,
  )
  if any(value is not None for value in replay_fields) and not all(
    value is not None for value in replay_fields
  ):
    raise RuntimeError("resolved update replay identity is incomplete")
  if publication_handoff_app_id is not None:
    if source != "publication_handoff":
      raise ValueError("publication handoff requires its dedicated source")
    if expected_app_id is not None:
      raise ValueError("publication handoff cannot be a pending replay")
  # Phase 1: immutable, review-bound candidate. A Store update and a conflict
  # replay both read the exact Git commit already selected by Review; fresh
  # installs keep the ordinary URL/inline package path.
  reviewed_commit = reviewed_upstream_commit or expected_upstream_commit
  reviewed_id = reviewed_app_id or (
    expected_app_id if expected_upstream_commit is not None else None
  )
  if reviewed_commit is not None and reviewed_id is not None:
    reviewed_app = db.get(models.App, reviewed_id)
    if reviewed_app is None or not app_git.is_repo(reviewed_app.source_dir):
      raise HTTPException(409, "Reviewed app source is no longer available.")
    reviewed_source_url = (
      requested_manifest_source(manifest_url)[0] if manifest_url
      else (_normalize_raw_base(raw_base or "") + "mobius.json")
    )
    try:
      has_manifest = await asyncio.to_thread(
        _reviewed_commit_has_manifest, reviewed_app.source_dir, reviewed_commit,
      )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
      raise HTTPException(
        409,
        detail={
          "code": "update_changed",
          "message": (
            "The reviewed Git update is no longer available. Refresh the "
            "update and try again."
          ),
        },
      ) from exc
    # Modern Store updates always carry the complete package in the selected
    # Git commit. Legacy URL-import conflict receipts predate that invariant;
    # replay those through their existing digest guard until the one-time
    # migration population disappears.
    if not has_manifest and expected_upstream_commit:
      candidate = await _fetch_install_candidate(
        manifest_url=manifest_url, manifest=manifest, raw_base=raw_base,
        reviewed_capability_digest=reviewed_capability_digest, db=db,
      )
    else:
      try:
        candidate = (await asyncio.to_thread(
          read_git_install_candidate,
          reviewed_app.source_dir,
          reviewed_commit,
          reviewed_source_url,
          source_identity=reviewed_app.source_identity,
        )).candidate
      except PackageTooLarge as exc:
        raise HTTPException(
          413, detail={"code": "package_too_large", "message": str(exc)},
        ) from exc
      except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        raise HTTPException(
          409,
          detail={
            "code": "update_changed",
            "message": (
              "The reviewed Git update is no longer available. Refresh the "
              "update and try again."
            ),
          },
        ) from exc
      validate_manifest_address(candidate.manifest, manifest_url, db=db)
  elif prepared is None:
    raise ValueError("an install without a reviewed commit needs its prepared package")
  else:
    candidate = prepared.candidate
    validate_manifest_address(candidate.manifest, manifest_url, db=db)
  _require_reviewed_candidate(
    candidate,
    manifest=manifest,
    reviewed_capability_digest=reviewed_capability_digest,
    reviewed_source_digest=reviewed_source_digest,
    expected_candidate_digest=expected_candidate_digest,
    is_update=reviewed_id is not None,
  )

  # Phase 2: immutable identity/update decision. No writes occur here.
  target = _select_install_target(
    db,
    candidate=candidate,
    manifest_url=manifest_url,
    source=source,
    expected_app_id=reviewed_app_id or expected_app_id,
    publication_handoff_app_id=publication_handoff_app_id,
  )
  await _authorize_source_handoff(target, candidate)

  # Phases 3 and 4. A declared Python lock takes two passes: the first stops
  # with the exact reconciled runtime tree and rolls back, that tree's env is
  # built and smoke-run holding no lock, and the second publishes only if it
  # reconciles the same source tree.
  install_candidate = functools.partial(
    _install_candidate,
    db,
    candidate=candidate,
    target=target,
    manifest_url=manifest_url,
    source=source,
    reviewed_upstream_commit=reviewed_upstream_commit,
    expected_upstream_commit=expected_upstream_commit,
    publication_handoff_app_id=publication_handoff_app_id,
    staged_clone=prepared.staged_clone if prepared is not None else None,
  )
  # Each pass holds the source lock across many Git threads and rolls back on
  # failure, and the env check runs threads from the handed-out runtime, all
  # under the caller's lifecycle lock. So each step settles before a cancelled
  # caller unwinds, and the cancellation then stops the install between steps.
  try:
    return await settled.settle(
      install_candidate(python_check=None), abandoned=_abandon_install_step,
    )
  except _UncheckedPythonTree as unchecked:
    pending = unchecked
  python_check = await settled.settle(
    _check_reconciled_python(pending), abandoned=_abandon_install_step,
  )
  return await settled.settle(
    install_candidate(python_check=python_check), abandoned=_abandon_install_step,
  )


def _abandon_install_step(step: asyncio.Future) -> None:
  """Release what a step left once its caller was cancelled."""
  outcome = step.exception()
  if isinstance(outcome, _UncheckedPythonTree):
    outcome.discard()
  elif outcome is not None:
    settled.log_abandoned_failure(step, "Install step")
  elif isinstance(step.result(), CheckedPythonTree):
    app_python_env.discard_env(step.result().env)


async def _install_candidate(
  db: Session,
  *,
  candidate: InstallCandidate,
  target: InstallTarget,
  manifest_url: str | None,
  source: str,
  reviewed_upstream_commit: str | None,
  expected_upstream_commit: str | None,
  publication_handoff_app_id: int | None,
  staged_clone: app_git.Staged | None,
  python_check: CheckedPythonTree | None,
) -> InstallResult:
  """Reconcile and activate one selected candidate in one transaction.

  ``python_check`` is the second pass's checked tree. It owns that env until
  publication links it, discarding it on rollback or conflict.
  """
  manifest = candidate.manifest
  raw_base = candidate.raw_base
  entry_bytes = candidate.entry_bytes
  icon_processed = candidate.icon_processed
  icon_warning = candidate.icon_warning
  bundled_job = candidate.bundled_job
  static_assets_fetched = candidate.static_assets
  source_files_fetched = candidate.source_files
  seeds_fetched = candidate.seeds
  capability_contract = candidate.capability_contract
  fetched_capability_digest = candidate.capability_digest
  candidate_digest = candidate.candidate_digest
  sched = manifest.get("schedule")
  python_env = python_check.env if python_check is not None else None
  existing = target.existing
  revive_after_commit = bool(existing and existing.deleted_at is not None)
  mode = target.mode
  # Whether the app could run before this install changes its contract: an
  # update that stops it is one the owner must be told about.
  was_usable = (
    existing is not None and existing.deleted_at is None
    and not app_consent.dormant(existing)
  )
  canonical_manifest_url = target.canonical_manifest_url

  warnings: list[str] = []
  conflict_paths: list[str] = []
  divergence: str = "none"
  reconciliation = app_git.ReconciliationReceipt()
  # Set when upstream content is actually folded into the served `main`
  # branch (a clean merge, or a forced take-upstream). The post-write
  # commit then replays the result on the upstream tip as its sole parent
  # (linear history) so the merge base advances — otherwise every later
  # update re-merges from the install point and conflicts spuriously. None
  # means a plain local commit (fresh install, or a conflict that left local
  # untouched).
  merge_applied = False
  equivalence_target_to_retire: str | None = None
  if icon_warning:
    warnings.append(icon_warning)

  # The per-app git model: an update merges the new upstream into the app's
  # local edits instead of clobbering them, so `source_tree` may end up being
  # the MERGED tree rather than the upstream bytes we just fetched.
  upstream_jsx_sha = hashlib.sha256(entry_bytes).hexdigest()
  # The schedule job script's bare filename — it is just one key in the source
  # tree, written executable. `exec_paths` carries that to record_upstream so
  # `upstream` and `main` agree on its mode; the cron phase reads `job_name` to
  # point the crontab entry at it.
  job_name = sched.get("job") if sched else None
  exec_paths = frozenset({job_name}) if job_name else frozenset()
  # Exec paths carried by a git tree we materialise on a cloned FF/clean-merge
  # UPDATE (read_ref_tree/read_merged_tree return bytes only, dropping the exec
  # bit). Defined at function scope because the disk-write loop that consumes it
  # runs for fresh installs too, where the update branch never sets it; the
  # cloned-update branches below reassign it. Empty for every non-cloned path.
  git_exec_paths: frozenset[str] = frozenset()
  # The ONE complete source tree that gets written to disk and recorded on
  # `upstream`: the entry, every declared sibling module, and the job script —
  # all just keys, no entry/sibling/job special-casing. `index.jsx` is one key.
  # A clean update replaces this with the MERGED tree so locally edited files
  # (the entry, a sibling, the job) survive instead of being clobbered.
  source_tree: dict[str, bytes] = {
    "index.jsx": entry_bytes,
    **source_files_fetched,
  }
  prompt_rel = manifest.get("system_prompt")
  if prompt_rel and len(source_tree.get(prompt_rel, b"")) > _SYSTEM_PROMPT_MAX_BYTES:
    raise HTTPException(
      400,
      f"Manifest system_prompt exceeds {_SYSTEM_PROMPT_MAX_BYTES} bytes.",
    )
  if job_name and bundled_job is not None:
    source_tree[job_name] = bundled_job
  # Keep the exact manifest-fetched package separate from the tree selected by
  # Git reconciliation below. Source completeness is a publication contract;
  # locally retained modules in a clean merge are outside that contract.
  published_source_tree = dict(source_tree)
  repo_ref = (
    _derive_repo_ref(manifest_url) if manifest_url is not None else None
  )
  cloned_install = False
  cloned_update = False
  # A one-time migration may have captured current source in a local Git
  # baseline before attaching its catalog origin. The first real-origin update
  # uses that captured commit as the explicit three-way base, then retires the
  # bridge instead of maintaining a second synthetic update model.
  git_merge_base_override: str | None = None
  # A catalog rename may adopt a manifest-less checkout whose canonical Git
  # origin proves the package identity but whose installer-owned branches were
  # created independently. Git still owns the merge verdict: compatible bytes
  # combine, and real differences surface through the ordinary conflict flow.
  allow_unrelated_histories = bool(
    target.adopting_previous_id and target.adopting_trusted_origin
  )
  # Source deletes are computed from old-upstream minus new-upstream. The prune
  # phase consumes this explicit diff so local-only tracked siblings are not
  # mistaken for files the manifest intentionally removed.
  dropped_source_paths: set[str] = set()
  # One canonical entry identity across fetched and origin-backed trees.
  # Explicit apply and Store install use the same path, so compilation never
  # branches on package provenance.
  entry_key = "index.jsx"
  # True once `source_tree` is the MERGED tree (a clean merge or forced
  # take-upstream): the post-write commit then replays the result on the
  # upstream tip as its sole parent so the merge base advances — otherwise
  # every later update re-merges from the install point and conflicts
  # spuriously. False means a plain local commit (fresh install, or a conflict
  # that left local untouched).

  # Phase 3: materialize under one compensation journal.
  # Every filesystem mutation registers its rollback/commit action on this
  # journal before the durable row commit.
  journal = InstallJournal()
  data_dir = Path(get_settings().data_dir)

  if python_env is not None:
    journal.rollback_actions.append(lambda: app_python_env.discard_env(python_env))

  try:
    app = await _prepare_app_row(
      db,
      candidate=candidate,
      target=target,
      source=source,
      journal=journal,
      warnings=warnings,
    )

    # --- Per-app git: record upstream + (on update) merge into local ---
    # The merge decision AND the disk write below run under ONE held span of
    # source_dir_lock — not two
    # separate critical sections — so explicit apply (which takes the same lock
    # before its own commit_local) cannot commit an agent edit in the gap and
    # have the write then clobber it (the edit would be lost from the live tree,
    # the bundle, and app.jsx_source, recoverable only from git history). We do
    # the merge BEFORE the compile so `source_tree` (which the compile + write
    # below consume) reflects the merged tree on a clean update, and so a
    # conflict can short-circuit both. The lock is released before the seeds
    # block takes app_storage_lock, preserving the documented acquisition order
    # (install_uninstall -> app_storage -> source_dir).
    git_source_dir = Path(app.source_dir)
    source_lock = fs_locks.source_dir_lock(str(git_source_dir))
    await source_lock.acquire()
    try:
      version = str(manifest.get("version", "unknown"))
      had_repo = app_git.is_repo(git_source_dir)
      # A pre-git-model app being adopted from the catalog — or any existing app
      # that somehow lost its repo — has editable owner source on disk and in DB
      # jsx_source but no per-app git history and no recorded upstream. Init the
      # repo now so the merge path below captures those on-disk edits onto `main`
      # BEFORE the catalog upstream is recorded. Without this the fresh-install
      # branch's align_local_to_upstream would reset --hard the tree to catalog
      # bytes and the owner's edits would land in ZERO git blobs — unrecoverable.
      adopt_repoless_source = bool(
        existing and not had_repo
      )
      if adopt_repoless_source:
        await asyncio.to_thread(app_git.ensure_repo, git_source_dir)
      merge_existing_source = existing and (had_repo or adopt_repoless_source)
      if merge_existing_source:
        prev_upstream_commit = app.upstream_commit
        if (
          prev_upstream_commit is None
          and await asyncio.to_thread(
            app_git.ref_exists, git_source_dir, app_git.UPSTREAM_BRANCH,
          )
        ):
          # Local apps created before the durable row recorded its Git baseline
          # still have the authoritative upstream ref. Adopt that exact commit
          # rather than treating a real local history as absent.
          prev_upstream_commit = await asyncio.to_thread(
            app_git.head_sha, git_source_dir, app_git.UPSTREAM_BRANCH,
          )
        if (
          expected_upstream_commit is not None
          and prev_upstream_commit != expected_upstream_commit
        ):
          raise HTTPException(
            409, "Pending update no longer matches the recorded upstream.",
          )
        restored_upstream = await asyncio.to_thread(
          app_git.restore_upstream_ref,
          git_source_dir, prev_upstream_commit,
        )
        if restored_upstream:
          log.warning(
            "install: restored %s upstream ref to DB-recorded commit %s",
            git_source_dir, prev_upstream_commit,
          )
        # A rolled-back pass, including the first pass of a Python check,
        # leaves `upstream` where it found it for the next reconciliation.
        journal.rollback_actions.append(
          lambda d=git_source_dir, c=prev_upstream_commit:
            app_git.restore_upstream_ref(d, c)
        )
        previous_upstream_paths = await asyncio.to_thread(
          _read_upstream_source_paths, git_source_dir, prev_upstream_commit,
        )
        # If a PRIOR resolver left an unresolved conflict (MERGE_HEAD still
        # set, markers on disk), abort it first — otherwise the commit_local
        # below would commit the conflict markers as "local edits" (silent
        # source corruption). The newer update supersedes the abandoned one
        # and re-merges against the latest upstream; the resolver chat is
        # deduped so this doesn't pile up chats.
        if expected_upstream_commit is not None:
          if await asyncio.to_thread(
            app_git.merge_in_progress, git_source_dir,
          ):
            raise HTTPException(
              409, "Resolve and save every conflict before replaying update.",
            )
        else:
          await asyncio.to_thread(
            app_git.abort_in_progress_merge, git_source_dir,
          )
        # Update of an app already on the Git model. First capture any
        # unapplied on-disk draft onto `main` so the divergence check and any
        # merge see the real local source.
        await asyncio.to_thread(
          app_git.commit_local, git_source_dir,
          "local edits before update",
        )
        # Decide divergence against the PREVIOUS upstream before advancing
        # it. When local `main` never diverged from what upstream last
        # shipped, the new upstream is the answer outright: no three-way
        # merge is needed or wanted. Taking the bytes verbatim here keeps
        # the no-edit case off merge_upstream entirely, so it can never
        # hinge on merge-tree's in-memory cat-file succeeding — the path
        # that, when it returned None, dropped to a local commit parented on
        # the old `main` tip and left `upstream` unreachable from `main`,
        # stranding the merge base at the install point and resolving the
        # NEXT update to stale local content. commit_replay still runs
        # (merge_applied gate) so the single-parent replay advances the base.
        # An adopted repo-less row has no recorded upstream to diverge from, so
        # `prev_upstream_commit` is empty and the check below would read "no
        # divergence" and take the catalog bytes verbatim — silently replacing
        # the owner source we just committed to `main`. Force the three-way
        # merge instead, so those edits are folded in cleanly or surfaced as an
        # owner-gated conflict (identical on-disk == catalog resolves clean).
        diverged = target.adopting_trusted_origin or adopt_repoless_source or (
          bool(prev_upstream_commit) and await asyncio.to_thread(
            app_git.local_diverged_from,
            git_source_dir, prev_upstream_commit,
          )
        )
        if expected_upstream_commit is not None:
          current_upstream = await asyncio.to_thread(
            app_git.head_sha, git_source_dir, app_git.UPSTREAM_BRANCH,
          )
          if current_upstream != expected_upstream_commit:
            raise HTTPException(
              409, "Pending update upstream ref changed before replay.",
            )
          cloned_update = await asyncio.to_thread(
            app_git.has_origin, git_source_dir,
          )
        elif reviewed_upstream_commit is not None or (
          repo_ref is not None
          and await asyncio.to_thread(app_git.has_origin, git_source_dir)
        ):
          try:
            if reviewed_upstream_commit is not None:
              upstream_sha = reviewed_upstream_commit
            else:
              upstream_sha = await settled.run_settled(
                _url_update_commit, git_source_dir, repo_ref[1], staged_clone,
              )
              if staged_clone is None:
                await asyncio.to_thread(
                  _require_git_candidate, git_source_dir, upstream_sha, candidate,
                )
            previous_tree = await asyncio.to_thread(
              app_git.read_ref_tree,
              git_source_dir,
              app_git.UPSTREAM_BRANCH,
            )
            synthetic_baseline = bool(
              prev_upstream_commit
            ) and replaces_migration_bridge(
              previous_tree, trusted_origin=target.trusted_origin,
            )
            if synthetic_baseline:
              await asyncio.to_thread(
                app_git.replace_upstream_ref,
                git_source_dir,
                upstream_sha,
                expected_sha=prev_upstream_commit,
              )
              promoted = app_git.FetchUpstreamResult(
                sha=upstream_sha,
                allow_unrelated_histories=True,
              )
              git_merge_base_override = prev_upstream_commit
            else:
              promoted = await asyncio.to_thread(
                app_git.promote_upstream,
                git_source_dir,
                upstream_sha,
                trusted_origin_adoption=target.trusted_origin,
              )
          except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            raise git_source_error(
              "The installed version was left unchanged.",
              exc,
              failure="The app's Git update could not be applied.",
            ) from exc
          app.upstream_commit = promoted.sha
          allow_unrelated_histories = (
            allow_unrelated_histories
            or promoted.allow_unrelated_histories
          )
          cloned_update = True
        if expected_upstream_commit is not None:
          new_upstream_paths = await asyncio.to_thread(
            _read_upstream_source_paths, git_source_dir,
            app_git.UPSTREAM_BRANCH,
          )
        elif not cloned_update:
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, source_tree, canonical_manifest_url, version,
            exec_paths=exec_paths,
          )
          new_upstream_paths = set(source_tree)
        else:
          new_upstream_paths = await asyncio.to_thread(
            _read_upstream_source_paths, git_source_dir,
            app_git.UPSTREAM_BRANCH,
          )
        dropped_source_paths = previous_upstream_paths - new_upstream_paths
        if not diverged:
          # No local edits → upstream wins outright for the whole tree; it is
          # `source_tree` as fetched for synthetic repos, or the full
          # origin-backed upstream tree for cloned repos. Taking the bytes
          # verbatim keeps the no-edit case off merge_upstream entirely.
          if cloned_update:
            upstream_tree = await asyncio.to_thread(
              app_git.read_ref_tree, git_source_dir, app_git.UPSTREAM_BRANCH,
            )
            source_tree = {
              rel: data for rel, data in upstream_tree.items()
              if rel not in _MERGED_NON_SOURCE
            }
          # A new upstream whose tree lacks the manifest's entry can't
          # fast-forward the served bundle — treat it as a conflict for the
          # agent to resolve, mirroring the clean-merge branch below, rather
          # than half-applying a tree with no entry.
          if prev_upstream_commit:
            reconciliation = await asyncio.to_thread(
              app_git.describe_reconciliation,
              git_source_dir,
              prev_upstream_commit,
              app_git.UPSTREAM_BRANCH,
            )
          if entry_key not in source_tree:
            mode = "conflict"
            conflict_paths = [entry_key]
            reconciliation = app_git.ReconciliationReceipt(
              proven_present=reconciliation.proven_present,
              local_only_paths=reconciliation.local_only_paths,
              new_upstream_paths=reconciliation.new_upstream_paths,
              compatible_paths=reconciliation.compatible_paths,
              unresolved_conflict_paths=(entry_key,),
              provenance_refs_used=reconciliation.provenance_refs_used,
            )
          else:
            divergence = "fast_forward"
            merge_applied = True
            # Only now that we WILL write this tree, read its exec bits so the
            # byte-write loop restores them (a conflict never reaches here, so
            # a degenerate/unreadable tree is never ls-tree'd).
            if cloned_update:
              git_exec_paths = await asyncio.to_thread(
                app_git.read_tree_exec_paths,
                git_source_dir, app_git.UPSTREAM_BRANCH,
              )
        else:
          # Local diverged: fold the new upstream into the local edits with
          # a three-way merge that touches neither `main` nor the working
          # tree, then act on the clean-vs-conflict verdict.
          # A resolver may already have committed this upstream merged with
          # `main` as it was when the update was parked. Installing the same
          # release (the resolver's finish, or the Store's Update button) then
          # combines edits that landed on `main` since with that answer through
          # the same three-way merge; only an overlap goes back to the resolver.
          # Key on the upstream ref just recorded: app.upstream_commit is only
          # refreshed after this for sources imported without a Git origin.
          resolved_commit = await asyncio.to_thread(
            committed_pending_resolution,
            git_source_dir,
            await asyncio.to_thread(
              app_git.head_sha, git_source_dir, app_git.UPSTREAM_BRANCH,
            ),
          )
          if resolved_commit is not None:
            merge = await asyncio.to_thread(
              app_git.merge_refs,
              git_source_dir,
              app_git.LOCAL_BRANCH,
              resolved_commit,
            )
          elif git_merge_base_override is not None:
            merge = await asyncio.to_thread(
              app_git.merge_refs,
              git_source_dir,
              app_git.LOCAL_BRANCH,
              app_git.UPSTREAM_BRANCH,
              merge_base=git_merge_base_override,
            )
            merge.reconciliation = await asyncio.to_thread(
              app_git.describe_reconciliation,
              git_source_dir,
              git_merge_base_override,
              app_git.UPSTREAM_BRANCH,
              local=app_git.LOCAL_BRANCH,
              conflict_paths=merge.conflict_paths,
            )
          else:
            merge = await asyncio.to_thread(
              app_git.merge_upstream,
              git_source_dir,
              allow_unrelated_histories=allow_unrelated_histories,
            )
          reconciliation = merge.reconciliation
          if merge.status == "conflict":
            # Every app, including the App Store itself, keeps local edits on
            # a real conflict; the Store stays served and resolves through the
            # same owner-gated resolver instead of overwriting local work.
            # JSON manifests can reconcile serialization drift and disjoint
            # edits structurally. Other files retain the APP_VERSION-only
            # rule. Remaining overlaps need the owner unless a path outside
            # a complete package can safely keep the owner's version.
            package_paths = await asyncio.to_thread(
              _update_package_paths, git_source_dir, manifest,
            )
            benign = await asyncio.to_thread(
              app_git.resolve_benign_conflict,
              git_source_dir, merge.conflict_paths,
              merge_base=None if resolved_commit else git_merge_base_override,
              package_paths=package_paths,
              incoming=resolved_commit or app_git.UPSTREAM_BRANCH,
            )
            resolved_source = None
            if benign is not None:
              resolved_source = {
                rel: data for rel, data in benign.tree.items()
                if rel not in _MERGED_NON_SOURCE
              }
              if benign.kept_local:
                upstream_tree = await asyncio.to_thread(
                  app_git.read_ref_tree, git_source_dir,
                  resolved_commit or app_git.UPSTREAM_BRANCH,
                )
                if not await _kept_local_bundle_unchanged(
                  resolved_source, manifest, static_assets_fetched,
                  upstream_tree, benign.kept_local,
                ):
                  resolved_source = None
            if resolved_source is not None and entry_key in resolved_source:
              source_tree = resolved_source
              divergence = "clean_merge"
              merge_applied = True
              warnings.append(
                "auto-resolved an update conflict"
              )
              if benign.kept_local:
                warnings.append(
                  "kept local edits to files outside the app package "
                  "(upstream edits dropped): "
                  + ", ".join(benign.kept_local)
                )
              reconciliation = app_git.ReconciliationReceipt(
                proven_present=reconciliation.proven_present,
                local_only_paths=reconciliation.local_only_paths,
                new_upstream_paths=reconciliation.new_upstream_paths,
                compatible_paths=reconciliation.compatible_paths,
                provenance_refs_used=reconciliation.provenance_refs_used,
                kept_local_paths=benign.kept_local,
              )
              # Resolved package files use merge modes; ancillary files keep
              # the owner's local mode as well as their bytes.
              git_exec_paths = await asyncio.to_thread(
                app_git.read_tree_exec_paths,
                git_source_dir, benign.tree_oid,
              )
              if benign.kept_local:
                local_exec_paths = await asyncio.to_thread(
                  app_git.read_tree_exec_paths, git_source_dir, app_git.LOCAL_BRANCH,
                )
                git_exec_paths = (
                  git_exec_paths.difference(benign.kept_local)
                  | local_exec_paths.intersection(benign.kept_local)
                )
            else:
              if resolved_commit is not None:
                raise HTTPException(
                  409,
                  detail={
                    "code": "resolution_behind_local_edits",
                    "message": (
                      "The app was edited while this update was being resolved, "
                      "and those edits overlap the resolution. Finish it in the "
                      "update's resolver chat."
                    ),
                    "conflict_paths": merge.conflict_paths,
                  },
                )
              # Never rebase local. The app stays served with its current
              # bundle + source; the new upstream is recorded for a later
              # agent-resolution pass. Switch to conflict mode below.
              mode = "conflict"
              conflict_paths = merge.conflict_paths
          else:
            # Clean merge: the WHOLE merged tree is what we write + compile.
            # Read it in full (one path for one and many files) and drop the
            # managed/non-source files so `source_tree` is the source set the
            # writer reconciles the worktree to. A clean verdict that yields
            # no entry (e.g. an unreadable tree) is treated as a conflict
            # rather than half-applying a merge we can't materialise.
            merged_tree = app_git.read_merged_tree(
              git_source_dir, merge.merged_tree_oid,
            )
            merged_source = {
              rel: data for rel, data in merged_tree.items()
              if rel not in _MERGED_NON_SOURCE
            }
            if entry_key not in merged_source:
              mode = "conflict"
              conflict_paths = merge.conflict_paths or [entry_key]
            else:
              source_tree = merged_source
              divergence = "clean_merge"
              merge_applied = True
              if merge.equivalent_change_refs:
                warnings.append(
                  "reconciled reviewed changes that were already present "
                  "upstream"
                )
              # Read exec bits only now that we WILL write this tree — a
              # conflict/unreadable verdict never ls-tree's the (possibly
              # degenerate) merged oid.
              git_exec_paths = await asyncio.to_thread(
                app_git.read_tree_exec_paths,
                git_source_dir, merge.merged_tree_oid,
              )
      else:
        # Known Git origins must keep their real history. A failed clone is
        # an install failure, not permission to invent a disconnected lineage.
        # URLs without a derivable origin remain supported: their fetched
        # source is imported into a local Git repository below.
        if repo_ref is not None:
          if staged_clone is None:
            raise HTTPException(
              409, "The app's Git repository must be fetched before install.",
            )
          try:
            journal.rollback_actions.append(await settled.run_settled(
              app_git.adopt_clone, staged_clone, git_source_dir,
            ))
            app.upstream_commit = staged_clone.sha
            entry_bytes = (git_source_dir / "index.jsx").read_bytes()
            upstream_jsx_sha = hashlib.sha256(entry_bytes).hexdigest()
            source_tree = {"index.jsx": entry_bytes}
            cloned_install = True
          except (
            OSError, subprocess.SubprocessError, RuntimeError, ValueError,
          ) as exc:
            raise git_source_error(
              "Nothing was installed.",
              exc,
              code="git_install_unavailable",
              failure="The app's Git repository could not be cloned or "
              "verified.",
              retry_hint="Retry when its source is available.",
            ) from exc
        if not cloned_install:
          # record the pristine source tree on `upstream`, then align the
          # local `main` branch to that commit so the working branch starts
          # exactly at the installed version — a shared base for the next
          # update's merge.
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, source_tree, canonical_manifest_url, version,
            exec_paths=exec_paths,
          )
          await asyncio.to_thread(
            app_git.align_local_to_upstream, git_source_dir,
          )
      app.upstream_jsx_sha = upstream_jsx_sha
      if not cloned_install:
        app.upstream_commit = await asyncio.to_thread(
          app_git.head_sha, git_source_dir, app_git.UPSTREAM_BRANCH,
        )
      if mode == "conflict":
        # Conflict: leave the working tree exactly as it was. The app keeps
        # serving its prior good bundle and Settings/App Store surface the
        # conflict paths. Only the owner's click-gated resolver endpoint
        # materializes the real merge (markers + MERGE_HEAD), and it does so
        # in the app's private resolution checkout, never in this tree.
        # `app.jsx_source` stays the LOCAL source and the upstream provenance
        # (upstream_commit / upstream_jsx_sha, set above) persists for the
        # later resolution.
        if not app.upstream_commit:
          raise RuntimeError("conflicting update has no recorded upstream commit")
        await asyncio.to_thread(
          stage_pending_conflict_update,
          git_source_dir,
          app_id=app.id,
          upstream_commit=app.upstream_commit,
          manifest=manifest,
          raw_base=raw_base,
          capability_digest=fetched_capability_digest,
          candidate_digest=candidate_digest,
          merge_base_override=git_merge_base_override,
        )

      if mode != "conflict" and merge_applied and merge_existing_source:
        # The merged tree is the complete answer, so every tracked path it
        # lacks goes: a file a resolver deleted as well as one upstream removed.
        dropped_source_paths |= await asyncio.to_thread(
          _read_upstream_source_paths, git_source_dir, app_git.LOCAL_BRANCH,
        ) - set(source_tree)

      # The served merge result owns deletions, not the new upstream alone:
      # an upstream deletion may conflict with an owner edit we retained.
      dropped_source_paths -= set(source_tree)

      # The disk-write phase runs INSIDE the same held lock for the Git path so
      # no source commit interleaves between the merge decision and the write; a
      # conflict skips it (the source stays the local edits, served by the prior
      # bundle).
      if mode != "conflict":
        equivalence_target_to_retire = await _activate_install_source(
          db,
          app=app,
          manifest=manifest,
          plan=ActivationPlan(
            source_tree=source_tree,
            published_source_tree=published_source_tree,
            static_assets=static_assets_fetched,
            dropped_source_paths=dropped_source_paths,
            exec_paths=exec_paths,
            git_exec_paths=git_exec_paths,
            entry_key=entry_key,
            job_name=job_name,
            cloned_install=cloned_install,
            cloned_update=cloned_update,
            merge_applied=merge_applied,
            updating=existing is not None,
            canonical_manifest_url=canonical_manifest_url,
            capability_contract=capability_contract,
            package_id=target.package_id,
            source_identity=target.source_identity,
            python_check=python_check,
          ),
          journal=journal,
          data_dir=data_dir,
        )
      else:
        # A conflict activates nothing, so its build is never linked.
        journal.commit_actions.append(lambda: app_python_env.discard_env(python_env))
    except Exception:
      # Undo this pass's source writes and upstream ref while the lock still
      # excludes other writers, so a commit cannot land in between and then be
      # overwritten. This includes the first pass of a Python check. The outer
      # handlers still shape the response; their rollback then has nothing left.
      db.rollback()
      journal.rollback_materialization()
      raise
    finally:
      # Release the per-source-dir lock (held across the merge + write for the
      # git path) BEFORE the seeds block takes app_storage_lock, preserving the
      # documented acquisition order.
      source_lock.release()

    if mode == "conflict":
      if publication_handoff_app_id is not None:
        # The owner explicitly approved this separately verified publication
        # handoff. Connect the original row even when later local edits need the
        # ordinary source resolver: the current source/bundle stay untouched,
        # while the reviewed identity and capability grant become durable.
        _apply_manifest_metadata(
          db,
          app,
          manifest=manifest,
          canonical_manifest_url=canonical_manifest_url,
          capability_contract=capability_contract,
          package_id=target.package_id,
          source_identity=target.source_identity,
          entry_source=None,
        )
      # Commit the recorded upstream provenance + return so the App Store can
      # surface a click-gated resolver. The served source/bundle stay the prior
      # good ones until the owner chooses Resolve in chat.
      _commit_prepared_app(
        db, app, journal, revive_after_commit=revive_after_commit,
      )
      db.refresh(app)
      activity.log_event(
        "app_install", app_id=app.id, slug=app.slug, source=source,
      )
      return InstallResult(
        app=app,
        mode=mode,
        warnings=warnings,
        manifest=manifest,
        conflict_paths=conflict_paths,
        divergence=divergence,
        reconciliation=reconciliation,
      )

    # Storage seeds — fresh installs always seed; updates only fill in keys
    # that don't exist yet so user data isn't clobbered. Under the per-app lock
    # (the install endpoint already holds the lifecycle lock, so this is the
    # documented lifecycle -> app order) so a REINSTALL's exists-check + write
    # can't race a concurrent storage PUT to the same key, and written
    # atomically so a reader never observes a torn seed (Codex review round-8
    # #2). Bootstrap installs hold no lifecycle lock but run before serving, so
    # taking app_storage_lock alone here is contention-free.
    async with fs_locks.app_storage_lock(app.id):
      for sub, content in seeds_fetched.items():
        target = _storage_path(app.id, sub)
        if mode == "update" and target.exists():
          continue
        atomic_write(target, content)
        journal.created_paths.append(target)

    # A successfully resolved manifest declaration owns the package icon.
    # Omission clears it; a warned fetch/decode failure preserves the last
    # accepted package icon rather than turning a partial network failure into
    # destructive state. An explicit owner override is stored separately.
    if icon_warning is None:
      app.icon_png = icon_processed

    # COMMIT FIRST — once the DB row is durable, cron registration
    # is a non-fatal "best effort" step. Doing cron BEFORE commit
    # could leave a crontab entry firing for a row that rolled back
    # (orphaned cron, mysterious 'app not found' errors at runtime).
    _commit_prepared_app(
      db, app, journal, revive_after_commit=revive_after_commit,
    )
    # The transaction is now irreversible. Never let a later refresh, activity
    # write, or cleanup error run the failure actions and remove files selected
    # by the durable row. Superseded artifacts can safely remain for the startup
    # orphan reaper if post-commit cleanup is interrupted.
    db.refresh(app)

    # Only the durable update may retire provenance.  Doing this before the DB
    # commit would lose the witness if a later storage/row failure rolled the
    # install back.  Ref deletion is best-effort post-commit housekeeping: the
    # replay already parents `main` directly on this upstream target, so keeping
    # an obsolete ref is harmless while deleting it bounds metadata growth.
    if equivalence_target_to_retire:
      try:
        async with fs_locks.source_dir_lock(str(app.source_dir)):
          await settled.run_settled(
            app_git.retire_landed_equivalent_changes,
            app.source_dir, equivalence_target_to_retire,
          )
      except Exception:
        log.warning(
          "install: could not retire integrated contribution provenance",
          exc_info=True,
        )

    # app_install: log only after the row is durable so the timestamp
    # in the activity log reflects when the install actually landed,
    # not when we entered the install pipeline. mode="update" still
    # emits an event — re-installs of the same manifest_url are
    # meaningful platform signals for the reflection agent.
    activity.log_event(
      "app_install",
      app_id=app.id,
      slug=app.slug,
      source=source,
      **({"kept_local_paths": list(reconciliation.kept_local_paths)}
         if reconciliation.kept_local_paths else {}),
    )

    # Success: drop any .bak snapshots we made — the new bundle is
    # now the canonical one.
    journal.cleanup_superseded()

  except CompileError as exc:
    app_name = str(
      manifest.get("name") or getattr(locals().get("app"), "slug", "app")
    )
    db.rollback()
    journal.rollback_materialization()
    raise HTTPException(422, _compile_error_detail(app_name, exc))
  except RolldownStalled as exc:
    db.rollback()
    journal.rollback_materialization()
    raise HTTPException(503, exc.detail()) from exc
  except app_git.HistoryIncomplete as exc:
    db.rollback()
    journal.rollback_materialization()
    raise git_source_error(
      "The installed version was left unchanged.", exc,
    ) from exc
  except (HTTPException, _UncheckedPythonTree):
    db.rollback()
    journal.rollback_materialization()
    raise
  except Exception:
    # Catch-all so a stray bug doesn't leak partial state or raw exception
    # reprs. The traceback is logged server-side for operators.
    log.exception("install: unexpected failure during materialize")
    db.rollback()
    journal.rollback_materialization()
    raise HTTPException(
      500, "Install failed due to an unexpected server error.",
    )

  from app import service_preload
  from app.applied_app_runtime import prune_runtime
  # A preloaded service host pins the runtime it imported.
  service_preload.retire(app.id, keep_revision=app.runtime_revision)
  try:
    await asyncio.to_thread(prune_runtime, app)
  except OSError:
    log.warning("Could not prune older applied runtime trees", exc_info=True)

  # Phase 4: best-effort effects after the durable boundary.
  await _run_post_commit_effects(
    db,
    app=app,
    mode=mode,
    candidate=candidate,
    warnings=warnings,
    was_usable=was_usable,
  )
  # Only now is the update fully converged. Until the receipt goes, a retry
  # reruns this whole install, post-commit effects included.
  try:
    async with fs_locks.source_dir_lock(app.source_dir):
      await clear_pending_conflict_update(db, app)
  except (OSError, subprocess.SubprocessError, SQLAlchemyError):
    db.rollback()
    log.warning("install: could not clear the finished pending update", exc_info=True)

  return InstallResult(
    app=app,
    mode=mode,
    warnings=warnings,
    manifest=manifest,
    conflict_paths=conflict_paths,
    divergence=divergence,
    reconciliation=reconciliation,
  )


def _cleanup(paths: list[Path]) -> None:
  """Removes anything we created during a failed install. Best-effort —
  swallows OSErrors because we're already on the failure path; the
  goal is to leave less mess, not to error-amplify."""
  for p in reversed(paths):
    try:
      if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
      elif p.exists():
        p.unlink()
    except OSError as exc:
      log.warning("install cleanup: %s — %s", p, exc)


def _run_rollback_actions(actions: list[Callable[[], None]]) -> None:
  """Runs the rollback callables in reverse order. Best-effort like
  `_cleanup`: a failure inside one rollback step shouldn't mask the
  underlying install failure, but we log loudly so the operator can
  fix the leftover state."""
  for action in reversed(actions):
    try:
      action()
    except (OSError, subprocess.SubprocessError) as exc:
      log.warning("install rollback: %s", exc)
