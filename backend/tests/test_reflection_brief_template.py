"""The baked Reflection scaffold keeps deterministic run metadata as tokens."""

from pathlib import Path
import re


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


def test_default_reflection_brief_title_uses_the_runner_owned_date_token():
  template = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "reflection-brief-template.html"
  ).read_text(encoding="utf-8")

  title = re.search(r"<title>(.*?)</title>", template, re.DOTALL)
  assert title is not None
  assert title.group(1).strip() == "Morning brief — {{DATE}}"
  assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", title.group(1)) is None

  dateline = re.search(
    r'<span class="dateline">(.*?)</span>', template, re.DOTALL,
  )
  assert dateline is not None
  assert dateline.group(1).strip() == "{{DATE_LONG}} · while you slept"
  assert re.search(r"\b20\d{2}\b", dateline.group(1)) is None
