"""Accepted app services own policy; the platform owns their hard boundary."""

import asyncio
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import app_services, auth as auth_tokens, models
from app.applied_app_runtime import runtime_parent
from app.config import get_settings


@pytest.mark.asyncio
async def test_one_apps_backlog_does_not_take_other_apps_execution_slots(monkeypatch):
  class BusyApp(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  busy = BusyApp()
  monkeypatch.setattr(app_services, "_app_slots", {1: busy})
  monkeypatch.setattr(app_services, "_global_slots", asyncio.Semaphore(1))
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  def entered(app, _contract):
    assert app.id == 2
    raise HTTPException(418, "second app admitted")

  monkeypatch.setattr(app_services, "service_entry", entered)
  first = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="second app admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=2), None, {}), timeout=1,
      )
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_spawn_reaps_process_before_releasing_runtime(monkeypatch, tmp_path):
  entry = tmp_path / "service.py"
  entry.write_text("import time; time.sleep(60)")
  created = asyncio.Event()
  finish_spawn = asyncio.Event()
  processes = []
  released = []
  real_spawn = asyncio.create_subprocess_exec

  async def delayed_spawn(*args, **kwargs):
    process = await real_spawn(*args, **kwargs)
    processes.append(process)
    created.set()
    await finish_spawn.wait()
    return process

  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "service_entry", lambda *a: entry)
  monkeypatch.setattr(app_services, "service_environment", lambda *a: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(
    close=lambda: released.append(processes[0].returncode),
  ))
  monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
  try:
    await asyncio.wait_for(created.wait(), timeout=3)
    task.cancel()
    await asyncio.sleep(0)  # Deliver cancellation precisely inside admission.
    task.cancel()  # A shutdown or timeout can cancel cleanup again.
    await asyncio.sleep(0)
    assert not task.done()
    assert not released
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, timeout=3)
    assert released and released[0] is not None, "runtime released before child was reaped"
    assert processes[0].returncode is not None
  finally:
    finish_spawn.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for process in processes:
      if process.returncode is None:
        os.killpg(process.pid, signal.SIGKILL)
      await process.wait()


SERVICE = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({
  "status": 201,
  "body": {
    "method": request["method"],
    "path": request["path"],
    "query": request["query"],
    "body": request["body"],
    "scope": request["actor"]["scope"],
    "app_slug": request["actor"].get("app_slug"),
  },
  "headers": {"X-App-Service": "yes"},
}))
'''


def _service_app(db, *, access="self", slug="service-test"):
  source = Path(get_settings().data_dir) / "apps" / slug
  source.mkdir(parents=True)
  app = models.App(
    name="Service test",
    slug=slug,
    description="",
    source_dir=str(source),
    jsx_source="export default () => null",
    capability_contract={
      "schema": 6,
      "service": {
        "entry": "service.py",
        "access": access,
        "protocol": "json-v1",
        "max_request_bytes": 8 * 1024 * 1024,
        "max_response_bytes": 8 * 1024 * 1024,
      },
    },
  )
  db.add(app)
  db.commit()
  revision = "a" * 64
  accepted = runtime_parent(app.id) / revision
  accepted.mkdir(parents=True)
  (accepted / "service.py").write_bytes(SERVICE)
  app.runtime_revision = revision
  db.commit()
  return app


def test_authenticated_service_receives_one_bounded_json_envelope(
  client, auth, db,
):
  app = _service_app(db)

  response = client.post(
    f"/api/apps/{app.id}/service/review/decision?state=ready&state=fresh",
    headers=auth,
    json={"record": "abc"},
  )

  assert response.status_code == 201
  assert response.headers["x-app-service"] == "yes"
  assert response.json() == {
    "method": "POST",
    "path": "review/decision",
    "query": {"state": ["ready", "fresh"]},
    "body": {"record": "abc"},
    "scope": "owner",
    "app_slug": None,
  }


def test_public_service_requires_an_explicit_reviewed_grant(client, auth, db):
  private = _service_app(db, slug="private-service")
  public = _service_app(db, access="public", slug="public-service")

  denied = client.get("/api/app-services/private-service/status")
  allowed = client.get("/api/app-services/public-service/status")

  assert denied.status_code == 404
  assert allowed.status_code == 201
  assert allowed.json()["scope"] == "public"
  assert private.id != public.id


def test_app_services_have_no_app_specific_route_aliases(client, auth, db):
  _service_app(db, access="public", slug="common")

  canonical = client.get("/api/app-services/common/status")
  legacy = client.get("/api/common/status")

  assert canonical.status_code == 201, canonical.text
  assert legacy.status_code == 404


def test_shared_service_requires_an_explicit_cross_app_grant(
  client, owner_token, db,
):
  owner = db.query(models.Owner).one()
  caller = models.App(
    name="Caller", slug="caller", description="", source_dir="/tmp/caller",
    jsx_source="export default () => null", token_nonce="caller-nonce",
  )
  db.add(caller)
  db.commit()
  private = _service_app(db, slug="private-shared")
  shared = _service_app(db, access="apps", slug="shared-service")
  token = auth_tokens.create_app_token(
    caller.id, owner.username, owner.token_epoch, app_nonce=caller.token_nonce,
  )
  headers = {"Authorization": f"Bearer {token}"}

  denied = client.get("/api/services/private-shared/status", headers=headers)
  allowed = client.get("/api/services/shared-service/status", headers=headers)

  assert denied.status_code == 403
  assert allowed.status_code == 201
  assert allowed.json()["scope"] == "app"
  assert allowed.json()["app_slug"] == "caller"
  assert private.id != shared.id


def test_service_contract_is_bound_to_the_accepted_runtime(client, auth, db):
  app = _service_app(db, slug="missing-entry")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").unlink()

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 503
  assert response.json()["detail"] == "Accepted app service entry is unavailable."


def test_service_runtime_stays_pinned_through_process_exit(
  client, auth, db, monkeypatch,
):
  app = _service_app(db, slug="pinned-service")
  state = {"closed": False}

  class Pin:
    def close(self):
      state["closed"] = True

  monkeypatch.setattr(app_services, "hold_runtime", lambda _app_id: Pin())
  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 201
  assert state["closed"] is True


def test_service_cannot_set_transport_or_credential_headers(client, auth, db):
  app = _service_app(db, slug="header-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import json\nprint(json.dumps({\"headers\":{\"Set-Cookie\":\"x=y\"}}))\n"
  )

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 502
  assert response.json()["detail"] == "response contains an invalid header"
  assert "set-cookie" not in response.headers


def test_service_rejects_nonstandard_json_constants(client, auth, db):
  app = _service_app(db, slug="constant-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text('print("{\\\"body\\\":NaN}")\n')

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 502
  assert response.json()["detail"] == "App service returned invalid JSON."


def test_service_rejects_nonstandard_request_json(client, auth, db):
  app = _service_app(db, slug="request-constant-service")

  response = client.post(
    f"/api/apps/{app.id}/service/status", headers={
      **auth, "Content-Type": "application/json",
    }, content='{"value":NaN}',
  )

  assert response.status_code == 400
  assert response.json()["detail"] == "App service requests must contain JSON."


def test_public_service_failure_does_not_expose_app_diagnostics(client, auth, db):
  app = _service_app(db, access="public", slug="failing-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import sys\nprint('private app detail', file=sys.stderr)\nraise SystemExit(1)\n"
  )

  response = client.get("/api/app-services/failing-service/status")

  assert response.status_code == 502, response.text
  assert response.json()["detail"] == "App service failed."
  assert "private app detail" not in response.text
