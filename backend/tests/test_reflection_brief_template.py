"""The baked Reflection scaffold asks only questions earned by a real run."""

from pathlib import Path


def test_default_reflection_brief_has_no_placeholder_question_carrier():
  template = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "reflection-brief-template.html"
  ).read_text(encoding="utf-8")

  assert "intentionally absent by default" in template
  assert "data-report-questions" not in template
  assert "{{QUESTION_" not in template
  assert "{{INPUT_" not in template


def test_default_reflection_brief_has_no_unverified_run_ledger():
  template = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "reflection-brief-template.html"
  ).read_text(encoding="utf-8")

  assert "Run ledger" not in template
  assert "{{N_INTERVIEWED}}" not in template
  assert ".ledger" not in template
  assert "Run at a glance" in template
  assert "Relative token use" in template
  assert "What I learned" not in template
  assert "conversation continues" not in template.lower()
  assert "Ask me anything" not in template
