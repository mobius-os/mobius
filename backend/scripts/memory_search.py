#!/usr/bin/env python3
"""Memory-search subagent: deep-reads the knowledge graph for one request.

The main chat agent does NOT traverse the graph on its own — the read-trace's
`nodes_read` is mostly empty. It answers from the injected router + recent chat
summaries and stops at the surface. This runner closes that gap: the main agent
calls it via ONE Bash line early in a chat, and it spawns a SEPARATE, read-only
`claude` subagent whose entire job is to walk the graph deeply for the parts
this conversation actually touches, then hand back the relevant memories.

It is the *recall* arm of the chat-centric memory model (see mind.md): the
per-chat note is what the agent WRITES every turn; this is how it READS the
rest of the graph on demand. The main agent integrates the printed result into
its own reasoning — it does not narrate this call to the partner.

Two things make it more than "just ask claude in a subprocess":

  - Read tracking. It runs the subagent with `--output-format stream-json` and
    records every memory node the subagent actually `Read` into the chat's
    read-trace (`nodes_read`), reusing `app.memory_trace`. That is the exact
    signal the nightly Dreaming pass diffs to learn which notes should sit
    nearer the surface next time.
  - A search methodology, not a conversation. The `--append-system-prompt`
    below is the subagent's whole identity: descend past the router into the
    MOCs and the notes themselves, breadth then depth, and report compactly.

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

# Hard-coded paths rather than env-derived: like the dreaming runner this can
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
  index.md            the router — one scent line per map. The ROUTE, not a source.
  mocs/<slug>.md      topic maps — links to notes, grouped under headings. The route.
  notes/<slug>.md     atomic notes — ONE durable fact each. THESE are the sources.
  chats/<id>/index.md per-chat notes — a past chat's growing summary + facts.

How to search:
  1. Read index.md. Open EVERY map that could plausibly relate — resolve ties
     toward opening, not skipping. A request about work, building, or planning
     touches about-the-user AND the apps map AND any projects section. Opening a
     map is cheap. (Open <2 maps for a multi-part request → you under-fanned.)
  2. In each map, read EVERY heading — e.g. "How they like to work", "Projects
     & plans" — not just the top. The request can touch a section far down a map.
  3. Open the actual notes/ and read their CONTENT. A map's one-line description
     is a SCENT, not a fact — if it smells relevant, OPEN the note before you
     rely on it. Follow [[wiki-links]] one hop to related notes.
  4. Before finalizing, Grep notes/ for the request's key nouns (the domain, the
     activity, "schema"/"data model", etc.) and open any hit you missed — EVERY
     time, not only when the router looks thin. Maps under-link; this catches
     orphans and siblings.
  When the request is to BUILD or EXTEND something, the relevant set includes
  every SIMILAR thing already built (sibling apps to copy from) and any shared
  data-model/schema note — not only the one whose name matches.
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


def build_prompt(query: str) -> str:
  return (
    "The partner's current request / focus in this chat is:\n\n"
    f"{query}\n\n"
    "Search the knowledge graph (the current directory) for everything "
    "relevant and report the relevant facts with their source note slugs, "
    "exactly as your instructions specify."
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


def run() -> int:
  if len(sys.argv) < 2 or not sys.argv[1].strip():
    sys.stderr.write('usage: memory_search.py "<request>" [chat_id]\n')
    return 2
  query = sys.argv[1]
  chat_id = sys.argv[2] if len(sys.argv) > 2 else ""

  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)

  cmd = [
    CLI_PATH,
    "-p",
    build_prompt(query),
    "--output-format",
    "stream-json",
    "--verbose",
    "--allowedTools",
    "Read",
    "Grep",
    "Glob",
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
