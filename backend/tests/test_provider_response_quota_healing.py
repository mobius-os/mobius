"""Only a provider response heals an older quota signal; input receipt stays.

These drive the real chat turn (`_run_chat_impl`), the real Stop and the real
writer acknowledgement around a scripted runner return, for both providers.
The runner's own `provider_responded` evidence is pinned separately in the
Codex, Claude and helper-host runner tests.
"""

import asyncio
from datetime import timedelta

import pytest

from app import chat as chat_mod, models, schemas
from app.broadcast import create_broadcast, remove_broadcast
from app.chat_writer import StartTurn, get_writer
from app.runtime_types import prompt_not_sent_result
from app.timeutil import now_naive_utc
from tests.test_chat_waits import _command_wait, _owner_chat, _seed_declaring_run

_PROVIDERS = {
  "codex": ("CodexProvider", "app.codex_sdk_runner.run_codex_sdk_turn"),
  "claude": ("ClaudeProvider", "app.claude_sdk_runner.run_claude_sdk_turn"),
}


def _submitted(**extra):
  return {"session_id": None, "cost_usd": None, "error": None, **extra}


# outcome -> (Stop during the runner?, runner return, Wait receipted?, limit kept?)
_OUTCOMES = {
  "no_response_stop": (
    True, _submitted(terminal_status="interrupted"), True, True,
  ),
  "partial_response_stop": (
    True, _submitted(terminal_status="interrupted", provider_responded=True),
    True, False,
  ),
  "normal_completion": (False, _submitted(provider_responded=True), True, False),
  "pre_submission_stop": (True, prompt_not_sent_result(None), False, True),
}


def _run_turn_with_limit(
  client, owner_token, db, monkeypatch, *, provider, stop, runner_result,
  limit_observed_at,
):
  chat_id = _owner_chat(client, owner_token)
  wait = _command_wait(
    db, chat_id=chat_id, description="carried wait result",
    kind="command", command="true",
    created_by_run_id=_seed_declaring_run(db, chat_id),
  )
  wait.status = "met"
  wait.met_at = now_naive_utc()
  db.add(models.ProviderAvailability(
    provider=provider,
    limited_until=now_naive_utc() + timedelta(hours=1),
    unavailable_reason="usage_limit",
    updated_at=limit_observed_at,
  ))
  db.commit()
  provider_cls, runner_path = _PROVIDERS[provider]
  monkeypatch.setattr(
    f"app.providers.{provider_cls}.check_auth", lambda self, _data_dir: None,
  )
  monkeypatch.setattr(
    f"app.providers.{provider_cls}.ensure_auth",
    lambda self, _data_dir: asyncio.sleep(0),
  )
  carried = []

  async def runner(*, user_message, bc, **_kwargs):
    carried.append("carried wait result" in user_message)
    if runner_result.get("provider_responded"):
      # What a responding provider streams before any Stop lands.
      bc.publish({"type": "text", "content": "Checking the result now."})
    if stop:
      outcome = await chat_mod.stop_chat_for(chat_id)
      assert outcome.stopped
    return dict(runner_result)

  monkeypatch.setattr(runner_path, runner)
  run_token = f"quota-{chat_id}"
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": "Any news?", "ts": 5, "cid": run_token},
    title_source="Any news?", default_provider=provider,
  )).result(timeout=5)
  create_broadcast(chat_id)
  try:
    asyncio.run(chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="Any news?")],
      chat_id=chat_id, session_id=None, provider_id=provider,
      run_gen=chat_mod.current_run_generation(chat_id), run_token=run_token,
    ))
  finally:
    remove_broadcast(chat_id)
  assert carried == [True]
  db.expire_all()
  return (
    db.get(models.ChatRun, run_token),
    db.get(models.ChatWait, wait.id),
    db.get(models.ProviderAvailability, provider),
  )


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
@pytest.mark.parametrize("outcome", sorted(_OUTCOMES))
def test_submitted_input_is_receipted_but_only_a_response_heals_an_older_limit(
  client, owner_token, db, monkeypatch, provider, outcome,
):
  stop, runner_result, receipted, limit_kept = _OUTCOMES[outcome]
  run, wait, availability = _run_turn_with_limit(
    client, owner_token, db, monkeypatch, provider=provider, stop=stop,
    runner_result=runner_result,
    limit_observed_at=now_naive_utc() - timedelta(minutes=1),
  )

  assert run.status == ("stopped" if stop else "completed")
  assert run.provider_execution_admitted is True
  assert (wait.resume_delivered_at is not None) is receipted
  assert (availability is not None) is limit_kept


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_a_response_never_clears_a_limit_observed_after_its_run_started(
  client, owner_token, db, monkeypatch, provider,
):
  _run, wait, availability = _run_turn_with_limit(
    client, owner_token, db, monkeypatch, provider=provider, stop=False,
    runner_result=_submitted(provider_responded=True),
    limit_observed_at=now_naive_utc() + timedelta(minutes=1),
  )

  assert wait.resume_delivered_at is not None
  assert availability is not None
