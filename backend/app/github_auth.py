"""GitHub credential store + device-flow state.

The owner connects GitHub from the Contribute app; the token lives on
disk under /data/cli-auth/gh/ (mirroring /data/cli-auth/claude/) in two
files:

- mobius-github.json — the backend's own record (token, login, user_id,
  scopes, token_source, connected_at). This is the ONLY read source for
  get_token(): gh rewrites hosts.yml at will and drops the scope list,
  so reading hosts.yml back would lose information.
- hosts.yml — gh CLI's native credential format, so `gh` (via the
  boot-time ~/.config/gh symlink the entrypoint maintains) and
  `git push` (via the `gh auth git-credential` helper) authenticate
  without any extra plumbing.

Both files are written 0o600 inside a 0o700 dir: the token is readable
only by the mobius user and never reaches the browser — status
endpoints echo metadata, never the token (INV1).

No FastAPI imports here — routes/github.py owns the HTTP surface.
"""

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings

GH_AUTH_DIR = Path(get_settings().data_dir) / "cli-auth" / "gh"
STATE_PATH = GH_AUTH_DIR / "mobius-github.json"
HOSTS_PATH = GH_AUTH_DIR / "hosts.yml"

# Single in-flight device flow (mirrors routes/auth.py's _active_pkce —
# single-owner app, one connect attempt at a time). Shape:
# {device_code, interval, next_poll_at}. Expiry comes from GitHub's
# expires_in (900s default), not the PKCE 300s.
_device_flow: dict | None = None


def get_device_flow() -> dict | None:
  """Returns the in-flight device flow state, or None."""
  return _device_flow


def set_device_flow(flow: dict | None) -> None:
  """Replaces the in-flight device flow state (None clears it)."""
  global _device_flow
  _device_flow = flow


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
  """Writes `content` to `path` created with mode 0600 (same idiom as
  routes/auth.py's _write_credentials — credentials never pass through
  a default-umask open)."""
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    f.write(content)


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

  _write_0600(STATE_PATH, json.dumps({
    "token": token,
    "login": login,
    "user_id": user_id,
    "scopes": scopes,
    "token_source": source,
    "connected_at": datetime.now(UTC).isoformat(),
  }, indent=2))

  # gh's flat hosts.yml shape. login/token come from GitHub's own API
  # ([A-Za-z0-9-] logins, opaque token strings) — no YAML quoting needed.
  _write_0600(HOSTS_PATH, (
    "github.com:\n"
    f"    user: {login}\n"
    f"    oauth_token: {token}\n"
    "    git_protocol: https\n"
  ))

  # Explicit argv list — never shell interpolation — so a hostile login
  # string could at worst become a weird config value, not a command.
  _set_git_identity(login, noreply_email(login, user_id))


def clear_credentials() -> None:
  """Disconnects GitHub: removes the credential dir entirely.

  The git identity set on connect is deliberately left in place —
  a stale name/email on local commits is harmless, and the entrypoint
  resets it to the defaults on the next boot.
  """
  shutil.rmtree(GH_AUTH_DIR, ignore_errors=True)


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
