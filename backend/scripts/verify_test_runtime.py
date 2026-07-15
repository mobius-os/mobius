#!/usr/bin/env python3
"""Fail-closed health check for the disposable Compose E2E runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen


FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
VERSION_URL = "http://127.0.0.1:8000/api/version"
HEALTH_URL = "http://127.0.0.1:8000/api/health"
PLATFORM_ROOT = Path("/data/platform")


def validate_runtime(version: dict[str, object], head: str, expected: str) -> list[str]:
  """Return identity errors; an empty list means the runtime is coherent."""
  errors: list[str] = []
  if version.get("test_runtime") is not True:
    errors.append(f"test_runtime={version.get('test_runtime')!r}")
  if version.get("serving_source") != "platform":
    errors.append(f"serving_source={version.get('serving_source')!r}")
  if version.get("frontend_source") != "platform":
    errors.append(f"frontend_source={version.get('frontend_source')!r}")
  for field in ("served_sha", "platform_sha"):
    if version.get(field) != head:
      errors.append(f"{field}={version.get(field)!r}, want {head}")
  if FULL_SHA.fullmatch(expected):
    if head != expected:
      errors.append(f"platform HEAD={head}, want BUILD_SHA {expected}")
    if version.get("sha") != expected:
      errors.append(f"sha={version.get('sha')!r}, want {expected}")
  return errors


def main() -> int:
  try:
    with urlopen(HEALTH_URL, timeout=3) as response:
      if response.status != 200:
        raise RuntimeError(f"health returned HTTP {response.status}")
    with urlopen(VERSION_URL, timeout=3) as response:
      version = json.load(response)
    head = subprocess.run(
      [
        "git",
        "-c",
        f"safe.directory={PLATFORM_ROOT}",
        "-C",
        str(PLATFORM_ROOT),
        "rev-parse",
        "HEAD",
      ],
      check=True,
      capture_output=True,
      text=True,
      timeout=3,
    ).stdout.strip()
    errors = validate_runtime(version, head, os.getenv("BUILD_SHA", "unknown"))
    if errors:
      raise RuntimeError("; ".join(errors))
  except Exception as exc:
    print(f"test runtime unhealthy: {exc}", file=sys.stderr)
    return 1
  print(f"test runtime healthy at {head}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
