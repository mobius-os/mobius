#!/usr/bin/env python3
"""Pure topology resolution used by the host installer and its tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def compose_files(root: Path, working_dir: Path, label: str) -> list[Path]:
    root = root.resolve(strict=True)
    working_dir = working_dir.resolve(strict=True)
    if not working_dir.is_relative_to(root):
        raise ValueError("the running app belongs to a different checkout")
    files: list[Path] = []
    for raw in label.split(","):
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = working_dir / candidate
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("Compose input is outside the trusted checkout")
        files.append(resolved)
    if not files:
        raise ValueError("the running app has no Compose file labels")
    return files


def environment_files(label: str) -> list[Path]:
    """Resolve the exact Compose environment files recorded on the container.

    These files may intentionally live beside the main checkout rather than a
    nested deployment worktree.  They are used only as Compose interpolation
    inputs, so require absolute, existing, non-symlinked regular files instead
    of pretending they are tracked topology source.
    """
    files: list[Path] = []
    for raw in label.split(","):
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError("Compose environment files must use absolute paths")
        if candidate.is_symlink():
            raise ValueError("Compose environment files may not use symlinks")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("Compose environment input is not a regular file")
        files.append(resolved)
    return files


def expected_networks(value: dict) -> list[str]:
    service_networks = value["services"]["app"].get("networks", {})
    definitions = value.get("networks", {})
    return sorted(
        str(definitions.get(key, {}).get("name") or key)
        for key in service_networks
    )


def main() -> int:
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "compose-files":
            for path in compose_files(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]):
                print(path)
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "environment-files":
            for path in environment_files(sys.argv[2]):
                print(path)
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "expected-networks":
            with Path(sys.argv[2]).open(encoding="utf-8") as handle:
                for name in expected_networks(json.load(handle)):
                    print(name)
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("invalid invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
