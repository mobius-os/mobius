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
BODY_CHUNK_BYTES = 64 * 1024
MAX_CHUNK_LINE_BYTES = 64 * 1024
MAX_CONTENT_LENGTH_DIGITS = 20
MAX_CHUNK_SIZE_DIGITS = 16
MAX_TRAILER_BYTES = 64 * 1024


class RequestBodyError(ValueError):
  """The downstream request body has invalid or incomplete framing."""

  status = HTTPStatus.BAD_REQUEST


class UnsupportedTransferEncoding(RequestBodyError):
  """The gateway cannot safely decode the requested transfer coding."""

  status = HTTPStatus.NOT_IMPLEMENTED


def _readline_crlf(stream) -> bytes:
  line = stream.readline(MAX_CHUNK_LINE_BYTES + 1)
  if len(line) > MAX_CHUNK_LINE_BYTES:
    raise RequestBodyError("request body framing line is too long")
  if not line.endswith(b"\r\n"):
    raise RequestBodyError("request body ended during chunk framing")
  return line[:-2]


def _comma_separated_tokens(values) -> set[str]:
  return {
    item.strip().lower()
    for value in values
    for item in value.split(",")
    if item.strip()
  }


class FixedLengthBody:
  """Expose exactly one Content-Length body as bounded streaming reads."""

  def __init__(self, stream, length: int):
    self.stream = stream
    self.remaining = length

  def read(self, size: int = -1) -> bytes:
    if self.remaining == 0:
      return b""
    if size == 0:
      return b""
    requested = self.remaining if size < 0 else min(size, self.remaining)
    requested = min(requested, BODY_CHUNK_BYTES)
    read = getattr(self.stream, "read1", self.stream.read)
    chunk = read(requested)
    if not chunk:
      raise RequestBodyError("request body ended before Content-Length")
    self.remaining -= len(chunk)
    return chunk


class ChunkedBody:
  """Decode downstream HTTP chunks while the upstream re-chunks the stream."""

  def __init__(self, stream):
    self.stream = stream
    self.remaining = 0
    self.done = False

  def __iter__(self):
    return self

  def __next__(self) -> bytes:
    if self.done:
      raise StopIteration

    while self.remaining == 0:
      size_line = _readline_crlf(self.stream)
      size_text = size_line.split(b";", 1)[0].strip()
      if (
        not size_text
        or len(size_text) > MAX_CHUNK_SIZE_DIGITS
        or any(char not in b"0123456789abcdefABCDEF" for char in size_text)
      ):
        raise RequestBodyError("invalid chunk size")
      size = int(size_text, 16)
      if size == 0:
        trailer_bytes = 0
        while True:
          trailer = _readline_crlf(self.stream)
          trailer_bytes += len(trailer) + 2
          if trailer_bytes > MAX_TRAILER_BYTES:
            raise RequestBodyError("request body trailers are too long")
          if not trailer:
            break
        self.done = True
        raise StopIteration
      self.remaining = size

    read = getattr(self.stream, "read1", self.stream.read)
    chunk = read(min(self.remaining, BODY_CHUNK_BYTES))
    if not chunk:
      raise RequestBodyError("request body ended during chunk data")
    self.remaining -= len(chunk)
    if self.remaining == 0 and self.stream.read(2) != b"\r\n":
      raise RequestBodyError("chunk data is missing its terminator")
    return chunk


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

  def _request_body(self) -> tuple[object | None, bool]:
    content_lengths = self.headers.get_all("Content-Length", [])
    transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
    if content_lengths and transfer_encodings:
      raise RequestBodyError(
        "Content-Length and Transfer-Encoding cannot be combined"
      )

    if content_lengths:
      if len(content_lengths) != 1:
        raise RequestBodyError("multiple Content-Length headers")
      raw_length = content_lengths[0].strip()
      if (
        not raw_length.isascii()
        or not raw_length.isdecimal()
        or len(raw_length) > MAX_CONTENT_LENGTH_DIGITS
      ):
        raise RequestBodyError("invalid Content-Length")
      length = int(raw_length)
      return (
        FixedLengthBody(self.rfile, length) if length > 0 else None,
        False,
      )

    if transfer_encodings:
      if (
        len(transfer_encodings) != 1
        or transfer_encodings[0].strip().lower() != "chunked"
      ):
        raise UnsupportedTransferEncoding(
          "only chunked Transfer-Encoding is supported"
        )
      return ChunkedBody(self.rfile), True

    return None, False

  def _headers(self) -> dict[str, str]:
    connection_tokens = _comma_separated_tokens(
      self.headers.get_all("Connection", [])
    )
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
    try:
      body, encode_chunked = self._request_body()
    except RequestBodyError as exc:
      self._plain(exc.status, str(exc))
      return

    host, port = self._target()
    conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT_SECONDS)
    try:
      conn.request(
        self.command,
        self.path,
        body=body,
        headers=self._headers(),
        encode_chunked=encode_chunked,
      )
      resp = conn.getresponse()
    except RequestBodyError as exc:
      conn.close()
      self._plain(exc.status, str(exc))
      return
    except OSError as exc:
      conn.close()
      self._plain(HTTPStatus.BAD_GATEWAY, f"Mobius upstream unavailable: {exc}")
      return

    try:
      self.send_response(resp.status, resp.reason)
      response_headers = resp.getheaders()
      response_connection_tokens = _comma_separated_tokens(
        value for key, value in response_headers
        if key.lower() == "connection"
      )
      blocked_response_headers = (
        HOP_BY_HOP_HEADERS | response_connection_tokens
      )
      # HTTPResponse decodes upstream chunk framing before read1() returns.
      # A stray Content-Length alongside Transfer-Encoding therefore no longer
      # describes the bytes sent downstream; close-delimit that response.
      if resp.chunked:
        blocked_response_headers.add("content-length")
      for key, value in response_headers:
        if key.lower() in blocked_response_headers:
          continue
        self.send_header(key, value)
      self.send_header("Connection", "close")
      self.end_headers()
      if self.command != "HEAD":
        while True:
          # read() tries to fill the requested byte count before returning.
          # That is fatal for Server-Sent Events: tiny events and keepalives
          # remain buffered until the stream ends or reaches 64 KB, leaving
          # Railway's edge with an apparently idle downstream request. read1()
          # returns the next available decoded chunk so each event crosses the
          # gateway as soon as the app emits it.
          chunk = resp.read1(64 * 1024)
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
