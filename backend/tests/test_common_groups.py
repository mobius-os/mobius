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

from app import models
from app.common_protocol import verify
from app.config import get_settings
from app.deps import Principal
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


def _owner_principal():
  return Principal(owner=models.Owner(id=1), app_id=None)


def _signed_group_envelope(private_b64, host=PEER_HOST, **fields):
  envelope = {"v": 0, "from": host, "sent_at": time.time(), **fields}
  envelope["sig"] = common_routes._sign(envelope, private_b64)
  return envelope


def _accept_host_member(client, private_b64, gid, host=PEER_HOST):
  invitation_id = groups_routes._load_host_group(gid)["members"][host]["invitation_id"]
  envelope = _signed_group_envelope(
    private_b64, host=host, type="group_accept", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, invitation_id=invitation_id,
  )
  response = client.post("/api/common/groups/inbox", json=envelope)
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "accepted"


def test_create_group_invites_members(client, db, auth, sent):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
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
  assert meta["request_status"] == "accepted"
  assert host_record["members"][PEER_HOST]["status"] == "invited"

  sent.clear()
  response = client.post(
    f"/api/common/groups/{gid}/send",
    json={"text": "host-authored message"}, headers=auth,
  )
  assert response.status_code == 200, response.text
  assert sent == [], "pending invitees are not active fan-out recipients"

  _accept_host_member(client, private_b64, gid)
  sent.clear()
  response = client.post(
    f"/api/common/groups/{gid}/send",
    json={"text": "after acceptance"}, headers=auth,
  )
  relay_host, relay = sent[0]
  assert relay_host == PEER_HOST
  original = relay["original"]
  assert original["type"] == "group_post"
  assert original["id"] == relay["id"]
  assert original["from"] == common_routes._own_host()
  assert original["to"] == common_routes._own_host()
  identity = common_routes._load_identity()
  assert verify(
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
  _accept_host_member(client, private_b64, gid)
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
  _accept_host_member(client, private_b64, gid)
  _accept_host_member(client, private_b64, gid, host="other.example.com")
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


def test_group_invitation_and_pending_messages_stay_quiet_until_acceptance(
  client, db, auth, sent, monkeypatch,
):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  # An accepted DM is intentionally irrelevant to group participation.
  convo = common_routes._conversation_dir(app, PEER_HOST)
  convo.mkdir(parents=True)
  (convo / "meta.json").write_text(json.dumps({
    "peer": PEER_HOST, "request_status": "accepted",
  }))
  notified = []
  monkeypatch.setattr(
    groups_routes.push, "notify_owner", lambda *_args, **_kwargs: notified.append(True)
  )

  gid = str(uuid.uuid4())
  added = _signed_group_envelope(
    private_b64, type="group_added", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid, group_name="Quiet plans",
    members=[
      {"host": PEER_HOST, "handle": "peer"},
      {"host": common_routes._own_host(), "handle": "alex"},
    ],
  )
  first = client.post("/api/common/groups/inbox", json=added)
  assert first.json() == {"status": "pending"}
  assert client.post("/api/common/groups/inbox", json=added).json() == {
    "status": "pending",
  }
  meta_path = groups_routes._group_dir(app, gid) / "meta.json"
  assert json.loads(meta_path.read_text())["request_status"] == "pending"
  assert notified == []

  original = _signed_group_envelope(
    private_b64, type="group_post", id=str(uuid.uuid4()), to=PEER_HOST,
    gid=gid, text="A message before acceptance",
  )
  relay = _signed_group_envelope(
    private_b64, type="group_message", id=original["id"],
    to=common_routes._own_host(), gid=gid, group_name="Quiet plans",
    author=PEER_HOST, author_handle="peer", text=original["text"],
    original=original,
  )
  assert client.post("/api/common/groups/inbox", json=relay).json() == {
    "status": "pending",
  }
  assert client.post("/api/common/groups/inbox", json=relay).json() == {
    "status": "duplicate",
  }
  pending = json.loads(meta_path.read_text())
  assert pending["request_status"] == "pending"
  assert pending["request_count"] == 2  # invitation plus one retained message
  assert pending["unread"] == 0
  assert notified == []

  accepted = client.post(f"/api/common/groups/{gid}/accept", headers=auth)
  assert accepted.json() == {"status": "accepted"}
  action_count = len(sent)
  assert sent[-1][1]["type"] == "group_accept"
  assert client.post(f"/api/common/groups/{gid}/accept", headers=auth).json() == {
    "status": "accepted",
  }
  assert len(sent) == action_count
  assert json.loads(meta_path.read_text())["request_status"] == "accepted"

  followup_original = _signed_group_envelope(
    private_b64, type="group_post", id=str(uuid.uuid4()), to=PEER_HOST,
    gid=gid, text="After acceptance",
  )
  followup = _signed_group_envelope(
    private_b64, type="group_message", id=followup_original["id"],
    to=common_routes._own_host(), gid=gid, group_name="Quiet plans",
    author=PEER_HOST, author_handle="peer", text=followup_original["text"],
    original=followup_original,
  )
  assert client.post("/api/common/groups/inbox", json=followup).json() == {
    "status": "delivered",
  }
  final = json.loads(meta_path.read_text())
  assert final["unread"] == 1
  assert notified == [True]


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
  pending_send = client.post(
    f"/api/common/groups/{gid}/send", json={"text": "too soon"}, headers=auth,
  )
  assert pending_send.status_code == 409
  accepted = client.post(f"/api/common/groups/{gid}/accept", headers=auth)
  assert accepted.status_code == 200, accepted.text
  assert accepted.json() == {"status": "accepted"}
  sent.clear()

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


def test_add_member_returns_safe_roster_and_retry_results_without_history(client, db, auth, sent, monkeypatch):
  app = _install_common_app(db)
  _join_locally()
  _, public = _make_peer_keypair()
  _seed_peer_actor_cache(public)
  gid = client.post('/api/common/groups', json={'name': 'Team'}, headers=auth).json()['gid']
  client.post(f'/api/common/groups/{gid}/send', json={'text': 'Earlier private history'}, headers=auth)
  sent.clear()

  async def actor(_host):
    return {'handle': 'peer', 'name': 'Private display name', 'unexpected': 'private'}
  monkeypatch.setattr(groups_routes, '_fetch_actor', actor)
  added = client.post(f'/api/common/groups/{gid}/members', json={'host': PEER_HOST}, headers=auth)
  assert added.status_code == 200
  result = added.json()
  assert result['status'] == 'added'
  assert result['delivered'] == {PEER_HOST: True}
  assert all(set(member) == {'host', 'handle'} for member in result['members'])
  assert {m['host'] for m in result['members']} == {common_routes._own_host(), PEER_HOST}
  assert [envelope['type'] for _, envelope in sent] == ['group_added']
  assert 'Earlier private history' not in json.dumps(sent)
  assert 'Private display name' not in json.dumps(sent)
  assert groups_routes._load_group_meta(app, gid)['members'] == result['members']

  async def offline(_host, _envelope): return False
  monkeypatch.setattr(groups_routes, '_deliver', offline)
  retry = client.post(f'/api/common/groups/{gid}/members', json={'host': PEER_HOST}, headers=auth)
  assert retry.json() == {**result, 'status': 'already_member', 'delivered': {PEER_HOST: False}}
  assert client.post('/api/common/groups/not-a-group/members', json={'host': PEER_HOST}, headers=auth).status_code == 400


def test_declined_host_invitation_requires_deliberate_reinvite(
  client, db, auth, sent,
):
  _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  gid = client.post(
    "/api/common/groups",
    json={"name": "Maybe", "members": [PEER_HOST]}, headers=auth,
  ).json()["gid"]
  decline = _signed_group_envelope(
    private_b64, type="group_decline", id=str(uuid.uuid4()),
    to=common_routes._own_host(), gid=gid,
    invitation_id=groups_routes._load_host_group(gid)["members"][PEER_HOST]["invitation_id"],
  )
  assert client.post("/api/common/groups/inbox", json=decline).json() == {
    "status": "declined",
  }
  assert groups_routes._load_host_group(gid)["members"][PEER_HOST]["status"] == "declined"
  sent.clear()
  # Messages are not fanned out to a declined invitation.
  assert client.post(
    f"/api/common/groups/{gid}/send", json={"text": "quiet"}, headers=auth,
  ).json()["failed_members"] == []
  assert sent == []
  reinvite = client.post(
    f"/api/common/groups/{gid}/members", json={"host": PEER_HOST}, headers=auth,
  )
  assert reinvite.json()["status"] == "reinvited"
  assert groups_routes._load_host_group(gid)["members"][PEER_HOST]["status"] == "invited"
  assert sent[-1][1]["type"] == "group_added"


def test_member_reinvite_version_reopens_once_and_rejects_delayed_invite(
  client, db, auth, sent,
):
  app = _install_common_app(db)
  _join_locally()
  private_b64, public_b64 = _make_peer_keypair()
  _seed_peer_actor_cache(public_b64)
  gid = str(uuid.uuid4())
  first = _signed_group_envelope(
    private_b64, type="group_added", id=str(uuid.uuid4()),
    invitation_version=1, to=common_routes._own_host(), gid=gid,
    group_name="Versioned", members=[],
  )
  assert client.post("/api/common/groups/inbox", json=first).json() == {
    "status": "pending",
  }
  assert client.post(f"/api/common/groups/{gid}/decline", headers=auth).json()[
    "status"
  ] == "declined"
  second = _signed_group_envelope(
    private_b64, type="group_added", id=str(uuid.uuid4()),
    invitation_version=2, to=common_routes._own_host(), gid=gid,
    group_name="Versioned", members=[],
  )
  assert client.post("/api/common/groups/inbox", json=second).json() == {
    "status": "pending",
  }
  assert groups_routes._load_group_meta(app, gid)["invitation_id"] == second["id"]
  assert client.post("/api/common/groups/inbox", json=first).json() == {
    "status": "stale",
  }
  meta = groups_routes._load_group_meta(app, gid)
  assert meta["invitation_id"] == second["id"]
  assert meta["request_status"] == "pending"


def test_host_deletion_is_idempotent_preserves_history_and_stops_all_writes(client, db, auth, sent, monkeypatch):
  app = _install_common_app(db)
  _join_locally()
  private, public = _make_peer_keypair()
  _seed_peer_actor_cache(public)
  gid = client.post('/api/common/groups', json={'name': 'Team', 'members': [PEER_HOST]}, headers=auth).json()['gid']
  posted = client.post(f'/api/common/groups/{gid}/send', json={'text': 'Keep this history'}, headers=auth).json()
  history = groups_routes._group_dir(app, gid) / 'msgs' / f"{posted['id']}.json"
  before = history.read_bytes()
  sent.clear()
  deleted = client.delete(f'/api/common/groups/{gid}', headers=auth)
  assert deleted.json() == {'status': 'deleted', 'delivered': {PEER_HOST: True}}
  group = groups_routes._load_host_group(gid)
  assert group['deleted_at'] > 0
  assert groups_routes._load_group_meta(app, gid)['deleted_at'] == group['deleted_at']
  assert history.read_bytes() == before
  assert sent[0][1]['type'] == 'group_deleted'
  assert sent[0][1]['sig']
  assert 'members' not in sent[0][1]

  async def old_server(_host, _envelope): return False
  monkeypatch.setattr(groups_routes, '_deliver', old_server)
  retry = client.delete(f'/api/common/groups/{gid}', headers=auth)
  assert retry.json() == {'status': 'deleted', 'delivered': {PEER_HOST: False}}
  assert groups_routes._load_host_group(gid)['deleted_at'] == group['deleted_at']
  assert client.post(f'/api/common/groups/{gid}/send', json={'text': 'Too late'}, headers=auth).status_code == 410
  assert client.post(f'/api/common/groups/{gid}/members', json={'host': 'another.example.com'}, headers=auth).status_code == 410
  incoming = _signed_group_envelope(private, type='group_post', id=str(uuid.uuid4()), to=common_routes._own_host(), gid=gid, text='Too late')
  assert client.post('/api/common/groups/inbox', json=incoming).status_code == 410
  assert len(list(history.parent.glob('*.json'))) == 1


def test_group_lifecycle_owner_surface_rejects_other_apps_and_remote_members(client, db, auth, sent):
  from app import auth as app_auth, models
  app = _install_common_app(db)
  _join_locally()
  gid = client.post('/api/common/groups', json={'name': 'Local group'}, headers=auth).json()['gid']
  other = models.App(name='Other', slug='other', source_dir='other', description='', jsx_source='')
  db.add(other); db.commit(); db.refresh(other)
  other_token = app_auth.create_access_token({'sub': 'test', 'scope': 'app', 'app_id': other.id})
  other_auth = {'Authorization': f'Bearer {other_token}'}
  assert client.delete(f'/api/common/groups/{gid}').status_code == 401
  assert client.delete(f'/api/common/groups/{gid}', headers=other_auth).status_code == 403
  assert client.post(f'/api/common/groups/{gid}/members', json={'host': PEER_HOST}, headers=other_auth).status_code == 403
  token = app_auth.create_access_token({'sub': 'test', 'scope': 'app', 'app_id': app.id})
  assert client.delete(f'/api/common/groups/{gid}', headers={'Authorization': f'Bearer {token}'}).status_code == 200

  private, public = _make_peer_keypair(); _seed_peer_actor_cache(public)
  remote_gid = str(uuid.uuid4())
  added = _signed_group_envelope(private, type='group_added', id=str(uuid.uuid4()), to=common_routes._own_host(), gid=remote_gid, members=[], group_name='Remote')
  assert client.post('/api/common/groups/inbox', json=added).status_code == 200
  assert client.post(f'/api/common/groups/{remote_gid}/accept').status_code == 401
  assert client.post(f'/api/common/groups/{remote_gid}/accept', headers=other_auth).status_code == 403
  assert client.post(
    f'/api/common/groups/{remote_gid}/accept',
    headers={'Authorization': f'Bearer {token}'},
  ).json() == {'status': 'accepted'}
  assert client.delete(f'/api/common/groups/{remote_gid}', headers=auth).status_code == 404
  assert client.post(f'/api/common/groups/{remote_gid}/members', json={'host': 'new.example.com'}, headers=auth).status_code == 404


def test_known_host_cannot_be_replaced_and_deleted_group_cannot_be_revived(client, db, auth, sent):
  app = _install_common_app(db)
  _join_locally()
  private, public = _make_peer_keypair(); _seed_peer_actor_cache(public)
  attacker_private, attacker_public = _make_peer_keypair()
  attacker = 'attacker.example.com'; _seed_peer_actor_cache(attacker_public, host=attacker)
  gid = str(uuid.uuid4())
  fields = {'type': 'group_added', 'id': str(uuid.uuid4()), 'to': common_routes._own_host(), 'gid': gid, 'members': [], 'group_name': 'Original'}
  added = _signed_group_envelope(private, **fields)
  assert client.post('/api/common/groups/inbox', json=added).status_code == 200
  assert client.post('/api/common/groups/inbox', json=_signed_group_envelope(attacker_private, host=attacker, **fields)).status_code == 403
  closure = {**fields, 'type': 'group_deleted'}
  assert client.post('/api/common/groups/inbox', json=_signed_group_envelope(attacker_private, host=attacker, **closure)).status_code == 403
  assert groups_routes._load_group_meta(app, gid)['host'] == PEER_HOST
  assert client.post(f'/api/common/groups/{gid}/send', json={'text': 'Too soon'}, headers=auth).status_code == 409
  assert client.post(f'/api/common/groups/{gid}/accept', headers=auth).status_code == 200
  sent.clear()
  sent_message = client.post(f'/api/common/groups/{gid}/send', json={'text': 'Local history'}, headers=auth).json()
  history = groups_routes._group_dir(app, gid) / 'msgs' / f"{sent_message['id']}.json"
  before = history.read_bytes()
  deleted = _signed_group_envelope(private, **closure)
  assert client.post('/api/common/groups/inbox', json=deleted).status_code == 200
  tombstone = groups_routes._load_group_meta(app, gid)['deleted_at']
  assert client.post('/api/common/groups/inbox', json=deleted).status_code == 200
  assert groups_routes._load_group_meta(app, gid)['deleted_at'] == tombstone
  assert history.read_bytes() == before
  assert client.post('/api/common/groups/inbox', json=added).status_code == 410
  assert client.post(f'/api/common/groups/{gid}/send', json={'text': 'Too late'}, headers=auth).status_code == 410
  original = _signed_group_envelope(private, type='group_post', id=str(uuid.uuid4()), to=PEER_HOST, gid=gid, text='Late relay')
  relay = _signed_group_envelope(private, type='group_message', id=original['id'], to=common_routes._own_host(), gid=gid, original=original)
  assert client.post('/api/common/groups/inbox', json=relay).status_code == 410
  assert len(list(history.parent.glob('*.json'))) == 1


def test_deleted_before_first_invite_stays_closed_and_invalid_roster_never_writes(client, db):
  app = _install_common_app(db)
  private, public = _make_peer_keypair(); _seed_peer_actor_cache(public)
  gid = str(uuid.uuid4())
  fields = {'to': common_routes._own_host(), 'gid': gid, 'group_name': 'Delayed'}
  deleted = _signed_group_envelope(private, type='group_deleted', **fields)
  assert client.post('/api/common/groups/inbox', json=deleted).status_code == 200
  added = _signed_group_envelope(private, type='group_added', members=[], **fields)
  assert client.post('/api/common/groups/inbox', json=added).status_code == 410
  assert groups_routes._load_group_meta(app, gid)['deleted_at']

  for members in [[{'host': '../escape'}], [{'host': 17}], ['not a member'], {}]:
    bad_gid = str(uuid.uuid4())
    added = _signed_group_envelope(private, type='group_added', members=members, **{**fields, 'gid': bad_gid})
    assert client.post('/api/common/groups/inbox', json=added).status_code == 400
    assert groups_routes._load_group_meta(app, bad_gid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['send', 'add', 'peer_post'])
async def test_host_delete_serializes_with_inflight_group_operation(db, sent, monkeypatch, operation):
  import asyncio
  from fastapi import HTTPException
  app = _install_common_app(db)
  _join_locally()
  private, public = _make_peer_keypair(); _seed_peer_actor_cache(public)
  second = 'second.example.com'; _seed_peer_actor_cache(public, host=second)
  new_member = 'new.example.com'; _seed_peer_actor_cache(public, host=new_member)
  monkeypatch.setattr(groups_routes, '_require_owner_or_common_app', lambda *_args: app)
  principal = _owner_principal()
  created = await groups_routes.create_group(
    groups_routes.CreateGroup(name='Race', members=[PEER_HOST, second]),
    db,
    principal,
  )
  gid = created['gid']
  group = groups_routes._load_host_group(gid)
  for member in (PEER_HOST, second):
    group['members'][member]['status'] = 'active'
  groups_routes._host_group_path(gid).write_text(json.dumps(group))
  entered, release = asyncio.Event(), asyncio.Event()
  order = []

  async def paused_delivery(_host, envelope):
    if envelope['type'] != 'group_deleted':
      entered.set()
      await release.wait()
    order.append(envelope['type'])
    return True
  monkeypatch.setattr(groups_routes, '_deliver', paused_delivery)
  if operation == 'send':
    work = groups_routes.send_group_message(
      gid, groups_routes.GroupSend(text='Before closure'), db, principal,
    )
  elif operation == 'add':
    work = groups_routes.add_group_member(
      gid, groups_routes.AddMember(host=new_member), db, principal,
    )
  else:
    envelope = _signed_group_envelope(private, type='group_post', id=str(uuid.uuid4()), to=common_routes._own_host(), gid=gid, text='Before closure')
    async def read(_request): return envelope
    monkeypatch.setattr(groups_routes, '_read_envelope', read)
    work = groups_routes.group_inbox(object(), db)
  task = asyncio.create_task(work)
  await asyncio.wait_for(entered.wait(), 2)
  deleting = asyncio.create_task(groups_routes.delete_group(gid, db, principal))
  await asyncio.sleep(0)
  assert not deleting.done()
  assert not groups_routes._load_host_group(gid).get('deleted_at')
  release.set()
  await asyncio.wait_for(asyncio.gather(task, deleting), 2)
  if operation == 'add':
    assert new_member in groups_routes._load_host_group(gid)['members']
  first_delete = order.index('group_deleted')
  assert all(kind == 'group_deleted' for kind in order[first_delete:])
  with pytest.raises(HTTPException) as exc:
    await groups_routes.send_group_message(
      gid, groups_routes.GroupSend(text='After closure'), db, principal,
    )
  assert exc.value.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [True, False])
async def test_member_closure_preserves_only_a_host_acknowledged_inflight_message(db, sent, monkeypatch, accepted):
  import asyncio
  from fastapi import HTTPException
  app = _install_common_app(db)
  _join_locally()
  private, public = _make_peer_keypair(); _seed_peer_actor_cache(public)
  gid = str(uuid.uuid4())
  await groups_routes._store_group_meta(app, gid, {'gid': gid, 'host': PEER_HOST, 'name': 'Remote'})
  monkeypatch.setattr(groups_routes, '_require_owner_or_common_app', lambda *_args: app)
  entered, release = asyncio.Event(), asyncio.Event()
  async def paused_delivery(_host, _envelope):
    entered.set(); await release.wait(); return accepted
  monkeypatch.setattr(groups_routes, '_deliver', paused_delivery)
  principal = _owner_principal()
  sending = asyncio.create_task(groups_routes.send_group_message(
    gid, groups_routes.GroupSend(text='Racing closure'), db, principal,
  ))
  await asyncio.wait_for(entered.wait(), 2)
  deleted = _signed_group_envelope(private, type='group_deleted', to=common_routes._own_host(), gid=gid)
  async def read(_request): return deleted
  monkeypatch.setattr(groups_routes, '_read_envelope', read)
  # Receipt must not deadlock behind the outbound request to this same host.
  assert (await asyncio.wait_for(groups_routes.group_inbox(object(), db), 2))['status'] == 'deleted'
  tombstone = groups_routes._load_group_meta(app, gid)['deleted_at']
  release.set()
  if accepted:
    receipt = await sending
    assert receipt['status'] == 'delivered'
    assert receipt['group_deleted'] is True
    history = groups_routes._group_dir(app, gid) / 'msgs' / f"{receipt['id']}.json"
    assert json.loads(history.read_text())['text'] == 'Racing closure'
  else:
    with pytest.raises(HTTPException) as exc:
      await sending
    assert exc.value.status_code == 410
    assert not (groups_routes._group_dir(app, gid) / 'msgs').exists()
  meta = groups_routes._load_group_meta(app, gid)
  assert meta['deleted_at'] == tombstone
  assert meta['unread'] == 0
