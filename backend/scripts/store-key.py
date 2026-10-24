#!/usr/bin/env python3
"""Sealed-card consumer: save one owner key into the Möbius key vault.

Reads one JSON object on stdin and stores its value (the "value" or
"api_key" field, or the only field) as the vault key NAME. Never prints, logs
or keeps the value anywhere else. Options describe how apps may use the key;
on an existing key they replace only what is given.

  store-key.py NAME [--header NAME [--prefix TEXT] | --query NAME]
               [--site HOST[:METHODS[:PATHS]]]... [--no-agents]

  --site api.example.com:GET,POST:/v1/*,/v2/*   (methods default to GET,
  paths to /*). Without --header or --query a new key is sent as
  "Authorization: Bearer <key>". A new key lets agent chats borrow it (so
  they never need to read the value); --no-agents turns that off.

Exit codes: 0 stored, 2 empty or malformed input, 4 could not store.
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
    headers = {"Content-Type": "application/json"} if payload else {}
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


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("name")
  slot = parser.add_mutually_exclusive_group()
  slot.add_argument("--header")
  slot.add_argument("--query")
  parser.add_argument("--prefix")
  parser.add_argument("--site", action="append", default=[])
  parser.add_argument("--agents", action="store_true", help=argparse.SUPPRESS)  # the default
  parser.add_argument("--no-agents", action="store_true")
  args = parser.parse_args(argv)
  try:
    value = _value(json.load(sys.stdin))
  except (ValueError, AttributeError):
    return 2
  if not value:
    return 2
  try:
    status, listing = _call("GET", "/keys")
    if status != 200:
      return 4
    existing = next((k for k in listing["keys"] if k["name"] == args.name), None)
    inject = existing["inject"] if existing else {
      "type": "header", "name": "Authorization", "prefix": "Bearer ",
    }
    if args.header:
      inject = {"type": "header", "name": args.header, "prefix": args.prefix or ""}
    elif args.query:
      inject = {"type": "query", "name": args.query}
    grants = dict(existing["grants"]) if existing else {"apps": [], "agents": True, "sites": []}
    if args.site:
      grants["sites"] = [_site(s) for s in args.site]
    if args.agents:
      grants["agents"] = True
    if args.no_agents:
      grants["agents"] = False
    status, _ = _call("PUT", f"/keys/{args.name}", {"value": value, "inject": inject, "grants": grants})
  except OSError:
    return 4
  return 0 if status == 200 else 4


if __name__ == "__main__":
  sys.exit(main())
