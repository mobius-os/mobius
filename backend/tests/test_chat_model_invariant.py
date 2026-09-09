"""One persisted-model invariant across chat creation and unattended starts."""

import ast
import json
import os
from pathlib import Path

import pytest

from app import chat_start, models, providers
from app.chat_start import (
  ProgrammaticChatModelRequired,
  require_programmatic_chat_model,
  start_programmatic_chat_turn,
)


def _write_settings(payload: dict) -> None:
  path = Path(os.environ["DATA_DIR"]) / "shared" / "agent-settings.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(payload), encoding="utf-8")


def test_only_first_install_chat_may_be_created_without_a_model(client, auth):
  _write_settings({})

  first = client.post("/api/chats", headers=auth, json={"title": "First"})
  second = client.post("/api/chats", headers=auth, json={"title": "Second"})

  assert first.status_code == 200, first.text
  first_detail = client.get(f"/api/chats/{first.json()['id']}", headers=auth).json()
  second_detail = client.get(f"/api/chats/{second.json()['id']}", headers=auth).json()
  assert first_detail["agent_settings_json"] is None
  assert second.status_code == 200, second.text
  settings = second_detail["agent_settings_json"]
  assert isinstance(settings.get("model"), str) and settings["model"]
  assert not providers._model_belongs_to_other_provider(
    settings["model"], second_detail["provider"],
  )


def test_new_chat_persists_the_latest_picker_choice(client, auth):
  _write_settings({
    "model": "gpt-5.6-sol",
    "provider": "codex",
    "effort": "xhigh",
    "effort_by_provider": {"codex": "xhigh"},
  })

  created = client.post("/api/chats", headers=auth, json={"title": "Pinned"})

  assert created.status_code == 200, created.text
  body = client.get(
    f"/api/chats/{created.json()['id']}", headers=auth,
  ).json()
  assert body["provider"] == "codex"
  assert body["agent_settings_json"] == {
    "model": "gpt-5.6-sol",
    "effort": "xhigh",
    "effort_by_provider": {"codex": "xhigh"},
  }


def test_programmatic_start_requires_a_persisted_compatible_model(db):
  chat = models.Chat(
    id="model-less-programmatic",
    title="Broken background chat",
    messages=[],
    provider="claude",
    agent_settings_json={"drawer_hidden": True},
  )
  db.add(chat)
  db.commit()

  with pytest.raises(ProgrammaticChatModelRequired, match="no explicitly selected"):
    require_programmatic_chat_model(chat.id, "claude")

  chat.agent_settings_json = {
    "drawer_hidden": True,
    "model": "claude-opus-4-8",
  }
  db.commit()
  assert require_programmatic_chat_model(chat.id, "claude") == "claude-opus-4-8"
  with pytest.raises(ProgrammaticChatModelRequired, match="does not match"):
    require_programmatic_chat_model(chat.id, "codex")


@pytest.mark.asyncio
async def test_programmatic_start_rejects_before_claiming_a_model_less_chat(
  db, monkeypatch,
):
  chat = models.Chat(
    id="model-less-start",
    title="Broken background chat",
    messages=[],
    provider="claude",
    agent_settings_json={"drawer_hidden": True},
  )
  db.add(chat)
  db.commit()
  monkeypatch.setattr(
    chat_start,
    "mark_starting",
    lambda _chat_id: pytest.fail("model-less chat reached the transient claim"),
  )

  with pytest.raises(ProgrammaticChatModelRequired, match="no explicitly selected"):
    await start_programmatic_chat_turn(
      chat_id=chat.id,
      title=chat.title,
      content="unattended work",
      provider="claude",
    )


def test_every_production_chat_constructor_supplies_agent_settings():
  """A new creator must make the model decision visible at its call site."""
  app_root = Path(__file__).resolve().parents[1] / "app"
  missing = []
  for path in app_root.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
      if not isinstance(node, ast.Call):
        continue
      func = node.func
      if not (
        isinstance(func, ast.Attribute)
        and func.attr == "Chat"
        and isinstance(func.value, ast.Name)
        and func.value.id == "models"
      ):
        continue
      keywords = {keyword.arg: keyword.value for keyword in node.keywords}
      if "agent_settings_json" not in keywords:
        missing.append(f"{path.relative_to(app_root.parent)}:{node.lineno}")
      elif isinstance(keywords["agent_settings_json"], ast.Constant) and (
        keywords["agent_settings_json"].value is None
      ):
        missing.append(f"{path.relative_to(app_root.parent)}:{node.lineno}=None")
  assert missing == []


def test_programmatic_start_callers_share_one_model_guard():
  """Every unattended start must keep routing through chat_start's guard."""
  app_root = Path(__file__).resolve().parents[1] / "app"
  callers = set()

  class StartCallerVisitor(ast.NodeVisitor):
    def __init__(self, path: Path):
      self.path = path
      self.function = None

    def _visit_function(self, node):
      previous = self.function
      self.function = node.name
      self.generic_visit(node)
      self.function = previous

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node):
      if (
        isinstance(node.func, ast.Name)
        and node.func.id == "start_programmatic_chat_turn"
      ):
        callers.add(
          f"{self.path.relative_to(app_root.parent)}:{self.function}"
        )
      self.generic_visit(node)

  for path in app_root.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    StartCallerVisitor(path).visit(tree)
  assert callers == {
    "app/agent_coordination.py:_wake_idle_recipient",
    "app/contribution_autopilot.py:spawn_round_turn",
    "app/platform_update.py:spawn_platform_conflict_chat",
    "app/routes/apps.py:_start_conflict_resolver_turn",
    "app/routes/contribution_reviews.py:start_reviews",
  }
