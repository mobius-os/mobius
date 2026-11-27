"""Gemini's Google Cloud sign-in, built on the connectors' Google-account sign-in."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app import connector_oauth as oauth
from app import connectors as core
from app import gemini_google_cloud as cloud
from app import providers

PROJECTS = [{"project_id": "my-proj-123", "name": "My project"}]


def _sign_in(monkeypatch, data_dir, tokens=None):
  async def exchange(client_id, client_secret, code, verifier):
    assert code == "4/abc" and verifier
    return tokens or {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}

  async def projects(access):
    assert access == "access-1"
    return PROJECTS

  monkeypatch.setattr(oauth, "gcloud_exchange_code", exchange)
  monkeypatch.setattr(oauth, "list_google_projects", projects)
  state = parse_qs(urlparse(cloud.start()).query)["state"][0]
  return asyncio.run(cloud.accept_code(data_dir, "4/abc", state))


def test_the_link_is_googles_code_page_with_a_sealed_verifier():
  query = parse_qs(urlparse(cloud.start()).query)
  assert query["redirect_uri"] == ["https://sdk.cloud.google.com/authcode.html"]
  assert query["code_challenge_method"] == ["S256"]
  flow = oauth.open_flow(query["state"][0])
  assert flow["purpose"] == "gemini-google-cloud" and flow["verifier"]


def test_a_code_lists_projects_and_the_chosen_project_connects(tmp_path, monkeypatch):
  data_dir = str(tmp_path)
  assert _sign_in(monkeypatch, data_dir) == PROJECTS
  assert cloud.project(data_dir) is None, "not connected until a project is chosen"
  stored = (tmp_path / "cli-auth" / "gemini" / "google-cloud.json").read_text()
  assert "refresh-1" not in stored and "access-1" not in stored, "tokens are sealed"

  cloud.choose_project(data_dir, "my-proj-123")
  monkeypatch.setattr(providers, "provider_enabled", lambda *_: True)
  gemini = providers.PROVIDERS["gemini"]
  assert gemini.connection_kind(data_dir) == "google_cloud"
  credential = gemini.read_relay_key(data_dir)
  assert credential.base_url.endswith("/projects/my-proj-123/locations/global/publishers/google")
  assert credential.headers == {"authorization": "Bearer access-1", "x-goog-user-project": "my-proj-123"}


def test_a_sign_in_from_another_flow_or_without_lasting_access_is_refused(tmp_path, monkeypatch):
  with pytest.raises(cloud.GoogleCloudSignInError, match="expired"):
    asyncio.run(cloud.accept_code(str(tmp_path), "4/abc", oauth.seal_flow({"purpose": "connector"})))
  with pytest.raises(cloud.GoogleCloudSignInError, match="lasting access"):
    _sign_in(monkeypatch, str(tmp_path), {"access_token": "access-1"})
  with pytest.raises(cloud.GoogleCloudSignInError, match="project"):
    cloud.choose_project(str(tmp_path), "Not A Project")


def _expire(tmp_path):
  path = tmp_path / "cli-auth" / "gemini" / "google-cloud.json"
  record = json.loads(path.read_text())
  path.write_text(json.dumps({**record, "expires_at": time.time() - 1}))


def test_an_expiring_token_is_refreshed_and_kept(tmp_path, monkeypatch):
  data_dir = str(tmp_path)
  _sign_in(monkeypatch, data_dir)
  cloud.choose_project(data_dir, "my-proj-123")
  _expire(tmp_path)

  async def refresh(discovery, client_id, client_secret, refresh_token):
    assert refresh_token == "refresh-1"
    return {"access_token": "access-2", "expires_in": 3600}

  monkeypatch.setattr(oauth, "refresh_tokens", refresh)
  assert cloud.access_token(data_dir) == "access-2"
  assert cloud.access_token(data_dir) == "access-2", "the new token is stored, not refreshed again"
  assert cloud.project(data_dir) == "my-proj-123", "the refresh token survives a refresh without one"


def test_a_revoked_sign_in_disconnects_but_a_network_blip_keeps_the_last_token(tmp_path, monkeypatch):
  data_dir = str(tmp_path)
  _sign_in(monkeypatch, data_dir)
  cloud.choose_project(data_dir, "my-proj-123")
  _expire(tmp_path)

  async def unreachable(*_):
    raise core.ConnectorError("down", transient=True)

  monkeypatch.setattr(oauth, "refresh_tokens", unreachable)
  assert cloud.access_token(data_dir) == "access-1"

  async def revoked(*_):
    raise oauth.OAuthError("revoked")

  monkeypatch.setattr(oauth, "refresh_tokens", revoked)
  assert cloud.access_token(data_dir) is None
  assert cloud.project(data_dir) is None
