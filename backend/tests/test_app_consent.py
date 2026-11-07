"""First-open consent: derived rows, the dormant-until-accepted gate, and the
human-only acceptance call."""

import json

import pytest
from sqlalchemy import create_engine, text

from app import app_consent, auth, models
from app.schema_migrations import _add_app_accepted_consent
from tests.test_apps_install import _stub_resolver_run_chat, bypass_url_validation  # noqa: F401

STORE_URL = "https://example.test/store-app/mobius.json"


def _contract(**data):
  base = {
    "chat_logs": {"requested": "none", "effective": "none", "redaction": "none"},
    "filesystem_api": False, "shared_memory": "none", "cross_app_access": "none",
    "share_with_apps": "none", "manage_apps": False, "manage_skills": False,
    "github_access": False, "github_connect": False, "connections_manage": False,
    "connect_manage": False, "identity_manage": False, "railway_manage": False,
  }
  base.update(data)
  return {"schema": 6, "agent": {"skills": [], "tools": [], "embeds_agent": False,
          "system_prompt": None}, "data": base, "runtime": {}, "public": {"network": []}}


def _app(db, slug, *, contract, store=True, accepted=None):
  row = models.App(
    name=slug.title(), description="", jsx_source="", compiled_path="",
    source_dir=f"/tmp/{slug}", slug=slug,
    manifest_url=STORE_URL + slug if store else None,
    capability_contract=contract, accepted_consent=accepted,
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def _owner_headers(client, owner_token):
  return {"Authorization": f"Bearer {owner_token}"}


def test_rows_are_derived_and_critical_is_platform_defined():
  contract = _contract(
    chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"},
    manage_apps=True,
  )
  contract["agent"]["embeds_agent"] = True
  rows = {r["id"]: r for r in app_consent.rows_from_contract(contract)}
  assert rows["chat_log_access"]["critical"] is False
  assert rows["manage_apps"]["critical"] is True
  assert rows["agent_start"]["critical"] is True
  # Most serious first.
  ordered = [r["critical"] for r in app_consent.rows_from_contract(contract)]
  assert ordered == sorted(ordered, reverse=True)


def test_runtime_capability_wording_comes_from_the_contract():
  contract = _contract()
  contract["runtime"] = {"media.microphone.capture": {"title": "Record audio", "description": "Use the microphone."}}
  (row,) = app_consent.rows_from_contract(contract)
  assert (row["title"], row["critical"]) == ("Record audio", False)


def test_unaccepted_store_app_is_dormant_until_accepted(client, owner_token, db):
  headers = _owner_headers(client, owner_token)
  app = _app(db, "newapp", contract=_contract(github_access=True))
  denied = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers)
  assert denied.status_code == 409
  assert denied.json()["detail"]["code"] == "consent_required"
  job = client.post("/api/auth/app-job-token", json={"app_id": app.id}, headers=headers)
  assert job.status_code == 409

  view = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()
  assert view["status"] == "required" and view["is_update"] is False
  assert [r["id"] for r in view["rows"]] == ["github_access"]
  assert view["baseline"]

  wrong = client.post(f"/api/apps/{app.id}/consent", json={"digest": "nope"}, headers=headers)
  assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "consent_changed"

  ok = client.post(f"/api/apps/{app.id}/consent", json={"digest": view["digest"]}, headers=headers)
  assert ok.status_code == 200 and ok.json()["status"] == "accepted"
  allowed = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers)
  assert allowed.status_code == 200
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "accepted"


def test_agent_tokens_cannot_accept(client, owner_token, db, chat):
  app = _app(db, "agentcheck", contract=_contract(github_access=True))
  headers = _owner_headers(client, owner_token)
  digest = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]
  agent = auth.create_agent_token(chat.id, "test", 0)
  response = client.post(
    f"/api/apps/{app.id}/consent", json={"digest": digest},
    headers={"Authorization": f"Bearer {agent}"},
  )
  assert response.status_code == 403
  assert "Agent tokens cannot supply owner input" in response.text
  db.refresh(app)
  assert app.accepted_consent is None


def test_local_apps_are_not_gated(client, owner_token, db):
  app = _app(db, "localapp", contract=_contract(manage_apps=True), store=False)
  headers = _owner_headers(client, owner_token)
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "not_applicable"
  assert client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).status_code == 200


def test_update_asks_only_when_it_needs_more(db):
  old = _contract(github_access=True)
  app = _app(db, "updater", contract=old, accepted=app_consent.platform_acceptance(old))
  assert app_consent.status(app) == "accepted"
  # Same or narrower request: nothing to ask.
  app.capability_contract = _contract()
  assert app_consent.status(app) == "accepted"
  # A wider request asks again, and shows only the new row.
  app.capability_contract = _contract(github_access=True, manage_apps=True)
  assert app_consent.status(app) == "required"
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["manage_apps"]
  # A higher level of the same row is new.
  chat = _contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"})
  app.capability_contract = chat
  app.accepted_consent = app_consent.platform_acceptance(chat)
  app.capability_contract = _contract(chat_logs={"requested": "summary_with_deleted", "effective": "summary_with_deleted", "redaction": "structural"})
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["chat_log_access"]


def test_notes_name_other_holders_and_pairing(db):
  _app(db, "reader", contract=_contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"}, cross_app_access="read"),
       accepted=app_consent.platform_acceptance(_contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"}, cross_app_access="read")))
  _app(db, "sharer", contract=_contract(share_with_apps="read"),
       accepted=app_consent.platform_acceptance(_contract(share_with_apps="read")))
  newcomer = _app(db, "newcomer", contract=_contract(
    chat_logs={"requested": "summary_with_deleted", "effective": "summary_with_deleted", "redaction": "structural"},
    cross_app_access="read", share_with_apps="read"))
  notes = {r["id"]: r["note"] for r in app_consent.build_request(db, newcomer)["rows"]}
  assert "Also held by Reader" in notes["chat_log_access"] and "hold only" in notes["chat_log_access"]
  assert "Sharer" in notes["cross_app_access"]
  assert "Reader could read it" in notes["share_with_apps"]


def test_migration_grandfathers_existing_apps_and_is_idempotent():
  engine = create_engine("sqlite://")
  contract = _contract(github_access=True)
  with engine.begin() as conn:
    conn.execute(text("CREATE TABLE apps (id INTEGER PRIMARY KEY, capability_contract JSON, project_templates_json JSON)"))
    conn.execute(text("INSERT INTO apps (id, capability_contract, project_templates_json) VALUES (1, :c, NULL), (2, NULL, NULL)"), {"c": json.dumps(contract)})
  _add_app_accepted_consent(engine)
  _add_app_accepted_consent(engine)
  with engine.connect() as conn:
    rows = dict(conn.execute(text("SELECT id, accepted_consent FROM apps")).fetchall())
  record = json.loads(rows[1])
  assert record["via"] == "migration" and record["contract"] == contract
  assert rows[2] is None


def test_every_contract_permission_has_a_row():
  """A permission added to the contract without a popup row must fail here."""
  from app.app_capabilities import contract_from_manifest
  manifest = {
    "permissions": {
      "chat_log_access": "summary_with_deleted", "filesystem_access": True,
      "cross_app_access": "write", "share_with_apps": "write",
      "shared_memory": "write", "manage_apps": True, "manage_skills": True,
      "github_access": True, "github_connect": True, "connections_manage": True,
      "connect_manage": True, "identity_manage": True, "railway_manage": True,
      "location_history_read": True, "job_secret_read": ["TOKEN"],
    },
  }
  contract = contract_from_manifest(manifest)
  unhandled = set(contract["data"]) - app_consent.HANDLED_DATA_KEYS
  assert not unhandled, f"contract.data keys without a consent row: {sorted(unhandled)}"
  ids = {r["id"] for r in app_consent.rows_from_contract(contract)}
  assert {"chat_log_access", "shared_memory", "cross_app_access", "share_with_apps",
          "filesystem_access", "manage_apps", "manage_skills", "github_access",
          "github_connect", "connections_manage", "connect_manage",
          "identity_manage", "railway_manage", "location_history_read",
          "job_secret_read"} <= ids


def test_helper_activity_read_has_a_non_critical_row():
  rows = app_consent.rows_from_contract(_contract(helper_activity_read=True))
  assert [(r["id"], r["title"], r["critical"]) for r in rows] == [
    ("helper_activity_read", "Read helper activity", False),
  ]
  assert "helper_activity_read" in app_consent.HANDLED_DATA_KEYS


def test_acceptance_stores_rows_and_survives_a_rewording(db, monkeypatch):
  contract = _contract(github_access=True)
  app = _app(db, "snapshot", contract=contract)
  digest = app_consent.request_digest(app_consent.current_rows(app))
  assert app_consent.accept(app, digest)
  record = app.accepted_consent
  assert record["rows"] == [{"id": "github_access", "rank": 1}] and record["version"] == 1
  assert "contract" not in record
  # Rewording a row must not turn the old answer into a new question.
  monkeypatch.setattr(app_consent, "_FLAGS", tuple(
    (k, i, "Changed title", "Changed text") for k, i, *_ in app_consent._FLAGS))
  assert app_consent.status(app) == "accepted"


def test_migrated_records_with_a_contract_copy_still_work(db):
  contract = _contract(github_access=True)
  app = _app(db, "legacy", contract=contract, accepted={
    "contract": contract, "project_builders": False, "digest": None,
    "at": "2026-10-20T00:00:00Z", "via": "migration",
  })
  assert app_consent.status(app) == "accepted"
  app.capability_contract = _contract(github_access=True, manage_apps=True)
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["manage_apps"]


def test_revoke_makes_the_app_dormant_again_and_is_human_only(client, owner_token, db, chat):
  headers = _owner_headers(client, owner_token)
  contract = _contract(github_access=True)
  app = _app(db, "revokable", contract=contract, accepted=app_consent.platform_acceptance(contract))
  agent = {"Authorization": "Bearer " + auth.create_agent_token(chat.id, "test", 0)}
  assert client.delete(f"/api/apps/{app.id}/consent", headers=agent).status_code == 403
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "accepted"
  assert client.delete(f"/api/apps/{app.id}/consent", headers=headers).status_code == 200
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "required"
  assert client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).status_code == 409


def test_install_preview_carries_the_same_rows(client, owner_token):
  headers = _owner_headers(client, owner_token)
  manifest = {
    "id": "previewed", "name": "Previewed", "version": "1.0.0",
    "description": "x", "entry": "index.jsx",
    "permissions": {"manage_apps": True},
  }
  response = client.post(
    "/api/apps/preview",
    json={"manifest": manifest, "raw_base": "https://example.test/previewed/"},
    headers=headers,
  )
  assert response.status_code == 200, response.text
  rows = response.json()["access_rows"]
  assert [(r["id"], r["critical"]) for r in rows] == [("manage_apps", True)]


def test_platform_apps_are_accepted_at_first_boot(db):
  contract = _contract(manage_apps=True)
  acceptance = app_consent.platform_acceptance(contract)
  app = _app(db, "platformapp", contract=contract, accepted=acceptance)
  assert acceptance["via"] == "platform"
  assert app_consent.status(app) == "accepted"


def _accepted(db, slug, contract):
  app = _app(db, slug, contract=contract)
  assert app_consent.accept(app, app_consent.request_digest(app_consent.current_rows(app)))
  return app


def test_named_secrets_are_part_of_the_accepted_answer(db):
  app = _accepted(db, "secrets", _contract(job_secret_read=["A"]))
  assert app.accepted_consent["rows"] == [
    {"id": "job_secret_read", "rank": 1, "scope": ["A"]},
  ]
  assert app_consent.status(app) == "accepted"
  # Fewer names is no new request; an added or swapped name asks again.
  app.capability_contract = _contract()
  assert app_consent.status(app) == "accepted"
  app.capability_contract = _contract(job_secret_read=["A", "B"])
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["job_secret_read"]
  app.capability_contract = _contract(job_secret_read=["B"])
  assert app_consent.status(app) == "required"


def test_changed_secret_names_change_the_digest_so_a_stale_accept_fails(db):
  app = _app(db, "stale", contract=_contract(job_secret_read=["A"]))
  shown = app_consent.request_digest(app_consent.current_rows(app))
  app.capability_contract = _contract(job_secret_read=["B"])
  assert not app_consent.accept(app, shown)
  assert app.accepted_consent is None


def test_background_work_is_a_row_with_timing_and_authority():
  contract = _contract()
  contract["background"] = {
    "job": "job.py", "mode": "scheduled", "cron": "0 6 * * *",
    "user_configurable": False, "initialize_on_install": True,
  }
  (row,) = app_consent.rows_from_contract(contract)
  assert row["id"] == "background_job"
  # Timing, the immediate first run and the run's authority stay stated.
  assert "every day at 06:00" in row["text"] and "as soon as you allow it" in row["text"]
  assert "broad access" in row["text"]


def test_background_escalation_asks_again(db):
  on_demand = _contract()
  on_demand["background"] = {"job": "j.py", "mode": "on_demand", "cron": None,
                             "initialize_on_install": False}
  app = _accepted(db, "jobs", on_demand)
  scheduled = _contract()
  scheduled["background"] = {"job": "j.py", "mode": "scheduled", "cron": "* * * * *",
                             "initialize_on_install": False}
  app.capability_contract = scheduled
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["background_job"]


def _service_contract(**data):
  contract = _contract(**data)
  contract["service"] = {"protocol": "json-v1", "entry": "service.py", "access": "self"}
  contract["agent"]["tools"] = [{
    "name": "lookup", "description": "Look something up.", "input_schema": {"type": "object"},
  }]
  return contract


def test_a_never_accepted_app_adds_nothing_to_agents_or_services(db):
  """Dormant means dormant: prompt fragment, tools and service wait for the answer."""
  from fastapi import HTTPException
  from app import app_services, app_tools, system_prompts

  contract = _service_contract()
  waiting = _app(db, "waiting", contract=contract)
  waiting.system_prompt_file = "waiting.md"
  accepted = _app(db, "answered", contract=contract, accepted=app_consent.platform_acceptance(contract))
  accepted.system_prompt_file = "answered.md"
  db.commit()

  assert [s.slug for s in system_prompts.fragment_sources(db)] == ["answered"]
  assert {t.app_id for t in app_tools.live_app_tools(db)} == {accepted.id}
  with pytest.raises(HTTPException) as refused:
    app_services.service_contract(waiting, access="self")
  assert refused.value.status_code == 404
  assert app_services.service_contract(accepted, access="self")["entry"] == "service.py"


def test_revoking_ends_sessions_that_were_already_issued(client, owner_token, db):
  headers = _owner_headers(client, owner_token)
  contract = _contract(github_access=True)
  app = _app(db, "sessions", contract=contract, accepted=app_consent.platform_acceptance(contract))
  token = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).json()["token"]
  app_headers = {"Authorization": f"Bearer {token}"}
  assert client.get(f"/api/storage/apps/{app.id}/note.json", headers=app_headers).status_code != 401
  assert client.delete(f"/api/apps/{app.id}/consent", headers=headers).status_code == 200
  assert client.get(f"/api/storage/apps/{app.id}/note.json", headers=app_headers).status_code == 401


def test_notes_never_name_an_app_that_was_not_accepted(db):
  _app(db, "silent", contract=_contract(github_access=True))
  newcomer = _app(db, "asker", contract=_contract(github_access=True))
  (row,) = app_consent.build_request(db, newcomer)["rows"]
  assert "Silent" not in row["note"] and "No installed app holds this yet" in row["note"]


def test_widening_from_app_only_to_public_asks_again(db):
  def contract(**exposure):
    c = _contract()
    c["service"] = exposure.get("service", {})
    c["public"] = exposure.get("public", {"network": []})
    return c

  apps_only = contract(service={"access": "apps"})
  app = _app(db, "widener", contract=apps_only, accepted=app_consent.platform_acceptance(apps_only))
  assert app_consent.status(app) == "accepted"
  for wider in (
    contract(service={"access": "public"}),
    contract(service={"access": "apps"}, public={"network": [], "storage": {"read": True}}),
    contract(service={"access": "apps"}, public={"network": ["example.com"]}),
  ):
    app.capability_contract = wider
    assert app_consent.status(app) == "required"
    assert [r["id"] for r in app_consent.needed_rows(app)] == ["reach"]
    assert app_consent.request_digest(app_consent.current_rows(app)) != app_consent.request_digest(
      app_consent.rows_from_contract(apps_only))


def test_revoke_and_reaccept_keep_the_apps_identity(client, owner_token, db):
  headers = _owner_headers(client, owner_token)
  contract = _contract(github_access=True)
  app = _app(db, "identity", contract=contract, accepted=app_consent.platform_acceptance(contract))
  nonce = app.token_nonce
  assert client.delete(f"/api/apps/{app.id}/consent", headers=headers).status_code == 200
  digest = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]
  assert client.post(f"/api/apps/{app.id}/consent", json={"digest": digest}, headers=headers).status_code == 200
  db.refresh(app)
  assert app.token_nonce == nonce


def test_first_acceptance_starts_the_install_time_run(client, owner_token, db, monkeypatch):
  from app.routes import app_consent as route

  launched = []
  monkeypatch.setattr(route, "launch_app_job", lambda *a, **k: launched.append(a))
  headers = _owner_headers(client, owner_token)
  contract = _contract()
  contract["background"] = {"job": "job.sh", "mode": "on_demand", "cron": None,
                            "initialize_on_install": True}
  app = _app(db, "firstrun", contract=contract)
  digest = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]
  assert client.post(f"/api/apps/{app.id}/consent", json={"digest": digest}, headers=headers).status_code == 200
  assert len(launched) == 1 and str(launched[0][1]).endswith("firstrun/job.sh")
  # A later re-accept after a widening does not run it again.
  client.delete(f"/api/apps/{app.id}/consent", headers=headers)
  digest = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]
  client.post(f"/api/apps/{app.id}/consent", json={"digest": digest}, headers=headers)
  assert len(launched) == 2  # revoke returns it to never-accepted, so it is a first acceptance again


def test_an_update_that_widens_exposure_blocks_service_calls_until_accepted(db):
  from fastapi import HTTPException
  from app import app_services

  apps_only = _service_contract()
  apps_only["service"]["access"] = "apps"
  app = _app(db, "wideservice", contract=apps_only, accepted=app_consent.platform_acceptance(apps_only))
  app_services.service_contract(app, access="self")
  widened = _service_contract()
  widened["service"]["access"] = "public"
  app.capability_contract = widened
  with pytest.raises(HTTPException) as err:
    app_services.service_contract(app, access="public")
  assert err.value.status_code == 404


def test_a_token_from_before_a_revoke_stays_dead_after_reaccepting(client, owner_token, db):
  headers = _owner_headers(client, owner_token)
  contract = _contract(github_access=True)
  app = _app(db, "revived", contract=contract)
  accept = lambda: client.post(
    f"/api/apps/{app.id}/consent",
    json={"digest": client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]},
    headers=headers,
  )
  assert accept().status_code == 200
  token = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).json()["token"]
  old = {"Authorization": f"Bearer {token}"}
  assert client.get(f"/api/storage/apps/{app.id}/note.json", headers=old).status_code != 401
  assert client.delete(f"/api/apps/{app.id}/consent", headers=headers).status_code == 200
  assert accept().status_code == 200
  assert client.get(f"/api/storage/apps/{app.id}/note.json", headers=old).status_code == 401
  fresh = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).json()["token"]
  assert client.get(
    f"/api/storage/apps/{app.id}/note.json", headers={"Authorization": f"Bearer {fresh}"},
  ).status_code != 401


def test_a_widened_update_pauses_every_surface_not_just_tokens(client, owner_token, db):
  from app import app_tools, system_prompts

  old = _service_contract()
  app = _app(db, "paused", contract=old, accepted=app_consent.platform_acceptance(old))
  app.system_prompt_file = "paused.md"
  db.commit()
  assert [s.slug for s in system_prompts.fragment_sources(db)] == ["paused"]
  wider = _service_contract(github_access=True)
  app.capability_contract = wider
  db.commit()
  assert not system_prompts.fragment_sources(db)
  assert not app_tools.live_app_tools(db)
  assert client.post(
    "/api/auth/app-job-token", json={"app_id": app.id}, headers=_owner_headers(client, owner_token),
  ).status_code == 409


def test_a_dormant_apps_skills_wait_for_acceptance(client, auth, owner_token, bypass_url_validation):
  from tests.test_app_skills import SKILL_V1, JSX, _install, _skill_manifest, _skills_dir

  installed = _install(client, auth, _skill_manifest(), {"index.jsx": JSX, "contributing.md": SKILL_V1})
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  skill = _skills_dir() / "contributing.md"
  assert not skill.exists()  # installed, but dormant: agents do not see it yet
  headers = _owner_headers(client, owner_token)
  digest = client.get(f"/api/apps/{app_id}/consent", headers=headers).json()["digest"]
  assert client.post(f"/api/apps/{app_id}/consent", json={"digest": digest}, headers=headers).status_code == 200
  assert skill.read_text() == SKILL_V1
  assert client.delete(f"/api/apps/{app_id}/consent", headers=headers).status_code == 200
  assert not skill.exists()


def test_an_update_that_pauses_an_accepted_app_notifies_the_owner(
  client, auth, owner_token, db, bypass_url_validation,
):
  import json as _json
  from unittest.mock import patch

  from app import models as m
  from tests.test_app_skills import BASE, JSX, _fake_async_client, _skill_manifest

  def serve(manifest):
    responses = {BASE + "mobius.json": (200, _json.dumps(manifest).encode()),
                 BASE + "index.jsx": (200, JSX.encode()),
                 BASE + "contributing.md": (200, b"# c\n")}
    return patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses))

  with serve(_skill_manifest()):
    app_id = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"}).json()["id"]
  headers = _owner_headers(client, owner_token)
  digest = client.get(f"/api/apps/{app_id}/consent", headers=headers).json()["digest"]
  client.post(f"/api/apps/{app_id}/consent", json={"digest": digest}, headers=headers)

  wider = _skill_manifest(version="2.0.0", permissions={
    "cross_app_access": "none", "share_with_apps": "none", "github_access": True})
  with serve(wider):
    update = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"})
    if update.status_code == 409:  # the platform asks the caller to confirm the new access
      update = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": BASE + "mobius.json",
        "reviewed_capability_digest": update.json()["detail"]["preview"]["capability_digest"]})
  assert update.status_code in (200, 201), update.text
  assert app_consent.dormant(db.get(m.App, app_id))
  note = db.query(m.Notification).filter(m.Notification.source_id == str(app_id)).one()
  assert "needs your approval" in note.title


def test_every_preview_carries_proposed_and_installed_rows():
  from app import schemas

  old = _contract(github_access=True)
  new = _contract(github_access=True, manage_apps=True)
  preview = schemas.AppPreviewOut(
    manifest={}, capability_contract=new, capability_digest="d",
    installed_contract=old, capability_diff={},
  ).model_dump()
  assert {r["id"] for r in preview["access_rows"]} == {"github_access", "manage_apps"}
  assert {r["id"] for r in preview["installed_access_rows"]} == {"github_access"}


def test_a_runtime_limit_change_changes_the_row_text():
  def rows(limit):
    contract = _contract()
    contract["runtime"] = {"device.asset-cache": {
      "title": "Store large files", "description": "Download verified assets.",
      "limits": {"max_bytes": limit}, "reason": None,
    }}
    return {r["id"]: r["text"] for r in app_consent.rows_from_contract(contract)}

  assert rows(256 * 1024 * 1024)["device.asset-cache"] == (
    "Download verified assets. Limits: up to 256 MiB."
  )
  assert rows(2 * 1024 ** 3)["device.asset-cache"].endswith("up to 2 GiB.")


def _runtime_contract(**limits):
  contract = _contract()
  contract["runtime"] = {"media.camera.capture": {
    "title": "Record video", "description": "Use a device camera.",
    "limits": {"max_duration_ms": 60_000, "max_bytes": 128 * 1024 * 1024, **limits},
    "reason": None,
  }}
  return contract


def test_runtime_limits_are_part_of_the_accepted_answer(db):
  app = _accepted(db, "camera", _runtime_contract())
  assert app.accepted_consent["rows"] == [{
    "id": "media.camera.capture", "rank": 1,
    "limits": {"max_bytes": 128 * 1024 * 1024, "max_duration_ms": 60_000},
  }]
  assert app_consent.status(app) == "accepted"


def test_raising_a_runtime_limit_asks_again_but_lowering_it_does_not(db):
  app = _accepted(db, "camera-update", _runtime_contract())
  app.capability_contract = _runtime_contract(max_duration_ms=30_000)
  assert app_consent.status(app) == "accepted"
  app.capability_contract = _runtime_contract(max_duration_ms=300_000)
  assert app_consent.status(app) == "required"
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["media.camera.capture"]


def test_changed_runtime_limits_change_the_digest_so_a_stale_accept_fails(db):
  app = _app(db, "camera-stale", contract=_runtime_contract())
  shown = app_consent.request_digest(app_consent.current_rows(app))
  app.capability_contract = _runtime_contract(max_bytes=256 * 1024 * 1024)
  assert not app_consent.accept(app, shown)
  assert app.accepted_consent is None


def test_an_answer_saved_without_runtime_limits_asks_once_more(db):
  # Rows accepted before limits were recorded say nothing about the limits the
  # owner saw, so they cannot cover any; one new answer records them.
  app = _app(db, "camera-legacy", contract=_runtime_contract(), accepted={
    "id": "legacy", "version": 1, "rows": [{"id": "media.camera.capture", "rank": 1}],
    "digest": "", "at": "2026-10-21T12:00:00Z", "via": "owner",
  })
  assert app_consent.status(app) == "required"
  assert app_consent.accept(app, app_consent.request_digest(app_consent.current_rows(app)))
  assert app_consent.status(app) == "accepted"


def test_installed_rows_use_the_installed_versions_templates():
  from app import schemas

  contract = _contract()
  preview = schemas.AppPreviewOut(
    manifest={"project_templates": [{"id": "x"}]}, capability_contract=contract,
    capability_digest="d", installed_contract=contract, capability_diff={},
    installed_project_templates=False,
  ).model_dump()
  assert "project_builders" in {r["id"] for r in preview["access_rows"]}
  assert "project_builders" not in {r["id"] for r in preview["installed_access_rows"]}


def _install_dormant_skilled(client, auth):
  from tests.test_app_skills import SKILL_V1, JSX, _install, _skill_manifest

  installed = _install(client, auth, _skill_manifest(), {"index.jsx": JSX, "contributing.md": SKILL_V1})
  assert installed.status_code == 201, installed.text
  return installed.json()["id"]


def test_recovering_a_dormant_app_does_not_publish_its_skills(
  client, auth, owner_token, bypass_url_validation,
):
  from tests.test_app_skills import SKILL_V1, _skills_dir

  app_id = _install_dormant_skilled(client, auth)
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert client.post(f"/api/apps/{app_id}/recover", headers=auth).status_code == 200
  skill = _skills_dir() / "contributing.md"
  assert not skill.exists()
  headers = _owner_headers(client, owner_token)
  digest = client.get(f"/api/apps/{app_id}/consent", headers=headers).json()["digest"]
  assert client.post(f"/api/apps/{app_id}/consent", json={"digest": digest}, headers=headers).status_code == 200
  assert skill.read_text() == SKILL_V1


def test_a_sync_that_fails_after_writing_still_leaves_a_dormant_skill_aside(
  client, auth, bypass_url_validation, monkeypatch,
):
  from app import app_skills
  from tests.test_app_skills import _skills_dir

  real = app_skills._regenerate_skills_index
  calls = []

  def fail_once(skills_dir):
    calls.append(1)
    if len(calls) == 1:
      raise OSError("index write failed")
    return real(skills_dir)

  monkeypatch.setattr(app_skills, "_regenerate_skills_index", fail_once)
  _install_dormant_skilled(client, auth)
  assert len(calls) >= 2  # the sync failed after writing, and the cleanup still ran
  assert not (_skills_dir() / "contributing.md").exists()


@pytest.mark.asyncio
async def test_resyncing_a_dormant_app_keeps_its_skills_aside(
  client, auth, db, bypass_url_validation,
):
  """Apply and update both resync through the same function."""
  from app import app_skills
  from tests.test_app_skills import _skill_manifest, _skills_dir

  app_id = _install_dormant_skilled(client, auth)
  app = db.get(models.App, app_id)
  warnings = []
  await app_skills._sync_app_skills(db, app, _skill_manifest(), warnings)
  assert not (_skills_dir() / "contributing.md").exists()


def test_a_failed_first_ownership_write_never_publishes_a_dormant_skill(
  client, auth, db, bypass_url_validation, monkeypatch,
):
  from app import app_skills, skills
  from tests.test_app_skills import _skills_dir

  real_write = app_skills.atomic_write
  failures = []

  def fail_first_ownership_write(path, content, *args, **kwargs):
    if path.name == ".app-skills.json" and not failures:
      failures.append(path)
      raise OSError("simulated ownership write failure")
    return real_write(path, content, *args, **kwargs)

  monkeypatch.setattr(app_skills, "atomic_write", fail_first_ownership_write)
  app_id = _install_dormant_skilled(client, auth)
  assert failures
  assert app_consent.dormant(db.get(models.App, app_id))
  assert not (_skills_dir() / "contributing.md").exists()
  assert "contributing.md" not in [
    s.read_path.name for s in skills.enumerate_skills(_skills_dir())
  ]


def test_a_dormant_skill_is_never_live_during_its_sync(
  client, auth, bypass_url_validation, monkeypatch,
):
  from app import app_skills
  from tests.test_app_skills import _skills_dir

  live = _skills_dir() / "contributing.md"
  real_write = app_skills.atomic_write
  writes = []

  def watch(path, content, *args, **kwargs):
    assert path != live and not live.exists()
    writes.append(path)
    return real_write(path, content, *args, **kwargs)

  monkeypatch.setattr(app_skills, "atomic_write", watch)
  _install_dormant_skilled(client, auth)
  assert _skills_dir() / ".inactive" in [p.parents[1] for p in writes if p.suffix == ".md"]
  assert not live.exists()


def test_an_update_that_pauses_an_app_sets_its_new_skill_aside_until_accepted(
  client, auth, owner_token, db, bypass_url_validation,
):
  import json as _json
  from unittest.mock import patch

  from tests.test_app_skills import BASE, JSX, _fake_async_client, _skill_manifest, _skills_dir

  def serve(manifest, skill):
    responses = {BASE + "mobius.json": (200, _json.dumps(manifest).encode()),
                 BASE + "index.jsx": (200, JSX.encode()),
                 BASE + "contributing.md": (200, skill)}
    return patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses))

  headers = _owner_headers(client, owner_token)
  with serve(_skill_manifest(), b"# v1\n"):
    app_id = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"}).json()["id"]
  digest = client.get(f"/api/apps/{app_id}/consent", headers=headers).json()["digest"]
  client.post(f"/api/apps/{app_id}/consent", json={"digest": digest}, headers=headers)
  live = _skills_dir() / "contributing.md"
  assert live.read_bytes() == b"# v1\n"

  wider = _skill_manifest(version="2.0.0", permissions={
    "cross_app_access": "none", "share_with_apps": "none", "github_access": True})
  with serve(wider, b"# v2\n"):
    update = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"})
    if update.status_code == 409:
      update = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": BASE + "mobius.json",
        "reviewed_capability_digest": update.json()["detail"]["preview"]["capability_digest"]})
  assert update.status_code in (200, 201), update.text
  assert app_consent.dormant(db.get(models.App, app_id))
  assert not live.exists()

  digest = client.get(f"/api/apps/{app_id}/consent", headers=headers).json()["digest"]
  assert client.post(f"/api/apps/{app_id}/consent", json={"digest": digest}, headers=headers).status_code == 200
  assert live.read_bytes() == b"# v2\n"


def test_an_app_that_asks_for_nothing_is_never_asked(client, owner_token, db):
  app = _app(db, "plain", contract=_contract())
  assert app_consent.status(app) == "accepted"
  assert not app_consent.dormant(app)
  headers = _owner_headers(client, owner_token)
  assert client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).status_code == 200
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "accepted"
  # An update that starts asking for something is asked, in full.
  app.capability_contract = _contract(github_access=True)
  assert app_consent.status(app) == "required"
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["github_access"]


def test_an_update_that_starts_asking_notifies_even_when_nothing_was_accepted_before(
  client, auth, owner_token, db, bypass_url_validation,
):
  import json as _json
  from unittest.mock import patch

  from app import models as m
  from tests.test_app_skills import BASE, JSX, _fake_async_client, _skill_manifest

  def serve(manifest):
    responses = {BASE + "mobius.json": (200, _json.dumps(manifest).encode()),
                 BASE + "index.jsx": (200, JSX.encode())}
    return patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses))

  plain = _skill_manifest(skills=[], source_files=[])
  with serve(plain):
    installed = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"})
  app_id = installed.json()["id"]
  assert not app_consent.dormant(db.get(m.App, app_id))  # it asks for nothing

  wider = _skill_manifest(version="2.0.0", skills=[], source_files=[], permissions={
    "cross_app_access": "none", "share_with_apps": "none", "github_access": True})
  with serve(wider):
    update = client.post("/api/apps/install", headers=auth, json={"manifest_url": BASE + "mobius.json"})
    if update.status_code == 409:
      update = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": BASE + "mobius.json",
        "reviewed_capability_digest": update.json()["detail"]["preview"]["capability_digest"]})
  assert update.status_code in (200, 201), update.text
  assert app_consent.dormant(db.get(m.App, app_id))
  note = db.query(m.Notification).filter(m.Notification.source_id == str(app_id)).one()
  assert "needs your approval" in note.title


def test_revoking_an_app_that_asks_for_nothing_reports_the_truth(client, owner_token, db):
  app = _app(db, "nothing", contract=_contract())
  response = client.delete(f"/api/apps/{app.id}/consent", headers=_owner_headers(client, owner_token))
  assert response.status_code == 200
  assert response.json()["status"] == "accepted"


def test_access_rows_read_in_plain_words():
  contract = _contract()
  contract["background"] = {"mode": "scheduled", "cron": "0 10 * * *"}
  contract["agent"] = {"embeds_agent": True}
  contract["runtime"] = {"speech.synthesis": {
    "title": "Generate speech", "description": "Turn text into audio.",
    "limits": {"max_text_chars": 50_000}, "reason": "Read reports aloud.",
  }}
  rows = {r["id"]: r for r in app_consent.rows_from_contract(contract)}
  job = rows["background_job"]
  assert job["title"] == "Run on a schedule"
  assert job["text"].startswith("Runs by itself every day at 10:00.")
  assert "token" not in job["text"] and "0 10" not in job["text"]
  assert rows["agent_start"]["text"] == (
    "Can open agent chats on its own. Commands in those chats run on your "
    "Möbius without asking you first."
  )
  assert rows["speech.synthesis"]["text"] == (
    "Turn text into audio. Limits: up to 50,000 characters. Why: Read reports aloud."
  )


def test_schedules_are_described_in_words_and_unusual_ones_quoted():
  describe = app_consent.describe_schedule
  assert describe("* * * * *") == "every minute"
  assert describe("*/1 * * * *") == "every minute"
  assert describe("*/15 * * * *") == "every 15 minutes"
  assert describe("5 * * * *") == "every hour"
  assert describe("0 */6 * * *") == "every 6 hours"
  assert describe("0 10 * * *") == "every day at 10:00"
  assert describe("30 7 * * 1") == "every Monday at 07:30"
  assert describe("0 10 * * 1-5") == "every weekday at 10:00"
  assert describe("0 10 * * 0,6") == "every Saturday and Sunday at 10:00"
  assert describe("0 10 * * 1,3,5") == "every Monday, Wednesday and Friday at 10:00"
  assert describe("0 9 1 * *") == "on the schedule \u201c0 9 1 * *\u201d"
  assert describe("") == "on a schedule"


def test_a_fixed_clock_time_names_no_timezone():
  # A fixed wall time runs in the owner's zone once known and in server time
  # before that, so naming any one zone would be false for some owners.
  for cron in ("0 10 * * *", "0 10 * * 1-5", "30 7 * * 1"):
    text = app_consent.describe_schedule(cron)
    assert "UTC" not in text and "your time" not in text and "server" not in text


def test_uneven_cron_steps_are_quoted_rather_than_called_intervals():
  # */45 fires at :00 and :45 (gaps of 45 and 15 minutes); */5 hours leaves a
  # 4-hour gap across midnight. Only steps that divide the period are intervals.
  describe = app_consent.describe_schedule
  for cron in ("*/45 * * * *", "*/7 * * * *", "0 */5 * * *", "0 */7 * * *", "*/0 * * * *"):
    assert describe(cron) == f"on the schedule \u201c{cron}\u201d"


def test_out_of_range_clock_fields_are_quoted_not_read_as_a_time():
  describe = app_consent.describe_schedule
  for cron in ("0 25 * * *", "61 10 * * *", "75 * * * *", "0 10 * * 5-1"):
    assert describe(cron) == f"on the schedule \u201c{cron}\u201d"


def test_every_catalog_limit_key_reads_in_plain_words():
  from app.app_capabilities import RUNTIME_CAPABILITY_DEFINITIONS

  for capability, definition in RUNTIME_CAPABILITY_DEFINITIONS.items():
    limits = definition["default_limits"]
    text = app_consent._runtime_text(capability, {"description": "Do it.", "limits": limits})
    assert "_" not in text and ". for" not in text and text.endswith("."), (capability, text)
  assert app_consent._runtime_text("device.asset-cache", {
    "description": "Download verified assets.",
    "limits": RUNTIME_CAPABILITY_DEFINITIONS["device.asset-cache"]["default_limits"],
  }) == (
    "Download verified assets. Limits: up to 256 MiB, up to 256 MiB per file, "
    "up to 8 MiB per download chunk."
  )
  assert app_consent._runtime_text("media.camera.capture", {
    "description": "Use a device camera while this app is visible.",
    "limits": {"max_duration_ms": 60_000, "max_bytes": 128 * 1024 * 1024},
  }) == (
    "Use a device camera while this app is visible for up to 60 seconds per "
    "request. Limits: up to 128 MiB."
  )


def test_rewording_never_changes_what_an_accepted_answer_covers():
  # The digest pins ids, levels, scopes and limits, not text, so plainer
  # wording never turns an earlier answer into a new question.
  contract = _contract()
  contract["background"] = {"mode": "scheduled", "cron": "0 10 * * *"}
  rows = app_consent.rows_from_contract(contract)
  reworded = [{**row, "text": row["text"] + " (reworded)"} for row in rows]
  assert app_consent.request_digest(rows) == app_consent.request_digest(reworded)
