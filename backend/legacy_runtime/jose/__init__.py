"""PyJWT-backed compatibility for persistent pre-migration checkouts.

Some Railway volumes retain a platform checkout whose auth module imports
``JWTError`` and ``jwt`` from python-jose. The current image uses PyJWT, whose
HS256 API is compatible with that historical call site. Keep this facade
limited to the two names that checkout imports instead of restoring the
broader, vulnerable dependency tree.
"""

import jwt
from jwt.exceptions import InvalidTokenError


JWTError = InvalidTokenError

__all__ = ["JWTError", "jwt"]
