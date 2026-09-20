"""First-turn startup context stays out of CLI slash-command arguments."""

from tests.goal_fixtures import goal_run as make_goal_run

import asyncio
import pytest

from app import chat as chat_mod, memory, models, schemas
from app.broadcast import create_broadcast


def test_goal_receives_startup_context_only_through_system_prompt(
  client, auth, db, monkeypatch,
):
  chat_id = client.post(
    "/api/chats", json={"title": "Goal prompt routing"}, headers=auth,
  ).json()["id"]
  digest = "RECENT_CHAT_DIGEST_SENTINEL"
  skills = "<available_skills>SKILL_SENTINEL</available_skills>"

  monkeypatch.setattr(
    chat_mod.memory,
    "build_memory_block",
    lambda *_args, **_kwargs: memory.MemoryBlock(text=digest),
  )
  monkeypatch.setattr(
    chat_mod, "_build_available_skills_block", lambda _data_dir: skills,
  )
  monkeypatch.setattr(
    "app.providers.ClaudeProvider.check_auth",
    lambda self, _data_dir: None,
  )
  monkeypatch.setattr(
    "app.providers.ClaudeProvider.ensure_auth",
    lambda self, _data_dir: asyncio.sleep(0),
  )
  captured = {}

  async def _runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "goal-session", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(
    "app.claude_sdk_runner.run_claude_sdk_turn", _runner,
  )
  # Direct runner entry still requires the durable admission record normally
  # created by the send lifecycle; do not bypass the duplicate-execution gate.
  run_token = "goal-prompt-routing-run"
  db.add(make_goal_run(db,
    id=run_token, root_run_id=run_token, chat_id=chat_id,
    status="running", provider="claude", provider_execution_admitted=False,
  ))
  db.commit()
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(
      role="user", content="/goal keep the objective clean",
    )],
    chat_id=chat_id,
    session_id=None,
    provider_id="claude",
    run_token=run_token,
    run_gen=chat_mod.current_run_generation(chat_id),
  ))

  assert captured["user_message"].startswith(
    "/goal keep the objective clean"
  )
  assert digest not in captured["user_message"]
  assert skills not in captured["user_message"]
  assert digest in captured["skill_text"]
  assert skills in captured["skill_text"]

  db.expire_all()
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  snapshot = db.get(
    models.SystemPromptSnapshot, chat.system_prompt_snapshot_id,
  )
  assert snapshot is not None
  assert digest not in snapshot.content
  assert skills not in snapshot.content



@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("session_id", [None, "existing-provider-session"])
def test_each_provider_receives_durable_goal_on_fresh_or_resumed_session(
  client, auth, db, monkeypatch, provider, session_id,
):
  """Exercise real chat prompt assembly; only provider execution is replaced."""
  chat_id = client.post(
    "/api/chats", json={"title": "Durable context parity"}, headers=auth,
  ).json()["id"]
  provider_class = "ClaudeProvider" if provider == "claude" else "CodexProvider"
  monkeypatch.setattr(f"app.providers.{provider_class}.check_auth", lambda *a: None)
  monkeypatch.setattr(f"app.providers.{provider_class}.ensure_auth", lambda *a: asyncio.sleep(0))
  captured = {}

  async def runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "returned-session", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(f"app.{provider}_sdk_runner.run_{provider}_sdk_turn", runner)
  run_id = "parity-attempt"
  db.add(make_goal_run(db, id=run_id, root_run_id=run_id, chat_id=chat_id,
    status="running", provider=provider, provider_execution_admitted=False,
    goal_id="original-obligation", goal_objective="ORIGINAL_OUTCOME_SENTINEL",
    goal_plan_json={"tasks":[
      {"id":"deployment", "title":"UNFINISHED_DEPLOYMENT", "status":"running", "depends_on":[]},
      {"id":"identity", "title":"UNFINISHED_IDENTITY", "status":"pending", "depends_on":[]},
      {"id":"hidden", "parent_id":"identity", "title":"UNRELATED_DESCENDANT", "status":"pending", "depends_on":[]},
    ]}))
  db.flush()
  goal = db.get(models.ChatGoal, "original-obligation")
  goal.checkpoint = "VERIFIED_PROGRESS_SENTINEL"
  goal.next_action = "NEXT_ACTION_SENTINEL"
  db.commit()
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="continue")],
    chat_id=chat_id, session_id=session_id, provider_id=provider,
    run_token=run_id, run_gen=chat_mod.current_run_generation(chat_id),
  ))
  prompt = captured["user_message"]
  for expected in ["ORIGINAL_OUTCOME_SENTINEL", "UNFINISHED_DEPLOYMENT",
                   "UNFINISHED_IDENTITY", "VERIFIED_PROGRESS_SENTINEL",
                   "NEXT_ACTION_SENTINEL", "original-obligation"]:
    assert expected in prompt
  assert prompt.count("<mobius_goal>") == 1
  assert prompt.count("ORIGINAL_OUTCOME_SENTINEL") == 1
  assert "UNRELATED_DESCENDANT" not in prompt
  assert "automatic_turns_remaining" not in prompt
  assert "resume_reason" not in prompt
