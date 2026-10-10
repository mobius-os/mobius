"""Shared browser identity stays revocable across request and token boundaries."""
import pytest
from app import auth as tokens, models
from app.config import get_settings
from app.database import SessionLocal
from tests.browser_access_fixtures import link_grant

ROOT = "/api/connect/browser-access"


@pytest.fixture
def https(client, monkeypatch):
  from app.routes.browser_access import _limiter
  _limiter.reset()
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  return client


def invite(client, label="Alice"):
  """Redeem a link issued before link invitations were retired."""
  with SessionLocal() as db:
    grant, secret = link_grant(db, db.query(models.Owner).one(), label)
    grant_id = grant.id
  response = client.post(ROOT + "/session/redeem", json={"invite": secret})
  assert response.status_code == 200, response.text
  # Only signing in writes the refresh cookie; its lifetime is fixed and long.
  cookie = response.headers["set-cookie"].lower()
  assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
  assert "max-age=34560000" in cookie
  assert response.json()["grant"]["id"] == grant_id
  return grant_id, response.json()["access_token"], secret


def test_invite_session_and_descendant_app_credentials_revoke_together(https, auth, db, monkeypatch):
  grant_id, token, invitation = invite(https)
  guest = {"Authorization": "Bearer " + token}
  assert https.get("/api/chats", headers=guest).status_code == 200
  renewed = https.post(ROOT + "/session")
  assert renewed.status_code == 200
  assert renewed.headers["cache-control"] == "no-store"
  # A late renewal response can never overwrite a newer sign-in's cookie.
  assert "set-cookie" not in renewed.headers
  app = models.App(name="Example", slug="example", source_dir=str(__import__("pathlib").Path(get_settings().data_dir) / "example"))
  db.add(app); db.commit()
  child = https.post("/api/auth/app-token", json={"app_id": app.id}, headers=guest)
  assert child.status_code == 200, child.text
  claims = tokens.decode_access_token(child.json()["token"])
  assert claims["browser_grant"] == grant_id
  # Pure route integration; running-process cancellation has its own tests.
  import app.chat
  async def stopped(*args, **kwargs): return None
  monkeypatch.setattr(app.chat, "stop_browser_grant_runs", stopped, raising=False)
  assert https.delete(ROOT + "/" + grant_id, headers=auth).status_code == 204
  assert https.get("/api/chats", headers=guest).status_code == 401
  assert https.get("/api/apps/", headers={"Authorization": "Bearer " + child.json()["token"]}).status_code == 401
  assert https.post(ROOT + "/session").status_code == 401
  assert https.get("/api/chats", headers=auth).status_code == 200


def test_guest_cannot_create_grants_or_launder_install_or_job_credentials(https, auth):
  _, token, _ = invite(https)
  guest = {"Authorization": "Bearer " + token}
  assert https.post(ROOT + "/accounts", json={"recipient_handle": "another-person"}, headers=guest).status_code == 403
  assert https.get(ROOT + "/shared", headers=guest).status_code == 403
  assert https.post(ROOT + "/shared/respond", headers=guest, json={
    "origin": "https://other.example", "grant_id": "g" * 32, "action": "accept",
  }).status_code == 403
  assert https.post("/api/auth/install-pass", json={"slug": "example"}, headers=guest).status_code == 403
  assert https.post("/api/auth/app-job-token", json={"app_id": 1}, headers=guest).status_code == 403
  assert https.post("/api/admin/sign-out-everywhere", headers=guest).status_code == 403


def test_logout_ends_only_that_browser_not_other_recipient_or_owner(https, auth):
  grant_a, token_a, _ = invite(https, "Alice")
  assert https.post(ROOT + "/session/logout", json={"grant_id": grant_a}).status_code == 204
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token_a}).status_code == 401
  grant_b, token_b, _ = invite(https, "Bob")
  assert grant_a != grant_b
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token_b}).status_code == 200
  assert https.get("/api/chats", headers=auth).status_code == 200


def test_session_cookie_cannot_be_used_from_sibling_origin(https, auth):
  invite(https)
  for origin in ["https://evil.example", "https://sub.shared.example", "null"]:
    assert https.post(ROOT + "/session", headers={"Origin": origin}).status_code == 403


def test_invite_replay_fails_and_plain_http_cannot_share(https, auth, monkeypatch):
  _, _, secret = invite(https)
  assert https.post(ROOT + "/session/redeem", json={"invite": secret}).status_code == 401
  monkeypatch.setattr(get_settings(), "frontend_origin", "http://shared.example")
  refused = https.post(ROOT + "/accounts", json={"recipient_handle": "sam-person"}, headers=auth)
  assert refused.status_code == 409 and "HTTPS" in refused.json()["detail"]


def test_claim_pair_must_be_valid_and_cannot_omit_grant(https, auth, db):
  owner = db.query(models.Owner).one()
  for claims in [{"browser_grant_epoch": 0}, {"browser_grant": "missing"}, {"browser_session": "missing"}]:
    token = tokens.create_access_token({"sub": owner.username, **claims}, token_epoch=owner.token_epoch)
    assert https.get("/api/chats", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_logout_revokes_minted_frame_and_media_tokens(https, auth, db):
  _, token, _ = invite(https)
  principal = __import__('app.deps', fromlist=['get_principal']).get_principal(token, db)
  app = models.App(name="Example", slug="example", source_dir=get_settings().data_dir + "/example")
  db.add(app); db.commit()
  frame = https.post("/api/auth/app-token", json={"app_id": app.id}, headers={"Authorization": "Bearer " + token}).json()["token"]
  media = tokens.create_media_token("demo", principal.owner.username, principal.owner.token_epoch,
    browser=principal.browser)
  assert tokens.decode_access_token(frame)["browser_session"] == principal.browser.session_id
  assert https.post(ROOT + "/session/logout", json={"grant_id": principal.browser_grant_id}).status_code == 204
  from app.deps import _resolve_owner
  from fastapi import HTTPException
  for child in [frame, media]:
    with pytest.raises(HTTPException) as error:
      _resolve_owner(child, db)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_open_browser_event_stream_stops_before_next_revoked_event(db):
  from app.browser_access import BrowserLineage, redeem_invitation, revoke_grant
  from app.deps import Principal, revocable_browser_stream
  owner = models.Owner(username="stream-owner", hashed_password="unused")
  db.add(owner); db.commit()
  grant, secret = link_grant(db, owner, "Guest")
  _, session, _, _ = redeem_invitation(db, secret)
  # The request's ORM session commits and closes before the stream runs.
  request_db = SessionLocal()
  request_owner = request_db.get(models.Owner, owner.id)
  principal = Principal(owner=request_owner, app_id=None,
    browser=BrowserLineage(grant.id, session.id))
  closed = []
  async def events():
    try:
      yield "before"
      revoke_grant(db, grant.id, owner.id)
      yield "must not escape"
    finally:
      closed.append(True)
  stream = revocable_browser_stream(events(), principal)
  request_db.commit()
  request_db.close()
  chunks = [chunk async for chunk in stream]
  assert chunks == ["before"]
  assert closed == [True]


def _guest_stream_fixture(db, name):
  from app.browser_access import BrowserLineage, redeem_invitation
  from app.deps import Principal
  owner = models.Owner(username=name, hashed_password="unused")
  db.add(owner); db.commit()
  grant, invitation = link_grant(db, owner, "Guest")
  secret, session, _, _ = redeem_invitation(db, invitation)
  principal = Principal(owner=owner, app_id=None, browser=BrowserLineage(grant.id, session.id))
  return owner, grant, secret, principal


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["logout", "revoke"])
async def test_idle_guest_stream_closes_promptly_when_access_ends(db, ending):
  import asyncio
  from app.browser_access import logout_session, revoke_grant
  from app.deps import revocable_browser_stream
  owner, grant, secret, principal = _guest_stream_fixture(db, "idle-" + ending)
  closed = []
  async def events():
    try:
      yield "first"
      await asyncio.Event().wait()  # nothing more to send for a long time
    finally:
      closed.append(True)
  stream = revocable_browser_stream(events(), principal)
  assert await anext(stream) == "first"
  waiting = asyncio.create_task(anext(stream))
  await asyncio.sleep(0.05)
  assert not waiting.done()
  if ending == "logout":
    logout_session(db, secret)
  else:
    revoke_grant(db, grant.id, owner.id)
  with pytest.raises(StopAsyncIteration):
    await asyncio.wait_for(waiting, timeout=2)
  assert closed == [True]


@pytest.mark.asyncio
async def test_busy_guest_stream_does_not_query_per_event(db, monkeypatch):
  import threading
  from app import browser_access
  from app.deps import revocable_browser_stream
  _, _, _, principal = _guest_stream_fixture(db, "busy-owner")
  checks = []
  real = browser_access.is_live
  def counted(*args, **kwargs):
    checks.append(threading.current_thread() is threading.main_thread())
    return real(*args, **kwargs)
  monkeypatch.setattr(browser_access, "is_live", counted)
  async def events():
    for index in range(200):
      yield index
  chunks = [chunk async for chunk in revocable_browser_stream(events(), principal)]
  assert chunks == list(range(200))
  # One check when the stream opens, off the event loop; none per event.
  assert checks == [False]


def test_private_service_bearer_retains_browser_attribution(https, auth, db):
  from app.app_services import service_environment
  from app.deps import _resolve_owner
  from app.browser_access import BrowserLineage, revoke_grant
  from fastapi import HTTPException
  grant, token, _ = invite(https)
  claims = tokens.decode_access_token(token)
  assert "browser_grant_epoch" not in claims
  owner = db.query(models.Owner).one()
  app = models.App(id=42, name="Example", slug="example", token_nonce="nonce", source_dir=get_settings().data_dir)
  environment = service_environment(app, owner, {"access": "self"}, public=False,
    browser=BrowserLineage(grant, claims["browser_session"]))
  issued = tokens.decode_access_token(environment["APP_TOKEN"])
  assert issued["browser_grant"] == grant
  assert issued["browser_session"] == claims["browser_session"]
  revoke_grant(db, grant, owner.id)
  with pytest.raises(HTTPException) as error:
    _resolve_owner(environment["APP_TOKEN"], db)
  assert error.value.status_code == 401


def test_switching_person_retires_only_previous_cookie_session(https, auth):
  grant_a, token_a, _ = invite(https, "Alice")
  grant_b, token_b, _ = invite(https, "Bob")
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token_a}).status_code == 401
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token_b}).status_code == 200
  # An old tab must not sign the new recipient out or delete their cookie.
  refused = https.post(ROOT + "/session/logout", json={"grant_id": grant_a})
  assert refused.status_code == 409
  assert "set-cookie" not in refused.headers
  assert https.post(ROOT + "/session").json()["grant"]["id"] == grant_b
  assert https.get("/api/chats", headers=auth).status_code == 200


def test_retired_link_routes_refuse_cleanly_and_existing_link_grants_stay_listed(https, auth):
  grant, token, _ = invite(https)
  # An older Connect app may still call these; they answer, never crash.
  for path in [ROOT, ROOT + f"/{grant}/invitation"]:
    refused = https.post(path, json={"label": "Bob"}, headers=auth)
    assert refused.status_code == 404 and refused.headers["content-type"] == "application/json"
  listed = https.get(ROOT, headers=auth).json()["grants"]
  assert [(row["id"], row["kind"], row["status"]) for row in listed] == [(grant, "invitation", "active")]
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_guest_cannot_start_installation_setup_or_read_owner_screen(https, auth):
  _, token, _ = invite(https)
  guest = {"Authorization": "Bearer " + token}
  assert https.post("/api/setup/rerun", headers=guest).status_code == 403
  assert https.get("/api/screen-control/sessions/fake/events", headers=guest).status_code == 403


def test_embedded_chat_session_inherits_guest_revocation(https, auth, db):
  from test_app_fixtures import create_local_app
  from app.browser_access import revoke_grant
  app = create_local_app(https, auth, name="Guest embed", description="test")
  grant, token, _ = invite(https)
  child = https.post("/api/auth/app-token", json={"app_id": app["id"]},
                     headers={"Authorization": "Bearer " + token}).json()["token"]
  child_headers = {"Authorization": "Bearer " + child, "Origin": "null"}
  chat = https.post("/api/app-chats", json={"title": "Guest embed"}, headers=child_headers).json()["id"]
  frame = "shared-embed-instance-1"
  bootstrap = https.post(f"/api/app-chats/{chat}/embed-capability", json={"instance_id": frame}, headers=child_headers).json()["capability"]
  response = https.post("/api/app-chat-embeds/session", json={"instance_id": frame}, headers={"Authorization": "Bearer " + bootstrap, "Origin": "null"})
  assert response.status_code == 200, response.text
  session = response.json()
  assert tokens.decode_access_token(session["token"])["browser_grant"] == grant
  embed_headers = {"Authorization": "Bearer " + session["token"], "X-Mobius-Embed-Instance": frame, "Origin": "null"}
  assert https.get(f"/api/chats/{chat}", headers=embed_headers).status_code == 200
  owner = db.query(models.Owner).one()
  revoke_grant(db, grant, owner.id)
  assert https.get(f"/api/chats/{chat}", headers=embed_headers).status_code == 401


@pytest.mark.asyncio
async def test_revocation_cancels_only_attributed_service_queue(db, monkeypatch):
  import asyncio
  from types import SimpleNamespace
  from app import app_services
  from app.browser_access import revoke_grant
  owner = models.Owner(username="service-owner", hashed_password="unused")
  db.add(owner); db.commit()
  grant, _ = link_grant(db, owner, "Alice")
  queued = asyncio.Event()
  class Gate(asyncio.Semaphore):
    async def acquire(self):
      queued.set()
      return await super().acquire()
  monkeypatch.setattr(app_services, "_app_slots", {(1, "private"): Gate(0)})
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **kw: {})
  released = []
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: released.append(True)))
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), owner,
    {"actor": {"browser_grant_id": grant.id}}))
  await asyncio.wait_for(queued.wait(), 1)
  revoke_grant(db, grant.id, owner.id)
  await app_services.cancel_browser_grant_calls(grant.id)
  assert task.cancelled()
  assert released == [True]
  assert grant.id not in app_services._browser_calls


def test_invalid_invitation_attempts_are_rate_limited(https):
  for _ in range(10):
    assert https.post(ROOT + "/session/redeem", json={"invite": "invalid-but-long-enough"}).status_code == 401
  assert https.post(ROOT + "/session/redeem", json={"invite": "invalid-but-long-enough"}).status_code == 429


def test_revoke_reports_unconfirmed_work_without_claiming_stop(https, auth, monkeypatch):
  import app.chat
  from app.routes import connect
  from fastapi import HTTPException
  grant, token, _ = invite(https)
  pending = [{"host_id": "demo", "request_id": "work", "remote_confirmed": False}]
  monkeypatch.setattr(connect, "cancel_browser_grant_commands", lambda value: pending if value == grant else [])
  async def incomplete(*args, **kwargs):
    raise HTTPException(503, {"code": "browser_grant_stop_incomplete", "chat_ids": ["still-stopping"]})
  monkeypatch.setattr(app.chat, "stop_browser_grant_runs", incomplete)
  response = https.delete(ROOT + "/" + grant, headers=auth)
  assert response.status_code == 202
  assert response.json() == {"revoked": True, "pending_commands": pending, "pending_chat_ids": ["still-stopping"]}
  assert https.get("/api/chats", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_browser_guest_cannot_create_independent_shared_membership(https, auth):
  _, token, _ = invite(https)
  guest = {"Authorization": "Bearer " + token}
  base = "/api/shared-apps/missing"
  assert https.post(base + "/invites", json={"invitee_name": "Other", "role": "editor"}, headers=guest).status_code == 403
  assert https.patch(base + "/members/other", json={"role": "editor"}, headers=guest).status_code == 403
  assert https.delete(base + "/members/other", headers=guest).status_code == 403
  assert https.delete(base + "/invites/other", headers=guest).status_code == 403


def test_existing_shared_app_owner_keeps_resource_confined_administration(db):
  from app.deps import SharedAppPrincipal
  from app.routes.shared_apps import _require_membership_administration
  owner = models.Owner(username="install-owner", hashed_password="unused")
  # Distinct existing sharing feature, not a browser guest; do not erase it.
  _require_membership_administration(SharedAppPrincipal(owner=owner, role="owner", member_id="member", instance_id="one-app"))


@pytest.mark.asyncio
async def test_revoked_list_derives_pending_stop_after_reload_without_cancelling(https, auth, db):
  from app.browser_access import revoke_grant
  from app.routes import connect
  grant_id, _, _ = invite(https)
  owner = db.query(models.Owner).one()
  host_id = connect._new_id()
  connect._save_host({"id": host_id, "name": "Fixture", "active_commands": []})
  command = connect._ActiveCommand("a" * 16, 60, cmd="true", browser_grant_id=grant_id)
  command.state = "canceling"
  connect._host_commands(host_id)[command.request_id] = command
  connect._persist_commands(host_id)
  revoke_grant(db, grant_id, owner.id)
  connect._commands.clear()  # A server restart discards the process-local map.
  channel = connect._Channel()
  connect._channels[host_id] = channel
  try:
    grants = https.get(ROOT, headers=auth).json()["grants"]
    assert grants[0]["status"] == "revoked"
    assert grants[0]["stop_pending"] is True
    assert channel.queue.empty()  # A read must not initiate another remote action.
    connect._host_commands(host_id).clear()
    connect._persist_commands(host_id)
    assert https.get(ROOT, headers=auth).json()["grants"][0]["stop_pending"] is False
  finally:
    connect._channels.pop(host_id, None)
    connect._commands.pop(host_id, None)


@pytest.mark.parametrize("route, body", [
  ("/api/apps/999/conflict-resolver-chat", {}),
  ("/api/apps/conflict-resolver-batch", {"app_ids": [999]}),
])
def test_guest_cannot_launch_clean_owner_conflict_resolver(https, auth, route, body):
  _, token, _ = invite(https)
  response = https.post(route, json=body,
                        headers={"Authorization": "Bearer " + token})
  assert response.status_code == 403
  assert "installation owner" in response.json()["detail"]
