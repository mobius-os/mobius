#!/usr/bin/env python3
"""Railway single-service gateway for Mobius.

Self-hosted Mobius uses Caddy to route `/recover*` to the frozen recoveryd
container before the main app catch-all. Railway templates run one public
service with one attached volume, so the app process and recoveryd must share
the service container if recoveryd is going to see `/data`.

This file is only the missing router. It is not a second recovery path:
`/recover*` goes to recoveryd, everything else goes to the main app.
"""

from __future__ import annotations

import argparse
import http.client
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOP_BY_HOP_HEADERS = {
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
}
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("MOBIUS_GATEWAY_TIMEOUT", "1200"))


def is_recovery_path(path: str) -> bool:
  """Return True for requests owned by recoveryd."""
  return path == "/recover" or path.startswith("/recover/")


def parse_upstream(value: str) -> tuple[str, int]:
  """Parse an HTTP upstream URL or host:port pair."""
  if "://" in value:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http":
      raise ValueError(f"Only http upstreams are supported: {value!r}")
    return parsed.hostname or "127.0.0.1", parsed.port or 80
  host, _, raw_port = value.partition(":")
  return host or "127.0.0.1", int(raw_port or "80")


def default_forwarded_proto() -> str:
  """Railway terminates TLS before the container."""
  if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
    return "https"
  return "http"


class Gateway(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  server_version = "MobiusRailwayGateway/1.0"

  app_upstream: tuple[str, int]
  recovery_upstream: tuple[str, int]

  def _target(self) -> tuple[str, int]:
    path = urllib.parse.urlparse(self.path).path
    return self.recovery_upstream if is_recovery_path(path) else self.app_upstream

  def _read_body(self) -> bytes | None:
    try:
      length = int(self.headers.get("Content-Length", "0") or "0")
    except (TypeError, ValueError):
      length = 0
    return self.rfile.read(length) if length > 0 else None

  def _headers(self) -> dict[str, str]:
    connection_tokens = {
      item.strip().lower()
      for item in self.headers.get("Connection", "").split(",")
      if item.strip()
    }
    blocked = HOP_BY_HOP_HEADERS | connection_tokens
    headers = {
      key: value
      for key, value in self.headers.items()
      if key.lower() not in blocked
    }

    host = self.headers.get("Host")
    if host:
      headers["Host"] = host
      headers.setdefault("X-Forwarded-Host", host)

    prior_for = self.headers.get("X-Forwarded-For")
    client_ip = self.client_address[0] if self.client_address else ""
    if client_ip:
      headers["X-Forwarded-For"] = (
        f"{prior_for}, {client_ip}" if prior_for else client_ip
      )
    headers.setdefault(
      "X-Forwarded-Proto",
      self.headers.get("X-Forwarded-Proto") or default_forwarded_proto(),
    )
    return headers

  def _plain(self, status: int, text: str) -> None:
    body = text.encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "text/plain; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "close")
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)
    self.close_connection = True

  def _proxy(self) -> None:
    host, port = self._target()
    conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT_SECONDS)
    try:
      conn.request(
        self.command,
        self.path,
        body=self._read_body(),
        headers=self._headers(),
      )
      resp = conn.getresponse()
    except OSError as exc:
      self._plain(HTTPStatus.BAD_GATEWAY, f"Mobius upstream unavailable: {exc}")
      return

    try:
      self.send_response(resp.status, resp.reason)
      for key, value in resp.getheaders():
        if key.lower() in HOP_BY_HOP_HEADERS:
          continue
        self.send_header(key, value)
      self.send_header("Connection", "close")
      self.end_headers()
      if self.command != "HEAD":
        while True:
          chunk = resp.read(64 * 1024)
          if not chunk:
            break
          self.wfile.write(chunk)
          self.wfile.flush()
    finally:
      conn.close()
      self.close_connection = True

  def do_GET(self) -> None:  # noqa: N802
    self._proxy()

  def do_HEAD(self) -> None:  # noqa: N802
    self._proxy()

  def do_POST(self) -> None:  # noqa: N802
    self._proxy()

  def do_PUT(self) -> None:  # noqa: N802
    self._proxy()

  def do_PATCH(self) -> None:  # noqa: N802
    self._proxy()

  def do_DELETE(self) -> None:  # noqa: N802
    self._proxy()

  def do_OPTIONS(self) -> None:  # noqa: N802
    self._proxy()

  def log_message(self, fmt: str, *args) -> None:
    sys.stderr.write("railway-gateway " + fmt % args + "\n")


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--host", default="0.0.0.0")
  parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
  parser.add_argument("--app", default="http://127.0.0.1:18000")
  parser.add_argument("--recovery", default="http://127.0.0.1:18001")
  args = parser.parse_args(argv)

  Gateway.app_upstream = parse_upstream(args.app)
  Gateway.recovery_upstream = parse_upstream(args.recovery)
  server = ThreadingHTTPServer((args.host, args.port), Gateway)
  server.daemon_threads = True
  print(
    "Mobius Railway gateway listening on "
    f"{args.host}:{args.port}; app={args.app}; recovery={args.recovery}",
    flush=True,
  )
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    return 130
  finally:
    server.server_close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
