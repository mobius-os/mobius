"""Ledger migrations after the one-way baseline stay additive.

ONE_WAY_UPGRADES_DESIGN.md §2: ledger migrations run before the one-way step
gate, so a later migration must never create, alter, index, or write a
step-owned table, and may otherwise only add unless it declares its writes in
``schema_migrations.MIGRATION_WRITES``. The checker reads each migration's
source (and the module functions it calls) rather than executing it.
"""

import ast
import inspect
import re
import textwrap

import pytest

from app import one_way_upgrades, schema_migrations


_STATEMENTS = (
  ("create_table", re.compile(
    r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)", re.I)),
  ("create_index", re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?[\"`]?\w+[\"`]?\s+ON\s+[\"`]?(\w+)", re.I)),
  ("alter", re.compile(r"\bALTER\s+TABLE\s+[\"`]?(\w+)[\"`]?\s+(\w+(?:\s+\w+)?)", re.I)),
  ("insert", re.compile(r"\b(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+[\"`]?(\w+)", re.I)),
  ("update", re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?[\"`]?(\w+)[\"`]?\s+SET\b", re.I)),
  ("delete", re.compile(r"\bDELETE\s+FROM\s+[\"`]?(\w+)", re.I)),
  ("drop", re.compile(r"\bDROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF\s+EXISTS\s+)?[\"`]?(\w+)", re.I)),
  ("create_trigger", re.compile(
    r"\bCREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+.*?\bON\s+[\"`]?(\w+)", re.I | re.S)),
)


def _strings(node: ast.AST) -> list[str]:
  found = []
  for child in ast.walk(node):
    if isinstance(child, ast.Constant) and isinstance(child.value, str):
      found.append(child.value)
    elif isinstance(child, ast.JoinedStr):
      found.append("".join(
        part.value if isinstance(part, ast.Constant) else "x"
        for part in child.values
      ))
  return found


def statements_in(source: str, helpers: dict[str, str] | None = None) -> list[tuple[str, str, bool]]:
  """(kind, target table, additive) for every SQL statement in the source.

  Also scans module-level helpers the migration calls by name, one level deep.
  """
  tree = ast.parse(textwrap.dedent(source))
  texts = _strings(tree)
  for call in ast.walk(tree):
    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
      helper = (helpers or {}).get(call.func.id)
      if helper is not None:
        texts += _strings(ast.parse(textwrap.dedent(helper)))
  found = []
  for text in texts:
    for kind, pattern in _STATEMENTS:
      for match in pattern.finditer(text):
        if kind in ("create_table", "create_index"):
          found.append((kind, match.group(2).lower(), bool(match.group(1))))
        elif kind == "alter":
          found.append((kind, match.group(1).lower(), match.group(2).upper() == "ADD COLUMN"))
        else:
          found.append((kind, match.group(1).lower(), False))
  return found


def violations(
  version: str,
  source: str,
  *,
  owned: frozenset[str],
  declared: dict[str, tuple[str, ...]],
  helpers: dict[str, str] | None = None,
) -> list[str]:
  problems = []
  allowed = {table.lower() for table in declared.get(version, ())}
  for kind, table, additive in statements_in(source, helpers):
    if table in owned:
      problems.append(f"{version}: {kind} touches step-owned table {table}")
    elif not additive and table not in allowed:
      problems.append(
        f"{version}: {kind} on {table} is not additive; declare it in "
        "MIGRATION_WRITES or make it a one-way step"
      )
  return problems


def _module_helpers() -> dict[str, str]:
  helpers = {}
  for name, value in vars(schema_migrations).items():
    if inspect.isfunction(value) and value.__module__ == schema_migrations.__name__:
      try:
        helpers[name] = inspect.getsource(value)
      except OSError:
        continue
  return helpers


def _post_baseline_migrations(migrations=None):
  migrations = schema_migrations._SCHEMA_MIGRATIONS if migrations is None else migrations
  versions = [version for version, _fn in migrations]
  baseline = versions.index(schema_migrations.ONE_WAY_BASELINE_MIGRATION)
  return tuple(migrations[baseline + 1:])


def scan(migrations=None, declared=None) -> list[str]:
  """The production check, over the real ledger unless given another."""
  owned = one_way_upgrades.step_owned_tables()
  helpers = _module_helpers()
  declared = schema_migrations.MIGRATION_WRITES if declared is None else declared
  problems = []
  for version, function in _post_baseline_migrations(migrations):
    problems += violations(
      version, inspect.getsource(function), owned=owned,
      declared=declared, helpers=helpers,
    )
  return problems


def test_migrations_after_the_one_way_baseline_are_additive():
  assert scan() == []


def _appended_destructive_migration(db):
  db.execute("DELETE FROM apps WHERE id = 0")


def _appended_additive_migration(db):
  db.execute("CREATE INDEX IF NOT EXISTS ix_apps_example ON apps (id)")


def test_the_production_scan_covers_a_newly_appended_migration():
  """Release F has no post-baseline migrations; the next one must be scanned."""
  ledger = schema_migrations._SCHEMA_MIGRATIONS
  assert _post_baseline_migrations() == tuple(
    ledger[[v for v, _f in ledger].index(schema_migrations.ONE_WAY_BASELINE_MIGRATION) + 1:]
  )
  destructive = (*ledger, ("9999_example_cleanup", _appended_destructive_migration))
  assert scan(destructive, declared={}) != []
  assert scan(destructive, declared={"9999_example_cleanup": ("apps",)}) == []
  additive = (*ledger, ("9999_example_index", _appended_additive_migration))
  assert scan(additive, declared={}) == []


def test_declarations_name_real_post_baseline_migrations():
  post = {version for version, _fn in _post_baseline_migrations()}
  assert set(schema_migrations.MIGRATION_WRITES) <= post
  owned = one_way_upgrades.step_owned_tables()
  for version, tables in schema_migrations.MIGRATION_WRITES.items():
    assert not set(tables) & owned, version


# --- the checker itself --------------------------------------------------------

OWNED = frozenset({"upgrade_units", "chat_messages"})


@pytest.mark.parametrize("sql", [
  "CREATE TABLE IF NOT EXISTS widgets (id INTEGER)",
  "CREATE INDEX IF NOT EXISTS ix_widgets ON widgets (id)",
  "CREATE UNIQUE INDEX IF NOT EXISTS ux ON widgets (id)",
  "ALTER TABLE widgets ADD COLUMN size INTEGER",
  "INSERT INTO t VALUES (1) ON CONFLICT(id) DO UPDATE SET x = 1",
])
def test_additive_statements_pass(sql):
  if sql.startswith("INSERT"):
    # The upsert is DML on t; only the DO UPDATE clause must not read as UPDATE.
    kinds = {kind for kind, _t, _a in statements_in(f"def m(db):\n  db.execute({sql!r})\n")}
    assert "update" not in kinds
    return
  source = f"def m(db):\n  db.execute({sql!r})\n"
  assert violations("0100_x", source, owned=OWNED, declared={}) == []


@pytest.mark.parametrize("sql", [
  "UPDATE widgets SET size = 0",
  "DELETE FROM widgets",
  "INSERT INTO widgets (id) SELECT id FROM other",
  "REPLACE INTO widgets VALUES (1)",
  "DROP TABLE widgets",
  "ALTER TABLE widgets RENAME TO gadgets",
  "ALTER TABLE widgets DROP COLUMN size",
  "CREATE TABLE widgets (id INTEGER)",
  "CREATE TRIGGER t AFTER DELETE ON widgets BEGIN SELECT 1; END",
])
def test_non_additive_statements_need_a_declaration(sql):
  source = f"def m(db):\n  db.execute({sql!r})\n"
  assert violations("0100_x", source, owned=OWNED, declared={})
  assert violations("0100_x", source, owned=OWNED, declared={"0100_x": ("widgets",)}) == []


@pytest.mark.parametrize("sql", [
  "CREATE INDEX IF NOT EXISTS ix ON chat_messages (seq)",
  "ALTER TABLE chat_messages ADD COLUMN extra TEXT",
  "DELETE FROM upgrade_units",
])
def test_step_owned_tables_are_off_limits_even_when_declared(sql):
  source = f"def m(db):\n  db.execute({sql!r})\n"
  declared = {"0100_x": ("chat_messages", "upgrade_units")}
  assert violations("0100_x", source, owned=OWNED, declared=declared)


def test_helpers_called_by_a_migration_are_scanned():
  helper = "def _wipe(db):\n  db.execute('DELETE FROM widgets')\n"
  source = "def m(db):\n  _wipe(db)\n"
  assert violations("0100_x", source, owned=OWNED, declared={}, helpers={"_wipe": helper})
