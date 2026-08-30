from app.routes.contribution_relay import _reviewed_description


def test_reviewed_description_prefers_the_visible_plan_body():
  record = {
    "plan": {"body_draft": "## Summary\n\nReviewed details.\n"},
    "description": "Internal description",
    "summary": "Internal summary",
  }

  assert _reviewed_description(record) == "## Summary\n\nReviewed details."


def test_reviewed_description_has_safe_legacy_fallbacks():
  assert _reviewed_description({"summary": "  Legacy summary  "}) == "Legacy summary"
  assert _reviewed_description({}) == "Reviewed in Möbius."
