"""Runtime resolution of contribution relay routing config.

Locks in the override-then-environment layering for both the target repository
and the anonymous-owner test-repository allowlist, the safe degradation of a
bad override edit to the environment, and the end-to-end guard the route
enforces — so retargeting the relay never needs a container recreate.
"""

import json

import pytest

from app import contribution_config
from app.routes.contribution_relay import (
  ContributionSubmitError,
  _configured_target_repo,
)


def _point_override(monkeypatch, tmp_path, contents=None, *, raw=None):
  path = tmp_path / "contribution-relay.json"
  if raw is not None:
    path.write_bytes(raw)
  elif contents is not None:
    path.write_text(json.dumps(contents), encoding="utf-8")
  monkeypatch.setattr(contribution_config, "_override_path", lambda: path)
  return path


def test_target_repo_empty_when_unset(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.delenv(contribution_config.TARGET_REPO_ENV, raising=False)
  assert contribution_config.target_repo() == ""


def test_target_repo_from_env(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "owner/repo")
  assert contribution_config.target_repo() == "owner/repo"


def test_target_repo_override_wins(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path, {"target_repo": "owner/from-file"})
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "owner/from-env")
  assert contribution_config.target_repo() == "owner/from-file"


def test_target_repo_blank_override_falls_back_to_env(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path, {"target_repo": "   "})
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "owner/env")
  assert contribution_config.target_repo() == "owner/env"


def test_target_repo_bad_override_falls_back_to_env(monkeypatch, tmp_path):
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "owner/env")
  for kwargs in (
    {"raw": b"{not json"},          # malformed JSON
    {"raw": b"\xff\xfe not utf-8"},  # non-UTF-8 bytes
    {"contents": ["owner/list"]},    # non-object JSON
    {"contents": {"target_repo": 123}},   # non-string value
    {"contents": {"target_repo": None}},   # null value
  ):
    _point_override(monkeypatch, tmp_path, **kwargs)
    assert contribution_config.target_repo() == "owner/env"


def test_test_repositories_parsed_from_env(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.setenv(
    contribution_config.TEST_REPOSITORIES_ENV, " Owner/One , owner/two ,",
  )
  assert contribution_config.test_repositories() == {"owner/one", "owner/two"}


def test_test_repositories_override_wins(monkeypatch, tmp_path):
  _point_override(
    monkeypatch, tmp_path, {"test_repositories": "owner/from-file"},
  )
  monkeypatch.setenv(
    contribution_config.TEST_REPOSITORIES_ENV, "owner/from-env",
  )
  assert contribution_config.test_repositories() == {"owner/from-file"}


def test_configured_target_repo_requires_explicit_target(monkeypatch, tmp_path):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.delenv(contribution_config.TARGET_REPO_ENV, raising=False)
  with pytest.raises(ContributionSubmitError) as exc:
    _configured_target_repo("owner/source")
  assert exc.value.code == "relay_target_not_configured"


def test_configured_target_repo_anonymous_owner_requires_same_repo(
  monkeypatch, tmp_path,
):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "mobius-os/mobius")
  monkeypatch.delenv(contribution_config.TEST_REPOSITORIES_ENV, raising=False)
  assert _configured_target_repo("mobius-os/mobius") == "mobius-os/mobius"
  with pytest.raises(ContributionSubmitError) as exc:
    _configured_target_repo("mobius-os/app-other")
  assert exc.value.code == "relay_target_mismatch"


def test_configured_target_repo_fork_requires_test_allowlist(
  monkeypatch, tmp_path
):
  _point_override(monkeypatch, tmp_path)
  monkeypatch.setenv(contribution_config.TARGET_REPO_ENV, "owner/fork")
  monkeypatch.delenv(contribution_config.TEST_REPOSITORIES_ENV, raising=False)
  with pytest.raises(ContributionSubmitError) as exc:
    _configured_target_repo("owner/source")
  assert exc.value.code == "anonymous_repo_not_allowed"


def test_configured_target_repo_fork_allowed_via_override_allowlist(
  monkeypatch, tmp_path
):
  # Both values from the live override — the whole point: retarget to a fork
  # and allow it without touching container env.
  _point_override(
    monkeypatch,
    tmp_path,
    {"target_repo": "owner/fork", "test_repositories": "owner/fork"},
  )
  monkeypatch.delenv(contribution_config.TARGET_REPO_ENV, raising=False)
  monkeypatch.delenv(contribution_config.TEST_REPOSITORIES_ENV, raising=False)
  assert _configured_target_repo("owner/source") == "owner/fork"
