#!/usr/bin/env python3
"""Dependency-free guard for published schema-migration history."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend" / "app" / "schema_migrations.py"
HISTORY = ROOT / "backend" / "tests" / "fixtures" / "migration_history.json"


def fail(message: str) -> None:
  print(f"schema-migrations: {message}", file=sys.stderr)
  raise SystemExit(1)


@dataclass(frozen=True)
class ModuleSymbols:
  path: Path
  definitions: dict[str, ast.AST]
  imports: dict[str, tuple[str, str]]


def _module_path(module: str) -> Path | None:
  if module != "app" and not module.startswith("app."):
    return None
  candidate = ROOT / "backend" / Path(*module.split("."))
  path = candidate.with_suffix(".py")
  if path.is_file():
    return path
  package = candidate / "__init__.py"
  return package if package.is_file() else None


def _symbols(path: Path) -> ModuleSymbols:
  module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
  definitions: dict[str, ast.AST] = {}
  imports: dict[str, tuple[str, str]] = {}
  for node in module.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      definitions[node.name] = node
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
      targets = node.targets if isinstance(node, ast.Assign) else [node.target]
      for target in targets:
        if isinstance(target, ast.Name):
          definitions[target.id] = node
    elif isinstance(node, ast.ImportFrom) and node.module:
      for alias in node.names:
        if alias.name != "*":
          imports[alias.asname or alias.name] = (node.module, alias.name)
    elif isinstance(node, ast.Import):
      for alias in node.names:
        imports[alias.asname or alias.name] = (alias.name, "")
  return ModuleSymbols(path, definitions, imports)


def semantic_hash(source: Path, function_name: str) -> str:
  """Hash a migration plus transitive local and imported ``app`` symbols."""
  modules: dict[Path, ModuleSymbols] = {}

  def load(path: Path) -> ModuleSymbols:
    resolved = path.resolve()
    if resolved not in modules:
      modules[resolved] = _symbols(resolved)
    return modules[resolved]

  dependencies: dict[str, ast.AST] = {}
  pending: list[tuple[ModuleSymbols, str]] = [(load(source), function_name)]
  while pending:
    owner, name = pending.pop()
    key = f"{owner.path.relative_to(ROOT)}:{name}"
    if key in dependencies:
      continue
    current = owner.definitions.get(name)
    if current is None:
      fail(f"migration dependency {key!r} does not exist")
    dependencies[key] = current

    scoped_imports = dict(owner.imports)
    for node in ast.walk(current):
      if isinstance(node, ast.ImportFrom) and node.module:
        for alias in node.names:
          if alias.name != "*":
            scoped_imports[alias.asname or alias.name] = (node.module, alias.name)
      elif isinstance(node, ast.Import):
        for alias in node.names:
          scoped_imports[alias.asname or alias.name] = (alias.name, "")
    referenced = {
      node.id for node in ast.walk(current) if isinstance(node, ast.Name)
    }
    for referenced_name in sorted(referenced):
      if referenced_name in owner.definitions:
        pending.append((owner, referenced_name))
        continue
      imported = scoped_imports.get(referenced_name)
      if imported is None:
        continue
      if imported[1] and _module_path(f"{imported[0]}.{imported[1]}") is not None:
        # ``from app import helper`` binds the helper module. Attribute reads
        # below select the exact symbols whose semantics the migration uses.
        continue
      imported_path = _module_path(imported[0])
      if imported_path is not None and imported[1]:
        pending.append((load(imported_path), imported[1]))
    # ``from app import helper`` and ``import app.helper as helper`` bind a
    # module rather than one symbol. Fingerprint only the attributes this
    # dependency actually reads, preserving a deep guard without coupling a
    # migration to unrelated edits elsewhere in that module.
    for node in ast.walk(current):
      if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        continue
      imported = scoped_imports.get(node.value.id)
      if imported is None:
        continue
      module_name, imported_name = imported
      module_target = (
        f"{module_name}.{imported_name}" if imported_name else module_name
      )
      imported_path = _module_path(module_target)
      if imported_path is not None:
        pending.append((load(imported_path), node.attr))
  shape = "\n".join(
    f"{key}\n{ast.dump(dependencies[key], include_attributes=False)}"
    for key in sorted(dependencies)
  ).encode()
  return hashlib.sha256(shape).hexdigest()


def main() -> None:
  module = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
  functions = {
    node.name: node
    for node in module.body
    if isinstance(node, ast.FunctionDef)
  }
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

  frozen = json.loads(HISTORY.read_text(encoding="utf-8"))
  if frozen.get("format") != 2 or not isinstance(frozen.get("migrations"), dict):
    fail("migration_history.json has an unsupported shape")
  expected = frozen["migrations"]
  if list(expected) != versions:
    fail(
      "registry and frozen history differ; append the new version and its "
      "semantic hash without editing or renumbering prior entries"
    )

  actual = {}
  for version, function_name in entries:
    function = functions.get(function_name)
    if function is None:
      fail(f"registered function {function_name!r} does not exist")
    actual[version] = semantic_hash(SOURCE, function_name)
  if actual != expected:
    changed = [
      version for version in versions
      if actual.get(version) != expected.get(version)
    ]
    fail(
      "published migration code changed for " + ", ".join(changed)
      + "; restore it and append a new migration"
    )

  print(f"schema-migrations: {len(entries)} immutable migrations verified")


if __name__ == "__main__":
  main()
