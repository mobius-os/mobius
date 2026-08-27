#!/usr/bin/env python3
"""Root-owned Möbius identity and capability broker.

The broker is started by entrypoint.sh before the application drops to the
mobius UID. Its Ed25519 private key and linked identity state remain in a
root-only directory. The application can submit a one-use enrollment receipt
over a Unix socket; Codex sees only a narrow loopback Responses proxy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socketserver
import stat
import threading
import time
import urllib.parse
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
COMMUNITY_BASE_URL = os.environ.get(
  "MOBIUS_COMMUNITY_REGISTRY_URL", IDENTITY_BASE_URL
).rstrip("/")
MAX_BODY = 2_000_000
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
INSTANCE_RE = re.compile(r"^mob_[A-Za-z0-9_-]{3,160}$")

# Declarative inference forwarding policy. Callers never supply a target URL,
# audience, or arbitrary upstream path.
INFERENCE_ROUTES = {
  ("GET", "/v1/models"): ("models:read", "mobius-agent-gateway"),
  ("GET", "/v1/balance"): ("balance:read", "mobius-agent-gateway"),
  ("POST", "/v1/responses"): (
    "inference:responses", "mobius-agent-gateway"
  ),
}

_COMMUNITY_ROUTES = (
  ("GET", re.compile(r"/v1/community/apps"), "community:read"),
  ("GET", re.compile(r"/v1/community/publications"), "community:read"),
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
    re.compile(
      r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/revisions/"
      r"[A-Za-z0-9_:-]{8,200}/installs"
    ),
    "community:install",
  ),
)


def _community_scope(method: str, route_path: str, query: str) -> str | None:
  scope = next((
    declared_scope
    for declared_method, pattern, declared_scope in _COMMUNITY_ROUTES
    if method == declared_method and pattern.fullmatch(route_path)
  ), None)
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


def _b64(value: bytes) -> str:
  return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
  return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


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
    self.key = _load_or_create_key()
    self.instance_id = _load_or_create_instance_id()
    self.lock = threading.RLock()
    self.client = httpx.Client(timeout=30.0, follow_redirects=False)
    self.state = self._load_state()

  def close(self) -> None:
    self.client.close()

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
    allow_community: bool = False,
  ) -> httpx.Response:
    split = urllib.parse.urlsplit(path)
    route_path = split.path
    if not route_path.startswith("/") or split.fragment:
      raise FileNotFoundError("broker route not found")
    declared = INFERENCE_ROUTES.get((method, route_path)) if not split.query else None
    if declared is not None:
      scope, audience = declared
      route = (scope, audience, GATEWAY_BASE_URL)
    else:
      community_scope = (
        _community_scope(method, route_path, split.query)
        if allow_community
        else None
      )
      route = (
        (community_scope, "mobius-community-registry", COMMUNITY_BASE_URL)
        if community_scope is not None
        else None
      )
    if route is None:
      raise FileNotFoundError("broker route not found")
    scope, audience, target = route
    request_id = self._request_id(method, path, body, headers)
    idempotency_key = headers.get("idempotency-key", "")
    if idempotency_key and not re.fullmatch(
      r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", idempotency_key
    ):
      raise ValueError("invalid idempotency key")
    if audience == "mobius-community-registry" and method != "GET" and not idempotency_key:
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
      timeout=None if route_path == "/v1/responses" else 30.0,
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
    body_limit = MAX_BODY
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
      body = self._body(maximum=body_limit)
      incoming = {key.lower(): value for key, value in self.headers.items()}
      upstream = broker.proxy(
        method=method,
        path=path,
        body=body,
        headers=incoming,
        allow_community=is_unix,
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
        for chunk in upstream.iter_raw():
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
      self._json(502, {"error": "central identity request failed", "status": exc.response.status_code})
    except httpx.HTTPError:
      self._json(502, {"error": "central service unavailable"})

  do_GET = _handle
  do_POST = _handle


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
  daemon_threads = True
  allow_reuse_address = True


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
  daemon_threads = True
  allow_reuse_address = True


def main() -> None:
  if os.geteuid() != 0:
    raise SystemExit("identity broker must run as root")
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
