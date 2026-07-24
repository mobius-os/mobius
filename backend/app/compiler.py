"""Compiles JSX source strings to ES modules using esbuild."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from app import timeutil
from app.app_compile_contract import (
  COMPILED_RUNTIME_BANNER,
  ESBUILD_TIMEOUT_SECS,
  esbuild_command,
  esbuild_environment,
  esbuild_metafile_contract_error,
)
from app.config import get_settings


class CompileError(RuntimeError):
  """A user-source compile failure with stderr for client-safe formatting."""

  def __init__(
    self,
    message: str,
    *,
    stderr: str = "",
    source_path: str | Path | None = None,
  ) -> None:
    super().__init__(message)
    self.stderr = stderr
    self.source_path = Path(source_path) if source_path is not None else None


_CONTENT_BUNDLE_RE = re.compile(r"^app-(?P<app_id>[0-9]+)-(?P<digest>[0-9a-f]{64})\.js$")
_LEGACY_BUNDLE_RE = re.compile(r"^app-(?P<app_id>[0-9]+)\.js$")


def _compiled_dir() -> Path:
  """Returns (and creates) the directory for compiled mini-app modules."""
  path = Path(get_settings().data_dir) / "compiled"
  path.mkdir(parents=True, exist_ok=True)
  return path


def _file_sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def _sync_published_bundle(path: Path) -> None:
  """Make a published artifact and its directory entry durable.

  ``os.replace`` gives readers an atomic name switch, but it does not by itself
  guarantee that the file or directory metadata survives a sudden power loss.
  The database must not commit ``compiled_path`` until both are on stable
  storage, otherwise a durable row could still outlive its freshly renamed
  bundle.
  """
  file_fd = os.open(path, os.O_RDONLY)
  try:
    os.fsync(file_fd)
  finally:
    os.close(file_fd)
  dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
  try:
    os.fsync(dir_fd)
  finally:
    os.close(dir_fd)


def owned_bundle_path(app_id: int, path: str | Path | None) -> Path | None:
  """Return a validated bundle path owned by ``app_id``.

  ``compiled_path`` is durable state, so cleanup must not trust it as an
  arbitrary unlink target. Both the legacy fixed name and the new immutable
  content-addressed names are accepted, but only directly under /compiled.
  """
  if not path:
    return None
  candidate = Path(path)
  try:
    if candidate.parent.resolve() != _compiled_dir().resolve():
      return None
  except OSError:
    return None
  canonical = _compiled_dir() / candidate.name
  legacy_match = _LEGACY_BUNDLE_RE.fullmatch(candidate.name)
  if legacy_match and int(legacy_match.group("app_id")) == app_id:
    return canonical
  match = _CONTENT_BUNDLE_RE.fullmatch(candidate.name)
  if match and int(match.group("app_id")) == app_id:
    return canonical
  return None


def publish_staged_bundle(app_id: int, staged: str | Path) -> Path:
  """Promote a compiled staging file to an immutable content path.

  Publication happens *before* the database row switches ``compiled_path``.
  The old row therefore keeps serving its old immutable file until commit,
  while a committed row can only point at a file that already exists. A crash
  on either side of commit is coherent; an uncommitted content file is merely
  an orphan for the startup reaper.
  """
  staged_path = Path(staged)
  compiled = _compiled_dir()
  try:
    valid_staging_parent = staged_path.parent.resolve() == compiled.resolve()
  except OSError as exc:
    raise ValueError("Invalid compiled-bundle staging path.") from exc
  if (
    not valid_staging_parent
    or staged_path.name != f"app-{app_id}.js.staging"
  ):
    raise ValueError("Compiled-bundle staging path does not match the app id.")
  staged_path = compiled / staged_path.name
  digest = _file_sha256(staged_path)
  final = compiled / f"app-{app_id}-{digest}.js"
  if final.exists():
    # Reuse an already-published identical artifact (for example after a crash
    # before commit). If the digest-named file was corrupted, atomically repair
    # it from the freshly compiled staging bytes.
    try:
      identical = _file_sha256(final) == digest
    except OSError:
      identical = False
    if identical:
      staged_path.unlink(missing_ok=True)
      _sync_published_bundle(final)
      return final
  os.replace(staged_path, final)
  _sync_published_bundle(final)
  return final


def unlink_app_bundle(app_id: int, path: str | Path | None) -> bool:
  """Best-effort unlink of one validated bundle owned by ``app_id``."""
  owned = owned_bundle_path(app_id, path)
  if owned is None:
    return False
  try:
    owned.unlink(missing_ok=True)
    return True
  except OSError:
    return False


def purge_app_bundles(app_id: int) -> None:
  """Best-effort removal of every legacy/content bundle for a hard-deleted app."""
  compiled = _compiled_dir()
  candidates = [
    compiled / f"app-{app_id}.js",
    compiled / f"app-{app_id}.js.staging",
    *compiled.glob(f"app-{app_id}-*.js"),
  ]
  for candidate in candidates:
    if candidate.name.endswith(".staging"):
      try:
        candidate.unlink(missing_ok=True)
      except OSError:
        pass
      continue
    unlink_app_bundle(app_id, candidate)


def _entry_source_path(app, *, source_root: Path | None = None) -> Path | None:
  source_dir = source_root or getattr(app, "source_dir", None)
  if not source_dir:
    return None
  source_root = Path(source_dir)
  entry = "index.jsx"
  manifest_path = source_root / "mobius.json"
  try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = manifest.get("entry") if isinstance(manifest, dict) else None
    if isinstance(declared, str) and declared.strip():
      entry = declared.strip()
  except (FileNotFoundError, OSError, json.JSONDecodeError):
    pass
  candidate = source_root / entry
  try:
    candidate.resolve().relative_to(source_root.resolve())
  except (OSError, ValueError):
    # A malformed local manifest must never redirect a compile write outside
    # its source tree. The manifest validator will report the declaration; the
    # compiler safely falls back to the canonical local entry.
    return source_root / "index.jsx"
  return candidate


def _copy_managed_static_assets(source_root: Path, snapshot_root: Path) -> None:
  """Copy installer-managed static assets without following filesystem links."""
  source_static = source_root / "static"
  if not source_static.is_dir() or source_static.is_symlink():
    return
  target_static = snapshot_root / "static"
  for current, dirs, files in os.walk(source_static, followlinks=False):
    current_path = Path(current)
    if current_path.is_symlink():
      raise RuntimeError("Managed static asset directory is a symlink.")
    safe_dirs = []
    for name in dirs:
      child = current_path / name
      if child.is_symlink():
        raise RuntimeError("Managed static asset directory contains a symlink.")
      safe_dirs.append(name)
    dirs[:] = safe_dirs
    relative = current_path.relative_to(source_static)
    destination = target_static / relative
    destination.mkdir(parents=True, exist_ok=True)
    for name in files:
      source = current_path / name
      if source.is_symlink() or not source.is_file():
        raise RuntimeError("Managed static asset is not a regular file.")
      shutil.copy2(source, destination / name)


def _remove_unsupported_outputs(
  out: Path,
  metafile: dict,
  *,
  metadata_cwd: str | Path | None = None,
) -> None:
  """Remove outputs esbuild wrote before metadata exposed a contract error."""
  out.unlink(missing_ok=True)
  outputs = metafile.get("outputs")
  if not isinstance(outputs, dict):
    return
  for details in outputs.values():
    if not isinstance(details, dict):
      continue
    css_bundle = details.get("cssBundle")
    if isinstance(css_bundle, str):
      css_path = Path(css_bundle)
      if not css_path.is_absolute() and metadata_cwd is not None:
        css_path = Path(metadata_cwd) / css_path
      css_path.unlink(missing_ok=True)


async def compile_jsx(
  app_id: int,
  jsx_source: str,
  *,
  out_path: str | Path,
  source_path: str | Path | None = None,
) -> str:
  """Compiles JSX source to an ES module and returns the output path.

  Args:
    app_id: The numeric ID of the mini-app being compiled.
    jsx_source: The JSX source code string.
    out_path: Where esbuild writes the bundle. Production callers pass the
      app's staging path, then publish the output by content hash (see
      ``recompile_app_bundle``). It is required so a new caller cannot silently
      bypass immutable publication through the retired fixed live filename.
    source_path: Optional real filesystem entrypoint. When present,
      esbuild compiles that path directly, allowing relative imports from
      sibling files in the app source tree. When omitted, the legacy
      string-only path writes ``jsx_source`` to a temp file and compiles that.

  Returns:
    The absolute path of the compiled JS file.

  Raises:
    CompileError: If the JSX is invalid or esbuild rejects the source.
  """
  # Fail loudly on malformed source before invoking esbuild — the
  # silent-0-byte case produces a downstream "no default export" at
  # runtime and wastes a debug round-trip.
  if not jsx_source or not jsx_source.strip():
    message = (
      "JSX source is empty. Write your component to "
      "apps/<name>/index.jsx before applying."
    )
    raise CompileError(message, stderr=message, source_path=source_path)
  out = Path(out_path)

  entry_path = Path(source_path) if source_path is not None else None
  tmp_path = None
  compile_cwd = None
  if entry_path is None:
    with tempfile.NamedTemporaryFile(
      suffix=".jsx", mode="w", delete=False, encoding="utf-8"
    ) as f:
      f.write(jsx_source)
      tmp_path = f.name
    entry_path = Path(tmp_path)
  else:
    # Compile real source trees from their root with a relative entry name.
    # Besides keeping sibling imports natural, this makes output deterministic:
    # esbuild's generated module comments no longer embed a random temporary
    # snapshot path (explicit apply compiles from one such snapshot).
    entry_path = entry_path.resolve()
    compile_cwd = str(entry_path.parent)
  command_entry = entry_path.name if compile_cwd is not None else entry_path

  metadata_fd, metadata_name = tempfile.mkstemp(
    prefix="mobius-esbuild-", suffix=".json",
  )
  os.close(metadata_fd)
  metadata_path = Path(metadata_name)
  try:
    try:
      proc = await asyncio.create_subprocess_exec(
        *esbuild_command(command_entry, out, metafile=metadata_path),
        env=esbuild_environment(),
        cwd=compile_cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
      )
    except FileNotFoundError:
      raise RuntimeError(
        "esbuild is not installed or not on PATH. "
        "The Docker image installs it automatically; for local dev run: "
        "npm install -g esbuild"
      )
    try:
      _, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=ESBUILD_TIMEOUT_SECS,
      )
    except asyncio.TimeoutError:
      proc.kill()
      await proc.communicate()
      raise RuntimeError(
        f"esbuild timed out after {ESBUILD_TIMEOUT_SECS} seconds"
      )
    except asyncio.CancelledError:
      # The awaiting task was cancelled (for example, during shutdown). Kill
      # the child so it can't keep running and
      # writing out_path after we've returned and released our locks.
      proc.kill()
      try:
        await proc.communicate()
      except Exception:
        pass
      raise
    if proc.returncode != 0:
      decoded_stderr = stderr.decode()
      raise CompileError(
        "Compilation failed.",
        stderr=decoded_stderr,
        source_path=entry_path,
      )
    try:
      metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      raise RuntimeError(f"Could not read esbuild metadata: {exc}") from exc
    contract_error = esbuild_metafile_contract_error(metadata)
    if contract_error:
      _remove_unsupported_outputs(
        out, metadata, metadata_cwd=compile_cwd,
      )
      raise CompileError(
        "Compilation failed.", stderr=contract_error, source_path=entry_path,
      )
  finally:
    metadata_path.unlink(missing_ok=True)
    if tmp_path is not None:
      os.unlink(tmp_path)

  return str(out)


async def recompile_app_bundle(db, app, jsx_source: str) -> None:
  """Recompiles ``app``'s JSX into an immutable bundle as one transaction.

  The new code compiles to a staging file, is atomically published under a
  content-addressed name, and only then does the DB transaction switch
  ``compiled_path`` to that immutable file. The previous row keeps pointing at
  its previous bundle until commit. Thus a crash before commit leaves the old
  row/bundle coherent, while a crash after commit cannot expose a row whose
  bundle was not promoted. When the row has an accepted ``source_commit``, its
  exact Git tree is materialized away from the editable worktree so relative
  imports resolve without reading or rewriting an unapplied draft. Legacy rows
  are only rebuilt from a byte-identical entry. A commit failure removes the
  newly published orphan and rolls back, so there is no window where an old DB
  row serves new code.

  ``db.commit()`` here also flushes any other changes the caller staged on
  the session (e.g. a PATCH's name/description), so the whole update lands
  or rolls back together.

  The caller MUST ensure the app's bundle path (keyed by app id) can't be
  concurrently reused while this runs, or publication/cleanup could touch a
  different app's artifacts. Explicit apply holds the lifecycle + per-app lock
  with ``app`` loaded fresh under it; a newly allocated id is uncommitted, so
  no other operation can reference it yet.

  Raises:
    RuntimeError: from ``compile_jsx`` on invalid JSX (committed path untouched).
    Exception: re-raised after rollback if publication or commit fails.
  """
  staged = _compiled_dir() / f"app-{app.id}.js.staging"
  previous_bundle = owned_bundle_path(
    app.id, getattr(app, "compiled_path", None),
  )
  source_path = _entry_source_path(app)
  compile_source_path = source_path
  snapshot = None
  source_commit = getattr(app, "source_commit", None)
  source_dir = getattr(app, "source_dir", None)
  if source_commit and source_dir:
    # Bundle recovery is not a source publication authority. Compile the exact
    # Git revision selected by SQLite in a temporary tree so an unapplied draft
    # survives boot/recovery byte-for-byte. Static assets are installer-managed
    # and deliberately excluded from Git, so copy that validated sidecar
    # without following links.
    from app import app_git
    snapshot = tempfile.TemporaryDirectory(prefix="mobius-app-recompile-")
    snapshot_root = Path(snapshot.name)
    try:
      await asyncio.to_thread(
        app_git.materialize_tree,
        Path(source_dir),
        source_commit,
        snapshot_root,
      )
      await asyncio.to_thread(
        _copy_managed_static_assets, Path(source_dir), snapshot_root,
      )
      compile_source_path = _entry_source_path(
        app, source_root=snapshot_root,
      )
      if compile_source_path is None:
        raise RuntimeError("Accepted app source has no entry path.")
      accepted_source = compile_source_path.read_text(encoding="utf-8")
      if accepted_source != jsx_source:
        raise RuntimeError(
          "Stored app source does not match its accepted Git revision."
        )
    except Exception:
      snapshot.cleanup()
      raise
  elif source_path is not None:
    # Legacy rows have no durable source commit. Never repair them by writing
    # into the editable tree: that was a hidden source mutation and could
    # destroy an unapplied draft. Only a byte-identical entry is safe to use.
    try:
      current_source = source_path.read_text(encoding="utf-8")
    except FileNotFoundError:
      current_source = None
    if current_source != jsx_source:
      raise RuntimeError(
        "Editable app source differs from the stored revision; apply the "
        "draft explicitly before rebuilding its bundle."
      )
    from app import app_git

    if await asyncio.to_thread(app_git.worktree_dirty, Path(source_dir)):
      raise RuntimeError(
        "Editable app source has an unapplied draft; apply it explicitly "
        "before rebuilding its bundle."
      )
  published = None
  try:
    await compile_jsx(
      app.id,
      jsx_source,
      out_path=staged,
      source_path=(
        compile_source_path if compile_source_path is not None else None
      ),
    )
  except Exception:
    if snapshot is not None:
      snapshot.cleanup()
    raise
  if snapshot is not None:
    snapshot.cleanup()
  try:
    published = publish_staged_bundle(app.id, staged)
  except Exception:
    staged.unlink(missing_ok=True)
    raise
  app.jsx_source = jsx_source
  app.compiled_path = str(published)
  # Advance updated_at so the /module and /frame ETags (derived from it) change.
  # Without this a warm browser that already holds the module sends the old
  # If-None-Match, the ETag is unchanged, the server 304s, and it serves the
  # stale bundle even though the compiled file changed. The app_updated event
  # remounts the iframe but the refetch would still 304 to the old code.
  app.updated_at = timeutil.now_naive_utc()
  try:
    db.commit()
  except Exception:
    db.rollback()
    staged.unlink(missing_ok=True)
    if published != previous_bundle:
      unlink_app_bundle(app.id, published)
    raise
  if previous_bundle != published:
    unlink_app_bundle(app.id, previous_bundle)


def reap_staging_bundles() -> None:
  """Removes leftover ``*.js.staging`` files at startup.

  Staging files are never referenced by ``compiled_path``. Content-addressed
  publication consumes them before commit, so any survivor is necessarily from
  an interrupted compile and can be discarded without consulting the DB.
  """
  for f in _compiled_dir().glob("*.js.staging"):
    try:
      f.unlink()
    except OSError:
      pass


def reap_orphaned_bundles(db) -> list[str]:
  """Remove compiled artifacts no App row references.

  Run after boot reconciliation. Tombstoned rows remain references because
  they are recoverable during the soft-delete window; hard-deleted rows do not.
  A crash before the DB commit may leave a published content bundle behind,
  while a crash after commit may leave the previous bundle behind. Both are
  unreferenced at the next boot and safe to reap.
  """
  from app import models

  referenced: set[Path] = set()
  for (stored_path,) in db.query(models.App.compiled_path).all():
    if not stored_path:
      continue
    try:
      referenced.add(Path(stored_path).resolve())
    except OSError:
      continue
  removed: list[str] = []
  for candidate in _compiled_dir().glob("app-*.js"):
    if not (
      _LEGACY_BUNDLE_RE.fullmatch(candidate.name)
      or _CONTENT_BUNDLE_RE.fullmatch(candidate.name)
    ):
      continue
    try:
      resolved = candidate.resolve()
    except OSError:
      continue
    if resolved in referenced:
      continue
    try:
      candidate.unlink()
      removed.append(str(candidate))
    except OSError:
      pass
  return removed


def _bundle_path_for_app(app) -> Path:
  stored = owned_bundle_path(app.id, getattr(app, "compiled_path", None))
  return stored or (_compiled_dir() / f"app-{app.id}.js")


async def reconcile_missing_bundles(db) -> list[int]:
  """Recompiles live App rows whose compiled bundle is missing or empty.

  Content-addressed publication makes new writes crash-consistent, but this
  reconciler still covers legacy rows interrupted under the old post-commit
  promotion protocol plus the whole missing-bundle class: manual deletion,
  corruption to zero bytes, or a volume restore that missed ``/data/compiled``.
  Each affected row is rebuilt from its stored ``jsx_source`` using the normal
  immutable publication transition.

  Only rows that need healing are recompiled: tombstoned (uninstalled) apps are
  skipped so the sweep never resurrects a deleted app, and a row with empty
  ``jsx_source`` is skipped because there is nothing to compile. A compile error
  on one app is logged and skipped so it can't block the others from healing or
  brick boot. Runs single-threaded before the server accepts requests, so no
  per-app lock is needed — no other operation can reference these ids yet.

  Returns:
    The ids of the apps whose bundle was successfully recompiled (the rest were
    already healthy, tombstoned, source-less, or failed to compile).
  """
  import logging

  from app import models

  log = logging.getLogger(__name__)
  rows = (
    db.query(models.App)
    .filter(models.App.deleted_at.is_(None))
    .all()
  )
  healed: list[int] = []
  for app in rows:
    bundle = _bundle_path_for_app(app)
    # An empty (zero-byte) bundle is an aborted/partial write and is as
    # unservable as a missing file, so treat both the same.
    try:
      present = bundle.exists() and bundle.stat().st_size > 0
    except OSError:
      present = False
    if present:
      continue
    if not app.jsx_source or not app.jsx_source.strip():
      continue
    try:
      await recompile_app_bundle(db, app, app.jsx_source)
      healed.append(app.id)
    except Exception as exc:
      # One app's bad source must not block the rest of the sweep or boot;
      # the agent can fix the source later from the still-present row.
      log.error(
        "missing-bundle reconcile failed for app %s: %s",
        app.id, exc, exc_info=True,
      )
  return healed


def _bundle_uses_current_runtime(bundle: Path) -> bool:
  """Return whether ``bundle`` starts with the current compiler ABI banner."""
  try:
    with bundle.open("rb") as stream:
      prefix = stream.read(len(COMPILED_RUNTIME_BANNER.encode("ascii")))
  except OSError:
    return False
  return prefix == COMPILED_RUNTIME_BANNER.encode("ascii")


def _bundle_is_content_addressed(app_id: int, bundle: Path) -> bool:
  """Return whether the path and bytes form this app's immutable artifact."""
  owned = owned_bundle_path(app_id, bundle)
  if owned is None:
    return False
  match = _CONTENT_BUNDLE_RE.fullmatch(owned.name)
  if match is None or int(match.group("app_id")) != app_id:
    return False
  try:
    return _file_sha256(owned) == match.group("digest")
  except OSError:
    return False


def app_bundle_uses_current_compile_contract(app) -> bool:
  """Return whether ``app`` points at a current immutable bundle.

  The banner includes both the host ABI and the additive artifact revision, so
  this predicate can require a runtime refresh without making old and new host
  processes reject one another during a live deployment.
  """
  bundle = _bundle_path_for_app(app)
  try:
    present = bundle.exists() and bundle.stat().st_size > 0
  except OSError:
    present = False
  return bool(
    present
    and _bundle_uses_current_runtime(bundle)
    and _bundle_is_content_addressed(app.id, bundle)
  )


async def reconcile_outdated_bundles(db) -> list[int]:
  """Rebuild legacy, corrupt, or older-contract bundles into immutable artifacts.

  Opaque app frames execute one self-contained module: React, the Mobius
  runtime bridge, and every supported app dependency are part of that artifact.
  A compiler-runtime change therefore needs a durable migration for already
  installed apps. ``esbuild_command`` stamps the ABI + artifact revision at byte
  zero, while publication names the output by its SHA-256. Keeping additive
  refreshes in the revision lets a live checkout and the pre-restart backend
  remain host-compatible throughout deployment. This boot sweep recompiles any
  present, non-empty bundle without the current banner or a valid content
  address. The latter condition migrates
  every legacy fixed-name bundle once and repairs the pre-migration same-ABI
  crash gap by rebuilding from the committed row source.

  The missing/empty sweep runs immediately before this one, so absent bundles
  are left to that purpose-built recovery path. Recompilation uses the existing
  staging + content-publication + commit transaction, isolates failures per
  app, and advances the app version only after a successful build. An
  interrupted boot can safely resume the remaining rows on the next start.

  Returns:
    The ids of apps successfully migrated to the current immutable contract.
  """
  import logging

  from app import models

  log = logging.getLogger(__name__)
  rows = (
    db.query(models.App)
    .filter(models.App.deleted_at.is_(None))
    .all()
  )
  migrated: list[int] = []
  for app in rows:
    bundle = _bundle_path_for_app(app)
    try:
      present = bundle.exists() and bundle.stat().st_size > 0
    except OSError:
      present = False
    if not present or app_bundle_uses_current_compile_contract(app):
      continue
    if not app.jsx_source or not app.jsx_source.strip():
      continue
    try:
      await recompile_app_bundle(db, app, app.jsx_source)
      migrated.append(app.id)
    except Exception as exc:
      log.error(
        "compiled-runtime reconcile failed for app %s: %s",
        app.id, exc, exc_info=True,
      )
  return migrated
