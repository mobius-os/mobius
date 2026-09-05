"""Global stop coverage across runner kinds."""

import asyncio
import hashlib

from app import chat as chat_mod, models
from app.delegations import RunPolicy, delegation_execution_token
from app.runner_registry import RunnerKind, registry


class _Handle:
  def __init__(self, chat_id: str, kind: RunnerKind, called: dict[str, int]):
    self.chat_id = chat_id
    self.kind = kind
    self._called = called

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self._called[self.chat_id] = self._called.get(self.chat_id, 0) + 1
    return True


def test_global_stop_stops_all_registered_kinds():
  called: dict[str, int] = {}
  registry.register(_Handle("chat-proc", RunnerKind.SUBPROCESS, called))
  registry.register(_Handle("chat-claude", RunnerKind.CLAUDE_SDK, called))
  registry.register(_Handle("chat-codex", RunnerKind.CODEX_SDK, called))

  stopped, _ = asyncio.run(chat_mod.stop_chat(None))

  assert stopped is True
  assert called == {
    "chat-proc": 1,
    "chat-claude": 1,
    "chat-codex": 1,
  }
  assert registry.all_alive_chat_ids() == set()


def test_chat_stop_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    "/api/chat/stop",
    json={"chat_id": chat.id},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_delegated_execution_bearer_cannot_stop_child_parent_foreign_or_all(
  client, owner_token, db, monkeypatch,
):
  """A delegated task controls children through /delegations, never Stop."""
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_ids = {}
  for key in ("child", "parent", "foreign", "owner-control"):
    response = client.post(
      "/api/chats", json={"title": key}, headers=owner_auth,
    )
    assert response.status_code == 200, response.text
    chat_ids[key] = response.json()["id"]

  app = models.App(
    name="Stop boundary", description="", slug="stop-boundary-app",
    source_dir="/tmp/mobius-tests/stop-boundary-app",
    jsx_source="export default () => null", token_nonce="stop-boundary-nonce",
  )
  db.add(app)
  db.flush()
  delegation_id = "stop-boundary-delegation"
  db.add(models.Delegation(
    id=delegation_id, app_id=app.id,
    parent_chat_id=chat_ids["parent"],
    parent_root_run_id="stop-boundary-parent-root",
    task_key="stop-boundary", child_chat_id=chat_ids["child"],
    provider="codex", model=None, effort=None, scope="write", cwd="/data",
    prompt_sha256=hashlib.sha256(b"check Stop boundary").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="stop-boundary-child-run", root_run_id="stop-boundary-child-run",
    chat_id=chat_ids["child"], status="running", provider="codex",
  ))
  db.commit()
  delegated_token = delegation_execution_token(db, RunPolicy(
    delegation_id=delegation_id, app_id=app.id, provider="codex",
    model=None, effort=None, scope="write", cwd="/data",
  ), run_id="stop-boundary-child-run")
  delegated_auth = {"Authorization": f"Bearer {delegated_token}"}

  calls = []

  async def record_stop(chat_id, *, db=None):
    del db
    calls.append(chat_id)
    return False, []

  monkeypatch.setattr("app.routes.chat.stop_chat", record_stop)
  responses = [
    client.post(
      "/api/chat/stop", json={"chat_id": chat_ids[target]},
      headers=delegated_auth,
    )
    for target in ("child", "parent", "foreign")
  ]
  responses.append(client.post(
    "/api/chat/stop", json={"chat_id": ""}, headers=delegated_auth,
  ))

  assert [response.status_code for response in responses] == [403] * 4
  assert calls == []

  owner_specific = client.post(
    "/api/chat/stop", json={"chat_id": chat_ids["owner-control"]},
    headers=owner_auth,
  )
  owner_global = client.post(
    "/api/chat/stop", json={"chat_id": ""}, headers=owner_auth,
  )
  assert owner_specific.status_code == 200, owner_specific.text
  assert owner_global.status_code == 200, owner_global.text
  assert calls == [chat_ids["owner-control"], None]
