import importlib.util
import io
import http.client
import socket
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_GATEWAY_PATH = (
  Path(__file__).resolve().parents[1] / "scripts" / "railway_gateway.py"
)


def _load_gateway():
  spec = importlib.util.spec_from_file_location("railway_gateway", _GATEWAY_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


def test_recover_paths_route_to_recoveryd():
  gateway = _load_gateway()
  assert gateway.is_recovery_path("/recover") is True
  assert gateway.is_recovery_path("/recover/") is True
  assert gateway.is_recovery_path("/recover/chat") is True
  assert gateway.is_recovery_path("/api/health") is False
  assert gateway.is_recovery_path("/recovering") is False


def test_parse_upstream_accepts_http_urls_and_host_ports():
  gateway = _load_gateway()
  assert gateway.parse_upstream("http://127.0.0.1:18000") == (
    "127.0.0.1", 18000)
  assert gateway.parse_upstream("localhost:18001") == ("localhost", 18001)
  assert gateway.parse_upstream("http://mobius.internal") == (
    "mobius.internal", 80)


def test_parse_upstream_rejects_non_http_urls():
  gateway = _load_gateway()
  with pytest.raises(ValueError):
    gateway.parse_upstream("https://example.test")


def test_gateway_has_no_app_imports():
  src = _GATEWAY_PATH.read_text()
  for line in src.splitlines():
    stripped = line.strip()
    assert not stripped.startswith("import app"), stripped
    assert not stripped.startswith("from app "), stripped
    assert not stripped.startswith("from app."), stripped


def _body_handler(gateway, headers, body=b""):
  handler = object.__new__(gateway.Gateway)
  parsed = Message()
  for key, value in headers:
    parsed[key] = value
  handler.headers = parsed
  handler.rfile = io.BytesIO(body)
  return handler


def test_fixed_length_body_is_bounded_and_never_reads_the_next_request():
  gateway = _load_gateway()
  following_request = b"GET /next HTTP/1.1\r\n\r\n"
  stream = io.BytesIO(b"x" * (gateway.BODY_CHUNK_BYTES + 5) + following_request)
  body = gateway.FixedLengthBody(stream, gateway.BODY_CHUNK_BYTES + 5)

  chunks = []
  while chunk := body.read(1_000_000):
    chunks.append(chunk)

  assert max(map(len, chunks)) <= gateway.BODY_CHUNK_BYTES
  assert b"".join(chunks) == b"x" * (gateway.BODY_CHUNK_BYTES + 5)
  assert stream.read() == following_request


def test_chunked_body_decodes_incrementally_and_consumes_trailers():
  gateway = _load_gateway()
  handler = _body_handler(gateway, [("Transfer-Encoding", "chunked")], (
    b"4;fixture=yes\r\nWiki\r\n"
    b"5\r\npedia\r\n"
    b"0\r\nIgnored-Trailer: value\r\n\r\n"
    b"GET /next HTTP/1.1\r\n\r\n"
  ))

  body, encode_chunked = handler._request_body()

  assert encode_chunked is True
  assert b"".join(body) == b"Wikipedia"
  assert handler.rfile.read() == b"GET /next HTTP/1.1\r\n\r\n"


def test_chunked_body_bounds_one_large_downstream_chunk():
  gateway = _load_gateway()
  payload = b"x" * (gateway.BODY_CHUNK_BYTES + 5)
  framed = (
    f"{len(payload):X}\r\n".encode("ascii")
    + payload
    + b"\r\n0\r\n\r\n"
  )

  chunks = list(gateway.ChunkedBody(io.BytesIO(framed)))

  assert max(map(len, chunks)) <= gateway.BODY_CHUNK_BYTES
  assert b"".join(chunks) == payload


@pytest.mark.parametrize(
  ("headers", "error", "status"),
  [
    (
      [("Content-Length", "1"), ("Transfer-Encoding", "chunked")],
      "Content-Length and Transfer-Encoding cannot be combined",
      400,
    ),
    ([("Content-Length", "-1")], "invalid Content-Length", 400),
    ([("Transfer-Encoding", "gzip")], "only chunked", 501),
    ([("Transfer-Encoding", "gzip, chunked")], "only chunked", 501),
  ],
)
def test_request_body_rejects_ambiguous_or_unsupported_framing(
  headers, error, status,
):
  gateway = _load_gateway()
  handler = _body_handler(gateway, headers)

  with pytest.raises(gateway.RequestBodyError, match=error) as exc:
    handler._request_body()
  assert exc.value.status == status


def test_request_body_rejects_unbounded_numeric_framing_fields():
  gateway = _load_gateway()
  handler = _body_handler(
    gateway,
    [("Content-Length", "9" * (gateway.MAX_CONTENT_LENGTH_DIGITS + 1))],
  )
  with pytest.raises(gateway.RequestBodyError, match="invalid Content-Length"):
    handler._request_body()

  framed = (
    b"f" * (gateway.MAX_CHUNK_SIZE_DIGITS + 1)
    + b"\r\n0\r\n\r\n"
  )
  with pytest.raises(gateway.RequestBodyError, match="invalid chunk size"):
    next(iter(gateway.ChunkedBody(io.BytesIO(framed))))


def test_zero_length_request_body_is_an_ordinary_empty_body():
  gateway = _load_gateway()
  handler = _body_handler(gateway, [("Content-Length", "0")])

  body, encode_chunked = handler._request_body()

  assert body is None
  assert encode_chunked is False


def test_gateway_removes_tokens_from_every_connection_header():
  gateway = _load_gateway()
  handler = _body_handler(
    gateway,
    [
      ("Connection", "X-First"),
      ("Connection", "X-Second, keep-alive"),
      ("X-First", "private"),
      ("X-Second", "private"),
      ("X-End-To-End", "public"),
    ],
  )
  handler.client_address = ("127.0.0.1", 1234)

  headers = handler._headers()

  assert "X-First" not in headers
  assert "X-Second" not in headers
  assert headers["X-End-To-End"] == "public"


def test_gateway_streams_fixed_length_upload_before_client_finishes():
  gateway = _load_gateway()
  first_received = threading.Event()
  received = []

  class UploadTarget(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
      length = int(self.headers["Content-Length"])
      received.append(self.rfile.read(5))
      first_received.set()
      received.append(self.rfile.read(length - 5))
      self.send_response(204)
      self.send_header("Content-Length", "0")
      self.end_headers()

    def log_message(self, _format, *_args):
      pass

  upstream = ThreadingHTTPServer(("127.0.0.1", 0), UploadTarget)
  gateway.Gateway.app_upstream = ("127.0.0.1", upstream.server_port)
  gateway.Gateway.recovery_upstream = gateway.Gateway.app_upstream
  proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Gateway)
  threads = [
    threading.Thread(target=server.serve_forever, daemon=True)
    for server in (upstream, proxy)
  ]
  for thread in threads:
    thread.start()

  client = socket.create_connection(("127.0.0.1", proxy.server_port), timeout=2)
  try:
    client.sendall(
      b"POST /upload HTTP/1.1\r\n"
      b"Host: example.test\r\n"
      b"Content-Length: 10\r\n"
      b"Connection: close\r\n\r\n"
      b"first"
    )
    assert first_received.wait(timeout=1), (
      "the gateway buffered the body instead of forwarding available bytes"
    )
    client.sendall(b"last!")
    response = b""
    while chunk := client.recv(4096):
      response += chunk
    assert b" 204 " in response.split(b"\r\n", 1)[0]
    assert received == [b"first", b"last!"]
  finally:
    client.close()
    for server in (proxy, upstream):
      server.shutdown()
      server.server_close()


def test_gateway_decodes_and_rechunks_streaming_uploads():
  gateway = _load_gateway()
  received = []

  class ChunkedUploadTarget(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
      assert self.headers["Transfer-Encoding"].lower() == "chunked"
      while True:
        size = int(self.rfile.readline().strip(), 16)
        if size == 0:
          assert self.rfile.readline() == b"\r\n"
          break
        received.append(self.rfile.read(size))
        assert self.rfile.read(2) == b"\r\n"
      self.send_response(204)
      self.send_header("Content-Length", "0")
      self.end_headers()

    def log_message(self, _format, *_args):
      pass

  upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedUploadTarget)
  gateway.Gateway.app_upstream = ("127.0.0.1", upstream.server_port)
  gateway.Gateway.recovery_upstream = gateway.Gateway.app_upstream
  proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Gateway)
  threads = [
    threading.Thread(target=server.serve_forever, daemon=True)
    for server in (upstream, proxy)
  ]
  for thread in threads:
    thread.start()

  client = http.client.HTTPConnection(
    "127.0.0.1", proxy.server_port, timeout=2,
  )
  try:
    client.request(
      "POST",
      "/upload",
      body=iter((b"Wiki", b"pedia")),
      encode_chunked=True,
    )
    response = client.getresponse()
    assert response.status == 204
    assert response.read() == b""
    assert received == [b"Wiki", b"pedia"]
  finally:
    client.close()
    for server in (proxy, upstream):
      server.shutdown()
      server.server_close()


def test_gateway_close_delimits_decoded_chunked_responses():
  gateway = _load_gateway()
  payload = b"decoded response"

  class ChunkedTarget(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
      self.send_response(200)
      self.send_header("Transfer-Encoding", "chunked")
      self.send_header("Content-Length", "999")
      self.send_header("Connection", "X-Upstream-Only")
      self.send_header("X-Upstream-Only", "private")
      self.end_headers()
      self.wfile.write(
        f"{len(payload):X}\r\n".encode("ascii")
        + payload
        + b"\r\n0\r\n\r\n"
      )
      self.wfile.flush()

    def log_message(self, _format, *_args):
      pass

  upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedTarget)
  gateway.Gateway.app_upstream = ("127.0.0.1", upstream.server_port)
  gateway.Gateway.recovery_upstream = gateway.Gateway.app_upstream
  proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Gateway)
  threads = [
    threading.Thread(target=server.serve_forever, daemon=True)
    for server in (upstream, proxy)
  ]
  for thread in threads:
    thread.start()

  client = http.client.HTTPConnection(
    "127.0.0.1", proxy.server_port, timeout=2,
  )
  try:
    client.request("GET", "/")
    response = client.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Length") is None
    assert response.getheader("Transfer-Encoding") is None
    assert response.getheader("X-Upstream-Only") is None
    assert response.read() == payload
  finally:
    client.close()
    for server in (proxy, upstream):
      server.shutdown()
      server.server_close()


def test_gateway_forwards_sse_before_the_upstream_stream_finishes():
  gateway = _load_gateway()
  first_event = b"data: first\n\n"
  release_upstream = threading.Event()
  first_written = threading.Event()

  class DelayedSSE(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
      self.send_response(200)
      self.send_header("Content-Type", "text/event-stream")
      self.send_header("Transfer-Encoding", "chunked")
      self.end_headers()
      self.wfile.write(
        f"{len(first_event):X}\r\n".encode("ascii") + first_event + b"\r\n"
      )
      self.wfile.flush()
      first_written.set()
      release_upstream.wait(timeout=2)
      self.wfile.write(b"0\r\n\r\n")
      self.wfile.flush()

    def log_message(self, _format, *_args):
      pass

  upstream = ThreadingHTTPServer(("127.0.0.1", 0), DelayedSSE)
  gateway.Gateway.app_upstream = ("127.0.0.1", upstream.server_port)
  gateway.Gateway.recovery_upstream = gateway.Gateway.app_upstream
  proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Gateway)
  threads = [
    threading.Thread(target=server.serve_forever, daemon=True)
    for server in (upstream, proxy)
  ]
  for thread in threads:
    thread.start()

  client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=1)
  try:
    client.request("GET", "/api/chats/example/stream")
    response = client.getresponse()
    assert response.status == 200
    assert first_written.wait(timeout=1)
    # The upstream is deliberately still open here. A buffering gateway times
    # out instead of returning this first SSE event.
    assert response.read1(len(first_event)) == first_event
  finally:
    release_upstream.set()
    client.close()
    for server in (proxy, upstream):
      server.shutdown()
      server.server_close()
