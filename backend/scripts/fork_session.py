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

from pydantic import BaseModel, ConfigDict, Field


_MOBIUS_CONTROL_ENV_VARS = (
  "API_BASE_URL",
  "AGENT_TOKEN",
  "CHAT_ID",
  "MOBIUS_RUN_TOKEN",
)
_CODEX_MCP_FEATURE_OVERRIDES = (
  "features.apps=false",
  "features.enable_mcp_apps=false",
  "features.plugins=false",
  "features.remote_plugin=false",
  "features.skill_mcp_dependency_install=false",
)
_MAX_CODEX_MCP_SERVERS = 256
_MAX_CODEX_MCP_NAME_CHARS = 256
_MAX_CODEX_MCP_INVENTORY_CHARS = 1_000_000


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


class _CodexMcpSurface(BaseModel):
  """Only the effective MCP surface needed for the coaching deny check."""

  model_config = ConfigDict(extra="ignore", populate_by_name=True)

  name: str
  tools: dict[str, Any] = Field(default_factory=dict)
  resources: list[Any] = Field(default_factory=list)
  resource_templates: list[Any] = Field(
    default_factory=list, alias="resourceTemplates",
  )


class _CodexMcpSurfacePage(BaseModel):
  model_config = ConfigDict(extra="ignore", populate_by_name=True)

  data: list[_CodexMcpSurface]
  next_cursor: str | None = Field(default=None, alias="nextCursor")


def _coaching_env() -> dict[str, str]:
  """Keep provider auth while removing Möbius run-control authority."""
  env = os.environ.copy()
  for name in _MOBIUS_CONTROL_ENV_VARS:
    env.pop(name, None)
  return env


def _codex_mcp_isolation_overrides(
  cwd: str,
  env: dict[str, str],
  *,
  runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, ...]:
  """Disable every effective Codex MCP source before app-server starts.

  Codex config overlays merge maps, so an empty ``mcp_servers`` table does not
  clear user or project servers. Inventory their names without starting them,
  then override each entry's ``enabled`` field in one higher-precedence map.
  Plugin/app MCP sources are disabled separately because they need not appear
  in ``codex mcp list``.
  """
  try:
    proc = runner(
      ["codex", "mcp", "list", "--json"],
      cwd=cwd,
      env=env,
      text=True,
      capture_output=True,
      timeout=15,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise ForkError(
      "Codex MCP inventory failed; refusing an unisolated coaching fork"
    ) from exc
  if proc.returncode:
    raise ForkError(
      "Codex MCP inventory failed; refusing an unisolated coaching fork"
    )
  stdout = proc.stdout or ""
  if len(stdout) > _MAX_CODEX_MCP_INVENTORY_CHARS:
    raise ForkError(
      "Codex MCP inventory was unexpectedly large; refusing coaching"
    )
  try:
    payload = json.loads(stdout)
  except (TypeError, ValueError) as exc:
    raise ForkError(
      "Codex MCP inventory was malformed; refusing an unisolated coaching fork"
    ) from exc
  if not isinstance(payload, list):
    raise ForkError(
      "Codex MCP inventory had an unexpected shape; refusing coaching"
    )

  names: set[str] = set()
  for item in payload:
    if not isinstance(item, dict):
      raise ForkError(
        "Codex MCP inventory had an unexpected entry; refusing coaching"
      )
    name = item.get("name")
    if (
      not isinstance(name, str)
      or not name
      or len(name) > _MAX_CODEX_MCP_NAME_CHARS
    ):
      raise ForkError(
        "Codex MCP inventory had an invalid server name; refusing coaching"
      )
    names.add(name)
  if len(names) > _MAX_CODEX_MCP_SERVERS:
    raise ForkError(
      "Codex MCP inventory had too many servers; refusing coaching"
    )

  overrides = list(_CODEX_MCP_FEATURE_OVERRIDES)
  if names:
    disabled = ",".join(
      f"{json.dumps(name, ensure_ascii=True)}={{enabled=false}}"
      for name in sorted(names)
    )
    overrides.append(f"mcp_servers={{{disabled}}}")
  return tuple(overrides)


async def _assert_codex_mcp_isolated(codex: Any, thread_id: str) -> None:
  """Fail before the prompt if any MCP tool or resource survived config."""
  if not thread_id:
    raise ForkError(
      "Codex MCP isolation could not be verified; refusing coaching"
    )
  client = getattr(codex, "_client", None)
  request = getattr(client, "request", None)
  if not callable(request):
    raise ForkError(
      "Codex MCP isolation could not be verified; refusing coaching"
    )

  cursor = None
  seen_cursors: set[str] = set()
  server_count = 0
  while True:
    params: dict[str, Any] = {
      "threadId": thread_id,
      "detail": "full",
      "limit": 100,
    }
    if cursor is not None:
      params["cursor"] = cursor
    try:
      page = await request(
        "mcpServerStatus/list",
        params,
        response_model=_CodexMcpSurfacePage,
      )
    except Exception as exc:
      raise ForkError(
        "Codex MCP isolation could not be verified; refusing coaching"
      ) from exc
    if any(
      server.tools or server.resources or server.resource_templates
      for server in page.data
    ):
      raise ForkError(
        "Codex coaching fork exposed an MCP capability; refusing coaching"
      )
    server_count += len(page.data)
    if server_count > _MAX_CODEX_MCP_SERVERS:
      raise ForkError(
        "Codex MCP isolation returned too many servers; refusing coaching"
      )
    cursor = page.next_cursor
    if cursor is None:
      return
    if cursor in seen_cursors:
      raise ForkError(
        "Codex MCP isolation pagination repeated; refusing coaching"
      )
    seen_cursors.add(cursor)


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
  env = _coaching_env()
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
      "--strict-mcp-config",
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
  mcp_inventory_runner: Callable[
    ..., subprocess.CompletedProcess[str]
  ] = subprocess.run,
) -> ForkResult:
  if sdk_loader is None:
    AsyncCodex, CodexConfig, ApprovalMode, Sandbox = _load_codex_sdk()
  else:
    AsyncCodex, CodexConfig, ApprovalMode, Sandbox = sdk_loader()

  env = _coaching_env()
  env.setdefault("CODEX_HOME", "/data/cli-auth/codex")
  config_overrides = _codex_mcp_isolation_overrides(
    cwd,
    env,
    runner=mcp_inventory_runner,
  )
  config = CodexConfig(
    cwd=cwd,
    env=env,
    config_overrides=config_overrides,
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
    await _assert_codex_mcp_isolated(codex, str(thread.id or ""))
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
  parser.add_argument("provider_or_session")
  parser.add_argument("session_or_cwd")
  parser.add_argument("cwd_or_prompt")
  parser.add_argument("prompt", nargs="?")
  return parser


def _parse_invocation(argv: list[str] | None = None) -> argparse.Namespace:
  """Accept the explicit provider form and the released Claude-only form."""
  args = _parser().parse_args(argv)
  if args.prompt is None:
    args.provider = "claude"
    args.session_id = args.provider_or_session
    args.cwd = args.session_or_cwd
    args.prompt = args.cwd_or_prompt
  else:
    args.provider = args.provider_or_session
    args.session_id = args.session_or_cwd
    args.cwd = args.cwd_or_prompt
  if args.provider not in {"claude", "codex"}:
    _parser().error("provider must be 'claude' or 'codex'")
  return args


def main(argv: list[str] | None = None) -> int:
  args = _parse_invocation(argv)
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
