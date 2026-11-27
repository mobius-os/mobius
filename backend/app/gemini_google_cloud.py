"""Google Cloud sign-in for the built-in Gemini provider, through Google's gcloud.

The owner signs in with Google's own command-line tool, the same way Claude
and Codex sign in: Möbius starts ``gcloud auth application-default login`` on
the server, shows its Google link, and passes back the verification code the
owner copies from Google's page. The sign-in belongs to Google's gcloud, so
Möbius registers no Google app of its own. Requests are billed to the Google
Cloud project the owner names and reach Gemini on the Agent Platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.storage_io import atomic_write

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_LINK = re.compile(r"https://accounts\.google\.com/\S+")
_TOKEN_SECONDS = 45 * 60
# Settings prepares the sign-in before the owner clicks, so an abandoned one
# must not wait for a code forever. Paired with LINK_FRESH_MS in GoogleCloudSignIn.jsx.
_LOGIN_SECONDS = 15 * 60
_login: dict[str, asyncio.subprocess.Process] = {}
_token: dict[str, tuple[str, float]] = {}


class GoogleCloudSignInError(RuntimeError):
  """A sign-in step failed; the message is shown to the owner."""


def gcloud_binary() -> str | None:
  return shutil.which("gcloud") or next(
    (path for path in ("/usr/lib/google-cloud-sdk/bin/gcloud", "/opt/google-cloud-sdk/bin/gcloud")
     if os.access(path, os.X_OK)), None,
  )


def _home(data_dir: str) -> Path:
  return Path(data_dir) / "cli-auth" / "gemini" / "gcloud"


def _project_file(data_dir: str) -> Path:
  return Path(data_dir) / "cli-auth" / "gemini" / "google-cloud.json"


def _env(data_dir: str) -> dict[str, str]:
  home = _home(data_dir)
  home.mkdir(parents=True, exist_ok=True)
  return {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
    "CLOUDSDK_CONFIG": str(home), "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "1",
    "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "1",
  }


def project(data_dir: str) -> str | None:
  """The signed-in project, or None when Google Cloud is not connected."""
  if not (_home(data_dir) / "application_default_credentials.json").exists():
    return None
  try:
    value = json.loads(_project_file(data_dir).read_text()).get("project")
  except (OSError, ValueError, AttributeError):
    return None
  return value if isinstance(value, str) and _PROJECT_ID.match(value) else None


def endpoint_root(project_id: str) -> str:
  return f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/publishers/google"


async def start(data_dir: str) -> str:
  """Start Google's sign-in and return the link the owner opens."""
  binary = gcloud_binary()
  if binary is None:
    raise GoogleCloudSignInError("Google's gcloud tool is not installed on this Möbius.")
  await cancel()
  process = await asyncio.create_subprocess_exec(
    binary, "auth", "application-default", "login", "--no-launch-browser", f"--scopes={_SCOPE}",
    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    env=_env(data_dir),
  )
  _login["active"] = process
  asyncio.get_running_loop().call_later(_LOGIN_SECONDS, _expire, process)
  try:
    link = await asyncio.wait_for(_read_link(process), timeout=30)
  except asyncio.TimeoutError as exc:
    await cancel()
    raise GoogleCloudSignInError("Google's sign-in did not start. Please try again.") from exc
  return link


async def _read_link(process: asyncio.subprocess.Process) -> str:
  while line := await process.stdout.readline():
    match = _LINK.search(line.decode(errors="replace"))
    if match:
      return match.group(0)
  raise GoogleCloudSignInError("Google's sign-in ended before showing a link.")


async def finish(data_dir: str, code: str, project_id: str) -> None:
  """Pass the verification code to gcloud and remember the project."""
  project_id = project_id.strip()
  if not _PROJECT_ID.match(project_id):
    raise GoogleCloudSignInError("Enter a Google Cloud project ID, such as my-project-123.")
  process = _login.get("active")
  if process is None or process.returncode is not None:
    raise GoogleCloudSignInError("The sign-in expired. Start it again.")
  process.stdin.write(code.strip().encode() + b"\n")
  await process.stdin.drain()
  try:
    output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
  except asyncio.TimeoutError as exc:
    await cancel()
    raise GoogleCloudSignInError("Google did not answer the sign-in. Please try again.") from exc
  finally:
    _login.pop("active", None)
  if process.returncode != 0:
    raise GoogleCloudSignInError("Google did not accept that code. Start the sign-in again.")
  atomic_write(_project_file(data_dir), json.dumps({"project": project_id}))
  _token.pop(data_dir, None)


def _expire(process: asyncio.subprocess.Process) -> None:
  """Stop a sign-in nobody finished; a newer one is left alone."""
  if _login.get("active") is not process:
    return
  _login.pop("active")
  if process.returncode is None:
    process.kill()


async def cancel() -> None:
  process = _login.pop("active", None)
  if process is not None and process.returncode is None:
    process.kill()
    await process.wait()


def access_token(data_dir: str) -> str | None:
  """A short-lived access token minted by gcloud from the saved sign-in."""
  cached = _token.get(data_dir)
  if cached and cached[1] > time.monotonic():
    return cached[0]
  binary = gcloud_binary()
  if binary is None:
    return None
  result = subprocess.run(
    [binary, "auth", "application-default", "print-access-token"],
    capture_output=True, text=True, timeout=30, env=_env(data_dir), check=False,
  )
  token = result.stdout.strip()
  if result.returncode != 0 or not token:
    return None
  _token[data_dir] = (token, time.monotonic() + _TOKEN_SECONDS)
  return token


def forget(data_dir: str) -> None:
  """Remove the sign-in and the project; other Gemini keys are untouched."""
  shutil.rmtree(_home(data_dir), ignore_errors=True)
  _project_file(data_dir).unlink(missing_ok=True)
  _token.pop(data_dir, None)
