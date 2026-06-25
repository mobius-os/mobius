#!/usr/bin/env python3
"""'Week in the life' reflection demo — tests the UNVALIDATED memory/reflection
goals with the isolation discipline from the plan review.

The load-bearing experiment: a durable fact stated OBLIQUELY in an OLD chat note
(beyond the recent-10 injection window) is unreachable pre-reflection (the
injected block doesn't carry it, and the baseline question is lexically disjoint
so a memory_search grep can't reach the oblique wording). Reflection consolidates
it into a clear notes/ note + lifts the scent into index.md (always injected). So
a FRESH post-reflection chat finds it FROM INJECTION — a clean A/B on the
always-on surface that isolates reflection's consolidation (= the live version of
the harness's reflection-in-the-middle, and the "prepared next morning" proof).

Subcommands (run in order; trigger reflection yourself between baseline + measure):
  seed      reset memory → clean persona graph → drive the real demo chats → bury
            the oblique fact (backdate mtime + DB updated_at) + filler to push it
            past recent-10.
  baseline  fresh chat, ask the buried fact; report MISS + whether it searched.
  measure   grep the brief + graph for reflection's behaviours (consolidation,
            film-app suggestion, recs, cross-chat connection).
  post      fresh chat, ask the buried fact again; report HIT (from injection).

NEVER point at prod — reuses recall-probe's test-only guard.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

_rp = importlib.util.spec_from_file_location(
  "recall_probe", str(Path(__file__).resolve().parent / "recall-probe.py"))
RP = importlib.util.module_from_spec(_rp)
_rp.loader.exec_module(RP)

MEM = "/data/shared/memory"

# --- persona: pre-consolidated graph notes (2 of the 3 multi-fact-build facts) -
PERSONA_NOTES = {
  "coffee-ratio": ("How the partner brews coffee", "coffee",
    "The partner brews pour-over at a **1:16** ratio, water **92°C**, medium "
    "grind, black. A coffee app should default to these numbers."),
  "standup": ("The partner's daily standup", "routine",
    "The partner runs a daily standup at **9:30** every weekday and wants a "
    "reminder a few minutes before."),
  # the RECURRING-topic signal for the new-app suggestion (no film app exists)
  "loves-films": ("The partner is into films", "interests",
    "The partner is really into films — they keep a running watch-list **by "
    "hand** and have said more than once they wish they could just track what "
    "they've watched and want to watch. There is **no film/movie app** installed."),
}

ABOUT = ("---\ntitle: About the user\ntype: moc\ntags: [user]\n---\n# About the user\n\n"
         "Durable facts about the partner.\n\n## Preferences & interests\n\n"
         "- [[coffee-ratio]] — how they brew their coffee (1:16, 92°C).\n"
         "- [[standup]] — daily 9:30 standup.\n"
         "- [[loves-films]] — into films; tracks a watch-list by hand, no app.\n")

INDEX = ("---\ntitle: Memory — Home\ntype: moc\n---\n# Memory\n\n"
         "Injected at session start. Follow a wiki-link to read detail on demand.\n\n"
         "## Maps\n\n- [[about-the-user]] — who the partner is.\n")

# real chats to DRIVE through the chat path (so reflection sees real signal).
# (key, first message). The agent's reply + the turn-end note make them real.
DRIVE_CHATS = {
  # the oblique buried fact — peanut allergy, never the word "allergy/dietary"
  "buried": "Ugh, work lunch today — had to skip the satay again because of the "
            "peanut thing. Always awkward explaining why I can't touch it. Anyway, "
            "unrelated: can you remind me what 1:16 works out to for a big mug?",
  # film signal #1 (legible tracking pain)
  "film1": "I finally watched Dune Part Two — visually stunning but the pacing "
           "dragged in the middle. I really need to start tracking what I've seen, "
           "I keep forgetting which films I've already watched.",
  # film signal #2 (legible tracking pain again)
  "film2": "Recommend me a slow, atmospheric sci-fi film like Blade Runner 2049? "
           "And remind me — my mental watch-list is getting unmanageable, I wish I "
           "had somewhere to keep it.",
  # cross-chat project #1
  "proj1": "I've been sketching a little budgeting side-project, working name "
           "Pennywise. Just thinking out loud about what it'd need.",
  # cross-chat project #2 (same project, separate chat — connection latent)
  "proj2": "More on the Pennywise budgeting project — I want it to categorise my "
           "spending automatically and warn me when I overspend on dining out.",
}

# the buried-fact recall question — lexically DISJOINT from "satay/peanut" so a
# memory_search grep on its nouns can't reach the oblique old note pre-reflection.
BURIED_Q = ("Before you ever suggest a recipe or a restaurant for me — is there "
            "any food or ingredient I need you to steer clear of?")
BURIED_TOKENS = ["peanut"]   # the fact that must surface


def _persona_note_md(title, tag, body):
  return (f"---\ntitle: {title}\ntype: note\nimportance: 3\naccess_count: 0\n"
          f"last_accessed: null\ntags: [{tag}]\nmocs: [about-the-user]\n"
          "created: 2026-06-24\nupdated: 2026-06-24\n---\n" + body + "\n")


def seed(p: "RP.Probe"):
  p._guard()
  # 1. reset to a clean persona graph (no prior-run contamination)
  notes = {s: _persona_note_md(*meta) for s, meta in PERSONA_NOTES.items()}
  payload = json.dumps({"notes": notes, "about": ABOUT, "index": INDEX})
  seeder = (
    "import os, json, shutil, subprocess\n"
    f"mem={MEM!r}; p=json.loads({json.dumps(payload)})\n"
    "for sub in ('chats','notes','mocs'):\n"
    "    shutil.rmtree(f'{mem}/{sub}', ignore_errors=True); os.makedirs(f'{mem}/{sub}')\n"
    "open(mem+'/index.md','w').write(p['index'])\n"
    "open(mem+'/mocs/about-the-user.md','w').write(p['about'])\n"
    "[open(f'{mem}/notes/{s}.md','w').write(b) for s,b in p['notes'].items()]\n"
    "r=subprocess.run(['python3','/app/scripts/build_memory_graph.py'],"
    "capture_output=True,text=True,env={**os.environ,'DATA_DIR':'/data'})\n"
    "open(mem+'/.ready','w').close()\n"
    "print((r.stdout or r.stderr).strip().splitlines()[-1])\n")
  print("reset+graph:", p.dexec("python3", "-c", seeder).stdout.strip())

  # 2. DRIVE the demo chats through the real chat path (real notes + traces)
  ids = {}
  for key, msg in DRIVE_CHATS.items():
    cid = p.api("POST", "/api/chats", {"title": f"demo:{key}"})["id"]
    p.api("POST", f"/api/chats/{cid}/messages", {"content": msg})
    print(f"  driving {key} ({cid[:8]}) …", flush=True)
    _settle(p, cid)
    ids[key] = cid
  print("driven:", {k: v[:8] for k, v in ids.items()})

  # 3. BURY the oblique fact: backdate its chat-note mtime (old) + DB updated_at
  #    (>24h, so it's a consolidation target not an interview target), and write
  #    filler chat notes NEWER than it so it falls past the recent-10 window.
  bury = (
    "import os, time, sqlite3\n"
    f"mem={MEM!r}; bid={ids['buried']!r}\n"
    "old = time.time() - 6*86400\n"
    f"os.utime(f'{{mem}}/chats/{{bid}}/index.md', (old, old))\n"
    "con=sqlite3.connect('/data/db/ultimate.db')\n"
    "con.execute(\"update chats set updated_at = datetime('now','-6 days') where id=?\",(bid,))\n"
    "con.commit()\n"
    # 11 filler chat notes, all newer than the buried one → push it past recent-10
    "for i in range(11):\n"
    "    d=f'{mem}/chats/filler-{i}'; os.makedirs(d, exist_ok=True)\n"
    "    open(d+'/index.md','w').write('---\\ntype: chat\\ndescription: a quick "
    "throwaway chat\\n---\\n## Summary\\nMinor housekeeping chat.\\n')\n"
    "    t=time.time()-i*60; os.utime(d+'/index.md',(t,t))\n"
    "print('buried + 11 filler written')\n")
  print(p.dexec("python3", "-c", bury).stdout.strip())
  # stash the ids for later phases
  p.dexec("bash", "-lc", f"echo '{json.dumps(ids)}' > /tmp/demo-ids.json")
  return True


def _chat_running(p, cid):
  """True while THIS chat's turn is active. Polls the chat_id specifically —
  global busy() breaks when an UNRELATED turn hangs (a lingering SDK client)."""
  s = p.api("GET", "/api/debug/status")
  ids = set()
  for key in ("active_sdk_clients", "active_sdk_sessions", "starting"):
    for x in s.get(key, []):
      ids.add(x.get("chat_id") if isinstance(x, dict) else x)
  return cid in ids


def _settle(p, cid):
  for i in range(RP.MAX_WAIT_S // RP.POLL_INTERVAL_S):
    time.sleep(RP.POLL_INTERVAL_S)
    if i > 1 and not _chat_running(p, cid):
      break
  time.sleep(2)  # brief grace for the note backstop after the turn clears


def _drive_fresh(p, question):
  """A FRESH chat (new session) asking `question`; returns (answer, searched,
  injected_has_token). Injected-block-only unless the agent chooses to search."""
  cid = p.api("POST", "/api/chats", {"title": "demo:recall"})["id"]
  p.api("POST", f"/api/chats/{cid}/messages", {"content": question})
  _settle(p, cid)
  msgs = p.api("GET", f"/api/chats/{cid}?limit=40").get("messages", [])
  answer = RP._assistant_text(msgs)
  jl = p._session_jsonl(cid)
  searched = injected = False
  if jl:
    sub, _gr, _cr, _nodes = p._analyse_jsonl(jl)   # proper tool_use parse
    searched = sub > 0
    raw = p.dexec("cat", jl).stdout
    # the injected <agent_experience> block rides the FIRST user message, before
    # any assistant turn — so the token appearing there means injection carried it.
    head = raw.split('"role":"assistant"', 1)[0].lower()
    injected = any(t.lower() in head for t in BURIED_TOKENS)
  return answer, searched, injected


def baseline(p):
  ans, searched, inj = _drive_fresh(p, BURIED_Q)
  hit = any(t.lower() in ans.lower() for t in BURIED_TOKENS)
  print("\n=== BASELINE (pre-reflection) ===")
  print(f"  searched (memory_search)? {searched}   token in injected block? {inj}")
  print(f"  recall HIT? {hit}   (want MISS)")
  print(f"  answer: {ans[:240]!r}")
  print("  => " + ("MISS ✓ (isolated)" if not hit else "HIT — confound, the fact was reachable pre-reflection"))
  return not hit


def post(p):
  ans, searched, inj = _drive_fresh(p, BURIED_Q)
  hit = any(t.lower() in ans.lower() for t in BURIED_TOKENS)
  print("\n=== POST-REFLECTION (fresh chat = 'prepared next morning') ===")
  print(f"  searched (memory_search)? {searched}   token in injected block? {inj}")
  print(f"  recall HIT? {hit}   (want HIT)")
  print(f"  answer: {ans[:240]!r}")
  print("  => " + ("HIT ✓ — from injection" if hit and inj else
                    ("HIT (via search, not injection)" if hit else "MISS — consolidation didn't surface it")))
  return hit


def measure(p):
  print("\n=== REFLECTION ARTIFACT CHECKS ===")
  g = lambda c: p.dexec("bash", "-lc", c).stdout.strip()
  # 1. buried fact consolidated into notes/ + scent lifted into index.md
  note = g(f"grep -rl -i peanut {MEM}/notes/ 2>/dev/null | head -1")
  scent = g(f"grep -il -E 'peanut|allerg|dietary' {MEM}/index.md {MEM}/mocs/*.md 2>/dev/null | head -1")
  print(f"  [consolidation] peanut in a notes/ file: {note or 'NO'}")
  print(f"  [consolidation] scent in index/MOC (router-reachable): {scent or 'NO'}")
  # 2. the brief
  brief = g("ls -t /data/apps/*/reports/*.html 2>/dev/null | head -1")
  if not brief:
    print("  [brief] NONE FOUND — did reflection run + write a brief?")
  else:
    txt = g(f"python3 -c \"import re,html,sys; t=open('{brief}').read(); "
            "t=re.sub(r'<(script|style).*?</\\\\1>','',t,flags=re.S); "
            "print(html.unescape(re.sub(r'<[^>]+>',' ',t)))\"")
    low = txt.lower()
    film_app = ("film" in low or "movie" in low or "watch" in low) and ("build" in low or "app" in low)
    recs = "recommend" in low or "watch next" in low
    triad = all(w in low for w in ("trigger", "why")) or "next" in low
    print(f"  [brief] {brief}")
    print(f"  [new-app suggestion] film/movie app proposed: {film_app}")
    print(f"  [recommendations] brief has recs: {recs}")
    print(f"  [usefulness] trigger/why/next-action triad present: {triad}")
  # 3. cross-chat connection between the two Pennywise chats
  conn = g(f"grep -rl -i pennywise {MEM}/notes/ {MEM}/mocs/ 2>/dev/null | head -1")
  print(f"  [connection] pennywise promoted to a notes/ or MOC: {conn or 'NO'}")
  # 4. PROVENANCE (the new idea): consolidated notes carry source: [chat:...]
  prov = g(f"grep -rli 'source:' {MEM}/notes/ 2>/dev/null | wc -l")
  total = g(f"ls {MEM}/notes/ 2>/dev/null | wc -l")
  print(f"  [provenance] notes/ with a source: field: {prov} / {total} total")
  # 5. SCOPE-NARROWING (the new idea): chat→chat links rewritten to chat→note.
  #    seed had film2→[[chats/film1]] + proj2→[[chats/proj1]]; after narrowing
  #    those should become [[<note>]] links. Count what remains vs note-links.
  cc = g(f"grep -rhoE '\\[\\[chats/[^]]+\\]\\]' {MEM}/chats/*/index.md 2>/dev/null | wc -l")
  cn = g(f"grep -rhoE '\\[\\[(notes/)?[a-z][a-z0-9-]+\\]\\]' {MEM}/chats/*/index.md 2>/dev/null | grep -vi chats | wc -l")
  print(f"  [scope-narrowing] remaining chat→chat [[chats/]] links: {cc}; chat→note links: {cn} (narrowed = chat→chat fell, chat→note rose)")
  return True


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("phase", choices=["seed", "baseline", "measure", "post"])
  ap.add_argument("--container", default="mobius-test")
  ap.add_argument("--port", default="8001")
  ap.add_argument("--user", default="admin")
  ap.add_argument("--password", default="admin")
  a = ap.parse_args()
  p = RP.Probe(a.container, a.port, a.user, a.password)
  {"seed": seed, "baseline": baseline, "measure": measure, "post": post}[a.phase](p)


if __name__ == "__main__":
  main()
