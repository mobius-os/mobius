"""Reflection-in-the-middle: run a test, reflect, measure the improvement.

The headline capability. A long-tail fact buried in a chat note that no
retrieval reaches on the RAW tree is promoted into `notes/` + a router line by
`pure_consolidation`; after that, a router-traversing system reaches it, so
`node_recall_after > node_recall_before`. This is the "we see improvement after
reflection" proof, fully offline.
"""
from functools import partial
from pathlib import Path

from memeval.answerer import DeterministicStubAnswerer
from memeval.corpus import RetrievalCase
from memeval.reflection_stage import pure_consolidation
from memeval.runner import run_retrieval_eval_with_reflection
from memeval.systems import V2RouterOneHopSystem

BURIED_FACT = "The user is allergic to penicillin."


def _case_with_buried_fact() -> RetrievalCase:
  # The fact lives ONLY in a chat note body, with no router/notes entry. A
  # router-traversing system can't reach it on the raw tree (the root index has
  # no scent line pointing anywhere useful).
  tree = {
    "index.md": "# Memory router\n\n- unrelated cooking stuff [open](cooking/index.md)\n",
    "cooking/index.md": "# Cooking\n\n- nothing relevant [open](pasta.md)\n",
    "cooking/pasta.md": "---\ntitle: Pasta\ntype: fact\n---\n\nThe user likes pasta.\n",
    "chats/c1/index.md": f"---\ntype: chat\n---\n## Summary\n{BURIED_FACT}\n",
  }
  return RetrievalCase(
    id="penicillin",
    memory_tree=tree,
    # V2RouterOneHopSystem reports node ids as tree-relative paths, so the gold
    # id is the path pure_consolidation will create, not the bare slug.
    gold_node_ids=["notes/penicillin-allergy.md"],
    query="penicillin allergy?",
    gold_answer="penicillin",
    should_abstain=False,
    tier="long",
    gold_fact_strings=[BURIED_FACT],
  )


def test_reflection_in_the_middle_improves_recall():
  case = _case_with_buried_fact()
  reflect = partial(
    pure_consolidation,
    fact=BURIED_FACT,
    note_slug="penicillin-allergy",
    router_scent="penicillin allergy: the user is allergic to penicillin",
  )
  report = run_retrieval_eval_with_reflection(
    [case],
    V2RouterOneHopSystem(),
    DeterministicStubAnswerer(),
    reflect,
  )
  # BEFORE: the router can't reach the buried fact.
  assert report.metrics["node_recall_before"] == 0.0
  assert report.metrics["evidence_recall_before"] == 0.0
  # AFTER: consolidation promoted it to notes/ + a router line, so the router
  # traversal now surfaces the node AND the fact.
  assert report.metrics["node_recall_after"] == 1.0
  assert report.metrics["evidence_recall_after"] == 1.0
  # The delta is the improvement the owner wants to see.
  assert report.metrics["node_recall_delta"] > 0.0
  assert report.metrics["evidence_recall_delta"] > 0.0


def test_pure_consolidation_is_idempotent(tmp_path: Path):
  (tmp_path / "index.md").write_text("# Memory router\n", encoding="utf-8")
  pure_consolidation(tmp_path, fact=BURIED_FACT, note_slug="pen")
  pure_consolidation(tmp_path, fact=BURIED_FACT, note_slug="pen")
  index = (tmp_path / "index.md").read_text(encoding="utf-8")
  # Router line written exactly once despite two calls.
  assert index.count("notes/pen.md") == 1
  assert (tmp_path / "notes" / "pen.md").read_text(encoding="utf-8").count(BURIED_FACT) == 1
