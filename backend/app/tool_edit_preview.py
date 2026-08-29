"""Build bounded unified-diff previews for provider edit tools."""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any


# Edit previews live inline with the tool block so they are immediately
# available when its disclosure opens. Keep that durable payload comparable to
# the ordinary bounded tool-output preview rather than copying whole files into
# every transcript.
MAX_EDIT_PREVIEW_CHARS = 20_000


def _bounded_preview(diff: str, *, relative: bool = False) -> dict | None:
  if not diff:
    return None
  truncated = len(diff) > MAX_EDIT_PREVIEW_CHARS
  preview = {
    "diff": diff[:MAX_EDIT_PREVIEW_CHARS],
    "truncated": truncated,
    **({"relative": True} if relative else {}),
  }
  # The transcript and live event keep the bounded preview above. The sink
  # removes this private handoff before either surface sees it and stores the
  # complete text in the existing compressed sidecar table. Only truncated
  # previews need a sidecar; ordinary edits remain one inline value.
  if truncated:
    preview["_full_diff"] = diff
  return preview


def edit_diff_sidecar_id(chat_id: str, tool_use_id: str) -> str:
  """Stable bounded key for one tool's complete edit diff sidecar."""
  digest = hashlib.sha256(
    f"{chat_id}\0{tool_use_id}".encode("utf-8", errors="surrogatepass")
  ).hexdigest()
  return f"edit-diff-{digest}"


def _quoted_path(path: str) -> str:
  if any(char.isspace() or char in {'"', "\\"} for char in path):
    return json.dumps(path, ensure_ascii=False)
  return path


def _git_path(side: str, path: str) -> str:
  # Absolute paths intentionally become a//data/...; the canonical parser
  # removes the a/ or b/ side and preserves the leading slash.
  return _quoted_path(f"{side}/{path}")


def _selection_hunk(edit: dict[str, Any]) -> list[str]:
  old = edit.get("old_string")
  new = edit.get("new_string")
  if not isinstance(old, str) or not isinstance(new, str):
    return []
  # Discard the ---/+++ headers: every selection belongs to the one file entry
  # declared by claude_edit_preview, while each @@ section remains independent.
  return list(difflib.unified_diff(
    old.splitlines(), new.splitlines(), lineterm="",
  ))[2:]


def _content_hunk(content: str, kind_type: str) -> list[str]:
  """Turn Codex's raw add/delete file body into a real unified hunk."""
  if kind_type not in {"add", "delete"} or not content:
    return []
  before = content.splitlines() if kind_type == "delete" else []
  after = content.splitlines() if kind_type == "add" else []
  return list(difflib.unified_diff(before, after, lineterm=""))[2:]


def claude_edit_preview(tool: str, inp: Any) -> dict | None:
  """Build an honest selection-relative preview from Claude edit arguments."""
  if not isinstance(inp, dict) or tool not in {"Edit", "MultiEdit"}:
    return None
  path = inp.get("file_path")
  if not isinstance(path, str) or not path:
    return None
  raw_edits = inp.get("edits") if tool == "MultiEdit" else [inp]
  edits = raw_edits if isinstance(raw_edits, list) else []
  hunks = [
    line
    for edit in edits if isinstance(edit, dict)
    for line in _selection_hunk(edit)
  ]
  if not hunks:
    return None
  header = [
    "diff --git "
    f"{_git_path('a', path)} {_git_path('b', path)}",
    f"--- {_git_path('a', path)}",
    f"+++ {_git_path('b', path)}",
  ]
  return _bounded_preview("\n".join([*header, *hunks]), relative=True)


def codex_edit_preview(changes: Any) -> dict | None:
  """Build a unified preview from Codex FileUpdateChange dictionaries."""
  if not isinstance(changes, list):
    return None
  sections: list[str] = []
  for change in changes:
    if not isinstance(change, dict):
      continue
    path = change.get("path")
    patch = change.get("diff")
    if not isinstance(path, str) or not path or not isinstance(patch, str):
      continue
    raw_kind = change.get("kind")
    kind = raw_kind if isinstance(raw_kind, dict) else {}
    kind_type = str(kind.get("type") or "update")
    # The pinned Codex app-server contract carries complete file content for
    # add/delete changes and unified diff text for updates. Dispatch on that
    # typed distinction before inspecting content: a valid new file may itself
    # begin with diff metadata such as `diff --git`, `@@`, or `GIT binary patch`.
    if kind_type in {"add", "delete"}:
      patch_lines = _content_hunk(patch, kind_type)
    elif patch.startswith("diff --git "):
      sections.append(patch)
      continue
    else:
      patch_lines = patch.splitlines()
    move_path = kind.get("move_path")
    new_path = move_path if isinstance(move_path, str) and move_path else path
    header = [
      "diff --git "
      f"{_git_path('a', path)} {_git_path('b', new_path)}",
    ]
    if kind_type == "add":
      header.extend([
        "new file mode 100644",
        "--- /dev/null",
        f"+++ {_git_path('b', new_path)}",
      ])
    elif kind_type == "delete":
      header.extend([
        "deleted file mode 100644",
        f"--- {_git_path('a', path)}",
        "+++ /dev/null",
      ])
    else:
      if new_path != path:
        header.extend([
          f"rename from {_quoted_path(path)}",
          f"rename to {_quoted_path(new_path)}",
        ])
      header.extend([
        f"--- {_git_path('a', path)}",
        f"+++ {_git_path('b', new_path)}",
      ])
    sections.append("\n".join([*header, *patch_lines]))
  return _bounded_preview("\n".join(sections))
