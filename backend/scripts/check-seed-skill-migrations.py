#!/usr/bin/env python3
"""Guard that superseded seed-skill text stays migratable on live installs.

``init_skills.py`` rewrites a live skill file only when its exact bytes match a
registered predecessor. Boot cannot tell an untouched older generation from an
owner-authored edit, so a change to a seeded skill must register the copy it
supersedes; otherwise every install that never edited that file keeps the older
text indefinitely.

Two modes:

``--against <rev>``   Fail when a commit between that revision's merge base and
                      HEAD replaces a seeded copy that is not registered. This
                      is the gate a candidate change runs before review; CI
                      can pin the exact base through
                      ``SEED_SKILL_MIGRATION_BASE_REF``.
``--report [skill]``  Print every shipped generation of a seed with its
                      registration state, so history can be enumerated without
                      reading ``git log`` by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = "backend/scripts/seed-skills"
REGISTRY_SOURCE = ROOT / "backend" / "scripts" / "init_skills.py"


class SupersededSeed(NamedTuple):
  name: str
  digest: str
  commit: str


def fail(message: str) -> None:
  print(f"seed-skill-migrations: {message}", file=sys.stderr)
  raise SystemExit(1)


def registered_migrations() -> dict[str, frozenset[str]]:
  spec = importlib.util.spec_from_file_location(
    "seed_skill_migration_registry", REGISTRY_SOURCE,
  )
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return {
    name: frozenset(digests)
    for name, digests in module._UNMODIFIED_MIGRATIONS.items()
  }


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
  completed = subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True,
  )
  if check and completed.returncode != 0:
    fail(completed.stderr.decode("utf-8", errors="replace").strip())
  return completed.stdout


def _blob(repo: Path, revision: str, path: str) -> bytes:
  return _git(repo, "show", f"{revision}:{path}", check=False)


def shipped_generations(
  name: str, *, repo: Path = ROOT, seed_path: str = SEED_PATH,
) -> list[tuple[str, str]]:
  """Distinct shipped digests of one seed, newest first, with their commits."""
  path = f"{seed_path}/{name}"
  generations: dict[str, str] = {}
  for line in _git(repo, "log", "--format=%H", "--", path).decode().splitlines():
    commit = line.strip()
    if not commit:
      continue
    content = _blob(repo, commit, path)
    if content:
      generations.setdefault(hashlib.sha256(content).hexdigest(), commit)
  return list(generations.items())


def superseded_seed_copies(
  against: str, *, repo: Path = ROOT, seed_path: str = SEED_PATH,
) -> list[SupersededSeed]:
  """Seeded copies replaced between that revision's merge base and HEAD."""
  base = _git(repo, "merge-base", against, "HEAD").decode().strip()
  if not base:
    fail(f"{against} and HEAD have no merge base")
  found: dict[tuple[str, str], str] = {}
  commit = ""
  log = _git(
    repo, "log", "--format=%H", "--name-only", f"{base}..HEAD", "--", seed_path,
  ).decode()
  for line in log.splitlines():
    line = line.strip()
    if not line:
      continue
    if len(line) == 40 and all(character in "0123456789abcdef" for character in line):
      commit = line
      continue
    path = Path(line)
    if path.suffix != ".md" or path.parent.as_posix() != seed_path:
      continue
    previous = _blob(repo, f"{commit}^", line)
    # A newly added skill supersedes nothing an install can still hold.
    if not previous:
      continue
    found.setdefault((path.name, hashlib.sha256(previous).hexdigest()), commit)
  return [SupersededSeed(name, digest, found[(name, digest)])
          for name, digest in sorted(found)]


def missing_registrations(
  against: str, *,
  repo: Path = ROOT,
  seed_path: str = SEED_PATH,
  registry: dict[str, frozenset[str]] | None = None,
) -> list[SupersededSeed]:
  """Superseded copies this range replaced without registering them."""
  registry = registered_migrations() if registry is None else registry
  shipped = {path.name for path in (repo / seed_path).glob("*.md")}
  missing = []
  for superseded in superseded_seed_copies(
    against, repo=repo, seed_path=seed_path,
  ):
    # Retired skills are owned by _RETIRED_UNMODIFIED_SKILLS instead.
    if superseded.name not in shipped:
      continue
    if superseded.digest in registry.get(superseded.name, frozenset()):
      continue
    missing.append(superseded)
  return missing


def _report(requested: str) -> int:
  registry = registered_migrations()
  if requested and not requested.endswith(".md"):
    requested += ".md"
  names = sorted(path.name for path in (ROOT / SEED_PATH).glob("*.md"))
  if requested:
    if requested not in names:
      fail(f"no seed skill named {requested}")
    names = [requested]
  for name in names:
    shipping = hashlib.sha256((ROOT / SEED_PATH / name).read_bytes()).hexdigest()
    print(f"{name} (shipping {shipping[:12]})")
    generations = shipped_generations(name)
    if not generations:
      print("  no shipped history in this checkout")
    for digest, commit in generations:
      mark = ("shipping seed" if digest == shipping else
              "registered" if digest in registry.get(name, frozenset())
              else "UNREGISTERED")
      print(f"  {digest[:12]}  {commit[:8]}  {mark}")
  return 0


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "--against", default=os.environ.get("SEED_SKILL_MIGRATION_BASE_REF"),
    help="Git revision whose merge base with HEAD bounds the candidate change",
  )
  parser.add_argument(
    "--report", nargs="?", const="", metavar="SKILL",
    help="List shipped generations and their registration state, then exit",
  )
  args = parser.parse_args(argv)
  if args.report is not None:
    return _report(args.report)
  if not args.against:
    fail("pass --against <rev> or set SEED_SKILL_MIGRATION_BASE_REF")
  missing = missing_registrations(args.against)
  for superseded in missing:
    print(
      f"seed-skill-migrations: {SEED_PATH}/{superseded.name} replaced a copy "
      f"that installs may still hold ({superseded.commit[:8]}); add\n"
      f'  "{superseded.digest}",\n'
      f'to _UNMODIFIED_MIGRATIONS["{superseded.name}"] in '
      f"backend/scripts/init_skills.py, then freeze it in "
      f"backend/tests/test_memory_boot.py",
      file=sys.stderr,
    )
  if missing:
    raise SystemExit(1)
  print(
    "seed-skill-migrations: every superseded seed copy is registered "
    f"against {args.against}",
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
