#!/usr/bin/env python3
"""Root-owned Möbius identity and capability broker.

The broker is started by entrypoint.sh before the application drops to the
mobius UID. Its Ed25519 private key and linked identity state remain in a
root-only directory. The application can submit a one-use enrollment receipt
over a Unix socket; Codex sees only a narrow loopback Responses proxy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import socket
import socketserver
import stat
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
  Ed25519PrivateKey,
)


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PRIVATE_DIR = DATA_DIR / "identity-broker"
KEY_PATH = PRIVATE_DIR / "instance-ed25519.pem"
STATE_PATH = PRIVATE_DIR / "identity.json"
INSTANCE_PATH = PRIVATE_DIR / "instance-id"
PENDING_BOOTSTRAP_PATH = PRIVATE_DIR / "pending-enrollment.jwt"
OAUTH_STATE_PATH = PRIVATE_DIR / "oauth-states.json"
SOCKET_PATH = Path(
  os.environ.get(
    "MOBIUS_IDENTITY_BROKER_SOCKET",
    "/run/mobius-identity-broker.sock",
  )
)
TCP_HOST = "127.0.0.1"
TCP_PORT = int(os.environ.get("MOBIUS_IDENTITY_BROKER_PORT", "8765"))
IDENTITY_BASE_URL = os.environ.get(
  "MOBIUS_IDENTITY_ISSUER", "https://www.mobius.you"
).rstrip("/")
GATEWAY_BASE_URL = os.environ.get(
  # Keep the compute service independently deployable while routing it through
  # the launcher's existing public edge. A separate hostname added DNS and TLS
  # state without creating a useful trust boundary: request capabilities still
  # authenticate every exact gateway route.
  "MOBIUS_AGENT_GATEWAY_URL", "https://www.mobius.you"
).rstrip("/")
CONTRIBUTION_BASE_URL = os.environ.get(
  "MOBIUS_CONTRIBUTION_RELAY_URL", IDENTITY_BASE_URL
).rstrip("/")
COMMUNITY_BASE_URL = os.environ.get(
  "MOBIUS_COMMUNITY_REGISTRY_URL", IDENTITY_BASE_URL
).rstrip("/")
MAX_BODY = 2_000_000
MAX_INFERENCE_BODY = 16_000_000
MAX_CONTRIBUTION_BODY = 3_000_000
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
INSTANCE_RE = re.compile(r"^mob_[A-Za-z0-9_-]{3,160}$")
MANAGED_INSTANCE_RE = re.compile(r"^mob_[A-Za-z0-9_-]{3,80}$")
OAUTH_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
MANAGED_PREFIX = "/managed"
MANAGED_UPSTREAM_POLICIES = (
  ("/api/instance/v1/identity", frozenset({"GET", "POST", "PATCH"})),
  ("/api/instance/v1/railway", frozenset({"GET", "POST", "PATCH", "DELETE"})),
  ("/api/instance/v1/container-replacement", frozenset({"GET", "POST"})),
)
MANAGED_EXACT_ROUTES = frozenset({
  ("GET", "/api/instance/v1/agent"),
  ("POST", "/api/instance/v1/agent/trial"),
})
MANAGED_USER_AGENT = "mobius-managed-deployment/1"

# Monotonic marker read (without importing this module) by the frozen launcher
# and runtime provenance. Bump it whenever the served app starts depending on
# broker routes an older broker lacks, so a stale served broker is rejected in
# favour of the baked copy instead of returning 404 for the new routes.
# 1 = pre-/managed broker; 2 = /managed upstream-proxy routes present;
# 3 = standalone web search endpoint; 4 = flat gateway wire for Codex web.run.
BROKER_ROUTE_EPOCH = 4

# Declarative public forwarding policy. Callers never supply a target URL,
# audience, or arbitrary upstream path. Contribution and community routes are
# available only through the root-owned Unix socket; loopback TCP remains the
# narrow inference surface exposed to Codex.
INFERENCE_ROUTES = {
  ("GET", "/v1/models"): ("models:read", "mobius-agent-gateway", "gateway"),
  ("GET", "/v1/balance"): ("balance:read", "mobius-agent-gateway", "gateway"),
  ("POST", "/v1/responses"): (
    "inference:responses", "mobius-agent-gateway", "gateway"
  ),
}

_COMMUNITY_ROUTES = (
  ("GET", re.compile(r"/v1/community/apps"), "community:read"),
  ("GET", re.compile(r"/v1/community/publications"), "community:read"),
  (
    "GET",
    re.compile(r"/v1/community/editorial/spotlight"),
    "community:read",
  ),
  ("GET", re.compile(r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}"), "community:read"),
  (
    "GET",
    re.compile(
      r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/revisions/"
      r"[A-Za-z0-9_:-]{8,200}"
    ),
    "community:read",
  ),
  ("POST", re.compile(r"/v1/community/apps"), "community:publish"),
  (
    "POST",
    re.compile(r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/withdraw"),
    "community:publish",
  ),
  (
    "POST",
    re.compile(
      r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/revisions/"
      r"[A-Za-z0-9_:-]{8,200}/installs"
    ),
    "community:install",
  ),
  (
    "PUT",
    re.compile(r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/rating"),
    "community:rate",
  ),
  (
    "POST",
    re.compile(
      r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/revisions/"
      r"[A-Za-z0-9_:-]{8,200}/comments"
    ),
    "community:comment",
  ),
  (
    "POST",
    re.compile(r"/v1/community/editorial/assets"),
    "community:editorial",
  ),
  (
    "PUT",
    re.compile(r"/v1/community/editorial/spotlight"),
    "community:editorial",
  ),
)


def _community_scope(method: str, route_path: str, query: str) -> str | None:
  scope = next(
    (
      declared_scope
      for declared_method, pattern, declared_scope in _COMMUNITY_ROUTES
      if method == declared_method and pattern.fullmatch(route_path)
    ),
    None,
  )
  if scope is None:
    return None
  if not query:
    return scope
  if method != "GET" or route_path not in {
    "/v1/community/apps", "/v1/community/publications",
  }:
    return None
  pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
  allowed = (
    {"q", "limit", "offset"}
    if route_path == "/v1/community/apps"
    else {"limit", "offset"}
  )
  keys = [key for key, _value in pairs]
  if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
    return None
  if urllib.parse.urlencode(sorted(pairs)) != query:
    return None
  return scope


def _request_body_limit(*, is_unix: bool, method: str, path: str) -> int:
  if method == "POST" and path == "/v1/alpha/search":
    return 256_000
  if method == "POST" and path == "/v1/responses":
    # Long-context requests are expected to exceed the broker's small control-
    # plane envelope. This route is still exact, capability-bound, and local;
    # retain a finite ceiling so one client cannot force unbounded buffering.
    return MAX_INFERENCE_BODY
  if is_unix and method == "POST" and (
    path == "/v1/contributions"
    or re.fullmatch(r"/v1/contributions/ctr_[0-9a-f]{32}/withdraw", path)
  ):
    return MAX_CONTRIBUTION_BODY
  return MAX_BODY


def _public_web_url(value: str) -> str:
  """Only hand search providers public HTTP URLs, never local targets."""
  split = urllib.parse.urlsplit(value)
  host = (split.hostname or "").lower().rstrip(".")
  if split.scheme not in {"http", "https"} or not host or split.username or split.password:
    raise ValueError("search page must be a public HTTP URL")
  if host == "localhost" or host.endswith((".localhost", ".localdomain", ".local", ".internal")):
    raise ValueError("search page must be a public HTTP URL")
  try:
    address = ipaddress.ip_address(host)
  except ValueError:
    # Some URL consumers accept shortened, octal, hex, or integer IPv4 hosts.
    # Reject these alternate spellings rather than trusting different parsers
    # to agree about where a page-open request will go.
    try:
      socket.inet_aton(host)
    except OSError:
      pass
    else:
      raise ValueError("search page must be a public HTTP URL")
    address = None
  if address is not None and not address.is_global:
    raise ValueError("search page must be a public HTTP URL")
  return value


def _search_text(value: Any, *, limit: int) -> str:
  return value.strip()[:limit] if isinstance(value, str) else ""


def _search_domain_matches(url: str, domains: list[str]) -> bool:
  if not domains:
    return True
  host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
  return any(host == domain.lower() or host.endswith("." + domain.lower())
             for domain in domains)


_GATEWAY_WEB_TOOL = "mobius_web_run"


def _flatten_web_tool(body: bytes) -> tuple[bytes, bool, bool]:
  """Present Codex's web.run to the subscription gateway as a plain function.

  Codex speaks the Responses namespace extension, but the custom-model
  gateway accepts top-level functions only. Keep this at the broker's provider
  boundary; Codex and its durable thread continue to see native web.run.
  """
  try:
    request = json.loads(body)
  except ValueError:
    return body, False, False
  if not isinstance(request, dict) or not isinstance(request.get("tools"), list):
    return body, False, False
  tools = request["tools"]
  if not any(isinstance(tool, dict) and tool.get("type") == "namespace"
             and tool.get("name") == "web" for tool in tools):
    return body, False, False
  if any(isinstance(tool, dict) and tool.get("name") == _GATEWAY_WEB_TOOL
         for tool in tools):
    raise ValueError("web search tool name collides with another tool")
  flattened = []
  changed = False
  for tool in tools:
    if not isinstance(tool, dict) or tool.get("type") != "namespace" or tool.get("name") != "web":
      flattened.append(tool)
      continue
    subtools = tool.get("tools")
    if not isinstance(subtools, list) or len(subtools) != 1:
      raise ValueError("unsupported Codex web tool declaration")
    run = subtools[0]
    if not isinstance(run, dict) or run.get("type") != "function" or run.get("name") != "run":
      raise ValueError("unsupported Codex web tool declaration")
    flattened.append({
      "type": "function", "name": _GATEWAY_WEB_TOOL,
      "description": run.get("description", "Search the web."),
      "parameters": run.get("parameters", {"type": "object"}),
    })
    changed = True
  if not changed:
    return body, False, False
  request["tools"] = flattened
  # Codex sends prior calls back as conversation input on the next request.
  # The gateway must see the same flat name there as in the advertised tools;
  # otherwise GLM can receive a result for an unadvertised web.run call.
  if isinstance(request.get("input"), list):
    for item in request["input"]:
      if (isinstance(item, dict) and item.get("type") == "function_call"
          and item.get("name") == "run" and item.get("namespace") == "web"):
        item["name"] = _GATEWAY_WEB_TOOL
        del item["namespace"]
  run_candidates = []
  for tool in tools:
    if not isinstance(tool, dict):
      continue
    namespace = tool.get("name") if tool.get("type") == "namespace" else ""
    members = (tool.get("tools") or []) if namespace else [tool]
    for member in members:
      if isinstance(member, dict) and member.get("name") == "run":
        run_candidates.append(namespace)
  # A bare `run` can denote web.run only when no other advertised tool
  # could have meant it. Never guess across namespace collisions.
  bare_run_is_web = run_candidates == ["web"]
  return json.dumps(request, separators=(",", ":")).encode(), True, bare_run_is_web


def _restore_web_tool_event(line: bytes, *, bare_run_is_web: bool = False) -> bytes:
  """Restore gateway function calls to Codex's native namespace wire item."""
  if not line.startswith(b"data:") or (
    _GATEWAY_WEB_TOOL.encode() not in line
    and (not bare_run_is_web or b'"run"' not in line)
  ):
    return line
  try:
    event = json.loads(line[5:].strip())
  except ValueError:
    return line

  changed = False

  def restore(value: Any) -> None:
    nonlocal changed
    if isinstance(value, dict):
      if value.get("type") == "function_call" and (
        value.get("name") == _GATEWAY_WEB_TOOL
        or (bare_run_is_web and value.get("name") == "run"
            and value.get("namespace") in (None, "", "functions"))
      ):
        value["name"] = "run"
        value["namespace"] = "web"
        changed = True
      for child in value.values():
        restore(child)
    elif isinstance(value, list):
      for child in value:
        restore(child)

  restore(event)
  return b"data: " + json.dumps(event, separators=(",", ":")).encode() if changed else line


def _restore_web_tool_stream(chunks: Any, *, bare_run_is_web: bool = False):
  """Rewrite one SSE line at a time without buffering the inference stream."""
  pending = bytearray()
  passthrough_line = False
  max_line = 1_000_000
  for chunk in chunks:
    pending.extend(chunk)
    while (newline := pending.find(b"\n")) >= 0:
      line = bytes(pending[:newline])
      del pending[:newline + 1]
      if passthrough_line or len(line) > max_line:
        yield line + b"\n"
      else:
        yield _restore_web_tool_event(line, bare_run_is_web=bare_run_is_web) + b"\n"
      passthrough_line = False
    if len(pending) > max_line:
      # An oversized provider event is forwarded unchanged. The normal path
      # still streams each token; no response-sized buffer enters this broker.
      yield bytes(pending)
      pending.clear()
      passthrough_line = True
  if pending:
    yield (bytes(pending) if passthrough_line else
           _restore_web_tool_event(bytes(pending), bare_run_is_web=bare_run_is_web))


def _b64(value: bytes) -> str:
  return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
  return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _managed_configuration() -> tuple[str, str, str] | None:
  """Validate the credential owned exclusively by this root process."""
  issuer = os.environ.get("MOBIUS_SSO_ISSUER", "").strip().rstrip("/")
  instance_id = os.environ.get("MOBIUS_SSO_INSTANCE_ID", "").strip()
  secret = os.environ.get("MOBIUS_SSO_CLIENT_SECRET", "")
  values = (issuer, instance_id, secret)
  if not any(values):
    return None
  if not all(values):
    raise RuntimeError("managed identity configuration is incomplete")
  parsed = urllib.parse.urlsplit(issuer)
  if (
    parsed.scheme != "https"
    or not parsed.netloc
    or parsed.path
    or parsed.query
    or parsed.fragment
    or parsed.username
    or parsed.password
  ):
    raise RuntimeError("managed identity issuer must be an HTTPS origin")
  if not MANAGED_INSTANCE_RE.fullmatch(instance_id):
    raise RuntimeError("managed identity instance id is invalid")
  if len(secret) < 32:
    raise RuntimeError("managed identity credential is invalid")
  return issuer, instance_id, secret


def _managed_upstream_path(method: str, path: str) -> str | None:
  """Map only declared instance API families; callers never choose an origin."""
  if not path.startswith(MANAGED_PREFIX):
    return None
  upstream = path.removeprefix(MANAGED_PREFIX)
  if (
    any(character in upstream for character in "?#%\\")
    or "//" in upstream
    or any(segment in {".", ".."} for segment in upstream.split("/"))
  ):
    return None
  if (method, upstream) in MANAGED_EXACT_ROUTES:
    return upstream
  if any(
    method in methods
    and (upstream == prefix or upstream.startswith(prefix + "/"))
    for prefix, methods in MANAGED_UPSTREAM_POLICIES
  ):
    return upstream
  return None


def _atomic_root_write(path: Path, value: bytes) -> None:
  temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
  fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  try:
    with os.fdopen(fd, "wb") as handle:
      handle.write(value)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temp, path)
  finally:
    temp.unlink(missing_ok=True)


def _reclaim_private_state_after_compat_chown() -> None:
  """Restore root ownership after the broad /data compatibility pass.

  Older official entrypoints make a persisted /data tree app-writable before
  this root broker starts. Adopt only the broker's fixed, already-private state
  files; reject links, unexpected owners, and permissive modes rather than
  blessing an attacker-controlled path.

  Each entry is validated and fixed up through a single descriptor. A
  concurrent app-user process can still swap a directory entry, but it can no
  longer redirect the root chown/chmod onto a target this function never
  checked.
  """
  if os.geteuid() != 0:
    return

  mobius = pwd.getpwnam("mobius")
  allowed_owners = {(0, 0), (mobius.pw_uid, mobius.pw_gid)}
  try:
    PRIVATE_DIR.mkdir(mode=0o700)
  except FileExistsError:
    pass

  # O_NOFOLLOW rejects a symlink planted at the entry itself, and O_NONBLOCK
  # keeps a planted FIFO or device node from stalling the open before fstat
  # can reject it. Both make the open, not a prior lstat, the decisive check.
  open_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
  opened: list[tuple[int, int]] = []
  try:
    try:
      dir_fd = os.open(PRIVATE_DIR, open_flags | os.O_DIRECTORY)
    except OSError as exc:
      raise RuntimeError("identity broker private directory is unsafe") from exc
    opened.append((dir_fd, 0o700))
    current = os.fstat(dir_fd)
    if (
      not stat.S_ISDIR(current.st_mode)
      or (current.st_uid, current.st_gid) not in allowed_owners
      or stat.S_IMODE(current.st_mode) & 0o077
    ):
      raise RuntimeError("identity broker private directory is unsafe")

    # These are fixed children of PRIVATE_DIR, so opening them by name relative
    # to the pinned directory keeps a rename of PRIVATE_DIR itself irrelevant.
    for path in (
      KEY_PATH,
      STATE_PATH,
      INSTANCE_PATH,
      PENDING_BOOTSTRAP_PATH,
      OAUTH_STATE_PATH,
    ):
      try:
        file_fd = os.open(path.name, open_flags, dir_fd=dir_fd)
      except FileNotFoundError:
        continue
      except OSError as exc:
        raise RuntimeError(
          f"identity broker file is unsafe: {path.name}"
        ) from exc
      opened.append((file_fd, 0o600))
      item = os.fstat(file_fd)
      if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or (item.st_uid, item.st_gid) not in allowed_owners
        or stat.S_IMODE(item.st_mode) & 0o077
      ):
        raise RuntimeError(f"identity broker file is unsafe: {path.name}")

    # Nothing is mutated until every entry has passed, so a rejected tree is
    # left exactly as found.
    for fd, mode in opened:
      os.fchown(fd, 0, 0)
      os.fchmod(fd, mode)
  finally:
    for fd, _mode in opened:
      os.close(fd)


def _prepare_private_dir() -> None:
  """Create or validate the broker's non-symlink, process-owned directory."""
  try:
    current = os.lstat(PRIVATE_DIR)
  except FileNotFoundError:
    PRIVATE_DIR.mkdir(mode=0o700)
    current = os.lstat(PRIVATE_DIR)
  if (
    stat.S_ISLNK(current.st_mode)
    or not stat.S_ISDIR(current.st_mode)
    or current.st_uid != os.geteuid()
  ):
    raise RuntimeError("identity broker private directory is unsafe")
  if current.st_gid != os.getegid():
    os.chown(PRIVATE_DIR, os.geteuid(), os.getegid())
  os.chmod(PRIVATE_DIR, 0o700)


def _private_file_exists(path: Path) -> bool:
  """Return whether a root-only regular broker file exists, failing closed."""
  try:
    current = os.lstat(path)
  except FileNotFoundError:
    return False
  if (
    stat.S_ISLNK(current.st_mode)
    or not stat.S_ISREG(current.st_mode)
    or current.st_uid != os.geteuid()
    or stat.S_IMODE(current.st_mode) & 0o077
  ):
    raise RuntimeError(f"identity broker file is unsafe: {path.name}")
  return True


def _prepare_socket_dir() -> None:
  """Require a root-owned parent so the unprivileged app cannot swap the UDS."""
  parent = SOCKET_PATH.parent
  current = os.lstat(parent)
  if (
    stat.S_ISLNK(current.st_mode)
    or not stat.S_ISDIR(current.st_mode)
    or current.st_uid != os.geteuid()
    or stat.S_IMODE(current.st_mode) & 0o022
  ):
    raise RuntimeError("identity broker socket directory is unsafe")


def _load_or_create_key() -> Ed25519PrivateKey:
  _prepare_private_dir()
  if _private_file_exists(KEY_PATH):
    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
      raise RuntimeError("identity broker key is not Ed25519")
    return key
  key = Ed25519PrivateKey.generate()
  encoded = key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
  )
  _atomic_root_write(KEY_PATH, encoded)
  return key


def _load_or_create_instance_id() -> str:
  configured = os.environ.get("MOBIUS_SSO_INSTANCE_ID", "").strip()
  if configured and INSTANCE_RE.fullmatch(configured):
    value = configured
  elif _private_file_exists(INSTANCE_PATH):
    value = INSTANCE_PATH.read_text(encoding="utf-8").strip()
  else:
    value = "mob_self_" + secrets.token_urlsafe(18).replace("-", "_")
  if not INSTANCE_RE.fullmatch(value):
    raise RuntimeError("invalid identity broker instance id")
  if not _private_file_exists(INSTANCE_PATH):
    _atomic_root_write(INSTANCE_PATH, (value + "\n").encode())
  return value


class Broker:
  def __init__(self) -> None:
    self.managed_credentials = _managed_configuration()
    os.environ.pop("MOBIUS_SSO_CLIENT_SECRET", None)
    self.key = _load_or_create_key()
    self.instance_id = _load_or_create_instance_id()
    self.lock = threading.RLock()
    self.client = httpx.Client(timeout=30.0, follow_redirects=False)
    # Codex's opaque ref_ids are local to one search session, not a durable index.
    self.search_sessions: dict[str, dict[str, Any]] = {}
    self.state = self._load_state()

  def close(self) -> None:
    self.client.close()

  def _parallel(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call the anonymous Parallel Search MCP behind Codex's native web.run."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def invoke() -> dict[str, Any]:
      async with streamablehttp_client(
        "https://search.parallel.ai/mcp", timeout=20, sse_read_timeout=20,
      ) as (read, write, _):
        async with ClientSession(read, write) as session:
          await session.initialize()
          response = await session.call_tool(tool, arguments)
          if response.isError:
            detail = " ".join(part.text for part in response.content if part.type == "text")
            raise ValueError(f"Parallel search failed: {detail[:500]}")
          for part in response.content:
            if part.type == "text":
              value = json.loads(part.text)
              if isinstance(value, dict) and isinstance(value.get("results"), list):
                if not value["results"] and value.get("errors"):
                  raise ValueError(f"Parallel search failed: {str(value['errors'])[:500]}")
                return value
          raise ValueError("Parallel search returned no results list")

    return asyncio.run(invoke())

  @staticmethod
  def _search_failure(error: Exception) -> str:
    def rate_limited(value: BaseException) -> bool:
      if isinstance(value, httpx.HTTPStatusError) and value.response.status_code == 429:
        return True
      if "rate limit" in str(value).lower() or "429" in str(value):
        return True
      return any(rate_limited(child) for child in getattr(value, "exceptions", ()))

    if rate_limited(error):
      return (
        "Live web search is temporarily rate-limited. Continue without claiming "
        "current facts were verified; tell the user the limit was reached."
      )
    return (
      "Live web search is temporarily unavailable. Continue without claiming "
      "current facts were verified; tell the user search was unavailable."
    )

  def standalone_search(self, request: dict[str, Any]) -> dict[str, Any]:
    """Serve native Codex web.run with one keyless search provider."""
    commands = request.get("commands") or {}
    if not isinstance(commands, dict):
      raise ValueError("search commands must be an object")
    session_id = _search_text(request.get("id"), limit=200)
    if not session_id:
      raise ValueError("search session id is required")
    provider_session = hashlib.sha256(session_id.encode()).hexdigest()
    unsupported = [
      key for key in ("click", "screenshot", "finance", "weather", "sports", "time")
      if commands.get(key)
    ]
    lines: list[str] = []
    results: list[dict[str, str]] = []
    if unsupported:
      lines.append("Unsupported web search operation(s): " + ", ".join(unsupported) + ".")
    budgets = {"short": 4000, "medium": 8000, "long": 16000}
    budget = budgets.get(commands.get("response_length"), 8000)
    with self.lock:
      now = time.monotonic()
      self.search_sessions = {
        key: value for key, value in self.search_sessions.items()
        if now - value["touched"] < 1800
      }
      if len(self.search_sessions) >= 100 and session_id not in self.search_sessions:
        oldest = min(self.search_sessions, key=lambda key: self.search_sessions[key]["touched"])
        self.search_sessions.pop(oldest, None)
      session = self.search_sessions.setdefault(
        session_id, {"touched": now, "turn": 0, "refs": {}}
      )
      session["touched"] = now
      turn = session["turn"]
      session["turn"] += 1

    for kind, source in (("search_query", "web"), ("image_query", "images")):
      queries = commands.get(kind) or []
      if not isinstance(queries, list) or len(queries) > 4:
        raise ValueError(f"{kind} must contain at most four queries")
      for query in queries:
        if not isinstance(query, dict) or not _search_text(query.get("q"), limit=500):
          raise ValueError("search query text is required")
        search_text = _search_text(query["q"], limit=500)
        domains = query.get("domains") or []
        if domains:
          if not isinstance(domains, list) or len(domains) > 8 or any(
            not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", domain)
            for domain in domains
          ):
            raise ValueError("invalid search domains")
        recency = query.get("recency")
        since = None
        if source == "web" and type(recency) is int and 0 < recency <= 3650:
          since = datetime.now(timezone.utc) - timedelta(days=recency)
        if source == "images":
          hits = []
          search_available = False
          lines.append("Image search is not supported by this provider; continue without images.")
        else:
          objective = "Find reliable, current sources for " + search_text
          if domains:
            objective += "; only these domains: " + ", ".join(domains)
          if since:
            objective += f"; published since {since:%Y-%m-%d}"
            lines.append("Freshness filtering is advisory; verify publication dates.")
          try:
            value = self._parallel("web_search", {
              "objective": objective[:1000], "search_queries": [search_text],
              "session_id": provider_session,
            })
            hits = value.get("results") or []
            search_available = True
          # MCP transports can surface rate limits and outages as ExceptionGroups.
          # Keep that external failure within the tool result, not the agent turn.
          except Exception as error:
            hits = []
            search_available = False
            lines.append(self._search_failure(error))
        if not isinstance(hits, list):
          hits = []
        lines.append(f"Search results for {query['q']}:")
        result_count = len(results)
        for hit in hits:
          if len(results) - result_count >= 5:
            break
          if not isinstance(hit, dict):
            continue
          url = _search_text(hit.get("url"), limit=2000)
          try:
            _public_web_url(url)
          except ValueError:
            continue
          if not _search_domain_matches(url, domains):
            continue
          with self.lock:
            refs = session["refs"]
            ref_id = f"turn{turn}search{len(refs)}"
            if len(refs) < 100:
              refs[ref_id] = url
            else:
              ref_id = url
          title = _search_text(hit.get("title"), limit=200) or url
          excerpts = hit.get("excerpts") or []
          snippet = _search_text(excerpts[0] if isinstance(excerpts, list) and excerpts else "", limit=700)
          results.append({
            "type": "text_result", "ref_id": ref_id, "url": url,
            "title": title, "snippet": snippet,
          })
          lines.append(f"【{ref_id}】 {title}\n{url}\n{snippet}")
        if search_available and len(results) == result_count:
          lines.append("No results.")

    for kind in ("open", "find"):
      operations = commands.get(kind) or []
      if not isinstance(operations, list) or len(operations) > 4:
        raise ValueError(f"{kind} must contain at most four pages")
      for operation in operations:
        if not isinstance(operation, dict):
          raise ValueError("page operation must be an object")
        ref_id = _search_text(operation.get("ref_id"), limit=2000)
        pattern = _search_text(operation.get("pattern"), limit=200) if kind == "find" else ""
        if kind == "find" and not pattern:
          raise ValueError("find pattern is required")
        with self.lock:
          url = session["refs"].get(ref_id)
        if url is None and not urllib.parse.urlsplit(ref_id).scheme:
          lines.append("Page reference unavailable; search again or open a public URL.")
          continue
        url = _public_web_url(url or ref_id)
        try:
          value = self._parallel("web_fetch", {
            "urls": [url], "objective": "Read this page", "full_content": True,
            "session_id": provider_session,
          })
          pages = value.get("results") or []
          page = pages[0] if pages and isinstance(pages[0], dict) else {}
          excerpts = page.get("excerpts") or []
          markdown = _search_text(
            page.get("full_content") or "\n".join(
              excerpt for excerpt in excerpts if isinstance(excerpt, str)
            ), limit=30_000,
          )
          note = ""
        except Exception as error:
          markdown = ""
          note = self._search_failure(error)
        if kind == "find":
          position = markdown.lower().find(pattern.lower())
          content = (
            markdown[max(0, position - 300):position + len(pattern) + 900]
            if position >= 0 else (
              "Pattern not found on page." if markdown else "Page text unavailable."
            )
          )
        else:
          content = markdown[:8000] or "No page text returned."
        lines.append(f"【{ref_id}】 {url}\n{note}\n{content}" if note else f"【{ref_id}】 {url}\n{content}")
        if markdown:
          results.append({"type": "text_result", "ref_id": ref_id, "url": url})

    return {"output": ("\n\n".join(lines) or "No search command was provided.")[:budget],
            "results": results}

  def public_jwk(self) -> dict[str, str]:
    raw = self.key.public_key().public_bytes(
      serialization.Encoding.Raw,
      serialization.PublicFormat.Raw,
    )
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64(raw)}

  def thumbprint(self) -> str:
    canonical = json.dumps(
      self.public_jwk(), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()

  def _load_state(self) -> dict[str, Any] | None:
    if not _private_file_exists(STATE_PATH):
      return None
    try:
      value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except ValueError:
      return None
    if (
      not isinstance(value, dict)
      or value.get("instance_id") != self.instance_id
      or value.get("key_thumbprint") != self.thumbprint()
    ):
      return None
    return value

  def identity(self) -> dict[str, Any]:
    with self.lock:
      state = dict(self.state or {})
    return {
      "linked": bool(state),
      "issuer": state.get("issuer"),
      "subject": state.get("subject"),
      "instance_id": self.instance_id,
      "key_generation": int(state.get("key_generation") or 1),
      "public_key_jwk": self.public_jwk(),
      "key_thumbprint": self.thumbprint(),
    }

  def _sign(self, claims: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
      claims, sort_keys=True, separators=(",", ":")
    ).encode()
    return {"claims": claims, "signature": _b64(self.key.sign(encoded))}

  @staticmethod
  def _receipt_claims(receipt: str) -> dict[str, Any]:
    parts = receipt.split(".")
    if len(parts) != 3:
      raise ValueError("invalid enrollment receipt")
    value = json.loads(_unb64(parts[1]))
    if not isinstance(value, dict):
      raise ValueError("invalid enrollment receipt")
    return value

  def enroll(self, receipt: str) -> dict[str, Any]:
    receipt_claims = self._receipt_claims(receipt)
    if receipt_claims.get("instance_id") != self.instance_id:
      raise ValueError("enrollment receipt belongs to another instance")
    now = int(time.time())
    assertion = self._sign({
      "purpose": "identity:enroll",
      "instance_id": self.instance_id,
      "key_thumbprint": self.thumbprint(),
      "receipt_jti": receipt_claims.get("jti"),
      "jti": secrets.token_urlsafe(24),
      "iat": now,
      "exp": now + 60,
    })
    response = self.client.post(
      f"{IDENTITY_BASE_URL}/identity/runtime-enroll",
      json={
        "receipt": receipt,
        "public_key_jwk": self.public_jwk(),
        "assertion": assertion,
        "idempotency_key": f"enroll:{receipt_claims.get('jti')}",
        "audit_context": {"source": "runtime-broker"},
      },
      headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    value = response.json()
    identity = value.get("identity") if isinstance(value, dict) else None
    if (
      not isinstance(identity, dict)
      or identity.get("instance_id") != self.instance_id
      or identity.get("key_thumbprint") != self.thumbprint()
    ):
      raise ValueError("identity service returned an invalid enrollment")
    state = {
      "issuer": identity["issuer"],
      "subject": identity["subject"],
      "instance_id": self.instance_id,
      "key_thumbprint": self.thumbprint(),
      "key_generation": int(identity.get("key_generation") or 1),
      "linked_at": int(time.time()),
    }
    _atomic_root_write(
      STATE_PATH,
      json.dumps(state, sort_keys=True, separators=(",", ":")).encode(),
    )
    with self.lock:
      self.state = state
    return self.identity()

  def unlink(self, expected_subject: str) -> dict[str, Any]:
    """Remove only the currently linked account while retaining instance keys."""
    if not expected_subject:
      raise ValueError("expected subject is required")
    with self.lock:
      state = dict(self.state or {})
      if state and state.get("subject") != expected_subject:
        raise PermissionError("linked identity does not match")
      STATE_PATH.unlink(missing_ok=True)
      PENDING_BOOTSTRAP_PATH.unlink(missing_ok=True)
      OAUTH_STATE_PATH.unlink(missing_ok=True)
      self.state = None
    return self.identity()

  def queue_bootstrap(self, receipt: str) -> None:
    """Persist an unconsumed enrollment receipt across runtime restarts.

    The parent entrypoint deliberately removes the receipt from the app's
    environment. Keeping the still-unused value here, under the same root-only
    boundary as the private key, prevents a transient central outage during
    first boot from permanently losing the Railway trial claim.
    """
    claims = self._receipt_claims(receipt)
    if (
      claims.get("instance_id") != self.instance_id
      or not isinstance(claims.get("exp"), int)
      or claims["exp"] <= int(time.time())
    ):
      raise ValueError("invalid or expired enrollment receipt")
    _atomic_root_write(PENDING_BOOTSTRAP_PATH, receipt.encode("ascii"))

  def retry_pending_once(self) -> bool:
    """Attempt one pending enrollment; delete only after success or expiry."""
    if self.state:
      PENDING_BOOTSTRAP_PATH.unlink(missing_ok=True)
      return True
    if not _private_file_exists(PENDING_BOOTSTRAP_PATH):
      return False
    try:
      receipt = PENDING_BOOTSTRAP_PATH.read_text(encoding="ascii").strip()
      claims = self._receipt_claims(receipt)
    except (ValueError, UnicodeError):
      return False
    if claims.get("instance_id") != self.instance_id:
      return False
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= int(time.time()):
      PENDING_BOOTSTRAP_PATH.unlink(missing_ok=True)
      return False
    try:
      self.enroll(receipt)
    except Exception:
      return False
    PENDING_BOOTSTRAP_PATH.unlink(missing_ok=True)
    return True

  def retry_pending_loop(self, stop: threading.Event) -> None:
    delay = 1.0
    while (
      not stop.is_set()
      and _private_file_exists(PENDING_BOOTSTRAP_PATH)
      and not self.state
    ):
      if self.retry_pending_once():
        return
      stop.wait(delay)
      delay = min(60.0, delay * 2)

  def save_oauth_state(self, value: dict[str, Any]) -> None:
    required = {
      "state", "owner", "verifier", "instance_id", "public_key_jwk",
      "redirect_uri", "expires_at",
    }
    allowed = required | {"select_account"}
    if (
      not required.issubset(value)
      or not set(value).issubset(allowed)
      or (
        "select_account" in value
        and not isinstance(value["select_account"], bool)
      )
      or not OAUTH_STATE_RE.fullmatch(str(value.get("state") or ""))
      or value.get("instance_id") != self.instance_id
      or not isinstance(value.get("expires_at"), (int, float))
      or value["expires_at"] <= time.time()
      or value["expires_at"] - time.time() > 600
      or not isinstance(value.get("public_key_jwk"), dict)
      or value["public_key_jwk"] != self.public_jwk()
    ):
      raise ValueError("invalid OAuth state")
    with self.lock:
      states = self._oauth_states()
      states[value["state"]] = value
      _atomic_root_write(
        OAUTH_STATE_PATH,
        json.dumps(states, sort_keys=True, separators=(",", ":")).encode(),
      )

  def consume_oauth_state(self, state: str) -> dict[str, Any] | None:
    if not OAUTH_STATE_RE.fullmatch(state):
      return None
    with self.lock:
      states = self._oauth_states()
      value = states.pop(state, None)
      _atomic_root_write(
        OAUTH_STATE_PATH,
        json.dumps(states, sort_keys=True, separators=(",", ":")).encode(),
      )
    if not isinstance(value, dict) or value.get("expires_at", 0) <= time.time():
      return None
    return value

  def _oauth_states(self) -> dict[str, dict[str, Any]]:
    if not _private_file_exists(OAUTH_STATE_PATH):
      return {}
    try:
      value = json.loads(OAUTH_STATE_PATH.read_text(encoding="utf-8"))
    except ValueError:
      return {}
    if not isinstance(value, dict):
      return {}
    now = time.time()
    return {
      key: item for key, item in value.items()
      if OAUTH_STATE_RE.fullmatch(key)
      and isinstance(item, dict)
      and isinstance(item.get("expires_at"), (int, float))
      and item["expires_at"] > now
    }

  def _request_id(
    self, method: str, path: str, body: bytes, headers: dict[str, str]
  ) -> str:
    supplied = headers.get("x-mobius-request-id", "")
    if REQUEST_ID_RE.fullmatch(supplied):
      return supplied
    metadata = headers.get("x-codex-turn-metadata", "").encode()
    if method == "POST" and path == "/v1/responses" and not metadata:
      raise ValueError("a Codex turn id or X-Mobius-Request-Id is required")
    material = b"mobius-broker-v1\0" + method.encode() + b"\0" + path.encode()
    material += b"\0" + metadata + b"\0" + body
    return "broker:" + hashlib.sha256(material).hexdigest()

  def _capability(
    self,
    *,
    audience: str,
    scope: str,
    method: str,
    path: str,
    body: bytes,
    request_id: str,
    idempotency_key: str = "",
  ) -> str:
    with self.lock:
      state = dict(self.state or {})
    if not state:
      raise PermissionError("a mobius.you account must be linked")
    now = int(time.time())
    claims = {
      "identity_issuer": state["issuer"],
      "sub": state["subject"],
      "instance_id": self.instance_id,
      "key_generation": int(state["key_generation"]),
      "key_thumbprint": self.thumbprint(),
      "aud": audience,
      "scope": scope,
      "method": method,
      "path": path,
      "body_sha256": hashlib.sha256(body).hexdigest(),
      "request_id": request_id,
      "jti": secrets.token_urlsafe(24),
      "iat": now,
      "exp": now + 60,
      "audit_context": {"source": "runtime-broker"},
    }
    if idempotency_key:
      claims["idempotency_key_sha256"] = hashlib.sha256(
        idempotency_key.encode("utf-8")
      ).hexdigest()
    response = self.client.post(
      f"{IDENTITY_BASE_URL}/identity/capabilities",
      json={"assertion": self._sign(claims)},
      headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    value = response.json()
    token = value.get("capability") if isinstance(value, dict) else None
    if not isinstance(token, str) or token.count(".") != 2:
      raise ValueError("identity service returned an invalid capability")
    return token

  def proxy(
    self,
    *,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    allow_private_routes: bool,
  ) -> httpx.Response:
    split = urllib.parse.urlsplit(path)
    route_path = split.path
    if not route_path.startswith("/") or split.fragment:
      raise FileNotFoundError("broker route not found")
    managed_path = _managed_upstream_path(method, path) if allow_private_routes else None
    if managed_path is not None:
      if self.managed_credentials is None:
        raise PermissionError("managed deployment is not configured")
      issuer, instance_id, secret = self.managed_credentials
      forwarded = {
        "Authorization": f"Bearer {secret}",
        "X-Mobius-Instance-Id": instance_id,
        "Accept": headers.get("accept", "application/json"),
        "Accept-Encoding": "identity",
        "User-Agent": MANAGED_USER_AGENT,
      }
      content_type = headers.get("content-type")
      if content_type:
        forwarded["Content-Type"] = content_type
      request = self.client.build_request(
        method,
        issuer + managed_path,
        content=body if body else None,
        headers=forwarded,
        timeout=30.0,
      )
      return self.client.send(request, stream=True)
    declared = INFERENCE_ROUTES.get((method, route_path)) if not split.query else None
    route = None
    if declared is not None:
      scope, audience, target_name = declared
      route = (
        scope,
        GATEWAY_BASE_URL if target_name == "gateway" else "",
        audience,
      )
    if route is None and allow_private_routes:
      if method == "POST" and route_path == "/v1/contributions" and not split.query:
        route = (
          "contribution:submit", CONTRIBUTION_BASE_URL,
          "mobius-contribution-relay",
        )
      elif method == "POST" and re.fullmatch(
        r"/v1/contributions/ctr_[0-9a-f]{32}/withdraw", route_path
      ) and not split.query:
        route = (
          "contribution:withdraw", CONTRIBUTION_BASE_URL,
          "mobius-contribution-relay",
        )
      elif method == "GET" and re.fullmatch(
        r"/v1/contributions/ctr_[0-9a-f]{32}", route_path
      ) and not split.query:
        route = (
          "contribution:read", CONTRIBUTION_BASE_URL,
          "mobius-contribution-relay",
        )
      else:
        community = _community_scope(method, route_path, split.query)
        if community is not None:
          route = (community, COMMUNITY_BASE_URL, "mobius-community-registry")
    if route is None:
      raise FileNotFoundError("broker route not found")
    scope, target, audience = route
    request_id = self._request_id(method, path, body, headers)
    idempotency_key = headers.get("idempotency-key", "")
    if idempotency_key and not re.fullmatch(
      r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", idempotency_key
    ):
      raise ValueError("invalid idempotency key")
    if method != "GET" and audience in {
      "mobius-contribution-relay", "mobius-community-registry"
    } and not idempotency_key:
      raise ValueError("an idempotency key is required")
    capability = self._capability(
      audience=audience,
      scope=scope,
      method=method,
      path=path,
      body=body,
      request_id=request_id,
      idempotency_key=idempotency_key,
    )
    forwarded = {
      "Authorization": f"Bearer {capability}",
      "Accept": headers.get("accept", "*/*"),
      "Accept-Encoding": "identity",
      "Content-Type": headers.get("content-type", "application/json"),
      "X-Mobius-Request-Id": request_id,
    }
    metadata = headers.get("x-codex-turn-metadata")
    if metadata:
      forwarded["x-codex-turn-metadata"] = metadata
    if idempotency_key:
      forwarded["Idempotency-Key"] = idempotency_key
    request = self.client.build_request(
      method,
      target + path,
      content=body if body else None,
      headers=forwarded,
      timeout=None if path == "/v1/responses" else 30.0,
    )
    # Do not buffer Responses API streams in the privileged broker. Besides
    # preserving token-by-token UX, this bounds the broker's memory footprint
    # for a response controlled by the upstream provider.
    return self.client.send(request, stream=True)


class _Handler(BaseHTTPRequestHandler):
  # Closing the connection delimits streamed bodies without requiring this
  # tiny broker to implement HTTP/1.1 chunk framing itself.
  protocol_version = "HTTP/1.0"
  server_version = "MobiusIdentityBroker/1"

  def log_message(self, _format: str, *_args: object) -> None:
    return

  def _json(self, status: int, value: dict[str, Any]) -> None:
    body = json.dumps(value, separators=(",", ":")).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def _body(self, *, maximum: int = MAX_BODY) -> bytes:
    try:
      length = int(self.headers.get("content-length", "0"))
    except ValueError as exc:
      raise ValueError("invalid content length") from exc
    if length < 0 or length > maximum:
      raise ValueError("request body is too large")
    return self.rfile.read(length)

  def _handle(self) -> None:
    broker: Broker = self.server.broker  # type: ignore[attr-defined]
    method = self.command.upper()
    path = self.path
    route_path = path.split("?", 1)[0]
    is_unix = bool(getattr(self.server, "is_unix", False))
    body_limit = _request_body_limit(is_unix=is_unix, method=method, path=path)
    try:
      if is_unix and route_path.startswith("/identity") and path != route_path:
        raise FileNotFoundError("broker route not found")
      if is_unix and method == "GET" and path == "/identity":
        self._json(200, broker.identity())
        return
      if is_unix and method == "POST" and path == "/identity/enroll":
        value = json.loads(self._body())
        if not isinstance(value, dict) or not isinstance(value.get("receipt"), str):
          raise ValueError("receipt is required")
        self._json(200, broker.enroll(value["receipt"]))
        return
      if is_unix and method == "POST" and path == "/identity/unlink":
        value = json.loads(self._body())
        subject = value.get("expected_subject") if isinstance(value, dict) else None
        self._json(200, broker.unlink(str(subject or "")))
        return
      if is_unix and method == "POST" and path == "/identity/oauth/start":
        value = json.loads(self._body())
        if not isinstance(value, dict):
          raise ValueError("OAuth state is required")
        broker.save_oauth_state(value)
        self._json(200, {"saved": True})
        return
      if is_unix and method == "POST" and path == "/identity/oauth/consume":
        value = json.loads(self._body())
        state = value.get("state") if isinstance(value, dict) else None
        pending = broker.consume_oauth_state(str(state or ""))
        self._json(200, {"pending": pending})
        return
      if method == "POST" and path == "/v1/alpha/search":
        request = json.loads(self._body(maximum=body_limit))
        if not isinstance(request, dict):
          raise ValueError("search request must be an object")
        self._json(200, broker.standalone_search(request))
        return
      body = self._body(maximum=body_limit)
      web_tool_flattened = False
      bare_run_is_web = False
      if method == "POST" and path == "/v1/responses":
        body, web_tool_flattened, bare_run_is_web = _flatten_web_tool(body)
      incoming = {key.lower(): value for key, value in self.headers.items()}
      upstream = broker.proxy(
        method=method,
        path=path,
        body=body,
        headers=incoming,
        allow_private_routes=is_unix,
      )
      try:
        self.send_response(upstream.status_code)
        excluded = {
          "connection", "content-length", "content-encoding", "transfer-encoding"
        }
        for key, value in upstream.headers.items():
          if key.lower() not in excluded:
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = upstream.iter_raw()
        if (web_tool_flattened and upstream.status_code == 200
            and upstream.headers.get("content-type", "").startswith("text/event-stream")):
          chunks = _restore_web_tool_stream(chunks, bare_run_is_web=bare_run_is_web)
        for chunk in chunks:
          if chunk:
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True
      finally:
        upstream.close()
    except FileNotFoundError:
      self._json(404, {"error": "broker route not found"})
    except PermissionError as exc:
      self._json(401, {"error": str(exc)})
    except ValueError as exc:
      self._json(400, {"error": str(exc)})
    except httpx.HTTPStatusError as exc:
      status = exc.response.status_code
      if route_path == "/v1/alpha/search":
        message = (
          "web search rate limit reached; try again later"
          if status == 429 else "web search service failed"
        )
        self._json(429 if status == 429 else 502, {"error": message, "status": status})
      else:
        self._json(502, {"error": "central identity request failed", "status": status})
    except httpx.HTTPError:
      self._json(502, {"error": (
        "web search service unavailable" if route_path == "/v1/alpha/search"
        else "central service unavailable"
      )})

  do_GET = _handle
  do_POST = _handle
  do_PUT = _handle
  do_PATCH = _handle
  do_DELETE = _handle


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
  daemon_threads = True
  allow_reuse_address = True


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
  daemon_threads = True
  allow_reuse_address = True


def main() -> None:
  if os.geteuid() != 0:
    raise SystemExit("identity broker must run as root")
  _reclaim_private_state_after_compat_chown()
  broker = Broker()
  _prepare_socket_dir()
  SOCKET_PATH.unlink(missing_ok=True)
  unix = _UnixServer(str(SOCKET_PATH), _Handler)
  unix.broker = broker  # type: ignore[attr-defined]
  unix.is_unix = True  # type: ignore[attr-defined]
  try:
    import grp
    os.chown(SOCKET_PATH, 0, grp.getgrnam("mobius").gr_gid)
  except (KeyError, PermissionError):
    os.chown(SOCKET_PATH, 0, 0)
  os.chmod(SOCKET_PATH, 0o660)
  tcp = _TcpServer((TCP_HOST, TCP_PORT), _Handler)
  tcp.broker = broker  # type: ignore[attr-defined]
  tcp.is_unix = False  # type: ignore[attr-defined]
  threads = [
    threading.Thread(target=unix.serve_forever, daemon=True),
    threading.Thread(target=tcp.serve_forever, daemon=True),
  ]
  for thread in threads:
    thread.start()
  bootstrap = os.environ.pop("MOBIUS_IDENTITY_BOOTSTRAP", "").strip()
  if bootstrap and not broker.state:
    try:
      broker.queue_bootstrap(bootstrap)
    except Exception as exc:
      print(f"identity broker bootstrap rejected: {type(exc).__name__}", flush=True)
  retry_stop = threading.Event()
  retry_thread = threading.Thread(
    target=broker.retry_pending_loop, args=(retry_stop,), daemon=True
  )
  retry_thread.start()
  try:
    for thread in threads:
      thread.join()
  finally:
    retry_stop.set()
    retry_thread.join(timeout=2)
    unix.server_close()
    tcp.server_close()
    broker.close()
    SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
  main()
