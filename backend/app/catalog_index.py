"""Cached index of the public skill catalogs, for the agent's skill-hunting.

`shared/skills/catalog-index.md` lists every skill in the curated catalog
sources (the same list the Skills app's Browse screen scans) as one greppable
line each — name, one-line description, install coordinates. The agent greps
it before any live GitHub crawling (see the `finding-skills` seed skill), so a
"find me a skill for X" request costs one local file read instead of a chain
of GitHub API calls. It is a cache, not a registry: the file is regenerated
wholesale from GitHub, gated to once per 24h unless forced, and losing it
costs nothing but the next refresh.

Fetching reuses `install._http_get`, so the SSRF blocklist, redirect pinning,
and byte caps are the same ones the app installer trusts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from app import install, skills
from app.config import get_settings
from app.manifest_contract import SKILL_MAX_BYTES
from app.storage_io import atomic_write

log = logging.getLogger(__name__)

INDEX_FILENAME = "catalog-index.md"
FRESH_SECONDS = 24 * 3600

# Mirror of the Skills app's DEFAULT_SOURCES (app-skills/catalog.js). The app
# side can be overridden by a sources.json in the app's storage; the route
# passes that override through when present so both surfaces scan one list.
CATALOG_SOURCES: list[dict] = [
  {"label": "Anthropic Skills", "repo": "anthropics/skills", "path": "skills", "ref": "main"},
  {"label": "Anthropic Knowledge Work", "repo": "anthropics/knowledge-work-plugins", "path": "", "ref": "main"},
  {"label": "Superpowers", "repo": "obra/superpowers", "path": "skills", "ref": "main"},
  {"label": "Trail of Bits Security", "repo": "trailofbits/skills", "path": "", "ref": "main"},
  {"label": "Cloudflare", "repo": "cloudflare/skills", "path": "skills", "ref": "main"},
  {"label": "Hermes bundled", "repo": "NousResearch/hermes-agent", "path": "skills", "ref": "main"},
  {"label": "Hermes optional", "repo": "NousResearch/hermes-agent", "path": "optional-skills", "ref": "main"},
]

# Bounds: a hostile/bloated source can't turn a refresh into a crawl.
_MAX_SKILLS_PER_SOURCE = 500
_FETCH_CONCURRENCY = 8
_DESC_MAX_CHARS = 160


def index_path(skills_dir: Path | None = None) -> Path:
  root = skills_dir or Path(get_settings().data_dir) / "shared" / "skills"
  return root / INDEX_FILENAME


def is_fresh(path: Path, now: float | None = None) -> bool:
  try:
    age = (now if now is not None else datetime.now(UTC).timestamp()) - path.stat().st_mtime
  except OSError:
    return False
  return 0 <= age < FRESH_SECONDS


def _one_line(text: str) -> str:
  line = " ".join(str(text or "").split())
  return line[: _DESC_MAX_CHARS - 1] + "…" if len(line) > _DESC_MAX_CHARS else line


def describe_skill_md(raw: str, fallback_name: str) -> str:
  """One indexable line from a SKILL.md: frontmatter description, else the
  first body paragraph. A YAML block scalar (`description: >`) leaves only the
  indicator behind in the flat parser — treat that as absent too."""
  meta, body = skills._parse_frontmatter(raw)
  desc = str(meta.get("description") or "").strip()
  if not desc or desc in {">", "|", ">-", ">+", "|-", "|+"}:
    desc = skills._fallback_description(body) or f"(no description) {fallback_name}"
  return _one_line(desc)


def build_index(per_source: list[dict], generated_at: str) -> str:
  """Render the index markdown. Pure: `per_source` is a list of
  {source: {label, repo, path, ref}, skills: [{name, dir, description}],
  error: str | None} in scan order."""
  lines = [
    "# Skill catalogs index",
    "",
    f"Generated {generated_at} — do not hand-edit. A cached list of every "
    "skill in the curated public catalogs (the same sources as the Skills "
    "app's Browse screen). Hunting for a skill? **Grep this file first**; "
    "fall back to live GitHub search only for topics not covered here. Each "
    "line ends with install coordinates `(repo dir @ref)` — use them with "
    "`POST /api/skills/install` as the `finding-skills` skill describes "
    "(read the skill's full SKILL.md and do the trust ritual before "
    "installing). Refresh me with `POST /api/skills/catalog-index/refresh` "
    '(body `{"force": true}` bypasses the 24h freshness gate).',
    "",
  ]
  for entry in per_source:
    src = entry["source"]
    lines.append(f"## {src['label']} ({src['repo']}{'/' + src['path'] if src.get('path') else ''})")
    lines.append("")
    if entry.get("error"):
      lines.append(f"_Scan failed: {entry['error']}_")
    elif not entry["skills"]:
      lines.append("_No skills found._")
    else:
      for s in entry["skills"]:
        lines.append(
          f"- {s['name']} — {s['description']} "
          f"({src['repo']} {s['dir']} @{src.get('ref') or 'main'})",
        )
    lines.append("")
  return "\n".join(lines).rstrip() + "\n"


async def _scan_source(client: httpx.AsyncClient, source: dict) -> dict:
  """One source → its skill list with descriptions. Never raises: a failed
  source becomes an `error` entry, a failed description a name-only line."""
  repo, path, ref = source["repo"], source.get("path") or "", source.get("ref") or "main"
  try:
    spec = quote(f"{ref}:{path}" if path else ref, safe="")
    raw = await install._http_get(
      client,
      f"https://api.github.com/repos/{repo}/git/trees/{spec}?recursive=1",
      max_bytes=4 * 1024 * 1024,
    )
    tree = json.loads(raw).get("tree")
    if not isinstance(tree, list):
      raise ValueError("no tree in GitHub response")
  except Exception as exc:  # noqa: BLE001 — refresh must survive one bad source
    log.warning("catalog-index: scanning %s failed: %s", repo, exc)
    return {"source": source, "skills": [], "error": _one_line(str(exc))}

  dirs = sorted(
    e["path"][: -len("/SKILL.md")]
    for e in tree
    if isinstance(e, dict)
    and isinstance(e.get("path"), str)
    and e["path"].endswith("/SKILL.md")
  )[:_MAX_SKILLS_PER_SOURCE]

  sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

  async def one(rel_dir: str) -> dict:
    full_dir = f"{path}/{rel_dir}" if path else rel_dir
    name = rel_dir.rsplit("/", 1)[-1]
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{full_dir}/SKILL.md"
    try:
      async with sem:
        md = await install._http_get(client, url, max_bytes=SKILL_MAX_BYTES)
      description = describe_skill_md(md.decode("utf-8", errors="replace"), name)
    except Exception:  # noqa: BLE001
      description = "(description unavailable)"
    return {"name": name, "dir": full_dir, "description": description}

  return {
    "source": source,
    "skills": list(await asyncio.gather(*(one(d) for d in dirs))),
    "error": None,
  }


async def refresh(force: bool = False, sources: list[dict] | None = None) -> dict:
  """Regenerate catalog-index.md unless it is fresh (<24h) and not forced.

  Returns {refreshed, skills, generated_at, path}; `refreshed` False means the
  freshness gate skipped the scan.
  """
  target = index_path()
  if not force and is_fresh(target):
    return {
      "refreshed": False,
      "skills": None,
      "generated_at": datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(),
      "path": str(target),
    }

  generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
  async with httpx.AsyncClient(follow_redirects=False, timeout=install._HTTP_TIMEOUT) as client:
    per_source = [await _scan_source(client, s) for s in (sources or CATALOG_SOURCES)]

  atomic_write(target, build_index(per_source, generated_at))
  try:
    os.chmod(target, 0o664)
  except OSError:
    pass
  total = sum(len(e["skills"]) for e in per_source)
  log.info("catalog-index: wrote %s (%d skills)", target, total)
  return {"refreshed": True, "skills": total, "generated_at": generated_at, "path": str(target)}
