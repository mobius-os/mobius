"""Compact chat reads keep the transcript light without changing stored truth."""

from app.chat_transcript import compact_messages_for_detail
from sqlalchemy import event


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

  compact = compact_messages_for_detail(messages, message_offset=40)

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
    "sources": [source],
  }
  assert compact[0]["blocks"][2] == {"type": "text", "content": "After"}
  assert messages[0]["blocks"][2]["output"] == "large output"


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
  )
  summary = compact[0]["blocks"][0]

  assert summary["tool_count"] == 101
  assert len(summary["entries"]) == 4
  assert summary["entries"][0]["item"]["duration_ms"] == 300
  assert [
    entry["item"].get("tool")
    for entry in summary["entries"][1:]
  ] == ["Bash", "Bash", "Edit"]


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
  )

  assert compact is messages
  assert compact[0] is single
  assert compact[1] is live


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

  compact = compact_messages_for_detail(messages, message_offset=7)
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
    "pending_messages": [],
    "pending_question_id": None,
  }
  chat_select = next(
    statement
    for statement in statements
    if "from chats" in statement and "chats.pending_messages" in statement
  )
  assert "chats.pending_messages" in chat_select
  assert "chats.messages as" not in chat_select
