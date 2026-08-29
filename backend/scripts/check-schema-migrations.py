#!/usr/bin/env python3
"""Dependency-free guard for append-only schema-migration history."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend" / "app" / "schema_migrations.py"
SOURCE_REPO_PATH = SOURCE.relative_to(ROOT)
HISTORY = ROOT / "backend" / "tests" / "fixtures" / "migration_history.json"
HISTORY_FORMAT = 3
HISTORY_HASH_KIND = "migration-owned-ast-v1"

# These migrations shipped before the repository required migration code to
# be self-contained. Their source remains immutable, but their historical
# runtime imports are deliberately not fingerprinted: doing so froze unrelated
# app_git/model/compiler work and still did not prove history was append-only.
LEGACY_APP_DEPENDENCIES = frozenset({
  "0004_app_identity_required",
  "0005_connectors",
  "0013_app_hosted_publication",
})


def fail(message: str) -> None:
  print(f"schema-migrations: {message}", file=sys.stderr)
  raise SystemExit(1)


def _definitions(module: ast.Module) -> dict[str, ast.AST]:
  definitions: dict[str, ast.AST] = {}
  for node in module.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      definitions[node.name] = node
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
      targets = node.targets if isinstance(node, ast.Assign) else [node.target]
      for target in targets:
        if isinstance(target, ast.Name):
          definitions[target.id] = node
  return definitions


def _registry(module: ast.Module) -> list[tuple[str, str]]:
  registry = next((
    node.value
    for node in module.body
    if isinstance(node, ast.Assign)
    and any(
      isinstance(target, ast.Name) and target.id == "_SCHEMA_MIGRATIONS"
      for target in node.targets
    )
  ), None)
  if not isinstance(registry, (ast.Tuple, ast.List)):
    fail("_SCHEMA_MIGRATIONS must remain a literal ordered sequence")

  entries: list[tuple[str, str]] = []
  for item in registry.elts:
    if (
      not isinstance(item, (ast.Tuple, ast.List))
      or len(item.elts) != 2
      or not isinstance(item.elts[0], ast.Constant)
      or not isinstance(item.elts[0].value, str)
      or not isinstance(item.elts[1], ast.Name)
    ):
      fail("each registry entry must be a literal (version, function) pair")
    entries.append((item.elts[0].value, item.elts[1].id))
  return entries


def _dependency_nodes(
  definitions: dict[str, ast.AST], root_name: str,
) -> dict[str, ast.AST]:
  dependencies: dict[str, ast.AST] = {}
  pending = [root_name]
  while pending:
    name = pending.pop()
    if name in dependencies:
      continue
    node = definitions.get(name)
    if node is None:
      fail(f"registered migration function {name!r} does not exist")
    dependencies[name] = node
    referenced = {
      child.id for child in ast.walk(node) if isinstance(child, ast.Name)
    }
    pending.extend(sorted(referenced & definitions.keys()))
  return dependencies


def _migration_hash(dependencies: dict[str, ast.AST]) -> str:
  """Fingerprint migration-owned code without freezing runtime modules."""
  shape = "\n".join(
    f"{name}\n{ast.dump(dependencies[name], include_attributes=False)}"
    for name in sorted(dependencies)
  ).encode()
  return hashlib.sha256(shape).hexdigest()


def _app_imports(
  module: ast.Module, dependencies: dict[str, ast.AST],
) -> set[str]:
  """Return runtime ``app`` modules used by one migration dependency set."""
  bound_imports: dict[str, str] = {}
  import_nodes = [
    node for node in module.body
    if isinstance(node, (ast.Import, ast.ImportFrom))
  ]
  for dependency in dependencies.values():
    import_nodes.extend(
      node for node in ast.walk(dependency)
      if isinstance(node, (ast.Import, ast.ImportFrom))
    )
  for node in import_nodes:
    if isinstance(node, ast.ImportFrom) and node.module:
      for alias in node.names:
        target = f"{node.module}.{alias.name}"
        bound_imports[alias.asname or alias.name] = target
    elif isinstance(node, ast.Import):
      for alias in node.names:
        bound_imports[alias.asname or alias.name] = alias.name

  referenced = {
    child.id
    for dependency in dependencies.values()
    for child in ast.walk(dependency)
    if isinstance(child, ast.Name)
  }
  return {
    module_name
    for name, module_name in bound_imports.items()
    if name in referenced
    and (module_name == "app" or module_name.startswith("app."))
  }


def inspect_history(raw: str, *, source: str) -> dict[str, tuple[str, str]]:
  """Validate one source revision and return version -> (function, hash)."""
  try:
    module = ast.parse(raw, filename=source)
  except SyntaxError as exc:
    fail(f"cannot parse {source}: {exc}")
  definitions = _definitions(module)
  entries = _registry(module)
  versions = [version for version, _name in entries]
  function_names = [name for _version, name in entries]
  try:
    numbers = [int(version.split("_", 1)[0]) for version in versions]
  except ValueError:
    fail("every migration version must start with a numeric sequence")
  if len(versions) != len(set(versions)):
    fail("migration versions must be unique")
  if numbers != sorted(set(numbers)):
    fail("migration numbers must strictly increase; rebase and renumber")
  if len(function_names) != len(set(function_names)):
    fail("one migration function cannot own multiple ledger entries")

  history: dict[str, tuple[str, str]] = {}
  for version, function_name in entries:
    dependencies = _dependency_nodes(definitions, function_name)
    runtime_imports = _app_imports(module, dependencies)
    if runtime_imports and version not in LEGACY_APP_DEPENDENCIES:
      fail(
        f"new migration {version} depends on mutable runtime module(s): "
        + ", ".join(sorted(runtime_imports))
        + "; keep migration code self-contained"
      )
    history[version] = (function_name, _migration_hash(dependencies))
  return history


def append_only_error(
  published: dict[str, object], candidate: dict[str, object],
) -> str | None:
  """Explain how candidate rewrites published history, or return ``None``."""
  published_entries = list(published.items())
  candidate_entries = list(candidate.items())
  if candidate_entries[:len(published_entries)] == published_entries:
    return None
  for index, (version, identity) in enumerate(published_entries):
    if index >= len(candidate_entries):
      return f"published migration {version} was removed"
    candidate_version, candidate_identity = candidate_entries[index]
    if candidate_version != version:
      return (
        f"published migration {version} was reordered, removed, or renamed "
        f"to {candidate_version}"
      )
    if candidate_identity != identity:
      return f"published migration {version} changed"
  return "published migration history changed"


def frozen_history_error(
  frozen: dict[str, str], candidate: dict[str, tuple[str, str]],
) -> str | None:
  """Explain how candidate differs from the checked-in immutable ledger."""
  candidate_versions = list(candidate)
  frozen_versions = list(frozen)
  if candidate_versions != frozen_versions:
    return (
      "registry and migration_history.json differ; append the new version and "
      "its hash without editing or renumbering prior entries"
    )
  changed = [
    version for version, (_function, digest) in candidate.items()
    if frozen.get(version) != digest
  ]
  if changed:
    return (
      "published migration code changed for " + ", ".join(changed)
      + "; restore it and append a new migration"
    )
  return None


def _frozen_history() -> dict[str, str]:
  try:
    frozen = json.loads(HISTORY.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as exc:
    fail(f"cannot read {HISTORY.relative_to(ROOT)}: {exc}")
  if (
    frozen.get("format") != HISTORY_FORMAT
    or frozen.get("hash_kind") != HISTORY_HASH_KIND
    or not isinstance(frozen.get("migrations"), dict)
  ):
    fail("migration_history.json has an unsupported shape or hash kind")
  migrations = frozen["migrations"]
  if not all(
    isinstance(version, str)
    and isinstance(digest, str)
    and len(digest) == 64
    and all(character in "0123456789abcdef" for character in digest)
    for version, digest in migrations.items()
  ):
    fail("migration_history.json contains an invalid migration entry")
  return migrations


def _published_source(ref: str) -> str:
  try:
    completed = subprocess.run(
      ["git", "show", f"{ref}:{SOURCE_REPO_PATH.as_posix()}"],
      cwd=ROOT,
      text=True,
      capture_output=True,
      check=True,
    )
  except subprocess.CalledProcessError as exc:
    detail = exc.stderr.strip() or exc.stdout.strip() or "not found"
    fail(f"cannot read published migration source at {ref!r}: {detail}")
  return completed.stdout


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Verify current and already-published schema migration history.",
  )
  parser.add_argument(
    "--against",
    default=os.environ.get("SCHEMA_MIGRATION_BASE_REF"),
    help="Git revision whose migration source is the immutable prefix",
  )
  args = parser.parse_args()

  candidate = inspect_history(
    SOURCE.read_text(encoding="utf-8"), source=str(SOURCE_REPO_PATH),
  )
  frozen_regression = frozen_history_error(_frozen_history(), candidate)
  if frozen_regression:
    fail(frozen_regression)
  if args.against:
    published = inspect_history(
      _published_source(args.against),
      source=f"{args.against}:{SOURCE_REPO_PATH}",
    )
    regression = append_only_error(published, candidate)
    if regression:
      fail(regression + "; restore it and append a new migration")
    suffix = f" and {args.against}"
  else:
    suffix = ""
  print(
    f"schema-migrations: {len(candidate)} immutable migrations verified "
    f"against migration_history.json{suffix}"
  )


if __name__ == "__main__":
  main()
