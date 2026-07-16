"""Authorization contract for the opaque three-frame chat embed."""

import hashlib
from datetime import timedelta

from app import auth, models
from app.timeutil import now_naive_utc


OPAQUE_HEADERS = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}


def _make_app(client, owner_token, name):
  owner_headers = {"Authorization": f"Bearer {owner_token}"}
  response = client.post(
    "/api/apps/",
    json={
      "name": name,
      "description": "embed test",
      "jsx_source": "export default () => null",
    },
    headers=owner_headers,
  )
  assert response.status_code == 201, response.text
  app_id = response.json()["id"]
  response = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers=owner_headers,
  )
  assert response.status_code == 200, response.text
  return app_id, response.json()["token"]


def _make_chat(client, app_token, title="Embedded chat"):
  response = client.post(
    "/api/app-chats",
    json={"title": title},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 201, response.text
  return response.json()["id"]


def _mint(client, app_token, chat_id, instance_id="embed-instance-0001"):
  response = client.post(
    f"/api/app-chats/{chat_id}/embed-capability",
    json={"instance_id": instance_id},
    headers={
      "Authorization": f"Bearer {app_token}",
      **OPAQUE_HEADERS,
    },
  )
  assert response.status_code == 200, response.text
  return response.json()["capability"]


def _exchange(client, capability, instance_id="embed-instance-0001"):
  return client.post(
    "/api/app-chat-embeds/session",
    json={"instance_id": instance_id},
    headers={"Authorization": f"Bearer {capability}", **OPAQUE_HEADERS},
  )


def _session(client, owner_token, name="embed-app", instance="embed-instance-0001"):
  app_id, app_token = _make_app(client, owner_token, name)
  chat_id = _make_chat(client, app_token)
  capability = _mint(client, app_token, chat_id, instance)
  response = _exchange(client, capability, instance)
  assert response.status_code == 200, response.text
  data = response.json()
  return app_id, app_token, chat_id, capability, data


def _embed_headers(session):
  return {
    "Authorization": f"Bearer {session['token']}",
    "X-Mobius-Embed-Instance": session["instance_id"],
    **OPAQUE_HEADERS,
  }


def test_bootstrap_is_one_use_and_session_is_exact_chat(client, owner_token):
  _, _, chat_id, capability, session = _session(client, owner_token)
  assert session["chat_id"] == chat_id
  assert session["role"] == "participant"
  assert "chat:read" in session["operations"]
  assert "chat:uploads" in session["operations"]
  assert isinstance(session["theme"]["css"], str)
  assert session["theme"]["css"]
  assert session["theme"]["mode"] in {"light", "dark"}

  replay = _exchange(client, capability, session["instance_id"])
  assert replay.status_code == 401

  detail = client.get(f"/api/chats/{chat_id}", headers=_embed_headers(session))
  assert detail.status_code == 200, detail.text
  assert detail.json()["session_id"] is None

  wrong_instance = dict(_embed_headers(session))
  wrong_instance["X-Mobius-Embed-Instance"] = "embed-instance-wrong"
  assert client.get(f"/api/chats/{chat_id}", headers=wrong_instance).status_code == 401


def test_app_cannot_mint_for_another_apps_chat(client, owner_token):
  _, app_a_token = _make_app(client, owner_token, "app-a")
  _, app_b_token = _make_app(client, owner_token, "app-b")
  chat_b = _make_chat(client, app_b_token)
  response = client.post(
    f"/api/app-chats/{chat_b}/embed-capability",
    json={"instance_id": "embed-instance-foreign"},
    headers={"Authorization": f"Bearer {app_a_token}", **OPAQUE_HEADERS},
  )
  assert response.status_code == 403


def test_session_cannot_read_another_chat_even_in_same_app(client, owner_token):
  _, app_token, own_chat, _, session = _session(client, owner_token)
  other_chat = _make_chat(client, app_token, "Other chat")
  headers = _embed_headers(session)
  assert client.get(f"/api/chats/{own_chat}", headers=headers).status_code == 200
  assert client.get(f"/api/chats/{other_chat}", headers=headers).status_code == 403


def test_expired_revoked_and_wrong_instance_bootstraps_fail(
  client, owner_token, db,
):
  _, app_token = _make_app(client, owner_token, "expiry-app")
  chat_id = _make_chat(client, app_token)

  capability = _mint(client, app_token, chat_id, "embed-instance-expired")
  grant = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.token_hash
    == hashlib.sha256(capability.encode()).hexdigest(),
  ).one()
  grant.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  assert _exchange(client, capability, "embed-instance-expired").status_code == 401

  capability = _mint(client, app_token, chat_id, "embed-instance-revoked")
  revoke = client.delete(
    f"/api/app-chats/{chat_id}/embed-sessions/embed-instance-revoked",
    headers={"Authorization": f"Bearer {app_token}", **OPAQUE_HEADERS},
  )
  assert revoke.status_code == 200
  assert _exchange(client, capability, "embed-instance-revoked").status_code == 401

  capability = _mint(client, app_token, chat_id, "embed-instance-correct")
  assert _exchange(client, capability, "embed-instance-wrong").status_code == 401


def test_owner_epoch_app_nonce_and_live_session_revocation(client, owner_token, db):
  app_id, app_token, chat_id, _, session = _session(client, owner_token)
  headers = _embed_headers(session)

  app = db.query(models.App).filter(models.App.id == app_id).one()
  app.token_nonce = "rotated-installation-nonce"
  db.commit()
  assert client.get(f"/api/chats/{chat_id}", headers=headers).status_code == 401

  # Restore the install binding, then prove the owner epoch independently.
  payload = auth.decode_access_token(session["token"])
  app.token_nonce = payload["app_nonce"]
  owner = db.query(models.Owner).one()
  owner.token_epoch += 1
  db.commit()
  assert client.get(f"/api/chats/{chat_id}", headers=headers).status_code == 401


def test_refresh_revokes_previous_session_and_destroy_revokes_current(
  client, owner_token,
):
  _, app_token, chat_id, _, first = _session(client, owner_token)
  first_headers = _embed_headers(first)

  second_capability = _mint(
    client, app_token, chat_id, first["instance_id"],
  )
  second_response = _exchange(client, second_capability, first["instance_id"])
  assert second_response.status_code == 200, second_response.text
  second = second_response.json()
  assert client.get(f"/api/chats/{chat_id}", headers=first_headers).status_code == 401
  assert client.get(
    f"/api/chats/{chat_id}", headers=_embed_headers(second),
  ).status_code == 200

  revoke = client.delete(
    f"/api/app-chats/{chat_id}/embed-sessions/{first['instance_id']}",
    headers={"Authorization": f"Bearer {app_token}", **OPAQUE_HEADERS},
  )
  assert revoke.status_code == 200
  assert client.get(
    f"/api/chats/{chat_id}", headers=_embed_headers(second),
  ).status_code == 401


def test_late_older_exchange_cannot_replace_a_newer_session(client, owner_token):
  _, app_token = _make_app(client, owner_token, "ordered-refresh-app")
  chat_id = _make_chat(client, app_token)
  instance = "embed-instance-ordered-refresh"
  older = _mint(client, app_token, chat_id, instance)
  newer = _mint(client, app_token, chat_id, instance)

  newest_session_response = _exchange(client, newer, instance)
  assert newest_session_response.status_code == 200
  newest_session = newest_session_response.json()
  assert _exchange(client, older, instance).status_code == 401
  assert client.get(
    f"/api/chats/{chat_id}", headers=_embed_headers(newest_session),
  ).status_code == 200


def test_only_consumed_replacement_permanently_supersedes_old_session(
  client, owner_token, db,
):
  from app.deps import chat_embed_session_is_active

  _, app_token, chat_id, _, older_session = _session(
    client, owner_token, name="permanent-order-app",
    instance="embed-instance-permanent-order",
  )
  older_headers = _embed_headers(older_session)
  upload = client.post(
    f"/api/chats/{chat_id}/uploads",
    files={"files": ("order.txt", b"ordered", "text/plain")},
    headers=older_headers,
  )
  assert upload.status_code == 200
  older_media = client.post(
    f"/api/chats/{chat_id}/media-token", headers=older_headers,
  ).json()["token"]

  # Minting is not handoff: failure-safe refresh keeps the old session fully
  # authoritative until the replacement bootstrap is actually consumed.
  replacement = _mint(
    client, app_token, chat_id, older_session["instance_id"],
  )
  assert client.get(f"/api/chats/{chat_id}", headers=older_headers).status_code == 200
  assert chat_embed_session_is_active(
    auth.decode_access_token(older_session["token"])["embed_session"],
  )
  assert client.get(
    f"/api/chats/{chat_id}/uploads/order.txt?token={older_media}",
  ).status_code == 200

  newer_response = _exchange(
    client, replacement, older_session["instance_id"],
  )
  assert newer_response.status_code == 200

  grants = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.chat_id == chat_id,
    models.ChatEmbedGrant.instance_id == older_session["instance_id"],
  ).order_by(models.ChatEmbedGrant.id).all()
  older_grant, newer_grant = grants[-2:]
  # Simulate the exact cleanup miss from an overlapping exchange, then destroy
  # and expire the replacement. Creation order remains the authority invariant:
  # the old grant cannot resurrect on API, media, or SSE liveness paths.
  older_grant.revoked_at = None
  newer_grant.revoked_at = now_naive_utc()
  newer_grant.session_expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()

  assert client.get(f"/api/chats/{chat_id}", headers=older_headers).status_code == 401
  assert not chat_embed_session_is_active(older_grant.session_id)
  assert client.get(
    f"/api/chats/{chat_id}/uploads/order.txt?token={older_media}",
  ).status_code == 401


def test_operations_are_bound_to_server_grant(client, owner_token, db):
  _, _, chat_id, _, session = _session(client, owner_token)
  grant = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.session_id.isnot(None),
  ).one()
  grant.operations_json = ["chat:read"]
  db.commit()
  response = client.get(f"/api/chats/{chat_id}", headers=_embed_headers(session))
  assert response.status_code == 401


def test_scoped_upload_and_media_token_die_with_session(
  client, owner_token, db,
):
  app_id, app_token, chat_id, _, session = _session(client, owner_token)
  headers = _embed_headers(session)
  uploaded = client.post(
    f"/api/chats/{chat_id}/uploads",
    files={"files": ("note.txt", b"hello", "text/plain")},
    headers=headers,
  )
  assert uploaded.status_code == 200, uploaded.text
  assert client.get(f"/api/chats/{chat_id}/uploads", headers=headers).status_code == 200

  media = client.post(f"/api/chats/{chat_id}/media-token", headers=headers)
  assert media.status_code == 200, media.text
  media_token = media.json()["token"]
  served = client.get(
    f"/api/chats/{chat_id}/uploads/note.txt?token={media_token}",
  )
  assert served.status_code == 200
  assert client.get(
    f"/api/chats/{chat_id}/uploads/note.txt",
    headers={"Authorization": f"Bearer {media_token}"},
  ).status_code == 200

  other_chat = _make_chat(client, app_token, "Other media chat")
  for request in (
    client.get(
      f"/api/chats/{other_chat}/uploads/note.txt?token={media_token}",
    ),
    client.get(
      f"/api/chats/{other_chat}/uploads/note.txt",
      headers={"Authorization": f"Bearer {media_token}"},
    ),
  ):
    assert request.status_code == 403

  media_headers = {"Authorization": f"Bearer {media_token}"}
  assert [response.status_code for response in (
    client.get("/api/chats", headers=media_headers),
    client.get("/api/apps/", headers=media_headers),
    client.get(f"/api/storage/apps/{app_id}/private.json", headers=media_headers),
    client.get(
      "/api/admin/activity?since=2020-01-01T00:00:00Z",
      headers=media_headers,
    ),
  )] == [403, 403, 403, 403]

  # Media URLs carry a narrower bearer, but revocation still follows the live
  # app installation nonce rather than lingering until their 15-minute expiry.
  app = db.query(models.App).filter(models.App.id == app_id).one()
  original_nonce = app.token_nonce
  app.token_nonce = "rotated-media-installation"
  db.commit()
  assert client.get(
    f"/api/chats/{chat_id}/uploads/note.txt?token={media_token}",
  ).status_code == 401
  app.token_nonce = original_nonce
  db.commit()

  client.delete(
    f"/api/app-chats/{chat_id}/embed-sessions/{session['instance_id']}",
    headers={"Authorization": f"Bearer {app_token}", **OPAQUE_HEADERS},
  )
  assert client.get(
    f"/api/chats/{chat_id}/uploads/note.txt?token={media_token}",
  ).status_code == 401


def test_hostile_caller_without_server_grant_stays_unauthorized(client):
  response = _exchange(
    client,
    "forged-browser-handshake-is-not-authorization",
    "hostile-frame-instance",
  )
  assert response.status_code == 401


def test_embed_session_is_denied_by_generic_owner_app_surfaces(
  client, owner_token,
):
  """The generic principal must fail closed outside the ChatView allowlist."""
  app_id, _, chat_id, _, session = _session(client, owner_token)
  headers = _embed_headers(session)

  checks = [
    client.get(f"/api/storage/apps/{app_id}/private.json", headers=headers),
    client.get(f"/api/apps/{app_id}/update-check", headers=headers),
    client.get(
      f"/api/github/contributions/{app_id}/review-status", headers=headers,
    ),
    client.post(
      "/api/notifications/send",
      json={"title": "must not send"},
      headers=headers,
    ),
    client.get("/api/notifications", headers=headers),
    client.get("/api/chat-logs", headers=headers),
    client.get(
      "/api/admin/activity?since=2020-01-01T00:00:00Z", headers=headers,
    ),
    client.get("/api/app-chats", headers=headers),
    client.get(f"/api/chats/{chat_id}/agent-context", headers=headers),
  ]
  assert [response.status_code for response in checks] == [403] * len(checks)
