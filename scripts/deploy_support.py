"""Pure deployment policy shared by deploy-prod.sh and its tests.

Keep subprocess orchestration in the shell script, but put deterministic input
normalization here so security-sensitive URL and image-reference rules have one
owner that can be exercised without Docker or a production environment.
"""

from __future__ import annotations

import argparse
import re
from urllib.parse import urlsplit

_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


class DeployInputError(ValueError):
  """A deploy setting is structurally unsafe or ambiguous."""


def normalize_gateway_origin(raw: str, shell_host: str | None = None) -> str:
  """Return one canonical HTTPS origin or raise ``DeployInputError``.

  Credentials, paths, query strings, fragments, malformed ports, and the
  shell's own host are rejected. The returned authority is lower-case and an
  explicit default port is removed, making it safe for Caddy substitution.
  """
  try:
    parsed = urlsplit(raw.strip())
    port = parsed.port
  except ValueError as exc:
    raise DeployInputError("invalid origin") from exc

  host = (parsed.hostname or "").rstrip(".").lower()
  forbidden_host = (shell_host or "").rstrip(".").lower()
  if (
    parsed.scheme != "https"
    or _HOST_RE.fullmatch(host) is None
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path not in ("", "/")
    or forbidden_host and host == forbidden_host
    or port is not None and not 1 <= port <= 65535
  ):
    raise DeployInputError("invalid origin")

  authority = host if port in (None, 443) else f"{host}:{port}"
  return f"https://{authority}"


def rollback_tag_for_image(image: str) -> str:
  """Return the stable rollback tag for a tagged or digest image reference."""
  repository = image.split("@", 1)[0]
  slash = repository.rfind("/")
  colon = repository.rfind(":")
  if colon > slash:
    repository = repository[:colon]
  if not repository:
    raise DeployInputError("invalid image reference")
  return f"{repository}:rollback-prev"


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  commands = parser.add_subparsers(dest="command", required=True)

  normalize = commands.add_parser("normalize-gateway-origin")
  normalize.add_argument("origin")
  normalize.add_argument("--shell-host")

  validate = commands.add_parser("validate-gateway-origin")
  validate.add_argument("origin")

  rollback = commands.add_parser("rollback-tag")
  rollback.add_argument("image")
  return parser


def main(argv: list[str] | None = None) -> int:
  args = _parser().parse_args(argv)
  try:
    if args.command == "normalize-gateway-origin":
      print(normalize_gateway_origin(args.origin, args.shell_host))
    elif args.command == "validate-gateway-origin":
      normalized = normalize_gateway_origin(args.origin)
      if normalized != args.origin:
        raise DeployInputError("origin is not canonical")
    else:
      print(rollback_tag_for_image(args.image))
  except DeployInputError:
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
