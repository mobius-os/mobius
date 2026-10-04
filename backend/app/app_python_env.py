"""Per-app Python environments built from a declared, hash-pinned lock.

Without a declaration, app services, preload hosts, and Python jobs run on the
platform's interpreter and borrow its libraries. An app may instead declare
``"python": {"lock": "<path>"}`` in mobius.json: a complete
``pip-compile --generate-hashes`` lock inside its source. The platform then
builds one virtual environment WITHOUT system site-packages and runs that
app's Python processes with it. Only /data survives container replacement, so
this is the one place an app's dependencies can live.

Layout under ``<data>/app-envs``:

- ``builds/<id>/`` is one venv, built in place and never moved: pip writes
  absolute paths (console-script shebangs) that must stay valid.
- ``<app id>/<key>`` is a relative symlink to a validated build; creating it
  is the atomic publication. The key combines the interpreter/platform
  compatibility with the lock's SHA-256.

Lifecycle:

- Build (``prepare_env`` / ``publish_env``): during Apply and Store install,
  before the accepted runtime pointer is published. The venv installs the lock
  wheels-only (no package build code runs), then passes ``pip check``. Every
  tree passed to ``prepare_env``, including one reusing an env, smoke-runs
  every present declared Python entry. The smoke run executes the app's own
  setup code as the backend user with no sandbox: it is reviewed app code,
  like the service it checks. Every build step runs in its own process group,
  which is killed when the step ends or times out; this is best effort, since
  a descendant that calls ``setsid`` leaves the group, but waiting for a step
  is always bounded. A matching env is reused; a failure fails the Apply or
  install, so the previous revision stays live. A Store install or update
  checks the exact runtime tree it reconciles (local edits merged in, or every
  module of a Git origin) between two passes of its transaction, holding no
  lock, and publishes it only if the second pass reconciles the same tree from
  the same inputs. A local edit to the lock itself is refused.
- Restore (``rebuild_accepted_env``): after an image replacement, rebuild a
  missing env from the frozen accepted runtime tree, never editable source.
  Restoration uses the same validation and atomic publication as Apply. A
  failure leaves the app's Python unavailable rather than falling back to the
  platform interpreter.
- Use (``resolve_env``): a declaring revision gets its env or
  ``PythonEnvUnavailable``, never the platform interpreter. A revision with no
  mobius.json is deliberately undeclared: accepted revisions may legitimately
  lack one (Store sources without a runtime manifest, legacy baselines). A
  manifest that exists but cannot be read fails closed instead. An image
  replacement changes the key, so processes fail closed while background
  restoration is pending or if it fails. ``activated_environment`` puts the env's
  ``bin`` first on PATH so a subprocess ``python3`` is the env's too.
- GC (``prune_envs``): ``applied_app_runtime.prune_runtime`` calls it under the
  runtime reader/job locks with the runtime trees it keeps, so an env lives
  exactly as long as a kept (current, previous, or pinned) runtime needs it.

This module is imported by the job runner, so it takes ``data_dir``
explicitly and imports nothing from the web application.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.manifest_contract import (
  ManifestContractError,
  job_interpreter,
  python_job_arguments,
  python_lock,
)


ENVS_DIRNAME = "app-envs"
_BUILDS_DIRNAME = "builds"
VENV_TIMEOUT_SECONDS = 120
INSTALL_TIMEOUT_SECONDS = 900
CHECK_TIMEOUT_SECONDS = 120
SMOKE_TIMEOUT_SECONDS = 60
_DIAGNOSTIC_CHARS = 2000
_BASE_ENV_NAMES = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"})
# pip sees only where to download from and how to reach it. Its configuration
# files are disabled (PIP_CONFIG_FILE=/dev/null) and no other PIP_* setting is
# inherited, so nothing can redirect the install target or relax hash checks.
_PIP_ENV_NAMES = _BASE_ENV_NAMES | frozenset({
  "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX",
  "PIP_TRUSTED_HOST", "PIP_CERT",
  "SSL_CERT_FILE", "SSL_CERT_DIR",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")

# Run with the new env's interpreter. A service entry runs its module-level
# setup (everything but its ``__main__`` block, as the preload host does), so
# the imports and framework construction a request needs are exercised. A job
# is a whole program, so only its top-level imports are loaded.
_SMOKE_PROGRAM = r"""
import ast, importlib, os, runpy, sys
kind, path = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
if kind == "service":
  runpy.run_path(path, run_name="__mobius_env_check__")
else:
  with open(path, "rb") as handle:
    tree = ast.parse(handle.read(), path)
  for node in tree.body:
    if isinstance(node, ast.Import):
      names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
      names = [node.module]
    else:
      continue
    for name in names:
      importlib.import_module(name)
"""


class PythonEnvUnavailable(RuntimeError):
  """A revision's environment cannot be used, so its Python must not start."""


class PythonEnvBuildError(RuntimeError):
  """The declared lock could not be built into a validated environment."""


@dataclass(frozen=True)
class StagedEnv:
  """A validated build awaiting ``publish_env``; a reused one is already published."""

  key: str
  root: Path
  reused: bool


@dataclass(frozen=True)
class PublishedEnv:
  """What ``publish_env`` created, so a rolled-back publication can remove it."""

  link: Path
  build: Path | None


def envs_parent(data_dir: Path | str, app_id: int) -> Path:
  return Path(data_dir) / ENVS_DIRNAME / str(int(app_id))


def _builds(data_dir: Path | str) -> Path:
  return Path(data_dir) / ENVS_DIRNAME / _BUILDS_DIRNAME


def _runtime_manifest(runtime_root: Path) -> dict | None:
  """The accepted manifest, None when the revision has none; unreadable fails closed."""
  path = runtime_root / "mobius.json"
  if not os.path.lexists(path):
    return None
  try:
    if path.is_symlink() or not path.is_file():
      raise ValueError("not a regular file")
    manifest = json.loads(path.read_bytes())
    if not isinstance(manifest, dict):
      raise ValueError("not a JSON object")
  except (OSError, ValueError) as exc:
    raise PythonEnvUnavailable(
      f"The accepted app revision's manifest cannot be read ({exc}); Apply the "
      "app again."
    ) from exc
  return manifest


def declared_lock(runtime_root: Path) -> str | None:
  """The lock path an accepted runtime tree declares, or None."""
  manifest = _runtime_manifest(runtime_root)
  if manifest is None:
    return None
  try:
    return python_lock(manifest)
  except ManifestContractError as exc:
    raise PythonEnvUnavailable(str(exc)) from exc


def _lock_file(runtime_root: Path, relative: str) -> Path:
  lock = runtime_root / relative
  try:
    inside = lock.resolve().is_relative_to(runtime_root.resolve())
  except (OSError, RuntimeError):
    inside = False
  if not inside or lock.is_symlink() or not lock.is_file():
    raise PythonEnvBuildError(
      f"The declared Python lock `{relative}` is missing from the app source."
    )
  return lock


@functools.cache
def compatibility_tag() -> str:
  """What a built env's binaries depend on in the running interpreter.

  The readable prefix names the ABI and platform; the digest also covers the
  base executable's location (a venv links to it), the C library and the full
  Python version, so an image that changes any of them selects a new env. An
  image replacement that keeps them reuses the env already in /data.
  """
  base = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
  libc = "-".join(platform.libc_ver())
  identity = "\0".join((
    sys.implementation.cache_tag or sys.implementation.name,
    sysconfig.get_platform(),
    sysconfig.get_config_var("SOABI") or "",
    platform.python_version(),
    base,
    libc,
  ))
  readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", (
    f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
  ))
  return f"{readable}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def env_key(lock_bytes: bytes) -> str:
  return f"{compatibility_tag()}-{hashlib.sha256(lock_bytes).hexdigest()}"


@functools.lru_cache(maxsize=256)
def declared_key(runtime_root: Path) -> str | None:
  # Accepted runtime trees are immutable and content-addressed by path, so a
  # tree's key never changes while this process runs. Failures are not cached.
  relative = declared_lock(runtime_root)
  if relative is None:
    return None
  try:
    return env_key(_lock_file(runtime_root, relative).read_bytes())
  except (OSError, PythonEnvBuildError) as exc:
    raise PythonEnvUnavailable(str(exc)) from exc


def _usable(env: Path) -> bool:
  # A venv's interpreter is a link to the base executable; a new image that
  # moved it leaves a dangling link, which exists() reports as missing.
  return (env / "pyvenv.cfg").is_file() and (env / "bin" / "python").exists()


def resolve_env(data_dir: Path | str, app_id: int, runtime_root: Path) -> Path | None:
  """The env an accepted runtime must run with; None when it declares none.

  A declaring revision gets its built env or ``PythonEnvUnavailable``, never
  a fallback to the platform interpreter.
  """
  key = declared_key(runtime_root)
  if key is None:
    return None
  env = envs_parent(data_dir, app_id) / key
  if not _usable(env):
    raise PythonEnvUnavailable(
      "This app's Python environment is unavailable for the current platform "
      "interpreter. Automatic restoration runs after boot; inspect GET /api/setup "
      "and retry with POST /api/setup/rerun if it fails."
    )
  return env


def python_for(env: Path | None) -> str:
  return sys.executable if env is None else str(env / "bin" / "python")


def activated_environment(environment: dict[str, str], env: Path | None) -> dict[str, str]:
  """A process environment whose ``python3`` on PATH is the app's env."""
  if env is None:
    return environment
  bin_dir = str(env / "bin")
  path = environment.get("PATH")
  return {
    **environment,
    "PATH": f"{bin_dir}{os.pathsep}{path}" if path else bin_dir,
    "VIRTUAL_ENV": str(env),
  }


def job_command_interpreter(declared: tuple[str, ...], env: Path | None) -> tuple[str, ...]:
  """Point a Python job's shebang at the app's env; other programs are unchanged."""
  if env is None:
    return declared
  arguments = python_job_arguments(declared)
  if arguments is None:
    return declared
  return (python_for(env), *arguments)


def redact(text: str) -> str:
  """Remove URL credentials (an index URL may carry them) from diagnostics."""
  return _URL_CREDENTIALS.sub(r"\1****@", text)


def _tail(*outputs: str) -> str:
  lines = [line for output in outputs for line in output.splitlines() if line.strip()]
  errors = [line for line in lines if line.lstrip().startswith("ERROR")]
  text = "\n".join(errors or lines[-12:])
  return redact(text[-_DIAGNOSTIC_CHARS:]) or "no diagnostics"


def _kill_group(pid: int) -> None:
  try:
    os.killpg(pid, signal.SIGKILL)
  except (ProcessLookupError, PermissionError):
    pass


_REAP_SECONDS = 5


def _stop(process: subprocess.Popen) -> None:
  """Kill a step's process group and reap it within a bound.

  Best effort: a descendant that left the group (``setsid``) survives the
  group kill and may still hold the output pipes, so they are closed rather
  than drained.
  """
  _kill_group(process.pid)
  try:
    process.wait(timeout=_REAP_SECONDS)
  except subprocess.TimeoutExpired:
    pass
  for stream in (process.stdout, process.stderr):
    if stream is not None:
      stream.close()


def _run(command: list[str], *, step: str, timeout: int, env: dict[str, str], cwd=None) -> None:
  try:
    process = subprocess.Popen(
      command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
      stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
      start_new_session=True,
    )
  except OSError as exc:
    raise PythonEnvBuildError(f"{step} could not start: {exc}") from exc
  try:
    # A descendant holding the pipes keeps this from returning, so the
    # step's own deadline also bounds waiting for them.
    stdout, stderr = process.communicate(timeout=timeout)
  except subprocess.TimeoutExpired as exc:
    _stop(process)
    raise PythonEnvBuildError(f"{step} exceeded {timeout} seconds.") from exc
  except BaseException:
    _stop(process)
    raise
  # Best effort: stop anything the step left running in its process group.
  _kill_group(process.pid)
  if process.returncode != 0:
    raise PythonEnvBuildError(f"{step} failed:\n{_tail(stdout, stderr)}")


def _smoke_targets(runtime_root: Path, manifest: dict) -> list[tuple[str, Path]]:
  targets = []
  service = manifest.get("service")
  entry = service.get("entry") if isinstance(service, dict) else None
  if isinstance(entry, str) and (runtime_root / entry).is_file():
    targets.append(("service", runtime_root / entry))
  schedule = manifest.get("schedule")
  job = schedule.get("job") if isinstance(schedule, dict) else None
  if isinstance(job, str) and (runtime_root / job).is_file():
    try:
      arguments = python_job_arguments(job_interpreter((runtime_root / job).read_bytes()))
    except ManifestContractError as exc:
      raise PythonEnvBuildError(str(exc)) from exc
    if arguments is not None:
      targets.append(("job", runtime_root / job))
  return targets


def _base_env(names=_BASE_ENV_NAMES) -> dict[str, str]:
  return {name: value for name, value in os.environ.items() if name in names}


def _build(build: Path, runtime_root: Path, manifest: dict, lock: Path, relative: str) -> None:
  _run(
    [sys.executable, "-m", "venv", str(build)],
    step="Creating the Python environment", timeout=VENV_TIMEOUT_SECONDS,
    env=_base_env(),
  )
  python = str(build / "bin" / "python")
  pip_env = _base_env(_PIP_ENV_NAMES)
  pip_env.update({
    "PIP_CONFIG_FILE": os.devnull,
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
  })
  _run(
    [
      python, "-m", "pip", "install",
      "--require-hashes", "--no-deps", "--only-binary=:all:",
      "-r", str(lock),
    ],
    step=f"Installing `{relative}` (wheels only, hash-checked)",
    timeout=INSTALL_TIMEOUT_SECONDS, env=pip_env, cwd=str(runtime_root),
  )
  _run(
    [python, "-m", "pip", "check"],
    step=f"`pip check` of `{relative}` (the lock must be complete)",
    timeout=CHECK_TIMEOUT_SECONDS, env=pip_env,
  )


def _smoke(env: Path, runtime_root: Path, manifest: dict, relative: str) -> None:
  python = str(env / "bin" / "python")
  for kind, path in _smoke_targets(runtime_root, manifest):
    with tempfile.TemporaryDirectory(prefix="mobius-env-check-") as storage:
      # Inert values: setup must not need a live token, and the check must not
      # touch the app's real storage. This is not a sandbox.
      smoke_env = activated_environment(_base_env(), env)
      smoke_env.update({
        "APP_ID": "0", "APP_SLUG": "", "APP_TOKEN": "",
        "APP_STORAGE_DIR": storage, "API_BASE_URL": "http://127.0.0.1:9",
      })
      _run(
        [python, "-c", _SMOKE_PROGRAM, kind, str(path)],
        step=f"Starting the app's {kind} `{path.name}` with `{relative}`",
        timeout=SMOKE_TIMEOUT_SECONDS, env=smoke_env, cwd=str(runtime_root),
      )


def prepare_env(
  data_dir: Path | str, app_id: int | None, runtime_root: Path,
) -> StagedEnv | None:
  """Build and validate the env an accepted runtime tree declares.

  Returns None for an undeclared tree. ``app_id`` is None for an app that has
  no row yet; it cannot have an env to reuse. Raises PythonEnvBuildError with
  pip's own diagnostics, which name the failing package.
  """
  try:
    manifest = _runtime_manifest(runtime_root)
    relative = declared_lock(runtime_root)
  except PythonEnvUnavailable as exc:
    raise PythonEnvBuildError(str(exc)) from exc
  if relative is None:
    return None
  lock = _lock_file(runtime_root, relative)
  key = env_key(lock.read_bytes())
  if app_id is not None:
    existing = envs_parent(data_dir, app_id) / key
    if _usable(existing):
      _smoke(existing, runtime_root, manifest, relative)
      return StagedEnv(key=key, root=existing, reused=True)
  builds = _builds(data_dir)
  builds.mkdir(parents=True, exist_ok=True)
  # Built where it will stay: a short, final path keeps pip's absolute script
  # shebangs valid (and within the kernel's shebang length).
  build = builds / uuid.uuid4().hex[:16]
  try:
    _build(build, runtime_root, manifest, lock, relative)
    _smoke(build, runtime_root, manifest, relative)
  except BaseException:
    shutil.rmtree(build, ignore_errors=True)
    raise
  return StagedEnv(key=key, root=build, reused=False)


def rebuild_accepted_env(data_dir: Path | str, app_id: int, root: Path) -> Path | None:
  """Restore the pinned accepted tree with the ordinary build/publication path.

  An env that is still usable was validated when its tree was accepted, so
  restoring after every start does not run the app's code again.
  """
  try:
    return resolve_env(data_dir, app_id, root)
  except PythonEnvUnavailable:
    pass
  staged = prepare_env(data_dir, app_id, root)
  if staged is None:
    return None
  try:
    publish_env(data_dir, app_id, staged)
  except BaseException:
    discard_env(staged)
    raise
  return resolve_env(data_dir, app_id, root)


def publish_env(data_dir: Path | str, app_id: int, staged: StagedEnv) -> PublishedEnv | None:
  """Link a validated build at its keyed location, never replacing a link.

  Creating the symlink at its final name is atomic and exclusive, so of two
  overlapping publications of one key exactly one wins; the loser discards
  its equivalent build. Returns what was created, or None when an existing
  env is used.
  """
  target = envs_parent(data_dir, app_id) / staged.key
  if staged.reused:
    return None
  target.parent.mkdir(parents=True, exist_ok=True)
  relative = os.path.relpath(staged.root, target.parent)
  for _attempt in range(2):
    try:
      os.symlink(relative, target)
      return PublishedEnv(link=target, build=staged.root)
    except FileExistsError:
      if _usable(target):
        # Same key, so an equivalent build from the same lock and interpreter.
        shutil.rmtree(staged.root)
        return None
      if not target.is_symlink():
        raise
      # A link whose build is gone publishes nothing; replace it once.
      target.unlink(missing_ok=True)
  raise PythonEnvBuildError(f"Could not publish the Python environment `{staged.key}`.")


def unpublish_env(published: PublishedEnv | None) -> None:
  """Undo ``publish_env`` for a rolled-back Apply or install.

  Only a link that still points at this publication's own build is removed.
  """
  if published is None or published.build is None:
    return
  link = published.link
  try:
    ours = link.is_symlink() and link.resolve() == published.build.resolve()
  except OSError:
    ours = False
  if ours:
    link.unlink(missing_ok=True)
  shutil.rmtree(published.build, ignore_errors=True)


def discard_env(staged: StagedEnv | None) -> None:
  if staged is not None and not staged.reused:
    shutil.rmtree(staged.root, ignore_errors=True)


def _remove_env(link: Path, builds: Path) -> None:
  build = link.resolve() if link.is_symlink() else None
  if link.is_symlink():
    link.unlink()
  elif link.is_dir():
    shutil.rmtree(link)
  if build is not None and build.parent == builds.resolve():
    shutil.rmtree(build, ignore_errors=True)


def remove_app_envs(data_dir: Path | str, app_id: int) -> None:
  """Remove every env of a deleted app."""
  parent = envs_parent(data_dir, app_id)
  if parent.is_dir():
    for env in parent.iterdir():
      _remove_env(env, _builds(data_dir))
    parent.rmdir()


def discard_interrupted_builds(data_dir: Path | str) -> None:
  """Remove builds no app links to; call only before Apply or install can run."""
  root = Path(data_dir) / ENVS_DIRNAME
  builds = _builds(data_dir)
  if not builds.is_dir():
    return
  linked = set()
  for parent in root.iterdir():
    if not parent.name.isdigit() or not parent.is_dir():
      continue
    for link in parent.iterdir():
      if link.is_symlink():
        linked.add(link.resolve())
  for build in builds.iterdir():
    if build.resolve() not in linked:
      shutil.rmtree(build, ignore_errors=True)


def prune_envs(data_dir: Path | str, app_id: int, kept_runtimes) -> int:
  """Remove an app's envs that no kept runtime tree references.

  The caller holds the runtime reader and job locks exclusively, so no
  process is running from a removed env. Envs for a previous interpreter are
  never referenced again and go with the first prune after replacement. A
  kept tree whose declaration cannot be read keeps every env (fail safe).
  """
  parent = envs_parent(data_dir, app_id)
  if not parent.is_dir():
    return 0
  referenced = set()
  for root in kept_runtimes:
    try:
      referenced.add(declared_key(Path(root)))
    except PythonEnvUnavailable:
      return 0
  removed = 0
  for env in parent.iterdir():
    if env.name not in referenced:
      _remove_env(env, _builds(data_dir))
      removed += 1
  return removed


if __name__ == "__main__":
  # A separate process lets shutdown interrupt the existing synchronous pip
  # builder without leaving an executor thread holding up a platform restart.
  def stop_build(_signal, _frame):
    raise SystemExit(1)  # _run/prepare_env already clean up on BaseException
  signal.signal(signal.SIGTERM, stop_build)
  rebuild_accepted_env(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]))
