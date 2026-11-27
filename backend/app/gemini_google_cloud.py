"""Google Cloud sign-in for the built-in Gemini provider, without gcloud.

It reuses the connectors' Google-account sign-in (``connector_oauth``): the
Cloud SDK's published installed-app client and Google's hosted "copy this code"
page, exactly as ``gcloud auth login --no-launch-browser`` does, so Möbius
neither ships gcloud nor registers a Google app of its own. The owner opens the
link, approves, pastes the code, then picks the Cloud project Gemini bills to.
Requests reach Gemini on that project's Agent Platform with a Bearer token.

The grant is one small file: the project and the sealed access and refresh
tokens. Tokens are refreshed here, before they expire, with the same refresh
call the connectors use.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from app import connector_oauth as oauth
from app import connectors as core
from app.storage_io import atomic_write

_FLOW_PURPOSE = "gemini-google-cloud"
_TOKEN_RESOURCE = "https://aiplatform.googleapis.com"
_REFRESH_SKEW_SECONDS = 120
_DEFAULT_TTL_SECONDS = 3600
_lock = threading.Lock()


class GoogleCloudSignInError(RuntimeError):
  """A sign-in step failed; the message is shown to the owner."""


def _grant_file(data_dir: str) -> Path:
  return Path(data_dir) / "cli-auth" / "gemini" / "google-cloud.json"


def _read(data_dir: str) -> dict[str, Any]:
  try:
    record = json.loads(_grant_file(data_dir).read_text())
  except (OSError, ValueError):
    return {}
  return record if isinstance(record, dict) else {}


def _write(data_dir: str, record: dict[str, Any]) -> None:
  path = _grant_file(data_dir)
  path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(path, json.dumps(record))


def _sealed(tokens: dict, previous: dict[str, Any]) -> dict[str, Any]:
  """The stored token fields for a token response; a refresh keeps the old refresh token."""
  expires_in = tokens.get("expires_in")
  ttl = expires_in if isinstance(expires_in, (int, float)) and 0 < expires_in <= 86400 else _DEFAULT_TTL_SECONDS
  refresh = tokens.get("refresh_token")
  return {
    "access": core.encrypt_oauth(str(tokens["access_token"])),
    "refresh": core.encrypt_oauth(str(refresh)) if refresh else previous.get("refresh"),
    "expires_at": time.time() + ttl,
  }


def start() -> str:
  """Google's sign-in link. The PKCE verifier travels sealed in ``state``."""
  verifier, challenge = oauth.generate_pkce()
  state = oauth.seal_flow({"purpose": _FLOW_PURPOSE, "verifier": verifier})
  client_id, _ = oauth.gcloud_client_identity()
  return oauth.gcloud_authorization_url(client_id, challenge, state)


async def accept_code(data_dir: str, code: str, state: str) -> list[dict]:
  """Exchange the pasted code and return the owner's Cloud projects to pick from."""
  flow = oauth.open_flow(state)
  if not flow or flow.get("purpose") != _FLOW_PURPOSE:
    raise GoogleCloudSignInError("The sign-in expired. Start it again.")
  client_id, client_secret = oauth.gcloud_client_identity()
  try:
    tokens = await oauth.gcloud_exchange_code(client_id, client_secret, code, flow["verifier"])
  except oauth.OAuthError as exc:
    raise GoogleCloudSignInError("Google did not accept that code. Start the sign-in again.") from exc
  except core.ConnectorError as exc:
    raise GoogleCloudSignInError("Could not reach Google. Please try again.") from exc
  if not tokens.get("refresh_token"):
    raise GoogleCloudSignInError("Google did not grant lasting access. Start the sign-in again.")
  with _lock:
    _write(data_dir, {"project": None, **_sealed(tokens, {})})
  return await oauth.list_google_projects(str(tokens["access_token"]))


def choose_project(data_dir: str, project_id: str) -> None:
  """Finish the sign-in by naming the project Gemini bills to."""
  project_id = project_id.strip()
  if not oauth.valid_gcloud_project_id(project_id):
    raise GoogleCloudSignInError("Choose a Google Cloud project, such as my-project-123.")
  with _lock:
    record = _read(data_dir)
    if not record.get("refresh"):
      raise GoogleCloudSignInError("The sign-in expired. Start it again.")
    _write(data_dir, {**record, "project": project_id})


def project(data_dir: str) -> str | None:
  """The signed-in project, or None when Google Cloud is not connected."""
  record = _read(data_dir)
  value = record.get("project")
  if record.get("refresh") and isinstance(value, str) and oauth.valid_gcloud_project_id(value):
    return value
  return None


def endpoint_root(project_id: str) -> str:
  return f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/publishers/google"


def access_token(data_dir: str) -> str | None:
  """A current access token, refreshed when it is about to expire.

  Runs on a worker thread (the relay reads credentials off the event loop), so
  the connectors' async refresh call runs in its own short loop. A revoked or
  undecryptable grant signs Gemini out; a transient failure keeps the last token.
  """
  with _lock:
    record = _read(data_dir)
    if not record.get("refresh") or not record.get("project"):
      return None
    try:
      if float(record.get("expires_at") or 0) - time.time() > _REFRESH_SKEW_SECONDS:
        return core.decrypt_oauth(record["access"])
      return _refreshed(data_dir, record)
    except oauth.OAuthError:
      _grant_file(data_dir).unlink(missing_ok=True)
      return None
    except core.ConnectorError as exc:
      if exc.transient:
        return core.decrypt_oauth(record["access"])
      _grant_file(data_dir).unlink(missing_ok=True)
      return None


def _refreshed(data_dir: str, record: dict[str, Any]) -> str:
  """Refresh the grant (caller holds the lock) and return the new access token."""
  client_id, client_secret = oauth.gcloud_client_identity()
  tokens = asyncio.run(oauth.refresh_tokens(
    oauth.gcloud_discovery(_TOKEN_RESOURCE), client_id, client_secret,
    core.decrypt_oauth(record["refresh"]),
  ))
  _write(data_dir, {**record, **_sealed(tokens, record)})
  return str(tokens["access_token"])


def forget(data_dir: str) -> None:
  """Remove the sign-in; other Gemini keys are untouched."""
  with _lock:
    _grant_file(data_dir).unlink(missing_ok=True)
