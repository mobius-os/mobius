"""The owner grant: the owner's confirmation, minted only by the owner's own session and spent once."""
import json

from app import applied_app_runtime, auth as auth_mod, models, owner_grants
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
  # An agent can mint an app token for Contribute, but not a clean one: it carries the agent's chat.
  minted = desk.client.post("/api/auth/app-token", json={"app_id": desk.app_id},
                            headers={"Authorization": f"Bearer {agent}"}).json()["token"]
  ask = {"app_id": desk.app_id, "action": "merge", "scope": {}, "body": MERGE}
  for token in [*app_tokens, agent, helper, minted]:
    headers = {"Authorization": f"Bearer {token}"}
    assert desk.client.post("/api/owner-grants/preview", json=ask, headers=headers).status_code == 403
    assert desk.client.post("/api/owner-grants", json={**ask, "digest": "0" * 64}, headers=headers).status_code == 403
  assert db.query(models.OwnerGrant).count() == 0
  laundered = {"Authorization": f"Bearer {minted}"}
  merge = desk.client.post(f"/api/github/contributions/{desk.app_id}/pulls/merge", json=MERGE, headers=laundered)
  assert merge.status_code == 403  # the owner-tap route refuses it ...
  assert desk.client.get(f"/api/github/contributions/{desk.app_id}/review-presets",
                         headers=laundered).status_code == 200  # ... and only that route
  assert desk.github.calls == []


def declare_owner_confirm(db, slug):
  """What installing an app whose mobius.json says owner_confirm leaves behind: its accepted manifest."""
  app = db.query(models.App).filter_by(slug=slug).one()
  app.runtime_revision = "a" * 40
  accepted = applied_app_runtime.runtime_parent(app.id) / app.runtime_revision
  accepted.mkdir(parents=True)
  (accepted / "mobius.json").write_text(json.dumps({"id": slug, "owner_confirm": True}))
  db.commit()


def test_an_app_that_declares_owner_confirm_merges_only_with_a_grant_for_that_exact_request(desk, db):
  assert desk.post("contribute").status_code == 200  # no declaration: the app merges as it always did
  desk.github.pull.update(merged=False, state="open")
  declare_owner_confirm(db, "contribute")
  refused = desk.post("contribute")
  assert refused.status_code == 403 and refused.json()["detail"] == owner_grants.OWNER_GRANT_REQUIRED
  assert confirm(desk, "merge", MERGE).status_code == 200
  assert desk.post("contribute", {**MERGE, "head_sha": MOVED}).status_code == 403  # another head is not covered
  assert desk.post("contribute").status_code == 200  # the grant for the head the owner saw is still live
  assert desk.post("contribute").status_code == 403  # and one request spends it
  assert [merge["sha"] for merge in desk.github.merges] == [SHA, SHA]
