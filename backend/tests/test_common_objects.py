"""Common federated shared objects — create, invite, join, CAS write, revoke.

Covers the collaboration contracts: hosted objects are created with version 1,
invites admit a signed peer exactly by role, non-members and revoked members
are rejected, writes are compare-and-swap (a stale expected_version returns a
conflict payload instead of clobbering), viewers cannot write, and app tokens
are confined to their own app's objects. Peer actor fetches are faked through
the on-disk actor cache so no network is involved.
"""

import base64
import json
import time

import pytest
from fastapi import HTTPException

from app.routes import common as common_routes
from app.routes import common_objects as objects_routes
from app.routes import identity as identity_routes


PEER_HOST = "peer.example.com"


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


def _seed_peer_actor_cache(public_b64: str, host: str = PEER_HOST):
  cache = common_routes._peer_cache_path(host)
  cache.parent.mkdir(parents=True, exist_ok=True)
  cache.write_text(json.dumps({
    "fetched_at": time.time(),
    "actor": {
      "protocol": common_routes.PROTOCOL,
      "host": host,
      "name": "Peer Person",
      "bio": "",
      "public_key": {"alg": "ed25519", "key_b64": public_b64},
      "inbox": "/api/common/inbox",
    },
  }))


def _signed(private_b64: str, envelope: dict, host: str = PEER_HOST) -> dict:
  envelope = {
    "v": 0,
    "from": host,
    "to": common_routes._own_host(),
    "sent_at": time.time(),
    **envelope,
  }
  envelope["sig"] = common_routes._sign(
    {k: v for k, v in envelope.items() if k != "sig"}, private_b64
  )
  return envelope


def _create_board(client, auth, doc=None):
  response = client.post(
    "/api/common/objects",
    json={"app": "kanban", "kind": "board", "label": "Test board",
          "doc": doc or {"v": 1, "title": "Board", "cards": {}}},
    headers=auth,
  )
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _invite(client, auth, oid, role="editor"):
  response = client.post(f"/api/common/objects/{oid}/invites", json={"role": role}, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()["invite"].rsplit("#", 1)[1]


def test_create_and_local_state_roundtrip(client, auth):
  oid = _create_board(client, auth, {"v": 1, "title": "Roadmap", "cards": {}})
  host = common_routes._own_host()

  read = client.get(f"/api/common/objects/{host}/{oid}/state", headers=auth)
  assert read.status_code == 200
  body = read.json()
  assert body["version"] == 1
  assert body["doc"]["title"] == "Roadmap"

  write = client.put(
    f"/api/common/objects/{host}/{oid}/state",
    json={"doc": {"v": 1, "title": "Roadmap 2", "cards": {}}, "expected_version": 1},
    headers=auth,
  )
  assert write.status_code == 200
  assert write.json() == {"status": "ok", "version": 2}

  stale = client.put(
    f"/api/common/objects/{host}/{oid}/state",
    json={"doc": {"v": 1, "title": "clobber", "cards": {}}, "expected_version": 1},
    headers=auth,
  )
  assert stale.status_code == 200
  conflict = stale.json()
  assert conflict["status"] == "conflict"
  assert conflict["version"] == 2
  assert conflict["doc"]["title"] == "Roadmap 2"  # current doc returned for merge

  # A version-gated read with a current cursor omits the doc payload.
  cached = client.get(f"/api/common/objects/{host}/{oid}/state", params={"since_version": 2}, headers=auth)
  assert cached.status_code == 200
  assert "doc" not in cached.json()


def test_join_requires_valid_invite_and_signature(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)

  bad = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": "not-a-real-invite"}),
  )
  assert bad.status_code == 403

  secret = _invite(client, auth, oid, role="editor")
  wrong_key, _ = _make_peer_keypair()
  forged = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(wrong_key, {"type": "object_join", "invite": secret}),
  )
  assert forged.status_code == 403

  joined = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": secret}),
  )
  assert joined.status_code == 200, joined.text
  body = joined.json()
  assert body["status"] == "joined"
  assert body["object"]["members"][PEER_HOST]["role"] == "editor"
  assert body["doc"]["title"] == "Board"


def test_capability_invite_is_consumed_by_first_distinct_peer(client, auth):
  oid = _create_board(client, auth)
  first_private, first_public = _make_peer_keypair()
  _seed_peer_actor_cache(first_public)
  secret = _invite(client, auth, oid)

  first = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(first_private, {"type": "object_join", "invite": secret}),
  )
  assert first.status_code == 200, first.text

  second_host = "second.example.com"
  second_private, second_public = _make_peer_keypair()
  _seed_peer_actor_cache(second_public, second_host)
  reused = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(
      second_private,
      {"type": "object_join", "invite": secret},
      second_host,
    ),
  )
  assert reused.status_code == 403
  assert reused.json()["detail"] == "Invite is invalid or expired."


def test_capability_invite_mutation_holds_the_object_lock(
  client, auth, monkeypatch,
):
  oid = _create_board(client, auth)
  held = False
  original_load = objects_routes._load_object
  original_save = objects_routes._save_object

  class TrackedLock:
    async def __aenter__(self):
      nonlocal held
      assert not held
      held = True

    async def __aexit__(self, *_args):
      nonlocal held
      held = False

  def load_while_locked(object_id):
    assert held
    return original_load(object_id)

  def save_while_locked(obj):
    assert held
    original_save(obj)

  monkeypatch.setattr(objects_routes, "_object_lock", lambda _oid: TrackedLock())
  monkeypatch.setattr(objects_routes, "_load_object", load_while_locked)
  monkeypatch.setattr(objects_routes, "_save_object", save_while_locked)

  created = client.post(
    f"/api/common/objects/{oid}/invites",
    json={"role": "editor"},
    headers=auth,
  )
  assert created.status_code == 200, created.text
  assert held is False


def test_peer_cas_write_and_viewer_confinement(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  secret = _invite(client, auth, oid, role="editor")
  client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": secret}),
  )

  ok = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {
      "type": "object_write",
      "doc": {"v": 1, "title": "Peer edit", "cards": {}},
      "expected_version": 1,
    }),
  )
  assert ok.status_code == 200
  assert ok.json() == {"status": "ok", "version": 2}

  stale = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {
      "type": "object_write",
      "doc": {"v": 1, "title": "stale", "cards": {}},
      "expected_version": 1,
    }),
  )
  assert stale.status_code == 200
  assert stale.json()["status"] == "conflict"
  assert stale.json()["doc"]["title"] == "Peer edit"

  # Downgrade to viewer by revoke + viewer re-invite; writes must then fail.
  assert client.delete(
    f"/api/common/objects/{oid}/members/{PEER_HOST}", headers=auth
  ).status_code == 200
  viewer_secret = _invite(client, auth, oid, role="viewer")
  rejoined = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": viewer_secret}),
  )
  assert rejoined.status_code == 200
  denied = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {
      "type": "object_write",
      "doc": {"v": 1, "title": "nope", "cards": {}},
      "expected_version": 2,
    }),
  )
  assert denied.status_code == 403


def test_revoked_member_loses_access(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  secret = _invite(client, auth, oid)
  client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": secret}),
  )
  assert client.delete(
    f"/api/common/objects/{oid}/members/{PEER_HOST}", headers=auth
  ).status_code == 200

  read = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_state", "since_version": -1}),
  )
  assert read.status_code == 403


def test_listing_and_membership_metadata(client, auth):
  oid = _create_board(client, auth)
  listing = client.get("/api/common/objects", params={"app": "kanban"}, headers=auth)
  assert listing.status_code == 200
  hosted = {o["id"]: o for o in listing.json()["hosted"]}
  assert oid in hosted  # the shared test data dir may hold earlier objects
  assert hosted[oid]["label"] == "Test board"
  # Invite secrets never appear in any readable surface.
  _invite(client, auth, oid)
  members = client.get(f"/api/common/objects/{oid}/members", headers=auth)
  assert members.status_code == 200
  assert "invites" not in members.json()
  assert "#" not in json.dumps(members.json())


def test_delete_object_removes_state(client, auth):
  oid = _create_board(client, auth)
  host = common_routes._own_host()
  assert client.delete(f"/api/common/objects/{oid}", headers=auth).status_code == 200
  assert client.get(
    f"/api/common/objects/{host}/{oid}/state", headers=auth
  ).status_code == 404
  assert objects_routes._load_object(oid) is None


def test_oversized_doc_is_rejected(client, auth):
  big = {"v": 1, "blob": "x" * (objects_routes.MAX_DOC_BYTES + 1)}
  response = client.post(
    "/api/common/objects",
    json={"app": "kanban", "kind": "board", "doc": big},
    headers=auth,
  )
  assert response.status_code == 413


def test_oversized_peer_envelope_is_rejected_before_buffering(client):
  response = client.post(
    "/api/common/objects/" + "ab" * 16 + "/peer",
    content=b"x" * (objects_routes.MAX_ENVELOPE_BYTES + 1),
    headers={"content-type": "application/json"},
  )
  assert response.status_code == 413


def test_handle_invite_preauthorizes_member_and_join_needs_no_code(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)

  # Invite by handle: the peer becomes a pending member even though their
  # instance is unreachable for delivery in tests.
  invited = client.post(
    f"/api/common/objects/{oid}/invites",
    json={"role": "editor", "address": f"ana@{PEER_HOST}"},
    headers=auth,
  )
  assert invited.status_code == 200, invited.text
  body = invited.json()
  assert body["status"] == "invited"
  assert body["host"] == PEER_HOST
  assert body["delivery"] == "unreachable"

  members = client.get(f"/api/common/objects/{oid}/members", headers=auth).json()
  assert members["members"][PEER_HOST]["pending"] is True

  # The signed join carries no code at all — identity is the credential.
  joined = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join"}),
  )
  assert joined.status_code == 200, joined.text
  assert joined.json()["object"]["members"][PEER_HOST].get("pending") is None

  # Re-inviting an active member is rejected.
  again = client.post(
    f"/api/common/objects/{oid}/invites",
    json={"role": "viewer", "address": PEER_HOST},
    headers=auth,
  )
  assert again.status_code == 400


@pytest.mark.asyncio
async def test_bare_handle_uses_account_registry_without_social_install(monkeypatch):
  async def resolve(_db, owner_id, handle):
    assert owner_id == 7
    assert handle == "collaborator"
    return [PEER_HOST]

  monkeypatch.setattr(identity_routes, "resolve_handle_hosts", resolve)

  assert await objects_routes._resolve_invitee("@Collaborator", object(), 7) == PEER_HOST


@pytest.mark.asyncio
async def test_known_handle_without_routable_mobius_is_distinct_from_missing(monkeypatch):
  async def resolve(_db, _owner_id, _handle):
    return []

  monkeypatch.setattr(identity_routes, "resolve_handle_hosts", resolve)

  with pytest.raises(HTTPException) as exc:
    await objects_routes._resolve_invitee("collaborator", object(), 7)
  assert exc.value.status_code == 409
  assert "exists" in exc.value.detail


def test_uninvited_signed_join_without_code_is_rejected(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  denied = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join"}),
  )
  assert denied.status_code == 403


def test_invitation_delivery_and_decline(client, auth):
  # Another instance invites THIS one: a signed invitation lands, is listed,
  # and declining removes it.
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  oid = "ab" * 16
  delivered = client.post(
    "/api/common/objects/invitations/deliver",
    json=_signed(private_b64, {
      "type": "object_invitation",
      "object": {"id": oid, "app": "kanban", "kind": "board",
                 "label": "Ana's roadmap", "role": "editor"},
    }),
  )
  assert delivered.status_code == 200, delivered.text

  listing = client.get(
    "/api/common/objects/invitations", params={"app": "kanban"}, headers=auth
  )
  invs = listing.json()["invitations"]
  assert any(i["id"] == oid and i["host"] == PEER_HOST for i in invs)

  declined = client.post(
    f"/api/common/objects/invitations/{PEER_HOST}/{oid}/decline", headers=auth
  )
  assert declined.status_code == 200
  listing = client.get(
    "/api/common/objects/invitations", params={"app": "kanban"}, headers=auth
  )
  assert not any(i["id"] == oid for i in listing.json()["invitations"])


def test_unsigned_invitation_is_rejected(client):
  response = client.post(
    "/api/common/objects/invitations/deliver",
    json={"v": 0, "type": "object_invitation", "from": PEER_HOST,
          "to": common_routes._own_host(), "sent_at": time.time(),
          "object": {"id": "cd" * 16, "app": "kanban", "role": "editor"},
          "sig": "bogus"},
  )
  assert response.status_code in (400, 403)


def test_member_entries_carry_handles(client, auth):
  oid = _create_board(client, auth)
  private_b64, public_b64 = _make_peer_keypair()
  # Actor card with a handle, like the current Common identity publishes.
  cache = common_routes._peer_cache_path(PEER_HOST)
  cache.parent.mkdir(parents=True, exist_ok=True)
  cache.write_text(json.dumps({
    "fetched_at": time.time(),
    "actor": {
      "protocol": common_routes.PROTOCOL, "host": PEER_HOST,
      "name": "Peer Person", "handle": "ana", "bio": "",
      "public_key": {"alg": "ed25519", "key_b64": public_b64},
      "inbox": "/api/common/inbox",
    },
  }))
  secret = _invite(client, auth, oid)
  joined = client.post(
    f"/api/common/objects/{oid}/peer",
    json=_signed(private_b64, {"type": "object_join", "invite": secret}),
  )
  assert joined.status_code == 200
  member = joined.json()["object"]["members"][PEER_HOST]
  assert member["handle"] == "ana"
  members = client.get(f"/api/common/objects/{oid}/members", headers=auth).json()
  assert members["members"][PEER_HOST]["handle"] == "ana"
