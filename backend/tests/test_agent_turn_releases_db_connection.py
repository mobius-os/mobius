"""Long provider turns must not pin a pooled database connection."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from app import chat as chat_mod
from app import chat_queue, database, models, schemas
from app.broadcast import create_broadcast, remove_broadcast
from app.database import SessionLocal, checked_out_connections, engine


def _wait_for_writer_connection():
  """Stabilize the async writer startup before taking pool baselines."""
  from app.chat_writer import get_writer
  assert get_writer()._session_ready.wait(timeout=2)


def test_sqlite_does_not_have_a_fixed_connection_pool_ceiling():
  """File SQLite opens per-unit connections instead of starving at 5 + 10."""
  assert isinstance(engine.pool, NullPool)


def test_sqlite_serves_more_than_the_old_fifteen_connection_ceiling():
  """Concurrent readers do not queue behind QueuePool's former hard cap."""
  _wait_for_writer_connection()
  baseline = checked_out_connections()
  sessions = [SessionLocal() for _ in range(20)]
  try:
    for session in sessions:
      assert session.execute(text("SELECT 1")).scalar_one() == 1
    assert checked_out_connections() == baseline + len(sessions)
  finally:
    for session in sessions:
      session.close()
  assert checked_out_connections() == baseline


class _Provider:
  def __init__(self, name: str):
    self.name = name

  def check_auth(self, _data_dir: str):
    return None

  async def ensure_auth(self, _data_dir: str):
    return None

  def build_env(self, **_kwargs):
    return {}


@pytest.mark.asyncio
async def test_agent_turn_closes_preflight_session_before_provider_wait(
  chat, db, monkeypatch,
):
  """The exact turn session is closed before a provider can wait indefinitely."""
  chat.provider = "codex"
  # Avoid the first-send settings commit so this exercises the setup-query
  # checkout rather than relying on commit() to return the connection.
  chat.agent_settings_json = {"model": "gpt-5.4"}
  db.commit()

  real_session_factory = database.SessionLocal
  turn_sessions = []

  class TrackingSession:
    def __init__(self):
      self.real = real_session_factory()
      self.close_calls = 0

    def close(self):
      self.close_calls += 1
      self.real.close()

    def __getattr__(self, name):
      return getattr(self.real, name)

  def tracking_session_factory():
    session = TrackingSession()
    turn_sessions.append(session)
    return session

  monkeypatch.setattr(database, "SessionLocal", tracking_session_factory)
  runner_started = asyncio.Event()
  release_runner = asyncio.Event()

  async def fake_runner(**kwargs):
    turn_db = kwargs["db"]
    assert turn_db is turn_sessions[0]
    assert turn_db.close_calls >= 1, (
      "the turn must close its preflight DB session before provider execution"
    )
    assert not turn_db.real.in_transaction()
    runner_started.set()
    await release_runner.wait()
    return {"session_id": None, "cost_usd": 0.0, "error": None}

  async def fake_complete(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  monkeypatch.setattr(
    "app.codex_sdk_runner.run_codex_sdk_turn",
    fake_runner,
  )
  monkeypatch.setattr(
    "app.providers.CodexProvider.check_auth",
    lambda self, _data_dir: None,
  )
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete)

  create_broadcast(chat.id)
  task = asyncio.create_task(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="hi")],
    chat_id=chat.id,
    session_id="existing-session",
    provider_id="codex",
    run_gen=chat_mod.current_run_generation(chat.id),
  ))
  try:
    await asyncio.wait_for(runner_started.wait(), timeout=2)
  finally:
    release_runner.set()
    await asyncio.wait_for(task, timeout=2)
    remove_broadcast(chat.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("provider_id", "provider_name"),
  (("codex", "Codex"), ("claude", "Claude Code")),
)
async def test_agent_turn_returns_connection_while_provider_is_running(
  monkeypatch, provider_id, provider_name,
):
  """The setup session may be reused after the await, but not held during it."""
  chat_id = f"pool-release-{provider_id}"
  setup = SessionLocal()
  try:
    setup.add(models.Owner(
      username="owner",
      hashed_password="unused",
      provider=provider_id,
    ))
    setup.add(models.Chat(
      id=chat_id,
      title="pool release",
      messages=[],
      provider=provider_id,
      session_id="existing-session",
    ))
    setup.commit()
  finally:
    setup.close()

  _wait_for_writer_connection()
  baseline = checked_out_connections()
  observed = []

  async def fake_runner(**_kwargs):
    observed.append(checked_out_connections())
    return {
      "session_id": "existing-session",
      "cost_usd": 0.0,
      "error": None,
    }

  async def fake_complete_turn(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  monkeypatch.setattr(
    chat_mod, "get_provider", lambda _provider_id: _Provider(provider_name),
  )
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete_turn)
  if provider_id == "codex":
    from app import codex_sdk_runner
    monkeypatch.setattr(codex_sdk_runner, "run_codex_sdk_turn", fake_runner)
  else:
    from app import claude_sdk_runner
    monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)
    monkeypatch.setattr(claude_sdk_runner, "_resumable", lambda *_a, **_k: True)

  create_broadcast(chat_id)
  result = await chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="hello")],
    chat_id=chat_id,
    session_id="existing-session",
    provider_id=provider_id,
  )

  assert result is chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED
  assert observed == [baseline], (
    "the provider await must begin with the turn's DB checkout returned"
  )
  assert checked_out_connections() == baseline


@pytest.mark.asyncio
async def test_agent_setup_exception_always_returns_connection(monkeypatch):
  """Unexpected setup failures are covered by the outer session owner."""
  _wait_for_writer_connection()
  baseline = checked_out_connections()

  def fail_after_checkout(db, *_args, **_kwargs):
    db.query(models.Owner).first()
    assert checked_out_connections() == baseline + 1
    raise RuntimeError("setup failed")

  monkeypatch.setattr(chat_mod, "_build_app_context", fail_after_checkout)

  with pytest.raises(RuntimeError, match="setup failed"):
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hello")],
      chat_id="pool-release-setup-error",
      provider_id="codex",
    )

  assert checked_out_connections() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("provider_id", "provider_name"),
  (("codex", "Codex"), ("claude", "Claude Code")),
)
async def test_provider_exception_requests_owned_browser_cleanup(
  monkeypatch, provider_id, provider_name,
):
  """A failed runner closes its browser unless the terminal gate is stale."""
  chat_id = f"browser-cleanup-{provider_id}"
  setup = SessionLocal()
  try:
    setup.add(models.Owner(
      username="owner",
      hashed_password="unused",
      provider=provider_id,
    ))
    setup.add(models.Chat(
      id=chat_id,
      title="browser cleanup",
      messages=[],
      provider=provider_id,
      session_id="existing-session",
    ))
    setup.commit()
  finally:
    setup.close()

  async def failing_runner(**_kwargs):
    raise RuntimeError("provider failed")

  captured = {}

  async def fake_complete_turn(**kwargs):
    captured.update(kwargs)
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  monkeypatch.setattr(
    chat_mod, "get_provider", lambda _provider_id: _Provider(provider_name),
  )
  monkeypatch.setattr(chat_mod, "_complete_turn", fake_complete_turn)
  if provider_id == "codex":
    from app import codex_sdk_runner
    monkeypatch.setattr(codex_sdk_runner, "run_codex_sdk_turn", failing_runner)
  else:
    from app import claude_sdk_runner
    monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", failing_runner)
    monkeypatch.setattr(claude_sdk_runner, "_resumable", lambda *_a, **_k: True)

  create_broadcast(chat_id)
  result = await chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="hello")],
    chat_id=chat_id,
    session_id="existing-session",
    provider_id=provider_id,
  )

  assert result is chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED
  assert captured["close_browser"] is True
