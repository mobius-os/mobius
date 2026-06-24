from memeval.metrics import (
  abstention_correct,
  answer_match,
  count_tokens,
  evidence_present,
  evidence_recall,
  exact_answer_accuracy,
  fact_f1,
  fact_precision,
  fact_recall,
  node_precision,
  node_recall,
)


def test_node_recall():
  assert node_recall(["a", "b"], ["a", "c"]) == 0.5
  assert node_recall([], ["a"]) == 0.0
  assert node_recall(["a"], []) == 1.0  # nothing required -> trivially satisfied


def test_node_precision():
  assert node_precision(["a", "b"], ["a"]) == 0.5
  assert node_precision([], ["a"]) == 0.0
  assert node_precision([], []) == 1.0


def test_answer_match_is_normalised_substring():
  assert answer_match("It is Python.", "python")
  assert not answer_match("It is Rust.", "python")


def test_exact_answer_accuracy_is_normalised_equality():
  assert exact_answer_accuracy(" Grams! ", "grams") == 1.0
  assert exact_answer_accuracy("use grams", "grams") == 0.0


def test_abstention_correct():
  assert abstention_correct("I don't know", should_abstain=True)
  assert not abstention_correct("It is Berlin", should_abstain=True)
  assert abstention_correct("It is Berlin", should_abstain=False)


def test_count_tokens_is_monotonic():
  assert count_tokens("") == 0
  assert count_tokens("a b c") < count_tokens("a b c d e f")


def test_evidence_recall_is_normalised_substring():
  ctx = "<<< chats/c1/index.md >>>\nThe user PREFERS  metric units.\n"
  # Normalised match: casefold + whitespace-collapsed, so the buried fact counts.
  assert evidence_recall(ctx, ["the user prefers metric units"]) == 1.0
  # Two golds, one present -> 0.5 (the right node can surface with one fact buried).
  assert evidence_recall(ctx, ["the user prefers metric units", "Pixel has epilepsy"]) == 0.5
  assert evidence_recall(ctx, ["Pixel has epilepsy"]) == 0.0
  # No gold facts -> nothing to recall, trivially satisfied.
  assert evidence_recall("", []) == 1.0


def test_evidence_present_is_all_or_nothing():
  ctx = "The user prefers metric units."
  assert evidence_present(ctx, ["the user prefers metric units"])
  assert not evidence_present(ctx, ["the user prefers metric units", "missing fact"])
  assert evidence_present(ctx, [])


def test_evidence_recall_catches_node_recall_blind_spot():
  # node_recall says the file surfaced; evidence_recall says the FACT is missing.
  surfaced_node = ["notes/units.md"]
  assert node_recall(surfaced_node, ["notes/units.md"]) == 1.0
  buried_context = "<<< notes/units.md >>>\n(see linked hub for the detail)\n"
  assert evidence_recall(buried_context, ["the user prefers metric units"]) == 0.0


def test_fact_set_metrics_are_normalised():
  predicted = ["The user prefers metric units.", "Transient meal plan."]
  gold = ["the user prefers metric units"]
  assert fact_precision(predicted, gold) == 0.5
  assert fact_recall(predicted, gold) == 1.0
  assert round(fact_f1(predicted, gold), 2) == 0.67
