"""Skills enumeration, metadata parsing, provenance, and the generated index.

The agent's how-to knowledge lives as markdown under `/data/shared/skills/`,
seeded create-if-absent by `backend/scripts/init_skills.py` and agent-editable
like memory. This module is the read-side + index-side of that layer; it does not
seed or own the delicate app-manifest sync (that stays in `install.py`).

Two on-disk shapes are recognized, so Möbius is natively compatible with the
external `SKILL.md`-directory convention (agentskills.io / Anthropic Agent Skills
/ the Hermes catalogs) without abandoning the historical flat files:

  shared/skills/<name>.md            legacy flat skill (seed + agent + app)
  shared/skills/<name>/SKILL.md      directory skill (+ optional resource files)

`SKILL.md` directories may carry YAML frontmatter (`name`, `description`,
`license`, `metadata`); a flat file usually has none, so metadata falls back to
the filename + first heading/paragraph. Parsing is deliberately dependency-free
(no PyYAML) in the same spirit as `manifest_contract.py`: only the flat scalar
keys we surface are read.

Provenance is informational — where a skill came from — computed from the two
installer-owned sidecars plus the baked seed name set:

  app:<slug>          declared by an installed mini-app (`.app-skills.json`)
  installed:<source>  pulled from an online source (`.installed-skills.json`)
  seed                ships in the platform seed tree
  agent               written or renamed by the agent/owner in place

`write_index()` renders `skills-index.md` — the cheap, progressive-disclosure
tier-1 list both providers Read (the Hermes `skills_list()` idea, file-shaped),
replacing the hand-maintained table that used to live in `skill/core.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.storage_io import atomic_write

# Installer-owned sidecars (kept in sync with the literals in install.py and
# routes/skills.py — a dotfile that is not `*.md`, so no skill loader lists it).
APP_SKILLS_SIDECAR = ".app-skills.json"
INSTALLED_SKILLS_SIDECAR = ".installed-skills.json"

# The generated tier-1 index. Written into the skills dir so the agent Reads it
# by a stable relative path (`shared/skills/skills-index.md`). Excluded from
# enumeration so the index never lists itself.
INDEX_FILENAME = "skills-index.md"

# The generated catalog cache (`app.catalog_index` writes it; defined here so
# every consumer shares one reservation list without a circular import).
CATALOG_INDEX_FILENAME = "catalog-index.md"

# Stems of the generated files, for the runners' usage accounting: Reading a
# generated index is consulting a listing, never loading a skill.
GENERATED_INDEX_STEMS = frozenset({"skills-index", "catalog-index"})

# Names inside the skills dir that are never skills.
_RESERVED_NAMES = frozenset({
  APP_SKILLS_SIDECAR,
  INSTALLED_SKILLS_SIDECAR,
  INDEX_FILENAME,
  CATALOG_INDEX_FILENAME,
  ".seed-version",
  ".inactive",
})

# Baked seed skill names, resolved once. Used only to label provenance; a
# missing seed dir (unusual) just means seed-authored files read as "agent",
# which is harmless for an informational label.
_SEED_CANDIDATES = (
  Path("/app/scripts/seed-skills"),
  Path(__file__).resolve().parent.parent / "scripts" / "seed-skills",
)

# Bounds for the frontmatter/description read — a skill is instruction prose, so
# we never need to slurp a large file just to label it.
_META_READ_BYTES = 16 * 1024
_DESCRIPTION_MAX = 300


@dataclass(frozen=True)
class Skill:
  """One enumerated skill and everything the index/API surfaces need."""

  name: str
  description: str
  provenance: str
  # Path the agent Reads (the flat file, or the dir's SKILL.md).
  read_path: Path
  # True for the `<name>/SKILL.md` directory shape.
  is_dir: bool
  # Extra frontmatter scalars we parsed but don't model explicitly.
  metadata: dict = field(default_factory=dict)


def _seed_names() -> set[str]:
  """Skill base-names present in the baked seed tree (`<stem>`)."""
  for candidate in _SEED_CANDIDATES:
    if candidate.is_dir():
      names = {p.stem for p in candidate.glob("*.md")}
      names |= {p.name for p in candidate.iterdir() if (p / "SKILL.md").is_file()}
      return names
  return set()


def _read_sidecar(path: Path) -> dict:
  """Best-effort JSON sidecar read; a missing/corrupt sidecar reads as {}."""
  import json

  try:
    loaded = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return {}
  return loaded if isinstance(loaded, dict) else {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
  """Split leading `---`-delimited YAML frontmatter from the body.

  Returns (scalars, body). Only flat `key: value` scalar lines are read —
  enough for `name`/`description`/`license`/`metadata`-flat keys — because that
  is all we surface and staying dependency-free avoids a PyYAML requirement on
  the recovery-adjacent import path. A file without a well-formed frontmatter
  block returns ({}, original-text).
  """
  if not text.startswith("---"):
    return {}, text
  # The opening fence must be its own line (`---` then newline).
  first_nl = text.find("\n")
  if first_nl == -1 or text[:first_nl].strip() != "---":
    return {}, text
  rest = text[first_nl + 1 :]
  end = rest.find("\n---")
  if end == -1:
    return {}, text
  block = rest[:end]
  # Body starts after the closing fence line.
  after = rest[end + 1 :]
  body_nl = after.find("\n")
  body = after[body_nl + 1 :] if body_nl != -1 else ""
  scalars: dict = {}
  for line in block.splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
      continue
    if ":" not in line:
      continue
    key, _, value = line.partition(":")
    key = key.strip()
    value = value.strip().strip('"').strip("'").strip()
    if key and value:
      scalars[key] = value
  return scalars, body


def _fallback_description(body: str) -> str:
  """First meaningful paragraph of a skill body, for a metadata-less file.

  Joins the paragraph's wrapped lines (up to the first blank line) so a
  hard-wrapped source file doesn't truncate its description mid-sentence.
  """
  collected: list[str] = []
  for raw in body.splitlines():
    line = raw.strip()
    if not line:
      if collected:
        break
      continue
    if line.startswith("#") or line.startswith("---"):
      if collected:
        break
      continue
    collected.append(line)
    if sum(len(part) for part in collected) >= _DESCRIPTION_MAX:
      break
  return " ".join(collected)[:_DESCRIPTION_MAX]


def _read_meta(read_path: Path, default_name: str) -> tuple[str, str, dict]:
  """(name, description, extra-metadata) for one skill file."""
  try:
    with read_path.open("r", encoding="utf-8", errors="replace") as handle:
      text = handle.read(_META_READ_BYTES)
  except OSError:
    return default_name, "", {}
  scalars, body = _parse_frontmatter(text)
  name = scalars.pop("name", None) or default_name
  description = scalars.pop("description", None) or _fallback_description(body)
  return name, description[:_DESCRIPTION_MAX], scalars


def _provenance(
  sidecar_key: str,
  base_name: str,
  app_owned: dict[str, str],
  installed: dict[str, str],
  seed_names: set[str],
) -> str:
  """Label where a skill came from. App/installed win over the seed set.

  `sidecar_key` is the key shape the sidecars use (`<name>.md` for flat app
  skills, bare `<name>` for installed dir skills); `base_name` is the bare
  name the seed set uses (file stem / dir name). Both are needed — comparing
  one against the other silently mislabels every seed skill as `agent`.
  """
  if sidecar_key in app_owned:
    return f"app:{app_owned[sidecar_key]}"
  if sidecar_key in installed or base_name in installed:
    src = installed.get(sidecar_key) or installed.get(base_name) or ""
    return f"installed:{src}" if src else "installed"
  if base_name in seed_names:
    return "seed"
  return "agent"


def _skills_dir() -> Path:
  from app.config import get_settings

  return Path(get_settings().data_dir) / "shared" / "skills"


def reconcile_installed(skills_dir: Path | None = None) -> list[str]:
  """Repair interrupted installs recorded in the installed-skills sidecar.

  The installer persists an ``"status": "installing"`` intent (carrying its
  staging directory name) BEFORE publishing, so a crash at any point leaves a
  self-describing state this sweep repairs:

    intent + published dir   -> the atomic rename happened; finalize the record
    intent + staging dir     -> the crash preceded publish; discard the staging
    intent + neither         -> nothing durable happened; drop the record

  Runs at boot (init_skills) and at the start of every install/uninstall
  (under the shared-skills lock), so an orphan can never outlive the next
  skills operation. Returns the names it repaired.
  """
  import json
  import shutil

  root = skills_dir or _skills_dir()
  sidecar = root / INSTALLED_SKILLS_SIDECAR
  records = _read_sidecar(sidecar)
  repaired: list[str] = []
  for name, rec in list(records.items()):
    if not isinstance(rec, dict) or rec.get("status") != "installing":
      continue
    target = root / str(name)
    staging_name = str(rec.get("staging") or "")
    if target.is_dir() and not target.is_symlink():
      rec.pop("status", None)
      rec.pop("staging", None)
    else:
      if staging_name:
        staging = root / staging_name
        if staging.is_dir() and not staging.is_symlink():
          shutil.rmtree(staging, ignore_errors=True)
      records.pop(name)
    repaired.append(str(name))
  if repaired:
    atomic_write(sidecar, json.dumps(records, indent=2, sort_keys=True) + "\n")
  return repaired


def enumerate_skills(skills_dir: Path | None = None) -> list[Skill]:
  """All installed skills (flat + directory), sorted by name.

  Reads the two installer sidecars once for provenance. Never raises on a
  malformed individual skill — a bad entry is skipped rather than failing the
  whole listing, because this feeds both the index write and a live API.
  """
  root = skills_dir or _skills_dir()
  if not root.is_dir():
    return []

  app_sidecar = _read_sidecar(root / APP_SKILLS_SIDECAR)
  app_owned: dict[str, str] = {}
  for rel, rec in app_sidecar.items():
    if isinstance(rec, dict) and rec.get("active", True):
      app_owned[rel] = str(rec.get("slug") or rec.get("app_id") or "")
  installed_sidecar = _read_sidecar(root / INSTALLED_SKILLS_SIDECAR)
  installed: dict[str, str] = {}
  for name, rec in installed_sidecar.items():
    if isinstance(rec, dict):
      installed[name] = str(rec.get("source") or "")
  seed_names = _seed_names()

  skills: list[Skill] = []
  for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
    if entry.name in _RESERVED_NAMES or entry.name.startswith("."):
      continue
    if entry.is_dir():
      read_path = entry / "SKILL.md"
      if not read_path.is_file():
        continue
      base_name = entry.name
      is_dir = True
      # App-owned flat skills key by `<name>.md`; a dir skill keys by its name.
      sidecar_key = base_name
    elif entry.is_file() and entry.suffix == ".md":
      read_path = entry
      base_name = entry.stem
      is_dir = False
      sidecar_key = entry.name
    else:
      continue
    name, description, metadata = _read_meta(read_path, base_name)
    skills.append(
      Skill(
        name=name,
        description=description,
        provenance=_provenance(
          sidecar_key, base_name, app_owned, installed, seed_names,
        ),
        read_path=read_path,
        is_dir=is_dir,
        metadata=metadata,
      )
    )
  return skills


def _index_body(skills: list[Skill]) -> str:
  """Render the tier-1 index markdown from enumerated skills."""
  lines = [
    "# Skills index",
    "",
    "Generated — do not hand-edit; regenerated on boot, skill install, and "
    "app install. The detailed how-to lives in each skill file; **`Read` the "
    "skill before that kind of work** rather than working from memory of a "
    "contract that may have changed.",
    "",
  ]
  if not skills:
    lines.append("_No skills installed._")
    return "\n".join(lines) + "\n"
  lines.append("| Skill | Read it before... | Source |")
  lines.append("|---|---|---|")
  for skill in skills:
    read_ref = (
      f"`shared/skills/{skill.read_path.parent.name}/SKILL.md`"
      if skill.is_dir
      else f"`shared/skills/{skill.read_path.name}`"
    )
    # Name and description originate in third-party frontmatter — one table
    # cell each, whatever they contain: newlines/pipes escaped, length capped.
    title = _table_cell(skill.name, 100)
    desc = _table_cell(skill.description, _DESCRIPTION_MAX)
    lines.append(f"| {read_ref} — {title} | {desc} | {skill.provenance} |")
  return "\n".join(lines) + "\n"


def _table_cell(text: str, max_len: int) -> str:
  """Bound an untrusted string into a single Markdown table cell."""
  line = "".join(ch if ch >= " " else " " for ch in str(text or ""))
  line = " ".join(line.split()).replace("|", "\\|")
  return line[: max_len - 1] + "…" if len(line) > max_len else line


def write_index(skills_dir: Path | None = None) -> Path | None:
  """(Re)write `skills-index.md`. Returns the path, or None if no skills dir.

  Best-effort and side-effect-only: callers on hot paths (install, boot) treat
  a failure as non-fatal. Mirrors init_skills' 0o664 group-writable convention
  so the agent can still hand-edit or delete the generated file.
  """
  root = skills_dir or _skills_dir()
  if not root.is_dir():
    return None
  index_path = root / INDEX_FILENAME
  atomic_write(index_path, _index_body(enumerate_skills(root)))
  try:
    os.chmod(index_path, 0o664)
  except OSError:
    pass
  return index_path
