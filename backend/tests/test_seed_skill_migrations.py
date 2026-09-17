"""A seed-skill change must keep every copy it supersedes migratable."""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
# origin/main is the base in CI and in a normal checkout; a fork clone or a
# shallow one can name the exact base through the environment instead.
BASE_REF = "origin/main"


def _guard():
  script = ROOT / "backend" / "scripts" / "check-seed-skill-migrations.py"
  spec = importlib.util.spec_from_file_location("seed_skill_migrations", script)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def _digest(text: str) -> str:
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
  completed = subprocess.run(
    ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
  )
  return completed.stdout.strip()


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
  return _git(repo, "rev-parse", "HEAD")


def _seed(repo: Path, guard, name: str, text: str) -> Path:
  path = repo / guard.SEED_PATH / name
  path.write_text(text, encoding="utf-8")
  return path


@pytest.fixture
def seed_repo(tmp_path):
  guard = _guard()
  repo = tmp_path / "repo"
  (repo / guard.SEED_PATH).mkdir(parents=True)
  _git(repo, "init", "-b", "main")
  return guard, repo


def test_unregistered_superseded_copy_is_reported(seed_repo):
  guard, repo = seed_repo
  seed = _seed(repo, guard, "sample.md", "first generation\n")
  base = _commit(repo, "add seed")
  seed.write_text("second generation\n", encoding="utf-8")
  _commit(repo, "reword seed")

  missing = guard.missing_registrations(base, repo=repo, registry={})

  assert [(item.name, item.digest) for item in missing] == [
    ("sample.md", _digest("first generation\n")),
  ]


def test_registered_superseded_copy_passes(seed_repo):
  guard, repo = seed_repo
  seed = _seed(repo, guard, "sample.md", "first generation\n")
  base = _commit(repo, "add seed")
  seed.write_text("second generation\n", encoding="utf-8")
  _commit(repo, "reword seed")

  registry = {"sample.md": frozenset({_digest("first generation\n")})}

  assert guard.missing_registrations(base, repo=repo, registry=registry) == []


def test_newly_added_seed_supersedes_nothing(seed_repo):
  guard, repo = seed_repo
  (repo / "README.md").write_text("base\n", encoding="utf-8")
  base = _commit(repo, "base")
  _seed(repo, guard, "fresh.md", "brand new guidance\n")
  _commit(repo, "add seed")

  assert guard.missing_registrations(base, repo=repo, registry={}) == []


def test_generation_superseded_before_the_base_is_not_rechecked(seed_repo):
  """Only the candidate range is judged; older gaps are not re-litigated."""
  guard, repo = seed_repo
  seed = _seed(repo, guard, "sample.md", "first generation\n")
  _commit(repo, "add seed")
  seed.write_text("second generation\n", encoding="utf-8")
  base = _commit(repo, "reword seed")

  assert guard.missing_registrations(base, repo=repo, registry={}) == []


def test_shipped_generations_enumerates_history(seed_repo):
  guard, repo = seed_repo
  seed = _seed(repo, guard, "sample.md", "first generation\n")
  _commit(repo, "add seed")
  seed.write_text("second generation\n", encoding="utf-8")
  _commit(repo, "reword seed")

  digests = [
    digest for digest, _ in guard.shipped_generations("sample.md", repo=repo)
  ]

  assert digests == [_digest("second generation\n"), _digest("first generation\n")]


def _base_ref() -> str:
  named = os.environ.get("SEED_SKILL_MIGRATION_BASE_REF", "").strip()
  if named:
    return named
  resolved = subprocess.run(
    ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", BASE_REF],
    capture_output=True,
  )
  return BASE_REF if resolved.returncode == 0 else ""


def test_checkout_registers_every_superseded_seed_copy():
  """The candidate change must leave every install able to migrate."""
  guard = _guard()
  against = _base_ref()
  if not against:
    pytest.skip("no base ref available; set SEED_SKILL_MIGRATION_BASE_REF")

  assert guard.missing_registrations(against) == []
