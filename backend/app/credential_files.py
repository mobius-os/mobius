"""File names that conventionally hold credentials.

One rule for every path that sends workspace files elsewhere (Store
publication, project copies), so the guards cannot drift apart. It judges a
single file name; callers decide which path segments to apply it to and
whether to refuse or skip. Content that the name does not reveal is the
caller's content scan's job, and no name rule catches every secret.
"""
from __future__ import annotations

# Bare "credentials" and "secrets" are file names here (for example
# ~/.aws/credentials); folders with those names are judged by their files.
_NAMES = frozenset({
  "credentials", "secrets", ".git-credentials",
  ".netrc", ".npmrc", ".pypirc", ".secret-key",
  "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
  "credentials.json", "secrets.json", "service-account.json",
  "service-token.txt",
})
# Environment files under any name, private keys (.key may be binary DER),
# and key stores and password databases a text scan cannot read (.jks,
# .keystore, .bks, .jceks, .p12, .pfx, .kdbx) or that have no PEM header
# (.ppk). .pem is text: the content scan finds a private key inside it, and a
# public certificate or CA bundle is ordinary app source.
_SUFFIXES = (
  ".env", ".key", ".ppk",
  ".jks", ".keystore", ".bks", ".jceks", ".p12", ".pfx", ".kdbx",
)


def is_credential_file(name: str) -> bool:
  """Whether one path segment is a conventional credential file name."""
  name = name.casefold()
  return name in _NAMES or name.startswith(".env.") or name.endswith(_SUFFIXES)
