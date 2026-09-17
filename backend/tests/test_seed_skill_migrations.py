"""A seed-skill change must register the copy it replaced.

`init_skills.py` rewrites a live skill file only when its bytes match a
registered predecessor, so a change to a seeded skill has to register the copy
it supersedes; otherwise every install that never edited that file keeps the
older text indefinitely. Boot cannot tell an untouched older generation from an
owner-authored edit, so only the change under review can record what it needs.
"""

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = "backend/scripts/seed-skills"
# origin/main is the base in CI and in a normal checkout; a fork clone or a
# shallow one can name the exact base through the environment instead.
BASE_REF = "origin/main"


def _init_skills():
  spec = importlib.util.spec_from_file_location(
    "seed_migration_registry", ROOT / "backend" / "scripts" / "init_skills.py",
  )
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
  done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
  if check and done.returncode != 0:
    pytest.fail(done.stderr.decode("utf-8", errors="replace").strip())
  return done.stdout


def replaced_seed_copies(
  against: str, *, repo: Path = ROOT, seed_path: str = SEED_PATH,
) -> list[tuple[str, str, str]]:
  """(skill, digest, commit) for each seeded copy this range replaced."""
  base = _git(repo, "merge-base", against, "HEAD").decode().strip()
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
    previous = _git(repo, "show", f"{commit}^:{line}", check=False)
    # A newly added skill supersedes nothing an install can still hold.
    if not previous:
      continue
    found.setdefault((path.name, hashlib.sha256(previous).hexdigest()), commit)
  return [(name, digest, found[(name, digest)]) for name, digest in sorted(found)]


def missing_registrations(
  against: str, registry, *, repo: Path = ROOT, seed_path: str = SEED_PATH,
) -> list[tuple[str, str, str]]:
  """Superseded copies this range replaced without registering them."""
  shipped = {path.name for path in (repo / seed_path).glob("*.md")}
  missing = []
  for name, digest, commit in replaced_seed_copies(
    against, repo=repo, seed_path=seed_path,
  ):
    # Retired skills are owned by _RETIRED_UNMODIFIED_SKILLS instead.
    if name not in shipped:
      continue
    if digest in registry.get(name, frozenset()):
      continue
    missing.append((name, digest, commit))
  return missing


def _seed_repo(tmp_path: Path, name: str, text: str) -> Path:
  repo = tmp_path / "repo"
  (repo / SEED_PATH).mkdir(parents=True)
  _git(repo, "init", "-b", "main")
  (repo / SEED_PATH / name).write_text(text, encoding="utf-8")
  return repo


def _commit(repo: Path, message: str) -> str:
  identity = {
    "GIT_AUTHOR_NAME": "Seed Test", "GIT_AUTHOR_EMAIL": "seed@example.test",
    "GIT_COMMITTER_NAME": "Seed Test", "GIT_COMMITTER_EMAIL": "seed@example.test",
  }
  _git(repo, "add", "-A")
  subprocess.run(
    ["git", "-C", str(repo), "commit", "-m", message],
    check=True, capture_output=True, env={**os.environ, **identity},
  )
  return _git(repo, "rev-parse", "HEAD").decode().strip()


def test_unregistered_superseded_copy_is_reported(tmp_path):
  repo = _seed_repo(tmp_path, "sample.md", "first generation\n")
  base = _commit(repo, "add seed")
  (repo / SEED_PATH / "sample.md").write_text("second generation\n", encoding="utf-8")
  reworded = _commit(repo, "reword seed")
  predecessor = hashlib.sha256(b"first generation\n").hexdigest()

  assert missing_registrations(base, {}, repo=repo) == [
    ("sample.md", predecessor, reworded),
  ]


def test_registered_superseded_copy_passes(tmp_path):
  repo = _seed_repo(tmp_path, "sample.md", "first generation\n")
  base = _commit(repo, "add seed")
  (repo / SEED_PATH / "sample.md").write_text("second generation\n", encoding="utf-8")
  _commit(repo, "reword seed")
  registry = {"sample.md": frozenset({hashlib.sha256(b"first generation\n").hexdigest()})}

  assert missing_registrations(base, registry, repo=repo) == []


def test_checkout_registers_every_superseded_seed_copy():
  """The candidate change must leave every install able to migrate."""
  against = os.environ.get("SEED_SKILL_MIGRATION_BASE_REF", "").strip() or BASE_REF
  missing = missing_registrations(against, _init_skills()._UNMODIFIED_MIGRATIONS)
  assert not missing, "\n".join(
    f"{SEED_PATH}/{name} replaced a copy installs may still hold ({commit[:8]}); "
    f'add "{digest}" to _UNMODIFIED_MIGRATIONS["{name}"] in '
    "backend/scripts/init_skills.py and freeze it in "
    "backend/tests/test_memory_boot.py"
    for name, digest, commit in missing
  )
