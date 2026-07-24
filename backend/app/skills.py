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

import hashlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.manifest_contract import SKILL_MAX_BYTES
from app.storage_io import atomic_write

log = logging.getLogger(__name__)

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


# Installer staging directories live under the skills root as `.staging-*`
# (see routes/skills.install_skill). Dot-prefixed, so enumeration never lists
# them; recovery is the only thing that touches them.
_STAGING_PREFIX = ".staging-"
# An unreferenced staging tree younger than this may belong to an install still
# in flight, so GC leaves it alone; older ones are crash orphans.
_STAGING_GC_AGE_SECONDS = 3600

# One install/recovery tree contract. The writer and the recovery reader share
# these bounds so recovery never accepts or loads a shape the installer could
# not have published.
RESOURCE_COUNT_MAX = 24
RESOURCE_TOTAL_MAX = 2 * 1024 * 1024
RESOURCE_MAX_DEPTH = 4
RESOURCE_SUFFIXES = frozenset({
  ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".py", ".js", ".ts",
  ".sh", ".toml", ".html", ".css",
})
TREE_FILE_COUNT_MAX = 1 + RESOURCE_COUNT_MAX  # root SKILL.md + resources
TREE_TOTAL_BYTES_MAX = SKILL_MAX_BYTES + RESOURCE_TOTAL_MAX

# One canonical identity for a complete skill tree: SKILL.md plus every
# resource, path AND bytes. Versioned so recovery can tell a recognized record
# from a malformed/legacy one — which it treats as corrupt and never adopts,
# rather than inferring ownership around missing fields.
_TREE_DIGEST_VERSION = "sha256-tree-v1"
_TREE_DIGEST_PREFIX = _TREE_DIGEST_VERSION + ":"


def tree_digest_from_files(files: dict[str, bytes]) -> str:
  """Canonical digest over a complete skill tree.

  Folds every root-relative path AND its exact bytes into one SHA-256, framed
  by length so no path/content boundary is ambiguous and no two distinct trees
  collide by concatenation. The installer computes this from the bytes it is
  about to publish; recovery recomputes it from the published tree — so a byte
  changed in ANY file (not just SKILL.md) yields a different digest and a
  tampered tree is never adopted as the reviewed one. The version tag is the
  record's proof of a recognized shape.
  """
  digest = hashlib.sha256()
  digest.update(_TREE_DIGEST_VERSION.encode("utf-8"))
  for rel in sorted(files):
    rel_bytes = rel.encode("utf-8")
    data = files[rel]
    digest.update(len(rel_bytes).to_bytes(8, "big"))
    digest.update(rel_bytes)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
  return _TREE_DIGEST_PREFIX + digest.hexdigest()


class CorruptInstalledSkillsSidecar(Exception):
  """The ownership sidecar exists but cannot be trusted as an object."""


def load_installed_sidecar(skills_dir: Path) -> dict:
  """Strict installed-skills sidecar read for every mutating path.

  Missing means an empty installation set. Present-but-unreadable, invalid
  JSON, or a non-object is corruption: callers must stop rather than infer an
  empty set and mutate owner data from that false premise.
  """
  path = skills_dir / INSTALLED_SKILLS_SIDECAR
  try:
    raw = path.read_text(encoding="utf-8")
  except FileNotFoundError:
    return {}
  except OSError as exc:
    raise CorruptInstalledSkillsSidecar(
      f"could not read {path.name}: {exc}",
    ) from exc
  try:
    loaded = json.loads(raw)
  except ValueError as exc:
    raise CorruptInstalledSkillsSidecar(
      f"{path.name} is not valid JSON: {exc}",
    ) from exc
  if not isinstance(loaded, dict):
    raise CorruptInstalledSkillsSidecar(f"{path.name} is not a JSON object")
  return loaded


def resource_rel_ok(rel: str) -> bool:
  """Whether an installer/recovery resource path belongs in a skill tree."""
  segments = rel.split("/")
  if not 1 <= len(segments) <= RESOURCE_MAX_DEPTH:
    return False
  for segment in segments:
    if not segment or segment.startswith(".") or "\\" in segment:
      return False
  return Path(rel).suffix.lower() in RESOURCE_SUFFIXES


def _safe_child(root: Path, name: str) -> Path | None:
  """A single-component child of `root`, or None if `name` would escape it.

  The installed-skills sidecar lives in the agent-editable shared tree, so its
  `name` keys and `staging` values are untrusted: a value like ``../victim`` or
  ``a/b`` must never steer a recovery delete or finalize outside `root`. `name`
  must be one plain path component (no separators, not ``.``/``..``) whose
  lexical parent is exactly `root`.
  """
  if not name or name in (".", "..") or "/" in name or "\\" in name:
    return None
  if os.sep in name or (os.altsep and os.altsep in name):
    return None
  candidate = root / name
  return candidate if candidate.parent == root else None


def _is_real_dir(path: Path) -> bool:
  """True only for a real directory — never a symlink (which we never follow)."""
  try:
    return stat.S_ISDIR(path.lstat().st_mode)
  except OSError:
    return False


def _read_tree_bytes(target: Path) -> dict[str, bytes] | None:
  """Root-relative path -> bytes for every regular file under `target`.

  Returns None on any surprise a clean install never contains — a symlink or
  special file, malformed path, or a tree outside the exact install count/byte
  bounds — so the caller fails closed. Never follows a symlink (lstat-gated).
  """
  out: dict[str, bytes] = {}
  resource_count = 0
  resource_bytes = 0
  seen_dirs: set[str] = set()
  stack = [target]
  while stack:
    cur = stack.pop()
    try:
      entries = list(cur.iterdir())
    except OSError:
      return None
    for entry in entries:
      try:
        mode = entry.lstat().st_mode
      except OSError:
        return None
      if stat.S_ISDIR(mode):
        try:
          rel_dir = entry.relative_to(target).as_posix()
          rel_dir.encode("utf-8")
        except (UnicodeError, ValueError):
          return None
        segments = rel_dir.split("/")
        if (
          not 1 <= len(segments) < RESOURCE_MAX_DEPTH
          or any(
            not segment or segment.startswith(".") or "\\" in segment
            for segment in segments
          )
        ):
          return None
        seen_dirs.add(rel_dir)
        stack.append(entry)
      elif stat.S_ISREG(mode):
        if len(out) >= TREE_FILE_COUNT_MAX:
          return None
        try:
          rel = entry.relative_to(target).as_posix()
          # The digest format is UTF-8. A filesystem surrogate or an excessive
          # depth is not a shape the installer can create.
          rel.encode("utf-8")
          declared_size = entry.lstat().st_size
          if declared_size < 0:
            return None
          if rel == "SKILL.md":
            if declared_size > SKILL_MAX_BYTES:
              return None
          else:
            if not resource_rel_ok(rel):
              return None
            resource_count += 1
            resource_bytes += declared_size
            if (
              resource_count > RESOURCE_COUNT_MAX
              or resource_bytes > RESOURCE_TOTAL_MAX
            ):
              return None
          data = entry.read_bytes()
          if len(data) != declared_size:
            return None
          out[rel] = data
        except (OSError, UnicodeError, ValueError):
          return None
      else:
        return None
  if "SKILL.md" not in out:
    return None
  # The installer only creates parent directories for real resources. An extra
  # empty directory is a different on-disk tree even though a file-only digest
  # would otherwise overlook it.
  file_dirs = {
    "/".join(rel.split("/")[:depth])
    for rel in out
    for depth in range(1, len(rel.split("/")))
  }
  if seen_dirs != file_dirs:
    return None
  return out


def _tree_digest_on_disk(target: Path) -> str | None:
  """Canonical tree digest of a published skill dir, or None if the tree can't
  be read wholly and safely (see `_read_tree_bytes`) — the caller fails closed.
  """
  files = _read_tree_bytes(target)
  if files is None:
    return None
  return tree_digest_from_files(files)


def _gc_orphan_staging(root: Path, referenced: set[str]) -> None:
  """Remove crash-orphaned `.staging-*` trees no record references.

  A crash BETWEEN staging-dir creation and the intent write leaves a staging
  tree nothing points at; the per-record sweep can't see it. Only real
  directories (never a symlink wearing the prefix), only those older than the
  GC age (so an install still in flight is safe), only directly under `root`.
  """
  import shutil

  now = time.time()
  try:
    entries = list(root.iterdir())
  except OSError:
    return
  for entry in entries:
    if not entry.name.startswith(_STAGING_PREFIX) or entry.name in referenced:
      continue
    try:
      st = entry.lstat()
    except OSError:
      continue
    if not stat.S_ISDIR(st.st_mode):
      continue  # a symlink/file wearing the prefix — not ours to remove
    if now - st.st_mtime < _STAGING_GC_AGE_SECONDS:
      continue  # possibly an in-flight install's staging
    shutil.rmtree(entry, ignore_errors=True)


def reconcile_installed(skills_dir: Path | None = None) -> list[str]:
  """Repair interrupted installs recorded in the installed-skills sidecar.

  The installer persists an ``"status": "installing"`` intent (carrying its
  staging directory name) BEFORE publishing, so a crash leaves a state this
  sweep repairs against the ONE true post-publish invariant — *target present
  AND staging absent*:

    target present, staging absent  -> the atomic rename happened; finalize the
                                       record, but only when the COMPLETE
                                       published tree (every path and byte)
                                       hashes to the record's canonical
                                       ``tree_digest``. A missing/unversioned
                                       digest is corrupt state — leave the
                                       intent untouched, never infer ownership
    staging present, target absent  -> the crash preceded publish; discard the
                                       staging tree and the intent
    neither present                 -> nothing durable happened; drop the record
    BOTH present                    -> ambiguous (corrupt/tampered sidecar, or an
                                       unrelated dir squatting the name); finalize
                                       nothing, delete nothing, keep the intent

  Every `name`/`staging` string is treated as untrusted (the sidecar lives in
  the agent-editable shared tree) and resolved through `_safe_child`, so a
  traversal value can never steer a delete outside `root`. A separate
  age-bounded GC reclaims staging trees a crash orphaned *before* the intent
  write. Runs at boot (init_skills) and at the start of every install/uninstall
  under the shared-skills lock. Returns the names it repaired.
  """
  import shutil

  root = skills_dir or _skills_dir()
  sidecar = root / INSTALLED_SKILLS_SIDECAR
  try:
    records = load_installed_sidecar(root)
  except CorruptInstalledSkillsSidecar as exc:
    # Recovery is itself a mutating path. With no trustworthy ownership set,
    # even age-bounded GC could delete the only surviving copy of an interrupted
    # install, so do nothing until the owner repairs the record.
    log.warning("skills recovery skipped: %s", exc)
    return []

  # Staging names any record still points at — GC must not reclaim these.
  referenced: set[str] = set()
  for rec in records.values():
    if isinstance(rec, dict):
      staging_name = rec.get("staging")
      if isinstance(staging_name, str) and staging_name:
        referenced.add(staging_name)

  repaired: list[str] = []
  dirty = False
  for name, rec in list(records.items()):
    if not isinstance(rec, dict) or rec.get("status") != "installing":
      continue
    target = _safe_child(root, str(name))
    staging = _safe_child(root, str(rec.get("staging") or ""))
    target_present = target is not None and _is_real_dir(target)
    staging_present = staging is not None and _is_real_dir(staging)

    if target_present and not staging_present:
      # Post-rename invariant met. Adopt only when the complete published tree
      # hashes to exactly the digest the record committed before publishing. A
      # missing or unversioned digest is a corrupt/legacy record, not a case to
      # infer around: leave the intent untouched for deliberate repair.
      expected = rec.get("tree_digest")
      if (
        isinstance(expected, str)
        and expected.startswith(_TREE_DIGEST_PREFIX)
        and _tree_digest_on_disk(target) == expected
      ):
        rec.pop("status", None)
        rec.pop("staging", None)
        dirty = True
        repaired.append(str(name))
      continue
    if staging_present and not target_present:
      # Crash before publish: discard the confined staging tree and the intent.
      shutil.rmtree(staging, ignore_errors=True)
      records.pop(name)
      dirty = True
      repaired.append(str(name))
      continue
    if not staging_present and not target_present:
      # Nothing durable survives (or an unresolvable name): drop the intent.
      records.pop(name)
      dirty = True
      repaired.append(str(name))
      continue
    # target_present AND staging_present -> ambiguous; fail closed, keep intent.

  # Reclaim crash orphans no record references (age-bounded; see helper).
  _gc_orphan_staging(root, referenced)

  if dirty:
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
