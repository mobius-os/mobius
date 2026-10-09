"""A retired provider session is never resumed; the chat continues fresh.

A session's own history can keep teaching the model an instruction Möbius has
withdrawn. The quiet-write frame is the case that motivated this: chats that
resumed sessions which had received it kept emitting frames nothing reads,
which showed up as raw text and silently lost the save.
"""

from app.chat_writer import create_chat
import hashlib

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


async def _run_turn(
  monkeypatch, provider_id: str, provider_name: str, *, retired: bool,
  delegated: bool = False, session_id: str | None = "old-session",
):
  chat_id = f"retired-{provider_id}-{retired}-{delegated}"
  with SessionLocal() as setup:
    setup.add(models.Owner(username="owner", hashed_password="unused", provider=provider_id))
    setup.add(create_chat(
      id=chat_id, title="retired session", provider=provider_id,
      session_id=session_id,
      messages=[
        {"role": "user", "content": "Fix the broker polling.", "ts": 1},
        {"role": "assistant", "content": "The broker fix is committed.", "ts": 2},
      ],
    ))
    setup.commit()
    if session_id:
      record_session_link(setup, provider_id, session_id, chat_id)
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
  refusals = []
  if delegated:
    with SessionLocal() as setup:
      setup.add(create_chat(id=f"parent-{chat_id}", title="parent", provider=provider_id))
      setup.add(models.Delegation(
        id=f"d-{chat_id}", app_id=None, parent_chat_id=f"parent-{chat_id}",
        parent_root_run_id=f"parent-run-{chat_id}", task_key="retired",
        child_chat_id=chat_id, provider=provider_id, model=None, effort=None,
        scope="write", cwd="/tmp",
        prompt_sha256=hashlib.sha256(b"Fix the broker polling.").hexdigest(),
      ))
      setup.commit()

    async def fake_refusal(**_kwargs):
      refusals.append(chat_id)
      return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

    monkeypatch.setattr(chat_mod, "_refuse_delegated_write_replay", fake_refusal)
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
      messages=[
        schemas.ChatMessage(role="user", content="Fix the broker polling."),
        schemas.ChatMessage(role="assistant", content="The broker fix is committed."),
        schemas.ChatMessage(role="user", content="Carry on."),
      ] if delegated else [schemas.ChatMessage(role="user", content="Carry on.")],
      chat_id=chat_id, session_id=session_id, provider_id=provider_id,
      run_token=run_token,
    )
  finally:
    remove_broadcast(chat_id)
  if delegated:
    return calls, refusals
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
  # Its provider sees a first turn, so it also gets the first-turn context.
  assert "<agent_experience>" in (call.get("system_prompt") or call.get("skill_text") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "provider_name"), PROVIDERS)
async def test_an_ordinary_session_still_resumes_natively(
  monkeypatch, provider_id, provider_name,
):
  call = await _run_turn(monkeypatch, provider_id, provider_name, retired=False)
  assert call["session_id"] == "old-session"
  assert "<resumed_context>" not in call["user_message"]
  assert "<agent_experience>" not in (call.get("system_prompt") or call.get("skill_text") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "provider_name"), PROVIDERS)
@pytest.mark.parametrize("retired", [True, False])
async def test_a_retired_helper_is_refused_instead_of_resumed(
  monkeypatch, provider_id, provider_name, retired,
):
  """Retirement clears a helper's session pointer, so its follow-up gets the
  same no-replay refusal as a lost helper session; an ordinary helper resumes."""
  calls, refusals = await _run_turn(
    monkeypatch, provider_id, provider_name, retired=False, delegated=True,
    session_id=None if retired else "old-session",
  )
  assert refusals == ([f"retired-{provider_id}-False-True"] if retired else [])
  if retired:
    assert calls == []


def test_resume_retired_is_false_for_unknown_or_missing_sessions(db):
  assert resume_retired(db, None) is False
  assert resume_retired(db, "never-seen") is False


def _retirement_db(tmp_path, *, journal_applied_at):
  eng = create_engine(f"sqlite:///{tmp_path / 'retire.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at DATETIME)"))
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR(64) PRIMARY KEY, session_id VARCHAR(256), provider VARCHAR(32))"
    ))
    conn.execute(text(
      "CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64), "
      "started_at DATETIME, provider_session_id VARCHAR(256))"
    ))
    conn.execute(text("CREATE TABLE delegations (id VARCHAR(64) PRIMARY KEY, child_chat_id VARCHAR(64))"))
    conn.execute(text(
      "CREATE TABLE chat_session_links (provider VARCHAR(32), session_id VARCHAR(128), "
      "chat_id VARCHAR(64), first_seen_at DATETIME, last_seen_at DATETIME, "
      "PRIMARY KEY (provider, session_id))"
    ))
    if journal_applied_at:
      conn.execute(text("INSERT INTO schema_migrations VALUES ('0078_agent_write_journal', :at)"),
                   {"at": journal_applied_at})
    conn.execute(text(
      "INSERT INTO chats VALUES ('top', 'tainted', 'claude'), "
      "('crashed', 'crashed-session', 'codex'), ('old', 'clean', 'codex'), "
      "('host-helper', 'claude-host:h1:agent-1:tool-1', 'claude'), "
      "('old-helper', 'old-helper-session', 'claude'), "
      "('unlinked', 'unlinked-session', 'mobius'), ('twin', 'unlinked-session', 'mobius')"
    ))
    conn.execute(text(
      "INSERT INTO chat_runs VALUES "
      "('r-top', 'top', '2026-10-02 10:00:00', 'tainted'), "
      "('r-crashed', 'crashed', '2026-10-02 11:00:00', NULL), "
      "('r-old', 'old', '2026-09-20 10:00:00', 'clean'), "
      "('r-host', 'host-helper', '2026-10-02 12:00:00', NULL), "
      "('r-old-helper', 'old-helper', '2026-09-20 12:00:00', 'old-helper-session'), "
      "('r-unlinked', 'unlinked', '2026-10-02 13:00:00', NULL), "
      "('r-twin', 'twin', '2026-10-02 14:00:00', NULL)"
    ))
    conn.execute(text("INSERT INTO delegations VALUES ('d1', 'host-helper'), ('d2', 'old-helper')"))
    conn.execute(text(
      "INSERT INTO chat_session_links (provider, session_id, chat_id) VALUES "
      "('claude', 'tainted', 'top'), ('codex', 'crashed-session', 'crashed'), "
      "('codex', 'clean', 'old'), ('claude', 'old-helper-session', 'old-helper')"
    ))
  return eng


def _retired(eng):
  with eng.connect() as conn:
    links = dict(conn.execute(text(
      "SELECT session_id, resume_retired_at IS NOT NULL FROM chat_session_links"
    )).all())
    pointers = dict(conn.execute(text("SELECT id, session_id FROM chats")).all())
  return links, pointers


def test_migration_retires_every_session_used_while_the_instruction_was_sent(tmp_path):
  eng = _retirement_db(tmp_path, journal_applied_at="2026-10-01 18:00:00")
  migrations._retire_quiet_write_sessions(eng)
  migrations._retire_quiet_write_sessions(eng)

  links, pointers = _retired(eng)
  # A run's own session, and a chat's current session even when its run died
  # before recording one; sessions only used before the window stay resumable.
  # A current session that never got a link (best-effort recording) gets one,
  # already retired, once even when two chats name it.
  assert links == {
    "tainted": 1, "crashed-session": 1, "clean": 0, "old-helper-session": 0,
    "unlinked-session": 1, "claude-host:h1:agent-1:tool-1": 1,
  }
  # A helper resumes through its pointer (shared-host helpers have no session
  # link at all), so a helper that ran in the window loses it.
  assert pointers["host-helper"] is None
  assert pointers["old-helper"] == "old-helper-session"
  assert pointers["top"] == "tainted"  # Top-level chats reseed from history.


def test_migration_retires_nothing_where_the_instruction_never_ran(tmp_path):
  eng = _retirement_db(tmp_path, journal_applied_at=None)
  migrations._retire_quiet_write_sessions(eng)
  links, pointers = _retired(eng)
  assert not any(links.values())
  assert pointers["host-helper"] == "claude-host:h1:agent-1:tool-1"


def test_migration_is_a_no_op_on_a_database_without_session_links(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
  migrations._retire_quiet_write_sessions(eng)
  with eng.connect() as conn:
    assert conn.execute(text("SELECT count(*) FROM sqlite_master")).scalar_one() == 0


def test_reseed_drops_leaked_write_frames_from_replies_only(db):
  """A reseeded session must not see itself writing the retired frames."""
  from app.chat_context import _build_resumed_context

  frame = '<MOBIUS_WRITE n1>\n{"id":"n1.write-1","tool":"checkpoint_chat","arguments":{}}\n</MOBIUS_WRITE>'
  chat = create_chat(id="reseed-frames", title="reseed", messages=[
    {"role": "user", "content": "Why do replies show <MOBIUS_WRITE ... />?", "ts": 1},
    {"role": "assistant", "content": f"Fixed the broker.\n{frame}\nDone.", "ts": 2},
    {"role": "assistant", "content": '<MOBIUS_WRITE n2>\n{"id":"n2.write-1",\n "tool":"x"}\n\nNext.', "ts": 3},
    {"role": "assistant", "content": 'Saved. <MOBIUS_WRITE tool="checkpoint_chat" /> Then ok.', "ts": 4},
  ])
  db.add(chat)
  db.commit()
  block = _build_resumed_context(chat)
  replies = block.split("Why do replies show <MOBIUS_WRITE ... />?", 1)[1]
  assert "MOBIUS_WRITE" not in replies and "write-1" not in replies
  assert "Fixed the broker." in replies and "Done." in replies and "Saved." in replies
  assert "Next." in replies and "Then ok." in replies and '"tool":"x"' not in replies
