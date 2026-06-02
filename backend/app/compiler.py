"""Compiles JSX source strings to ES modules using esbuild."""

import asyncio
import os
import re
import tempfile
from pathlib import Path

from app.config import get_settings
from app.runtime_libs import RUNTIME_LIBS

_ESBUILD_TIMEOUT_SECS = 30

# `export default <anything>` with optional async/function/class/paren/identifier.
# Covers: `export default function`, `export default class`, `export default App`,
# `export default () =>`, `export default {...}`, etc.
_EXPORT_DEFAULT_RE = re.compile(r"^\s*export\s+default\b", re.MULTILINE)


def _compiled_dir() -> Path:
  """Returns (and creates) the directory for compiled mini-app modules."""
  path = Path(get_settings().data_dir) / "compiled"
  path.mkdir(parents=True, exist_ok=True)
  return path


async def compile_jsx(
  app_id: int, jsx_source: str, *, out_path: str | Path | None = None,
) -> str:
  """Compiles JSX source to an ES module and returns the output path.

  Args:
    app_id: The numeric ID of the mini-app being compiled.
    jsx_source: The JSX source code string.
    out_path: Where esbuild writes the bundle. Defaults to the live
      bundle path ``app-<id>.js``. Pass a staging path to compile
      out-of-place so the live bundle is only swapped in after the
      DB commit succeeds (see ``recompile_app_bundle``).

  Returns:
    The absolute path of the compiled JS file.

  Raises:
    RuntimeError: If esbuild exits with a non-zero status.
  """
  # Fail loudly on malformed source before invoking esbuild — the
  # silent-0-byte case produces a downstream "no default export" at
  # runtime and wastes a debug round-trip.
  if not jsx_source or not jsx_source.strip():
    raise RuntimeError(
      "JSX source is empty. Write your component to "
      "apps/<name>/index.jsx before registering."
    )
  if not _EXPORT_DEFAULT_RE.search(jsx_source):
    raise RuntimeError(
      "JSX source has no `export default` — mini-apps must export a "
      "default React component. Add `export default function MyApp(...)` "
      "or `export default ComponentName`."
    )

  out = Path(out_path) if out_path is not None else _compiled_dir() / f"app-{app_id}.js"

  with tempfile.NamedTemporaryFile(
    suffix=".jsx", mode="w", delete=False, encoding="utf-8"
  ) as f:
    f.write(jsx_source)
    tmp_path = f.name

  try:
    try:
      proc = await asyncio.create_subprocess_exec(
        "esbuild",
        tmp_path,
        "--bundle",
        "--format=esm",
        "--jsx=automatic",
        "--platform=browser",
        *[f"--external:{lib}" for lib in RUNTIME_LIBS],
        f"--outfile={out}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
      )
    except FileNotFoundError:
      raise RuntimeError(
        "esbuild is not installed or not on PATH. "
        "The Docker image installs it automatically; for local dev run: "
        "npm install -g esbuild"
      )
    try:
      _, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=_ESBUILD_TIMEOUT_SECS,
      )
    except asyncio.TimeoutError:
      proc.kill()
      await proc.communicate()
      raise RuntimeError(
        f"esbuild timed out after {_ESBUILD_TIMEOUT_SECS} seconds"
      )
    if proc.returncode != 0:
      raise RuntimeError(
        f"Compilation failed:\n{stderr.decode()}"
      )
  finally:
    os.unlink(tmp_path)

  return str(out)


async def recompile_app_bundle(db, app, jsx_source: str) -> None:
  """Recompiles ``app``'s JSX into its live bundle as one transaction.

  The live bundle ``app-<id>.js`` is never left half-written or orphaned:
  the new code compiles to a ``.staging`` sibling, the DB transaction
  commits, and only a durable commit promotes the staging file into the
  live path via an atomic rename. A compile error leaves the live bundle
  untouched (esbuild writes its outfile only on success); a commit failure
  discards the staging file and rolls back, so the live bundle keeps the
  prior code. There is no snapshot to restore, and no window where the
  live bundle is the new code while the DB still holds the old.

  ``db.commit()`` here also flushes any other changes the caller staged on
  the session (e.g. a PATCH's name/description), so the whole update lands
  or rolls back together.

  The caller MUST hold the app's lifecycle + per-app lock and have loaded
  ``app`` fresh under it. The bundle path is keyed by app id, so without
  the lock a concurrent uninstall + SQLite id reuse could make this swap
  clobber a different app's bundle.

  Raises:
    RuntimeError: from ``compile_jsx`` on invalid JSX (live bundle untouched).
    Exception: re-raised after rollback if the commit fails (staging
      discarded, live bundle untouched).
  """
  live = _compiled_dir() / f"app-{app.id}.js"
  staged = _compiled_dir() / f"app-{app.id}.js.staging"
  await compile_jsx(app.id, jsx_source, out_path=staged)
  app.jsx_source = jsx_source
  app.compiled_path = str(live)
  try:
    db.commit()
  except Exception:
    db.rollback()
    staged.unlink(missing_ok=True)
    raise
  os.replace(staged, live)
