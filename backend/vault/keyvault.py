"""Möbius key vault: keeps keys encrypted and lends them to apps by name.

The owner and the owner's agents may read a key. Apps never receive a value:
they send an ordinary HTTPS request through the vault, naming the key and the
destination, and the vault adds the key on the way out.

The vault listens on one Unix socket in its folder (mode 0600). App server
code still runs as the server's own user, so that mode is not yet a boundary
against it (see KEY_VAULT_DESIGN.md). The vault trusts whoever can open that
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
itself, never follows redirects, streams the answer back with the value
redacted, and appends a traffic record that never contains the value. Reads,
changes, grants and requests go to a separate audit log, so a flood of refused
uses cannot push them out.

This program imports nothing from the Möbius server, so the store keeps one
small owner that is easy to review.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import copy
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

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# 2: each app grant carries its own sites ({"id", "sites"}); version 1 stores
# are migrated at load, each app keeping the key's sites it could reach then.
STORE_VERSION = 2
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
# One app may not crowd out the others' requests or flood the owner with asks.
MAX_REQUESTS_PER_APP = 10
MAX_REASON = 300
# The sites one ask may list, as stored: ten asks from one app stay small.
MAX_ASK_SITES_BYTES = 8 * 1024
UPSTREAM_TIMEOUT_SECS = 60
# A borrow ends after this long however steadily the site trickles its answer,
# so a purge waiting for the app's borrows always finishes.
BORROW_DEADLINE_SECS = 300
# Borrows beyond this many at once are answered 429 instead of each holding a
# thread and a buffered body.
MAX_CONCURRENT_BORROWS = 32
# One caller (an app, an agent's chat, the owner) beyond this many borrows at
# once is answered 429 itself, so it cannot take every slot from the others.
MAX_BORROWS_PER_CALLER = 8
# A caller that takes this long to accept the next piece of an answer is cut
# off, so one that stops reading cannot hold its slot until the deadline.
CALLER_SEND_TIMEOUT_SECS = 60
# Synthesizes IPv6 addresses for IPv4 hosts; the embedded IPv4 is the real
# destination, so it must be public too.
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
USAGE_ROTATE_BYTES = 512 * 1024
AUDIT_ROTATE_BYTES = 2 * 1024 * 1024
# Log fields come partly from callers; bounded lines keep the logs useful.
LOG_FIELD_LIMITS = {"key": 80, "caller": 140, "host": 260, "path": 300, "outcome": 120, "reason": 300}
REDACTED = b"[redacted by key vault]"
# The slot may never name a header that frames or routes the request, nor one
# that sites routinely log or echo as ordinary metadata: a key sent there could
# reach whoever reads those logs, out of redaction's sight.
FORBIDDEN_SLOT_HEADERS = frozenset({
  "host", "content-length", "transfer-encoding", "connection", "upgrade",
  "te", "trailer", "keep-alive", "proxy-authorization", "proxy-connection",
  "x-vault-caller", "accept-encoding",
  "user-agent", "referer", "origin", "content-type", "accept", "accept-language",
  "from", "via", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip",
})
HOP_BY_HOP = frozenset({
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "proxy-connection", "te", "trailer", "trailers", "transfer-encoding",
  "upgrade",
})
# Headers some APIs (Google's among them) read as the real method. The site
# rule checks the wire method, so a caller allowed only POST could otherwise
# have the site run PATCH or DELETE with the key.
METHOD_OVERRIDE_HEADERS = frozenset({
  "x-http-method-override", "x-http-method", "x-method-override",
})
# Headers some servers and proxies (IIS, Symfony, Zend) read as the real
# path. The path rule checks the request line, so these would let a grant on
# one path run another with the key.
URL_OVERRIDE_HEADERS = frozenset({"x-original-url", "x-rewrite-url", "x-override-url"})


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
  """Vault names to try for a new app-owned key: always scoped to the app.

  A plain name would let an app plant a key under the name another app later
  asks for, so the owner's approval card would offer the planter's value as
  if it were the owner's own key.
  """
  plain = re.sub(r"[^A-Za-z0-9_-]", "-", secret_name)
  scoped = f"app{int(app_id)}-{plain}"
  yield scoped[:64]
  for n in range(2, 100):
    suffix = f"-{n}"
    yield scoped[:64 - len(suffix)] + suffix


def validate_sites(sites, whose: str) -> list[dict]:
  if sites is None:
    sites = []
  if not isinstance(sites, list) or len(sites) > MAX_SITES:
    raise VaultError(400, f"{whose} can use a key on at most {MAX_SITES} sites.")
  return [validate_site(site) for site in sites]


def validate_grants(raw) -> dict:
  """Who may use a key, and where.

  ``sites`` are where the owner and agents may send it; each app grant has
  its own ``sites``, so approving sites for one app never widens another app
  or the agents.
  """
  if raw is None:
    raw = {}
  if not isinstance(raw, dict):
    raise VaultError(400, "Grants must list apps, agent use and sites.")
  apps = raw.get("apps") or []
  if not isinstance(apps, list) or len(apps) > MAX_LIST:
    raise VaultError(400, "Too many apps.")
  clean_apps = {}
  for grant in apps:
    app_id = grant.get("id") if isinstance(grant, dict) else None
    # Apps are granted by numeric id: names and slugs can be reused.
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
      raise VaultError(400, f"'{app_id if isinstance(grant, dict) else grant}' is not an app id.")
    clean_apps[app_id] = {"id": app_id, "sites": validate_sites(grant.get("sites"), "An app")}
  return {
    "apps": [clean_apps[app_id] for app_id in sorted(clean_apps)],
    "agents": bool(raw.get("agents")),
    "sites": validate_sites(raw.get("sites"), "You and your agents"),
  }


def app_grant(grants: dict, app_id: int) -> dict | None:
  return next((grant for grant in grants.get("apps") or [] if grant["id"] == app_id), None)


def app_ids(grants: dict) -> list[int]:
  return [grant["id"] for grant in grants.get("apps") or []]


def without_app(grants: dict, app_id: int) -> list[dict]:
  return [grant for grant in grants.get("apps") or [] if grant["id"] != app_id]


def widened_apps(before: dict | None, after: dict) -> list[int]:
  """Apps ``after`` lets use the key, or use it somewhere ``before`` did not.

  Compared by what the sites allow, not their text, so removing, narrowing or
  reordering sites never counts as widening.
  """
  widened = []
  for grant in after.get("apps") or []:
    old = app_grant(before, grant["id"]) if before else None
    if old is None or sites_outside(old["sites"], grant["sites"]):
      widened.append(grant["id"])
  return widened


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
    return app_grant(grants, int(ident)) is not None
  return False


def caller_sites(grants: dict, kind: str, ident: str) -> list[dict]:
  """Where this caller may send the key: an app only to its own grant's sites."""
  if kind == "app":
    grant = app_grant(grants, int(ident))
    return grant["sites"] if grant else []
  return grants.get("sites") or []


def normalized_request_path(raw_path: str) -> str:
  """Decode a request path for rule matching, refusing ambiguous forms.

  Dot segments, encoded slashes and backslashes are refused outright: a path
  that means one thing to the rule and another to the server would let a
  narrow grant reach a different endpoint. So are the forms some servers
  decode further than the rule does: a dot segment with path parameters
  (``..;``, which Java servers treat as ``..``), an encoded ``%`` (a second
  decoding pass would reveal ``%2e%2e``) and encoded bytes that are not valid
  UTF-8 (overlong ``%C0%AE`` is a dot to lenient decoders).
  """
  if not raw_path.startswith("/"):
    raise VaultError(400, "The destination path must start with /.")
  if any(not "\x21" <= ch <= "\x7e" for ch in raw_path):
    raise VaultError(400, "Percent-encode spaces and non-ASCII characters in the path.")
  lowered = raw_path.lower()
  if "%2f" in lowered or "%5c" in lowered or "\\" in raw_path or "%00" in lowered:
    raise VaultError(403, "Encoded slashes are not allowed in key requests.")
  if "%25" in lowered:
    raise VaultError(403, "Encoded percent signs are not allowed in key requests.")
  try:
    decoded = urllib.parse.unquote(raw_path, errors="strict")
  except UnicodeDecodeError:
    raise VaultError(403, "Encoded bytes in the path must be valid UTF-8.")
  for segment in decoded.split("/"):
    if segment.split(";", 1)[0] in (".", ".."):
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


def site_allows(sites: list[dict], host: str, method: str, path: str) -> str | None:
  """Return None when some site rule allows the request, else the reason."""
  hosts = [s for s in sites if host_matches(s["host"], host)]
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


# JSON's short escapes and the named HTML entities sites commonly emit.
_JSON_SHORT_ESCAPES = {
  '"': '\\"', "\\": "\\\\", "/": "\\/",
  "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t",
}
_NAMED_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}
# Encoded forms shorter than this (base64, hex) would hide unrelated text.
MIN_ENCODED_FORM = 8


def _char_echoes(ch: str) -> list[bytes]:
  """Every way an escaping encoder writes one character, longest first."""
  raw = ch.encode()
  units = ch.encode("utf-16-be")
  forms = {
    raw,
    "".join(f"%{byte:02x}" for byte in raw).encode(),
    "".join(f"\\u{int.from_bytes(units[i:i + 2], 'big'):04x}" for i in range(0, len(units), 2)).encode(),
    f"&#{ord(ch)};".encode(),
    f"&#x{ord(ch):x};".encode(),
  }
  if ch == " ":
    forms.add(b"+")
  if ch in _JSON_SHORT_ESCAPES:
    forms.add(_JSON_SHORT_ESCAPES[ch].encode())
  if ch in _NAMED_ENTITIES:
    forms.add(_NAMED_ENTITIES[ch].encode())
  return sorted(forms, key=len, reverse=True)


def _base64_cores(data: bytes) -> set[bytes]:
  """The base64 text ``data`` encodes to wherever it starts in a larger blob.

  Each of the three byte alignments gives different text; the characters that
  also carry neighbouring bytes' bits are left out, so the core is the same
  whatever surrounds the key (``Basic`` credentials, JSON, a token).
  """
  cores = set()
  for offset in range(3):
    encoded = base64.b64encode(b"\0" * offset + data + b"\0\0")
    core = encoded[-(-8 * offset // 6):8 * (offset + len(data)) // 6]
    cores |= {core, core.translate(bytes.maketrans(b"+/", b"-_"))}
  return {core for core in cores if len(core) >= MIN_ENCODED_FORM}


@dataclass(frozen=True)
class SecretEchoes:
  """What an echo of one key can look like, and how long such an echo can be."""
  pattern: re.Pattern
  longest: int


def secret_echoes(value: str) -> SecretEchoes:
  """Match the key wherever a site echoes it, however the site escaped it.

  Escaping is matched per character, so mixed and partial escaping is caught
  too: raw, percent-encoding, JSON ``\\u`` and short escapes, HTML numeric and
  named entities, ``+`` for a space. Letters match in either case, which also
  catches hex digits written either way and an upper-cased echo. Hex, UTF-16
  (either byte order) and base64 at every alignment cover a key the site
  re-encoded whole.
  """
  per_char, longest, groups = [], 0, {}
  for ch in value:
    if ch not in groups:
      forms = _char_echoes(ch)
      groups[ch] = (b"(?:" + b"|".join(re.escape(form) for form in forms) + b")", len(forms[0]))
    group, size = groups[ch]
    per_char.append(group)
    longest += size
  data = value.encode()
  whole = [data.hex().encode(), value.encode("utf-16-le"), value.encode("utf-16-be")]
  whole = [form for form in whole if len(form) >= MIN_ENCODED_FORM]
  cores = _base64_cores(data)
  alternatives = [b"(?i:" + b"".join(per_char) + b")", *(b"(?i:" + re.escape(form) + b")" for form in whole),
                  *(re.escape(core) for core in sorted(cores, key=len, reverse=True))]
  longest = max([longest, *map(len, whole), *map(len, cores)])
  return SecretEchoes(re.compile(b"|".join(alternatives)), longest)


def redact(data: bytes, echoes: SecretEchoes) -> bytes:
  return echoes.pattern.sub(REDACTED, data)


class StreamRedactor:
  """Redacts a body that arrives in pieces, exactly as ``redact`` would whole.

  An echo can straddle two pieces, so the last ``longest - 1`` bytes of what
  has arrived always wait for the next piece (or the end), whatever they
  contain: holding back only text that could begin the key would let a site
  echoing a caller's guesses reveal, by when each guess arrives, which ones
  begin the key. Everything before that tail is final and goes out at once.
  """

  def __init__(self, echoes: SecretEchoes) -> None:
    self.echoes = echoes
    self.hold = max(echoes.longest - 1, 0)
    self.pending = b""

  def feed(self, data: bytes) -> bytes:
    data = self.pending + data
    cut = len(data) - self.hold
    if cut <= 0:
      self.pending = data
      return b""
    # From any start before ``cut`` the longest echo fits in what has arrived,
    # so these matches are the ones whole-body redaction would find.
    out, position = [], 0
    for match in self.echoes.pattern.finditer(data):
      if match.start() >= cut:
        break
      out += [data[position:match.start()], REDACTED]
      position = match.end()
    end = max(position, cut)
    out.append(data[position:end])
    self.pending = data[end:]
    return b"".join(out)

  def finish(self) -> bytes:
    out, self.pending = redact(self.pending, self.echoes), b""
    return out


class Inflater:
  """Decodes a response body piece by piece, so redaction sees what the caller will.

  The vault asks for identity encoding, but a site may compress anyway, and a
  compressed echo of the key would slip past redaction only to be inflated by
  the caller. gzip and deflate are decoded (bounded like any response); any
  other encoding is refused before the answer starts, and so is any transfer
  coding but plain ``chunked``, which the HTTP client would otherwise pass on
  undecoded.
  """

  def __init__(self, headers: list[tuple[str, str]]) -> None:
    transfer = [value.strip().lower() for name, value in headers if name.lower() == "transfer-encoding"]
    if transfer and transfer != ["chunked"]:
      raise VaultError(502, f"The site answered with an unsupported transfer encoding ({', '.join(transfer)}).")
    encodings = [
      token.strip().lower()
      for name, value in headers if name.lower() == "content-encoding"
      for token in value.split(",") if token.strip()
    ]
    encodings = [token for token in encodings if token != "identity"]
    if len(encodings) > 1 or (encodings and encodings[0] not in ("gzip", "x-gzip", "deflate")):
      raise VaultError(502, f"The site answered with an unsupported encoding ({', '.join(encodings)}).")
    self.encoding = encodings[0] if encodings else None
    self.headers = [(name, value) for name, value in headers if name.lower() != "content-encoding"]
    self._inflater = None
    self._head = b""
    self._total = 0

  def _bounded(self, data: bytes) -> bytes:
    self._total += len(data)
    if self._total > MAX_RESPONSE_BYTES:
      raise VaultError(502, "The response is too large for the key vault.")
    return data

  def _start(self, data: bytes):
    if self.encoding != "deflate":
      return zlib.decompressobj(47)  # gzip or zlib framing
    # "deflate" arrives zlib-framed or raw; a zlib header is two bytes whose
    # value is a multiple of 31 and names method 8.
    zlib_framed = len(data) >= 2 and data[0] & 0x0F == 8 and (data[0] * 256 + data[1]) % 31 == 0
    return zlib.decompressobj(15 if zlib_framed else -15)

  def _inflate(self, data: bytes) -> bytes:
    try:
      out = self._inflater.decompress(data, MAX_RESPONSE_BYTES - self._total + 1)
    except zlib.error:
      raise VaultError(502, "The site sent a response that could not be decompressed.")
    if self._inflater.unconsumed_tail:
      raise VaultError(502, "The response is too large for the key vault.")
    return self._bounded(out)

  def feed(self, data: bytes) -> bytes:
    if self.encoding is None or not data:
      return self._bounded(data)
    if self._inflater is None:
      self._head += data
      if len(self._head) < 2:
        return b""
      data, self._head = self._head, b""
      self._inflater = self._start(data)
    return self._inflate(data)

  def finish(self) -> bytes:
    """The rest of the body; a compressed body that stops short is an error."""
    out = b""
    if self._inflater is None:
      if not self._head:
        return b""
      self._inflater = self._start(self._head)
      out = self._inflate(self._head)
    out += self._bounded(self._inflater.flush())
    if not self._inflater.eof:
      raise VaultError(502, "The site's compressed answer ended early.")
    return out


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
    # An obsolete folded line reaches here as a value spanning lines; sent
    # on, the site could read its continuation as a header of its own.
    if "\r" in header_value or "\n" in header_value:
      raise VaultError(400, f"The {name} header is folded over several lines; send it on one line.")
    if (
      lowered in HOP_BY_HOP or lowered in METHOD_OVERRIDE_HEADERS or lowered in URL_OVERRIDE_HEADERS
      # A ranged answer could carry an echoed key in slices that redaction,
      # which matches the whole value, would never recognise.
      or lowered in ("host", "content-length", "accept-encoding", "range", "if-range")
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
  """The encrypted key store, its grants, its counters and its logs.

  Two logs, neither holding a value: ``usage.jsonl`` records traffic through
  the use path (any caller can make a lot of it), ``audit.jsonl`` records
  reads, changes, grants and requests (only trusted callers or capped asks
  make those). Counters live in ``stats.json`` so they never depend on how
  much log is left.

  Every change goes through ``_change``: it takes effect only once written,
  so the vault never acts on a grant, a deletion or a purge it would lose at
  restart.
  """

  def __init__(self, data_dir: Path) -> None:
    self.dir = data_dir
    self.store_path = data_dir / "keys.json"
    self.master_path = data_dir / "master.key"
    self.usage_path = data_dir / "usage.jsonl"
    self.audit_path = data_dir / "audit.jsonl"
    self.stats_path = data_dir / "stats.json"
    self.requests_path = data_dir / "requests.json"
    self._lock = threading.RLock()
    self._changing = False
    self._audit_after_save: list[dict] | None = None  # a list while a change runs
    self._last_stamp_ms = 0
    self._prepare_folder()
    self._aead = AESGCM(self._load_master_key())
    records, migrated = self._load_records()
    if migrated:
      self._write_store(records)
    self._records = records
    self._refuse_a_master_key_that_opens_nothing()
    self._requests = self._load_requests()
    self._stats = self._load_stats()
    if not self.stats_path.exists():
      self._write_stats(self._stats)

  def _prepare_folder(self) -> None:
    # Reasserted at every start instead of trusting what is found.
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

  def _refuse_a_master_key_that_opens_nothing(self) -> None:
    """A master key from another store would start cleanly, then fail every read and borrow."""
    if self._records and not any(self._opens(name, record) for name, record in self._records.items()):
      raise SystemExit("key vault: the master key opens none of the stored keys; refusing to start")

  def _load_records(self) -> tuple[dict, bool]:
    """The stored keys, and whether an older store was migrated."""
    if not self.store_path.exists():
      return {}, False
    payload = json.loads(self.store_path.read_text())
    records = payload.get("keys") or {}
    if payload.get("version") == 1:
      return {name: _migrated_from_version_one(name, record) for name, record in records.items()}, True
    if payload.get("version") != STORE_VERSION:
      raise SystemExit("key vault: unknown store version; refusing to start")
    return records, False

  def _load_requests(self) -> dict:
    """Open requests. A damaged file is set aside rather than stopping the vault:
    losing pending asks only means their apps ask again."""
    if not self.requests_path.exists():
      return {}
    try:
      payload = json.loads(self.requests_path.read_text())
      requests = (payload.get("requests") or {}) if isinstance(payload, dict) else None
      if not isinstance(requests, dict) or not all(map(_is_stored_request, requests.values())):
        raise ValueError("unexpected shape")
    except ValueError as exc:
      aside = self.requests_path.with_name(f"{self.requests_path.name}.damaged-{int(time.time())}")
      os.replace(self.requests_path, aside)
      print(f"key vault: {self.requests_path.name} is damaged ({type(exc).__name__}); moved it to "
            f"{aside.name} and started with no open requests", file=sys.stderr, flush=True)
      return {}
    return requests

  def _load_stats(self) -> dict:
    if self.stats_path.exists():
      return json.loads(self.stats_path.read_text()).get("keys") or {}
    # A store from before counters existed: count what its log still holds once.
    stats: dict[str, dict] = {}
    for entry in reversed(self.usage(limit=None)):
      if entry.get("key") in self._records:
        _count(stats.setdefault(entry["key"], _empty_stats()), entry["outcome"], entry["ts"])
    return stats

  def _write_store(self, records: dict) -> None:
    payload = {"version": STORE_VERSION, "keys": records}
    _atomic_write(self.store_path, json.dumps(payload, indent=1, sort_keys=True).encode())

  def _write_requests(self, requests: dict) -> None:
    _atomic_write(self.requests_path, json.dumps({"requests": requests}, indent=1, sort_keys=True).encode())

  def _write_stats(self, stats: dict) -> None:
    _atomic_write(self.stats_path, json.dumps({"keys": stats}, sort_keys=True).encode())

  @contextlib.contextmanager
  def _change(self):
    """Change keys, requests and counters; the change takes effect once written.

    The body edits copies under the lock (no reader sees them before then).
    Each file whose part changed is then written atomically; a part whose
    write fails (a full disk) is put back as it is on disk, and the error
    goes to the caller, so retrying the change really saves it. A refusal
    raised by the body discards the whole change. A change made inside
    another joins it.

    The body audits itself with ``_audit_change``; those lines are appended
    only once the outermost change is saved, so a failed write never leaves a
    line for a change that did not happen. A line that then fails to append
    is reported on stderr: the change did take effect, and the caller hears so.
    """
    with self._lock:
      if self._changing:
        yield
        return
      before = {"_records": self._records, "_requests": self._requests, "_stats": self._stats}
      for attr, state in before.items():
        setattr(self, attr, copy.deepcopy(state))
      self._changing = True
      self._audit_after_save = audit = []
      try:
        try:
          yield
        except BaseException:
          for attr, state in before.items():
            setattr(self, attr, state)
          raise
        parts = (("_records", self._write_store), ("_requests", self._write_requests),
                 ("_stats", self._write_stats))
        for index, (attr, write) in enumerate(parts):
          if getattr(self, attr) == before[attr]:
            continue
          try:
            write(getattr(self, attr))
          except BaseException:
            for unwritten, _ in parts[index:]:
              setattr(self, unwritten, before[unwritten])
            raise
      finally:
        self._changing = False
        self._audit_after_save = None
      for fields in audit:
        try:
          self._append(self.audit_path, AUDIT_ROTATE_BYTES, fields)
        except OSError as exc:
          print(f"key vault: saved, but could not audit {fields['outcome']!r} on {fields['key']!r} "
                f"by {fields['caller']!r} ({type(exc).__name__})", file=sys.stderr, flush=True)

  def _audit_change(self, key: str, caller: str, outcome: str, *, reason: str | None = None) -> None:
    """Audit what the running ``_change`` does, once it is saved."""
    self._audit_after_save.append(_audit_fields(key, caller, outcome, reason=reason))

  def _seal(self, name: str, value: str) -> dict:
    nonce = secrets.token_bytes(12)
    sealed = self._aead.encrypt(nonce, value.encode(), name.encode())
    return {"nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode()}

  def _open(self, name: str, record: dict) -> str:
    try:
      nonce = base64.b64decode(record["nonce"])
      sealed = base64.b64decode(record["sealed"])
      return self._aead.decrypt(nonce, sealed, name.encode()).decode()
    except (InvalidTag, binascii.Error, KeyError, TypeError, ValueError):
      raise VaultError(500, "This key can't be decrypted.")

  def _opens(self, name: str, record: dict) -> bool:
    try:
      self._open(name, record)
    except VaultError:
      return False
    return True

  # -- owner operations ----------------------------------------------------

  def list_keys(self) -> list[dict]:
    with self._lock:
      records = {name: dict(record) for name, record in self._records.items()}
      stats = {name: dict(item) for name, item in self._stats.items()}
    return [
      {
        "name": name,
        "inject": record["inject"],
        "grants": record["grants"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "revision": record.get("revision", 0),
        "origin": record.get("origin"),
        "multiline": bool(record.get("multiline")),
        **stats.get(name, _empty_stats()),
      }
      for name, record in sorted(records.items())
    ]

  def put_key(self, name: str, body: dict, *, actor: str = "owner") -> dict:
    """Create or update a key.

    An update changes only what it names: an omitted ``inject`` or ``grants``
    keeps the key's current one, so replacing a value never withdraws who may
    use it. A new key needs a value and where it is sent.

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

    ``actor`` is who made the change, as the server attested it, for the audit
    log. An agent may set keys up and take access away, but switching agent
    use back on and letting an app use a key, or reach more sites, are the
    owner's own decisions; the server refuses them too.
    """
    validate_name(name)
    origin = validate_origin(body["origin"]) if body.get("origin") is not None else None
    with self._change():
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
        value = validate_value(body["value"])
        # Marked so the owner sees it, and the platform never puts it in a header.
        sealed = {**self._seal(name, value), "multiline": "\n" in value or "\r" in value}
      elif existing is not None:
        sealed = {"nonce": existing["nonce"], "sealed": existing["sealed"],
                  "multiline": bool(existing.get("multiline"))}
      else:
        raise VaultError(400, "A new key needs a value.")
      if body.get("inject") is not None:
        inject = validate_inject(body["inject"])
      elif existing is not None:
        inject = existing["inject"]
      else:
        raise VaultError(400, "A new key needs where it is sent: a header or a query parameter.")
      if body.get("grants") is not None:
        grants = validate_grants(body["grants"])
      else:
        grants = copy.deepcopy(existing["grants"]) if existing else validate_grants(None)
      if actor.split(":", 1)[0] == "agent":
        _refuse_an_agent_widening(existing, grants)
      kept_origin = existing.get("origin") if existing else origin
      if existing is not None and body.get("value") is not None and origin != kept_origin:
        if kept_origin:
          grants["apps"] = without_app(grants, kept_origin["app"])
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
      # A request is settled once its app may reach every site it asked for.
      for rid in [rid for rid, item in self._requests.items()
                  if item["key"] == name and _sites_covered(app_grant(grants, item["app"]), item["sites"])]:
        del self._requests[rid]
      self._audit_change(name, actor, "added" if existing is None else "updated")
    return {"name": name, "created": existing is None}

  def _owned_name(self, origin: dict) -> str | None:
    """The key the app in ``origin`` created for that secret and still holds."""
    return next(
      (n for n, r in self._records.items()
       if r.get("origin") == origin and origin["app"] in app_ids(r["grants"])),
      None,
    )

  def save_owned(self, app_id: int, secret_name: str, value: str, sites=None) -> dict:
    """Save an app's own secret by its origin, in one locked step.

    The key ``app_id`` created for ``secret_name`` and still holds is
    replaced, keeping the owner's choices; otherwise a new key is created at
    the first free app-scoped name (``app<id>-<name>``, then numbered), granted to
    the app and the owner's agents, sent as a bearer token, on ``sites`` (none
    unless the server names the app's model-provider site). An app holds at
    most ``MAX_OWNED_PER_APP`` such keys, as in the per-app store. Doing both
    in one change means simultaneous saves can never leave two keys for one
    app secret.
    """
    origin = validate_origin({"app": app_id, "name": secret_name})
    value = validate_value(value)
    with self._change():
      name = self._owned_name(origin)
      if name is not None:
        record = self._records[name]
        self.put_key(name, {"value": value, "inject": record["inject"],
                            "grants": record["grants"], "origin": origin}, actor=f"app:{app_id}")
        return {"name": name, "created": False}
      owned = sum(1 for r in self._records.values()
                  if (r.get("origin") or {}).get("app") == app_id and app_id in app_ids(r["grants"]))
      if owned >= MAX_OWNED_PER_APP:
        raise VaultError(413, f"An app may store at most {MAX_OWNED_PER_APP} secrets.")
      for name in owned_name_candidates(app_id, secret_name):
        if name not in self._records:
          self.put_key(name, {
            "value": value,
            "inject": dict(DEFAULT_INJECT),
            "grants": {"apps": [{"id": app_id, "sites": sites or []}], "agents": True, "sites": sites or []},
            "origin": origin,
            "create_only": True,
          }, actor=f"app:{app_id}")
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
    with self._change():
      name = self._owned_name(origin)
      if name is None:
        return None
      deleted = self._release_locked(name, app_id)
      self._audit_change(name, caller_text(("app", str(app_id))), "revoked" if deleted else "released")
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
    if origin and origin["app"] not in app_ids(record["grants"]):
      del record["origin"]

  def _release_locked(self, name: str, app_id: int) -> bool:
    """Withdraw ``app_id`` from its own key; delete the key unless shared.

    The caller holds a change open. Returns whether the key was deleted.
    """
    record = self._records[name]
    others = without_app(record["grants"], app_id)
    if others:
      record["grants"]["apps"] = others
      self._drop_orphaned_origin(record)
      self._touch(record)
    else:
      del self._records[name]
      self._stats.pop(name, None)
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
    with self._change():
      owned = [n for n, r in self._records.items()
               if (r.get("origin") or {}).get("app") == app_id and app_id in app_ids(r["grants"])]
      deleted = [name for name in owned if self._release_locked(name, app_id)]
      for name in owned:
        self._audit_change(name, "platform", "revoked" if name in deleted else "released",
                           reason=f"app:{app_id} data erased")
    return deleted

  def revoke_key(self, name: str, *, actor: str = "owner") -> bool:
    validate_name(name)
    with self._change():
      if self._records.pop(name, None) is None:
        return False
      self._stats.pop(name, None)
      self._audit_change(name, actor, "revoked")
    return True

  def reveal(self, name: str, caller: tuple[str, str]) -> str:
    """The value itself, for the owner, the owner's agents and the platform.

    Apps are refused here as well as by the server: lending a key to an app
    means using it on the app's behalf, never handing it over. A key whose
    Agents switch is off is kept from agents for reading as well as use.
    """
    validate_name(name)
    if caller[0] == "app":
      self.log_event(name, caller_text(caller), "denied", status=403,
                     reason="apps cannot read key values")
      raise VaultError(403, "Apps cannot read key values; use the key through the vault.")
    with self._lock:
      record = self._records.get(name)
      if record is None:
        raise VaultError(404, f"No key named {name}")
      refused = caller[0] == "agent" and not record["grants"].get("agents")
      value = None if refused else self._open(name, record)
    if refused:
      self.log_event(name, caller_text(caller), "denied", status=403,
                     reason="agents are switched off for this key")
      raise VaultError(403, "Agents are switched off for this key in Settings › Keys.")
    self.log_event(name, caller_text(caller), "revealed")
    return value

  # -- app access requests (the handshake) -----------------------------------

  def request_access(self, app_id: int, body: dict) -> dict:
    """An app asks to use a key; answer granted, pending or denied.

    Asking is idempotent, so an app checks its status by asking again. The
    first ask for a key returns ``created`` so the server tells the owner
    once, never on every retry. A granted app that asks for sites its grant
    does not cover keeps using what it has, is told which sites are missing
    (``sites_not_allowed``), and its ask for them waits for the owner like
    any request (``sites_request``).
    """
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
      raise VaultError(400, "Invalid app id.")
    name = validate_name(body.get("key"))
    reason = str(body.get("reason") or "").strip()[:MAX_REASON]
    sites = body.get("sites") or []
    if not isinstance(sites, list) or len(sites) > MAX_SITES:
      raise VaultError(400, f"Ask for at most {MAX_SITES} sites.")
    sites = [validate_site(site) for site in sites]
    # A wildcard would reach hosts the app's author may own on a shared
    # domain (any *.workers.dev, *.vercel.app, ...), and that host reads the
    # key from the request itself. Only the owner may widen a key that way.
    wildcard = next((site["host"] for site in sites if site["host"].startswith("*.")), None)
    if wildcard is not None:
      raise VaultError(
        400, f"Ask for exact sites such as api.example.com, not {wildcard}; "
        "only the owner can allow every subdomain, on the key's page.",
      )
    if len(json.dumps(sites)) > MAX_ASK_SITES_BYTES:
      raise VaultError(400, "Ask for fewer sites or paths at once.")
    inject = validate_inject(body["inject"]) if body.get("inject") is not None else None
    request_id = f"{app_id}-{name}"
    with self._change():
      record = self._records.get(name)
      grant = app_grant(record["grants"], app_id) if record is not None else None
      existing = self._requests.get(request_id)
      if grant is not None:
        missing = sites_outside(grant["sites"], sites)
        if not missing:
          self._requests.pop(request_id, None)
          return {"status": "granted", "key": name}
        answer = {"status": "granted", "key": name, "id": request_id, "sites_not_allowed": missing}
        if existing is not None:
          return {**answer, "sites_request": existing["status"]}
        sites, inject = missing, None
      elif existing is not None:
        return {"status": existing["status"], "key": name, "id": request_id}
      if sum(1 for item in self._requests.values() if item["app"] == app_id) >= MAX_REQUESTS_PER_APP:
        raise VaultError(429, "This app has too many open key requests; ask the owner to review them.")
      if len(self._requests) >= MAX_REQUESTS:
        raise VaultError(429, "Too many open key requests; ask the owner to review them.")
      self._requests[request_id] = {
        "id": request_id, "app": app_id, "app_name": str(body.get("app_name") or "")[:100],
        "key": name, "reason": reason, "sites": sites, "inject": inject,
        "status": "pending", "created_at": _now(), "ticket": secrets.token_hex(8),
      }
      self._audit_change(name, f"app:{app_id}", "requested")
    if grant is not None:
      return {**answer, "sites_request": "pending", "created": True}
    return {"status": "pending", "key": name, "id": request_id, "created": True}

  def list_requests(self) -> list[dict]:
    """Open requests; ``granted`` marks an app asking for more sites on a key it has.

    ``key_origin`` names the app that saved an existing key for itself, so the
    owner never mistakes an app's planted value for a key of their own.
    """
    with self._lock:
      requests = [dict(item) for item in self._requests.values()]
      for item in requests:
        record = self._records.get(item["key"])
        item["key_exists"] = record is not None
        item["key_origin"] = dict(record["origin"]) if record is not None and record.get("origin") else None
        item["granted"] = record is not None and app_grant(record["grants"], item["app"]) is not None
    return sorted(requests, key=lambda item: item["created_at"], reverse=True)

  def approve_request(self, request_id: str, body: dict, *, actor: str = "owner") -> dict:
    """Grant the app exactly what the owner's answer carries.

    The request only proposes. The sites the answer names go to this app's
    own grant, never to another app or the agents, so approving one app never
    widens anyone else's use. A new key is created with the answer's value and
    slot (``Authorization: Bearer`` when the answer names none); an existing
    key keeps its value and slot, which its own page changes.

    The answer names the request's ``ticket``: a request id is the app id and
    key name, so a request card left open while its app was purged would
    otherwise approve the same ask from an unrelated app that reused the id.

    Only the owner's own answer approves (``actor`` ``owner``); the server
    refuses everyone else too.
    """
    if actor != "owner":
      raise VaultError(403, "Only the owner can let an app use a key, from Settings › Keys.")
    with self._change():
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      if body.get("ticket") != request.get("ticket"):
        raise VaultError(409, "That request changed since it was shown; review it again.")
      name = request["key"]
      sites = validate_sites(body.get("sites"), "An app")
      existing = self._records.get(name)
      if existing is not None:
        if any(body.get(field) is not None for field in ("value", "inject")):
          raise VaultError(
            409, f"{name} already exists, and approving only lets the app use it. "
            "Change the key itself on its page.",
          )
        grants, inject = existing["grants"], existing["inject"]
      else:
        grants = {"apps": [], "agents": False, "sites": []}
        inject = body.get("inject") or dict(DEFAULT_INJECT)
      grant = app_grant(grants, request["app"]) or {"id": request["app"], "sites": []}
      merged = {(s["host"], tuple(s["methods"]), tuple(s["paths"])): s for s in grant["sites"]}
      for site in sites:
        merged.setdefault((site["host"], tuple(site["methods"]), tuple(site["paths"])), site)
      grants = {**grants, "apps": [*without_app(grants, request["app"]),
                                   {"id": request["app"], "sites": list(merged.values())}]}
      put = {"grants": grants, "inject": inject}
      if existing is None:
        put["value"] = body.get("value")
      result = self.put_key(name, put, actor=actor)
      self._requests.pop(request_id, None)
      self._audit_change(name, actor, f"granted app:{request['app']}")
    return {**result, "granted": request["app"]}

  def deny_request(self, request_id: str, body: dict | None = None, *, actor: str = "owner") -> dict:
    """Refuse an ask. A ``ticket`` in ``body`` must be the request's, as when
    approving, so a card left open across an id reuse can't deny the new ask."""
    ticket = (body or {}).get("ticket")
    with self._change():
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      if ticket is not None and ticket != request.get("ticket"):
        raise VaultError(409, "That request changed since it was shown; review it again.")
      request["status"] = "denied"
      request["answered_at"] = _now()
      self._audit_change(request["key"], actor, f"denied app:{request['app']}")
    return {"denied": request_id}

  def forget_app(self, app_id: int, *, actor: str = "platform") -> dict:
    """Erase a purged app's own keys and drop it from every grant and request.

    App ids can be reused once an app is purged, so a grant must never outlive
    the app it was given to.
    """
    with self._change():
      deleted = self.erase_owned(app_id)
      keys = [n for n, r in self._records.items() if app_grant(r["grants"], app_id)]
      for name in keys:
        grants = self._records[name]["grants"]
        grants["apps"] = without_app(grants, app_id)
        self._drop_orphaned_origin(self._records[name])
        self._touch(self._records[name])
      requests = [rid for rid, item in self._requests.items() if item["app"] == app_id]
      for rid in requests:
        del self._requests[rid]
      for name in keys:
        self._audit_change(name, actor, f"revoked app:{app_id} (app purged)")
    return {"keys": keys, "deleted": deleted, "requests": len(requests)}

  def forget_request(self, request_id: str, *, actor: str = "owner") -> bool:
    with self._change():
      request = self._requests.pop(request_id, None)
      if request is None:
        return False
      self._audit_change(request["key"], actor, f"forgot request app:{request['app']}")
    return True

  # -- use -----------------------------------------------------------------

  def authorize(self, name: str, caller: tuple[str, str], method: str, host: str, path: str):
    """Return (inject, value) for an allowed use, or raise the denial."""
    with self._lock:
      record = self._records.get(name)
      if record is None:
        # An app without a grant learns nothing, including whether the name exists.
        if caller[0] == "app":
          raise VaultError(403, f"{caller_text(caller)} may not use this key")
        raise VaultError(404, f"No key named {name}" if NAME_RE.fullmatch(name) else "No key by that name")
      if not caller_allowed(record["grants"], *caller):
        raise VaultError(403, f"{caller_text(caller)} may not use this key")
      reason = site_allows(caller_sites(record["grants"], *caller), host, method, path)
      if reason:
        raise VaultError(403, reason)
      value = self._open(name, record)
      if record["inject"]["type"] == "header":
        if any(ch in value for ch in "\r\n"):
          raise VaultError(400, f"{name} has line breaks, so it can't be sent in a header.")
        try:
          (record["inject"].get("prefix", "") + value).encode("latin-1")
        except UnicodeEncodeError:
          raise VaultError(400, f"{name} has characters a header can't carry, so it can't be sent in a header.")
      return dict(record["inject"]), value

  # -- logs and counters (never values) ----------------------------------------

  def _stamp(self) -> str:
    """A log timestamp that is unique and increasing within this process."""
    ms = max(int(time.time() * 1000), self._last_stamp_ms + 1)
    self._last_stamp_ms = ms
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"

  def _append(self, path: Path, rotate_bytes: int, fields: dict) -> dict:
    entry = {
      name: (str(value)[:LOG_FIELD_LIMITS[name]] if name in LOG_FIELD_LIMITS else value)
      for name, value in fields.items() if value is not None
    }
    with self._lock:
      entry["ts"] = self._stamp()
      line = (json.dumps(entry, sort_keys=True) + "\n").encode()
      try:
        if path.stat().st_size + len(line) > rotate_bytes:
          os.replace(path, path.with_suffix(".1.jsonl"))
      except FileNotFoundError:
        pass
      fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
      try:
        os.write(fd, line)
      finally:
        os.close(fd)
    return entry

  def log_traffic(
    self, key: str, caller: str, method: str, host: str, path: str, outcome: str,
    *, status: int | None = None, reason: str | None = None, ms: int | None = None,
  ) -> None:
    """One request through the use path, and the key's counters."""
    entry = self._append(self.usage_path, USAGE_ROTATE_BYTES, {
      "key": key, "caller": caller, "method": method, "host": host, "path": path,
      "outcome": outcome, "status": status, "reason": reason, "ms": ms,
    })
    with self._lock:
      if key not in self._records or outcome not in ("used", "denied"):
        return
      # Counted on a copy and kept only once written, like every change
      # (``_change`` would copy the whole store for one counter).
      stats = {**self._stats, key: dict(self._stats.get(key) or _empty_stats())}
      _count(stats[key], outcome, entry["ts"])
      self._write_stats(stats)
      self._stats = stats

  def log_event(
    self, key: str, caller: str, outcome: str,
    *, status: int | None = None, reason: str | None = None,
  ) -> None:
    """A read or a refusal, kept apart from use traffic (changes use ``_audit_change``)."""
    self._append(self.audit_path, AUDIT_ROTATE_BYTES, _audit_fields(key, caller, outcome, status=status, reason=reason))

  def usage(self, *, key: str | None = None, limit: int | None = 100) -> list[dict]:
    """Newest first, traffic and audit together."""
    entries = []
    for path in (
      self.usage_path.with_suffix(".1.jsonl"), self.usage_path,
      self.audit_path.with_suffix(".1.jsonl"), self.audit_path,
    ):
      try:
        lines = path.read_text().splitlines()
      except FileNotFoundError:
        continue
      for line in lines:
        try:
          entry = json.loads(line)
        except ValueError:
          continue
        if key is None or entry.get("key") == key:
          entries.append(entry)
    entries.sort(key=lambda entry: str(entry.get("ts", "")), reverse=True)
    return entries if limit is None else entries[:max(1, min(limit, 500))]


def _audit_fields(key: str, caller: str, outcome: str, *, status: int | None = None,
                  reason: str | None = None) -> dict:
  return {"key": key, "caller": caller, "method": "-", "host": "-", "path": "-",
          "outcome": outcome, "status": status, "reason": reason}


def _empty_stats() -> dict:
  return {"uses": 0, "denied": 0, "last_used": None}


def _count(item: dict, outcome: str, ts: str) -> None:
  if outcome == "used":
    item["uses"] += 1
    item["last_used"] = ts
  elif outcome == "denied":
    item["denied"] += 1


def _sites_covered(grant: dict | None, wanted: list[dict]) -> bool:
  """Whether an app grant already allows everything in ``wanted``."""
  return grant is not None and not sites_outside(grant["sites"], wanted)


def _refuse_an_agent_widening(existing: dict | None, grants: dict) -> None:
  """Agents may take access away, but giving it is the owner's own decision."""
  if existing is not None and grants["agents"] and not existing["grants"].get("agents"):
    raise VaultError(403, "Only you can let agents use this key, from Settings › Keys.")
  if widened_apps(existing["grants"] if existing else None, grants):
    raise VaultError(403, "Only you can let an app use a key or reach more sites, from Settings › Keys.")


def _migrated_from_version_one(name: str, record: dict) -> dict:
  """A version 1 record with each app keeping exactly the sites it could reach.

  Validated like any new grant (duplicate app ids merge, a missing ``sites``
  is no sites), and an origin whose app no longer holds a grant is dropped,
  as every later change would.
  """
  grants = record.get("grants") or {}
  sites = grants.get("sites") or []
  try:
    migrated = {**record, "grants": validate_grants({
      "apps": [{"id": app_id, "sites": sites} for app_id in grants.get("apps") or []],
      "agents": grants.get("agents"),
      "sites": sites,
    })}
  except VaultError as exc:
    raise SystemExit(f"key vault: version 1 key {name} can't be migrated ({exc.reason}); refusing to start")
  Vault._drop_orphaned_origin(migrated)
  return migrated


def _is_stored_request(item) -> bool:
  return (
    isinstance(item, dict) and isinstance(item.get("app"), int) and isinstance(item.get("key"), str)
    and isinstance(item.get("sites"), list) and isinstance(item.get("status"), str)
    and isinstance(item.get("created_at"), str)
  )


# ---------------------------------------------------------------------------
# The vault socket: one HTTP server on a mode-0600 Unix socket, spoken to by
# the Möbius server, which attests every caller, and same-user owner tools.
# ---------------------------------------------------------------------------

USE_PATH_RE = re.compile(r"^/use/([^/]+)/([^/]+)(/.*)?$")


class FinalAnswer(http.client.HTTPResponse):
  """A site's final answer: interim 1xx answers (103 Early Hints) are skipped.

  http.client skips only 100 Continue itself and would hand a 103 back as the
  answer, leaving the real one unread. This relies on ``_read_status``, which
  ``begin`` calls for each status line.
  """

  MAX_INTERIM = 16

  def _read_status(self):
    for _ in range(self.MAX_INTERIM):
      version, status, reason = super()._read_status()
      if not 100 < status < 200 or status == 101:
        return version, status, reason
      http.client.parse_headers(self.fp)
    raise http.client.HTTPException("The site sent too many interim answers.")


class BorrowDeadline:
  """When a borrow must end, enforced on the site's connection.

  Each wait gets its own timeout (``time_left``), which alone would let a site
  that trickles header or chunk-size lines keep a borrow, and a purge waiting
  for it, open long past the deadline: ``watch`` shuts the site's connection
  down when the deadline passes, and ``stop`` cancels that.
  """

  def __init__(self, started: float) -> None:
    self.at = started + BORROW_DEADLINE_SECS
    self._cut = threading.Event()
    self._timer: threading.Timer | None = None

  @property
  def passed(self) -> bool:
    return self._cut.is_set() or time.monotonic() >= self.at

  def raise_if_passed(self) -> None:
    if self.passed:
      raise self._overrun()

  def time_left(self, limit: float) -> float:
    """The timeout for the next wait, at most ``limit``; 504 once the deadline passed."""
    left = self.at - time.monotonic()
    if self._cut.is_set() or left <= 0:
      raise self._overrun()
    return min(limit, left)

  @staticmethod
  def _overrun() -> VaultError:
    return VaultError(504, f"The site took longer than {BORROW_DEADLINE_SECS} seconds to answer.")

  def watch(self, sock: socket.socket) -> None:
    def cut() -> None:
      self._cut.set()
      # The plain socket's shutdown: an SSL socket's own would drop its TLS
      # state under the thread reading from it. Reads then end at once.
      with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)

    self._timer = threading.Timer(max(0.0, self.at - time.monotonic()), cut)
    self._timer.daemon = True
    self._timer.start()

  def stop(self) -> None:
    if self._timer is not None:
      self._timer.cancel()


class BorrowSlots:
  """Borrows running at once: at most ``per_caller`` for one caller (``app:<id>``,
  ``agent:<chat>``, ``owner``) and ``total`` in all; beyond either, 429."""

  def __init__(self, total: int, per_caller: int) -> None:
    self._lock = threading.Lock()
    self._total = total
    self._per_caller = per_caller
    self._held: dict[str, int] = {}

  def take(self, caller: str) -> None:
    with self._lock:
      if self._held.get(caller, 0) >= self._per_caller:
        raise VaultError(429, f"This caller already has {self._per_caller} requests running through the key vault; try again shortly.")
      if sum(self._held.values()) >= self._total:
        raise VaultError(429, "The key vault is busy with other requests; try again shortly.")
      self._held[caller] = self._held.get(caller, 0) + 1

  def give_back(self, caller: str) -> None:
    with self._lock:
      self._held[caller] -= 1
      if not self._held[caller]:
        del self._held[caller]


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
    self._answer_started = True
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
    self._answer_started = False
    split = urllib.parse.urlsplit(self.path)
    try:
      if split.path.startswith("/use/"):
        self._use(split)
      else:
        self._admin_answer(split)
    except OSError as exc:
      # The store or a log could not be written (a full disk): the change did
      # not take effect, and the caller hears so rather than an empty reply.
      if not self._answer_started:
        with contextlib.suppress(OSError):
          self._send_json(500, {"error": f"The key vault could not write its files ({type(exc).__name__})."})

  # -- owner and handshake operations ----------------------------------------

  def _admin_answer(self, split) -> None:
    try:
      self._admin(split)
    except VaultError as exc:
      self._send_json(exc.status, {"error": exc.reason})
    except ValueError:
      self._send_json(400, {"error": "Invalid request."})

  def _content_length(self) -> int | None:
    raw = self.headers.get("Content-Length")
    if not raw:
      return None
    if not re.fullmatch(r"[0-9]{1,15}", raw.strip()):
      raise VaultError(400, "Invalid Content-Length.")
    return int(raw)

  def _json_body(self) -> dict:
    size = self._content_length() or 0
    if size > MAX_ADMIN_BYTES:
      raise VaultError(413, "Request too large.")
    try:
      body = json.loads(self.rfile.read(size) or b"{}")
    except ValueError:
      raise VaultError(400, "Invalid JSON.")
    if not isinstance(body, dict):
      raise VaultError(400, "Invalid JSON.")
    return body

  def _manager(self) -> str:
    """Who is changing the store, as the server attested it; never an app.

    Every change names its caller so the audit log records who made it.
    """
    caller = parse_caller(self.headers.get("X-Vault-Caller"))
    if caller[0] == "app":
      raise VaultError(403, "Apps cannot change keys.")
    return caller_text(caller)

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
      self._send_json(200, vault.put_key(parts[1], self._json_body(), actor=self._manager()))
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "keys":
      if not vault.revoke_key(parts[1], actor=self._manager()):
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
      self._send_json(200, vault.approve_request(parts[1], self._json_body(), actor=self._manager()))
    elif method == "POST" and len(parts) == 3 and parts[0] == "requests" and parts[2] == "deny":
      self._send_json(200, vault.deny_request(parts[1], self._json_body(), actor=self._manager()))
    elif method == "POST" and parts == ["owned"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id and its secret name.")
      self._send_json(200, vault.save_owned(
        body["app"], body["name"], body.get("value"), sites=body.get("sites"),
      ))
    elif method == "POST" and parts == ["owned", "release"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id and its secret name.")
      released = vault.release_owned(body["app"], body["name"])
      if released is None:
        raise VaultError(404, "That app owns no such key.")
      self._send_json(200, released)
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "apps" and parts[1].isdigit():
      self._send_json(200, vault.forget_app(int(parts[1]), actor=self._manager()))
    elif method == "DELETE" and len(parts) == 3 and parts[0] == "apps" and parts[1].isdigit() and parts[2] == "owned":
      self._send_json(200, {"deleted": vault.erase_owned(int(parts[1]))})
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "requests":
      if not vault.forget_request(parts[1], actor=self._manager()):
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
    # The log names a key only by a valid key name, never by the caller's text.
    logged_name = name if NAME_RE.fullmatch(name) else "(invalid name)"
    host = match.group(2).lower()
    raw_path = match.group(3) or "/"
    method = self.command
    caller_label = "unknown"
    started = time.monotonic()
    deadline = BorrowDeadline(started)
    borrowing = False
    try:
      try:
        caller = parse_caller(self.headers.get("X-Vault-Caller"))
        caller_label = caller_text(caller)
        self.server.borrows.take(caller_label)
        borrowing = True
        if not HOST_RE.fullmatch(host) or host.startswith("*."):
          raise VaultError(400, "The destination must be a site name like api.example.com (HTTPS, port 443).")
        path = normalized_request_path(raw_path)
        body = self._read_body()
        inject, value = self.vault.authorize(name, caller, method, host, path)
        request = build_upstream_request(
          method=method, host=host, raw_path=raw_path, raw_query=split.query,
          caller_headers=list(self.headers.items()), inject=inject, value=value,
        )
      except VaultError as exc:
        refused = exc.status < 500 and exc.status != 429
        self.vault.log_traffic(
          logged_name, caller_label, method, host, raw_path, "denied" if refused else "failed",
          status=exc.status, reason=exc.reason,
        )
        self._send_json(exc.status, {"error": "denied", "reason": exc.reason}, denied=True)
        return
      self._relay(logged_name, caller_label, request, body, secret_echoes(value), started, deadline)
    finally:
      deadline.stop()
      if borrowing:
        self.server.borrows.give_back(caller_label)

  def _relay(self, name, caller_label, request: UpstreamRequest, body, echoes, started, deadline: BorrowDeadline) -> None:
    method, host, raw_path = request.method, request.host, request.target.split("?", 1)[0]
    connection = None
    try:
      connection, sock, response = self._open_upstream(request, body, deadline)
      inflater = Inflater(response.getheaders())
    except VaultError as exc:
      if connection is not None:
        connection.close()
      # A 403 here is the destination check (non-public address) and a 400 a
      # request HTTP can't carry, refusals like any other; anything else is
      # the site failing to answer.
      refused = exc.status in (400, 403)
      self.vault.log_traffic(
        name, caller_label, method, host, raw_path, "denied" if refused else "failed",
        status=exc.status, reason=exc.reason,
        ms=int((time.monotonic() - started) * 1000),
      )
      self._send_json(exc.status, {"error": "denied" if refused else "upstream", "reason": exc.reason}, denied=True)
      return

    def record(failure: str | None) -> None:
      self.vault.log_traffic(
        name, caller_label, method, host, raw_path, "failed" if failure else "used",
        status=response.status, reason=failure,
        ms=int((time.monotonic() - started) * 1000),
      )

    try:
      self._stream_answer(name, response, inflater, echoes, record, sock, deadline)
    finally:
      connection.close()

  def _stream_answer(self, name, response, inflater: Inflater, echoes, record, sock, deadline: BorrowDeadline) -> None:
    """Relay the site's answer as it arrives, recording how it ended.

    Server events and streamed model output reach the caller as the site sends
    them. The record is written before the answer completes, so a caller that
    has the whole answer can already find it. Once the status is sent a
    failure can no longer become an error answer, so the vault ends the
    connection without the closing chunk and the caller sees an incomplete
    body rather than a complete-looking one: so does an answer that stops
    short of its Content-Length, or outlasts the borrow's deadline.

    The status goes out with its standard reason phrase and the site's
    ``X-Vault-*`` headers are dropped: neither may speak for the vault.
    """
    has_body = self.command != "HEAD" and response.status not in (204, 304) and response.status >= 200
    self._answer_started = True
    self.connection.settimeout(CALLER_SEND_TIMEOUT_SECS)
    self.send_response(response.status)
    for header, header_value in inflater.headers:
      lowered = header.lower()
      if lowered in HOP_BY_HOP or lowered == "content-length" or lowered.startswith("x-vault-"):
        continue
      cleaned = redact(header_value.encode("latin-1", "replace"), echoes).decode("latin-1")
      self.send_header(header, cleaned)
    self.send_header("Transfer-Encoding" if has_body else "Content-Length", "chunked" if has_body else "0")
    self.send_header("Connection", "close")
    self.send_header("X-Vault-Key", name)
    if not has_body:
      record(None)
      self.end_headers()
      return
    self.end_headers()
    redactor = StreamRedactor(echoes)

    def send(data: bytes) -> None:
      if data:
        self.connection.settimeout(deadline.time_left(CALLER_SEND_TIMEOUT_SECS))
        try:
          self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
          self.wfile.flush()
        except TimeoutError:
          raise VaultError(504, "The caller stopped reading the answer.") from None

    try:
      # http.client closes the socket itself once a sized answer is complete.
      while not response.isclosed():
        sock.settimeout(deadline.time_left(UPSTREAM_TIMEOUT_SECS))
        piece = response.read1(64 * 1024)
        if not piece:
          break
        send(redactor.feed(inflater.feed(piece)))
      if response.length:
        raise http.client.IncompleteRead(b"", response.length)
      # A site cut off at the deadline reads as an answer that simply ended.
      deadline.raise_if_passed()
      send(redactor.feed(inflater.finish()))
      send(redactor.finish())
    except VaultError as exc:
      record(exc.reason)
      return
    except (OSError, http.client.HTTPException) as exc:
      late = deadline.passed
      record(f"The answer took longer than {BORROW_DEADLINE_SECS} seconds" if late
             else f"The answer broke off ({type(exc).__name__})")
      return
    record(None)
    try:
      self.wfile.write(b"0\r\n\r\n")
      self.wfile.flush()
    except OSError:
      pass

  def _read_body(self) -> bytes | None:
    if self.headers.get("Transfer-Encoding"):
      raise VaultError(411, "Send a Content-Length; chunked bodies are not supported.")
    size = self._content_length()
    if size is None:
      return None
    if size > MAX_REQUEST_BYTES:
      raise VaultError(413, "The request body is too large.")
    return self.rfile.read(size)

  def _open_upstream(self, request: UpstreamRequest, body: bytes | None, deadline: BorrowDeadline):
    """Send the request; return the open connection, its socket and the answer's head."""
    connection = None
    try:
      connection = self.connect(request.host, deadline.time_left(UPSTREAM_TIMEOUT_SECS))
      connection.response_class = FinalAnswer
      connection.connect()
      deadline.watch(connection.sock)
      connection.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
      for header, header_value in request.headers:
        connection.putheader(header, header_value)
      if body is not None:
        connection.putheader("Content-Length", str(len(body)))
      connection.endheaders(body)
      # Kept for read timeouts: http.client lets go of it once the answer
      # owns the connection.
      sock = connection.sock
      # http.client never follows redirects; a 3xx goes back to the caller
      # without the key ever travelling to the new location.
      response = connection.getresponse()
      # A head cut off at the deadline may parse as a complete one.
      deadline.raise_if_passed()
      return connection, sock, response
    except VaultError:
      if connection is not None:
        connection.close()
      raise
    except (OSError, http.client.HTTPException) as exc:
      if connection is not None:
        connection.close()
      deadline.raise_if_passed()
      raise VaultError(502, f"{request.host} could not be reached ({type(exc).__name__})")
    except ValueError:
      # Text HTTP can't carry (a non-ASCII query, a header value outside
      # Latin-1) is refused by http.client before anything is sent.
      if connection is not None:
        connection.close()
      raise VaultError(400, "The request has characters HTTP can't carry; percent-encode them.")

  do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle


class VaultServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
  daemon_threads = True

  def __init__(self, *args, **kwargs) -> None:
    super().__init__(*args, **kwargs)
    self.borrows = BorrowSlots(MAX_CONCURRENT_BORROWS, MAX_BORROWS_PER_CALLER)


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
