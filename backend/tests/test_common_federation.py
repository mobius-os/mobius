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
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app import models
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
  cache = common_routes._peer_cache_path(host)
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


def test_actor_card_publishes_identity_and_key(client):
  response = client.get("/api/common/actor")
  assert response.status_code == 200
  actor = response.json()
  assert actor["protocol"] == "common/0"
  assert actor["host"] == common_routes._own_host()
  assert actor["public_key"]["alg"] == "ed25519"
  base64.b64decode(actor["public_key"]["key_b64"])  # decodes to a real key
  assert actor["encryption_key"]["alg"] == "x25519"
  assert len(base64.b64decode(actor["encryption_key"]["key_b64"])) == 32
  assert actor["joined_at"] is None
  assert actor["member_since"] is None
  assert actor["apps"] == []
  # The private keys never leave the identity file, and neither does the
  # owner's display name — only the handle is public.
  assert "private" not in json.dumps(actor)
  assert "name" not in actor
  assert actor["avatar"] is False


def test_existing_identity_is_migrated_with_encryption_keys():
  identity = common_routes._load_identity()
  signing_public = identity["public_key_b64"]
  identity.pop("enc_private_key_b64")
  identity.pop("enc_public_key_b64")
  common_routes._save_identity(identity)

  migrated = common_routes._load_identity()
  assert migrated["public_key_b64"] == signing_public
  assert len(base64.b64decode(migrated["enc_private_key_b64"])) == 32
  assert len(base64.b64decode(migrated["enc_public_key_b64"])) == 32
  persisted = json.loads(common_routes._identity_path().read_text())
  assert persisted["enc_public_key_b64"] == migrated["enc_public_key_b64"]


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


def test_inbox_accepts_signed_message_and_is_idempotent(client, db):
  app = _install_common_app(db)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  envelope = _signed_message(private_b64, text="first federated hello")

  response = client.post("/api/common/inbox", json=envelope)
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "delivered"

  stored = (
    Path(get_settings().data_dir) / "apps" / str(app.id)
    / "conversations" / PEER_HOST / "msgs" / f"{envelope['id']}.json"
  )
  record = json.loads(stored.read_text())
  assert record["dir"] == "in"
  assert record["text"] == "first federated hello"
  assert record["peer_handle"] == "peer"

  # Redelivery of the same envelope id is acknowledged, not duplicated.
  again = client.post("/api/common/inbox", json=envelope)
  assert again.json()["status"] == "duplicate"


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
  real_async_client = httpx.AsyncClient

  def handler(request):
    captured.update(json.loads(request.content))
    return httpx.Response(200, json={"status": "delivered"})

  def client_factory(**kwargs):
    return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

  monkeypatch.setattr(common_routes.httpx, "AsyncClient", client_factory)
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
  real_async_client = httpx.AsyncClient

  def handler(request):
    captured.update(json.loads(request.content))
    return httpx.Response(200, json={"status": "delivered"})

  def client_factory(**kwargs):
    return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

  monkeypatch.setattr(common_routes.httpx, "AsyncClient", client_factory)
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
  assert common_routes._verify(
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
  _install_common_app(db)
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
