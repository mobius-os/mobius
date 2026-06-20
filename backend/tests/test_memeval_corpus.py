from pathlib import Path

from memeval.corpus import (
  CAPTURE_CASES,
  CONSOLIDATION_FIXTURES,
  CORPUS,
  E2E_CASES,
  RETRIEVAL_CASES,
  CaptureCase,
  E2ECase,
  RetrievalCase,
  write_corpus,
)


def test_corpus_exposes_minimum_v2_dimensions():
  assert len(CORPUS["capture"]) >= 2
  assert len(CORPUS["retrieval"]) >= 2
  assert len(CORPUS["consolidation"]) >= 8
  assert len(CORPUS["e2e"]) >= 2
  assert all(isinstance(case, CaptureCase) for case in CAPTURE_CASES)
  assert all(isinstance(case, RetrievalCase) for case in RETRIEVAL_CASES)
  assert all(isinstance(case, E2ECase) for case in E2E_CASES)


def test_write_corpus_materializes_retrieval_tree(tmp_path: Path):
  tree = write_corpus(RETRIEVAL_CASES[0], tmp_path)
  assert (tree / "index.md").is_file()
  assert (tree / "cooking" / "index.md").is_file()
  assert (tree / "cooking" / "baking-setup.md").is_file()
  assert (tree / "hubs" / "units.md").is_file()
  assert (tree / ".ready").is_file()


def test_required_example_fixtures_are_present():
  capture = next(case for case in CAPTURE_CASES if case.id == "metric-units-no-cups")
  assert "The user prefers metric units." in capture.gold_facts
  assert "The user dislikes converting cups/Fahrenheit." in capture.gold_facts
  assert "The user is making a meal plan today." in capture.excluded_facts

  retrieval = next(case for case in RETRIEVAL_CASES if case.id == "cake-recipe-units")
  assert retrieval.gold_node_ids == ["cooking/baking-setup.md", "hubs/units.md"]
  assert retrieval.gold_answer == "grams"

  abstention = next(case for case in RETRIEVAL_CASES if case.should_abstain)
  assert not abstention.gold_node_ids

  assert any(case.id == "pixel-enrichment" for case in E2E_CASES)
  assert any(case.id == "defect-bootstrap-not-retired" for case in CONSOLIDATION_FIXTURES)
