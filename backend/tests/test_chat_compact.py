"""Incoming-provider synthesis and atomic provider-switch coverage."""

import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from app import chat as chat_mod, compaction, models
from app.chat_writer import (
  ReplaceTranscript,
  StartTurn,
  alloc_run_token,
  await_ack,
  get_writer,
)
from app.config import get_settings


def _make_chat_with_messages(client, auth, messages):
  chat_id = client.post(
    "/api/chats", json={"title": "Compact me"}, headers=auth,
  ).json()["id"]
  client.put(
    f"/api/chats/{chat_id}", json={"messages": messages}, headers=auth,
  )
  return chat_id


def _payload(switch_id="switch-1", provider="codex"):
  return {
    "switch_id": switch_id,
    "provider": provider,
    "agent_settings_json": {
      "model": "gpt-5.4" if provider == "codex" else "claude-opus-4-8",
      "effort": "high",
      "effort_by_provider": {"claude": "medium", "codex": "high"},
    },
  }


def _connect_codex(monkeypatch):
  monkeypatch.setattr(
    "app.providers.ClaudeProvider.check_auth", lambda self, _data_dir: None,
  )
  monkeypatch.setattr(
    "app.providers.CodexProvider.check_auth", lambda self, _data_dir: None,
  )


def _write_summary(chat_id, text):
  note = (
    Path(get_settings().data_dir)
    / "shared" / "memory" / "chats" / chat_id / "index.md"
  )
  note.parent.mkdir(parents=True, exist_ok=True)
  note.write_text(
    "---\ntype: chat\n---\n## Digest\nshort\n\n"
    f"## Summary\n{text}\n\n## Facts & intent\n- private\n",
    encoding="utf-8",
  )


def test_incoming_provider_synthesizes_and_switches_atomically(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)
  source = "Goal: build X. Decision: use the API. Next: wire persistence."
  captured = {}

  async def _stub(messages, **kwargs):
    captured["messages"] = messages
    captured.update(kwargs)
    return "Incoming Codex handoff: build X, then wire persistence."

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "Build me an app"},
    {"role": "assistant", "content": "I scaffolded it."},
  ])
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.provider = "claude"
  row.session_id = "claude-session"
  row.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.commit()
  _write_summary(chat_id, source)

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["protocol"] == "provider-switch-v1"
  assert body["switch_id"] == "switch-1"
  assert captured["provider_id"] == "codex"
  assert captured["model"] == "gpt-5.4"
  assert captured["effort"] == "high"
  assert captured["source_summary"] == source
  assert body["provider"] == "codex"
  assert body["stored"]["switch_id"] == "switch-1"
  assert body["stored"]["from_provider"] == "claude"
  assert body["stored"]["to_provider"] == "codex"

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "codex"
  assert row.session_id is None
  assert row.agent_settings_json["model"] == "gpt-5.4"
  assert row.messages[-1]["content"] == body["summary"]
  assert row.messages[0]["content"] == "Build me an app"
  assert chat_mod._latest_compaction_brief(row) == body["summary"]


def test_legacy_bodyless_compact_then_patch_remains_compatible(
  client, auth, db, monkeypatch,
):
  """A cached pre-PM219 frontend can finish its original two-call flow."""
  _connect_codex(monkeypatch)

  async def _stub(_messages, **_kwargs):
    return "portable legacy handoff"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "keep this context"},
    {"role": "assistant", "content": "I will."},
  ])

  compact = client.post(f"/api/chats/{chat_id}/compact", headers=auth)
  assert compact.status_code == 200, compact.text
  assert compact.json()["stored"]["legacy_switch_ready"] is True
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "claude"

  switched = client.patch(
    f"/api/chats/{chat_id}",
    headers=auth,
    json={
      "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4", "effort": "high"},
    },
  )
  assert switched.status_code == 200, switched.text
  assert switched.json()["provider"] == "codex"
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "codex"
  assert row.session_id is None


def test_switch_atomically_supersedes_park_and_stale_auto_resume(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)

  async def _stub(_messages, **_kwargs):
    return "incoming provider owns this continuation"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "continue after my limit"},
    {"role": "assistant", "content": "paused"},
  ])
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.auto_resume_on_limit = True
  db.add(models.ChatRun(
    id="park-before-switch",
    chat_id=chat_id,
    status="resume_pending",
    provider="claude",
  ))
  db.commit()

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 200, response.text
  db.expire_all()
  parked = db.query(models.ChatRun).filter(
    models.ChatRun.id == "park-before-switch",
  ).one()
  assert parked.status == "interrupted"

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw),
  )
  resumed = asyncio.run(chat_mod._auto_resume_chat(
    chat_id, "claude", park_token="park-before-switch",
  ))
  assert resumed is False
  assert scheduled == []


@pytest.mark.parametrize(
  ("source_provider", "target_provider", "target_model"),
  [
    ("claude", "codex", "gpt-5.4"),
    ("codex", "claude", "claude-opus-4-8"),
  ],
)
def test_next_real_runner_uses_target_fresh_session_and_handoff(
  client, auth, db, monkeypatch,
  source_provider, target_provider, target_model,
):
  """Acceptance evidence for the first real turn in both directions."""
  from app import schemas
  from app.broadcast import create_broadcast

  _connect_codex(monkeypatch)
  monkeypatch.setattr(
    "app.providers.ClaudeProvider.ensure_auth",
    lambda self, _data_dir: asyncio.sleep(0),
  )
  handoff = f"portable handoff for {target_provider}"

  async def _synthesize(_messages, **_kwargs):
    return handoff

  monkeypatch.setattr(compaction, "summarize_chat", _synthesize)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "original request"},
    {"role": "assistant", "content": "original answer"},
  ])
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.provider = source_provider
  row.session_id = f"{source_provider}-session"
  row.agent_settings_json = {
    "model": (
      "claude-sonnet-4-6" if source_provider == "claude" else "gpt-5.4"
    ),
  }
  db.commit()

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch",
    headers=auth,
    json=_payload(
      switch_id=f"{source_provider}-to-{target_provider}",
      provider=target_provider,
    ),
  )
  assert response.status_code == 200, response.text

  captured = {}

  async def _runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "new-session", "cost_usd": 0.0, "error": None}

  runner_path = (
    "app.codex_sdk_runner.run_codex_sdk_turn"
    if target_provider == "codex"
    else "app.claude_sdk_runner.run_claude_sdk_turn"
  )
  monkeypatch.setattr(runner_path, _runner)
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="ACTUAL NEXT REQUEST")],
    chat_id=chat_id,
    session_id=None,
    provider_id=target_provider,
    run_gen=chat_mod.current_run_generation(chat_id),
  ))

  assert captured["session_id"] is None
  assert captured["agent_settings"]["model"] == target_model
  assert "<compacted_chat>" in captured["user_message"]
  assert handoff in captured["user_message"]
  assert "ACTUAL NEXT REQUEST" in captured["user_message"]
  assert captured["user_message"].index(handoff) < captured["user_message"].index(
    "ACTUAL NEXT REQUEST"
  )


def test_synthesis_failure_leaves_provider_session_settings_and_messages(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)

  async def _stub(_messages, **_kwargs):
    raise compaction.CompactionError("Incoming provider is unavailable.")

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.session_id = "old-session"
  row.agent_settings_json = {"model": "claude-sonnet-4-6"}
  before_messages = list(row.messages)
  db.commit()

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 422, response.text
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "claude"
  assert row.session_id == "old-session"
  assert row.agent_settings_json == {"model": "claude-sonnet-4-6"}
  assert row.messages == before_messages


def test_chat_change_during_synthesis_rejects_without_partial_switch(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "original"},
    {"role": "assistant", "content": "answer"},
  ])

  async def _stub(_messages, **_kwargs):
    ack = get_writer().submit(ReplaceTranscript(
      chat_id=chat_id,
      messages=[{"role": "user", "content": "changed concurrently"}],
    ))
    await await_ack(ack)
    return "stale synthesized handoff"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 409
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "claude"
  assert row.messages == [{"role": "user", "content": "changed concurrently"}]


def test_turn_start_during_synthesis_wins_without_partial_switch(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "original"},
    {"role": "assistant", "content": "answer"},
  ])

  async def _stub(_messages, **_kwargs):
    ack = get_writer().submit(StartTurn(
      chat_id=chat_id,
      run_token=alloc_run_token(),
      user_msg={"role": "user", "content": "racing send", "ts": 10},
      title_source="racing send",
      default_provider="claude",
    ))
    await await_ack(ack)
    return "handoff that must not commit"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 409
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "claude"
  assert row.run_status == "running"
  assert row.messages[-1]["content"] == "racing send"
  assert not any(m.get("kind") == "compaction" for m in row.messages)


def test_route_send_waits_for_handoff_then_starts_on_incoming_provider(
  client, auth, db, monkeypatch,
):
  """The public send route cannot slip into the synthesis/commit window."""
  _connect_codex(monkeypatch)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "original"},
    {"role": "assistant", "content": "answer"},
  ])
  synthesis_started = threading.Event()
  finish_synthesis = threading.Event()
  responses = {}

  async def _stub(_messages, **_kwargs):
    synthesis_started.set()
    while not finish_synthesis.is_set():
      await asyncio.sleep(0.01)
    return "incoming provider briefing"

  async def _runner(*_args, **_kwargs):
    return None

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  monkeypatch.setattr("app.routes.chats_stream.run_chat", _runner)

  def switch():
    responses["switch"] = client.post(
      f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
    )

  def send():
    responses["send"] = client.post(
      f"/api/chats/{chat_id}/messages",
      headers=auth,
      json={"content": "continue after switching"},
    )

  switch_thread = threading.Thread(target=switch)
  send_thread = threading.Thread(target=send)
  switch_thread.start()
  assert synthesis_started.wait(timeout=2)
  send_thread.start()
  time.sleep(0.1)
  assert send_thread.is_alive(), "send bypassed the provider transition gate"

  finish_synthesis.set()
  switch_thread.join(timeout=5)
  send_thread.join(timeout=5)
  assert responses["switch"].status_code == 200
  assert responses["send"].status_code == 202

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "codex"
  assert row.messages[-2]["kind"] == "compaction"
  assert row.messages[-1]["content"] == "continue after switching"


def test_retry_with_same_switch_id_is_idempotent(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)
  calls = 0

  async def _stub(_messages, **_kwargs):
    nonlocal calls
    calls += 1
    return "one handoff"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
  payload = _payload(switch_id="stable-retry")
  first = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=payload,
  )
  owner = db.query(models.Owner).first()
  owner.provider = "claude"
  db.commit()
  second = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=payload,
  )
  assert first.status_code == second.status_code == 200
  assert calls == 1
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  markers = [m for m in row.messages if m.get("kind") == "compaction"]
  assert len(markers) == 1
  assert db.query(models.Owner).first().provider == "codex"

  changed = _payload(switch_id="stable-retry")
  changed["agent_settings_json"]["model"] = "gpt-5.5"
  mismatch = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=changed,
  )
  assert mismatch.status_code == 409
  assert calls == 1


def test_summary_created_during_synthesis_forces_retry(
  client, auth, db, monkeypatch,
):
  _connect_codex(monkeypatch)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])

  async def _stub(_messages, **_kwargs):
    _write_summary(chat_id, "new complete running summary")
    return "transcript-only handoff"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 409
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "claude"
  assert not any(m.get("kind") == "compaction" for m in row.messages)


def test_busy_chat_rejects_before_synthesis(client, auth, db, monkeypatch):
  _connect_codex(monkeypatch)
  called = False

  async def _stub(_messages, **_kwargs):
    nonlocal called
    called = True
    return "must not run"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.run_status = "running"
  db.commit()
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 409
  assert called is False


def test_disconnected_target_rejects_before_synthesis(
  client, auth, monkeypatch,
):
  monkeypatch.setattr(
    "app.providers.CodexProvider.check_auth",
    lambda self, _data_dir: "Codex is not connected.",
  )
  called = False

  async def _stub(_messages, **_kwargs):
    nonlocal called
    called = True
    return "must not run"

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=_payload(),
  )
  assert response.status_code == 409
  assert called is False


def test_switch_requires_coherent_target_settings(client, auth):
  chat_id = _make_chat_with_messages(client, auth, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
  missing = client.post(
    f"/api/chats/{chat_id}/provider-switch",
    headers=auth,
    json={
      "switch_id": "missing-settings",
      "provider": "codex",
      "agent_settings_json": {},
    },
  )
  assert missing.status_code == 422

  mismatch = _payload(switch_id="wrong-model")
  mismatch["agent_settings_json"]["model"] = "claude-sonnet-4-6"
  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json=mismatch,
  )
  assert response.status_code == 422


def test_build_transcript_text_skips_handoffs_and_tail_caps():
  messages = [
    {"role": "user", "content": "first"},
    {"role": "assistant", "kind": "compaction", "content": "derived"},
    {"role": "assistant", "content": "second"},
  ]
  text = compaction.build_transcript_text(messages)
  assert "USER: first" in text
  assert "ASSISTANT: second" in text
  assert "derived" not in text

  big = [{
    "role": "user",
    "content": "x" * (compaction._MAX_TRANSCRIPT_CHARS + 100),
  }]
  capped = compaction.build_transcript_text(big)
  assert len(capped) == compaction._MAX_TRANSCRIPT_CHARS


def test_cumulative_chat_summary_is_unbounded_compaction_source(tmp_path):
  note = tmp_path / "shared" / "memory" / "chats" / "c1" / "index.md"
  note.parent.mkdir(parents=True)
  early = "EARLY DECISION " + ("x" * 70_000)
  late = "LATE NEXT STEP"
  note.write_text(
    "---\ntype: chat\ndescription: work\n---\n"
    "## Digest\nshort paragraph\n\n"
    f"## Summary\n{early}\n{late}\n\n"
    "## Facts & intent\n- private fact\n",
    encoding="utf-8",
  )
  summary = compaction.load_cumulative_summary(str(tmp_path), "c1")
  assert summary is not None
  assert early in summary
  assert late in summary
  assert "private fact" not in summary


def test_codex_agent_text_reads_completed_message():
  stdout = (
    b'{"type":"thread.started","thread_id":"t"}\n'
    b'{"type":"item.completed","item":{"type":"agent_message",'
    b'"text":"portable handoff"}}\n'
  )
  assert compaction._codex_agent_text(stdout) == "portable handoff"


@pytest.mark.asyncio
async def test_synthesis_uses_summary_and_current_transcript(
  monkeypatch, tmp_path,
):
  captured = {}

  class _Provider:
    def check_auth(self, _data_dir):
      return None

    async def ensure_auth(self, _data_dir):
      return None

  async def _turn(prompt, **kwargs):
    captured["prompt"] = prompt
    captured.update(kwargs)
    return "portable briefing"

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", _turn)
  result = await compaction.summarize_chat(
    [
      {"role": "user", "content": "latest question"},
      {"role": "assistant", "content": "latest answer"},
      {"role": "assistant", "kind": "compaction", "content": "derived"},
    ],
    data_dir=str(tmp_path),
    provider_id="codex",
    source_summary="complete early history",
    model="gpt-5.4",
    effort="high",
  )
  assert result == "portable briefing"
  assert "complete early history" in captured["prompt"]
  assert "latest question" in captured["prompt"]
  assert "latest answer" in captured["prompt"]
  assert "derived" not in captured["prompt"]


@pytest.mark.asyncio
async def test_large_synthesis_progressively_reads_every_source_interval(
  monkeypatch, tmp_path,
):
  prompts = []

  class _Provider:
    def check_auth(self, _data_dir):
      return None

    async def ensure_auth(self, _data_dir):
      return None

  async def _turn(prompt, **_kwargs):
    prompts.append(prompt)
    return f"portable briefing revision {len(prompts)}"

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", _turn)
  result = await compaction.summarize_chat(
    [
      {"role": "user", "content": "EARLY_TRANSCRIPT " + "a" * 55_000},
      {"role": "assistant", "content": "MIDDLE_TRANSCRIPT " + "b" * 55_000},
      {"role": "user", "content": "LATEST_TRANSCRIPT " + "c" * 55_000},
    ],
    data_dir=str(tmp_path),
    provider_id="codex",
    source_summary="STALE_RUNNING_SUMMARY",
  )

  assert len(prompts) > 1
  assert len(prompts) <= compaction._MAX_SYNTHESIS_CALLS
  assert all(
    len(prompt.encode("utf-8")) <= compaction._MAX_SYNTHESIS_PROMPT_BYTES
    for prompt in prompts
  )
  all_prompts = "\n".join(prompts)
  assert "STALE_RUNNING_SUMMARY" in all_prompts
  assert "EARLY_TRANSCRIPT" in all_prompts
  assert "MIDDLE_TRANSCRIPT" in all_prompts
  assert "LATEST_TRANSCRIPT" in all_prompts
  assert result == f"portable briefing revision {len(prompts)}"


def test_utf8_chunks_are_byte_bounded_and_lossless():
  source = ("plain🙂漢字" * 100) + "final"
  chunks = compaction._utf8_chunks(source, 37)
  assert "".join(chunks) == source
  assert all(len(chunk.encode("utf-8")) <= 37 for chunk in chunks)


@pytest.mark.asyncio
async def test_synthesis_rejects_source_above_call_budget(monkeypatch, tmp_path):
  called = False

  class _Provider:
    def check_auth(self, _data_dir):
      return None

    async def ensure_auth(self, _data_dir):
      return None

  async def _turn(_prompt, **_kwargs):
    nonlocal called
    called = True
    return "brief"

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", _turn)
  with pytest.raises(compaction.CompactionError, match="too large"):
    await compaction.summarize_chat(
      [],
      data_dir=str(tmp_path),
      provider_id="codex",
      source_summary="x" * compaction._MAX_SYNTHESIS_SOURCE_BYTES,
    )
  assert called is False


@pytest.mark.asyncio
async def test_progressive_synthesis_has_overall_deadline(monkeypatch, tmp_path):
  class _Provider:
    def check_auth(self, _data_dir):
      return None

    async def ensure_auth(self, _data_dir):
      return None

  async def _turn(_prompt, **_kwargs):
    await asyncio.sleep(1)
    return "too late"

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", _turn)
  monkeypatch.setattr(compaction, "_SYNTHESIS_TOTAL_TIMEOUT_SECS", 0.01)
  with pytest.raises(compaction.CompactionError, match="overall time limit"):
    await compaction.summarize_chat(
      [{"role": "user", "content": "hello"}],
      data_dir=str(tmp_path),
      provider_id="codex",
    )


@pytest.mark.asyncio
async def test_claude_synthesis_rejects_partial_text_on_error_terminal(
  monkeypatch, tmp_path,
):
  from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, TextBlock,
  )

  messages = [
    AssistantMessage(
      content=[TextBlock(text="partial, unsafe to commit")],
      model="claude-opus",
      session_id="synth-session",
    ),
    ResultMessage(
      subtype="error_during_execution",
      duration_ms=10,
      duration_api_ms=5,
      is_error=True,
      num_turns=1,
      session_id="synth-session",
      stop_reason="error",
      total_cost_usd=0.01,
      usage={"input_tokens": 1, "output_tokens": 2},
    ),
  ]

  class _Provider:
    def build_env(self, **_kwargs):
      return {}

  class _Client:
    disconnected = False

    async def connect(self):
      return None

    async def query(self, _prompt):
      return None

    async def receive_response(self):
      for message in messages:
        yield message

    async def disconnect(self):
      self.disconnected = True

  client = _Client()
  monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", lambda **_kw: {})
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: client)
  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())

  with pytest.raises(compaction.CompactionError, match="could not compact"):
    await compaction._run_claude_summarize_turn(
      "prompt", data_dir=str(tmp_path), model=None, effort=None,
    )
  assert client.disconnected is True


@pytest.mark.asyncio
async def test_claude_synthesis_requires_terminal_result(monkeypatch, tmp_path):
  from claude_agent_sdk.types import AssistantMessage, TextBlock

  class _Provider:
    def build_env(self, **_kwargs):
      return {}

  class _Client:
    async def connect(self):
      return None

    async def query(self, _prompt):
      return None

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text="unterminated")],
        model="claude-opus",
        session_id="synth-session",
      )

    async def disconnect(self):
      return None

  monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", lambda **_kw: {})
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: _Client())
  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())

  with pytest.raises(compaction.CompactionError, match="terminal result"):
    await compaction._run_claude_summarize_turn(
      "prompt", data_dir=str(tmp_path), model=None, effort=None,
    )


@pytest.mark.asyncio
async def test_codex_synthesis_disables_tools_and_isolates_cwd(
  monkeypatch, tmp_path,
):
  captured = {}

  class _Provider:
    def build_env(self, **_kwargs):
      return {}

  class _Process:
    returncode = 0
    pid = 123

    async def communicate(self, _stdin=None):
      return (
        b'{"type":"item.completed","item":'
        b'{"type":"agent_message","text":"brief"}}\n',
        b"",
      )

  async def _spawn(*cmd, **kwargs):
    captured["cmd"] = cmd
    captured["cwd"] = kwargs["cwd"]
    return _Process()

  monkeypatch.setattr(compaction.shutil, "which", lambda _name: "/bin/codex")
  monkeypatch.setattr("app.providers.get_provider", lambda _pid: _Provider())
  monkeypatch.setattr(compaction.asyncio, "create_subprocess_exec", _spawn)
  result = await compaction._run_codex_summarize_turn(
    "prompt", data_dir=str(tmp_path), model="gpt-5.4", effort="high",
  )
  assert result == "brief"
  assert captured["cwd"] != str(tmp_path)
  assert "--ignore-rules" in captured["cmd"]
  disabled = {
    captured["cmd"][index + 1]
    for index, value in enumerate(captured["cmd"][:-1])
    if value == "--disable"
  }
  assert {"shell_tool", "unified_exec", "apps", "browser_use"} <= disabled


def test_latest_compaction_brief_reads_newest_portable_seed():
  row = SimpleNamespace(messages=[
    {"role": "assistant", "kind": "compaction", "content": "old"},
    {"role": "user", "content": "continue"},
    {"role": "assistant", "kind": "compaction", "content": "new"},
  ])
  assert chat_mod._latest_compaction_brief(row) == "new"
