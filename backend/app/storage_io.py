"""Shared filesystem helpers for per-app and shared storage.

Lives apart from ``routes/storage.py`` so the installer (``install.py``) can
reuse the SAME atomic write the storage API uses when it seeds an app's initial
files — otherwise a seed would be written non-atomically and a concurrent read
could observe it torn (Codex review round-8 #2).
"""

import os
import tempfile
from pathlib import Path

# Hard cap on a single storage object — enforced on BOTH the PUT request body
# and the file served back. Möbius runs on a memory-tight host (recurring OOM),
# so an app writing or reading an unbounded blob would threaten the whole
# instance. 50 MB is far above any real per-key app payload (notes, reports,
# save files) while still bounding the blast radius (Codex review round-8 #3).
MAX_STORAGE_BYTES = 50 * 1024 * 1024


def atomic_write(file_path: Path, content: str | bytes) -> None:
  """Writes content to file_path atomically — no torn or interleaved reads.

  A reader (or the listing-based completion poll a mini-app runs after a job)
  must never observe a half-written file, and two concurrent writers to the
  same path must not interleave bytes into one corrupt file. Write the full
  body to a uniquely-named temp file in the SAME directory, fsync it, then
  ``os.replace()`` onto the target — a same-filesystem rename is atomic on
  POSIX, so a reader sees either the old file or the new one, never a
  truncation. A crash mid-write leaves only the temp file; the target is never
  partial.
  """
  file_path.parent.mkdir(parents=True, exist_ok=True)
  data = content.encode("utf-8") if isinstance(content, str) else content
  # Unique temp name (mkstemp) so concurrent writers to the same path don't
  # collide on the temp file itself. mkstemp creates 0600; chmod to 0644 so the
  # file is readable the same way a normal umask-022 write would leave it.
  fd, tmp = tempfile.mkstemp(
    dir=file_path.parent, prefix=f".{file_path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(fd, "wb") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, file_path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise
