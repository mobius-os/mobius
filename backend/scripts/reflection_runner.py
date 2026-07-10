#!/usr/bin/env python3
"""Standalone multi-turn runner for the nightly Reflection pass.

This is the autonomous, unattended cousin of `app.claude_sdk_runner`.
It runs ONE goal-driven Reflection session: the reflection skill becomes
the system prompt, a short "reflection goal" becomes the first user
message, and the SDK drives a long multi-turn loop with the FULL tool
surface (Bash, Read, Write, Edit, WebSearch, agent-browser, ...) until
the goal loop ends.

Why this is its own runner (not `run_claude_sdk_turn`):

  - **Unattended, not interactive.** There is no partner watching, no
    SSE wire, no `ChatBroadcast`, no `AskUserQuestion` round-trip. The
    production runner's whole control surface (pending-questions
    registry, Stop handle, per-token broadcast events, persistence
    actor) exists to serve a live chat. Reflection has none of that — it
    streams to a log file and returns when done. Reusing the chat
    runner would mean stubbing every one of those collaborators.
  - **Isolation, like recovery.** `recover_chat_runner.py` is
    deliberately kept off the production chat path so a broken chat
    stack can't take down recovery. Reflection follows the same instinct
    for the opposite reason: a long autonomous run that forks chats,
    edits skills, reviews app health, and writes a brief should not
    share mutable state (registries, the writer actor, active-client
    maps) with the daytime chat path it may be operating on while the
    partner sleeps.
  - **No tool gating, no sandbox.** The v1 Reflection agent ran
    token-less and Bash-less against a staging copy. This runner is the
    opposite: full capability, instructed (not policed) by the skill,
    per Möbius's "code empowers the agent; it does not police it." The
    wrapper (`app-reflection/fetch.sh` in the catalog app) owns no-overlap + timeout +
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
    (interviews → skill edits → Memory-system review → app fixes →
    research → brief) fits in one goal loop. The brief is the night's
    one deliverable; the conversation about it is opened by the partner
    on tap in the Reflection app, not by this run.
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
    minimal brief from whatever the cut-off run left behind. The
    rescue never spawns another rescue.

Importability: every heavyweight import (the SDK) is inside `run()`,
so `import backend.scripts.reflection_runner` (and `py_compile`) works
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
# These are intentionally hard-coded rather than env-derived: Reflection
# runs from cron with a near-empty environment, and the wrapper exports
# only the few vars the agent's own shell needs (SERVICE_TOKEN,
# API_BASE_URL, CLAUDE_CONFIG_DIR). The runner's own paths are a
# deployment constant, not per-instance state — same posture as
# recover_chat_runner.py's module-level Path constants.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILL_PATH = DATA_DIR / "shared" / "skills" / "reflection.md"
LOG_PATH = DATA_DIR / "cron-logs" / "reflection.log"
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
BAKED_BRIEF_TEMPLATE = Path("/app/scripts/reflection-brief-template.html")
BRIEF_TEMPLATE_DEST = DATA_DIR / "apps" / "reflection" / "reflection-brief-template.html"

# Multi-turn budget for the whole nightly goal loop. High because one
# Reflection run spans many phases (interviews, skill edits, Memory-system
# review, app fixes, research, brief), each costing several tool
# turns. The wrapper's `timeout` is the real wall-clock bound; this is the
# SDK-side ceiling so a wedged loop can't spin forever even if the timeout
# were removed.
DEFAULT_MAX_TURNS = 60

# Default provider/model when settings.json doesn't pin one. Reflection
# defaults to Claude (the production default provider); the owner can
# override per-instance via /data/apps/reflection/settings.json without
# touching code.
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

# Turn budget for the guaranteed-brief rescue session. Small on
# purpose: read the cut-off run's leavings, write a minimal brief,
# commit — no investigation, no chat (the partner opens that on tap).
FALLBACK_MAX_TURNS = 12

# The runner shares the process exit-code space with its wrapper
# (`app-reflection/fetch.sh` in the catalog app), whose OWN config errors take the low
# codes: 2 = no app id, 3 = no service token, 5 = lock skip, 124 = timeout.
# Historically the runner ALSO returned 2/3 for model/usage/auth failures, so
# a usage-limit night surfaced to the owner as "config error (exit 2)" and a
# 401 as "missing service token (exit 3)" — both pointing at the wrong fix.
# The runner therefore uses its OWN error band (>=64) that can never collide
# with a wrapper config code, so the cron_outcome label stays honest about
# whether the night failed on config vs on the model.
GENERIC_MODEL_RC = 64
USAGE_LIMIT_RC = 65
# A CLI auth failure (a 401 / expired credential). Named AUTH_FAILURE_RC for
# the existing call sites; conceptually "provider auth expired." Distinct from
# a generic model error so the guaranteed-brief layer can skip the doomed CLI
# rescue (which would just 401 again and burn the budget) and have the Python
# runner write a static brief itself, so a brief lands even when the model is
# unreachable. The CLI mislabels a 401 ResultMessage as subtype="success"
# while setting is_error=True, so the only reliable signal is the error/result
# STRING — `_is_auth_failure` matches it.
AUTH_FAILURE_RC = 66

# Substrings that mark a result/error string as a CLI authentication
# failure. Matched case-insensitively against the ResultMessage's
# error/result text. Confirmed from prod logs (401 on an expired CLI
# credential surfaces these phrasings).
_AUTH_FAILURE_MARKERS = (
  "401",
  "invalid authentication credentials",
  "failed to authenticate",
  "authentication_error",
  "oauth token has expired",
)

# Substrings that mark a result/error string as a provider USAGE/RATE limit
# (a weekly cap, a 429, quota exhaustion) rather than a transient model error.
# A heuristic, matched case-insensitively and kept deliberately conservative:
# a miss just falls through to GENERIC_MODEL_RC and the ordinary CLI rescue,
# so a false negative costs nothing; a false positive would only skip a rescue
# that was likely doomed anyway.
_USAGE_LIMIT_MARKERS = (
  "usage limit",
  "rate limit",
  "rate_limit",
  "429",
  "quota",
  "too many requests",
  "weekly limit",
)


def _is_auth_failure(text: str | None) -> bool:
  """True when a result/error string names a CLI authentication failure.

  The CLI returns a ResultMessage with is_error=True but a misleading
  subtype="success" on a 401, so subtype is useless for routing. The
  error/result STRING is the only honest signal — match the known 401
  phrasings (see `_AUTH_FAILURE_MARKERS`) case-insensitively.
  """
  if not text:
    return False
  low = text.lower()
  return any(marker in low for marker in _AUTH_FAILURE_MARKERS)


def _is_usage_limit(text: str | None) -> bool:
  """True when a result/error string names a provider usage/rate limit.

  Companion to `_is_auth_failure`: an account that has hit its weekly cap
  won't recover within the night, so a CLI rescue would just burn budget on
  another blocked call. Routing this to USAGE_LIMIT_RC both labels the night
  honestly and lets the guaranteed-brief layer write a static "blocked night"
  brief instead of a doomed rescue. See `_USAGE_LIMIT_MARKERS`.
  """
  if not text:
    return False
  low = text.lower()
  return any(marker in low for marker in _USAGE_LIMIT_MARKERS)


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
  agent to bail by turn 40 to the night's floor deliverable — a shipped
  brief — but the agent cannot observe its own turn count, so the runner
  counts assistant turns and speaks the number into the session at the
  right moments. When a single step crosses both thresholds, only the
  sterner message is returned (two back-to-back budget warnings would
  dilute each other).
  """
  soft, hard = steering_thresholds(max_turns)
  if prev_turn < hard <= turn:
    return (
      f"TURN BUDGET: you are at turn {turn} of {max_turns} and almost "
      "out. Stop open-ended investigation now and write a MINIMAL "
      "brief (a heading and a few honest lines on what was done, what "
      "was skipped, and what needs the partner), save it to the reports "
      "dir, commit it, and stop. Skip everything else."
    )
  if prev_turn < soft <= turn:
    return (
      f"TURN BUDGET: you are at turn {turn} of {max_turns}. STOP "
      "open-ended investigation now. Commit whatever safe work is done, "
      "then write the brief. Remaining app triage, Memory-system review, "
      "and research are over unless needed for one brief sentence."
    )
  return None


def todays_brief_path() -> Path | None:
  """Returns tonight's expected brief path, or None when unknowable.

  The brief lands in the Reflection app's NUMERIC storage dir
  (`/data/apps/<id>/reports/<date>.html`); the numeric id is staged
  by fetch.sh at inputs/app_id before the runner starts. A missing or
  empty stage means the path can't be resolved here — callers treat
  that as "assume no brief" and let the rescue agent resolve the id
  itself.
  """
  app_id_file = DATA_DIR / "apps" / "reflection" / "inputs" / "app_id"
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
  runs_dir = DATA_DIR / "apps" / "reflection" / "runs" / today
  inputs_dir = DATA_DIR / "apps" / "reflection" / "inputs"
  return "\n".join([
    f"The main Reflection run of {today} was CUT OFF (turn budget or "
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
    "reports dir first). The partner opens the conversation about the "
    "brief on tap in the Reflection app — you do NOT create a chat.",
    "3. Commit with pm-commit and stop. The brief file is the one "
    "non-negotiable deliverable; skip anything that threatens it.",
  ])


def _write_static_floor_brief(brief_path: Path, message_html: str) -> bool:
  """Atomically writes a minimal, self-contained HTML floor brief.

  The guaranteed-brief layer's whole point is "the partner never wakes to
  nothing." When the night failed for a reason a CLI rescue can't fix (a 401,
  or a weekly usage cap), spawning a rescue session would just fail the same
  way and burn the budget — defeating the guarantee exactly when it matters.
  So the Python runner writes the brief ITSELF: a heading and one honest line,
  valid standalone HTML with no template dependency.

  Written to a temp file and `os.replace`d into place so a crash or a
  `timeout` SIGKILL mid-write can't leave a TRUNCATED `.html` the UI would
  still list as tonight's brief. Returns True on success; any OS error is
  logged and swallowed (a failed write must never crash the run — the exit
  code is the record).
  """
  import tempfile
  from datetime import date
  today = date.today().isoformat()
  html = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '  <meta charset="utf-8">\n'
    '  <meta name="viewport" content="width=device-width, '
    'initial-scale=1">\n'
    f"  <title>Reflection — {today}</title>\n"
    "</head>\n"
    "<body>\n"
    f"  <h1>Reflection — {today}</h1>\n"
    f"  {message_html}\n"
    "</body>\n"
    "</html>\n"
  )
  try:
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
      dir=str(brief_path.parent), prefix=".brief-", suffix=".tmp",
    )
    try:
      with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(html)
        fh.flush()
        os.fsync(fh.fileno())
      os.replace(tmp, brief_path)
    except BaseException:
      try:
        os.unlink(tmp)
      except OSError:
        pass
      raise
    return True
  except OSError as exc:
    _log(f"ERROR could not write static floor brief: {exc!r}")
    return False


def write_static_auth_failure_brief(brief_path: Path) -> bool:
  """Static floor brief for an auth-failure night (see `_write_static_floor_brief`)."""
  return _write_static_floor_brief(
    brief_path,
    "<p>Tonight's reflection couldn't run — the CLI failed to "
    "authenticate; I'll resume tomorrow.</p>",
  )


def write_static_usage_limit_brief(brief_path: Path) -> bool:
  """Static floor brief for a usage/rate-limit night (see `_write_static_floor_brief`)."""
  return _write_static_floor_brief(
    brief_path,
    "<p>Tonight's reflection couldn't run — the model's usage limit "
    "was reached; I'll resume once it resets.</p>",
  )


def _safety_snapshot(label: str) -> None:
  """Best-effort git snapshot of /data BEFORE Reflection mutates anything.

  The nightly run rewrites skills, fixes apps, and writes reports — edits to
  agent-owned files under /data that the "git is the undo" contract promises are
  recoverable. Until now that promise rested entirely on the agent's own
  `pm-commit` discipline MID-run, so an early edit before the first commit had
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
  """Appends one timestamped line to the reflection log.

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
      fh.write(f"[{stamp}] reflection_runner: {message}\n")
  except OSError:
    pass


def load_skill() -> str:
  """Returns the reflection skill text used as the system prompt.

  The agent-editable skill at /data/shared/skills/reflection.md is the
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
    Path("/app/scripts/seed-skills/reflection.md"),
    Path(__file__).resolve().parent / "seed-skills" / "reflection.md",
  ):
    if fallback.is_file():
      try:
        return fallback.read_text(encoding="utf-8")
      except OSError:
        continue
  raise FileNotFoundError(
    f"reflection skill not found at {SKILL_PATH} or any baked fallback"
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
    src = Path(__file__).resolve().parent / "reflection-brief-template.html"
  if not src.is_file():
    _log("brief template not found in image — phase 6 will use the fallback")
    return
  try:
    BRIEF_TEMPLATE_DEST.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_TEMPLATE_DEST.write_bytes(src.read_bytes())
  except OSError as exc:
    _log(f"could not seed brief template to {BRIEF_TEMPLATE_DEST}: {exc!r}")


def load_settings() -> dict:
  """Reads /data/apps/reflection/settings.json, tolerating absence/corruption.

  This is the SAME file the Reflection mini-app writes (cron hour,
  exclude_apps). Provider/model selection adds two optional
  keys — `provider` ("claude" | "codex") and `model` (a provider model
  id) — so the owner can steer which agent dreams without a code
  change. Missing or malformed → {} (the caller applies defaults).
  """
  path = DATA_DIR / "apps" / "reflection" / "settings.json"
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def load_global_agent_settings() -> dict:
  """Reads /data/shared/agent-settings.json, tolerating absence/corruption."""
  path = DATA_DIR / "shared" / "agent-settings.json"
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def _model_belongs_to_other_provider(model: str, provider: str) -> bool:
  """True when a known model id clearly belongs to another provider."""
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
  """Normalize one provider/model/effort choice.

  The runner avoids importing app.providers so cron importability stays
  stdlib-only. This mirrors the backend's defensive shape: unknown providers
  are dropped, empty strings become None, and a known cross-provider model id
  is ignored instead of being handed to the wrong CLI.
  """
  if not isinstance(raw, dict):
    return None
  if raw.get("enabled") is False:
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


def _has_app_primary_override(settings: dict) -> bool:
  """Whether settings.json intentionally overrides the system primary agent.

  Reflection versions before the system background-agent picker seeded
  `provider: "claude"` even when the owner had never chosen a per-app model.
  Treat that exact legacy default (Claude provider only, no model/effort, no
  explicit mode marker) as inherited so existing installs can follow the new
  system primary/fallback settings.
  """
  mode = settings.get("primary_agent_mode")
  if mode == "system":
    return False
  if mode == "app":
    return True
  provider = settings.get("provider")
  provider = provider.strip() if isinstance(provider, str) else None
  model = settings.get("model")
  model = model.strip() if isinstance(model, str) else None
  effort = settings.get("effort")
  effort = effort.strip() if isinstance(effort, str) else None
  if model or effort:
    return True
  if provider and provider != DEFAULT_PROVIDER:
    return True
  return False


def _resolve_agents(settings: dict) -> dict:
  """Returns primary/fallback provider choices for the nightly run.

  Per-app settings.json remains authoritative when it names a provider/model.
  Otherwise Reflection inherits the system-level background agent defaults
  from /data/shared/agent-settings.json. That lets the owner set "Claude first,
  Codex fallback" once in Settings while still allowing Reflection to opt into
  its own choices later.
  """
  global_settings = load_global_agent_settings()
  raw_background = global_settings.get("background_agents")
  background = raw_background if isinstance(raw_background, dict) else {}
  global_choices: list[dict] = []
  raw_choices = background.get("providers")
  if isinstance(raw_choices, list):
    for index, raw_choice in enumerate(raw_choices):
      choice = _clean_agent_choice(
        raw_choice,
        label=f"global background provider {index + 1}",
      )
      if choice and not any(_same_agent_choice(choice, existing) for existing in global_choices):
        global_choices.append(choice)

  if not global_choices:
    global_primary = _clean_agent_choice(
      background.get("primary"),
      fallback_provider=DEFAULT_PROVIDER,
      label="global background primary",
    )
    global_fallback = _clean_agent_choice(
      background.get("fallback"),
      label="global background fallback",
    )
    if global_primary:
      global_choices.append(global_primary)
    if global_fallback and not _same_agent_choice(global_primary, global_fallback):
      global_choices.append(global_fallback)

  if not global_choices:
    global_primary = _clean_agent_choice(
      {
        "provider": DEFAULT_PROVIDER,
        "model": global_settings.get("model"),
        "effort": global_settings.get("effort"),
      },
      fallback_provider=DEFAULT_PROVIDER,
      label="global primary",
    )
    if global_primary:
      global_choices.append(global_primary)

  global_primary = global_choices[0]
  global_fallback = global_choices[1] if len(global_choices) > 1 else None
  primary_provider = global_primary.get("provider") or DEFAULT_PROVIDER
  has_app_primary = _has_app_primary_override(settings)
  primary = None
  if has_app_primary:
    primary = _clean_agent_choice(
      {
        "provider": settings.get("provider"),
        "model": settings.get("model"),
        "effort": settings.get("effort"),
      },
      fallback_provider=primary_provider,
      label="reflection primary",
    )
  if primary is None:
    primary = global_primary

  raw_fallback = None
  if any(settings.get(k) for k in ("fallback_provider", "fallback_model", "fallback_effort")):
    raw_fallback = {
      "provider": settings.get("fallback_provider"),
      "model": settings.get("fallback_model"),
      "effort": settings.get("fallback_effort"),
    }
  fallback = (
    _clean_agent_choice(raw_fallback, label="reflection fallback")
    if raw_fallback is not None else global_fallback
  )
  if _same_agent_choice(primary, fallback):
    fallback = None
  return {"primary": primary, "fallback": fallback}


def _resolve_model(settings: dict) -> tuple[str, str | None, str | None]:
  """Backward-compatible helper returning the resolved primary choice."""
  primary = _resolve_agents(settings)["primary"]
  return primary["provider"], primary.get("model"), primary.get("effort")


def build_goal(settings: dict) -> str:
  """Builds the first user message — the 'reflection goal' that kicks off the loop.

  The skill (system prompt) holds the full procedure; this message is
  the GO signal plus the run-specific context pointers the agent needs:
  today's date, where the wrapper staged its inputs (including the new
  per-app-digest.json), and which apps to skip. Keep it short — the
  skill is the contract, the goal is the trigger.
  """
  from datetime import date
  today = date.today().isoformat()
  exclude = settings.get("exclude_apps") or []
  inputs_dir = DATA_DIR / "apps" / "reflection" / "inputs"
  lines = [
    f"It is the night of {today}. Begin tonight's Reflection run.",
    "",
    f"Staged context is under {inputs_dir}/ — start here:",
    "  - reflection-run-history.txt  YOUR OWN recent runs (exit codes, log",
    "                          friction, your last skill edits) — read FIRST; a",
    "                          recurring failure across nights is tonight's",
    "                          first fix (phase 0 / phase 2).",
    "  - per-app-digest.json   compact analytics digest (opens_24h, signal",
    "                          counts, app_errors_24h + recent_app_errors for",
    "                          UNCAUGHT crashes, last_5_errors for signalled",
    "                          ones) — read to orient phase 4",
    "  - gather-errors.txt     which staged inputs FAILED to fetch tonight",
    "                          (empty = all fetched OK). A non-empty file means",
    "                          some inputs are missing due to a transport error,",
    "                          NOT a quiet night — say so in the brief instead of",
    "                          implying nothing happened.",
    "  - activity.jsonl        last 24h of raw platform events",
    "  - chats.md              recent chats list (fork + interview these)",
    "  - prev-report.html      yesterday's brief (don't repeat yourself)",
    "  - prev-question-answers.json  the partner's taps on a recent brief's",
    "                          question cards — saved for THIS run (no live",
    "                          agent waited). Read in phase 0; ACT on each in",
    "                          phase 2 (build the pick, drop the declines).",
    "                          Absent on first runs / when nothing was asked.",
    "",
    "Follow your skill (your system prompt) for the full procedure. "
    "Memory owns graph consolidation; Reflection reviews Memory's update "
    "log for system-improvement signals but does not drain or rewrite the "
    "graph. The floor deliverable is the brief (phase 6). At turn 40 cut "
    "unfinished deep work and ship a truthful brief so the partner wakes "
    "to something useful.",
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
  # "reflection" so it never collides with a per-chat profile.
  env.setdefault(
    "AGENT_BROWSER_PROFILE",
    str(DATA_DIR / "agent-browser-profiles" / "reflection"),
  )
  env.setdefault("AGENT_BROWSER_SESSION", "reflection")
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
) -> tuple[bool, bool, bool, bool]:
  """Drains one SDK response stream to its terminal result.

  Counts assistant turns and, when `countdown` is on, injects the
  turn-budget steering text from `steering_message` as a user message
  into the live session — `client.query` writes to the streaming
  stdin, and the CLI hands queued user input to the model between
  tool iterations of the in-flight loop. Message types are detected
  by class NAME so the drain avoids a second SDK import and works
  against test fakes; `_drain_message` already imported the real
  types for log formatting.

  Returns (saw_result, result_error, auth_failure, usage_limit).
  `auth_failure` is True when the terminal error result names a CLI
  authentication failure (a 401 / expired credential); `usage_limit` is
  True when it names a provider usage/rate cap — see `_is_auth_failure`
  / `_is_usage_limit`. The CLI mislabels a 401 as subtype="success"
  while setting is_error=True, so the error/result STRING is the only
  honest signal; we capture it both to set these flags and to name the
  failure in the log (the bare "subtype=success" line otherwise hides a
  401). Auth takes precedence over usage when a string somehow matches
  both.
  """
  turns_seen = 0
  saw_result = False
  result_error = False
  auth_failure = False
  usage_limit = False
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
      # next run and the Reflection app believe a brief was produced
      # when none was.
      if getattr(sdk_msg, "is_error", False):
        result_error = True
        err_text = (
          sdk_msg.result
          if isinstance(getattr(sdk_msg, "result", None), str)
          else None
        )
        if _is_auth_failure(err_text):
          auth_failure = True
        elif _is_usage_limit(err_text):
          usage_limit = True
        # Log the captured error string alongside the subtype. A 401
        # arrives as subtype="success" (the CLI mislabels it), so the
        # old "(subtype=success)" line hid the real cause; naming the
        # error string lets a future 401 be diagnosed at a glance.
        _log(
          "WARN run ended in error "
          f"(subtype={getattr(sdk_msg, 'subtype', '?')}; "
          f"auth_failure={auth_failure}; usage_limit={usage_limit}) "
          f"result error: {err_text or '(none)'}"
        )
  return saw_result, result_error, auth_failure, usage_limit


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
    # Hard-block the Claude Code harness / deferred tools that have no place
    # in an unattended Möbius cron. This is a real bug fix, not hygiene: from
    # 2026-06-30 the nightly agent began reaching for a leaked harness
    # `PushNotification` tool (loaded via `ToolSearch`) instead of the
    # documented `curl /api/notifications/send`, and that tool is a silent
    # no-op inside Möbius — so a week of morning briefs were written but never
    # delivered. `ToolSearch` is the loader for every deferred tool, so
    # blocking it stops the rest from being pulled in; the others are named in
    # case they are ever pre-loaded. A denylist is the only reliable lever:
    # `disallowed_tools` is a hard block even under `bypassPermissions`, while
    # `allowed_tools` is currently ignored by the SDK (claude-agent-sdk #361).
    # The morning push is now owned by the wrapper (reflection/fetch.sh), which
    # curls the notifications API deterministically after the run.
    "disallowed_tools": [
      "PushNotification",
      "ToolSearch",
      "Workflow",
      "ScheduleWakeup",
    ],
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
    saw_result, result_error, auth_failure, usage_limit = await _drain_session(
      client, log_fh, max_turns=max_turns, countdown=countdown,
    )
    if not saw_result:
      _log("WARN stream ended without a terminal ResultMessage")
      return GENERIC_MODEL_RC
    if auth_failure:
      # Distinct from the generic model error: the guaranteed-brief
      # layer must NOT spawn another CLI session (it would just 401
      # again) — it writes a static brief itself instead.
      return AUTH_FAILURE_RC
    if usage_limit:
      # Same reasoning as auth: a weekly/rate cap won't clear tonight,
      # so route to a static floor brief rather than a doomed rescue.
      return USAGE_LIMIT_RC
    if result_error:
      return GENERIC_MODEL_RC
    return 0
  except Exception as exc:  # noqa: BLE001 — top-level guard for cron
    _log(f"ERROR reflection run crashed: {exc!r}")
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

  Codex can run the same Reflection skill through the app-server SDK
  path. The normal chat runner publishes SSE; Reflection swaps in a
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
      chat_id="reflection-nightly",
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
    err = str(result.get("error") or "")
    _log(f"WARN codex run ended in error: {err}")
    if _is_auth_failure(err):
      return AUTH_FAILURE_RC
    if _is_usage_limit(err):
      return USAGE_LIMIT_RC
    return GENERIC_MODEL_RC
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
  countdown: bool,
) -> int:
  """Runs the selected provider choice through its matching SDK path."""
  provider = choice["provider"]
  model = choice.get("model")
  effort = choice.get("effort")
  if provider == "codex":
    return await _run_codex_session(
      goal=goal, skill_text=skill_text, env=env,
      model=model, effort=effort, log_fh=log_fh,
    )
  return await _run_claude_session(
    goal=goal, skill_text=skill_text, env=env, model=model,
    effort=effort, max_turns=max_turns, log_fh=log_fh,
    countdown=countdown,
  )


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
  spawn one short rescue session whose only goal is a minimal brief
  built from whatever the cut-off run left behind.

  Blocked-night case (rc in {AUTH_FAILURE_RC, USAGE_LIMIT_RC}): the
  night died because the model is unreachable for the rest of it — a
  401, or a usage/rate cap that won't reset before morning. Spawning
  another CLI session would just fail the same way, defeating the
  guarantee exactly when it's needed — so the Python runner writes a
  minimal static brief ITSELF (no CLI) and stops. When the brief path
  can't be resolved (app id unstaged), there's nowhere to write, so
  fall through to the normal rescue as a last resort.

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
    if brief is not None and rc in (AUTH_FAILURE_RC, USAGE_LIMIT_RC):
      # The model is unreachable for the rest of the night (a 401 that
      # would just 401 again, or a usage cap that won't reset before
      # morning) — do NOT spawn another doomed CLI session. Write the
      # static floor brief directly so the partner still wakes to
      # something honest about why the night didn't run.
      if rc == AUTH_FAILURE_RC:
        _log(
          f"main run failed auth (rc={rc}) with no brief at {brief} — "
          "writing static auth-failure brief without the CLI"
        )
        wrote = write_static_auth_failure_brief(brief)
        kind = "static auth brief"
      else:
        _log(
          f"main run hit a usage limit (rc={rc}) with no brief at "
          f"{brief} — writing static usage-limit brief without the CLI"
        )
        wrote = write_static_usage_limit_brief(brief)
        kind = "static usage-limit brief"
      _log(
        f"guaranteed-brief fallback finished ({kind}) "
        f"brief_written={'yes' if wrote else 'no'}"
      )
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
  """Runs the whole Reflection session and returns a process exit code.

  Returns 0 on a clean run (the SDK reached a terminal result, error
  or not — an agent that decides "quiet night, nothing to do" is a
  success), 1 on an infrastructure failure (skill missing, SDK couldn't
  start, an unexpected exception), and one of the runner's own error-band
  codes (all >=64, so they never collide with the wrapper's config codes
  2/3/5) when the goal loop ended in an error: GENERIC_MODEL_RC (64) for
  a generic model/max_turns failure, USAGE_LIMIT_RC (65) for a provider
  usage/rate cap, AUTH_FAILURE_RC (66) for a CLI auth failure (a 401).
  The wrapper maps the exit code into the `cron_outcome` event, so this
  is the one signal the activity log records about whether the night
  ran. A non-zero night additionally triggers the guaranteed-brief
  fallback (which never changes the exit code — it rescues the
  deliverable, not the record); the auth/usage rcs route that fallback
  to a CLI-free static brief instead of another doomed CLI rescue.
  """
  settings = load_settings()
  agents = _resolve_agents(settings)
  primary = agents["primary"]
  fallback = agents.get("fallback")
  provider = primary["provider"]
  model = primary.get("model")
  effort = primary.get("effort")
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

  # Guaranteed pre-run restore point: commit /data BEFORE the agent rewrites
  # skills or apps, so "git is the undo" holds even if tonight's run edits a
  # file before its own first pm-commit. Best-effort; never blocks.
  from datetime import date
  _safety_snapshot(f"reflection: pre-run safety snapshot {date.today().isoformat()}")

  try:
    rc = await _run_agent_choice(
      primary, goal=goal, skill_text=skill_text, env=env,
      max_turns=max_turns, log_fh=log_fh, countdown=True,
    )
    if (
      rc in (AUTH_FAILURE_RC, USAGE_LIMIT_RC)
      and fallback is not None
      and fallback_needed(rc, todays_brief_path())
    ):
      _log(
        f"primary background agent failed rc={rc}; trying fallback "
        f"provider={fallback['provider']} "
        f"model={fallback.get('model') or '(default)'} "
        f"effort={fallback.get('effort') or '(default)'}"
      )
      rc = await _run_agent_choice(
        fallback, goal=goal, skill_text=skill_text, env=env,
        max_turns=max_turns, log_fh=log_fh, countdown=True,
      )
      provider = fallback["provider"]
      model = fallback.get("model")
      effort = fallback.get("effort")
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
