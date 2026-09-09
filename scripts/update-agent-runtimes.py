#!/usr/bin/env python3
"""Propose stable agent runtime pins in a disposable checkout, never install them."""

import argparse
import json
from pathlib import Path
import re
import tomllib
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
    claude_pin = r"(?m)^(claude-agent-sdk==)[^\s]+$"
    sdk_pin = r"(openai-codex @ git\+https://github.com/openai/codex.git@)[0-9a-f]{40}"
    bin_pin = r"(openai-codex-cli-bin==)[0-9.]+"
    codex = json.loads(fetch("https://registry.npmjs.org/@openai/codex/latest"))["version"]
    claude = json.loads(fetch("https://pypi.org/pypi/claude-agent-sdk/json"))["info"]["version"]
    changes = []
    for name, text, pattern, latest in [
        ("Codex", dockerfile, codex_pin, codex),
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

    current_codex = re.search(codex_pin, dockerfile)[0].split("=", 1)[1]
    if codex != current_codex:
        # The npm CLI and Python protocol types must come from the SAME release.
        commit = json.loads(fetch(
            f"https://api.github.com/repos/openai/codex/commits/rust-v{codex}"
        ))["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Codex release did not resolve to an immutable commit")
        project = tomllib.loads(fetch(
            f"https://raw.githubusercontent.com/openai/codex/{commit}/sdk/python/pyproject.toml"
        ))["project"]
        bins = [value.removeprefix("openai-codex-cli-bin==")
                for value in project["dependencies"]
                if value.startswith("openai-codex-cli-bin==")]
        if len(bins) != 1:
            raise ValueError("Codex SDK changed its CLI package contract; review required")
        stable(bins[0])
        dockerfile = replace_pin(dockerfile, codex_pin, codex)
        dockerfile = replace_pin(dockerfile, sdk_pin, commit)
        dockerfile = replace_pin(dockerfile, bin_pin, bins[0])
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
