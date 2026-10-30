"""Möbius key vault: keeps keys encrypted and lends them to apps by name.

The owner and the owner's agents may read a key. Apps never receive a value:
they send an ordinary HTTPS request through the vault, naming the key and the
destination, and the vault adds the key on the way out.

The vault listens on one Unix socket in its folder (mode 0600, so app code
running as another user cannot connect). It trusts whoever can open that
socket: the Möbius server, and owner tools running as the same user such as
``store-key.py``. The server authenticates every caller with its own
platform-issued token and passes the attested identity in ``X-Vault-Caller``:

    app:<app-id> | agent:<chat-id> | owner | platform:<purpose>

    METHOD /use/<key>/<host>/<path>?<query>    use a key (apps, agents, owner)
    GET    /keys/<key>/value                   reveal (never for app callers)
    POST   /requests                           an app asks to use a key
    POST   /requests/<id>/approve | /deny      the owner answers

For a use, the vault checks the caller and the destination against the key's
grants (default deny), injects the key into its one configured slot (a header
or a query parameter), connects to a public address it resolved and pinned
itself, never follows redirects, redacts the value from the response, and
appends a usage record that never contains the value.

This program imports nothing from the Möbius server, so the store keeps one
small owner that is easy to review.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import fnmatch
import http.client
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


STORE_VERSION = 1
SOCKET_NAME = "vault.sock"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(
  r"^(\*\.)?(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MIN_VALUE_LENGTH = 1
MAX_VALUE_LENGTH = 8192
MAX_KEYS = 200
# What the per-app secret store allowed; apps' own keys keep that bound.
MAX_OWNED_PER_APP = 16
MAX_SITES = 50
MAX_LIST = 100
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_ADMIN_BYTES = 256 * 1024
MAX_REQUESTS = 200
MAX_REASON = 300
UPSTREAM_TIMEOUT_SECS = 60
# Synthesizes IPv6 addresses for IPv4 hosts; the embedded IPv4 is the real
# destination, so it must be public too.
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
USAGE_ROTATE_BYTES = 512 * 1024
REDACTED = b"[redacted by key vault]"
# The slot may never name a header that frames or routes the request.
FORBIDDEN_SLOT_HEADERS = frozenset({
  "host", "content-length", "transfer-encoding", "connection", "upgrade",
  "te", "trailer", "keep-alive", "proxy-authorization", "proxy-connection",
  "x-vault-caller", "accept-encoding",
})
HOP_BY_HOP = frozenset({
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "proxy-connection", "te", "trailer", "trailers", "transfer-encoding",
  "upgrade",
})


class VaultError(Exception):
  """A request the vault refuses, with an owner- or caller-facing reason."""

  def __init__(self, status: int, reason: str) -> None:
    super().__init__(reason)
    self.status = status
    self.reason = reason


# ---------------------------------------------------------------------------
# Validation of owner-supplied key records
# ---------------------------------------------------------------------------

def validate_name(name: str) -> str:
  if not isinstance(name, str) or not NAME_RE.fullmatch(name):
    raise VaultError(400, "Key names start with a letter and use letters, digits, - or _ (max 64).")
  return name


def validate_value(value) -> str:
  if not isinstance(value, str):
    raise VaultError(400, "The key value must be text.")
  value = value.strip()
  if len(value) < MIN_VALUE_LENGTH:
    raise VaultError(400, "The key value can't be empty.")
  if len(value) > MAX_VALUE_LENGTH:
    raise VaultError(400, "The key value is too long.")
  if "\x00" in value:
    raise VaultError(400, "The key value cannot contain NUL characters.")
  return value


# How a key is sent when nobody chose otherwise: an app's own save, or an
# approved request for a new key whose app did not say.
DEFAULT_INJECT = {"type": "header", "name": "Authorization", "prefix": "Bearer "}


def validate_inject(raw) -> dict:
  if not isinstance(raw, dict):
    raise VaultError(400, "Choose where the key is sent: a header or a query parameter.")
  kind = raw.get("type")
  name = str(raw.get("name") or "").strip()
  if kind == "header":
    if not TOKEN_RE.fullmatch(name) or name.lower() in FORBIDDEN_SLOT_HEADERS:
      raise VaultError(400, "That header name can't carry a key.")
    prefix = str(raw.get("prefix") or "")
    if len(prefix) > 64 or any(ch in prefix for ch in "\r\n\x00"):
      raise VaultError(400, "The header prefix must be short and on one line.")
    return {"type": "header", "name": name, "prefix": prefix}
  if kind == "query":
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{1,64}", name):
      raise VaultError(400, "That query parameter name can't carry a key.")
    return {"type": "query", "name": name}
  raise VaultError(400, "The key slot must be a header or a query parameter.")


def validate_site(raw) -> dict:
  if not isinstance(raw, dict):
    raise VaultError(400, "Each site needs a host, methods and paths.")
  host = str(raw.get("host") or "").strip().lower().rstrip(".")
  if not HOST_RE.fullmatch(host):
    raise VaultError(400, f"'{host or '(empty)'}' is not a site name like api.example.com.")
  methods = raw.get("methods") or []
  if not isinstance(methods, list) or not methods:
    raise VaultError(400, f"Choose at least one method for {host}.")
  methods = sorted({str(m).upper() for m in methods})
  for method in methods:
    if method not in METHODS:
      raise VaultError(400, f"Unsupported method {method}.")
  paths = raw.get("paths") or []
  if not isinstance(paths, list) or not paths or len(paths) > 20:
    raise VaultError(400, f"Give {host} between one and 20 paths, such as /v1/*.")
  clean_paths = []
  for pattern in paths:
    pattern = str(pattern).strip()
    if not pattern.startswith("/") or len(pattern) > 200 or any(
      ch in pattern for ch in "?#\\\r\n\x00[]"
    ):
      raise VaultError(400, f"Path '{pattern}' must start with / (use * as a wildcard).")
    clean_paths.append(pattern)
  return {"host": host, "methods": methods, "paths": sorted(set(clean_paths))}


LEGACY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_origin(raw) -> dict:
  """The app and the app's own secret name a key was created for."""
  if not isinstance(raw, dict):
    raise VaultError(400, "A key's origin names an app id and its secret name.")
  app_id, name = raw.get("app"), raw.get("name")
  if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
    raise VaultError(400, "Invalid origin app id.")
  if not isinstance(name, str) or not LEGACY_NAME_RE.fullmatch(name):
    raise VaultError(400, "Invalid origin secret name.")
  return {"app": app_id, "name": name}


def owned_name_candidates(app_id: int, secret_name: str):
  """Vault names to try for a new app-owned key, most readable first."""
  plain = re.sub(r"[^A-Za-z0-9_-]", "-", secret_name)
  if not plain[:1].isalpha():
    plain = f"k{plain}"
  plain = plain[:64]
  yield plain
  scoped = f"app{int(app_id)}-{plain}"
  yield scoped[:64]
  for n in range(2, 100):
    suffix = f"-{n}"
    yield scoped[:64 - len(suffix)] + suffix


def validate_grants(raw) -> dict:
  if raw is None:
    raw = {}
  if not isinstance(raw, dict):
    raise VaultError(400, "Grants must list apps, agent use and sites.")
  apps = raw.get("apps") or []
  if not isinstance(apps, list) or len(apps) > MAX_LIST:
    raise VaultError(400, "Too many apps.")
  clean_apps = []
  for app_id in apps:
    # Apps are granted by numeric id: names and slugs can be reused.
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
      raise VaultError(400, f"'{app_id}' is not an app id.")
    clean_apps.append(app_id)
  sites = raw.get("sites") or []
  if not isinstance(sites, list) or len(sites) > MAX_SITES:
    raise VaultError(400, f"A key can be used on at most {MAX_SITES} sites.")
  return {
    "apps": sorted(set(clean_apps)),
    "agents": bool(raw.get("agents")),
    "sites": [validate_site(site) for site in sites],
  }


# ---------------------------------------------------------------------------
# Rules: caller, site, method and path (default deny)
# ---------------------------------------------------------------------------

CALLER_RE = re.compile(r"(app|agent|owner|platform)(?::([A-Za-z0-9_.-]{1,128}))?")


def parse_caller(header: str | None) -> tuple[str, str]:
  """Return (kind, id) for the caller the server attested, or raise."""
  match = CALLER_RE.fullmatch((header or "").strip())
  if not match:
    raise VaultError(403, "The caller is not identified.")
  kind, ident = match.group(1), match.group(2) or ""
  if kind == "app" and not ident.isdigit():
    raise VaultError(403, "App callers are identified by app id.")
  return kind, ident


def caller_text(caller: tuple[str, str]) -> str:
  return f"{caller[0]}:{caller[1]}" if caller[1] else caller[0]


def caller_allowed(grants: dict, kind: str, ident: str) -> bool:
  if kind == "owner":
    return True
  if kind == "agent":
    return bool(grants.get("agents"))
  if kind == "app":
    return int(ident) in (grants.get("apps") or [])
  return False


def normalized_request_path(raw_path: str) -> str:
  """Decode a request path for rule matching, refusing ambiguous forms.

  Dot segments, encoded slashes and backslashes are refused outright: a path
  that means one thing to the rule and another to the server would let a
  narrow grant reach a different endpoint.
  """
  if not raw_path.startswith("/"):
    raise VaultError(400, "The destination path must start with /.")
  lowered = raw_path.lower()
  if "%2f" in lowered or "%5c" in lowered or "\\" in raw_path or "%00" in lowered:
    raise VaultError(403, "Encoded slashes are not allowed in key requests.")
  decoded = urllib.parse.unquote(raw_path)
  for segment in decoded.split("/"):
    if segment in (".", ".."):
      raise VaultError(403, "Dot segments are not allowed in key requests.")
  if "//" in decoded:
    raise VaultError(403, "Empty path segments are not allowed in key requests.")
  return decoded


def host_matches(pattern: str, host: str) -> bool:
  if pattern.startswith("*."):
    return host.endswith(pattern[1:]) and host != pattern[2:]
  return host == pattern


def _pattern_within(pattern: str, allowed: str) -> bool:
  """Whether every path ``pattern`` matches is matched by ``allowed`` too.

  Matching the narrower pattern's text against the wider one is exact when
  the wider one uses only ``*``: its literal characters must then meet the
  narrower pattern's literal characters, and every wildcard of the narrower
  pattern falls inside one of its ``*``. A ``?`` there is compared only as
  the same text.
  """
  return pattern == allowed or ("?" not in allowed and fnmatch.fnmatchcase(pattern, allowed))


def sites_outside(sites: list[dict], requested: list[dict]) -> list[dict]:
  """The part of ``requested`` that ``sites`` do not already allow.

  Each entry is a host with methods that all lack the same paths, so the
  owner sees exactly what an approval leaves out.
  """
  outside = []
  for want in requested:
    by_paths: dict[tuple, list[str]] = {}
    for method in want["methods"]:
      missing = tuple(
        path for path in want["paths"]
        if not any(
          host_matches(site["host"], want["host"]) and method in site["methods"]
          and any(_pattern_within(path, allowed) for allowed in site["paths"])
          for site in sites
        )
      )
      if missing:
        by_paths.setdefault(missing, []).append(method)
    outside += [
      {"host": want["host"], "methods": methods, "paths": list(paths)}
      for paths, methods in by_paths.items()
    ]
  return outside


def site_allows(grants: dict, host: str, method: str, path: str) -> str | None:
  """Return None when some site rule allows the request, else the reason."""
  hosts = [s for s in grants.get("sites") or [] if host_matches(s["host"], host)]
  if not hosts:
    return f"{host} is not an allowed site for this key"
  with_method = [s for s in hosts if method in s["methods"]]
  if not with_method:
    return f"{method} is not allowed on {host}"
  for site in with_method:
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in site["paths"]):
      return None
  return f"{path} is not an allowed path on {host}"


# ---------------------------------------------------------------------------
# Destination pinning and the upstream exchange
# ---------------------------------------------------------------------------

def resolve_public_address(host: str) -> str:
  """Resolve once and pin; refuse unless every address is public.

  Requiring all answers to be global keeps a mixed answer set from steering
  the connection to a private service, and connecting to the pinned address
  closes the gap between check and use (DNS rebinding).
  """
  try:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
  except socket.gaierror:
    raise VaultError(502, f"{host} could not be resolved")
  addresses = []
  for info in infos:
    address = ipaddress.ip_address(info[4][0])
    reached = address
    if address.version == 6 and address in NAT64_PREFIX:
      reached = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if not reached.is_global or reached.is_multicast:
      raise VaultError(403, f"{host} resolves to a non-public address")
    addresses.append(str(address))
  if not addresses:
    raise VaultError(502, f"{host} has no address")
  return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
  """HTTPS to a pre-resolved address, verifying the certificate for the host."""

  def __init__(self, host: str, address: str, timeout: float) -> None:
    super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
    self._pinned_address = address

  def connect(self) -> None:
    raw = socket.create_connection((self._pinned_address, 443), self.timeout)
    self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def open_pinned_connection(host: str, timeout: float) -> http.client.HTTPConnection:
  return PinnedHTTPSConnection(host, resolve_public_address(host), timeout)


def secret_forms(value: str) -> list[bytes]:
  """The encodings in which an echoed key commonly reappears."""
  forms = {
    value,
    urllib.parse.quote(value, safe=""),
    urllib.parse.quote_plus(value),
    json.dumps(value)[1:-1],
    base64.b64encode(value.encode()).decode(),
    base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
  }
  return sorted((f.encode() for f in forms if f), key=len, reverse=True)


def decoded_body(headers: list[tuple[str, str]], payload: bytes) -> tuple[list[tuple[str, str]], bytes]:
  """The response as plain bytes, so redaction sees what the caller will see.

  The vault asks for identity encoding, but a site may compress anyway, and a
  compressed echo of the key would slip past redaction only to be inflated by
  the caller. gzip and deflate are decoded here (bounded like any response);
  any other encoding is refused rather than forwarded.
  """
  encodings = [
    token.strip().lower()
    for name, value in headers if name.lower() == "content-encoding"
    for token in value.split(",") if token.strip()
  ]
  encodings = [token for token in encodings if token != "identity"]
  plain = [(name, value) for name, value in headers if name.lower() != "content-encoding"]
  if not encodings:
    return plain, payload
  if len(encodings) > 1 or encodings[0] not in ("gzip", "x-gzip", "deflate"):
    raise VaultError(502, f"The site answered with an unsupported encoding ({', '.join(encodings)}).")
  # wbits 47 accepts gzip or zlib framing; raw deflate needs -15.
  for wbits in ((47,) if encodings[0] != "deflate" else (15, -15)):
    inflater = zlib.decompressobj(wbits)
    try:
      body = inflater.decompress(payload, MAX_RESPONSE_BYTES + 1)
    except zlib.error:
      continue
    if len(body) > MAX_RESPONSE_BYTES or inflater.unconsumed_tail:
      raise VaultError(502, "The response is too large for the key vault.")
    return plain, body
  raise VaultError(502, "The site sent a response that could not be decompressed.")


def redact(data: bytes, forms: list[bytes]) -> bytes:
  for form in forms:
    if form in data:
      data = data.replace(form, REDACTED)
  return data


@dataclass(frozen=True)
class UpstreamRequest:
  method: str
  host: str
  target: str
  headers: list[tuple[str, str]]


def build_upstream_request(
  *, method: str, host: str, raw_path: str, raw_query: str,
  caller_headers: list[tuple[str, str]], inject: dict, value: str,
) -> UpstreamRequest:
  """Place the key in its one slot and drop anything that could displace it."""
  slot = inject["name"].lower() if inject["type"] == "header" else None
  headers = []
  for name, header_value in caller_headers:
    lowered = name.lower()
    if (
      # A ranged answer could carry an echoed key in slices that redaction,
      # which matches the whole value, would never recognise.
      lowered in HOP_BY_HOP or lowered in ("host", "content-length", "accept-encoding", "range", "if-range")
      or lowered.startswith("x-vault-") or lowered.startswith("proxy-")
      or lowered == slot
    ):
      continue
    headers.append((name, header_value))
  headers.append(("Host", host))
  # Identity encoding keeps an echoed key visible to redaction.
  headers.append(("Accept-Encoding", "identity"))
  query = raw_query
  if inject["type"] == "header":
    headers.append((inject["name"], f"{inject.get('prefix', '')}{value}"))
  else:
    pairs = [
      (k, v) for k, v in urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
      if k != inject["name"]
    ]
    pairs.append((inject["name"], value))
    query = urllib.parse.urlencode(pairs)
  target = raw_path + (f"?{query}" if query else "")
  return UpstreamRequest(method=method, host=host, target=target, headers=headers)


# ---------------------------------------------------------------------------
# Encrypted store and usage log
# ---------------------------------------------------------------------------

def _now() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, data: bytes) -> None:
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except FileNotFoundError:
      pass
    raise


class Vault:
  """The encrypted key store, its grants, and its value-free usage log."""

  def __init__(self, data_dir: Path) -> None:
    self.dir = data_dir
    self.store_path = data_dir / "keys.json"
    self.master_path = data_dir / "master.key"
    self.usage_path = data_dir / "usage.jsonl"
    self.requests_path = data_dir / "requests.json"
    self._lock = threading.RLock()
    self._prepare_folder()
    self._aead = AESGCM(self._load_master_key())
    self._records = self._load_records()
    self._requests = self._load_requests()
    # Earlier builds kept an origin after its app lost the grant; never let
    # one stored that way confer ownership.
    orphaned = [name for name, record in self._records.items()
                if record.get("origin") and record["origin"]["app"] not in record["grants"]["apps"]]
    for name in orphaned:
      self._drop_orphaned_origin(self._records[name])
    if orphaned:
      self._save()

  def _prepare_folder(self) -> None:
    # The modes are what keep app code running as another user out, so the
    # vault reasserts them at every start instead of trusting what it finds.
    self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(self.dir, 0o700)
    for child in self.dir.iterdir():
      if child.is_file() and not child.is_symlink():
        os.chmod(child, 0o600)
    ignore = self.dir / ".gitignore"
    if not ignore.exists():
      # Keeps the folder out of the /data safety-net repository regardless of
      # which image-owned ignore template is installed.
      _atomic_write(ignore, b"*\n")

  def _load_master_key(self) -> bytes:
    if self.master_path.exists():
      key = self.master_path.read_bytes()
      if len(key) != 32:
        raise SystemExit("key vault: master key is damaged; refusing to start")
      return key
    key = secrets.token_bytes(32)
    if self.store_path.exists():
      raise SystemExit("key vault: master key missing for an existing store; refusing to start")
    _atomic_write(self.master_path, key)
    return key

  def _load_records(self) -> dict:
    if not self.store_path.exists():
      return {}
    payload = json.loads(self.store_path.read_text())
    if payload.get("version") != STORE_VERSION:
      raise SystemExit("key vault: unknown store version; refusing to start")
    return payload.get("keys") or {}

  def _save(self) -> None:
    payload = {"version": STORE_VERSION, "keys": self._records}
    _atomic_write(self.store_path, json.dumps(payload, indent=1, sort_keys=True).encode())

  def _load_requests(self) -> dict:
    if not self.requests_path.exists():
      return {}
    return json.loads(self.requests_path.read_text()).get("requests") or {}

  def _save_requests(self) -> None:
    payload = json.dumps({"requests": self._requests}, indent=1, sort_keys=True).encode()
    _atomic_write(self.requests_path, payload)

  def _seal(self, name: str, value: str) -> dict:
    nonce = secrets.token_bytes(12)
    sealed = self._aead.encrypt(nonce, value.encode(), name.encode())
    return {"nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode()}

  def _open(self, name: str, record: dict) -> str:
    nonce = base64.b64decode(record["nonce"])
    sealed = base64.b64decode(record["sealed"])
    return self._aead.decrypt(nonce, sealed, name.encode()).decode()

  # -- owner operations ----------------------------------------------------

  def list_keys(self) -> list[dict]:
    with self._lock:
      records = {name: dict(record) for name, record in self._records.items()}
    stats = self.usage_stats()
    return [
      {
        "name": name,
        "inject": record["inject"],
        "grants": record["grants"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "revision": record.get("revision", 0),
        "origin": record.get("origin"),
        **stats.get(name, {"uses": 0, "denied": 0, "last_used": None}),
      }
      for name, record in sorted(records.items())
    ]

  def put_key(self, name: str, body: dict) -> dict:
    """Create or update a key.

    ``create_only`` refuses an occupied name atomically, so a caller choosing a
    free name can never overwrite another key. ``revision``, when given, must
    equal the key's current revision (bumped by every change), so an edit
    made from a stale copy, such as a grant to an app that has since been
    purged, is refused instead of written back.

    ``origin`` records the app and the app's own secret name a key was created
    for. It is set at creation and an update never moves it to another app. A
    new value from anyone but that app's own save (``save_owned``, which states
    the same origin) clears it and withdraws the app's grant, which it gave
    itself by creating the key: once the owner or an agent supplies the value,
    the key is the owner's, and the app uses it only if the owner grants it.
    """
    validate_name(name)
    inject = validate_inject(body.get("inject"))
    grants = validate_grants(body.get("grants"))
    origin = validate_origin(body["origin"]) if body.get("origin") is not None else None
    with self._lock:
      existing = self._records.get(name)
      if existing is not None and body.get("create_only"):
        raise VaultError(409, f"A key named {name} already exists.")
      if body.get("revision") is not None and (
        existing is None or body["revision"] != existing.get("revision", 0)
      ):
        raise VaultError(409, f"{name} changed since it was loaded; review it again.")
      if existing is None and len(self._records) >= MAX_KEYS:
        raise VaultError(400, f"The vault holds at most {MAX_KEYS} keys.")
      if body.get("value") is not None:
        sealed = self._seal(name, validate_value(body["value"]))
      elif existing is not None:
        sealed = {"nonce": existing["nonce"], "sealed": existing["sealed"]}
      else:
        raise VaultError(400, "A new key needs a value.")
      kept_origin = existing.get("origin") if existing else origin
      if existing is not None and body.get("value") is not None and origin != kept_origin:
        if kept_origin:
          grants["apps"] = [a for a in grants["apps"] if a != kept_origin["app"]]
        kept_origin = None
      now = _now()
      self._records[name] = {
        **sealed,
        "inject": inject,
        "grants": grants,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "revision": existing.get("revision", 0) + 1 if existing else 1,
      }
      if kept_origin:
        self._records[name]["origin"] = kept_origin
        self._drop_orphaned_origin(self._records[name])
      self._save()
      granted = [rid for rid, item in self._requests.items()
                 if item["key"] == name and item["app"] in grants["apps"]]
      for rid in granted:
        del self._requests[rid]
      if granted:
        self._save_requests()
    self.log_usage(name, "owner", "-", "-", "-", "added" if existing is None else "updated")
    return {"name": name, "created": existing is None}

  def save_owned(self, app_id: int, secret_name: str, value: str) -> dict:
    """Save an app's own secret by its origin, in one locked step.

    The key ``app_id`` created for ``secret_name`` and still holds is
    replaced; otherwise a new key is created at the first free name (plain,
    then app-scoped, then numbered), granted to the app and the owner's
    agents, sent as a bearer token, with no sites yet. An app holds at most
    ``MAX_OWNED_PER_APP`` such keys, as in the per-app store. Doing both under
    the vault's lock means simultaneous saves can never leave two keys for one
    app secret.
    """
    origin = validate_origin({"app": app_id, "name": secret_name})
    value = validate_value(value)
    with self._lock:
      for name, record in self._records.items():
        if record.get("origin") == origin and app_id in record["grants"]["apps"]:
          self.put_key(name, {"value": value, "inject": record["inject"],
                              "grants": record["grants"], "origin": origin})
          return {"name": name, "created": False}
      owned = sum(1 for r in self._records.values()
                  if (r.get("origin") or {}).get("app") == app_id and app_id in r["grants"]["apps"])
      if owned >= MAX_OWNED_PER_APP:
        raise VaultError(413, f"An app may store at most {MAX_OWNED_PER_APP} secrets.")
      for name in owned_name_candidates(app_id, secret_name):
        if name not in self._records:
          self.put_key(name, {
            "value": value,
            "inject": dict(DEFAULT_INJECT),
            "grants": {"apps": [app_id], "agents": True, "sites": []},
            "origin": origin,
            "create_only": True,
          })
          return {"name": name, "created": True}
    raise VaultError(409, f"No free vault name for {secret_name}.")

  def release_owned(self, app_id: int, secret_name: str) -> dict | None:
    """Let an app give up its own secret, in one locked step.

    The app's grant is withdrawn from the key it created for ``secret_name``.
    When no other app still holds a grant, the key itself is deleted;
    otherwise it stays, with the owner's current settings, for the apps the
    owner shared it with. Checking ownership and changing the key under the
    same lock means a grant the owner adds meanwhile is never lost or deleted.
    Returns None when the app owns no such key.
    """
    origin = validate_origin({"app": app_id, "name": secret_name})
    with self._lock:
      name = next(
        (n for n, r in self._records.items()
         if r.get("origin") == origin and app_id in r["grants"]["apps"]),
        None,
      )
      if name is None:
        return None
      deleted = self._release_locked(name, app_id)
      self._save()
    self.log_usage(
      name, caller_text(("app", str(app_id))), "-", "-", "-",
      "revoked" if deleted else "released",
    )
    return {"name": name, "deleted": deleted}

  @staticmethod
  def _touch(record: dict) -> None:
    record["updated_at"] = _now()
    record["revision"] = record.get("revision", 0) + 1

  @staticmethod
  def _drop_orphaned_origin(record: dict) -> None:
    """Forget the creating app once it no longer holds a grant on the key.

    ``origin`` makes its app the key's owner (it may replace and release the
    key, and use it for its own model provider without the key's sites). App
    ids are reused after a purge, so an origin that outlived its app's grant
    would hand that ownership to an unrelated app the owner later grants.
    """
    origin = record.get("origin")
    if origin and origin["app"] not in record["grants"]["apps"]:
      del record["origin"]

  def _release_locked(self, name: str, app_id: int) -> bool:
    """Withdraw ``app_id`` from its own key; delete the key unless shared.

    The caller holds the lock and saves. Returns whether the key was deleted.
    """
    record = self._records[name]
    others = [a for a in record["grants"]["apps"] if a != app_id]
    if others:
      record["grants"]["apps"] = others
      self._drop_orphaned_origin(record)
      self._touch(record)
    else:
      del self._records[name]
    return not others

  def erase_owned(self, app_id: int) -> list[str]:
    """Release every key ``app_id`` created for itself and still holds.

    Erasing an app's data (or purging the app) erases its secrets, as the
    per-app store did: each such key is deleted, or kept only for the other
    apps the owner shared it with. Keys the app merely borrows, and keys whose
    value the owner or an agent supplied (no longer the app's), are untouched.
    Returns the names of the deleted keys.
    """
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
      raise VaultError(400, "Invalid app id.")
    with self._lock:
      owned = [n for n, r in self._records.items()
               if (r.get("origin") or {}).get("app") == app_id and app_id in r["grants"]["apps"]]
      deleted = [name for name in owned if self._release_locked(name, app_id)]
      if owned:
        self._save()
    for name in owned:
      self.log_usage(name, "platform", "-", "-", "-",
                     "revoked" if name in deleted else "released",
                     reason=f"app:{app_id} data erased")
    return deleted

  def revoke_key(self, name: str) -> bool:
    validate_name(name)
    with self._lock:
      if self._records.pop(name, None) is None:
        return False
      self._save()
    self.log_usage(name, "owner", "-", "-", "-", "revoked")
    return True

  def reveal(self, name: str, caller: tuple[str, str]) -> str:
    """The value itself, for the owner, the owner's agents and the platform.

    Apps are refused here as well as by the server: lending a key to an app
    means using it on the app's behalf, never handing it over.
    """
    validate_name(name)
    if caller[0] == "app":
      self.log_usage(name, caller_text(caller), "-", "-", "-", "denied",
                     status=403, reason="apps cannot read key values")
      raise VaultError(403, "Apps cannot read key values; use the key through the vault.")
    with self._lock:
      record = self._records.get(name)
      if record is None:
        raise VaultError(404, f"No key named {name}")
      value = self._open(name, record)
    self.log_usage(name, caller_text(caller), "-", "-", "-", "revealed")
    return value

  # -- app access requests (the handshake) -----------------------------------

  def request_access(self, app_id: int, body: dict) -> dict:
    """An app asks to use a key; answer granted, pending or denied.

    Asking is idempotent, so an app checks its status by asking again. The
    first ask for a key returns ``created`` so the server tells the owner
    once, never on every retry.
    """
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
      raise VaultError(400, "Invalid app id.")
    name = validate_name(body.get("key"))
    reason = str(body.get("reason") or "").strip()[:MAX_REASON]
    sites = body.get("sites") or []
    if not isinstance(sites, list) or len(sites) > MAX_SITES:
      raise VaultError(400, f"Ask for at most {MAX_SITES} sites.")
    sites = [validate_site(site) for site in sites]
    inject = validate_inject(body["inject"]) if body.get("inject") is not None else None
    request_id = f"{app_id}-{name}"
    with self._lock:
      record = self._records.get(name)
      if record is not None and app_id in record["grants"]["apps"]:
        if self._requests.pop(request_id, None) is not None:
          self._save_requests()
        return {"status": "granted", "key": name}
      existing = self._requests.get(request_id)
      if existing is not None:
        return {"status": existing["status"], "key": name, "id": request_id}
      if len(self._requests) >= MAX_REQUESTS:
        raise VaultError(429, "Too many open key requests; ask the owner to review them.")
      self._requests[request_id] = {
        "id": request_id, "app": app_id, "app_name": str(body.get("app_name") or "")[:100],
        "key": name, "reason": reason, "sites": sites, "inject": inject,
        "status": "pending", "created_at": _now(), "ticket": secrets.token_hex(8),
      }
      self._save_requests()
    self.log_usage(name, f"app:{app_id}", "-", "-", "-", "requested")
    return {"status": "pending", "key": name, "id": request_id, "created": True}

  def list_requests(self) -> list[dict]:
    """Open requests; ``outside_sites`` is what an existing key would not allow."""
    with self._lock:
      requests = [dict(item) for item in self._requests.values()]
      for item in requests:
        record = self._records.get(item["key"])
        item["key_exists"] = record is not None
        item["outside_sites"] = (
          sites_outside(record["grants"]["sites"], item["sites"]) if record else []
        )
    return sorted(requests, key=lambda item: item["created_at"], reverse=True)

  def approve_request(self, request_id: str, body: dict) -> dict:
    """Grant the app; create the key first when the owner supplies its value.

    A new key starts with the sites the owner confirms (the request's by
    default). An existing key keeps its own sites: they are shared by every
    app and agent that may use the key, so approving one app never widens
    them. The app may use the key only where it already works; the answer's
    ``outside`` lists what it asked for beyond that, which only the owner's
    own edit of the key's sites can add.

    The answer names the request's ``ticket``: a request id is the app id and
    key name, so a request card left open while its app was purged would
    otherwise approve the same ask from an unrelated app that reused the id.
    """
    with self._lock:
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      if body.get("ticket") != request.get("ticket"):
        raise VaultError(409, "That request changed since it was shown; review it again.")
      name = request["key"]
      existing = self._records.get(name)
      if existing is not None:
        if any(body.get(field) is not None for field in ("value", "inject", "sites")):
          raise VaultError(
            409, f"{name} already exists, and approving only lets the app use it as it is. "
            "Review the request again, and change the key itself on its page.",
          )
        grants = {**existing["grants"], "apps": sorted(set(existing["grants"]["apps"]) | {request["app"]})}
        put = {"grants": grants, "inject": existing["inject"]}
        outside = sites_outside(existing["grants"]["sites"], request["sites"])
      else:
        sites = body.get("sites", request["sites"])
        if not isinstance(sites, list) or len(sites) > MAX_SITES:
          raise VaultError(400, f"Choose at most {MAX_SITES} sites.")
        grants = {"apps": [request["app"]], "agents": False, "sites": [validate_site(s) for s in sites]}
        inject = body.get("inject") or request["inject"] or dict(DEFAULT_INJECT)
        put = {"grants": grants, "inject": inject, "value": body.get("value")}
        outside = []
      result = self.put_key(name, put)
      self._requests.pop(request_id, None)
      self._save_requests()
    self.log_usage(name, "owner", "-", "-", "-", f"granted app:{request['app']}")
    return {**result, "granted": request["app"], "outside": outside}

  def deny_request(self, request_id: str) -> dict:
    with self._lock:
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      request["status"] = "denied"
      request["answered_at"] = _now()
      self._save_requests()
    self.log_usage(request["key"], "owner", "-", "-", "-", f"denied app:{request['app']}")
    return {"denied": request_id}

  def forget_app(self, app_id: int) -> dict:
    """Erase a purged app's own keys and drop it from every grant and request.

    App ids can be reused once an app is purged, so a grant must never outlive
    the app it was given to.
    """
    with self._lock:
      deleted = self.erase_owned(app_id)
      keys = [n for n, r in self._records.items() if app_id in r["grants"]["apps"]]
      for name in keys:
        grants = self._records[name]["grants"]
        grants["apps"] = [a for a in grants["apps"] if a != app_id]
        self._drop_orphaned_origin(self._records[name])
        self._touch(self._records[name])
      requests = [rid for rid, item in self._requests.items() if item["app"] == app_id]
      for rid in requests:
        del self._requests[rid]
      if keys:
        self._save()
      if requests:
        self._save_requests()
    for name in keys:
      self.log_usage(name, "platform", "-", "-", "-", f"revoked app:{app_id} (app purged)")
    return {"keys": keys, "deleted": deleted, "requests": len(requests)}

  def forget_request(self, request_id: str) -> bool:
    with self._lock:
      if self._requests.pop(request_id, None) is None:
        return False
      self._save_requests()
    return True

  # -- use -----------------------------------------------------------------

  def authorize(self, name: str, caller: tuple[str, str], method: str, host: str, path: str):
    """Return (inject, value) for an allowed use, or raise the denial."""
    with self._lock:
      record = self._records.get(name)
      if record is None:
        raise VaultError(404, f"No key named {name}")
      if not caller_allowed(record["grants"], *caller):
        raise VaultError(403, f"{caller_text(caller)} may not use this key")
      reason = site_allows(record["grants"], host, method, path)
      if reason:
        raise VaultError(403, reason)
      value = self._open(name, record)
      if record["inject"]["type"] == "header" and any(ch in value for ch in "\r\n"):
        raise VaultError(400, f"{name} has line breaks, so it can't be sent in a header.")
      return dict(record["inject"]), value

  # -- usage log (never values) ----------------------------------------------

  def log_usage(
    self, key: str, caller: str, method: str, host: str, path: str,
    outcome: str, *, status: int | None = None, reason: str | None = None,
    ms: int | None = None,
  ) -> None:
    entry = {
      "ts": _now(), "key": key, "caller": caller, "method": method,
      "host": host, "path": path[:300], "outcome": outcome,
    }
    if status is not None:
      entry["status"] = status
    if reason:
      entry["reason"] = reason[:300]
    if ms is not None:
      entry["ms"] = ms
    line = (json.dumps(entry, sort_keys=True) + "\n").encode()
    with self._lock:
      try:
        if self.usage_path.stat().st_size + len(line) > USAGE_ROTATE_BYTES:
          os.replace(self.usage_path, self.usage_path.with_suffix(".1.jsonl"))
      except FileNotFoundError:
        pass
      fd = os.open(self.usage_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
      try:
        os.write(fd, line)
      finally:
        os.close(fd)

  def usage(self, *, key: str | None = None, limit: int = 100) -> list[dict]:
    entries = [e for e in self._usage_entries() if key is None or e.get("key") == key]
    return list(reversed(entries))[:max(1, min(limit, 500))]

  def _usage_entries(self) -> list[dict]:
    """Every logged entry, oldest first (the log is bounded by rotation)."""
    entries = []
    for path in (self.usage_path.with_suffix(".1.jsonl"), self.usage_path):
      try:
        lines = path.read_text().splitlines()
      except FileNotFoundError:
        continue
      for line in lines:
        try:
          entries.append(json.loads(line))
        except ValueError:
          continue
    return entries

  def usage_stats(self) -> dict:
    stats: dict[str, dict] = {}
    for entry in self._usage_entries():
      item = stats.setdefault(entry["key"], {"uses": 0, "denied": 0, "last_used": None})
      if entry["outcome"] == "used":
        item["uses"] += 1
        item["last_used"] = entry["ts"]
      elif entry["outcome"] == "denied":
        item["denied"] += 1
    return stats


# ---------------------------------------------------------------------------
# The vault socket: one HTTP server on a mode-0600 Unix socket, spoken to by
# the Möbius server, which attests every caller, and same-user owner tools.
# ---------------------------------------------------------------------------

USE_PATH_RE = re.compile(r"^/use/([^/]+)/([^/]+)(/.*)?$")


class VaultHandler(BaseHTTPRequestHandler):
  server_version = "MobiusKeyVault/2"
  sys_version = ""
  protocol_version = "HTTP/1.1"
  vault: Vault
  connect = staticmethod(open_pinned_connection)

  def address_string(self) -> str:
    return "vault-socket"

  def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
    pass

  def _send_json(self, status: int, payload, *, denied: bool = False) -> None:
    body = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Connection", "close")
    if denied:
      self.send_header("X-Vault-Denied", "1")
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)

  def _handle(self) -> None:
    # One exchange per connection: a refusal can leave a body unread, which
    # must never be parsed as the next request.
    self.close_connection = True
    split = urllib.parse.urlsplit(self.path)
    if split.path.startswith("/use/"):
      self._use(split)
      return
    try:
      self._admin(split)
    except VaultError as exc:
      self._send_json(exc.status, {"error": exc.reason})
    except ValueError:
      self._send_json(400, {"error": "Invalid request."})

  # -- owner and handshake operations ----------------------------------------

  def _json_body(self) -> dict:
    size = int(self.headers.get("Content-Length") or 0)
    if size > MAX_ADMIN_BYTES:
      raise VaultError(413, "Request too large.")
    try:
      body = json.loads(self.rfile.read(size) or b"{}")
    except ValueError:
      raise VaultError(400, "Invalid JSON.")
    if not isinstance(body, dict):
      raise VaultError(400, "Invalid JSON.")
    return body

  def _admin(self, split) -> None:
    parts = [urllib.parse.unquote(p) for p in split.path.split("/") if p]
    query = dict(urllib.parse.parse_qsl(split.query))
    method = self.command
    vault = self.vault
    if method == "GET" and parts == ["health"]:
      self._send_json(200, {"ok": True, "keys": len(vault.list_keys())})
    elif method == "GET" and parts == ["keys"]:
      self._send_json(200, {"keys": vault.list_keys()})
    elif method == "PUT" and len(parts) == 2 and parts[0] == "keys":
      self._send_json(200, vault.put_key(parts[1], self._json_body()))
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "keys":
      if not vault.revoke_key(parts[1]):
        raise VaultError(404, f"No key named {parts[1]}")
      self._send_json(200, {"revoked": parts[1]})
    elif method == "GET" and len(parts) == 3 and parts[0] == "keys" and parts[2] == "value":
      caller = parse_caller(self.headers.get("X-Vault-Caller"))
      self._send_json(200, {"name": parts[1], "value": vault.reveal(parts[1], caller)})
    elif method == "GET" and parts == ["usage"]:
      limit = int(query.get("limit") or 100)
      self._send_json(200, {"usage": vault.usage(key=query.get("key") or None, limit=limit)})
    elif method == "GET" and parts == ["requests"]:
      self._send_json(200, {"requests": vault.list_requests()})
    elif method == "POST" and parts == ["requests"]:
      kind, ident = parse_caller(self.headers.get("X-Vault-Caller"))
      if kind != "app":
        raise VaultError(403, "Only apps ask for keys.")
      self._send_json(200, vault.request_access(int(ident), self._json_body()))
    elif method == "POST" and len(parts) == 3 and parts[0] == "requests" and parts[2] == "approve":
      self._send_json(200, vault.approve_request(parts[1], self._json_body()))
    elif method == "POST" and len(parts) == 3 and parts[0] == "requests" and parts[2] == "deny":
      self._send_json(200, vault.deny_request(parts[1]))
    elif method == "POST" and parts == ["owned"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id and its secret name.")
      self._send_json(200, vault.save_owned(body["app"], body["name"], body.get("value")))
    elif method == "POST" and parts == ["owned", "release"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id and its secret name.")
      released = vault.release_owned(body["app"], body["name"])
      if released is None:
        raise VaultError(404, "That app owns no such key.")
      self._send_json(200, released)
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "apps" and parts[1].isdigit():
      self._send_json(200, vault.forget_app(int(parts[1])))
    elif method == "DELETE" and len(parts) == 3 and parts[0] == "apps" and parts[1].isdigit() and parts[2] == "owned":
      self._send_json(200, {"deleted": vault.erase_owned(int(parts[1]))})
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "requests":
      if not vault.forget_request(parts[1]):
        raise VaultError(404, "That request is gone.")
      self._send_json(200, {"forgotten": parts[1]})
    else:
      self._send_json(404, {"error": "not found"})

  # -- use -------------------------------------------------------------------

  def _use(self, split) -> None:
    match = USE_PATH_RE.fullmatch(split.path)
    if not match:
      self._send_json(404, {"error": "Use /use/<key>/<host>/<path>."})
      return
    name = urllib.parse.unquote(match.group(1))
    host = match.group(2).lower()
    raw_path = match.group(3) or "/"
    method = self.command
    caller_label = "unknown"
    started = time.monotonic()
    try:
      caller = parse_caller(self.headers.get("X-Vault-Caller"))
      caller_label = caller_text(caller)
      if not HOST_RE.fullmatch(host) or host.startswith("*."):
        raise VaultError(400, "The destination must be a site name like api.example.com (HTTPS, port 443).")
      path = normalized_request_path(raw_path)
      body = self._read_body()
      inject, value = self.vault.authorize(name, caller, method, host, path)
    except VaultError as exc:
      self.vault.log_usage(
        name, caller_label, method, host, raw_path, "denied",
        status=exc.status, reason=exc.reason,
      )
      self._send_json(exc.status, {"error": "denied", "reason": exc.reason}, denied=True)
      return

    request = build_upstream_request(
      method=method, host=host, raw_path=raw_path, raw_query=split.query,
      caller_headers=list(self.headers.items()), inject=inject, value=value,
    )
    forms = secret_forms(value)
    try:
      status, reason, headers, payload = self._exchange(request, body)
    except VaultError as exc:
      # A 403 here is the destination check (non-public address), a refusal
      # like any other; anything else is the site failing to answer.
      outcome = "denied" if exc.status == 403 else "failed"
      self.vault.log_usage(
        name, caller_label, method, host, raw_path, outcome,
        status=exc.status, reason=exc.reason,
        ms=int((time.monotonic() - started) * 1000),
      )
      error = "denied" if exc.status == 403 else "upstream"
      self._send_json(exc.status, {"error": error, "reason": exc.reason}, denied=True)
      return
    payload = redact(payload, forms)
    self.vault.log_usage(
      name, caller_label, method, host, raw_path, "used",
      status=status, ms=int((time.monotonic() - started) * 1000),
    )
    self.send_response(status, reason)
    for header, header_value in headers:
      if header.lower() in HOP_BY_HOP or header.lower() == "content-length":
        continue
      cleaned = redact(header_value.encode("latin-1", "replace"), forms).decode("latin-1")
      self.send_header(header, cleaned)
    self.send_header("Content-Length", str(len(payload)))
    self.send_header("Connection", "close")
    self.send_header("X-Vault-Key", name)
    self.end_headers()
    if method != "HEAD":
      self.wfile.write(payload)

  def _read_body(self) -> bytes | None:
    if self.headers.get("Transfer-Encoding"):
      raise VaultError(411, "Send a Content-Length; chunked bodies are not supported.")
    length = self.headers.get("Content-Length")
    if not length:
      return None
    try:
      size = int(length)
    except ValueError:
      raise VaultError(400, "Invalid Content-Length.")
    if size < 0 or size > MAX_REQUEST_BYTES:
      raise VaultError(413, "The request body is too large.")
    return self.rfile.read(size)

  def _exchange(self, request: UpstreamRequest, body: bytes | None):
    try:
      connection = self.connect(request.host, UPSTREAM_TIMEOUT_SECS)
      try:
        connection.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
        for header, header_value in request.headers:
          connection.putheader(header, header_value)
        if body is not None:
          connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        # http.client never follows redirects; a 3xx goes back to the caller
        # without the key ever travelling to the new location.
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
          raise VaultError(502, "The response is too large for the key vault.")
        headers, payload = decoded_body(response.getheaders(), payload)
        return response.status, response.reason, headers, payload
      finally:
        connection.close()
    except VaultError:
      raise
    except (OSError, http.client.HTTPException) as exc:
      raise VaultError(502, f"{request.host} could not be reached ({type(exc).__name__})")

  do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle


class VaultServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
  daemon_threads = True


def hold_single_instance_lock(data_dir: Path) -> int:
  """Hold the folder's lock for this process's lifetime, or exit.

  Two vaults on one folder would each load their own copy of the store and
  unlink each other's socket. A vault outliving a restarted server exits
  within seconds; until then the new one exits and is restarted by the
  supervisor's backoff, so it loads the store only after the last write.
  """
  data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
  fd = os.open(data_dir / "vault.lock", os.O_RDWR | os.O_CREAT, 0o600)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    os.close(fd)
    raise SystemExit("key vault: another vault is running on this folder")
  return fd


def serve(data_dir: Path, parent_pid: int | None) -> None:
  hold_single_instance_lock(data_dir)
  vault = Vault(data_dir)
  handler = type("BoundVaultHandler", (VaultHandler,), {"vault": vault})
  socket_path = data_dir / SOCKET_NAME
  for stale in (socket_path, data_dir / "admin.sock"):
    try:
      stale.unlink()
    except FileNotFoundError:
      pass
  previous = os.umask(0o177)
  try:
    server = VaultServer(str(socket_path), handler)
  finally:
    os.umask(previous)
  os.chmod(socket_path, 0o600)

  stop = threading.Event()

  def shutdown(*_args) -> None:
    stop.set()

  signal.signal(signal.SIGTERM, shutdown)
  signal.signal(signal.SIGINT, shutdown)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  print(f"key vault ready on {socket_path}", flush=True)
  # A vault orphaned by a crashed server would hold the socket forever.
  while not stop.wait(2):
    if parent_pid and os.getppid() != parent_pid:
      break
  server.shutdown()
  try:
    socket_path.unlink()
  except FileNotFoundError:
    pass


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--data", required=True, type=Path)
  parser.add_argument("--parent", type=int, default=None)
  args = parser.parse_args(argv)
  serve(args.data, args.parent)


if __name__ == "__main__":
  sys.exit(main())
