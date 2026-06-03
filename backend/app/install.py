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

from app import activity, app_git, fs_locks, models
from app.compiler import compile_jsx
from app.config import get_settings
from app.providers import per_app_git_enabled
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
# Aggregate caps across ALL seeds in one manifest. The per-file cap alone
# leaves the total unbounded (a manifest can list many seeds), so a small
# manifest could still force large memory growth holding them all (Codex
# review round-10 #6). These bound the count and the summed bytes.
_SEEDS_COUNT_MAX = 64
_SEEDS_TOTAL_MAX = 32 * 1024 * 1024

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
  # NAT64 well-known prefix — a resolver can hand back 64:ff9b::<v4> for a
  # blocked IPv4 (e.g. 64:ff9b::a9fe:a9fe == 169.254.169.254), which the
  # ipv4_mapped check below does NOT catch (that only handles ::ffff:). The
  # install fetcher has no legitimate need to reach a host only via NAT64, so
  # block the whole prefix.
  ipaddress.ip_network("64:ff9b::/96"),
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
  # A purely-numeric id becomes the slug and source dir /data/apps/<id>,
  # which collides with the numeric-id storage tree another app writes to
  # (storage uses /data/apps/<integer app id>). Reserve bare integers for
  # storage (Codex review #4).
  if mid.isdigit():
    raise HTTPException(
      400,
      f"Manifest `id` {mid!r} must not be purely numeric — bare integers "
      "are reserved for the per-app storage path /data/apps/<id>.",
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
    # An IPv4-mapped IPv6 address (::ffff:a.b.c.d) reaches the SAME host as
    # a.b.c.d but would not match the IPv4 entries in _BLOCKED_NETS — so
    # ::ffff:169.254.169.254 would slip past the cloud-metadata block. Check
    # the embedded IPv4 against every blocked net too.
    candidates = [ip]
    if ip.version == 6 and ip.ipv4_mapped is not None:
      candidates.append(ip.ipv4_mapped)
    for cand in candidates:
      for net in _BLOCKED_NETS:
        if cand in net:
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
  # Strip the trailing manifest FILENAME — `mobius.json` or any other `*.json`
  # a paste-a-URL install pointed at — so the URL path canonicalises to the same
  # directory base the inline path keys on (its `raw_base` is that directory).
  # Stripping only `/mobius.json` split a non-mobius.json app into two App rows
  # when installed once via URL and once inline.
  if base.endswith(".json") and "/" in base:
    base = base.rsplit("/", 1)[0]
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
                   bundled_job_bytes: bytes | None,
                   app_id: int | None = None) -> None:
  """Writes the bundled job (if any) then runs init-cron-scaffold.sh.

  The scaffold script writes init-cron.sh + installs the crontab entry
  AND restores it on the next container restart by replaying every
  /data/apps/*/init-cron.sh from the entrypoint. Idempotent — calling
  it for an unchanged (slug, schedule, job) is a no-op.

  If the manifest bundled a job script, write it first so the scaffold
  doesn't stub-out the same path. The scaffold preserves existing job
  files; the agent or user can edit them later.

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
  if bundled_job_bytes:
    job_path.write_bytes(bundled_job_bytes)
    job_path.chmod(0o755)
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
    - 'conflict' — ONLY when the per-app git model is enabled
      (providers.per_app_git_enabled) AND a three-way merge of the new
      upstream into the app's local edits conflicted. Nothing is
      clobbered: the on-disk source, the compiled bundle, and the DB
      row's jsx_source all keep the local edits; the new upstream bytes
      are recorded on the `upstream` branch for a later agent-resolution
      pass. `conflict_paths` names the files that need resolving. The
      App row is committed (so the recorded upstream sha persists) but
      the served app is unchanged.

  The per-app git model is ON by default. When explicitly off, install
  + update behave exactly as before this feature: a blind jsx_source
  overwrite with no `.git` repo created. 'conflict' never occurs while
  the flag is off.

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
  existing = (
    db.query(models.App)
    .filter(models.App.manifest_url == canonical_manifest_url)
    .first()
  )
  mode = "update" if existing else "install"

  warnings: list[str] = []
  conflict_paths: list[str] = []
  divergence: str = "none"
  if icon_warning:
    warnings.append(icon_warning)

  # The per-app git model is ON by default (feature 084). When explicitly
  # off, everything below behaves exactly as before: a blind jsx_source
  # overwrite with no .git repo. When on, an update merges the new
  # upstream into the app's local edits instead of clobbering them, and
  # `effective_source` may end up being the MERGED bytes rather than the
  # upstream bytes we just fetched. We read the flag once so a mid-flight
  # settings change can't split the decision across this one install.
  git_on = per_app_git_enabled(str(get_settings().data_dir))
  upstream_jsx_sha = hashlib.sha256(entry_bytes).hexdigest()
  # The source that actually gets compiled + written to disk. Defaults
  # to the upstream bytes; the git merge step below may replace it with
  # the merged bytes on a clean update.
  effective_source = jsx_source

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
      # jsx_source is assigned from `effective_source` AFTER the git
      # merge decision below — on a conflict we keep the local edits, so
      # we must not blindly stamp the upstream bytes here.
      # Rewrite the manifest_url column to the canonical shape so
      # rows installed before the canonicaliser landed migrate
      # forward on their next update. New installs already write
      # the canonical form below.
      app.manifest_url = canonical_manifest_url
      app.cross_app_access = perms.get("cross_app_access", app.cross_app_access)
      app.share_with_apps = perms.get("share_with_apps", app.share_with_apps)
      app.chat_log_access = perms.get("chat_log_access", app.chat_log_access)
      # Mirror manage_apps on update too — an app can gain or lose
      # install authority across versions. Default to the existing
      # value when the manifest omits the key.
      if "manage_apps" in perms:
        app.manage_apps = bool(perms["manage_apps"])
      # Mirror manifest.offline_capable into the App row on every
      # update, so an app that flips its offline behaviour between
      # versions (e.g. news 1.5.1 online-only → 1.5.2 offline-capable)
      # gets the SW + outbox semantics matching what it ships, not
      # what it used to ship.
      if "offline_capable" in manifest:
        app.offline_capable = bool(manifest["offline_capable"])
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
        # Non-behavioral telemetry: the install still succeeds under the
        # suffixed slug, but a collision means two apps now share a stem
        # (user-built vs store, or two store apps). Logging requested-vs-
        # assigned makes that observable to the dreaming agent / store UI
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
        # The manifest's `offline_capable: true` opts the app into the
        # SW frame cache + the window.mobius.storage outbox. Without
        # this line every installed app defaulted to offline_capable=
        # false on the App row regardless of what the manifest declared
        # — apps that paid the cost of being offline-ready in code
        # didn't actually behave offline-ready end-to-end.
        offline_capable=bool(manifest.get("offline_capable", False)),
      )
      db.add(app)
      db.flush()  # assign app.id without committing yet

    # --- Per-app git: record upstream + (on update) merge into local ---
    # Gated on the flag. The git ops run under source_dir_lock — the same
    # lock the source write below takes — acquired here as a SEPARATE,
    # non-nested critical section (acquire/release, then re-acquire for
    # the write), which is safe for a non-reentrant asyncio.Lock. We do
    # the merge BEFORE the compile so `effective_source` (which the
    # compile + write below consume) reflects the merged bytes on a clean
    # update, and so a conflict can short-circuit both.
    # Guard on the raw string, NOT the Path: Path("") is a truthy Path, so a
    # row with an empty source_dir would otherwise initialize a git repo in
    # the server's working directory. Only engage git when there's a real dir.
    git_source_dir = Path(app.source_dir) if app.source_dir else None
    if git_on and git_source_dir:
      async with fs_locks.source_dir_lock(str(git_source_dir)):
        version = str(manifest.get("version", "unknown"))
        had_repo = app_git.is_repo(git_source_dir)
        if existing and had_repo:
          prev_upstream_commit = app.upstream_commit
          # Update of an app already on the git model. First capture any
          # uncommitted on-disk local edits onto `main` (the watcher may
          # not have committed the agent's latest save yet) so the merge
          # diffs against the real local source. Then record the new
          # pristine bytes on `upstream` (parent = the previously-recorded
          # upstream, giving a shared merge base) and ask for a clean-vs-
          # conflict verdict WITHOUT touching the working tree.
          await asyncio.to_thread(
            app_git.commit_local, git_source_dir,
            "local edits before update",
          )
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, entry_bytes, canonical_manifest_url, version,
          )
          merge = await asyncio.to_thread(
            app_git.merge_upstream, git_source_dir,
          )
          if merge.status == "conflict":
            # Never rebase local. The app stays served with
            # its current bundle + source; the new upstream is recorded
            # for a later agent-resolution pass. Skip compile + source
            # overwrite by switching to conflict mode below.
            mode = "conflict"
            conflict_paths = merge.conflict_paths
          elif merge.merged_bytes is not None:
            # Clean merge: the merged source is what we compile + write.
            effective_source = merge.merged_bytes.decode("utf-8")
            diverged = False
            if prev_upstream_commit:
              diverged = await asyncio.to_thread(
                app_git.local_diverged_from,
                git_source_dir, prev_upstream_commit,
              )
            divergence = "clean_merge" if diverged else "fast_forward"
        elif existing and not had_repo:
          # Lazy migration: an app installed before the flag was on has
          # no repo. Seed it with its CURRENT on-disk source as the base
          # upstream version, then record the new upstream on top. The
          # base IS the local source, so there is no historical divergence
          # to conflict on — the addendum's accepted "no historical
          # conflicts" migration. The first post-migration update takes
          # upstream for any line it changed; subsequent updates merge
          # normally against the recorded base.
          jsx_path = git_source_dir / "index.jsx"
          base_bytes = (
            jsx_path.read_bytes() if jsx_path.exists() else entry_bytes
          )
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, base_bytes, canonical_manifest_url,
            "migrated-base",
          )
          await asyncio.to_thread(
            app_git.align_local_to_upstream, git_source_dir,
          )
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, entry_bytes, canonical_manifest_url, version,
          )
          merge = await asyncio.to_thread(
            app_git.merge_upstream, git_source_dir,
          )
          if merge.status == "conflict":
            mode = "conflict"
            conflict_paths = merge.conflict_paths
          elif merge.merged_bytes is not None:
            effective_source = merge.merged_bytes.decode("utf-8")
        else:
          # Install: record the pristine bytes on `upstream`, then align
          # the local `main` branch to that commit so the working branch
          # starts exactly at the installed version — a shared base for
          # the next update's merge.
          await asyncio.to_thread(
            app_git.record_upstream,
            git_source_dir, entry_bytes, canonical_manifest_url, version,
          )
          await asyncio.to_thread(
            app_git.align_local_to_upstream, git_source_dir,
          )
        app.upstream_jsx_sha = upstream_jsx_sha
        app.upstream_commit = await asyncio.to_thread(
          app_git.head_sha, git_source_dir, app_git.UPSTREAM_BRANCH,
        )

    if mode == "conflict":
      # Conflict short-circuit: persist the recorded upstream provenance
      # (upstream_commit + upstream_jsx_sha, set above) but leave the
      # served app entirely as-is. We deliberately do NOT touch
      # `app.jsx_source` — it still holds the LOCAL source, and the
      # on-disk file + compiled bundle keep the local edits — so the row,
      # the disk, and the bundle stay consistent on the local version
      # until an agent resolves the conflict and saves. Skip the compile,
      # source-write, seeds, icon, and cron blocks below; the served
      # version is unchanged so its seeds/cron are unchanged too.
      db.commit()
      db.refresh(app)
      activity.log_event(
        "app_install", app_id=app.id, slug=app.slug, source=source,
      )
      return app, mode, warnings, manifest, conflict_paths, divergence

    if existing:
      # Apply the (possibly merged) source to the row now that the merge
      # decision is made. On the flag-off path this is just `jsx_source`.
      app.jsx_source = effective_source

    # Compile the JSX OUT OF PLACE to a staging file and promote it into the
    # live bundle only AFTER the DB commit (commit_actions run post-commit). So
    # a concurrent module read never observes a missing or half-written live
    # bundle mid-update, and a rollback or crash discards the staging file,
    # leaving the prior bundle intact (a leaked staging file is reaped at
    # startup and is never served). Raises on syntax error -> the outer except
    # rolls everything back. Same transactional recompile PATCH and the file
    # watcher use (compiler.recompile_app_bundle).
    live_bundle = data_dir / "compiled" / f"app-{app.id}.js"
    staged_bundle = data_dir / "compiled" / f"app-{app.id}.js.staging"
    await compile_jsx(app.id, effective_source, out_path=staged_bundle)
    app.compiled_path = str(live_bundle)
    rollback_actions.append(
      lambda s=staged_bundle: s.unlink() if s.exists() else None
    )
    commit_actions.append(
      lambda s=staged_bundle, l=live_bundle: os.replace(s, l)
    )

    # Write source_dir/index.jsx so the file watcher sees the app. Under the
    # per-source-dir lock (the endpoint already holds the lifecycle lock, and
    # the seeds' app_storage_lock below is acquired SEPARATELY, never nested
    # with this — so no lock-order violation) a concurrent create/patch can't
    # claim this directory mid-write (Codex review round-9 #3). Written
    # atomically, and on an UPDATE the prior JSX is snapshotted to a .bak so a
    # later rollback restores it — otherwise the watcher would compile the
    # rolled-back (broken) update (round-9 #2).
    source_dir_path = Path(app.source_dir or "")
    if source_dir_path:
      async with fs_locks.source_dir_lock(str(source_dir_path)):
        # Refuse if a DIFFERENT app already claims this source dir (Codex
        # review round-10 #1). The slug is freshly unique, so this only
        # catches a manually-created app that explicitly supplied the same
        # path; on the update path exclude_id is this very app.
        _reject_if_source_dir_taken(
          db, str(source_dir_path), exclude_id=app.id
        )
        source_dir_path.mkdir(parents=True, exist_ok=True)
        jsx_file = source_dir_path / "index.jsx"
        if jsx_file.exists():
          jsx_backup = jsx_file.with_suffix(".jsx.bak")
          if jsx_backup.exists():
            try:
              jsx_backup.unlink()
            except OSError:
              pass
          shutil.copy2(jsx_file, jsx_backup)
          rollback_actions.append(
            lambda b=jsx_backup, o=jsx_file:
              os.replace(b, o) if b.exists() else None
          )
          commit_actions.append(
            lambda b=jsx_backup: b.unlink() if b.exists() else None
          )
        else:
          created_paths.append(jsx_file)
        atomic_write(jsx_file, effective_source)
        # On the git path, commit the working-tree source onto the local
        # `main` branch so the watcher's future commits build on a known
        # base and the merge base for the NEXT update is the source we
        # just wrote, not a stale tree. No-op when the source is
        # unchanged.
        if git_on:
          await asyncio.to_thread(
            app_git.commit_local, source_dir_path,
            f"install: {manifest.get('name', app.slug)} "
            f"v{manifest.get('version', 'unknown')}",
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
    # meaningful platform signals for the dreaming agent.
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

  # --- Phase 5: post-commit job-script write + cron registration ------
  # The app is fully installed at this point. Job-write / cron failures
  # become warnings, not 500s — the user just needs to re-set the schedule.
  #
  # A bundled job script (manifest `schedule.job`) is written to source_dir
  # whenever one was fetched, INDEPENDENT of whether the manifest also
  # declares a recurring `schedule.default`. An app may ship an on-demand
  # job invoked only through the run-job endpoint (e.g. the LaTeX app's
  # build.sh, compiled on a Build click) with no recurring schedule —
  # coupling the write to cron registration would fetch the script but never
  # land it on disk, and run-job would 400 with "no job script". A recurring
  # crontab entry is installed only when `schedule.default` is present.
  has_cron = bool(sched and sched.get("default"))
  if bundled_job or has_cron:
    slug = app.slug
    job_name = sched.get("job", "fetch.sh") if sched else "fetch.sh"
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
          raise HTTPException(404, "App removed before job-script write.")
        app_data_dir.mkdir(parents=True, exist_ok=True)
        job_path = app_data_dir / job_name
        if has_cron and CRON_SCAFFOLD.exists():
          # _register_cron writes the bundled job first, then installs the
          # crontab entry pointing at it.
          await asyncio.to_thread(
            _register_cron,
            slug, sched["default"], job_path, bundled_job, app.id,
          )
        else:
          # Either an on-demand-only job (no recurring schedule) or, when a
          # schedule IS declared, a test env that mocks the scaffold away.
          # Land the script on disk so run-job can find it either way.
          if bundled_job:
            job_path.write_bytes(bundled_job)
            job_path.chmod(0o755)
          if has_cron:
            # Schedule declared but the scaffold isn't on PATH (tests):
            # persist a sentinel so the contract is observable + warn.
            sentinel = app_data_dir / ".cron-pending.json"
            sentinel.write_text(json.dumps({
              "expr": sched["default"], "job": job_name,
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
