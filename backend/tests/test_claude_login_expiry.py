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
  {"refreshToken": "r", "refreshTokenExpiresAt": 10**400},  # beyond float range
  {"refreshToken": "r", "refreshTokenExpiresAt": float("inf")},
  {"refreshToken": "r", "refreshTokenExpiresAt": float("nan")},
])
def test_login_end_is_unknown_without_a_usable_stored_date(tmp_path, oauth):
  assert claude_login_expires_at(_store(tmp_path, oauth)) is None


# check_auth reads the end with the same rule as claude_login_expires_at: an
# end the CLI would reject is no end, so the sign-in can still refresh.
@pytest.mark.parametrize("end", [
  0, True, "2026-11-06", 1e20, pytest.param(10**400, id="int-beyond-float"),
  float("inf"), float("nan"),
])
def test_a_login_end_the_cli_rejects_does_not_block_refresh(tmp_path, end):
  data_dir = _store(tmp_path, {
    "accessToken": "a", "refreshToken": "r", "expiresAt": 0, "refreshTokenExpiresAt": end,
  })
  assert ClaudeProvider().check_auth(data_dir) is None


def test_a_passed_login_end_blocks_refresh(tmp_path):
  data_dir = _store(tmp_path, {
    "accessToken": "a", "refreshToken": "r", "expiresAt": 0,
    "refreshTokenExpiresAt": time.time() * 1000 - DAY_MS,
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


def _serve_token_response(monkeypatch, body):
  import httpx

  original = httpx.AsyncClient
  monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
    # Raw JSON, since httpx refuses to encode the non-finite case itself.
    transport=httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(body))),
    **kwargs,
  ))


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_in_days,lifetime,expected_in_days", [
  (30, 7 * 86400, 7),  # a shorter sign-in from the refresh wins
  (5, 7 * 86400, 5),  # a refresh never moves the end later
  (5, None, 5),
  # Like the CLI, a zero or negative lifetime ends the sign-in now or earlier.
  (5, 0, 0),
  (5, -86400, -1),
  (5, float("inf"), 5),
  (5, 1e308, 5),
  pytest.param(5, 10**400, 5, id="5-int-beyond-float-5"),
  (5, 3651 * 86400, 5),  # past the CLI's ten-year horizon
  (None, 7 * 86400, None),  # no stored end: the CLI records none either
  # A stored end the CLI would reject is dropped, not kept or replaced.
  (-3650, 7 * 86400, None),  # before 2025, like a stored 0
  (3651, 7 * 86400, None),
  (float("nan"), 7 * 86400, None),
])
async def test_refresh_keeps_the_earlier_login_end(monkeypatch, stored_in_days, lifetime, expected_in_days):
  body = {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
  if lifetime is not None:
    body["refresh_token_expires_in"] = lifetime
  _serve_token_response(monkeypatch, body)
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
  for text in ("not json", "[]", '"x"', '{"claudeAiOauth": []}'):
    creds.write_text(text)
    assert claude_login_expires_at(str(tmp_path)) is None


def test_owner_status_survives_credentials_that_are_not_an_object(
  client, auth, monkeypatch, tmp_path,
):
  from app.config import get_settings

  # The file can change between check_auth's read and this one.
  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: None)
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text("[]")
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  response = client.get("/api/auth/providers/status", headers=auth)
  assert response.status_code == 200
  assert response.json()["claude"]["login_expires_at"] is None


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
  ({"refresh_token_expires_in": 0}, 0),
  ({"refresh_token_expires_in": -86400}, -86400),
  ({}, None),
  ({"refresh_token_expires_in": None}, None),
  ({"refresh_token_expires_in": True}, None),
  ({"refresh_token_expires_in": float("inf")}, None),
  ({"refresh_token_expires_in": 1e308}, None),
  ({"refresh_token_expires_in": 3651 * 86400}, None),  # past the CLI's horizon
  ({"refresh_token_expires_in": 10**400}, None),
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


# Sign-in and refresh turn a token response into stored credentials through
# claude_oauth_after_token_grant. Neither stores a response without an access
# token; only a new sign-in, which has spent nothing yet, also refuses one
# without a refresh token or a usable expiry.
NO_ACCESS_TOKEN_RESPONSES = [
  ["not", "an", "object"],
  {"refresh_token": "r", "expires_in": 3600},
  {"access_token": "", "refresh_token": "r", "expires_in": 3600},
  {"access_token": ["a"], "refresh_token": "r", "expires_in": 3600},
]
UNUSABLE_EXPIRES_IN = [None, True, "3600", 0, -3600, float("inf"), float("nan"), 1e308, 10**400]
UNUSABLE_EXPIRES_IN_IDS = ["none", "bool", "string", "zero", "negative", "inf", "nan", "1e308", "int-beyond-float"]
UNUSABLE_SIGN_IN_RESPONSES = [
  *NO_ACCESS_TOKEN_RESPONSES,
  {"access_token": "a", "expires_in": 3600},
  {"access_token": "a", "refresh_token": "r"},
  *({"access_token": "a", "refresh_token": "r", "expires_in": value} for value in UNUSABLE_EXPIRES_IN),
]


@pytest.mark.parametrize("body", UNUSABLE_SIGN_IN_RESPONSES)
def test_sign_in_refuses_an_unusable_token_response(tmp_path, monkeypatch, body):
  from app.config import get_settings
  from app.routes import auth as auth_routes

  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  with pytest.raises(RuntimeError):
    auth_routes._write_credentials(body)
  assert not (tmp_path / "cli-auth" / "claude" / ".credentials.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NO_ACCESS_TOKEN_RESPONSES)
async def test_refresh_refuses_a_response_without_an_access_token(monkeypatch, body):
  _serve_token_response(monkeypatch, body)
  with pytest.raises(RuntimeError):
    await providers._refresh_claude_access_token({"refreshToken": "r", "expiresAt": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", UNUSABLE_EXPIRES_IN, ids=UNUSABLE_EXPIRES_IN_IDS)
async def test_refresh_keeps_rotated_tokens_when_the_expiry_is_unusable(monkeypatch, expires_in):
  # Anthropic has already spent the old refresh token, so dropping the
  # response would force a reconnect; the access token is stored as expired
  # so the next use refreshes again.
  _serve_token_response(monkeypatch, {
    "access_token": "a2", "refresh_token": "r2", "expires_in": expires_in,
  })
  refreshed = await providers._refresh_claude_access_token({"refreshToken": "r", "expiresAt": 0})
  assert (refreshed["accessToken"], refreshed["refreshToken"]) == ("a2", "r2")
  assert refreshed["expiresAt"] <= time.time() * 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_token", ["missing", None, ""])
async def test_refresh_without_a_new_refresh_token_keeps_the_stored_one(monkeypatch, refresh_token):
  body = {"access_token": "a2", "expires_in": 3600}
  if refresh_token != "missing":
    body["refresh_token"] = refresh_token
  _serve_token_response(monkeypatch, body)
  before_ms = int(time.time() * 1000)
  refreshed = await providers._refresh_claude_access_token({"refreshToken": "r", "expiresAt": 0})
  assert (refreshed["accessToken"], refreshed["refreshToken"]) == ("a2", "r")
  assert refreshed["expiresAt"] >= before_ms + 3600 * 1000


def test_a_new_sign_in_drops_the_previous_accounts_keys(tmp_path, monkeypatch):
  from app.config import get_settings
  from app.routes import auth as auth_routes

  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  previous = {key: {"previous": "account"} for key in providers._CLAUDE_ACCOUNT_KEYS}
  assert set(previous) == {
    "claudeAiOauth", "organizationUuid", "trustedDeviceToken",
    "trustedDeviceEnrolledAfterRefusalAtMs", "enterpriseGateway", "designOauth",
    "cloudSessionsOauth",
  }
  creds.write_text(json.dumps({**previous, "mcpOAuth": {"server": {"accessToken": "mcp"}}}))
  auth_routes._write_credentials({
    "access_token": "a", "refresh_token": "r", "expires_in": 3600,
    "organization": {"uuid": "22222222-2222-4222-8222-222222222222"},
  })
  saved = json.loads(creds.read_text())
  assert set(saved) == {"claudeAiOauth", "organizationUuid", "mcpOAuth"}
  assert saved["organizationUuid"] == "22222222-2222-4222-8222-222222222222"
  assert saved["claudeAiOauth"]["accessToken"] == "a"
  assert saved["mcpOAuth"] == {"server": {"accessToken": "mcp"}}


@pytest.mark.parametrize("optional", [
  {"account": "person@example.com", "organization": ["uuid"]},
  {"account": {"email_address": 7}, "organization": {"uuid": None}},
  {"account": None, "organization": "22222222-2222-4222-8222-222222222222", "scope": 5},
])
def test_a_new_sign_in_ignores_malformed_optional_fields(tmp_path, monkeypatch, optional):
  # The one-time code is already spent, so a malformed optional field must
  # not lose the sign-in.
  from app.config import get_settings
  from app.routes import auth as auth_routes

  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  auth_routes._write_credentials({
    "access_token": "a", "refresh_token": "r", "expires_in": 3600, **optional,
  })
  saved = json.loads((tmp_path / "cli-auth" / "claude" / ".credentials.json").read_text())
  assert "organizationUuid" not in saved
  oauth = saved["claudeAiOauth"]
  assert (oauth["accessToken"], oauth["email"], oauth["scopes"]) == ("a", "", [])


def test_a_new_sign_in_replaces_the_sign_in_and_keeps_the_rest_of_the_file(tmp_path, monkeypatch):
  from app.config import get_settings
  from app.routes import auth as auth_routes

  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  auth_routes._write_credentials({
    "access_token": "a", "refresh_token": "r", "expires_in": 3600,
    "refresh_token_expires_in": 86400,
  })
  assert creds.stat().st_mode & 0o777 == 0o600
  stored = json.loads(creds.read_text())
  stored["mcpOAuth"] = {"server": {"accessToken": "mcp"}}
  stored["organizationUuid"] = "11111111-1111-4111-8111-111111111111"
  creds.write_text(json.dumps(stored))

  before_ms = int(time.time() * 1000)
  auth_routes._write_credentials({
    "access_token": "a2", "refresh_token": "r2", "expires_in": 3600,
    "refresh_token_expires_in": 30 * 86400, "scope": "user:inference",
    "account": {"email_address": "person@example.com"},
    "organization": {"uuid": "22222222-2222-4222-8222-222222222222"},
  })
  saved = json.loads(creds.read_text())
  assert saved["mcpOAuth"] == {"server": {"accessToken": "mcp"}}
  # The organization is the new sign-in's, never the previous account's.
  assert saved["organizationUuid"] == "22222222-2222-4222-8222-222222222222"
  oauth = saved["claudeAiOauth"]
  assert (oauth["accessToken"], oauth["refreshToken"]) == ("a2", "r2")
  assert oauth["scopes"] == ["user:inference"]
  assert oauth["email"] == "person@example.com"
  # Unlike a refresh, a new sign-in takes its end from the response.
  assert oauth["refreshTokenExpiresAt"] - before_ms >= 30 * DAY_MS
  assert creds.stat().st_mode & 0o777 == 0o600
  assert not creds.with_suffix(".json.tmp").exists()

  # A sign-in whose response names no organization leaves none behind.
  auth_routes._write_credentials({"access_token": "a3", "refresh_token": "r3", "expires_in": 3600})
  saved = json.loads(creds.read_text())
  assert "organizationUuid" not in saved and saved["mcpOAuth"]
