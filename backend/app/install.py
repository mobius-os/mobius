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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.orm import Session

from app import (
  activity,
  app_git,
  data_git,
  fs_locks,
  icon_assets,
  models,
  source_dirs,
)
from app import app_cron
from app.app_capabilities import contract_and_digest
from app.app_source_check import check_app_source
from app.compiler import (
  CompileError,
  compile_jsx,
  owned_bundle_path,
  publish_staged_bundle,
  unlink_app_bundle,
)
from app.config import get_settings
from app.manifest_contract import (
  ENTRY_MAX_BYTES as _CONTRACT_ENTRY_MAX_BYTES,
  ICON_MAX_BYTES as _CONTRACT_ICON_MAX_BYTES,
  MANIFEST_MAX_BYTES as _CONTRACT_MANIFEST_MAX_BYTES,
  SEED_MAX_BYTES as _CONTRACT_SEED_MAX_BYTES,
  SEEDS_COUNT_MAX as _CONTRACT_SEEDS_COUNT_MAX,
  SEEDS_TOTAL_MAX as _CONTRACT_SEEDS_TOTAL_MAX,
  SKILL_MAX_BYTES as _CONTRACT_SKILL_MAX_BYTES,
  SOURCE_FILES_TOTAL_MAX as _CONTRACT_SOURCE_FILES_TOTAL_MAX,
  STATIC_ASSET_MAX_BYTES as _CONTRACT_STATIC_ASSET_MAX_BYTES,
  STATIC_ASSETS_COUNT_MAX as _CONTRACT_STATIC_ASSETS_COUNT_MAX,
  STATIC_ASSETS_TOTAL_MAX as _CONTRACT_STATIC_ASSETS_TOTAL_MAX,
  SYSTEM_PROMPT_MAX_BYTES as _CONTRACT_SYSTEM_PROMPT_MAX_BYTES,
  ManifestContractError,
  static_asset_entries,
  validate_manifest_contract,
  validate_storage_destination,
)
# Keep the underscore alias: install._http_get calls _validate_url_safe, and
# the install tests patch `app.install._validate_url_safe`. The canonical
# validator now lives in net_utils (shared with routes/proxy.py) — see
# net_utils.py for why the two SSRF validators were unified.
from app.net_utils import validate_url_safe as _validate_url_safe
from app.storage_io import atomic_write
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

# Entry JSX cap. Real apps run 5-50 KB; 1 MB is enough headroom for
# anything reasonable while bounding worst-case install cost.
_ENTRY_MAX_BYTES = _CONTRACT_ENTRY_MAX_BYTES

# Seed file cap (per file). Storage seeds are prompts, default
# configs, sample images — never huge.
_SEED_MAX_BYTES = _CONTRACT_SEED_MAX_BYTES

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
# a bogus mode. "fullscreen" is the one games want (no OS status bar / notch).
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


# Aggregate caps across ALL seeds in one manifest. The per-file cap alone
# leaves the total unbounded (a manifest can list many seeds), so a small
# manifest could still force large memory growth holding them all (Codex
# review round-10 #6). These bound the count and the summed bytes.
_SEEDS_COUNT_MAX = _CONTRACT_SEEDS_COUNT_MAX
_SEEDS_TOTAL_MAX = _CONTRACT_SEEDS_TOTAL_MAX

# Static site assets declared by a manifest. These are for prebuilt apps that
# need durable files below /data/apps/<slug>/static (served at /app-assets/...),
# not one-off files dropped into the platform frontend.
_STATIC_ASSET_MAX_BYTES = _CONTRACT_STATIC_ASSET_MAX_BYTES
_STATIC_ASSETS_COUNT_MAX = _CONTRACT_STATIC_ASSETS_COUNT_MAX
_STATIC_ASSETS_TOTAL_MAX = _CONTRACT_STATIC_ASSETS_TOTAL_MAX
_STATIC_ASSETS_MANIFEST = ".mobius-static-assets.json"
_PENDING_UPDATE_DIR = "mobius-pending-update"

# Sibling source modules a multi-file mini-app declares alongside `entry`
# (`cards.js`, `utils.js`, …) so esbuild can bundle the import graph. The shared
# manifest contract caps the count; fetch additionally caps the summed bytes.
_SOURCE_FILES_TOTAL_MAX = _CONTRACT_SOURCE_FILES_TOTAL_MAX

# Shared skill files an app declares via manifest `skills`: each entry is a
# root-level `source_files` basename the post-commit sync phase copies into
# /data/shared/skills/. Small caps — skills are instruction prose, not data.
_SKILL_MAX_BYTES = _CONTRACT_SKILL_MAX_BYTES

# A system-prompt fragment is more privileged than a skill because it is read
# every turn. It uses the same root-level markdown and byte-size contract.
_SYSTEM_PROMPT_MAX_BYTES = _CONTRACT_SYSTEM_PROMPT_MAX_BYTES

# Installer-owned ownership/provenance sidecar inside the skills dir. A
# dotfile that is not `*.md`, so the skill loaders (the app-skills catalog
# app, the SDK skill-load observers) never list or read it.
_APP_SKILLS_SIDECAR = ".app-skills.json"

# Tracked files in a merged tree that are NOT hand-written app source: the
# managed .gitignore, the install-managed static-asset manifest, and the cron
# script. The job script is dropped separately (its name is known only at call
# time). Excluding these keeps the source-write loop from rewriting an
# install-managed artifact a clean merge happened to carry on `main`.
_MERGED_NON_SOURCE = frozenset((
  ".gitignore", _STATIC_ASSETS_MANIFEST, "init-cron.sh",
))

# Icon cap matches the icon-upload route's 12 MB ceiling.
_ICON_MAX_BYTES = _CONTRACT_ICON_MAX_BYTES

_HTTP_TIMEOUT = 15.0

# Hard cap on redirect hops. Real GitHub raw URLs don't redirect at
# all; legitimate community hosts shouldn't need more than a couple.
# The cap is the safety net against redirect loops + redirect-based
# SSRF where each hop slips through validation by aiming at a
# different host.
_MAX_REDIRECTS = 5

# Tests override this module attribute to prevent production cron mutation.
CRON_SCAFFOLD = app_cron.BAKED_CRON_SCAFFOLD


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


def _derive_raw_base(manifest_url: str) -> str:
  """Everything before the trailing filename — entry, icon, and seed
  file references resolve relative to this."""
  if "/" not in manifest_url:
    raise HTTPException(400, "Cannot derive raw_base from manifest_url.")
  return manifest_url.rsplit("/", 1)[0] + "/"


def _derive_repo_ref(manifest_url: str) -> tuple[str, str] | None:
  """Return the GitHub repo/ref for a raw GitHub manifest URL, if derivable.

  Deliberately narrow: only a ROOT manifest at a SINGLE-SEGMENT ref
  (`raw.githubusercontent.com/<org>/<repo>/<ref>/<manifest-file>`, exactly four
  path segments) is clone-eligible. That is the canonical `mobius-os/app-*`
  shape, and it is the only shape where the repo root's `index.jsx` is the entry
  the manifest points at. Two other shapes must NOT be clone-derived — they fall
  back to the synthetic HTTP-fetch install instead of mis-cloning:
    - a manifest in a repo SUBDIR (`…/<ref>/<subdir>/mobius.json`) — cloning the
      repo root would then get the wrong `index.jsx`.
    - a branch name CONTAINING A SLASH (`…/<repo>/feature/x/mobius.json`) — a
      greedy `parts[:3]` would mis-read the ref as `feature`.
  Both push the segment count past four, so the strict `== 4` check rejects
  them. The caller (`install_from_manifest`) treats a `None` return as
  not-clone-eligible and keeps the already-fetched HTTP entry.
  """
  parsed = urlparse(manifest_url)
  parts = [unquote(part) for part in parsed.path.split("/") if part]
  if (
    parsed.scheme != "https"
    or parsed.hostname != "raw.githubusercontent.com"
    or len(parts) != 4
  ):
    return None
  org, repo, ref, _manifest_file = parts
  for part in (org, repo, ref):
    if part in ("", ".", "..") or part.startswith("-") or "\\" in part:
      return None
  return f"https://github.com/{org}/{repo}.git", ref


def _canonical_for_inline(raw_base: str, manifest_id: str) -> str:
  """Synthesize a stable manifest_url for inline-manifest installs.

  Used when the caller passed `manifest` + `raw_base` instead of a
  manifest_url. We need SOMETHING to key update-vs-install
  discrimination on; the raw_base + manifest_id is unique-enough for
  that purpose."""
  return _canonical_identity_key(raw_base, manifest_id)


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


def _canonical_base(url_or_base: str) -> str:
  """The canonical base of a manifest URL: fragment, query string, a trailing
  `/mobius.json`, and a trailing slash all stripped.

  Strip BOTH fragment and query string. Without ?-strip, two paste-a-URL flows
  for the same app (with vs without `?utm_source=…`) would canonicalise to
  different keys and split the app into two App rows on the second install.
  The identity key is `<base>#manifest-id=<id>`, so this base is ALSO the prefix
  to match installed rows on regardless of the manifest id — callers that need
  to ask "is this URL's app installed?" LIKE `<base>#manifest-id=%`."""
  base = url_or_base.split("#", 1)[0].split("?", 1)[0]
  if base.endswith("/mobius.json"):
    base = base[: -len("/mobius.json")]
  return base.rstrip("/")


def _trusted_catalog_repo_base(url_or_base: str) -> str | None:
  """A ref-INDEPENDENT identity base for a trusted mobius-os catalog ROOT
  manifest, or None when `url_or_base` isn't that shape.

  `raw.githubusercontent.com/mobius-os/<repo>/<ref>/mobius.json` collapses to the
  identity `raw.githubusercontent.com/mobius-os/<repo>` — the mutable `<ref>`
  segment dropped. A `mobius-os/app-*` repo is ONE app whatever revision we
  fetch, so a pinned-commit bump (bootstrap moving app-skills from `main` to a
  reviewed commit, and every later re-pin) must name the SAME app as the row it
  supersedes — never fork a duplicate, never miss an owner's tombstone and
  silently undo their uninstall.

  Deliberately narrow, mirroring `_derive_repo_ref`: only the canonical
  single-segment-ref ROOT shape (canonical base == `<org>/<repo>/<ref>`, three
  path parts) qualifies. A repo SUBDIR manifest keeps its ref-bearing base, so
  two apps hosted in one repo can never collapse onto one identity.
  """
  if not isinstance(url_or_base, str) or not url_or_base:
    return None
  base = _canonical_base(url_or_base)
  parsed = urlparse(base)
  if parsed.hostname != "raw.githubusercontent.com":
    return None
  parts = [unquote(part) for part in parsed.path.split("/") if part]
  if len(parts) != 3 or ".." in parts:
    return None
  org, repo, ref = parts
  if org != "mobius-os":
    return None
  for part in (org, repo, ref):
    if part in ("", ".", "..") or part.startswith("-") or "\\" in part:
      return None
  return f"{parsed.scheme}://{parsed.hostname}/{org}/{repo}"


def _find_ref_independent_catalog_row(
  db: Session, canonical_manifest_url: str, manifest_id: str,
) -> models.App | None:
  """An existing row for the same trusted repo + manifest id at ANY ref.

  Used only when the exact-ref identity lookups miss. Prefers a live row, then
  the lowest id, and re-verifies each candidate in Python because SQL `LIKE`
  treats `_` as a wildcard (a ref may contain one). Tombstone-agnostic like the
  primary identity lookup: an explicit install/update/pin-bump that reaches here
  revives a soft-deleted row in place (keeping its id + storage). Honoring an
  owner's uninstall of a `reinstall_after_uninstall=False` app is the bootstrap
  layer's job — it decides whether to call install at all.
  """
  repo_base = _trusted_catalog_repo_base(canonical_manifest_url)
  if repo_base is None:
    return None
  suffix = f"#manifest-id={manifest_id}"
  candidates = (
    db.query(models.App)
    .filter(models.App.manifest_url.like(f"{repo_base}/%{suffix}"))
    .order_by(
      case((models.App.deleted_at.is_(None), 0), else_=1),
      models.App.id.asc(),
    )
    .all()
  )
  for cand in candidates:
    url = cand.manifest_url or ""
    if url.endswith(suffix) and _trusted_catalog_repo_base(url) == repo_base:
      return cand
  return None


def _canonical_identity_key(url_or_base: str, manifest_id: str) -> str:
  """Single canonical shape for the `manifest_url` column.

  The two install paths (inline-manifest install with `raw_base`, and
  URL install with `manifest_url=.../mobius.json`) used to write
  visibly different strings into `App.manifest_url` for the same
  underlying app. Re-installing via the other path then missed the
  update branch and created a duplicate row. The fragment is purely a
  marker — it's never dereferenced over the wire."""
  return f"{_canonical_base(url_or_base)}#manifest-id={manifest_id}"


def _should_force_core_store_update(
  source: str, manifest_id: str, canonical_manifest_url: str,
) -> bool:
  """Core App Store self-updates must not wedge behind their own local edits.

  Normal apps preserve local edits and surface conflicts for an agent to
  resolve. The App Store is the installer for resolving those conflicts, so
  letting its own update conflict creates a dead-end: the user presses Update,
  the backend records upstream, but the running store remains old forever. For
  the canonical mobius-os App Store only, the published upstream source wins.
  """
  parsed = urlparse(canonical_manifest_url)
  path_parts = [
    unquote(part)
    for part in parsed.path.split("/")
    if part
  ]
  return (
    source == "store"
    and manifest_id == "store"
    and parsed.hostname == "raw.githubusercontent.com"
    and path_parts[:2] == ["mobius-os", "app-store"]
  )




async def _http_get(
  client: httpx.AsyncClient, url: str, max_bytes: int, _hops: int = 0,
) -> bytes:
  """GETs a URL with SSRF validation + manual redirect handling.

  Each hop is re-validated through `_validate_url_safe` so a 302 to
  a private IP gets rejected just like a direct request to one — and the
  connection is PINNED to the validated IP (we fetch `pinned_url`, an
  IP-in-netloc URL, with the real hostname carried as the Host header + TLS
  SNI). That makes the address we checked the address we actually connect to,
  closing the DNS-rebinding gap where httpx would re-resolve at connect time.
  `follow_redirects` is False on the client; we walk the chain ourselves with a
  hop count cap, resolving each Location against the original (hostname) URL.

  Reads the body as a stream and aborts as soon as the running byte
  total crosses `max_bytes` — `r.content` would buffer the full
  response before the cap fires, so a hostile upstream could force
  us to allocate `max_bytes` × N pending requests in memory.
  """
  if _hops > _MAX_REDIRECTS:
    raise HTTPException(
      502, f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url}",
    )
  pinned_url, host_header, sni_host = _validate_url_safe(url)
  try:
    async with client.stream(
      "GET", pinned_url,
      headers={"Host": host_header},
      # httpcore/anyio expect a hostname string here. Passing bytes worked with
      # older httpx but now reaches idna2008_resolve(), which calls `.encode()`
      # and crashes every real HTTPS install/update before the request is sent.
      extensions={"sni_hostname": sni_host},
    ) as r:
      # Handle redirects + error statuses with the stream closed
      # quickly so we don't hold a connection while recursing.
      if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location")
        if not loc:
          raise HTTPException(
            502, f"Redirect from {url} missing Location header.",
          )
        next_url = urljoin(url, loc)
      else:
        next_url = None
        if r.status_code == 404:
          raise HTTPException(404, f"Not found: {url}")
        if r.status_code == 429:
          raise HTTPException(429, _rate_limit_detail(url, r.headers))
        if r.status_code >= 400:
          raise HTTPException(
            502, f"Upstream {r.status_code} fetching {url}",
          )
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
  except httpx.TimeoutException:
    raise HTTPException(504, f"Timeout fetching {url}")
  except httpx.RequestError as exc:
    raise HTTPException(502, f"Failed to fetch {url}: {exc}")
  # Recurse outside the stream context so the previous connection is
  # already released by the time we open the next one.
  return await _http_get(client, next_url, max_bytes, _hops + 1)


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


def _rate_limit_detail(url: str, headers) -> str:
  host = urlparse(url).hostname or "upstream"
  service = "GitHub" if "github" in host.lower() else host
  detail = f"{service} rate-limited this app update."
  retry_after = _header(headers, "retry-after")
  if retry_after:
    try:
      seconds = int(float(retry_after))
      if seconds > 0:
        return f"{detail} Try again in {_wait_label(seconds)}."
    except ValueError:
      pass
  reset = _header(headers, "x-ratelimit-reset")
  if reset:
    try:
      reset_at = datetime.fromtimestamp(int(reset), UTC)
      return f"{detail} Try again after {reset_at.isoformat(timespec='minutes')}."
    except (ValueError, OSError):
      pass
  return f"{detail} Please wait a minute and try again."


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
      backup_existing_file(target, rel)
    else:
      created_paths.append(target)
    atomic_write(target, content)

  for rel in sorted(previous_assets - set(assets)):
    target = (static_root / rel).resolve()
    if static_root not in target.parents:
      continue
    if not target.exists() or not target.is_file():
      continue
    backup_existing_file(target, rel)
    target.unlink()

  if metadata_path.exists():
    backup_existing_file(metadata_path, _STATIC_ASSETS_MANIFEST)
  else:
    created_paths.append(metadata_path)
  atomic_write(metadata_path, json.dumps(sorted(assets), indent=2) + "\n")

  if backup_root_used:
    commit_actions.append(
      lambda d=backup_root: shutil.rmtree(d, ignore_errors=True)
    )


def stage_pending_conflict_update(
  source_dir: str | Path,
  *,
  app_id: int,
  upstream_commit: str,
  manifest: dict,
  raw_base: str,
  capability_digest: str,
  candidate_digest: str,
) -> None:
  """Persist everything explicit resolution needs to finish an update.

  A conflict deliberately returns before install materialization. The resolver
  may run minutes later (or after a restart), so its exact identity must
  outlive the request. The digest binds every fetched source/static/icon/seed
  byte; replay refetches and refuses to promote if any byte moved. The receipt
  lives under ``.git`` and is replaced atomically, so a restart sees either the
  previous complete candidate or the new complete candidate, never a gap.
  """
  repo = Path(source_dir)
  git_dir = repo / ".git"
  target = git_dir / _PENDING_UPDATE_DIR
  if target.is_symlink():
    raise ValueError("pending update path must not be a symlink")
  target.mkdir(parents=True, exist_ok=True)
  atomic_write(target / "receipt.json", json.dumps({
    "schema": 1,
    "app_id": app_id,
    "upstream_commit": upstream_commit,
    "manifest": manifest,
    "raw_base": raw_base,
    "capability_digest": capability_digest,
    "candidate_digest": candidate_digest,
  }, ensure_ascii=False, sort_keys=True) + "\n")


def read_pending_conflict_update_receipt(
  source_dir: str | Path, *, app_id: int, upstream_commit: str | None,
) -> dict | None:
  """Read validated pending metadata without loading potentially large assets."""
  root = Path(source_dir) / ".git" / _PENDING_UPDATE_DIR
  receipt_path = root / "receipt.json"
  if root.is_symlink() or receipt_path.is_symlink():
    return None
  try:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if (
    not isinstance(receipt, dict)
    or receipt.get("schema") != 1
    or receipt.get("app_id") != app_id
    or not upstream_commit
    or receipt.get("upstream_commit") != upstream_commit
    or not isinstance(receipt.get("manifest"), dict)
    or not isinstance(receipt.get("raw_base"), str)
    or not isinstance(receipt.get("capability_digest"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("candidate_digest", ""))
  ):
    return None
  return receipt


def _install_candidate_digest(
  *,
  manifest: dict,
  raw_base: str,
  entry_bytes: bytes,
  icon_processed: bytes | None,
  bundled_job: bytes | None,
  static_assets: dict[str, bytes],
  source_files: dict[str, bytes],
  seeds: dict[str, bytes],
) -> str:
  """Bind a deferred conflict resolution to the exact fetched candidate.

  A resolver can finish long after the first install request. Its replay may
  fetch through a moving branch URL, so manifest/version equality alone is not
  enough: publishers can replace static, seed, icon, or source bytes without a
  version bump. Length-prefixed fields make this digest unambiguous while
  streaming large assets instead of base64-encoding them into the receipt.
  """
  digest = hashlib.sha256()

  def add(label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(4, "big"))
    digest.update(label_bytes)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)

  add(
    "manifest",
    json.dumps(
      manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"),
  )
  add("raw_base", raw_base.encode("utf-8"))
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


def _source_review_digest(
  *,
  manifest: dict,
  entry_bytes: bytes,
  bundled_job: bytes | None,
  source_files: dict[str, bytes],
) -> str:
  """Bind an owner-reviewed diff to the manifest and executable source bytes."""
  digest = hashlib.sha256()

  def add(label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(4, "big"))
    digest.update(label_bytes)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)

  add(
    "manifest",
    json.dumps(
      manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"),
  )
  add("entry", entry_bytes)
  add("job-present", b"1" if bundled_job is not None else b"0")
  if bundled_job is not None:
    add("job", bundled_job)
  for rel in sorted(source_files):
    add("source-path", rel.encode("utf-8"))
    add("source-bytes", source_files[rel])
  return digest.hexdigest()


def clear_pending_conflict_update(source_dir: str | Path) -> None:
  shutil.rmtree(
    Path(source_dir) / ".git" / _PENDING_UPDATE_DIR,
    ignore_errors=True,
  )


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
      _count, warnings = reconcile_app_cron_supervision(cron_db)
    finally:
      cron_db.close()
    for warning in warnings:
      log.warning("install rollback cron supervision skipped: %s", warning)
  except Exception as exc:
    # Rollback is already a best-effort failure path. Log without masking the
    # original install exception that caused it.
    log.warning("install rollback cron reconciliation failed: %s", exc)


def _crontab_without_app(current: str, source_dir: Path) -> str | None:
  """Return `current` crontab text with every line whose COMMAND runs a
  script under `source_dir` removed — or None if nothing matched, so the
  caller can skip rewriting entirely.

  Matches on the command's executable path (see `app_cron.crontab_command_path`),
  NOT a whole-line substring: that keeps the news/news-2 prefix safe AND
  avoids dropping an unrelated app whose ARGUMENTS merely reference this
  app's dir (e.g. `... /data/apps/agg/run.sh --feed /data/apps/news/x`).
  Comments, blanks, and `PATH=`/env lines run no command and are preserved;
  `@daily`/`@reboot` shorthand and inline `VAR=val <cmd>` are handled too.
  """
  needle = f"{str(source_dir).rstrip('/')}/"
  kept, dropped = [], False
  for ln in current.splitlines():
    if app_cron.crontab_command_path(ln).startswith(needle):
      dropped = True
    else:
      kept.append(ln)
  if not dropped:
    return None
  return ("\n".join(kept) + "\n") if kept else ""


def _unregister_cron(source_dir: Path) -> None:
  """Remove crontab entries that invoke scripts under `source_dir`.

  Called on app delete so a removed app does not leave a crontab entry
  firing a now-missing script. The spool isn't on the /data volume, so
  an orphan self-clears on the next container restart anyway — this just
  stops it firing (and erroring) in the meantime, and prevents stale
  lines like the `news-2/job.sh` orphan from accumulating across
  reinstalls. Best-effort: every failure is swallowed, exactly like the
  source-tree rmtree this accompanies. Runs `crontab -u mobius` (the
  server runs as mobius, which may edit its own crontab).
  """
  if app_cron.cron_mutation_blocked_in_test_runtime():
    return
  try:
    listing = subprocess.run(
      ["crontab", "-u", "mobius", "-l"],
      capture_output=True, text=True, timeout=10,
    )
  except (OSError, subprocess.SubprocessError):
    return
  if listing.returncode != 0:
    # No crontab yet, or no crontab binary (as in the test image) —
    # nothing to clean.
    return
  new_crontab = _crontab_without_app(listing.stdout, source_dir)
  if new_crontab is None:
    return  # no entry referenced this app — leave the crontab untouched
  try:
    proc = subprocess.run(
      ["crontab", "-u", "mobius", "-"],
      input=new_crontab, text=True, timeout=10, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return
  if proc.returncode != 0:
    log.warning(
      "cron: failed to rewrite mobius crontab on app delete (rc=%s)",
      proc.returncode,
    )


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


async def _sync_app_skills(
  db: Session,
  app: "models.App",
  manifest: dict,
  warnings: list[str],
) -> None:
  """Materializes manifest-declared skill files into /data/shared/skills.

  Post-commit best-effort phase (same contract as cron): the app row is
  already durable, so every failure appends a warning instead of raising.
  Bytes come from the app's FINAL on-disk source tree, which is uniform
  across the synthetic, clone, and post-merge update paths (validation
  guarantees skills ⊆ root source_files, so the tree carries them).

  The never-lose-work contract: a present skill file whose bytes differ
  from what this installer last recorded (agent edits, or a pre-manifest
  seed copy) is git-snapshotted into the /data repo BEFORE being
  overwritten, and left untouched when the snapshot cannot be guaranteed.
  Ownership rides the installer-owned sidecar so one app can never
  silently take over another live app's skill file.
  """
  skills = list(dict.fromkeys(manifest.get("skills") or []))
  data_dir = Path(get_settings().data_dir)
  skills_dir = data_dir / "shared" / "skills"
  source_dir = Path(app.source_dir)
  version = str(manifest.get("version", "unknown"))
  async with fs_locks.shared_skills_lock():
    from app import skills as skills_mod

    # Resolve any crash-interrupted /api/skills install FIRST, so a pending
    # directory skill is either a fully-owned collision below or gone — the
    # same reconciliation the direct install/uninstall paths run under this
    # lock. Then read the installed-skills sidecar so this writer honors the
    # SAME basename-collision policy as a direct install: a flat `foo.md` must
    # never coexist with an installed directory skill `foo/` under one id.
    skills_mod.reconcile_installed(skills_dir)
    installed_records = skills_mod._read_sidecar(
      skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR
    )
    sidecar_path = skills_dir / _APP_SKILLS_SIDECAR
    records: dict = {}
    if sidecar_path.exists():
      try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
          records = loaded
      except (OSError, ValueError):
        # A corrupt sidecar downgrades every present file to the
        # modified-or-unrecorded case — snapshot-then-overwrite — which is
        # the safe direction: work is preserved, ownership is re-earned.
        warnings.append("skills: ownership sidecar unreadable — rebuilding")
    desired = set(skills)
    # An update that drops a previously-owned skill must deactivate it just as
    # surely as an uninstall. Preserve the exact bytes in a retired archive,
    # then release the basename for another app.
    for rel, rec in list(records.items()):
      if not isinstance(rec, dict) or rec.get("app_id") != app.id:
        continue
      if rel in desired:
        continue
      target = skills_dir / rel
      retired = (
        skills_dir / ".inactive" / str(app.id) / "retired" / rel
      )
      inactive = skills_dir / ".inactive" / str(app.id) / rel
      source = target if target.is_file() else inactive
      if source.is_file():
        retired.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, retired)
      records.pop(rel, None)
      warnings.append(f"skill {rel}: retired by update")
    for rel in skills:
      try:
        content = (source_dir / rel).read_bytes()
      except OSError:
        # Validation checked the manifest's declaration, not the repo's
        # contents — a clone whose tree lacks the file lands here.
        warnings.append(f"skill {rel}: missing from installed source tree")
        continue
      if len(content) > _SKILL_MAX_BYTES:
        warnings.append(
          f"skill {rel}: exceeds {_SKILL_MAX_BYTES} bytes — skipped"
        )
        continue
      rec = records.get(rel)
      owner_id = rec.get("app_id") if isinstance(rec, dict) else None
      if owner_id is not None and owner_id != app.id:
        owner = db.query(models.App).filter(models.App.id == owner_id).first()
        if owner is not None:
          warnings.append(
            f"skill {rel}: owned by app {owner.slug} — skipped"
          )
          continue
        # The recorded owner was hard-purged after its recovery window, so the
        # basename is no longer reserved and this app may take it over.
      # Cross-SHAPE collision: an install-provenance skill may hold the same
      # logical id in the OTHER on-disk shape. `rel` is the flat `<stem>.md`;
      # the colliding directory skill is `<stem>/`. Refuse to write a second
      # skill under one id (the direct-install path enforces the same both-shape
      # rule) — the owner resolves it by removing one deliberately.
      stem = Path(rel).stem
      dir_shape = skills_dir / stem
      if isinstance(installed_records.get(stem), dict) or dir_shape.is_dir():
        warnings.append(
          f"skill {rel}: an installed skill already holds id {stem!r} "
          "(directory shape) — skipped"
        )
        continue
      if (
        isinstance(rec, dict)
        and owner_id == app.id
        and rec.get("active", True) is False
      ):
        inactive = skills_dir / ".inactive" / str(app.id) / rel
        if inactive.is_file():
          retired = (
            skills_dir / ".inactive" / str(app.id) / "retired" / rel
          )
          retired.parent.mkdir(parents=True, exist_ok=True)
          os.replace(inactive, retired)
      target = skills_dir / rel
      recorded_sha = rec.get("sha256") if isinstance(rec, dict) else None
      if target.exists():
        current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_sha != recorded_sha:
          # Modified since last recorded, or never recorded (an agent
          # edit, or the old platform seed's copy): snapshot the current
          # bytes before replacing them — never trade edits for an update.
          if (data_dir / ".git").is_dir():
            try:
              ok, detail = await asyncio.to_thread(
                data_git.snapshot_path,
                data_dir,
                f"shared/skills/{rel}",
                f"pre-install snapshot of {rel} (app {app.slug} v{version})",
              )
            except Exception as exc:
              ok, detail = False, repr(exc)
            if not ok:
              warnings.append(
                f"skill {rel}: left unchanged (snapshot failed: {detail}); "
                "will retry next update"
              )
              continue
            warnings.append(f"skill {rel}: snapshotted then updated")
          else:
            warnings.append(
              f"skill {rel}: updated, no /data repo for snapshot"
            )
      atomic_write(target, content)
      # 0o664 mirrors init_skills' boot convention — group-writable so the
      # agent can edit the skill no matter which uid materialized it.
      os.chmod(target, 0o664)
      records[rel] = {
        "app_id": app.id,
        "slug": app.slug,
        "manifest_url": app.manifest_url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "installed_at": datetime.now(UTC).isoformat(),
        "active": True,
      }
      # Persist provenance immediately after each file lands: a crash
      # between files must never leave an installed skill without its
      # ownership record (the next install would treat it as agent-modified
      # and snapshot-then-overwrite — safe, but noisy). Installs are rare
      # and the JSON is tiny, so a per-file write costs nothing.
      atomic_write(
        sidecar_path,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Persist a drop-only reconciliation too (the per-file writes above cover
    # every non-empty desired set).
    if not skills:
      skills_dir.mkdir(parents=True, exist_ok=True)
      atomic_write(
        sidecar_path,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # The skill set changed (or may have) — refresh the generated tier-1 index
    # INSIDE the lock. Regenerating after release let a concurrent direct
    # install/uninstall's newer index be overwritten by this writer's stale
    # snapshot. Best-effort like everything else in this post-commit phase.
    _regenerate_skills_index(skills_dir)


def _read_app_skill_records(skills_dir: Path) -> tuple[Path, dict]:
  sidecar = skills_dir / _APP_SKILLS_SIDECAR
  try:
    value = json.loads(sidecar.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    value = {}
  return sidecar, value if isinstance(value, dict) else {}


async def deactivate_app_skills(app_id: int) -> list[str]:
  """Remove one app's skills from discovery while preserving exact bytes."""
  warnings: list[str] = []
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    changed = False
    for rel, rec in records.items():
      if not isinstance(rec, dict) or rec.get("app_id") != app_id:
        continue
      if Path(rel).name != rel or not rel.endswith(".md"):
        warnings.append(f"skill record {rel!r}: invalid basename")
        continue
      target = skills_dir / rel
      inactive = skills_dir / ".inactive" / str(app_id) / rel
      try:
        if target.is_file():
          inactive.parent.mkdir(parents=True, exist_ok=True)
          os.replace(target, inactive)
        if inactive.is_file():
          # Keep ``sha256`` as the last app-supplied baseline. The inactive
          # digest records the exact preserved bytes separately; otherwise an
          # owner-edited skill would become its own new baseline and a later
          # app update could overwrite it without taking the normal snapshot.
          rec["inactive_sha256"] = hashlib.sha256(
            inactive.read_bytes()
          ).hexdigest()
          rec["active"] = False
          rec["inactive_path"] = str(inactive.relative_to(skills_dir))
          rec["deactivated_at"] = datetime.now(UTC).isoformat()
          changed = True
      except OSError as exc:
        warnings.append(f"skill {rel}: could not deactivate ({exc})")
    if changed:
      atomic_write(
        sidecar,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)
  return warnings


async def restore_app_skills(app_id: int) -> list[str]:
  """Restore exact tombstoned skill bytes and their discovery records."""
  warnings: list[str] = []
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    changed = False
    for rel, rec in records.items():
      if (
        not isinstance(rec, dict)
        or rec.get("app_id") != app_id
        or rec.get("active", True) is not False
      ):
        continue
      inactive = skills_dir / ".inactive" / str(app_id) / rel
      target = skills_dir / rel
      if not inactive.is_file():
        warnings.append(f"skill {rel}: preserved bytes are missing")
        continue
      try:
        if target.exists():
          if target.read_bytes() != inactive.read_bytes():
            conflict = (
              skills_dir / ".inactive" / str(app_id) / "conflicts" / rel
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
            warnings.append(
              f"skill {rel}: preserved a file created while app was inactive"
            )
          else:
            target.unlink()
        os.replace(inactive, target)
        rec["active"] = True
        rec.pop("inactive_path", None)
        rec.pop("inactive_sha256", None)
        rec["restored_at"] = datetime.now(UTC).isoformat()
        changed = True
      except OSError as exc:
        warnings.append(f"skill {rel}: could not restore ({exc})")
    if changed:
      atomic_write(
        sidecar,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)
  return warnings


async def purge_app_skills(app_id: int) -> None:
  """Release records and inactive archives after the recovery TTL expires."""
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    for rel, rec in list(records.items()):
      if isinstance(rec, dict) and rec.get("app_id") == app_id:
        target = skills_dir / rel
        if rec.get("active", True) and target.is_file():
          try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == rec.get("sha256"):
              target.unlink()
          except OSError:
            pass
        records.pop(rel, None)
    shutil.rmtree(
      skills_dir / ".inactive" / str(app_id), ignore_errors=True,
    )
    atomic_write(
      sidecar,
      json.dumps(records, indent=2, sort_keys=True) + "\n",
    )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)


def _regenerate_skills_index(skills_dir: Path) -> None:
  """Best-effort refresh of the generated skills-index.md after a change."""
  try:
    from app import skills as skills_mod

    skills_mod.write_index(skills_dir)
  except Exception:  # noqa: BLE001 - the index is a convenience surface
    log.warning("skills index regeneration failed", exc_info=True)


def _check_source_completeness(
  *,
  app_name: str,
  manifest: dict,
  source_tree: dict[str, bytes],
  entry_key: str,
  static_dests: list[str],
  job_name: str | None,
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
  directory so source checks see the exact path compilation sees. For example,
  logical destination ``logo.js`` is importable as ``./static/logo.js``.
  """
  files: dict[str, str] = {
    rel: data.decode("utf-8", "replace") for rel, data in source_tree.items()
  }
  static_source_paths = [f"static/{dest}" for dest in static_dests]
  for path in static_source_paths:
    files.setdefault(path, "")

  result = check_app_source(
    files,
    entry=entry_key,
    source_files=manifest.get("source_files") or [],
    job=job_name,
    static_assets=static_source_paths,
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


@dataclass
class FetchedUpstream:
  """The manifest + source bytes install would record, fetched read-only.

  `source_files` and `job_bytes` mirror what `install_from_manifest` records on
  the per-app `upstream` branch — canonical ``index.jsx``, its declared sibling
  modules, and the schedule job script."""
  manifest: dict
  entry_bytes: bytes
  source_files: dict[str, bytes]
  job_name: str | None
  job_bytes: bytes | None


async def fetch_upstream_source(manifest_url: str) -> FetchedUpstream:
  """Fetch a manifest and its source files read-only — no install, DB, or git.

  The read-only twin of `install_from_manifest`'s fetch phase: GET the manifest
  at `manifest_url`, then the entry JSX, every declared `source_files` sibling,
  and the schedule job script — exactly the files install records on the
  per-app `upstream` branch. Reuses the same `_http_get` (SSRF-validated,
  size-capped, manual-redirect) and `_validate_manifest` that install uses, so
  the fetched bytes match install's byte-for-byte and a later content compare
  against the recorded upstream tree is apples-to-apples.

  Storage seeds, static assets, and the icon are deliberately NOT fetched: none
  of them are tracked source (seeds land in the id-keyed storage tree, static
  assets under gitignored `static/`, the icon as a processed PNG), so they never
  appear on the `upstream` branch an update-check compares against.

  Raises HTTPException on any fetch or validation failure. The caller decides
  whether that is a hard error or a degrade-to-unknown."""
  # follow_redirects=False — _http_get walks the chain manually so every hop is
  # re-validated against SSRF, matching install_from_manifest's client setup.
  async with httpx.AsyncClient(
    timeout=_HTTP_TIMEOUT, follow_redirects=False,
  ) as cli:
    raw = await _http_get(cli, manifest_url, _MANIFEST_MAX_BYTES)
    try:
      manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
      raise HTTPException(400, f"Manifest is not valid JSON: {exc}")
    _validate_manifest(manifest)
    raw_base = _normalize_raw_base(_derive_raw_base(manifest_url))

    entry_bytes = await _http_get(
      cli, raw_base + manifest["entry"], _ENTRY_MAX_BYTES,
    )

    source_files: dict[str, bytes] = {}
    source_files_total = 0
    for rel in manifest.get("source_files") or []:
      data = await _http_get(cli, raw_base + rel, _ENTRY_MAX_BYTES)
      source_files_total += len(data)
      if source_files_total > _SOURCE_FILES_TOTAL_MAX:
        raise HTTPException(
          400,
          f"Manifest source_files exceed {_SOURCE_FILES_TOTAL_MAX} bytes total.",
        )
      source_files[rel] = data

    sched = manifest.get("schedule")
    job_name = sched.get("job") if isinstance(sched, dict) else None
    job_bytes: bytes | None = None
    if job_name:
      job_bytes = await _http_get(cli, raw_base + job_name, _ENTRY_MAX_BYTES)

  return FetchedUpstream(
    manifest=manifest,
    entry_bytes=entry_bytes,
    source_files=source_files,
    job_name=job_name,
    job_bytes=job_bytes,
  )


async def _fetch_and_validate_manifest(
  cli: httpx.AsyncClient,
  *,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
) -> tuple[dict, str]:
  """Load one manifest and return it with its normalized asset base.

  Preview and install intentionally share this exact boundary.  The preview is
  therefore not a second, weaker interpretation that can drift from what the
  installer eventually applies.
  """
  if (manifest_url is None) == (manifest is None):
    raise HTTPException(
      400, "Provide exactly one of `manifest_url` or `manifest`.",
    )
  if manifest_url is not None:
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
  return manifest, _normalize_raw_base(raw_base)


async def preview_manifest_capabilities(
  *,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
) -> tuple[dict, str, dict, str]:
  """Return the validated manifest/base and its canonical review contract."""
  async with httpx.AsyncClient(
    timeout=_HTTP_TIMEOUT,
    follow_redirects=False,
  ) as cli:
    loaded, normalized_base = await _fetch_and_validate_manifest(
      cli,
      manifest_url=manifest_url,
      manifest=manifest,
      raw_base=raw_base,
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


@dataclass(frozen=True)
class InstallTarget:
  """Identity decision made before any database or filesystem mutation."""

  existing: models.App | None
  mode: str
  renaming: bool
  canonical_manifest_url: str
  force_core_store_update: bool


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
    """Undo pre-commit filesystem work; never undo a durable install."""
    if self.durable:
      return
    _run_rollback_actions(self.rollback_actions)
    _cleanup(self.created_paths)

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
  reviewed_capability_digest: str | None,
  reviewed_source_digest: str | None,
  expected_app_id: int | None,
  expected_upstream_commit: str | None,
  expected_candidate_digest: str | None,
) -> InstallCandidate:
  """Fetch every install input once and enforce all review/replay guards."""
  async with httpx.AsyncClient(
    timeout=_HTTP_TIMEOUT,
    follow_redirects=False,
  ) as cli:
    manifest, raw_base = await _fetch_and_validate_manifest(
      cli,
      manifest_url=manifest_url,
      manifest=manifest,
      raw_base=raw_base,
    )
    capability_contract, capability_digest = contract_and_digest(manifest)
    if (
      reviewed_capability_digest is not None
      and reviewed_capability_digest != capability_digest
    ):
      raise HTTPException(
        409,
        {
          "code": "capability_changed",
          "message": (
            "The app's capabilities changed after they were reviewed. "
            "Review the current manifest and try again."
          ),
          "manifest": manifest,
          "capability_contract": capability_contract,
          "capability_digest": capability_digest,
        },
      )

    entry_bytes = await _http_get(
      cli, raw_base + manifest["entry"], _ENTRY_MAX_BYTES,
    )

    icon_processed: bytes | None = None
    icon_warning: str | None = None
    if manifest.get("icon"):
      try:
        icon_raw = await _http_get(
          cli, raw_base + manifest["icon"], _ICON_MAX_BYTES,
        )
        icon_processed = icon_assets.normalize_icon(icon_raw)
      except icon_assets.InvalidIcon as exc:
        icon_warning = f"icon: {exc}"
        log.info("install: icon skipped — %s", exc)
      except HTTPException as exc:
        # A broken optional icon must not block an otherwise valid app.
        icon_warning = f"icon: {exc.detail}"
        log.info("install: icon skipped — %s", exc.detail)

    schedule = manifest.get("schedule")
    bundled_job = None
    if schedule and schedule.get("job"):
      bundled_job = await _http_get(
        cli, raw_base + schedule["job"], _ENTRY_MAX_BYTES,
      )

    static_assets: dict[str, bytes] = {}
    static_assets_total = 0
    for dest, src in static_asset_entries(
      manifest.get("static_assets") or {},
    ).items():
      if len(static_assets) >= _STATIC_ASSETS_COUNT_MAX:
        raise HTTPException(
          400,
          "Manifest has too many static_assets "
          f"(max {_STATIC_ASSETS_COUNT_MAX}).",
        )
      data = await _http_get(
        cli, raw_base + src, _STATIC_ASSET_MAX_BYTES,
      )
      static_assets_total += len(data)
      if static_assets_total > _STATIC_ASSETS_TOTAL_MAX:
        raise HTTPException(
          400,
          "Manifest static_assets exceed "
          f"{_STATIC_ASSETS_TOTAL_MAX} bytes total.",
        )
      static_assets[dest] = data

    source_files: dict[str, bytes] = {}
    source_files_total = 0
    for rel in manifest.get("source_files") or []:
      data = await _http_get(cli, raw_base + rel, _ENTRY_MAX_BYTES)
      source_files_total += len(data)
      if source_files_total > _SOURCE_FILES_TOTAL_MAX:
        raise HTTPException(
          400,
          f"Manifest source_files exceed {_SOURCE_FILES_TOTAL_MAX} bytes total.",
        )
      source_files[rel] = data

    seeds: dict[str, bytes] = {}
    seeds_total = 0
    for sub, value in (manifest.get("storage_seeds") or {}).items():
      if len(seeds) >= _SEEDS_COUNT_MAX:
        raise HTTPException(
          400,
          f"Manifest has too many storage_seeds (max {_SEEDS_COUNT_MAX}).",
        )
      if _seed_value_is_inline(value):
        data = json.dumps(
          value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
      else:
        data = await _http_get(cli, raw_base + value, _SEED_MAX_BYTES)
      seeds_total += len(data)
      if seeds_total > _SEEDS_TOTAL_MAX:
        raise HTTPException(
          400,
          f"Manifest storage_seeds exceed {_SEEDS_TOTAL_MAX} bytes total.",
        )
      seeds[sub] = data

  candidate_digest = _install_candidate_digest(
    manifest=manifest,
    raw_base=raw_base,
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
  )
  source_review_digest = _source_review_digest(
    manifest=manifest,
    entry_bytes=entry_bytes,
    bundled_job=bundled_job,
    source_files=source_files,
  )
  if (
    reviewed_source_digest is not None
    and source_review_digest != reviewed_source_digest
  ):
    raise HTTPException(
      409,
      {
        "code": "update_changed",
        "message": (
          "The app source changed after it was reviewed. "
          "Refresh the update preview and try again."
        ),
      },
    )
  replay_fields = (
    expected_app_id,
    expected_upstream_commit,
    expected_candidate_digest,
  )
  if any(value is not None for value in replay_fields) and not all(
    value is not None for value in replay_fields
  ):
    raise RuntimeError("resolved update replay identity is incomplete")
  if (
    expected_candidate_digest is not None
    and candidate_digest != expected_candidate_digest
  ):
    raise HTTPException(
      409,
      {
        "code": "pending_update_changed",
        "message": (
          "The pending update changed upstream. "
          "Review the latest update and start again."
        ),
      },
    )

  return InstallCandidate(
    manifest=manifest,
    raw_base=raw_base,
    entry_bytes=entry_bytes,
    icon_processed=icon_processed,
    icon_warning=icon_warning,
    bundled_job=bundled_job,
    static_assets=static_assets,
    source_files=source_files,
    seeds=seeds,
    capability_contract=capability_contract,
    capability_digest=capability_digest,
    candidate_digest=candidate_digest,
    source_review_digest=source_review_digest,
  )


def _select_install_target(
  db: Session,
  *,
  candidate: InstallCandidate,
  manifest_url: str | None,
  source: str,
  expected_app_id: int | None,
) -> InstallTarget:
  """Resolve install/update/adoption identity without mutating the target row."""
  manifest = candidate.manifest
  manifest_id = manifest["id"]
  source_for_key = (
    manifest_url if manifest_url is not None else candidate.raw_base
  )
  canonical_manifest_url = _canonical_identity_key(
    source_for_key, manifest_id,
  )
  force_core_store_update = _should_force_core_store_update(
    source, manifest_id, canonical_manifest_url,
  )
  existing = (
    db.query(models.App)
    .filter(models.App.manifest_url == canonical_manifest_url)
    .first()
  )
  if existing is None:
    # A catalog pin may move refs while retaining repo + manifest identity.
    existing = _find_ref_independent_catalog_row(
      db, canonical_manifest_url, manifest_id,
    )

  renaming = False
  if existing is None:
    # A rename is explicit and source-bound: previous_id is looked up under the
    # same canonical package base, never by a globally reusable slug.
    prev_id = manifest.get("previous_id")
    if prev_id:
      prev_canonical = _canonical_identity_key(source_for_key, prev_id)
      existing = (
        db.query(models.App)
        .filter(
          models.App.manifest_url == prev_canonical,
          models.App.deleted_at.is_(None),
        )
        .first()
      )
      if existing:
        renaming = True

  if expected_app_id is not None and (
    existing is None or existing.id != expected_app_id
  ):
    raise HTTPException(409, "Pending update no longer matches this app.")
  return InstallTarget(
    existing=existing,
    mode="update" if existing else "install",
    renaming=renaming,
    canonical_manifest_url=canonical_manifest_url,
    force_core_store_update=force_core_store_update,
  )


async def _run_post_commit_effects(
  db: Session,
  *,
  app: models.App,
  mode: str,
  source: str,
  candidate: InstallCandidate,
  warnings: list[str],
) -> None:
  """Converge cron, skills, and initialization after durability.

  Every failure here becomes a warning. The app row and selected bundle are
  already durable, so this phase must never enter the pre-commit rollback path.
  """
  manifest = candidate.manifest
  schedule = manifest.get("schedule")
  job_name = schedule.get("job") if schedule else None
  cron_job_name = job_name or "job.sh"
  has_cron = bool(schedule and schedule.get("default"))
  drop_prior_cron = mode == "update"
  if candidate.bundled_job or has_cron or drop_prior_cron:
    slug = app.slug
    data_dir = Path(get_settings().data_dir)
    app_data_dir = Path(app.source_dir)
    try:
      async with fs_locks.source_dir_lock(str(app_data_dir)):
        if not db.query(models.App.id).filter(models.App.id == app.id).first():
          raise HTTPException(404, "App removed before cron registration.")
        app_data_dir.mkdir(parents=True, exist_ok=True)
        if drop_prior_cron:
          await asyncio.to_thread(_drop_app_cron, app_data_dir)
        job_path = app_data_dir / cron_job_name
        if (
          job_name
          and job_path.exists()
          and not os.access(job_path, os.X_OK)
        ):
          warnings.append(
            f"schedule.job {cron_job_name} is not executable — cron/run-job "
            "will fail until the app repo commits the executable bit"
          )
        active_cron_scaffold = app_cron.cron_scaffold(CRON_SCAFFOLD)
        if has_cron and active_cron_scaffold.exists():
          await asyncio.to_thread(
            app_cron.register_cron,
            slug,
            schedule["default"],
            job_path,
            app.id,
            scaffold=active_cron_scaffold,
          )
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

  if (
    mode == "install"
    and schedule
    and schedule.get("initialize_on_install") is True
    and job_name
    and app.source_dir
  ):
    try:
      from app.app_jobs import launch_app_job

      source_dir = Path(app.source_dir)
      wait_for_ready = source == "bootstrap"
      launch_app_job(
        app.id,
        source_dir / job_name,
        source_dir,
        wait_for_ready=wait_for_ready,
      )
      warnings.append(
        "initialization waiting for startup readiness"
        if wait_for_ready else "initialization started"
      )
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
  renaming = target.renaming
  canonical_manifest_url = target.canonical_manifest_url

  if existing:
    app = existing
    app.deleted_at = None
    app.name = manifest["name"]
    app.description = manifest.get("description", "")
    db.flush()

    if renaming and manifest_id != app.slug:
      old_source_dir = app.source_dir
      target_slug = manifest_id
      target_source_dir = str(data_dir / "apps" / target_slug)
      moved = False
      if old_source_dir and Path(old_source_dir).is_dir():
        async with fs_locks.source_dir_lock(old_source_dir):
          try:
            _reject_if_source_dir_taken(
              db, target_source_dir, exclude_id=app.id,
            )
            target_taken = False
          except HTTPException:
            target_taken = True
          if not target_taken and not Path(target_source_dir).exists():
            _unregister_cron(Path(old_source_dir))
            os.rename(old_source_dir, target_source_dir)
            moved = True
            journal.rollback_actions.append(
              _reconcile_cron_after_install_rollback
            )
            journal.rollback_actions.append(
              lambda o=old_source_dir, n=target_source_dir:
                os.rename(n, o)
                if Path(n).is_dir() and not Path(o).exists()
                else None
            )
      if moved:
        app.slug = target_slug
        app.source_dir = target_source_dir
        app.manifest_url = canonical_manifest_url
        db.flush()
      elif old_source_dir and Path(old_source_dir).is_dir():
        warnings.append(
          f"could not rename slug {app.slug}->{manifest_id}: target in use"
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
  app = models.App(
    name=manifest["name"],
    description=manifest.get("description", ""),
    jsx_source=candidate.entry_bytes.decode("utf-8"),
    source_dir=source_dir,
    slug=slug,
    manifest_url=canonical_manifest_url,
    cross_app_access=permissions.get("cross_app_access", "none"),
    share_with_apps=permissions.get("share_with_apps", "none"),
    chat_log_access=permissions.get("chat_log_access", "none"),
    manage_apps=bool(permissions.get("manage_apps", False)),
    manage_skills=bool(permissions.get("manage_skills", False)),
    github_access=bool(permissions.get("github_access", False)),
    github_connect=bool(permissions.get("github_connect", False)),
    filesystem_access=bool(permissions.get("filesystem_access", False)),
    offline_capable=bool(manifest.get("offline_capable", False)),
    embeds_agent=bool(manifest.get("embeds_agent", False)),
    offline_contract=manifest.get("offline") or None,
    system_prompt_file=manifest.get("system_prompt") or None,
    system_app=bool(manifest.get("system_app", False)),
    capability_contract=candidate.capability_contract,
  )
  db.add(app)
  db.flush()
  return app

@dataclass(frozen=True)
class ActivationPlan:
  """Source and manifest state selected by reconciliation for activation."""

  source_tree: dict[str, bytes]
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
  """
  app.version = str(manifest.get("version", "")).strip() or None
  app.theme_color = _manifest_color(manifest.get("theme_color"))
  app.background_color = (
    _manifest_color(manifest.get("background_color")) or app.theme_color
  )
  app.display = _manifest_display(manifest.get("display"))

  entry_source = plan.source_tree[plan.entry_key].decode("utf-8")
  if plan.updating:
    permissions = manifest.get("permissions") or {}
    app.jsx_source = entry_source
    app.manifest_url = plan.canonical_manifest_url
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
    if "offline_capable" in manifest:
      app.offline_capable = bool(manifest["offline_capable"])
    if "embeds_agent" in manifest:
      app.embeds_agent = bool(manifest["embeds_agent"])
    app.offline_contract = manifest.get("offline") or None
    app.system_prompt_file = manifest.get("system_prompt") or None
    app.system_app = bool(manifest.get("system_app", False))
    app.capability_contract = plan.capability_contract

  staged_bundle = data_dir / "compiled" / f"app-{app.id}.js.staging"
  source_dir = Path(app.source_dir)

  _reject_if_source_dir_taken(db, str(source_dir), exclude_id=app.id)
  source_dir.mkdir(parents=True, exist_ok=True)
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
        source_tree=plan.source_tree,
        entry_key=plan.entry_key,
        static_dests=list(plan.static_assets),
        job_name=plan.job_name,
      )

  _write_static_assets(
    source_dir,
    plan.static_assets,
    journal.created_paths,
    journal.rollback_actions,
    journal.commit_actions,
  )
  await compile_jsx(
    app.id,
    entry_source,
    out_path=staged_bundle,
    source_path=source_dir / plan.entry_key,
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
  return equivalence_target


async def install_from_manifest(
  db: Session,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  source: str = "url",
  reviewed_capability_digest: str | None = None,
  reviewed_source_digest: str | None = None,
  expected_app_id: int | None = None,
  expected_upstream_commit: str | None = None,
  expected_candidate_digest: str | None = None,
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
    - FastAPI surfaces each HTTPException with its proper status code;
      we never catch + swallow anything that would land the DB or
      filesystem in a half state.
  """
  # Phase 1: immutable, review-bound candidate. All network I/O ends here.
  candidate = await _fetch_install_candidate(
    manifest_url=manifest_url,
    manifest=manifest,
    raw_base=raw_base,
    reviewed_capability_digest=reviewed_capability_digest,
    reviewed_source_digest=reviewed_source_digest,
    expected_app_id=expected_app_id,
    expected_upstream_commit=expected_upstream_commit,
    expected_candidate_digest=expected_candidate_digest,
  )
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

  # Phase 2: immutable identity/update decision. No writes occur here.
  target = _select_install_target(
    db,
    candidate=candidate,
    manifest_url=manifest_url,
    source=source,
    expected_app_id=expected_app_id,
  )
  existing = target.existing
  mode = target.mode
  canonical_manifest_url = target.canonical_manifest_url
  force_core_store_update = target.force_core_store_update

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
  repo_ref = (
    _derive_repo_ref(manifest_url) if manifest_url is not None else None
  )
  cloned_install = False
  cloned_update = False
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
        diverged = adopt_repoless_source or (
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
        elif repo_ref is not None and await asyncio.to_thread(
          app_git.has_origin, git_source_dir,
        ):
          _, ref = repo_ref
          try:
            app.upstream_commit = await asyncio.to_thread(
              app_git.fetch_upstream, git_source_dir, ref,
            )
            cloned_update = True
          except Exception as exc:
            log.warning(
              "install: fetch from origin at %s failed; falling back to "
              "fetched source path — %r",
              ref, exc,
            )
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
          merge = await asyncio.to_thread(
            app_git.merge_upstream, git_source_dir,
          )
          reconciliation = merge.reconciliation
          if merge.status == "conflict":
            if force_core_store_update:
              # Core App Store self-update: published upstream wins, keep the
              # fetched `source_tree` and apply it like a fast-forward.
              warnings.append(
                "core App Store self-update replaced local edits with upstream"
              )
              divergence = "fast_forward"
              merge_applied = True
            else:
              # Before routing to the owner, auto-resolve a conflict CONFINED
              # to the version identifier: a version label is never a semantic
              # merge, so take-upstream is always right. This kills the most
              # common update-conflict class — a prior local "agent edit"
              # bumped the version and the release bumps the same line. Any
              # conflict beyond the version line returns None and falls through
              # to the owner-resolver flow. Fail-safe: a genuine local edit is
              # never dropped (a residual conflict aborts the whole attempt).
              version_only = await asyncio.to_thread(
                app_git.resolve_version_only_conflict,
                git_source_dir, merge.conflict_paths,
              )
              resolved_source = None
              if version_only is not None:
                resolved_source = {
                  rel: data for rel, data in version_only.tree.items()
                  if rel not in _MERGED_NON_SOURCE
                }
              if resolved_source is not None and entry_key in resolved_source:
                source_tree = resolved_source
                divergence = "clean_merge"
                merge_applied = True
                warnings.append(
                  "auto-resolved a version-only update conflict "
                  "(took the upstream version)"
                )
                reconciliation = app_git.ReconciliationReceipt(
                  proven_present=reconciliation.proven_present,
                  local_only_paths=reconciliation.local_only_paths,
                  new_upstream_paths=reconciliation.new_upstream_paths,
                  compatible_paths=reconciliation.compatible_paths,
                  provenance_refs_used=reconciliation.provenance_refs_used,
                )
                # Exec bits come from the same merged tree the resolution was
                # built on, mirroring the clean-merge branch above.
                git_exec_paths = await asyncio.to_thread(
                  app_git.read_tree_exec_paths,
                  git_source_dir, version_only.tree_oid,
                )
              else:
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
        # Fresh install (or an existing app that somehow lost its repo):
        # for a new raw-GitHub catalog install, prefer a REAL clone so the
        # source tree carries origin/<ref> and the app's own .gitignore. If
        # cloning fails (private repo, renamed repo, offline git access), fall
        # back to the existing synthetic-upstream path unchanged. Canonical
        # index.jsx is read back from the clone — the repo's bytes, not the
        # HTTP fetch, are authoritative on this path.
        if not existing and repo_ref is not None:
          repo_url, ref = repo_ref
          try:
            app.upstream_commit = await asyncio.to_thread(
              app_git.clone_upstream, git_source_dir, repo_url, ref,
            )
            entry_bytes = (git_source_dir / "index.jsx").read_bytes()
            upstream_jsx_sha = hashlib.sha256(entry_bytes).hexdigest()
            source_tree = {"index.jsx": entry_bytes}
            cloned_install = True
          except Exception as exc:
            log.warning(
              "install: clone from %s at %s failed; falling back to "
              "fetched source path — %r",
              repo_url, ref, exc,
            )
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
        # materializes a REAL working-tree merge conflict (markers +
        # MERGE_HEAD) for the agent to resolve with ordinary git.
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
        )

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
          ),
          journal=journal,
          data_dir=data_dir,
        )
    finally:
      # Release the per-source-dir lock (held across the merge + write for the
      # git path) BEFORE the seeds block takes app_storage_lock, preserving the
      # documented acquisition order.
      source_lock.release()

    if mode == "conflict":
      # Commit the recorded upstream provenance + return so the App Store can
      # surface a click-gated resolver. The served source/bundle stay the prior
      # good ones until the owner chooses Resolve in chat.
      db.commit()
      journal.mark_durable()
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
    db.commit()
    # The transaction is now irreversible. Never let a later refresh, activity
    # write, or cleanup error run the failure actions and remove files selected
    # by the durable row. Superseded artifacts can safely remain for the startup
    # orphan reaper if post-commit cleanup is interrupted.
    journal.mark_durable()
    db.refresh(app)

    # Only the durable update may retire provenance.  Doing this before the DB
    # commit would lose the witness if a later storage/row failure rolled the
    # install back.  Ref deletion is best-effort post-commit housekeeping: the
    # replay already parents `main` directly on this upstream target, so keeping
    # an obsolete ref is harmless while deleting it bounds metadata growth.
    if equivalence_target_to_retire:
      try:
        async with fs_locks.source_dir_lock(str(app.source_dir)):
          await asyncio.to_thread(
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
    )

    # Success: drop any .bak snapshots we made — the new bundle is
    # now the canonical one.
    journal.cleanup_superseded()
    clear_pending_conflict_update(app.source_dir)

  except CompileError as exc:
    app_name = str(
      manifest.get("name") or getattr(locals().get("app"), "slug", "app")
    )
    db.rollback()
    journal.rollback_materialization()
    raise HTTPException(422, _compile_error_detail(app_name, exc))
  except HTTPException:
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

  # Phase 4: best-effort effects after the durable boundary.
  await _run_post_commit_effects(
    db,
    app=app,
    mode=mode,
    source=source,
    candidate=candidate,
    warnings=warnings,
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
    except OSError as exc:
      log.warning("install rollback: %s", exc)
