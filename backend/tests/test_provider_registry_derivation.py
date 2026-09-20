"""Provider registration owns validation, model inference, and agent identity."""

import hashlib
from typing import get_args

import pytest
from pydantic import ValidationError

from app import models, providers
from app.agent_lifecycle import stable_agent_id
from app.events import process_event
from app.routes.delegations import DelegationSubmit
from app.schemas import AgentEffort, ChatPatch, ChatProviderSwitch


@pytest.fixture
def registered_provider(monkeypatch):
  class RegisteredProvider(providers.CodexProvider):
    name = "Registered test provider"
    switch_efforts = frozenset({"medium", "high"})

    def check_auth(self, data_dir):
      return None

  instance = RegisteredProvider()
  monkeypatch.setitem(providers.PROVIDERS, "registered", instance)
  monkeypatch.setitem(providers.KNOWN_MODELS, "registered", ["registered-model"])
  return instance


def _switch(provider, model="future-model", effort="medium"):
  return ChatProviderSwitch(
    provider=provider,
    agent_settings_json={"model": model, "effort": effort},
    switch_id="switch-1",
  )


def _delegation(provider):
  return DelegationSubmit(
    app_id=1, parent_chat_id="chat", task_key="task.one",
    prompt="Review the source", provider=provider, scope="read",
  )


@pytest.mark.parametrize("provider", sorted(providers.PROVIDERS))
def test_existing_providers_remain_valid(provider):
  assert _switch(provider).provider == provider
  assert _delegation(provider).provider == provider


@pytest.mark.parametrize("provider,allowed", [
  ("claude", {"low", "medium", "high", "xhigh", "max", "ultracode"}),
  ("codex", {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
  ("mobius", {"minimal", "low", "medium", "high", "max"}),
])
def test_existing_switch_effort_contract_is_unchanged(provider, allowed):
  for effort in get_args(AgentEffort):
    if effort in allowed:
      assert _switch(provider, effort=effort).agent_settings_json.effort == effort
    else:
      with pytest.raises(ValidationError, match="target effort"):
        _switch(provider, effort=effort)


def test_new_registration_needs_no_validation_or_event_allowlist_edit(
  registered_provider,
):
  assert ChatPatch(provider="registered").provider == "registered"
  assert _switch("registered", "registered-model").provider == "registered"
  assert _delegation("registered").provider == "registered"
  blocks = []
  assert process_event({"type": "context_compacted", "provider": "registered"}, blocks)
  assert blocks == [{"type": "context_compaction", "provider": "registered"}]
  with pytest.raises(ValidationError, match="target effort"):
    _switch("registered", "registered-model", "ultra")
  with pytest.raises(ValidationError, match="target model"):
    _switch("registered", "gpt-5.4")


@pytest.mark.parametrize("provider", ["unknown", "local", "", None, [], {}])
def test_unregistered_or_malformed_provider_is_rejected_without_event_crash(provider):
  with pytest.raises(ValidationError):
    _switch(provider)
  with pytest.raises(ValidationError):
    _delegation(provider)
  blocks = []
  assert process_event({"type": "context_compacted", "provider": provider}, blocks)
  assert blocks == [{"type": "context_compaction"}]


@pytest.mark.parametrize("provider", ["codex", "mobius", "claude", "unknown"])
def test_existing_agent_identity_bytes_are_unchanged(provider):
  material = (
    f"{provider}\0agent" if provider in {"codex", "mobius"}
    else f"{provider}\0session\0agent"
  )
  expected = "agent-" + hashlib.sha256(material.encode()).hexdigest()
  assert stable_agent_id(provider, "session", "agent") == expected


def test_new_provider_uses_runtime_identity_scope(registered_provider):
  assert stable_agent_id("registered", "one", "child") == stable_agent_id(
    "registered", "two", "child",
  )
  assert stable_agent_id("registered", "one", "child") != stable_agent_id(
    "codex", "one", "child",
  )
  registered_provider.runtime_kind = "claude_sdk"
  assert stable_agent_id("registered", "one", "child") != stable_agent_id(
    "registered", "two", "child",
  )


@pytest.mark.parametrize("target,model", [("mobius", "spark"), ("registered", "registered-model")])
def test_model_only_patch_selects_its_provider_not_a_two_provider_flip(
  client, auth, chat, db, monkeypatch, registered_provider, target, model,
):
  monkeypatch.setattr(providers.MobiusProvider, "check_auth", lambda self, _: None)
  chat.session_id = "original-session"
  db.commit()
  response = client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"agent_settings_json": {"model": model}},
  )
  assert response.status_code == 200, response.text
  assert response.json()["provider"] == target
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert row.provider == target
  assert row.session_id is None
  assert row.agent_settings_json["model"] == model


def test_model_inference_keeps_auth_failure_atomic(
  client, auth, chat, db, monkeypatch, registered_provider,
):
  monkeypatch.setattr(registered_provider, "check_auth", lambda _: "Disconnected")
  chat.session_id = "original-session"
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.commit()
  response = client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"agent_settings_json": {"model": "registered-model"}},
  )
  assert response.status_code == 409
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert row.provider == "claude"
  assert row.session_id == "original-session"
  assert row.agent_settings_json == {"model": "claude-sonnet-4-6"}


def test_model_inference_cannot_bypass_populated_chat_handoff(
  client, auth, chat, db, registered_provider,
):
  response = client.put(
    f"/api/chats/{chat.id}", headers=auth,
    json={"messages": [
      {"role": "user", "content": "Hello"},
      {"role": "assistant", "content": "Hello back"},
    ]},
  )
  assert response.status_code == 200, response.text
  response = client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"agent_settings_json": {"model": "registered-model"}},
  )
  assert response.status_code == 409
  assert "handoff" in response.json()["detail"]
  db.expire_all()
  assert db.get(models.Chat, chat.id).provider == "claude"
