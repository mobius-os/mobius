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
  from app import drawer_pins
  with drawer_pins.serialized_write():
    assert update_chat.archive_completed_app_resolver(db, chat.id)
    db.commit()
  db.refresh(chat)
  assert chat.archived_at is not None
  assert chat.deleted_at is None


@pytest.mark.asyncio
async def test_pending_update_filing_commits_binding_and_archive_together(
  db, chat, tmp_path, monkeypatch,
):
  from app import pending_update
  repo = tmp_path / "app"
  (repo / ".git").mkdir(parents=True)
  app = models.App(
    name="Update target", slug="update-target", source_dir=str(repo),
    conflict_resolver_chat_id=chat.id,
    conflict_resolver_upstream_commit="target", upstream_commit="target",
  )
  db.add(app)
  db.commit()
  chat.agent_settings_json = update_chat.resolver_batch_settings({}, [app])
  db.commit()
  monkeypatch.setattr(pending_update, "_drop_pending_update_worktree", lambda _: None)

  await pending_update.clear_pending_conflict_update(db, app, completed=True)

  db.expire_all()
  assert db.get(models.App, app.id).conflict_resolver_chat_id is None
  filed = db.get(models.Chat, chat.id)
  assert filed.archived_at is not None
  assert filed.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes", [(False, True), (True, False), (True, True)])
async def test_resolver_batch_archives_only_after_every_member_succeeds(
  db, chat, tmp_path, monkeypatch, outcomes,
):
  from app import pending_update
  apps = []
  for index in range(2):
    repo = tmp_path / f"app-{index}"
    (repo / ".git").mkdir(parents=True)
    app = models.App(
      name=f"Update target {index}", slug=f"update-target-{index}",
      source_dir=str(repo), conflict_resolver_chat_id=chat.id,
      conflict_resolver_upstream_commit=f"target-{index}",
      upstream_commit=f"target-{index}",
    )
    db.add(app)
    apps.append(app)
  db.flush()
  chat.agent_settings_json = update_chat.resolver_batch_settings({}, apps)
  db.commit()
  monkeypatch.setattr(pending_update, "_drop_pending_update_worktree", lambda _: None)

  for app, completed in zip(apps, outcomes):
    await pending_update.clear_pending_conflict_update(db, app, completed=completed)
    # Simulate a fresh ORM read/retry between member completions.
    db.expire_all()
    if app is apps[0]:
      assert db.get(models.Chat, chat.id).archived_at is None

  assert (db.get(models.Chat, chat.id).archived_at is not None) == all(outcomes)
  batch = db.get(models.Chat, chat.id).agent_settings_json[update_chat._BATCH_KEY]
  assert [batch[str(app.id)]["completed"] for app in apps] == list(outcomes)


@pytest.mark.asyncio
async def test_stale_or_unrecorded_resolver_binding_cannot_certify_completion(
  db, chat, tmp_path, monkeypatch,
):
  from app import pending_update
  repo = tmp_path / "stale"
  (repo / ".git").mkdir(parents=True)
  app = models.App(
    name="Stale", slug="stale", source_dir=str(repo),
    conflict_resolver_chat_id=chat.id,
    conflict_resolver_upstream_commit="older", upstream_commit="newer",
  )
  db.add(app)
  db.flush()
  chat.agent_settings_json = update_chat.resolver_batch_settings({}, [app])
  db.commit()
  monkeypatch.setattr(pending_update, "_drop_pending_update_worktree", lambda _: None)
  await pending_update.clear_pending_conflict_update(db, app, completed=True)
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is None
  assert db.get(models.Chat, chat.id).agent_settings_json[
    update_chat._BATCH_KEY][str(app.id)]["completed"] is None


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
