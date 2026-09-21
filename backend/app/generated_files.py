"""Detects files an agent turn wrote to its cwd that look like a deliverable
the user asked for (a PDF, a spreadsheet, a chart image, ...) so they can be
offered as a real download instead of the model's own bare-text claim.

PDFs in particular are almost never produced via the Write tool (which writes
text) — they're a side effect of a Bash-executed script (reportlab, weasyprint,
pandoc, ...). Detection is therefore a before/after directory diff around
mutating tool calls (Write/Edit/MultiEdit/Bash/apply_patch), not a parse of
any single tool's arguments. See claude_sdk_runner.py's generated_file_hook
and codex_sdk_runner.py's equivalent for the call sites.

Security note: this module only ever produces *candidates*. Whether a
candidate becomes something a browser can fetch is decided entirely by
routes/generated_files.py, which looks up a requested name in the specific
chat's OWN recorded list rather than resolving any client-supplied path
against cwd (a non-delegated chat's cwd is /data, a root shared by every
other chat and by credential paths). See that module's docstring.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from app import workspace_files

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

# Excluded both for scan performance and, for the credential paths, so they
# are never even eligible to be recorded — the same set delegations.py's
# normalize_cwd forbids a delegation cwd from targeting.
HIDDEN_DIRS = frozenset({
  ".git", "node_modules", "__pycache__", ".venv", "venv",
  "dist", "build", ".next", ".cache",
  "cli-auth",
})

# A generated-file candidate above this size is not recorded: it protects
# against a runaway script ballooning the chat's stored metadata or being
# served as an oversized download. Mirrors uploads.py's MAX_UPLOAD_MB default.
MAX_RECORDED_BYTES = 100 * 1024 * 1024

# Cap on new candidates recorded per single tool-call diff, so a script that
# writes thousands of small files in one call can't flood the chat's
# generated_files list.
MAX_CANDIDATES_PER_DIFF = 20

Snapshot = dict[str, tuple[str, int]]


def snapshot(cwd: str) -> Snapshot:
  """Returns {relative_path: (modified_at, size)} for every file under cwd.

  Reuses workspace_files.list_entries for its symlink-safety and scan-limit
  protections rather than re-implementing directory walking. A missing or
  non-directory cwd yields an empty snapshot rather than raising — detection
  must never fail the turn it is observing.
  """
  root = Path(cwd)
  try:
    listing = workspace_files.list_entries(
      root, root, recursive=True, hidden_dirs=HIDDEN_DIRS,
    )
  except (OSError, NotADirectoryError):
    return {}
  return {
    entry["path"]: (entry["modified_at"], entry["size"])
    for entry in listing["entries"]
    if entry["type"] == "file"
  }


def diff_new_or_changed(before: Snapshot, after: Snapshot) -> list[dict]:
  """Returns allowlisted files that are new or changed between two snapshots.

  Bounded to MAX_CANDIDATES_PER_DIFF entries and MAX_RECORDED_BYTES each —
  see the module docstring for why. Order follows the underlying scan (folder-
  first, name order), so truncation is deterministic rather than arbitrary.
  """
  candidates: list[dict] = []
  for path, (modified_at, size) in after.items():
    if before.get(path) == (modified_at, size):
      continue
    if size > MAX_RECORDED_BYTES:
      continue
    suffix = Path(path).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
      continue
    candidates.append({
      "name": Path(path).name,
      "path": path,
      "size": size,
      "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
    })
    if len(candidates) >= MAX_CANDIDATES_PER_DIFF:
      break
  return candidates
