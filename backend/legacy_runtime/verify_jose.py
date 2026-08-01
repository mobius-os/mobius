"""Build-time smoke test for the persistent-checkout jose facade."""

from jose import JWTError, jwt


key = "s" * 32
token = jwt.encode(
  {"scope": "mobius_sso_state"}, key, algorithm="HS256"
)
assert jwt.decode(token, key, algorithms=["HS256"])["scope"] == (
  "mobius_sso_state"
)

try:
  jwt.decode(token, "x" * 32, algorithms=["HS256"])
except JWTError:
  pass
else:
  raise AssertionError("JWTError did not catch an invalid signature")
