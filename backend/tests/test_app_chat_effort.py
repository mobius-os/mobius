"""Explicit app chat effort uses the normal per-chat setting, never a global default."""
import pytest

from app import models
from app.routes.chats import AppChatStart
from test_app_fixtures import create_local_app


def _make_app(client, owner_token, name):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name=name)["id"]
  token = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth).json()["token"]
  return app_id, token


def test_app_chat_effort_create_patch_list_and_isolation(client, owner_token, db):
  app_id, token = _make_app(client, owner_token, "effort-owner")
  auth = {"Authorization": f"Bearer {token}"}
  created = client.post("/api/app-chats", headers=auth, json={
    "provider": "codex", "model": "gpt-6-astra", "effort": "xhigh",
    "scope": "assistant", "system_prompt": "App bootstrap",
  })
  assert created.status_code == 201, created.text
  cid = created.json()["id"]
  row = db.get(models.Chat, cid)
  assert row.agent_settings_json["effort"] == "xhigh"
  assert row.agent_settings_json["model"] == "gpt-6-astra"
  listed = client.get("/api/app-chats?scope=assistant", headers=auth).json()
  assert listed[0]["id"] == cid
  assert listed[0]["effort"] == "xhigh"
  assert listed[0]["model"] == "gpt-6-astra"
  # An ordinary metadata update must not clear explicit effort.
  assert client.patch(f"/api/app-chats/{cid}", headers=auth, json={"scope_label": "Assistant"}).status_code == 200
  db.refresh(row)
  assert row.agent_settings_json["effort"] == "xhigh"
  changed = client.patch(f"/api/app-chats/{cid}", headers=auth, json={"effort": "high"})
  assert changed.status_code == 200, changed.text
  assert changed.json()["agent_settings_json"]["effort"] == "high"
  assert changed.json()["agent_settings_json"]["system_prompt"] == "App bootstrap"
  other = client.post("/api/app-chats", headers=auth, json={"provider": "codex", "model": "gpt-6-astra"})
  assert other.status_code == 201
  assert "effort" not in db.get(models.Chat, other.json()["id"]).agent_settings_json
  # Effort support must not weaken app attribution.
  _, other_token = _make_app(client, owner_token, "different-app")
  denied = client.patch(f"/api/app-chats/{cid}", headers={"Authorization": f"Bearer {other_token}"}, json={"effort": "low"})
  assert denied.status_code in (403, 404)
  db.refresh(row)
  assert row.agent_settings_json["effort"] == "high"


@pytest.mark.parametrize("effort", ["extra high", "bogus", 42, {}])
def test_app_chat_rejects_unknown_effort(client, owner_token, effort):
  _, token = _make_app(client, owner_token, "invalid-effort")
  auth = {"Authorization": f"Bearer {token}"}
  r = client.post("/api/app-chats", headers=auth, json={"effort": effort})
  assert r.status_code == 422


def test_scoped_start_carries_the_same_effort_contract():
  value = AppChatStart(content="hello", scope="assistant", provider="codex", model="gpt-6-astra", effort="xhigh")
  assert value.model_dump()["effort"] == "xhigh"
