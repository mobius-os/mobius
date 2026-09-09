"""Stable boot-selected source inputs, captured before other app imports.

This is evidence of committed source present at the import boundary and still
unchanged after startup, not an import hook or protection against adversarial
change-and-restore during imports. It deliberately excludes frontend builds,
dependencies, external configuration, and host state. Drift disables activation
proof without preventing the diagnostic server from starting.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from types import MappingProxyType
from typing import Mapping


RESTART_SOURCE_PATHS = ("backend/app", "backend/scripts/pm-commit", "skill/core.md")


def supported_restart_path(path: str) -> bool:
  return path in RESTART_SOURCE_PATHS[1:] or path.startswith("backend/app/")


def filesystem_manifest(repo: Path, paths: list[str]) -> dict[str, dict[str, str]]:
  result = {}
  root = repo.resolve()
  for path in sorted(set(paths)):
    raw = root / path
    if raw.is_symlink():
      raise ValueError("loaded_source_is_not_a_regular_file")
    candidate = raw.resolve()
    if root not in candidate.parents:
      raise ValueError("loaded_source_path_escaped_repository")
    if not candidate.exists():
      result[path] = {"state": "absent"}
      continue
    if not candidate.is_file():
      raise ValueError("loaded_source_is_not_a_regular_file")
    result[path] = {
      "state": "file",
      "mode": "100755" if stat.S_IMODE(candidate.stat().st_mode) & 0o111 else "100644",
      "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }
  return result


@dataclass(frozen=True)
class BootSourceInputs:
  repo: Path
  source_kind: str
  source_sha: str | None
  files: Mapping[str, Mapping[str, str]]
  valid: bool

  def unchanged_manifest(self, repo: Path, paths: list[str]) -> dict:
    if not self.valid or repo.resolve() != self.repo:
      raise ValueError("boot_source_inputs_unavailable")
    expected = {
      path: dict(self.files.get(path, {"state": "absent"})) for path in paths
    }
    if filesystem_manifest(repo, paths) != expected:
      raise ValueError("source_changed_during_startup")
    return expected


def capture_boot_source_inputs(
  repo: Path | None = None, *, source_kind: str | None = None,
  source_sha: str | None = None,
) -> BootSourceInputs:
  repo = (repo or Path(__file__).resolve().parents[2]).resolve()
  files = {}
  valid = False
  try:
    if source_kind is None:
      source_kind = Path(os.environ.get(
        "MOBIUS_SERVING_SOURCE_FILE", "/tmp/serving-source",
      )).read_text().strip()
    if source_sha is None:
      source_sha = Path(os.environ.get(
        "MOBIUS_SERVING_SHA_FILE", "/tmp/serving-sha",
      )).read_text().strip()
    if source_kind == "platform" and source_sha:
      def git(*args):
        return subprocess.run(
          ["git", "-C", str(repo), *args], check=True, capture_output=True,
          text=True, timeout=30,
        ).stdout
      head = git("rev-parse", "HEAD").strip()
      dirty = git("status", "--porcelain", "--untracked-files=all", "--", *RESTART_SOURCE_PATHS)
      if head == source_sha and not dirty.strip():
        paths = git("ls-files", "-z", "--", *RESTART_SOURCE_PATHS).split("\0")
        files = filesystem_manifest(repo, [p for p in paths if supported_restart_path(p)])
        valid = True
  except (OSError, ValueError, subprocess.SubprocessError):
    pass
  return BootSourceInputs(
    repo, source_kind or "unknown", source_sha,
    MappingProxyType({p: MappingProxyType(v) for p, v in files.items()}), valid,
  )


BOOT_SOURCE_INPUTS = capture_boot_source_inputs()
