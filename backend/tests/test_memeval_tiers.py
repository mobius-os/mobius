"""Short-term (injected recent-10) vs long-term (graph via search) tiers.

Proves the discrimination the owner wants: a short-only system
(`ProductionInjectionSystem`, the recent-`RECENT_CHAT_NOTES` window) FAILS a
'long' case — a fact aged past the window, living only in `notes/` — that a
`MemorySearchSystem` (driven by a deterministic stub that finds it) RECOVERS.
"""
import time
from pathlib import Path

from app.memory import RECENT_CHAT_NOTES
from memeval.answerer import DeterministicStubAnswerer
from memeval.corpus import RetrievalCase, make_chat_tree
from memeval.metrics import evidence_recall, node_recall
from memeval.runner import run_retrieval_eval
from memeval.systems import (
  MemorySearchSystem,
  ProductionInjectionSystem,
  SearchResult,
)

LONG_FACT = "The user's late grandmother's recipe uses 240 grams of flour."


def _tree_with_buried_long_fact(root: Path) -> Path:
  """A chat tree where the long fact lives in a chat note pushed BEYOND the
  recent-window by mtime, and also promoted into notes/ (the long-term home the
  search arm reads). The recent window is filled with newer, unrelated chats."""
  now = time.time()
  notes = {f"recent{i}": f"---\ntype: chat\n---\n## Summary\nRecent chat {i}.\n"
           for i in range(RECENT_CHAT_NOTES + 2)}
  # The chat that ORIGINALLY mentioned the long fact, aged far into the past so
  # it sorts past the recent-window cutoff and injection never sees it.
  notes["grandma"] = f"---\ntype: chat\n---\n## Summary\n{LONG_FACT}\n"
  mtimes = {f"recent{i}": now - i for i in range(RECENT_CHAT_NOTES + 2)}
  mtimes["grandma"] = now - 10_000_000  # ancient -> beyond the recent-10 window
  # The long-term home: the consolidated note the search arm reaches.
  extra = {
    "notes/grandma-flour.md":
      f"---\ntitle: Grandma flour\ntype: fact\n---\n\n{LONG_FACT}\n",
  }
  return make_chat_tree(notes, mtimes=mtimes, extra=extra, target_dir=root)


def test_short_only_misses_long_fact_that_search_recovers(tmp_path: Path):
  mem = _tree_with_buried_long_fact(tmp_path / "shared" / "memory")

  # Short-term arm: the real injection. The ancient 'grandma' note is beyond the
  # recent-10 window, so the long fact is NOT in the injected context.
  injected = ProductionInjectionSystem(tmp_path).retrieve("grandmother flour grams?")
  assert evidence_recall(injected.context, [LONG_FACT]) == 0.0
  assert "notes/grandma-flour.md" not in injected.selected_node_ids

  # Long-term arm: a deterministic stub standing in for the memory-search
  # subagent finds the consolidated note and returns it.
  def stub_search(query: str) -> SearchResult:
    body = (mem / "notes" / "grandma-flour.md").read_text(encoding="utf-8")
    return SearchResult(context=body, node_ids=["grandma-flour"])

  recovered = MemorySearchSystem(stub_search).retrieve("grandmother flour grams?")
  assert evidence_recall(recovered.context, [LONG_FACT]) == 1.0
  assert node_recall(recovered.selected_node_ids, ["grandma-flour"]) == 1.0


def test_runner_splits_recall_by_tier(tmp_path: Path):
  # A short case (fact in the injected window) and a long case (fact only in
  # notes/). ProductionInjectionSystem answers the short, fails the long; the
  # runner reports the split so the failure is visible per-tier.
  now = time.time()
  short_tree = {
    "shared/memory/index.md": "# Memory router\n",
    "shared/memory/.ready": "",
    "shared/memory/chats/s1/index.md":
      "---\ntype: chat\n---\n## Summary\nThe user prefers metric units.\n",
  }
  long_tree = {
    "shared/memory/index.md": "# Memory router\n",
    "shared/memory/.ready": "",
    "shared/memory/notes/blood.md":
      "---\ntitle: Blood type\ntype: fact\n---\n\nThe user's blood type is O negative.\n",
  }
  cases = [
    RetrievalCase(
      id="short-metric",
      memory_tree=short_tree,
      query="metric units?",
      gold_node_ids=["chat:s1"],
      gold_answer="metric",
      should_abstain=False,
      tier="short",
      gold_fact_strings=["The user prefers metric units."],
    ),
    RetrievalCase(
      id="long-blood",
      memory_tree=long_tree,
      query="blood type?",
      gold_node_ids=["blood"],
      gold_answer="O negative",
      should_abstain=False,
      tier="long",
      gold_fact_strings=["The user's blood type is O negative."],
    ),
  ]
  report = run_retrieval_eval(cases, ProductionInjectionSystem(), DeterministicStubAnswerer())
  # Split keys exist and discriminate: short tier recalls, long tier does not.
  assert report.metrics["node_recall_short"] == 1.0
  assert report.metrics["evidence_recall_short"] == 1.0
  assert report.metrics["node_recall_long"] == 0.0
  assert report.metrics["evidence_recall_long"] == 0.0
