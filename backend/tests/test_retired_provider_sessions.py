"""A retired provider session is never resumed; the chat continues fresh.

A session's own history can keep teaching the model an instruction Möbius has
withdrawn. The quiet-write frame is the case that motivated this: chats that
resumed sessions which had received it kept emitting frames nothing reads,
which showed up as raw text and silently lost the save.
"""

import pytest
from sqlalchemy import create_engine, text

from app import chat as chat_mod
from app import chat_queue, models, schemas
from app import schema_migrations as migrations
from app.broadcast import create_broadcast, remove_broadcast
from app.chat_writer import StartTurn, get_writer
from app.database import SessionLocal
from app.session_links import record_session_link, resume_retired
from app.timeutil import now_naive_utc


class _Provider:
  def __init__(self, name: str):
    self.name = name

  def check_auth(self, _data_dir: str):
    return None

  async def ensure_auth(self, _data_dir: str):
    return None

  def build_env(self, **_kwargs):
    return {}


async def _run_turn(monkeypatch, provider_id: str, provider_name: str, *, retired: bool):
  chat_id = f"retired-{provider_id}-{retired}"
  with SessionLocal() as setup:
    setup.add(models.Owner(username="owner", hashed_password="unused", provider=provider_id))
    setup.add(models.Chat(
      id=chat_id, title="retired session", provider=provider_id,
      session_id="old-session",
      messages=[
        {"role": "user", "content": "Fix the broker polling.", "ts": 1},
        {"role": "assistant", "content": "The broker fix is committed.", "ts": 2},
      ],
    ))
    setup.commit()
    record_session_link(setup, provider_id, "old-session", chat_id)
    if retired:
      setup.get(models.ChatSessionLink, (provider_id, "old-session")).resume_retired_at = now_naive_utc()
      setup.commit()

  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {"session_id": "new-session", "cost_usd": 0.0, "error": None}

  async def fake_complete_turn(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_record_run_metrics(**_kwargs):
    return None

  monkeypatch.setattr(chat_mod, "get_provider", lambda _id: _Provider(provider_name))
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete_turn)
  monkeypatch.setattr(chat_mod, "_record_run_metrics", fake_record_run_metrics)
  if provider_id == "codex":
    from app import codex_sdk_runner
    monkeypatch.setattr(codex_sdk_runner, "run_codex_sdk_turn", fake_runner)
  else:
    from app import claude_sdk_runner
    monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)
    # The transcript still exists: retirement, not loss, forces the fresh start.
    monkeypatch.setattr(claude_sdk_runner, "_resumable", lambda *_a, **_k: True)

  run_token = f"rt-{chat_id}"
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": "Carry on.", "ts": 3, "cid": f"m-{chat_id}"},
    title_source="Carry on.", default_provider=provider_id,
  )).result(timeout=5)
  create_broadcast(chat_id)
  try:
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="Carry on.")],
      chat_id=chat_id, session_id="old-session", provider_id=provider_id,
      run_token=run_token,
    )
  finally:
    remove_broadcast(chat_id)
  assert len(calls) == 1
  return calls[0]


PROVIDERS = (("codex", "Codex"), ("claude", "Claude Code"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "provider_name"), PROVIDERS)
async def test_a_retired_session_starts_fresh_with_the_chats_own_history(
  monkeypatch, provider_id, provider_name,
):
  call = await _run_turn(monkeypatch, provider_id, provider_name, retired=True)
  assert call["session_id"] is None
  assert "<resumed_context>" in call["user_message"]
  assert "The broker fix is committed." in call["user_message"]
  assert call["user_message"].rstrip().endswith("Carry on.")


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "provider_name"), PROVIDERS)
async def test_an_ordinary_session_still_resumes_natively(
  monkeypatch, provider_id, provider_name,
):
  call = await _run_turn(monkeypatch, provider_id, provider_name, retired=False)
  assert call["session_id"] == "old-session"
  assert "<resumed_context>" not in call["user_message"]


def test_resume_retired_is_false_for_unknown_or_missing_sessions(db):
  assert resume_retired(db, "claude", None) is False
  assert resume_retired(db, "claude", "never-seen") is False


def test_migration_retires_exactly_the_sessions_that_received_quiet_writes(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'retire.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY, provider_session_id VARCHAR(128))"))
    conn.execute(text("CREATE TABLE agent_write_streams (run_id VARCHAR(64) PRIMARY KEY)"))
    conn.execute(text(
      "CREATE TABLE chat_session_links (provider VARCHAR(32), session_id VARCHAR(128), "
      "chat_id VARCHAR(64), first_seen_at DATETIME, last_seen_at DATETIME, "
      "PRIMARY KEY (provider, session_id))"
    ))
    conn.execute(text(
      "INSERT INTO chat_runs VALUES ('run-quiet', 'tainted'), ('run-plain', 'clean'), "
      "('run-none', NULL)"
    ))
    conn.execute(text("INSERT INTO agent_write_streams VALUES ('run-quiet'), ('run-none')"))
    conn.execute(text(
      "INSERT INTO chat_session_links (provider, session_id, chat_id) VALUES "
      "('claude', 'tainted', 'a'), ('codex', 'clean', 'b')"
    ))

  migrations._retire_quiet_write_sessions(eng)
  migrations._retire_quiet_write_sessions(eng)
  migrations._drop_agent_write_journal(eng)
  migrations._retire_quiet_write_sessions(eng)

  with eng.connect() as conn:
    rows = dict(conn.execute(text(
      "SELECT session_id, resume_retired_at IS NOT NULL FROM chat_session_links"
    )).all())
  assert rows == {"tainted": 1, "clean": 0}


def test_migration_is_a_no_op_on_a_database_without_session_links(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
  migrations._retire_quiet_write_sessions(eng)
  with eng.connect() as conn:
    assert conn.execute(text("SELECT count(*) FROM sqlite_master")).scalar_one() == 0
