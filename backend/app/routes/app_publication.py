"""Artifact persistence and public-site ownership for installed apps."""

import asyncio
import html
import logging
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import fs_locks, storage_io
from app.artifact_data import (
  ArtifactDataError,
  MAX_ARTIFACT_KEYS,
  MAX_ARTIFACT_TOTAL_BYTES,
  MAX_ARTIFACT_VALUE_BYTES,
  artifact_dir_path,
  artifact_file_path,
  artifact_usage,
  canonical_json,
  list_artifact_keys,
  parse_json,
  read_json_file,
  validate_artifact_id,
  validate_artifact_key,
)
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.publication import (
  InvalidPublicationRegistry,
  PublicationRecord,
  PublicationReservationConflict,
  _PUBLISH_PROJECT_RE,
  _TOKEN_RE,
  atomic_promote_directory,
  create_publication_record,
  new_publication_record,
  published_root,
  read_publication_record,
  registry_path,
  registry_root,
  replace_publication_record,
)
from app.resource_access import live_app_or_404
from app.routes.storage import _recheck_app_identity
from app.storage_io import (
  app_dir_usage, atomic_write, read_capped_body,
  rmtree_strict as _rmtree_strict,
)


router = APIRouter()
log = logging.getLogger("mobius.apps.publication")


def _read_publish_token_hint(token_file: Path) -> str | None:
  """Read the app-writable token hint without following its symlink."""
  if token_file.is_symlink():
    return None
  try:
    info = token_file.stat()
  except OSError:
    return None
  if not info.st_size or info.st_size > 128 or not token_file.is_file():
    return None
  flags = os.O_RDONLY
  if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
  try:
    fd = os.open(token_file, flags)
  except OSError:
    return None
  try:
    raw = os.read(fd, 129)
  finally:
    os.close(fd)
  try:
    token = raw.decode("utf-8").strip()
  except UnicodeDecodeError:
    return None
  return token if _TOKEN_RE.fullmatch(token) else None


def _registry_records_for_app(settings, app_id: int) -> list[PublicationRecord]:
  root = registry_root(settings)
  if root.is_symlink() or not root.is_dir():
    return []
  records = []
  for path in root.glob("*.json"):
    token = path.stem
    if not _TOKEN_RE.fullmatch(token):
      continue
    try:
      record = read_publication_record(settings, token)
    except InvalidPublicationRegistry as exc:
      log.warning("publish registry %s is invalid: %s", token, exc)
      continue
    if record is not None and record.app_id == app_id:
      records.append(record)
  return records


def _legacy_project_hint(storage: Path, token_file: Path) -> str | None:
  try:
    rel = token_file.relative_to(storage)
  except ValueError:
    return None
  if rel.parts == ("build", "publish-token.txt"):
    return None
  if (
    len(rel.parts) == 4
    and rel.parts[0] == "projects"
    and rel.parts[2:] == ("build", "publish-token.txt")
    and _PUBLISH_PROJECT_RE.fullmatch(rel.parts[1])
  ):
    return rel.parts[1]
  return None


async def _revoke_publish_token(
  settings,
  app_id: int,
  app_gen: str | None,
  token: str,
  project_id: str | None,
) -> bool:
  """Permanently revoke one owned token before physical cleanup.

  Returns whether the token is now durably un-servable — either because the
  revocation was written or because this app never owned it. False means a
  reservation the caller asked about is STILL ACTIVE, so a caller that reports
  success to the owner must not ignore it: the page would stay public.
  """
  if not _TOKEN_RE.fullmatch(token or ""):
    return True
  try:
    record = read_publication_record(settings, token)
  except InvalidPublicationRegistry as exc:
    # A corrupt reservation already fails closed.  Do not let an app-writable
    # hint authorize deleting the unknown reservation's snapshot.
    log.warning("cannot revoke invalid publication %s: %s", token, exc)
    return False
  if record is None:
    # No reservation exists, so nothing here proves this app owns the token.
    # The only thing naming it is publish-token.txt, which lives in app-
    # writable storage — any app can plant another app's token there and would
    # otherwise get that app's public snapshot deleted. The registry is the
    # sole ownership authority: a hint may POINT AT a record, never create one.
    # Pre-registry snapshots are therefore inert rather than hint-revocable;
    # removing one is an explicit owner action, not an app-triggered side
    # effect.
    log.warning(
      "ignoring unregistered publish-token hint %s while revoking app %s",
      token, app_id,
    )
    return True
  if record.app_id != app_id:
    log.warning(
      "ignoring publish-token hint %s owned by app %s while revoking app %s",
      token, record.app_id, app_id,
    )
    return True
  # A hint names a token but does not prove which PROJECT it belongs to, and
  # hints live in app-writable storage. Checking app_id alone would let one
  # project's stray hint permanently revoke and delete a sibling project's
  # publication inside the same app, so honor the hint only when the registry
  # agrees on the whole binding. `project_id=None` means the caller is tearing
  # the app down wholesale and every project of the live generation goes.
  if project_id is not None and record.project_id != project_id:
    log.warning(
      "ignoring publish-token hint %s for project %s while revoking project %s",
      token, record.project_id, project_id,
    )
    return True
  try:
    if record.state != "revoked":
      record = replace_publication_record(settings, record, "revoked")
  except (OSError, InvalidPublicationRegistry,
          PublicationReservationConflict) as exc:
    log.error("failed to persist revocation for token %s: %s", token, exc)
    return False

  # The durable revoked state is written before either best-effort rmtree.
  for root_name in ("published", "published-data"):
    root = Path(settings.data_dir) / root_name
    target = root / token
    if root.is_symlink() or target.is_symlink():
      log.error("refusing symlink publication cleanup: %s", target)
      continue
    if not target.exists():
      continue
    try:
      await asyncio.to_thread(shutil.rmtree, target)
    except OSError as exc:
      log.warning("revoked token %s cleanup failed for %s: %s",
                  token, target, exc)
  # The snapshot rmtree above is best-effort cleanup; the durable `revoked`
  # record already makes the token un-servable, so reaching here is success.
  return True


async def _revoke_app_publish_tokens(
  settings,
  app_id: int,
  app_gen: str | None,
) -> None:
  """Revoke registry-owned and legacy tokens while app storage still exists.

  The caller holds ``app_storage_lock(app_id)``.  Each token is independent so
  one corrupt record or failed rmtree cannot prevent revoking the rest.
  """
  tokens: dict[str, str | None] = {
    record.token: record.project_id
    for record in _registry_records_for_app(settings, app_id)
  }
  storage = Path(settings.data_dir) / "apps" / str(app_id)
  if not storage.is_symlink() and storage.is_dir():
    try:
      token_files = list(storage.rglob("build/publish-token.txt"))
    except OSError as exc:
      log.warning("legacy publish-token scan failed for app %s: %s", app_id, exc)
      token_files = []
    for token_file in token_files:
      token = _read_publish_token_hint(token_file)
      if token is not None:
        tokens.setdefault(token, _legacy_project_hint(storage, token_file))
  for token, project_id in tokens.items():
    try:
      await _revoke_publish_token(
        settings, app_id, app_gen, token, project_id,
      )
    except Exception as exc:  # best-effort batch boundary
      log.exception("unexpected publication revoke failure for %s: %s",
                    token, exc)


@router.api_route(
  "/{app_id}/artifact-data/{artifact_id}",
  methods=["GET"],
  dependencies=[Depends(reject_cross_site)],
)
async def artifact_data_keys(
  app_id: int,
  artifact_id: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """List an artifact's stored keys, derived from the directory.

  The keys are enumerated server-side precisely so no client has to maintain an
  index file: two tabs updating one would race and silently drop a key. The
  directory cannot disagree with itself.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only access its own artifact data.")
  if not validate_artifact_id(artifact_id):
    raise HTTPException(400, "Invalid artifact_id.")
  app = live_app_or_404(db, app_id)
  expected_nonce = app.token_nonce
  settings = get_settings()
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    try:
      keys = list_artifact_keys(
        artifact_dir_path(settings, app_id, artifact_id),
      )
    except ArtifactDataError as exc:
      raise HTTPException(400, str(exc)) from exc
  return {"keys": keys}


@router.api_route(
  "/{app_id}/artifact-data/{artifact_id}/{key}",
  methods=["GET", "PUT", "DELETE"],
  dependencies=[Depends(reject_cross_site)],
)
async def artifact_data_value(
  app_id: int,
  artifact_id: str,
  key: str,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Read or mutate one server-validated, quota-bound artifact JSON key."""
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only access its own artifact data.")
  if not validate_artifact_id(artifact_id) or not validate_artifact_key(key):
    raise HTTPException(400, "Invalid artifact_id or key.")
  app = live_app_or_404(db, app_id)
  expected_nonce = app.token_nonce
  value_bytes = None
  if request.method == "PUT":
    raw = await read_capped_body(request, cap=MAX_ARTIFACT_VALUE_BYTES)
    try:
      value_bytes = canonical_json(parse_json(raw))
    except ArtifactDataError as exc:
      raise HTTPException(400, str(exc)) from exc
    if len(value_bytes) > MAX_ARTIFACT_VALUE_BYTES:
      raise HTTPException(413, "Artifact value exceeds 64 KB.")

  settings = get_settings()
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    try:
      artifact_root, file_path = artifact_file_path(
        settings, app_id, artifact_id, key,
      )
    except ArtifactDataError as exc:
      raise HTTPException(400, str(exc)) from exc

    if request.method == "GET":
      try:
        return read_json_file(file_path)
      except ArtifactDataError as exc:
        raise HTTPException(404, "Artifact value not found.") from exc

    if request.method == "DELETE":
      if file_path.is_symlink() or not file_path.is_file():
        raise HTTPException(404, "Artifact value not found.")
      file_path.unlink()
      return Response(status_code=204)

    try:
      total, key_count = artifact_usage(artifact_root)
    except ArtifactDataError as exc:
      raise HTTPException(400, str(exc)) from exc
    try:
      old_size = file_path.stat().st_size if file_path.is_file() else 0
    except OSError:
      old_size = 0
    is_new_key = not file_path.is_file()
    if is_new_key and key_count >= MAX_ARTIFACT_KEYS:
      raise HTTPException(400, "Artifact data is limited to 100 keys.")
    projected = total - old_size + len(value_bytes)
    if projected > MAX_ARTIFACT_TOTAL_BYTES:
      raise HTTPException(413, "Artifact data exceeds the 1 MB quota.")
    # The per-artifact caps above bound ONE namespace, and artifact_id is
    # caller-chosen — inventing namespaces would otherwise multiply them
    # without limit. The per-app backstop every other storage write already
    # honors is what actually bounds the tree, so charge this write against it
    # too. Read the cap from the module so a test can shrink it.
    app_dir = Path(settings.data_dir) / "apps" / str(app_id)
    app_projected = app_dir_usage(app_dir) - old_size + len(value_bytes)
    if app_projected > storage_io.MAX_APP_STORAGE_BYTES:
      raise HTTPException(
        413,
        "App storage quota exceeded — this write would bring the app to "
        f"{app_projected} bytes, over the "
        f"{storage_io.MAX_APP_STORAGE_BYTES}-byte per-app limit.",
      )
    atomic_write(file_path, value_bytes)
  return Response(status_code=204)


class LinkPreviewRequest(BaseModel):
  title: str = Field(min_length=1, max_length=200)
  description: str | None = Field(default=None, max_length=500)
  image_path: str | None = Field(default=None, max_length=240)
  image_alt: str | None = Field(default=None, max_length=300)
  image_width: int | None = Field(default=None, ge=1, le=10000)
  image_height: int | None = Field(default=None, ge=1, le=10000)
  site_name: str | None = Field(default=None, max_length=100)


class PublishRequest(BaseModel):
  project_id: str | None = None
  link_preview: LinkPreviewRequest | None = None


def _publication_url(settings, token: str) -> str:
  origin = settings.frontend_origin.rstrip("/")
  return f"{origin}/sites/{token}/"


def _validated_preview_image(
  site_dir: Path,
  image_path: str | None,
) -> tuple[str, str] | None:
  if image_path is None:
    return None
  raw = image_path.strip()
  rel = PurePosixPath(raw)
  if (
    not raw
    or rel.is_absolute()
    or any(part in ("", ".", "..") for part in rel.parts)
    or "\\" in raw
  ):
    raise HTTPException(422, "invalid link preview image_path")
  target = site_dir.joinpath(*rel.parts)
  if not target.is_file() or target.is_symlink():
    raise HTTPException(400, "Link preview image is missing from the site.")
  content_type, _encoding = mimetypes.guess_type(raw)
  if content_type not in {
    "image/gif", "image/jpeg", "image/png", "image/webp",
  }:
    raise HTTPException(422, "unsupported link preview image type")
  return quote(raw, safe="/"), content_type


def _link_preview_markup(
  settings,
  token: str,
  preview: LinkPreviewRequest,
  site_dir: Path,
) -> bytes:
  title = preview.title.strip()
  if not title:
    raise HTTPException(422, "link preview title cannot be blank")
  canonical = _publication_url(settings, token)
  image = _validated_preview_image(site_dir, preview.image_path)
  tags = [
    '<meta property="og:type" content="website">',
    f'<meta property="og:title" content="{html.escape(title, quote=True)}">',
    f'<meta property="og:url" content="{html.escape(canonical, quote=True)}">',
    f'<link rel="canonical" href="{html.escape(canonical, quote=True)}">',
  ]
  description = (preview.description or "").strip()
  if description:
    escaped = html.escape(description, quote=True)
    tags.append(f'<meta property="og:description" content="{escaped}">')
  site_name = (preview.site_name or "").strip()
  if site_name:
    tags.append(
      f'<meta property="og:site_name" '
      f'content="{html.escape(site_name, quote=True)}">',
    )
  if image:
    image_path, content_type = image
    image_url = f"{canonical}{image_path}"
    escaped_url = html.escape(image_url, quote=True)
    tags.extend((
      f'<meta property="og:image" content="{escaped_url}">',
      f'<meta property="og:image:type" content="{content_type}">',
    ))
    if preview.image_width is not None:
      tags.append(
        f'<meta property="og:image:width" content="{preview.image_width}">',
      )
    if preview.image_height is not None:
      tags.append(
        f'<meta property="og:image:height" content="{preview.image_height}">',
      )
    image_alt = (preview.image_alt or "").strip()
    if image_alt:
      escaped_alt = html.escape(image_alt, quote=True)
      tags.append(f'<meta property="og:image:alt" content="{escaped_alt}">')
    tags.extend((
      '<meta name="twitter:card" content="summary_large_image">',
      f'<meta name="twitter:title" '
      f'content="{html.escape(title, quote=True)}">',
    ))
    if description:
      tags.append(
        f'<meta name="twitter:description" '
        f'content="{html.escape(description, quote=True)}">',
      )
    tags.append(f'<meta name="twitter:image" content="{escaped_url}">')
    if image_alt:
      tags.append(
        f'<meta name="twitter:image:alt" content="{escaped_alt}">',
      )
  block = (
    "\n<!-- mobius:link-preview:start -->\n"
    + "\n".join(tags)
    + "\n<!-- mobius:link-preview:end -->"
  )
  return block.encode("utf-8")


def _inject_link_preview(
  settings,
  token: str,
  preview: LinkPreviewRequest,
  site_dir: Path,
) -> None:
  index = site_dir / "index.html"
  if not index.is_file() or index.is_symlink():
    raise HTTPException(400, "Link previews require a site index.html.")
  source = index.read_bytes()
  head = re.search(br"<head(?:\s[^>]*)?>", source, flags=re.IGNORECASE)
  if head is None:
    raise HTTPException(400, "Link previews require an HTML head.")
  markup = _link_preview_markup(settings, token, preview, site_dir)
  atomic_write(index, source[:head.end()] + markup + source[head.end():])


def _publish_paths(settings, app, project_id: str | None):
  storage = Path(settings.data_dir) / "apps" / str(app.id)
  base = storage / "projects" / project_id if project_id else storage
  return base / "build" / "site", base / "build" / "publish-token.txt"


def _validate_publish_paths(settings, app, project_id: str | None) -> None:
  storage = Path(settings.data_dir) / "apps" / str(app.id)
  site_dir, token_file = _publish_paths(settings, app, project_id)
  components = [storage]
  if project_id is not None:
    components.extend((storage / "projects", storage / "projects" / project_id))
  components.extend((site_dir.parent, site_dir, token_file))
  if any(path.is_symlink() for path in components):
    raise HTTPException(400, "Symlinks are not allowed in publish paths.")
  storage_resolved = storage.resolve()
  site_resolved = site_dir.resolve()
  if storage_resolved not in site_resolved.parents:
    raise HTTPException(400, "Publish path escaped app storage.")


def _mint_publish_record(settings, app, project_id: str | None):
  while True:
    token = uuid.uuid4().hex
    if os.path.lexists(registry_path(settings, token)):
      continue
    if os.path.lexists(published_root(settings) / token):
      continue
    record = new_publication_record(
      token, app.id, app.token_nonce, project_id, state="staged",
    )
    try:
      create_publication_record(settings, record)
    except PublicationReservationConflict:
      continue
    return token, record


@router.post("/{app_id}/publish", dependencies=[Depends(reject_cross_site)])
async def publish_app_site(
  app_id: int,
  body: PublishRequest,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Publish a project's built static site to a stable token URL.

  Snapshots <storage>/[projects/<pid>/]build/site/ to
  <data_dir>/published/<token>/ and returns /sites/<token>/. The token is
  stable per project (kept in the project's build/ dir) so re-publishing
  updates the SAME URL. Owner or the app's own token only.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only publish its own site.")
  project_id = (body.project_id or "").strip() or None
  if project_id is not None and not _PUBLISH_PROJECT_RE.match(project_id):
    raise HTTPException(422, "invalid project_id")
  app = live_app_or_404(db, app_id)
  expected_nonce = app.token_nonce
  settings = get_settings()
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    _validate_publish_paths(settings, app, project_id)
    site_dir, token_file = _publish_paths(settings, app, project_id)
    try:
      site_ready = site_dir.is_dir() and any(site_dir.iterdir())
    except OSError:
      site_ready = False
    if not site_ready:
      raise HTTPException(
        400, "No built site to publish — build the project first.",
      )

    token = _read_publish_token_hint(token_file)
    record = None
    if token is not None:
      try:
        hinted = read_publication_record(settings, token)
      except InvalidPublicationRegistry:
        hinted = False
      if (
        isinstance(hinted, PublicationRecord)
        and hinted.binding() == (app.id, app.token_nonce, project_id)
        and hinted.state == "active"
      ):
        # Re-publishing an app's OWN registered token keeps its URL stable.
        record = hinted
      # An unregistered token is deliberately NOT adopted. publish-token.txt
      # sits in app-writable storage, so adopting a token merely because a
      # hint names it and published/<token>/ happens to exist would let any
      # app claim another app's already-shared public URL and overwrite its
      # content. The registry is the sole ownership authority, so an
      # unrecognized hint falls through to minting a fresh token below; the
      # pre-registry snapshot keeps serving its old content untouched.
    republishing = record is not None
    if record is None:
      token, record = _mint_publish_record(settings, app, project_id)

    root = published_root(settings)
    staging_root = root / ".staging"
    if root.is_symlink() or staging_root.is_symlink():
      raise HTTPException(400, "Invalid published staging directory.")
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = staging_root / uuid.uuid4().hex
    destination = root / token
    had_destination = destination.exists()

    def _snapshot():
      if any(path.is_symlink() for path in site_dir.rglob("*")):
        raise HTTPException(
          400, "Built site contains symlinks; refusing to publish.",
        )
      shutil.copytree(site_dir, stage, symlinks=True)
      if body.link_preview is not None:
        _inject_link_preview(
          settings, token, body.link_preview, stage,
        )

    promoted = False
    preserve_stage = False
    try:
      await asyncio.to_thread(_snapshot)
      # Keep the exchange itself on this task so cancellation cannot leave the
      # thread committing a swap after ``promoted`` incorrectly stayed false.
      atomic_promote_directory(stage, destination)
      promoted = True
      atomic_write(token_file, token)
      record = replace_publication_record(settings, record, "active")
    except BaseException as publish_exc:
      if record.state == "staged":
        # A first publish has no prior public generation. Revoke its new
        # reservation and remove the promoted candidate before surfacing the
        # failure.
        try:
          replace_publication_record(settings, record, "revoked")
        except (OSError, InvalidPublicationRegistry,
                PublicationReservationConflict):
          pass
      if promoted and record.state == "staged":
        try:
          await asyncio.to_thread(shutil.rmtree, destination)
        except OSError:
          pass
      elif promoted and republishing:
        try:
          if had_destination:
            # RENAME_EXCHANGE left the prior complete generation in ``stage``.
            # Exchange it back before the request reports the metadata failure.
            atomic_promote_directory(stage, destination)
          else:
            # An active record with a missing snapshot was already a 404. Put
            # that prior state back rather than making failed content public.
            _rmtree_strict(destination)
        except OSError as rollback_exc:
          # The stage is now the only known copy of the previous generation.
          # Never let the finally block destroy the owner's recovery copy.
          preserve_stage = True
          log.error(
            "republish rollback failed for token %s; prior generation kept "
            "at %s: %s",
            token, stage, rollback_exc,
          )
          raise rollback_exc from publish_exc
      raise
    finally:
      if not preserve_stage and stage.exists() and not stage.is_symlink():
        try:
          await asyncio.to_thread(shutil.rmtree, stage)
        except OSError:
          pass
  return {
    "token": token,
    "url": f"/sites/{token}/",
    "public_url": _publication_url(settings, token),
  }


@router.delete("/{app_id}/publish", dependencies=[Depends(reject_cross_site)])
async def unpublish_app_site(
  app_id: int,
  project_id: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Revoke a published URL permanently, then remove its snapshot and hint."""
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only unpublish its own site.")
  if project_id and not _PUBLISH_PROJECT_RE.match(project_id):
    raise HTTPException(422, "invalid project_id")
  app = live_app_or_404(db, app_id)
  expected_nonce = app.token_nonce
  project_id = project_id or None
  settings = get_settings()
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    # The registry lives OUTSIDE app-writable storage and is the authority for
    # what is public, so enumerate + revoke registry-owned tokens first and let
    # nothing app-controlled gate it. The legacy publish-token.txt hint lives in
    # app storage, where an app job could leave build/site or the hint itself a
    # symlink; validating those paths must not be able to keep a registered URL
    # alive while unpublish 400s.
    records = [
      record for record in _registry_records_for_app(settings, app_id)
      if record.app_gen == app.token_nonce and record.project_id == project_id
    ]
    registry_tokens = {record.token for record in records}
    revoked = True
    # Revoke registry-owned tokens FIRST and let nothing app-controlled run
    # before this loop — not even reading the hint. A filesystem error from the
    # app-writable publish paths (symlink loop, EIO) must never abort unpublish
    # before the registered URL is dead.
    for token in registry_tokens:
      if not await _revoke_publish_token(
        settings, app_id, app.token_nonce, token, project_id,
      ):
        revoked = False
    # The legacy publish-token.txt hint lives in app storage; read it only if
    # its paths are sane, and treat ANY error (HTTP validation or raw OS) as
    # "no legacy hint" rather than letting it block the revoke above.
    token_file = None
    hint = None
    try:
      _validate_publish_paths(settings, app, project_id)
      _site, token_file = _publish_paths(settings, app, project_id)
      hint = _read_publish_token_hint(token_file)
    except (HTTPException, OSError) as exc:
      log.warning(
        "app %s unpublish: skipping legacy hint, publish paths unusable: %s",
        app_id, exc,
      )
      token_file = None
      hint = None
    if hint is not None and hint not in registry_tokens:
      if not await _revoke_publish_token(
        settings, app_id, app.token_nonce, hint, project_id,
      ):
        revoked = False
    # Only drop the hint once the URL is really dead. Deleting it after a
    # failed revocation would remove the last pointer to a page that is still
    # public, and answering {"ok": true} would tell the owner their artifact
    # was unshared while anyone holding the link could still read it.
    if not revoked:
      raise HTTPException(
        500,
        "Could not revoke the public URL — it is still live. "
        "Check storage health and try again.",
      )
    if token_file is not None and not token_file.is_symlink():
      try:
        token_file.unlink()
      except OSError:
        pass
  return {"ok": True}
