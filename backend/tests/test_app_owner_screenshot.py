from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.app_capabilities import contract_from_manifest
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from test_app_fixtures import create_local_app


def _manifest(permission=True):
  return {
    "id": "owner-shot",
    "name": "Owner shot",
    "version": "0.1.0",
    "description": "Capture the owner's shell",
    "entry": "index.jsx",
    "permissions": {"owner_screenshot": permission},
  }


def _make_app(client, owner_token, db, *, allowed: bool):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  target = client.post(
    "/api/chats", json={"title": "Owning chat"}, headers=owner_auth,
  )
  assert target.status_code == 200, target.text
  app = create_local_app(
    client,
    owner_auth,
    name="Screenshot bridge" if allowed else "No screenshot bridge",
    chat_id=target.json()["id"],
    manifest_extra={
      "permissions": {"owner_screenshot": True} if allowed else {},
    },
  )
  app_token = client.post(
    "/api/auth/app-token",
    json={"app_id": app["id"]},
    headers=owner_auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {app_token}"}
  output = client.post(
    "/api/app-chats",
    json={"title": "Telegram output"},
    headers=app_auth,
  )
  assert output.status_code == 201, output.text
  db.expire_all()
  return app, app_auth, target.json()["id"], output.json()["id"]


def test_owner_screenshot_permission_is_reviewed_in_contract():
  manifest = _manifest()
  validate_manifest_contract(manifest)
  assert contract_from_manifest(manifest)["data"]["owner_screenshot"] is True


def test_owner_screenshot_permission_must_be_boolean():
  with pytest.raises(ManifestContractError, match="must be a boolean"):
    validate_manifest_contract(_manifest("yes"))


def test_permitted_app_captures_to_its_own_chat(
  client, owner_token, db,
):
  app, app_auth, target_chat_id, output_chat_id = _make_app(
    client, owner_token, db, allowed=True,
  )
  with patch(
    "app.routes.chats._capture_owner_shell",
    return_value="owner-shell.png",
  ) as capture:
    response = client.post(
      f"/api/app-chats/{output_chat_id}/owner-screenshot",
      json={"warm_only": False},
      headers=app_auth,
    )
  assert response.status_code == 200, response.text
  assert response.json() == {
    "ready": True,
    "chat_id": output_chat_id,
    "filename": "owner-shell.png",
  }
  kwargs = capture.call_args.kwargs
  assert kwargs["app_id"] == app["id"]
  assert kwargs["target_chat_id"] == target_chat_id
  assert kwargs["output_chat_id"] == output_chat_id
  assert kwargs["warm_only"] is False
  assert isinstance(kwargs["owner_token"], str) and kwargs["owner_token"]


def test_warmup_returns_no_media_filename(client, owner_token, db):
  _, app_auth, _, output_chat_id = _make_app(
    client, owner_token, db, allowed=True,
  )
  with patch("app.routes.chats._capture_owner_shell", return_value=None):
    response = client.post(
      f"/api/app-chats/{output_chat_id}/owner-screenshot",
      json={"warm_only": True},
      headers=app_auth,
    )
  assert response.status_code == 200, response.text
  assert response.json()["ready"] is True
  assert response.json()["filename"] is None


def test_unpermitted_app_cannot_capture_owner_shell(client, owner_token, db):
  _, app_auth, _, output_chat_id = _make_app(
    client, owner_token, db, allowed=False,
  )
  with patch("app.routes.chats._capture_owner_shell") as capture:
    response = client.post(
      f"/api/app-chats/{output_chat_id}/owner-screenshot",
      json={"warm_only": False},
      headers=app_auth,
    )
  assert response.status_code == 403
  assert "permissions.owner_screenshot=true" in response.json()["detail"]
  capture.assert_not_called()


def test_owner_token_cannot_use_app_screenshot_route(client, owner_token, db):
  _, _, _, output_chat_id = _make_app(client, owner_token, db, allowed=True)
  response = client.post(
    f"/api/app-chats/{output_chat_id}/owner-screenshot",
    json={"warm_only": False},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 403


def _write_test_png(path):
  path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1024)


def test_warm_owner_profile_skips_capture_while_fresh(tmp_path):
  from app.routes import chats

  profile = tmp_path / "agent-browser-profiles" / "app-owner-7"
  marker = tmp_path / "agent-browser-profiles" / "app-owner-7.ready"
  marker.parent.mkdir(parents=True)
  marker.write_text("target-chat")
  with (
    patch.object(chats, "get_settings", return_value=SimpleNamespace(data_dir=tmp_path)),
    patch.object(chats, "_run_screenshot_command") as run,
  ):
    result = chats._capture_owner_shell(
      app_id=7,
      target_chat_id="target-chat",
      output_chat_id="output-chat",
      owner_token="owner-token",
      warm_only=True,
    )
  assert result is None
  run.assert_not_called()
  assert not profile.exists()


def test_direct_owner_capture_uses_warm_browser_profile(tmp_path):
  from app.routes import chats

  marker = tmp_path / "agent-browser-profiles" / "app-owner-7.ready"
  marker.parent.mkdir(parents=True)
  marker.write_text("target-chat")

  def capture(command, **_kwargs):
    _write_test_png(tmp_path / "chats" / "output-chat" / "media" / command[-1].split("/")[-1])

  with (
    patch.object(chats, "get_settings", return_value=SimpleNamespace(data_dir=tmp_path)),
    patch.object(chats, "_run_screenshot_command", side_effect=capture) as run,
  ):
    filename = chats._capture_owner_shell(
      app_id=7,
      target_chat_id="target-chat",
      output_chat_id="output-chat",
      owner_token="owner-token",
      warm_only=False,
    )
  assert filename and filename.startswith("owner-shell-")
  assert run.call_args.args[0][:2] == ["agent-browser", "screenshot"]
  assert (tmp_path / "chats" / "output-chat" / "media" / filename).exists()
