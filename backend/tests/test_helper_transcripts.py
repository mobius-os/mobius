"""Opening a helper shows its own conversation, and only to its parent chat."""

import json
from pathlib import Path

from app import helper_transcripts, models
from app.agent_lifecycle import normalize_chat_event, record_event
from app.config import get_settings


def _jsonl(path: Path, records: list[dict]) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("".join(json.dumps(record) + "\n" for record in records))
  return path


def _home(name: str) -> Path:
  return Path(get_settings().data_dir) / "cli-auth" / name


def _claude_sidechain(session: str, agent: str) -> Path:
  return _jsonl(
    _home("claude") / "projects" / "-work" / session / "subagents"
    / f"agent-{agent}.jsonl",
    [
      {"type": "user", "message": {"role": "user", "content": "Review the diff"}},
      {"type": "attachment", "attachment": {"skills": "…"}},
      {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "git diff --stat"}},
      ]}},
      {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": " 2 files changed"}]},
      ]}},
      {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "No blockers."},
      ]}},
    ],
  )


def _codex_rollout(thread: str) -> Path:
  return _jsonl(
    _home("codex") / "sessions" / "2026" / "09" / "24"
    / f"rollout-2026-09-24T14-07-17-{thread}.jsonl",
    [
      {"type": "session_meta", "payload": {"id": thread}},
      # Forked parent context precedes the child's own first task.
      {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "PARENT HISTORY"}]}},
      {"type": "event_msg", "payload": {"type": "task_started"}},
      {"type": "response_item", "payload": {
        "type": "agent_message", "author": "/root",
        "content": [
          {"type": "input_text", "text": (
            "Message Type: NEW_TASK\nTask name: /root/audit_login\n"
            "Sender: /root\nPayload:\n")},
          {"type": "encrypted_content", "encrypted_content": "gAAAA"},
        ]}},
      {"type": "response_item", "payload": {
        "type": "custom_tool_call", "call_id": "c1", "name": "exec",
        "input": 'text(await tools.exec_command({cmd:"git status --short"}))'}},
      {"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": "c1", "output": [
          {"type": "input_text", "text": "Script completed\nOutput:\n"},
          {"type": "input_text", "text": json.dumps(
            {"exit_code": 0, "output": " M app.py"})},
        ]}},
      {"type": "response_item", "payload": {
        "type": "function_call", "call_id": "c2", "namespace": "agents",
        "name": "send_message",
        "arguments": json.dumps({"target": "/root", "message": "gAAAA-secret"})}},
      {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Login audit clean."}]}},
    ],
  )


def _record(db, chat_id, run_id, event):
  values = normalize_chat_event(chat_id=chat_id, chat_run_id=run_id, event=event)
  assert values is not None
  record_event(db, values)


def _seed_parent_with_helpers(db):
  db.add_all([
    models.Chat(id="helper-parent", title="Parent", messages=[]),
    models.Chat(id="helper-stranger", title="Stranger", messages=[]),
  ])
  db.commit()
  for task_id in ("a1claude", "a2gone"):
    _record(db, "helper-parent", None, {
      "type": "task_start", "task_id": task_id, "description": "Review",
      "task_type": "local_agent", "provider_session_id": "sess-1",
    })
  _record(db, "helper-parent", "root-run", {
    "type": "agent_lifecycle", "provider": "codex",
    "provider_session_id": "root-thread", "provider_agent_id": "child-thread",
    "provider_activation_id": "call_abc", "parent_kind": "main",
    "event_type": "agent_started", "state": "running",
    "agent_type": "/root/audit_login", "source": "runner",
  })


def test_a_chat_opens_only_the_helper_conversations_it_recorded(client, auth, db):
  _seed_parent_with_helpers(db)
  _claude_sidechain("sess-1", "a1claude")

  response = client.get("/api/chats/helper-parent/helpers/a1claude", headers=auth)
  assert response.status_code == 200, response.text
  assert response.json() == {
    "provider": "claude",
    "truncated": False,
    "blocks": [
      {"type": "text", "content": "Review the diff", "role": "user"},
      {"type": "tool", "tool": "Bash", "input": "git diff --stat",
       "output": " 2 files changed", "status": "done"},
      {"type": "text", "content": "No blockers."},
    ],
  }

  for chat_id, task_id in (
    ("helper-stranger", "a1claude"),  # another chat's helper
    ("helper-parent", "unknown"),  # never recorded
    ("helper-parent", "a2gone"),  # recorded, but its transcript is gone
  ):
    missing = client.get(f"/api/chats/{chat_id}/helpers/{task_id}", headers=auth)
    assert missing.status_code == 404, (chat_id, task_id)


def test_codex_helper_conversation_is_the_child_rollout_without_forked_history(
  client, auth, db,
):
  _seed_parent_with_helpers(db)
  _codex_rollout("child-thread")

  # A Codex row's task id is its activation, matched through the stored digest.
  response = client.get("/api/chats/helper-parent/helpers/call_abc", headers=auth)
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["provider"] == "codex"
  assert body["blocks"] == [
    {"type": "text", "role": "user", "content": (
      "New task: Audit login — from the main agent. "
      "Codex keeps the message text encrypted.")},
    {"type": "tool", "tool": "Bash", "input": "git status --short",
     "output": " M app.py", "status": "done"},
    {"type": "tool", "tool": "Agent", "input": "send message → the main agent",
     "output": "", "status": "running"},
    {"type": "text", "content": "Login audit clean."},
  ]


def test_huge_transcript_reads_only_its_newest_records(db, monkeypatch):
  _seed_parent_with_helpers(db)
  path = _claude_sidechain("sess-1", "a1claude")
  monkeypatch.setattr(helper_transcripts, "TAIL_BYTES", path.stat().st_size // 2)

  result = helper_transcripts.read_helper_conversation(db, "helper-parent", "a1claude")

  assert result["truncated"] is True
  assert result["blocks"][-1] == {"type": "text", "content": "No blockers."}
  assert "Review the diff" not in json.dumps(result["blocks"])


def _codex_child_working(thread: str, *, answered: bool) -> Path:
  records = [
    {"type": "event_msg", "payload": {"type": "task_started"}},
    {"type": "response_item", "payload": {
      "type": "custom_tool_call", "call_id": "c1", "name": "exec",
      "input": 'text(await tools.exec_command({cmd:"sleep 25"}))'}},
  ]
  if answered:
    records.append({"type": "event_msg", "payload": {
      "type": "task_complete", "last_agent_message": "Login audit clean."}})
  return _jsonl(
    _home("codex") / "sessions" / "2026" / "09" / "25"
    / f"rollout-2026-09-25T03-11-43-{thread}.jsonl",
    records,
  )


def test_codex_helper_activity_is_the_childs_current_tool_and_latest_answer():
  _codex_child_working("child-busy", answered=False)
  assert helper_transcripts.codex_helper_activity("child-busy") == ("Bash", None)

  _codex_child_working("child-done", answered=True)
  assert helper_transcripts.codex_helper_activity("child-done") == (
    "Bash", "Login audit clean.",
  )
  assert helper_transcripts.codex_helper_activity("no-such-child") == (None, None)


def test_codex_rows_gain_names_live_activity_and_rollout_summary():
  from app.codex_events import CodexHelperRows, read_codex_helper_tools

  _codex_child_working("child-row", answered=False)
  rows = CodexHelperRows("host")

  # A reactivation marker carries no agent path, so the row starts generic…
  opened = rows.public_events({
    "event_type": "agent_started", "provider_agent_id": "child-row",
    "provider_activation_id": "status:child-row:1",
  })
  assert opened[-1]["description"] == "Background helper"
  # …and is renamed as soon as any marker for the same child names it.
  named = rows.public_events({
    "event_type": "agent_started", "provider_agent_id": "child-row",
    "provider_activation_id": "status:child-row:1",
    "agent_type": "/root/audit_login",
  })
  assert {event["description"] for event in named} == {"Audit login"}

  running = rows.running_children()
  assert running == [("status:child-row:1", "child-row")]
  tools = read_codex_helper_tools(running)
  assert rows.progress_events(tools) == [{
    "type": "task_progress", "task_id": "status:child-row:1",
    "last_tool_name": "Bash", "tool_use_id": "host",
  }]
  assert rows.progress_events(tools) == []  # unchanged tool: no new tick

  _codex_child_working("child-row", answered=True)
  done = rows.public_events({
    "event_type": "agent_terminal", "state": "done",
    "provider_agent_id": "child-row",
    "provider_activation_id": "status:child-row:1",
  })
  assert done[-1]["summary"] == "Login audit clean."
  assert rows.stranded_events() == []


def test_interrupted_turn_closes_its_still_open_codex_rows():
  from app.codex_events import CodexHelperRows

  rows = CodexHelperRows("host")
  rows.public_events({
    "event_type": "agent_started", "provider_agent_id": "gone",
    "provider_activation_id": "call_1", "agent_type": "/root/fix",
  })

  assert rows.stranded_events() == [{
    "type": "task_done", "task_id": "call_1", "status": "stopped",
    "summary": None, "tool_use_id": "host",
  }]
