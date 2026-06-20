"""Eval runners for memory-v2 capture, retrieval, consolidation, and e2e."""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from memeval.answerer import Answerer
from memeval.consolidation_checks import (
  bootstrap_retired,
  broken_router_target_count,
  dangling_wikilink_count,
  duplicate_normalized_title_count,
  hub_fan_in,
  missing_directory_index_count,
  orphan_count,
)
from memeval.corpus import (
  CaptureCase,
  ConsolidationFixture,
  E2ECase,
  RetrievalCase,
  write_corpus,
)
from memeval.metrics import (
  abstention_correct,
  answer_match,
  count_tokens,
  fact_f1,
  fact_precision,
  fact_recall,
  node_precision,
  node_recall,
)
from memeval.systems import MemorySystem


@dataclass
class EvalReport:
  n: int
  metrics: dict[str, float] = field(default_factory=dict)
  details: list[dict] = field(default_factory=list)

  @property
  def mean_node_recall(self) -> float:
    return self.metrics.get("mean_node_recall", 0.0)

  @property
  def answer_accuracy(self) -> float:
    return self.metrics.get("answer_accuracy", 0.0)

  @property
  def abstention_accuracy(self) -> float:
    return self.metrics.get("abstention_accuracy", 0.0)

  @property
  def mean_context_tokens(self) -> float:
    return self.metrics.get("mean_context_tokens", 0.0)

  def summary(self) -> str:
    rendered = " ".join(f"{key}={value:.2f}" for key, value in sorted(self.metrics.items()))
    return f"n={self.n} {rendered}".strip()


def run_capture_eval(
    cases: list[CaptureCase], fact_extractor: Callable[[str], list[str]]
) -> EvalReport:
  precisions: list[float] = []
  recalls: list[float] = []
  f1s: list[float] = []
  details: list[dict] = []
  for case in cases:
    transcript = _render_transcript(case.transcripts)
    source = f"{transcript}\n\nDaytime inbox:\n{case.daytime_inbox}".strip()
    predicted = fact_extractor(source)
    precisions.append(fact_precision(predicted, case.gold_facts))
    recalls.append(fact_recall(predicted, case.gold_facts))
    f1s.append(fact_f1(predicted, case.gold_facts))
    details.append({"id": case.id, "predicted_facts": predicted})
  return EvalReport(
    n=len(cases),
    metrics={
      "fact_precision": _mean(precisions),
      "fact_recall": _mean(recalls),
      "fact_f1": _mean(f1s),
    },
    details=details,
  )


def run_retrieval_eval(
    cases: list[RetrievalCase], system: MemorySystem, answerer: Answerer
) -> EvalReport:
  recalls: list[float] = []
  precisions: list[float] = []
  answer_hits: list[float] = []
  abstain_hits: list[float] = []
  tokens: list[int] = []
  details: list[dict] = []
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for case in cases:
      case_system = _system_for_case(system, case, root / case.id)
      res = case_system.retrieve(case.query)
      out = answerer.answer(context=res.context, query=case.query)
      recalls.append(node_recall(res.selected_node_ids, case.gold_node_ids))
      precisions.append(node_precision(res.selected_node_ids, case.gold_node_ids))
      tokens.append(count_tokens(res.context))
      if case.should_abstain:
        abstain_hits.append(float(abstention_correct(out, should_abstain=True)))
      else:
        answer_hits.append(float(answer_match(out, case.gold_answer)))
      details.append({
        "id": case.id,
        "selected_node_ids": res.selected_node_ids,
        "answer": out,
      })
  return EvalReport(
    n=len(cases),
    metrics={
      "mean_node_recall": _mean(recalls),
      "mean_node_precision": _mean(precisions),
      "answer_accuracy": _mean(answer_hits) if answer_hits else 0.0,
      "abstention_accuracy": _mean(abstain_hits) if abstain_hits else 1.0,
      "mean_context_tokens": _mean(tokens),
    },
    details=details,
  )


def run_consolidation_eval(fixtures: list[ConsolidationFixture]) -> EvalReport:
  pass_hits: list[float] = []
  details: list[dict] = []
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for fixture in fixtures:
      tree = write_corpus(fixture, root / fixture.id)
      observed = _consolidation_observed(tree)
      expected = fixture.expected_invariants
      hits = [_invariant_matches(observed, key, value) for key, value in expected.items()]
      pass_hits.append(float(all(hits)))
      details.append({"id": fixture.id, "observed": observed, "expected": expected})
  return EvalReport(
    n=len(fixtures),
    metrics={"invariant_accuracy": _mean(pass_hits)},
    details=details,
  )


def run_e2e_eval(
    cases: list[E2ECase], system: MemorySystem, answerer: Answerer
) -> EvalReport:
  retrieval_cases: list[RetrievalCase] = []
  for case in cases:
    for i, question in enumerate(case.questions):
      retrieval_cases.append(
        RetrievalCase(
          id=f"{case.id}:{i}",
          memory_tree=case.memory_tree,
          query=question.text,
          gold_node_ids=[],
          gold_answer=question.gold_answer,
          should_abstain=question.should_abstain,
        )
      )
  return run_retrieval_eval(retrieval_cases, system, answerer)


def run_eval(corpus, system: MemorySystem, answerer: Answerer) -> EvalReport:
  """Compatibility shim for the old skeleton runner name."""
  if isinstance(corpus, list):
    return run_retrieval_eval(corpus, system, answerer)
  raise TypeError("run_eval now expects retrieval cases; use the split v2 runners")


def _system_for_case(system: MemorySystem, case: RetrievalCase | E2ECase, tree_dir: Path) -> MemorySystem:
  write_corpus(case, tree_dir)
  factory = getattr(system, "for_tree", None)
  if callable(factory):
    return factory(tree_dir)
  return system


def _consolidation_observed(tree: Path) -> dict:
  return {
    "orphan_count": orphan_count(tree),
    "duplicate_normalized_title_count": duplicate_normalized_title_count(tree),
    "dangling_wikilink_count": dangling_wikilink_count(tree),
    "hub_fan_in": hub_fan_in(tree),
    "missing_directory_index_count": missing_directory_index_count(tree),
    "broken_router_target_count": broken_router_target_count(tree),
    "bootstrap_retired": bootstrap_retired(tree),
  }


def _invariant_matches(observed: dict, key: str, expected: object) -> bool:
  value = observed.get(key)
  if isinstance(expected, dict) and isinstance(value, dict):
    return all(value.get(k) == v for k, v in expected.items())
  return value == expected


def _render_transcript(turns) -> str:
  return "\n".join(f"{turn.role}: {turn.content}" for turn in turns)


def _mean(values: list[float] | list[int]) -> float:
  return sum(values) / len(values) if values else 0.0
