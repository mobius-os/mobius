#!/usr/bin/env python3
"""Propose stable agent runtime pins in a disposable checkout, never install them."""

import argparse
import json
from pathlib import Path
import re
from urllib.request import urlopen


def read_url(url):
    with urlopen(url, timeout=30) as response:
        return response.read().decode()


def stable(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Expected a stable release, got {version!r}")
    return tuple(map(int, version.split(".")))


def replace_pin(text, pattern, value):
    """Refuse source drift rather than silently publishing a partial update."""
    updated, count = re.subn(pattern, lambda match: match[1] + value, text)
    if count != 1:
        raise ValueError(f"Expected one pin for {pattern!r}, found {count}")
    return updated


def propose(root, fetch=read_url):
    dockerfile = (root / "Dockerfile").read_text()
    requirements = (root / "backend/requirements.txt").read_text()
    codex_pin = r"(?m)^(ARG CODEX_VERSION=)[^\s]+$"
    codex_sdk_pin = r"(?m)^(ARG CODEX_SDK_VERSION=)[^\s]+$"
    claude_pin = r"(?m)^(claude-agent-sdk==)[^\s]+$"
    codex = json.loads(fetch("https://registry.npmjs.org/@openai/codex/latest"))["version"]
    sdk_info = json.loads(fetch("https://pypi.org/pypi/openai-codex/json"))["info"]
    codex_sdk = sdk_info["version"]
    sdk_bins = [requirement.removeprefix("openai-codex-cli-bin==")
                for requirement in (sdk_info["requires_dist"] or [])
                if requirement.startswith("openai-codex-cli-bin==")]
    if sdk_bins != [codex_sdk]:
        raise ValueError("Codex SDK release does not declare its matching CLI runtime")
    if codex != codex_sdk:
        raise ValueError("Codex npm CLI and Python SDK releases differ; wait for matching versions")
    claude = json.loads(fetch("https://pypi.org/pypi/claude-agent-sdk/json"))["info"]["version"]
    changes = []
    for name, text, pattern, latest in [
        ("Codex", dockerfile, codex_pin, codex),
        ("Codex Python SDK", dockerfile, codex_sdk_pin, codex_sdk),
        ("Claude SDK + bundled CLI", requirements, claude_pin, claude),
    ]:
        match = re.search(pattern, text)
        if not match:
            raise ValueError(f"Missing {name} pin")
        current = match[0][len(match[1]):]
        if stable(latest) < stable(current):
            raise ValueError(f"Refusing {name} downgrade: {current} -> {latest}")
        if latest != current:
            changes.append(f"{name}: {current} → {latest}")
    if not changes:
        return {}, []

    dockerfile = replace_pin(dockerfile, codex_pin, codex)
    dockerfile = replace_pin(dockerfile, codex_sdk_pin, codex_sdk)
    requirements = replace_pin(requirements, claude_pin, claude)
    files = {"Dockerfile": dockerfile, "backend/requirements.txt": requirements}
    return {path: content for path, content in files.items()
            if content != (root / path).read_text()}, changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write", action="store_true", help="Write candidate pins; never install")
    args = parser.parse_args()
    files, changes = propose(args.root)
    print("\n".join(changes) if changes else "Agent runtimes are current.")
    if args.write:
        for path, content in files.items():
            (args.root / path).write_text(content)
        if "backend/requirements.txt" in files:
            print("Regenerate backend/requirements.lock before proposing this change.")


if __name__ == "__main__":
    main()
