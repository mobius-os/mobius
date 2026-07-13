"""Atomic install + update lifecycle for mini-apps from a manifest.

The store mini-app (and future bootstrap hook) hands the backend a
`mobius.json` URL or inline manifest. This module does the rest:
fetch entry JSX, create/update the App row, compile, write
source_dir for the file watcher, seed storage, upload icon, register
cron. Wrapped in a single SQLAlchemy transaction with on-failure
filesystem cleanup so partial installs don't land.

Why this is server-side, not in the store mini-app:
  - Mini-apps can only PUT into their OWN storage scope, but install
    seeds another app's scope (target's `/data/apps/<new_id>/`).
  - Mini-apps can't shell out to `init-cron-scaffold.sh`; cron needs
    a subprocess + crontab access that lives only in the container.
  - Mini-apps can't write `/data/apps/<slug>/index.jsx` (source_dir),
    so the file-watcher never picks up edits — apps land in a
    "runs but uneditable" state.
  - 4-step client-side flow (POST app, PUT seeds, PUT icon, mark cron)
    can leave the DB row with missing seeds + missing source_dir on a
    mid-flight failure. One transaction here makes that all-or-nothing.

See feature ticket 062 for the design rationale.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import warnings as _warnings_mod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from PIL import Image as _PILImage
from sqlalchemy import case
from sqlalchemy.orm import Session

from app import activity, app_git, fs_locks, legacy_platform_apps, models, source_dirs
from app.app_source_check import check_app_source
from app.compiler import CompileError, compile_jsx
from app.config import get_settings
# Keep the underscore alias: install._http_get calls _validate_url_safe, and
# the install tests patch `app.install._validate_url_safe`. The canonical
# validator now lives in net_utils (shared with routes/proxy.py) — see
# net_utils.py for why the two SSRF validators were unified.
from app.net_utils import validate_url_safe as _validate_url_safe
from app.storage_io import atomic_write
from app.routes.apps import (
  _derive_source_dir, _reject_if_source_dir_taken, _slugify_for_source_dir,
  allocate_unique_slug,
)

# Decompression-bomb defense. PIL's default MAX_IMAGE_PIXELS (~89M)
# is generous enough that a malicious tiny PNG with a giant declared
# dimension can still allocate gigabytes during `load()`. 32M pixels
# (~5657×5657) is enough headroom for any reasonable icon while
# bounding worst-case allocation. The hard ceiling below (4096×4096)
# is a second gate on raw width/height — checked BEFORE `load()` so
# we reject the bomb cheaply via metadata.
_PILImage.MAX_IMAGE_PIXELS = 32_000_000
_ICON_MAX_DIM = 4096

log = logging.getLogger("mobius.install")

# Manifest fetch cap. A legitimate manifest is < 4 KB. The cap is
# the safety net against malicious URLs streaming GB of data.
_MANIFEST_MAX_BYTES = 64 * 1024

# Entry JSX cap. Real apps run 5-50 KB; 1 MB is enough headroom for
# anything reasonable while bounding worst-case install cost.
_ENTRY_MAX_BYTES = 1024 * 1024

# Seed file cap (per file). Storage seeds are prompts, default
# configs, sample images — never huge.
_SEED_MAX_BYTES = 4 * 1024 * 1024

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
  cleaned = _ANSI_RE.sub("", exc.stderr or "")
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
_SEEDS_COUNT_MAX = 64
_SEEDS_TOTAL_MAX = 32 * 1024 * 1024

# Static site assets declared by a manifest. These are for prebuilt apps that
# need durable files below /data/apps/<slug>/static (served at /app-assets/...),
# not one-off files dropped into the platform frontend.
_STATIC_ASSET_MAX_BYTES = 16 * 1024 * 1024
_STATIC_ASSETS_COUNT_MAX = 256
_STATIC_ASSETS_TOTAL_MAX = 64 * 1024 * 1024
_STATIC_ASSETS_MANIFEST = ".mobius-static-assets.json"

# Sibling source modules a multi-file mini-app declares alongside `entry`
# (`cards.js`, `utils.js`, …) so esbuild can bundle the import graph. Bounds
# mirror the static-asset guards: cap the count here, cap the summed bytes at
# fetch time. Each module reuses the entry cap per file.
_SOURCE_FILES_COUNT_MAX = 50
_SOURCE_FILES_TOTAL_MAX = 8 * 1024 * 1024

# Install-managed path prefixes a `source_files` entry must never claim. These
# are written/owned by other phases (static_assets under static/, the cron
# scaffold's init-cron.sh, build output, the .bak snapshots, the integer-id
# storage tree) — a manifest that listed one as a source file would have the
# source-write loop fight the phase that owns it. Mirrors the app_git
# `_GITIGNORE` set conceptually so the per-app git model and the installer agree
# on what is hand-written source versus generated/managed artifact.
_SOURCE_FILES_MANAGED_PREFIXES = (
  "static/", "dist/", ".build/", "node_modules/",
)
_SOURCE_FILES_MANAGED_EXACT = frozenset((
  "index.jsx", ".gitignore", "init-cron.sh", _STATIC_ASSETS_MANIFEST,
))

# Shared skill files an app declares via manifest `skills`: each entry is a
# root-level `source_files` basename the post-commit sync phase copies into
# /data/shared/skills/. Small caps — skills are instruction prose, not data.
_SKILLS_COUNT_MAX = 5
_SKILL_MAX_BYTES = 256 * 1024

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
_ICON_MAX_BYTES = 12 * 1024 * 1024

_HTTP_TIMEOUT = 15.0

# Hard cap on redirect hops. Real GitHub raw URLs don't redirect at
# all; legitimate community hosts shouldn't need more than a couple.
# The cap is the safety net against redirect loops + redirect-based
# SSRF where each hop slips through validation by aiming at a
# different host.
_MAX_REDIRECTS = 5

# Cron scaffold lives at this path in the built image. Tests override
# the module attribute to bypass the scaffold (which hardcodes
# `/data/apps/<slug>/` and doesn't accept the test's `/tmp/testdata`).
CRON_SCAFFOLD = Path("/app/scripts/init-cron-scaffold.sh")

# Cron field grammar: minute hour dom month dow, allowing the
# standard wildcards / ranges / lists / step values. Deliberately
# rejects every shell metacharacter — no `;`, no `$`, no backtick,
# no quotes. Per-field count check below enforces 5 columns.
_CRON_FIELD_OK = re.compile(r"^[\d\*/,\- ]+$")

_REQUIRED_FIELDS = ("id", "name", "version", "description", "entry")

# Slugs are also used as cron-script path components; init-cron-scaffold.sh
# rejects anything outside this set, so reject at the boundary too.
_SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _validate_slug_field(value, field: str) -> None:
  """Apply the manifest-id slug rules to `value`, raising HTTPException(400).

  Both `id` and `previous_id` become a /data/apps/<slug> path component and a
  cron-script argv, so they share one charset + shape contract. Factored out so
  the two checks can't drift (and so a renamed app's old id is held to the same
  bar its new id was held to when it first installed).
  """
  if not isinstance(value, str) or not value:
    raise HTTPException(400, f"Manifest `{field}` must be a non-empty string.")
  if any(ch not in _SLUG_OK for ch in value):
    raise HTTPException(
      400,
      f"Manifest `{field}` {value!r} contains invalid chars "
      "(allow a-z, 0-9, -, _).",
    )
  # Reject leading `-` / `_` to prevent the slug from being smuggled
  # as an argv flag into init-cron-scaffold.sh (or any other tool we
  # hand it to). The scaffold uses `$1` directly so this is defense-
  # in-depth — the real concern is future callers that do use getopt.
  if value[0] in "-_":
    raise HTTPException(
      400, f"Manifest `{field}` must not start with '-' or '_', got {value!r}",
    )
  # A purely-numeric id becomes the slug and source dir /data/apps/<id>,
  # which collides with the numeric-id storage tree another app writes to
  # (storage uses /data/apps/<integer app id>). Reserve bare integers for
  # storage.
  if value.isdigit():
    raise HTTPException(
      400,
      f"Manifest `{field}` {value!r} must not be purely numeric — bare "
      "integers are reserved for the per-app storage path /data/apps/<id>.",
    )


def _validate_manifest(m: dict) -> None:
  """Raises HTTPException(400) with a precise message on any issue."""
  missing = [k for k in _REQUIRED_FIELDS if not m.get(k)]
  if missing:
    raise HTTPException(
      400,
      f"Manifest is missing required fields: {', '.join(missing)}",
    )
  mid = m["id"]
  _validate_slug_field(mid, "id")
  # `previous_id` is the optional predecessor identity an app declares when it
  # renames (or adopts a baked predecessor). It must pass the SAME slug rules as
  # `id` and name a DIFFERENT app — pointing it at its own id is a no-op that
  # would only confuse the rename migration below.
  prev_id = m.get("previous_id")
  if prev_id is not None:
    _validate_slug_field(prev_id, "previous_id")
    if prev_id == mid:
      raise HTTPException(
        400, "Manifest `previous_id` must differ from `id`.",
      )
  if not isinstance(m.get("name"), str):
    raise HTTPException(400, "Manifest `name` must be a string.")
  if not isinstance(m.get("entry"), str):
    raise HTTPException(400, "Manifest `entry` must be a string.")
  _validate_repo_relative_path(m["entry"], "entry")
  if m.get("icon") is not None:
    _validate_repo_relative_path(m["icon"], "icon")
  perms = m.get("permissions", {})
  if not isinstance(perms, dict):
    raise HTTPException(400, "Manifest `permissions` must be an object.")
  for key in ("cross_app_access", "share_with_apps"):
    val = perms.get(key, "none")
    if val not in ("none", "read", "write"):
      raise HTTPException(
        400,
        f"Manifest `permissions.{key}` must be one of none/read/write.",
      )
  # chat_log_access has its own value space (the redaction tiers), not
  # the storage read/write/none ladder. 'full' is reserved but the read
  # API rejects it until a concrete consumer lands (design §2) — we
  # accept it in the manifest so the column round-trips, and surface the
  # "deferred" gap at request time rather than install time.
  log_access = perms.get("chat_log_access", "none")
  if log_access not in ("none", "summary", "full"):
    raise HTTPException(
      400,
      "Manifest `permissions.chat_log_access` must be one of "
      "none/summary/full.",
    )
  if "manage_apps" in perms and not isinstance(perms["manage_apps"], bool):
    raise HTTPException(
      400, "Manifest `permissions.manage_apps` must be a boolean.",
    )
  if "github_access" in perms and not isinstance(perms["github_access"], bool):
    raise HTTPException(
      400, "Manifest `permissions.github_access` must be a boolean.",
    )
  if "filesystem_access" in perms and not isinstance(perms["filesystem_access"], bool):
    raise HTTPException(
      400, "Manifest `permissions.filesystem_access` must be a boolean.",
    )
  # Optional `offline` block — declares the app's offline contract.
  # Schema only (P1-D): accepted, validated, and stored on the App row as JSON;
  # no store badge built yet. The block is informational for the SW/agent but
  # shapes no server-side enforcement — design philosophy §4 ("code empowers
  # the agent; it does not police it").
  _validate_manifest_offline(m.get("offline"))
  seeds = m.get("storage_seeds", {})
  if seeds is not None and not isinstance(seeds, dict):
    raise HTTPException(400, "Manifest `storage_seeds` must be an object.")
  for sub, value in (seeds or {}).items():
    if not isinstance(sub, str) or not sub:
      raise HTTPException(400, "Manifest `storage_seeds` keys must be paths.")
    if isinstance(value, str):
      _validate_repo_relative_path(value, f"storage_seeds.{sub}")
  static_assets = m.get("static_assets", {})
  if static_assets is not None and not isinstance(static_assets, (dict, list)):
    raise HTTPException(
      400, "Manifest `static_assets` must be an object or array.",
    )
  for dest, src in _static_asset_entries(static_assets).items():
    _validate_repo_relative_path(dest, f"static_assets.{dest}")
    _validate_repo_relative_path(src, f"static_assets.{dest}")
  # Optional sibling modules a multi-file app imports from `index.jsx`. Each is
  # a repo-relative path the installer fetches and writes next to the entry so
  # esbuild can bundle the import graph. `entry` is declared separately and the
  # managed `.gitignore` is never author-supplied, so both are rejected here.
  source_files = m.get("source_files")
  if source_files is not None:
    if not isinstance(source_files, list):
      raise HTTPException(400, "Manifest `source_files` must be an array.")
    if len(source_files) > _SOURCE_FILES_COUNT_MAX:
      raise HTTPException(
        400,
        "Manifest has too many source_files "
        f"(max {_SOURCE_FILES_COUNT_MAX}).",
      )
    # The schedule job script is written to the source-dir root under its bare
    # filename, so a source file naming it would collide with the job-write
    # phase. Pull the declared job name here so the loop can reject that too.
    job_sched = m.get("schedule")
    declared_job = (
      job_sched.get("job") if isinstance(job_sched, dict) else None
    )
    for i, rel in enumerate(source_files):
      _validate_repo_relative_path(rel, f"source_files[{i}]")
      # Reject any entry that collides with an install-managed path. `entry`
      # (index.jsx) is declared separately, and the rest (.gitignore, the cron
      # script, the static-asset manifest, the static_assets / build-output /
      # storage trees, the declared job script) are written and owned by other
      # install phases — a source file there would have the source-write loop
      # fight the owning phase.
      if (
        rel in _SOURCE_FILES_MANAGED_EXACT
        or rel == declared_job
        or rel.endswith(".bak")
        or rel[0].isdigit()
        or any(rel.startswith(p) for p in _SOURCE_FILES_MANAGED_PREFIXES)
      ):
        raise HTTPException(
          400,
          f"Manifest `source_files[{i}]` {rel!r} collides with an "
          "install-managed path (entry, .gitignore, static/, dist/, .build/, "
          "node_modules/, the cron/job scripts, .bak snapshots, or the "
          "numeric-id storage tree).",
        )
  # Shared skill files the app ships (`skills`). Shape only here; the
  # subset-of-source_files rule is the existence guarantee — skill bytes ride
  # the source tree on every install path (synthetic fetch, clone, clone
  # fallback), so the sync phase can read them from the final on-disk tree.
  # The size cap is enforced there too, where the final bytes are known.
  skills = m.get("skills")
  if skills is not None:
    if not isinstance(skills, list):
      raise HTTPException(400, "Manifest `skills` must be an array.")
    if len(skills) > _SKILLS_COUNT_MAX:
      raise HTTPException(
        400, f"Manifest has too many skills (max {_SKILLS_COUNT_MAX}).",
      )
    root_sources = {
      rel for rel in (source_files or [])
      if isinstance(rel, str) and "/" not in rel
    }
    for i, rel in enumerate(skills):
      if not isinstance(rel, str) or not rel.endswith(".md") or rel == ".md":
        raise HTTPException(
          400, f"Manifest `skills[{i}]` must be a `<name>.md` filename.",
        )
      if "/" in rel or "\\" in rel or ".." in rel or rel.startswith("."):
        raise HTTPException(
          400,
          f"Manifest `skills[{i}]` {rel!r} must be a bare .md basename — "
          "no directories, no traversal, no dotfiles (dotfiles in the "
          "skills dir are installer-owned).",
        )
      if rel not in root_sources:
        raise HTTPException(
          400,
          f"Manifest `skills[{i}]` {rel!r} must also be listed in "
          "`source_files` as a root-level file — the installer reads skill "
          "bytes from the installed source tree, so a skill that is not a "
          "source file has nothing to install.",
        )
  sched = m.get("schedule")
  if sched is not None:
    if not isinstance(sched, dict):
      raise HTTPException(400, "Manifest `schedule` must be an object.")
    expr = sched.get("default")
    if expr is not None:
      _validate_cron_expr(expr)
    job = sched.get("job")
    if job is not None and (
      not isinstance(job, str) or "/" in job or ".." in job
    ):
      raise HTTPException(
        400,
        "Manifest `schedule.job` must be a bare filename (no path "
        "separators): cron registration and the run-job endpoint both use "
        "only the basename, so a nested path would silently register/run a "
        "different file than the manifest names.",
      )
    if job is not None:
      _validate_repo_relative_path(job, "schedule.job")


def _validate_manifest_offline(offline) -> None:
  """Validate the optional `offline` block in a manifest.

  Accepted shape:
    {
      "reads":     bool,              # app reads storage offline (optional)
      "writes":    "queued" | "none", # write strategy (optional)
      "execution": "full" | "partial" | "none", # compute capability (optional)
      "precache":  [str, ...]         # extra repo-relative paths to precache (optional)
    }

  All keys are optional; an empty dict {} is valid. The block is stored as JSON
  on the App row and forwarded in AppOut. It is informational — no field gates
  server behaviour (the offline_capable flag on the App row is the runtime gate).
  """
  if offline is None:
    return
  if not isinstance(offline, dict):
    raise HTTPException(400, "Manifest `offline` must be an object.")
  if "reads" in offline and not isinstance(offline["reads"], bool):
    raise HTTPException(400, "Manifest `offline.reads` must be a boolean.")
  if "writes" in offline:
    if offline["writes"] not in ("queued", "none"):
      raise HTTPException(
        400,
        "Manifest `offline.writes` must be one of queued/none.",
      )
  if "execution" in offline:
    if offline["execution"] not in ("full", "partial", "none"):
      raise HTTPException(
        400,
        "Manifest `offline.execution` must be one of full/partial/none.",
      )
  precache = offline.get("precache")
  if precache is not None:
    if not isinstance(precache, list):
      raise HTTPException(400, "Manifest `offline.precache` must be an array.")
    for i, p in enumerate(precache):
      _validate_repo_relative_path(p, f"offline.precache[{i}]")


def _validate_repo_relative_path(path: str, field: str) -> None:
  """Reject manifest asset paths that are not repo-relative.

  The public schema says entry/icon/job/string storage seeds are paths within
  the manifest repo. Enforcing that here keeps community-manifest mistakes as
  clean 400s instead of fetching odd concatenated URLs or surfacing later 500s.

  For `storage_seeds` the rejection also names the contract: a string value is
  a path, a non-string is an inline JSON literal. Authors routinely reach for a
  string to inline file content (HTML/CSS/JS), which trips this check on the
  first scheme/fragment in the markup — a bare "must be a relative path" then
  reads as a typo rather than the wrong shape. The hint teaches the fork.
  """
  seed_hint = (
    " For storage_seeds, a string value is a repo-relative path that the"
    " installer fetches, not inline content. To seed literal text, put it in"
    " a repo file and point this key at that path; to store an inline JSON"
    " value, use a non-string (object/array/number/bool/null)."
  ) if field.startswith("storage_seeds.") else ""
  if not isinstance(path, str) or not path:
    raise HTTPException(
      400, f"Manifest `{field}` must be a non-empty string.{seed_hint}"
    )
  parsed = urlparse(path)
  if (
    parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or
    path.startswith("/") or "\\" in path
  ):
    raise HTTPException(
      400,
      f"Manifest `{field}` must be a relative path inside the app repo."
      f"{seed_hint}",
    )
  parts = [unquote(part) for part in path.split("/")]
  if any(part in ("", ".", "..") for part in parts):
    raise HTTPException(
      400,
      f"Manifest `{field}` must not contain empty, '.', or '..' segments."
      f"{seed_hint}",
    )
  if any("/" in part or "\\" in part for part in parts):
    raise HTTPException(
      400,
      f"Manifest `{field}` must not contain encoded path separators."
      f"{seed_hint}",
    )


def _validate_cron_expr(expr: str) -> None:
  """5-field cron grammar, no shell metacharacters. Prevents the
  schedule expression from being smuggled past the argv barrier into
  whatever cron interpreter the scaffold installs it under."""
  if not isinstance(expr, str):
    raise HTTPException(400, "schedule.default must be a string.")
  if not expr or expr[0] in "-":
    raise HTTPException(
      400, f"schedule.default must not be empty or start with '-': {expr!r}",
    )
  if not _CRON_FIELD_OK.match(expr):
    raise HTTPException(
      400,
      f"schedule.default contains disallowed characters: {expr!r}. "
      "Allowed: digits, *, /, ,, -, whitespace.",
    )
  if len(expr.split()) < 5:
    raise HTTPException(
      400,
      f"schedule.default must have at least 5 cron fields, got {expr!r}",
    )


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


# Frozen old core-app slugs kept reserved so a hostile manifest cannot adopt a
# pre-rename core row on not-yet-migrated installs. Safe to drop only after the
# migration window closes.
PRE_RENAME_PLATFORM_SLUGS = ("mind", "dreaming")

# Historical platform-owned app slugs. They are no longer core apps, but old
# instances can still have rows pointing at /data/platform/core-apps/<slug>.
# Keep them reserved from untrusted previous_id adoption and migrate them only
# from the trusted mobius-os catalog.
HISTORICAL_PLATFORM_APP_SLUGS = legacy_platform_apps.SLUGS

# Platform/store slugs that must never be silently ADOPTED (and thereby replaced
# in place, inheriting the row's id + storage) by a `previous_id` declaration in
# an untrusted manifest. See the guard in the predecessor-adoption block.
_RESERVED_PLATFORM_SLUGS = frozenset({
  *HISTORICAL_PLATFORM_APP_SLUGS, "store", *PRE_RENAME_PLATFORM_SLUGS,
})


def _is_legacy_platform_source_dir(
  source_dir: str | None,
  data_dir: str | Path,
  slug: str | None,
) -> bool:
  return legacy_platform_apps.is_legacy_source_dir(source_dir, data_dir, slug)


def _is_historical_platform_app_source_dir(
  source_dir: str | None,
  manifest_url: str | None,
  data_dir: str | Path,
  slug: str | None,
) -> bool:
  """True for a retired core-app row at /data/apps/<slug> not yet migrated.

  One prod-era installer shape registered Memory/Reflection/Beat Machine as
  editable app rows under /data/apps/<slug> with no manifest_url. They are not
  platform-owned source dirs, but they are still the predecessor rows the
  trusted mobius-os catalog entry must update in place. A catalog install lands
  at exactly that same /data/apps/<slug> path but stamps a canonical
  manifest_url, so path + slug alone cannot tell the un-migrated row from the
  migrated steady state — matching on those alone re-fired the migration (an
  extra GitHub fetch, and a no-owner-review fast-forward) on every boot. Gate on
  the empty manifest_url the migration fills in, so this stays a ONE-SHOT
  predicate that mirrors the install-side platform_row query's NULL/'' filter.
  Keep it narrow: only historical platform slugs from legacy_platform_apps match.
  """
  if manifest_url:
    return False
  if not source_dir or not slug or slug not in HISTORICAL_PLATFORM_APP_SLUGS:
    return False
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    return False
  apps_root = source_dirs.apps_root(data_dir)
  return resolved.parent == apps_root and resolved.name == slug


def _is_trusted_legacy_platform_catalog_install(
  app: models.App | None,
  manifest_id: str,
  canonical_manifest_url: str,
  data_dir: str | Path,
) -> bool:
  """True when a trusted same-id catalog install should move a baked app out
  of platform-owned source.

  Old rows can be found through several identity shapes before the no-URL
  legacy-platform fallback runs: canonical manifest_url, raw `mobius.json`, or
  a bare base URL. Treat all of those as the same forward migration, while still
  rejecting previous_id renames or untrusted manifests.
  """
  return (
    app is not None
    and manifest_id == app.slug
    and manifest_id in HISTORICAL_PLATFORM_APP_SLUGS
    and _is_trusted_catalog_source(canonical_manifest_url)
    and _is_legacy_platform_source_dir(app.source_dir, data_dir, app.slug)
  )


def _is_trusted_catalog_source(canonical_manifest_url: str) -> bool:
  """True when the manifest is published under the canonical mobius-os org on
  raw.githubusercontent.com.

  Gates BOTH the legacy-shape update fallback and the `previous_id` platform-slug
  adoption below. An owner-pasted manifest from any other host is therefore never
  matched against a mobius-os row, so it can neither hijack Memory/Reflection/the
  store by declaring their slug nor overwrite them by pointing at their base.

  Reject any `..` segment: `raw.githubusercontent.com/mobius-os/../evil/…`
  string-checks as mobius-os here but GitHub resolves it to the `evil` org — the
  path is compared BEFORE the fetch normalizes it, so treat traversal as untrusted.
  `unquote` runs first, so a percent-encoded `%2e%2e` is caught too.
  """
  parsed = urlparse(canonical_manifest_url)
  parts = [unquote(part) for part in parsed.path.split("/") if part]
  return (
    parsed.hostname == "raw.githubusercontent.com"
    and ".." not in parts
    and len(parts) >= 2
    and parts[0] == "mobius-os"
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
      extensions={"sni_hostname": sni_host.encode("ascii")},
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


def _static_asset_entries(value) -> dict[str, str]:
  """Normalize manifest.static_assets into dest -> source repo paths."""
  if not value:
    return {}
  if isinstance(value, list):
    return {path: path for path in value}
  if isinstance(value, dict):
    entries: dict[str, str] = {}
    for dest, src in value.items():
      if not isinstance(dest, str) or not isinstance(src, str):
        raise HTTPException(
          400,
          "Manifest `static_assets` entries must map paths to paths.",
        )
      entries[dest] = src
    return entries
  raise HTTPException(
    400, "Manifest `static_assets` must be an object or array.",
  )


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


def _process_icon(raw: bytes) -> bytes:
  """PIL pipeline matches routes/apps.py:update_icon — center-square,
  resize-to-fit, preserve alpha, re-encode as PNG.

  Decompression-bomb defense lives here: we inspect `img.size` BEFORE
  calling `img.load()` (PIL reads only the IHDR/header to populate
  `.size`, so the giant allocation is still avoidable at this point).
  Anything above _ICON_MAX_DIM × _ICON_MAX_DIM is rejected as 415
  alongside the PIL-bomb signals.
  """
  from PIL import Image
  try:
    img = Image.open(io.BytesIO(raw))
    # PIL emits DecompressionBombWarning when an image's pixel count
    # exceeds MAX_IMAGE_PIXELS. Locally promote it to an error so the
    # bomb path goes through our 415 instead of a `warnings.warn` that
    # silently lets `load()` proceed.
    with _warnings_mod.catch_warnings():
      _warnings_mod.simplefilter("error", Image.DecompressionBombWarning)
      w, h = img.size
      if w > _ICON_MAX_DIM or h > _ICON_MAX_DIM:
        raise HTTPException(
          415,
          f"Icon dimensions {w}x{h} exceed {_ICON_MAX_DIM}x{_ICON_MAX_DIM} cap.",
        )
      img.load()
  except HTTPException:
    raise
  except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
    raise HTTPException(415, f"Icon rejected as decompression bomb: {exc}")
  except Exception:
    raise HTTPException(415, "Icon is not a valid image.")
  if img.mode not in ("RGB", "RGBA"):
    # Palette-mode PNGs carry transparency in a tRNS chunk, not in the
    # mode string — `"A" in img.mode` reads "P" as opaque, and a convert
    # to RGB flattens every transparent pixel to black. That is exactly
    # how the catalog's quantized (palette-mode) icons got a baked black
    # background at install time. Convert to RGBA whenever the image has
    # any transparency signal; RGB only when provably opaque.
    has_alpha = (
      "A" in img.mode
      or "transparency" in img.info
      or img.mode == "P"
    )
    img = img.convert("RGBA" if has_alpha else "RGB")
  w, h = img.size
  if w != h:
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
  if img.size[0] > 1024:
    img = img.resize((1024, 1024), Image.LANCZOS)
  out = io.BytesIO()
  img.save(out, format="PNG", optimize=True)
  return out.getvalue()


def _register_cron(slug: str, schedule_expr: str, job_path: Path,
                   app_id: int | None = None) -> None:
  """Runs init-cron-scaffold.sh to install the crontab entry.

  The scaffold script writes init-cron.sh + installs the crontab entry
  AND restores it on the next container restart by replaying every
  /data/apps/*/init-cron.sh from the entrypoint. Idempotent — calling
  it for an unchanged (slug, schedule, job) is a no-op.

  The job script itself is written earlier, in the transactional source
  write (so a locally edited job survives an update via the per-app git
  merge); the scaffold preserves an existing job file rather than stubbing
  it, so it never clobbers what we wrote.

  The job filename (e.g. fetch.sh, from the manifest's schedule.job) is
  passed to the scaffold so the crontab entry points at the real job —
  the scaffold defaults to job.sh otherwise, which would leave a
  manifest that ships fetch.sh firing an empty stub.

  `app_id`, when given, is passed as the scaffold's 4th arg so the
  crontab command becomes `<job-path> <app_id>`. A reusable job that
  reads its target app from "$1" (the same contract as the run-job
  "Generate now" endpoint) then fires correctly from cron. Without it,
  such a job runs with no id and exits early — which is exactly how a
  freshly-installed news app's cron lands dead on arrival.
  """
  scaffold = CRON_SCAFFOLD
  if not scaffold.exists():
    # In tests we mock this away; in containers it's always present.
    raise HTTPException(500, "init-cron-scaffold.sh missing from image.")
  cmd = [str(scaffold), slug, schedule_expr, job_path.name]
  if app_id is not None:
    cmd.append(str(app_id))
  result = subprocess.run(
    cmd, capture_output=True, text=True, timeout=30,
  )
  if result.returncode != 0:
    raise HTTPException(
      500,
      f"Cron registration failed: {result.stderr.strip()[:400]}",
    )


def _crontab_command_path(line: str) -> str:
  """The executable path a crontab job line runs, or "" for a line that
  runs no job (blank, comment, or a `NAME=value` env/setting line).

  The schedule is either a single `@shorthand` token (@daily/@reboot/…) or
  five whitespace-separated fields; the rest is the command. cron also lets
  the command be prefixed with inline `NAME=value` assignments, which we
  skip to reach the real executable (the first non-assignment token).
  """
  s = line.strip()
  if not s or s.startswith("#"):
    return ""
  first = s.split(None, 1)[0]
  if first.startswith("@"):
    cmd = (s.split(None, 1) + [""])[1]
  elif "=" in first:
    return ""  # NAME=value env/setting line — runs no command
  else:
    parts = s.split(None, 5)
    cmd = parts[5] if len(parts) == 6 else ""
  toks = cmd.split()
  while toks and "=" in toks[0] and not toks[0].startswith("/"):
    toks.pop(0)
  return toks[0] if toks else ""


def _crontab_without_app(current: str, source_dir: Path) -> str | None:
  """Return `current` crontab text with every line whose COMMAND runs a
  script under `source_dir` removed — or None if nothing matched, so the
  caller can skip rewriting entirely.

  Matches on the command's executable path (see `_crontab_command_path`),
  NOT a whole-line substring: that keeps the news/news-2 prefix safe AND
  avoids dropping an unrelated app whose ARGUMENTS merely reference this
  app's dir (e.g. `... /data/apps/agg/run.sh --feed /data/apps/news/x`).
  Comments, blanks, and `PATH=`/env lines run no command and are preserved;
  `@daily`/`@reboot` shorthand and inline `VAR=val <cmd>` are handled too.
  """
  needle = f"{str(source_dir).rstrip('/')}/"
  kept, dropped = [], False
  for ln in current.splitlines():
    if _crontab_command_path(ln).startswith(needle):
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
  entry AND delete the replayable init-cron.sh under `source_dir`.

  The update path is otherwise add-only, so an app that migrates from a
  recurring schedule (v1) to on-demand-only (v2, no `schedule.default`) would
  leave the v1 crontab line firing and its init-cron.sh re-installed by the
  entrypoint boot replay forever (card 099). Removing the script — not just
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
  if ".." in sub or sub.startswith("/"):
    raise HTTPException(400, f"Invalid storage path: {sub}")
  for ch in sub:
    if not (ch.isalnum() or ch in "._-/"):
      raise HTTPException(400, f"Invalid storage path char: {sub}")
  return data_dir / "apps" / str(app_id) / sub


# Env vars that would redirect the skill-snapshot git commands away from the
# /data repo (git exports them into hook environments, where they OVERRIDE
# `-C`). app_git._git_env is deliberately NOT reused for the snapshot: its
# GIT_CEILING_DIRECTORIES is designed to STOP repo discovery at /data, which
# is exactly the repo the snapshot targets.
_SNAPSHOT_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _snapshot_shared_skill(
  data_dir: Path, rel: str, slug: str, version: str,
) -> tuple[bool, str]:
  """Commits shared/skills/<rel>'s current bytes into the /data repo.

  Returns (ok, detail). ok=True means the current content is durable in git
  history — either a fresh pre-install snapshot commit, or the file was
  already committed clean (the nightly /data safety-net commit got there
  first, which IS the snapshot). ok=False means durability could not be
  guaranteed (index.lock, dubious ownership, unborn HEAD, ...) and the
  caller must NOT overwrite the file.

  `--only` + the pathspec keeps the commit to this one file, so a racing
  `git add -A` (the nightly pm-commit) can't be swept into it and unrelated
  staged files stay staged.
  """
  env = {
    k: v for k, v in os.environ.items() if k not in _SNAPSHOT_GIT_ENV_DROP
  }
  # Explicit identity: the /data repo normally carries user.name from
  # entrypoint.sh, but a snapshot must not fail (and thereby block a skill
  # update) just because that config is missing.
  base = [
    "git", "-C", str(data_dir),
    "-c", "user.name=Mobius", "-c", "user.email=mobius@localhost",
  ]

  def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
      [*base, *args], capture_output=True, text=True, timeout=30, env=env,
    )

  def _reason(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[0] if lines else f"git exited {proc.returncode}"

  path = f"shared/skills/{rel}"
  status = _run("status", "--porcelain", "--", path)
  if status.returncode != 0:
    return False, _reason(status)
  if not status.stdout.strip():
    return True, "already committed"
  add = _run("add", "--", path)
  if add.returncode != 0:
    return False, _reason(add)
  commit = _run(
    "commit", "--only",
    "-m", f"pre-install snapshot of {rel} (app {slug} v{version})",
    "--", path,
  )
  if commit.returncode != 0:
    return False, _reason(commit)
  return True, "committed"


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
  if not skills:
    return
  if not app.source_dir:
    # A legacy no-source_dir app has no on-disk tree to read skill bytes
    # from — Path("") / rel would resolve against the server's CWD.
    warnings.append("skills: app has no source_dir — skipped")
    return
  data_dir = Path(get_settings().data_dir)
  skills_dir = data_dir / "shared" / "skills"
  source_dir = Path(app.source_dir)
  version = str(manifest.get("version", "unknown"))
  async with fs_locks.shared_skills_lock():
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
        owner = (
          db.query(models.App)
          .filter(
            models.App.id == owner_id, models.App.deleted_at.is_(None),
          )
          .first()
        )
        if owner is not None:
          warnings.append(
            f"skill {rel}: owned by app {owner.slug} — skipped"
          )
          continue
        # Recorded owner is gone (uninstalled past TTL or tombstoned) —
        # this app takes the file over through the normal cases below.
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
                _snapshot_shared_skill, data_dir, rel, app.slug, version,
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
  Static-asset dests are recorded as empty-content keys so a relative import
  onto one (``import logo from './static/x.png'``) resolves instead of tripping
  a false "missing import".
  """
  files: dict[str, str] = {
    rel: data.decode("utf-8", "replace") for rel, data in source_tree.items()
  }
  for dest in static_dests:
    files.setdefault(dest, "")

  result = check_app_source(
    files,
    entry=entry_key,
    source_files=manifest.get("source_files") or [],
    job=job_name,
    static_assets=list(static_dests),
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
  the per-app `upstream` branch — the entry, its declared sibling modules, and
  the schedule job script. The entry name the manifest declares is carried
  separately in `manifest["entry"]` so the caller can key the entry the way the
  recorded tree does (synthetic installs record it as "index.jsx"; a cloned
  repo keys it by its repo path)."""
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


async def install_from_manifest(
  db: Session,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
  source: str = "url",
) -> tuple[models.App, str, list[str], dict, list[str], str]:
  """Returns `(app, mode, warnings, manifest, conflict_paths, divergence)`.

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

  The per-app git model is unconditional for any app with a real
  source_dir. An app with no source_dir takes the legacy path — a blind
  jsx_source overwrite with no `.git` repo created — and 'conflict' never
  occurs there.

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
  if (manifest_url is None) == (manifest is None):
    raise HTTPException(
      400, "Provide exactly one of `manifest_url` or `manifest`.",
    )

  # --- Phase 1: fetch + validate manifest -----------------------------
  # follow_redirects=False — _http_get walks the chain manually so
  # every hop runs through _validate_url_safe (a 302 to a private IP
  # would otherwise bypass our pre-flight check).
  async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False) as cli:
    if manifest_url is not None:
      raw = await _http_get(cli, manifest_url, _MANIFEST_MAX_BYTES)
      try:
        manifest = json.loads(raw)
      except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Manifest is not valid JSON: {exc}")
      if raw_base is None:
        raw_base = _derive_raw_base(manifest_url)
    elif raw_base is None:
      raise HTTPException(
        400, "When passing inline `manifest`, also pass `raw_base`.",
      )

    _validate_manifest(manifest)
    raw_base = _normalize_raw_base(raw_base)

    # --- Phase 2: fetch entry JSX + bundled assets --------------------
    entry_bytes = await _http_get(
      cli, raw_base + manifest["entry"], _ENTRY_MAX_BYTES,
    )
    jsx_source = entry_bytes.decode("utf-8")

    icon_processed: bytes | None = None
    icon_warning: str | None = None
    if manifest.get("icon"):
      try:
        icon_raw = await _http_get(
          cli, raw_base + manifest["icon"], _ICON_MAX_BYTES,
        )
        icon_processed = _process_icon(icon_raw)
      except HTTPException as exc:
        # Icon is non-blocking — apps install fine with the auto
        # letter-icon. Surface as a warning, not a hard fail.
        icon_warning = f"icon: {exc.detail}"
        log.info("install: icon skipped — %s", exc.detail)

    bundled_job: bytes | None = None
    sched = manifest.get("schedule")
    if sched and sched.get("job"):
      bundled_job = await _http_get(
        cli, raw_base + sched["job"], _ENTRY_MAX_BYTES,
      )

    static_assets_fetched: dict[str, bytes] = {}
    static_assets_total = 0
    for dest, src in _static_asset_entries(
      manifest.get("static_assets") or {},
    ).items():
      if len(static_assets_fetched) >= _STATIC_ASSETS_COUNT_MAX:
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
      static_assets_fetched[dest] = data

    source_files_fetched: dict[str, bytes] = {}
    source_files_total = 0
    for rel in manifest.get("source_files") or []:
      data = await _http_get(cli, raw_base + rel, _ENTRY_MAX_BYTES)
      source_files_total += len(data)
      if source_files_total > _SOURCE_FILES_TOTAL_MAX:
        raise HTTPException(
          400,
          f"Manifest source_files exceed {_SOURCE_FILES_TOTAL_MAX} bytes total.",
        )
      source_files_fetched[rel] = data

    seeds_fetched: dict[str, bytes] = {}
    seeds_total = 0
    for sub, value in (manifest.get("storage_seeds") or {}).items():
      if len(seeds_fetched) >= _SEEDS_COUNT_MAX:
        raise HTTPException(
          400,
          f"Manifest has too many storage_seeds (max {_SEEDS_COUNT_MAX}).",
        )
      if _seed_value_is_inline(value):
        data = json.dumps(value).encode("utf-8")
      else:
        data = await _http_get(cli, raw_base + value, _SEED_MAX_BYTES)
      seeds_total += len(data)
      if seeds_total > _SEEDS_TOTAL_MAX:
        raise HTTPException(
          400,
          f"Manifest storage_seeds exceed {_SEEDS_TOTAL_MAX} bytes total.",
        )
      seeds_fetched[sub] = data

  # --- Phase 3: decide install vs update -------------------------------
  # Match by manifest_url, NOT by slug. Slug is now a routing concern
  # only — two apps (one user-built, one installed from a manifest)
  # may want the same slug stem, and allocate_unique_slug already
  # handles the collision by appending -2/-3/... Identity for "is
  # this the same app re-installed" is keyed on a canonical form of
  # the URL it came from. The same app installed via
  # `manifest_url=.../mobius.json` and via inline manifest +
  # `raw_base=...` would otherwise produce two distinct strings; the
  # canonicaliser folds both into `<base>#manifest-id=<id>` so
  # re-install reliably hits the update branch.
  manifest_id = manifest["id"]
  source_for_key = manifest_url if manifest_url is not None else raw_base
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
  if existing is None and _is_trusted_catalog_source(canonical_manifest_url):
    # SHAPE-DRIFT TOLERANCE (trusted catalog only). The SAME app can carry an
    # OLDER manifest_url string than today's canonical `<base>#manifest-id=<id>`:
    # install-core-apps / register_app wrote the raw `<base>/mobius.json`, and
    # very old rows wrote a bare `<base>`. Match those legacy shapes of the same
    # canonical BASE so a store update lands IN PLACE — the write path below
    # self-heals `app.manifest_url` to the canonical form — instead of forking a
    # duplicate row (the "app installed, not updated" dup that surfaced once core
    # apps became store-updatable).
    #
    # GATED on the trusted mobius-os catalog because this matches on BASE, not
    # manifest id: an UNTRUSTED install (esp. the inline-manifest path, where the
    # caller supplies both id and raw_base) pointing a DIFFERENT id at an
    # existing row's base would otherwise flip a fresh install into an in-place
    # OVERWRITE of that row before any adoption check runs. Within mobius-os one
    # repo is one base is one app, so a base match there is unambiguously the
    # same app, and the fetched code comes from that trusted base regardless.
    # Every affected legacy row (the core apps) is mobius-os, so the gate keeps
    # the fix while closing the overwrite path. Tombstone-agnostic like the
    # primary lookup, but prefer a LIVE row when a live + a soft-deleted legacy
    # row share the base.
    base = _canonical_base(canonical_manifest_url)
    existing = (
      db.query(models.App)
      .filter(
        models.App.manifest_url.in_([f"{base}/mobius.json", base, f"{base}/"])
      )
      .order_by(
        # live rows (deleted_at IS NULL) first, then lowest id
        case((models.App.deleted_at.is_(None), 0), else_=1),
        models.App.id.asc(),
      )
      .first()
    )
  # adopt_kind records HOW a predecessor was matched when the manifest_url
  # lookup missed — "" for a normal install/update (the canonical match above),
  # "rename" when this manifest renamed a prior catalog app (via `previous_id`),
  # "legacy" when it adopts a baked/register_app predecessor that carried no
  # catalog identity. The rename-migration block below keys off this so it only
  # moves a source tree on a genuine rename, never on a plain re-install.
  adopt_kind = ""
  if existing is None:
    # PREDECESSOR ADOPTION. The canonical manifest_url didn't match, but this
    # install may be the SUCCESSOR of an already-installed app — a rename, or a
    # catalog version of a predecessor that was baked in without a manifest_url.
    # Adopting that row (instead of minting a new one) keeps the numeric id —
    # and therefore all storage under /data/apps/<id> — and avoids a duplicate
    # drawer entry. Unlike the primary lookup (deliberately tombstone-agnostic
    # so a re-install REVIVES a soft-deleted app), adoption stays clear of
    # tombstones: silently resurrecting a deleted predecessor under a new
    # identity would be surprising, and the deleted row holds its slug until the
    # TTL purge anyway.
    # Both predecessor lookups are gated on the manifest DECLARING `previous_id`.
    # That declaration is the author's explicit "this install supersedes app
    # <previous_id>" intent — the only signal that distinguishes a deliberate
    # takeover from the accidental case the platform already tolerates: a
    # user-built app and a store app innocently sharing a slug stem coexist as
    # two rows (allocate_unique_slug suffixes the newcomer). Without the
    # declaration there is NO DB field separating a baked predecessor from a
    # user-built app at the same slug, so adopting on slug-match alone would
    # silently hijack the user's app — which `test_install_with_same_slug_
    # different_manifest_keeps_both` exists to forbid.
    prev_id = manifest.get("previous_id")
    # A manifest must not use `previous_id` to ADOPT (and thereby replace) a
    # platform/core app unless it comes from the trusted mobius-os catalog.
    # Otherwise an owner who pastes an untrusted manifest declaring
    # previous_id="memory" would take over Memory in place — inheriting its id
    # and stored data. Trusted-catalog renames still work; anything else falls
    # through to a normal install as a separate app rather than a hijack.
    if (
      prev_id in _RESERVED_PLATFORM_SLUGS
      and not _is_trusted_catalog_source(canonical_manifest_url)
    ):
      prev_id = None
    if prev_id:
      # RENAME: the predecessor came through the catalog under `previous_id`.
      # Match its canonical identity from the SAME base.
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
        adopt_kind = "rename"
      else:
        # LEGACY-SLUG: the predecessor was a baked/register_app app at slug
        # `previous_id` that never carried a catalog identity (manifest_url
        # NULL or ""). Adopt ONLY such no-identity rows — a real catalog
        # install at a coincidental slug keeps its own manifest_url and is
        # never hijacked. Matching on `previous_id` (not the new `manifest_id`)
        # keeps this an opt-in, author-declared takeover.
        existing = (
          db.query(models.App)
          .filter(
            models.App.slug == prev_id,
            models.App.deleted_at.is_(None),
            (models.App.manifest_url.is_(None))
            | (models.App.manifest_url == ""),
          )
          .first()
        )
        if existing:
          adopt_kind = "legacy"
  settings_data_dir = get_settings().data_dir
  if _is_trusted_legacy_platform_catalog_install(
    existing, manifest_id, canonical_manifest_url, settings_data_dir,
  ):
    adopt_kind = "legacy-platform"
  if (
    existing is None
    and manifest_id in HISTORICAL_PLATFORM_APP_SLUGS
    and _is_trusted_catalog_source(canonical_manifest_url)
  ):
    platform_row = (
      db.query(models.App)
      .filter(
        models.App.slug == manifest_id,
        models.App.deleted_at.is_(None),
        (models.App.manifest_url.is_(None))
        | (models.App.manifest_url == ""),
      )
      .first()
    )
    if platform_row:
      if _is_legacy_platform_source_dir(
        platform_row.source_dir, settings_data_dir, manifest_id,
      ):
        existing = platform_row
        adopt_kind = "legacy-platform"
      elif _is_historical_platform_app_source_dir(
        platform_row.source_dir, platform_row.manifest_url,
        settings_data_dir, manifest_id,
      ):
        existing = platform_row
        adopt_kind = "legacy-catalog"
  mode = "update" if existing else "install"
  legacy_platform_migration = adopt_kind == "legacy-platform"
  if (
    existing
    and _is_legacy_platform_source_dir(
      existing.source_dir, settings_data_dir, existing.slug,
    )
    and not legacy_platform_migration
  ):
    raise HTTPException(
      409,
      (
        "This legacy platform-owned app must be migrated by installing its "
        "trusted mobius-os catalog entry."
      ),
    )

  warnings: list[str] = []
  conflict_paths: list[str] = []
  divergence: str = "none"
  # Set when upstream content is actually folded into the served `main`
  # branch (a clean merge, or a forced take-upstream). The post-write
  # commit then replays the result on the upstream tip as its sole parent
  # (linear history) so the merge base advances — otherwise every later
  # update re-merges from the install point and conflicts spuriously. None
  # means a plain local commit (fresh install, or a conflict that left local
  # untouched).
  merge_applied = False
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
  # The `source_tree` key that carries the entry's bytes — and, on every path
  # that writes or compiles from disk, the entry's on-disk name. Fetched/
  # synthetic trees always key the entry as the root "index.jsx" they
  # materialize (whatever the manifest calls it); a real origin-backed tree
  # (cloned install or update) keys files by their repo names, so there the
  # entry sits under manifest["entry"]. The clone branches below reassign it.
  entry_key = "index.jsx"
  # True once `source_tree` is the MERGED tree (a clean merge or forced
  # take-upstream): the post-write commit then replays the result on the
  # upstream tip as its sole parent so the merge base advances — otherwise
  # every later update re-merges from the install point and conflicts
  # spuriously. False means a plain local commit (fresh install, or a conflict
  # that left local untouched).

  # --- Phase 4: materialize. Wrapped so cleanup runs on any failure. --
  # `created_paths`: files/dirs to delete on failure (and leave on
  # success). `cleanup_actions`: callables run on the success path
  # (commit) OR rollback path (revert) — used for backup-rename
  # rollback on the update path's compiled bundle, so a failed
  # recompile restores the previous good bundle on disk to match the
  # DB row that rolled back.
  created_paths: list[Path] = []
  rollback_actions: list[Callable[[], None]] = []
  commit_actions: list[Callable[[], None]] = []
  data_dir = Path(get_settings().data_dir)
  perms = manifest.get("permissions") or {}

  try:
    if existing:
      app = existing
      # Reinstalling a tombstoned app REVIVES it: the manifest_url match finds
      # the soft-deleted row (the query is deleted_at-agnostic on purpose), and
      # clearing deleted_at reattaches the SAME id + its preserved storage tree
      # instead of minting a fresh empty app. No-op for a normal update. The
      # recompile + cron re-register + updated_at bump happen on this path too,
      # so a revived store app comes back fully wired (feature 110).
      app.deleted_at = None
      app.name = manifest["name"]
      app.description = manifest.get("description", "")
      # jsx_source AND the capability/offline fields are assigned AFTER the
      # git merge decision below and AFTER the conflict short-circuit returns.
      # On a conflict we keep the local edits and keep serving the OLD code,
      # so we must not stamp the new manifest's source — nor its capabilities
      # or offline semantics — onto a row whose running code is still the old
      # version. Doing so would, e.g., grant manage_apps install authority to
      # unreviewed old code, or flip offline_capable away from what the
      # running code's service-worker logic expects.
      db.flush()
      if adopt_kind == "legacy-platform":
        old_source_dir = app.source_dir
        target_slug = manifest_id
        target_source_dir = str(data_dir / "apps" / target_slug)
        try:
          _reject_if_source_dir_taken(
            db, target_source_dir, exclude_id=app.id,
          )
        except HTTPException:
          target_slug = allocate_unique_slug(db, manifest["name"])
          target_source_dir = str(data_dir / "apps" / target_slug)
        if not Path(target_source_dir).exists():
          created_paths.append(Path(target_source_dir))
        if old_source_dir:
          async with fs_locks.source_dir_lock(old_source_dir):
            await asyncio.to_thread(_unregister_cron, Path(old_source_dir))
        app.slug = target_slug
        app.source_dir = target_source_dir
        app.manifest_url = canonical_manifest_url
        db.flush()
      elif adopt_kind == "legacy-catalog":
        # A pre-git-model catalog app: editable source already sits at
        # /data/apps/<slug> (manifest_id == app.slug on this path, so no
        # slug/source_dir move), only the canonical identity is missing. Stamp it
        # NOW, before the git merge below can short-circuit on a conflict. The
        # post-merge re-stamp is skipped on conflict, so deferring to it would
        # leave a conflicting migration with a NULL manifest_url — the boot
        # migration would then keep re-firing (and re-fetching) every restart
        # instead of running once. The identity move is orthogonal to the source:
        # the owner still resolves the surfaced conflict through the click-gated
        # resolver, and the served source/bundle stay theirs until they do.
        app.manifest_url = canonical_manifest_url
        db.flush()

      # Identity migration after an ADOPTION (rename or legacy). The adopted row
      # still carries the predecessor's slug + on-disk source tree, but its new
      # identity is `manifest_id` — move the tree to the new id's path and
      # re-stamp the row so the app is fully consistent. The watcher resolves
      # source_dir from this row, and the per-app .git rides along inside the
      # directory, so an atomic same-filesystem os.rename keeps git history +
      # working tree intact. The numeric id (and thus /data/apps/<id> storage)
      # is never touched. Done BEFORE the git block + source-write + cron phases
      # so the rest of the install writes to and registers cron at the NEW path.
      # A plain re-install (adopt_kind == "") never enters here.
      if adopt_kind and adopt_kind != "legacy-platform" and manifest_id != app.slug:
        old_source_dir = app.source_dir
        target_slug = manifest_id
        target_source_dir = str(data_dir / "apps" / target_slug)
        moved = False
        if old_source_dir and Path(old_source_dir).is_dir():
          async with fs_locks.source_dir_lock(old_source_dir):
            try:
              # Reject the move if the target path is already another app's
              # source tree — adopting a rename must never stomp a coexisting
              # app. The lock above + this check are atomic against a concurrent
              # create/patch claiming target_source_dir.
              _reject_if_source_dir_taken(
                db, target_source_dir, exclude_id=app.id,
              )
              target_taken = False
            except HTTPException:
              target_taken = True
            if not target_taken and not Path(target_source_dir).exists():
              # Drop the predecessor's crontab entry at the OLD path first; the
              # source-write + cron phases below re-register at the new path.
              _unregister_cron(Path(old_source_dir))
              os.rename(old_source_dir, target_source_dir)
              moved = True
              # Re-establish the predecessor's crontab at the restored old path
              # if the move is rolled back, otherwise a later-phase failure
              # leaves the source tree back at the old path but its cron entry
              # gone until the next reboot. Rollback actions run in REVERSE
              # append order, so this is appended BEFORE the move-reversal below
              # to run AFTER the dir is back at old_source_dir. For a
              # non-scheduled app (no init-cron.sh) it's a no-op.
              rollback_actions.append(
                lambda o=old_source_dir:
                  subprocess.run(
                    ["bash", str(Path(o) / "init-cron.sh")],
                    timeout=10, check=False,
                  ) if (Path(o) / "init-cron.sh").exists() else None
              )
              # Reverse the move on rollback so a later-phase failure doesn't
              # leave a half-renamed app. Registered before slug/source_dir are
              # re-stamped on the row, which roll back with the DB transaction.
              rollback_actions.append(
                lambda o=old_source_dir, n=target_source_dir:
                  os.rename(n, o) if Path(n).is_dir()
                  and not Path(o).exists() else None
              )
        if moved:
          app.slug = target_slug
          app.source_dir = target_source_dir
          # Re-stamp the canonical identity here, INSIDE the adoption block, so
          # the row's three identity fields (slug, source_dir, manifest_url) move
          # together regardless of which update branch runs below. The later
          # restamp at the end of the existing-update path is past the conflict
          # short-circuit return — a rename that hits a git conflict would
          # otherwise keep the predecessor's OLD url while carrying the new
          # slug/source_dir, so the next install of the new id would miss the
          # manifest_url match and mint a duplicate. The later assignment stays
          # (idempotent) for the non-conflict paths.
          app.manifest_url = canonical_manifest_url
          db.flush()
        else:
          # Target slug/dir taken (or the old tree is missing): keep the old
          # slug + dir and surface the skipped rename. The update still lands
          # on the SAME row, so there's still no duplicate — only the on-disk
          # identity stays at the old name.
          if old_source_dir and Path(old_source_dir).is_dir():
            warnings.append(
              f"could not rename slug {app.slug}->{manifest_id}: "
              "target in use"
            )
    else:
      # Identity by manifest_url means we're now genuinely in the
      # install branch — but slug is a separate concern. The user
      # may already own an app whose slug stem happens to match
      # manifest.id (most commonly: they built one, then the store
      # ships an "official" one with the same id). allocate_unique_slug
      # appends -2/-3/... so both rows coexist; the partner sees both
      # in the drawer and picks the one they want.
      slug = manifest_id
      taken = db.query(models.App).filter(models.App.slug == slug).first()
      if taken:
        slug = allocate_unique_slug(db, manifest["name"])
        # Non-behavioral telemetry: the install still succeeds under the
        # suffixed slug, but a collision means two apps now share a stem
        # (user-built vs store, or two store apps). Logging requested-vs-
        # assigned makes that observable to the reflection agent / store UI
        # without a DB rename. Best-effort like every activity emit.
        activity.log_event(
          "slug_collision",
          requested_slug=manifest_id,
          assigned_slug=slug,
          source=source,
        )
      source_dir = str(data_dir / "apps" / slug)
      created_paths.append(Path(source_dir))
      app = models.App(
        name=manifest["name"],
        description=manifest.get("description", ""),
        jsx_source=jsx_source,
        source_dir=source_dir,
        slug=slug,
        manifest_url=canonical_manifest_url,
        cross_app_access=perms.get("cross_app_access", "none"),
        share_with_apps=perms.get("share_with_apps", "none"),
        chat_log_access=perms.get("chat_log_access", "none"),
        manage_apps=bool(perms.get("manage_apps", False)),
        github_access=bool(perms.get("github_access", False)),
        filesystem_access=bool(perms.get("filesystem_access", False)),
        # The manifest's `offline_capable: true` opts the app into the
        # SW frame cache + the window.mobius.storage outbox. Without
        # this line every installed app defaulted to offline_capable=
        # false on the App row regardless of what the manifest declared
        # — apps that paid the cost of being offline-ready in code
        # didn't actually behave offline-ready end-to-end.
        offline_capable=bool(manifest.get("offline_capable", False)),
        embeds_agent=bool(manifest.get("embeds_agent", False)),
        # P1-D: persist the offline contract block (None when not declared).
        offline_contract=manifest.get("offline") or None,
      )
      db.add(app)
      db.flush()  # assign app.id without committing yet

    # --- Per-app git: record upstream + (on update) merge into local ---
    # Engaged whenever the app has a real source_dir. The merge decision AND the
    # disk write below run under ONE held span of source_dir_lock — not two
    # separate critical sections — so the file watcher (which takes the same lock
    # before its own commit_local) cannot commit an agent edit in the gap and
    # have the write then clobber it (the edit would be lost from the live tree,
    # the bundle, and app.jsx_source, recoverable only from git history). We do
    # the merge BEFORE the compile so `source_tree` (which the compile + write
    # below consume) reflects the merged tree on a clean update, and so a
    # conflict can short-circuit both. The lock is released before the seeds
    # block takes app_storage_lock, preserving the documented acquisition order
    # (install_uninstall -> app_storage -> source_dir).
    # Guard on the raw string, NOT the Path: Path("") is a truthy Path, so a
    # row with an empty source_dir would otherwise initialize a git repo in
    # the server's working directory. Only engage git when there's a real dir.
    git_source_dir = Path(app.source_dir) if app.source_dir else None
    source_lock = (
      fs_locks.source_dir_lock(str(git_source_dir)) if git_source_dir else None
    )
    if source_lock is not None:
      await source_lock.acquire()
    try:
      if git_source_dir:
        version = str(manifest.get("version", "unknown"))
        had_repo = app_git.is_repo(git_source_dir)
        # A pre-git-model app being adopted from the catalog — or any existing app
        # that somehow lost its repo — has editable owner source on disk and in DB
        # jsx_source but no per-app git history and no recorded upstream. Init the
        # repo now so the merge path below captures those on-disk edits onto `main`
        # BEFORE the catalog upstream is recorded. Without this the fresh-install
        # branch's align_local_to_upstream would reset --hard the tree to catalog
        # bytes and the owner's edits would land in ZERO git blobs — unrecoverable.
        # Excludes legacy-platform rows: their real source lived under
        # /data/platform/core-apps and the /data/apps/<slug> dir is only the
        # retired cron sidecar, not owner source to preserve.
        adopt_repoless_source = bool(
          existing and not had_repo and not legacy_platform_migration
        )
        if adopt_repoless_source:
          await asyncio.to_thread(app_git.ensure_repo, git_source_dir)
        merge_existing_source = existing and not legacy_platform_migration and (
          had_repo or adopt_repoless_source
        )
        if merge_existing_source:
          prev_upstream_commit = app.upstream_commit
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
          await asyncio.to_thread(
            app_git.abort_in_progress_merge, git_source_dir,
          )
          # Update of an app already on the git model. First capture any
          # uncommitted on-disk local edits onto `main` (the watcher may
          # not have committed the agent's latest save yet) so the
          # divergence check and any merge see the real local source.
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
          if repo_ref is not None and await asyncio.to_thread(
            app_git.has_origin, git_source_dir,
          ):
            _, ref = repo_ref
            try:
              app.upstream_commit = await asyncio.to_thread(
                app_git.fetch_upstream, git_source_dir, ref,
              )
              cloned_update = True
              # Real origin trees key files by their repo names — the entry
              # lives under the manifest's own name, not a synthetic root
              # index.jsx.
              entry_key = manifest["entry"]
            except Exception as exc:
              log.warning(
                "install: fetch from origin at %s failed; falling back to "
                "fetched source path — %r",
                ref, exc,
              )
          if not cloned_update:
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
            if entry_key not in source_tree:
              mode = "conflict"
              conflict_paths = [entry_key]
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
            if merge.status == "conflict":
              if force_core_store_update:
                # Core App Store self-update: published upstream wins, keep the
                # fetched `source_tree` and apply it like a fast-forward. The
                # fetched tree keys its entry as the root index.jsx, so the
                # entry key reverts with it (a cloned fetch above may have
                # pointed it at the repo's own entry name).
                warnings.append(
                  "core App Store self-update replaced local edits with upstream"
                )
                divergence = "fast_forward"
                merge_applied = True
                entry_key = "index.jsx"
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
                # Read exec bits only now that we WILL write this tree — a
                # conflict/unreadable verdict never ls-tree's the (possibly
                # degenerate) merged oid.
                git_exec_paths = await asyncio.to_thread(
                  app_git.read_tree_exec_paths,
                  git_source_dir, merge.merged_tree_oid,
                )
        else:
          # Fresh installs and legacy-platform migrations both build local
          # `main` from the catalog tree. For legacy platform rows, an existing
          # /data/apps/<slug> repo is only the old runtime sidecar; source lived
          # under /data/platform/core-apps/<slug>, so do not merge its stale
          # source state as user edits.
          # Fresh install (or an existing app that somehow lost its repo):
          # for a new raw-GitHub catalog install, prefer a REAL clone so the
          # source tree carries origin/<ref> and the app's own .gitignore. If
          # cloning fails (private repo, renamed repo, offline git access), fall
          # back to the existing synthetic-upstream path unchanged. The entry
          # is read back from the clone under whatever name the manifest
          # declares — the repo's bytes, not the HTTP fetch, are canonical on
          # this path. `source_tree` keys it by that same repo name and
          # `entry_key` tracks it, so every downstream consumer (entry_source,
          # the compile) reads the right file and no synthetic root index.jsx
          # is ever materialized for a cloned tree (the write loop is skipped;
          # the on-disk file keeps the repo's name).
          if not existing and repo_ref is not None:
            repo_url, ref = repo_ref
            try:
              app.upstream_commit = await asyncio.to_thread(
                app_git.clone_upstream, git_source_dir, repo_url, ref,
              )
              entry_bytes = (git_source_dir / manifest["entry"]).read_bytes()
              jsx_source = entry_bytes.decode("utf-8")
              upstream_jsx_sha = hashlib.sha256(entry_bytes).hexdigest()
              source_tree = {manifest["entry"]: entry_bytes}
              entry_key = manifest["entry"]
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
          pass

      # The disk-write phase runs INSIDE the same held lock for the git path so
      # no watcher commit interleaves between the merge decision and the write; a
      # conflict skips it (the source stays the local edits, served by the prior
      # bundle). The no-source_dir legacy path falls through with no lock.
      if mode != "conflict":
        # Stamp the installed version on the row now that we know the source is
        # actually being applied (the conflict path skips this with the old
        # version intact). This is what GET /api/apps/ exposes, so the store and
        # any out-of-band caller read the installed version without a side-map.
        app.version = str(manifest.get("version", "")).strip() or None
        app.theme_color = _manifest_color(manifest.get("theme_color"))
        app.background_color = _manifest_color(manifest.get("background_color")) or app.theme_color
        app.display = _manifest_display(manifest.get("display"))

        # `app.jsx_source` mirrors the entry the tree carries (the merged
        # entry on a clean update, the upstream bytes otherwise).
        entry_source = source_tree[entry_key].decode("utf-8")
        if existing:
          # Apply the (possibly merged) source AND the new manifest's capability /
          # offline fields now that the merge decision is made and the conflict
          # short-circuit above has been skipped. Deferring these past the
          # conflict skip keeps a served-old-code conflict from jumping
          # capabilities or offline semantics ahead of the code actually running.
          # Without local divergence (or for an app with no source_dir),
          # the entry is just the upstream bytes.
          app.jsx_source = entry_source
          # Re-stamp the canonical identity. A no-op for a plain re-install (the
          # row already matched on this exact value), but LOAD-BEARING for an
          # adopted predecessor: a rename carried the predecessor's OLD canonical
          # url and a legacy adoption carried NULL/"" — without this, the next
          # install of the new id would miss the manifest_url match and mint a
          # duplicate, defeating the adoption. Deferred past the conflict skip so
          # a served-old-code conflict keeps its old provenance.
          app.manifest_url = canonical_manifest_url
          app.cross_app_access = perms.get("cross_app_access", app.cross_app_access)
          app.share_with_apps = perms.get("share_with_apps", app.share_with_apps)
          app.chat_log_access = perms.get("chat_log_access", app.chat_log_access)
          # Privileged booleans and offline_capable can change across
          # versions; default to the existing value when the manifest omits the key.
          if "manage_apps" in perms:
            app.manage_apps = bool(perms["manage_apps"])
          if "github_access" in perms:
            app.github_access = bool(perms["github_access"])
          # Filesystem authority is opt-in on every published version. Omitting
          # the grant from an update revokes it instead of preserving a stale
          # privileged bit from an older manifest.
          app.filesystem_access = bool(perms.get("filesystem_access", False))
          if "offline_capable" in manifest:
            app.offline_capable = bool(manifest["offline_capable"])
          if "embeds_agent" in manifest:
            app.embeds_agent = bool(manifest["embeds_agent"])
          # P1-D: persist the offline contract block (replaces on update to match
          # the new manifest; None if the key is absent in the new manifest).
          app.offline_contract = manifest.get("offline") or None

        # The compiled bundle is written OUT OF PLACE to a staging file and
        # promoted into the live bundle only AFTER the DB commit (commit_actions
        # run post-commit). So a concurrent module read never observes a missing
        # or half-written live bundle mid-update, and a rollback or crash
        # discards the staging file, leaving the prior bundle intact (a leaked
        # staging file is reaped at startup and is never served). The actual
        # compile happens below, AFTER the source files are on disk, so esbuild
        # can bundle a multi-file app's sibling imports from the real source tree
        # — a syntax error there raises and the outer except rolls everything
        # back. Same transactional shape the recompile PATCH and the watcher use.
        live_bundle = data_dir / "compiled" / f"app-{app.id}.js"
        staged_bundle = data_dir / "compiled" / f"app-{app.id}.js.staging"
        app.compiled_path = str(live_bundle)

        # Guard on the raw string, not the Path: Path("") is PosixPath('.'),
        # which is truthy — a legacy sourceless app (source_dir NULL/"") would
        # slip into this branch and write index.jsx into the process cwd.
        source_dir_path = Path(app.source_dir) if app.source_dir else None
        if source_dir_path is not None:
          # The per-source-dir lock is ALREADY held (acquired above for the merge
          # decision and kept across this write), so a watcher commit can't
          # interleave and the merge result we computed is exactly what lands.
          # Every file is written atomically, and on an UPDATE the prior copy is
          # snapshotted to a .bak so a later rollback restores it — otherwise the
          # watcher would compile the rolled-back (broken) update. A multi-file
          # app's siblings must be on disk before esbuild bundles them.
          _reject_if_source_dir_taken(
            db, str(source_dir_path), exclude_id=app.id
          )
          source_dir_path.mkdir(parents=True, exist_ok=True)
          jsx_file = source_dir_path / "index.jsx"
          # Write the WHOLE source tree: index.jsx + every sibling + the job
          # script, all just keys. index.jsx keeps its historical
          # `index.jsx.bak` snapshot name (so the existing rollback expectations
          # hold); every other file uses `<name>.bak`. Nested paths get their
          # parent dirs created first; the job script is staged executable.
          if not cloned_install:
            for rel, content in source_tree.items():
              target = source_dir_path / rel
              if rel == "index.jsx":
                backup = jsx_file.with_suffix(".jsx.bak")
              else:
                backup = target.with_name(target.name + ".bak")
              # Create parent dirs first so the realpath check sees the actual
              # on-disk shape (a symlinked existing parent resolves to its
              # target), then reject any write whose resolved path escapes the
              # source dir.
              target.parent.mkdir(parents=True, exist_ok=True)
              _assert_within(source_dir_path, target, f"source_files {rel}")
              _write_source_file(
                target, content, backup,
                created_paths, rollback_actions, commit_actions,
              )
              if rel in exec_paths or rel in git_exec_paths:
                target.chmod(0o755)
            # Reconcile the worktree to the source tree: a file the new version
            # dropped (a sibling, an old job) must be deleted, not left on disk
            # to be re-recorded onto `main` as permanent local divergence. The
            # explicit upstream-diff delete set keeps local-only tracked source
            # files out of prune's reach.
            _prune_dropped_source_files(
              source_dir_path, dropped_source_paths,
              rollback_actions, commit_actions,
            )
            # On the synthetic-fetch path the on-disk tree is EXACTLY what the
            # manifest declared (entry + source_files + job), so an entry that
            # imports an undeclared sibling ships an incomplete tree that can't
            # load — the Editor launch bug. Reject it here with a precise 422
            # (esbuild would too, but with a cryptic "Could not resolve"). A
            # git-origin-backed tree (`cloned_install` fresh clone, or
            # `cloned_update` origin fetch) is skipped: it carries the whole
            # repo, complete by construction, so source_files completeness is
            # moot for it — the standalone CLI (scripts/validate-app.py) is the
            # pre-push gate that holds a cloned app's manifest to the same bar
            # for OTHER install paths. Errors raise HTTPException(422) → the
            # outer handler rolls the source writes back.
            if not cloned_update:
              _check_source_completeness(
                app_name=str(manifest.get("name") or app.slug),
                manifest=manifest,
                source_tree=source_tree,
                entry_key=entry_key,
                static_dests=list(static_assets_fetched.keys()),
                job_name=job_name,
              )
          # Compile now that the whole source tree is on disk. Passing the real
          # entry path makes esbuild resolve `./cards.js`-style sibling imports
          # from the files just written; promotion of the staged bundle into the
          # live path is a post-commit commit_action, and a compile failure here
          # raises into the outer except which runs the source rollback actions
          # appended above (restoring every .bak). `entry_key` is the entry's
          # on-disk name on every path that reaches here: synthetic trees
          # write it as the root index.jsx, cloned trees keep the repo's own
          # filename (a cloned install skipped the write loop entirely).
          await compile_jsx(
            app.id, entry_source,
            out_path=staged_bundle, source_path=source_dir_path / entry_key,
          )
          rollback_actions.append(
            lambda s=staged_bundle: s.unlink() if s.exists() else None
          )
          commit_actions.append(
            lambda s=staged_bundle, l=live_bundle: os.replace(s, l)
          )
          _write_static_assets(
            source_dir_path,
            static_assets_fetched,
            created_paths,
            rollback_actions,
            commit_actions,
          )
          # On the git path, commit the working-tree source onto the local
          # `main` branch so the watcher's future commits build on a known base.
          # When this update folded upstream into the served source, record it
          # as a single-parent replay on the upstream tip (commit_replay) so the
          # merge base advances and history stays linear — otherwise the NEXT
          # update re-merges from the install point and conflicts spuriously
          # even on disjoint changes. A plain local commit otherwise (fresh
          # install, or a conflict that left local untouched). No-op when the
          # source is unchanged.
          if git_source_dir:
            commit_msg = (
              f"install: {manifest.get('name', app.slug)} "
              f"v{manifest.get('version', 'unknown')}"
            )
            if merge_applied and app.upstream_commit:
              await asyncio.to_thread(
                app_git.commit_replay, source_dir_path,
                app.upstream_commit, commit_msg,
              )
            else:
              await asyncio.to_thread(
                app_git.commit_local, source_dir_path, commit_msg,
              )
        else:
          # No source_dir (legacy app): there is no sibling tree on disk, so
          # compile the bare entry string with no source_path — esbuild writes it
          # to a temp file and bundles that. The staged bundle still promotes
          # into the live path post-commit.
          await compile_jsx(app.id, entry_source, out_path=staged_bundle)
          rollback_actions.append(
            lambda s=staged_bundle: s.unlink() if s.exists() else None
          )
          commit_actions.append(
            lambda s=staged_bundle, l=live_bundle: os.replace(s, l)
          )
    finally:
      # Release the per-source-dir lock (held across the merge + write for the
      # git path) BEFORE the seeds block takes app_storage_lock, preserving the
      # documented acquisition order. A no-op for the no-source_dir legacy path.
      if source_lock is not None:
        source_lock.release()

    if mode == "conflict":
      # Commit the recorded upstream provenance + return so the App Store can
      # surface a click-gated resolver. The served source/bundle stay the prior
      # good ones until the owner chooses Resolve in chat.
      db.commit()
      db.refresh(app)
      activity.log_event(
        "app_install", app_id=app.id, slug=app.slug, source=source,
      )
      return app, mode, warnings, manifest, conflict_paths, divergence

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
        created_paths.append(target)

    # Icon — re-apply on update so a version bump's new icon lands.
    if icon_processed:
      app.icon_png = icon_processed

    # COMMIT FIRST — once the DB row is durable, cron registration
    # is a non-fatal "best effort" step. Doing cron BEFORE commit
    # could leave a crontab entry firing for a row that rolled back
    # (orphaned cron, mysterious 'app not found' errors at runtime).
    db.commit()
    db.refresh(app)

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
    for action in commit_actions:
      try:
        action()
      except OSError as exc:
        log.warning("install: post-commit cleanup failed — %s", exc)

  except CompileError as exc:
    app_name = str(
      manifest.get("name") or getattr(locals().get("app"), "slug", "app")
    )
    db.rollback()
    _run_rollback_actions(rollback_actions)
    _cleanup(created_paths)
    raise HTTPException(422, _compile_error_detail(app_name, exc))
  except HTTPException:
    db.rollback()
    _run_rollback_actions(rollback_actions)
    _cleanup(created_paths)
    raise
  except Exception:
    # Catch-all so a stray bug doesn't leak partial state or raw exception
    # reprs. The traceback is logged server-side for operators.
    log.exception("install: unexpected failure during materialize")
    db.rollback()
    _run_rollback_actions(rollback_actions)
    _cleanup(created_paths)
    raise HTTPException(
      500, "Install failed due to an unexpected server error.",
    )

  # --- Phase 5: post-commit cron registration -------------------------
  # The app is fully installed at this point. Cron failures become
  # warnings, not 500s — the user just needs to re-set the schedule.
  #
  # The job script (manifest `schedule.job`) is ALREADY on disk: it was
  # written in the transactional source write above, INDEPENDENT of whether
  # the manifest also declares a recurring `schedule.default`. An app may
  # ship an on-demand job invoked only through the run-job endpoint (e.g. the
  # LaTeX app's build.sh, compiled on a Build click) with no recurring
  # schedule, and run-job needs the script on disk to find it. A recurring
  # crontab entry is installed here only when `schedule.default` is present.
  # `job_name` was derived once before the git block; reuse it so the cron
  # entry points at the same file the source write produced. A manifest may
  # declare `schedule.default` without a `schedule.job`; fall back to the
  # scaffold's own default basename so the crontab entry is still well-formed
  # (it points at a stub the scaffold writes when no job was shipped).
  cron_job_name = job_name or "job.sh"
  has_cron = bool(sched and sched.get("default"))
  # An UPDATE must CONVERGE cron state, not just add to it: the prior install
  # may have registered a crontab line this new manifest no longer wants
  # (recurring → on-demand migration). So on update we unconditionally drop
  # the existing entry first, then re-register below only if the new manifest
  # still declares one. A fresh install has nothing to drop. (Card 099.)
  # Guard on source_dir like the git block above: a legacy no-source_dir app
  # never had a per-app dir or a crontab line to converge, so forcing the cron
  # block on its update would only materialize a stray empty /data/apps/<slug>/
  # via the app_data_dir.mkdir below (card 099, stray-dir follow-up).
  drop_prior_cron = mode == "update" and bool(app.source_dir)
  if bundled_job or has_cron or drop_prior_cron:
    slug = app.slug
    # Use the app's ACTUAL source_dir (where the JSX + job script live), not a
    # freshly re-derived /data/apps/<slug>. After a valid source-dir PATCH the
    # two diverge, which would split the job file from the source tree (Codex
    # review round-10 #7). `slug` stays the cron job identifier.
    app_data_dir = Path(app.source_dir) if app.source_dir else data_dir / "apps" / slug
    try:
      # Under the per-source-dir lock so the job-file writes serialize vs a
      # concurrent create/patch claiming this directory, and recheck the app
      # row still exists first (the endpoint's lifecycle lock already excludes a
      # concurrent uninstall, but the recheck makes the write never happen for a
      # vanished row) — Codex review round-9 #3.
      async with fs_locks.source_dir_lock(str(app_data_dir)):
        if not db.query(models.App.id).filter(models.App.id == app.id).first():
          raise HTTPException(404, "App removed before cron registration.")
        app_data_dir.mkdir(parents=True, exist_ok=True)
        # Drop any prior crontab entry + init-cron.sh BEFORE re-registering, so
        # the net effect matches the new manifest. The scaffold's own rewrite is
        # idempotent, but it never removes a line the new manifest dropped — that
        # is exactly the orphan this clears. Off the event loop (shells out).
        if drop_prior_cron:
          await asyncio.to_thread(_drop_app_cron, app_data_dir)
        job_path = app_data_dir / cron_job_name
        # A cloned install/update materializes the job script with the repo's
        # own mode — the installer never chmods tracked clone files (a mode
        # change would read as permanent local divergence vs origin). A repo
        # carrying the job as 100644 therefore lands non-executable, and the
        # crontab/run-job exec fails silently at fire time; surface it now.
        # The durable fix is repo-side (commit the +x bit). The synthetic
        # write loop chmods declared jobs 0o755, so this never fires there.
        if (
          job_name
          and job_path.exists()
          and not os.access(job_path, os.X_OK)
        ):
          warnings.append(
            f"schedule.job {cron_job_name} is not executable — cron/run-job "
            "will fail until the app repo commits the executable bit"
          )
        if has_cron and CRON_SCAFFOLD.exists():
          # The job script is already on disk; the scaffold preserves it and
          # installs the crontab entry pointing at it.
          await asyncio.to_thread(
            _register_cron,
            slug, sched["default"], job_path, app.id,
          )
        else:
          # Either an on-demand-only job (no recurring schedule) or, when a
          # schedule IS declared, a test env that mocks the scaffold away. The
          # job script already landed in the transactional source write, so
          # run-job can find it either way.
          if has_cron:
            # Schedule declared but the scaffold isn't on PATH (tests):
            # persist a sentinel so the contract is observable + warn.
            sentinel = app_data_dir / ".cron-pending.json"
            sentinel.write_text(json.dumps({
              "expr": sched["default"], "job": cron_job_name,
              "status": "pending — init-cron-scaffold.sh not on PATH",
            }), encoding="utf-8")
            warnings.append(
              "cron: scaffold script not available — registration pending"
            )
    except HTTPException as exc:
      # The write/cron failed but the app is installed. Surface as a warning.
      log.warning("install: job-script/cron step failed post-commit — %s",
                  exc.detail)
      warnings.append(f"cron: registration failed — {exc.detail}")
    except Exception as exc:
      log.exception("install: job-script/cron step failed post-commit")
      warnings.append(f"cron: registration failed — {exc!r}")

  # --- Phase 6: post-commit shared-skill materialization ---------------
  # Same best-effort contract as cron: the app is durably installed, so a
  # skill failure is a warning, never a 500. A conflicting update returned
  # earlier, so new-version skills are never installed while old code is
  # still being served.
  try:
    await _sync_app_skills(db, app, manifest, warnings)
  except Exception as exc:
    log.exception("install: skill sync failed post-commit")
    warnings.append(f"skills: sync failed — {exc!r}")

  return app, mode, warnings, manifest, conflict_paths, divergence


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
