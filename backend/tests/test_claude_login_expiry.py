"""The owner sees when the stored Claude sign-in ends.

Möbius refreshes access tokens itself, but the sign-in has a fixed end
(`refreshTokenExpiresAt`) that refreshing does not move, and check_auth refuses
credentials past it. The provider status route shows that date to the true
owner, read straight from the stored credentials with the CLI's rules.
"""

from __future__ import annotations

import json
import time

import pytest

from app import providers
from app.providers import ClaudeProvider, claude_login_expires_at
from test_app_fixtures import create_local_app

END_MS = 1793985933000  # 2026-11-06T17:25:33Z
DAY_MS = 24 * 60 * 60 * 1000


def _store(tmp_path, oauth):
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True, exist_ok=True)
  creds.write_text(json.dumps({"claudeAiOauth": oauth}))
  return str(tmp_path)


def test_login_end_is_read_from_the_stored_sign_in(tmp_path):
  data_dir = _store(tmp_path, {
    "accessToken": "a", "refreshToken": "r", "expiresAt": END_MS + 4 * 60 * 60 * 1000,
    "refreshTokenExpiresAt": END_MS,
  })
  assert claude_login_expires_at(data_dir) == "2026-11-06T17:25:33Z"


@pytest.mark.parametrize("oauth", [
  {"accessToken": "a", "expiresAt": END_MS, "refreshTokenExpiresAt": END_MS},  # no refresh token
  {"accessToken": "a", "refreshToken": "r", "expiresAt": END_MS},  # never recorded
  {"refreshToken": "r", "refreshTokenExpiresAt": True},
  {"refreshToken": "r", "refreshTokenExpiresAt": "2026-11-06"},
  # The CLI treats an end more than three days before the access expiry as stale.
  {"refreshToken": "r", "expiresAt": END_MS + 3 * DAY_MS + 1, "refreshTokenExpiresAt": END_MS},
  # The CLI accepts only an end between 2025-01-01 and ten years from now.
  {"refreshToken": "r", "refreshTokenExpiresAt": 0},
  {"refreshToken": "r", "refreshTokenExpiresAt": 1735689600000 - 1},
  {"refreshToken": "r", "refreshTokenExpiresAt": time.time() * 1000 + 3651 * DAY_MS},
  {"refreshToken": "r", "refreshTokenExpiresAt": 1e20},
  {"refreshToken": "r", "refreshTokenExpiresAt": float("inf")},
  {"refreshToken": "r", "refreshTokenExpiresAt": float("nan")},
])
def test_login_end_is_unknown_without_a_usable_stored_date(tmp_path, oauth):
  assert claude_login_expires_at(_store(tmp_path, oauth)) is None


@pytest.mark.parametrize("end", [float("inf"), float("nan")])
def test_a_non_finite_login_end_cannot_refresh(tmp_path, end):
  data_dir = _store(tmp_path, {
    "accessToken": "a", "refreshToken": "r", "expiresAt": 0, "refreshTokenExpiresAt": end,
  })
  assert ClaudeProvider().check_auth(data_dir) is not None


def test_owner_status_reports_no_login_end_for_an_unusable_stored_date(
  client, auth, monkeypatch, tmp_path,
):
  from app.config import get_settings

  monkeypatch.setattr(get_settings(), "data_dir", _store(tmp_path, {
    "accessToken": "a", "refreshToken": "r",
    "expiresAt": time.time() * 1000 + DAY_MS, "refreshTokenExpiresAt": 1e20,
  }))
  response = client.get("/api/auth/providers/status", headers=auth)
  assert response.status_code == 200
  assert response.json()["claude"]["login_expires_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_in_days,lifetime,expected_in_days", [
  (30, 7 * 86400, 7),  # a shorter sign-in from the refresh wins
  (5, 7 * 86400, 5),  # a refresh never moves the end later
  (5, None, 5),
  (5, 0, 5),
  (5, -86400, 5),
  (5, float("inf"), 5),
  (None, 7 * 86400, None),  # no stored end: the CLI records none either
])
async def test_refresh_keeps_the_earlier_login_end(monkeypatch, stored_in_days, lifetime, expected_in_days):
  import httpx

  body = {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
  if lifetime is not None:
    body["refresh_token_expires_in"] = lifetime
  original = httpx.AsyncClient
  monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
    # Raw JSON, since httpx refuses to encode the non-finite case itself.
    transport=httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(body))),
    **kwargs,
  ))
  now_ms = int(time.time() * 1000)
  oauth = {"accessToken": "a", "refreshToken": "r", "expiresAt": now_ms}
  if stored_in_days is not None:
    oauth["refreshTokenExpiresAt"] = now_ms + stored_in_days * DAY_MS
  refreshed = await providers._refresh_claude_access_token(oauth)
  if expected_in_days is None:
    assert "refreshTokenExpiresAt" not in refreshed
  else:
    end = refreshed["refreshTokenExpiresAt"] - now_ms
    assert expected_in_days * DAY_MS <= end <= expected_in_days * DAY_MS + 5000


def test_login_end_is_unknown_without_credentials(tmp_path):
  assert claude_login_expires_at(str(tmp_path)) is None
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text("not json")
  assert claude_login_expires_at(str(tmp_path)) is None


def test_provider_status_shows_login_end_only_to_the_owner(client, auth, monkeypatch):
  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(providers, "claude_login_expires_at", lambda data_dir: "2026-11-06T17:25:33Z")
  owner = client.get("/api/auth/providers/status", headers=auth).json()
  assert owner["claude"]["login_expires_at"] == "2026-11-06T17:25:33Z"
  assert "login_expires_at" not in owner["codex"]

  from app.auth import create_access_token
  app = create_local_app(client, auth, name="Expiry picker", description="x")
  app_token = create_access_token({"sub": "test", "scope": "app", "app_id": app["id"]})
  as_app = client.get(
    "/api/auth/providers/status", headers={"Authorization": f"Bearer {app_token}"},
  ).json()
  assert as_app["claude"]["configured"] is True
  assert "login_expires_at" not in as_app["claude"]


def test_signed_out_claude_status_has_no_login_end(client, auth, monkeypatch):
  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: "Not signed in.")
  body = client.get("/api/auth/providers/status", headers=auth).json()
  assert "login_expires_at" not in body["claude"]


@pytest.mark.parametrize("extra,expected_seconds", [
  ({"refresh_token_expires_in": 86400}, 86400),
  ({}, None),
  ({"refresh_token_expires_in": None}, None),
  ({"refresh_token_expires_in": True}, None),
])
def test_mobius_sign_in_records_the_login_end_when_the_response_has_it(tmp_path, monkeypatch, extra, expected_seconds):
  from app.config import get_settings
  from app.routes import auth as auth_routes

  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  before_ms = int(time.time() * 1000)
  auth_routes._write_credentials({
    "access_token": "a", "refresh_token": "r", "expires_in": 3600, **extra,
  })
  oauth = json.loads(
    (tmp_path / "cli-auth" / "claude" / ".credentials.json").read_text(),
  )["claudeAiOauth"]
  if expected_seconds is None:
    assert "refreshTokenExpiresAt" not in oauth
  else:
    end = oauth["refreshTokenExpiresAt"] - before_ms
    assert expected_seconds * 1000 <= end <= expected_seconds * 1000 + 5000
