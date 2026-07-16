#!/usr/bin/env python3
"""Fail-closed preflight for an in-container manual browser/Caddy gate.

Run this *after uvicorn is healthy but before auth.setup or any fixture write*:

  GATE="$(mktemp -d /tmp/mobius-manual-e2e.XXXXXX)"
  export DATA_DIR="$GATE"
  export DATABASE_URL="sqlite:///$GATE/db/ultimate.db"
  export MOBIUS_TEST_RUNTIME=1 MOEBIUS_SKIP_BOOTSTRAP=1
  # start uvicorn and capture its PID, then:
  python3 scripts/verify-manual-e2e-runtime.py --gate "$GATE" --pid "$UVICORN_PID"

``test_runtime=true`` is not a data-isolation boundary by itself.  This check
reads the environment of the process that actually imported the backend,
rejects an inherited production database URL, resolves the SQLite file under
the gate, and pins a write-free initial inventory.  The ordinary Docker-based
``scripts/playwright-local.sh`` remains the preferred local runner; this is for
the explicit actual-Caddy/manual topology gate.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
from typing import NoReturn
from urllib.parse import unquote, urlsplit


def _fail(message: str) -> NoReturn:
  raise SystemExit(f"refusing manual E2E: {message}")


def _process_environment(pid: int) -> dict[str, str]:
  try:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
  except OSError as exc:
    _fail(f"cannot read uvicorn process environment for pid {pid}: {exc}")
  env: dict[str, str] = {}
  for item in raw.split(b"\0"):
    if not item or b"=" not in item:
      continue
    key, value = item.split(b"=", 1)
    env[key.decode(errors="replace")] = value.decode(errors="replace")
  return env


def _absolute_sqlite_path(database_url: str) -> Path:
  if not database_url:
    _fail("DATABASE_URL is absent (the backend would use its live default)")
  parsed = urlsplit(database_url)
  if parsed.scheme != "sqlite" or parsed.netloc:
    _fail("DATABASE_URL must be an absolute sqlite:////... URL for this gate")
  # urlsplit('sqlite:////tmp/x.db').path == '//tmp/x.db'.  Collapse only the
  # URL's extra root slash; do not accept a relative sqlite:/// path.
  raw_path = unquote(parsed.path)
  if not raw_path.startswith("//"):
    _fail("DATABASE_URL must use four slashes and an absolute filesystem path")
  return Path(raw_path[1:]).resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
  try:
    path.relative_to(root)
    return True
  except ValueError:
    return False


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--gate", required=True, type=Path)
  parser.add_argument("--pid", required=True, type=int)
  args = parser.parse_args()

  gate = args.gate.resolve(strict=True)
  tmp_root = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
  if not _is_within(gate, tmp_root):
    _fail(f"gate must be beneath disposable temp root {tmp_root}, not {gate}")

  env = _process_environment(args.pid)
  test_runtime = env.get("MOBIUS_TEST_RUNTIME")
  if test_runtime != "1":
    _fail(f"uvicorn MOBIUS_TEST_RUNTIME is {test_runtime!r}, not '1'")
  skip_bootstrap = env.get("MOEBIUS_SKIP_BOOTSTRAP")
  if skip_bootstrap != "1":
    _fail(
      "uvicorn must set MOBIUS_SKIP_BOOTSTRAP=1 for a zero-app inventory "
      f"(got {skip_bootstrap!r})"
    )

  data_dir_raw = env.get("DATA_DIR")
  if not data_dir_raw:
    _fail("uvicorn DATA_DIR is absent")
  data_dir = Path(data_dir_raw).resolve(strict=False)
  if data_dir != gate:
    _fail(f"uvicorn DATA_DIR={data_dir} does not equal gate {gate}")

  db_path = _absolute_sqlite_path(env.get("DATABASE_URL", ""))
  if not _is_within(db_path, gate):
    _fail(f"uvicorn database {db_path} is outside gate {gate}")
  if not db_path.is_file():
    _fail(f"uvicorn database does not exist yet: {db_path}")

  try:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    actual = Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if actual != db_path:
      _fail(f"SQLite opened {actual}, expected {db_path}")
    counts = {
      table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
      for table in ("owner", "chats", "apps")
    }
  except sqlite3.Error as exc:
    _fail(f"cannot inspect fresh gate database {db_path}: {exc}")
  finally:
    try:
      db.close()
    except UnboundLocalError:
      pass

  dirty = {table: count for table, count in counts.items() if count != 0}
  if dirty:
    _fail(f"database is not fresh before first browser write: {dirty}")

  print(f"manual E2E isolation verified: pid={args.pid} gate={gate} db={db_path}")


if __name__ == "__main__":
  main()
