"""Detect files an agent wrote to its chat-private output directory.

Files that look like user-facing deliverables (a PDF, spreadsheet, chart image,
...) are offered as real downloads instead of the model's bare-text claim.

PDFs in particular are almost never produced via a file-editing tool (those
write text) — they're a side effect of a shell-executed script. Each runner
provides ``MOBIUS_GENERATED_DIR`` and an instruction to place only finished
deliverables there. Detection is a before/after diff of that directory rather
than a parse of arbitrary shell commands.

Why this walks the tree itself instead of reusing workspace_files.list_entries:
that helper bounds ONE API response for display (`LIST_LIMIT = 1000`) and
reports the cut with a `truncated` flag. Borrowing it made the diff silently
partial on any install whose data_dir holds more than a thousand files — the
deliverable simply fell outside the window and was never detected, and a
pre-existing unrelated file could ENTER the window between the two snapshots
and be recorded as this chat's deliverable. A diff needs completeness
semantics a display listing does not owe it, so the walk below is explicit
about them: it is either complete, or it reports itself incomplete and the
diff refuses to publish anything (see `Snapshot.complete`).

Security note: the observed directory is structurally owned by one chat under
``data_dir/chats/<chat>/generated``. This replaces the earlier shared-cwd
reservation scheme: slow or overlapping provider tools cannot make one chat's
file enter another chat's scan. This module still only produces *candidates*.
Whether a
candidate becomes something a browser can fetch is decided entirely by
routes/generated_files.py, which looks up a requested name in the specific
chat's OWN recorded rows rather than resolving any client-supplied path
against cwd (a non-delegated chat's cwd is data_dir, a root shared by every
other chat and by credential paths). See that module's docstring.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

log = logging.getLogger(__name__)

# Deliverable file types worth surfacing as a download. Deliberately excludes
# source/config/cache extensions (.py, .json, .log, .pyc, ...) so an agent's
# own working files never show up as spurious "downloads".
ALLOWED_EXTENSIONS = frozenset({
  ".pdf",
  ".doc", ".docx", ".odt",
  ".xls", ".xlsx", ".ods", ".csv",
  ".ppt", ".pptx", ".odp",
  ".png", ".jpg", ".jpeg", ".gif", ".svg",
  ".zip",
  ".mp3", ".wav",
  ".mp4",
})

# Directory names never descended into: build/cache noise (scan cost) plus the
# credential paths delegations.py's normalize_cwd forbids a delegation cwd
# from targeting, so they can never become a candidate in the first place.
HIDDEN_DIRS = frozenset({
  ".git", "node_modules", "__pycache__", ".venv", "venv",
  "dist", "build", ".next", ".cache",
  "cli-auth",
})

# Tool names that cannot write to cwd. This is purely a cost hint — skipping
# them avoids a cwd reservation plus two tree walks per call — so a name
# missing from the set costs a wasted scan, never correctness: the diff still
# proves what actually changed.
READ_ONLY_TOOLS = frozenset({
  "read", "glob", "grep", "websearch", "webfetch",
  "web_search", "web_fetch", "list_agent_peers",
})


def output_dir(data_dir: str, chat_id: str, *, create: bool = False) -> Path:
  """Return this chat's private generated-deliverable directory.

  Generated files deliberately live under a chat-owned namespace rather than
  the provider cwd, which is commonly the shared data root. This makes file
  provenance structural: another chat cannot enter this detector's scan even
  when both providers run tools concurrently.
  """
  chat_segment = quote(str(chat_id), safe="._-")
  if not chat_segment:
    raise ValueError("empty chat id for generated-file directory")
  directory = Path(data_dir) / "chats" / chat_segment / "generated"
  if create:
    directory.mkdir(parents=True, exist_ok=True)
  return directory


def delivery_instruction(directory: Path) -> str:
  """Provider instruction for the one directory the detector observes."""
  return (
    "When you create a final user-facing deliverable such as a PDF, document, "
    "spreadsheet, presentation, image, archive, audio, or video file, save the "
    f"finished file in {directory}. That path is also available as "
    "$MOBIUS_GENERATED_DIR. Keep temporary and source files outside it."
  )


def is_read_only_tool(tool_name: str | None) -> bool:
  """True for a tool whose call cannot leave a new deliverable behind.

  Both providers namespace their tool names — Claude as
  `mcp__<server>__<tool>`, Codex as `<server>:<tool>` — and the bare name
  lives in the last segment of either shape.
  """
  if not tool_name:
    return False
  bare = str(tool_name).rsplit("__", 1)[-1].rsplit(":", 1)[-1]
  return bare.strip().lower() in READ_ONLY_TOOLS


# A candidate above this size is not recorded: it protects against a runaway
# script ballooning the chat's stored metadata or being served as an oversized
# download. Mirrors uploads.py's MAX_UPLOAD_MB default.
MAX_RECORDED_BYTES = 100 * 1024 * 1024

# Cap on new candidates recorded per single tool-call diff, so a script that
# writes thousands of small files in one call can't flood one chat.
MAX_CANDIDATES_PER_DIFF = 20

# Ceiling on rows one chat may accumulate. Unlike uploads — which a person
# adds by hand — these are agent-driven and otherwise unbounded across a long
# chat, and every insert probes existing names for a basename collision. See
# chat_writer._record_generated_file.
MAX_RECORDED_ROWS_PER_CHAT = 500

# Ceiling on directory entries ONE snapshot will look at. Unlike the display
# listing's cap this is not a silent truncation: crossing it marks the
# snapshot incomplete and the diff then publishes nothing, so a pathological
# tree degrades to "no detection" (visible in the log) rather than to
# "detection that quietly invents or misses deliverables". Generous enough
# that an ordinary data_dir never reaches it.
MAX_SCAN_ENTRIES = 200_000


class Snapshot(NamedTuple):
  """One scan of cwd's allowlisted files.

  `complete` is False when the walk hit MAX_SCAN_ENTRIES. A diff involving an
  incomplete snapshot is not sound — the cut point can move between two scans,
  which both hides real deliverables and lets unrelated pre-existing files
  look new — so `diff_new_or_changed` refuses to publish from one.
  """

  files: dict[str, tuple[int, int]]
  complete: bool


def _is_other_chat_dir(relative_parts: tuple[str, ...], own_chat_id: str) -> bool:
  """True for `.../chats/<someone-else's-id>` — never descended into.

  Every per-chat namespace in this codebase (a chat's uploads, its memory
  state) lives under a `chats/<chat_id>/` path. Skipping other chats' subtrees
  during the walk is both a cross-chat correctness guarantee and the single
  biggest scan saving on an install with many chats.
  """
  return (
    len(relative_parts) >= 2
    and relative_parts[-2] == "chats"
    and relative_parts[-1] != own_chat_id
  )


def snapshot(cwd: str, *, own_chat_id: str) -> Snapshot:
  """Returns {relative_path: (mtime_ns, size)} for cwd's allowlisted files.

  Only allowlisted extensions are stat'ed — the walk reads directory entries
  and checks the name's suffix first — so the map stays small and the cost
  stays close to the tree's readdir cost even under a large data_dir.
  Symlinks are never followed or recorded (a symlinked path could otherwise
  resolve outside cwd), and a missing or unreadable cwd yields an empty,
  incomplete snapshot rather than raising: detection must never fail the turn
  it is observing.
  """
  root = Path(cwd)
  files: dict[str, tuple[int, int]] = {}
  scanned = 0
  stack: list[tuple[str, tuple[str, ...]]] = [(str(root), ())]
  while stack:
    folder, relative_parts = stack.pop()
    try:
      with os.scandir(folder) as entries:
        for entry in entries:
          scanned += 1
          if scanned > MAX_SCAN_ENTRIES:
            log.warning(
              "generated-file scan exceeded %d entries under %s; "
              "skipping detection for this call",
              MAX_SCAN_ENTRIES, cwd,
            )
            return Snapshot(files={}, complete=False)
          try:
            if entry.is_symlink():
              continue
            if entry.is_dir(follow_symlinks=False):
              if entry.name in HIDDEN_DIRS:
                continue
              child_parts = (*relative_parts, entry.name)
              if _is_other_chat_dir(child_parts, own_chat_id):
                continue
              stack.append((entry.path, child_parts))
              continue
            if not entry.is_file(follow_symlinks=False):
              continue
            if os.path.splitext(entry.name)[1].lower() not in ALLOWED_EXTENSIONS:
              continue
            stat = entry.stat(follow_symlinks=False)
          except OSError:
            # A file vanishing mid-walk (the agent's own script cleaning up
            # after itself) is ordinary, not a scan failure.
            continue
          files["/".join((*relative_parts, entry.name))] = (
            stat.st_mtime_ns, stat.st_size,
          )
    except OSError:
      # An unreadable directory does not invalidate the rest of the walk.
      continue
  return Snapshot(files=files, complete=True)


def snapshot_output_dir(data_dir: str, *, chat_id: str) -> Snapshot:
  """Snapshot only one chat's generated directory, keyed from data_dir.

  Persisted paths stay relative to ``data_dir`` so the download route can
  resolve them without accepting an absolute path from either provider.
  """
  directory = output_dir(data_dir, chat_id)
  snap = snapshot(str(directory), own_chat_id=chat_id)
  prefix = directory.relative_to(Path(data_dir)).as_posix()
  return Snapshot(
    files={f"{prefix}/{path}": value for path, value in snap.files.items()},
    complete=snap.complete,
  )


def _belongs_to_other_chat(path: str, own_chat_id: str) -> bool:
  """True when `path` sits under a DIFFERENT chat's own namespace.

  `snapshot` already refuses to descend into those subtrees; this is the
  defense-in-depth half, applied to whatever a snapshot actually returned.
  Ordinary chats all share cwd == settings.data_dir (the root every chat's
  own state lives under), so without both halves a concurrently running chat
  B's file could be attributed to chat A's turn and served by A's scoped
  download route.
  """
  parts = Path(path).parts
  for index, part in enumerate(parts[:-1]):
    if part == "chats" and parts[index + 1] != own_chat_id:
      return True
  return False


def diff_new_or_changed(
  before: Snapshot, after: Snapshot, *, own_chat_id: str,
) -> list[dict]:
  """Returns allowlisted files that are new or changed between two snapshots.

  Publishes nothing unless BOTH snapshots are complete: a diff against a
  partial view invents deliverables as readily as it misses them, and a
  wrongly recorded row is downloadable, so this fails closed.

  `own_chat_id` is required (not optional) so a call site cannot silently
  skip the cross-chat exclusion by omission. Bounded to
  MAX_CANDIDATES_PER_DIFF entries and MAX_RECORDED_BYTES each; sorted so a
  truncated batch is the same batch on every run rather than dict-order luck.
  """
  if not before.complete or not after.complete:
    return []
  candidates: list[dict] = []
  for path, fingerprint in sorted(after.files.items()):
    if before.files.get(path) == fingerprint:
      continue
    if _belongs_to_other_chat(path, own_chat_id):
      continue
    if fingerprint[1] > MAX_RECORDED_BYTES:
      continue
    candidates.append({
      "name": Path(path).name,
      "path": path,
      "size": fingerprint[1],
      "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
    })
    if len(candidates) >= MAX_CANDIDATES_PER_DIFF:
      break
  return candidates
