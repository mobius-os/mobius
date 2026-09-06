"""Owner-consented, exact-chat live screen relay contracts."""

import asyncio

import pytest
from pydantic import ValidationError

from app import auth as auth_module
from app.routes.screen_control import AgentCommandBody
from app.screen_control import ScreenControlRegistry
from test_app_fixtures import create_local_app


def _agent_headers(chat_id: str) -> dict[str, str]:
  token = auth_module.create_agent_token(chat_id, "test", 0)
  return {"Authorization": f"Bearer {token}"}


def test_owner_can_start_only_for_the_invoking_apps_chat(client, auth, chat, db):
  app = create_local_app(client, auth, name="Screen Control Test")
  chat.created_by_app_id = app["id"]
  db.commit()
  started = client.post(
    "/api/screen-control/sessions",
    headers=auth,
    json={
      "appId": app["id"],
      "chatId": chat.id,
      "route": f"/chat/{chat.id}",
      "viewport": {"width": 1170, "height": 2532, "pixelRatio": 2.625},
    },
  )
  assert started.status_code == 200, started.text
  assert started.json()["active"] is True
  assert started.json()["appId"] == app["id"]

  # A broad browser login is the wrong role at the chat-side boundary.
  owner_status = client.get(f"/api/screen-control/chats/{chat.id}", headers=auth)
  assert owner_status.status_code == 403

  agent = _agent_headers(chat.id)
  status = client.get(f"/api/screen-control/chats/{chat.id}", headers=agent)
  assert status.status_code == 200, status.text
  assert status.json()["sessionId"] == started.json()["sessionId"]

  wrong_chat = client.get("/api/screen-control/chats/another-chat", headers=agent)
  assert wrong_chat.status_code == 403

  stopped = client.delete(f"/api/screen-control/chats/{chat.id}", headers=agent)
  assert stopped.status_code == 204, stopped.text
  assert client.get(f"/api/screen-control/chats/{chat.id}", headers=agent).json() == {
    "active": False,
  }


def test_owner_cannot_bind_control_to_an_unattributed_or_foreign_app_chat(
  client, auth, chat, db,
):
  app = create_local_app(client, auth, name="Owning Control App")
  other = create_local_app(client, auth, name="Other Control App")

  unattributed = client.post(
    "/api/screen-control/sessions",
    headers=auth,
    json={"appId": app["id"], "chatId": chat.id},
  )
  assert unattributed.status_code == 404

  chat.created_by_app_id = other["id"]
  db.commit()
  foreign = client.post(
    "/api/screen-control/sessions",
    headers=auth,
    json={"appId": app["id"], "chatId": chat.id},
  )
  assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_relay_returns_only_the_browser_answer_to_the_waiting_command():
  registry = ScreenControlRegistry()
  session = await registry.start(
    owner_username="owner",
    app_id=7,
    chat_id="chat-a",
    route="/chat/chat-a",
    viewport={"width": 1280, "height": 720, "pixelRatio": 1},
  )
  assert await registry.connect_browser(session.id, "owner") is session

  waiting = asyncio.create_task(registry.issue_command(session, {
    "action": "click", "ref": "e3",
  }))
  command = await asyncio.wait_for(session.commands.get(), timeout=1)
  assert command["action"] == "click"
  assert command["ref"] == "e3"

  accepted = await registry.answer(session, command["commandId"], {
    "ok": True, "result": {"clicked": "e3"}, "error": None,
  })
  assert accepted is True
  assert await waiting == {
    "ok": True, "result": {"clicked": "e3"}, "error": None,
  }
  assert await registry.answer(session, command["commandId"], {"ok": True}) is False
  await registry.stop(session)


@pytest.mark.asyncio
async def test_last_browser_disconnect_retires_the_unusable_session():
  registry = ScreenControlRegistry()
  session = await registry.start(
    owner_username="owner",
    app_id=7,
    chat_id="chat-a",
    route="/chat/chat-a",
    viewport={"width": 1280, "height": 720, "pixelRatio": 1},
  )
  await registry.connect_browser(session.id, "owner")
  await registry.disconnect_browser(session.id)

  assert session.active is False
  assert await registry.get_for_chat("chat-a", "owner") is None


@pytest.mark.parametrize("payload", [
  {"action": "snapshot", "ref": "e1"},
  {"action": "click"},
  {"action": "click", "x": 10},
  {"action": "type"},
  {"action": "scroll"},
  {"action": "press", "key": "F12"},
  {"action": "evaluate", "text": "document.cookie"},
])
def test_command_vocabulary_rejects_ambiguous_or_arbitrary_browser_actions(payload):
  with pytest.raises(ValidationError):
    AgentCommandBody.model_validate(payload)


@pytest.mark.parametrize("payload", [
  {"action": "snapshot"},
  {"action": "screenshot"},
  {"action": "click", "ref": "app:42:e3"},
  {"action": "click", "x": 10, "y": 20},
  {"action": "type", "ref": "e7", "text": "hello", "replace": True},
  {"action": "scroll", "deltaY": 560},
  {"action": "press", "key": "Enter"},
])
def test_command_vocabulary_accepts_the_reviewed_semantic_actions(payload):
  assert AgentCommandBody.model_validate(payload).action == payload["action"]
