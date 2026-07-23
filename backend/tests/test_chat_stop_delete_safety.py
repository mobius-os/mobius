"""Stop-contract for registry-backed handles on timeout.

When handle.stop() returns False (the SDK subprocess is still draining),
stop_chat_for must NOT unregister the handle or finalize the broadcast.
The zombie runner is still alive and will call its own finally block; that
block holds the generation guard and owns transcript teardown. Removing
the registry entry here would allow a new turn to claim the chat before
the zombie finalizes — a zombie-run clobber.
"""

import asyncio

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class _FailingHandle:
  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.stop_calls = 0

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self.stop_calls += 1
    return False


def test_stop_chat_for_false_leaves_handle_registered():
  """When stop() returns False the handle is left in the registry so the
  zombie runner's own finally block owns teardown."""
  handle = _FailingHandle("chat-delete-safety")
  registry.register(handle)

  stopped, _ = asyncio.run(chat_mod.stop_chat_for("chat-delete-safety"))

  assert stopped is False
  assert handle.stop_calls == 1
  # Handle must remain — the zombie runner still needs to clean up.
  assert (
    registry.get_handle("chat-delete-safety", RunnerKind.CLAUDE_SDK)
    is not None
  )


def test_stop_chat_for_timeout_leaves_chat_running():
  """A timed-out stop leaves the chat in a running-ish state (the zombie
  runner is still alive). is_chat_running may still return True because
  the handle is still registered."""
  chat_id = "chat-delete-safety-timeout"
  handle = _FailingHandle(chat_id)
  registry.register(handle)

  stopped, _ = asyncio.run(chat_mod.stop_chat_for(chat_id))

  assert stopped is False
  assert handle.stop_calls == 1
  # The handle was NOT unregistered — is_alive reports True.
  assert registry.is_alive(chat_id) is True


def test_stop_chat_for_escalates_same_handle_and_closes_browser(monkeypatch):
  chat_id = "chat-hard-stop"
  calls: list[str] = []

  class _EscalatingHandle(_FailingHandle):
    async def force_stop(self, timeout: float = 5.0) -> bool:
      del timeout
      calls.append("force")
      return True

  async def _close_browser(closed_chat_id: str) -> None:
    calls.append(f"browser:{closed_chat_id}")

  monkeypatch.setattr(chat_mod, "_close_browser_session", _close_browser)
  handle = _EscalatingHandle(chat_id)
  registry.register(handle)

  stopped, _ = asyncio.run(chat_mod.stop_chat_for(chat_id))

  assert stopped is True
  assert calls == ["force", f"browser:{chat_id}"]
  assert registry.get_handle(chat_id, handle.kind) is None


def test_stop_chat_for_never_escalates_replaced_handle(monkeypatch):
  chat_id = "chat-successor-guard"
  calls: list[str] = []

  class _OldHandle(_FailingHandle):
    async def stop(self, timeout: float = 2.0) -> bool:
      del timeout
      registry.register(successor)
      return False

    async def force_stop(self, timeout: float = 5.0) -> bool:
      del timeout
      calls.append("unsafe-force")
      return True

  successor = _FailingHandle(chat_id)
  old = _OldHandle(chat_id)
  registry.register(old)

  stopped, _ = asyncio.run(chat_mod.stop_chat_for(chat_id))

  assert stopped is False
  assert calls == []
  assert registry.get_handle(chat_id, old.kind) is successor


def test_stop_on_orphaned_run_after_restart_succeeds(client, auth, db):
  """Stop on an orphaned run — run_status stuck 'running' with an EMPTY
  registry (the exact shape a prior restart leaves: the in-memory registry
  is gone but the durable marker survives) — must succeed gracefully: clear
  the stuck marker + the queue, return success, NOT error or strand the chat.

  This is the no-handles arm of stop_chat_for: with no live handle there is
  no runner teardown to defer to, so the marker is cleared immediately."""
  from datetime import UTC, datetime

  from app import models

  chat_id = "orphan-after-restart"
  c = models.Chat(
    id=chat_id, title="t",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending_messages=[{"role": "user", "content": "queued", "ts": 2}],
    run_status="running",
    run_started_at=datetime.now(UTC),
  )
  db.add(c)
  db.add(models.ChatRun(
    id="rt-orphan-after-restart",
    chat_id=chat_id,
    status="running",
    provider="claude",
    started_at=datetime.now(UTC),
  ))
  db.commit()
  # No registry handle — exactly the post-restart orphan shape.
  assert chat_mod.is_chat_running(chat_id) is False

  r = client.post("/api/chat/stop", json={"chat_id": chat_id}, headers=auth)
  assert r.status_code == 200, r.text
  assert r.json()["stopped"] is True, "Stop on an orphan must report success"

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assert row.run_status is None, "the orphaned 'running' marker must be cleared"
  assert row.run_started_at is None
  run = db.query(models.ChatRun).filter(
    models.ChatRun.id == "rt-orphan-after-restart",
  ).one()
  assert run.status == "stopped"
  assert chat_mod.is_chat_running(chat_id) is False
