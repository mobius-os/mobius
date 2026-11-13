"""Update resolver choices and filing are scoped to their authoritative jobs."""

import json

import pytest
from fastapi import HTTPException

from app import models, platform_update, providers, update_chat


def test_explicit_update_choice_is_frozen_without_background_fallback(
  db, monkeypatch,
):
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(
    "app.background_agents.resolve_background_chat_choice",
    lambda *args: (_ for _ in ()).throw(AssertionError("must not use fallback")),
  )
  choice = update_chat.resolve_update_chat_choice(
    db, update_chat.UpdateAgentChoice(
      provider="codex", model="gpt-5.5", effort="high",
    ),
  )
  assert choice["provider"] == "codex"
  assert choice["agent_settings"]["model"] == "gpt-5.5"
  assert choice["agent_settings"]["effort"] == "high"


def test_unavailable_explicit_update_provider_does_not_fallback(db, monkeypatch):
  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, data_dir: "unavailable",
  )
  with pytest.raises(HTTPException) as exc:
    update_chat.resolve_update_chat_choice(
      db, update_chat.UpdateAgentChoice(provider="codex", model="gpt-5.5"),
    )
  assert exc.value.status_code == 409


def test_completed_app_resolver_archives_without_deleting_history(db, chat):
  update_chat.archive_completed_app_resolver(db, chat.id)
  db.refresh(chat)
  assert chat.archived_at is not None
  assert chat.deleted_at is None


def test_platform_resolver_archives_only_exact_confirmed_target(
  db, chat, tmp_path, monkeypatch,
):
  repo = tmp_path / "platform"
  (repo / ".git").mkdir(parents=True)
  marker = platform_update._completed_resolver_path(repo)
  marker.write_text(json.dumps({"target": "target-a", "chat_id": chat.id}))
  monkeypatch.setattr(platform_update, "_platform_activation_impact",
                      lambda *args, **kwargs: {"required_actions": ["server_restart"]})
  platform_update._archive_confirmed_update_resolver(repo, "target-a")
  db.refresh(chat)
  assert chat.archived_at is None
  assert marker.exists()
  monkeypatch.setattr(platform_update, "_platform_activation_impact",
                      lambda *args, **kwargs: {"required_actions": []})

  platform_update._archive_confirmed_update_resolver(repo, "target-b")
  db.refresh(chat)
  assert chat.archived_at is None
  assert marker.exists()

  platform_update._archive_confirmed_update_resolver(repo, "target-a")
  db.refresh(chat)
  assert chat.archived_at is not None
  assert chat.deleted_at is None
  assert not marker.exists()


def test_owner_temporary_repair_chat_retry_keeps_original_agent_and_opt_in(
  client, auth, db, monkeypatch,
):
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(providers, "provider_selectable", lambda data_dir, provider: True)
  first = client.post("/api/chats", headers=auth, json={
    "title": "Repair update",
    "recovery_request_id": "same-update-repair",
    "agent": {"provider": "codex", "model": "gpt-5.5", "effort": "high"},
    "archive_on_goal_complete": True,
  })
  assert first.status_code == 200, first.text
  retry = client.post("/api/chats", headers=auth, json={
    "title": "Different retry payload",
    "recovery_request_id": "same-update-repair",
    "agent": {"provider": "codex", "model": "gpt-5.4", "effort": "low"},
  })
  assert retry.status_code == 200, retry.text
  assert retry.json()["id"] == first.json()["id"]
  db.expire_all()
  chat = db.get(models.Chat, first.json()["id"])
  assert chat.provider == "codex"
  assert chat.agent_settings_json["model"] == "gpt-5.5"
  assert chat.agent_settings_json["effort"] == "high"
  assert chat.agent_settings_json["archive_on_goal_complete"] is True


def test_app_temporary_repair_chat_saves_opt_in(client, owner_token, db):
  from test_app_fixtures import create_local_app
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="repair-owner")["id"]
  token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=owner_auth,
  ).json()["token"]
  response = client.post("/api/app-chats", headers={
    "Authorization": f"Bearer {token}",
  }, json={
    "title": "Review failed update", "provider": "claude",
    "model": "claude-opus-4-8", "effort": "high",
    "owner_visible": True, "archive_on_goal_complete": True,
  })
  assert response.status_code == 201, response.text
  db.expire_all()
  chat = db.get(models.Chat, response.json()["id"])
  assert chat.created_by_app_id == app_id
  assert chat.agent_settings_json["archive_on_goal_complete"] is True
  assert chat.agent_settings_json["model"] == "claude-opus-4-8"
