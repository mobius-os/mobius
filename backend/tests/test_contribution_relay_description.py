import pytest

from app.github_contributions import ContributionSubmitError
from app.routes.contribution_relay import _reviewed_public_metadata


def test_reviewed_public_metadata_uses_only_the_exact_plan_text():
  record = {
    "title": "Internal title",
    "description": "Internal description",
    "summary": "Internal summary",
    "plan": {
      "title": "Reviewed title",
      "body_draft": "## Summary\n\nReviewed details.\n",
    },
  }

  assert _reviewed_public_metadata(record) == (
    "Reviewed title",
    "## Summary\n\nReviewed details.\n",
  )


def test_reviewed_public_metadata_rejects_legacy_fallbacks():
  with pytest.raises(ContributionSubmitError, match="no reviewed relay title"):
    _reviewed_public_metadata({"summary": "Legacy summary"})

  with pytest.raises(ContributionSubmitError, match="no reviewed relay title"):
    _reviewed_public_metadata({})
