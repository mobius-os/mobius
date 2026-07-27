"""Materialize Möbius's shared skills into Codex's project-local skills dir.

Claude gets first-class skill auto-loading via the SDK ``skills="all"`` option
when the owner turns skills on. Codex has no such SDK switch, but its app-server
auto-discovers skills from ``<cwd>/.codex/skills/<name>/SKILL.md`` and injects
each skill's name + description into the model-visible prompt, loading the body
only when the skill activates — the same on-demand shape Claude gets. Möbius runs
Codex turns with ``cwd = data_dir``, so mirroring the shared skills into
``<data_dir>/.codex/skills`` gives Codex the same skill parity, gated on the same
``skills_enabled`` flag.

Each generated shim is a tiny ``SKILL.md``: frontmatter (``name`` + ``description``)
so Codex can match it by description, plus a body that points at the authoritative
shared-skill file. Pointing (rather than copying) keeps ONE source of truth — Codex
reads that file when the skill activates — so a shim can never drift from the real
skill, and directory skills keep their resource files. A manifest records exactly
which entries this module owns, so a later sync prunes only its own shims and never
touches Codex's built-in ``.system`` skills (which live under ``CODEX_HOME``, a
different directory that Codex merges with the project-local one).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.skills import enumerate_skills

# Marks the set of shim directories this module owns, so a disable/prune step
# removes exactly what it created and nothing a human or Codex placed by hand.
_MANIFEST = ".mobius-managed.json"
_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_dir_name(name: str) -> str | None:
  """A filesystem-safe skill directory name, or None if nothing usable remains."""
  cleaned = _UNSAFE.sub("-", str(name or "").strip()).strip("-.")
  if not cleaned or cleaned in (".", ".."):
    return None
  return cleaned


def render_shim(name: str, description: str, read_path: Path | str) -> str:
  """The SKILL.md a Codex shim carries: match metadata + a pointer to the source."""
  desc = " ".join(str(description or "").split()) or f"The {name} skill."
  return (
    "---\n"
    f"name: {name}\n"
    f"description: {desc}\n"
    "---\n\n"
    "The full, authoritative instructions for this skill are in the file\n"
    f"`{read_path}`. Read that file now and follow it exactly.\n"
  )


def _read_manifest(target: Path) -> list[str]:
  try:
    data = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return []
  names = data.get("names") if isinstance(data, dict) else None
  return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _write_manifest(target: Path, names: list[str]) -> None:
  target.mkdir(parents=True, exist_ok=True)
  (target / _MANIFEST).write_text(
    json.dumps({"names": sorted(names)}, ensure_ascii=True), encoding="utf-8"
  )


def _prune(target: Path, names: list[str]) -> None:
  """Remove managed shim directories (each is just our generated SKILL.md)."""
  for name in names:
    shim = target / name / "SKILL.md"
    try:
      shim.unlink()
    except OSError:
      pass
    try:
      (target / name).rmdir()  # only succeeds if we left it empty
    except OSError:
      pass


def sync_codex_skills(data_dir: str | Path, enabled: bool) -> list[str]:
  """Idempotently mirror shared skills into ``<data_dir>/.codex/skills``.

  Returns the sorted skill names currently materialized (empty when disabled).
  Writes a shim only when its content changed, and prunes only entries this
  module previously created, so Codex's built-in skills stay untouched. Any I/O
  error on a single skill is skipped rather than aborting the whole sync — skill
  discovery is advisory and must never break a turn from starting.
  """
  target = Path(data_dir) / ".codex" / "skills"
  previously_managed = _read_manifest(target)

  if not enabled:
    _prune(target, previously_managed)
    if target.exists():
      _write_manifest(target, [])
    return []

  try:
    skills = enumerate_skills(Path(data_dir) / "shared" / "skills")
  except Exception:
    return []

  wanted: dict[str, str] = {}
  for skill in skills:
    safe = _safe_dir_name(skill.name)
    if not safe or safe in wanted:
      continue
    wanted[safe] = render_shim(skill.name, skill.description, skill.read_path)

  target.mkdir(parents=True, exist_ok=True)
  for safe, content in wanted.items():
    shim = target / safe / "SKILL.md"
    try:
      if not shim.is_file() or shim.read_text(encoding="utf-8") != content:
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(content, encoding="utf-8")
    except OSError:
      continue

  _prune(target, [n for n in previously_managed if n not in wanted])
  names = sorted(wanted)
  _write_manifest(target, names)
  return names
