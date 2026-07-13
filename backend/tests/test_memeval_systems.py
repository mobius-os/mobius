from pathlib import Path

from memeval.answerer import DeterministicStubAnswerer, SealedLLMAnswerer
from memeval.corpus import RETRIEVAL_CASES, write_corpus
from memeval.systems import (
  FixedBundleBaselineSystem,
  FlatInboxSystem,
  NoMemorySystem,
  ProductionInjectionSystem,
  V2RouterOneHopSystem,
)


def test_no_memory_returns_empty_context():
  res = NoMemorySystem().retrieve("anything")
  assert res.context == ""
  assert res.selected_node_ids == []


def test_fixed_bundle_returns_all_markdown_files(tmp_path: Path):
  tree = write_corpus(RETRIEVAL_CASES[0], tmp_path)
  res = FixedBundleBaselineSystem(tree).retrieve("unrelated")
  assert "cooking/baking-setup.md" in res.selected_node_ids
  assert "hubs/units.md" in res.selected_node_ids
  assert "The user wants cake recipes in grams." in res.context


def test_flat_inbox_reads_flat_notes_directory(tmp_path: Path):
  notes = tmp_path / "notes"
  notes.mkdir()
  (notes / "a.md").write_text("A", encoding="utf-8")
  (notes / "b.md").write_text("B", encoding="utf-8")
  res = FlatInboxSystem(tmp_path).retrieve("anything")
  assert res.selected_node_ids == ["notes/a.md", "notes/b.md"]
  assert "A" in res.context and "B" in res.context


def test_v2_router_selects_matching_note_and_one_hop_see_also(tmp_path: Path):
  tree = write_corpus(RETRIEVAL_CASES[0], tmp_path)
  res = V2RouterOneHopSystem(tree).retrieve("cake recipe, cups or grams?")
  assert res.selected_node_ids == ["cooking/baking-setup.md", "hubs/units.md"]
  assert "cake recipes in grams" in res.context
  assert "prefers metric units" in res.context


def test_v2_router_abstention_query_selects_nothing(tmp_path: Path):
  tree = write_corpus(RETRIEVAL_CASES[0], tmp_path)
  res = V2RouterOneHopSystem(tree).retrieve("what is my blood type?")
  assert res.context == ""
  assert res.selected_node_ids == []


def test_production_injection_exercises_the_real_build_memory_block(tmp_path: Path):
  # Chat continuity injects only recent per-chat notes through the live
  # app.memory.build_memory_block. The optional graph router stays behind the
  # Memory system app's on-demand reader.
  mem = tmp_path / "shared" / "memory"
  (mem / "chats" / "c1").mkdir(parents=True)
  (mem / "index.md").write_text("# Home\n\n- cooking router scent\n", encoding="utf-8")
  (mem / "chats" / "c1" / "index.md").write_text(
    "---\ntype: chat\ndescription: cake chat\n---\n"
    "## Summary\nUser wants cake recipes in grams.\n",
    encoding="utf-8",
  )
  (mem / ".ready").write_text("", encoding="utf-8")
  res = ProductionInjectionSystem(tmp_path).retrieve("cups or grams?")
  # Query-independent continuity must not pull graph state into a new chat.
  assert res.selected_node_ids == ["chat:c1"]
  assert "cooking router scent" not in res.context
  assert "cake recipes in grams" in res.context


def test_production_injection_empty_without_ready_sentinel(tmp_path: Path):
  # No `.ready` -> graph mode is gated off -> an empty block (the agent reads
  # the graph on demand instead). Lock in the gate the live model relies on.
  mem = tmp_path / "shared" / "memory"
  mem.mkdir(parents=True)
  (mem / "index.md").write_text("# Home\n", encoding="utf-8")
  res = ProductionInjectionSystem(tmp_path).retrieve("anything")
  assert res.context == ""
  assert res.selected_node_ids == []


def test_deterministic_stub_answerer_never_needs_gold():
  ans = DeterministicStubAnswerer(
    fixed_answer=None,
    context_answers=[("flashing-light toys", "Avoid flashing-light toys.")],
  )
  assert ans.answer(context="Pixel should avoid flashing-light toys.", query="Pixel?") == (
    "Avoid flashing-light toys."
  )
  assert "don't know" in ans.answer(context="unrelated", query="Pixel?").lower()


def test_sealed_llm_answerer_wraps_injectable_callable():
  seen = {}

  def complete(prompt: str) -> str:
    seen["prompt"] = prompt
    return "grams"

  ans = SealedLLMAnswerer(complete)
  assert ans.answer(context="The user prefers grams.", query="cups or grams?") == "grams"
  assert "Context:\nThe user prefers grams." in seen["prompt"]
  assert "Query:\ncups or grams?" in seen["prompt"]
