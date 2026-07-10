#!/usr/bin/env python3
"""Standalone runner for the scheduled Memory consolidation pass.

Memory owns the knowledge graph. The daytime agent keeps the current chat note
fresh; this runner gives the Memory app a scheduled, unattended pass that can
promote durable facts, merge duplicates, prune stale notes, rebuild graph.json,
and leave an update log for Reflection to read later.

The module stays stdlib-importable so py_compile works in images that do not
have the agent SDK installed yet. Heavy imports happen inside `run()`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILL_PATH = DATA_DIR / "shared" / "skills" / "memory.md"
LOG_PATH = DATA_DIR / "cron-logs" / "memory.log"
MEMORY_DIR = DATA_DIR / "shared" / "memory"
UPDATE_LOG_DIR = MEMORY_DIR / "update-log"
CLAUDE_CONFIG_DIR = DATA_DIR / "cli-auth" / "claude"
CODEX_HOME = DATA_DIR / "cli-auth" / "codex"
CLI_PATH = "/usr/local/bin/claude"
PM_COMMIT = "/app/scripts/pm-commit"
BUILD_GRAPH = "/app/scripts/build_memory_graph.py"
DEFAULT_MAX_TURNS = 32
DEFAULT_PROVIDER = "claude"
KNOWN_MODELS_BY_PROVIDER = {
  "claude": (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5-20251001",
    "claude-sonnet-4-7-20251215",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5-20251001",
    "claude-haiku-4-5-20251001",
  ),
  "codex": ("gpt-5.5", "gpt-5.4"),
}


def _log(message: str) -> None:
  try:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as fh:
      fh.write(f"[{stamp}] memory_runner: {message}\n")
  except OSError:
    pass


def load_skill() -> str:
  """Returns the Memory skill that defines graph maintenance rules."""
  if SKILL_PATH.is_file():
    try:
      text = SKILL_PATH.read_text(encoding="utf-8")
      if text.strip():
        return text
    except OSError:
      pass
  for fallback in (
    Path("/app/scripts/seed-skills/memory.md"),
    Path(__file__).resolve().parent / "seed-skills" / "memory.md",
  ):
    if fallback.is_file():
      try:
        return fallback.read_text(encoding="utf-8")
      except OSError:
        continue
  raise FileNotFoundError(
    f"memory skill not found at {SKILL_PATH} or any baked fallback"
  )


def build_goal() -> str:
  today = date.today().isoformat()
  return "\n".join([
    f"It is {today}. Run the scheduled Memory consolidation pass.",
    "",
    "Memory owns /data/shared/memory. Reflection may read the update log later,",
    "but it does not consolidate the graph for you.",
    "",
    "Work in this order:",
    "1. Review recent chat notes under /data/shared/memory/chats and, when a",
    "   chat note is thin or suspicious, inspect the matching chat transcript",
    "   from /data/db/ultimate.db.",
    "2. Promote only durable, future-useful user or instance facts into",
    "   notes/, merge exact or near duplicates when the winner is clear,",
    "   supersede corrected facts, prune stale notes, and keep source:",
    "   provenance on promoted facts.",
    "3. Reorganize only where it makes recall cheaper: repair orphans and",
    "   dangling links, add missing map descriptions, split overgrown notes or",
    "   maps when the Memory skill says to, and leave ambiguous contradictions",
    "   marked for a future user decision rather than guessing.",
    "4. Rebuild the viewer index with python3 /app/scripts/build_memory_graph.py",
    "   and fix any errors it reports.",
    "5. Append one JSONL line to",
    f"   {UPDATE_LOG_DIR}/{today}.jsonl with at least timestamp, summary,",
    "   changed_paths, counts, and followups. This is Reflection's input for",
    "   improving the memory system itself; keep it factual and compact.",
    "6. Commit with pm-commit 'memory: scheduled consolidation <short summary>'.",
    "",
    "Do not write a morning brief, triage unrelated apps, or edit the Reflection",
    "skill unless the Memory skill itself has a durable maintenance rule to fix.",
    f"Your working directory is {DATA_DIR}. You have a real service token in",
    "$AGENT_TOKEN / $SERVICE_TOKEN and full tools.",
  ])


def build_env() -> dict[str, str]:
  env = dict(os.environ)
  env["DATA_DIR"] = str(DATA_DIR)
  env.setdefault("API_BASE_URL", "http://localhost:8000")
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
  env["CODEX_HOME"] = str(CODEX_HOME)
  env.setdefault(
    "AGENT_BROWSER_PROFILE",
    str(DATA_DIR / "agent-browser-profiles" / "memory"),
  )
  env.setdefault("AGENT_BROWSER_SESSION", "memory")
  return env


def load_global_agent_settings() -> dict:
  path = DATA_DIR / "shared" / "agent-settings.json"
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def _model_belongs_to_other_provider(model: str, provider: str) -> bool:
  for known_provider, models in KNOWN_MODELS_BY_PROVIDER.items():
    if known_provider != provider and model in models:
      return True
  return False


def _clean_agent_choice(
  raw: dict | None,
  *,
  fallback_provider: str | None = None,
  label: str = "settings",
) -> dict | None:
  if not isinstance(raw, dict):
    return None
  provider = raw.get("provider")
  if provider not in ("claude", "codex"):
    provider = fallback_provider if fallback_provider in ("claude", "codex") else None
  if provider not in ("claude", "codex"):
    return None
  model = raw.get("model")
  model = model.strip() if isinstance(model, str) and model.strip() else None
  if model and _model_belongs_to_other_provider(model, provider):
    _log(f"{label} model {model!r} mismatches provider {provider!r}; dropping")
    model = None
  effort = raw.get("effort")
  effort = effort.strip() if isinstance(effort, str) and effort.strip() else None
  return {"provider": provider, "model": model, "effort": effort}


def _same_agent_choice(a: dict | None, b: dict | None) -> bool:
  if not a or not b:
    return False
  return (
    a.get("provider") == b.get("provider")
    and (a.get("model") or None) == (b.get("model") or None)
    and (a.get("effort") or None) == (b.get("effort") or None)
  )


def resolve_agents() -> dict:
  global_settings = load_global_agent_settings()
  raw_background = global_settings.get("background_agents")
  background = raw_background if isinstance(raw_background, dict) else {}
  primary = _clean_agent_choice(
    background.get("primary"),
    fallback_provider=DEFAULT_PROVIDER,
    label="global background primary",
  )
  if primary is None:
    primary = _clean_agent_choice(
      {
        "provider": DEFAULT_PROVIDER,
        "model": global_settings.get("model"),
        "effort": global_settings.get("effort"),
      },
      fallback_provider=DEFAULT_PROVIDER,
      label="global primary",
    )
  if primary is None:
    primary = {"provider": DEFAULT_PROVIDER, "model": None, "effort": None}
  fallback = _clean_agent_choice(
    background.get("fallback"),
    label="global background fallback",
  )
  if _same_agent_choice(primary, fallback):
    fallback = None
  return {"primary": primary, "fallback": fallback}


def _drain_message(sdk_msg, log_fh) -> tuple[bool, bool]:
  """Logs one SDK message and returns (saw_result, result_error)."""
  saw_result = False
  result_error = False
  kind = type(sdk_msg).__name__
  try:
    if kind == "AssistantMessage":
      for block in getattr(sdk_msg, "content", []):
        bkind = type(block).__name__
        if bkind == "ToolUseBlock":
          preview = json.dumps(getattr(block, "input", {}), ensure_ascii=True)
          log_fh.write(f"  · tool {getattr(block, 'name', '?')}: {preview[:200]}\n")
        elif bkind == "TextBlock":
          text = (getattr(block, "text", "") or "").strip()
          if text:
            log_fh.write(f"  > {text[:500]}\n")
    elif kind == "ResultMessage":
      saw_result = True
      result_error = bool(getattr(sdk_msg, "is_error", False))
      if result_error:
        result = getattr(sdk_msg, "result", "")
        log_fh.write(f"  ! result error: {str(result)[:500]}\n")
      log_fh.write(
        f"  = turn result (cost_usd={getattr(sdk_msg, 'total_cost_usd', None)})\n"
      )
    log_fh.flush()
  except OSError:
    pass
  return saw_result, result_error


class _LogBroadcast:
  def __init__(self, log_fh):
    self.log_fh = log_fh

  def publish(self, event: dict) -> None:
    if self.log_fh is None:
      return
    try:
      kind = event.get("type") if isinstance(event, dict) else None
      if kind == "text":
        text = (event.get("content") or "").strip()
        if text:
          self.log_fh.write(f"  > {text[:500]}\n")
      elif kind in ("tool_start", "tool_output", "error", "session_init"):
        self.log_fh.write(
          "  · codex "
          + json.dumps(event, ensure_ascii=True, default=str)[:500]
          + "\n"
        )
      self.log_fh.flush()
    except OSError:
      pass


def _safety_snapshot(label: str) -> None:
  if not Path(PM_COMMIT).exists():
    return
  try:
    proc = subprocess.run(
      [PM_COMMIT, "--allow-broad", label],
      cwd=str(DATA_DIR),
      capture_output=True,
      text=True,
      timeout=120,
    )
    if proc.returncode == 0:
      _log("pre-run safety snapshot committed (or no-op)")
    else:
      _log(f"WARN pre-run snapshot rc={proc.returncode}: {(proc.stderr or '')[:200]}")
  except Exception as exc:  # noqa: BLE001 - cron guard
    _log(f"WARN pre-run snapshot failed: {exc!r}")


def _rebuild_graph() -> int:
  if not Path(BUILD_GRAPH).exists():
    _log("WARN graph builder missing; skipping final rebuild")
    return 0
  try:
    proc = subprocess.run(
      ["python3", BUILD_GRAPH],
      cwd=str(DATA_DIR),
      capture_output=True,
      text=True,
      timeout=300,
    )
  except Exception as exc:  # noqa: BLE001 - cron guard
    _log(f"ERROR graph rebuild crashed: {exc!r}")
    return 1
  output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
  if output:
    _log(f"graph rebuild output: {output[:1000]}")
  return proc.returncode


async def _run_claude_session(
  *,
  choice: dict,
  goal: str,
  skill_text: str,
  env: dict[str, str],
  max_turns: int,
  log_fh,
) -> int:
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

  options_kwargs: dict = {
    "system_prompt": skill_text,
    "cwd": str(DATA_DIR),
    "env": env,
    "setting_sources": None,
    "permission_mode": "bypassPermissions",
    "max_turns": max_turns,
    "cli_path": CLI_PATH,
    "disallowed_tools": [
      "PushNotification",
      "ToolSearch",
      "Workflow",
      "ScheduleWakeup",
    ],
  }
  if choice.get("model"):
    options_kwargs["model"] = choice["model"]
  if choice.get("effort"):
    options_kwargs["effort"] = choice["effort"]
  options = ClaudeAgentOptions(**options_kwargs)
  client = ClaudeSDKClient(options)
  try:
    try:
      await asyncio.wait_for(client.connect(), timeout=60.0)
    except asyncio.TimeoutError:
      _log("ERROR SDK connect timed out after 60s")
      return 1
    await client.query(goal)
    saw_result = False
    result_error = False
    async for sdk_msg in client.receive_response():
      msg_saw_result, msg_error = _drain_message(sdk_msg, log_fh)
      saw_result = saw_result or msg_saw_result
      result_error = result_error or msg_error
    if not saw_result:
      _log("ERROR stream ended without a terminal ResultMessage")
      return 1
    if result_error:
      _log("ERROR Memory agent ended with a model/turn error")
      return 64
    return 0
  finally:
    try:
      await client.disconnect()
    except Exception:
      pass


async def _run_codex_session(
  *,
  choice: dict,
  goal: str,
  skill_text: str,
  env: dict[str, str],
  log_fh,
) -> int:
  try:
    from app.codex_sdk_runner import run_codex_sdk_turn
    result = await run_codex_sdk_turn(
      user_message=goal,
      session_id=None,
      base_env=env,
      cwd=str(DATA_DIR),
      chat_id="memory-scheduled",
      bc=_LogBroadcast(log_fh),
      pending_questions={},
      db=None,
      agent_settings={
        "model": choice.get("model"),
        "effort": choice.get("effort"),
      },
      system_prompt=skill_text,
    )
  except Exception as exc:
    _log(f"ERROR codex runner failed: {exc!r}")
    return 1
  if result.get("error"):
    _log(f"WARN codex run ended in error: {str(result.get('error') or '')[:500]}")
    return 64
  _log(
    "codex run complete "
    f"session_id={result.get('session_id') or '(none)'} "
    f"cost_usd={result.get('cost_usd')}"
  )
  return 0


async def _run_agent_choice(
  choice: dict,
  *,
  goal: str,
  skill_text: str,
  env: dict[str, str],
  max_turns: int,
  log_fh,
) -> int:
  if choice.get("provider") == "codex":
    return await _run_codex_session(
      choice=choice, goal=goal, skill_text=skill_text, env=env, log_fh=log_fh,
    )
  return await _run_claude_session(
    choice=choice, goal=goal, skill_text=skill_text, env=env,
    max_turns=max_turns, log_fh=log_fh,
  )


async def run() -> int:
  skill_text = load_skill()
  goal = build_goal()
  env = build_env()
  agents = resolve_agents()
  primary = agents["primary"]
  fallback = agents.get("fallback")
  max_turns = int(os.environ.get("MEMORY_MAX_TURNS") or DEFAULT_MAX_TURNS)
  _log(
    f"start provider={primary['provider']} model={primary.get('model') or '(default)'} "
    f"max_turns={max_turns} cwd={DATA_DIR}"
  )
  _safety_snapshot(f"memory: pre-run safety snapshot {date.today().isoformat()}")

  LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
  with LOG_PATH.open("a", encoding="utf-8") as log_fh:
    rc = await _run_agent_choice(
      primary, goal=goal, skill_text=skill_text, env=env,
      max_turns=max_turns, log_fh=log_fh,
    )
    if rc != 0 and fallback is not None:
      _log(
        f"primary background agent failed rc={rc}; trying fallback "
        f"provider={fallback['provider']} model={fallback.get('model') or '(default)'}"
      )
      rc = await _run_agent_choice(
        fallback, goal=goal, skill_text=skill_text, env=env,
        max_turns=max_turns, log_fh=log_fh,
      )
    if rc != 0:
      return rc

  graph_rc = _rebuild_graph()
  if graph_rc != 0:
    _log(f"ERROR graph rebuild failed rc={graph_rc}")
    return graph_rc
  _log("done")
  return 0


def main() -> None:
  try:
    rc = asyncio.run(run())
  except Exception as exc:  # noqa: BLE001 - top-level cron guard
    _log(f"ERROR memory run crashed: {exc!r}")
    rc = 1
  sys.exit(rc)


if __name__ == "__main__":
  main()
