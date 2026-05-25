"""Shared FastAPI auth-token dependencies.

The upload and generate routes both serve resources that browser-side
`<img>` tags / iframes fetch directly; those elements can't set
custom headers, so the token has to ride on `?token=` as a fallback.
Two route files used to declare identical `_auth_token` dependencies;
this module is the single source of truth so a future auth-flow change
needs editing in one place.
"""

from typing import Optional

from fastapi import Header, HTTPException, Query


def get_auth_token(
  authorization: Optional[str] = Header(default=None),
  token: Optional[str] = Query(default=None),
) -> str:
  """Extracts the bearer token from the Authorization header or query.

  Used as a FastAPI dependency on routes that browser elements without
  header support (image tags, iframes) need to fetch. Header wins
  when both are present.

  Args:
    authorization: Optional `Authorization: Bearer <token>` header.
    token: Optional `?token=<token>` query parameter.

  Returns:
    The raw JWT string.

  Raises:
    HTTPException: 401 when neither source supplies a token.
  """
  if authorization and authorization.startswith("Bearer "):
    return authorization[len("Bearer "):]
  if token:
    return token
  raise HTTPException(status_code=401, detail="Not authenticated.")
