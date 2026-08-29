"""Owner MCP connections must follow the owner's own chats only."""

import asyncio

import pytest

from app import chat as chat_mod
from app import chat_queue, models, schemas
from app.broadcast import create_broadcast, remove_broadcast


def _app_row(db, marker):
  row = models.App(
    slug=f"grant-policy-{marker}",
    source_dir=f"/tmp/mobius-tests/grant-policy-{marker}",
    name="grant-policy app",
    description="test",
    jsx_source="export default () => null",
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


async def _drive_turn(chat_id, monkeypatch, *, expected_include):
  """Run one codex turn with fakes; return the connector plans the runner saw."""
  observed = []
  granted_plan = object()

  def fake_connector_plan(_db, *, include_owner_connectors):
    observed.append(include_owner_connectors)
    return granted_plan if include_owner_connectors else None

  monkeypatch.setattr("app.connectors.build_turn_plan", fake_connector_plan)

  plans = []

  async def fake_runner(**kwargs):
    plans.append(kwargs["connector_plan"])
    return {"session_id": None, "cost_usd": 0.0, "error": None}

  async def fake_complete(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  monkeypatch.setattr("app.codex_sdk_runner.run_codex_sdk_turn", fake_runner)
  monkeypatch.setattr(
    "app.providers.CodexProvider.check_auth", lambda self, _data_dir: None,
  )
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete)

  create_broadcast(chat_id)
  try:
    await asyncio.wait_for(chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat_id,
      session_id="existing-session",
      provider_id="codex",
      run_gen=chat_mod.current_run_generation(chat_id),
    ), timeout=5)
  finally:
    remove_broadcast(chat_id)
  assert observed == [expected_include]
  return plans, granted_plan


@pytest.mark.asyncio
async def test_app_attributed_chat_does_not_inherit_owner_connections(
  chat, db, monkeypatch,
):
  app_row = _app_row(db, "attributed")
  chat.provider = "codex"
  chat.agent_settings_json = {"model": "gpt-5.4"}
  chat.created_by_app_id = app_row.id
  db.commit()

  plans, _granted = await _drive_turn(chat.id, monkeypatch, expected_include=False)
  assert plans == [None]


@pytest.mark.asyncio
async def test_owner_chat_keeps_owner_connections(chat, db, monkeypatch):
  chat.provider = "codex"
  chat.agent_settings_json = {"model": "gpt-5.4"}
  db.commit()

  plans, granted = await _drive_turn(chat.id, monkeypatch, expected_include=True)
  assert plans == [granted]


def test_delegated_prompt_states_connections_unavailable():
  from app.delegations import RunPolicy

  policy = RunPolicy(
    delegation_id="delegation-1",
    app_id=2,
    provider="codex",
    model=None,
    effort=None,
    scope="read",
    cwd="/data",
  )
  assert (
    "Owner-managed MCP connections are not available" in policy.system_prompt
  )


def test_app_chat_context_states_connections_unavailable(db):
  app_row = _app_row(db, "context")
  row = models.Chat(
    id="grant-policy-context-chat",
    title="app chat",
    messages=[],
    created_by_app_id=app_row.id,
  )
  db.add(row)
  db.commit()

  from app.chat_context import _build_app_context

  block, _env = _build_app_context(
    db, "grant-policy-context-chat", "/tmp/mobius-tests",
  )
  assert "Owner-managed MCP connections are not available" in block
