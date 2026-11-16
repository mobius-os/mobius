"""Inviting someone to a live Project by Möbius @handle.

Both Möbius instances are this one test server: every outbound peer request is
looped back into the same app, so the sender, the receiver's verification, and
the acceptance all run their real endpoints.
"""
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest

from app import models
from app.config import get_settings
from app.routes import project_handle_invites as invites
from app.timeutil import now_naive_utc

HOST = "ana.example"


@pytest.fixture
def peers(client, monkeypatch):
  """Loop peer HTTPS calls into this app and stub the account directory."""
  monkeypatch.setattr(get_settings(), "frontend_origin", f"https://{HOST}")
  directory = {"ana": [HOST], "bob": [HOST], "mallory": ["mallory.example"]}
  state = {"me": "ana", "calls": [], "fail_delivery": False, "directory": directory, "redeem_answer": None}

  async def resolve(_db, _owner_id, handle):
    if handle not in directory:
      raise invites.HTTPException(404, "No one has claimed that mobius.you handle.")
    return directory[handle]

  async def own_handle(_db, _owner):
    return state["me"]

  async def federation(method, url, *, json=None, timeout_seconds=10.0, **_):
    parts = urlsplit(url)
    state["calls"].append((parts.netloc, parts.path))
    if parts.netloc == "dead.example":
      # What federation_request raises for a host that doesn't resolve.
      raise invites.HTTPException(400, "Cannot resolve host 'dead.example'")
    if parts.path.endswith("/redeem") and state["redeem_answer"] is not None:
      return httpx.Response(200, json=state["redeem_answer"])
    if parts.netloc != HOST or (state["fail_delivery"] and parts.path.endswith("/inbox")):
      raise httpx.ConnectError("unreachable")
    if parts.path.endswith("/inbox"):
      # The receiver answers as the invitee.
      state["me"] = "bob"
    try:
      return client.request(method, parts.path, json=json)
    finally:
      if parts.path.endswith("/inbox"):
        state["me"] = "ana"

  monkeypatch.setattr(invites, "resolve_handle_hosts", resolve)
  monkeypatch.setattr(invites, "owner_handle", own_handle)
  monkeypatch.setattr(invites, "federation_request", federation)
  invites.inbox_limiter.reset()
  return state


def make_project(client, auth, name="Shared garden"):
  response = client.post("/api/projects", headers=auth, json={"name": name, "template_id": "blank"})
  assert response.status_code == 200, response.text
  return response.json()


def send(client, auth, project, handle="@Bob", role="editor"):
  return client.post("/api/project-invites/send", headers=auth, json={
    "project_id": project["id"], "handle": handle, "role": role,
  })


def test_handle_invite_arrives_verified_notifies_and_accepting_joins_as_handle(client, auth, db, peers):
  project = make_project(client, auth)
  sent = send(client, auth, project)
  assert sent.status_code == 200, sent.text
  assert sent.json()["handle"] == "bob" and sent.json()["delivered_to"] == HOST
  assert "join_url" not in sent.json()
  assert (HOST, "/api/project-invites/check") in peers["calls"]

  received = client.get("/api/project-invites", headers=auth).json()
  assert len(received) == 1
  invite = received[0]
  assert invite["from_handle"] == "ana"
  assert invite["project_name"] == "Shared garden"
  assert invite["role"] == "editor" and invite["status"] == "pending"
  row = db.get(models.ReceivedProjectInvite, invite["id"])
  assert row.invite_secret_sealed and len(row.invite_secret_sealed) > 43

  note = db.query(models.Notification).filter_by(source_id=invite["id"]).one()
  assert note.title == "@ana invited you to a project"
  assert note.target == "/shell/?projects=1"

  # The sender sees the invitation as pending under the invitee's handle.
  collaboration = client.get(f"/api/projects/{project['id']}/collaboration", headers=auth).json()
  assert [item["invitee_name"] for item in collaboration["invites"]] == ["@bob"]

  peers["me"] = "bob"
  accepted = client.post(f"/api/project-invites/{invite['id']}/accept", headers=auth)
  assert accepted.status_code == 200, accepted.text
  body = accepted.json()
  assert body["status"] == "accepted"
  assert body["open_url"].startswith(f"https://{HOST}/shared/project/{project['id']}#access=")
  member = db.query(models.ProjectMember).filter_by(project_id=project["id"]).one()
  assert member.display_name == "@bob" and member.role == "editor"
  # The shared project says how long this access lasts.
  row = db.get(models.ReceivedProjectInvite, invite["id"])
  db.refresh(row)
  assert abs(row.access_expires_at - (now_naive_utc() + timedelta(days=30))) < timedelta(minutes=1)

  opened = client.post(f"/api/project-invites/{invite['id']}/open", headers=auth)
  assert opened.status_code == 200
  token = opened.json()["url"].split("#access=", 1)[1]
  detail = client.get(f"/api/projects/{project['id']}", headers={"Authorization": f"Bearer {token}"})
  assert detail.status_code == 200, detail.text

  # Accepting twice is harmless and never spends a second invitation.
  assert client.post(f"/api/project-invites/{invite['id']}/accept", headers=auth).status_code == 200
  assert db.query(models.ProjectMember).filter_by(project_id=project["id"]).count() == 1


def test_inbox_rejects_an_invite_whose_host_is_not_the_senders(client, auth, db, peers):
  peers["calls"].clear()
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1",
    "from_handle": "mallory",
    "invite_url": f"https://{HOST}/project-invite#" + "a" * 43,
  })
  assert response.status_code == 403
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert peers["calls"] == []


def test_inbox_rejects_an_invitation_the_sender_cannot_confirm(client, auth, db, peers):
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1",
    "from_handle": "ana",
    "invite_url": f"https://{HOST}/project-invite#" + "b" * 43,
  })
  assert response.status_code == 422
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert db.query(models.Notification).count() == 0


@pytest.mark.parametrize("url", [
  f"http://{HOST}/project-invite#" + "c" * 43,
  f"https://{HOST}/elsewhere#" + "c" * 43,
  f"https://{HOST}/project-invite?x=1#" + "c" * 43,
  f"https://{HOST}/project-invite#short",
])
def test_inbox_rejects_malformed_invitation_links(client, peers, url):
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1", "from_handle": "ana", "invite_url": url,
  })
  assert response.status_code == 422


def test_undeliverable_invite_is_withdrawn(client, auth, db, peers):
  project = make_project(client, auth)
  peers["fail_delivery"] = True
  response = send(client, auth, project)
  assert response.status_code == 502
  assert "@bob" in response.json()["detail"]
  row = db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one()
  assert row.revoked_at is not None


def test_self_and_unknown_handles_are_refused_before_creating_an_invite(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project, handle="@ana").status_code == 400
  assert send(client, auth, project, handle="@nobody").status_code == 404
  assert send(client, auth, project, handle="not a handle!").status_code == 400
  assert db.query(models.ProjectInvite).count() == 0


def test_decline_hides_and_remove_deletes(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  declined = client.post(f"/api/project-invites/{invite_id}/decline", headers=auth)
  assert declined.status_code == 200 and declined.json()["status"] == "declined"
  assert client.get("/api/project-invites", headers=auth).json() == []
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 409
  assert db.get(models.ReceivedProjectInvite, invite_id).invite_secret_sealed is None
  assert client.delete(f"/api/project-invites/{invite_id}", headers=auth).status_code == 204
  assert db.get(models.ReceivedProjectInvite, invite_id) is None


def test_owner_only_endpoints_require_the_owner(client, peers):
  assert client.get("/api/project-invites").status_code == 401
  assert client.post("/api/project-invites/send", json={"project_id": "x", "handle": "bob"}).status_code == 401


def test_one_sender_cannot_crowd_the_inbox(client, auth, db, peers):
  now = now_naive_utc()
  for index in range(invites.MAX_PENDING_PER_SENDER):
    db.add(models.ReceivedProjectInvite(
      id=str(uuid.uuid4()), delivery_key=f"{index:064x}", from_handle="ana",
      from_host=HOST, project_name="Waiting", role="editor", received_at=now,
      expires_at=now + timedelta(days=1), status="pending",
    ))
  db.commit()
  project = make_project(client, auth)
  refused = send(client, auth, project)
  assert refused.status_code == 502
  assert "@ana already has 10 invitations waiting here" in refused.json()["detail"]
  assert db.query(models.ReceivedProjectInvite).count() == invites.MAX_PENDING_PER_SENDER


def link_invite(client, auth, project, invitee_name=None):
  response = client.post(f"/api/projects/{project['id']}/invites", headers=auth, json={
    "invitee_name": invitee_name, "role": "editor",
  })
  assert response.status_code == 200, response.text
  return response.json()


def deliver(client, url, sender="ana"):
  return client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1", "from_handle": sender, "invite_url": url,
  })


def test_an_unresolvable_host_is_skipped_for_the_next_one(client, auth, db, peers):
  peers["directory"]["bob"] = ["dead.example", HOST]
  project = make_project(client, auth)
  sent = send(client, auth, project)
  assert sent.status_code == 200, sent.text
  assert sent.json()["delivered_to"] == HOST


def test_an_invite_no_host_accepts_is_revoked_even_when_every_host_is_unresolvable(client, auth, db, peers):
  peers["directory"]["bob"] = ["dead.example"]
  project = make_project(client, auth)
  response = send(client, auth, project)
  assert response.status_code == 502
  assert "resolve" not in response.json()["detail"]
  row = db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one()
  assert row.revoked_at is not None


def test_only_an_invitation_made_for_this_person_reaches_their_inbox(client, auth, db, peers):
  project = make_project(client, auth)
  # A private link, or an invitation meant for someone else, may be held by
  # anyone; pushing it into this inbox must not read as "@ana invited you".
  for name in (None, "@bob"):
    response = deliver(client, link_invite(client, auth, project, name)["join_url"])
    assert response.status_code == 403, response.text
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert db.query(models.Notification).count() == 0


def test_repeated_delivery_stores_and_notifies_once(client, auth, db, peers):
  project = make_project(client, auth)
  url = link_invite(client, auth, project, "@ana")["join_url"]
  assert deliver(client, url).status_code == 200
  assert deliver(client, url).status_code == 200
  assert db.query(models.ReceivedProjectInvite).count() == 1
  assert db.query(models.Notification).count() == 1


def test_an_invitation_about_to_expire_is_refused(client, auth, db, peers):
  project = make_project(client, auth)
  invite = link_invite(client, auth, project, "@ana")
  db.get(models.ProjectInvite, invite["id"]).expires_at = now_naive_utc() + timedelta(minutes=5)
  db.commit()
  response = deliver(client, invite["join_url"])
  assert response.status_code == 422
  assert db.query(models.ReceivedProjectInvite).count() == 0


def test_check_describes_only_a_live_invitation_and_is_never_cached(client, auth, db, peers):
  project = make_project(client, auth)
  invite = link_invite(client, auth, project, "@ana")
  secret = invite["join_url"].split("#", 1)[1]
  checked = client.post("/api/project-invites/check", json={"invite": secret})
  assert checked.status_code == 200
  assert checked.headers["cache-control"] == "no-store"
  assert checked.json()["invitee"] == "@ana"
  db.get(models.ProjectInvite, invite["id"]).revoked_at = now_naive_utc()
  db.commit()
  assert client.post("/api/project-invites/check", json={"invite": secret}).status_code == 410


def test_accepting_a_withdrawn_invitation_marks_it_expired(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at = now_naive_utc()
  db.commit()
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 410
  listed = client.get("/api/project-invites", headers=auth).json()[0]
  assert listed["status"] == "pending" and listed["expired"] is True


def test_a_malformed_redeem_answer_leaves_the_invitation_pending(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  peers["redeem_answer"] = {"access_token": "short", "project": {"id": "../x"}}
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  assert db.get(models.ReceivedProjectInvite, invite_id).status == "pending"


def test_the_inbox_is_rate_limited(client, peers):
  url = f"https://{HOST}/project-invite#short"
  codes = [deliver(client, url).status_code for _ in range(31)]
  assert codes[:30] == [422] * 30 and codes[30] == 429
