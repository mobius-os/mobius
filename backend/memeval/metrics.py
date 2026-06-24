"""Pure scoring functions for the eval harness. No I/O, no LLM."""
from __future__ import annotations

import re

_ABSTAIN_MARKERS = ("i don't know", "i do not know", "not sure", "no information")


def node_recall(selected_ids: list[str], gold_ids: list[str]) -> float:
  """Fraction of gold nodes present in the selected set. No gold -> 1.0."""
  if not gold_ids:
    return 1.0
  sel = set(selected_ids)
  hit = sum(1 for g in gold_ids if g in sel)
  return hit / len(gold_ids)


def node_precision(selected: list[str], gold: list[str]) -> float:
  """Fraction of selected nodes that are gold. Empty selected -> no hit."""
  if not selected:
    return 1.0 if not gold else 0.0
  gold_set = set(gold)
  hit = sum(1 for item in selected if item in gold_set)
  return hit / len(selected)


def answer_match(answer: str, gold_answer: str) -> bool:
  """Normalised substring match (case/space-insensitive)."""
  a = " ".join(answer.lower().split())
  g = " ".join(gold_answer.lower().split())
  return g in a


def exact_answer_accuracy(predicted: str, gold: str) -> float:
  return 1.0 if _normalize(predicted) == _normalize(gold) else 0.0


def abstention_correct(answer: str, *, should_abstain: bool) -> bool:
  a = " ".join(answer.lower().split())
  abstained = any(m in a for m in _ABSTAIN_MARKERS)
  return abstained == should_abstain


def count_tokens(text: str) -> int:
  """Cheap whitespace token proxy (good enough for relative comparisons)."""
  return len(text.split())


def evidence_recall(context: str, gold_fact_strings: list[str]) -> float:
  """Fraction of gold fact strings actually present in the retrieved context.

  Where `node_recall` asks "did the right NODE surface," this asks the stricter
  question "is the answering FACT actually in the bytes the answerer can see."
  The right file can surface with the needle buried, paraphrased away, or
  truncated out — `node_recall` says 1.0 and the answerer still can't answer.
  Matching is normalized (casefold + whitespace-collapsed) substring; no gold
  facts means there is nothing to recall, so trivially 1.0.
  """
  if not gold_fact_strings:
    return 1.0
  haystack = _normalize(context)
  hit = sum(1 for fact in gold_fact_strings if _normalize(fact) in haystack)
  return hit / len(gold_fact_strings)


def evidence_present(context: str, gold_fact_strings: list[str]) -> bool:
  """True iff EVERY gold fact string is present in the context (recall == 1.0).

  The boolean sibling of `evidence_recall` for the common single-fact case where
  "did the evidence land at all" reads cleaner than a fraction."""
  return evidence_recall(context, gold_fact_strings) >= 1.0


def fact_precision(predicted: list[str], gold: list[str]) -> float:
  pred_set = {_normalize(fact) for fact in predicted if _normalize(fact)}
  gold_set = {_normalize(fact) for fact in gold if _normalize(fact)}
  if not pred_set:
    return 1.0 if not gold_set else 0.0
  return len(pred_set & gold_set) / len(pred_set)


def fact_recall(predicted: list[str], gold: list[str]) -> float:
  pred_set = {_normalize(fact) for fact in predicted if _normalize(fact)}
  gold_set = {_normalize(fact) for fact in gold if _normalize(fact)}
  if not gold_set:
    return 1.0
  return len(pred_set & gold_set) / len(gold_set)


def fact_f1(predicted: list[str], gold: list[str]) -> float:
  precision = fact_precision(predicted, gold)
  recall = fact_recall(predicted, gold)
  if precision + recall == 0:
    return 0.0
  return 2 * precision * recall / (precision + recall)


def _normalize(text: str) -> str:
  return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())
