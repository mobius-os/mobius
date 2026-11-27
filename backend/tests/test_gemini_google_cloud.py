"""Gemini's Google Cloud (gcloud) sign-in."""

import asyncio
import json
import stat

import pytest

from app import gemini_google_cloud, providers


def test_google_cloud_connection_sends_a_bearer_token_to_the_project(tmp_path, monkeypatch):
  data_dir = str(tmp_path)
  home = tmp_path / "cli-auth" / "gemini" / "gcloud"
  home.mkdir(parents=True)
  (home / "application_default_credentials.json").write_text("{}")
  (tmp_path / "cli-auth" / "gemini" / "google-cloud.json").write_text(json.dumps({"project": "my-proj-123"}))
  monkeypatch.setattr(gemini_google_cloud, "access_token", lambda _d: "tok")
  monkeypatch.setattr(providers, "provider_enabled", lambda *_: True)
  gemini = providers.PROVIDERS["gemini"]
  assert gemini.connection_kind(data_dir) == "google_cloud"
  credential = gemini.read_relay_key(data_dir)
  assert credential.base_url == (
    "https://aiplatform.googleapis.com/v1/projects/my-proj-123/locations/global/publishers/google"
  )
  assert credential.headers == {"authorization": "Bearer tok", "x-goog-user-project": "my-proj-123"}


def _fake_gcloud(tmp_path, accept: bool):
  script = tmp_path / "gcloud"
  script.write_text(
    "#!/bin/sh\n"
    "echo 'Go to the following link in your browser:'\n"
    "echo '    https://accounts.google.com/o/oauth2/auth?client_id=x&scope=y'\n"
    "printf 'Once finished, enter the verification code provided in your browser: '\n"
    "read code\n"
    f"{'mkdir -p \"$CLOUDSDK_CONFIG\" && echo {{}} > \"$CLOUDSDK_CONFIG/application_default_credentials.json\"' if accept else 'exit 1'}\n"
  )
  script.chmod(script.stat().st_mode | stat.S_IEXEC)
  return str(script)


@pytest.mark.parametrize("accept", [True, False])
def test_gcloud_sign_in_shows_the_link_then_takes_the_code(tmp_path, monkeypatch, accept):
  monkeypatch.setattr(gemini_google_cloud, "gcloud_binary", lambda: _fake_gcloud(tmp_path, accept))
  data_dir = str(tmp_path / "data")

  async def flow():
    link = await gemini_google_cloud.start(data_dir)
    assert link.startswith("https://accounts.google.com/o/oauth2/auth")
    await gemini_google_cloud.finish(data_dir, "4/abc", "my-proj-123")

  if accept:
    asyncio.run(flow())
    assert gemini_google_cloud.project(data_dir) == "my-proj-123"
  else:
    with pytest.raises(gemini_google_cloud.GoogleCloudSignInError):
      asyncio.run(flow())
    assert gemini_google_cloud.project(data_dir) is None


def test_sign_in_refuses_a_malformed_project(tmp_path):
  with pytest.raises(gemini_google_cloud.GoogleCloudSignInError, match="project ID"):
    asyncio.run(gemini_google_cloud.finish(str(tmp_path), "code", "Not A Project"))


def test_an_abandoned_prepared_sign_in_is_stopped_but_a_newer_one_is_kept(tmp_path, monkeypatch):
  monkeypatch.setattr(gemini_google_cloud, "gcloud_binary", lambda: _fake_gcloud(tmp_path, True))
  monkeypatch.setattr(gemini_google_cloud, "_LOGIN_SECONDS", 0.2)
  data_dir = str(tmp_path / "data")

  async def flow():
    await gemini_google_cloud.start(data_dir)
    first = gemini_google_cloud._login["active"]
    await asyncio.sleep(0.1)
    await gemini_google_cloud.start(data_dir)
    second = gemini_google_cloud._login["active"]
    await asyncio.sleep(0.15)
    assert first.returncode is not None
    assert gemini_google_cloud._login.get("active") is second and second.returncode is None
    await asyncio.sleep(0.2)
    await asyncio.wait_for(second.wait(), timeout=2)
    assert "active" not in gemini_google_cloud._login
    with pytest.raises(gemini_google_cloud.GoogleCloudSignInError, match="expired"):
      await gemini_google_cloud.finish(data_dir, "4/abc", "my-proj-123")

  asyncio.run(flow())
