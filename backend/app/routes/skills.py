"""Owner-facing skills surface: list, install-from-online, uninstall.

Skills are the agent's how-to knowledge under `/data/shared/skills/` — seeded
create-if-absent, agent-editable like memory. This router adds the missing two
verbs so the owner (or the Skills mini-app, holding `manage_skills`) can pull a
skill from the public ecosystem — Anthropic Agent Skills, the Hermes catalogs,
agentskills.io, awesome-lists — and remove one it installed.

  GET    /api/skills             list every installed skill + provenance + usage
  POST   /api/skills/install     fetch a SKILL.md (dir or single file) from GitHub
  DELETE /api/skills/{name}      remove an install-provenance skill (git-snapshot)

Design choices that match the codebase philosophy:
  - Fetching reuses `install._http_get` + `net_utils.validate_url_safe`, so the
    SSRF blocklist, redirect pinning, and byte caps are the exact same ones the
    app installer trusts. No second network path to audit.
  - Installed skills land in the DIRECTORY shape (`<name>/SKILL.md` + resources)
    — the external convention — recorded in an installer-owned
    `.installed-skills.json` sidecar, the same ownership pattern as
    `.app-skills.json`. A basename already taken by a seed / agent / app skill
    is a 409, never a silent overwrite: prevention lives in the flow, and the
    agent/owner renames or removes deliberately.
  - Uninstall only ever touches install-provenance skills; a seed or agent skill
    keeps its own lifecycle. The bytes are git-snapshotted into the `/data` repo
    before removal, so nothing the agent authored is lost.
  - There is no code validator on WHAT a skill instructs — a skill is prose the
    trusted agent chooses to read. The trust ritual (read it, summarize it,
    confirm) lives in the `finding-skills.md` seed skill, not here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, catalog_index, install, models, skills
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_manage_skills,
  get_principal,
  reject_cross_site,
)
from app import fs_locks
from app.manifest_contract import SKILL_MAX_BYTES
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/skills", tags=["skills"])

log = logging.getLogger(__name__)

# A skill name is also a directory name under shared/skills/, so it obeys the
# same shape as an app slug: lowercase, no traversal, no leading punctuation.
_SKILL_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Resource files a directory skill may carry alongside SKILL.md (including in
# subdirectories — scripts/, references/, assets/ are common in the ecosystem).
# Bounded so an install can't smuggle a data payload through the skills tree —
# a skill is instruction prose plus a few references, not an app.
_RESOURCE_COUNT_MAX = 24
_RESOURCE_TOTAL_MAX = 2 * 1024 * 1024
_RESOURCE_MAX_DEPTH = 4
# Only text/reference file types are stored; anything else is skipped with a
# warning rather than materialized into the skills tree.
_RESOURCE_SUFFIXES = frozenset({
  ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".py", ".js", ".ts",
  ".sh", ".toml", ".html", ".css",
})

_GITHUB_API = "https://api.github.com"
_USAGE_WINDOW_DAYS = 30

_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_REF_OK = re.compile(r"[A-Za-z0-9._/-]{1,100}")
_REPO_OK = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class SkillInstall(BaseModel):
  """Body for POST /api/skills/install.

  Two source shapes, mirroring how the app installer accepts either a repo or a
  raw URL:
    - `repo` + `path` (+ optional `ref`, default `main`): a GitHub path to a
      skill directory (containing SKILL.md) or a single markdown file.
    - `url`: a direct raw URL to a single SKILL.md / `<name>.md`.
  `name` optionally overrides the derived skill name (must match the skill-name
  charset).
  """

  repo: str | None = None
  path: str | None = None
  ref: str = "main"
  url: str | None = None
  name: str | None = None


def _skills_dir() -> Path:
  return Path(get_settings().data_dir) / "shared" / "skills"


def _chown_mobius(path: Path) -> None:
  """Best-effort: make an installed skill agent-editable (mirrors init_skills)."""
  try:
    m = pwd.getpwnam("mobius")
  except KeyError:
    return
  for p in [path, *(path.rglob("*") if path.is_dir() else [])]:
    try:
      os.chown(p, m.pw_uid, m.pw_gid)
      os.chmod(p, 0o775 if p.is_dir() else 0o664)
    except OSError:
      pass


class _CorruptSidecar(Exception):
  """The installed-skills sidecar is present but unparseable/not an object."""


def _load_installed_sidecar(skills_dir: Path) -> dict:
  """Parse the installed-skills sidecar, distinguishing MISSING from CORRUPT.

  A missing sidecar is a legitimate empty state ({}). A present-but-unparseable
  or non-object sidecar is CORRUPT and raises: mapping corruption to {} (as the
  read path does) would let the next MUTATION overwrite the file with only its
  own new record, silently orphaning every previously-installed skill —
  present on disk but unowned and no longer uninstallable through the API.
  """
  path = skills_dir / skills.INSTALLED_SKILLS_SIDECAR
  try:
    raw = path.read_text(encoding="utf-8")
  except FileNotFoundError:
    return {}
  except OSError as exc:
    raise _CorruptSidecar(f"could not read {path.name}: {exc}") from exc
  try:
    loaded = json.loads(raw)
  except ValueError as exc:
    raise _CorruptSidecar(f"{path.name} is not valid JSON: {exc}") from exc
  if not isinstance(loaded, dict):
    raise _CorruptSidecar(f"{path.name} is not a JSON object")
  return loaded


def _read_installed_sidecar(skills_dir: Path) -> dict:
  """Best-effort read for display paths: corruption degrades to {} here.

  Mutations must use `_load_installed_sidecar` instead so they fail closed
  rather than clobbering a recoverable sidecar.
  """
  try:
    return _load_installed_sidecar(skills_dir)
  except _CorruptSidecar:
    return {}


def _write_installed_sidecar(skills_dir: Path, records: dict) -> None:
  atomic_write(
    skills_dir / skills.INSTALLED_SKILLS_SIDECAR,
    json.dumps(records, indent=2, sort_keys=True) + "\n",
  )


def _entry_kind(path: Path) -> str:
  """lstat-based dirent type: 'absent' | 'dir' | 'file' | 'symlink' | 'other'.

  Never follows symlinks — for install/uninstall decisions a link is a link,
  wherever it points, so it can neither hide a collision nor redirect a write
  or delete outside the skills tree.
  """
  try:
    st = os.lstat(path)
  except OSError:
    return "absent"
  if stat.S_ISLNK(st.st_mode):
    return "symlink"
  if stat.S_ISDIR(st.st_mode):
    return "dir"
  if stat.S_ISREG(st.st_mode):
    return "file"
  return "other"


def _provenance_of(skills_dir: Path, base_name: str) -> str:
  """Provenance label for an existing basename (for a collision message)."""
  for skill in skills.enumerate_skills(skills_dir):
    disk_name = (
      skill.read_path.parent.name if skill.is_dir else skill.read_path.stem
    )
    if disk_name == base_name:
      return skill.provenance
  return "unknown"


def _derive_name(explicit: str | None, source_segments: list[str]) -> str:
  """Skill name from an explicit override, else the source path.

  For `.../<name>/SKILL.md` the name is `<name>`; for `.../<name>.md` it is the
  stem. Validated against the skill-name charset either way.
  """
  if explicit:
    candidate = explicit.strip().lower()
  else:
    segs = [s for s in source_segments if s]
    tail = segs[-1] if segs else ""
    if tail.upper() == "SKILL.MD" and len(segs) >= 2:
      candidate = segs[-2].lower()
    elif tail.lower().endswith(".md"):
      candidate = tail[:-3].lower()
    else:
      candidate = tail.lower()
  if _SKILL_NAME_OK.fullmatch(candidate) is None:
    raise HTTPException(
      400,
      f"Could not derive a valid skill name from {candidate!r}; pass an "
      "explicit `name` matching ^[a-z0-9][a-z0-9._-]*$.",
    )
  return candidate


async def _resolve_commit(client: httpx.AsyncClient, repo: str, ref: str) -> str:
  """Resolve a mutable ref to the immutable commit OID it points at right now.

  Contents, tree, and every raw-file fetch afterwards name this OID, so an
  install is one consistent snapshot even if the branch moves while requests
  are in flight — and the OID lands in provenance so the exact installed
  revision stays known. A 40-hex ref is already an OID and passes through.
  """
  if _GIT_SHA.fullmatch(ref):
    return ref
  if _GIT_REF_OK.fullmatch(ref) is None or ".." in ref:
    raise HTTPException(400, f"Invalid git ref {ref!r}.")
  url = f"{_GITHUB_API}/repos/{repo}/commits/{quote(ref, safe='')}"
  raw = await install._http_get(client, url, max_bytes=1 * 1024 * 1024)
  try:
    sha = json.loads(raw).get("sha")
  except (ValueError, AttributeError):
    sha = None
  if not isinstance(sha, str) or _GIT_SHA.fullmatch(sha) is None:
    raise HTTPException(
      502, f"Could not resolve ref {ref!r} to a commit in {repo}.",
    )
  return sha


async def _github_contents(
  client: httpx.AsyncClient, repo: str, path: str, ref: str,
) -> list | dict:
  """GitHub contents API for a repo path. Returns a list (dir) or dict (file)."""
  if not _REPO_OK.fullmatch(repo):
    raise HTTPException(400, f"`repo` must be `owner/name`, got {repo!r}.")
  clean = path.strip("/")
  if ".." in clean.split("/"):
    raise HTTPException(400, "`path` must not contain traversal segments.")
  url = f"{_GITHUB_API}/repos/{repo}/contents/{clean}?ref={ref}"
  raw = await install._http_get(client, url, max_bytes=1 * 1024 * 1024)
  try:
    return json.loads(raw)
  except ValueError as exc:
    raise HTTPException(502, f"GitHub contents API returned non-JSON: {exc}")


async def _github_tree(
  client: httpx.AsyncClient, repo: str, path: str, ref: str,
) -> list[dict]:
  """Recursive git-trees listing of ONE subtree (`<ref>:<path>`), one request.

  `<ref>:<path>` is git rev syntax resolving to the tree object at that path,
  so the response paths come back already relative to the skill directory —
  exactly the relpaths the installer materializes.
  """
  spec = quote(f"{ref}:{path}" if path else ref, safe="")
  url = f"{_GITHUB_API}/repos/{repo}/git/trees/{spec}?recursive=1"
  raw = await install._http_get(client, url, max_bytes=4 * 1024 * 1024)
  try:
    data = json.loads(raw)
  except ValueError as exc:
    raise HTTPException(502, f"GitHub trees API returned non-JSON: {exc}")
  tree = data.get("tree") if isinstance(data, dict) else None
  if not isinstance(tree, list):
    raise HTTPException(502, "GitHub trees API returned no tree for this path.")
  if data.get("truncated"):
    # An incomplete listing must not masquerade as the whole skill.
    raise HTTPException(
      502,
      "GitHub truncated the tree listing for this path — the skill directory "
      "cannot be enumerated completely, so nothing was installed.",
    )
  return [e for e in tree if isinstance(e, dict)]


def _resource_rel_ok(rel: str) -> bool:
  """Whether a tree-relative path is safe to materialize under the skill dir.

  Every segment must be a plain name (no dot-prefixed files/dirs, no
  traversal, no backslashes), depth is bounded, and the suffix must be in the
  text/reference allowlist.
  """
  segments = rel.split("/")
  if not 1 <= len(segments) <= _RESOURCE_MAX_DEPTH:
    return False
  for seg in segments:
    if not seg or seg.startswith(".") or "\\" in seg:
      return False
  return Path(rel).suffix.lower() in _RESOURCE_SUFFIXES


async def _fetch_files(
  client: httpx.AsyncClient, body: SkillInstall,
) -> tuple[str, dict[str, bytes], str, str | None]:
  """Resolve the request into (skill_name, {relpath: bytes}, source, commit).

  `relpath` is always relative to the skill directory (SKILL.md at the root;
  resources may live in subdirectories). Exactly one skill is produced; the
  SKILL.md file is required. For repo installs `commit` is the OID every fetch
  was pinned to; a raw-URL install has no commit to pin (None).
  """
  files: dict[str, bytes] = {}

  # Direct raw URL → a single markdown file becomes this skill's SKILL.md.
  if body.url:
    segments = body.url.split("?", 1)[0].split("/")
    name = _derive_name(body.name, segments)
    data = await install._http_get(client, body.url, max_bytes=SKILL_MAX_BYTES)
    files["SKILL.md"] = data
    return name, files, _source_label(body.url), None

  if not (body.repo and body.path):
    raise HTTPException(
      400, "Provide either `url`, or both `repo` and `path`.",
    )
  if not _REPO_OK.fullmatch(body.repo):
    raise HTTPException(400, f"`repo` must be `owner/name`, got {body.repo!r}.")

  # Pin the whole install to one immutable revision before any content fetch.
  commit = await _resolve_commit(client, body.repo, body.ref)
  listing = await _github_contents(client, body.repo, body.path, commit)
  segments = body.path.strip("/").split("/")

  if isinstance(listing, dict):
    # A single file path. Treat as this skill's SKILL.md. download_url comes
    # from the ?ref=<commit> listing, so it names the pinned revision too.
    name = _derive_name(body.name, segments)
    download = listing.get("download_url")
    if not download:
      raise HTTPException(502, "GitHub file entry missing download_url.")
    files["SKILL.md"] = await install._http_get(
      client, download, max_bytes=SKILL_MAX_BYTES,
    )
    return name, files, _source_label(body.repo), commit

  # A directory: enumerate the WHOLE subtree with one scoped git-trees call —
  # the ecosystem keeps scripts/references in subdirectories, which the
  # top-level contents listing above cannot see — then fetch SKILL.md
  # (case-insensitive, root only) plus bounded resources.
  name = _derive_name(body.name, segments)
  clean_path = body.path.strip("/")
  tree = await _github_tree(client, body.repo, clean_path, commit)

  def _raw_url(rel: str) -> str:
    prefix = f"{quote(clean_path, safe='/')}/" if clean_path else ""
    return (
      "https://raw.githubusercontent.com/"
      f"{body.repo}/{commit}/{prefix}{quote(rel, safe='/')}"
    )

  skill_rel = next(
    (
      str(e.get("path"))
      for e in tree
      if e.get("type") == "blob" and str(e.get("path", "")).upper() == "SKILL.MD"
    ),
    None,
  )
  if skill_rel is None:
    raise HTTPException(
      400,
      f"No SKILL.md in {body.repo}/{body.path} — not a skill directory.",
    )
  files["SKILL.md"] = await install._http_get(
    client, _raw_url(skill_rel), max_bytes=SKILL_MAX_BYTES,
  )

  resource_count = 0
  resource_total = 0
  for entry in tree:
    if entry.get("type") != "blob":
      continue
    rel = str(entry.get("path", ""))
    if not rel or rel == skill_rel or not _resource_rel_ok(rel):
      continue
    if resource_count >= _RESOURCE_COUNT_MAX:
      break
    remaining = _RESOURCE_TOTAL_MAX - resource_total
    if remaining <= 0:
      break
    declared = entry.get("size")
    if isinstance(declared, int) and declared > remaining:
      continue  # skip an over-budget file, smaller ones may still fit
    data = await install._http_get(client, _raw_url(rel), max_bytes=remaining)
    files[rel] = data
    resource_total += len(data)
    resource_count += 1

  return name, files, _source_label(body.repo), commit


def _source_label(source: str) -> str:
  """A short provenance label: `owner/repo` for a repo, else the host."""
  if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
    return source
  from urllib.parse import urlparse

  return urlparse(source).hostname or source


def _snapshot_skill_dir(data_dir: Path, name: str) -> tuple[bool, str]:
  """Commit shared/skills/<name>/ into the /data repo before removal.

  Mirrors install._snapshot_shared_skill but scopes the pathspec to the whole
  skill directory so a directory skill's resources are captured too. Returns
  (ok, detail); ok=False means durability could not be guaranteed and the
  caller must not delete.
  """
  env = {
    k: v for k, v in os.environ.items()
    if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
  }
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

  path = f"shared/skills/{name}"
  status = _run("status", "--porcelain", "--", path)
  if status.returncode != 0:
    return False, _reason(status)
  if not status.stdout.strip():
    return True, "already committed"
  add = _run("add", "--", path)
  if add.returncode != 0:
    return False, _reason(add)
  commit = _run(
    "commit", "--only", "-m", f"pre-uninstall snapshot of skill {name}",
    "--", path,
  )
  if commit.returncode != 0:
    return False, _reason(commit)
  return True, "committed"


def _redact_source_url(url: str | None) -> str | None:
  """Origin + path only — no userinfo, query, or fragment.

  Signed object links and private raw URLs commonly carry access tokens or
  signatures in the query string (and occasionally in userinfo); those must
  never cross the API boundary to an ordinary app token. The full submitted URL
  stays owner-only; every caller still gets the authoritative `skill_sha256`.
  """
  if not url:
    return url
  from urllib.parse import urlparse, urlunparse

  try:
    parts = urlparse(url)
  except ValueError:
    return None
  host = parts.hostname or ""
  if parts.port:
    host = f"{host}:{parts.port}"
  return urlunparse((parts.scheme, host, parts.path, "", "", ""))


@router.get("")
def list_skills(principal=Depends(get_principal)) -> dict:
  """Every installed skill with metadata, provenance, and recent usage counts.

  Drives the Skills mini-app's "installed" view. Readable by the owner or any
  app token (browsing is not privileged; only install/uninstall are gated) —
  but the raw submitted `source_url` (which may carry credentials in its query
  string) is owner-only; app callers get a redacted origin/path instead.
  """
  is_owner = getattr(principal, "scope", "app") == "owner"
  skills_dir = _skills_dir()
  now = datetime.now(UTC)
  try:
    counts = {
      row["skill"]: row["count"]
      for row in activity.most_used_skills(
        now - timedelta(days=_USAGE_WINDOW_DAYS), now,
      )
    }
  except Exception:  # pragma: no cover - usage is a non-critical enrichment
    counts = {}
  installed_records = _read_installed_sidecar(skills_dir)
  out = []
  for skill in skills.enumerate_skills(skills_dir):
    disk_name = (
      skill.read_path.parent.name if skill.is_dir else skill.read_path.stem
    )
    row = {
      "name": skill.name,
      "id": disk_name,
      "description": skill.description,
      "provenance": skill.provenance,
      "is_dir": skill.is_dir,
      # Usage is keyed STRICTLY by the on-disk id — both runners observe loads
      # by file path. Frontmatter `name` is untrusted; keying by it would let
      # an alias borrow another skill's count.
      "uses_30d": counts.get(disk_name, 0),
    }
    rec = installed_records.get(disk_name)
    if isinstance(rec, dict):
      # The immutable install identity, safe for every caller: the content hash
      # is the authoritative locator (a mutable/redirected repo URL is not).
      row["commit"] = rec.get("commit")
      row["source_repo"] = rec.get("repo")
      row["source_path"] = rec.get("path")
      row["skill_sha256"] = rec.get("skill_sha256")
      # The installer-owned bounded file inventory (relative paths incl.
      # SKILL.md). The companion app assesses installed-skill compatibility from
      # THIS authoritative list — the paginated shared-list walk can silently
      # omit names its narrower path regex rejects, so it can't be trusted as
      # complete. Safe for every caller (filenames only, no secrets).
      files = rec.get("files")
      row["files"] = files if isinstance(files, list) else None
      # The raw submitted URL can carry query credentials — full only for the
      # owner; app callers get a redacted origin/path locator.
      url = rec.get("url")
      row["source_url"] = url if is_owner else _redact_source_url(url)
    out.append(row)
  return {"skills": out}


@router.post("/install", status_code=201, dependencies=[Depends(reject_cross_site)])
async def install_skill(
  body: SkillInstall,
  _: models.Owner = Depends(get_owner_or_app_with_manage_skills),
) -> dict:
  """Fetch a skill (SKILL.md dir or single markdown) from GitHub into the tree.

  Basename collision with any existing skill (seed / agent / app / installed) is
  a 409 with the existing provenance — the agent or owner resolves it rather
  than an overwrite happening silently.
  """
  skills_dir = _skills_dir()
  skills_dir.mkdir(parents=True, exist_ok=True)

  # follow_redirects=False — install._http_get walks the chain itself so every
  # hop is SSRF-revalidated and IP-pinned.
  async with httpx.AsyncClient(follow_redirects=False, timeout=install._HTTP_TIMEOUT) as client:
    name, files, source, commit = await _fetch_files(client, body)

  if "SKILL.md" not in files or not files["SKILL.md"].strip():
    raise HTTPException(400, "Resolved skill has no SKILL.md content.")

  async with fs_locks.shared_skills_lock():
    # Repair anything a previous crash left behind before deciding collisions.
    skills.reconcile_installed(skills_dir)

    # ANY existing dirent at either skill shape is a collision — including a
    # symlink or non-skill leftover. Deciding by lstat (never following the
    # entry) closes the redirect hole where a pre-existing link at the target
    # name could route install writes outside shared/skills.
    target_dir = skills_dir / name
    for existing in (target_dir, skills_dir / f"{name}.md"):
      if _entry_kind(existing) != "absent":
        raise HTTPException(
          409,
          f"A skill named {name!r} already exists "
          f"(provenance: {_provenance_of(skills_dir, name)}). Remove or "
          "rename it first, or install under a different `name`.",
        )

    # One crash-recoverable transition, in four durable steps:
    #   1. stage the complete bounded tree in a fresh dot-prefixed dir
    #   2. persist an 'installing' intent (with the staging name) in the
    #      sidecar — from here on, ANY crash leaves a self-describing state
    #      that reconcile_installed() repairs on boot or the next mutation
    #   3. publish with a single atomic rename
    #   4. finalize the record (drop the intent markers)
    # A failure before 3 publishes nothing; a crash after 3 leaves a visible
    # skill WITH a record that reconciles to owned — never an orphan that
    # blocks retries and refuses uninstall.
    record = {
      "source": source,
      "repo": body.repo,
      "path": body.path,
      "ref": body.ref if body.repo else None,
      "commit": commit,
      "url": body.url,
      # The immutable identity of the reviewed entry document — for raw-URL
      # installs (no commit to pin) this is the only revision evidence.
      "skill_sha256": hashlib.sha256(files["SKILL.md"]).hexdigest(),
      "files": sorted(files.keys()),
      "installed_at": datetime.now(UTC).isoformat(),
    }

    staged = Path(tempfile.mkdtemp(prefix=".staging-", dir=skills_dir))
    try:
      for rel, data in files.items():
        # rel is a validated relative path (SKILL.md at the root, or a vetted
        # resource path — _resource_rel_ok rejects traversal and dot
        # segments); atomic_write creates the intermediate directories.
        atomic_write(staged / rel, data)
      _chown_mobius(staged)
    except OSError as exc:
      shutil.rmtree(staged, ignore_errors=True)
      raise HTTPException(
        500,
        f"Install of {name!r} failed while staging files ({exc}); nothing "
        "was published.",
      )

    try:
      records = _load_installed_sidecar(skills_dir)
    except _CorruptSidecar as exc:
      shutil.rmtree(staged, ignore_errors=True)
      raise HTTPException(
        500,
        f"Refusing to install {name!r}: the installed-skills ownership record "
        f"is corrupt ({exc}). Repair or remove "
        f"shared/skills/{skills.INSTALLED_SKILLS_SIDECAR} first — overwriting "
        "it would orphan every already-installed skill. Nothing was published.",
      )
    records[name] = {**record, "status": "installing", "staging": staged.name}
    try:
      _write_installed_sidecar(skills_dir, records)
    except Exception as exc:  # noqa: BLE001
      shutil.rmtree(staged, ignore_errors=True)
      raise HTTPException(
        500,
        f"Install of {name!r} could not record its intent ({exc}); nothing "
        "was published.",
      )

    try:
      os.rename(staged, target_dir)
    except OSError as exc:
      records.pop(name, None)
      try:
        _write_installed_sidecar(skills_dir, records)
      except Exception:  # noqa: BLE001 — reconcile drops the intent later
        log.exception("could not withdraw install intent for %r", name)
      shutil.rmtree(staged, ignore_errors=True)
      raise HTTPException(
        500,
        f"Install of {name!r} failed to publish ({exc}); nothing was "
        "published.",
      )

    records[name] = record
    try:
      _write_installed_sidecar(skills_dir, records)
    except Exception as exc:  # noqa: BLE001
      # The skill IS published and its intent record IS durable — say exactly
      # that. reconcile_installed() finalizes it on the next operation; no
      # claim of a rollback that didn't happen.
      raise HTTPException(
        500,
        f"{name!r} was published but its install record could not be "
        f"finalized ({exc}); it will reconcile automatically on the next "
        "skills operation.",
      )

    warnings = _refresh_index_with_warning(skills_dir)

  log.info("installed skill %r from %s (%d file(s))", name, source, len(files))
  return {
    "name": name,
    "source": source,
    "commit": commit,
    "files": sorted(files.keys()),
    "warnings": warnings,
  }


def _refresh_index_with_warning(skills_dir: Path) -> list[str]:
  """Regenerate skills-index.md from the locked final state.

  Runs INSIDE the skills lock so the written snapshot can never interleave
  with a concurrent mutation, and never raises: by the time it runs the
  install/uninstall has durably succeeded, so an index failure must degrade
  to a truthful warning in the response — not a 500 that makes the caller
  retry a mutation that already happened.
  """
  try:
    skills.write_index(skills_dir)
    return []
  except Exception as exc:  # noqa: BLE001
    log.warning("skills index regeneration failed", exc_info=True)
    return [
      f"skills-index.md regeneration failed ({exc}); the index will refresh "
      "on the next install, uninstall, or boot.",
    ]


class CatalogRefreshBody(BaseModel):
  force: bool = False


def _catalog_sources_override(db: Session) -> list[dict] | None:
  """The Skills app's saved Browse-source list, if it has one.

  The app's catalog sources are app data — a `sources.json` in its storage
  (`/data/apps/<id>/`) overrides its defaults. The cached catalog index should
  scan the same list the owner actually browses, so pick the override up here;
  None falls back to `catalog_index.CATALOG_SOURCES` (the mirror of the app's
  defaults).
  """
  try:
    app_row = db.query(models.App).filter(models.App.slug == "skills").first()
    if app_row is None:
      return None
    raw = (
      Path(get_settings().data_dir) / "apps" / str(app_row.id) / "sources.json"
    ).read_text(encoding="utf-8")
    data = json.loads(raw)
  except (OSError, ValueError):
    return None
  if not isinstance(data, list):
    return None
  cleaned = [s for s in data if isinstance(s, dict) and s.get("repo")]
  return cleaned or None


@router.post("/catalog-index/refresh", dependencies=[Depends(reject_cross_site)])
async def refresh_catalog_index(
  body: CatalogRefreshBody | None = None,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_skills),
) -> dict:
  """Regenerate the agent's cached catalog index (shared/skills/catalog-index.md).

  Gated to once per 24h unless `force` — the Skills app fires this
  fire-and-forget whenever the owner opens the Browse screen, so the gate is
  what keeps casual browsing from hammering GitHub.
  """
  return await catalog_index.refresh(
    force=bool(body and body.force),
    sources=_catalog_sources_override(db),
  )


@router.delete("/{name}", dependencies=[Depends(reject_cross_site)])
async def uninstall_skill(
  name: str,
  _: models.Owner = Depends(get_owner_or_app_with_manage_skills),
) -> dict:
  """Remove an install-provenance skill after git-snapshotting its bytes.

  Only skills recorded in `.installed-skills.json` can be removed here — a seed
  or agent-authored skill keeps its own lifecycle and returns 409.
  """
  if _SKILL_NAME_OK.fullmatch(name) is None:
    raise HTTPException(400, "Invalid skill name.")
  skills_dir = _skills_dir()
  data_dir = Path(get_settings().data_dir)

  async with fs_locks.shared_skills_lock():
    # A crash-interrupted install reconciles first, so its skill is either a
    # properly owned record (removable here) or gone — never a stuck orphan.
    skills.reconcile_installed(skills_dir)
    try:
      records = _load_installed_sidecar(skills_dir)
    except _CorruptSidecar as exc:
      raise HTTPException(
        500,
        f"Refusing to uninstall {name!r}: the installed-skills ownership "
        f"record is corrupt ({exc}). Repair or remove "
        f"shared/skills/{skills.INSTALLED_SKILLS_SIDECAR} first. Nothing was "
        "deleted.",
      )
    if name not in records:
      raise HTTPException(
        409,
        f"{name!r} is not an installed skill — only skills added via "
        "/api/skills/install can be uninstalled here.",
      )
    target = skills_dir / name
    flat = skills_dir / f"{name}.md"
    target_kind = _entry_kind(target)
    flat_kind = _entry_kind(flat)

    # Only the two expected shapes are ever deleted. A symlink (or any other
    # surprise dirent) is refused outright: rmtree on a link deletes nothing,
    # and dropping the ownership record anyway would report success while the
    # entry survives — or worse, a follow would delete through the link.
    if target_kind not in ("absent", "dir"):
      raise HTTPException(
        409,
        f"Refusing to remove {name!r}: shared/skills/{name} is a "
        f"{target_kind}, not an installed skill directory. Inspect and "
        "remove it manually; the install record was kept.",
      )
    if target_kind == "absent" and flat_kind not in ("absent", "file"):
      raise HTTPException(
        409,
        f"Refusing to remove {name!r}: shared/skills/{name}.md is a "
        f"{flat_kind}, not a regular file. Inspect and remove it manually; "
        "the install record was kept.",
      )

    if target_kind == "dir" and (data_dir / ".git").is_dir():
      try:
        ok, detail = await asyncio.to_thread(_snapshot_skill_dir, data_dir, name)
      except Exception as exc:  # pragma: no cover - defensive
        ok, detail = False, repr(exc)
      if not ok:
        raise HTTPException(
          500,
          f"Refusing to remove {name!r}: could not snapshot its bytes into "
          f"git first ({detail}). Nothing was deleted.",
        )

    # Deletion failures surface as errors WITH the record kept — never a
    # success report for a deletion that didn't happen.
    try:
      if target_kind == "dir":
        shutil.rmtree(target)
      elif flat_kind == "file":
        # Defensive: an installed record for a flat file (older shape).
        flat.unlink()
    except OSError as exc:
      raise HTTPException(
        500,
        f"Could not delete {name!r} ({exc}); the install record was kept.",
      )

    # The ownership record goes only once the entry is provably gone.
    removed_kind = target_kind if target_kind == "dir" else flat_kind
    check = target if target_kind == "dir" else flat
    if removed_kind != "absent" and _entry_kind(check) != "absent":
      raise HTTPException(
        500,
        f"{name!r} is still present after deletion; the install record "
        "was kept.",
      )
    records.pop(name, None)
    _write_installed_sidecar(skills_dir, records)
    warnings = _refresh_index_with_warning(skills_dir)

  log.info("uninstalled skill %r", name)
  return {"removed": name, "warnings": warnings}
