"""Unit tests for `app.auth_helpers.get_auth_token`.

The helper is a FastAPI dependency, but its body is pure — call it
directly with positional/keyword args to exercise each branch (header
present, query present, both missing).
"""

import pytest
from fastapi import HTTPException

from app.auth_helpers import get_auth_token


def test_extracts_token_from_authorization_header():
  """`Authorization: Bearer <token>` header returns the bare token."""
  assert get_auth_token(authorization="Bearer abc.def.ghi", token=None) == "abc.def.ghi"


def test_extracts_token_from_query_when_header_absent():
  """Query param `?token=` is the fallback for browser elements that
  cannot set headers (img tags, iframes)."""
  assert get_auth_token(authorization=None, token="xyz.token") == "xyz.token"


def test_header_wins_when_both_supplied():
  """When both are present the header takes precedence — callers
  should not rely on the query param overriding the header."""
  result = get_auth_token(
    authorization="Bearer from-header",
    token="from-query",
  )
  assert result == "from-header"


def test_raises_401_when_neither_supplied():
  """Both sources missing raises HTTPException(401)."""
  with pytest.raises(HTTPException) as exc:
    get_auth_token(authorization=None, token=None)
  assert exc.value.status_code == 401


def test_raises_401_on_non_bearer_authorization_header():
  """An Authorization header without the `Bearer ` prefix and no
  query token also raises 401 — the helper does not silently accept
  arbitrary header values."""
  with pytest.raises(HTTPException) as exc:
    get_auth_token(authorization="Basic dXNlcjpwYXNz", token=None)
  assert exc.value.status_code == 401


def test_falls_back_to_query_when_authorization_header_is_not_bearer():
  """A non-bearer header is ignored; if the query has a token, return
  it. Confirms the helper's branching order matches the original
  duplicated implementations."""
  result = get_auth_token(authorization="Basic xxx", token="t.t.t")
  assert result == "t.t.t"
