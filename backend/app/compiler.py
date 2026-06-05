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


def _entry_source_path(app) -> Path | None:
  source_dir = getattr(app, "source_dir", None)
  if not source_dir:
    return None
  return Path(source_dir) / "index.jsx"


def _atomic_write(path: Path, data: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(
    prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
  )
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      f.write(data)
    os.replace(tmp, path)
  except Exception:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def _restore_entry_source(
  path: Path, previous_source: str | None, source_existed: bool,
) -> None:
  if source_existed:
    _atomic_write(path, previous_source if previous_source is not None else "")
    return
  try:
    path.unlink()
  except FileNotFoundError:
    pass


async def compile_jsx(
  app_id: int,
  jsx_source: str,
  *,
  out_path: str | Path | None = None,
  source_path: str | Path | None = None,
) -> str:
  """Compiles JSX source to an ES module and returns the output path.

  Args:
    app_id: The numeric ID of the mini-app being compiled.
    jsx_source: The JSX source code string.
    out_path: Where esbuild writes the bundle. Defaults to the live
      bundle path ``app-<id>.js``. Pass a staging path to compile
      out-of-place so the live bundle is only swapped in after the
      DB commit succeeds (see ``recompile_app_bundle``).
    source_path: Optional real filesystem entrypoint. When present,
      esbuild compiles that path directly, allowing relative imports from
      sibling files in the app source tree. When omitted, the legacy
      string-only path writes ``jsx_source`` to a temp file and compiles that.

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

  entry_path = Path(source_path) if source_path is not None else None
  tmp_path = None
  if entry_path is None:
    with tempfile.NamedTemporaryFile(
      suffix=".jsx", mode="w", delete=False, encoding="utf-8"
    ) as f:
      f.write(jsx_source)
      tmp_path = f.name
    entry_path = Path(tmp_path)

  try:
    try:
      proc = await asyncio.create_subprocess_exec(
        "esbuild",
        str(entry_path),
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
    except asyncio.CancelledError:
      # The awaiting task was cancelled (a superseded debounced watcher
      # recompile, or shutdown). Kill the child so it can't keep running and
      # writing out_path after we've returned and released our locks.
      proc.kill()
      try:
        await proc.communicate()
      except Exception:
        pass
      raise
    if proc.returncode != 0:
      raise RuntimeError(
        f"Compilation failed:\n{stderr.decode()}"
      )
  finally:
    if tmp_path is not None:
      os.unlink(tmp_path)

  return str(out)


async def recompile_app_bundle(db, app, jsx_source: str) -> None:
  """Recompiles ``app``'s JSX into its live bundle as one transaction.

  The live bundle ``app-<id>.js`` is never left half-written or orphaned:
  the new code compiles to a ``.staging`` sibling, the DB transaction
  commits, and only a durable commit promotes the staging file into the
  live path via an atomic rename. When the app has a ``source_dir``,
  ``index.jsx`` is synced before compile so esbuild can resolve relative
  imports from that real source tree; compile or commit failure restores the
  previous entry file. The live bundle stays untouched on compile failure, and
  a commit failure discards the staging file and rolls back, so there is no
  window where the live bundle is the new code while the DB still holds the old.

  ``db.commit()`` here also flushes any other changes the caller staged on
  the session (e.g. a PATCH's name/description), so the whole update lands
  or rolls back together.

  The caller MUST ensure the app's bundle path (keyed by app id) can't be
  concurrently reused while this runs, or the post-commit swap could clobber a
  different app's bundle. PATCH and the watcher satisfy this by holding the
  lifecycle + per-app lock with ``app`` loaded fresh under it; ``create_app``
  satisfies it trivially because the id is brand-new and uncommitted, so no
  other operation can reference it yet.

  Raises:
    RuntimeError: from ``compile_jsx`` on invalid JSX (live bundle untouched).
    Exception: re-raised after rollback if the commit fails (staging
      discarded, live bundle untouched).
  """
  live = _compiled_dir() / f"app-{app.id}.js"
  staged = _compiled_dir() / f"app-{app.id}.js.staging"
  source_path = _entry_source_path(app)
  previous_source = None
  source_existed = False
  if source_path is not None:
    try:
      previous_source = source_path.read_text(encoding="utf-8")
      source_existed = True
    except FileNotFoundError:
      pass
    if previous_source != jsx_source:
      _atomic_write(source_path, jsx_source)
  try:
    await compile_jsx(
      app.id,
      jsx_source,
      out_path=staged,
      source_path=source_path if source_path is not None else None,
    )
  except Exception:
    if source_path is not None and previous_source != jsx_source:
      _restore_entry_source(source_path, previous_source, source_existed)
    raise
  app.jsx_source = jsx_source
  app.compiled_path = str(live)
  try:
    db.commit()
  except Exception:
    db.rollback()
    staged.unlink(missing_ok=True)
    if source_path is not None and previous_source != jsx_source:
      _restore_entry_source(source_path, previous_source, source_existed)
    raise
  os.replace(staged, live)


def reap_staging_bundles() -> None:
  """Removes leftover ``*.js.staging`` files at startup.

  ``recompile_app_bundle`` (and the installer) compile to a staging file and
  promote it with an atomic rename only after the DB commit. A process death in
  the tiny window between commit and rename would leave a staging file behind.
  Discarding it is always safe: it was either never committed, or
  committed-but-not-promoted — in which case the live bundle keeps the prior
  code and the next edit recompiles. A staging file is never served, so a leak
  is at worst a stale bundle that self-heals, never wrong code being served.
  """
  for f in _compiled_dir().glob("*.js.staging"):
    try:
      f.unlink()
    except OSError:
      pass
