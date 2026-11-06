"""The owner sees when the stored Claude sign-in hard-expires.

Möbius refreshes access tokens itself, but the sign-in has a fixed end that
renewing does not move. `claude auth status --json` reports it as
`loginExpiresAt`; the provider status route surfaces only that timestamp, held
briefly, to the true owner. The CLI is always faked here: no credential is read.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from app import providers
from app.providers import ClaudeProvider
from test_app_fixtures import create_local_app

# What the CLI prints, account fields included, so tests prove they never leak.
STATUS = {
  "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
  "email": "owner@example.invalid", "orgId": "org-redacted",
  "loginExpiresAt": "2026-11-06T17:25:33.609Z",
}


@pytest.fixture
def fake_cli(monkeypatch):
  calls = []
  outcome = {"stdout": json.dumps(STATUS), "returncode": 0, "before": None}

  def run(argv, **kwargs):
    calls.append(SimpleNamespace(argv=argv, env=kwargs["env"]))
    if outcome["before"]:
      outcome["before"]()
    if isinstance(outcome["stdout"], BaseException):
      raise outcome["stdout"]
    return subprocess.CompletedProcess(
      argv, outcome["returncode"], outcome["stdout"].encode(), b"",
    )

  monkeypatch.setattr(providers.shutil, "which", lambda name, path=None: f"/fake/{name}")
  monkeypatch.setattr(providers.subprocess, "run", run)
  return SimpleNamespace(calls=calls, outcome=outcome)


def test_login_expiry_reads_only_the_timestamp_with_mobius_claude_env(tmp_path, fake_cli):
  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text("{}")

  assert ClaudeProvider().login_expires_at(str(tmp_path)) == "2026-11-06T17:25:33Z"
  (call,) = fake_cli.calls
  assert call.argv == ["/fake/claude", "auth", "status", "--json"]
  assert call.env["CLAUDE_CONFIG_DIR"] == str(creds.parent)


@pytest.mark.parametrize("stdout,expected", [
  (json.dumps({**STATUS, "loginExpiresAt": None}), None),  # API key or no recorded end
  (json.dumps({"loggedIn": False}), None),
  (json.dumps({**STATUS, "loginExpiresAt": "2026-11-06T19:25:33+02:00"}), "2026-11-06T17:25:33Z"),
])
def test_login_expiry_normalizes_or_reports_unknown(tmp_path, fake_cli, stdout, expected):
  fake_cli.outcome["stdout"] = stdout
  assert ClaudeProvider().login_expires_at(str(tmp_path)) == expected


@pytest.mark.parametrize("stdout,returncode", [
  (json.dumps(STATUS), 1),
  ("not json", 0),
  ("[]", 0),
  (json.dumps({**STATUS, "loginExpiresAt": 1762450000000}), 0),
  (json.dumps({**STATUS, "loginExpiresAt": "2026-11-06T17:25:33"}), 0),  # no zone
  (subprocess.TimeoutExpired(["claude"], 15), 0),
])
def test_failed_login_expiry_read_raises_without_echoing_output(tmp_path, fake_cli, stdout, returncode):
  fake_cli.outcome.update(stdout=stdout, returncode=returncode)
  with pytest.raises(Exception) as caught:
    ClaudeProvider().login_expires_at(str(tmp_path))
  assert "owner@example.invalid" not in str(caught.value)


def test_login_expiry_is_held_but_failures_are_not(tmp_path, fake_cli, monkeypatch):
  now = [1000.0]
  monkeypatch.setattr(providers.time, "monotonic", lambda: now[0])
  provider = ClaudeProvider()

  fake_cli.outcome["returncode"] = 1
  with pytest.raises(RuntimeError):
    provider.login_expires_at(str(tmp_path))
  fake_cli.outcome["returncode"] = 0
  assert provider.login_expires_at(str(tmp_path)) == "2026-11-06T17:25:33Z"
  assert len(fake_cli.calls) == 2  # the failure was not held

  now[0] += ClaudeProvider.LOGIN_EXPIRY_HOLD_SECONDS - 1
  assert provider.login_expires_at(str(tmp_path)) == "2026-11-06T17:25:33Z"
  assert len(fake_cli.calls) == 2

  now[0] += 2
  provider.login_expires_at(str(tmp_path))
  assert len(fake_cli.calls) == 3


def test_sign_in_change_drops_the_held_expiry_and_a_read_in_flight(tmp_path, fake_cli):
  provider = ClaudeProvider()
  provider.login_expires_at(str(tmp_path))
  provider.forget_account_reads()
  provider.login_expires_at(str(tmp_path))
  assert len(fake_cli.calls) == 2

  # A read that began under the old sign-in serves its caller, never the hold.
  provider.forget_account_reads()
  fake_cli.outcome["before"] = provider.forget_account_reads
  provider.login_expires_at(str(tmp_path))
  fake_cli.outcome["before"] = None
  provider.login_expires_at(str(tmp_path))
  assert len(fake_cli.calls) == 4


def test_provider_status_shows_login_expiry_only_to_the_owner(client, auth, monkeypatch):
  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(
    ClaudeProvider, "login_expires_at", lambda self, data_dir: "2026-11-06T17:25:33Z",
  )
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


def test_provider_status_reports_unknown_expiry_when_the_read_fails(client, auth, monkeypatch):
  def fail(self, data_dir):
    raise RuntimeError("claude auth status exited 1")

  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(ClaudeProvider, "login_expires_at", fail)
  body = client.get("/api/auth/providers/status", headers=auth).json()
  assert body["claude"]["configured"] is True
  assert body["claude"]["login_expires_at"] is None


def test_signed_out_claude_status_does_not_run_the_cli(client, auth, monkeypatch):
  def unexpected(self, data_dir):
    raise AssertionError("expiry read while signed out")

  monkeypatch.setattr(ClaudeProvider, "check_auth", lambda self, data_dir: "Not signed in.")
  monkeypatch.setattr(ClaudeProvider, "login_expires_at", unexpected)
  body = client.get("/api/auth/providers/status", headers=auth).json()
  assert "login_expires_at" not in body["claude"]


@pytest.mark.parametrize("extra,expected_seconds", [
  ({"refresh_token_expires_in": 86400}, 86400),
  ({}, None),
  ({"refresh_token_expires_in": None}, None),
  ({"refresh_token_expires_in": True}, None),
])
def test_mobius_sign_in_records_the_login_end_like_the_cli(tmp_path, monkeypatch, extra, expected_seconds):
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
