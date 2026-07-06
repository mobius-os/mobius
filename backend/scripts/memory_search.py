#!/usr/bin/env python3
"""Memory-search subagent: deep-reads the knowledge graph for one request.

The main chat agent does NOT traverse the graph on its own — the read-trace's
`nodes_read` is mostly empty. It answers from the injected router + recent chat
summaries and stops at the surface. This runner closes that gap: the main agent
calls it via ONE Bash line early in a chat, and it spawns a SEPARATE, read-only
`claude` subagent whose entire job is to walk the graph deeply for the parts
this conversation actually touches, then hand back the relevant memories.

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

Two things make it more than "just ask claude in a subprocess":

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
from pathlib import Path

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
# be invoked from a near-empty environment, and the only var that matters to
# the spawned CLI (CLAUDE_CONFIG_DIR) is set explicitly below.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MEMORY_DIR = DATA_DIR / "shared" / "memory"
CLAUDE_CONFIG_DIR = DATA_DIR / "cli-auth" / "claude"
CLI_PATH = "/usr/local/bin/claude"

# A faster model is the right default for a helper that fires on most chats;
# override with MEMORY_SEARCH_MODEL when traversal quality needs the big model.
DEFAULT_MODEL = os.environ.get("MEMORY_SEARCH_MODEL", "claude-sonnet-4-6")

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

  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)

  cmd = [
    CLI_PATH,
    "-p",
    build_prompt(query, _hints_clause(max_depth, max_breadth)),
    "--output-format",
    "stream-json",
    "--verbose",
    "--allowedTools",
    # Scoped read-only Bash so a hop is one batched `cat a.md b.md c.md` (the CLI
    # serializes N Read calls into N turns; batching a hop into one call is the
    # speedup) while the subagent stays write-incapable. This scoping is
    # load-bearing: the query is the partner's raw chat text and note bodies are
    # agent-written, so a prompt injection could try to make this "read-only"
    # search mutate the graph. Pinning Bash to cat/rg/ls keeps the walk fully
    # agentic (the model still picks the route and follows the edges) and leaves
    # Read/Grep/Glob for single-file use.
    #
    # Verified live against the pinned CLI (2.1.x, headless -p): batched `cat`
    # and `rg` execute; a `touch` or `>`-redirect bash write is refused by the
    # CLI's own headless write-sandbox ("may only modify files in the allowed
    # working directories", and even those need an interactive grant no `-p` run
    # can give); the Write/Edit tools are denied. Residual risk: the CLI's
    # permission matching does NOT decompose compound commands, so a read-only
    # rider after an allowed prefix (e.g. `cat x.md && echo …`) still runs — but a
    # rider cannot mutate (writes hit the sandbox) and no network/exfil tool is
    # granted, so the read-only guarantee holds. --disallowedTools names the
    # write-shaped tools explicitly (deny beats allow) so the denial does not
    # rely on their mere absence from the allow-list.
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
  if DEFAULT_MODEL:
    cmd += ["--model", DEFAULT_MODEL]

  read_ids: list[str] = []
  final_text = ""
  try:
    proc = subprocess.run(
      cmd,
      cwd=str(MEMORY_DIR),
      env=env,
      capture_output=True,
      text=True,
      timeout=TIMEOUT_SECS,
    )
  except subprocess.TimeoutExpired:
    sys.stderr.write(f"memory_search: timed out after {TIMEOUT_SECS}s\n")
    return 1

  for line in proc.stdout.splitlines():
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

  # Batched `cat` reads leave no per-file Read events, so the cited SOURCES line
  # is the authoritative read-trace; union in any actual Read-tool ids too, in
  # case the model reached for single-file Read on a hop.
  for nid in _sources_to_node_ids(final_text):
    if nid not in read_ids:
      read_ids.append(nid)

  if chat_id and read_ids:
    from app.memory_trace import record_note_read

    for nid in read_ids:
      record_note_read(DATA_DIR, chat_id, nid)

  # The synthesis is the only thing on stdout — it's what the main agent reads
  # and integrates. The read count goes to stderr so the tool block shows it
  # without polluting the integrated text.
  sys.stdout.write((final_text or "No relevant memories.").rstrip() + "\n")
  sys.stderr.write(
    f"memory_search: read {len(read_ids)} node(s): "
    f"{', '.join(read_ids) or '(none)'}\n"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(run())
