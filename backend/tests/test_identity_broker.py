from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest


BROKER_PATH = Path(__file__).parents[1] / "runtime" / "identity_broker.py"
ENTRYPOINT_PATH = Path(__file__).parents[1] / "scripts" / "entrypoint.sh"
SPEC = importlib.util.spec_from_file_location("mobius_identity_broker", BROKER_PATH)
assert SPEC and SPEC.loader
broker_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker_module)


def test_entrypoint_preserves_managed_identity_credential():
  source = ENTRYPOINT_PATH.read_text()
  assert "unset MOBIUS_COMPUTE_INSTANCE_TOKEN" in source
  assert "unset MOBIUS_SSO_CLIENT_SECRET" not in source


@pytest.fixture()
def broker(tmp_path, monkeypatch):
  private = tmp_path / "identity-broker"
  monkeypatch.setattr(broker_module, "PRIVATE_DIR", private)
  monkeypatch.setattr(broker_module, "KEY_PATH", private / "instance-ed25519.pem")
  monkeypatch.setattr(broker_module, "STATE_PATH", private / "identity.json")
  monkeypatch.setattr(broker_module, "INSTANCE_PATH", private / "instance-id")
  monkeypatch.setattr(
    broker_module, "PENDING_BOOTSTRAP_PATH", private / "pending-enrollment.jwt"
  )
  monkeypatch.setattr(broker_module.os, "chown", lambda *_args: None)
  monkeypatch.delenv("MOBIUS_SSO_INSTANCE_ID", raising=False)
  value = broker_module.Broker()
  yield value
  value.close()


def test_private_key_and_identity_state_stay_root_only(broker):
  key_mode = stat.S_IMODE(broker_module.KEY_PATH.stat().st_mode)
  private_mode = stat.S_IMODE(broker_module.PRIVATE_DIR.stat().st_mode)

  assert key_mode == 0o600
  assert private_mode == 0o700
  public = broker.identity()
  assert public["linked"] is False
  assert set(public) == {
    "linked", "issuer", "subject", "instance_id", "key_generation",
    "public_key_jwk", "key_thumbprint",
  }
  assert "private" not in json.dumps(public).lower()


def test_private_directory_and_key_reject_precreated_symlinks(tmp_path, monkeypatch):
  private_target = tmp_path / "attacker-controlled"
  private_target.mkdir()
  private_link = tmp_path / "identity-broker-link"
  private_link.symlink_to(private_target, target_is_directory=True)
  monkeypatch.setattr(broker_module, "PRIVATE_DIR", private_link)

  with pytest.raises(RuntimeError, match="private directory is unsafe"):
    broker_module._prepare_private_dir()

  private = tmp_path / "identity-broker"
  private.mkdir(mode=0o700)
  outside_key = tmp_path / "known-key.pem"
  outside_key.write_text("attacker chosen")
  key_link = private / "instance-ed25519.pem"
  key_link.symlink_to(outside_key)
  monkeypatch.setattr(broker_module, "PRIVATE_DIR", private)
  monkeypatch.setattr(broker_module, "KEY_PATH", key_link)

  with pytest.raises(RuntimeError, match="file is unsafe"):
    broker_module._load_or_create_key()


def test_socket_parent_must_not_be_app_writable(tmp_path, monkeypatch):
  unsafe = tmp_path / "run"
  unsafe.mkdir(mode=0o777)
  unsafe.chmod(0o777)
  monkeypatch.setattr(
    broker_module, "SOCKET_PATH", unsafe / "mobius-identity-broker.sock"
  )

  with pytest.raises(RuntimeError, match="socket directory is unsafe"):
    broker_module._prepare_socket_dir()


def test_unlink_clears_only_matching_identity_and_keeps_instance_keys(broker):
  state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "instance_id": broker.instance_id,
    "key_thumbprint": broker.thumbprint(),
    "key_generation": 1,
  }
  broker_module._atomic_root_write(
    broker_module.STATE_PATH,
    json.dumps(state, separators=(",", ":")).encode(),
  )
  broker.state = state
  key_before = broker_module.KEY_PATH.read_bytes()
  instance_before = broker.instance_id

  with pytest.raises(PermissionError):
    broker.unlink("another_user")
  assert broker.identity()["linked"] is True
  assert broker_module.STATE_PATH.exists()

  unlinked = broker.unlink("user_example")
  assert unlinked["linked"] is False
  assert not broker_module.STATE_PATH.exists()
  assert broker_module.KEY_PATH.read_bytes() == key_before
  assert broker.instance_id == instance_before


def _unsigned_receipt(instance_id, *, expires_in=600):
  header = broker_module._b64(b'{"alg":"HS256","typ":"JWT"}')
  payload = broker_module._b64(json.dumps({
    "instance_id": instance_id,
    "jti": "receipt_test_123456789",
    "exp": int(time.time()) + expires_in,
  }).encode())
  return f"{header}.{payload}.signature"


def test_transient_bootstrap_is_root_persisted_and_survives_restart(
  broker, monkeypatch,
):
  receipt = _unsigned_receipt(broker.instance_id)
  broker.queue_bootstrap(receipt)
  assert broker_module.PENDING_BOOTSTRAP_PATH.read_text() == receipt
  assert stat.S_IMODE(broker_module.PENDING_BOOTSTRAP_PATH.stat().st_mode) == 0o600

  monkeypatch.setattr(
    broker, "enroll", lambda _receipt: (_ for _ in ()).throw(httpx.ConnectError("down"))
  )
  assert broker.retry_pending_once() is False
  assert broker_module.PENDING_BOOTSTRAP_PATH.exists()

  # A fresh broker process loads the same root key/instance and can consume the
  # still-pending receipt after the central service recovers.
  restarted = broker_module.Broker()
  try:
    def recovered(_receipt):
      restarted.state = {
        "issuer": "https://www.mobius.you",
        "subject": "user_example",
        "instance_id": restarted.instance_id,
        "key_thumbprint": restarted.thumbprint(),
        "key_generation": 1,
      }
      return restarted.identity()

    monkeypatch.setattr(restarted, "enroll", recovered)
    assert restarted.retry_pending_once() is True
    assert not broker_module.PENDING_BOOTSTRAP_PATH.exists()
  finally:
    restarted.close()


def test_mobius_uid_cannot_read_broker_files_or_root_process_environment(broker):
  if os.geteuid() != 0:
    pytest.skip("requires root to exercise the production UID boundary")
  if subprocess.run(
    ["id", "mobius"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
  ).returncode:
    pytest.skip("production mobius user is not installed")
  broker_module._atomic_root_write(broker_module.STATE_PATH, b'{"linked":true}')
  for protected in (broker_module.KEY_PATH, broker_module.STATE_PATH):
    result = subprocess.run(
      ["runuser", "-u", "mobius", "--", "cat", str(protected)],
      capture_output=True,
    )
    assert result.returncode != 0

  process = subprocess.Popen(
    ["python3", "-c", "import time; time.sleep(10)"],
    env={**os.environ, "MOBIUS_TEST_DURABLE_SECRET": "must-not-leak"},
  )
  try:
    result = subprocess.run(
      ["runuser", "-u", "mobius", "--", "cat", f"/proc/{process.pid}/environ"],
      capture_output=True,
    )
    assert b"must-not-leak" not in result.stdout
    assert result.returncode != 0
  finally:
    process.terminate()
    process.wait(timeout=5)


def test_proxy_surface_is_limited_to_declared_inference_routes(
  broker, monkeypatch,
):
  monkeypatch.setattr(broker, "_capability", lambda **_kwargs: "capability")

  for method, path in (
    ("POST", "/v1/contributions"),
    ("GET", "/v1/community/apps"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/identity/oauth/start"),
    ("GET", "/identity"),
    ("GET", "/v1/models?admin=true"),
    ("GET", "/v1/models#admin"),
  ):
    with pytest.raises(FileNotFoundError):
      broker.proxy(method=method, path=path, body=b"{}", headers={})


def test_proxy_streams_identity_encoding_and_never_forwards_caller_target(
  broker, monkeypatch,
):
  seen = {}

  class FakeClient:
    def build_request(self, method, url, **kwargs):
      seen["method"] = method
      seen["url"] = url
      seen["kwargs"] = kwargs
      return httpx.Request(method, url, content=kwargs.get("content"), headers=kwargs["headers"])

    def send(self, request, *, stream):
      seen["stream"] = stream
      return httpx.Response(
        200, request=request, stream=httpx.ByteStream(b"event: done\n\n")
      )

    def close(self):
      return None

  broker.client.close()
  broker.client = FakeClient()
  broker.state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "key_generation": 1,
  }
  monkeypatch.setattr(broker, "_capability", lambda **_kwargs: "one-use")
  response = broker.proxy(
    method="GET", path="/v1/models", body=b"",
    headers={
      "accept-encoding": "gzip",
      "x-forwarded-host": "attacker.example",
    },
  )
  try:
    assert seen["url"] == broker_module.GATEWAY_BASE_URL + "/v1/models"
    assert seen["stream"] is True
    assert seen["kwargs"]["headers"]["Accept-Encoding"] == "identity"
    assert "attacker.example" not in json.dumps(seen)
    assert response.read() == b"event: done\n\n"
  finally:
    response.close()


def test_gateway_capability_wraps_the_exact_signed_request_binding(broker):
  seen = {}

  def handler(request: httpx.Request):
    seen["request"] = request
    return httpx.Response(200, json={"capability": "header.payload.signature"})

  broker.client.close()
  broker.client = httpx.Client(transport=httpx.MockTransport(handler))
  broker.state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "key_generation": 7,
  }
  body = b'{"model":"inkling","input":"hello"}'

  assert broker._capability(
    audience="mobius-agent-gateway",
    scope="inference:responses",
    method="POST",
    path="/v1/responses",
    body=body,
    request_id="turn:12345678",
  ) == "header.payload.signature"

  request = seen["request"]
  assert request.url == httpx.URL(
    broker_module.IDENTITY_BASE_URL + "/identity/capabilities"
  )
  payload = json.loads(request.content)
  assert set(payload) == {"assertion"}
  envelope = payload["assertion"]
  claims = envelope["claims"]
  assert claims["aud"] == "mobius-agent-gateway"
  assert claims["scope"] == "inference:responses"
  assert claims["method"] == "POST"
  assert claims["path"] == "/v1/responses"
  assert claims["body_sha256"] == hashlib.sha256(body).hexdigest()
  assert claims["request_id"] == "turn:12345678"
  canonical = json.dumps(
    claims, sort_keys=True, separators=(",", ":")
  ).encode()
  broker.key.public_key().verify(
    broker_module._unb64(envelope["signature"]), canonical,
  )


def test_handler_only_supports_identity_and_inference_http_methods():
  handler = broker_module._Handler
  assert handler.do_GET is handler._handle
  assert handler.do_POST is handler._handle
  assert not hasattr(handler, "do_PUT")
  assert not hasattr(handler, "do_DELETE")


def test_contribution_proxy_is_uds_only_and_binds_exact_requests(
  broker, monkeypatch,
):
  seen = {}

  class FakeClient:
    def build_request(self, method, url, **kwargs):
      seen["url"] = url
      seen["headers"] = kwargs["headers"]
      return httpx.Request(
        method, url, content=kwargs.get("content"), headers=kwargs["headers"],
      )

    def send(self, request, *, stream):
      return httpx.Response(
        200, request=request, stream=httpx.ByteStream(b"{}"),
      )

    def close(self):
      return None

  capabilities = []
  broker.client.close()
  broker.client = FakeClient()
  monkeypatch.setattr(
    broker, "_capability",
    lambda **kwargs: capabilities.append(kwargs) or "one-use",
  )
  create_path = "/v1/contributions"
  create_body = b'{"repo":"mobius-os/mobius"}'
  key = "contribution:0123456789abcdef"

  with pytest.raises(FileNotFoundError):
    broker.proxy(
      method="POST", path=create_path, body=create_body,
      headers={"idempotency-key": key},
    )

  response = broker.proxy(
    method="POST", path=create_path, body=create_body,
    headers={"idempotency-key": key}, allow_contributions=True,
  )
  response.close()
  assert seen["url"] == broker_module.CONTRIBUTION_BASE_URL + create_path
  assert seen["headers"]["Idempotency-Key"] == key
  assert capabilities[-1]["audience"] == "mobius-contribution-relay"
  assert capabilities[-1]["scope"] == "contribution:submit"
  assert capabilities[-1]["idempotency_key"] == key

  contribution = "ctr_1234567890abcdef1234567890abcdef"
  response = broker.proxy(
    method="GET", path=f"/v1/contributions/{contribution}", body=b"",
    headers={}, allow_contributions=True,
  )
  response.close()
  assert capabilities[-1]["scope"] == "contribution:read"

  withdraw_path = f"/v1/contributions/{contribution}/withdraw"
  response = broker.proxy(
    method="POST", path=withdraw_path, body=b'{"revision":1}',
    headers={"idempotency-key": "withdraw:0123456789abcdef"},
    allow_contributions=True,
  )
  response.close()
  assert capabilities[-1]["scope"] == "contribution:withdraw"

  for method, forbidden in (
    ("GET", "/v1/contributions/github/status"),
    ("GET", f"/v1/contributions/{contribution}?subject=other"),
    ("DELETE", f"/v1/contributions/{contribution}"),
    ("POST", "https://evil.test/v1/contributions"),
  ):
    with pytest.raises(FileNotFoundError):
      broker.proxy(
        method=method, path=forbidden, body=b"", headers={},
        allow_contributions=True,
      )


def test_contribution_mutations_require_idempotency(broker):
  with pytest.raises(ValueError, match="idempotency key is required"):
    broker.proxy(
      method="POST", path="/v1/contributions", body=b"{}", headers={},
      allow_contributions=True,
    )


def test_large_body_exception_is_only_for_exact_contribution_mutations():
  contribution = "ctr_1234567890abcdef1234567890abcdef"
  assert broker_module._request_body_limit(
    is_unix=True, method="POST", path="/v1/contributions",
  ) == broker_module.MAX_CONTRIBUTION_BODY
  assert broker_module._request_body_limit(
    is_unix=True, method="POST",
    path=f"/v1/contributions/{contribution}/withdraw",
  ) == broker_module.MAX_CONTRIBUTION_BODY
  for is_unix, method, path in (
    (False, "POST", "/v1/contributions"),
    (True, "POST", "/v1/contributions?x=1"),
    (True, "GET", f"/v1/contributions/{contribution}"),
    (True, "POST", "/v1/community/apps"),
  ):
    assert broker_module._request_body_limit(
      is_unix=is_unix, method=method, path=path,
    ) == broker_module.MAX_BODY


def test_unix_handler_rejects_identity_queries_and_forwards_inference():
  # AF_UNIX paths are capped at roughly 108 bytes on Linux; long worktree
  # paths can make pytest's ordinary tmp_path exceed that.
  socket_dir = tempfile.TemporaryDirectory(prefix="mobius-broker-")
  socket_path = Path(socket_dir.name) / "broker.sock"
  seen = []

  class FakeBroker:
    def identity(self):
      return {"linked": True}

    def proxy(
      self, *, method, path, body, headers, allow_contributions=False,
    ):
      seen.append((method, path, body))
      request = httpx.Request(method, "https://central.test" + path)
      payload = json.dumps({"method": method}).encode()
      return httpx.Response(
        200,
        request=request,
        headers={"Content-Type": "application/json"},
        stream=httpx.ByteStream(payload),
      )

  server = broker_module._UnixServer(str(socket_path), broker_module._Handler)
  server.broker = FakeBroker()
  server.is_unix = True
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://broker") as client:
      assert client.get("/identity").json() == {"linked": True}
      assert client.get("/identity?subject=user_other").status_code == 404
      models = client.get("/v1/models")
    assert models.json() == {"method": "GET"}
    assert seen == [("GET", "/v1/models", b"")]
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    socket_dir.cleanup()
