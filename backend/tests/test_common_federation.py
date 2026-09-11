"""Common federation (protocol v0) — identity, signed envelopes, inbox, board.

Covers the security-relevant contracts: the actor card publishes a usable key,
the inbox only accepts envelopes whose signature verifies against the claimed
sender's actor card, delivery is idempotent by envelope id, and the community
host's directory/board accept only signed registrations/posts. Peer actor
fetches are faked through the on-disk actor cache so no network is involved.
"""

import base64
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app import common_protocol, models
from app.common_protocol import verify
from app.config import get_settings
from app.routes import common as common_routes


PEER_HOST = "peer.example.com"


def _install_common_app(db) -> models.App:
  app = models.App(
    name="Common", slug="common", source_dir="common",
    description="", jsx_source="",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _make_peer_keypair():
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  from cryptography.hazmat.primitives import serialization
  key = Ed25519PrivateKey.generate()
  private_b64 = base64.b64encode(
    key.private_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PrivateFormat.Raw,
      encryption_algorithm=serialization.NoEncryption(),
    )
  ).decode()
  public_b64 = base64.b64encode(
    key.public_key().public_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PublicFormat.Raw,
    )
  ).decode()
  return private_b64, public_b64



def _make_peer_encryption_keypair():
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
  key = X25519PrivateKey.generate()
  private_b64 = base64.b64encode(
    key.private_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PrivateFormat.Raw,
      encryption_algorithm=serialization.NoEncryption(),
    )
  ).decode()
  public_b64 = base64.b64encode(
    key.public_key().public_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PublicFormat.Raw,
    )
  ).decode()
  return private_b64, public_b64


def _seed_peer_actor_cache(
  public_b64: str, host: str = PEER_HOST, *,
  encryption_public_b64: str | None = None, handle: str = "peer",
):
  """Pre-populate the actor cache so no outbound fetch happens in tests."""
  actor = {
    "protocol": common_routes.PROTOCOL,
    "host": host,
    "handle": handle,
    "bio": "",
    "public_key": {"alg": "ed25519", "key_b64": public_b64},
    "inbox": "/api/common/inbox",
  }
  if encryption_public_b64 is not None:
    actor["encryption_key"] = {
      "alg": "x25519", "key_b64": encryption_public_b64,
    }
  cache = common_routes._actor_verifier.cache_path(host)
  cache.parent.mkdir(parents=True, exist_ok=True)
  cache.write_text(json.dumps({
    "fetched_at": time.time(),
    "actor": actor,
  }))


def _signed_message(private_b64: str, text: str = "hello", **overrides) -> dict:
  envelope = {
    "v": 0,
    "type": "message",
    "id": str(uuid.uuid4()),
    "from": PEER_HOST,
    "to": common_routes._own_host(),
    "text": text,
    "sent_at": time.time(),
  }
  envelope.update(overrides)
  envelope["sig"] = common_routes._sign(
    {k: v for k, v in envelope.items() if k != "sig"}, private_b64
  )
  return envelope


def _sealed_message(
  signing_private_b64: str, recipient_public_b64: str, *, text: str = "hello",
  attachment=None, reply_to=None,
) -> dict:
  from cryptography.hazmat.primitives import hashes, serialization
  from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
  )
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  from cryptography.hazmat.primitives.kdf.hkdf import HKDF
  message_id = str(uuid.uuid4())
  ephemeral = X25519PrivateKey.generate()
  recipient = X25519PublicKey.from_public_bytes(
    base64.b64decode(recipient_public_b64)
  )
  shared = ephemeral.exchange(recipient)
  key = HKDF(
    algorithm=hashes.SHA256(), length=32, salt=b"",
    info=b"common/0 dm v1",
  ).derive(shared)
  nonce = os.urandom(12)
  plaintext = common_routes._canonical({
    "text": text, "attachment": attachment, "reply_to": reply_to,
  })
  ciphertext = ChaCha20Poly1305(key).encrypt(
    nonce, plaintext, message_id.encode("utf-8")
  )
  ephemeral_public = ephemeral.public_key().public_bytes(
    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
  )
  envelope = {
    "v": 0, "type": "message", "id": message_id, "from": PEER_HOST,
    "to": common_routes._own_host(), "text": "", "sent_at": time.time(),
    "enc": {
      "v": 1,
      "epk_b64": base64.b64encode(ephemeral_public).decode(),
      "nonce_b64": base64.b64encode(nonce).decode(),
      "ct_b64": base64.b64encode(ciphertext).decode(),
    },
  }
  envelope["sig"] = common_routes._sign(envelope, signing_private_b64)
  return envelope


def _open_sealed_payload(envelope: dict, recipient_private_b64: str) -> dict:
  from cryptography.hazmat.primitives import hashes
  from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
  )
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  from cryptography.hazmat.primitives.kdf.hkdf import HKDF
  enc = envelope["enc"]
  private = X25519PrivateKey.from_private_bytes(
    base64.b64decode(recipient_private_b64)
  )
  ephemeral = X25519PublicKey.from_public_bytes(base64.b64decode(enc["epk_b64"]))
  key = HKDF(
    algorithm=hashes.SHA256(), length=32, salt=b"",
    info=b"common/0 dm v1",
  ).derive(private.exchange(ephemeral))
  plaintext = ChaCha20Poly1305(key).decrypt(
    base64.b64decode(enc["nonce_b64"]),
    base64.b64decode(enc["ct_b64"]),
    envelope["id"].encode("utf-8"),
  )
  return json.loads(plaintext)


def _attachment(data: bytes = b"small image") -> dict:
  return {
    "mime": "image/png",
    "data_b64": base64.b64encode(data).decode(),
    "w": 24,
    "h": 16,
  }


def test_actor_probe_does_not_initialize_identity_or_publish_private_profile(client):
  # Installing Social is not consent to create or publish a federation identity.
  common_dir = common_routes._identity_path().parent
  shutil.rmtree(common_dir, ignore_errors=True)
  assert not common_dir.exists()
  assert client.get("/api/common/actor").status_code == 404
  assert client.get("/api/common/avatar").status_code == 404
  assert not common_routes._identity_path().exists()
  assert not common_dir.exists()

  # Authenticated owner use initializes private federation. Peers can discover
  # only the keys needed to verify and encrypt protocol traffic until Join.
  identity = common_routes._load_identity()
  identity.update(handle="private-handle", bio="private bio")
  common_routes._save_identity(identity)
  private_actor = client.get("/api/common/actor")
  assert private_actor.status_code == 200
  assert set(private_actor.json()) == {
    "protocol", "host", "public_key", "encryption_key", "inbox",
  }
  assert "private-handle" not in private_actor.text
  assert "private bio" not in private_actor.text
  assert client.get("/api/common/avatar").status_code == 404
  assert not common_routes._public_store.directory_path().exists()

  identity["joined_at"] = time.time()
  common_routes._save_identity(identity)
  response = client.get("/api/common/actor")
  assert response.status_code == 200
  actor = response.json()
  assert actor["protocol"] == "common/0"
  assert actor["host"] == common_routes._own_host()
  assert actor["public_key"]["alg"] == "ed25519"
  base64.b64decode(actor["public_key"]["key_b64"])  # decodes to a real key
  assert actor["encryption_key"]["alg"] == "x25519"
  assert len(base64.b64decode(actor["encryption_key"]["key_b64"])) == 32
  assert actor["joined_at"] == identity["joined_at"]
  assert actor["member_since"] is None
  assert actor["apps"] == []
  # The private keys never leave the identity file, and neither does the
  # owner's display name — only the handle is public.
  assert "private_key" not in json.dumps(actor)
  assert "name" not in actor
  assert actor["avatar"] is False


def test_private_dm_cold_cache_discovers_keys_at_real_actor_endpoints(
  client, db, auth, monkeypatch, tmp_path,
):
  """A private first message and accepted reply work without directory join."""
  app = _install_common_app(db)
  alice = "alice.example.com"
  bob = "bob.example.com"
  base_settings = common_routes.get_settings()
  settings = {
    host: base_settings.model_copy(update={
      "domain": host, "data_dir": str(tmp_path / host),
    })
    for host in (alice, bob)
  }
  active = {"host": alice}
  monkeypatch.setattr(
    common_routes, "get_settings", lambda: settings[active["host"]]
  )

  async def owner_profile(db, principal):
    return {
      "identity": common_routes._load_identity(),
      "profile": None,
      "account_error": None,
    }

  monkeypatch.setattr(common_routes, "_refresh_profile_cache", owner_profile)

  # Bob has explicitly opened Social, but neither owner has joined the public
  # directory. Alice's first deliberate send initializes her own keys.
  active["host"] = bob
  assert client.get("/api/common/me", headers=auth).status_code == 200
  active["host"] = alice
  assert not common_routes._identity_path().exists()

  actor_fetches = []

  async def route_to_peer(method, url, *, json=None, params=None, **_kwargs):
    parsed = httpx.URL(url)
    target = parsed.host
    assert target in settings
    actor_fetches.append((active["host"], target, parsed.path))
    previous = active["host"]
    active["host"] = target
    try:
      transport = httpx.ASGITransport(app=client.app)
      async with httpx.AsyncClient(
        transport=transport, base_url=f"https://{target}",
      ) as peer:
        return await peer.request(
          method, parsed.path, json=json, params=params,
        )
    finally:
      active["host"] = previous

  monkeypatch.setattr(common_routes, "federation_request", route_to_peer)
  monkeypatch.setattr(common_protocol, "federation_request", route_to_peer)
  first = client.post(
    "/api/common/send", json={"to": bob, "text": "private hello"},
    headers=auth,
  )
  assert first.status_code == 200, first.text
  assert first.json()["status"] == "delivered"
  active["host"] = bob
  bob_convo = common_routes._conversation_dir(app, alice)
  bob_record = json.loads(
    (bob_convo / "msgs" / f"{first.json()['id']}.json").read_text()
  )
  assert bob_record["text"] == "private hello"
  assert bob_record["encrypted"] is True
  assert json.loads((bob_convo / "meta.json").read_text())["request_status"] == "pending"

  accepted = client.post(
    f"/api/common/requests/dm/{alice}/accept", headers=auth,
  )
  assert accepted.json() == {"status": "accepted"}

  # Expire both actor caches before the accepted reply. Verification and
  # encryption must continue to use each instance's real /actor endpoint.
  common_routes._actor_verifier.cache_path(alice).unlink(missing_ok=True)
  active["host"] = alice
  common_routes._actor_verifier.cache_path(bob).unlink(missing_ok=True)
  active["host"] = bob
  reply = client.post(
    "/api/common/send", json={"to": alice, "text": "accepted reply"},
    headers=auth,
  )
  assert reply.status_code == 200, reply.text
  assert reply.json()["status"] == "delivered"
  active["host"] = alice
  alice_reply = json.loads(
    (common_routes._conversation_dir(app, bob) / "msgs"
     / f"{reply.json()['id']}.json").read_text()
  )
  assert alice_reply["text"] == "accepted reply"
  assert alice_reply["encrypted"] is True

  assert actor_fetches.count((alice, bob, "/api/common/actor")) == 2
  assert actor_fetches.count((bob, alice, "/api/common/actor")) == 2
  for host in (alice, bob):
    active["host"] = host
    actor = client.get("/api/common/actor").json()
    assert set(actor) == {
      "protocol", "host", "public_key", "encryption_key", "inbox",
    }
    assert not common_routes._public_store.directory_path().exists()


def test_accepting_legacy_request_initializes_private_keys_without_join(
  client, db, auth,
):
  app = _install_common_app(db)
  identity_path = common_routes._identity_path()
  identity_path.unlink(missing_ok=True)
  convo = common_routes._conversation_dir(app, PEER_HOST)
  convo.mkdir(parents=True)
  (convo / "meta.json").write_text(json.dumps({
    "peer": PEER_HOST, "request_status": "pending", "request_count": 1,
  }))

  accepted = client.post(
    f"/api/common/requests/dm/{PEER_HOST}/accept", headers=auth,
  )
  assert accepted.json() == {"status": "accepted"}
  actor = client.get("/api/common/actor")
  assert actor.status_code == 200
  assert set(actor.json()) == {
    "protocol", "host", "public_key", "encryption_key", "inbox",
  }
  assert not common_routes._public_store.directory_path().exists()


def test_existing_private_identity_actor_discovery_migrates_encryption_keys(client):
  identity = common_routes._load_identity()
  signing_public = identity["public_key_b64"]
  identity.pop("enc_private_key_b64")
  identity.pop("enc_public_key_b64")
  common_routes._save_identity(identity)

  # Legacy private conversations retain key discovery after a cold-cache
  # actor fetch, even when their identity predates encrypted DMs.
  actor = client.get("/api/common/actor")
  assert actor.status_code == 200
  migrated = json.loads(common_routes._identity_path().read_text())
  assert migrated["public_key_b64"] == signing_public
  assert len(base64.b64decode(migrated["enc_private_key_b64"])) == 32
  assert len(base64.b64decode(migrated["enc_public_key_b64"])) == 32
  assert actor.json()["encryption_key"]["key_b64"] == migrated["enc_public_key_b64"]


def test_actor_card_publishes_join_date_and_only_public_apps(
  client, db, auth
):
  owner = db.query(models.Owner).first()
  owner.created_at = datetime(2022, 3, 4, 17, 45)
  published = models.App(
    name="Public tool", slug="public-tool", source_dir="public-tool",
    description="d" * 180, jsx_source="",
    published_manifest_url="https://apps.example.com/public/mobius.json",
  )
  local = models.App(
    name="Private notes", slug="private-notes", source_dir="private-notes",
    description="must stay private", jsx_source="",
  )
  deleted = models.App(
    name="Old public tool", slug="old-public", source_dir="old-public",
    description="deleted", jsx_source="", deleted_at=datetime(2025, 1, 1),
    published_manifest_url="https://apps.example.com/old/mobius.json",
  )
  db.add_all([published, local, deleted])
  db.commit()
  identity = common_routes._load_identity()
  identity["joined_at"] = 1_725_000_000.5
  common_routes._save_identity(identity)

  actor = client.get("/api/common/actor").json()
  assert actor["joined_at"] == 1_725_000_000.5
  assert actor["member_since"] == "2022-03-04"
  assert actor["apps"] == [{
    "name": "Public tool", "description": "d" * 140,
  }]
  assert "Private notes" not in json.dumps(actor)


def test_inbox_quietly_stores_initial_request_and_is_idempotent(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = _signed_message(private_b64, text="first federated hello")

  notified = []
  monkeypatch.setattr(
    common_routes.push, "notify_owner", lambda *_args, **_kwargs: notified.append(True)
  )
  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "pending"

  stored = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST / "msgs" / f"{envelope['id']}.json"
  )
  record = json.loads(stored.read_text())
  assert record["dir"] == "in"
  assert record["text"] == "first federated hello"
  assert record["peer_handle"] == "peer"
  meta_path = stored.parent.parent / "meta.json"
  meta = json.loads(meta_path.read_text())
  assert meta["request_status"] == "pending"
  assert meta["request_count"] == 1
  assert meta["unread"] == 0
  assert notified == []

  # Redelivery of the same envelope id is acknowledged, not duplicated.
  again = client.post("/api/common/inbox", json=envelope)
  assert again.json()["status"] == "duplicate"
  assert json.loads(meta_path.read_text())["request_count"] == 1

  accepted = client.post(
    f"/api/common/requests/dm/{PEER_HOST}/accept", headers=auth,
  )
  assert accepted.status_code == 200, accepted.text
  assert accepted.json() == {"status": "accepted"}
  # Acceptance is idempotent and the next delivery is an ordinary unread DM.
  assert client.post(
    f"/api/common/requests/dm/{PEER_HOST}/accept", headers=auth,
  ).json() == {"status": "accepted"}
  followup = _signed_message(private_b64, text="accepted follow-up")
  assert client.post("/api/common/inbox", json=followup).json()["status"] == "delivered"
  accepted_meta = json.loads(meta_path.read_text())
  assert accepted_meta["request_status"] == "accepted"
  assert accepted_meta["unread"] == 1
  assert notified == [True]


def test_inbox_opens_signed_encrypted_message(client, db):
  app = _install_common_app(db)
  signing_private, signing_public = _make_peer_keypair()
  _, peer_encryption_public = _make_peer_encryption_keypair()
  _seed_peer_actor_cache(
    signing_public, encryption_public_b64=peer_encryption_public
  )
  identity = common_routes._load_identity()
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "alex",
    "excerpt": "Earlier",
  }
  envelope = _sealed_message(
    signing_private, identity["enc_public_key_b64"],
    text="secret hello", reply_to=reply_to,
  )

  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 200, response.text
  stored = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST / "msgs" / f"{envelope['id']}.json"
  )
  record = json.loads(stored.read_text())
  assert record["text"] == "secret hello"
  assert record["reply_to"] == reply_to
  assert record["encrypted"] is True


def test_inbox_encrypted_message_rejects_tampering_and_bad_signature(
  client, db
):
  _install_common_app(db)
  signing_private, signing_public = _make_peer_keypair()
  _, peer_encryption_public = _make_peer_encryption_keypair()
  _seed_peer_actor_cache(
    signing_public, encryption_public_b64=peer_encryption_public
  )
  identity = common_routes._load_identity()

  bad_signature = _sealed_message(
    signing_private, identity["enc_public_key_b64"], text="signed secret"
  )
  ciphertext = bytearray(base64.b64decode(bad_signature["enc"]["ct_b64"]))
  ciphertext[0] ^= 1
  bad_signature["enc"]["ct_b64"] = base64.b64encode(ciphertext).decode()
  assert client.post(
    "/api/common/inbox", json=bad_signature
  ).status_code == 403

  tampered = _sealed_message(
    signing_private, identity["enc_public_key_b64"], text="sealed secret"
  )
  ciphertext = bytearray(base64.b64decode(tampered["enc"]["ct_b64"]))
  ciphertext[-1] ^= 1
  tampered["enc"]["ct_b64"] = base64.b64encode(ciphertext).decode()
  tampered["sig"] = common_routes._sign(
    {k: v for k, v in tampered.items() if k != "sig"}, signing_private
  )
  response = client.post("/api/common/inbox", json=tampered)
  assert response.status_code == 400
  assert response.json()["detail"] == "Message could not be decrypted."


def test_inbox_stores_image_attachment_and_photo_preview(client, db):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  image = b"\x89PNG\r\n\x1a\nsmall"
  envelope = _signed_message(
    private_b64, text="", attachment=_attachment(image)
  )

  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 200, response.text
  convo = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST
  )
  record = json.loads(
    (convo / "msgs" / f"{envelope['id']}.json").read_text()
  )
  assert record["attachment"] == {
    "mime": "image/png", "w": 24, "h": 16,
    "file": f"media/{envelope['id']}.png",
  }
  assert "data_b64" not in record["attachment"]
  assert (convo / record["attachment"]["file"]).read_bytes() == image
  assert json.loads((convo / "meta.json").read_text())["last_text"] == "📷 Photo"


def test_inbox_rejects_oversized_image_and_large_plain_envelope(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)

  oversized = _signed_message(
    private_b64,
    text="",
    attachment=_attachment(b"x" * (common_routes.MAX_ATTACHMENT_BYTES + 1)),
  )
  response = client.post("/api/common/inbox", json=oversized)
  assert response.status_code in (400, 413)

  plain = _signed_message(
    private_b64,
    padding="x" * common_routes.MAX_ENVELOPE_BYTES,
  )
  response = client.post("/api/common/inbox", json=plain)
  assert response.status_code == 413


def test_inbox_stores_quoted_reply_verbatim(client, db):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "alex",
    "excerpt": "The earlier message",
  }
  envelope = _signed_message(private_b64, reply_to=reply_to)

  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 200, response.text
  stored = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST / "msgs" / f"{envelope['id']}.json"
  )
  assert json.loads(stored.read_text())["reply_to"] == reply_to


def test_owner_send_inlines_and_stores_attachment_and_reply(
  client, db, auth, monkeypatch
):
  app = _install_common_app(db)
  _, signing_public = _make_peer_keypair()
  _seed_peer_actor_cache(signing_public)
  captured = {}

  async def request(_method, url, *, json, **_kwargs):
    captured.update(json)
    return httpx.Response(
      200, json={"status": "delivered"}, request=httpx.Request("POST", url)
    )

  monkeypatch.setattr(common_routes, "federation_request", request)
  image = b"\x89PNG\r\n\x1a\noutgoing"
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "peer",
    "excerpt": "Previous note",
  }
  response = client.post(
    "/api/common/send",
    json={
      "to": PEER_HOST, "text": "", "attachment": _attachment(image),
      "reply_to": reply_to,
    },
    headers=auth,
  )
  assert response.status_code == 200, response.text
  assert captured["attachment"] == _attachment(image)
  assert captured["reply_to"] == reply_to
  assert "enc" not in captured

  message_id = response.json()["id"]
  convo = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST
  )
  record = json.loads((convo / "msgs" / f"{message_id}.json").read_text())
  assert record["reply_to"] == reply_to
  assert "data_b64" not in record["attachment"]
  assert (convo / record["attachment"]["file"]).read_bytes() == image


def test_owner_send_seals_for_peer_with_encryption_key(
  client, db, auth, monkeypatch
):
  app = _install_common_app(db)
  _, signing_public = _make_peer_keypair()
  encryption_private, encryption_public = _make_peer_encryption_keypair()
  _seed_peer_actor_cache(
    signing_public, encryption_public_b64=encryption_public
  )
  captured = {}

  async def request(_method, url, *, json, **_kwargs):
    captured.update(json)
    return httpx.Response(
      200, json={"status": "delivered"}, request=httpx.Request("POST", url)
    )

  monkeypatch.setattr(common_routes, "federation_request", request)
  image = b"\x89PNG\r\n\x1a\nencrypted outgoing"
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "peer",
    "excerpt": "Previous encrypted note",
  }
  response = client.post(
    "/api/common/send",
    json={
      "to": PEER_HOST, "text": "private hello",
      "attachment": _attachment(image), "reply_to": reply_to,
    },
    headers=auth,
  )
  assert response.status_code == 200, response.text
  assert captured["text"] == ""
  assert "enc" in captured
  assert "attachment" not in captured
  assert "reply_to" not in captured
  identity = common_routes._load_identity()
  assert verify(
    {k: v for k, v in captured.items() if k != "sig"},
    captured["sig"], identity["public_key_b64"],
  )
  assert _open_sealed_payload(captured, encryption_private) == {
    "text": "private hello",
    "attachment": _attachment(image),
    "reply_to": reply_to,
  }

  message_id = response.json()["id"]
  convo = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST
  )
  record = json.loads((convo / "msgs" / f"{message_id}.json").read_text())
  assert record["text"] == "private hello"
  assert record["encrypted"] is True
  assert record["reply_to"] == reply_to
  assert (convo / record["attachment"]["file"]).read_bytes() == image


def test_pending_request_cannot_reply_until_acceptance(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  assert client.post(
    "/api/common/inbox", json=_signed_message(private_b64)
  ).json()["status"] == "pending"

  called = []
  async def request(*_args, **_kwargs):
    called.append(True)
    raise AssertionError("a pending request must not reach the network")
  monkeypatch.setattr(common_routes, "federation_request", request)
  response = client.post(
    "/api/common/send", json={"to": PEER_HOST, "text": "reply"}, headers=auth,
  )
  assert response.status_code == 409
  assert called == []
  assert json.loads(
    (common_routes._conversation_dir(app, PEER_HOST) / "meta.json").read_text()
  )["request_status"] == "pending"


def test_first_owner_message_establishes_consent_without_directory_join(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  _, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)

  async def request(_method, url, **_kwargs):
    return httpx.Response(
      200, json={"status": "pending"}, request=httpx.Request("POST", url)
    )
  monkeypatch.setattr(common_routes, "federation_request", request)
  response = client.post(
    "/api/common/send", json={"to": PEER_HOST, "text": "hello"}, headers=auth,
  )
  assert response.status_code == 200, response.text
  meta = json.loads(
    (common_routes._conversation_dir(app, PEER_HOST) / "meta.json").read_text()
  )
  assert meta["request_status"] == "accepted"
  # Private-message consent must not publish the owner in a public directory.
  assert not common_routes._public_store.directory_path().exists()


def test_legacy_conversation_remains_accepted_on_new_delivery(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  convo = common_routes._conversation_dir(app, PEER_HOST)
  convo.mkdir(parents=True)
  (convo / "meta.json").write_text(json.dumps({
    "peer": PEER_HOST, "last_text": "legacy", "unread": 2,
  }))
  notified = []
  monkeypatch.setattr(
    common_routes.push, "notify_owner", lambda *_args, **_kwargs: notified.append(True)
  )
  response = client.post(
    "/api/common/inbox", json=_signed_message(private_b64, text="still here")
  )
  assert response.json()["status"] == "delivered"
  meta = json.loads((convo / "meta.json").read_text())
  assert meta["request_status"] == "accepted"
  assert meta["unread"] == 3
  assert notified == [True]


@pytest.mark.asyncio
async def test_concurrent_duplicate_request_delivery_counts_once(db):
  import asyncio
  app = _install_common_app(db)
  record = {
    "id": str(uuid.uuid4()), "dir": "in", "peer": PEER_HOST,
    "peer_handle": "peer", "text": "one", "sent_at": time.time(),
    "status": "delivered",
  }
  results = await asyncio.gather(*(
    common_routes._store_message(db, app, PEER_HOST, dict(record))
    for _ in range(2)
  ))
  assert sorted(created for created, _state in results) == [False, True]
  meta = json.loads(
    (common_routes._conversation_dir(app, PEER_HOST) / "meta.json").read_text()
  )
  assert meta["request_status"] == "pending"
  assert meta["request_count"] == 1
  assert meta["unread"] == 0


def test_declined_dm_request_retains_later_messages_quietly(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  notified = []
  monkeypatch.setattr(
    common_routes.push, "notify_owner", lambda *_args, **_kwargs: notified.append(True)
  )
  first = _signed_message(private_b64, text="first")
  assert client.post("/api/common/inbox", json=first).json()["status"] == "pending"
  assert client.post(
    f"/api/common/requests/dm/{PEER_HOST}/decline", headers=auth,
  ).json() == {"status": "declined"}
  later = _signed_message(private_b64, text="retained, still quiet")
  assert client.post("/api/common/inbox", json=later).json()["status"] == "pending"
  convo = common_routes._conversation_dir(app, PEER_HOST)
  assert (convo / "msgs" / f"{later['id']}.json").is_file()
  meta = json.loads((convo / "meta.json").read_text())
  assert meta["request_status"] == "declined"
  assert meta["unread"] == 0
  assert notified == []


def test_blocked_dm_discards_message_and_attachment_without_any_state_change(
  client, db, auth, monkeypatch,
):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  notified = []
  monkeypatch.setattr(
    common_routes.push, "notify_owner", lambda *_args, **_kwargs: notified.append(True)
  )
  first = _signed_message(private_b64, text="retained history")
  assert client.post("/api/common/inbox", json=first).json()["status"] == "pending"
  assert client.post(
    f"/api/common/requests/dm/{PEER_HOST}/block", headers=auth,
  ).json() == {"status": "blocked"}

  convo = common_routes._conversation_dir(app, PEER_HOST)
  version_path = common_routes._app_data_dir(app) / "state" / "version.json"
  before = {
    path.relative_to(common_routes._app_data_dir(app)).as_posix(): path.read_bytes()
    for path in common_routes._app_data_dir(app).rglob("*") if path.is_file()
  }
  blocked = _signed_message(
    private_b64, text="", attachment=_attachment(b"must be discarded")
  )
  response = client.post("/api/common/inbox", json=blocked)
  # Do not reveal the owner's local block decision to the remote sender.
  assert response.json() == {"status": "delivered"}
  after = {
    path.relative_to(common_routes._app_data_dir(app)).as_posix(): path.read_bytes()
    for path in common_routes._app_data_dir(app).rglob("*") if path.is_file()
  }
  assert after == before
  assert (convo / "msgs" / f"{first['id']}.json").is_file()
  assert not (convo / "msgs" / f"{blocked['id']}.json").exists()
  assert not (convo / "media" / f"{blocked['id']}.png").exists()
  assert json.loads((convo / "meta.json").read_text())["request_status"] == "blocked"
  assert version_path.read_bytes() == before["state/version.json"]
  assert notified == []


@pytest.mark.asyncio
@pytest.mark.parametrize("block_first", [True, False])
async def test_block_decision_and_inbound_store_have_one_lock_order(
  db, block_first,
):
  """A raced message is wholly before Block or wholly discarded after it."""
  import asyncio
  app = _install_common_app(db)
  first = {
    "id": str(uuid.uuid4()), "dir": "in", "peer": PEER_HOST,
    "text": "request", "sent_at": time.time(), "status": "delivered",
  }
  assert await common_routes._store_message(db, app, PEER_HOST, first) == (
    True, "pending",
  )
  raced = {
    "id": str(uuid.uuid4()), "dir": "in", "peer": PEER_HOST,
    "text": "raced", "sent_at": time.time(), "status": "delivered",
  }
  lock = common_routes.fs_locks.app_storage_lock(app.id)
  async with lock:
    if block_first:
      decision = asyncio.create_task(
        common_routes._set_dm_request_state(app, PEER_HOST, "blocked")
      )
      delivery = asyncio.create_task(
        common_routes._store_message(db, app, PEER_HOST, raced)
      )
    else:
      delivery = asyncio.create_task(
        common_routes._store_message(db, app, PEER_HOST, raced)
      )
      decision = asyncio.create_task(
        common_routes._set_dm_request_state(app, PEER_HOST, "blocked")
      )
    await asyncio.sleep(0)
    assert not decision.done()
    assert not delivery.done()

  created, state = await delivery
  assert await decision == "blocked"
  raced_path = common_routes._conversation_dir(
    app, PEER_HOST
  ) / "msgs" / f"{raced['id']}.json"
  assert (created, state, raced_path.exists()) == (
    (False, "blocked", False) if block_first
    else (True, "pending", True)
  )

  # Once the decision wins, concurrent later traffic is all discarded.
  later = [
    {
      "id": str(uuid.uuid4()), "dir": "in", "peer": PEER_HOST,
      "text": f"later {index}", "sent_at": time.time(),
      "status": "delivered",
    }
    for index in range(3)
  ]
  before_version = (
    common_routes._app_data_dir(app) / "state" / "version.json"
  ).read_bytes()
  results = await asyncio.gather(*(
    common_routes._store_message(db, app, PEER_HOST, record)
    for record in later
  ))
  assert results == [(False, "blocked")] * len(later)
  assert all(not (
    raced_path.parent / f"{record['id']}.json"
  ).exists() for record in later)
  assert (
    common_routes._app_data_dir(app) / "state" / "version.json"
  ).read_bytes() == before_version


def test_inbox_rejects_bad_signature(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = _signed_message(private_b64)
  envelope["text"] = "tampered after signing"
  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 403


def test_inbox_rejects_wrong_signer_key(client, db):
  _install_common_app(db)
  attacker_private, _ = _make_peer_keypair()
  _, real_public = _make_peer_keypair()
  _seed_peer_actor_cache(real_public)  # actor card advertises the REAL key
  envelope = _signed_message(attacker_private)
  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 403


def test_inbox_rejects_stale_timestamp(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = _signed_message(private_b64, sent_at=time.time() - 7200)
  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 400


def test_directory_registration_and_search(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = {
    "v": 0,
    "type": "register",
    "from": PEER_HOST,
    "handle": "peer",
    "bio": "Building things",
    "sent_at": time.time(),
  }
  envelope["sig"] = common_routes._sign(envelope, private_b64)
  response = client.post("/api/common/directory", json=envelope)
  assert response.status_code == 200, response.text

  found = client.get("/api/common/directory", params={"q": "peer"}).json()
  assert [u["host"] for u in found["users"]] == [PEER_HOST]
  missing = client.get("/api/common/directory", params={"q": "nobody"}).json()
  assert missing["users"] == []


def test_board_accepts_signed_post_and_serves_feed(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = {
    "v": 0,
    "type": "board_post",
    "id": str(uuid.uuid4()),
    "from": PEER_HOST,
    "text": "Hello from a federated peer",
    "sent_at": time.time(),
  }
  envelope["sig"] = common_routes._sign(envelope, private_b64)
  response = client.post("/api/common/board", json=envelope)
  assert response.status_code == 200, response.text

  board = client.get("/api/common/board").json()
  assert board["posts"][0]["text"] == "Hello from a federated peer"
  assert board["posts"][0]["handle"] == "peer"


def test_board_attachment_is_stored_served_and_exposed_as_metadata(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  image = b"\x89PNG\r\n\x1a\nboard"
  envelope = {
    "v": 0,
    "type": "board_post",
    "id": str(uuid.uuid4()),
    "from": PEER_HOST,
    "text": "",
    "attachment": _attachment(image),
    "sent_at": time.time(),
  }
  envelope["sig"] = common_routes._sign(envelope, private_b64)

  response = client.post("/api/common/board", json=envelope)
  assert response.status_code == 200, response.text
  media = client.get(f"/api/common/board/media/{envelope['id']}")
  assert media.status_code == 200
  assert media.headers["content-type"] == "image/png"
  assert media.content == image

  post = client.get("/api/common/board").json()["posts"][0]
  assert post["attachment"] == {"mime": "image/png", "w": 24, "h": 16}
  assert "data_b64" not in post["attachment"]


def test_owner_publish_stores_local_board_attachment(client, db, auth):
  _install_common_app(db)
  image = b"\x89PNG\r\n\x1a\nlocal"
  response = client.post(
    "/api/common/publish",
    json={"text": "", "attachment": _attachment(image)},
    headers=auth,
  )
  assert response.status_code == 200, response.text
  post_id = response.json()["id"]

  public_media = client.get(f"/api/common/board/media/{post_id}")
  assert public_media.content == image
  owner_media = client.get(
    f"/api/common/board-media/{post_id}", headers=auth
  )
  assert owner_media.status_code == 200
  assert owner_media.content == image


def test_owner_board_media_caches_remote_community_image(
  client, db, auth, monkeypatch
):
  _install_common_app(db)
  identity = common_routes._load_identity()
  identity["community_host"] = PEER_HOST
  common_routes._save_identity(identity)
  image = b"\x89PNG\r\n\x1a\nremote"
  fetched = []

  async def fake_download(url):
    fetched.append(url)
    return "image/png", image

  monkeypatch.setattr(common_routes, "_download_board_media", fake_download)
  post_id = str(uuid.uuid4())
  first = client.get(f"/api/common/board-media/{post_id}", headers=auth)
  second = client.get(f"/api/common/board-media/{post_id}", headers=auth)

  assert first.status_code == 200
  assert first.content == image
  assert second.content == image
  assert fetched == [
    f"https://{PEER_HOST}/api/common/board/media/{post_id}"
  ]


def test_board_rejects_unsigned_post(client, db):
  _install_common_app(db)
  response = client.post("/api/common/board", json={
    "v": 0, "type": "board_post", "id": str(uuid.uuid4()),
    "from": PEER_HOST, "text": "spam", "sent_at": time.time(),
  })
  assert response.status_code in (400, 403, 502)


def test_board_replies_are_signed_idempotent_and_counted(client, db):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  post = {
    "v": 0, "type": "board_post", "id": str(uuid.uuid4()),
    "from": PEER_HOST, "text": "reply here", "sent_at": time.time(),
  }
  post["sig"] = common_routes._sign(post, private_b64)
  assert client.post("/api/common/board", json=post).status_code == 200

  reply = {
    "v": 0, "type": "board_reply", "post_id": post["id"],
    "id": str(uuid.uuid4()), "text": "first reply",
    "from": PEER_HOST, "sent_at": time.time(),
  }
  reply["sig"] = common_routes._sign(reply, private_b64)
  accepted = client.post("/api/common/board/reply", json=reply)
  assert accepted.status_code == 200, accepted.text
  assert accepted.json() == {"status": "ok", "reply_count": 1}
  duplicate = client.post("/api/common/board/reply", json=reply)
  assert duplicate.json() == {"status": "ok", "reply_count": 1}

  replies = client.get(f"/api/common/board/{post['id']}/replies").json()
  assert replies["replies"] == [{
    "id": reply["id"],
    "host": PEER_HOST,
    "handle": "peer",
    "text": "first reply",
    "created_at": reply["sent_at"],
  }]
  feed = client.get("/api/common/board").json()
  entry = next(item for item in feed["posts"] if item["id"] == post["id"])
  assert entry["reply_count"] == 1
  assert "replies" not in entry

  unsigned = {k: v for k, v in reply.items() if k != "sig"}
  unsigned["id"] = str(uuid.uuid4())
  rejected = client.post("/api/common/board/reply", json=unsigned)
  assert rejected.status_code in (400, 403)


def test_join_uses_mobius_you_identity(client, db, auth, monkeypatch):
  _install_common_app(db)
  from app.routes import identity as identity_routes

  async def fake_profile(_db, _owner):
    return {"display_name": "Alex Doe", "handle": "alex"}

  monkeypatch.setattr(identity_routes, "resolve_owner_profile", fake_profile)
  me = client.get("/api/common/me", headers=auth).json()
  assert me["connected"] is True
  assert me["joined"] is False
  assert me["name"] == "Alex Doe"
  assert me["handle"] == "alex"

  joined = client.post("/api/common/join", headers=auth)
  assert joined.status_code == 200, joined.text
  assert joined.json()["directory"] == "registered"
  me = client.get("/api/common/me", headers=auth).json()
  assert me["joined"] is True
  found = client.get("/api/common/directory", params={"q": "alex"}).json()
  assert found["users"][0]["host"] == common_routes._own_host()
  assert found["users"][0]["handle"] == "alex"


def test_join_requires_connected_profile(client, db, auth, monkeypatch):
  _install_common_app(db)
  from app.routes import identity as identity_routes

  async def no_profile(_db, _owner):
    return None

  monkeypatch.setattr(identity_routes, "resolve_owner_profile", no_profile)
  me = client.get("/api/common/me", headers=auth).json()
  assert me["connected"] is False
  response = client.post("/api/common/join", headers=auth)
  assert response.status_code == 409


def test_owner_surface_requires_auth(client, db):
  _install_common_app(db)
  assert client.get("/api/common/me").status_code == 401
  assert client.post(
    "/api/common/send", json={"to": PEER_HOST, "text": "hi"}
  ).status_code == 401


def test_other_apps_cannot_use_owner_surface(client, db, auth):
  app = _install_common_app(db)
  other = models.App(
    name="Other", slug="other-app", source_dir="other-app",
    description="", jsx_source="",
  )
  db.add(other)
  db.commit()
  db.refresh(other)
  from app import auth as app_auth
  token = app_auth.create_access_token({"sub": "test", "scope": "app", "app_id": other.id})
  headers = {"Authorization": f"Bearer {token}"}
  response = client.get("/api/common/me", headers=headers)
  assert response.status_code == 403
  request_meta = common_routes._conversation_dir(app, PEER_HOST)
  request_meta.mkdir(parents=True)
  (request_meta / "meta.json").write_text(json.dumps({
    "peer": PEER_HOST, "request_status": "pending",
  }))
  request_url = f"/api/common/requests/dm/{PEER_HOST}/accept"
  assert client.post(request_url).status_code == 401
  assert client.post(request_url, headers=headers).status_code == 403
  from app import auth as app_auth
  common_token = app_auth.create_access_token({
    "sub": "test", "scope": "app", "app_id": app.id,
  })
  accepted = client.post(
    request_url, headers={"Authorization": f"Bearer {common_token}"},
  )
  assert accepted.json() == {"status": "accepted"}


@pytest.fixture(autouse=True)
def _clean_common_state():
  """Each test starts with a fresh /common tree (identity, peers, board)."""
  yield
  import shutil
  common_dir = Path(get_settings().data_dir) / "common"
  if common_dir.exists():
    shutil.rmtree(common_dir)


def test_board_likes_toggle_and_feed_counts(client, db, auth):
  _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  post = {
    "v": 0, "type": "board_post", "id": str(uuid.uuid4()),
    "from": PEER_HOST, "text": "likeable", "sent_at": time.time(),
  }
  post["sig"] = common_routes._sign(post, private_b64)
  assert client.post("/api/common/board", json=post).status_code == 200

  # A signed peer like toggles on, then off.
  react = {
    "v": 0, "type": "board_react", "post_id": post["id"],
    "from": PEER_HOST, "sent_at": time.time(),
  }
  react["sig"] = common_routes._sign(react, private_b64)
  first = client.post("/api/common/board/react", json=react).json()
  assert first["likes"] == 1
  react2 = {**react, "sent_at": time.time()}
  react2.pop("sig")
  react2["sig"] = common_routes._sign(react2, private_b64)
  second = client.post("/api/common/board/react", json=react2).json()
  assert second["likes"] == 0

  # An unsigned like is rejected; like membership never leaks in the feed.
  bad = {**react, "sig": "AAAA"}
  assert client.post("/api/common/board/react", json=bad).status_code == 403
  react3 = {k: v for k, v in react.items() if k != "sig"}
  react3["sent_at"] = time.time()
  react3["sig"] = common_routes._sign(react3, private_b64)
  client.post("/api/common/board/react", json=react3)
  board = client.get("/api/common/board", params={"viewer": PEER_HOST}).json()
  entry = next(p for p in board["posts"] if p["id"] == post["id"])
  assert entry["like_count"] == 1
  assert entry["liked"] is True
  assert "likes" not in entry

@pytest.mark.parametrize('legacy_member', [False, True])
def test_public_browse_host_does_not_change_membership_and_proxies_replies(
  client, db, auth, monkeypatch, legacy_member,
):
  _install_common_app(db)
  identity_path = common_routes._identity_path()
  if legacy_member:
    identity = common_routes._load_identity()
    identity.update(community_host='legacy.example.com', joined_at=123)
    common_routes._save_identity(identity)
    before = identity_path.read_bytes()
  else:
    identity_path.unlink(missing_ok=True)
    before = None
  calls = []
  async def remote(method, url, **kwargs):
    calls.append((method, url))
    result = {'replies': [{'id': 'remote-reply', 'text': 'remote'}]}
    if url.endswith('/board'): result = {'posts': []}
    if url.endswith('/directory'): result = {'users': []}
    return httpx.Response(200, json=result, request=httpx.Request(method, url))
  monkeypatch.setattr(common_routes, 'federation_request', remote)
  async def media(url):
    calls.append(('GET', url))
    return 'image/png', b'fixture'
  monkeypatch.setattr(common_routes, '_download_board_media', media)
  post_id = str(uuid.uuid4())
  for path in ['feed', 'people', f'replies/{post_id}', f'board-media/{post_id}']:
    response = client.get('/api/common/' + path,
      params={'community_host': 'global.example.com'}, headers=auth)
    assert response.status_code == 200, response.text
  assert all(method == 'GET' and url.startswith('https://global.example.com/')
    for method, url in calls)
  assert ('GET', f'https://global.example.com/api/common/board/{post_id}/replies') in calls
  assert (identity_path.read_bytes() if identity_path.exists() else None) == before
  assert client.get('/api/common/replies/' + post_id,
    params={'community_host': 'https://bad.example/path'}, headers=auth).status_code == 400
  assert client.get('/api/common/replies/' + post_id).status_code in (401, 403)


def test_local_people_uses_shared_directory_store(client, auth, db):
  _install_common_app(db)
  response = client.get("/api/common/people", params={
    "community_host": common_routes._own_host(), "q": "nobody-matches-this-query",
  }, headers=auth)
  assert response.status_code == 200, response.text
  assert response.json() == {"host": common_routes._own_host(), "users": []}


def test_board_writes_and_canonical_host_route_to_community_host(client, auth, db, monkeypatch):
  _install_common_app(db)
  calls = []
  async def remote(method, url, **kwargs):
    calls.append((method, url))
    result = {'status': 'posted'}
    return httpx.Response(200, json=result, request=httpx.Request(method, url))
  monkeypatch.setattr(common_routes, 'federation_request', remote)

  # publish with community_host param
  pub = client.post('/api/common/publish?community_host=global.example.com', json={'text': 'remote post'}, headers=auth)
  assert pub.status_code == 200, pub.text
  assert ('POST', 'https://global.example.com/api/common/board') in calls

  # like with community_host param
  post_id = str(uuid.uuid4())
  like = client.post('/api/common/like?community_host=global.example.com', json={'post_id': post_id}, headers=auth)
  assert like.status_code == 200, like.text
  assert ('POST', 'https://global.example.com/api/common/board/react') in calls

  # reply with community_host param
  reply = client.post('/api/common/reply?community_host=global.example.com', json={'post_id': post_id, 'text': 'reply'}, headers=auth)
  assert reply.status_code == 200, reply.text
  assert ('POST', 'https://global.example.com/api/common/board/reply') in calls

  # canonical host logic: for external deployment, always resolves to the single global community host
  monkeypatch.setattr(common_routes, '_own_host', lambda: 'peer-instance.railway.app')
  assert common_routes._canonical_community_host() == common_routes.COMMUNITY_HOST
  assert common_routes._canonical_community_host('peer-instance.railway.app') == common_routes.COMMUNITY_HOST
