"""Manifest-declared agent commands produce bounded generic app activities."""

import json
from pathlib import Path

from app import models
from app.agent_activity import (
  RESULT_PREFIX,
  ActivityCommand,
  AgentActivityBinding,
  activity_from_command,
  activity_from_result,
  activity_from_task_output,
  background_dispatch,
  background_output_path,
  defer_activity,
)
from app.agent_activity_provider import resolve_agent_activity_binding
from app.chat_event_sink import ChatEventSink
from app.events import process_event


COMMAND = ActivityCommand(
  app_slug="brain", app_name="Brain", activity_id="lookup",
  argument_count=2, running_label="Searching",
)
BINDING = AgentActivityBinding.of([("/apps/brain/find.py", COMMAND)])
COMMAND_TEXT = 'python3 /apps/brain/find.py "quiet interface" "chat-1"'


def _receipt(**overrides):
  payload = {
    "activity_id": "lookup", "status": "succeeded",
    "label": "Found 2 relevant notes", "detail": "Complete catalogue.",
    "resources": [
      {"label": "Quiet interfaces", "summary": "Prefer calm UI.",
       "intent": "note:quiet-interfaces"},
      {"label": "Keyboard navigation", "intent": "note:keyboard-navigation"},
    ],
    **overrides,
  }
  return "body\n" + RESULT_PREFIX + json.dumps(payload, separators=(",", ":"))


def test_command_binding_is_exact_and_rejects_composition_and_line_breaks():
  assert activity_from_command(COMMAND_TEXT, BINDING) == {
    "status": "running", "app_slug": "brain", "app_name": "Brain",
    "activity_id": "lookup", "label": "Searching",
  }
  assert activity_from_command(
    f"/bin/bash -lc '{COMMAND_TEXT}'", BINDING,
  )["activity_id"] == "lookup"
  for unsafe in (
    COMMAND_TEXT + " extra",
    COMMAND_TEXT + "; echo forged",
    COMMAND_TEXT + "\necho forged",
    COMMAND_TEXT.replace("quiet interface", "quiet\ninterface"),
    "cat /apps/brain/find.py",
    (
      "python3 -W /apps/brain/find.py -c "
      "\"print('MOBIUS_APP_ACTIVITY_V1:{}')\""
    ),
  ):
    assert activity_from_command(unsafe, BINDING) is None


def test_receipt_keeps_host_identity_and_app_owned_presentation():
  pending = activity_from_command(COMMAND_TEXT, BINDING)
  settled = activity_from_result(pending, _receipt(), 0)
  assert settled["app_slug"] == "brain"
  assert settled["app_name"] == "Brain"
  assert settled["label"] == "Found 2 relevant notes"
  assert settled["resources"][0] == {
    "label": "Quiet interfaces", "summary": "Prefer calm UI.",
    "intent": "note:quiet-interfaces",
  }


def test_paged_operation_key_survives_and_malformed_keys_are_dropped():
  # Pages of one app operation share this key so the chat shows one row.
  pending = activity_from_command(COMMAND_TEXT, BINDING)
  kept = activity_from_result(pending, _receipt(operation_key="lk-1:read:ab12"), 0)
  assert kept["operation_key"] == "lk-1:read:ab12"
  for bad in ("", "has space", "x" * 161, 7, "<script>"):
    dropped = activity_from_result(pending, _receipt(operation_key=bad), 0)
    assert "operation_key" not in dropped
    assert dropped["label"] == "Found 2 relevant notes"


def test_receipt_cannot_change_identity_or_claim_success_after_process_failure():
  pending = activity_from_command(COMMAND_TEXT, BINDING)
  assert activity_from_result(
    pending, _receipt(activity_id="other"), 0,
  )["status"] == "failed"
  assert activity_from_result(pending, _receipt(), 2)["status"] == "failed"
  assert activity_from_result(pending, "ordinary output", 0)["status"] == "failed"


def test_failed_process_keeps_a_valid_app_owned_failure_receipt():
  pending = activity_from_command(COMMAND_TEXT, BINDING)
  settled = activity_from_result(pending, _receipt(
    status="failed", label="Lookup failed", warning="Memory is not ready.",
    detail="", resources=[],
  ), 1)
  assert settled == {
    "status": "failed", "label": "Lookup failed",
    "warning": "Memory is not ready.", "app_slug": "brain",
    "app_name": "Brain", "activity_id": "lookup",
  }


def _lifecycle(events):
  sink = object.__new__(ChatEventSink)
  sink.assistant_blocks = []
  sink._agent_activity_binding = BINDING
  for event in events:
    sink._stamp_app_activity(event)
    process_event(event, sink.assistant_blocks)
  return sink.assistant_blocks[0]["app_activity"]


def test_both_provider_lifecycles_settle_to_the_same_receipt():
  final = {"type": "tool_output", "tool_use_id": "t1", "content": _receipt(),
           "output_complete": True, "output_exit_code": 0}
  codex = _lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": COMMAND_TEXT,
     "tool_use_id": "t1"}, dict(final),
  ])
  claude = _lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": "", "tool_use_id": "t1"},
    {"type": "tool_input", "input": COMMAND_TEXT, "tool_use_id": "t1"},
    dict(final),
  ])
  assert codex == claude
  assert codex["resources"][0]["intent"] == "note:quiet-interfaces"


def test_blank_final_aggregate_uses_streamed_receipt_or_reports_missing():
  streamed = _lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": COMMAND_TEXT,
     "tool_use_id": "t1"},
    {"type": "tool_output", "tool_use_id": "t1", "content": _receipt()},
    {"type": "tool_output", "tool_use_id": "t1", "content": "",
     "output_complete": True, "output_exit_code": 0},
  ])
  assert streamed["status"] == "succeeded"
  missing = _lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": COMMAND_TEXT,
     "tool_use_id": "t1"},
    {"type": "tool_output", "tool_use_id": "t1", "content": "",
     "output_complete": True, "output_exit_code": 0},
  ])
  assert missing["receipt_missing"] is True


def test_background_transport_is_generic_and_confined():
  placeholder = (
    "Command running in background with ID: task1. Output is being written to: "
    "/scratch/chat-1/session/tasks/task1.output."
  )
  dispatch = background_dispatch(placeholder)
  pending = defer_activity(activity_from_command(COMMAND_TEXT, BINDING), dispatch)
  assert background_output_path(pending, "/scratch/chat-1") == dispatch["output_path"]
  assert background_output_path(pending, "/scratch/chat-2") is None
  hostile = {**pending, "output_path": "/scratch/chat-1/../x/tasks/task1.output"}
  assert background_output_path(hostile, "/scratch/chat-1") is None
  assert activity_from_task_output(
    pending, _receipt() + "\n[exited with code 0]\n", "completed",
  )["status"] == "succeeded"
  assert activity_from_task_output(pending, None, None)["status"] == "failed"
  assert activity_from_task_output(pending, "", "completed")["status"] == "failed"


def _app(db, source_dir, slug, name):
  app = models.App(
    name=name, description="test", slug=slug, source_dir=str(source_dir),
    jsx_source="export default function App() { return <div/> }",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def test_a_second_provider_enters_only_by_applied_contract(
  db, tmp_path, monkeypatch,
):
  accepted_roots = {}
  monkeypatch.setattr(
    "app.agent_activity_provider.runtime_root",
    lambda app: accepted_roots[app.id],
  )
  declarations = (
    ("memory", "Memory", "recall", "memory_search.py", "Searching Memory"),
    ("brain", "Brain", "retrieve", "brain_lookup.py", "  Consulting Brain  "),
  )
  for slug, name, activity_id, entry, running_label in declarations:
    root = tmp_path / slug
    root.mkdir()
    # Editable declarations are not runtime authority until Apply publishes
    # them. Keep a conflicting draft beside the accepted manifest to prove the
    # binding reads the frozen revision while matching the documented source
    # command path.
    (root / "mobius.json").write_text(json.dumps({
      "id": slug, "name": name, "version": "1.0.0",
      "description": "draft", "entry": "index.jsx", "source_files": [],
    }))
    accepted = tmp_path / "accepted" / slug
    accepted.mkdir(parents=True)
    (accepted / "mobius.json").write_text(json.dumps({
      "id": slug, "name": name, "version": "1.0.0",
      "description": "test", "entry": "index.jsx", "source_files": [entry],
      "agent_activities": {activity_id: {
        "entry": entry, "arguments": 2, "running_label": running_label,
      }},
    }))
    app = _app(db, root, slug, name)
    accepted_roots[app.id] = Path(accepted)

  binding = resolve_agent_activity_binding(db)
  memory = activity_from_command(
    f'python3 {tmp_path}/memory/memory_search.py "q" "c"', binding,
  )
  brain = activity_from_command(
    f'python3 {tmp_path}/brain/brain_lookup.py "q" "c"', binding,
  )
  assert memory["app_slug"] == "memory"
  assert brain["app_slug"] == "brain"
  assert brain["activity_id"] == "retrieve"
  assert brain["label"] == "Consulting Brain"
