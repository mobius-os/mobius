from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


BROKER_PATH = Path(__file__).parents[1] / "runtime" / "identity_broker.py"
SPEC = importlib.util.spec_from_file_location("mobius_identity_broker", BROKER_PATH)
assert SPEC and SPEC.loader
broker_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker_module)


def test_broker_and_app_consumers_share_the_root_owned_socket():
  backend = Path(__file__).parents[1]
  socket = "/run/mobius-identity-broker.sock"

  assert str(broker_module.SOCKET_PATH) == socket
  for relative in (
    "app/runtime_identity.py",
    "app/contribution_broker.py",
    "app/community_broker.py",
    "app/providers.py",
    "scripts/entrypoint.sh",
  ):
    source = (backend / relative).read_text(encoding="utf-8")
    assert socket in source
    assert "/data/run/mobius-identity-broker.sock" not in source


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
  monkeypatch.setattr(
    broker_module, "OAUTH_STATE_PATH", private / "oauth-states.json"
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


def _legacy_state_paths(tmp_path, monkeypatch):
  private = tmp_path / "identity-broker"
  private.mkdir(mode=0o700)
  key = private / "instance-ed25519.pem"
  key.write_text("legacy-key", encoding="utf-8")
  key.chmod(0o600)
  owner = (os.getuid(), os.getgid())
  monkeypatch.setattr(broker_module, "PRIVATE_DIR", private)
  monkeypatch.setattr(broker_module, "KEY_PATH", key)
  monkeypatch.setattr(broker_module, "STATE_PATH", private / "identity.json")
  monkeypatch.setattr(broker_module, "INSTANCE_PATH", private / "instance-id")
  monkeypatch.setattr(
    broker_module, "PENDING_BOOTSTRAP_PATH", private / "pending-enrollment.jwt"
  )
  monkeypatch.setattr(
    broker_module, "OAUTH_STATE_PATH", private / "oauth-states.json"
  )
  monkeypatch.setattr(broker_module.os, "geteuid", lambda: 0)
  monkeypatch.setattr(
    broker_module.pwd, "getpwnam",
    lambda _name: SimpleNamespace(pw_uid=owner[0], pw_gid=owner[1]),
  )
  return private, key


def _record_adopted_inodes(monkeypatch, on_first_call=None):
  """Capture which inodes the root fixup targets, without needing real root."""
  adopted = []

  def fake_fchown(fd, uid, gid):
    if on_first_call is not None and not adopted:
      on_first_call()
    adopted.append((os.fstat(fd).st_ino, uid, gid))

  monkeypatch.setattr(broker_module.os, "fchown", fake_fchown)
  return adopted


def test_root_broker_reclaims_only_locked_down_legacy_state(tmp_path, monkeypatch):
  private, key = _legacy_state_paths(tmp_path, monkeypatch)
  oauth_state = private / "oauth-states.json"
  oauth_state.write_text("{}", encoding="utf-8")
  oauth_state.chmod(0o600)
  adopted = _record_adopted_inodes(monkeypatch)

  broker_module._reclaim_private_state_after_compat_chown()

  assert adopted == [
    (private.stat().st_ino, 0, 0),
    (key.stat().st_ino, 0, 0),
    (oauth_state.stat().st_ino, 0, 0),
  ]
  assert stat.S_IMODE(private.stat().st_mode) == 0o700
  assert stat.S_IMODE(key.stat().st_mode) == 0o600
  assert stat.S_IMODE(oauth_state.stat().st_mode) == 0o600


def test_root_broker_refuses_symlinked_legacy_state(tmp_path, monkeypatch):
  _private, key = _legacy_state_paths(tmp_path, monkeypatch)
  key.unlink()
  outside = tmp_path / "outside.pem"
  outside.write_text("do not adopt", encoding="utf-8")
  # Locked down like genuine state, so being a symlink is the only reason
  # this can be refused.
  outside.chmod(0o600)
  key.symlink_to(outside)
  adopted = _record_adopted_inodes(monkeypatch)

  with pytest.raises(RuntimeError, match="file is unsafe"):
    broker_module._reclaim_private_state_after_compat_chown()

  assert adopted == []


def test_root_broker_refuses_symlinked_private_directory(tmp_path, monkeypatch):
  private, _key = _legacy_state_paths(tmp_path, monkeypatch)
  outside = tmp_path / "outside-dir"
  outside.mkdir(mode=0o700)
  link = tmp_path / "identity-broker-link"
  link.symlink_to(outside, target_is_directory=True)
  monkeypatch.setattr(broker_module, "PRIVATE_DIR", link)
  adopted = _record_adopted_inodes(monkeypatch)

  with pytest.raises(RuntimeError, match="private directory is unsafe"):
    broker_module._reclaim_private_state_after_compat_chown()

  assert adopted == []
  assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_root_broker_fixup_survives_entry_swapped_after_validation(
  tmp_path, monkeypatch
):
  """A validated entry replaced mid-run must not redirect the root fixup."""
  private, key = _legacy_state_paths(tmp_path, monkeypatch)
  key_inode = key.stat().st_ino
  outside = tmp_path / "outside.pem"
  outside.write_text("do not adopt", encoding="utf-8")
  outside.chmod(0o644)

  def swap_key_for_symlink():
    key.unlink()
    key.symlink_to(outside)

  adopted = _record_adopted_inodes(monkeypatch, on_first_call=swap_key_for_symlink)

  broker_module._reclaim_private_state_after_compat_chown()

  assert adopted == [(private.stat().st_ino, 0, 0), (key_inode, 0, 0)]
  assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_root_broker_refuses_linked_legacy_state(tmp_path, monkeypatch):
  _private, key = _legacy_state_paths(tmp_path, monkeypatch)
  key.unlink()
  outside = tmp_path / "outside"
  outside.write_text("do not adopt", encoding="utf-8")
  os.link(outside, key)

  with pytest.raises(RuntimeError, match="file is unsafe"):
    broker_module._reclaim_private_state_after_compat_chown()


def test_root_broker_refuses_permissive_legacy_state(tmp_path, monkeypatch):
  _private, key = _legacy_state_paths(tmp_path, monkeypatch)
  key.chmod(0o640)

  with pytest.raises(RuntimeError, match="file is unsafe"):
    broker_module._reclaim_private_state_after_compat_chown()


def test_root_broker_refuses_planted_fifo_without_stalling(tmp_path, monkeypatch):
  """The FIFO is refused, and opening it must not wait for a writer.

  A FIFO is the shape that isolates the S_ISREG guard: it has one link, the
  expected owner, and a private mode, so every other rejection clause passes
  it. A planted directory is already refused by the link check.
  """
  _private, key = _legacy_state_paths(tmp_path, monkeypatch)
  key.unlink()
  os.mkfifo(key, 0o600)
  adopted = _record_adopted_inodes(monkeypatch)

  def _stalled(_signum, _frame):
    raise AssertionError("opening the planted FIFO blocked on a writer")

  # Without O_NONBLOCK this open waits forever for a writer that never comes,
  # so fail the assertion instead of hanging the suite.
  previous = signal.signal(signal.SIGALRM, _stalled)
  signal.alarm(5)
  try:
    with pytest.raises(RuntimeError, match="file is unsafe"):
      broker_module._reclaim_private_state_after_compat_chown()
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)

  assert adopted == []


def test_oauth_state_rejects_a_precreated_symlink(broker, tmp_path):
  target = tmp_path / "attacker-oauth.json"
  target.write_text("{}", encoding="utf-8")
  broker_module.OAUTH_STATE_PATH.symlink_to(target)

  with pytest.raises(RuntimeError, match="file is unsafe"):
    broker._oauth_states()


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
  broker_module._atomic_root_write(broker_module.OAUTH_STATE_PATH, b"{}")
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
  assert not broker_module.OAUTH_STATE_PATH.exists()
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


def test_browser_oauth_state_is_root_persisted_multi_worker_safe_and_single_use(broker):
  state = "state_" + ("x" * 40)
  value = {
    "state": state,
    "owner": "owner",
    "verifier": "verifier_" + ("v" * 40),
    "instance_id": broker.instance_id,
    "public_key_jwk": broker.public_jwk(),
    "redirect_uri": "https://example.test/api/auth/provider/mobius/callback",
    "expires_at": time.time() + 300,
  }
  broker.save_oauth_state(value)
  assert stat.S_IMODE(broker_module.OAUTH_STATE_PATH.stat().st_mode) == 0o600

  # A second broker object represents another backend worker/process using the
  # same root-owned state boundary.
  other_worker = broker_module.Broker()
  try:
    assert other_worker.consume_oauth_state(state) == value
    assert broker.consume_oauth_state(state) is None
  finally:
    other_worker.close()


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


def test_loopback_surface_cannot_reach_identity_contribution_or_generic_targets(
  broker, monkeypatch,
):
  monkeypatch.setattr(broker, "_capability", lambda **_kwargs: "capability")

  with pytest.raises(FileNotFoundError):
    broker.proxy(
      method="POST", path="/v1/contributions", body=b"{}", headers={},
      allow_contributions=False,
    )
  with pytest.raises(FileNotFoundError):
    broker.proxy(
      method="POST", path="/v1/chat/completions", body=b"{}", headers={},
      allow_contributions=False,
    )
  with pytest.raises(FileNotFoundError):
    broker.proxy(
      method="GET", path="/identity", body=b"", headers={},
      allow_contributions=False,
    )


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
    allow_contributions=False,
  )
  try:
    assert seen["url"] == broker_module.GATEWAY_BASE_URL + "/v1/models"
    assert seen["stream"] is True
    assert seen["kwargs"]["headers"]["Accept-Encoding"] == "identity"
    assert "attacker.example" not in json.dumps(seen)
    assert response.read() == b"event: done\n\n"
  finally:
    response.close()


def test_contribution_proxy_binds_and_forwards_idempotency_key(broker, monkeypatch):
  seen = {}

  class FakeClient:
    def build_request(self, method, url, **kwargs):
      seen["url"] = url
      seen["headers"] = kwargs["headers"]
      return httpx.Request(method, url, content=kwargs.get("content"), headers=kwargs["headers"])

    def send(self, request, *, stream):
      return httpx.Response(201, request=request, content=b"{}")

    def close(self):
      return None

  broker.client.close()
  broker.client = FakeClient()
  broker.state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "key_generation": 1,
  }
  def capability(**kwargs):
    seen["capability"] = kwargs
    return "token"

  monkeypatch.setattr(broker, "_capability", capability)
  key = "contribution:0123456789abcdef"
  response = broker.proxy(
    method="POST",
    path="/v1/contributions",
    body=b'{"repo":"mobius-os/mobius"}',
    headers={"idempotency-key": key, "x-mobius-request-id": "request:12345678"},
    allow_contributions=True,
  )
  response.close()

  assert seen["url"] == broker_module.CONTRIBUTION_BASE_URL + "/v1/contributions"
  assert seen["headers"]["Idempotency-Key"] == key
  assert seen["capability"]["idempotency_key"] == key
  assert seen["capability"]["audience"] == "mobius-contribution-relay"
  assert seen["capability"]["scope"] == "contribution:submit"

  response = broker.proxy(
    method="GET",
    path="/v1/contributions/ctr_1234567890abcdef1234567890abcdef",
    body=b"",
    headers={"x-mobius-request-id": "request:12345678"},
    allow_contributions=True,
  )
  response.close()
  assert seen["capability"]["scope"] == "contribution:read"
  assert seen["capability"]["method"] == "GET"

  response = broker.proxy(
    method="POST",
    path="/v1/contributions/ctr_1234567890abcdef1234567890abcdef/withdraw",
    body=b'{"contract_version":1,"revision":1}',
    headers={
      "idempotency-key": "mobius-withdraw:0123456789abcdef",
      "x-mobius-request-id": "request:12345678",
    },
    allow_contributions=True,
  )
  response.close()
  assert seen["capability"]["scope"] == "contribution:withdraw"
  assert seen["capability"]["method"] == "POST"

  for method, forbidden in (
    ("GET", "/v1/contributions/github/status"),
    ("DELETE", "/v1/contributions/github"),
  ):
    with pytest.raises(FileNotFoundError):
      broker.proxy(
        method=method, path=forbidden, body=b"", headers={},
        allow_contributions=True,
      )


def test_capability_request_wraps_exact_signed_request_binding(broker):
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
  body = b'{"files":[],"repo":"example-owner/mobius"}'
  key = "mobius-pr:1234567890abcdef"

  assert broker._capability(
    audience="mobius-contribution-relay",
    scope="contribution:submit",
    method="POST",
    path="/v1/contributions",
    body=body,
    request_id="contribution:12345678",
    idempotency_key=key,
  ) == "header.payload.signature"

  request = seen["request"]
  assert request.url == httpx.URL(
    broker_module.IDENTITY_BASE_URL + "/identity/capabilities"
  )
  payload = json.loads(request.content)
  assert set(payload) == {"assertion"}
  envelope = payload["assertion"]
  claims = envelope["claims"]
  assert claims["aud"] == "mobius-contribution-relay"
  assert claims["scope"] == "contribution:submit"
  assert claims["method"] == "POST"
  assert claims["path"] == "/v1/contributions"
  assert claims["body_sha256"] == hashlib.sha256(body).hexdigest()
  assert claims["idempotency_key_sha256"] == hashlib.sha256(
    key.encode("utf-8")
  ).hexdigest()
  assert claims["request_id"] == "contribution:12345678"
  canonical = json.dumps(
    claims, sort_keys=True, separators=(",", ":")
  ).encode()
  broker.key.public_key().verify(
    broker_module._unb64(envelope["signature"]), canonical,
  )


def test_community_proxy_binds_canonical_query_and_rejects_route_expansion(
  broker, monkeypatch,
):
  seen = {}

  class FakeClient:
    def build_request(self, method, url, **kwargs):
      seen["url"] = url
      return httpx.Request(
        method, url, content=kwargs.get("content"), headers=kwargs["headers"],
      )

    def send(self, request, *, stream):
      return httpx.Response(200, request=request, content=b"{}")

    def close(self):
      return None

  broker.client.close()
  broker.client = FakeClient()
  broker.state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "key_generation": 1,
  }
  monkeypatch.setattr(
    broker, "_capability",
    lambda **kwargs: seen.setdefault("capability", kwargs) or "token",
  )
  target = "/v1/community/apps?limit=25&offset=0&q=latex"
  response = broker.proxy(
    method="GET", path=target, body=b"", headers={}, allow_contributions=True,
  )
  response.close()

  assert seen["url"] == broker_module.COMMUNITY_BASE_URL + target
  assert seen["capability"]["path"] == target
  assert seen["capability"]["audience"] == "mobius-community-registry"
  assert seen["capability"]["scope"] == "community:read"

  for forbidden in (
    "/v1/community/apps?offset=0&limit=25",
    "/v1/community/apps?admin=true",
    "/v1/community/apps?limit=25&review_status=unreviewed",
    "/v1/community/apps/app_12345678?limit=2",
    "/v1/community/private-audit",
  ):
    with pytest.raises(FileNotFoundError):
      broker.proxy(
        method="GET", path=forbidden, body=b"", headers={},
        allow_contributions=True,
      )


def test_community_mutations_are_narrow_and_require_idempotency(
  broker, monkeypatch,
):
  seen = []

  class FakeClient:
    def build_request(self, method, url, **kwargs):
      seen.append((method, url, kwargs["headers"]))
      return httpx.Request(
        method, url, content=kwargs.get("content"), headers=kwargs["headers"],
      )

    def send(self, request, *, stream):
      return httpx.Response(200, request=request, content=b"{}")

    def close(self):
      return None

  broker.client.close()
  broker.client = FakeClient()
  broker.state = {
    "issuer": "https://www.mobius.you",
    "subject": "user_example",
    "key_generation": 1,
  }
  capabilities = []
  monkeypatch.setattr(
    broker, "_capability",
    lambda **kwargs: capabilities.append(kwargs) or "token",
  )

  publish_path = "/v1/community/apps"
  with pytest.raises(ValueError, match="idempotency key is required"):
    broker.proxy(
      method="POST", path=publish_path, body=b'{}', headers={},
      allow_contributions=True,
    )
  broker.proxy(
    method="POST", path=publish_path, body=b'{}',
    headers={"idempotency-key": "publish:1234567890abcdef"},
    allow_contributions=True,
  ).close()
  install_path = (
    "/v1/community/apps/app_12345678/revisions/"
    "rev_12345678/installs"
  )
  broker.proxy(
    method="POST", path=install_path, body=b'{}',
    headers={"idempotency-key": "install:1234567890abcdef"},
    allow_contributions=True,
  ).close()
  rating_path = "/v1/community/apps/app_12345678/rating"
  broker.proxy(
    method="PUT", path=rating_path, body=b'{"value":5}',
    headers={"idempotency-key": "rating:1234567890abcdef"},
    allow_contributions=True,
  ).close()
  comment_path = (
    "/v1/community/apps/app_12345678/revisions/"
    "rev_12345678/comments"
  )
  broker.proxy(
    method="POST", path=comment_path, body=b'{"body":"Useful"}',
    headers={"idempotency-key": "comment:1234567890abcdef"},
    allow_contributions=True,
  ).close()
  editorial_asset_path = "/v1/community/editorial/assets"
  broker.proxy(
    method="POST", path=editorial_asset_path, body=b'{"data_base64":"abcd"}',
    headers={"idempotency-key": "editorial:1234567890abcdef"},
    allow_contributions=True,
  ).close()
  editorial_feed_path = "/v1/community/editorial/spotlight"
  broker.proxy(
    method="PUT", path=editorial_feed_path, body=b'{"items":[]}',
    headers={"idempotency-key": "editorial:feed:12345678"},
    allow_contributions=True,
  ).close()

  assert capabilities[0]["scope"] == "community:publish"
  assert capabilities[0]["path"] == publish_path
  assert capabilities[1]["scope"] == "community:install"
  assert capabilities[1]["path"] == install_path
  assert capabilities[2]["scope"] == "community:rate"
  assert capabilities[2]["path"] == rating_path
  assert capabilities[3]["scope"] == "community:comment"
  assert capabilities[3]["path"] == comment_path
  assert capabilities[4]["scope"] == "community:editorial"
  assert capabilities[4]["path"] == editorial_asset_path
  assert capabilities[5]["scope"] == "community:editorial"
  assert capabilities[5]["path"] == editorial_feed_path
  assert seen[0][2]["Idempotency-Key"] == "publish:1234567890abcdef"

  retired = (
    ("POST", "/v1/community/publications"),
    ("POST", "/v1/community/apps/app_12345678/remixes"),
    ("DELETE", "/v1/community/comments/comment_12345678"),
    ("POST", "/v1/community/comments/comment_12345678/reports"),
    (
      "POST",
      "/v1/community/apps/app_12345678/revisions/rev_12345678/reviews",
    ),
  )
  for method, path in retired:
    with pytest.raises(FileNotFoundError):
      broker.proxy(
        method=method, path=path, body=b'{}',
        headers={"idempotency-key": "retired:1234567890abcdef"},
        allow_contributions=True,
      )


def test_large_body_exception_is_only_for_exact_contribution_routes():
  assert broker_module._request_body_limit(
    is_unix=True, method="POST", path="/v1/contributions",
  ) == broker_module.MAX_CONTRIBUTION_BODY
  assert broker_module._request_body_limit(
    is_unix=True, method="POST",
    path="/v1/contributions/ctr_1234567890abcdef1234567890abcdef/withdraw",
  ) == broker_module.MAX_CONTRIBUTION_BODY
  for args in (
    (False, "POST", "/v1/contributions"),
    (True, "POST", "/v1/community/apps"),
    (True, "POST", "/v1/contributions?x=1"),
    (True, "POST", "/v1/responses"),
    (True, "GET", "/v1/community/publications"),
  ):
    assert broker_module._request_body_limit(
      is_unix=args[0], method=args[1], path=args[2],
    ) == broker_module.MAX_BODY


def test_handler_supports_feedback_broker_http_methods():
  handler = broker_module._Handler
  assert handler.do_GET is handler._handle
  assert handler.do_POST is handler._handle
  assert handler.do_PUT is handler._handle
  assert not hasattr(handler, "do_DELETE")


def test_unix_handler_rejects_identity_queries_and_forwards_feedback_mutations():
  socket_dir = Path(tempfile.mkdtemp(prefix="broker-", dir="/tmp"))
  socket_path = socket_dir / "broker.sock"
  seen = []

  class FakeBroker:
    def identity(self):
      return {"linked": True}

    def proxy(self, *, method, path, body, headers, allow_contributions):
      seen.append((method, path, body, allow_contributions))
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
      published = client.post(
        "/v1/community/apps",
        content=b'{"github":{}}',
        headers={"Idempotency-Key": "publish:1234567890abcdef"},
      )
      rating = client.put(
        "/v1/community/apps/app_12345678/rating",
        content=b'{"value":4}',
        headers={"Idempotency-Key": "rating:1234567890abcdef"},
      )
    assert published.json() == {"method": "POST"}
    assert rating.json() == {"method": "PUT"}
    assert seen == [
      ("POST", "/v1/community/apps", b'{"github":{}}', True),
      ("PUT", "/v1/community/apps/app_12345678/rating", b'{"value":4}', True),
    ]
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    shutil.rmtree(socket_dir, ignore_errors=True)
