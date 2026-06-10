"""Shared FastAPI auth-token dependencies.

The upload and generate routes both serve resources that browser-side
`<img>` tags / iframes fetch directly; those elements can't set
custom headers, so the token has to ride on `?token=` as a fallback.
Two route files used to declare identical `_auth_token` dependencies;
this module is the single source of truth so a future auth-flow change
needs editing in one place.
"""

from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, Query


@dataclass
class TokenSource:
  """The raw JWT and whether it arrived via query param or header.

  Media-serving routes need this to enforce the media-token restriction:
  full owner JWTs must not ride ?token= (they leak into access logs, browser
  history, and Referer headers). Only short-lived media-scoped tokens are
  accepted on query params; Authorization headers accept any valid owner token.
  """
  token: str
  from_query: bool


def get_auth_token_source(
  authorization: Optional[str] = Header(default=None),
  token: Optional[str] = Query(default=None),
) -> TokenSource:
  """Extracts the bearer token and source from the Authorization header or query.

  Used as a FastAPI dependency on media-serving routes that need to distinguish
  query-param tokens (must be short-lived media tokens) from header tokens
  (full owner JWTs are fine). Header wins when both are present.

  Raises:
    HTTPException: 401 when neither source supplies a token.
  """
  if authorization and authorization.startswith("Bearer "):
    return TokenSource(token=authorization[len("Bearer "):], from_query=False)
  if token:
    return TokenSource(token=token, from_query=True)
  raise HTTPException(status_code=401, detail="Not authenticated.")


def get_auth_token(
  authorization: Optional[str] = Header(default=None),
  token: Optional[str] = Query(default=None),
) -> str:
  """Extracts the bearer token from the Authorization header or query.

  Compatibility dependency for routes that call resolve_owner_only — they
  only need the raw string and do not distinguish query vs header.
  Header wins when both are present.

  Raises:
    HTTPException: 401 when neither source supplies a token.
  """
  if authorization and authorization.startswith("Bearer "):
    return authorization[len("Bearer "):]
  if token:
    return token
  raise HTTPException(status_code=401, detail="Not authenticated.")
