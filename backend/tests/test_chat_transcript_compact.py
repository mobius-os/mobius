"""Compact chat reads keep the transcript light without changing stored truth."""

import asyncio

from app import models, questions
from app.chat_transcript import compact_messages_for_detail
from app.pending_questions import PendingQuestion
from app.routes.chats import _chat_detail_window
from sqlalchemy import event
from app.memory_recall import EMPTY_RECALL_BINDING


def test_anchor_window_keeps_predecessor_and_authoritative_tail():
  messages = [
    {"role": "user" if index % 2 == 0 else "assistant", "ts": 1000 + index}
    for index in range(45)
  ]

  page, offset, found = _chat_detail_window(
    messages,
    limit=20,
    before=None,
    anchor_key="assistant-1011",
  )

  assert found is True
  assert offset == 10
  assert page == messages[10:]
  assert page[1] is messages[11]
  assert page[-1] is messages[-1]


def test_anchor_window_accepts_every_durable_row_alias():
  messages = [
    {"role": "assistant", "ts": 900},
    {
      "id": "server-message",
      "cid": "client-message",
      "role": "user",
      "ts": 1000,
    },
    {"role": "assistant", "ts": 1100},
  ]

  for alias in ("server-message", "client-message", "user-1000", "user-1"):
    page, offset, found = _chat_detail_window(
      messages,
      limit=20,
      before=None,
      anchor_key=alias,
    )

    assert found is True, alias
    assert offset == 0, alias
    assert page == messages, alias


def test_missing_anchor_fails_closed_to_the_ordinary_recent_page():
  messages = [{"role": "user", "ts": index} for index in range(45)]

  page, offset, found = _chat_detail_window(
    messages,
    limit=20,
    before=None,
    anchor_key="user-missing",
  )

  assert found is False
  assert offset == 25
  assert page == messages[-20:]


def test_compacts_multi_step_activity_and_preserves_render_metadata():
  source = {"title": "Reference", "url": "https://example.com/reference"}
  messages = [{
    "role": "assistant",
    "content": "Answer",
    "blocks": [
      {"type": "text", "content": "Before"},
      {"type": "thinking", "thinking_id": "thought-1", "duration_ms": 1200},
      {
        "type": "tool",
        "tool": "WebSearch",
        "tool_use_id": "tool-1",
        "status": "done",
        "input": {"query": "large private input"},
        "output": "large output",
        "sources": [source],
        "subagent": {"helper": {"status": "done"}},
      },
      {
        "type": "tool",
        "tool": "Bash",
        "tool_use_id": "tool-2",
        "status": "done",
        "output": "more output",
        "output_exit_code": 0,
      },
      {"type": "text", "content": "After"},
    ],
  }]

  compact = compact_messages_for_detail(
    messages, message_offset=40, binding=EMPTY_RECALL_BINDING,
  )

  assert compact is not messages
  assert compact[0] is not messages[0]
  assert "content" not in compact[0]
  assert compact[0]["blocks"][0] == {"type": "text", "content": "Before"}
  summary = compact[0]["blocks"][1]
  assert summary == {
    "type": "activity",
    "activity_id": "40:1:4",
    "message_index": 40,
    "start": 1,
    "end": 4,
    "tool_count": 2,
    "entries": [
      {
        "item": {
          "type": "thinking",
          "thinking_id": "thought-1",
          "duration_ms": 1200,
        },
        "idx": 1,
      },
      {
        "item": {
          "type": "tool",
          "tool": "WebSearch",
          "status": "done",
          "tool_use_id": "tool-1",
          "subagent": {"helper": {"status": "done"}},
        },
        "idx": 2,
      },
      {
        "item": {
          "type": "tool",
          "tool": "Bash",
          "status": "done",
          "tool_use_id": "tool-2",
          "output_exit_code": 0,
        },
        "idx": 3,
      },
    ],
  }
  assert compact[0]["source_ref"] == {
    "message_index": 40,
    "count": 1,
  }
  assert all("sources" not in block for block in compact[0]["blocks"])
  assert compact[0]["blocks"][2] == {"type": "text", "content": "After"}
  assert messages[0]["blocks"][2]["output"] == "large output"
  assert messages[0]["blocks"][2]["sources"] == [source]


def test_repeated_activity_metadata_is_bounded_by_variety():
  blocks = [
    {"type": "thinking", "duration_ms": 100},
    *[
      {
        "type": "tool",
        "tool": "Bash",
        "status": "done",
        "output": f"step {index}",
      }
      for index in range(100)
    ],
    {"type": "thinking", "duration_ms": 200},
    {"type": "tool", "tool": "Edit", "status": "done"},
  ]

  compact = compact_messages_for_detail(
    [{"role": "assistant", "blocks": blocks}],
    message_offset=0,
    binding=EMPTY_RECALL_BINDING,
  )
  summary = compact[0]["blocks"][0]

  assert summary["tool_count"] == 101
  assert len(summary["entries"]) == 4
  assert summary["entries"][0]["item"]["duration_ms"] == 300
  assert [
    entry["item"].get("tool")
    for entry in summary["entries"][1:]
  ] == ["Bash", "Bash", "Edit"]


def test_context_compaction_stays_between_separate_activity_runs():
  blocks = [
    {"type": "thinking", "duration_ms": 100},
    {"type": "tool", "tool": "Read", "status": "done"},
    {"type": "context_compaction", "provider": "codex"},
    {"type": "thinking", "duration_ms": 200},
    {"type": "tool", "tool": "Bash", "status": "done"},
  ]

  compact = compact_messages_for_detail(
    [{"role": "assistant", "blocks": blocks}],
    message_offset=7,
    binding=EMPTY_RECALL_BINDING,
  )

  assert [block["type"] for block in compact[0]["blocks"]] == [
    "activity", "context_compaction", "activity",
  ]
  assert compact[0]["blocks"][1] == blocks[2]
  assert compact[0]["blocks"][0]["end"] == 2
  assert compact[0]["blocks"][2]["start"] == 3


def test_long_activity_runs_are_split_into_fetchable_ranges():
  blocks = [
    {
      "type": "tool",
      "tool": "Bash",
      "status": "done",
      "output": f"step {index}",
    }
    for index in range(2001)
  ]

  compact = compact_messages_for_detail(
    [{"role": "assistant", "blocks": blocks}],
    message_offset=4,
    binding=EMPTY_RECALL_BINDING,
  )

  assert compact[0]["blocks"] == [
    {
      **compact[0]["blocks"][0],
      "activity_id": "4:0:2000",
      "message_index": 4,
      "start": 0,
      "end": 2000,
      "tool_count": 2000,
    },
    blocks[2000],
  ]
  assert compact[0]["blocks"][0]["type"] == "activity"
  assert compact[0]["blocks"][0]["end"] - compact[0]["blocks"][0]["start"] == 2000


def test_single_activity_and_live_message_remain_self_contained():
  single = {
    "role": "assistant",
    "content": "One step",
    "blocks": [
      {"type": "tool", "tool": "Read", "input": "/tmp/note.txt"},
      {"type": "text", "content": "Done"},
    ],
  }
  live = {
    "role": "assistant",
    "blocks": [
      {"type": "thinking", "content": "working"},
      {"type": "tool", "tool": "Bash", "output": "live"},
    ],
  }
  messages = [single, live]

  compact = compact_messages_for_detail(
    messages,
    message_offset=0,
    live_message=live,
    binding=EMPTY_RECALL_BINDING,
  )

  assert compact is messages
  assert compact[0] is single
  assert compact[1] is live


def test_compact_route_folds_settled_activity_while_live_turn_waits_for_answer(
  client,
  auth,
  db,
  monkeypatch,
):
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Parked question"},
  )
  assert created.status_code == 200
  chat_id = created.json()["id"]
  chat = db.get(models.Chat, chat_id)
  chat.live_assistant = {
    "role": "assistant",
    "ts": 42,
    "blocks": [
      {"type": "thinking", "content": "settled reasoning"},
      {"type": "tool", "tool": "Bash", "output": "settled output"},
      {"type": "question", "question_id": "question-1", "questions": []},
    ],
  }
  db.commit()
  question_loop = asyncio.new_event_loop()
  pending = PendingQuestion(
    question_id="question-1",
    questions=[],
    future=question_loop.create_future(),
  )
  questions.register(chat_id, pending)
  monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _: True)

  try:
    response = client.get(
      f"/api/chats/{chat_id}?limit=20&compact=1",
      headers=auth,
    )
  finally:
    questions.cancel(chat_id)
    question_loop.close()

  assert response.status_code == 200
  payload = response.json()
  assert payload["running"] is True
  assert payload["pending_question_id"] == "question-1"
  assert payload["messages"][0]["blocks"] == [
    {
      "type": "activity",
      "activity_id": "0:0:2",
      "message_index": 0,
      "start": 0,
      "end": 2,
      "entries": [
        {"item": {"type": "thinking"}, "idx": 0},
        {
          "item": {"type": "tool", "tool": "Bash", "status": "done"},
          "idx": 1,
        },
      ],
      "tool_count": 1,
    },
    {"type": "question", "question_id": "question-1", "questions": []},
  ]
  assert "settled reasoning" not in response.text
  assert "settled output" not in response.text


def test_image_reads_stay_distinctive_and_question_twins_are_not_rendered():
  messages = [{
    "role": "assistant",
    "content": "Picked",
    "blocks": [
      {"type": "thinking", "duration_ms": 10},
      {"type": "tool", "tool": "Read", "input": "/tmp/photo.webp"},
      {"type": "tool", "tool": "Bash", "output": "one"},
      {"type": "thinking", "duration_ms": 20},
      {"type": "tool", "tool": "request_user_input", "status": "done"},
      {"type": "question", "question_id": "q1", "questions": []},
    ],
  }]

  compact = compact_messages_for_detail(
    messages, message_offset=7, binding=EMPTY_RECALL_BINDING,
  )
  blocks = compact[0]["blocks"]

  assert blocks[0]["type"] == "thinking"
  assert blocks[1] == messages[0]["blocks"][1]
  assert blocks[2]["type"] == "activity"
  assert blocks[2]["start"] == 2
  assert blocks[2]["end"] == 4
  assert blocks[3]["type"] == "question"
  assert all(
    block.get("tool") != "request_user_input"
    for block in blocks
    if isinstance(block, dict)
  )


def test_skill_reads_stay_distinctive_in_compact_history():
  skill_read = {
    "type": "tool",
    "tool": "Bash",
    "tool_use_id": "cmd-skills",
    "status": "done",
    "skills": ["platform-maintenance", "theming"],
    "input": "cat /data/shared/skills/platform-maintenance.md",
    "output": "skill text",
  }
  messages = [{
    "role": "assistant",
    "blocks": [
      {"type": "thinking", "duration_ms": 10},
      skill_read,
      {"type": "tool", "tool": "Read", "input": "/tmp/note.txt"},
      {"type": "thinking", "duration_ms": 20},
    ],
  }]

  compact = compact_messages_for_detail(
    messages, message_offset=0, binding=EMPTY_RECALL_BINDING,
  )
  blocks = compact[0]["blocks"]

  visible_skill_read = next(
    block for block in blocks
    if isinstance(block, dict) and block.get("tool_use_id") == "cmd-skills"
  )
  assert visible_skill_read == skill_read
  assert all(
    not (
      block.get("type") == "activity"
      and any(
        entry.get("item", {}).get("tool_use_id") == "cmd-skills"
        for entry in block.get("entries", [])
      )
    )
    for block in blocks
    if isinstance(block, dict)
  )


def test_compact_route_defers_activity_detail_until_expansion(client, auth):
  messages = [{
    "role": "assistant",
    "content": "Complete answer",
    "blocks": [
      {"type": "thinking", "content": "private trace", "duration_ms": 500},
      {
        "type": "tool",
        "tool": "Bash",
        "tool_use_id": "tool-full",
        "status": "done",
        "input": "printf hello",
        "output": "hello",
      },
      {"type": "text", "content": "Complete answer"},
    ],
  }]
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Compact route", "messages": messages},
  )
  assert created.status_code == 200
  chat_id = created.json()["id"]

  compact = client.get(
    f"/api/chats/{chat_id}?limit=20&compact=1",
    headers=auth,
  )
  assert compact.status_code == 200
  summary = compact.json()["messages"][0]["blocks"][0]
  assert summary["type"] == "activity"
  assert "content" not in summary["entries"][0]["item"]
  assert "output" not in summary["entries"][1]["item"]

  detail = client.get(
    f"/api/chats/{chat_id}/activity-detail"
    "?message_index=0&start=0&end=2",
    headers=auth,
  )
  assert detail.status_code == 200
  entries = detail.json()["entries"]
  assert entries[0]["item"]["content"] == "private trace"
  assert entries[1]["item"]["output"] == "hello"


def test_compact_route_defers_reference_metadata_until_expansion(client, auth):
  first = {
    "title": "First title",
    "url": "https://example.com/first",
  }
  enriched = {
    "title": "Useful duplicate title",
    "url": "https://example.com/duplicate",
    "snippet": "Context loaded only after expansion.",
  }
  messages = [{
    "role": "assistant",
    "content": "Sourced answer",
    "blocks": [
      {
        "type": "tool",
        "tool": "WebSearch",
        "status": "done",
        "sources": [
          first,
          {
            "title": "https://example.com/duplicate",
            "url": "https://example.com/duplicate",
          },
        ],
      },
      {
        "type": "tool",
        "tool": "WebSearch",
        "status": "done",
        "sources": [enriched, first],
      },
      {"type": "text", "content": "Sourced answer"},
    ],
  }]
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Lazy references", "messages": messages},
  )
  assert created.status_code == 200
  chat_id = created.json()["id"]

  compact = client.get(
    f"/api/chats/{chat_id}?limit=20&compact=1",
    headers=auth,
  )
  assert compact.status_code == 200
  message = compact.json()["messages"][0]
  assert message["source_ref"] == {"message_index": 0, "count": 2}
  assert all(
    "sources" not in block
    for block in message["blocks"]
    if isinstance(block, dict)
  )
  assert "Context loaded only after expansion." not in compact.text

  sources = client.get(
    f"/api/chats/{chat_id}/message-sources?message_index=0",
    headers=auth,
  )
  assert sources.status_code == 200
  assert sources.json() == {"sources": [first, enriched]}
  assert client.get(
    f"/api/chats/{chat_id}/message-sources?message_index=9",
    headers=auth,
  ).status_code == 404


def test_activity_detail_queries_only_candidate_tool_sidecars(
  client,
  auth,
  db,
):
  messages = [{
    "role": "assistant",
    "blocks": [
      {"type": "thinking", "content": "trace"},
      {
        "type": "tool",
        "tool": "Bash",
        "tool_use_id": "tool-candidate",
        "status": "done",
        "output": "preview",
        "output_truncated": True,
      },
    ],
  }]
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Scoped sidecars", "messages": messages},
  )
  chat_id = created.json()["id"]
  statements = []
  engine = db.get_bind()

  def capture_sql(_, __, statement, *args):
    statements.append(statement.lower())

  event.listen(engine, "before_cursor_execute", capture_sql)
  try:
    detail = client.get(
      f"/api/chats/{chat_id}/activity-detail"
      "?message_index=0&start=0&end=2",
      headers=auth,
    )
  finally:
    event.remove(engine, "before_cursor_execute", capture_sql)

  assert detail.status_code == 200
  sidecar_select = next(
    statement
    for statement in statements
    if "from tool_outputs" in statement
  )
  assert "tool_outputs.chat_id =" in sidecar_select
  assert "tool_outputs.tool_use_id in (" in sidecar_select


def test_runtime_route_does_not_select_transcript_json(
  client,
  auth,
  db,
  monkeypatch,
):
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Runtime projection"},
  )
  chat_id = created.json()["id"]
  monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _: True)
  statements = []
  engine = db.get_bind()

  def capture_sql(_, __, statement, *args):
    statements.append(statement.lower())

  event.listen(engine, "before_cursor_execute", capture_sql)
  try:
    runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth)
  finally:
    event.remove(engine, "before_cursor_execute", capture_sql)

  assert runtime.status_code == 200
  assert runtime.json() == {
    "running": True,
    "active_assistant_message_id": None,
    "active_goal_objective": None,
    "goal": None,
    "pending_messages": [],
    "pending_question_id": None,
    "updated_at": created.json()["updated_at"],
    "waits": [],
  }
  chat_select = next(
    statement
    for statement in statements
    if "from chats" in statement and "chats.pending_messages" in statement
  )
  assert "chats.pending_messages" in chat_select
  assert "chats.updated_at" in chat_select
  assert "chats.active_assistant_message_id" in chat_select
  assert "chats.messages as" not in chat_select
  assert not any(
    "json_extract(chats.live_assistant" in statement
    for statement in statements
  )


def test_detail_and_runtime_expose_the_durable_assistant_owner(
  client,
  auth,
  db,
  monkeypatch,
):
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Assistant owner"},
  )
  chat_id = created.json()["id"]
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  chat.active_assistant_message_id = "assistant-current"
  db.commit()
  monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _: True)

  detail = client.get(f"/api/chats/{chat_id}", headers=auth)
  runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth)

  assert detail.status_code == 200
  assert runtime.status_code == 200
  assert detail.json()["active_assistant_message_id"] == "assistant-current"
  assert runtime.json()["active_assistant_message_id"] == "assistant-current"


def test_parked_question_exposes_its_tail_owner_without_live_json(
  client,
  auth,
  db,
):
  """A restart-safe parked card still owns one assistant row after commit.

  This is the real failure topology: an older answered card, a hidden
  continuation boundary, then the current unanswered card. The lightweight
  runtime must name the final row without reading transcript/live JSON.
  """
  created = client.post(
    "/api/chats",
    headers=auth,
    json={"title": "Parked assistant owner"},
  )
  chat_id = created.json()["id"]
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  chat.messages = [
    {"role": "user", "content": "earlier", "ts": 1},
    {
      "id": "assistant-older",
      "role": "assistant",
      "ts": 2,
      "blocks": [{
        "type": "question",
        "question_id": "question-older",
        "questions": [{"id": "old", "question": "Old choice?"}],
        "answers": {"old": "Done"},
      }],
    },
    {
      "role": "user",
      "kind": "wait_result",
      "hidden": True,
      "content": "checks finished",
      "ts": 3,
    },
    {
      "id": "assistant-current",
      "role": "assistant",
      "ts": 4,
      "blocks": [{
        "type": "question",
        "question_id": "question-current",
        "questions": [{"id": "current", "question": "Current choice?"}],
      }],
    },
  ]
  chat.pending_question_id = "question-current"
  chat.live_assistant = None
  chat.active_assistant_message_id = "assistant-current"
  db.commit()

  detail = client.get(f"/api/chats/{chat_id}", headers=auth)
  runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth)

  assert detail.status_code == 200
  assert runtime.status_code == 200
  assert detail.json()["active_assistant_message_id"] == "assistant-current"
  assert runtime.json()["active_assistant_message_id"] == "assistant-current"
  assert runtime.json()["pending_question_id"] == "question-current"
