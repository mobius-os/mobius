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
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import warnings as _warnings_mod
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException
from PIL import Image as _PILImage
from sqlalchemy.orm import Session

from app import models
from app.compiler import compile_jsx
from app.config import get_settings
from app.routes.apps import (
  _derive_source_dir, _slugify_for_source_dir, allocate_unique_slug,
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

# Networks the install fetcher must never reach. Hitting them from
# our (network-privileged) backend turns the install endpoint into
# an SSRF springboard: a malicious manifest URL could probe the
# container's own loopback (own API, metrics), Docker bridge
# (sibling services), cloud-provider metadata (169.254.169.254 →
# IAM credentials on AWS / GCP / Azure), or any other internal
# resource the container can reach.
_BLOCKED_NETS = [
  ipaddress.ip_network("0.0.0.0/8"),
  ipaddress.ip_network("10.0.0.0/8"),
  ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
  ipaddress.ip_network("127.0.0.0/8"),
  ipaddress.ip_network("169.254.0.0/16"),    # link-local + cloud metadata
  ipaddress.ip_network("172.16.0.0/12"),
  ipaddress.ip_network("192.168.0.0/16"),
  ipaddress.ip_network("::1/128"),
  ipaddress.ip_network("fc00::/7"),          # ULA
  ipaddress.ip_network("fe80::/10"),         # link-local IPv6
]

# Cron field grammar: minute hour dom month dow, allowing the
# standard wildcards / ranges / lists / step values. Deliberately
# rejects every shell metacharacter — no `;`, no `$`, no backtick,
# no quotes. Per-field count check below enforces 5 columns.
_CRON_FIELD_OK = re.compile(r"^[\d\*/,\- ]+$")

_REQUIRED_FIELDS = ("id", "name", "version", "description", "entry")

# Slugs are also used as cron-script path components; init-cron-scaffold.sh
# rejects anything outside this set, so reject at the boundary too.
_SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _validate_manifest(m: dict) -> None:
  """Raises HTTPException(400) with a precise message on any issue."""
  missing = [k for k in _REQUIRED_FIELDS if not m.get(k)]
  if missing:
    raise HTTPException(
      400,
      f"Manifest is missing required fields: {', '.join(missing)}",
    )
  mid = m["id"]
  if not isinstance(mid, str) or not mid:
    raise HTTPException(400, "Manifest `id` must be a non-empty string.")
  if any(ch not in _SLUG_OK for ch in mid):
    raise HTTPException(
      400,
      f"Manifest `id` {mid!r} contains invalid chars (allow a-z, 0-9, -, _).",
    )
  # Reject leading `-` / `_` to prevent the slug from being smuggled
  # as an argv flag into init-cron-scaffold.sh (or any other tool we
  # hand it to). The scaffold uses `$1` directly so this is defense-
  # in-depth — the real concern is future callers that do use getopt.
  if mid[0] in "-_":
    raise HTTPException(
      400, f"Manifest `id` must not start with '-' or '_', got {mid!r}",
    )
  if not isinstance(m.get("name"), str):
    raise HTTPException(400, "Manifest `name` must be a string.")
  if not isinstance(m.get("entry"), str):
    raise HTTPException(400, "Manifest `entry` must be a string.")
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
  sched = m.get("schedule")
  if sched is not None:
    if not isinstance(sched, dict):
      raise HTTPException(400, "Manifest `schedule` must be an object.")
    expr = sched.get("default")
    if expr is not None:
      _validate_cron_expr(expr)
    job = sched.get("job")
    if job is not None and (
      not isinstance(job, str) or job.startswith("/") or ".." in job
    ):
      raise HTTPException(
        400, f"Manifest `schedule.job` must be a relative repo path",
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


def _validate_url_safe(url: str) -> None:
  """Rejects URLs whose hostname resolves to a private / loopback /
  link-local / cloud-metadata range.

  The install endpoint is the SSRF surface: we fetch arbitrary URLs
  on behalf of an authenticated owner. From inside the container we
  can reach our own loopback (the Möbius API itself), the Docker
  bridge (sibling containers), and cloud metadata services (IAM
  credential exfiltration on AWS/GCP/Azure). Reject those targets
  before the connect.

  TOCTOU caveat: DNS can change between this validation and the
  actual TCP connect. The mitigation is acceptable for single-owner
  Möbius — exploiting it requires racing a DNS flip against the
  install handler, which is loud and slow. For multi-tenant or
  reduced-trust deployments, switch to an httpx transport that
  validates the *actual* connect IP. Tracked alongside ticket 062.
  """
  parsed = urlparse(url)
  if parsed.scheme not in ("http", "https"):
    raise HTTPException(
      400, f"URL scheme must be http or https, got {parsed.scheme!r}",
    )
  host = parsed.hostname
  if not host:
    raise HTTPException(400, f"URL is missing a hostname: {url}")
  try:
    infos = socket.getaddrinfo(host, None)
  except socket.gaierror as exc:
    raise HTTPException(400, f"Cannot resolve host {host!r}: {exc}")
  for info in infos:
    ip_str = info[4][0]
    try:
      ip = ipaddress.ip_address(ip_str)
    except ValueError:
      continue
    for net in _BLOCKED_NETS:
      if ip in net:
        raise HTTPException(
          400,
          f"URL {host!r} resolves to blocked address {ip} "
          f"(network {net}).",
        )


def _derive_raw_base(manifest_url: str) -> str:
  """Everything before the trailing filename — entry, icon, and seed
  file references resolve relative to this."""
  if "/" not in manifest_url:
    raise HTTPException(400, "Cannot derive raw_base from manifest_url.")
  return manifest_url.rsplit("/", 1)[0] + "/"


def _canonical_for_inline(raw_base: str, manifest_id: str) -> str:
  """Synthesize a stable manifest_url for inline-manifest installs.

  Used when the caller passed `manifest` + `raw_base` instead of a
  manifest_url. We need SOMETHING to key update-vs-install
  discrimination on; the raw_base + manifest_id is unique-enough for
  that purpose."""
  base = raw_base.rstrip("/")
  return f"{base}#manifest-id={manifest_id}"


async def _http_get(
  client: httpx.AsyncClient, url: str, max_bytes: int, _hops: int = 0,
) -> bytes:
  """GETs a URL with SSRF validation + manual redirect handling.

  Each hop is re-validated through `_validate_url_safe` so a 302 to
  a private IP gets rejected just like a direct request to one.
  `follow_redirects` is False on the client; we walk the chain
  ourselves with a hop count cap.

  Reads the body as a stream and aborts as soon as the running byte
  total crosses `max_bytes` — `r.content` would buffer the full
  response before the cap fires, so a hostile upstream could force
  us to allocate `max_bytes` × N pending requests in memory.
  """
  if _hops > _MAX_REDIRECTS:
    raise HTTPException(
      502, f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url}",
    )
  _validate_url_safe(url)
  try:
    async with client.stream("GET", url) as r:
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


def _seed_value_is_inline(value) -> bool:
  """`storage_seeds` values: a string is a repo-relative path; anything
  else (dict, list, bool, number) is an inline JSON literal."""
  return not isinstance(value, str)


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
    img = img.convert("RGBA" if "A" in img.mode else "RGB")
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
                   bundled_job_bytes: bytes | None) -> None:
  """Writes job.sh (if bundled in the repo) then runs init-cron-scaffold.sh.

  The scaffold script writes init-cron.sh + installs the crontab entry
  AND restores it on the next container restart by replaying every
  /data/apps/*/init-cron.sh from the entrypoint. Idempotent — calling
  it for an unchanged (slug, schedule) is a no-op.

  If the manifest bundled a job script, write it first so the scaffold
  doesn't stub-out the same path. The scaffold preserves existing
  job.sh files; the agent or user can edit them later.
  """
  if bundled_job_bytes:
    job_path.write_bytes(bundled_job_bytes)
    job_path.chmod(0o755)
  scaffold = CRON_SCAFFOLD
  if not scaffold.exists():
    # In tests we mock this away; in containers it's always present.
    raise HTTPException(500, "init-cron-scaffold.sh missing from image.")
  result = subprocess.run(
    [str(scaffold), slug, schedule_expr],
    capture_output=True, text=True, timeout=30,
  )
  if result.returncode != 0:
    raise HTTPException(
      500,
      f"Cron registration failed: {result.stderr.strip()[:400]}",
    )


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


async def install_from_manifest(
  db: Session,
  manifest_url: str | None,
  manifest: dict | None,
  raw_base: str | None,
) -> tuple[models.App, str, list[str], dict]:
  """Returns `(app, mode, warnings, manifest)`.

  The parsed manifest dict comes back so callers can read fields the
  App row doesn't store (notably `version`) without re-fetching.

  Modes:
    - 'install' — created a new App row.
    - 'update' — manifest's id matched an existing app's slug; that
      row's jsx_source + (missing) storage seeds + source_dir got
      refreshed in place. Icon + cron are re-applied to keep the
      end state coherent with the new manifest.

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

    seeds_fetched: dict[str, bytes] = {}
    for sub, value in (manifest.get("storage_seeds") or {}).items():
      if _seed_value_is_inline(value):
        seeds_fetched[sub] = json.dumps(value).encode("utf-8")
      else:
        seeds_fetched[sub] = await _http_get(
          cli, raw_base + value, _SEED_MAX_BYTES,
        )

  # --- Phase 3: decide install vs update -------------------------------
  # Match by manifest_url, NOT by slug. Slug is now a routing concern
  # only — two apps (one user-built, one installed from a manifest)
  # may want the same slug stem, and allocate_unique_slug already
  # handles the collision by appending -2/-3/... Identity for "is
  # this the same app re-installed" is keyed on the URL it came
  # from. Inline manifests synthesize a canonical key from raw_base
  # + manifest.id so they still get update-on-reinstall behavior.
  manifest_id = manifest["id"]
  canonical_manifest_url = (
    manifest_url if manifest_url is not None
    else _canonical_for_inline(raw_base, manifest_id)
  )
  existing = (
    db.query(models.App)
    .filter(models.App.manifest_url == canonical_manifest_url)
    .first()
  )
  mode = "update" if existing else "install"

  warnings: list[str] = []
  if icon_warning:
    warnings.append(icon_warning)

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
      app.name = manifest["name"]
      app.description = manifest.get("description", "")
      app.jsx_source = jsx_source
      app.cross_app_access = perms.get("cross_app_access", app.cross_app_access)
      app.share_with_apps = perms.get("share_with_apps", app.share_with_apps)
      db.flush()
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
      source_dir = str(data_dir / "apps" / slug)
      app = models.App(
        name=manifest["name"],
        description=manifest.get("description", ""),
        jsx_source=jsx_source,
        source_dir=source_dir,
        slug=slug,
        manifest_url=canonical_manifest_url,
        cross_app_access=perms.get("cross_app_access", "none"),
        share_with_apps=perms.get("share_with_apps", "none"),
      )
      db.add(app)
      db.flush()  # assign app.id without committing yet

    # On the update path, snapshot the existing compiled bundle to
    # `.bak` BEFORE compile overwrites it. If anything downstream
    # fails and the DB rolls back to the old compiled_path string,
    # we restore the file at that path from the snapshot — otherwise
    # the row points at a path holding the new (possibly broken)
    # bundle. Fresh installs don't need this: rollback drops the row
    # entirely and `_cleanup` removes the file we just created.
    if mode == "update":
      old_compiled = data_dir / "compiled" / f"app-{app.id}.js"
      backup = old_compiled.with_suffix(".js.bak")
      if old_compiled.exists():
        # Best-effort: a stale .bak from a prior crashed install
        # would block the rename. Clear it first.
        if backup.exists():
          try:
            backup.unlink()
          except OSError:
            pass
        os.rename(old_compiled, backup)
        rollback_actions.append(
          lambda b=backup, o=old_compiled: os.rename(b, o)
            if b.exists() else None
        )
        commit_actions.append(
          lambda b=backup: b.unlink() if b.exists() else None
        )

    # Compile JSX → /data/compiled/app-<id>.js. Raises on syntax error
    # which our outer except catches + rolls everything back.
    app.compiled_path = await compile_jsx(app.id, jsx_source)

    # Write source_dir/index.jsx so the file watcher sees the app.
    # Without this, agent edits to the app's JSX don't recompile.
    source_dir_path = Path(app.source_dir or "")
    if source_dir_path:
      source_dir_path.mkdir(parents=True, exist_ok=True)
      jsx_file = source_dir_path / "index.jsx"
      first_write = not jsx_file.exists()
      jsx_file.write_text(jsx_source, encoding="utf-8")
      if first_write:
        created_paths.append(jsx_file)

    # Storage seeds — fresh installs always seed; updates only fill
    # in keys that don't exist yet so user data isn't clobbered.
    for sub, content in seeds_fetched.items():
      target = _storage_path(app.id, sub)
      if mode == "update" and target.exists():
        continue
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(content)
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

    # Success: drop any .bak snapshots we made — the new bundle is
    # now the canonical one.
    for action in commit_actions:
      try:
        action()
      except OSError as exc:
        log.warning("install: post-commit cleanup failed — %s", exc)

  except HTTPException:
    db.rollback()
    _run_rollback_actions(rollback_actions)
    _cleanup(created_paths)
    raise
  except Exception as exc:
    # Catch-all so a stray bug doesn't leak partial state. Re-raise
    # as 500 with a useful detail; uvicorn already logs the traceback.
    log.exception("install: unexpected failure during materialize")
    db.rollback()
    _run_rollback_actions(rollback_actions)
    _cleanup(created_paths)
    raise HTTPException(500, f"Install failed: {exc!r}")

  # --- Phase 5: post-commit cron registration -------------------------
  # The app is fully installed at this point. Cron failures become
  # warnings, not 500s — the user just needs to re-set the schedule.
  if sched and sched.get("default"):
    slug = app.slug
    job_name = sched.get("job", "fetch.sh")
    app_data_dir = data_dir / "apps" / slug
    try:
      app_data_dir.mkdir(parents=True, exist_ok=True)
      job_path = app_data_dir / job_name
      if CRON_SCAFFOLD.exists():
        await asyncio.to_thread(
          _register_cron,
          slug, sched["default"], job_path, bundled_job,
        )
      else:
        # In tests we mock the scaffold; persist a sentinel so the
        # contract is still observable + warn the caller.
        if bundled_job:
          job_path.write_bytes(bundled_job)
          job_path.chmod(0o755)
        sentinel = app_data_dir / ".cron-pending.json"
        sentinel.write_text(json.dumps({
          "expr": sched["default"], "job": job_name,
          "status": "pending — init-cron-scaffold.sh not on PATH",
        }), encoding="utf-8")
        warnings.append(
          "cron: scaffold script not available — registration pending"
        )
    except HTTPException as exc:
      # Cron failed but the app is installed. Surface as a warning.
      log.warning("install: cron registration failed post-commit — %s",
                  exc.detail)
      warnings.append(f"cron: registration failed — {exc.detail}")
    except Exception as exc:
      log.exception("install: cron registration failed post-commit")
      warnings.append(f"cron: registration failed — {exc!r}")

  return app, mode, warnings, manifest


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
