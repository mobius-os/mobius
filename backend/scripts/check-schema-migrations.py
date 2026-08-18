#!/usr/bin/env python3
"""Dependency-free guard for published schema-migration history."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend" / "app" / "schema_migrations.py"
HISTORY = ROOT / "backend" / "tests" / "fixtures" / "migration_history.json"


def fail(message: str) -> None:
  print(f"schema-migrations: {message}", file=sys.stderr)
  raise SystemExit(1)


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
  if frozen.get("format") != 1 or not isinstance(frozen.get("migrations"), dict):
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
    shape = ast.dump(function, include_attributes=False).encode()
    actual[version] = hashlib.sha256(shape).hexdigest()
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
