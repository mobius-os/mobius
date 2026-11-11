"""The owner grant: the owner's confirmation, minted only by the owner's own session and spent once."""
import pytest
from fastapi import HTTPException

from app import auth as auth_mod, models, owner_grants
from app.deps import Principal
from app.routes import contribution_reviews as routes
from app.timeutil import now_naive_utc
from tests.test_contribution_direct_merge import MOVED, NAME, NUMBER, SHA, desk  # noqa: F401  (fixture and constants)

MERGE = {"repo": NAME, "number": NUMBER, "head_sha": SHA}
POST = {"repo": NAME, "number": NUMBER, "result_id": "result-1", "digest": "a" * 64}


def confirm(desk, action, body, **scope):
  """The shell's two calls with the owner's own bearer: preview the sheet, then mint on the owner's tap."""
  ask = {"app_id": desk.app_id, "action": action, "scope": scope, "body": body}
  sheet = desk.client.post("/api/owner-grants/preview", json=ask, headers=desk.owner)
  assert sheet.status_code == 200, sheet.text
  return desk.client.post("/api/owner-grants", json={**ask, "digest": sheet.json()["digest"]}, headers=desk.owner)


def test_the_owner_confirms_once_and_the_posting_agent_spends_it_exactly_once(desk, db):
  owner = db.query(models.Owner).first()
  db.query(models.Chat).filter_by(id="desk-chat").update({"created_by_app_id": desk.app_id})
  db.commit()
  minted = confirm(desk, "respond_post", {**POST, "chat_id": "desk-chat"})
  assert minted.status_code == 200, minted.text
  assert minted.json()["granted"] is True and "id" not in minted.json()  # an app never holds a grant id
  agent = {"Authorization": "Bearer " + auth_mod.create_agent_token(
    "desk-chat", owner.username, owner.token_epoch, run_id="desk-run")}
  spend = lambda: desk.client.post("/api/owner-grants/consume", headers=agent,
                                   json={"app_id": desk.app_id, "action": "respond_post", "body": POST})
  assert spend().status_code == 200
  again = spend()
  assert again.status_code == 403 and again.json()["detail"]["code"] == "owner_grant_required"


def test_an_app_an_agent_and_a_helper_can_neither_preview_nor_mint(desk, db):
  owner = db.query(models.Owner).first()
  app_tokens = [auth_mod.create_app_token(app.id, owner.username, owner.token_epoch, app_nonce=app.token_nonce)
                for app in db.query(models.App).filter(models.App.slug.in_(["contribute", "other"]))]
  agent = auth_mod.create_agent_token("desk-chat", owner.username, owner.token_epoch, run_id="desk-run")
  helper = auth_mod.create_agent_token("desk-chat", owner.username, owner.token_epoch,
                                       delegation_id="child", delegation_chat="desk-chat")
  ask = {"app_id": desk.app_id, "action": "merge", "scope": {}, "body": MERGE}
  for token in [*app_tokens, agent, helper]:
    headers = {"Authorization": f"Bearer {token}"}
    assert desk.client.post("/api/owner-grants/preview", json=ask, headers=headers).status_code == 403
    assert desk.client.post("/api/owner-grants", json={**ask, "digest": "0" * 64}, headers=headers).status_code == 403
  assert db.query(models.OwnerGrant).count() == 0


def test_a_grant_covers_only_the_request_confirmed_and_only_until_it_expires(desk, db):
  owner = db.query(models.Owner).first()
  assert confirm(desk, "merge", MERGE).status_code == 200
  app = Principal(owner=owner, app_id=desk.app_id, scope="app")
  spend = lambda head: owner_grants.consume_owner_grant(
    db, app, desk.app_id, "merge", routes.DirectMerge(**{**MERGE, "head_sha": head}))
  with pytest.raises(HTTPException) as other_head:
    spend(MOVED)
  db.query(models.OwnerGrant).update({"expires_at": now_naive_utc().replace(year=2000)})
  db.commit()
  with pytest.raises(HTTPException) as expired:
    spend(SHA)
  assert other_head.value.status_code == expired.value.status_code == 403
  assert other_head.value.detail == expired.value.detail == owner_grants.OWNER_GRANT_REQUIRED  # no oracle
  # The owner's own session needs no grant for a route action: it is the session that mints them.
  owner_grants.consume_owner_grant(db, Principal(owner=owner, app_id=None), desk.app_id, "merge",
                                   routes.DirectMerge(**MERGE))
