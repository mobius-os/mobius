#!/usr/bin/env python3
"""Propose stable agent runtime pins in a disposable checkout, never install them."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import urlopen

NPM_CODEX = "https://registry.npmjs.org/@openai/codex"
PYPI_CODEX = "https://pypi.org/pypi/openai-codex"
PYPI_CLAUDE = "https://pypi.org/pypi/claude-agent-sdk/json"
CODEX_PIN = r"(?m)^(ARG CODEX_VERSION=)[^\s]+$"
CODEX_SDK_PIN = r"(?m)^(ARG CODEX_SDK_VERSION=)[^\s]+$"
CLAUDE_PIN = r"(?m)^(claude-agent-sdk==)[^\s]+$"
CHROME_FEED = ("https://googlechromelabs.github.io/chrome-for-testing/"
               "last-known-good-versions-with-downloads.json")
CHROME_DOWNLOADS = "https://storage.googleapis.com/chrome-for-testing-public"
CHROME_PIN = r"(?m)^(ARG CHROME_VERSION=)[^\s]+$"
# Dockerfile sha256 argument -> Chrome for Testing download for linux64.
CHROME_ARCHIVES = {
    "CHROME_SHA256": "chrome",
    "CHROME_HEADLESS_SHELL_SHA256": "chrome-headless-shell",
}


def read_url(url):
    with urlopen(url, timeout=30) as response:
        return response.read().decode()


def sha256_of_url(url):
    """Chrome for Testing publishes no checksums; hash the archive the build will fetch."""
    digest = hashlib.sha256()
    with urlopen(url, timeout=120) as response:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable(version, parts=3):
    """Chrome for Testing versions have four numeric parts; the agent runtimes three."""
    if not re.fullmatch(rf"\d+(?:\.\d+){{{parts - 1}}}", version):
        raise ValueError(f"Expected a stable release, got {version!r}")
    return tuple(map(int, version.split(".")))


def propose_chrome(dockerfile, fetch, digest):
    """Pin the current Stable with fresh archive hashes, or return the Dockerfile unchanged."""
    channel = json.loads(fetch(CHROME_FEED))["channels"]["Stable"]
    latest = channel["version"]
    match = re.search(CHROME_PIN, dockerfile)
    if not match:
        raise ValueError("Missing Chrome for Testing pin")
    current = match[0][len(match[1]):]
    if stable(latest, parts=4) < stable(current, parts=4):
        raise ValueError(f"Refusing Chrome for Testing downgrade: {current} -> {latest}")
    if latest == current:
        return dockerfile, []
    dockerfile = replace_pin(dockerfile, CHROME_PIN, latest)
    for argument, download in CHROME_ARCHIVES.items():
        url = f"{CHROME_DOWNLOADS}/{latest}/linux64/{download}-linux64.zip"
        listed = [entry["url"] for entry in channel["downloads"][download]
                  if entry["platform"] == "linux64"]
        if listed != [url]:
            raise ValueError(f"Chrome for Testing feed does not list {url}")
        dockerfile = replace_pin(
            dockerfile, rf"(?m)^(ARG {argument}=)[0-9a-f]{{64}}$", digest(url))
    return dockerfile, [f"Chrome for Testing: {current} → {latest}"]


def replace_pin(text, pattern, value):
    """Refuse source drift rather than silently publishing a partial update."""
    updated, count = re.subn(pattern, lambda match: match[1] + value, text)
    if count != 1:
        raise ValueError(f"Expected one pin for {pattern!r}, found {count}")
    return updated


def pinned(text, pattern, name):
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"Missing {name} pin")
    return match[0][len(match[1]):]


def declared_cli(sdk_info):
    return [requirement.removeprefix("openai-codex-cli-bin==")
            for requirement in (sdk_info["requires_dist"] or [])
            if requirement.startswith("openai-codex-cli-bin==")]


def on_npm(fetch, version):
    try:
        return json.loads(fetch(f"{NPM_CODEX}/{version}"))["version"] == version
    except HTTPError as error:
        if error.code == 404:
            return False
        raise


def propose_codex(fetch, current):
    """Pick the newest stable release both npm and PyPI publish, or keep the current pair.

    The registries' latest tags often differ for days; an older release that both
    publish is still a matched pair, and a lagging side only delays Codex.
    """
    npm_latest = json.loads(fetch(f"{NPM_CODEX}/latest"))["version"]
    sdk = json.loads(fetch(f"{PYPI_CODEX}/json"))
    sdk_latest = sdk["info"]["version"]
    for name, latest in (("Codex", npm_latest), ("Codex Python SDK", sdk_latest)):
        if stable(latest) < stable(current):
            raise ValueError(f"Refusing {name} downgrade: {current} -> {latest}")
    ceiling = min(stable(npm_latest), stable(sdk_latest))
    candidates = sorted(
        (version for version, files in sdk["releases"].items()
         if re.fullmatch(r"\d+\.\d+\.\d+", version)
         and stable(current) < stable(version) <= ceiling
         and any(not file.get("yanked") for file in files)),
        key=stable, reverse=True)
    for version in candidates:
        info = (sdk["info"] if version == sdk_latest
                else json.loads(fetch(f"{PYPI_CODEX}/{version}/json"))["info"])
        if declared_cli(info) != [version]:
            raise ValueError(f"Codex SDK {version} does not declare its matching CLI runtime")
        if on_npm(fetch, version):
            return version, [f"Codex: {current} → {version}",
                             f"Codex Python SDK: {current} → {version}"]
    if npm_latest == sdk_latest == current:
        return current, []
    return current, [f"Codex: kept {current}; no newer release is on both npm "
                     f"({npm_latest}) and PyPI ({sdk_latest})"]


def propose(root, fetch=read_url, digest=sha256_of_url):
    dockerfile = (root / "Dockerfile").read_text()
    requirements = (root / "backend/requirements.txt").read_text()
    codex_current = pinned(dockerfile, CODEX_PIN, "Codex")
    if pinned(dockerfile, CODEX_SDK_PIN, "Codex Python SDK") != codex_current:
        raise ValueError("Codex CLI and Python SDK pins differ")
    codex, changes = propose_codex(fetch, codex_current)
    claude_current = pinned(requirements, CLAUDE_PIN, "Claude SDK + bundled CLI")
    claude = json.loads(fetch(PYPI_CLAUDE))["info"]["version"]
    if stable(claude) < stable(claude_current):
        raise ValueError(f"Refusing Claude SDK + bundled CLI downgrade: {claude_current} -> {claude}")
    if claude != claude_current:
        changes.append(f"Claude SDK + bundled CLI: {claude_current} → {claude}")
    dockerfile, chrome_changes = propose_chrome(dockerfile, fetch, digest)
    changes += chrome_changes

    dockerfile = replace_pin(dockerfile, CODEX_PIN, codex)
    dockerfile = replace_pin(dockerfile, CODEX_SDK_PIN, codex)
    requirements = replace_pin(requirements, CLAUDE_PIN, claude)
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
