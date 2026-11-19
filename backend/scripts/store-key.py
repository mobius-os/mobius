#!/usr/bin/env python3
"""Sealed-card consumer: save one owner key into the Möbius key vault.

Reads one JSON object on stdin and stores its value (the "value" or
"api_key" field, or the only field) as the vault key NAME. Never prints, logs
or keeps the value anywhere else. Options describe how apps may use the key;
on an existing key they replace only what is given.

  store-key.py NAME [--header NAME [--prefix TEXT] | --query NAME]
               [--site HOST[:METHODS[:PATHS]]]... [--no-agents] [--replace]
  store-key.py NAME --part PART[:plain[:LABEL]]... (--basic USER PASS | --slots JSON)
               [--site ...]... [--no-agents] [--replace]

  With --part, the key has several parts: each is read from the input field
  of the same name (one card field per part), secret unless marked plain (an
  id, a username, a region: shown in Settings and not redacted). --basic
  sends two parts as "Authorization: Basic"; --slots takes the vault's slot
  list as JSON, e.g. '[{"type": "header", "name": "X-Id", "template": "{id}"}]'.
  On an existing key, --part replaces only the parts it names.

  --site api.example.com:GET,POST:/v1/*,/v2/*   (methods default to GET,
  paths to /*). Without --header or --query a new key is sent as
  "Authorization: Bearer <key>". A new key lets agent chats borrow it (so
  they never need to read the value); --no-agents turns that off.

  An existing key is never replaced unless --replace is given: without it,
  a key that already has the name is left exactly as it was (exit 3). A
  replacement applies only to the key as it was read, so a change someone
  made meanwhile is never overwritten (exit 4; read it and try again).

This program talks to the vault socket directly, as the owner, so the
server's route checks and the owner's notifications do not apply to it.

Exit codes: 0 stored, 2 empty or malformed input, 3 the key exists (rerun
with --replace to replace it), 4 could not store.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
from pathlib import Path

VAULT_SOCKET = Path("/data/.vault/vault.sock")


class _UnixConnection(http.client.HTTPConnection):
  def __init__(self, path: Path) -> None:
    super().__init__("vault", timeout=15)
    self._path = str(path)

  def connect(self) -> None:
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.sock.settimeout(self.timeout)
    self.sock.connect(self._path)


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
  connection = _UnixConnection(VAULT_SOCKET)
  try:
    payload = json.dumps(body).encode() if body is not None else None
    # The value came from the owner's own sealed card, so the change is theirs.
    headers = {"X-Vault-Caller": "owner"}
    if payload:
      headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    return response.status, json.loads(response.read() or b"{}")
  finally:
    connection.close()


def _site(text: str) -> dict:
  host, _, rest = text.partition(":")
  methods, _, paths = rest.partition(":")
  return {
    "host": host,
    "methods": [m for m in (methods or "GET").split(",") if m],
    "paths": [p for p in (paths or "/*").split(",") if p],
  }


def _value(raw: dict) -> str:
  for field in ("value", "api_key"):
    if isinstance(raw.get(field), str):
      return raw[field].strip()
  if len(raw) == 1 and isinstance(next(iter(raw.values())), str):
    return next(iter(raw.values())).strip()
  return ""


def _parts(raw: dict, specs: list[str]) -> list[dict] | None:
  """The parts named by ``--part`` with their values from the input, or None
  when one is missing or empty."""
  parts = []
  for spec in specs:
    name, _, rest = spec.partition(":")
    kind, _, label = rest.partition(":")
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
      return None
    part = {"name": name, "value": value.strip(), "secret": kind != "plain"}
    if label:
      part["label"] = label
    parts.append(part)
  return parts


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("name")
  slot = parser.add_mutually_exclusive_group()
  slot.add_argument("--header")
  slot.add_argument("--query")
  slot.add_argument("--basic", nargs=2, metavar=("USER_PART", "PASSWORD_PART"))
  slot.add_argument("--slots")
  parser.add_argument("--part", action="append", default=[])
  parser.add_argument("--prefix")
  parser.add_argument("--site", action="append", default=[])
  parser.add_argument("--agents", action="store_true", help=argparse.SUPPRESS)  # the default
  parser.add_argument("--no-agents", action="store_true")
  parser.add_argument("--replace", action="store_true")
  args = parser.parse_args(argv)
  try:
    raw = json.load(sys.stdin)
    parts = _parts(raw, args.part) if args.part else None
    value = None if args.part else _value(raw)
  except (ValueError, AttributeError):
    return 2
  if not (parts or value):
    return 2
  try:
    status, listing = _call("GET", "/keys")
    if status != 200:
      return 4
    existing = next((k for k in listing["keys"] if k["name"] == args.name), None)
    if existing and not args.replace:
      return 3
    inject = existing["inject"] if existing else {
      "type": "header", "name": "Authorization", "prefix": "Bearer ",
    }
    if args.header:
      inject = {"type": "header", "name": args.header, "prefix": args.prefix or ""}
    elif args.query:
      inject = {"type": "query", "name": args.query}
    elif args.basic:
      inject = {"type": "basic", "username": args.basic[0], "password": args.basic[1]}
    elif args.slots:
      try:
        inject = {"type": "slots", "slots": json.loads(args.slots)}
      except ValueError:
        return 2
    grants = dict(existing["grants"]) if existing else {"apps": [], "agents": True, "sites": []}
    if args.site:
      grants["sites"] = [_site(s) for s in args.site]
    if args.agents:
      grants["agents"] = True
    if args.no_agents:
      grants["agents"] = False
    body = {"parts": parts} if parts else {"value": value}
    body.update(inject=inject, grants=grants)
    if existing:
      # Only the key as it was read: never a change made since.
      body["revision"] = existing.get("revision", 0)
    else:
      # Never a key someone else created since the list was read.
      body["create_only"] = True
    status, _ = _call("PUT", f"/keys/{args.name}", body)
  except OSError:
    return 4
  if status == 409 and not existing:
    return 3
  return 0 if status == 200 else 4


if __name__ == "__main__":
  sys.exit(main())
