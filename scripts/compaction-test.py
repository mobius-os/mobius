#!/usr/bin/env python3
"""Compaction-vs-summary test.

Premise: the per-chat note's `## Summary` is maintained every turn, so it should
already contain everything a context-window COMPACTION of the full transcript
would — a bit more detail, but a superset. If so, compaction can become
compact(summary) instead of compact(full transcript): cheaper (summary << chat)
and it reuses distillation we already pay for.

This drives a substantial MULTI-TURN chat, then:
  compact_full     = compact the full transcript      (what compaction produces today)
  summary          = the note's maintained ## Summary
  compact_summary  = compact the SUMMARY
and LLM-judges:
  SUPERSET    — does `summary` contain all durable info in `compact_full`?
  EQUIVALENCE — is `compact_summary` ≈ `compact_full`?
Both holding validates wiring the note into compaction.

Usage: compaction-test.py --container mobius-test-memv2 --port 8037
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

_rp = importlib.util.spec_from_file_location(
  "recall_probe", str(Path(__file__).resolve().parent / "recall-probe.py"))
RP = importlib.util.module_from_spec(_rp)
_rp.loader.exec_module(RP)

# a substantial back-and-forth: durable facts, a chosen option, a constraint, and
# a mid-stream CHANGE (Sat→Sun) — exactly the things compaction must preserve.
TURNS = [
  "Help me plan a dinner party for 6 next Saturday — something impressive but not "
  "a nightmare to cook. Give me two or three directions.",
  "Let's go with the hand-rolled pasta direction. Two of the guests are vegetarian, "
  "and remember I genuinely can't do peanuts — anaphylactic, so nothing with them "
  "anywhere near the kitchen.",
  "Good. For dessert keep it light — a sorbet. I'll handle drinks myself, a couple "
  "of natural wines (one orange). Can you suggest a starter that fits?",
  "Actually we have to move it to Sunday, Saturday just got booked. And squeeze in "
  "a small cheese course between the main and the sorbet.",
]

COMPACT_PROMPT = (
  "You are compacting a conversation so it fits a context window — the next agent "
  "will continue from YOUR compaction WITHOUT the original. Produce a tight "
  "compaction: the durable facts, the decisions made (and any that CHANGED), open "
  "threads, constraints, and context needed to continue. Drop pleasantries and "
  "dead ends. Output ONLY the compaction.\n\n--- CONTENT ---\n")

SUPERSET_PROMPT = (
  "A is a COMPACTION of a chat transcript. B is a separately-maintained running "
  "SUMMARY of the same chat. Question: does B contain ALL the durable information "
  "in A (it may have MORE — that's fine)? List each concrete fact/decision/"
  "constraint in A that is MISSING from B (or 'none'). End with exactly one line: "
  "VERDICT: SUPERSET  or  VERDICT: MISSING.\n\n=== A (compaction of transcript) ===\n"
  "{a}\n\n=== B (maintained summary) ===\n{b}\n")

EQUIV_PROMPT = (
  "Two compactions of the same chat. FULL was compacted from the raw transcript; "
  "SUMM was compacted from a maintained running summary. Do they carry the SAME "
  "durable information (facts, decisions, changes, constraints)? List anything in "
  "FULL that is missing or wrong in SUMM (or 'none'). End with exactly one line: "
  "VERDICT: EQUIVALENT  or  VERDICT: DIVERGENT.\n\n=== FULL ===\n{full}\n\n"
  "=== SUMM ===\n{summ}\n")


def _claude(p, prompt):
  r = p.dexec("env", "CLAUDE_CONFIG_DIR=/data/cli-auth/claude", "/usr/local/bin/claude",
              "-p", prompt, "--output-format", "text", "--tools", "",
              "--model", "claude-sonnet-4-6")
  return (r.stdout or r.stderr).strip()


def _transcript(p, cid):
  msgs = p.api("GET", f"/api/chats/{cid}?limit=80").get("messages", [])
  out = []
  for m in msgs:
    c = m.get("content")
    t = c if isinstance(c, str) else " ".join(
      b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if t.strip():
      out.append(f"{m.get('role')}: {t.strip()}")
  return "\n\n".join(out)


def _summary(p, cid):
  raw = p.dexec("cat", f"/data/shared/memory/chats/{cid}/index.md").stdout
  # pull the ## Summary section (up to the next ## )
  if "## Summary" not in raw:
    return ""
  body = raw.split("## Summary", 1)[1]
  return body.split("\n## ", 1)[0].strip()


def run(p):
  p._guard()
  cid = p.api("POST", "/api/chats", {"title": "compaction-test"})["id"]
  print(f"driving a {len(TURNS)}-turn chat ({cid[:8]}) …")
  for n, msg in enumerate(TURNS, 1):
    p.api("POST", f"/api/chats/{cid}/messages", {"content": msg})
    for i in range(RP.MAX_WAIT_S // RP.POLL_INTERVAL_S):
      time.sleep(RP.POLL_INTERVAL_S)
      if p.busy() == 0 and i > 1:
        break
    print(f"  turn {n}/{len(TURNS)} done")

  transcript = _transcript(p, cid)
  summary = _summary(p, cid)
  print(f"\ntranscript: {len(transcript)} chars | maintained summary: {len(summary)} chars")
  if not summary:
    print("NO maintained summary — the note has no ## Summary; can't compare.")
    return False

  print("compacting full transcript …")
  compact_full = _claude(p, COMPACT_PROMPT + transcript)
  print("compacting the maintained summary …")
  compact_summary = _claude(p, COMPACT_PROMPT + summary)
  print("judging superset (summary ⊇ compaction) …")
  superset = _claude(p, SUPERSET_PROMPT.format(a=compact_full, b=summary))
  print("judging equivalence (compact(summary) ≈ compact(full)) …")
  equiv = _claude(p, EQUIV_PROMPT.format(full=compact_full, summ=compact_summary))

  out = {
    "chat_id": cid, "transcript_chars": len(transcript), "summary_chars": len(summary),
    "compact_full": compact_full, "summary": summary,
    "compact_summary": compact_summary, "superset_judge": superset, "equiv_judge": equiv,
  }
  Path("/tmp/compaction-test.json").write_text(json.dumps(out, indent=2))

  def verdict(text, key):
    line = next((l for l in text.splitlines() if l.strip().upper().startswith("VERDICT")), "")
    return key in line.upper()

  print("\n" + "=" * 72)
  print("--- maintained SUMMARY ---\n" + summary)
  print("\n--- COMPACT(full transcript) ---\n" + compact_full)
  print("\n--- COMPACT(summary) ---\n" + compact_summary)
  print("\n--- SUPERSET judge (summary contains all of compact_full?) ---\n" + superset)
  print("\n--- EQUIVALENCE judge (compact_summary ≈ compact_full?) ---\n" + equiv)
  print("=" * 72)
  ok = verdict(superset, "SUPERSET") and verdict(equiv, "EQUIVALENT")
  print("RESULT:", "PREMISE HOLDS ✓ (summary can drive compaction)" if ok
        else "PREMISE NOT FULLY HELD ✗ — see judges (full output: /tmp/compaction-test.json)")
  return ok


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--container", default="mobius-test")
  ap.add_argument("--port", default="8001")
  ap.add_argument("--user", default="admin")
  ap.add_argument("--password", default="admin")
  a = ap.parse_args()
  p = RP.Probe(a.container, a.port, a.user, a.password)
  run(p)


if __name__ == "__main__":
  main()
