"""Sealed answerers for the memory eval harness.

Answerers receive only the retrieved context and the user query. They never see
gold answers, gold facts, or selected node ids.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


class Answerer(Protocol):
  def answer(self, *, context: str, query: str) -> str: ...


@dataclass(frozen=True)
class TriggeredAnswer:
  trigger: str
  answer: str
  source: str = "context"  # "context" or "query"


class DeterministicStubAnswerer:
  """Predictable no-LLM answerer for plumbing tests.

  By default this returns a fixed answer. Tests that need context-sensitive
  behaviour can provide trigger rules without giving the answerer gold labels.
  """

  def __init__(
      self,
      fixed_answer: str | None = "stub answer",
      *,
      default_answer: str = "I don't know.",
      context_answers: Iterable[tuple[str, str] | TriggeredAnswer] = (),
      query_answers: Iterable[tuple[str, str] | TriggeredAnswer] = (),
  ):
    self._fixed_answer = fixed_answer
    self._default_answer = default_answer
    self._rules: list[TriggeredAnswer] = []
    for rule in context_answers:
      self._rules.append(_coerce_rule(rule, source="context"))
    for rule in query_answers:
      self._rules.append(_coerce_rule(rule, source="query"))

  def answer(self, *, context: str, query: str) -> str:
    if self._fixed_answer is not None:
      return self._fixed_answer
    haystacks = {
      "context": context.casefold(),
      "query": query.casefold(),
    }
    for rule in self._rules:
      if rule.trigger.casefold() in haystacks[rule.source]:
        return rule.answer
    return self._default_answer


class SealedLLMAnswerer:
  """Wraps an injectable completion function for offline-testable LLM use."""

  def __init__(self, complete: Callable[[str], str]):
    self._complete = complete

  def answer(self, *, context: str, query: str) -> str:
    prompt = (
      "Answer the query using only the supplied memory context.\n"
      "If the context does not contain the answer, say \"I don't know.\"\n\n"
      f"Context:\n{context}\n\n"
      f"Query:\n{query}\n\n"
      "Answer:"
    )
    return self._complete(prompt)


def _coerce_rule(
    rule: tuple[str, str] | TriggeredAnswer, *, source: str
) -> TriggeredAnswer:
  if isinstance(rule, TriggeredAnswer):
    return rule
  trigger, answer = rule
  return TriggeredAnswer(trigger=trigger, answer=answer, source=source)
