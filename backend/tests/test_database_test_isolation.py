"""A test process cannot inherit an unmarked application database."""

import pytest

from app import database


def test_test_database_requires_disposable_runtime_marker(monkeypatch):
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  monkeypatch.delenv("MOBIUS_TEST_DATABASE_ISOLATED", raising=False)

  with pytest.raises(RuntimeError, match="unisolated test process"):
    database._assert_test_database_isolated()


def test_disposable_runtime_marker_allows_test_database(monkeypatch):
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  monkeypatch.setenv("MOBIUS_TEST_DATABASE_ISOLATED", "1")

  database._assert_test_database_isolated()
