from memeval.answerer import DeterministicStubAnswerer
from memeval.corpus import (
  CAPTURE_CASES,
  CONSOLIDATION_FIXTURES,
  E2E_CASES,
  RETRIEVAL_CASES,
)
from memeval.runner import (
  run_capture_eval,
  run_consolidation_eval,
  run_e2e_eval,
  run_retrieval_eval,
)
from memeval.systems import NoMemorySystem, V2RouterOneHopSystem


def test_run_capture_eval_scores_fact_extraction():
  def extractor(text: str) -> list[str]:
    facts = []
    if "metric" in text:
      facts.append("The user prefers metric units.")
      facts.append("The user dislikes converting cups/Fahrenheit.")
    if "Pixel" in text:
      facts.extend([
        "The user's dog is named Pixel.",
        "Pixel has epilepsy.",
        "Pixel should avoid flashing-light toys.",
      ])
    return facts

  report = run_capture_eval(CAPTURE_CASES, extractor)
  assert report.n == len(CAPTURE_CASES)
  assert report.metrics["fact_precision"] == 1.0
  assert report.metrics["fact_recall"] == 1.0
  assert report.metrics["fact_f1"] == 1.0


def test_run_retrieval_eval_never_passes_gold_to_answerer():
  answerer = DeterministicStubAnswerer(
    fixed_answer=None,
    context_answers=[("cake recipes in grams", "grams")],
  )
  report = run_retrieval_eval(RETRIEVAL_CASES, V2RouterOneHopSystem(), answerer)
  assert report.n == len(RETRIEVAL_CASES)
  assert report.mean_node_recall == 1.0
  assert report.answer_accuracy == 1.0
  assert report.abstention_accuracy == 1.0
  assert report.mean_context_tokens > 0
  assert "mean_node_recall" in report.summary()


def test_run_retrieval_eval_shows_no_memory_baseline_cannot_answer():
  answerer = DeterministicStubAnswerer(fixed_answer=None)
  report = run_retrieval_eval(RETRIEVAL_CASES, NoMemorySystem(), answerer)
  assert report.mean_node_recall == 1.0 / len(RETRIEVAL_CASES)
  assert report.answer_accuracy == 0.0
  assert report.abstention_accuracy == 1.0


def test_run_consolidation_eval_scores_expected_invariants():
  report = run_consolidation_eval(CONSOLIDATION_FIXTURES)
  assert report.n == len(CONSOLIDATION_FIXTURES)
  assert report.metrics["invariant_accuracy"] == 1.0


def test_run_e2e_eval_compares_systems_with_stub_answerer():
  answerer = DeterministicStubAnswerer(
    fixed_answer=None,
    context_answers=[
      ("flashing-light toys", "flashing-light toys"),
      ("cake recipes in grams", "grams"),
    ],
  )
  routed = run_e2e_eval(E2E_CASES, V2RouterOneHopSystem(), answerer)
  none = run_e2e_eval(E2E_CASES, NoMemorySystem(), answerer)
  assert routed.answer_accuracy == 1.0
  assert none.answer_accuracy == 0.0


def test_run_e2e_eval_reports_evidence_recall_from_gold_fact_strings():
  # E2EQuestion.gold_fact_strings is now wired through: the router surfaces the
  # node AND the fact, so evidence_recall is 1.0; NoMemory has empty context so
  # the answering fact is absent -> evidence_recall 0.0. This is the RIGHT-INFO
  # signal node_recall alone can't give for the e2e cases.
  answerer = DeterministicStubAnswerer(fixed_answer="stub")
  routed = run_e2e_eval(E2E_CASES, V2RouterOneHopSystem(), answerer)
  none = run_e2e_eval(E2E_CASES, NoMemorySystem(), answerer)
  assert "evidence_recall" in routed.metrics
  assert routed.metrics["evidence_recall"] == 1.0
  assert none.metrics["evidence_recall"] == 0.0
