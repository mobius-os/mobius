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
    `query(goal)` + drain runs the whole autonomous session. The
    agent decides its own sub-steps via tool use within that budget;
    the only extra `query()` calls are the turn-budget steering
    messages (below), which speak the turn count into the session
    because the agent cannot observe it on its own.

Two reliability layers protect the brief (the night's one
non-negotiable deliverable), added after three of four prod nights
died at `max_turns` (subtype error_max_turns) with NO brief:

  - **Turn-countdown injection.** The drain loop counts assistant
    turns and injects a steering user message when the run crosses
    the thresholds from `steering_thresholds` (35 and 45 with the
    default 60-turn budget). The skill's "bail to the brief by turn
    40" rule is prose the agent can't act on — it has no view of its
    own turn count — so the runner supplies the number at the moment
    it matters.
  - **Guaranteed-brief fallback.** When the main session still ends
    in error and tonight's brief file is missing, `run()` spawns ONE
    short rescue session (`FALLBACK_MAX_TURNS`) whose only goal is a
    minimal brief + morning chat from whatever the cut-off run left
    behind. The rescue never spawns another rescue.

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
import subprocess
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
# The denylist-guarded `git add -A && git commit` helper baked into the image.
PM_COMMIT = "/app/scripts/pm-commit"

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

# Turn budget for the guaranteed-brief rescue session. Small on
# purpose: read the cut-off run's leavings, write a minimal brief,
# open the morning chat, commit — no investigation.
FALLBACK_MAX_TURNS = 12


def steering_thresholds(max_turns: int) -> tuple[int, int]:
  """Returns the (soft, hard) turn counts that trigger budget steering.

  Scaled from the 60-turn default (35 and 45) so an owner-overridden
  max_turns keeps the same shape: the soft warning lands just past
  halfway, the hard one at three quarters. Integer floor math keeps
  the result deterministic, and the hard threshold always trails the
  soft one by at least one turn so the two messages can't collapse
  into the same turn.
  """
  soft = max(1, max_turns * 35 // 60)
  hard = max(soft + 1, max_turns * 45 // 60)
  return soft, hard


def steering_message(
  prev_turn: int, turn: int, max_turns: int,
) -> str | None:
  """Returns the turn-budget steering text to inject, or None.

  Pure crossing detector: fires when the (prev_turn, turn] step
  crosses a threshold from `steering_thresholds`. The skill tells the
  agent to bail by turn 40 to the night's TWO floor deliverables — a
  drained memory inbox and a shipped brief, in that order — but the
  agent cannot observe its own turn count, so the runner counts
  assistant turns and speaks the number into the session at the right
  moments. Both messages protect the inbox drain (phase 3a): the
  graph froze for days when the old steering said "phases 1-5 are
  over" and the agent skipped consolidation entirely. The drain is
  cheap and should already be done by these thresholds if the night
  was ordered right; the steering is the backstop for a night that
  ran long before reaching it. When a single step crosses both
  thresholds, only the sterner message is returned (two back-to-back
  budget warnings would dilute each other).
  """
  soft, hard = steering_thresholds(max_turns)
  if prev_turn < hard <= turn:
    return (
      f"TURN BUDGET: you are at turn {turn} of {max_turns} and almost "
      "out. Two floor deliverables, in order: if the memory inbox "
      "isn't drained yet, fold each remaining inbox.md line into the "
      "graph, empty the inbox, and commit (be quick) — THEN write a "
      "MINIMAL brief (a heading and a few honest lines on what was "
      "done and what was cut off), save it to the reports dir, open "
      "the morning chat, and stop. Skip everything else."
    )
  if prev_turn < soft <= turn:
    return (
      f"TURN BUDGET: you are at turn {turn} of {max_turns}. STOP "
      "open-ended investigation now. If you haven't drained the "
      "memory inbox yet (phase 3a), do that minimal drain FIRST and "
      "commit it — it is the night's other non-negotiable deliverable "
      "and must not be skipped. Then write the brief and open the "
      "morning chat. The deeper Mind reorg, remaining app triage, and "
      "research are over."
    )
  return None


def todays_brief_path() -> Path | None:
  """Returns tonight's expected brief path, or None when unknowable.

  The brief lands in the Dreaming app's NUMERIC storage dir
  (`/data/apps/<id>/reports/<date>.html`); the numeric id is staged
  by fetch.sh at inputs/app_id before the runner starts. A missing or
  empty stage means the path can't be resolved here — callers treat
  that as "assume no brief" and let the rescue agent resolve the id
  itself.
  """
  app_id_file = DATA_DIR / "apps" / "dreaming" / "inputs" / "app_id"
  try:
    app_id = app_id_file.read_text(encoding="utf-8").strip()
  except OSError:
    return None
  if not app_id:
    return None
  from datetime import date
  return (
    DATA_DIR / "apps" / app_id / "reports"
    / f"{date.today().isoformat()}.html"
  )


def fallback_needed(rc: int, brief_path: Path | None) -> bool:
  """True when the night failed AND left no brief for the partner.

  A clean run (rc 0) wrote its brief per the skill contract — trust
  it. A failed run whose brief is already on disk (the agent shipped
  phase 6 and then crashed) needs no rescue. Everything else does,
  including an unresolvable brief path — one redundant rescue pass
  beats a morning with nothing.
  """
  if rc == 0:
    return False
  if brief_path is None:
    return True
  return not brief_path.is_file()


def build_fallback_goal() -> str:
  """Builds the goal message for the guaranteed-brief rescue pass."""
  from datetime import date
  today = date.today().isoformat()
  runs_dir = DATA_DIR / "apps" / "dreaming" / "runs" / today
  inputs_dir = DATA_DIR / "apps" / "dreaming" / "inputs"
  return "\n".join([
    f"The main Dreaming run of {today} was CUT OFF (turn budget or "
    "crash) before it could deliver the brief. You are a short rescue "
    f"pass with roughly {FALLBACK_MAX_TURNS} turns and ONE goal: the "
    "partner must not wake to nothing.",
    "",
    "Do NOT restart the night's phases. Instead:",
    f"1. Skim what the run left behind: {runs_dir}/ (interviews, "
    f"working notes) and `git -C {DATA_DIR} log --oneline -10` for "
    "what it committed.",
    "2. Write a minimal self-contained HTML brief — a heading plus a "
    "few honest paragraphs covering what got done and what was cut "
    f"off mid-flight — to {DATA_DIR}/apps/$APP_ID/reports/{today}.html, "
    f"where APP_ID is the number in {inputs_dir}/app_id (mkdir -p the "
    "reports dir first).",
    "3. Open the morning chat per your skill's phase 6 with a 2-3 "
    "line summary, note that tonight's run was cut off, and write the "
    "reports meta.json chat link.",
    "4. Commit with pm-commit and stop. The brief file is the one "
    "non-negotiable deliverable; skip anything that threatens it.",
  ])


def _safety_snapshot(label: str) -> None:
  """Best-effort git snapshot of /data BEFORE Dreaming mutates anything.

  The nightly run consolidates the memory graph, rewrites skills, and fixes
  apps — destructive overwrites of agent-owned files under /data/shared that
  the "git is the undo" contract (mind.md) promises are recoverable. Until now
  that promise rested entirely on the agent's own `pm-commit` discipline
  MID-run, so a consolidation that overwrote a note before the first commit had
  no pre-state restore point beyond LAST night's. Committing the current tree as
  the very first thing the run does guarantees one.

  `--allow-broad` so a full day's accumulated changes aren't refused by
  pm-commit's 50-file guard; a no-op (nothing changed) exits 0. Any failure is
  logged and swallowed — a snapshot must NEVER block the night's run.
  """
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
      _log(
        f"WARN pre-run snapshot rc={proc.returncode}: "
        f"{(proc.stderr or '').strip()[:200]}"
      )
  except Exception as exc:
    _log(f"WARN pre-run snapshot failed: {exc!r}")


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
  exclude_apps). Provider/model selection adds two optional
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
  the GO signal plus the run-specific context pointers the agent needs:
  today's date, where the wrapper staged its inputs (including the new
  per-app-digest.json), and which apps to skip. Keep it short — the
  skill is the contract, the goal is the trigger.
  """
  from datetime import date
  today = date.today().isoformat()
  exclude = settings.get("exclude_apps") or []
  inputs_dir = DATA_DIR / "apps" / "dreaming" / "inputs"
  lines = [
    f"It is the night of {today}. Begin tonight's Dreaming run.",
    "",
    f"Staged context is under {inputs_dir}/ — start here:",
    "  - dreaming-run-history.txt  YOUR OWN recent runs (exit codes, log",
    "                          friction, your last skill edits) — read FIRST; a",
    "                          recurring failure across nights is tonight's",
    "                          first fix (phase 0 / phase 2).",
    "  - per-app-digest.json   compact analytics digest (opens_24h, signal",
    "                          counts, app_errors_24h + recent_app_errors for",
    "                          UNCAUGHT crashes, last_5_errors for signalled",
    "                          ones) — read to orient phase 4",
    "  - changed-since-last-run.txt  /data files changed since your last run",
    "  - activity.jsonl        last 24h of raw platform events",
    "  - chats.md              recent chats list (fork + interview these)",
    "  - prev-report.html      yesterday's brief (don't repeat yourself)",
    "",
    "Follow your skill (your system prompt) for the full procedure. "
    "Two floor deliverables: drain the memory inbox (phase 3a) EARLY "
    "— before app triage — then ship the brief (phase 6). At turn 40 "
    "cut any unfinished deep work; if the inbox still has lines, do "
    "the minimal drain first, then the brief, so the partner wakes to "
    "something and the graph never freezes.",
    "",
    f"Your working directory is {DATA_DIR}. You have a real token "
    "($AGENT_TOKEN / $SERVICE_TOKEN) and full tools — no sandbox. "
    "Commit as you go with pm-commit.",
  ]
  if exclude:
    lines.append(
      f"\nThe owner asked you to SKIP these apps tonight: {', '.join(map(str, exclude))}."
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


class _LogBroadcast:
  """Minimal broadcast shim for the unattended Codex runner path."""

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


async def _drain_session(
  client, log_fh, *, max_turns: int, countdown: bool,
) -> tuple[bool, bool]:
  """Drains one SDK response stream to its terminal result.

  Counts assistant turns and, when `countdown` is on, injects the
  turn-budget steering text from `steering_message` as a user message
  into the live session — `client.query` writes to the streaming
  stdin, and the CLI hands queued user input to the model between
  tool iterations of the in-flight loop. Message types are detected
  by class NAME so the drain avoids a second SDK import and works
  against test fakes; `_drain_message` already imported the real
  types for log formatting.

  Returns (saw_result, result_error).
  """
  turns_seen = 0
  saw_result = False
  result_error = False
  async for sdk_msg in client.receive_response():
    if log_fh is not None:
      _drain_message(sdk_msg, log_fh)
    kind = type(sdk_msg).__name__
    if kind == "AssistantMessage":
      prev_turn = turns_seen
      turns_seen += 1
      if countdown:
        steer = steering_message(prev_turn, turns_seen, max_turns)
        if steer is not None:
          # Best-effort: a failed injection leaves the run no worse
          # than before this layer existed (the fallback still
          # guarantees the brief), so log and keep draining.
          try:
            await client.query(steer)
            _log(
              "injected turn-budget steering at turn "
              f"{turns_seen}/{max_turns}"
            )
          except Exception as exc:
            _log(f"WARN steering injection failed: {exc!r}")
    if kind == "ResultMessage":
      saw_result = True
      # is_error covers a hard failure AND the max_turns cap (subtype
      # error_max_turns). A night that ended in error must NOT report
      # success — otherwise cron_outcome records exit 0 and both the
      # next run and the Dreaming app believe a brief was produced
      # when none was.
      if getattr(sdk_msg, "is_error", False):
        result_error = True
        _log(
          "WARN run ended in error "
          f"(subtype={getattr(sdk_msg, 'subtype', '?')})"
        )
  return saw_result, result_error


async def _run_claude_session(
  *,
  goal: str,
  skill_text: str,
  env: dict[str, str],
  model: str | None,
  effort: str | None,
  max_turns: int,
  log_fh,
  countdown: bool,
) -> int:
  """Runs one Claude SDK goal loop and returns a process exit code.

  `countdown=True` enables turn-budget steering (the main nightly
  run). The rescue pass runs with it off — its budget is tiny and its
  goal already IS "write the brief".
  """
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

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

  client = ClaudeSDKClient(options)
  try:
    try:
      await asyncio.wait_for(client.connect(), timeout=60.0)
    except asyncio.TimeoutError:
      _log("ERROR SDK connect timed out after 60s")
      return 1

    await client.query(goal)
    saw_result, result_error = await _drain_session(
      client, log_fh, max_turns=max_turns, countdown=countdown,
    )
    if not saw_result:
      _log("WARN stream ended without a terminal ResultMessage")
      return 2
    if result_error:
      return 2
    return 0
  except Exception as exc:  # noqa: BLE001 — top-level guard for cron
    _log(f"ERROR dreaming run crashed: {exc!r}")
    return 1
  finally:
    try:
      await client.disconnect()
    except Exception:
      pass


async def _run_codex_session(
  *,
  goal: str,
  skill_text: str,
  env: dict[str, str],
  model: str | None,
  effort: str | None,
  log_fh,
) -> int:
  """Runs one Codex SDK goal loop and returns a process exit code.

  Codex can run the same Dreaming skill through the app-server SDK
  path. The normal chat runner publishes SSE; Dreaming swaps in a
  log-only broadcast so unattended runs still leave a useful trace.
  Codex has no max_turns option — the wrapper's wall-clock timeout is
  its hard bound, and the goal text carries any turn guidance.
  """
  try:
    from app.codex_sdk_runner import run_codex_sdk_turn
    result = await run_codex_sdk_turn(
      user_message=goal,
      session_id=None,
      base_env=env,
      cwd=str(DATA_DIR),
      chat_id="dreaming-nightly",
      bc=_LogBroadcast(log_fh),
      pending_questions={},
      db=None,
      agent_settings={
        "model": model,
        "effort": effort,
      },
      system_prompt=skill_text,
    )
  except Exception as exc:
    _log(f"ERROR codex runner failed: {exc!r}")
    return 1
  if result.get("error"):
    _log(f"WARN codex run ended in error: {result.get('error')}")
    return 1
  _log(
    "codex run complete "
    f"session_id={result.get('session_id') or '(none)'} "
    f"cost_usd={result.get('cost_usd')}"
  )
  return 0


async def _maybe_write_fallback_brief(
  rc: int,
  *,
  provider: str,
  skill_text: str,
  env: dict[str, str],
  model: str | None,
  effort: str | None,
  log_fh,
) -> None:
  """Guaranteed-brief layer: rescues a failed night that has no brief.

  Three of four prod nights died at max_turns (subtype
  error_max_turns) with NO brief — the partner woke to nothing. When
  the main session ends non-zero and tonight's brief file is missing,
  spawn one short rescue session whose only goal is a minimal brief +
  morning chat built from whatever the cut-off run left behind.

  Recursion guard: this helper is called exactly once, from run(),
  after the MAIN session only. The rescue session it spawns goes
  through the plain session helpers (countdown off, no further
  fallback), so a failing rescue ends the night instead of recursing.
  Best-effort throughout — the rescue must never turn a recorded
  failure into a crash, and the main run's exit code is preserved
  either way so cron_outcome stays honest about the night.
  """
  try:
    brief = todays_brief_path()
    if not fallback_needed(rc, brief):
      return
    _log(
      f"main run failed (rc={rc}) with no brief at "
      f"{brief or '(unresolved path)'} — running guaranteed-brief "
      "fallback"
    )
    goal = build_fallback_goal()
    if provider == "codex":
      fallback_rc = await _run_codex_session(
        goal=goal, skill_text=skill_text, env=env,
        model=model, effort=effort, log_fh=log_fh,
      )
    else:
      fallback_rc = await _run_claude_session(
        goal=goal, skill_text=skill_text, env=env, model=model,
        effort=effort, max_turns=FALLBACK_MAX_TURNS, log_fh=log_fh,
        countdown=False,
      )
    wrote = brief is not None and brief.is_file()
    _log(
      f"guaranteed-brief fallback finished rc={fallback_rc} "
      f"brief_written={'yes' if wrote else 'no'}"
    )
  except Exception as exc:
    _log(f"ERROR guaranteed-brief fallback crashed: {exc!r}")


async def run() -> int:
  """Runs the whole Dreaming session and returns a process exit code.

  Returns 0 on a clean run (the SDK reached a terminal result, error
  or not — an agent that decides "quiet night, nothing to do" is a
  success), 1 on an infrastructure failure (skill missing, SDK couldn't
  start, an unexpected exception), 2 when the goal loop ended in an
  error result (including the max_turns cap). The wrapper maps the
  exit code into the `cron_outcome` event, so this is the one signal
  the activity log records about whether the night ran. A non-zero
  night additionally triggers the guaranteed-brief fallback (which
  never changes the exit code — it rescues the deliverable, not the
  record).
  """
  settings = load_settings()
  provider, model, effort = _resolve_model(settings)
  skill_text = load_skill()
  seed_brief_template()
  goal = build_goal(settings)
  env = build_env()
  max_turns = int(settings.get("max_turns") or DEFAULT_MAX_TURNS)
  log_fh = None
  try:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG_PATH.open("a", encoding="utf-8")
  except OSError:
    log_fh = None

  _log(
    f"start provider={provider} model={model or '(default)'} "
    f"effort={effort or '(default)'} max_turns={max_turns} cwd={DATA_DIR}"
  )

  # Guaranteed pre-run restore point: commit /data BEFORE the agent consolidates
  # memory / rewrites skills, so "git is the undo" holds even if tonight's run
  # overwrites a note before its own first pm-commit. Best-effort; never blocks.
  from datetime import date
  _safety_snapshot(f"dreaming: pre-run safety snapshot {date.today().isoformat()}")

  try:
    if provider == "codex":
      rc = await _run_codex_session(
        goal=goal, skill_text=skill_text, env=env,
        model=model, effort=effort, log_fh=log_fh,
      )
    else:
      rc = await _run_claude_session(
        goal=goal, skill_text=skill_text, env=env, model=model,
        effort=effort, max_turns=max_turns, log_fh=log_fh,
        countdown=True,
      )
    if rc == 0:
      _log("done")
    else:
      await _maybe_write_fallback_brief(
        rc, provider=provider, skill_text=skill_text, env=env,
        model=model, effort=effort, log_fh=log_fh,
      )
    return rc
  finally:
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
