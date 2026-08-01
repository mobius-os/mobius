#!/usr/bin/env python3
"""Fail when a tracked Markdown document links to a missing local path."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*]\(([^)\s]+)(?:\s+[\"'][^)]*)?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def markdown_files() -> list[Path]:
  result = subprocess.run(
    ["git", "ls-files", "*.md"],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
  )
  return [ROOT / relative for relative in result.stdout.splitlines()]


def missing_links(path: Path) -> list[tuple[int, str]]:
  missing: list[tuple[int, str]] = []
  fenced = False
  for line_number, line in enumerate(
    path.read_text(encoding="utf-8").splitlines(), start=1
  ):
    if FENCE.match(line):
      fenced = not fenced
      continue
    if fenced:
      continue
    for match in LINK.finditer(line):
      raw_target = match.group(1).strip("<>")
      if (
        not raw_target
        or raw_target.startswith(("#", "/", "http://", "https://", "mailto:"))
      ):
        continue
      target = unquote(raw_target.split("#", 1)[0].split("?", 1)[0])
      if target and not (path.parent / target).exists():
        missing.append((line_number, raw_target))
  return missing


def main() -> int:
  failures: list[str] = []
  for path in markdown_files():
    for line_number, target in missing_links(path):
      failures.append(f"{path.relative_to(ROOT)}:{line_number}: {target}")
  if failures:
    print("Missing local Markdown targets:")
    print("\n".join(failures))
    return 1
  print("All tracked Markdown links resolve.")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
