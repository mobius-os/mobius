"""Codex restores the turn's context after compaction even when its hook fails.

The SessionStart(compact) hook is the primary path. The runner also sees the
compaction in its event stream and, if the hook did not deliver by the
model's first output after it, steers the same text into the live turn. The
refresher counts the hook call and the stream report as one compaction.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app import codex_sdk_runner
from app.codex_sdk_runner import ActiveCodexTurn, _codex_platform_hook_thread_config
from app.compaction_context import CompactionContextRefresh
from tests.test_codex_sdk_runner import (
  _FakeBroadcast, _FakeThread, _FakeTurnCompletedNotification, _FakeTurnHandle, _fake_sdk,
)

DELIVERED = "Context was compacted; open owner requests follow.\nFRESH_REQUESTS"


def test_one_compaction_seen_by_hook_and_stream_is_one_need():
  for order in (("hook", "stream"), ("stream", "hook")):
    refresh = CompactionContextRefresh(lambda: DELIVERED)
    refresh.mark_compacted(order[0])
    assert asyncio.run(refresh.take()) == DELIVERED
    refresh.mark_compacted(order[1])
    assert not refresh.stale
  # A stream report the hook never matched stays a need for the fallback.
  refresh = CompactionContextRefresh(lambda: DELIVERED)
  refresh.mark_compacted("stream")
  assert refresh.stale


def _active(*, hook_installed=True, load=lambda: DELIVERED):
  turn = _FakeTurnHandle()
  active = ActiveCodexTurn(SimpleNamespace(id="thread-a"), turn, chat_id="chat",
                           compaction_refresh=CompactionContextRefresh(load))
  active.compaction_hook_installed = hook_installed
  return active, turn


async def _settle(active):
  await asyncio.gather(*active._restore_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_first", [True, False])
async def test_a_delivering_hook_leaves_nothing_for_the_fallback(hook_first):
  active, turn = _active()
  if hook_first:
    assert await active.context_after_compaction("thread-a") == DELIVERED
    active.observe_compaction()
  else:
    active.observe_compaction()
    assert await active.context_after_compaction("thread-a") == DELIVERED
  active.observe_model_output()
  await _settle(active)
  assert turn.steered == []


@pytest.mark.asyncio
async def test_a_failed_hook_load_is_steered_once_after_the_next_model_output():
  loads = iter([RuntimeError("endpoint failed"), DELIVERED])

  def load():
    value = next(loads)
    if isinstance(value, Exception):
      raise value
    return value

  active, turn = _active(load=load)
  active.observe_compaction()
  # The hook reached the backend but its read failed: it adds nothing.
  assert await active.context_after_compaction("thread-a") == ""
  await _settle(active)
  assert turn.steered == []  # The hook path still owns it until the model answers.
  active.observe_model_output()
  active.observe_model_output()
  await _settle(active)
  assert turn.steered == [DELIVERED]


@pytest.mark.asyncio
async def test_one_compaction_reported_twice_by_the_stream_is_steered_once():
  active, turn = _active(hook_installed=False)
  active.observe_compaction()
  active.observe_compaction()  # Item and legacy notification of one compaction.
  active.observe_model_output()
  await _settle(active)
  assert turn.steered == [DELIVERED]


@pytest.mark.asyncio
async def test_an_ending_turn_is_not_steered():
  active, turn = _active(hook_installed=False)
  active._interrupt_requested = True
  active.observe_compaction()
  await _settle(active)
  assert turn.steered == []
  assert active._compaction_refresh.stale  # The next turn start carries it.


class _ItemStarted:
  def __init__(self, item):
    self.item = SimpleNamespace(root=item)


class _ItemCompleted:
  def __init__(self, item):
    self.item = SimpleNamespace(root=item)


class _Compaction:
  pass


class _Reasoning:
  id = "reasoning-1"


class _ScriptedTurn(_FakeTurnHandle):
  """Streams a compaction, then lets the test act at the model's next output."""

  def __init__(self, before_output):
    super().__init__(turn_id="turn")
    self._before_output = before_output
    self.steered_event = asyncio.Event()

  async def stream(self):
    yield SimpleNamespace(method="item/completed", payload=_ItemCompleted(_Compaction()))
    await self._before_output()
    yield SimpleNamespace(method="item/started", payload=_ItemStarted(_Reasoning()))
    try:
      await asyncio.wait_for(self.steered_event.wait(), timeout=0.5)
    except asyncio.TimeoutError:
      pass
    completed = SimpleNamespace(id="turn", usage=None, error=None)
    yield SimpleNamespace(method="turn/completed",
                          payload=_FakeTurnCompletedNotification(completed))

  async def steer(self, message):
    await super().steer(message)
    self.steered_event.set()


def _run(monkeypatch, *, discovery, before_output=None):
  state = {}

  async def no_hook_call():
    return None

  async def hook_call():
    state["hook"] = await state["active"].context_after_compaction("thread-a")

  turn = _ScriptedTurn({None: no_hook_call, "hook": hook_call}[before_output])
  thread = _FakeThread("thread-a", turn)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def thread_start(self, **_kwargs):
      return thread

  sdk = {**_fake_sdk(FakeAsyncCodex), "HooksListResponse": object,
         "ContextCompactionThreadItem": _Compaction,
         "ItemCompletedNotification": _ItemCompleted,
         "ItemStartedNotification": _ItemStarted}
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)
  monkeypatch.setattr(codex_sdk_runner, "_codex_platform_hook_thread_config", discovery)
  register = codex_sdk_runner.registry.register

  def capture(active):
    state["active"] = active
    return register(active)

  monkeypatch.setattr(codex_sdk_runner.registry, "register", capture)
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="work", session_id=None, base_env={}, cwd="/tmp", chat_id="chat",
    bc=_FakeBroadcast(), pending_questions={}, db=None,
    compaction_refresh=CompactionContextRefresh(lambda: DELIVERED),
  ))
  assert result["error"] is None
  return turn, state


def _listing(monkeypatch, request):
  class Client:
    async def request(self, method, params, response_model):
      return await request()
  monkeypatch.setattr(codex_sdk_runner, "control_client", lambda _: Client())


def _discovered(*overrides):
  async def listing():
    from tests.test_codex_goal_brief_hook import _metadata
    return SimpleNamespace(data=[SimpleNamespace(hooks=[
      _metadata(override, key=f"hook-{n}") for n, override in enumerate(overrides)
    ])])
  return listing


def test_turn_without_its_hook_steers_the_context_after_an_observed_compaction(monkeypatch):
  # Codex did not discover the restore hook, so it is left untrusted.
  _listing(monkeypatch, _discovered(codex_sdk_runner._codex_owner_card_hook_override()))
  turn, state = _run(monkeypatch, discovery=_codex_platform_hook_thread_config)
  assert not state["active"].compaction_hook_installed
  assert turn.steered == [DELIVERED]


def test_a_discovered_hook_is_known_as_installed(monkeypatch):
  _listing(monkeypatch, _discovered(*codex_sdk_runner._codex_platform_hook_overrides()))
  _turn, state = _run(monkeypatch, discovery=_codex_platform_hook_thread_config)
  assert state["active"].compaction_hook_installed


async def _trusting_discovery(codex, sdk, cwd, config, hook_overrides, *, optional=()):
  return {**(config or {}), "hooks.state": {
    str(n): {"trusted_hash": "hash"} for n in range(len(hook_overrides) + len(optional))
  }}


def test_turn_whose_hook_call_failed_steers_the_context_at_the_next_model_output(monkeypatch):
  # The hook is trusted but its call never reached the backend.
  turn, state = _run(monkeypatch, discovery=_trusting_discovery)
  assert state["active"].compaction_hook_installed
  assert turn.steered == [DELIVERED]


def test_turn_whose_hook_delivered_is_not_steered_again(monkeypatch):
  turn, state = _run(monkeypatch, discovery=_trusting_discovery, before_output="hook")
  assert state["hook"] == DELIVERED
  assert turn.steered == []
