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
import json
import logging
import os
import pwd
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, catalog_index, install, models, skills
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner_or_app,
  get_owner_or_app_with_manage_skills,
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


def _read_installed_sidecar(skills_dir: Path) -> dict:
  path = skills_dir / skills.INSTALLED_SKILLS_SIDECAR
  try:
    loaded = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return {}
  return loaded if isinstance(loaded, dict) else {}


def _write_installed_sidecar(skills_dir: Path, records: dict) -> None:
  atomic_write(
    skills_dir / skills.INSTALLED_SKILLS_SIDECAR,
    json.dumps(records, indent=2, sort_keys=True) + "\n",
  )


def _existing_basenames(skills_dir: Path) -> set[str]:
  """Every on-disk skill basename (flat `<name>.md` stem or dir `<name>`)."""
  names: set[str] = set()
  if not skills_dir.is_dir():
    return names
  for entry in skills_dir.iterdir():
    if entry.name.startswith("."):
      continue
    if entry.is_dir() and (entry / "SKILL.md").is_file():
      names.add(entry.name)
    elif entry.is_file() and entry.suffix == ".md" and entry.name != skills.INDEX_FILENAME:
      names.add(entry.stem)
  return names


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


async def _github_contents(
  client: httpx.AsyncClient, repo: str, path: str, ref: str,
) -> list | dict:
  """GitHub contents API for a repo path. Returns a list (dir) or dict (file)."""
  if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
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
  from urllib.parse import quote

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
) -> tuple[str, dict[str, bytes], str]:
  """Resolve the request into (skill_name, {relpath: bytes}, source_label).

  `relpath` is always relative to the skill directory (SKILL.md at the root;
  resources may live in subdirectories). Exactly one skill is produced; the
  SKILL.md file is required.
  """
  files: dict[str, bytes] = {}

  # Direct raw URL → a single markdown file becomes this skill's SKILL.md.
  if body.url:
    segments = body.url.split("?", 1)[0].split("/")
    name = _derive_name(body.name, segments)
    data = await install._http_get(client, body.url, max_bytes=SKILL_MAX_BYTES)
    files["SKILL.md"] = data
    return name, files, _source_label(body.url)

  if not (body.repo and body.path):
    raise HTTPException(
      400, "Provide either `url`, or both `repo` and `path`.",
    )

  listing = await _github_contents(client, body.repo, body.path, body.ref)
  segments = body.path.strip("/").split("/")

  if isinstance(listing, dict):
    # A single file path. Treat as this skill's SKILL.md.
    name = _derive_name(body.name, segments)
    download = listing.get("download_url")
    if not download:
      raise HTTPException(502, "GitHub file entry missing download_url.")
    files["SKILL.md"] = await install._http_get(
      client, download, max_bytes=SKILL_MAX_BYTES,
    )
    return name, files, _source_label(body.repo)

  # A directory: enumerate the WHOLE subtree with one scoped git-trees call —
  # the ecosystem keeps scripts/references in subdirectories, which the
  # top-level contents listing above cannot see — then fetch SKILL.md
  # (case-insensitive, root only) plus bounded resources.
  name = _derive_name(body.name, segments)
  clean_path = body.path.strip("/")
  tree = await _github_tree(client, body.repo, clean_path, body.ref)

  from urllib.parse import quote

  def _raw_url(rel: str) -> str:
    prefix = f"{quote(clean_path, safe='/')}/" if clean_path else ""
    return (
      "https://raw.githubusercontent.com/"
      f"{body.repo}/{quote(body.ref, safe='')}/{prefix}{quote(rel, safe='/')}"
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

  return name, files, _source_label(body.repo)


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


@router.get("")
def list_skills(_: models.Owner = Depends(get_current_owner_or_app)) -> dict:
  """Every installed skill with metadata, provenance, and recent usage counts.

  Drives the Skills mini-app's "installed" view. Readable by the owner or any
  app token (browsing is not privileged; only install/uninstall are gated).
  """
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
  out = []
  for skill in skills.enumerate_skills(skills_dir):
    disk_name = (
      skill.read_path.parent.name if skill.is_dir else skill.read_path.stem
    )
    out.append({
      "name": skill.name,
      "id": disk_name,
      "description": skill.description,
      "provenance": skill.provenance,
      "is_dir": skill.is_dir,
      "uses_30d": counts.get(skill.name, 0) or counts.get(disk_name, 0),
    })
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
    name, files, source = await _fetch_files(client, body)

  if "SKILL.md" not in files or not files["SKILL.md"].strip():
    raise HTTPException(400, "Resolved skill has no SKILL.md content.")

  async with fs_locks.shared_skills_lock():
    if name in _existing_basenames(skills_dir):
      raise HTTPException(
        409,
        f"A skill named {name!r} already exists "
        f"(provenance: {_provenance_of(skills_dir, name)}). Remove or rename "
        "it first, or install under a different `name`.",
      )
    target_dir = skills_dir / name
    for rel, data in files.items():
      # rel is a validated relative path (SKILL.md at the root, or a vetted
      # resource path — _resource_rel_ok rejects traversal and dot segments);
      # atomic_write creates the intermediate directories.
      atomic_write(target_dir / rel, data)
    _chown_mobius(target_dir)

    records = _read_installed_sidecar(skills_dir)
    records[name] = {
      "source": source,
      "repo": body.repo,
      "path": body.path,
      "url": body.url,
      "files": sorted(files.keys()),
      "installed_at": datetime.now(UTC).isoformat(),
    }
    _write_installed_sidecar(skills_dir, records)

  skills.write_index(skills_dir)
  log.info("installed skill %r from %s (%d file(s))", name, source, len(files))
  return {"name": name, "source": source, "files": sorted(files.keys())}


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
    records = _read_installed_sidecar(skills_dir)
    if name not in records:
      raise HTTPException(
        409,
        f"{name!r} is not an installed skill — only skills added via "
        "/api/skills/install can be uninstalled here.",
      )
    target = skills_dir / name
    if target.exists() and (data_dir / ".git").is_dir():
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
    if target.is_dir():
      import shutil

      shutil.rmtree(target, ignore_errors=True)
    elif target.with_suffix(".md").is_file():
      # Defensive: an installed record for a flat file (older shape).
      target.with_suffix(".md").unlink(missing_ok=True)
    records.pop(name, None)
    _write_installed_sidecar(skills_dir, records)

  skills.write_index(skills_dir)
  log.info("uninstalled skill %r", name)
  return {"removed": name}
