"""Common federation group chats — host records, membership, signed relays.

Outbound delivery is captured by patching `_deliver`, so no network runs.
Peer actor verification reuses the on-disk actor cache seeding from the
federation tests.
"""

import json
import time
import uuid
from pathlib import Path

import pytest

from app.config import get_settings
from app.routes import common as common_routes
from app.routes import common_groups as groups_routes

from tests.test_common_federation import (
  PEER_HOST,
  _attachment,
  _install_common_app,
  _make_peer_keypair,
  _seed_peer_actor_cache,
)


@pytest.fixture(autouse=True)
def _clean_common_state():
  yield
  import shutil
  common_dir = Path(get_settings().data_dir) / "common"
  if common_dir.exists():
    shutil.rmtree(common_dir)


@pytest.fixture
def sent(monkeypatch):
  """Capture outbound group deliveries instead of hitting the network."""
  captured = []

  async def fake_deliver(host, envelope):
    captured.append((host, envelope))
    return True

  monkeypatch.setattr(groups_routes, "_deliver", fake_deliver)
  return captured


def _join_locally(name="Alex", handle="alex"):
  identity = common_routes._load_identity()
  identity.update(name=name, handle=handle, joined_at=time.time())
  common_routes._save_identity(identity)
  return identity


def _signed_group_envelope(private_b64, host=PEER_HOST, **fields):
  envelope = {"v": 0, "from": host, "sent_at": time.time(), **fields}
  envelope["sig"] = common_routes._sign(envelope, private_b64)
  return envelope


def test_create_group_invites_members(client, db, auth, sent):
  app = _install_common_app(db)
  _join_locally()
  _, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)

  response = client.post(
    "/api/common/groups",
    json={"name": "Weekend plans", "members": [PEER_HOST]},
    headers=auth,
  )
  assert response.status_code == 200, response.text
  gid = response.json()["gid"]
  assert response.json()["invited"] == {PEER_HOST: True}

  host_record = json.loads(groups_routes._host_group_path(gid).read_text())
  assert set(host_record["members"]) == {common_routes._own_host(), PEER_HOST}
  # The invite envelope is a signed group_added carrying the roster.
  host, envelope = sent[0]
  assert host == PEER_HOST
  assert envelope["type"] == "group_added"
  assert envelope["group_name"] == "Weekend plans"

  meta = json.loads(
    (groups_routes._group_dir(app, gid) / "meta.json").read_text()
  )
  assert meta["name"] == "Weekend plans"

  sent.clear()
  response = client.post(
    f"/api/common/groups/{gid}/send",
    json={"text": "host-authored message"}, headers=auth,
  )
  assert response.status_code == 200, response.text
  relay_host, relay = sent[0]
  assert relay_host == PEER_HOST
  original = relay["original"]
  assert original["type"] == "group_post"
  assert original["id"] == relay["id"]
  assert original["from"] == common_routes._own_host()
  assert original["to"] == common_routes._own_host()
  identity = common_routes._load_identity()
  assert common_routes._verify(
    {k: v for k, v in original.items() if k != "sig"},
    original["sig"], identity["public_key_b64"],
  )


def test_host_accepts_member_post_and_relays(client, db, auth, sent):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  created = client.post(
    "/api/common/groups",
    json={"name": "Test group", "members": [PEER_HOST]},
    headers=auth,
  ).json()
  gid = created["gid"]
  sent.clear()

  envelope = _signed_group_envelope(
    private_b64,
    type="group_post", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, text="hello group",
  )
  response = client.post("/api/common/groups/inbox", json=envelope)
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "delivered"

  stored = groups_routes._group_dir(app, gid) / "msgs" / f"{envelope['id']}.json"
  record = json.loads(stored.read_text())
  assert record["author"] == PEER_HOST
  assert record["dir"] == "in"
  # With only host + author as members there is nobody else to relay to.
  assert sent == []

  # Redelivery is idempotent.
  again = client.post("/api/common/groups/inbox", json=envelope)
  assert again.json()["status"] == "duplicate"


def test_group_attachment_and_reply_are_stored_and_relayed(
  client, db, auth, sent
):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  _seed_peer_actor_cache(public_b64, host="other.example.com")
  created = client.post(
    "/api/common/groups",
    json={"name": "Photo group", "members": [PEER_HOST, "other.example.com"]},
    headers=auth,
  ).json()
  gid = created["gid"]
  sent.clear()
  image = b"\x89PNG\r\n\x1a\nrelay"
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "alex",
    "excerpt": "Earlier in the group",
  }
  envelope = _signed_group_envelope(
    private_b64,
    type="group_post", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, text="",
    attachment=_attachment(image), reply_to=reply_to,
  )

  response = client.post("/api/common/groups/inbox", json=envelope)
  assert response.status_code == 200, response.text
  group_dir = groups_routes._group_dir(app, gid)
  record = json.loads(
    (group_dir / "msgs" / f"{envelope['id']}.json").read_text()
  )
  assert record["reply_to"] == reply_to
  assert record["attachment"] == {
    "mime": "image/png", "w": 24, "h": 16,
    "file": f"media/{envelope['id']}.png",
  }
  assert (group_dir / record["attachment"]["file"]).read_bytes() == image
  assert json.loads((group_dir / "meta.json").read_text())["last_text"] == "📷 Photo"

  assert len(sent) == 1
  host, relay = sent[0]
  assert host == "other.example.com"
  assert relay["type"] == "group_message"
  assert relay["attachment"] == envelope["attachment"]
  assert relay["reply_to"] == reply_to
  assert relay["original"] == envelope


def test_host_rejects_non_member_post(client, db, auth, sent):
  _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  created = client.post(
    "/api/common/groups", json={"name": "Private", "members": []}, headers=auth,
  ).json()
  envelope = _signed_group_envelope(
    private_b64,
    type="group_post", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=created["gid"], text="let me in",
  )
  response = client.post("/api/common/groups/inbox", json=envelope)
  assert response.status_code == 403


def test_member_requires_author_signed_original_in_host_relay(
  client, db, auth
):
  app = _install_common_app(db)
  host_private, host_public = _make_peer_keypair()
  author_private, author_public = _make_peer_keypair()
  author_host = "author.example.com"
  _seed_peer_actor_cache(host_public)
  _seed_peer_actor_cache(
    author_public, host=author_host, handle="author-handle"
  )
  gid = str(uuid.uuid4())

  added = _signed_group_envelope(
    host_private,
    type="group_added", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, group_name="Friends",
    members=[{"host": PEER_HOST, "handle": "peer"}],
  )
  response = client.post("/api/common/groups/inbox", json=added)
  assert response.status_code == 200, response.text

  original = _signed_group_envelope(
    author_private, host=author_host,
    type="group_post", id=str(uuid.uuid4()), to=PEER_HOST,
    gid=gid, text="author-signed hello",
  )
  relay = _signed_group_envelope(
    host_private,
    type="group_message", id=original["id"],
    to=common_routes._own_host(), gid=gid, group_name="Friends",
    author="forged-wrapper.example.com", author_handle="forged-wrapper",
    text="forged wrapper text", original=original,
  )
  response = client.post("/api/common/groups/inbox", json=relay)
  assert response.status_code == 200, response.text
  record = json.loads(
    (groups_routes._group_dir(app, gid) / "msgs" / f"{relay['id']}.json")
    .read_text()
  )
  assert record["author"] == author_host
  assert record["author_handle"] == "author-handle"
  assert record["text"] == "author-signed hello"
  assert record["sent_at"] == original["sent_at"]

  absent = _signed_group_envelope(
    host_private,
    type="group_message", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, group_name="Friends",
    author=author_host, author_handle="author-handle", text="missing",
  )
  assert client.post(
    "/api/common/groups/inbox", json=absent
  ).status_code == 403

  forged_original = _signed_group_envelope(
    author_private, host=author_host,
    type="group_post", id=str(uuid.uuid4()), to=PEER_HOST,
    gid=gid, text="signed then altered",
  )
  forged_original["text"] = "host forgery"
  forged = _signed_group_envelope(
    host_private,
    type="group_message", id=forged_original["id"],
    to=common_routes._own_host(), gid=gid, group_name="Friends",
    author=author_host, author_handle="author-handle", text="host forgery",
    original=forged_original,
  )
  assert client.post(
    "/api/common/groups/inbox", json=forged
  ).status_code == 403

  # A relay signed by a different instance than the group host still fails.
  other_private, other_public = _make_peer_keypair()
  _seed_peer_actor_cache(other_public, host="other.example.com")
  wrong_host = {
    "v": 0, "from": "other.example.com", "sent_at": time.time(),
    "type": "group_message", "id": str(uuid.uuid4()),
    "to": common_routes._own_host(), "gid": gid, "group_name": "Friends",
    "author": "other.example.com", "author_handle": "Other", "text": "spoof",
    "original": original,
  }
  wrong_host["sig"] = common_routes._sign(wrong_host, other_private)
  assert client.post(
    "/api/common/groups/inbox", json=wrong_host
  ).status_code == 403


def test_member_send_posts_to_group_host(client, db, auth, sent):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  gid = str(uuid.uuid4())
  added = _signed_group_envelope(
    private_b64,
    type="group_added", id=str(uuid.uuid4()), to=common_routes._own_host(),
    gid=gid, group_name="Friends",
    members=[{"host": PEER_HOST, "handle": "peer"}],
  )
  assert client.post("/api/common/groups/inbox", json=added).status_code == 200

  image = b"\x89PNG\r\n\x1a\nmember"
  reply_to = {
    "id": str(uuid.uuid4()),
    "author_handle": "peer",
    "excerpt": "Earlier",
  }
  response = client.post(
    f"/api/common/groups/{gid}/send",
    json={
      "text": "hi all", "attachment": _attachment(image),
      "reply_to": reply_to,
    },
    headers=auth,
  )
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "delivered"
  host, envelope = sent[0]
  assert host == PEER_HOST
  assert envelope["type"] == "group_post"
  assert envelope["gid"] == gid
  assert envelope["attachment"] == _attachment(image)
  assert envelope["reply_to"] == reply_to
  records = list((groups_routes._group_dir(app, gid) / "msgs").glob("*.json"))
  assert len(records) == 1
  record = json.loads(records[0].read_text())
  assert record["reply_to"] == reply_to
  assert (groups_routes._group_dir(app, gid) / record["attachment"]["file"]).read_bytes() == image
