#!/usr/bin/env python3
"""Reproducible recall scoreboard for Möbius memory.

The problem this solves: a chat's persisted `messages` keep only the final text,
NOT the tool blocks — so you can't tell from the DB/API whether the agent actually
ran the memory-search subagent, which notes it read, or whether the search
crashed. The truth lives in the CLI session jsonl. This tool reads THAT, so recall
behaviour is observable and regression-testable.

What it does:
  1. SEED a known corpus into a (test!) container's `/data/shared/memory`:
     long-term facts that live ONLY in graph `notes/` (reached via the search
     subagent) and short-term facts in recent `chats/<id>/index.md` (injected at
     session start by mtime). This is the discriminator.
  2. RUN a set of probes — each a question tagged short/long with the fact that
     answers it — driving a fresh chat per probe through the real API, then
     reading the session jsonl to report, per probe:
       - searched?   did the agent actually invoke memory_search.py (a real Bash
                      tool_use, not just text)?
       - crashed?    did a memory_search call error (e.g. the cwd-import bug)?
       - read:       which graph nodes it Read.
       - recall:     does the answer contain the expected fact (right info, not
                     random)?
       - verdict:    short-term should NOT need a search; long-term SHOULD search.

Usage (default = seed then run):
  recall-probe.py            [--container mobius-test] [--port 8001] [--user admin] [--password admin]
  recall-probe.py --seed     ...      # seed only
  recall-probe.py --run      ...      # run probes against an already-seeded container

NEVER point this at prod — it RESETS /data/shared/memory to a synthetic corpus
(wipes chats/notes/mocs so prior chats can't leak long-term facts into the
injected recent-10, which would break the short-vs-long discriminator). It
refuses a container whose name contains 'prod' or whose base ends in :8000.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

# --- the corpus: long-term facts ONLY in notes/, short-term in recent chats ----

# notes/<slug>.md  (long-term — reached via the search subagent, NOT injected)
NOTES = {
  "coffee-brewing": (
    "How the partner brews coffee", ["coffee", "preferences"],
    "The partner brews **pour-over** coffee at a **1:16** coffee-to-water ratio, "
    "water at **92°C**, **medium grind**, drunk black. 30 g dose → ~480 ml. "
    "Any coffee app/timer should default to THESE numbers, never generic ones.",
  ),
  "thesis-deep-decisions": (
    "The partner's PhD thesis", ["thesis", "projects"],
    "The partner is writing a **UCL Computer Science PhD thesis** titled "
    "**“Deep Decision Making”**, stitching four published papers into one "
    "monograph. Wants a compelling narrative, strong arguments, no AI-bot overselling.",
  ),
  "design-prefs": (
    "The partner's app-design taste", ["design", "preferences"],
    "Every app should feel **Apple-quality**: simple, beautiful, functional, "
    "information-dense without clutter. Visual register: **dark charcoal background "
    "with an emerald accent**. They notice spacing, hierarchy, restraint.",
  ),
}

# chats/<id>/index.md  (short-term — injected by mtime; first listed = newest)
CHAT_NOTES = [
  ("c-habits", "building a daily habits tracker",
   "Built a Habits app last week; the partner ticks it each morning over coffee. "
   "Wants a streak view next.",
   ["Built a Habits app; checked every morning.", "intent: a lasting daily routine."]),
  ("c-mira", "Mira starting school in September",
   "The partner's daughter Mira is 6 and starts primary school this September.",
   ["The partner's daughter is Mira; she is 6.",
    "Mira starts primary school in September 2026.",
    "intent: prepare for Mira's first school day."]),
  ("c-lisbon", "planning the July trip to Lisbon",
   "Planning a Lisbon trip in July; wants a packing list.",
   ["Travelling to Lisbon in July 2026.", "intent: plan the Lisbon trip."]),
]

# a minimal valid router for the reset graph (one real link → no orphans/dangling)
INDEX_MD = ("---\ntitle: Memory — Home\ntype: moc\n---\n# Memory\n\n"
            "Injected at session start. Follow a wiki-link to read detail on demand.\n\n"
            "## Maps\n\n- [[about-the-user]] — who the partner is: preferences, projects.\n")

POLL_INTERVAL_S = 5
MAX_WAIT_S = 360  # a real LLM turn (incl. a build) can run several minutes

# The discriminator corpus + tier vocabulary here is the LIVE-container twin of
# the offline fixtures in backend/memeval/corpus.py (make_chat_tree); kept
# separate on purpose — that one writes a local tmp tree, this writes inside the
# container and runs build_memory_graph.py. Keep the tier semantics in sync.

# probes: (question, tier, [expected fact substrings], should_search)
PROBES = [
  ("What's my daughter's name and how old is she again?",
   "short", ["Mira", "6"], False),
  ("Remind me exactly how I brew my coffee — the ratio and the water temperature?",
   "long", ["1:16", "92"], True),
  ("What is my PhD thesis about?",
   "long", ["Deep Decision Making"], True),
]


def _note_md(title, tags, body):
  return (f"---\ntitle: {title}\ntype: note\nimportance: 3\naccess_count: 0\n"
          f"last_accessed: null\ntags: [{', '.join(tags)}]\nmocs: [about-the-user]\n"
          f"created: 2026-06-24\nupdated: 2026-06-24\n---\n{body}\n")


def _about_user_md():
  links = "\n".join(f"- [[{slug}]] — {title}" for slug, (title, _, _) in NOTES.items())
  return ("---\ntitle: About the user\ntype: moc\ntags: [user]\n---\n# About the user\n\n"
          "Durable facts about the partner.\n\n"
          f"## Preferences & projects\n\n{links}\n")


def _chat_md(desc, summary, facts):
  return (f"---\ntype: chat\ndescription: {desc}\n---\n## Summary\n{summary}\n\n"
          "## Facts & intent\n" + "".join(f"- {f}\n" for f in facts))


def _assistant_text(msgs):
  """The assistant's text across a chat's messages (content is a plain string or
  a list of blocks). Used to score recall against the expected fact."""
  out = []
  for m in msgs:
    if m.get("role") != "assistant":
      continue
    c = m.get("content")
    out.append(c if isinstance(c, str) else
               " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text"))
  return " ".join(t for t in out if t).strip()


# --- container plumbing --------------------------------------------------------


class Probe:
  def __init__(self, container, port, user, password):
    self.container = container
    self.base = f"http://localhost:{port}"
    self.user = user
    self.password = password
    self._tok = None

  def _guard(self):
    # This RESETS /data/shared/memory, so positively REQUIRE a test container —
    # the real prod is named `mobius` on :8000, so a 'prod'-substring check would
    # miss it; require 'test' in the name + refuse :8000 as belt-and-suspenders.
    if "test" not in self.container or self.base.endswith(":8000"):
      sys.exit("refusing: recall-probe RESETS /data/shared/memory and only runs "
               f"against a TEST container (got container={self.container}, "
               f"base={self.base}). Name it *test* and use a non-8000 port.")

  def dexec(self, *cmd, user="mobius"):
    return subprocess.run(["docker", "exec", "-u", user, self.container, *cmd],
                          capture_output=True, text=True)

  def token(self):
    if self._tok:
      return self._tok
    data = f"username={self.user}&password={self.password}".encode()
    req = urllib.request.Request(self.base + "/api/auth/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    self._tok = json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]
    return self._tok

  def api(self, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(self.base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {self.token()}")
    if body is not None:
      req.add_header("Content-Type", "application/json")
    raw = urllib.request.urlopen(req, timeout=60).read().decode()
    return json.loads(raw) if raw.strip() else {}

  def busy(self):
    s = self.api("GET", "/api/debug/status")
    return sum(len(s.get(k, [])) for k in ("active_sdk_clients", "active_sdk_sessions", "starting"))

  # --- seed ---

  def seed(self):
    self._guard()
    mem = "/data/shared/memory"
    # write notes, the map, and chat notes via a single in-container python so we
    # control mtimes locally (docker cp mtimes are unreliable across hosts).
    payload = {
      "notes": {slug: _note_md(*meta) for slug, meta in NOTES.items()},
      "about": _about_user_md(),
      "index": INDEX_MD,
      "chats": [(cid, _chat_md(desc, summ, facts)) for cid, desc, summ, facts in CHAT_NOTES],
    }
    seeder = (
      "import os, json, time, shutil, subprocess\n"
      f"mem = {mem!r}\n"
      f"p = json.loads({json.dumps(json.dumps(payload))})\n"
      # RESET to a clean minimal graph: prior chat notes leak 'long-term' facts
      # into the injected recent-10, which breaks the short-vs-long discriminator.
      "for sub in ('chats','notes','mocs'):\n"
      "    shutil.rmtree(f'{mem}/{sub}', ignore_errors=True); os.makedirs(f'{mem}/{sub}')\n"
      "open(mem + '/index.md','w').write(p['index'])\n"
      "[open(f'{mem}/notes/{s}.md','w').write(b) for s,b in p['notes'].items()]\n"
      "open(mem + '/mocs/about-the-user.md','w').write(p['about'])\n"
      "now = time.time()\n"
      "for i,(cid,body) in enumerate(p['chats']):\n"
      "    d = f'{mem}/chats/{cid}'; os.makedirs(d, exist_ok=True)\n"
      "    open(d + '/index.md','w').write(body)\n"
      "    mt = now - i*3600  # first = newest\n"
      "    os.utime(d + '/index.md', (mt, mt))\n"
      "r = subprocess.run(['python3','/app/scripts/build_memory_graph.py'],\n"
      "    capture_output=True, text=True, env={**os.environ,'DATA_DIR':'/data'})\n"
      "open(mem + '/.ready','w').close()\n"
      "print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'graph: (no output)')\n"
      "print('rc', r.returncode)\n"
    )
    r = self.dexec("python3", "-c", seeder)
    print("seed:", (r.stdout + r.stderr).strip()[-400:])
    return r.returncode == 0

  # --- run a probe ---

  def _session_jsonl(self, cid):
    # the CLI session jsonl for a chat, by its STORED session_id — robust, where
    # grepping the jsonls by question text breaks on unicode (an em-dash in the
    # question escapes differently than it's stored, finding nothing).
    prog = ("import sqlite3,sys;c=sqlite3.connect('/data/db/ultimate.db');"
            "r=c.execute('select session_id from chats where id=?',(sys.argv[1],)).fetchone();"
            "print(r[0] if r and r[0] else '')")
    sid = self.dexec("python3", "-c", prog, cid).stdout.strip()
    return f"/data/cli-auth/claude/projects/-data/{sid}.jsonl" if sid else ""

  def _analyse_jsonl(self, path):
    """Parse the session jsonl for HOW the agent reached the answer:
    - subagent runs: real memory_search.py Bash tool_use
    - graph reads: Read/Grep/Bash tool_use touching /shared/memory (the agent
      can reach a shallow fact by Read-ing the linked note directly, not only via
      the subagent — both count as 'went to the graph')
    - crashes: a tool_result carrying ModuleNotFoundError (the recall-arm bug)"""
    raw = self.dexec("cat", path).stdout
    subagent = crashes = graph_reads = 0
    nodes = []
    for ln in raw.splitlines():
      try:
        o = json.loads(ln)
      except ValueError:
        continue
      msg = o.get("message") or {}
      content = msg.get("content") if isinstance(msg, dict) else None
      for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
          continue
        if b.get("type") == "tool_use":
          inp = b.get("input") or {}
          cmd = inp.get("command", "")
          blob = cmd or inp.get("file_path") or inp.get("pattern") or ""
          if "memory_search.py" in blob:
            subagent += 1
          elif "shared/memory" in blob and not (">" in cmd or "<<" in cmd):
            # a READ of the graph (the Read/Grep tool, or a cat/grep) — NOT the
            # agent's own note WRITE (cat > chats/<id>/index.md <<EOF, which also
            # mentions a memory path).
            graph_reads += 1
            label = blob.split("shared/memory/")[-1].split()[0][:60]
            if any(seg in label for seg in ("notes/", "mocs/", "chats/")):
              nodes.append(label)
        if b.get("type") == "tool_result":
          if "ModuleNotFoundError" in json.dumps(b.get("content")):
            crashes += 1
    return subagent, graph_reads, crashes, sorted(set(nodes))

  def run_probe(self, question, tier, expected, should_search):
    chat = self.api("POST", "/api/chats", {"title": f"probe: {tier}"})
    cid = chat["id"]
    self.api("POST", f"/api/chats/{cid}/messages", {"content": question})
    # poll until the turn settles; ignore the first couple of idle reads — the
    # run may not have registered in /api/debug/status yet (startup grace).
    for i in range(MAX_WAIT_S // POLL_INTERVAL_S):
      time.sleep(POLL_INTERVAL_S)
      if self.busy() == 0 and i > 1:
        break
    msgs = self.api("GET", f"/api/chats/{cid}?limit=40").get("messages", [])
    answer = _assistant_text(msgs)
    jl = self._session_jsonl(cid)
    subagent = graph_reads = crashes = 0
    nodes = []
    if jl:
      subagent, graph_reads, crashes, nodes = self._analyse_jsonl(jl)
    graph_access = subagent > 0 or graph_reads > 0
    mechanism = ("subagent" if subagent else ("direct-read" if graph_reads else "injection"))
    recalled = [e for e in expected if e.lower() in answer.lower()]
    recall_ok = len(recalled) == len(expected)
    # honest verdict: short-term should answer from INJECTION (no graph trip);
    # long-term must GO TO THE GRAPH (subagent or direct read) — a long answer
    # with no graph access means contamination or hallucination, not recall.
    access_ok = (graph_access == should_search)
    return {
      "tier": tier, "question": question, "mechanism": mechanism, "crashed": crashes > 0,
      "nodes": nodes, "recall_hits": recalled, "recall_total": len(expected),
      "recall_ok": recall_ok, "access_ok": access_ok, "answer_empty": not answer.strip(),
    }

  def run(self):
    self._guard()
    print(f"\n{'tier':6} {'recall':9} {'reached via':13} {'crash':6} question")
    print("-" * 80)
    results = []
    for q, tier, expected, should in PROBES:
      r = self.run_probe(q, tier, expected, should)
      results.append(r)
      rv = f"{len(r['recall_hits'])}/{r['recall_total']}" + (" ✓" if r["recall_ok"] else " ✗")
      mech = r["mechanism"] + ("" if r["access_ok"] else " ⚠")
      crash = "EMPTY" if r["answer_empty"] else ("YES" if r["crashed"] else "no")
      print(f"{tier:6} {rv:9} {mech:13} {crash:6} {q[:44]}")
      if r["nodes"]:
        print(f"       read: {', '.join(r['nodes'])}")
    ok = all(r["recall_ok"] and not r["crashed"] and not r["answer_empty"] for r in results)
    access_note = all(r["access_ok"] for r in results)
    print("-" * 80)
    print("RECALL:", "ALL PASS ✓" if ok else "FAILURES ✗",
          "| reach-mechanism as-expected:" , "yes" if access_note else "no (see ⚠)")
    return ok


def main():
  ap = argparse.ArgumentParser(description="Reproducible Möbius recall scoreboard.")
  ap.add_argument("--container", default="mobius-test")
  ap.add_argument("--port", default="8001")
  ap.add_argument("--user", default="admin")
  ap.add_argument("--password", default="admin")
  ap.add_argument("--seed", action="store_true", help="seed only (default: seed then run)")
  ap.add_argument("--run", action="store_true", help="run probes only (default: seed then run)")
  a = ap.parse_args()
  p = Probe(a.container, a.port, a.user, a.password)
  # neither flag → do both; --seed or --run → that phase only.
  do_seed = a.seed or not a.run
  do_run = a.run or not a.seed
  ok = True
  if do_seed:
    ok = p.seed() and ok
  if do_run:
    ok = p.run() and ok
  sys.exit(0 if ok else 1)


if __name__ == "__main__":
  main()
