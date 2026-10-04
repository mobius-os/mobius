"""Pure deployment policy shared by deploy-prod.sh and its tests.

Keep subprocess orchestration in the shell script, but put deterministic input
normalization here so security-sensitive URL and image-reference rules have one
owner that can be exercised without Docker or a production environment.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
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


# --- Rollback floor preflight ------------------------------------------------
#
# ONE_WAY_UPGRADES_DESIGN.md, "Host rollbacks". Before an automatic rollback
# replaces a failed container, the host compares the rollback image's baked
# COMPAT_LEVEL with the database's floor, read directly and read-only. The same
# probe text is embedded in scripts/mobius-rebuild-host.py, which installs as a
# single file; a test keeps the two copies identical.

BAKED_COMPAT_PATH = "/app/platform-baked/backend/app/compat.py"
# The DATABASE_URL every Compose file pins, and app.config's default.
ROLLBACK_DATABASE = "/data/db/ultimate.db"

# Runs as `python3 -I -c FLOOR_PROBE <db>` in a one-off container with the data
# mounted read-only, after the failed container is stopped. It never imports
# application code. The last stdout line is `floor=<n>` (exit 0) or
# `error=<reason>` (exit 3): anything ambiguous fails closed.
FLOOR_PROBE = """\
import os, sqlite3, stat, sys, urllib.parse
path = sys.argv[1]
def verdict(text, code):
  print(text)
  raise SystemExit(code)
try:
  info = os.lstat(path)
except FileNotFoundError:
  verdict("error=database_missing", 3)
if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
  verdict("error=database_not_a_file", 3)
with open(path, "rb") as handle:
  if handle.read(16) != b"SQLite format 3\\x00":
    verdict("error=database_invalid", 3)
def size(suffix):
  try:
    return os.lstat(path + suffix).st_size
  except FileNotFoundError:
    return None
if size("-journal"):
  verdict("error=hot_journal", 3)
wal, shm = size("-wal"), size("-shm")
if wal is not None and shm is None:
  verdict("error=wal_without_shm", 3)
# The writer is stopped. Without a WAL the main file is complete; open it as
# immutable, because a read-only mount cannot create the -shm a WAL-mode open
# otherwise needs. With a WAL and its -shm, a plain read-only open replays it.
query = "mode=ro" if wal is not None else "mode=ro&immutable=1"
try:
  con = sqlite3.connect("file:" + urllib.parse.quote(path) + "?" + query, uri=True)
  tables = {row[0] for row in con.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table'")}
  if "platform_compat" in tables:
    row = con.execute("SELECT floor FROM platform_compat WHERE id = 1").fetchone()
    if row is None:
      verdict("error=floor_row_missing", 3)
    floor = row[0]
  elif "schema_migrations" in tables:
    floor = 0
  else:
    verdict("error=not_a_mobius_database", 3)
  # A missing or damaged floor row must not look safe: an active step's
  # level is a floor too.
  top = None
  if "platform_upgrades" in tables:
    top = con.execute(
      "SELECT MAX(level) FROM platform_upgrades WHERE state = 'active'").fetchone()[0]
  con.close()
except sqlite3.Error as exc:
  verdict("error=read_failed:" + type(exc).__name__, 3)
if type(floor) is not int or floor < 0:
  verdict("error=floor_invalid", 3)
if top is not None:
  if type(top) is not int or top < 0:
    verdict("error=floor_invalid", 3)
  floor = max(floor, top)
verdict("floor=%d" % floor, 0)
"""


def compat_level(source: str, name: str = "COMPAT_LEVEL") -> int:
  """``NAME = <int>`` from compat.py text, without importing it.

  Absent, unparsable, or non-literal counts as 0: the lowest level can only
  make the rollback check stricter. Mirrors app.compat.declared_level.
  """
  try:
    body = ast.parse(source).body
  except (SyntaxError, ValueError):
    return 0
  levels = []
  for node in body:
    if isinstance(node, ast.Assign):
      targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
      targets, value = [node.target], node.value
    else:
      continue
    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
      levels.append(value.value if isinstance(value, ast.Constant) else None)
  # Python uses the last assignment; a duplicate is ambiguous, so it counts as
  # malformed, like any other value that is not one literal integer.
  if len(levels) != 1:
    return 0
  level = levels[0]
  return level if type(level) is int and level >= 0 else 0


# --- Synthetic-turn oracle ---------------------------------------------------
#
# The deploy synthetic turn drives a real provider chat turn and must decide
# pass/fail/pending from the chat's terminal state. The 2026-09-01 incident
# (a full /data deferring admission) publishes an ERROR/RESUMABLE block and
# ends the run — which the old canary oracle, accepting "any assistant message
# with blocks", scored as PASSING. This owner rejects that false-green.

SYNTHETIC_PASSED = "passed"
SYNTHETIC_FAILED = "failed"
SYNTHETIC_PENDING = "pending"


def _is_error_block(block: dict) -> bool:
  # A pause/resumable/error block is a FAILURE signal for a synthetic turn,
  # even when it is a "benign" pause — the deploy proved the turn could not
  # produce a real answer.
  if not isinstance(block, dict):
    return False
  return bool(
    block.get("type") == "error"
    or block.get("resumable")
    or block.get("pause")
  )


def _has_text(value: object) -> bool:
  return isinstance(value, str) and bool(value.strip())


def classify_synthetic_turn(chat: dict) -> str:
  """Classify a synthetic deploy turn from the chat runtime JSON.

  ``chat`` is the parsed ``GET /api/chats/{id}`` body: ``running`` plus
  ``messages`` (each with ``role``, ``content``, optional ``blocks``).

  * ``pending`` — the run is still going; keep polling.
  * ``failed`` — no assistant reply, OR the reply is an error/resumable/pause
    block, OR it has no non-empty text. This is what the incident produces.
  * ``passed`` — a settled assistant reply with real, non-empty text and no
    error/pause marker.
  """
  if chat.get("running"):
    return SYNTHETIC_PENDING
  messages = chat.get("messages") or []
  assistant = None
  for message in messages:
    if isinstance(message, dict) and message.get("role") == "assistant":
      assistant = message
  if assistant is None:
    # Infrastructure failure must never score as a passing turn — an empty
    # assistant response is exactly the incident's "non-responsive chat".
    return SYNTHETIC_FAILED
  blocks = assistant.get("blocks") or []
  if _is_error_block(assistant) or any(_is_error_block(b) for b in blocks):
    return SYNTHETIC_FAILED
  content = assistant.get("content")
  has_text_block = any(
    isinstance(block, dict)
    and (_has_text(block.get("text")) or _has_text(block.get("content")))
    for block in blocks
  )
  if _has_text(content) or has_text_block:
    return SYNTHETIC_PASSED
  return SYNTHETIC_FAILED


# --- Durable deployment receipt ---------------------------------------------

RECEIPT_SCHEMA_VERSION = 1

# Canonical, NON-SECRET receipt fields. deploy-prod.sh assembles these from
# /api/version, /api/ready, /api/ready/agent, /api/debug/status, and the
# synthetic-turn result, then pipes them here to validate + canonicalize.
RECEIPT_FIELDS = (
  "outcome",                 # success | failed | rolled_back
  "timestamp",               # ISO-8601 UTC
  "deploy_actor",
  "build_sha",
  "platform_sha",
  "served_sha",
  "serving_source",          # platform | baked
  "image_id",
  "image_digest",
  "rollback_target",         # exact image ref to roll back to
  "migration_result",        # {ready: bool, schema_gaps: [...]}
  "data_free_bytes",
  "data_pressure_state",
  "host_root_free_bytes",
  "protected_runtime_state", # current | stale | unavailable
  "provider_auth",           # {provider: bool}  (NON-SECRET)
  "synthetic_turn",          # {provider: {result, latency_s, terminal}}
  "public_basic_health",     # HTTP status of public /api/health
  "public_agent_readiness",  # HTTP status / verdict of public /api/ready/agent
)

_RECEIPT_OUTCOMES = ("success", "failed", "rolled_back")


def build_deploy_receipt(data: dict) -> dict:
  """Validate + canonicalize a deployment receipt (success OR failure).

  A failed/rolled_back deploy MUST still record its rollback target — the whole
  point is that post-incident forensics and rollback have an authoritative
  record instead of scrollback. Unknown top-level keys are dropped so the
  receipt stays canonical. This is not a secret scrubber: the caller remains
  responsible for supplying only the documented non-secret values.
  """
  outcome = data.get("outcome")
  if outcome not in _RECEIPT_OUTCOMES:
    raise DeployInputError(
      f"receipt outcome must be one of {_RECEIPT_OUTCOMES}"
    )
  receipt: dict = {"schema_version": RECEIPT_SCHEMA_VERSION}
  for field in RECEIPT_FIELDS:
    receipt[field] = data.get(field)
  if outcome in ("failed", "rolled_back") and not receipt.get("rollback_target"):
    raise DeployInputError(
      "a failed/rolled_back receipt must record rollback_target"
    )
  return receipt


def _read_json_arg(source: str) -> dict:
  raw = sys.stdin.read() if source == "-" else source
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError as exc:
    raise DeployInputError("invalid json") from exc
  if not isinstance(parsed, dict):
    raise DeployInputError("expected a json object")
  return parsed


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

  commands.add_parser("compat-level", help="read compat.py text on stdin")
  commands.add_parser("floor-probe", help="print the rollback floor probe")

  classify = commands.add_parser("classify-turn")
  classify.add_argument("chat_json", help="chat runtime JSON, or - for stdin")

  receipt = commands.add_parser("build-receipt")
  receipt.add_argument("receipt_json", help="receipt fields JSON, or - for stdin")
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
    elif args.command == "classify-turn":
      result = classify_synthetic_turn(_read_json_arg(args.chat_json))
      print(result)
      # A settled synthetic turn that did not pass is a deploy-gate failure.
      if result == SYNTHETIC_FAILED:
        return 2
    elif args.command == "compat-level":
      # Bounded: compat.py is a few KiB; anything larger is not one.
      print(compat_level(sys.stdin.read(1 << 16)))
    elif args.command == "floor-probe":
      sys.stdout.write(FLOOR_PROBE)
    elif args.command == "build-receipt":
      receipt = build_deploy_receipt(_read_json_arg(args.receipt_json))
      print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    else:
      print(rollback_tag_for_image(args.image))
  except DeployInputError as exc:
    print(str(exc), file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
