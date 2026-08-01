"""Fail-closed registry state machine for the one-time compatible image bridge."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
ABSENT_RE = re.compile(
  r"(?:not found|manifest unknown|name_unknown|manifest_unknown)", re.IGNORECASE
)


class StateError(RuntimeError):
  """The registry is unavailable or outside the explicitly allowed states."""


@dataclass(frozen=True)
class Identity:
  digest: str
  revision: str


def validate_identity(identity: Identity) -> Identity:
  if not DIGEST_RE.fullmatch(identity.digest):
    raise StateError("registry returned an invalid image digest")
  if not SHA_RE.fullmatch(identity.revision):
    raise StateError("registry returned an invalid image revision")
  return identity


def _parse_identity(value: str) -> Identity:
  parts = value.strip().split("|", 1)
  if len(parts) != 2:
    raise StateError("registry returned malformed image identity")
  return validate_identity(Identity(parts[0], parts[1]))


def inspect_tag(
  repository: str,
  tag: str,
  *,
  attempts: int = 5,
  runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
  sleeper: Callable[[float], None] = time.sleep,
) -> Identity | None:
  """Return one exact public tag identity, ``None`` only for repeated 404s."""

  reference = f"{repository}:{tag}"
  command = [
    "docker",
    "buildx",
    "imagetools",
    "inspect",
    reference,
    "--format",
    '{{.Manifest.Digest}}|{{index .Image.Config.Labels "org.opencontainers.image.revision"}}',
  ]
  absent = 0
  for attempt in range(1, attempts + 1):
    result = runner(
      command,
      capture_output=True,
      text=True,
      check=False,
      env=os.environ.copy(),
    )
    if result.returncode == 0:
      return _parse_identity(result.stdout)
    error = (result.stderr or "").strip()
    if reference.lower() not in error.lower() or not ABSENT_RE.search(error):
      raise StateError(f"ambiguous registry failure for {reference}: {error or 'no error'}")
    absent += 1
    if attempt < attempts:
      sleeper(float(attempt))
  if absent == attempts:
    return None
  raise StateError(f"could not classify {reference}")


def classify_inventory(
  *,
  main: Identity,
  external: Identity | None,
  daily: Identity | None,
  current_sha: str,
  previous: Identity,
) -> tuple[str, str]:
  """Accept exactly S0 (fresh), S1 (main moved), or S2 (complete)."""

  validate_identity(main)
  validate_identity(previous)
  if external is not None:
    validate_identity(external)
  if daily is not None:
    raise StateError("the previously absent :daily tag exists")

  if main == previous:
    if external is not None:
      raise StateError("external-recovery exists before :main owns the bootstrap digest")
    return "build", ""

  if main.revision != current_sha:
    raise StateError("the public :main compatibility floor changed unexpectedly")
  if external is None:
    return "reuse", main.digest
  if external != main:
    raise StateError("partial bootstrap channels disagree on their exact identity")
  return "reuse", main.digest


def assert_prewrite_state(
  *,
  tag: str,
  main: Identity,
  external: Identity | None,
  daily: Identity | None,
  current: Identity,
  previous: Identity,
) -> None:
  if daily is not None:
    raise StateError(":daily appeared during compatibility publication")
  if tag == "main":
    if main not in {previous, current}:
      raise StateError(":main changed after the initial inventory")
    if external not in {None, current}:
      raise StateError(":external-recovery changed after the initial inventory")
    return
  if tag == "external-recovery":
    if main != current:
      raise StateError(":main does not hold the selected digest before external publication")
    if external not in {None, current}:
      raise StateError(":external-recovery changed before its write")
    return
  raise StateError("unsupported compatibility tag")


def assert_final_state(
  *,
  main: Identity,
  external: Identity | None,
  daily: Identity | None,
  current: Identity,
) -> None:
  if main != current or external != current:
    raise StateError("compatible channels do not share the selected exact identity")
  if daily is not None:
    raise StateError(":daily appeared during compatibility publication")


def _common_identities(args: argparse.Namespace) -> tuple[Identity, Identity | None, Identity | None]:
  main = inspect_tag(args.repository, "main")
  if main is None:
    raise StateError("the required public :main compatibility floor is absent")
  external = inspect_tag(args.repository, "external-recovery")
  daily = inspect_tag(args.repository, "daily")
  return main, external, daily


def _write_outputs(path: str, values: dict[str, str]) -> None:
  with Path(path).open("a", encoding="utf-8") as target:
    target.writelines(f"{key}={value}\n" for key, value in values.items())


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  parser.add_argument("command", choices=("inventory", "prewrite", "final"))
  parser.add_argument("--repository", required=True)
  parser.add_argument("--current-sha", required=True)
  parser.add_argument("--previous-sha", required=True)
  parser.add_argument("--previous-digest", required=True)
  parser.add_argument("--selected-digest", default="")
  parser.add_argument("--tag", choices=("main", "external-recovery"))
  parser.add_argument("--output")
  return parser


def main(argv: list[str] | None = None) -> int:
  args = build_parser().parse_args(argv)
  if not SHA_RE.fullmatch(args.current_sha) or not SHA_RE.fullmatch(args.previous_sha):
    raise StateError("invalid compatibility SHA")
  previous = validate_identity(Identity(args.previous_digest, args.previous_sha))
  live_main, live_external, live_daily = _common_identities(args)

  if args.command == "inventory":
    if not args.output:
      raise StateError("inventory output path is required")
    mode, digest = classify_inventory(
      main=live_main,
      external=live_external,
      daily=live_daily,
      current_sha=args.current_sha,
      previous=previous,
    )
    _write_outputs(args.output, {"mode": mode, "digest": digest})
    return 0

  if not DIGEST_RE.fullmatch(args.selected_digest):
    raise StateError("invalid selected compatibility digest")
  current = Identity(args.selected_digest, args.current_sha)
  if args.command == "prewrite":
    if not args.tag:
      raise StateError("prewrite tag is required")
    assert_prewrite_state(
      tag=args.tag,
      main=live_main,
      external=live_external,
      daily=live_daily,
      current=current,
      previous=previous,
    )
    return 0

  assert_final_state(
    main=live_main,
    external=live_external,
    daily=live_daily,
    current=current,
  )
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except StateError as exc:
    print(f"compatibility bootstrap refused: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
