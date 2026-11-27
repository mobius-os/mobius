"""After an in-turn compaction the agent sees its open owner requests again.

The turn-start message carries the open list (and the Goal brief); a provider
compacting inside a running turn can summarize both away. Each provider's
post-compaction boundary restores them, read fresh at delivery, and never
hands the owner's list to a delegated helper. The injected list is a bounded
summary; the tool's own list stays whole.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import compaction_context, models, owner_input_requests as requests
from app.database import SessionLocal

from tests.test_claude_sdk_runner import _Bus, _install_fake_client, _run_turn


def _add(chat_id, key, prompt="Which environment should the release go to?", **spec):
  with SessionLocal() as session:
    requests.add_request(session, chat_id=chat_id, run_id=None, goal_id=None,
                         key=key, spec={"prompt": prompt, **spec})


def _restored_keys(text):
  body = text.rsplit("<open_owner_requests>", 1)[1].split("</open_owner_requests>", 1)[0]
  return [row["key"] for row in json.loads(body)]


# Shared carrier -------------------------------------------------------------


def test_a_goal_less_chat_restores_its_open_requests_read_at_delivery(chat):
  _add(chat.id, "release-target")
  refresh = compaction_context.compaction_context_refresh(chat.id, "run", delegated=False)
  # Recorded after the turn started: the restore reads the list now.
  _add(chat.id, "tone", "How formal should the changelog be?")
  with SessionLocal() as session:
    requests.withdraw_request(session, chat_id=chat.id, ref="release-target",
                              reason="The owner chose staging in chat.")
  assert asyncio.run(refresh.take()) == ""  # nothing owed before a compaction

  refresh.mark_compacted()
  restored = asyncio.run(refresh.take())

  assert restored.startswith(
    "Context was compacted; open owner requests follow (owner_input with op list shows them).\n")
  assert "<mobius_goal" not in restored
  assert _restored_keys(restored) == ["tone"]
  assert asyncio.run(refresh.take()) == ""  # once per compaction


def test_a_coordinator_restores_its_goal_brief_and_open_requests_together(chat, monkeypatch):
  from app import goals
  _add(chat.id, "tone", "How formal should the changelog be?")
  monkeypatch.setattr(goals, "turn_goal_brief", lambda *a, **k: "<mobius_goal>G</mobius_goal>")
  with SessionLocal() as session:
    restored = compaction_context.compaction_context(session, chat.id, "run", delegated=False)

  lead, body = restored.split("\n", 1)
  # The lead names both full reads, so a clipped hook context still points at them.
  assert lead == (
    "Context was compacted; current Goal brief follows (update_goal with no arguments "
    "shows the plan); open owner requests follow (owner_input with op list shows them).")
  assert body.startswith("<mobius_goal>G</mobius_goal>\n\n")
  assert _restored_keys(body) == ["tone"]
  # No owner message follows a restore, so it is never framed as one.
  assert "while these requests were open" not in restored


def test_a_delegated_helper_never_receives_the_owner_request_list(chat, monkeypatch):
  from app import goals
  # Even rows on the helper's own chat id stay out: a helper asks its parent.
  _add(chat.id, "tone", "How formal should the changelog be?")
  monkeypatch.setattr(goals, "helper_goal_brief", lambda *a, **k: "<mobius_goal_brief>H</mobius_goal_brief>")
  with SessionLocal() as session:
    assert compaction_context.compaction_context(session, chat.id, "run", delegated=True) == (
      "Context was compacted; current Goal brief follows (read_goal re-reads it).\n"
      "<mobius_goal_brief>H</mobius_goal_brief>")
    monkeypatch.setattr(goals, "helper_goal_brief", lambda *a, **k: "")
    assert compaction_context.compaction_context(session, chat.id, "run", delegated=True) == ""


# Turn wiring ----------------------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_every_top_level_turn_arms_a_restore_that_carries_open_requests(
  client, auth, db, monkeypatch, provider,
):
  from app import chat as chat_mod, schemas
  from app.broadcast import create_broadcast
  chat_id = client.post("/api/chats", json={"title": "Asks"}, headers=auth).json()["id"]
  _add(chat_id, "release-target")
  cls = "ClaudeProvider" if provider == "claude" else "CodexProvider"
  monkeypatch.setattr(f"app.providers.{cls}.check_auth", lambda *a: None)
  monkeypatch.setattr(f"app.providers.{cls}.ensure_auth", lambda *a: asyncio.sleep(0))
  restored = []

  async def runner(**kwargs):
    assert "<open_owner_requests>" in kwargs["user_message"]
    refresh = kwargs["compaction_refresh"]
    _add(chat_id, "tone", "How formal should the changelog be?")
    refresh.mark_compacted()
    restored.append(await refresh.take())
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(f"app.{provider}_sdk_runner.run_{provider}_sdk_turn", runner)
  db.add(models.ChatRun(id="asking-run", root_run_id="asking-run", chat_id=chat_id,
                        status="running", provider=provider, provider_execution_admitted=False))
  db.commit()
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="Ship it")],
    chat_id=chat_id, session_id=None, provider_id=provider,
    run_token="asking-run", run_gen=chat_mod.current_run_generation(chat_id),
  ))
  assert len(restored) == 1, "provider was not reached"
  assert _restored_keys(restored[0]) == ["release-target", "tone"]


# Provider boundaries --------------------------------------------------------


def _session_start_hook(options):
  return next(hook for matcher in options.hooks["SessionStart"] for hook in matcher.hooks
              if hook.__name__ == "context_after_compaction_session_start_hook")


def _post_tool_hook(options):
  return next(hook for matcher in options.hooks["PostToolUse"] for hook in matcher.hooks
              if hook.__name__ == "context_after_compaction_hook")


@pytest.mark.asyncio
async def test_claude_restores_context_in_the_request_that_continues_a_compacted_turn(monkeypatch):
  clients = _install_fake_client(monkeypatch)
  loads = []
  refresh = compaction_context.CompactionContextRefresh(
    lambda: loads.append(1) or "RESTORED")
  await _run_turn("chat-claude-restore", bc=_Bus(), cwd="/data", compaction_refresh=refresh)
  options = clients[0].options
  session_start, post_tool = _session_start_hook(options), _post_tool_hook(options)
  compact = {"hook_event_name": "SessionStart", "source": "compact"}

  # Only a compaction's SessionStart restores, and never for a subagent.
  assert await session_start({**compact, "source": "startup"}, None, {}) == {"continue_": True}
  assert await session_start({**compact, "agent_id": "sub"}, None, {}) == {"continue_": True}
  assert loads == []

  await options.hooks["PreCompact"][0].hooks[0]({"trigger": "auto"}, None, {})
  restored = await session_start(compact, None, {})
  assert restored["hookSpecificOutput"] == {
    "hookEventName": "SessionStart", "additionalContext": "RESTORED"}
  # The PostToolUse fallback has nothing left to deliver for that compaction.
  assert await post_tool({"tool_name": "Bash"}, "t1", {}) == {"continue_": True}
  assert loads == [1]


@pytest.mark.asyncio
async def test_claude_tool_result_restores_when_no_session_start_arrived(monkeypatch):
  clients = _install_fake_client(monkeypatch)
  refresh = compaction_context.CompactionContextRefresh(lambda: "RESTORED")
  await _run_turn("chat-claude-fallback", bc=_Bus(), cwd="/data", compaction_refresh=refresh)
  options = clients[0].options
  await options.hooks["PreCompact"][0].hooks[0]({"trigger": "auto"}, None, {})
  restored = await _post_tool_hook(options)({"tool_name": "Bash"}, "t1", {})
  assert restored["hookSpecificOutput"]["additionalContext"] == "RESTORED"
  # A later compaction is its own need.
  again = await _session_start_hook(options)(
    {"hook_event_name": "SessionStart", "source": "compact"}, None, {})
  assert again["hookSpecificOutput"]["additionalContext"] == "RESTORED"


@pytest.mark.asyncio
async def test_codex_compaction_hook_returns_the_fresh_open_requests(chat):
  from app.codex_sdk_runner import ActiveCodexTurn
  refresh = compaction_context.compaction_context_refresh(chat.id, "run", delegated=False)
  active = ActiveCodexTurn(SimpleNamespace(id="thread"), object(), chat_id=chat.id,
                           compaction_refresh=refresh)
  _add(chat.id, "tone", "How formal should the changelog be?")
  restored = await active.context_after_compaction("thread")
  assert _restored_keys(restored) == ["tone"]
  # Each compaction restores the list again.
  assert _restored_keys(await active.context_after_compaction("thread")) == ["tone"]


# Steering --------------------------------------------------------------------


def _show_on_card(chat_id, key, card_id="shown-card"):
  with SessionLocal() as session:
    row = session.query(models.OwnerInputRequest).filter_by(chat_id=chat_id, key=key).one()
    row.published_question_id = card_id
    session.get(models.Chat, chat_id).open_request_card_id = card_id
    session.commit()


@pytest.mark.parametrize("shown", [True, False], ids=["shown", "not-yet-shown"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_owner_steer_while_requests_are_open_says_to_settle_what_it_answers(
  client, auth, monkeypatch, provider, shown,
):
  from app.broadcast import create_broadcast
  from app.runner_registry import registry
  from tests import test_chats_stream_steer as steer_harness
  chat_id = f"steer-open-{provider}"
  if provider == "codex":
    steer_harness._make_codex_chat(chat_id, steer_enabled=True)
    delivered = []

    async def fake_steer(cid, message, *_durable):
      delivered.append(message)
      return True

    steer_harness._patch_codex_steer(monkeypatch, fake_steer)
    registry.register(steer_harness._make_active_codex_turn(chat_id))
  else:
    steer_harness._make_claude_chat(chat_id, steer_enabled=True)
    handle = steer_harness._make_active_claude_client(chat_id)
    registry.register(handle)
  create_broadcast(chat_id)
  _add(chat_id, "tone", "How formal should the changelog be?")
  if shown:
    _show_on_card(chat_id, "tone")

  res = client.post(f"/api/chats/{chat_id}/messages",
                    json={"content": "Keep it casual"}, headers=auth)

  assert res.status_code == 202 and res.json()["status"] == "steered", res.text
  text = (delivered[0] if provider == "codex"
          else steer_harness._claude_attempts(handle)[0].text)
  # Only a request already on a card was passed over; one recorded in this
  # turn has not been shown, so the message is not written instead of it.
  wrote_instead = "The owner wrote the message below while these requests were open"
  assert (wrote_instead in text) is shown
  assert ("resolve with source owner_message" in text) is shown
  assert "resolve or withdraw one the owner's latest message has settled" in text
  assert _restored_keys(text) == ["tone"]
  assert text.rstrip().endswith("Keep it casual")
  # Only the provider's copy is framed; the owner's row is their own words.
  rows = [*(steer_harness._read_chat(chat_id).pending_messages or []),
          *steer_harness._read_messages(chat_id)]
  assert "Keep it casual" in [row.get("content") for row in rows]
  assert all("<open_owner_requests>" not in (row.get("content") or "") for row in rows)


# Bound -----------------------------------------------------------------------


def _maximal_rows(chat_id, char):
  """Every open row at each summarized field's own limit, in ``char``.

  Stored directly: a spec in escaping-heavy text would exceed today's spec
  byte cap, but the injected bound must not depend on that cap.
  """
  for index in range(requests.MAX_OPEN_REQUESTS):
    key = f"k{index:02d}-" + "x" * 76
    _add(chat_id, key, "placeholder")
    with SessionLocal() as session:
      row = requests.open_request_by_key(session, chat_id, key)
      row.spec_json = {
        "kind": "secret", "header": char * 80, "prompt": char * 2000,
        "fields": [{"name": "answer", "kind": "choice", "multiple": True, "options": [
          {"id": str(n), "label": f"{n}" + char * 99, "description": "d"}
          for n in range(requests.MAX_OPTIONS)]}],
        "work_key": "w" * 256,
        "secure_input": {"fields": [{"label": char * 80}] * 8},
      }
      row.blocks_json = {"tasks": [char * 64] * 20, "work": char * 500}
      session.commit()


@pytest.mark.parametrize("char", ["a", "<", "中"])
def test_the_injected_open_list_stays_within_its_budget_and_says_it_was_shortened(chat, char):
  from app.context_budget import OPEN_REQUESTS_CONTEXT_CHARS
  _maximal_rows(chat.id, char)
  with SessionLocal() as session:
    context = requests.build_open_requests_context(session, chat.id, owner_wrote_instead=True)
    whole = requests.open_snapshot(session, chat.id)["open"]

  body = context.rsplit("<open_owner_requests>", 1)[1].split("</open_owner_requests>", 1)[0]
  assert len(body) <= OPEN_REQUESTS_CONTEXT_CHARS
  # The whole block, with the compaction lead, stays inline in a Claude hook.
  assert len("Context was compacted; open owner requests follow "
             "(owner_input with op list shows them).\n" + context) < 10_000
  assert "Rows marked shortened are cut to fit here; call owner_input with op list" in context
  rows = json.loads(body)
  assert [row["key"] for row in rows] == [row["key"] for row in whole]  # none dropped
  assert all(row.get("shortened") for row in rows)
  # The tool's own list is unaffected: every row whole.
  assert all(len(row["prompt"]) == 2000 and len(row["options"]) == requests.MAX_OPTIONS
             for row in whole)


def test_a_long_prompt_becomes_a_marked_excerpt_and_short_rows_stay_whole(chat):
  # A request recorded before prompts were limited can still be this long.
  _add(chat.id, "long", "placeholder")
  with SessionLocal() as session:
    row = requests.open_request_by_key(session, chat.id, "long")
    row.spec_json = {**row.spec_json, "prompt": "p" * 2000}
    session.commit()
  _add(chat.id, "short", "Which environment?",
       options=[{"label": "Staging", "description": "Deploy to staging only."},
                {"label": "Production", "description": "Deploy to production."}])
  with SessionLocal() as session:
    context = requests.build_open_requests_context(session, chat.id)
  rows = {row["key"]: row for row in json.loads(
    context.rsplit("<open_owner_requests>", 1)[1].split("</open_owner_requests>", 1)[0])}
  assert len(rows["long"]["prompt"]) <= 600 and rows["long"]["shortened"] is True
  assert rows["long"]["prompt"].endswith("characters; owner_input list shows it whole]")
  assert rows["short"] == {
    "id": rows["short"]["id"], "key": "short", "kind": "question", "revision": 1,
    "prompt": "Which environment?", "options": ["Staging", "Production"],
    "shown_to_owner": False,
  }


def test_an_unshortened_list_carries_no_shortening_note(chat):
  _add(chat.id, "tone", "How formal should the changelog be?")
  with SessionLocal() as session:
    context = requests.build_open_requests_context(session, chat.id)
  assert "shortened" not in context
  assert "the owner settles each request on it separately.\n<open_owner_requests>" in context
