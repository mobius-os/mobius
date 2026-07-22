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
