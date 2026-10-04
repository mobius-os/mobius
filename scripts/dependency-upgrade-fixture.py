#!/usr/bin/env python3
"""Prepare one disposable, committed-image input for the hosted upgrade test.

This edits only a caller-supplied temporary checkout. The two Python bases are
the official amd64 manifests, not mutable tag-only references. The added lock
entry uses PyPI's published wheel hash; image pip still downloads and verifies
the real distribution under --require-hashes.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

OLD_BASE = (
    "python:3.12.13-slim-trixie@sha256:"
    "d657ab0ade19f404a6ccc883ab399540de667aff751748ce23c07330c5a89e64"
)
NEW_BASE = (
    "python:3.12.14-slim-trixie@sha256:"
    "44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b"
)
PACKAGE = "colorama==0.4.6"
WHEEL = "colorama-0.4.6-py2.py3-none-any.whl"


def wheel_hash(index: dict) -> str:
    matches = [
        item.get("digests", {}).get("sha256")
        for item in index.get("urls", [])
        if item.get("filename") == WHEEL and item.get("packagetype") == "bdist_wheel"
    ]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0] or ""):
        raise ValueError("PyPI did not supply the expected wheel and SHA-256")
    return matches[0]


def prepare(root: Path, stage: str, digest: str | None = None) -> None:
    dockerfile = root / "Dockerfile"
    content = dockerfile.read_text()
    before = "FROM python:3.12-slim-trixie" if stage == "old" else f"FROM {OLD_BASE}"
    after = OLD_BASE if stage == "old" else NEW_BASE
    if stage not in {"old", "new"} or content.count(before + "\n") != 1:
        raise ValueError(f"expected exactly one {before!r} in Dockerfile")
    updates = {dockerfile: content.replace(before + "\n", f"FROM {after}\n", 1)}
    if stage == "old":
        dockerfile.write_text(updates[dockerfile])
        return
    if not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("a wheel SHA-256 is required for the new fixture")
    requirements = root / "backend/requirements.txt"
    lock = root / "backend/requirements.lock"
    for path in (requirements, lock):
        data = path.read_text()
        if re.search(r"(?m)^colorama(?:[=<>!~]|\s)", data):
            raise ValueError(f"fixture package already declared in {path}")
        addition = f"{PACKAGE}\n\n" if path == requirements else (
            f"{PACKAGE} \\\n    --hash=sha256:{digest}\n\n"
        )
        # The upgrade harness preserves an owner's note at EOF. Put the real
        # dependency change at the start so these independent edits can merge.
        updates[path] = addition + data
    for path, data in updates.items():
        path.write_text(data)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"old", "new"}:
        raise SystemExit("usage: dependency-upgrade-fixture.py old|new <temporary-checkout>")
    stage, root = sys.argv[1], Path(sys.argv[2])
    digest = None
    if stage == "new":
        with urllib.request.urlopen(
            "https://pypi.org/pypi/colorama/0.4.6/json", timeout=20
        ) as response:
            digest = wheel_hash(json.load(response))
    prepare(root, stage, digest)


if __name__ == "__main__":
    main()
