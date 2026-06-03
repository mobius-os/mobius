#!/usr/bin/env python3
"""Standalone multi-turn runner for the nightly Dreaming pass.

This is the autonomous, unattended cousin of `app.claude_sdk_runner`.
It runs ONE goal-driven Dreaming session: the dreaming skill becomes
the system prompt, a short "dreaming goal" becomes the first user
message, and the SDK drives a long multi-turn loop with the FULL tool
surface (Bash, Read, Write, Edit, WebSearch, agent-browser, ...) until
the goal loop ends.

Why this is its own runner (not `run_claude_sdk_turn`):

  - **Unattended, not interactive.** There is no partner watching, no
    SSE wire, no `ChatBroadcast`, no `AskUserQuestion` round-trip. The
    production runner's whole control surface (pending-questions
    registry, Stop handle, per-token broadcast events, persistence
    actor) exists to serve a live chat. Dreaming has none of that — it
    streams to a log file and returns when done. Reusing the chat
    runner would mean stubbing every one of those collaborators.
  - **Isolation, like recovery.** `recover_chat_runner.py` is
    deliberately kept off the production chat path so a broken chat
    stack can't take down recovery. Dreaming follows the same instinct
    for the opposite reason: a long autonomous run that forks chats,
    rewrites the Mind graph, and edits skills should not share mutable
    state (registries, the writer actor, active-client maps) with the
    daytime chat path it may be operating on while the partner sleeps.
  - **No tool gating, no sandbox.** The v1 Dreaming agent ran
    token-less and Bash-less against a staging copy. This runner is the
    opposite: full capability, instructed (not policed) by the skill,
    per Möbius's "code empowers the agent; it does not police it." The
    wrapper (`core-apps/dreaming/fetch.sh`) owns no-overlap + timeout +
    the activity emit; the SKILL owns what the agent does.

The SDK call mirrors `app.claude_sdk_runner._run_once`: same
`ClaudeAgentOptions` shape (custom `system_prompt` string, NOT a
preset+append — Möbius owns its system prompt end-to-end), same
`cli_path`, same `setting_sources=None`. The differences are
deliberate and all in service of "autonomous":

  - `permission_mode="bypassPermissions"` — no `can_use_tool`
    callback, no keepalive hook. Every tool auto-runs; there is no
    user to approve anything.
  - `max_turns` is high (default 60) so the multi-phase run
    (interviews → skill edits → Mind consolidation → app fixes →
    research → brief + morning chat) fits in one goal loop.
  - We loop on `client.receive_response()` exactly once: the SDK's
    own `max_turns` is the multi-turn budget, and a single
    `query(goal)` + drain runs the whole autonomous session. We do
    NOT re-`query()` — the agent decides its own sub-steps via tool
    use within that budget.

Importability: every heavyweight import (the SDK) is inside `run()`,
so `import backend.scripts.dreaming_runner` (and `py_compile`) works
in any environment, including one without `claude-agent-sdk`
installed. The module-level surface is stdlib only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# --- Fixed locations (the container's data layout) -------------------
# These are intentionally hard-coded rather than env-derived: Dreaming
# runs from cron with a near-empty environment, and the wrapper exports
# only the few vars the agent's own shell needs (SERVICE_TOKEN,
# API_BASE_URL, CLAUDE_CONFIG_DIR). The runner's own paths are a
# deployment constant, not per-instance state — same posture as
# recover_chat_runner.py's module-level Path constants.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILL_PATH = DATA_DIR / "shared" / "skills" / "dreaming.md"
SETTINGS_PATH = DATA_DIR / "apps" / "dreaming" / "settings.json"
LOG_PATH = DATA_DIR / "cron-logs" / "dreaming.log"
CLAUDE_CONFIG_DIR = DATA_DIR / "cli-auth" / "claude"
CODEX_HOME = DATA_DIR / "cli-auth" / "codex"
CLI_PATH = "/usr/local/bin/claude"

# The brief template is baked into the image at /app/scripts; the agent runs
# with cwd=/data and the SDK Read tool is scoped to that subtree, so a Read of
# the /app path fails ("result error: error") even though the file is
# world-readable — which is exactly what stranded the 2026-06-03 run in phase 6.
# Seed a copy into the agent's own domain each run and point the skill there.
BAKED_BRIEF_TEMPLATE = Path("/app/scripts/dreaming-brief-template.html")
BRIEF_TEMPLATE_DEST = DATA_DIR / "apps" / "dreaming" / "dreaming-brief-template.html"

# Multi-turn budget for the whole nightly goal loop. High because one
# Dreaming run spans many phases (interviews, skill edits, graph
# consolidation, app fixes, research, brief + morning chat), each
# costing several tool turns. The wrapper's `timeout` is the real
# wall-clock bound; this is the SDK-side ceiling so a wedged loop can't
# spin forever even if the timeout were removed.
DEFAULT_MAX_TURNS = 60

# Default provider/model when settings.json doesn't pin one. Dreaming
# defaults to Claude (the production default provider); the owner can
# override per-instance via /data/apps/dreaming/settings.json without
# touching code.
DEFAULT_PROVIDER = "claude"


def _log(message: str) -> None:
  """Appends one timestamped line to the dreaming log.

  Uses a bare append (not the logging module) so this works before
  any handler is configured and stays readable interleaved with the
  SDK's own streamed lines. Best-effort: a logging failure must never
  abort the run (the wrapper's heartbeats are the liveness signal).
  """
  try:
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
      fh.write(f"[{stamp}] dreaming_runner: {message}\n")
  except OSError:
    pass


def load_skill() -> str:
  """Returns the dreaming skill text used as the system prompt.

  The agent-editable skill at /data/shared/skills/dreaming.md is the
  source of truth (it can rewrite itself between runs). If it is
  missing — a fresh instance whose init_skills.py hasn't run, or a
  removed file — fall back to the baked seed so the run still has a
  contract, rather than starting with an empty system prompt (which
  the SDK transport would serialize as `--system-prompt ""`, wiping
  any default).
  """
  if SKILL_PATH.is_file():
    try:
      text = SKILL_PATH.read_text(encoding="utf-8")
      if text.strip():
        return text
    except OSError:
      pass
  for fallback in (
    Path("/app/scripts/seed-skills/dreaming.md"),
    Path(__file__).resolve().parent / "seed-skills" / "dreaming.md",
  ):
    if fallback.is_file():
      try:
        return fallback.read_text(encoding="utf-8")
      except OSError:
        continue
  raise FileNotFoundError(
    f"dreaming skill not found at {SKILL_PATH} or any baked fallback"
  )


def seed_brief_template() -> None:
  """Copies the baked brief template into the agent's /data domain.

  The SDK Read tool can't reach /app under cwd=/data, so the agent must
  read the template from a /data path. Refresh it every run (best-effort)
  so template improvements in the image propagate; a missing baked source
  or a copy failure is non-fatal — the skill's phase-6 fallback writes a
  plain brief if the template isn't readable.
  """
  src = BAKED_BRIEF_TEMPLATE
  if not src.is_file():
    src = Path(__file__).resolve().parent / "dreaming-brief-template.html"
  if not src.is_file():
    _log("brief template not found in image — phase 6 will use the fallback")
    return
  try:
    BRIEF_TEMPLATE_DEST.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_TEMPLATE_DEST.write_bytes(src.read_bytes())
  except OSError as exc:
    _log(f"could not seed brief template to {BRIEF_TEMPLATE_DEST}: {exc!r}")


def load_settings() -> dict:
  """Reads /data/apps/dreaming/settings.json, tolerating absence/corruption.

  This is the SAME file the Dreaming mini-app writes (cron hour,
  verbosity, exclude_apps). Provider/model selection adds two optional
  keys — `provider` ("claude" | "codex") and `model` (a provider model
  id) — so the owner can steer which agent dreams without a code
  change. Missing or malformed → {} (the caller applies defaults).
  """
  if not SETTINGS_PATH.is_file():
    return {}
  try:
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def _resolve_model(settings: dict) -> tuple[str, str | None, str | None]:
  """Returns (provider, model, effort) from settings.json + defaults.

  Provider defaults to Claude. Model/effort are passed through only
  when present and self-consistent: a model that belongs to the OTHER
  provider is dropped (a cross-provider id would surface as an obscure
  SDK error) — same defensive normalization the chat runners do. Both
  may be None, in which case the SDK uses its own account default.
  """
  provider = settings.get("provider") or DEFAULT_PROVIDER
  if provider not in ("claude", "codex"):
    provider = DEFAULT_PROVIDER
  model = settings.get("model") or None
  effort = settings.get("effort") or None
  if model:
    # Reject a model that clearly belongs to the other provider. We
    # avoid importing app.providers (keeps this runner importable
    # without the backend package on sys.path) and inline the short
    # known-model check instead.
    other = {
      "claude": ("gpt-5.5", "gpt-5.4"),
      "codex": (
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6",
      ),
    }.get(provider, ())
    if model in other:
      _log(f"settings model {model!r} mismatches provider {provider!r}; dropping")
      model = None
  return provider, model, effort


def build_goal(settings: dict) -> str:
  """Builds the first user message — the 'dreaming goal' that kicks off the loop.

  The skill (system prompt) holds the full procedure; this message is
  just the GO signal plus the run-specific context pointers the agent
  needs to start: today's date, where the wrapper staged its inputs,
  the verbosity the owner picked, and the apps they excluded. Keep it
  short — the skill is the contract, the goal is the trigger.
  """
  from datetime import date
  today = date.today().isoformat()
  verbosity = settings.get("verbosity") or "standard"
  exclude = settings.get("exclude_apps") or []
  inputs_dir = DATA_DIR / "apps" / "dreaming" / "inputs"
  lines = [
    f"It is the night of {today}. Begin tonight's Dreaming run.",
    "",
    "Follow your skill (your system prompt) end-to-end: interview every "
    "agent that worked in the last 24h, improve the skills from what you "
    "learn, consolidate the Mind graph, fix and harden the apps, do any "
    "predictable research, then write the brief and open the morning chat.",
    "",
    f"The wrapper has staged this run's context under {inputs_dir}/ — "
    "read it first (activity.jsonl for the last 24h of platform events, "
    "chats.md for the recent chats list, prev-report.html for yesterday's "
    "brief so you don't repeat yourself).",
    "",
    f"Your working directory is {DATA_DIR}. You have a real token "
    "($AGENT_TOKEN / $SERVICE_TOKEN) and full tools — no sandbox. Commit "
    "as you go with pm-commit. Time-box: if you run long, finish the "
    "current chunk, then jump to writing the brief and opening the "
    "morning chat so the partner always wakes to something.",
    "",
    f"Owner preferences for this run: verbosity={verbosity}.",
  ]
  if exclude:
    lines.append(
      f"The owner asked you to SKIP these apps tonight: {', '.join(map(str, exclude))}."
    )
  return "\n".join(lines)


def build_env() -> dict[str, str]:
  """Builds the environment the SDK subprocess inherits.

  Starts from the current process env (the wrapper already exported
  SERVICE_TOKEN, API_BASE_URL, AGENT_TOKEN, etc.) and pins the CLI
  credential dirs so the spawned `claude` / `codex` binary finds the
  same auth the chat path uses. Mirrors `ClaudeProvider.build_env` /
  `CodexProvider.build_env` for the two vars that matter to the CLI,
  without importing the backend package.
  """
  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
  env["CODEX_HOME"] = str(CODEX_HOME)
  # A stable agent-browser profile for the night so repeated screenshots
  # warm one cache instead of cold-starting Chromium each time. Keyed to
  # "dreaming" so it never collides with a per-chat profile.
  env.setdefault(
    "AGENT_BROWSER_PROFILE",
    str(DATA_DIR / "agent-browser-profiles" / "dreaming"),
  )
  env.setdefault("AGENT_BROWSER_SESSION", "dreaming")
  return env


def _drain_message(sdk_msg, log_fh) -> None:
  """Writes a one-line digest of one SDK message to the log.

  Unattended runs have no UI, so the log IS the trace. We don't need
  the full event matrix the chat dispatcher builds (no SSE consumer);
  a compact human-readable line per assistant text block, tool use,
  and result is enough to debug a night after the fact. Best-effort:
  a formatting error on one message never aborts the drain.
  """
  try:
    from claude_agent_sdk.types import (
      AssistantMessage,
      ResultMessage,
      TextBlock,
      ThinkingBlock,
      ToolUseBlock,
    )
  except Exception:
    return

  def write(line: str) -> None:
    try:
      log_fh.write(line.rstrip() + "\n")
      log_fh.flush()
    except OSError:
      pass

  if isinstance(sdk_msg, AssistantMessage):
    for block in sdk_msg.content:
      if isinstance(block, ToolUseBlock):
        # One line naming the tool + a short input preview, so the log
        # reads as a sequence of actions taken during the night.
        preview = json.dumps(block.input, ensure_ascii=True)[:200]
        write(f"  · tool {block.name}: {preview}")
      elif isinstance(block, TextBlock):
        text = (block.text or "").strip()
        if text:
          write(f"  > {text[:500]}")
      elif isinstance(block, ThinkingBlock):
        thinking = (block.thinking or "").strip()
        if thinking:
          write(f"  ~ {thinking[:200]}")
    return

  if isinstance(sdk_msg, ResultMessage):
    if sdk_msg.is_error:
      err = sdk_msg.result if isinstance(sdk_msg.result, str) else "error"
      write(f"  ! result error: {err}")
    cost = sdk_msg.total_cost_usd
    write(f"  = turn result (cost_usd={cost})")


async def run() -> int:
  """Runs the whole Dreaming session and returns a process exit code.

  Returns 0 on a clean run (the SDK reached a terminal result, error
  or not — an agent that decides "quiet night, nothing to do" is a
  success), 1 on an infrastructure failure (skill missing, SDK couldn't
  start, an unexpected exception). The wrapper maps the exit code into
  the `cron_outcome` event, so this is the one signal the activity log
  records about whether the night ran.
  """
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

  settings = load_settings()
  provider, model, effort = _resolve_model(settings)
  skill_text = load_skill()
  seed_brief_template()
  goal = build_goal(settings)
  env = build_env()
  max_turns = int(settings.get("max_turns") or DEFAULT_MAX_TURNS)

  _log(
    f"start provider={provider} model={model or '(default)'} "
    f"effort={effort or '(default)'} max_turns={max_turns} cwd={DATA_DIR}"
  )

  # Codex selection is recorded but the autonomous path runs through the
  # Claude SDK only for now: the Codex SDK runner is built around a live
  # TurnHandle + the request_user_input bridge, neither of which an
  # unattended run uses. If the owner pins Codex we log it and proceed on
  # Claude rather than failing the night — the skill is provider-agnostic
  # and the dreaming work (forking sessions, editing files, the API
  # calls) is identical. A dedicated Codex autonomous path can land later
  # if the owner actually wants to dream on Codex.
  if provider == "codex":
    _log("provider=codex requested; autonomous path runs on Claude SDK — proceeding on claude")

  options_kwargs: dict = {
    "system_prompt": skill_text,
    "cwd": str(DATA_DIR),
    "env": env,
    "setting_sources": None,
    "permission_mode": "bypassPermissions",
    "max_turns": max_turns,
    "cli_path": CLI_PATH,
  }
  if model:
    options_kwargs["model"] = model
  if effort:
    options_kwargs["effort"] = effort
  options = ClaudeAgentOptions(**options_kwargs)

  log_fh = None
  try:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG_PATH.open("a", encoding="utf-8")
  except OSError:
    log_fh = None

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
      if log_fh is not None:
        _drain_message(sdk_msg, log_fh)
      # The terminal ResultMessage ends the goal loop. We detect it by
      # class name to avoid a second import here; the drain above already
      # imported the real types for formatting.
      if type(sdk_msg).__name__ == "ResultMessage":
        saw_result = True
        # is_error covers a hard failure AND the max_turns cap (subtype
        # error_max_turns). A night that ended in error must NOT report
        # success — otherwise cron_outcome records exit 0 and both the next
        # run and the Dreaming app believe a brief was produced when none was.
        if getattr(sdk_msg, "is_error", False):
          result_error = True
          _log(f"WARN run ended in error (subtype={getattr(sdk_msg, 'subtype', '?')})")

    if not saw_result:
      _log("WARN stream ended without a terminal ResultMessage")
      return 2
    if result_error:
      return 2
    _log("done")
    return 0
  except Exception as exc:  # noqa: BLE001 — top-level guard for cron
    _log(f"ERROR dreaming run crashed: {exc!r}")
    return 1
  finally:
    try:
      await client.disconnect()
    except Exception:
      pass
    if log_fh is not None:
      try:
        log_fh.close()
      except OSError:
        pass


def main() -> int:
  """Synchronous entry point for the wrapper / CLI."""
  logging.basicConfig(level=logging.INFO)
  try:
    return asyncio.run(run())
  except KeyboardInterrupt:
    _log("interrupted")
    return 130


if __name__ == "__main__":
  sys.exit(main())
