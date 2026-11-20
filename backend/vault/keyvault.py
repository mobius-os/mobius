"""Möbius key vault: keeps keys encrypted and lends them to apps by name.

The owner and the owner's agents may read a key. Apps never receive a value:
they send an ordinary HTTPS request through the vault, naming the key and the
destination, and the vault adds the key on the way out.

The vault listens on one Unix socket in its folder (mode 0600). App server
code still runs as the server's own user, so that mode is not yet a boundary
against it (see KEY_VAULT_DESIGN.md). The vault trusts whoever can open that
socket: the Möbius server, owner tools running as the same user such as
``store-key.py``, and anything else with that user's rights; any of them can
state any caller. The server authenticates every caller with its own
platform-issued token and passes the attested identity in ``X-Vault-Caller``
(the owner-only rules and notifications are the server routes'):

    app:<app-id>:<install-id> | agent:<chat-id> | owner | platform:<purpose>

An app is named by its id and its installation: SQLite reuses app ids, so a
grant, a request or a key's origin names both, and matches an app caller only
when both match.

    METHOD /use/<key>/<host>/<path>?<query>    use a key (apps, agents, owner)
    GET    /keys/<key>/value                   reveal (never for app callers)
    POST   /requests                           an app or an agent asks to use a key
    POST   /requests/<id>/approve | /deny      the owner answers
    DELETE /requests/<id>                      the owner (or the platform) removes one
    DELETE /chats/<chat-id>/requests?before=<time>
                                               a deleted chat's waiting asks go, those
                                               it made up to its deletion
    POST   /apps/installs                      the platform names each app's installation

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
import calendar
import contextlib
import copy
import fcntl
import fnmatch
import hashlib
import hmac
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# 2: each app grant carries its own sites ({"id", "sites", "paused"}); version 1
# stores are migrated at load, each app keeping the key's sites it could reach
# then. ``paused`` arrived later within version 2: a grant without it is live.
# 3: each app grant and a key's origin name the app's installation
# (``install``). An older store is migrated at load with ``install`` None
# ("unstamped"), which matches no app caller until the platform names the
# installation (``Vault.adopt_installs``).
# 4: a key holds named parts (``parts``) instead of one sealed value; an older
# value becomes the secret part ``value``, sealed exactly as before. The file
# it migrated from is kept beside the store (``keys.v<version>.json``) for an
# older vault (the recovery fallback, a restore from the image) that can't
# read version 4.
STORE_VERSION = 4
SOCKET_NAME = "vault.sock"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(
  r"^(\*\.)?(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
# An app's installation id (the server's ``apps.install_id``).
INSTALL_RE = re.compile(r"[0-9a-f]{32}")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MIN_VALUE_LENGTH = 1
MAX_VALUE_LENGTH = 8192
# A key's parts: the one most keys have is ``value``; others name themselves.
# Eight matches the fields one sealed owner card can carry.
VALUE_PART = "value"
MAX_PARTS = 8
MAX_PART_LABEL = 80
MAX_SLOTS = 8
MAX_TEMPLATE = 256
TEMPLATE_PART_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]{0,63})\}")
QUERY_NAME_RE = re.compile(r"[A-Za-z0-9_.~-]{1,64}")
BODY_FIELD_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
BODY_FORMATS = {"json": "application/json", "form": "application/x-www-form-urlencoded"}
# Signing recipes: the vault signs the request (AWS) or mints a token from a
# private key (JWT) itself, so neither the secret nor what it makes leaves.
AWS_NAME_RE = re.compile(r"[a-z0-9-]{1,32}")
# What the vault sets on an AWS-signed request; a caller's copy never travels.
AWS_SET_BY_VAULT = ("authorization", "x-amz-date", "x-amz-security-token", "x-amz-content-sha256")
AWS_UNRESERVED = "-_.~"
JWT_ALGS = ("RS256", "ES256")
JWT_CLAIMS = ("iss", "sub", "aud", "scope")
JWT_MAX_CLAIM = 512
JWT_DEFAULT_LIFETIME = 600
JWT_MAX_LIFETIME = 3600
# Where a JWT is exchanged for an access token: fixed by the recipe's kind,
# never named by the caller or the key file.
GOOGLE_TOKEN = ("oauth2.googleapis.com", "/token")
GOOGLE_AUDIENCE = "https://oauth2.googleapis.com/token"
GITHUB_API = "api.github.com"
# GitHub refuses an app JWT that lives longer than ten minutes.
GITHUB_MAX_LIFETIME = 600
MAX_TOKEN_ANSWER = 64 * 1024
# A cached token is renewed this long before it expires.
TOKEN_MARGIN_SECS = 60
MAX_KEYS = 200
# What the per-app secret store allowed; apps' own keys keep that bound.
MAX_OWNED_PER_APP = 16
MAX_SITES = 50
MAX_LIST = 100
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_ADMIN_BYTES = 256 * 1024
MAX_REQUESTS = 200
# One app may not crowd out the others' requests or flood the owner with asks;
# the owner's agents together are held to the same number.
MAX_REQUESTS_PER_APP = 10
# Where an agent refused a key, or a site for it, asks the owner instead.
AGENT_ASK = "the request_key tool"
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
CALLER_SEND_SLICE = 64 * 1024
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


class GrantRefusal(VaultError):
  """A use the key's grants refuse: no such key, caller or site.

  ``logged`` is the reason the owner's usage log records, when the caller is
  told less: a paused app hears what an app without a grant hears.
  """

  def __init__(self, status: int, reason: str, *, logged: str | None = None) -> None:
    super().__init__(status, reason)
    self.logged = logged or reason


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


def _slot_header_name(raw) -> str:
  name = str(raw or "").strip()
  if not TOKEN_RE.fullmatch(name) or name.lower() in FORBIDDEN_SLOT_HEADERS:
    raise VaultError(400, f"The header {name or '(empty)'} can't carry a key.")
  return name


def _part_name(raw, what: str) -> str:
  name = str(raw or "").strip()
  if not NAME_RE.fullmatch(name):
    raise VaultError(400, f"{what} must name one of the key's parts.")
  return name


def _template(raw) -> str:
  template = str(raw or "")
  literal = TEMPLATE_PART_RE.sub("", template)
  if not TEMPLATE_PART_RE.search(template):
    raise VaultError(400, "A header template names at least one part, like Token {secret}.")
  if len(template) > MAX_TEMPLATE or any(ch in literal for ch in "{}\r\n\x00"):
    raise VaultError(400, f"A header template is one line of at most {MAX_TEMPLATE} characters, "
                     "with braces only around part names.")
  try:
    literal.encode("latin-1")
  except UnicodeEncodeError:
    raise VaultError(400, "A header template has characters a header can't carry.")
  return template


def _validate_slot(raw) -> dict:
  if not isinstance(raw, dict):
    raise VaultError(400, "Each slot is a header, a query parameter or a body field.")
  kind = raw.get("type")
  if kind == "header":
    return {"type": "header", "name": _slot_header_name(raw.get("name")), "template": _template(raw.get("template"))}
  if kind == "query":
    name = str(raw.get("name") or "").strip()
    if not QUERY_NAME_RE.fullmatch(name):
      raise VaultError(400, f"The query parameter {name or '(empty)'} can't carry a key.")
    return {"type": "query", "name": name, "part": _part_name(raw.get("part"), "A query slot")}
  if kind == "body":
    fmt = raw.get("format")
    if fmt not in BODY_FORMATS:
      raise VaultError(400, "A body slot's format is json or form.")
    field = str(raw.get("field") or "").strip()
    if not BODY_FIELD_RE.fullmatch(field):
      raise VaultError(400, f"The body field {field or '(empty)'} can't carry a key.")
    return {"type": "body", "format": fmt, "field": field, "part": _part_name(raw.get("part"), "A body slot")}
  raise VaultError(400, "Each slot is a header, a query parameter or a body field.")


def validate_inject(raw) -> dict:
  """How a key is sent: its send recipe.

  ``header`` and ``query`` send the part ``value`` in one slot. ``basic``
  sends two parts as ``Authorization: Basic``. ``slots`` places several parts,
  each in a header (a template naming parts), a query parameter or a field of
  a JSON or form body. Which parts a recipe uses is ``recipe_parts``.
  """
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
    if not QUERY_NAME_RE.fullmatch(name):
      raise VaultError(400, "That query parameter name can't carry a key.")
    return {"type": "query", "name": name}
  if kind == "basic":
    username = _part_name(raw.get("username"), "Basic's username")
    password = _part_name(raw.get("password"), "Basic's password")
    if username == password:
      raise VaultError(400, "Basic's username and password are two different parts.")
    return {"type": "basic", "username": username, "password": password}
  if kind == "slots":
    slots = raw.get("slots")
    if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS:
      raise VaultError(400, f"A key is sent in between one and {MAX_SLOTS} slots.")
    slots = [_validate_slot(slot) for slot in slots]
    headers = [slot["name"].lower() for slot in slots if slot["type"] == "header"]
    queries = [slot["name"] for slot in slots if slot["type"] == "query"]
    bodies = [slot for slot in slots if slot["type"] == "body"]
    if len(set(headers)) != len(headers) or len(set(queries)) != len(queries):
      raise VaultError(400, "Two slots name the same header or query parameter.")
    if len({slot["field"] for slot in bodies}) != len(bodies):
      raise VaultError(400, "Two slots name the same body field.")
    if len({slot["format"] for slot in bodies}) > 1:
      raise VaultError(400, "A key's body slots all use one format: json or form.")
    return {"type": "slots", "slots": slots}
  if kind == "aws_sigv4":
    return _validate_aws(raw)
  if kind == "jwt_bearer":
    return _validate_jwt(raw)
  raise VaultError(400, "A key is sent in a header or a query parameter, as basic, in slots, "
                   "signed for AWS (aws_sigv4), or as a token from a private key (jwt_bearer).")


def _validate_aws(raw: dict) -> dict:
  recipe = {
    "type": "aws_sigv4",
    "access_key_id": _part_name(raw.get("access_key_id"), "The access key id"),
    "secret_access_key": _part_name(raw.get("secret_access_key"), "The secret access key"),
  }
  if raw.get("session_token") is not None:
    recipe["session_token"] = _part_name(raw.get("session_token"), "The session token")
  if len(set(recipe.values())) != len(recipe):
    raise VaultError(400, "An AWS key's id, secret and session token are different parts.")
  for field in ("region", "service"):
    value = str(raw.get(field) or "").strip()
    if not AWS_NAME_RE.fullmatch(value):
      raise VaultError(400, f"Name the AWS {field}, such as {'eu-central-1' if field == 'region' else 's3'}.")
    recipe[field] = value
  return recipe


def _jwt_text(value, what: str) -> str:
  text = str(value).strip()
  if not text or len(text) > JWT_MAX_CLAIM or any(ch in text for ch in "\r\n\x00"):
    raise VaultError(400, f"{what} is one line of at most {JWT_MAX_CLAIM} characters.")
  return text


def _validate_jwt(raw: dict) -> dict:
  """A token signed with the key's private key, with claims fixed here.

  Sent as ``Authorization: Bearer`` itself, or first exchanged at a fixed
  endpoint: ``google`` (a service account; the scope is required and the
  audience is Google's token endpoint) or ``github`` (an app, for one
  installation). The caller never chooses a claim.
  """
  alg = raw.get("alg") or "RS256"
  if alg not in JWT_ALGS:
    raise VaultError(400, "A token is signed RS256 or ES256.")
  claims_raw = raw.get("claims") or {}
  if not isinstance(claims_raw, dict) or set(claims_raw) - set(JWT_CLAIMS):
    raise VaultError(400, f"A token's claims are only {', '.join(JWT_CLAIMS)}.")
  claims = {name: _jwt_text(value, f"The {name} claim") for name, value in claims_raw.items()}
  lifetime = raw.get("lifetime_secs", JWT_DEFAULT_LIFETIME)
  if isinstance(lifetime, bool) or not isinstance(lifetime, int) or not 60 <= lifetime <= JWT_MAX_LIFETIME:
    raise VaultError(400, f"A token lives between 60 and {JWT_MAX_LIFETIME} seconds.")
  recipe = {"type": "jwt_bearer", "private_key": _part_name(raw.get("private_key"), "The private key"),
            "alg": alg, "claims": claims, "lifetime_secs": lifetime}
  if raw.get("key_id") is not None:
    recipe["key_id"] = _jwt_text(raw["key_id"], "The key id")
  exchange = raw.get("exchange")
  if exchange is None:
    if "iss" not in claims:
      raise VaultError(400, "A token sent as it is names its issuer (the iss claim).")
    return recipe
  kind = exchange.get("kind") if isinstance(exchange, dict) else None
  if kind == "google":
    if alg != "RS256" or "scope" not in claims:
      raise VaultError(400, "A Google service account signs RS256 and names its scope.")
    if claims.get("aud", GOOGLE_AUDIENCE) != GOOGLE_AUDIENCE:
      raise VaultError(400, f"A Google service account's audience is {GOOGLE_AUDIENCE}.")
    claims["aud"] = GOOGLE_AUDIENCE
    recipe["exchange"] = {"kind": "google"}
  elif kind == "github":
    installation = str(exchange.get("installation_id") or "").strip()
    if alg != "RS256" or "iss" not in claims or not re.fullmatch(r"[1-9][0-9]{0,19}", installation):
      raise VaultError(400, "A GitHub App signs RS256, names its app (the iss claim) and one installation id.")
    recipe["lifetime_secs"] = min(lifetime, GITHUB_MAX_LIFETIME)
    recipe["exchange"] = {"kind": "github", "installation_id": installation}
  else:
    raise VaultError(400, "A token is exchanged with google or github, or not at all.")
  return recipe


def recipe_parts(inject: dict) -> list[str]:
  """The parts a validated recipe sends, in the order it names them."""
  kind = inject["type"]
  if kind in ("header", "query"):
    return [VALUE_PART]
  if kind == "basic":
    return [inject["username"], inject["password"]]
  if kind == "aws_sigv4":
    return [inject["access_key_id"], inject["secret_access_key"],
            *([inject["session_token"]] if inject.get("session_token") else [])]
  if kind == "jwt_bearer":
    return [inject["private_key"]]
  names = []
  for slot in inject["slots"]:
    names += TEMPLATE_PART_RE.findall(slot["template"]) if slot["type"] == "header" else [slot["part"]]
  return list(dict.fromkeys(names))


def recipe_fits(inject: dict, part_names) -> list[str]:
  """The parts ``inject`` sends that ``part_names`` lacks (empty when it fits)."""
  have = set(part_names)
  return [name for name in recipe_parts(inject) if name not in have]


def validate_part_specs(raw) -> list[dict]:
  """The parts a request proposes for a new key: names, labels and whether
  each is secret, never values (the owner pastes those on the card)."""
  if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PARTS:
    raise VaultError(400, f"A key has between one and {MAX_PARTS} parts.")
  specs = []
  for item in raw:
    if not isinstance(item, dict):
      raise VaultError(400, "Each part names itself and says whether it is secret.")
    spec = _part_meta(item, None)
    # A hint for the card: a key file is pasted into a box, not a line.
    if item.get("multiline") is not None:
      if not isinstance(item["multiline"], bool) or (item["multiline"] and not spec["secret"]):
        raise VaultError(400, f"Only a secret part such as a key file has several lines ({spec['name']}).")
      spec["multiline"] = item["multiline"]
    specs.append(spec)
  if len({spec["name"] for spec in specs}) != len(specs):
    raise VaultError(400, "Two parts have the same name.")
  if not any(spec["secret"] for spec in specs):
    raise VaultError(400, "A key has at least one secret part.")
  return specs


def _part_meta(item: dict, existing: dict | None) -> dict:
  """A part's name, label and secrecy; an existing part keeps what ``item`` omits."""
  name = _part_name(item.get("name"), "A part")
  label = item.get("label")
  label = (existing or {}).get("label", "") if label is None else str(label).strip()
  if len(label) > MAX_PART_LABEL or any(ch in label for ch in "\r\n\x00"):
    raise VaultError(400, f"A part's label is one line of at most {MAX_PART_LABEL} characters.")
  secret = item.get("secret")
  if secret is None:
    secret = (existing or {}).get("secret", True)
  if not isinstance(secret, bool):
    raise VaultError(400, f"Part {name} is secret or not: true or false.")
  return {"name": name, "label": label, "secret": secret}


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


def validate_app_id(raw, what: str = "Invalid app id.") -> int:
  if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
    raise VaultError(400, what)
  return raw


def validate_install(raw, app_id) -> str:
  """An app's installation id, which every app grant, request and origin names."""
  if not isinstance(raw, str) or not INSTALL_RE.fullmatch(raw):
    raise VaultError(400, f"App {app_id} needs its installation id (install).")
  return raw


def validate_origin(raw) -> dict:
  """The app (id and installation) and the app's own secret name a key was created for."""
  if not isinstance(raw, dict):
    raise VaultError(400, "A key's origin names an app id, its installation and its secret name.")
  app_id, name = validate_app_id(raw.get("app"), "Invalid origin app id."), raw.get("name")
  if not isinstance(name, str) or not LEGACY_NAME_RE.fullmatch(name):
    raise VaultError(400, "Invalid origin secret name.")
  return {"app": app_id, "install": validate_install(raw.get("install"), app_id), "name": name}


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
  or the agents. A ``paused`` app keeps its grant and sites but may not use
  the key until it is switched back on. Each app grant names the app's
  ``install``ation as well as its id, and a key grants an app id once.
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
    # Apps are granted by numeric id and installation: names and slugs, and
    # ids alone, can be reused.
    validate_app_id(app_id, f"'{app_id if isinstance(grant, dict) else grant}' is not an app id.")
    paused = grant.get("paused")
    if paused is not None and not isinstance(paused, bool):
      raise VaultError(400, f"App {app_id} is paused or not: true or false.")
    clean_apps[app_id] = {
      "id": app_id, "install": validate_install(grant.get("install"), app_id),
      "sites": validate_sites(grant.get("sites"), "An app"), "paused": bool(paused),
    }
  return {
    "apps": [clean_apps[app_id] for app_id in sorted(clean_apps)],
    "agents": bool(raw.get("agents")),
    "sites": validate_sites(raw.get("sites"), "You and your agents"),
  }


def app_grant(grants: dict, app_id: int, install: str | None) -> dict | None:
  """The grant for app ``app_id`` in installation ``install``, or None.

  A grant not yet stamped with its installation (``install`` None, from a
  store before installations were recorded) names no installation, so it
  matches no app until ``Vault.adopt_installs`` stamps it.
  """
  if install is None:
    return None
  return next((grant for grant in grants.get("apps") or []
               if grant["id"] == app_id and grant.get("install") == install), None)


def origin_holds_grant(record: dict) -> bool:
  """Whether the app that created a key (its ``origin``: id and installation)
  still holds its grant on it, which is what makes the key that app's own."""
  origin = record.get("origin")
  return bool(origin) and any(
    grant["id"] == origin["app"] and grant.get("install") == origin.get("install")
    for grant in record["grants"].get("apps") or []
  )


def without_app(grants: dict, app_id: int) -> list[dict]:
  """Every app grant but app id ``app_id``'s, whatever its installation."""
  return [grant for grant in grants.get("apps") or [] if grant["id"] != app_id]


def widened_apps(before: dict | None, after: dict) -> list[int]:
  """Apps ``after`` lets use the key, or use it somewhere ``before`` did not.

  Compared by what the sites allow, not their text, so removing, narrowing or
  reordering sites never counts as widening. Switching a paused app back on
  does; pausing one only takes use away.
  """
  widened = []
  for grant in after.get("apps") or []:
    old = app_grant(before, grant["id"], grant["install"]) if before else None
    if (old is None or sites_outside(old["sites"], grant["sites"])
        or (old.get("paused") and not grant.get("paused"))):
      widened.append(grant["id"])
  return widened


# ---------------------------------------------------------------------------
# Rules: caller, site, method and path (default deny)
# ---------------------------------------------------------------------------

CALLER_RE = re.compile(r"(app|agent|owner|platform)(?::([A-Za-z0-9_.-]{1,128}))?")
# ``app:<id>:<install>``: an app caller always names its installation.
APP_CALLER_RE = re.compile(r"app:([1-9][0-9]{0,17}):([0-9a-f]{32})")


def parse_caller(header: str | None) -> tuple[str, str]:
  """Return (kind, id) for the caller the server attested, or raise.

  An app's id is ``<app-id>:<install>`` (``app_caller`` splits it): an app
  caller without its installation is refused, so it can never match a grant
  given to another installation that had the same id.
  """
  text = (header or "").strip()
  app = APP_CALLER_RE.fullmatch(text)
  if app:
    return "app", f"{app.group(1)}:{app.group(2)}"
  match = CALLER_RE.fullmatch(text)
  if not match:
    raise VaultError(403, "The caller is not identified.")
  if match.group(1) == "app":
    raise VaultError(403, "App callers are identified by app id and installation.")
  return match.group(1), match.group(2) or ""


def app_caller(ident: str) -> tuple[int, str]:
  """An app caller's (app id, installation), from ``parse_caller``'s id."""
  app_id, _, install = ident.partition(":")
  return int(app_id), install


def caller_text(caller: tuple[str, str]) -> str:
  return f"{caller[0]}:{caller[1]}" if caller[1] else caller[0]


def caller_name(caller: tuple[str, str]) -> str:
  """How a refusal names its caller: an app by its id (the log keeps its installation)."""
  return f"app:{app_caller(caller[1])[0]}" if caller[0] == "app" else caller_text(caller)


def caller_allowed(grants: dict, kind: str, ident: str) -> bool:
  if kind == "owner":
    return True
  if kind == "agent":
    return bool(grants.get("agents"))
  if kind == "app":
    grant = app_grant(grants, *app_caller(ident))
    return grant is not None and not grant.get("paused")
  return False


def caller_sites(grants: dict, kind: str, ident: str) -> list[dict]:
  """Where this caller may send the key: an app only to its own grant's sites."""
  if kind == "app":
    grant = app_grant(grants, *app_caller(ident))
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
    raw = socket.create_connection((self._pinned_address, self.port), self.timeout)
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


def secret_echoes(*values: str) -> SecretEchoes:
  """Match each secret wherever a site echoes it, however the site escaped it.

  Escaping is matched per character, so mixed and partial escaping is caught
  too: raw, percent-encoding, JSON ``\\u`` and short escapes, HTML numeric and
  named entities, ``+`` for a space. Letters match in either case, which also
  catches hex digits written either way and an upper-cased echo. Hex, UTF-16
  (either byte order) and base64 at every alignment cover a key the site
  re-encoded whole. A key with several secret parts passes each of them (and
  whatever the vault composed from them); longer secrets are tried first, so
  one that contains another is redacted whole.
  """
  if not values:
    # Nothing secret was sent: match nothing (an empty pattern matches everywhere).
    return SecretEchoes(re.compile(rb"(?!)"), 0)
  groups: dict[str, tuple[bytes, int]] = {}
  alternatives: list[tuple[int, bytes]] = []
  longest = 0
  for value in dict.fromkeys(values):
    per_char, size_sum = [], 0
    for ch in value:
      if ch not in groups:
        forms = _char_echoes(ch)
        groups[ch] = (b"(?:" + b"|".join(re.escape(form) for form in forms) + b")", len(forms[0]))
      group, size = groups[ch]
      per_char.append(group)
      size_sum += size
    data = value.encode()
    whole = [data.hex().encode(), value.encode("utf-16-le"), value.encode("utf-16-be")]
    whole = [form for form in whole if len(form) >= MIN_ENCODED_FORM]
    cores = _base64_cores(data)
    alternatives.append((len(value), b"(?i:" + b"".join(per_char) + b")"))
    alternatives += [(len(form), b"(?i:" + re.escape(form) + b")") for form in whole]
    alternatives += [(len(core), re.escape(core)) for core in cores]
    longest = max([longest, size_sum, *map(len, whole), *map(len, cores)])
  ordered = [pattern for _, pattern in sorted(alternatives, key=lambda item: item[0], reverse=True)]
  return SecretEchoes(re.compile(b"|".join(ordered)), longest)


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


@dataclass(frozen=True)
class Loan:
  """What one allowed use sends: the recipe, the parts it uses, and which of
  them are secret (redacted from the answer; plain parts are not)."""
  inject: dict
  values: dict
  secret: frozenset

  def echoes(self, *minted: str) -> SecretEchoes:
    """What to redact: every secret part sent, what the vault composed from
    them, and any token it ``minted`` for this use."""
    secrets_sent = [self.values[name] for name in self.values if name in self.secret]
    if self.inject["type"] == "basic":
      # The pair as it travels, should a site echo the header back whole.
      secrets_sent.append(_basic_credentials(self.inject, self.values))
    if self.inject["type"] == "jwt_bearer":
      # A private key is never sent; the token made from it is.
      secrets_sent = [value for value in secrets_sent if value != self.values[self.inject["private_key"]]]
    return secret_echoes(*secrets_sent, *minted)


def _basic_credentials(inject: dict, values: dict) -> str:
  pair = f"{values[inject['username']]}:{values[inject['password']]}"
  return base64.b64encode(pair.encode()).decode()


def recipe_headers(inject: dict, values: dict) -> list[tuple[str, str]]:
  """The headers a recipe adds, refused (400) when one can't carry its parts."""
  kind = inject["type"]
  if kind == "header":
    added = [(inject["name"], f"{inject.get('prefix', '')}{values[VALUE_PART]}")]
  elif kind == "basic":
    added = [("Authorization", "Basic " + _basic_credentials(inject, values))]
  elif kind == "slots":
    added = [(slot["name"], TEMPLATE_PART_RE.sub(lambda m: values[m.group(1)], slot["template"]))
             for slot in inject["slots"] if slot["type"] == "header"]
  else:
    added = []
  for name, header_value in added:
    if any(ch in header_value for ch in "\r\n"):
      raise VaultError(400, f"A part sent in {name} has line breaks, so it can't be sent in a header.")
    try:
      header_value.encode("latin-1")
    except UnicodeEncodeError:
      raise VaultError(400, f"A part sent in {name} has characters a header can't carry.")
  return added


def _recipe_queries(inject: dict, values: dict) -> list[tuple[str, str]]:
  if inject["type"] == "query":
    return [(inject["name"], values[VALUE_PART])]
  if inject["type"] == "slots":
    return [(slot["name"], values[slot["part"]]) for slot in inject["slots"] if slot["type"] == "query"]
  return []


class _JsonNumber(str):
  """A JSON number kept exactly as the caller wrote it."""


def _refuse_constant(name: str):
  raise ValueError(f"{name} is not JSON")


def _json_text(item) -> str:
  if isinstance(item, _JsonNumber):
    return str(item)
  if isinstance(item, str):
    return json.dumps(item, ensure_ascii=False)
  if item is True:
    return "true"
  if item is False:
    return "false"
  if item is None:
    return "null"
  if isinstance(item, _JsonObject):
    return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_json_text(v)}" for k, v in item) + "}"
  return "[" + ",".join(_json_text(v) for v in item) + "]"


class _JsonObject(list):
  """A JSON object as the pairs the caller wrote, in order."""


def _media_type(headers: list[tuple[str, str]]) -> str | None:
  value = next((v for k, v in headers if k.lower() == "content-type"), None)
  return value.split(";", 1)[0].strip().lower() if value is not None else None


def recipe_body(
  inject: dict, values: dict, body: bytes | None, headers: list[tuple[str, str]],
) -> tuple[bytes | None, str | None]:
  """The body with the recipe's fields set, and a Content-Type to add (or None).

  The caller's body must be in the slots' format (JSON with a top-level
  object, or a form). No body is an empty one, and then the vault names the
  format itself. A field the caller already gave is replaced, and a JSON body
  with a duplicate top-level member is refused, so how the site resolves a
  duplicate can never decide which value it reads. JSON is written back
  member for member, numbers exactly as given.
  """
  slots = [slot for slot in inject["slots"] if slot["type"] == "body"] if inject["type"] == "slots" else []
  if not slots:
    return body, None
  fmt = slots[0]["format"]
  wanted = BODY_FORMATS[fmt]
  media = _media_type(headers)
  empty = not body
  add_type = None
  if media is None:
    if not empty:
      raise VaultError(400, f"This key goes in a {fmt} body; send it with Content-Type: {wanted}.")
    add_type = wanted
  elif not (media == wanted or (fmt == "json" and media.endswith("+json"))):
    raise VaultError(400, f"This key goes in a {fmt} body; send it with Content-Type: {wanted}.")
  fields = [(slot["field"], values[slot["part"]]) for slot in slots]
  names = {name for name, _ in fields}
  if fmt == "json":
    try:
      parsed = _JsonObject() if empty else json.loads(
        body.decode("utf-8"), object_pairs_hook=_JsonObject,
        parse_float=_JsonNumber, parse_int=_JsonNumber, parse_constant=_refuse_constant,
      )
    except (UnicodeDecodeError, ValueError):
      raise VaultError(400, "The request body is not valid JSON (UTF-8).")
    if not isinstance(parsed, _JsonObject):
      raise VaultError(400, "This key goes in a JSON body whose top level is an object.")
    members = [k for k, _ in parsed]
    if len(set(members)) != len(members):
      raise VaultError(400, "The JSON body names a member twice; send each once.")
    kept = _JsonObject((k, v) for k, v in parsed if k not in names)
    kept += fields
    return _json_text(kept).encode("utf-8"), add_type
  try:
    pairs = [] if empty else urllib.parse.parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
  except (UnicodeDecodeError, ValueError):
    raise VaultError(400, "The request body is not a valid form; percent-encode it.")
  pairs = [(k, v) for k, v in pairs if k not in names] + fields
  return urllib.parse.urlencode(pairs).encode("ascii"), add_type


# -- signing ---------------------------------------------------------------

def _aws_quote(text: str) -> str:
  return urllib.parse.quote(text, safe=AWS_UNRESERVED)


def aws_canonical_target(raw_path: str, raw_query: str, service: str) -> tuple[str, str, str, str]:
  """The path and query as sent, and as signed.

  Each path segment is decoded and encoded the AWS way (only unreserved
  characters kept); every service but S3 signs that path encoded once more.
  The query is sorted and encoded the same way, and sent exactly as signed
  so the site computes the same string.
  """
  wire_path = "/".join(_aws_quote(urllib.parse.unquote(seg)) for seg in raw_path.split("/")) or "/"
  signed_path = wire_path if service == "s3" else urllib.parse.quote(wire_path, safe="/" + AWS_UNRESERVED)
  pairs = sorted((_aws_quote(k), _aws_quote(v))
                 for k, v in urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
  query = "&".join(f"{k}={v}" for k, v in pairs)
  return wire_path, signed_path, query, query


def _aws_signed_header(name: str) -> bool:
  lowered = name.lower()
  return lowered in ("host", "content-type", "content-md5") or lowered.startswith("x-amz-")


def aws_sigv4_authorization(
  *, method: str, signed_path: str, signed_query: str, headers: list[tuple[str, str]],
  payload_hash: str, access_key_id: str, secret_access_key: str, region: str, service: str, amz_date: str,
) -> str:
  """The ``Authorization`` value AWS Signature Version 4 gives this request.

  ``headers`` are the request's own, ``X-Amz-Date`` (``amz_date``) included;
  the signature covers ``Host``, ``Content-Type``, ``Content-MD5`` and every
  ``X-Amz-*`` header among them.
  """
  canonical: dict[str, list[str]] = {}
  for name, value in headers:
    if _aws_signed_header(name):
      canonical.setdefault(name.lower(), []).append(" ".join(value.split()))
  names = sorted(canonical)
  canonical_headers = "".join(f"{name}:{','.join(canonical[name])}\n" for name in names)
  signed_headers = ";".join(names)
  request = "\n".join([method, signed_path, signed_query, canonical_headers, signed_headers, payload_hash])
  scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
  to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
  key = ("AWS4" + secret_access_key).encode()
  for step in (amz_date[:8], region, service, "aws4_request"):
    key = hmac.new(key, step.encode(), hashlib.sha256).digest()
  signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
  return (f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
          f"SignedHeaders={signed_headers}, Signature={signature}")


def _b64url(data: bytes) -> str:
  return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_signing_key(text: str):
  """The private key in a PEM part, or in a service-account JSON file pasted
  whole, and the file's ``client_email`` (None for a PEM)."""
  text = text.strip()
  email = None
  if text.startswith("{"):
    try:
      account = json.loads(text)
    except ValueError:
      raise VaultError(400, "The key file is not valid JSON.")
    pem, email = account.get("private_key"), account.get("client_email")
    if not isinstance(pem, str):
      raise VaultError(400, "The key file has no private_key.")
    text = pem
  try:
    key = serialization.load_pem_private_key(text.encode(), password=None)
  except (ValueError, TypeError):
    raise VaultError(400, "The private key isn't an unencrypted PEM key.")
  return key, email if isinstance(email, str) else None


def sign_jwt(inject: dict, private: str, now: float) -> tuple[str, float]:
  """A JWT with the recipe's claims, signed with ``private``, and when it expires."""
  key, email = jwt_signing_key(private)
  alg = inject["alg"]
  if alg == "RS256" and not isinstance(key, rsa.RSAPrivateKey):
    raise VaultError(400, "RS256 needs an RSA private key.")
  if alg == "ES256" and not (isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1"):
    raise VaultError(400, "ES256 needs a P-256 private key.")
  claims = dict(inject["claims"])
  if "iss" not in claims:
    if email is None:
      raise VaultError(400, "Name the token's issuer: the iss claim, or a key file with client_email.")
    claims["iss"] = email
  issued = int(now) - (60 if (inject.get("exchange") or {}).get("kind") == "github" else 0)
  expires = int(now) + inject["lifetime_secs"]
  header = {"alg": alg, "typ": "JWT", **({"kid": inject["key_id"]} if inject.get("key_id") else {})}
  payload = {**claims, "iat": issued, "exp": expires}
  signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                   + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
  if alg == "RS256":
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
  else:
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
  return f"{signing_input}.{_b64url(signature)}", float(expires)


def build_upstream_request(
  *, method: str, host: str, raw_path: str, raw_query: str,
  caller_headers: list[tuple[str, str]], loan: Loan, body: bytes | None = None,
  bearer: str | None = None, now: float | None = None,
) -> tuple[UpstreamRequest, bytes | None]:
  """Place the key's parts as its recipe says and drop anything that could
  displace them: the caller's own header or query parameter with a slot's
  name never travels, nor a body field a slot fills. A ``jwt_bearer`` key is
  sent as the token the vault minted (``bearer``); an ``aws_sigv4`` request
  is signed last, over exactly what goes out, at ``now``."""
  inject, values = loan.inject, loan.values
  added_headers = recipe_headers(inject, values)
  if inject["type"] == "jwt_bearer":
    added_headers = [("Authorization", f"Bearer {bearer}")]
  slots = {name.lower() for name, _ in added_headers}
  if inject["type"] == "aws_sigv4":
    slots |= set(AWS_SET_BY_VAULT)
  body, content_type = recipe_body(inject, values, body, caller_headers)
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
      or lowered in slots
    ):
      continue
    headers.append((name, header_value))
  headers.append(("Host", host))
  # Identity encoding keeps an echoed key visible to redaction.
  headers.append(("Accept-Encoding", "identity"))
  if content_type is not None:
    headers.append(("Content-Type", content_type))
  headers += added_headers
  query = raw_query
  queries = _recipe_queries(inject, values)
  if queries:
    names = {name for name, _ in queries}
    pairs = [
      (k, v) for k, v in urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
      if k not in names
    ]
    query = urllib.parse.urlencode(pairs + queries)
  if inject["type"] == "aws_sigv4":
    return _aws_signed(method, host, raw_path, query, headers, body, inject, values, now)
  target = raw_path + (f"?{query}" if query else "")
  return UpstreamRequest(method=method, host=host, target=target, headers=headers), body


def _aws_signed(method, host, raw_path, raw_query, headers, body, inject, values, now):
  wire_path, signed_path, wire_query, signed_query = aws_canonical_target(raw_path, raw_query, inject["service"])
  amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
  payload_hash = hashlib.sha256(body or b"").hexdigest()
  headers = [*headers, ("X-Amz-Date", amz_date)]
  if inject["service"] == "s3":
    # S3 and stores speaking its API need the payload hash as a header.
    headers.append(("X-Amz-Content-Sha256", payload_hash))
  if inject.get("session_token"):
    headers.append(("X-Amz-Security-Token", values[inject["session_token"]]))
  authorization = aws_sigv4_authorization(
    method=method, signed_path=signed_path, signed_query=signed_query, headers=headers,
    payload_hash=payload_hash, access_key_id=values[inject["access_key_id"]],
    secret_access_key=values[inject["secret_access_key"]], region=inject["region"],
    service=inject["service"], amz_date=amz_date,
  )
  headers.append(("Authorization", authorization))
  target = wire_path + (f"?{wire_query}" if wire_query else "")
  return UpstreamRequest(method=method, host=host, target=target, headers=headers), body


# ---------------------------------------------------------------------------
# Encrypted store and usage log
# ---------------------------------------------------------------------------

def _now() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_precise() -> str:
  """Now to the microsecond, for when a request was asked: a deletion stamp
  is compared against it (``Vault.forget_chat``)."""
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _instant(text: str) -> datetime:
  """A stored or given ISO time as an aware UTC datetime (naive means UTC)."""
  moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
  return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


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
    raw = self.store_path.read_bytes()
    payload = json.loads(raw)
    records = payload.get("keys") or {}
    version = payload.get("version")
    if version not in (1, 2, 3, STORE_VERSION):
      raise SystemExit("key vault: unknown store version; refusing to start")
    if version == 1:
      records = {name: _migrated_from_version_one(name, record) for name, record in records.items()}
    for record in records.values():
      for grant in (record.get("grants") or {}).get("apps") or []:
        grant.setdefault("paused", False)
        # Before version 3 a grant named only an app id: unstamped until the
        # platform names the installation that id belongs to.
        grant.setdefault("install", None)
      if record.get("origin"):
        record["origin"].setdefault("install", None)
    if version == STORE_VERSION:
      return records, False
    # An older vault (the recovery fallback, a restore from the image) can't
    # read version 4: keep the file this one migrated from, once.
    backup = self.dir / f"keys.v{version}.json"
    if not backup.exists():
      _atomic_write(backup, raw)
    return {name: _migrated_to_parts(record) for name, record in records.items()}, True

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
      for item in requests.values():
        item.setdefault("requester", "app")  # stored before agents could ask
        if item["requester"] == "app":
          item.setdefault("install", None)  # unstamped, as a store's grants (STORE_VERSION)
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

  @staticmethod
  def _sealed_for(name: str, part: str) -> bytes:
    """What a part is sealed under: the key's name, and the part's name for any
    part but ``value`` (sealed as a single value always was, so migrating
    rewrites nothing). ``#`` is in no name, so no two parts share one."""
    return (name if part == VALUE_PART else f"{name}#{part}").encode()

  def _seal_part(self, name: str, meta: dict, value: str) -> dict:
    """A stored part: a secret one sealed, a plain one as text."""
    if not meta["secret"]:
      if "\n" in value or "\r" in value:
        raise VaultError(400, f"Part {meta['name']} isn't secret, so it is one line.")
      return {**meta, "text": value}
    nonce = secrets.token_bytes(12)
    sealed = self._aead.encrypt(nonce, value.encode(), self._sealed_for(name, meta["name"]))
    return {**meta, "nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode(),
            # Marked so the owner sees it, and the platform never puts it in a header.
            "multiline": "\n" in value or "\r" in value}

  def _open_part(self, name: str, part: dict) -> str:
    if not part.get("secret"):
      return part["text"]
    try:
      nonce = base64.b64decode(part["nonce"])
      sealed = base64.b64decode(part["sealed"])
      return self._aead.decrypt(nonce, sealed, self._sealed_for(name, part["name"])).decode()
    except (InvalidTag, binascii.Error, KeyError, TypeError, ValueError):
      raise VaultError(500, "This key can't be decrypted.")

  def _open(self, name: str, record: dict, part: str = VALUE_PART) -> str:
    stored = _part(record, part)
    if stored is None:
      raise VaultError(404, f"{name} has no part named {part}.")
    return self._open_part(name, stored)

  def _opens(self, name: str, record: dict) -> bool:
    try:
      for part in record["parts"]:
        if part.get("secret"):
          self._open_part(name, part)
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
        "multiline": bool((_part(record, VALUE_PART) or {}).get("multiline")),
        # Plain parts (an id, a region) show their text; secret ones never do.
        "parts": [_listed_part(part) for part in record["parts"]],
        **stats.get(name, _empty_stats()),
      }
      for name, record in sorted(records.items())
    ]

  def put_key(self, name: str, body: dict, *, actor: str = "owner") -> dict:
    """Create or update a key.

    An update changes only what it names: an omitted ``inject`` or ``grants``
    keeps the key's current one, so replacing a value never withdraws who may
    use it. A new key needs a value and where it is sent.

    ``value`` is the part ``value`` (secret). ``parts`` lists parts to add or
    replace, each ``{name, value, secret?, label?}``; a part the key already
    has keeps its secrecy and label unless the item names them, and every
    other part stays as it is, so rotating one secret never retypes the rest.
    ``remove_parts`` names parts to drop. Afterwards the key must have at
    least one secret part and every part its recipe sends.

    ``create_only`` refuses an occupied name atomically, so a caller choosing a
    free name can never overwrite another key. ``revision``, when given, must
    equal the key's current revision (bumped by every change), so an edit
    made from a stale copy, such as a grant to an app that has since been
    purged, is refused instead of written back.

    ``origin`` records the app (id and installation) and the app's own secret
    name a key was created for. It is set at creation and an update never
    moves it to another app. A
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
      parts, new_values = self._parts_after(name, existing, body)
      if body.get("inject") is not None:
        inject = validate_inject(body["inject"])
      elif existing is not None:
        inject = existing["inject"]
      else:
        raise VaultError(400, "A new key needs where it is sent: a header or a query parameter.")
      missing = recipe_fits(inject, [part["name"] for part in parts])
      if missing:
        raise VaultError(400, f"{name} is sent with {', '.join(missing)}, which it has no part for.")
      if not _sends_a_secret(inject, parts):
        raise VaultError(400, f"{name} would be sent without any secret part; mark the secret one secret.")
      if body.get("grants") is not None:
        grants = validate_grants(body["grants"])
      else:
        grants = copy.deepcopy(existing["grants"]) if existing else validate_grants(None)
      if actor.split(":", 1)[0] == "agent":
        _refuse_an_agent_widening(existing, grants)
      kept_origin = existing.get("origin") if existing else origin
      if existing is not None and new_values and origin != kept_origin:
        if kept_origin:
          grants["apps"] = without_app(grants, kept_origin["app"])
        kept_origin = None
      now = _now()
      self._records[name] = {
        "parts": parts,
        "inject": inject,
        "grants": grants,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "revision": existing.get("revision", 0) + 1 if existing else 1,
      }
      if kept_origin:
        self._records[name]["origin"] = kept_origin
        self._drop_orphaned_origin(self._records[name])
      # A request is settled once its asker may reach every site it asked for.
      for rid in [rid for rid, item in self._requests.items()
                  if item["key"] == name and _sites_covered(asker_grant(grants, item), item["sites"])]:
        del self._requests[rid]
      self._audit_change(name, actor, "added" if existing is None else "updated")
    return {"name": name, "created": existing is None}

  def _parts_after(self, name: str, existing: dict | None, body: dict) -> tuple[list[dict], bool]:
    """The key's parts once ``body`` is applied, and whether it changed a value."""
    current = {part["name"]: part for part in (existing or {}).get("parts") or []}
    items = []
    if body.get("value") is not None:
      items.append({"name": VALUE_PART, "value": body["value"]})
    raw = body.get("parts")
    if raw is not None:
      if not isinstance(raw, list) or len(raw) > MAX_PARTS:
        raise VaultError(400, f"A key has at most {MAX_PARTS} parts.")
      items += raw
    named = []
    for item in items:
      if not isinstance(item, dict):
        raise VaultError(400, "Each part names itself and gives its value.")
      meta = _part_meta(item, current.get(str(item.get("name") or "").strip()))
      if meta["name"] in named:
        raise VaultError(400, f"Part {meta['name']} is given twice.")
      named.append(meta["name"])
      current[meta["name"]] = self._seal_part(name, meta, validate_value(item.get("value")))
    removed = body.get("remove_parts") or []
    if not isinstance(removed, list) or not all(isinstance(part, str) for part in removed):
      raise VaultError(400, "Name the parts to remove.")
    for part in removed:
      if part in named or current.pop(part, None) is None:
        raise VaultError(400, f"{name} has no part {part} to remove.")
    if existing is None and not current:
      raise VaultError(400, "A new key needs a value.")
    if len(current) > MAX_PARTS:
      raise VaultError(400, f"A key has at most {MAX_PARTS} parts.")
    if not any(part["secret"] for part in current.values()):
      raise VaultError(400, "A key keeps at least one secret part.")
    # The part ``value`` first, then the others in the order they were added.
    ordered = sorted(current.values(), key=lambda part: part["name"] != VALUE_PART)
    return ordered, bool(named)

  def _owned_name(self, origin: dict) -> str | None:
    """The key the app in ``origin`` created for that secret and still holds."""
    return next(
      (n for n, r in self._records.items()
       if r.get("origin") == origin and origin_holds_grant(r)),
      None,
    )

  def save_owned(self, app_id: int, install: str, secret_name: str, value: str, sites=None) -> dict:
    """Save an app's own secret by its origin, in one locked step.

    The key app ``app_id`` (installation ``install``) created for
    ``secret_name`` and still holds is replaced, keeping the owner's choices;
    otherwise a new key is created at the first free app-scoped name
    (``app<id>-<name>``, then numbered), granted to the app and the owner's
    agents, sent as a bearer token, on ``sites`` (none unless the server
    names the app's model-provider site). An app holds at most
    ``MAX_OWNED_PER_APP`` such keys, as in the per-app store. Doing both in
    one change means simultaneous saves can never leave two keys for one app
    secret.
    """
    origin = validate_origin({"app": app_id, "install": install, "name": secret_name})
    value = validate_value(value)
    actor = f"app:{app_id}:{install}"
    with self._change():
      name = self._owned_name(origin)
      if name is not None:
        record = self._records[name]
        self.put_key(name, {"value": value, "inject": record["inject"],
                            "grants": record["grants"], "origin": origin}, actor=actor)
        return {"name": name, "created": False}
      owned = sum(1 for r in self._records.values()
                  if (r.get("origin") or {}).get("app") == app_id
                  and r["origin"].get("install") == install and origin_holds_grant(r))
      if owned >= MAX_OWNED_PER_APP:
        raise VaultError(413, f"An app may store at most {MAX_OWNED_PER_APP} secrets.")
      for name in owned_name_candidates(app_id, secret_name):
        if name not in self._records:
          self.put_key(name, {
            "value": value,
            "inject": dict(DEFAULT_INJECT),
            "grants": {"apps": [{"id": app_id, "install": install, "sites": sites or []}],
                       "agents": True, "sites": sites or []},
            "origin": origin,
            "create_only": True,
          }, actor=actor)
          return {"name": name, "created": True}
    raise VaultError(409, f"No free vault name for {secret_name}.")

  def release_owned(self, app_id: int, install: str, secret_name: str) -> dict | None:
    """Let an app give up its own secret, in one locked step.

    The app's grant is withdrawn from the key it created for ``secret_name``.
    When no other app still holds a grant, the key itself is deleted;
    otherwise it stays, with the owner's current settings, for the apps the
    owner shared it with. Checking ownership and changing the key under the
    same lock means a grant the owner adds meanwhile is never lost or deleted.
    Returns None when the app owns no such key.
    """
    origin = validate_origin({"app": app_id, "install": install, "name": secret_name})
    with self._change():
      name = self._owned_name(origin)
      if name is None:
        return None
      deleted = self._release_locked(name, app_id)
      self._audit_change(name, f"app:{app_id}:{install}", "revoked" if deleted else "released")
    return {"name": name, "deleted": deleted}

  @staticmethod
  def _touch(record: dict) -> None:
    record["updated_at"] = _now()
    record["revision"] = record.get("revision", 0) + 1

  @staticmethod
  def _drop_orphaned_origin(record: dict) -> None:
    """Forget the creating app once it no longer holds a grant on the key.

    ``origin`` makes its app the key's owner (it may replace and release the
    key). Once that installation no longer holds a grant on the key, the key
    is the owner's.
    """
    if record.get("origin") and not origin_holds_grant(record):
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
    """Release every key app id ``app_id`` created for itself and still holds.

    Erasing an app's data (or purging the app) erases its secrets, as the
    per-app store did: each such key is deleted, or kept only for the other
    apps the owner shared it with. Keys the app merely borrows, and keys whose
    value the owner or an agent supplied (no longer the app's), are untouched.
    The platform erases by id: the app being wiped or purged holds it now.
    Returns the names of the deleted keys.
    """
    validate_app_id(app_id)
    with self._change():
      owned = [n for n, r in self._records.items()
               if (r.get("origin") or {}).get("app") == app_id and origin_holds_grant(r)]
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

  def reveal(self, name: str, caller: tuple[str, str], part: str = VALUE_PART) -> str:
    """One part's value (``value`` unless named), for the owner, the owner's
    agents and the platform.

    Apps are refused here as well as by the server: lending a key to an app
    means using it on the app's behalf, never handing it over. A key whose
    Agents switch is off is kept from agents for reading as well as use.
    """
    validate_name(name)
    _part_name(part, "A read")
    if caller[0] == "app":
      self.log_event(name, caller_text(caller), "denied", status=403,
                     reason="apps cannot read key values")
      raise VaultError(403, "Apps cannot read key values; use the key through the vault.")
    with self._lock:
      record = self._records.get(name)
      if record is None:
        raise VaultError(404, f"No key named {name}")
      refused = caller[0] == "agent" and not record["grants"].get("agents")
      value = None if refused else self._open(name, record, part)
    if refused:
      self.log_event(name, caller_text(caller), "denied", status=403,
                     reason="agents are switched off for this key")
      raise VaultError(403, "Agents are switched off for this key in Settings › Keys.")
    self.log_event(name, caller_text(caller), "revealed", reason=None if part == VALUE_PART else f"part {part}")
    return value

  # -- access requests (the handshake) ---------------------------------------

  def request_access(self, app_id: int, install: str, body: dict) -> dict:
    """App ``app_id`` (installation ``install``) asks to use a key; answer
    granted, paused, pending or denied.

    Asking is idempotent, so an app checks its status by asking again. The
    first ask for a key returns ``created`` so the server tells the owner
    once, never on every retry. A stored request answers for every later ask
    for the same key until the owner answers it, and every answer about it
    carries the ``sites`` and ``reason`` it stores: what approving it grants. A granted app that asks for sites its grant
    does not cover keeps using what it has, is told which sites are missing
    (``sites_not_allowed``), and its ask for them waits for the owner like
    any request (``sites_request``). An app the owner paused on the key
    hears ``paused`` where it would hear ``granted``: asking changes nothing
    until the owner switches it back on.
    """
    validate_app_id(app_id)
    asker = {"requester": "app", "app": app_id, "install": validate_install(install, app_id),
             "app_name": str(body.get("app_name") or "")[:100]}
    return self._ask(body, asker, id_prefix=str(app_id), caller=f"app:{app_id}:{install}")

  def request_for_agents(self, body: dict, *, actor: str) -> dict:
    """The owner's agents ask to send a key to sites, as an app asks.

    Agents share the owner's sites (``grants.sites``) and the Agents switch,
    so one request per key (``agent-<key>``) speaks for all of them, and it
    is ``granted`` once the switch is on and those sites cover the ask.
    Approving it adds the sites there and switches agents on (or creates the
    key, as for an app). ``actor`` is the agent that asked, for the audit log;
    its chat (``agent:<chat>``) is kept as the request's ``chat``, so the
    card can be answered in the chat that asked (another chat asking for the
    same key joins it and does not move it).
    """
    kind, _, chat = actor.partition(":")
    asker = {"requester": "agent", "app": None, "app_name": "Your agent",
             "chat": chat if kind == "agent" else ""}
    return self._ask(body, asker, id_prefix="agent", caller=actor)

  def _ask(self, body: dict, asker: dict, *, id_prefix: str, caller: str) -> dict:
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
    specs = validate_part_specs(body["parts"]) if body.get("parts") is not None else None
    request_id = f"{id_prefix}-{name}"
    with self._change():
      record = self._records.get(name)
      if specs is not None and inject is None:
        raise VaultError(400, "Say how a key with several parts is sent (inject).")
      if inject is not None:
        # A new key gets the parts the request lists (or the one ``value``).
        # An existing key keeps its own parts: a recipe that doesn't fit them
        # is never set (``sets_inject``), and the card says the key keeps its own.
        have = [spec["name"] for spec in specs] if specs is not None else [VALUE_PART]
        missing = recipe_fits(inject, have)
        if missing and record is None:
          raise VaultError(400, f"The recipe sends {', '.join(missing)}; list every part it sends in parts.")
        if record is None and specs is not None and not _sends_a_secret(inject, specs):
          raise VaultError(400, "The recipe sends no secret part; mark the secret one secret.")
      grant = asker_grant(record["grants"], asker) if record is not None else None
      existing = self._requests.get(request_id)
      if existing is not None and existing.get("install") != asker.get("install"):
        # Another installation's ask under the same id (one gone, or one from
        # before installations were recorded): never this asker's to inherit.
        # Its ticket goes with it, so a card still showing it answers nothing.
        del self._requests[request_id]
        existing = None
      if grant is not None:
        status = "paused" if grant.get("paused") else "granted"
        missing = sites_outside(grant["sites"], sites)
        if not missing:
          # The asker may already do this. A stored request of its own is
          # left alone: ``put_key`` settled it if the grant covers it, and
          # otherwise it still waits for the owner or stands refused.
          return {"status": status, "key": name}
        answer = {"status": status, "key": name, "id": request_id, "sites_not_allowed": missing}
        # A request already stored answers for any later ask until the owner
        # answers it, and the answer names what it stores: whatever shows the
        # request (a card, the dialog over an app) shows what approving grants.
        if existing is not None:
          self._asked_again(existing, asker)
          return {**answer, "sites_request": existing["status"], **_asked(existing)}
        # A key nothing sends yet may still take the slot the asker needs.
        sites, inject = missing, inject if sent_nowhere(record["grants"]) else None
      elif existing is not None:
        self._asked_again(existing, asker)
        return {"status": existing["status"], "key": name, "id": request_id, **_asked(existing)}
      # Only waiting requests count: a refusal is the owner's answer, kept
      # until they let the asker ask again, and blocks nothing else.
      waiting = [item for item in self._requests.values() if item["status"] == "pending"]
      if sum(1 for item in waiting if _asked_by(item, asker)) >= MAX_REQUESTS_PER_APP:
        who = "This app has" if asker["requester"] == "app" else "Your agents have"
        raise VaultError(
          429, f"{who} {MAX_REQUESTS_PER_APP} key requests waiting for an answer; "
          "ask the owner to answer or remove them in Settings › Keys.",
        )
      if len(waiting) >= MAX_REQUESTS:
        raise VaultError(429, "Too many key requests are waiting; ask the owner to answer or remove "
                         "them in Settings › Keys.")
      now = _now_precise()
      self._requests[request_id] = {
        "id": request_id, **asker,
        "key": name, "reason": reason, "sites": sites, "inject": inject,
        "status": "pending", "created_at": now, "asked_at": now, "ticket": secrets.token_hex(8),
      }
      if specs is not None and record is None:
        self._requests[request_id]["parts"] = specs
      self._audit_change(name, caller, "requested")
    asked = {"sites": sites, "reason": reason, "created": True}
    if grant is not None:
      return {**answer, "sites_request": "pending", **asked}
    return {"status": "pending", "key": name, "id": request_id, **asked}

  def _asked_again(self, request: dict, asker: dict) -> None:
    """The chat that made waiting agent request ``request`` asked for it again:
    note when (``asked_at``), so a withdrawal for an earlier deletion of that
    chat (``forget_chat``) leaves it for the card the chat shows now. The
    caller holds a change open."""
    if (request["requester"] == "agent" and request["status"] == "pending"
        and asker.get("chat") and request.get("chat") == asker["chat"]):
      request["asked_at"] = _now_precise()

  def list_requests(self) -> list[dict]:
    """Open requests; ``granted`` marks an asker wanting more sites on a key it may use.

    ``requester`` is ``app`` (with its ``app`` id) or ``agent`` (``app``
    None). ``chat_id`` is the chat whose agent asked, or None (an app's
    request, an agent without a chat, or one stored before chats were kept).
    ``key_origin`` names the app that saved an existing key for
    itself, so the owner never mistakes an app's planted value for a key of
    their own.
    """
    with self._lock:
      requests = [dict(item) for item in self._requests.values()]
      for item in requests:
        chat = item.pop("chat", None)
        item["chat_id"] = chat if isinstance(chat, str) and chat else None
        record = self._records.get(item["key"])
        item["key_exists"] = record is not None
        item["key_origin"] = dict(record["origin"]) if record is not None and record.get("origin") else None
        item["granted"] = record is not None and asker_grant(record["grants"], item) is not None
        # Whether Allow also sets how the key is sent (its card shows the new
        # slot), or the key keeps its own slot although the asker proposed
        # another (its card says so): a key something already sends never
        # changes slot under it.
        item["sets_inject"] = sets_inject(record, item)
        item["inject_kept"] = (record is not None and item.get("inject") is not None
                               and item["inject"] != record["inject"] and not item["sets_inject"])
        item["key_inject"] = copy.deepcopy(record["inject"]) if record is not None else None
        # The parts a new key is pasted as on the card: the request's, or the
        # one ``value``. An existing key's own parts are listed with the key.
        item["parts"] = item.get("parts") if record is None else None
    return sorted(requests, key=lambda item: item["created_at"], reverse=True)

  def approve_request(self, request_id: str, body: dict, *, actor: str = "owner") -> dict:
    """Grant the asker exactly what the owner's answer carries.

    The request only proposes. The sites the answer names go to this app's
    own grant, never to another app or the agents, so approving one app never
    widens anyone else's use; an agents' request adds them to the owner's and
    agents' sites and switches agents on. A new key is created with the
    answer's value and slot (``Authorization: Bearer`` when the answer names
    none). An existing key keeps its value, and its slot unless nothing sends
    it yet (``sent_nowhere``): then an answer naming the slot the request
    proposed, as its card showed, sets it. That is how a key saved ahead of
    time (always as a bearer token) gets the slot its first user needs.
    Approving more sites for a paused app leaves it paused.

    The answer names the request's ``ticket``, so a card left open while the
    request was replaced (a newer ask under the same id: the app id and key
    name) never approves the newer one. The grant goes to the installation
    that asked; a request not yet stamped with one (see ``adopt_installs``)
    can't be approved until it is.

    Only the owner's own answer approves (``actor`` ``owner``); the server
    refuses everyone else too.
    """
    if actor != "owner":
      raise VaultError(403, "Only the owner can answer a key request, from Settings › Keys.")
    with self._change():
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      if body.get("ticket") != request.get("ticket"):
        raise VaultError(409, "That request changed since it was shown; review it again.")
      name = request["key"]
      for_agents = request["requester"] == "agent"
      if not for_agents and request.get("install") is None:
        raise VaultError(409, "That app's request is still being checked; try again in a moment.")
      sites = validate_sites(body.get("sites"), "You and your agents" if for_agents else "An app")
      existing = self._records.get(name)
      if existing is not None:
        if body.get("value") is not None or body.get("parts") is not None:
          raise VaultError(
            409, f"{name} already exists, and approving only says who may use it where. "
            "Change the key itself on its page.",
          )
        grants, inject = existing["grants"], existing["inject"]
        if body.get("inject") is not None:
          if not sets_inject(existing, request) or validate_inject(body["inject"]) != request["inject"]:
            raise VaultError(
              409, f"{name} is already sent another way, or the request changed since it was shown; "
              "review it again.",
            )
          inject = request["inject"]
      else:
        grants = {"apps": [], "agents": False, "sites": []}
        # A key of several parts is sent as its request proposed: no default fits it.
        inject = body.get("inject") or (request["inject"] if request.get("parts") else dict(DEFAULT_INJECT))
      if for_agents:
        grants = {**grants, "agents": True, "sites": _with_sites(grants["sites"], sites)}
      else:
        grant = (app_grant(grants, request["app"], request["install"])
                 or {"id": request["app"], "install": request["install"], "sites": []})
        # A key grants an app id once: this installation's grant replaces any
        # other's for the same id, which no app can use any more.
        grants = {**grants, "apps": [*without_app(grants, request["app"]),
                                     {**grant, "sites": _with_sites(grant["sites"], sites)}]}
      put = {"grants": grants, "inject": inject}
      if existing is None:
        put.update(_new_key_parts(request, body))
      result = self.put_key(name, put, actor=actor)
      self._requests.pop(request_id, None)
      self._audit_change(name, actor, f"granted {_asker_label(request)}")
    return {**result, "granted": request["app"] if not for_agents else "agents"}

  def deny_request(self, request_id: str, body: dict | None = None, *, actor: str = "owner") -> dict:
    """Refuse an ask. A ``ticket`` in ``body`` must be the request's, as when
    approving, so a card left open while the request was replaced can't deny
    the newer ask.

    A refusal is the owner's answer about the key, so only the owner refuses
    (``actor`` ``owner``), as only the owner approves.
    """
    if actor != "owner":
      raise VaultError(403, "Only the owner can answer a key request, from Settings › Keys.")
    ticket = (body or {}).get("ticket")
    with self._change():
      request = self._requests.get(request_id)
      if request is None:
        raise VaultError(404, "That request is gone.")
      if ticket is not None and ticket != request.get("ticket"):
        raise VaultError(409, "That request changed since it was shown; review it again.")
      request["status"] = "denied"
      request["answered_at"] = _now()
      self._audit_change(request["key"], actor, f"denied {_asker_label(request)}")
    return {"denied": request_id}

  def forget_app(self, app_id: int, *, actor: str = "platform") -> dict:
    """Erase a purged app's own keys and drop its id from every grant and request.

    Erasing its keys is the purge's data deletion. Dropping its grants and
    requests is tidying: they name the purged installation, which no other
    app can ever be, even one that reuses the id.
    """
    validate_app_id(app_id)
    with self._change():
      deleted = self.erase_owned(app_id)
      withdrawn = self._withdraw(lambda entry_id, _install: entry_id == app_id, actor=actor, why="app purged")
    return {"keys": withdrawn["keys"], "deleted": deleted, "requests": withdrawn["requests"]}

  def withdraw_apps(self, installs, *, actor: str = "platform", why: str) -> dict:
    """Drop the app installations ``installs`` (``(app id, install)`` pairs;
    ``install`` None for entries not yet stamped with one) from every grant and
    request, in one change.

    Nothing is erased: a key one of them saved for itself stays, with its
    value, as the owner's (its origin goes with its app's grant), so the owner
    can let an app use it again. Whatever fails part way can therefore only
    leave grants withdrawn, which the owner can allow again. A restore uses
    this for the installations its database does not have; ``why`` says so in
    the audit log.
    """
    pairs = set()
    for app_id, install in installs:
      validate_app_id(app_id)
      pairs.add((app_id, None if install is None else validate_install(install, app_id)))
    return self._withdraw(lambda app_id, install: (app_id, install) in pairs, actor=actor, why=why)

  def _withdraw(self, names, *, actor: str, why: str) -> dict:
    """Drop every app grant and request ``names(app id, install)`` is true for."""
    with self._change():
      keys = []
      for name, record in self._records.items():
        gone = [g for g in record["grants"]["apps"] if names(g["id"], g.get("install"))]
        if not gone:
          continue
        record["grants"]["apps"] = [g for g in record["grants"]["apps"] if g not in gone]
        self._drop_orphaned_origin(record)
        self._touch(record)
        keys.append(name)
        for grant in gone:
          self._audit_change(name, actor, f"revoked app:{grant['id']} ({why})")
      requests = [rid for rid, item in self._requests.items()
                  if item["requester"] == "app" and names(item["app"], item.get("install"))]
      for rid in requests:
        del self._requests[rid]
    return {"keys": keys, "requests": len(requests)}

  def adopt_installs(self, installs: dict, *, actor: str = "platform") -> dict:
    """Stamp every app grant, request and key origin that names no
    installation yet with the installation ``installs`` (app id to install,
    every app the platform holds) gives its id, and withdraw those whose id
    no app holds, as a purge would (values kept).

    Only entries from a store before installations were recorded lack one;
    they match no app caller until stamped. Their ids named the apps the
    platform held then, as purges kept them, so the server calls this once
    it starts, before such an app could ask anew. Stamped entries are never
    changed here: one naming another installation is already unusable, and a
    purge or restore tidies it. A request its asker's grant now covers is
    settled, as when the owner widens a grant.
    """
    if actor.split(":", 1)[0] != "platform":
      raise VaultError(403, "Only the platform names app installations.")
    if not isinstance(installs, dict):
      raise VaultError(400, "Name each app id's installation.")
    known = {}
    for raw_id, install in installs.items():
      app_id = validate_app_id(int(raw_id) if isinstance(raw_id, str) and raw_id.isdigit() else raw_id)
      known[app_id] = validate_install(install, app_id)
    with self._change():
      withdrawn = self._withdraw(lambda app_id, install: install is None and app_id not in known,
                                 actor=actor, why="no app holds this id")
      stamped = 0
      for record in self._records.values():
        unstamped = [g for g in record["grants"]["apps"] if g.get("install") is None]
        for grant in unstamped:
          grant["install"] = known[grant["id"]]
        origin = record.get("origin")
        if origin and origin.get("install") is None and origin["app"] in known:
          origin["install"] = known[origin["app"]]
        self._drop_orphaned_origin(record)
        if unstamped:
          self._touch(record)
          stamped += len(unstamped)
      for item in self._requests.values():
        if item["requester"] == "app" and item.get("install") is None:
          item["install"] = known[item["app"]]
          stamped += 1
      for rid, item in list(self._requests.items()):
        record = self._records.get(item["key"])
        if record is not None and _sites_covered(asker_grant(record["grants"], item), item["sites"]):
          del self._requests[rid]
    return {"stamped": stamped, **withdrawn}

  def forget_chat(self, chat: str, *, before: str, actor: str = "platform") -> int:
    """Withdraw the waiting requests the agent of deleted chat ``chat`` made
    up to its deletion at ``before``.

    Deleting the chat settled its card for good, so nothing could answer them
    there. A refusal stays: it is the owner's answer about the key, not the
    chat's. A request the chat asked for after ``before`` (``asked_at``: it
    was recovered and asked again) stays for the card it shows now, so a
    withdrawal that reaches the vault late never removes it.
    """
    if actor != "owner" and actor.split(":", 1)[0] != "platform":
      raise VaultError(403, "Only the platform withdraws a deleted chat's key requests.")
    cutoff = _instant(before)
    with self._change():
      waiting = [rid for rid, item in self._requests.items() if item["requester"] == "agent"
                 and item.get("chat") == chat and item["status"] == "pending"
                 and _instant(item.get("asked_at") or item["created_at"]) <= cutoff]
      for rid in waiting:
        request = self._requests.pop(rid)
        self._audit_change(request["key"], actor, f"forgot request {_asker_label(request)} (chat deleted)")
    return len(waiting)

  def forget_request(self, request_id: str, *, ticket: str | None = None, actor: str = "owner") -> bool:
    """Withdraw one request. With ``ticket``, only while it is still that
    exact ask: a later ask under the same id has its own ticket and stays.

    Removing a request, a refusal included, is the owner's choice or the
    platform's tidying, never an agent's: an agent could otherwise close
    another chat's request or undo the owner's refusal of an app.
    """
    if actor != "owner" and actor.split(":", 1)[0] != "platform":
      raise VaultError(403, "Only the owner can remove a key request, from Settings › Keys.")
    with self._change():
      request = self._requests.get(request_id)
      if request is None or (ticket is not None and request.get("ticket") != ticket):
        return False
      del self._requests[request_id]
      self._audit_change(request["key"], actor, f"forgot request {_asker_label(request)}")
    return True

  # -- use -----------------------------------------------------------------

  def authorize(self, name: str, caller: tuple[str, str], method: str, host: str, path: str) -> Loan:
    """Return what an allowed use sends (a ``Loan``), or raise the denial."""
    with self._lock:
      record = self._records.get(name)
      refused = f"{caller_name(caller)} may not use this key"
      if record is None:
        # An app without a grant learns nothing, including whether the name exists.
        if caller[0] == "app":
          raise GrantRefusal(403, refused)
        raise GrantRefusal(404, f"No key named {name}" if NAME_RE.fullmatch(name) else "No key by that name")
      if not caller_allowed(record["grants"], *caller):
        # A paused app is told what any app without a grant is told; the
        # owner's log says why.
        paused = caller[0] == "app" and app_grant(record["grants"], *app_caller(caller[1])) is not None
        raise GrantRefusal(403, refused, logged=f"{caller_name(caller)} is paused for this key" if paused else None)
      reason = site_allows(caller_sites(record["grants"], *caller), host, method, path)
      if reason:
        raise GrantRefusal(403, reason)
      inject = copy.deepcopy(record["inject"])
      used = {part["name"]: part for part in record["parts"] if part["name"] in recipe_parts(inject)}
      values = {part: self._open_part(name, stored) for part, stored in used.items()}
      if inject["type"] == "header":
        if any(ch in values[VALUE_PART] for ch in "\r\n"):
          raise VaultError(400, f"{name} has line breaks, so it can't be sent in a header.")
        try:
          (inject.get("prefix", "") + values[VALUE_PART]).encode("latin-1")
        except UnicodeEncodeError:
          raise VaultError(400, f"{name} has characters a header can't carry, so it can't be sent in a header.")
      else:
        # Headers can't carry a part with line breaks; a query or body can.
        recipe_headers(inject, values)
      return Loan(inject, values, frozenset(part for part, stored in used.items() if stored.get("secret")))

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


def sent_nowhere(grants: dict) -> bool:
  """No site of its own and no app with a site: nothing can send the key yet
  (an app saved or imported it with no site, say), so the first request
  approved for it may still choose how it is sent."""
  return not grants.get("sites") and not any(grant.get("sites") for grant in grants.get("apps") or [])


def sets_inject(record: dict | None, request: dict) -> bool:
  """Whether approving ``request`` sets how existing key ``record`` is sent:
  it proposed a slot other than the key's own, and nothing sends the key yet."""
  return (record is not None and request.get("inject") is not None
          and sent_nowhere(record["grants"]) and request["inject"] != record["inject"]
          and not recipe_fits(request["inject"], [part["name"] for part in record["parts"]]))


def _sends_a_secret(inject: dict, parts: list[dict]) -> bool:
  """Whether ``inject`` sends at least one secret part: what it sends is what
  the vault redacts, so a key sent with only plain parts would be no key."""
  sent = set(recipe_parts(inject))
  return any(part["secret"] for part in parts if part["name"] in sent)


def _new_key_parts(request: dict, body: dict) -> dict:
  """What the owner's answer saves for a key the request creates.

  A request listing parts gets exactly those, with the names, labels and
  secrecy it proposed and the values the answer carries (``parts``, a map
  of part name to value). Any other request gets the one ``value``.
  """
  specs = request.get("parts")
  if not specs:
    return {"value": body.get("value")}
  values = body.get("parts")
  if not isinstance(values, dict) or set(values) != {spec["name"] for spec in specs}:
    raise VaultError(400, "Give a value for each part the request lists.")
  return {"parts": [{**spec, "value": values[spec["name"]]} for spec in specs]}


def _part(record: dict, name: str) -> dict | None:
  return next((part for part in record.get("parts") or [] if part["name"] == name), None)


def _listed_part(part: dict) -> dict:
  listed = {"name": part["name"], "label": part.get("label", ""), "secret": bool(part.get("secret"))}
  if part.get("secret"):
    listed["multiline"] = bool(part.get("multiline"))
  else:
    listed["text"] = part["text"]
  return listed


def _migrated_to_parts(record: dict) -> dict:
  """A record from before version 4: its one sealed value becomes the secret part ``value``."""
  migrated = {k: v for k, v in record.items() if k not in ("nonce", "sealed", "multiline")}
  migrated["parts"] = [{
    "name": VALUE_PART, "label": "", "secret": True,
    "nonce": record["nonce"], "sealed": record["sealed"], "multiline": bool(record.get("multiline")),
  }]
  return migrated


def asker_grant(grants: dict, asker: dict) -> dict | None:
  """What a request's asker (an app, or the owner's agents) may already do
  with a key: the app's own grant, or for agents the owner's sites while the
  Agents switch is on."""
  if asker["requester"] == "agent":
    return {"sites": grants.get("sites") or []} if grants.get("agents") else None
  return app_grant(grants, asker["app"], asker.get("install"))


def _asked(request: dict) -> dict:
  """What a stored request asks: what approving it would grant."""
  return {"sites": request["sites"], "reason": request["reason"]}


def _asked_by(item: dict, asker: dict) -> bool:
  return item["requester"] == asker["requester"] and item["app"] == asker["app"]


def _asker_label(request: dict) -> str:
  """Who a request is for, as the audit log names it."""
  return "agents" if request["requester"] == "agent" else f"app:{request['app']}"


def _with_sites(current: list[dict], added: list[dict]) -> list[dict]:
  """``current`` plus each site of ``added`` it does not already list."""
  merged = {(s["host"], tuple(s["methods"]), tuple(s["paths"])): s for s in current}
  for site in added:
    merged.setdefault((site["host"], tuple(site["methods"]), tuple(site["paths"])), site)
  return list(merged.values())


def _sites_covered(grant: dict | None, wanted: list[dict]) -> bool:
  """Whether a grant (``asker_grant``) already allows everything in ``wanted``."""
  return grant is not None and not sites_outside(grant["sites"], wanted)


def _refuse_an_agent_widening(existing: dict | None, grants: dict) -> None:
  """Agents may take access away, but giving it is the owner's own decision.

  An agent that needs a key on a new site asks (``AGENT_ASK``), and the
  owner answers the card.
  """
  if existing is not None and grants["agents"] and not existing["grants"].get("agents"):
    raise VaultError(403, "Only you can let agents use this key, from Settings › Keys.")
  if widened_apps(existing["grants"] if existing else None, grants):
    raise VaultError(403, "Only you can let an app use a key, reach more sites or switch back on, "
                     "from Settings › Keys.")
  if sites_outside(existing["grants"]["sites"] if existing else [], grants["sites"]):
    raise VaultError(403, f"Only the owner adds sites a key is sent to; ask with {AGENT_ASK}.")


def _migrated_from_version_one(name: str, record: dict) -> dict:
  """A version 1 record with each app keeping exactly the sites it could reach.

  Validated like any new grant (duplicate app ids merge, a missing ``sites``
  is no sites), each app grant unstamped (see ``STORE_VERSION``), and an
  origin whose app no longer holds a grant is dropped, as every later change
  would.
  """
  grants = record.get("grants") or {}
  try:
    clean = validate_grants({"agents": grants.get("agents"), "sites": grants.get("sites") or []})
    app_ids = sorted({validate_app_id(app_id, f"'{app_id}' is not an app id.")
                      for app_id in grants.get("apps") or []})
  except VaultError as exc:
    raise SystemExit(f"key vault: version 1 key {name} can't be migrated ({exc.reason}); refusing to start")
  clean["apps"] = [{"id": app_id, "install": None, "sites": copy.deepcopy(clean["sites"]), "paused": False}
                   for app_id in app_ids]
  migrated = {**record, "grants": clean}
  if migrated.get("origin"):
    migrated["origin"] = {**migrated["origin"], "install": None}
  Vault._drop_orphaned_origin(migrated)
  return migrated


def _is_stored_request(item) -> bool:
  if not isinstance(item, dict):
    return False
  if item.get("requester") == "agent":
    asker_ok = item.get("app") is None
  else:
    asker_ok = (item.get("requester") in (None, "app") and isinstance(item.get("app"), int)
                and (item.get("install") is None or isinstance(item["install"], str)))
  return (
    asker_ok and isinstance(item.get("key"), str)
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

  Connecting is bounded by the connection's timeout (``time_left`` when it is
  made): it limits the TCP connect, and the TLS handshake as a whole, since
  Python applies a socket's timeout to the handshake as one deadline. After
  that each read gets its own timeout, which alone would let a site that
  trickles header or chunk-size lines keep a borrow, and a purge waiting for
  it, open long past the deadline: ``watch`` shuts the site's connection down
  when the deadline passes, and ``stop`` cancels that.
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
  """Borrows running at once: at most ``per_caller`` for one caller
  (``app:<id>:<install>``, ``agent:<chat>``, ``owner``) and ``total`` in all;
  beyond either, 429."""

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


class TokenCache:
  """Tokens minted from private keys, in memory only, until shortly before
  they expire. Keyed by the key, its recipe and its private part, so any
  change to them mints afresh; a restart forgets them all."""

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._tokens: dict[str, tuple[str, float]] = {}

  def get(self, key: str, mint) -> str:
    now = time.time()
    with self._lock:
      held = self._tokens.get(key)
    if held is not None and held[1] - TOKEN_MARGIN_SECS > now:
      return held[0]
    token, expires = mint()
    with self._lock:
      self._tokens = {k: v for k, v in self._tokens.items() if v[1] > now}
      self._tokens[key] = (token, expires)
    return token


def _token_answer(host: str, status: int, data: bytes, jwt: str) -> dict:
  if len(data) > MAX_TOKEN_ANSWER:
    raise VaultError(502, f"{host} answered the token request with too much.")
  try:
    answer = json.loads(data or b"{}")
  except ValueError:
    answer = None
  if not isinstance(answer, dict):
    raise VaultError(502, f"{host} didn't answer the token request with JSON ({status}).")
  if status >= 300:
    said = answer.get("error_description") or answer.get("error") or answer.get("message") or ""
    said = redact(str(said)[:200].encode(), secret_echoes(jwt)).decode(errors="replace").rstrip(". ")
    raise VaultError(502, f"{host} refused the token ({status}){': ' + said if said else ''}.")
  return answer


def _usable_token(host: str, token) -> str:
  if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{8,4096}", token):
    raise VaultError(502, f"{host} answered without a usable token.")
  return token


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

  def _send_json(
    self, status: int, payload, *, denied: bool = False, sent_key: str | None = None,
  ) -> None:
    self._answer_started = True
    body = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Connection", "close")
    if denied:
      self.send_header("X-Vault-Denied", "1")
    if sent_key is not None:
      # The request had already gone out with the key when it failed.
      self.send_header("X-Vault-Key", sent_key)
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
      part = query.get("part") or VALUE_PART
      answer = {"name": parts[1], "value": vault.reveal(parts[1], caller, part)}
      self._send_json(200, answer if part == VALUE_PART else {**answer, "part": part})
    elif method == "GET" and parts == ["usage"]:
      limit = int(query.get("limit") or 100)
      self._send_json(200, {"usage": vault.usage(key=query.get("key") or None, limit=limit)})
    elif method == "GET" and parts == ["requests"]:
      self._send_json(200, {"requests": vault.list_requests()})
    elif method == "POST" and parts == ["requests"]:
      caller = parse_caller(self.headers.get("X-Vault-Caller"))
      if caller[0] == "app":
        self._send_json(200, vault.request_access(*app_caller(caller[1]), self._json_body()))
      elif caller[0] == "agent":
        self._send_json(200, vault.request_for_agents(self._json_body(), actor=caller_text(caller)))
      else:
        raise VaultError(403, "Only apps and agents ask for keys.")
    elif method == "POST" and len(parts) == 3 and parts[0] == "requests" and parts[2] == "approve":
      self._send_json(200, vault.approve_request(parts[1], self._json_body(), actor=self._manager()))
    elif method == "POST" and len(parts) == 3 and parts[0] == "requests" and parts[2] == "deny":
      self._send_json(200, vault.deny_request(parts[1], self._json_body(), actor=self._manager()))
    elif method == "POST" and parts == ["owned"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id, its installation and its secret name.")
      self._send_json(200, vault.save_owned(
        body["app"], body.get("install"), body["name"], body.get("value"), sites=body.get("sites"),
      ))
    elif method == "POST" and parts == ["owned", "release"]:
      body = self._json_body()
      if not isinstance(body.get("app"), int) or not isinstance(body.get("name"), str):
        raise VaultError(400, "Name the app id, its installation and its secret name.")
      released = vault.release_owned(body["app"], body.get("install"), body["name"])
      if released is None:
        raise VaultError(404, "That app owns no such key.")
      self._send_json(200, released)
    elif method == "POST" and parts == ["apps", "installs"]:
      self._send_json(200, vault.adopt_installs(self._json_body().get("installs"), actor=self._manager()))
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "apps" and parts[1].isdigit():
      self._send_json(200, vault.forget_app(int(parts[1]), actor=self._manager()))
    elif method == "DELETE" and len(parts) == 3 and parts[0] == "apps" and parts[1].isdigit() and parts[2] == "owned":
      self._send_json(200, {"deleted": vault.erase_owned(int(parts[1]))})
    elif method == "DELETE" and len(parts) == 3 and parts[0] == "chats" and parts[2] == "requests":
      if not query.get("before"):
        raise VaultError(400, "Name when the chat was deleted (before=).")
      self._send_json(200, {"forgotten": vault.forget_chat(
        parts[1], before=query["before"], actor=self._manager(),
      )})
    elif method == "DELETE" and len(parts) == 2 and parts[0] == "requests":
      if not vault.forget_request(parts[1], ticket=query.get("ticket") or None, actor=self._manager()):
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
        loan = self.vault.authorize(name, caller, method, host, path)
        bearer = self._bearer(name, loan, deadline) if loan.inject["type"] == "jwt_bearer" else None
        request, body = build_upstream_request(
          method=method, host=host, raw_path=raw_path, raw_query=split.query,
          caller_headers=list(self.headers.items()), loan=loan, body=body, bearer=bearer,
        )
      except VaultError as exc:
        refused = exc.status < 500 and exc.status != 429
        self.vault.log_traffic(
          logged_name, caller_label, method, host, raw_path, "denied" if refused else "failed",
          status=exc.status, reason=exc.logged if isinstance(exc, GrantRefusal) else exc.reason,
        )
        answer = {"error": "denied", "reason": exc.reason}
        if isinstance(exc, GrantRefusal) and caller_label.split(":", 1)[0] == "agent":
          # Agents do not widen a key themselves: the owner answers a card.
          answer["ask"] = AGENT_ASK
        self._send_json(exc.status, answer, denied=True)
        return
      self._relay(logged_name, caller_label, request, body, loan.echoes(*filter(None, [bearer])), started, deadline)
    finally:
      deadline.stop()
      if borrowing:
        self.server.borrows.give_back(caller_label)

  # -- tokens from private keys -------------------------------------------

  def _bearer(self, name: str, loan: Loan, deadline: BorrowDeadline) -> str:
    inject = loan.inject
    private = loan.values[inject["private_key"]]
    key = hashlib.sha256(json.dumps([name, inject, private], sort_keys=True).encode()).hexdigest()
    return self.server.tokens.get(key, lambda: self._mint(inject, private, deadline))

  def _mint(self, inject: dict, private: str, deadline: BorrowDeadline) -> tuple[str, float]:
    """A token for ``inject``: the signed JWT itself, or what its fixed
    exchange endpoint gives for it."""
    jwt, expires = sign_jwt(inject, private, time.time())
    exchange = inject.get("exchange")
    if exchange is None:
      return jwt, expires
    if exchange["kind"] == "google":
      host, path = GOOGLE_TOKEN
      body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt,
      }).encode()
      headers = [("Content-Type", "application/x-www-form-urlencoded")]
    else:
      host, path = GITHUB_API, f"/app/installations/{exchange['installation_id']}/access_tokens"
      body = b""
      headers = [("Authorization", f"Bearer {jwt}"), ("Accept", "application/vnd.github+json"),
                 ("X-GitHub-Api-Version", "2022-11-28"), ("User-Agent", "mobius-key-vault")]
    status, data = self._token_request(host, path, headers, body, deadline)
    answer = _token_answer(host, status, data, jwt)
    now = time.time()
    if exchange["kind"] == "google":
      lifetime = answer.get("expires_in")
      if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise VaultError(502, f"{host} didn't say when its token expires.")
      return _usable_token(host, answer.get("access_token")), now + lifetime
    try:
      expires_at = calendar.timegm(time.strptime(str(answer.get("expires_at")), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
      raise VaultError(502, f"{host} didn't say when its token expires.")
    return _usable_token(host, answer.get("token")), float(expires_at)

  def _token_request(self, host, path, headers, body, deadline: BorrowDeadline) -> tuple[int, bytes]:
    connection = None
    try:
      connection = self.connect(host, deadline.time_left(UPSTREAM_TIMEOUT_SECS))
      connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
      for header, value in [("Host", host), ("Accept-Encoding", "identity"), *headers,
                            ("Content-Length", str(len(body)))]:
        connection.putheader(header, value)
      connection.endheaders(body)
      response = connection.getresponse()
      return response.status, response.read(MAX_TOKEN_ANSWER + 1)
    except (OSError, http.client.HTTPException) as exc:
      deadline.raise_if_passed()
      raise VaultError(502, f"{host} could not be reached for a token ({type(exc).__name__}).")
    finally:
      if connection is not None:
        connection.close()

  def _relay(self, name, caller_label, request: UpstreamRequest, body, echoes, started, deadline: BorrowDeadline) -> None:
    method, host, raw_path = request.method, request.host, request.target.split("?", 1)[0]
    connection = None
    self._key_sent = False  # set once the request, key included, starts to leave
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
      self._send_json(
        exc.status, {"error": "denied" if refused else "upstream", "reason": exc.reason},
        denied=True, sent_key=name if self._key_sent else None,
      )
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
    try:
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
      self.end_headers()
    except OSError:
      # The key was sent and the site answered: the use happened even though
      # the caller no longer takes the answer, so it is still logged.
      record("The caller stopped waiting before the answer arrived")
      return
    if not has_body:
      record(None)
      return
    redactor = StreamRedactor(echoes)

    def send(data: bytes) -> None:
      # One inflated piece can be megabytes, and a socket timeout bounds a whole
      # write: sent in slices, the timeout catches a caller that stopped
      # reading, not one that reads slowly.
      for start in range(0, len(data), CALLER_SEND_SLICE):
        part = data[start:start + CALLER_SEND_SLICE]
        self.connection.settimeout(deadline.time_left(CALLER_SEND_TIMEOUT_SECS))
        try:
          self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
          self.wfile.flush()
        except TimeoutError:
          deadline.raise_if_passed()
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
      # Within the deadline, this timeout bounds connecting and the whole TLS
      # handshake (see BorrowDeadline); never give it a flat timeout.
      connection = self.connect(request.host, deadline.time_left(UPSTREAM_TIMEOUT_SECS))
      connection.response_class = FinalAnswer
      connection.connect()
      deadline.watch(connection.sock)
      connection.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
      for header, header_value in request.headers:
        connection.putheader(header, header_value)
      if body is not None:
        connection.putheader("Content-Length", str(len(body)))
      # From here the key may have left, even if sending fails part way.
      self._key_sent = True
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
    self.tokens = TokenCache()


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
  bound = os.stat(socket_path)

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
  remove_own_socket(socket_path, bound)


def remove_own_socket(path: Path, bound: os.stat_result) -> None:
  """Remove ``path`` only while it is still the socket this vault bound.

  Whatever replaced it since (a restore, an owner's tool) is not this
  vault's to remove.
  """
  try:
    current = os.lstat(path)
  except FileNotFoundError:
    return
  if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
    path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--data", required=True, type=Path)
  parser.add_argument("--parent", type=int, default=None)
  args = parser.parse_args(argv)
  serve(args.data, args.parent)


if __name__ == "__main__":
  sys.exit(main())
