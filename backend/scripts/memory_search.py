#!/usr/bin/env python3
"""Memory-search subagent: deep-reads the knowledge graph for one request.

The main chat agent does NOT traverse the graph on its own — the read-trace's
`nodes_read` is mostly empty. It answers from the injected router + recent chat
summaries and stops at the surface. This runner closes that gap: the main agent
calls it via ONE Bash line early in a chat, and it spawns a SEPARATE, read-only
search subagent whose entire job is to walk the graph deeply for the parts this
conversation actually touches, then hand back the relevant memories. It uses the
system background-agent primary/fallback settings, so Claude can fall through to
Codex when it is out of usage or otherwise unavailable.

It is the *recall* arm of the chat-centric memory model (see memory.md): the
per-chat note is what the agent WRITES every turn; this is how it READS the
rest of the graph on demand. The main agent integrates the printed result into
its own reasoning — it does not narrate this call to the partner.

It is a fully AGENTIC tree walk — the subagent picks the route and follows the
[[link]]/moc edges itself, judging relevance contextually (not a flat retrieve).
What makes it FAST is the I/O: it walks DOWN the hierarchy (router -> picked
maps -> picked notes) reading each level as a single batched `cat a.md b.md …`
instead of one `Read` per model turn. The CLI serializes `Read` calls, so
batching a hop into one `cat` collapses ~O(notes) serial turns into ~O(hops)
turns — the ~3x win today, bounded at any graph size because only the relevant
slice of each level is ever read. (A prior version read one note per turn: ~60s.)

Two things make it more than "just ask a model in a subprocess":

  - Read tracking. Because reads are batched `cat`s (no per-file `Read` events),
    the chat's read-trace (`nodes_read`, via `app.memory_trace`) is taken from the
    notes the subagent CITES in its `SOURCES:` line — the "dug-for" signal the
    nightly Reflection pass diffs to learn which notes to surface next time.
  - A search methodology, not a conversation. The `--append-system-prompt`
    below is the subagent's whole identity: orient from the map, then batch-read
    and expand along the edges, breadth then depth, and report compactly.

Usage:  memory_search.py "<the partner's current request / focus>" [chat_id]
Prints the synthesis (relevant memories) to stdout; records reads silently to
the chat's read-trace when a chat_id is given. Exit 0 ok · 2 bad args.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

# This script imports app.memory / app.memory_trace (server-only helpers) to map
# Read paths to graph node ids and record the read-trace — but it is invoked from
# ARBITRARY cwds: the chat agent runs it from /data, the reflection runner too,
# neither with PYTHONPATH set. Without the package root on sys.path the first
# `from app.memory import ...` (in the read-tracking loop) raises
# ModuleNotFoundError and the subagent's whole synthesis is lost — the recall arm
# silently fails and the agent falls back to shallow injected context. Put the
# script's grandparent (/app, which holds the `app` package) on sys.path so those
# imports resolve regardless of cwd. (chat.py's auto-search sets PYTHONPATH=/app;
# the agent's own Bash invocation does not — this covers both.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Hard-coded paths rather than env-derived: like the reflection runner this can
# be invoked from a near-empty environment. Provider auth homes are set
# explicitly below so both automatic and agent-invoked memory searches use the
# same credentials as normal Möbius turns.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MEMORY_DIR = DATA_DIR / "shared" / "memory"
CLAUDE_CONFIG_DIR = DATA_DIR / "cli-auth" / "claude"
CODEX_HOME = DATA_DIR / "cli-auth" / "codex"
CLAUDE_CLI_PATH = "/usr/local/bin/claude"
CODEX_CLI_PATH = "/usr/local/bin/codex"

# A faster model is the right default for a helper that fires on most chats;
# override with MEMORY_SEARCH_MODEL when traversal quality needs the big model.
DEFAULT_CLAUDE_MODEL = os.environ.get("MEMORY_SEARCH_MODEL", "claude-sonnet-4-6")
DEFAULT_CODEX_MODEL = os.environ.get("MEMORY_SEARCH_CODEX_MODEL", "gpt-5.5")
KNOWN_MODELS = {
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

# How long to let the subagent traverse before giving up (seconds).
TIMEOUT_SECS = int(os.environ.get("MEMORY_SEARCH_TIMEOUT", "180"))

# The search agent's whole identity. THIS is the artifact we iterate — keep the
# "how to search" rules here and the "what to search for" in the per-call prompt.
SEARCH_SYSTEM_PROMPT = """\
You are the memory-search subagent for Möbius. Your ONE job: given the partner's
current request, return the memories that would actually change how you answer
or build for it — found by READING the notes, not skimming the router.

The graph is the current directory:
  index.md            the router — one scent line per map. Small at ANY graph
                      size (one line per map, not per note). Your entry point.
  mocs/<slug>.md      topic maps — link to notes, grouped under headings. The route.
  notes/<slug>.md     atomic notes — ONE durable fact each. THESE are the sources.
  chats/<id>/index.md per-chat notes — a past chat's growing summary + facts.

How to search — an agentic walk DOWN the hierarchy, reading each level in ONE
batch. This stays fast whether the graph has 18 notes or 18,000, because you only
ever read the RELEVANT slice at each level (the router never balloons; you never
read the whole graph):
  1. `cat index.md` — the router. Pick EVERY map that could plausibly bear on the
     request, generously; a request about work/building/planning touches
     about-the-user AND the apps map AND any projects map.
  2. `cat mocs/<a>.md mocs/<b>.md …` — read all the maps you picked in ONE batch.
     Each lists its notes with a scent + headings. Now pick EVERY note that could
     bear on the request, PLUS their linked neighbours ([[wiki-links]], cross-chat
     `[[chats/<id>]]`).
  3. `cat notes/<x>.md notes/<y>.md chats/<id>/index.md …` — read ALL those notes
     (and their neighbours) in ONE batch. **NEVER read notes one per turn** —
     one-file-per-turn is the slow failure this rule exists to prevent.
  4. Only if a note's CONTENT reveals a clearly-relevant note you hadn't reached
     (a recurring cross-chat thread, a sibling app to copy from, a shared
     data-model/schema note), do ONE more batched `cat` of just those, or
     `rg -l "<noun>" notes chats` in a single call to catch an under-linked orphan.
     Then STOP and report — you should be done in ~3-4 batched reads, not dozens.
  When the request is to BUILD or EXTEND something, the relevant set includes
  every SIMILAR thing already built and any shared data-model/schema note.
  Judge relevance as you read (opening generously, reporting strictly — below).
  Stopping at the router, or after a single note, is a FAILURE.

Relevance — generous OPENING, strict REPORTING:
  - Report a fact if it would change your answer/build for THIS request — even
    when the request's topic word isn't in the note (a daily-coffee preference
    bears on a meal plan; a sibling app bears on a new build).
  - EXCLUDE facts merely true about the partner but inert here (how they like
    chat replies phrased has nothing to do with an app's data model).
  - Report what the note SAYS, plus at most a short clause of obvious
    implication. Do NOT supply domain advice (macros, logistics, scheduling
    theory) the notes don't contain — that's the requester's job.

Output ONLY this — no preamble, no "I found", no headings, no grouping or bold
section titles, no narration. Just a flat list:
  - One line per relevant fact: the fact, then its source note slug(s) in
    (parentheses); comma-separate slugs only when notes truly share one fact.
  - A final line — SOURCES: the notes/ (and chats/) slugs you OPENED and drew a
    fact from. Never list index.md or a map (those are the route), and never
    list a file you did not open.
  - If nothing is genuinely relevant, output exactly: No relevant memories.

Report a fact ONLY from a note you opened — never from a scent line, never
labelled "inferred". Invent nothing. Edit nothing — you are strictly read-only.
"""

_AUTH_FAILURE_MARKERS = (
  "401",
  "invalid authentication credentials",
  "failed to authenticate",
  "authentication_error",
  "oauth token has expired",
  "not logged in",
  "login required",
)

_USAGE_LIMIT_MARKERS = (
  "usage limit",
  "rate limit",
  "quota",
  "429",
  "too many requests",
  "temporarily unavailable",
)


class SearchRunResult(NamedTuple):
  provider: str
  text: str
  stdout: str
  stderr: str
  returncode: int
  read_ids: list[str]
  # The result event's own error flag. The Claude CLI mislabels a 401
  # ResultMessage as subtype="success" while setting is_error=True and can exit
  # 0, so the exit code alone reads that failed run as a success — is_error is
  # the reliable structured signal (recovery_chat_runner reads the same field).
  # Codex has no equivalent in its output file, so it stays False and relies on
  # the exit code, which is trustworthy for that CLI.
  is_error: bool = False


def _is_provider_failure(
  text: str, returncode: int = 1, is_error: bool = False
) -> bool:
  """True when a CLI result looks like auth/usage/provider trouble.

  The memory-search helper is best-effort, but provider exhaustion is exactly
  when trying the configured fallback is valuable. A run failed if the process
  exited nonzero OR the result event carried is_error (the exit-0-with-error
  case the Claude CLI produces on a 401). A clean run (exit 0, not is_error) is
  scanned only for familiar auth/usage strings in its stderr — never its
  synthesis text, so a legitimate memory that mentions "rate limit" can't be
  misread as a failure.
  """
  if returncode != 0 or is_error:
    return True
  low = (text or "").lower()
  return any(marker in low for marker in (*_AUTH_FAILURE_MARKERS, *_USAGE_LIMIT_MARKERS))


def _load_global_agent_settings() -> dict:
  path = DATA_DIR / "shared" / "agent-settings.json"
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}
  return data if isinstance(data, dict) else {}


def _model_belongs_to_other_provider(model: str, provider: str) -> bool:
  for known_provider, models in KNOWN_MODELS.items():
    if known_provider != provider and model in models:
      return True
  return False


def _clean_choice(raw: dict | None, fallback_provider: str | None = None) -> dict | None:
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
    model = None
  effort = raw.get("effort")
  effort = effort.strip() if isinstance(effort, str) and effort.strip() else None
  return {"provider": provider, "model": model, "effort": effort}


def _same_choice(a: dict | None, b: dict | None) -> bool:
  if not a or not b:
    return False
  return (
    a.get("provider") == b.get("provider")
    and (a.get("model") or None) == (b.get("model") or None)
    and (a.get("effort") or None) == (b.get("effort") or None)
  )


def _resolve_search_agents() -> list[dict]:
  """Returns provider choices for memory search, primary first.

  Memory search is a live-chat helper, but it behaves like a tiny background
  subagent. It therefore inherits the system background primary/fallback so a
  Claude usage/auth outage can fall through to Codex without changing the main
  chat. Env vars remain as an escape hatch for direct CLI testing.
  """
  forced_provider = os.environ.get("MEMORY_SEARCH_PROVIDER")
  if forced_provider in ("claude", "codex"):
    model_env = (
      "MEMORY_SEARCH_CODEX_MODEL" if forced_provider == "codex"
      else "MEMORY_SEARCH_MODEL"
    )
    return [{
      "provider": forced_provider,
      "model": os.environ.get(model_env) or None,
      "effort": os.environ.get("MEMORY_SEARCH_EFFORT") or None,
    }]

  settings = _load_global_agent_settings()
  raw_background = settings.get("background_agents")
  background = raw_background if isinstance(raw_background, dict) else {}
  raw_choices = background.get("providers")
  if isinstance(raw_choices, list):
    choices: list[dict] = []
    for raw_choice in raw_choices:
      choice = _clean_choice(raw_choice)
      if choice and not any(_same_choice(choice, existing) for existing in choices):
        choices.append(choice)
    if choices:
      return choices

  primary = _clean_choice(background.get("primary"), fallback_provider="claude")
  if primary is None:
    primary = _clean_choice(
      {
        "provider": "claude",
        "model": settings.get("model") or DEFAULT_CLAUDE_MODEL,
        "effort": settings.get("effort"),
      },
      fallback_provider="claude",
    )
  fallback = _clean_choice(background.get("fallback"))
  choices = [primary] if primary else []
  if fallback and not _same_choice(primary, fallback):
    choices.append(fallback)
  return choices or [{"provider": "claude", "model": DEFAULT_CLAUDE_MODEL, "effort": None}]


def _extract_read_ids_from_claude_stdout(
  stdout: str,
) -> tuple[str, list[str], bool]:
  read_ids: list[str] = []
  final_text = ""
  is_error = False
  for line in stdout.splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      obj = json.loads(line)
    except ValueError:
      continue
    typ = obj.get("type")
    if typ == "assistant":
      for block in obj.get("message", {}).get("content", []) or []:
        if (
          isinstance(block, dict)
          and block.get("type") == "tool_use"
          and block.get("name") == "Read"
        ):
          fp = (block.get("input") or {}).get("file_path")
          if fp:
            nid = _path_to_node_id(fp)
            if nid and nid not in read_ids:
              read_ids.append(nid)
    elif typ == "result":
      final_text = obj.get("result") or final_text
      # is_error is the one honest signal on a 401 the CLI exits 0 for.
      is_error = bool(obj.get("is_error"))

  for nid in _sources_to_node_ids(final_text):
    if nid not in read_ids:
      read_ids.append(nid)
  return final_text, read_ids, is_error


def _run_claude_search(choice: dict, prompt: str) -> SearchRunResult:
  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
  # Scoped read-only Bash lets a hop be one batched `cat a.md b.md c.md` while
  # keeping the subagent write-incapable. This is load-bearing: the query is raw
  # chat text and note bodies are agent-written, so a prompt injection could try
  # to mutate the graph. Pin Bash to cat/rg/ls and deny write-shaped tools.
  cmd = [
    CLAUDE_CLI_PATH,
    "-p",
    prompt,
    "--output-format",
    "stream-json",
    "--verbose",
    "--allowedTools",
    "Bash(cat:*)",
    "Bash(rg:*)",
    "Bash(ls:*)",
    "Read",
    "Grep",
    "Glob",
    "--disallowedTools",
    "Write",
    "Edit",
    "NotebookEdit",
    "--add-dir",
    str(MEMORY_DIR),
    "--append-system-prompt",
    SEARCH_SYSTEM_PROMPT,
  ]
  model = choice.get("model") or DEFAULT_CLAUDE_MODEL
  if model:
    cmd += ["--model", model]
  proc = subprocess.run(
    cmd,
    cwd=str(MEMORY_DIR),
    env=env,
    capture_output=True,
    text=True,
    timeout=TIMEOUT_SECS,
  )
  final_text, read_ids, is_error = _extract_read_ids_from_claude_stdout(
    proc.stdout
  )
  return SearchRunResult(
    "claude", final_text, proc.stdout, proc.stderr, proc.returncode, read_ids,
    is_error,
  )


def _run_codex_search(choice: dict, prompt: str) -> SearchRunResult:
  env = dict(os.environ)
  env["CODEX_HOME"] = str(CODEX_HOME)
  with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=True) as out:
    cmd = [
      CODEX_CLI_PATH,
      "exec",
      "--skip-git-repo-check",
      "--ephemeral",
      "--ignore-user-config",
      "--ignore-rules",
      "--json",
      "-s",
      "read-only",
      "-a",
      "never",
      "-C",
      str(MEMORY_DIR),
      "-o",
      out.name,
    ]
    model = choice.get("model") or DEFAULT_CODEX_MODEL
    if model:
      cmd += ["--model", model]
    effort = choice.get("effort")
    if effort:
      cmd += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    codex_prompt = (
      SEARCH_SYSTEM_PROMPT
      + "\n\n"
      + "You are running inside a read-only sandbox. Use only read-only "
        "inspection commands such as cat, rg, and ls. Do not attempt writes, "
        "network access, package installs, or external tool setup.\n\n"
      + prompt
    )
    cmd.append(codex_prompt)
    proc = subprocess.run(
      cmd,
      cwd=str(MEMORY_DIR),
      env=env,
      capture_output=True,
      text=True,
      timeout=TIMEOUT_SECS,
    )
    out.seek(0)
    final_text = out.read().strip()
  read_ids = _sources_to_node_ids(final_text)
  return SearchRunResult(
    "codex", final_text, proc.stdout, proc.stderr, proc.returncode, read_ids,
  )


def _run_search_choice(choice: dict, prompt: str) -> SearchRunResult:
  if choice.get("provider") == "codex":
    return _run_codex_search(choice, prompt)
  return _run_claude_search(choice, prompt)


def _failure_snippet(result: SearchRunResult) -> str:
  detail = result.stderr.strip() or result.text.strip() or result.stdout.strip()
  detail = " ".join(detail.split())
  return detail[:240] if detail else f"exit {result.returncode}"


def _hints_clause(max_depth: int | None, max_breadth: int | None) -> str:
  """A SOFT traversal budget the caller may pass. Finding the useful information
  is always the goal — these only shape effort (a quick shallow lookup vs a deep
  dig). Empty when no hints were given (the default deep traversal)."""
  if not max_depth and not max_breadth:
    return ""
  parts = []
  if max_breadth:
    parts.append(f"open about {max_breadth} map(s)/note(s) at each level")
  if max_depth:
    parts.append(f"descend about {max_depth} hop(s) from the router")
  return (
    "\n\nTraversal hint (SOFT — finding the useful information is the goal; "
    "exceed these freely whenever a clearly-relevant thread needs it): "
    + "; ".join(parts)
    + "."
  )


def build_prompt(query: str, hints: str = "") -> str:
  return (
    "The partner's current request / focus in this chat is:\n\n"
    f"{query}\n\n"
    "Search the knowledge graph (the current directory) for everything "
    "relevant and report the relevant facts with their source note slugs, "
    "exactly as your instructions specify." + hints
  )


def _path_to_node_id(file_path: str) -> str | None:
  """Maps an absolute Read path to its graph node id, or None if it's not a
  memory node. Reuses the same rule build_memory_block uses for injection so
  read-trace ids line up with graph.json without re-deriving the mapping."""
  try:
    rel = Path(file_path).resolve().relative_to(MEMORY_DIR.resolve())
  except (ValueError, OSError):
    return None
  from app.memory import _loaded_path_to_id

  return _loaded_path_to_id(str(rel))


def _slug_to_node_id(slug: str) -> str | None:
  """Maps a SOURCES slug the model cited to a graph node id, trying the shapes
  the model uses ("foo", "notes/foo", "chats/<id>"). Reuses _path_to_node_id so
  the ids line up with graph.json."""
  s = slug.strip().strip("`,.()[]").removesuffix(".md")
  if not s:
    return None
  if s.startswith("chats/"):
    # A per-chat note's file is chats/<id>/index.md, but the model cites it three
    # ways: "chats/<id>", "chats/<id>/index", and "chats/<id>/index.md" (the .md
    # already stripped above, leaving "…/index"). Peel a trailing "/index" so all
    # three land on the same candidate — without this the "…/index" spelling
    # built "chats/<id>/index/index.md" and silently dropped the chat.
    base = s[: -len("/index")] if s.endswith("/index") else s
    candidates = [f"{base}/index.md"]
  elif "/" in s:
    candidates = [f"{s}.md"]
  else:
    candidates = [f"notes/{s}.md", f"mocs/{s}.md", f"chats/{s}/index.md"]
  for rel in candidates:
    nid = _path_to_node_id(str(MEMORY_DIR / rel))
    if nid:
      return nid
  return None


def _sources_to_node_ids(final_text: str) -> list[str]:
  """The notes the model drew facts from, parsed from its `SOURCES:` line. With
  batched `cat` there are no per-file `Read` events to count, so SOURCES carries
  the read-trace signal — and it's the better one: what was CITED, not merely
  opened. The nightly Reflection pass diffs this to learn which notes to surface."""
  ids: list[str] = []
  for line in final_text.splitlines():
    s = line.strip()
    if not s.upper().startswith("SOURCES:"):
      continue
    for tok in s.split(":", 1)[1].replace(";", ",").split(","):
      nid = _slug_to_node_id(tok)
      if nid and nid not in ids:
        ids.append(nid)
  return ids


def _parse_args(argv: list[str]) -> tuple[list[str], int | None, int | None]:
  """Pull optional --max-depth / --max-breadth out of argv; the rest is
  positional (query, then optional chat_id). Hints are soft (see _hints_clause).
  A recognized flag with no value, or a non-integer value, is a usage error —
  never let it fall through and corrupt the query text."""
  positional: list[str] = []
  max_depth = max_breadth = None
  i = 0
  while i < len(argv):
    a = argv[i]
    if a in ("--max-depth", "--max-breadth"):
      if i + 1 >= len(argv):
        raise ValueError(f"{a} needs an integer value")
      try:
        n = int(argv[i + 1])
      except ValueError:
        raise ValueError(f"{a} needs an integer value, got {argv[i + 1]!r}")
      if a == "--max-depth":
        max_depth = n
      else:
        max_breadth = n
      i += 2
      continue
    positional.append(a)
    i += 1
  return positional, max_depth, max_breadth


def run() -> int:
  usage = (
    'usage: memory_search.py "<request>" [chat_id] '
    "[--max-depth N] [--max-breadth N]\n"
  )
  try:
    positional, max_depth, max_breadth = _parse_args(sys.argv[1:])
  except ValueError as exc:
    sys.stderr.write(f"{usage}  ({exc})\n")
    return 2
  if not positional or not positional[0].strip():
    sys.stderr.write(usage)
    return 2
  query = positional[0]
  chat_id = positional[1] if len(positional) > 1 else ""
  prompt = build_prompt(query, _hints_clause(max_depth, max_breadth))
  choices = _resolve_search_agents()

  final: SearchRunResult | None = None
  failures: list[str] = []
  try:
    for index, choice in enumerate(choices):
      provider = choice.get("provider") or "claude"
      try:
        result = _run_search_choice(choice, prompt)
      except subprocess.TimeoutExpired:
        failures.append(f"{provider}: timed out after {TIMEOUT_SECS}s")
        if index + 1 < len(choices):
          sys.stderr.write(
            f"memory_search: {provider} timed out after {TIMEOUT_SECS}s; "
            "trying fallback\n"
          )
        continue

      failure_output = result.stderr if result.returncode == 0 else "\n".join([
        result.stdout,
        result.stderr,
      ])
      if _is_provider_failure(
        failure_output, result.returncode, result.is_error
      ):
        failures.append(f"{provider}: {_failure_snippet(result)}")
        if index + 1 < len(choices):
          sys.stderr.write(
            f"memory_search: {provider} failed; trying fallback "
            f"({_failure_snippet(result)})\n"
          )
        continue

      final = result
      break
  except OSError as exc:
    failures.append(f"provider launch failed: {exc}")

  if final is None:
    sys.stdout.write("No relevant memories.\n")
    sys.stderr.write(
      "memory_search: all search providers failed: "
      + (" | ".join(failures) if failures else "none configured")
      + "\n"
    )
    return 1

  read_ids = list(final.read_ids)
  final_text = final.text

  if chat_id and read_ids:
    from app.memory_trace import record_note_read

    for nid in read_ids:
      record_note_read(DATA_DIR, chat_id, nid)

  # The synthesis is the only thing on stdout — it's what the main agent reads
  # and integrates. The read count goes to stderr so the tool block shows it
  # without polluting the integrated text.
  sys.stdout.write((final_text or "No relevant memories.").rstrip() + "\n")
  sys.stderr.write(
    f"memory_search: provider={final.provider} read {len(read_ids)} node(s): "
    f"{', '.join(read_ids) or '(none)'}\n"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(run())
