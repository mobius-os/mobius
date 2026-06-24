"""Reflection stages for the before/after harness.

`run_retrieval_eval_with_reflection` (in `runner.py`) scores retrieval on the raw
written tree, runs a `reflect_fn(data_dir)` IN THE MIDDLE, then re-scores on the
mutated tree — the headline capability: "run a test, reflect, measure the
improvement." A `reflect_fn` takes the memory-tree root and mutates it in place.

Two are provided:

- `pure_consolidation` — a DETERMINISTIC, CLI-free stand-in for what reflection's
  phase-3 consolidation does to ONE fact: promote a buried durable fact out of a
  per-chat note that has aged past the recent-`RECENT_CHAT_NOTES` window into a
  first-class `notes/<slug>.md`, and add a router line in `index.md` pointing at
  it. After this, a router-traversing or search system can reach the fact that
  injection alone could not — so `node_recall_after > node_recall_before`. Fully
  offline; this is what makes the before/after harness unit-testable.

- `live_reflection` — a thin wrapper that shells the REAL
  `backend/scripts/reflection_runner.py` against the test `DATA_DIR`. LIVE-GATED
  (`MEMEVAL_LIVE=1`), mirroring `run_live_eval.py`; never runs in unit tests.

WHERE THE STAND-IN DIVERGES FROM REAL REFLECTION (the live gap to keep in mind):
real phase-3 consolidation is an LLM judgement call — it reads the day's chat
notes, DECIDES which spans are durable facts worth promoting, dedups against
existing notes, may merge several chat mentions into one atomic note, writes a
proper frontmatter note, threads it under the right MOC/hub, and rebuilds
`graph.json` via the indexer. `pure_consolidation` does the mechanical SHAPE of
exactly one such promotion (chat-note span → `notes/<slug>.md` + a router line)
with a caller-supplied fact and slug — no extraction judgement, no dedup, no MOC
threading, no `graph.json` rebuild. It proves the harness measures a real
reachability delta; it does not reproduce reflection's selection intelligence.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def pure_consolidation(
    data_dir: str | Path,
    *,
    fact: str,
    note_slug: str,
    router_scent: str | None = None,
) -> None:
  """Promote one buried durable `fact` into `notes/<note_slug>.md` and add a
  router line for it in `index.md`. Deterministic, CLI-free. Idempotent: a
  second call with the same slug rewrites the same note and does not duplicate
  the router line.

  `data_dir` is the memory-tree root (the dir holding `index.md` + `chats/`).
  `router_scent` is the human-facing scent text for the new router line; it
  defaults to the fact itself so a scent-matching router can find it.
  """
  root = Path(data_dir)
  notes_dir = root / "notes"
  notes_dir.mkdir(parents=True, exist_ok=True)
  note_path = notes_dir / f"{note_slug}.md"
  note_path.write_text(
    f"---\ntitle: {note_slug.replace('-', ' ').title()}\ntype: fact\n---\n\n{fact.strip()}\n",
    encoding="utf-8",
  )

  index = root / "index.md"
  scent = router_scent if router_scent is not None else fact.strip()
  link = f"- {scent} [open](notes/{note_slug}.md)"
  existing = index.read_text(encoding="utf-8") if index.is_file() else "# Memory router\n"
  # Idempotent: don't append a duplicate router line for the same target.
  if f"notes/{note_slug}.md" not in existing:
    if not existing.endswith("\n"):
      existing += "\n"
    existing += link + "\n"
  index.write_text(existing, encoding="utf-8")


def live_reflection(
    data_dir: str | Path,
    *,
    timeout: int = 7200,
) -> None:
  """Shell the REAL `backend/scripts/reflection_runner.py` against `data_dir`.

  LIVE ONLY — refuses to run unless `MEMEVAL_LIVE=1` (mirrors `run_live_eval.py`
  and `MemorySearchSystem.live`). The reflection runner reads `DATA_DIR` from the
  env and runs the full nightly pass (interview, consolidate, brief), so this
  expects a fully-provisioned test `DATA_DIR` (CLI creds, reflection skill,
  settings). Unit tests use `pure_consolidation` instead.
  """
  if os.environ.get("MEMEVAL_LIVE") != "1":
    raise RuntimeError(
      "live_reflection requires MEMEVAL_LIVE=1 (it shells the real "
      "reflection_runner.py). Unit tests must use pure_consolidation instead."
    )
  runner = Path(__file__).resolve().parents[1] / "scripts" / "reflection_runner.py"
  env = dict(os.environ, DATA_DIR=str(data_dir))
  subprocess.run(
    ["python3", str(runner)],
    env=env,
    timeout=timeout,
    check=False,
  )


def _slugify(text: str) -> str:
  """Lowercase hyphenated slug — handy when a caller wants a slug from the fact."""
  return "-".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())[:60] or "note"
