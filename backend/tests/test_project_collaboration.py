"""Project invitations enforce revocable, role-scoped human access."""

from datetime import timedelta

from urllib.parse import urlsplit

from app import models
from app.timeutil import now_naive_utc


def _project(client, auth, name="Shared workspace"):
  response = client.post(
    "/api/projects", headers=auth,
    json={"name": name, "template_id": "blank"},
  )
  assert response.status_code == 200, response.text
  return response.json()


def _invite(client, auth, project_id, role="editor", name="A collaborator"):
  response = client.post(
    f"/api/projects/{project_id}/invites", headers=auth,
    json={"invitee_name": name, "role": role},
  )
  assert response.status_code == 200, response.text
  payload = response.json()
  return payload, urlsplit(payload["join_url"]).fragment


def _redeem(client, secret, display_name="Sam"):
  response = client.post(
    "/api/projects/invites/redeem",
    json={"invite": secret, "display_name": display_name},
  )
  assert response.status_code == 200, response.text
  payload = response.json()
  return payload, {"Authorization": f"Bearer {payload['access_token']}"}


def test_editor_invite_is_one_use_and_confined_to_one_project(
  client, auth, db,
):
  project = _project(client, auth)
  other = _project(client, auth, "Not shared")
  invite, secret = _invite(client, auth, project["id"])

  stored = db.get(models.ProjectInvite, invite["id"])
  assert stored.token_hash != secret
  assert secret not in stored.token_hash

  session, guest = _redeem(client, secret)
  assert session["role"] == "editor"
  assert client.post(
    "/api/projects/invites/redeem",
    json={"invite": secret, "display_name": "Replay"},
  ).status_code == 410

  detail = client.get(f"/api/projects/{project['id']}", headers=guest)
  assert detail.status_code == 200
  assert detail.json()["chats"] == []
  assert client.get("/api/projects", headers=guest).status_code == 403
  assert client.get(
    f"/api/projects/{other['id']}", headers=guest,
  ).status_code == 404

  write = client.put(
    f"/api/projects/{project['id']}/file?path=notes.txt", headers=guest,
    json={"content": "working together\n", "expected_revision": None},
  )
  assert write.status_code == 200, write.text
  missing_precondition = client.put(
    f"/api/projects/{project['id']}/file?path=notes.txt", headers=guest,
    json={"content": "unsafe overwrite\n"},
  )
  assert missing_precondition.status_code == 428
  assert client.get(
    f"/api/projects/{project['id']}/file?path=notes.txt", headers=guest,
  ).json()["content"] == "working together\n"


def test_role_changes_apply_immediately_and_revocation_ends_the_session(
  client, auth,
):
  project = _project(client, auth)
  _invite_row, secret = _invite(client, auth, project["id"], role="editor")
  session, guest = _redeem(client, secret)
  member_id = session["member_id"]

  demote = client.patch(
    f"/api/projects/{project['id']}/members/{member_id}", headers=auth,
    json={"role": "viewer"},
  )
  assert demote.status_code == 200, demote.text
  assert client.get(
    f"/api/projects/{project['id']}/files", headers=guest,
  ).status_code == 200
  assert client.put(
    f"/api/projects/{project['id']}/file?path=blocked.txt", headers=guest,
    json={"content": "no", "expected_revision": None},
  ).status_code == 403

  promote = client.patch(
    f"/api/projects/{project['id']}/members/{member_id}", headers=auth,
    json={"role": "maintainer"},
  )
  assert promote.status_code == 200
  assert client.post(
    f"/api/projects/{project['id']}/git/init", headers=guest,
  ).status_code == 200

  revoke = client.delete(
    f"/api/projects/{project['id']}/members/{member_id}", headers=auth,
  )
  assert revoke.status_code == 204
  denied = client.get(f"/api/projects/{project['id']}", headers=guest)
  assert denied.status_code == 401
  assert "revoked" in denied.json()["detail"].lower()


def test_presence_and_invite_revocation_are_visible_to_the_owner(client, auth):
  project = _project(client, auth)
  pending, _pending_secret = _invite(
    client, auth, project["id"], role="viewer", name="Taylor",
  )
  accepted, accepted_secret = _invite(
    client, auth, project["id"], role="viewer", name="Morgan",
  )
  session, guest = _redeem(client, accepted_secret, "Morgan")

  assert client.post(
    f"/api/projects/{project['id']}/presence", headers=auth,
  ).status_code == 204
  assert client.post(
    f"/api/projects/{project['id']}/presence", headers=guest,
  ).status_code == 204
  collaboration = client.get(
    f"/api/projects/{project['id']}/collaboration", headers=auth,
  ).json()
  assert collaboration["role"] == "owner"
  assert {member["id"] for member in collaboration["members"]} == {
    "owner", session["member_id"],
  }
  assert all(member["online"] for member in collaboration["members"])
  assert [invite["id"] for invite in collaboration["invites"]] == [pending["id"]]

  assert client.delete(
    f"/api/projects/{project['id']}/invites/{pending['id']}", headers=auth,
  ).status_code == 204
  collaboration = client.get(
    f"/api/projects/{project['id']}/collaboration", headers=auth,
  ).json()
  assert collaboration["invites"] == []
  assert accepted["id"] != pending["id"]


def test_human_and_agent_work_claims_are_project_scoped(client, auth):
  project = _project(client, auth)
  _invite_row, secret = _invite(client, auth, project["id"], role="editor")
  session, guest = _redeem(client, secret, "Morgan")

  human = client.put(
    f"/api/projects/{project['id']}/work-claim", headers=guest,
    json={"path": "notes.md", "summary": "Editing notes.md"},
  )
  assert human.status_code == 200, human.text
  assert human.json()["actor_key"] == f"member:{session['member_id']}"

  chat = client.post(
    f"/api/projects/{project['id']}/chats", headers=auth,
    json={"title": "Builder", "recovery_request_id": "claim-builder"},
  ).json()
  agent = client.put(
    f"/api/projects/{project['id']}/work-claim", headers=auth,
    json={
      "chat_id": chat["id"], "path": "index.html",
      "summary": "Refreshing the homepage",
    },
  )
  assert agent.status_code == 200, agent.text
  assert agent.json()["actor_kind"] == "agent"

  visible = client.get(
    f"/api/projects/{project['id']}/work-claims", headers=guest,
  ).json()["claims"]
  assert {(row["actor_kind"], row["path"]) for row in visible} == {
    ("human", "notes.md"), ("agent", "index.html"),
  }

  outside = _project(client, auth, "Other project")
  assert client.get(
    f"/api/projects/{outside['id']}/work-claims", headers=guest,
  ).status_code == 404
  assert client.put(
    f"/api/projects/{project['id']}/work-claim", headers=guest,
    json={
      "chat_id": chat["id"], "summary": "Impersonate an agent",
    },
  ).status_code == 403


def test_expired_work_claim_can_be_refreshed(client, auth, db):
  project = _project(client, auth)
  created = client.put(
    f"/api/projects/{project['id']}/work-claim", headers=auth,
    json={"path": "notes.md", "summary": "Editing notes.md"},
  )
  assert created.status_code == 200, created.text

  claim = db.get(models.ProjectWorkClaim, created.json()["id"])
  claim.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()

  refreshed = client.put(
    f"/api/projects/{project['id']}/work-claim", headers=auth,
    json={"path": "done.md", "summary": "Editing done.md"},
  )
  assert refreshed.status_code == 200, refreshed.text
  assert refreshed.json()["id"] == created.json()["id"]
  assert refreshed.json()["path"] == "done.md"
