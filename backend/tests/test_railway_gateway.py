import importlib.util
import http.client
import threading
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


def test_railway_entrypoint_supervises_gateway_and_app_processes():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text()
  assert "_wait_for_railway_child_exit" in entrypoint
  assert '_railway_child_running "$_gateway_pid"' in entrypoint
  assert '_railway_child_running "$_app_pid"' in entrypoint
  assert '"/proc/${_child_pid}/status"' in entrypoint
  assert 'wait "$_gateway_pid"' in entrypoint
  assert 'wait "$_app_pid"' in entrypoint
  assert "A clean child exit is still a service failure" in entrypoint
