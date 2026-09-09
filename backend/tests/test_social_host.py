"""Shared Common public contract and isolated-host security boundaries."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import common_transport
from app.common_protocol import (
  MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_ENVELOPE_BYTES, canonical, sign,
)
from app.common_public import CommonPublicStore
from app.config import get_settings
from app.routes import common as personal_common
from app.social_host import SOURCE_SHA, create_app

PEER_HOST = "peer.example.com"


def test_common_zero_canonical_signature_bytes_are_frozen():
  assert canonical({"text": "café", "v": 0}) == (
    b'{"text":"caf\\u00e9","v":0}'
  )


def _keypair() -> tuple[str, str]:
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  key = Ed25519PrivateKey.generate()
  private = key.private_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
  )
  public = key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
  )
  return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def _seed_actor(verifier, public_key: str, host: str = PEER_HOST) -> None:
  path = verifier.cache_path(host)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps({
    "fetched_at": time.time(),
    "actor": {
      "protocol": "common/0",
      "host": host,
      "handle": host.split(".")[0],
      "bio": "",
      "public_key": {"alg": "ed25519", "key_b64": public_key},
    },
  }), encoding="utf-8")


def _signed(private_key: str, envelope: dict) -> dict:
  value = dict(envelope)
  value["sig"] = sign(value, private_key)
  return value


@dataclass
class PublicRuntime:
  name: str
  client: TestClient
  verifier: object
  store: CommonPublicStore
  limiter: object


@pytest.fixture(params=("personal", "sidecar"))
def public_runtime(request, client, tmp_path):
  """Run the same protocol assertions against both ASGI applications."""
  if request.param == "personal":
    personal_common._public_write_limiter.reset()
    common_dir = Path(get_settings().data_dir) / "common"
    shutil.rmtree(common_dir, ignore_errors=True)
    yield PublicRuntime(
      "personal", client, personal_common._actor_verifier,
      personal_common._public_store, personal_common._public_write_limiter,
    )
    shutil.rmtree(common_dir, ignore_errors=True)
    personal_common._public_write_limiter.reset()
    return

  application = create_app(tmp_path)
  with TestClient(application) as sidecar:
    yield PublicRuntime(
      "sidecar", sidecar, application.state.actor_verifier,
      application.state.social_store, application.state.limiter,
    )
  application.state.limiter.reset()


def _post(runtime: PublicRuntime, private_key: str, *, attachment=None) -> dict:
  envelope = {
    "v": 0,
    "type": "board_post",
    "id": str(uuid.uuid4()),
    "from": PEER_HOST,
    "text": "hello" if attachment is None else "",
    "sent_at": time.time(),
  }
  if attachment is not None:
    envelope["attachment"] = attachment
  envelope = _signed(private_key, envelope)
  response = runtime.client.post("/api/common/board", json=envelope)
  assert response.status_code == 200, response.text
  return envelope


def test_same_directory_board_reply_reaction_contract(public_runtime):
  private_key, public_key = _keypair()
  _seed_actor(public_runtime.verifier, public_key)

  registration = _signed(private_key, {
    "v": 0,
    "type": "register",
    "from": PEER_HOST,
    "handle": "peer",
    "bio": "building",
    "sent_at": time.time(),
  })
  assert public_runtime.client.post(
    "/api/common/directory", json=registration
  ).json() == {"status": "registered"}
  assert public_runtime.client.get(
    "/api/common/directory", params={"q": "peer"}
  ).json()["users"] == [{
    "host": PEER_HOST, "handle": "peer", "bio": "building",
  }]

  post = _post(public_runtime, private_key)
  # Stable ids are idempotent and the duplicate does not create another file.
  assert public_runtime.client.post(
    "/api/common/board", json=post
  ).status_code == 200

  reply = _signed(private_key, {
    "v": 0,
    "type": "board_reply",
    "post_id": post["id"],
    "id": str(uuid.uuid4()),
    "text": "one reply",
    "from": PEER_HOST,
    "sent_at": time.time(),
  })
  first_reply = public_runtime.client.post(
    "/api/common/board/reply", json=reply
  )
  duplicate_reply = public_runtime.client.post(
    "/api/common/board/reply", json=reply
  )
  assert first_reply.json() == duplicate_reply.json() == {
    "status": "ok", "reply_count": 1,
  }
  assert public_runtime.client.get(
    f"/api/common/board/{post['id']}/replies"
  ).json()["replies"][0]["id"] == reply["id"]

  reaction = _signed(private_key, {
    "v": 0,
    "type": "board_react",
    "post_id": post["id"],
    "from": PEER_HOST,
    "sent_at": time.time(),
  })
  first_reaction = public_runtime.client.post(
    "/api/common/board/react", json=reaction
  ).json()
  retried_reaction = public_runtime.client.post(
    "/api/common/board/react", json=reaction
  ).json()
  assert first_reaction == retried_reaction == {
    "status": "ok", "likes": 1, "liked": True,
  }

  feed = public_runtime.client.get(
    "/api/common/board", params={"viewer": PEER_HOST}
  ).json()
  assert len(feed["posts"]) == 1
  projected = feed["posts"][0]
  assert projected["id"] == post["id"]
  assert projected["like_count"] == 1
  assert projected["reply_count"] == 1
  assert "likes" not in projected
  assert "replies" not in projected
  assert "_reaction_replays" not in projected

  later_reaction = {**reaction, "sent_at": time.time()}
  later_reaction.pop("sig")
  later_reaction = _signed(private_key, later_reaction)
  assert public_runtime.client.post(
    "/api/common/board/react", json=later_reaction
  ).json() == {"status": "ok", "likes": 0, "liked": False}


def test_future_dated_reaction_retry_is_idempotent_for_full_valid_window(
  public_runtime, monkeypatch,
):
  from app.common_protocol import CLOCK_SKEW_S
  now = time.time()
  clock = [now]
  monkeypatch.setattr(time, "time", lambda: clock[0])
  private_key, public_key = _keypair()
  _seed_actor(public_runtime.verifier, public_key)
  post_id = str(uuid.uuid4())
  public_runtime.store.store_post({
    "id": post_id, "created_at": now, "text": "fixture", "replies": [],
  })
  reaction = _signed(private_key, {
    "v": 0, "type": "board_react", "post_id": post_id,
    "from": PEER_HOST, "sent_at": now + CLOCK_SKEW_S - 1,
  })
  for elapsed in (0, CLOCK_SKEW_S + 1, 2 * CLOCK_SKEW_S - 1):
    clock[0] = now + elapsed
    response = public_runtime.client.post("/api/common/board/react", json=reaction)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "likes": 1, "liked": True}
  clock[0] = now + 2 * CLOCK_SKEW_S
  assert public_runtime.client.post(
    "/api/common/board/react", json=reaction,
  ).status_code == 400


def test_same_bad_signature_and_old_timestamp_contract(public_runtime):
  private_key, public_key = _keypair()
  _seed_actor(public_runtime.verifier, public_key)
  bad = _signed(private_key, {
    "v": 0, "type": "register", "from": PEER_HOST,
    "handle": "peer", "bio": "", "sent_at": time.time(),
  })
  bad["handle"] = "tampered"
  assert public_runtime.client.post(
    "/api/common/directory", json=bad
  ).status_code == 403

  stale = _signed(private_key, {
    "v": 0, "type": "register", "from": PEER_HOST,
    "handle": "peer", "bio": "", "sent_at": time.time() - 601,
  })
  assert public_runtime.client.post(
    "/api/common/directory", json=stale
  ).status_code == 400

  non_finite = _signed(private_key, {
    "v": 0, "type": "register", "from": PEER_HOST,
    "handle": "peer", "bio": "", "sent_at": float("nan"),
  })
  assert public_runtime.client.post(
    "/api/common/directory",
    content=json.dumps(non_finite),
    headers={"content-type": "application/json"},
  ).status_code == 400


def test_same_media_size_boundary_contract(public_runtime):
  private_key, public_key = _keypair()
  _seed_actor(public_runtime.verifier, public_key)
  at_limit = b"x" * MAX_ATTACHMENT_BYTES
  post = _post(public_runtime, private_key, attachment={
    "mime": "image/png",
    "data_b64": base64.b64encode(at_limit).decode(),
    "w": 1,
    "h": 1,
  })
  media = public_runtime.client.get(
    f"/api/common/board/media/{post['id']}"
  )
  assert media.status_code == 200
  assert media.content == at_limit
  assert media.headers["x-content-type-options"] == "nosniff"

  over_limit = b"x" * (MAX_ATTACHMENT_BYTES + 1)
  rejected = _signed(private_key, {
    "v": 0, "type": "board_post", "id": str(uuid.uuid4()),
    "from": PEER_HOST, "text": "", "sent_at": time.time(),
    "attachment": {
      "mime": "image/png",
      "data_b64": base64.b64encode(over_limit).decode(),
      "w": 1,
      "h": 1,
    },
  })
  assert public_runtime.client.post(
    "/api/common/board", json=rejected
  ).status_code == 413
  assert public_runtime.client.post(
    "/api/common/board",
    content=b"x" * (MAX_ATTACHMENT_ENVELOPE_BYTES + 1),
    headers={"content-type": "application/json"},
  ).status_code == 413


def test_store_locks_prevent_concurrent_directory_and_reply_loss(tmp_path):
  store = CommonPublicStore(tmp_path)
  second_runtime_store = CommonPublicStore(tmp_path)
  post_id = str(uuid.uuid4())
  store.store_post({
    "id": post_id, "host": PEER_HOST, "handle": "peer",
    "text": "concurrent", "created_at": time.time(), "replies": [],
  })

  def mutate(index: int) -> None:
    active_store = store if index % 2 else second_runtime_store
    host = f"peer-{index}.example"
    active_store.register(host, f"peer-{index}", "")
    active_store.add_reply(
      post_id, str(uuid.uuid5(uuid.NAMESPACE_DNS, host)), host,
      f"peer-{index}", f"reply {index}", time.time(),
    )

  with ThreadPoolExecutor(max_workers=16) as pool:
    list(pool.map(mutate, range(64)))

  assert len(json.loads(store.directory_path().read_text())) == 64
  assert len(store.get_replies(post_id)["replies"]) == 64


def test_fixture_import_is_read_without_conversion_or_pruning(tmp_path):
  common = tmp_path / "common"
  (common / "board").mkdir(parents=True)
  (common / "board-media").mkdir()
  post_id = "deadbeef-0000-0000-0000-000000000001"
  (common / "directory.json").write_text(json.dumps({
    "legacy.example": {
      "handle": "legacy", "bio": "kept", "registered_at": 123.0,
    },
  }))
  (common / "board" / f"{post_id}.json").write_text(json.dumps({
    "id": post_id, "host": "legacy.example", "handle": "legacy",
    "text": "preserved", "created_at": 123.0,
    "likes": {"viewer.example": 124.0},
    "replies": [{
      "id": "deadbeef-0000-0000-0000-000000000002",
      "host": "viewer.example", "handle": "viewer", "text": "still here",
      "created_at": 125.0,
    }],
  }))
  image = b"legacy-png-bytes"
  (common / "board-media" / f"{post_id}.png").write_bytes(image)

  with TestClient(create_app(tmp_path)) as client:
    assert client.get("/api/common/directory").json()["users"][0]["host"] == "legacy.example"
    projected = client.get(
      "/api/common/board", params={"viewer": "viewer.example"}
    ).json()["posts"][0]
    assert projected["id"] == post_id
    assert projected["like_count"] == 1
    assert projected["reply_count"] == 1
    assert client.get(f"/api/common/board/media/{post_id}").content == image

  assert (common / "directory.json").is_file()
  assert (common / "board" / f"{post_id}.json").is_file()
  assert (common / "board-media" / f"{post_id}.png").is_file()


def test_sidecar_surface_health_and_immutable_version(tmp_path, monkeypatch):
  monkeypatch.setenv("BUILD_SHA", "f" * 40)
  monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "e" * 40)
  application = create_app(tmp_path)
  with TestClient(application) as client:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/version").json() == {
      "service": "mobius-social", "source_sha": SOURCE_SHA,
    }
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

  def concrete_routes(router):
    for route in router.routes:
      included = getattr(route, "original_router", None)
      if included is not None:
        yield from concrete_routes(included)
      else:
        yield route

  actual = {
    (method, route.path)
    for route in concrete_routes(application.router)
    for method in getattr(route, "methods", set())
  }
  assert actual == {
    ("GET", "/healthz"),
    ("GET", "/version"),
    ("GET", "/api/common/directory"),
    ("POST", "/api/common/directory"),
    ("GET", "/api/common/board"),
    ("POST", "/api/common/board"),
    ("GET", "/api/common/board/media/{post_id}"),
    ("GET", "/api/common/board/{post_id}/replies"),
    ("POST", "/api/common/board/react"),
    ("POST", "/api/common/board/reply"),
  }


def test_sidecar_starts_without_owner_database_config_or_keys(tmp_path):
  env = {
    "PATH": os.environ["PATH"],
    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    "SOCIAL_DATA_DIR": str(tmp_path),
  }
  script = (
    "from fastapi.testclient import TestClient; "
    "from app.social_host import app; "
    "c=TestClient(app); "
    "c.__enter__(); "
    "assert c.get('/healthz').json()=={'status':'ok'}; "
    "c.__exit__(None,None,None)"
  )
  result = subprocess.run(
    [sys.executable, "-c", script], env=env, text=True,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
  )
  assert result.returncode == 0, result.stderr


def test_sidecar_public_writes_have_one_shared_ingress_limit(tmp_path):
  application = create_app(tmp_path)
  application.state.limiter.reset()
  with TestClient(application) as client:
    for index in range(120):
      path = (
        "/api/common/directory" if index % 2
        else "/api/common/board/react"
      )
      assert client.post(path, json={}).status_code == 400
    assert client.post("/api/common/board", json={}).status_code == 429
  application.state.limiter.reset()


@pytest.mark.asyncio
async def test_sidecar_actor_lookup_uses_shared_ssrf_rejection(
  tmp_path, monkeypatch,
):
  application = create_app(tmp_path)
  calls = []

  def getaddrinfo(_host, _port, *_args, **_kwargs):
    return [
      (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0))
    ]

  class MustNotConnect:
    def __init__(self, **_kwargs):
      calls.append(True)

  monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
  monkeypatch.setattr(common_transport.httpx, "AsyncClient", MustNotConnect)
  with pytest.raises(HTTPException) as exc:
    await application.state.actor_verifier.fetch_actor("loopback.example", force=True)
  assert getattr(exc.value, "status_code", None) == 502
  assert calls == []


@pytest.mark.asyncio
async def test_sidecar_actor_lookup_accepts_mocked_bounded_key(
  tmp_path, monkeypatch,
):
  application = create_app(tmp_path)
  _, public_key = _keypair()
  actor = {
    "protocol": "common/0", "host": "peer.example", "handle": "peer",
    "public_key": {"alg": "ed25519", "key_b64": public_key},
  }

  async def fake_request(*_args, **_kwargs):
    return httpx.Response(
      200, json=actor,
      request=httpx.Request("GET", "https://peer.example/api/common/actor"),
    )

  from app import common_protocol
  monkeypatch.setattr(common_protocol, "federation_request", fake_request)
  assert await application.state.actor_verifier.fetch_actor(
    "peer.example", force=True
  ) == actor

  actor["public_key"]["key_b64"] = base64.b64encode(b"x" * 33).decode()
  with pytest.raises(HTTPException) as exc:
    await application.state.actor_verifier.fetch_actor(
      "peer.example", force=True
    )
  assert getattr(exc.value, "status_code", None) == 502
