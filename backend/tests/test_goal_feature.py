import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_agent_sdk.types import ResultMessage, StreamEvent

from app import models, schemas
from app.broadcast import create_broadcast
from app.chat import (
  _is_cli_slash_command,
  _parse_goal_command,
  current_run_generation,
  run_chat,
)
from app.claude_sdk_runner import run_claude_sdk_turn


class _Bus:
  chat_id = "goal-chat"
  run_token = "rt-goal"

  def __init__(self):
    self.events = []

  def publish(self, event):
    self.events.append(event)
    return True


def _delta(text):
  return StreamEvent(
    uuid="evt",
    session_id="sess",
    event={
      "type": "content_block_delta",
      "delta": {"type": "text_delta", "text": text},
    },
  )


def _result():
  return ResultMessage(
    subtype="success",
    duration_ms=1,
    duration_api_ms=1,
    is_error=False,
    num_turns=1,
    session_id="sess",
    stop_reason="end_turn",
    total_cost_usd=0.0,
    usage={"input_tokens": 1, "output_tokens": 1},
  )


def test_goal_parser_never_marks_goal_as_sdk_slash_command():
  assert _parse_goal_command("/goal say PONG") == ("set", "say PONG")
  assert _parse_goal_command("\n/goal clear") == ("clear", "")
  assert _parse_goal_command("/goal") == ("status", "")
  assert _parse_goal_command("/data/apps/x is broken") is None
  assert _is_cli_slash_command("/goal say PONG") is False


def test_goal_set_strips_prefix_and_persists_goal(db, owner_token):
  chat = models.Chat(
    id="goal-set",
    title="Goal",
    provider="claude",
    messages=[{"role": "user", "content": "/goal write a haiku", "ts": 1}],
  )
  db.add(chat)
  db.commit()
  captured = {}

  async def fake_runner(**kwargs):
    captured.update(kwargs)
    kwargs["bc"].publish({"type": "text", "content": "started"})
    return {"session_id": "sess", "cost_usd": 0.0, "error": None}

  async def scenario():
    create_broadcast(chat.id)
    await run_chat(
      [schemas.ChatMessage(role="user", content="/goal write a haiku")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=current_run_generation(chat.id),
      run_token="rt-goal-set",
    )

  with patch("app.providers.ClaudeProvider.check_auth", return_value=None), \
       patch("app.claude_sdk_runner.run_claude_sdk_turn",
             side_effect=fake_runner):
    asyncio.run(scenario())

  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  goal = refreshed.agent_settings_json["goal"]
  assert goal["condition"] == "write a haiku"
  assert goal["turns"] == 0
  assert captured["user_message"].endswith("write a haiku")
  assert "/goal" not in captured["user_message"]
  assert captured["goal_mode"] is True


def test_goal_clear_and_status_skip_sdk(db, owner_token):
  chat = models.Chat(
    id="goal-clear",
    title="Goal",
    provider="claude",
    messages=[{"role": "user", "content": "/goal clear", "ts": 1}],
    agent_settings_json={
      "goal": {
        "condition": "finish it",
        "turns": 2,
        "started_at": "now",
        "last_reason": "not done",
      }
    },
  )
  db.add(chat)
  db.commit()
  called = False

  async def fake_runner(**_kwargs):
    nonlocal called
    called = True
    return {"session_id": "sess", "cost_usd": 0.0, "error": None}

  async def scenario():
    create_broadcast(chat.id)
    await run_chat(
      [schemas.ChatMessage(role="user", content="/goal clear")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=current_run_generation(chat.id),
      run_token="rt-goal-clear",
    )

  with patch("app.providers.ClaudeProvider.check_auth", return_value=None), \
       patch("app.claude_sdk_runner.run_claude_sdk_turn",
             side_effect=fake_runner):
    asyncio.run(scenario())

  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert called is False
  assert refreshed.agent_settings_json is None
  assert refreshed.messages[-1]["content"] == "Goal cleared."


def test_goal_status_reports_without_sdk(db, owner_token):
  chat = models.Chat(
    id="goal-status",
    title="Goal",
    provider="claude",
    messages=[{"role": "user", "content": "/goal", "ts": 1}],
    agent_settings_json={
      "goal": {
        "condition": "finish it",
        "turns": 3,
        "started_at": "now",
        "last_reason": "still missing",
      }
    },
  )
  db.add(chat)
  db.commit()
  called = False

  async def fake_runner(**_kwargs):
    nonlocal called
    called = True
    return {"session_id": "sess", "cost_usd": 0.0, "error": None}

  async def scenario():
    create_broadcast(chat.id)
    await run_chat(
      [schemas.ChatMessage(role="user", content="/goal")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=current_run_generation(chat.id),
      run_token="rt-goal-status",
    )

  with patch("app.providers.ClaudeProvider.check_auth", return_value=None), \
       patch("app.claude_sdk_runner.run_claude_sdk_turn",
             side_effect=fake_runner):
    asyncio.run(scenario())

  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert called is False
  assert refreshed.agent_settings_json["goal"]["turns"] == 3
  assert "State: active" in refreshed.messages[-1]["content"]
  assert "Goal: finish it" in refreshed.messages[-1]["content"]
  assert "Turns used: 3" in refreshed.messages[-1]["content"]
  assert "Last reason: still missing" in refreshed.messages[-1]["content"]
  assert "Elapsed:" in refreshed.messages[-1]["content"]
  assert "Token spend: 0" in refreshed.messages[-1]["content"]


@pytest.mark.asyncio
async def test_goal_met_clears_goal_and_ends(db, monkeypatch):
  goal = {
    "condition": "finish it",
    "turns": 0,
    "started_at": "now",
    "last_reason": None,
  }
  db.add(models.Chat(
    id="goal-chat",
    title="Goal",
    messages=[{"role": "user", "content": "finish it", "ts": 1}],
    agent_settings_json={"goal": goal},
  ))
  db.commit()

  class _Client:
    def __init__(self, _options):
      self.queries = []

    async def connect(self):
      return None

    async def query(self, message):
      self.queries.append(message)

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield _delta("done")
      yield _result()

  clients = []
  monkeypatch.setattr(
    "app.claude_sdk_runner.ClaudeSDKClient",
    lambda options: clients.append(_Client(options)) or clients[-1],
  )
  bus = _Bus()

  async def evaluator(_condition, _latest, _recent):
    return {"met": True, "reason": "done"}

  result = await run_claude_sdk_turn(
    "finish it",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="goal-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=db,
    goal_mode=True,
    goal=goal,
    goal_evaluator=evaluator,
  )

  db.expire_all()
  refreshed = (
    db.query(models.Chat).filter(models.Chat.id == "goal-chat").first()
  )
  assert result["error"] is None
  assert clients[0].queries == ["finish it"]
  assert refreshed.agent_settings_json is None
  assert any(e["type"] == "goal_met" for e in bus.events)


@pytest.mark.asyncio
async def test_goal_not_met_requeries_same_claude_session(db, monkeypatch):
  goal = {
    "condition": "finish it",
    "turns": 0,
    "started_at": "now",
    "last_reason": None,
  }
  db.add(models.Chat(
    id="goal-chat",
    title="Goal",
    messages=[{"role": "user", "content": "finish it", "ts": 1}],
    agent_settings_json={"goal": goal},
  ))
  db.commit()

  class _Client:
    def __init__(self, _options):
      self.queries = []

    async def connect(self):
      return None

    async def query(self, message):
      self.queries.append(message)

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

    async def receive_response(self):
      if len(self.queries) == 1:
        yield _delta("partial")
      else:
        yield _delta("done")
      yield _result()

  clients = []
  monkeypatch.setattr(
    "app.claude_sdk_runner.ClaudeSDKClient",
    lambda options: clients.append(_Client(options)) or clients[-1],
  )
  evaluations = [
    {"met": False, "reason": "missing proof"},
    {"met": True, "reason": "complete"},
  ]

  async def evaluator(_condition, _latest, _recent):
    return evaluations.pop(0)

  bus = _Bus()
  await run_claude_sdk_turn(
    "finish it",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="goal-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=db,
    goal_mode=True,
    goal=goal,
    goal_evaluator=evaluator,
  )

  assert clients[0].queries[0] == "finish it"
  assert clients[0].queries[1] == (
    "[goal] Not met: missing proof. Keep working toward: finish it"
  )
  assert any(e["type"] == "goal_continue" and e["turn"] == 1
             for e in bus.events)


@pytest.mark.asyncio
async def test_goal_safety_cap_pauses_without_clearing(db, monkeypatch):
  goal = {
    "condition": "finish it stop after 0",
    "turns": 0,
    "started_at": "now",
    "last_reason": None,
  }
  db.add(models.Chat(
    id="goal-chat",
    title="Goal",
    messages=[{"role": "user", "content": "finish it", "ts": 1}],
    agent_settings_json={"goal": goal},
  ))
  db.commit()

  class _Client:
    def __init__(self, _options):
      self.queries = []

    async def connect(self):
      return None

    async def query(self, message):
      self.queries.append(message)

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield _delta("not enough")
      yield _result()

  clients = []
  monkeypatch.setattr(
    "app.claude_sdk_runner.ClaudeSDKClient",
    lambda options: clients.append(_Client(options)) or clients[-1],
  )
  bus = _Bus()

  async def evaluator(_condition, _latest, _recent):
    return {"met": False, "reason": "still missing"}

  await run_claude_sdk_turn(
    "finish it",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="goal-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=db,
    goal_mode=True,
    goal=goal,
    goal_evaluator=evaluator,
  )

  db.expire_all()
  refreshed = (
    db.query(models.Chat).filter(models.Chat.id == "goal-chat").first()
  )
  assert clients[0].queries == ["finish it"]
  assert refreshed.agent_settings_json["goal"]["condition"] == goal["condition"]
  assert any(e["type"] == "goal_paused" for e in bus.events)
