#!/usr/bin/env python3
"""Fork an exact Claude or Codex session for Agent Coaching.

This module intentionally has one success path: the provider forks the named
session and the coaching prompt runs inside that fork. Missing, expired, or
unforkable sessions fail loudly. Stored chat messages are never used to seed a
replacement agent.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


class ForkError(RuntimeError):
  """Raised when an exact provider-session fork cannot be completed."""


@dataclass(frozen=True)
class ForkResult:
  provider: str
  source_session_id: str
  forked_session_id: str
  answer: str
  method: str = "session_fork"
  exact_session_fork: bool = True


def _validated_result(
  *, provider: str, source_session_id: str, forked_session_id: str, answer: str
) -> ForkResult:
  forked_session_id = (forked_session_id or "").strip()
  answer = (answer or "").strip()
  if not forked_session_id:
    raise ForkError(f"{provider} did not return a forked session id")
  if forked_session_id == source_session_id:
    raise ForkError(f"{provider} returned the source session instead of a fork")
  if not answer:
    raise ForkError(f"{provider} returned an empty coaching response")
  return ForkResult(
    provider=provider,
    source_session_id=source_session_id,
    forked_session_id=forked_session_id,
    answer=answer,
  )


def _fork_claude(
  source_session_id: str,
  cwd: str,
  prompt: str,
  *,
  runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ForkResult:
  env = os.environ.copy()
  env.setdefault("CLAUDE_CONFIG_DIR", "/data/cli-auth/claude")
  proc = runner(
    [
      "claude",
      "--resume",
      source_session_id,
      "--fork-session",
      "--print",
      prompt,
      "--output-format",
      "json",
      "--restricted",
      "--tools",
      "",
    ],
    cwd=cwd,
    env=env,
    text=True,
    capture_output=True,
  )
  if proc.returncode:
    detail = (proc.stderr or proc.stdout or "provider command failed").strip()
    raise ForkError(f"Claude exact-session fork failed: {detail}")
  try:
    payload = json.loads(proc.stdout)
  except (TypeError, ValueError) as exc:
    raise ForkError("Claude exact-session fork returned malformed JSON") from exc
  if not isinstance(payload, dict):
    raise ForkError("Claude exact-session fork returned an unexpected JSON shape")
  return _validated_result(
    provider="claude",
    source_session_id=source_session_id,
    forked_session_id=str(payload.get("session_id") or ""),
    answer=str(payload.get("result") or ""),
  )


def _load_codex_sdk() -> tuple[Any, Any, Any, Any]:
  """Load Codex through the platform's pinned wire-compatibility boundary."""
  # The platform runner owns compatibility between the pinned Python SDK and
  # the matching app-server wire format. Reuse that exact provider boundary
  # here: importing the generated types directly can reject valid persisted
  # history before ``thread/fork`` returns (notably the app-server's
  # ``subAgentActivity(kind=completed)`` lifecycle marker).
  backend = str(Path(__file__).resolve().parents[1])
  if backend not in sys.path:
    sys.path.insert(0, backend)
  from app.codex_sdk_runner import _sdk_imports

  sdk = _sdk_imports()
  return (
    sdk["AsyncCodex"],
    sdk["CodexConfig"],
    sdk["ApprovalMode"],
    sdk["Sandbox"],
  )


async def _fork_codex_async(
  source_session_id: str,
  cwd: str,
  prompt: str,
  *,
  sdk_loader: Callable[[], tuple[Any, Any, Any, Any]] | None = None,
) -> ForkResult:
  if sdk_loader is None:
    AsyncCodex, CodexConfig, ApprovalMode, Sandbox = _load_codex_sdk()
  else:
    AsyncCodex, CodexConfig, ApprovalMode, Sandbox = sdk_loader()

  env = os.environ.copy()
  env.setdefault("CODEX_HOME", "/data/cli-auth/codex")
  config = CodexConfig(
    cwd=cwd,
    env=env,
    client_name="mobius_agent_coaching",
    client_title="Möbius Agent Coaching",
  )
  async with AsyncCodex(config) as codex:
    thread = await codex.thread_fork(
      source_session_id,
      approval_mode=ApprovalMode.deny_all,
      cwd=cwd,
      sandbox=Sandbox.read_only,
    )
    result = await thread.run(
      prompt,
      approval_mode=ApprovalMode.deny_all,
      cwd=cwd,
      sandbox=Sandbox.read_only,
    )

  if result.error is not None:
    raise ForkError(f"Codex exact-session fork turn failed: {result.error}")
  return _validated_result(
    provider="codex",
    source_session_id=source_session_id,
    forked_session_id=str(thread.id or ""),
    answer=str(result.final_response or ""),
  )


def fork_session(
  provider: str, source_session_id: str, cwd: str, prompt: str
) -> ForkResult:
  provider = provider.strip().lower()
  if provider not in {"claude", "codex"}:
    raise ForkError(f"unsupported coaching provider: {provider or '(empty)'}")
  if not source_session_id.strip():
    raise ForkError("an exact source session id is required")
  if not prompt.strip():
    raise ForkError("a coaching prompt is required")
  if not Path(cwd).is_dir():
    raise ForkError(f"working directory does not exist: {cwd}")
  if provider == "claude":
    try:
      return _fork_claude(source_session_id, cwd, prompt)
    except ForkError:
      raise
    except Exception as exc:
      raise ForkError(f"Claude exact-session fork failed: {exc}") from exc
  try:
    return asyncio.run(_fork_codex_async(source_session_id, cwd, prompt))
  except ForkError:
    raise
  except Exception as exc:
    raise ForkError(f"Codex exact-session fork failed: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Fork and coach one exact Claude or Codex session"
  )
  parser.add_argument("--json", action="store_true", dest="as_json")
  parser.add_argument("provider", choices=("claude", "codex"))
  parser.add_argument("session_id")
  parser.add_argument("cwd")
  parser.add_argument("prompt")
  return parser


def main(argv: list[str] | None = None) -> int:
  args = _parser().parse_args(argv)
  try:
    result = fork_session(args.provider, args.session_id, args.cwd, args.prompt)
  except ForkError as exc:
    print(f"fork-session: {exc}", file=sys.stderr)
    return 1
  if args.as_json:
    print(json.dumps(asdict(result), ensure_ascii=False))
  else:
    print(result.answer)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
