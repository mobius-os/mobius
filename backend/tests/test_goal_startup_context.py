"""First-turn startup context stays out of CLI slash-command arguments."""

import asyncio

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
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(
      role="user", content="/goal keep the objective clean",
    )],
    chat_id=chat_id,
    session_id=None,
    provider_id="claude",
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
