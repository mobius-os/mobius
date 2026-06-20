from memeval.metrics import (
  abstention_correct,
  answer_match,
  count_tokens,
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


def test_fact_set_metrics_are_normalised():
  predicted = ["The user prefers metric units.", "Transient meal plan."]
  gold = ["the user prefers metric units"]
  assert fact_precision(predicted, gold) == 0.5
  assert fact_recall(predicted, gold) == 1.0
  assert round(fact_f1(predicted, gold), 2) == 0.67
