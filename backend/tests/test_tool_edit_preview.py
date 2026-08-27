"""Provider edit tools preserve bounded, renderable diff previews."""

from app.codex_events import _tool_start_event
from app.events import process_event
from app.tool_edit_preview import (
  MAX_EDIT_PREVIEW_CHARS,
  claude_edit_preview,
  codex_edit_preview,
)


def test_claude_edit_preview_uses_only_the_supplied_selection(tmp_path):
  path = tmp_path / "hello world.py"
  preview = claude_edit_preview("Edit", {
    "file_path": str(path),
    "old_string": "old value",
    "new_string": "new value",
  })

  assert preview["relative"] is True
  assert preview["truncated"] is False
  assert f'"a/{path}" "b/{path}"' in preview["diff"]
  assert "@@ -1 +1 @@" in preview["diff"]
  assert "-old value\n+new value" in preview["diff"]


def test_claude_multi_edit_preview_keeps_each_selection_as_one_hunk(tmp_path):
  preview = claude_edit_preview("MultiEdit", {
    "file_path": str(tmp_path / "app.py"),
    "edits": [
      {"old_string": "before", "new_string": "after"},
      {"old_string": "left", "new_string": "right"},
    ],
  })

  assert preview["relative"] is True
  assert "-before\n+after" in preview["diff"]
  assert "-left\n+right" in preview["diff"]
  assert preview["diff"].count("@@ -1 +1 @@") == 2


def test_codex_patch_preview_keeps_multiple_file_kinds_and_bounds_payload():
  preview = codex_edit_preview([
    {
      "path": "/data/hello world.py",
      "kind": {"type": "update"},
      "diff": "@@ -1 +1 @@\n-old\n+new",
    },
    {
      "path": "new.py",
      "kind": {"type": "add"},
      "diff": "@@ -0,0 +1 @@\n+hello",
    },
  ])

  assert 'diff --git "a//data/hello world.py"' in preview["diff"]
  assert "new file mode 100644" in preview["diff"]
  assert "diff --git a/new.py b/new.py" in preview["diff"]

  large = codex_edit_preview([{
    "path": "large.txt",
    "kind": {"type": "add"},
    "diff": "@@ -0,0 +1 @@\n+" + ("x" * (MAX_EDIT_PREVIEW_CHARS + 100)),
  }])
  assert large["truncated"] is True
  assert len(large["diff"]) == MAX_EDIT_PREVIEW_CHARS
  assert len(large["_full_diff"]) > len(large["diff"])


def test_preview_quoting_preserves_unicode_paths():
  preview = codex_edit_preview([{
    "path": "/data/café file.py",
    "kind": {"type": "update"},
    "diff": "@@ -1 +1 @@\n-old\n+new",
  }])

  assert '"a//data/café file.py"' in preview["diff"]
  assert "\\u00e9" not in preview["diff"]


def test_codex_file_change_start_carries_shared_edit_preview():
  class FileChangeThreadItem:
    changes = [{
      "path": "src/app.js",
      "kind": {"type": "update"},
      "diff": "@@ -1 +1 @@\n-old\n+new",
    }]

  sdk = {
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": FileChangeThreadItem,
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": type("WebSearchThreadItem", (), {}),
  }

  event = _tool_start_event(FileChangeThreadItem(), sdk)
  assert event["tool"] == "Edit"
  assert event["input"] == "src/app.js"
  assert "-old\n+new" in event["edit_preview"]["diff"]


def test_tool_lifecycle_persists_preview_from_start_and_input_updates():
  first = {"diff": "diff --git a/a b/a", "truncated": False}
  final = {"diff": "diff --git a/a b/a\n@@ -1 +1 @@\n-a\n+b", "truncated": False}
  blocks = []

  process_event({
    "type": "tool_start", "tool": "Edit", "input": "a",
    "tool_use_id": "edit-1", "edit_preview": first,
  }, blocks)
  process_event({
    "type": "tool_input", "input": "a", "tool_use_id": "edit-1",
    "edit_preview": final,
  }, blocks)

  assert blocks[0]["edit_preview"] == final
