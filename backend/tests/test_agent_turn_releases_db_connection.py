"""Long provider turns must not pin a pooled database connection."""

import pytest

from app import chat as chat_mod
from app import chat_queue, models, schemas
from app.broadcast import create_broadcast
from app.database import SessionLocal, engine


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

  baseline = engine.pool.checkedout()
  observed = []

  async def fake_runner(**_kwargs):
    observed.append(engine.pool.checkedout())
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
  assert engine.pool.checkedout() == baseline


@pytest.mark.asyncio
async def test_agent_setup_exception_always_returns_connection(monkeypatch):
  """Unexpected setup failures are covered by the outer session owner."""
  baseline = engine.pool.checkedout()

  def fail_after_checkout(db, *_args, **_kwargs):
    db.query(models.Owner).first()
    assert engine.pool.checkedout() == baseline + 1
    raise RuntimeError("setup failed")

  monkeypatch.setattr(chat_mod, "_build_app_context", fail_after_checkout)

  with pytest.raises(RuntimeError, match="setup failed"):
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hello")],
      chat_id="pool-release-setup-error",
      provider_id="codex",
    )

  assert engine.pool.checkedout() == baseline


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
