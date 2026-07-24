"""GitHub credential store + device-flow state.

The owner connects GitHub from the Contribute app; the token lives on
disk under /data/cli-auth/gh/ (mirroring /data/cli-auth/claude/) in two
credential files:

- mobius-github.json — the backend's own record (token, login, user_id,
  scopes, token_source, connected_at). This is the ONLY read source for
  get_token(): gh rewrites hosts.yml at will and drops the scope list,
  so reading hosts.yml back would lose information.
- hosts.yml — gh CLI's native credential format, so `gh` (via the
  boot-time ~/.config/gh symlink the entrypoint maintains) and
  `git push` (via the `gh auth git-credential` helper) authenticate
  without any extra plumbing.

The short-lived device authorization attempt is also stored there as
device-flow.json. Persisting it means a backend restart cannot silently lose
the browser's active attempt, while the attempt id prevents an older tab from
polling or cancelling a newer one.

All files are written atomically with mode 0o600 inside a 0o700 dir: the token
is readable only by the mobius user and never reaches the browser — status
endpoints echo metadata, never the token (INV1).

No FastAPI imports here — routes/github.py owns the HTTP surface.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings

GH_AUTH_DIR = Path(get_settings().data_dir) / "cli-auth" / "gh"
STATE_PATH = GH_AUTH_DIR / "mobius-github.json"
HOSTS_PATH = GH_AUTH_DIR / "hosts.yml"
DEVICE_FLOW_PATH = GH_AUTH_DIR / "device-flow.json"
CONNECTION_LOCK_PATH = GH_AUTH_DIR.parent / ".github-connection.lock"

# One owner-scoped attempt is active at a time. Unlike the old process-only
# dictionary, the attempt is persisted so a backend restart during GitHub's
# authorization window does not strand the UI. The opaque attempt_id keeps a
# stale tab from polling or cancelling a newer attempt.


def get_device_flow() -> dict | None:
  """Returns the current durable device-flow attempt.

  Read the tiny atomic file every time. A process cache would make another
  worker's poll/cancel invisible and could reuse a one-shot GitHub device code.
  """
  try:
    with DEVICE_FLOW_PATH.open(encoding="utf-8") as handle:
      payload = json.load(handle)
  except (OSError, ValueError):
    return None
  return payload if isinstance(payload, dict) else None


def set_device_flow(flow: dict | None) -> None:
  """Persists the current attempt atomically (None clears it)."""
  if flow is None:
    try:
      DEVICE_FLOW_PATH.unlink()
    except FileNotFoundError:
      pass
    return
  os.makedirs(GH_AUTH_DIR, mode=0o700, exist_ok=True)
  os.chmod(GH_AUTH_DIR, 0o700)
  _write_0600(DEVICE_FLOW_PATH, json.dumps(flow, indent=2))


def try_acquire_connection_lock() -> int | None:
  """Try to acquire the owner-wide GitHub mutation lock without blocking."""
  CONNECTION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  fd = os.open(CONNECTION_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
  try:
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    os.close(fd)
    return None
  except BaseException:
    os.close(fd)
    raise
  return fd


def release_connection_lock(fd: int) -> None:
  """Release a descriptor returned by :func:`try_acquire_connection_lock`."""
  try:
    fcntl.flock(fd, fcntl.LOCK_UN)
  finally:
    os.close(fd)


def read_state() -> dict | None:
  """Returns the parsed mobius-github.json, or None if absent/corrupt."""
  try:
    with open(STATE_PATH, encoding="utf-8") as f:
      state = json.load(f)
  except (OSError, ValueError):
    return None
  return state if isinstance(state, dict) else None


def get_token() -> str | None:
  """Returns the stored GitHub token, or None when not connected.

  Reads mobius-github.json only — never hosts.yml (gh rewrites that
  file and it carries no scope information).
  """
  state = read_state() or {}
  token = state.get("token")
  return token if isinstance(token, str) and token else None


def noreply_email(login: str, user_id: int | str | None) -> str:
  user_id_text = str(user_id or "").strip()
  local_part = f"{user_id_text}+{login}" if user_id_text else login
  return f"{local_part}@users.noreply.github.com"


def _set_git_identity(name: str, email: str) -> None:
  targets: list[tuple[str, list[str]]] = [
    ("global", ["git", "config", "--global"]),
  ]
  data_dir = Path(get_settings().data_dir)
  for repo in (data_dir, data_dir / "platform"):
    if (repo / ".git").exists():
      targets.append((str(repo), ["git", "-C", str(repo), "config"]))

  for _, base_cmd in targets:
    for key, value in (("user.name", name), ("user.email", email)):
      subprocess.run(
        [*base_cmd, key, value],
        check=False, capture_output=True, timeout=10,
      )


def _write_0600(path: Path, content: str) -> None:
  """Atomically writes `content` with mode 0600 in the target directory."""
  path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  fd, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
  )
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(content)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary, path)
  except BaseException:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def write_credentials(
  *,
  token: str,
  login: str,
  user_id: int | str,
  scopes: list[str],
  source: str,
) -> None:
  """Persists a validated GitHub token (source: "device" | "pat").

  Writes both credential files 0600 inside a 0700 dir, then points the
  global git identity at the connected user so contributed commits
  attribute correctly (<user_id>+<login>@users.noreply.github.com is
  GitHub's no-reply address form). The entrypoint re-asserts the same
  identity from mobius-github.json at every boot, so it survives
  container recreation.
  """
  os.makedirs(GH_AUTH_DIR, mode=0o700, exist_ok=True)
  # makedirs only applies the mode on creation; tighten a pre-existing dir.
  os.chmod(GH_AUTH_DIR, 0o700)

  # hosts.yml is a derived CLI view. Publish it before the canonical state so
  # /status cannot report a newly connected account whose gh credential file
  # failed to materialize. Backend GitHub/git subprocesses also inject GH_TOKEN
  # from STATE_PATH, so a later gh rewrite cannot desynchronize those actions.
  _write_0600(HOSTS_PATH, (
    "github.com:\n"
    f"    user: {json.dumps(login)}\n"
    f"    oauth_token: {json.dumps(token)}\n"
    "    git_protocol: https\n"
  ))

  # Canonical commit point: get_token()/status read only this atomic record.
  _write_0600(STATE_PATH, json.dumps({
    "token": token,
    "login": login,
    "user_id": user_id,
    "scopes": scopes,
    "token_source": source,
    "connected_at": datetime.now(UTC).isoformat(),
  }, indent=2))

  # Explicit argv list — never shell interpolation — so a hostile login
  # string could at worst become a weird config value, not a command.
  _set_git_identity(login, noreply_email(login, user_id))


def clear_credentials() -> None:
  """Disconnects GitHub: removes the credential dir entirely.

  The git identity set on connect is deliberately left in place —
  a stale name/email on local commits is harmless, and the entrypoint
  resets it to the defaults on the next boot.
  """
  try:
    shutil.rmtree(GH_AUTH_DIR)
  except FileNotFoundError:
    pass


_gh_version_cache: str | None = None
_gh_version_checked = False


def gh_version() -> str | None:
  """Returns the installed gh CLI version (e.g. "2.96.0"), or None.

  Cached for the process lifetime — the binary is baked into the image
  and cannot change under a running server.
  """
  global _gh_version_cache, _gh_version_checked
  if _gh_version_checked:
    return _gh_version_cache
  _gh_version_checked = True
  if shutil.which("gh"):
    try:
      out = subprocess.run(
        ["gh", "--version"], capture_output=True, text=True, timeout=10,
      ).stdout
      m = re.search(r"gh version (\S+)", out or "")
      _gh_version_cache = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
      _gh_version_cache = None
  return _gh_version_cache
