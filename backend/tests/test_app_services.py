"""Accepted app services own policy; the platform owns their hard boundary."""

import asyncio
import gc
import json
import logging
import os
import random
from pathlib import Path
import signal
import sys
import time
import tracemalloc
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import app_services, auth as auth_tokens, models
from app.applied_app_runtime import runtime_parent
from app.config import get_settings


def test_capabilities_document_cross_lane_concurrency():
  contract = (Path(__file__).parents[2] / "CAPABILITIES.md").read_text()
  assert "Private and public requests use separate serialized lanes" in contract
  assert "the app must\nprovide its own file or database locking" in contract


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["app", "global"])
async def test_queued_service_pins_runtime_until_cancelled(monkeypatch, blocked_at):
  class Gate(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  gate = Gate()
  events = []
  monkeypatch.setattr(app_services, "_app_slots", {
    (1, "private"): gate if blocked_at == "app" else asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": gate if blocked_at == "global" else asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})

  def pin(_app_id):
    events.append("pinned")
    return SimpleNamespace(close=lambda: events.append("released"))

  monkeypatch.setattr(app_services, "hold_runtime", pin)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {}))
  try:
    await asyncio.wait_for(gate.waiting.wait(), timeout=1)
    assert events == ["pinned"], "queued old-runtime requests must prevent a false drain verdict"
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert events == ["pinned", "released"]


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
  monkeypatch.setattr(app_services, "_app_slots", {(1, "private"): busy})
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  def entered(app, _contract):
    assert app.id == 2
    raise HTTPException(418, "second app admitted")

  monkeypatch.setattr(app_services, "service_entry", entered)
  first = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {}))
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="second app admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=2, slug="test"), None, {}), timeout=1,
      )
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["app", "global"])
async def test_public_callback_does_not_queue_behind_private_request(monkeypatch, blocked_at):
  class BusyPrivateLane(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  busy = BusyPrivateLane()
  monkeypatch.setattr(app_services, "_app_slots", {
    (1, "private"): busy if blocked_at == "app" else asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": busy if blocked_at == "global" else asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  def entered(app, _contract):
    assert app.id == 1
    raise HTTPException(418, "public callback admitted")

  monkeypatch.setattr(app_services, "service_entry", entered)
  private = asyncio.create_task(
    app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {"public": False}),
  )
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="public callback admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {"public": True}),
        timeout=1,
      )
  finally:
    private.cancel()
    await asyncio.gather(private, return_exceptions=True)


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
  monkeypatch.setattr(app_services, "service_environment", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(
    close=lambda: released.append(processes[0].returncode),
  ))
  monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {}))
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


@pytest.mark.asyncio
async def test_a_second_cancel_cannot_interrupt_stopping_a_running_service(
  monkeypatch, tmp_path,
):
  entry = tmp_path / "service.py"
  entry.write_text("import time; time.sleep(60)")
  processes, released = [], []
  stopping, finish_stop = asyncio.Event(), asyncio.Event()
  real_spawn, real_stop = asyncio.create_subprocess_exec, app_services._stop_process

  async def spawn(*args, **kwargs):
    processes.append(await real_spawn(*args, **kwargs))
    return processes[-1]

  async def gated_stop(process, *tasks):
    stopping.set()
    await finish_stop.wait()
    await real_stop(process, *tasks)

  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "service_entry", lambda *a: entry)
  monkeypatch.setattr(app_services, "service_environment", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(
    close=lambda: released.append(processes[0].returncode),
  ))
  monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
  monkeypatch.setattr(app_services, "_stop_process", gated_stop)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {}))
  try:
    while not processes:
      await asyncio.sleep(.01)
    await asyncio.sleep(.05)  # Past admission, into execution.
    task.cancel("request gone")
    await asyncio.wait_for(stopping.wait(), timeout=3)
    task.cancel()  # A shutdown cancels the stop itself.
    await asyncio.sleep(.02)
    assert not task.done() and not released
    finish_stop.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, timeout=3)
    assert released and released[0] is not None, "runtime released before child was reaped"
  finally:
    finish_stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for process in processes:
      if process.returncode is None:
        os.killpg(process.pid, signal.SIGKILL)
      await process.wait()


@pytest.mark.asyncio
async def test_cancel_wins_over_a_spawn_that_then_fails_unexpectedly(monkeypatch, tmp_path, caplog):
  started, finish_spawn, released = asyncio.Event(), asyncio.Event(), []

  async def failing_spawn(*args, **kwargs):
    started.set()
    await finish_spawn.wait()
    raise ValueError("bad spawn argument")

  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "service_entry", lambda *a: tmp_path / "service.py")
  monkeypatch.setattr(app_services, "service_environment", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(
    close=lambda: released.append(True),
  ))
  monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_spawn)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1, slug="test"), None, {}))
  await asyncio.wait_for(started.wait(), timeout=3)
  task.cancel("request gone")
  await asyncio.sleep(0)
  assert not task.done() and not released
  finish_spawn.set()
  with pytest.raises(asyncio.CancelledError, match="^request gone$"):
    await asyncio.wait_for(task, timeout=3)
  assert released
  assert "failed to start" in caplog.text and "bad spawn argument" in caplog.text


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
  "headers": {"Content-Language": "en"},
}))
'''


def _service_app(
  db, *, access="self", slug="service-test", service_id=None, aliases=(),
  service_bytes=SERVICE, max_bytes=8 * 1024 * 1024, route_limits=None,
):
  source = Path(get_settings().data_dir) / "apps" / slug
  source.mkdir(parents=True)
  service = {
    "entry": "service.py",
    "access": access,
    "protocol": "json-v1",
    "max_request_bytes": max_bytes,
    "max_response_bytes": max_bytes,
  }
  if route_limits is not None:
    service["route_limits"] = route_limits
  app = models.App(
    name="Service test",
    slug=slug,
    description="",
    source_dir=str(source),
    jsx_source="export default () => null",
    capability_contract={"schema": 6, "service": service},
    service_id=service_id,
  )
  db.add(app)
  db.commit()
  for alias in aliases:
    db.add(models.AppServiceAlias(service_id=alias, app_id=app.id))
  db.commit()
  revision = "a" * 64
  accepted = runtime_parent(app.id) / revision
  accepted.mkdir(parents=True)
  (accepted / "service.py").write_bytes(service_bytes)
  app.runtime_revision = revision
  db.commit()
  return app


def test_service_request_body_uses_accepted_limit_before_buffering(client, auth, db, monkeypatch):
  from app.routes import app_services as routes
  app = _service_app(db, max_bytes=1024)
  seen = []
  original = routes.read_capped_body

  async def tracked(request, limit, **kwargs):
    seen.append(limit)
    return await original(request, limit, **kwargs)

  monkeypatch.setattr(routes, "read_capped_body", tracked)
  denied = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    content=b'"' + b'x' * 1024 + b'"',
  )
  assert denied.status_code == 413
  assert seen == [1024]
  allowed = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert allowed.status_code == 201


def test_service_default_eight_mib_still_rejects_oversize_body(client, auth, db):
  app = _service_app(db)
  denied = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    content=b'"' + b'x' * (8 * 1024 * 1024) + b'"',
  )
  assert denied.status_code == 413


def test_reviewed_service_accepts_request_above_default_eight_mib(client, auth, db):
  service = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({"body": {"length": len(request["body"]["payload"])}}))
'''
  app = _service_app(db, service_bytes=service, max_bytes=60 * 1024 * 1024)
  response = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    json={"payload": "x" * (8 * 1024 * 1024)},
  )
  assert response.status_code == 200
  assert response.json() == {"length": 8 * 1024 * 1024}


def test_binary_response_budget_includes_base64_envelope(client, auth, db):
  service = b'''import base64, json, sys
json.load(sys.stdin)
print(json.dumps({"body_base64": base64.b64encode(b"x" * 1536).decode(), "media_type": "image/gif"}))
'''
  small = _service_app(db, slug="binary-small", service_bytes=service, max_bytes=2048)
  large = _service_app(db, slug="binary-large", service_bytes=service, max_bytes=4096)
  denied = client.get(f"/api/apps/{small.id}/service/echo", headers=auth)
  allowed = client.get(f"/api/apps/{large.id}/service/echo", headers=auth)
  assert denied.status_code == 503
  assert allowed.status_code == 200
  assert allowed.content == b"x" * 1536


@pytest.mark.asyncio
async def test_preload_and_tool_lane_receive_the_reviewed_output_budget(db, auth, monkeypatch):
  from app import service_preload
  app = _service_app(db, max_bytes=12 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  seen = []
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, _request, **kwargs):
    seen.append(kwargs["max_stdout"])
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  status, body, _headers, _media = await app_services.invoke_service(
    app, owner, {"actor": {}, "public": False}, lane="tools",
  )
  assert (status, body, seen) == (200, {"ok": True}, [12 * 1024 * 1024])


@pytest.mark.asyncio
async def test_dense_tool_request_serialization_uses_bounded_buffer(db, auth, monkeypatch):
  import tracemalloc
  from app import service_preload

  app = _service_app(db, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": [0] * 200_000, "caption": "café"}
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, request, **kwargs):
    # Input allocation is excluded; serialization must not retain one object
    # per JSON token. Leave headroom for encoding and normal invocation setup.
    _current, peak = tracemalloc.get_traced_memory()
    assert peak < len(request) * 8
    assert json.loads(request) == envelope
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  tracemalloc.start()
  try:
    status, body, _headers, _media = await app_services.invoke_service(
      app, owner, envelope, lane="tools",
    )
  finally:
    tracemalloc.stop()
  assert (status, body) == (200, {"ok": True})


@pytest.mark.asyncio
async def test_tool_lane_rejects_oversize_request_before_execution(db, auth, monkeypatch):
  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {"arguments": {"payload": "x" * 1024}}},
      lane="tools",
    )
  assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_oversize_nested_tool_string_rejects_without_scalar_copy_or_execution(
  db, auth, monkeypatch,
):
  import tracemalloc

  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": {"arguments": {"nested": ["\"" * (8 * 1024 * 1024)]}}}
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope, lane="tools")
    _current, peak = tracemalloc.get_traced_memory()
  finally:
    tracemalloc.stop()
  assert caught.value.status_code == 413
  assert peak < 2 * 1024 * 1024, "oversize scalar was escaped and UTF-8 encoded before admission"


@pytest.mark.asyncio
@pytest.mark.parametrize("scalar", ["\x00" * 512, "\"" * 512, "é" * 512])
async def test_tool_string_escape_or_utf8_expansion_counts_toward_limit(
  db, auth, monkeypatch, scalar,
):
  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {scalar: "ok"}}, lane="tools",
    )
  assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_valid_nested_tool_json_bytes_and_invalid_data_errors_are_unchanged(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": {"café\"": ["\x00" * 20, "雪" * 20]}}
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, request, **kwargs):
    assert request == json.dumps(
      envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  status, body, _headers, _media = await app_services.invoke_service(
    app, owner, envelope, lane="tools",
  )
  assert (status, body) == (200, {"ok": True})
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {"bad": object()}}, lane="tools",
    )
  assert caught.value.status_code == 400


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
  assert response.headers["content-language"] == "en"
  assert response.json() == {
    "method": "POST",
    "path": "review/decision",
    "query": {"state": ["ready", "fresh"]},
    "body": {"record": "abc"},
    "scope": "owner",
    "app_slug": None,
  }


SERVICE_ENV = b'''import json, os, sys
json.load(sys.stdin)
print(json.dumps({
  "status": 200,
  "body": {
    "provider_env": sorted(
      key for key in ("DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
      if key in os.environ
    ),
  },
}))
'''


def test_only_authenticated_services_receive_provider_credentials(
  client, auth, db, monkeypatch,
):
  monkeypatch.setenv("DATA_DIR", "/data")
  monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/data/cli-auth/claude")
  monkeypatch.setenv("CODEX_HOME", "/data/cli-auth/codex")
  private = _service_app(
    db, slug="recall-navigator", service_bytes=SERVICE_ENV,
  )
  public = _service_app(
    db, access="public", slug="public-echo", service_bytes=SERVICE_ENV,
  )
  everything = ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "DATA_DIR"]

  # A private service the owner reaches may run a provider CLI, so it receives
  # the same credential locations as its scheduled job.
  private_call = client.post(
    f"/api/apps/{private.id}/service/echo", headers=auth, json={},
  )
  assert private_call.json()["provider_env"] == everything

  # An anonymous caller of a public service gets none of them.
  anonymous_call = client.post("/api/app-services/public-echo/echo", json={})
  assert anonymous_call.json()["provider_env"] == []

  # Nor does the owner's own authenticated call to a publicly reachable service:
  # the credentials follow the reviewed access, not the current caller.
  owner_call = client.post(
    f"/api/apps/{public.id}/service/echo", headers=auth, json={},
  )
  assert owner_call.json()["provider_env"] == []


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


def test_public_service_uses_stable_identity_without_leaving_slug_alias(
  client, auth, db,
):
  _service_app(
    db, access="public", slug="renamed-product", service_id="stable-social",
  )

  stable = client.get("/api/app-services/stable-social/status")
  mutable_slug = client.get("/api/app-services/renamed-product/status")

  assert stable.status_code == 201, stable.text
  assert mutable_slug.status_code == 404


def test_public_service_routes_only_explicit_transition_aliases(
  client, auth, db,
):
  app = _service_app(
    db, access="public", slug="renamed-product", service_id="social",
    aliases=("common",),
  )

  canonical = client.get("/api/app-services/social/status")
  transition = client.get("/api/app-services/common/status")
  product_slug = client.get("/api/app-services/renamed-product/status")

  assert canonical.status_code == 201, canonical.text
  assert transition.status_code == 201, transition.text
  assert product_slug.status_code == 404
  db.query(models.AppServiceAlias).filter_by(app_id=app.id).delete()
  db.commit()
  assert client.get("/api/app-services/common/status").status_code == 404


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


def test_service_headers_outside_the_allowlist_never_reach_the_response(client, auth, db):
  # A service answers on the shell origin, so a header such as Set-Cookie,
  # Clear-Site-Data, or NEL/Report-To would act on the owner's whole session
  # rather than on this one response. Only the allowlisted names pass.
  app = _service_app(db, slug="header-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import json\nprint(json.dumps({\"headers\":{"
    "\"Set-Cookie\":\"x=y\",\"NEL\":\"{}\",\"Report-To\":\"{}\","
    "\"Service-Worker-Allowed\":\"/\",\"X-Anything\":\"1\","
    "\"ETag\":\"\\\"v1\\\"\",\"Content-Disposition\":\"inline\"}}))\n"
  )

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 200
  for name in ("set-cookie", "nel", "report-to", "service-worker-allowed", "x-anything"):
    assert name not in response.headers
  assert response.headers["etag"] == '"v1"'
  assert response.headers["content-disposition"] == "inline"


@pytest.mark.parametrize("name", [
  "Set-Cookie", "Clear-Site-Data", "Refresh", "Service-Worker-Allowed",
  "NEL", "Report-To", "Content-Type", "Content-Length", "Transfer-Encoding",
])
def test_service_cannot_set_session_transport_or_origin_wide_headers(name):
  assert app_services._response_headers({name: "*"}, public=True) == {}


@pytest.mark.parametrize("value", [
  "public, max-age=60", "max-age=60, s-maxage=600", "max-age=60, must-revalidate",
])
def test_authenticated_service_response_is_never_shared_cacheable(value):
  # A shared cache may store a response to an authorized request only when
  # the response opts in (RFC 9111 section 3.5). An owner-authenticated service
  # response must not.
  assert app_services._response_headers({"Cache-Control": value}, public=False) == {}
  assert app_services._response_headers({"Cache-Control": value}, public=True) == {
    "Cache-Control": value,
  }


@pytest.mark.parametrize("value", ["no-store", "private, max-age=60", "no-cache"])
def test_authenticated_service_keeps_private_cache_policy(value):
  assert app_services._response_headers({"Cache-Control": value}, public=False) == {
    "Cache-Control": value,
  }


def test_service_rejects_a_malformed_allowed_header():
  with pytest.raises(ValueError):
    app_services._response_headers({"ETag": "a\r\nSet-Cookie: x=y"}, public=True)


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


def test_handled_service_failure_retains_bounded_diagnostics(
  client, auth, db, caplog,
):
  app = _service_app(db, slug="diagnostic-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import json,sys\n"
    "print('private traceback detail', file=sys.stderr)\n"
    "print(json.dumps({\"status\":500,\"body\":{\"detail\":\"safe\"}}))\n"
  )

  with caplog.at_level(logging.WARNING, logger="app.app_services"):
    response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 500
  assert response.json() == {"detail": "safe"}
  assert "private traceback detail" not in response.text
  assert "private traceback detail" in caplog.text


@pytest.mark.parametrize('route', ['numeric', 'named'])
@pytest.mark.parametrize('caller_kind', ['app', 'owner', 'delegated-owner'])
def test_authenticated_routes_preserve_the_same_complete_actor(
  client, auth, db, route, caller_kind,
):
  from app.deps import Principal, get_principal
  from app.main import app as server

  target = _service_app(db, access='public', slug='stable-actor')
  accepted = runtime_parent(target.id) / target.runtime_revision
  (accepted / 'service.py').write_text(
    'import json,sys\nr=json.load(sys.stdin)\n'
    'print(json.dumps({"status":200,"body":r["actor"]}))\n'
  )
  owner = db.query(models.Owner).one()
  headers = auth
  expected = {'scope':'owner', 'app_id':None, 'app_slug':None, 'delegated':False,
              'access':'write'}
  if caller_kind == 'app':
    token = auth_tokens.create_app_token(
      target.id, owner.username, owner.token_epoch, app_nonce=target.token_nonce,
    )
    headers = {'Authorization':f'Bearer {token}'}
    expected.update(scope='app', app_id=target.id, app_slug=target.slug)
  elif caller_kind == 'delegated-owner':
    from fastapi import Depends
    from app.database import get_db

    def delegated_principal(session=Depends(get_db)):
      return Principal(owner=session.query(models.Owner).one(), app_id=None,
                       delegation_id='fixture-delegation')

    server.dependency_overrides[get_principal] = delegated_principal
    # No delegation row stands behind this bearer, so it may only read.
    expected.update(delegated=True, access='read')
  path = (f'/api/apps/{target.id}/service/actor' if route == 'numeric'
          else '/api/services/stable-actor/actor')
  try:
    response = client.post(path, headers=headers, json={'actor':{'scope':'owner'}})
    assert response.status_code == 200, response.text
    assert response.json() == expected
  finally:
    if caller_kind == 'delegated-owner':
      server.dependency_overrides.pop(get_principal, None)


SERVICE_TOKEN = b'''import json, os, sys
json.load(sys.stdin)
print(json.dumps({"status": 200, "body": {"token": os.environ["APP_TOKEN"]}}))
'''


def test_public_invocation_cannot_spend_on_the_owners_providers(client, auth, db):
  app = _service_app(
    db, access="public", slug="public-token", service_bytes=SERVICE_TOKEN,
  )
  anonymous = client.post("/api/app-services/public-token/echo", json={})
  public = {"Authorization": f"Bearer {anonymous.json()['token']}"}

  # The anonymous visitor's request may not start or drive the owner's agents,
  # nor read what only the app's own authenticated backend may.
  assert client.post("/api/app-chats", headers=public, json={}).status_code == 403
  assert client.post(
    "/api/app-chats/start", headers=public, json={"scope": "x", "prompt": "hi"},
  ).status_code == 403
  assert client.get("/api/app-chats", headers=public).status_code == 403
  assert client.get(
    f"/api/apps/{app.id}/secrets/key", headers=public,
  ).status_code == 403
  # It keeps the few reads and notices a public service is reviewed to use.
  assert client.get("/api/apps/", headers=public).status_code == 200
  assert client.post(
    "/api/notifications/send", headers=public, json={"title": "t", "body": "b"},
  ).status_code == 200

  # The owner's own call to the same service carries the app's full authority.
  owner_call = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth, json={},
  )
  private = {"Authorization": f"Bearer {owner_call.json()['token']}"}
  assert client.get("/api/app-chats", headers=private).status_code == 200


@pytest.mark.parametrize(
  "path", ["tools/log", "/tools/log", "//tools/log", "./tools/log", "x/../tools/log"],
)
def test_http_callers_cannot_reach_the_platforms_tool_lane(client, auth, db, path):
  app = _service_app(db, slug="tool-forge")
  response = client.post(
    f"/api/apps/{app.id}/service/{path}", headers=auth,
    json={"arguments": {}, "call": {"chat_id": "forged"}},
  )
  assert response.status_code == 404


# --- Public request allowance --------------------------------------------------

def _public_manifest(**service) -> dict:
  return {
    "id": "live-room", "name": "Live room", "version": "0.1.0",
    "description": "A multiplayer room.", "entry": "index.jsx",
    "permissions": {}, "source_files": ["service.py"],
    "service": {"entry": "service.py", "access": "public", **service},
  }


def test_public_service_may_declare_a_capped_request_allowance():
  from app.app_capabilities import contract_from_manifest
  from app.manifest_contract import ManifestContractError, validate_manifest_contract

  manifest = _public_manifest(public_requests_per_minute=480)
  validate_manifest_contract(manifest)
  service = contract_from_manifest(manifest)["service"]
  assert service["public_requests_per_minute"] == 480
  assert app_services.public_requests_per_minute(service) == 480
  assert app_services.public_requests_per_minute({"access": "public"}) == 60

  for bad in (59, 601, 120.0, True, "120"):
    with pytest.raises(ManifestContractError, match="public_requests_per_minute"):
      validate_manifest_contract(_public_manifest(public_requests_per_minute=bad))
  private = _public_manifest(public_requests_per_minute=120)
  private["service"]["access"] = "self"
  with pytest.raises(ManifestContractError, match="only to a service with `access: public`"):
    validate_manifest_contract(private)


def test_public_door_counts_each_caller_per_service_against_its_allowance():
  from app.routes import app_services as routes

  def request(host):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={}, scope={})

  for _ in range(60):
    routes._admit_public_request(request("198.51.100.7"), "9101", 60)
  with pytest.raises(HTTPException) as refused:
    routes._admit_public_request(request("198.51.100.7"), "9101", 60)
  assert refused.value.status_code == 429
  assert int(refused.value.headers["Retry-After"]) >= 1

  # Another service keeps its own count, and may have accepted a larger allowance.
  for _ in range(300):
    routes._admit_public_request(request("198.51.100.7"), "9102", 300)
  with pytest.raises(HTTPException):
    routes._admit_public_request(request("198.51.100.7"), "9102", 300)
  # Another caller is counted apart.
  routes._admit_public_request(request("198.51.100.8"), "9101", 60)


def test_public_door_counts_probes_of_non_public_services(client, db, monkeypatch):
  from limits.storage import MemoryStorage
  from limits.strategies import FixedWindowRateLimiter
  from app.routes import app_services as routes

  monkeypatch.setattr(routes, "_public_window", FixedWindowRateLimiter(MemoryStorage()))
  _service_app(db, slug="private-probe")
  for _ in range(30):
    assert client.get("/api/app-services/private-probe/status").status_code == 404
    assert client.get("/api/app-services/missing-probe/status").status_code == 404
  assert client.get("/api/app-services/private-probe/status").status_code == 429


@pytest.fixture
def fresh_public_door(monkeypatch):
  from limits.storage import MemoryStorage
  from limits.strategies import FixedWindowRateLimiter
  from app.routes import app_services as routes

  monkeypatch.setattr(routes, "_public_window", FixedWindowRateLimiter(MemoryStorage()))
  lookups = []
  resolve = routes._service_app
  monkeypatch.setattr(
    routes, "_service_app", lambda session, slug: lookups.append(slug) or resolve(session, slug),
  )
  return lookups


def _set_public_allowance(db, app, per_minute):
  contract = dict(app.capability_contract)
  contract["service"] = {**contract["service"], "public_requests_per_minute": per_minute}
  app.capability_contract = contract
  db.commit()


def test_public_door_refuses_over_limit_callers_and_probes_without_a_lookup(
  client, auth, db, fresh_public_door,
):
  _service_app(db, access="public", slug="busy-service")
  for _ in range(60):
    assert client.get("/api/app-services/busy-service/status").status_code == 201
  for index in range(60):
    assert client.get(f"/api/app-services/probe-{index}/status").status_code == 404
  assert fresh_public_door == ["busy-service"] * 60

  fresh_public_door.clear()
  for index in range(20):
    refused = client.get("/api/app-services/busy-service/status")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert client.get(f"/api/app-services/late-probe-{index}/status").status_code == 429
  assert fresh_public_door == []


def test_public_service_applied_after_probe_exhaustion_uses_its_own_bucket(
  client, auth, db, fresh_public_door,
):
  for index in range(61):
    client.get(f"/api/app-services/probe-{index}/status")
  assert client.get("/api/app-services/probe-x/status").status_code == 429

  _service_app(db, access="public", slug="fresh-service")
  assert client.get("/api/app-services/fresh-service/status").status_code == 201


def test_raised_public_allowance_applies_to_the_next_request(
  client, auth, db, fresh_public_door,
):
  app = _service_app(db, access="public", slug="growing-service")
  for _ in range(60):
    assert client.get("/api/app-services/growing-service/status").status_code == 201
  assert client.get("/api/app-services/growing-service/status").status_code == 429

  _set_public_allowance(db, app, 600)
  assert client.get("/api/app-services/growing-service/status").status_code == 201


def test_withdrawn_public_service_stops_resolving_at_once(
  client, auth, db, fresh_public_door,
):
  app = _service_app(db, access="public", slug="closing-service")
  assert client.get("/api/app-services/closing-service/status").status_code == 201

  contract = dict(app.capability_contract)
  contract["service"] = {**contract["service"], "access": "self"}
  app.capability_contract = contract
  db.commit()
  fresh_public_door.clear()
  assert client.get("/api/app-services/closing-service/status").status_code == 404
  assert fresh_public_door == []


def _stale_snapshot(entries):
  """What another process's unseen change leaves: a current-looking snapshot."""
  import time as clock
  from app import public_service_index

  with public_service_index._lock:
    public_service_index._snapshot = public_service_index.Snapshot(
      public_service_index._generation, clock.monotonic(), dict(entries), set(),
    )


def test_stale_snapshot_never_decides_the_accepted_cap(
  client, auth, db, fresh_public_door,
):
  from app import public_service_index

  app = _service_app(db, access="public", slug="lowered-service")
  for _ in range(60):
    assert client.get("/api/app-services/lowered-service/status").status_code == 201

  # Another process lowered the cap from 600; this process still has 600 cached.
  _stale_snapshot({"lowered-service": (str(app.id), 600)})
  assert client.get("/api/app-services/lowered-service/status").status_code == 429
  assert public_service_index.lookup(db, "lowered-service")[0] == (str(app.id), 60)


def test_moved_alias_is_charged_only_to_its_current_service(
  client, auth, db, fresh_public_door,
):
  from app import public_service_index

  former = _service_app(db, access="public", slug="former-service")
  current = _service_app(db, access="public", slug="current-service", aliases=("moved",))
  _stale_snapshot({
    "former-service": (str(former.id), 60),
    "current-service": (str(current.id), 60),
    "moved": (str(former.id), 60),
  })

  assert client.get("/api/app-services/moved/status").status_code == 201
  assert public_service_index.lookup(db, "moved")[0] == (str(current.id), 60)
  # The stale entry spent nothing of the former service's allowance.
  for _ in range(60):
    assert client.get("/api/app-services/former-service/status").status_code == 201
  assert client.get("/api/app-services/former-service/status").status_code == 429
  # The moved request counted once, against the service it reached.
  for _ in range(59):
    assert client.get("/api/app-services/current-service/status").status_code == 201
  assert client.get("/api/app-services/current-service/status").status_code == 429


def test_a_stale_exhausted_bucket_never_refuses_the_current_service(
  client, auth, db, fresh_public_door,
):
  former = _service_app(db, access="public", slug="spent-service")
  current = _service_app(db, access="public", slug="open-service", aliases=("moved-on",))
  for _ in range(60):
    assert client.get("/api/app-services/spent-service/status").status_code == 201

  # Another process moved the alias; this process still maps it to the spent bucket.
  _stale_snapshot({
    "spent-service": (str(former.id), 60),
    "open-service": (str(current.id), 60),
    "moved-on": (str(former.id), 60),
  })
  assert client.get("/api/app-services/moved-on/status").status_code == 201

  # Likewise a raised allowance the snapshot has not seen yet.
  _set_public_allowance(db, former, 600)
  _stale_snapshot({"spent-service": (str(former.id), 60)})
  assert client.get("/api/app-services/spent-service/status").status_code == 201


def test_an_unconfirmed_refusal_costs_one_lookup_per_snapshot(
  client, auth, db, fresh_public_door,
):
  app = _service_app(db, access="public", slug="steady-service")
  for _ in range(60):
    assert client.get("/api/app-services/steady-service/status").status_code == 201

  _stale_snapshot({"steady-service": (str(app.id), 60)})
  fresh_public_door.clear()
  for _ in range(20):
    assert client.get("/api/app-services/steady-service/status").status_code == 429
  assert fresh_public_door == ["steady-service"]


def test_concurrent_stale_lookups_rebuild_the_snapshot_once(monkeypatch):
  import threading
  import time as clock
  from app import public_service_index

  builds = []
  started = threading.Event()

  def slow_build(_db):
    builds.append(1)
    started.set()
    clock.sleep(0.2)
    return {"svc": ("1", 60)}

  monkeypatch.setattr(public_service_index, "_build", slow_build)
  monkeypatch.setattr(public_service_index, "_snapshot", None)
  results = []
  threads = [
    threading.Thread(target=lambda: results.append(public_service_index.lookup(None, "svc")[0]))
    for _ in range(8)
  ]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()

  assert builds == [1]
  assert results == [("1", 60)] * 8


def test_a_late_stale_request_keeps_the_rebuilt_snapshot(monkeypatch):
  from app import public_service_index

  builds = []

  def build(_db):
    builds.append(1)
    return {"svc": ("1", 60)}

  monkeypatch.setattr(public_service_index, "_build", build)
  monkeypatch.setattr(public_service_index, "_snapshot", None)
  _, early = public_service_index.lookup(None, "svc")
  _, late = public_service_index.lookup(None, "svc")
  assert late is early

  # The first request to find the snapshot stale discards it and the next
  # request rebuilds; the slower one holding the same old snapshot then
  # reports it stale too.
  public_service_index.discard(early)
  _, rebuilt = public_service_index.lookup(None, "svc")
  public_service_index.discard(late)

  assert public_service_index.lookup(None, "svc")[1] is rebuilt
  assert builds == [1, 1]


def test_http_dense_container_decode_peak_includes_input_allocation(
  client, auth, db, monkeypatch,
):
  import tracemalloc

  app = _service_app(db, max_bytes=60 * 1024 * 1024)
  real_decode = app_services.ServiceExchangeAdmission.decode
  measurements = []

  async def decoded_only(_app, _owner, envelope, **_kwargs):
    # Measure the actual HTTP materialization boundary, not the encoder.
    measurements.append(tracemalloc.get_traced_memory()[1])
    return 200, {"count": len(envelope["body"])}, {}, None

  monkeypatch.setattr(app_services, "invoke_service", decoded_only)
  # Reproduce the predecessor's decode at the same HTTP boundary, with only
  # 2.1 MB of input (never a dense 60 MiB/OOM trial).
  async def predecessor_decode(_self, raw):
    return json.loads(raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", predecessor_decode)
  tracemalloc.start()
  try:
    raw = b"[" + b"[]," * 699_999 + b"[]]"
    before = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
    baseline = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert before.status_code == 200
  assert before.json() == {"count": 700_000}
  assert baseline > 35 * 1024 * 1024
  del raw
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", real_decode)
  tracemalloc.start()
  try:
    raw = b"[" + b"[]," * 699_999 + b"[]]"
    after = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
    repaired = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert after.status_code == 413
  assert len(measurements) == 1, "dense JSON reached invocation after the resource scan"
  assert repaired < 12 * 1024 * 1024, "decoded containers were allocated before resource rejection"
  assert app_services._admitted_exchange_cost == 0
  print(f"HTTP decode peak including input: before={baseline}, after={repaired} bytes")


class _BodyBuffer(bytearray):
  """Record the shared body reader's buffer copies and the cost admitted before each."""

  events: list = []

  def extend(self, chunk):
    _BodyBuffer.events.append(("extend", len(self) + len(chunk), app_services._admitted_exchange_cost))
    super().extend(chunk)

  def __bytes__(self):
    _BodyBuffer.events.append(("copy", len(self), app_services._admitted_exchange_cost))
    return bytes(memoryview(self))


@pytest.fixture
def body_buffer(monkeypatch):
  from app import storage_io

  _BodyBuffer.events = []
  monkeypatch.setattr(storage_io, "bytearray", _BodyBuffer, raising=False)
  return _BodyBuffer.events


@pytest.mark.parametrize("route", ["self", "shared", "public"])
def test_http_resource_admission_precedes_each_body_buffer_copy_for_every_route(
  client, auth, db, body_buffer, monkeypatch, route,
):
  app = _service_app(db, access="public")
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 1)
  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  response = client.post(path, headers=auth, content=b"{}")
  assert response.status_code == 413
  assert response.json()["detail"] == "App service request admission budget is exhausted."
  assert body_buffer == [], "request body was buffered before admission"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.parametrize("raw", [
  # Each exceeds its cap by at least one mark or level, nothing more.
  b"[" + b"{}," * (app_services.MAX_JSON_STRUCTURE // 2) + b"{}]",
  b"{" + b'"x":0,' * (app_services.MAX_JSON_STRUCTURE // 3) + b'"x":0}',
  b"[" * (app_services.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (app_services.MAX_JSON_DEPTH + 1),
], ids=["dense-objects", "duplicate-members", "nesting"])
def test_http_structural_limits_reject_before_stdlib_materialization(
  client, auth, db, monkeypatch, raw,
):
  app = _service_app(db)
  real_loads = json.loads

  def guarded_loads(value, *args, **kwargs):
    if isinstance(value, bytes) and value == raw:
      pytest.fail("HTTP input tree materialized")
    return real_loads(value, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 413
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "ascii"])
def test_http_resource_scan_preserves_stdlib_escaped_unicode_and_chunk_boundaries(
  client, auth, db, encoding,
):
  app = _service_app(db)
  # A backslash/escaped quote crosses the incremental scanner's chunk boundary.
  value = {"payload": "x" * 65_522 + '\\"{},[]:雪😀' * 30_000, "nested": [{"é": "\x00"}]}
  raw = json.dumps(value, ensure_ascii=encoding == "ascii").encode(encoding)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 201
  assert response.json()["body"] == value


@pytest.mark.parametrize("raw", [b'{"x":', b'"\\uZZZZ"', b'"\xff"', b'NaN', b'Infinity', b'{]'])
def test_http_malformed_json_stays_400_and_releases_admission(client, auth, db, raw):
  app = _service_app(db)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 400
  assert app_services._admitted_exchange_cost == 0


def test_http_decode_cost_rejects_before_materialization(client, auth, db, monkeypatch):
  app = _service_app(db)
  monkeypatch.setattr(app_services, "MAX_JSON_DECODE_COST", 1024)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=b'"' + b'x' * 2048 + b'"')
  assert response.status_code == 413
  assert app_services._admitted_exchange_cost == 0


def _nested(depth, leaf=0):
  for _ in range(depth):
    leaf = [leaf]
  return leaf


def _compact(value, encoding="utf-8", ensure_ascii=False):
  return json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":")).encode(encoding)


# Payloads an 8 MiB service accepted before structural admission existed. Each
# exceeds the predecessor's 262,144 marks or depth 64 and fits the budgets.
_LEGACY_DENSE_AND_DEEP = {
  "dense-ints": list(range(300_000)),
  "deep": _nested(70),
  "dense-literals": [True, False, None] * 100_000,
  "dense-empties": [[], {}] * 110_000,
  "wide-object": {f"k{i}": i for i in range(100_000)},
  "dense-unicode": ["é雪😀\n"] * 140_000,
}


@pytest.mark.parametrize("shape", sorted(_LEGACY_DENSE_AND_DEEP))
def test_http_legacy_dense_and_deep_payloads_round_trip_within_unchanged_budgets(
  client, auth, db, shape,
):
  value = _LEGACY_DENSE_AND_DEEP[shape]
  service = b'''import json, sys
print(json.dumps({"body": json.load(sys.stdin)["body"]}))
'''
  app = _service_app(db, access="public", service_bytes=service)
  raw = _compact(value)
  assert len(raw) <= 8 * MIB
  response = client.post(f"/api/app-services/{app.slug}/echo", content=raw)
  assert response.status_code == 200
  assert response.json() == value
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
@pytest.mark.parametrize("shape", ["dense-ints", "deep", "wide-object"])
async def test_materialized_legacy_payloads_reach_the_service_unchanged(
  db, auth, monkeypatch, caller, shape,
):
  from app import service_preload

  value = _LEGACY_DENSE_AND_DEEP[shape]
  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(_python, _entry, _environment, request, _timeout, _max_stdout):
    received = json.loads(request)["body"]
    assert (received["arguments"] if caller == "tool" else received) == value
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  if caller == "tool":
    status, result, _headers, _media = await app_services.invoke_service(app, owner, {
      "schema": 1, "method": "POST", "path": "/tools/echo", "query": {}, "headers": {},
      "body": {"arguments": value, "call": {}}, "public": False, "actor": {},
    }, lane="tools")
    assert status == 200
  else:
    result = await app_services.invoke_policy(app, owner, "echo", value)
  assert result == {"ok": True}
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
@pytest.mark.parametrize("excess", ["marks", "depth"])
async def test_materialized_envelope_past_the_structural_bounds_rejects_before_serialization(
  db, auth, monkeypatch, caller, excess,
):
  app = _service_app(db, max_bytes=60 * MIB)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(
    app_services, "_serialize_request", lambda *_: pytest.fail("request serialized"),
  )
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  # The envelope is depth 0, so its body nests exactly as an HTTP body does.
  body = (
    [0] * app_services.MAX_JSON_STRUCTURE if excess == "marks"
    else {"value": _nested(app_services.MAX_JSON_DEPTH)}
  )
  with pytest.raises(HTTPException) as caught:
    if caller == "tool":
      await app_services.invoke_service(app, owner, {
        "schema": 1, "method": "POST", "path": "/tools/echo", "query": {},
        "headers": {}, "body": body, "public": False, "actor": {},
      }, lane="tools")
    else:
      await app_services.invoke_policy(app, owner, "echo", body)
  assert caught.value.status_code == 413
  assert caught.value.detail == "App service request structural resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


# Lone surrogates parse from valid escaped JSON but have no UTF-8 form. Short
# strings are sized without encoding; past one scan slice they are encoded in
# slices, so the cut, the key position and either surrogate half are covered.
_SLICE = app_services._JSON_SCAN_BYTES
_LONE_SURROGATE_BODIES = {
  "short": {"value": "x\ud800"},
  "below-slice": {"value": "x" * (_SLICE - 1) + "\ud800"},
  "at-slice": {"value": "x" * _SLICE + "\ud800"},
  "past-slice-low": {"value": "x" * (_SLICE + 1) + "\udfff"},
  "slice-end": {"value": "x" * (_SLICE - 1) + "\ud800" + "x" * _SLICE},
  # Reversed halves stay two lone surrogates; JSON joins only a high-low pair.
  "reversed-pair-at-cut": {"value": "x" * (_SLICE - 1) + "\ude00\ud83d"},
  "dict-key": {"x" * (2 * _SLICE) + "\ud800": 1},
  "list-item": {"value": ["ok", "x" * (3 * _SLICE) + "\udc00"]},
}


@pytest.mark.parametrize("body", sorted(_LONE_SURROGATE_BODIES))
@pytest.mark.parametrize("access", ["private", "public"])
def test_http_lone_surrogate_body_is_invalid_data_at_any_length(
  client, auth, db, monkeypatch, access, body,
):
  app = _service_app(db, access="self" if access == "private" else "public")
  value = _LONE_SURROGATE_BODIES[body]
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  raw = json.dumps(value).encode("ascii")
  if access == "private":
    response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  else:
    response = client.post(f"/api/app-services/{app.slug}/echo", content=raw)
  assert response.status_code == 400
  assert response.json()["detail"] == "App service request contains invalid JSON data."
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", sorted(_LONE_SURROGATE_BODIES))
@pytest.mark.parametrize("caller", ["tool", "policy", "browser"])
async def test_materialized_lone_surrogate_is_invalid_data_at_any_length(
  db, auth, monkeypatch, caller, body,
):
  app = _service_app(db)
  owner = db.query(models.Owner).first()
  value = _LONE_SURROGATE_BODIES[body]
  grant_id = "b" * 32
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  with pytest.raises(HTTPException) as caught:
    if caller == "policy":
      await app_services.invoke_policy(app, owner, "echo", value)
    else:
      actor = {"browser_grant_id": grant_id} if caller == "browser" else {}
      await app_services.invoke_service(app, owner, {
        "schema": 1, "method": "POST", "path": "/tools/echo", "query": {},
        "headers": {}, "body": value, "public": False, "actor": actor,
      }, lane="tools")
  assert caught.value.status_code == 400
  assert caught.value.detail == "App service request contains invalid JSON data."
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("shift", [-1, 0, 1])
async def test_valid_wide_unicode_near_the_sizing_slice_still_sizes_and_serializes(shift):
  # Astral, BMP and escaped characters around the first slice cut are still
  # sized within the bound and serialize unchanged.
  for tail in ("😀", "雪", "\x00", "😀" * _SLICE):
    value = {"value": "x" * (_SLICE + shift) + tail, "k" * _SLICE + "😀": ["é" * (_SLICE + shift)]}
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.retain(value)
      request = await app_services._serialize_request(value, MIB)
      assert admission.encoded_bytes >= len(request)
      assert json.loads(request) == value
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_retained_depth_matches_the_lexical_scan_at_its_exact_bound():
  deepest = _nested(app_services.MAX_JSON_DEPTH)
  with app_services.ServiceExchangeAdmission(MIB) as admission:
    body = await admission.decode(_compact(deepest))
    await admission.retain({"body": body})
    assert admission.request_cost > 0
  with pytest.raises(HTTPException):
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.retain({"body": [deepest]})
  assert app_services._admitted_exchange_cost == 0


async def _number_work_verdicts(value):
  """The HTTP (lexical) and materialized (retain) verdicts for one compact body."""
  raw = _compact(value)
  verdicts = []
  for source in ("http", "materialized"):
    try:
      with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
        if source == "http":
          admission.admit_received(len(raw))
          await admission.decode(raw)
        else:
          await admission.retain(value)
    except HTTPException as exc:
      verdicts.append(exc.detail)
    else:
      verdicts.append("accepted")
  assert app_services._admitted_exchange_cost == 0
  return verdicts


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", [
  (list(range(300_000)), "accepted"),
  (list(range(500_000)), "structural"),
  # Each 24-character float measured up to 18.6 small-int elements to parse.
  ([-1.2345678901234567e-300 * (i + 1) for i in range(10_000)], "accepted"),
  ([-1.2345678901234567e-300 * (i + 1) for i in range(40_000)], "structural"),
  # 4300 digits is the stdlib's limit; each measured about 1,300 elements.
  ([int("9" * 4300)] * 50, "accepted"),
  ([int("9" * 4300)] * 400, "structural"),
], ids=["300k-ints", "500k-ints", "10k-long-floats", "40k-long-floats", "50-4300-digit-ints", "400-4300-digit-ints"])
async def test_compact_number_work_has_one_verdict_over_http_and_materialized(value, expected):
  verdicts = await _number_work_verdicts(value)
  if expected == "accepted":
    assert verdicts == ["accepted", "accepted"]
  else:
    assert verdicts == [
      "App service request structural resource limit exceeded.",
      "App service request structural resource limit exceeded.",
    ]


@pytest.mark.asyncio
async def test_redundant_number_text_costs_more_only_over_http():
  # Whitespace and longer spellings are parse work the raw text really holds;
  # the materialized value is charged on its compact form only.
  raw = b"[" + b"1.00000000000000000000e0 ,  " * 40_000 + b"1]"
  value = json.loads(raw)
  with pytest.raises(HTTPException):
    with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
      await admission.decode(raw)
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    await admission.retain(value)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [1, 2, 3, 2150, 4299])
async def test_number_split_across_scan_chunks_is_charged_as_one_token(monkeypatch, split):
  # A 4300-digit int is quadratic work; a chunk boundary inside it must not
  # turn it into two cheaper tokens. Halves would cost at least one unit less.
  token = b"9" * 4300
  raw = b"[" + b" " * (64 * 1024 - 1 - split) + token + b"]"
  assert raw[64 * 1024 - split:64 * 1024] == token[:split]
  units = 1 + len(raw) // app_services._JSON_BARE_CHARS_PER_UNIT + app_services._json_number_work(4300, app_services._JSON_INT)
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units)
  with app_services.ServiceExchangeAdmission(MIB) as admission:
    assert await admission.decode(raw) == [int(token)]
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units - 1)
  with pytest.raises(HTTPException) as caught:
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.decode(raw)
  assert caught.value.detail == "App service request structural resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_minus_sign_split_from_its_float_at_a_chunk_end_still_charges_the_whole_token(monkeypatch):
  token = b"-1.2345678901234567e-300"
  size = app_services._JSON_SCAN_BYTES
  raw = b"[" + b" " * (size - 2) + token + b"]"
  assert raw[size - 1:size] == b"-"
  units = 1 + len(raw) // app_services._JSON_BARE_CHARS_PER_UNIT
  units += app_services._json_number_work(len(token), app_services._JSON_EXPONENT)
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units)
  with app_services.ServiceExchangeAdmission(MIB) as admission:
    assert await admission.decode(raw) == [-1.2345678901234567e-300]
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units - 1)
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.decode(raw)
  assert refused.value.status_code == 413
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pressure", ["exhausted", "walk-fits-sizing-does-not"])
async def test_materialized_scalar_sizing_copies_only_after_admission(monkeypatch, pressure):
  body = {"body": "x" * (2 * app_services._JSON_SCAN_BYTES)}
  # Another exchange holds the rest of the budget.
  other = app_services.MAX_ADMITTED_EXCHANGE_COST - (0 if pressure == "exhausted" else 64 * 1024)
  monkeypatch.setattr(app_services, "_admitted_exchange_cost", other)
  real_encode = app_services.json.encoder.encode_basestring
  copied = []
  monkeypatch.setattr(
    app_services.json.encoder, "encode_basestring",
    lambda value: copied.append(len(value)) or real_encode(value),
  )
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      await admission.retain(body)
  assert refused.value.status_code == 413
  assert refused.value.detail == "App service request admission budget is exhausted."
  assert copied == [], "a sizing copy was made before its reservation"
  assert app_services._admitted_exchange_cost == other


@pytest.mark.asyncio
async def test_materialized_sizing_failure_after_copies_releases_its_reservation():
  # Measured slice by slice, the escaped form outgrows the ceiling midway.
  body = {"body": "\x00" * (app_services._JSON_SCAN_BYTES * 3)}
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(4 * app_services._JSON_SCAN_BYTES * 6 // 5) as admission:
      await admission.retain(body)
  assert refused.value.detail == "App service request is too large."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_browser_revocation_cancels_a_rendering_http_response_before_handoff(db, auth, monkeypatch):
  from app import service_preload
  from app.routes import app_services as routes

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "_actor_browser", lambda _: SimpleNamespace(grant_id="grant", session_id="session"))
  monkeypatch.setattr(app_services, "service_environment", lambda *_a, **_k: {})
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    return b'{"body":{"ok":true}}', b"", 0

  entered = asyncio.Event()

  async def paused_render(_value):
    entered.set()
    await asyncio.Event().wait()

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  monkeypatch.setattr(app_services, "render_json", paused_render)

  async def http_exchange():
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      result = await app_services.invoke_service(app, owner, {"actor": {}}, admission=admission)
      return await routes._response(*result)

  task = asyncio.create_task(http_exchange())
  await asyncio.wait_for(entered.wait(), 5)
  assert task in app_services._browser_calls["grant"]
  assert app_services.browser_grant_has_active_calls("grant")
  await app_services.cancel_browser_grant_calls("grant")
  assert task.cancelled()
  assert "grant" not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_tool_result_handoff_still_ends_its_browser_attribution(db, auth, monkeypatch):
  from app import service_preload

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "_actor_browser", lambda _: SimpleNamespace(grant_id="grant", session_id="session"))
  monkeypatch.setattr(app_services, "service_environment", lambda *_a, **_k: {})
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    assert app_services.browser_grant_has_active_calls("grant")
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  result = await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
  assert result[1] == {"ok": True}
  assert "grant" not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


_NEGATIVE_WORD = -123456789012345678


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["request", "response", "materialized"])
async def test_negative_machine_word_ints_are_accepted_in_every_direction(direction):
  # 18 digits and a sign still fit the parser's machine-word path.
  value = [_NEGATIVE_WORD] * 150_000
  raw = _compact(value)
  assert len(raw) < 8 * MIB
  with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
    if direction == "materialized":
      await admission.retain({"body": value})
    else:
      admission.admit_received(len(raw))
      decoded = await (admission.decode(raw) if direction == "request" else admission.decode_response(raw))
      assert decoded == value
  assert app_services._admitted_exchange_cost == 0


def _int_list_units(value):
  raw = _compact(value)
  work = sum(
    app_services._json_number_work(len(str(item)), app_services._JSON_INT, item < 0)
    for item in value
  )
  return raw, len(value) + len(raw) // app_services._JSON_BARE_CHARS_PER_UNIT + work


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
  _NEGATIVE_WORD, -_NEGATIVE_WORD, -1234567890123456789, 1234567890123456789,
  -int("9" * 40), int("9" * 40), -int("9" * 4300),
], ids=["negative-word", "positive-word", "negative-19", "positive-19", "negative-40", "positive-40", "negative-4300"])
@pytest.mark.parametrize("scan_bytes", [16 * 1024, 1, 2, 5, 10, 20])
async def test_int_work_is_the_same_for_every_chunk_split_and_at_its_exact_bound(monkeypatch, item, scan_bytes):
  # The sign costs its character only; digits decide the parse path. Every
  # split of the token across chunks gets the verdict of the whole token.
  raw, units = _int_list_units([item, item])
  monkeypatch.setattr(app_services, "_JSON_SCAN_BYTES", scan_bytes)
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units)
  with app_services.ServiceExchangeAdmission(MIB) as admission:
    assert await admission.decode(raw) == [item, item]
  monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units - 1)
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.decode(raw)
  assert refused.value.detail == "App service request structural resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [_NEGATIVE_WORD, -1234567890123456789, 1234567890123456789])
async def test_int_work_verdict_matches_over_http_and_materialized_at_the_bound(monkeypatch, item):
  value = [item] * 1000
  _raw, units = _int_list_units(value)
  for cap, expected in ((units, "accepted"), (units - 1, "App service request structural resource limit exceeded.")):
    monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", cap)
    assert await _number_work_verdicts(value) == [expected, expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [-int("9" * 4300), int("9" * 4300)])
async def test_costly_big_ints_are_still_refused_and_cancellation_releases_their_scan(item):
  raw = _compact([item] * 400)
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      await admission.decode(raw)
  assert refused.value.detail == "App service request structural resource limit exceeded."
  started = asyncio.Event()

  async def scan():
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      admission.admit_received(len(raw))
      started.set()
      await admission.decode(raw)

  task = asyncio.create_task(scan())
  await started.wait()
  await asyncio.sleep(0)
  task.cancel()
  await asyncio.gather(task, return_exceptions=True)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


# Ordinary numbers an 8 MiB service accepted before number work was charged:
# short and repr-length small exponents and 19-digit nanosecond timestamps.
_exponent_rng = random.Random(7)
_ORDINARY_EXPONENTS_AND_TIMESTAMPS = {
  "small-repr-exponents-30k": [10 ** _exponent_rng.uniform(-9, -4.1) for _ in range(30_000)],
  "short-exponents-60k": [float(f"{1 + i % 9}.{1 + i % 7}e-0{5 + i % 5}") for i in range(60_000)],
  "ns-timestamps-150k": [1697040000123456789 + i for i in range(150_000)],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_ORDINARY_EXPONENTS_AND_TIMESTAMPS))
@pytest.mark.parametrize("direction", ["request", "response", "materialized"])
async def test_ordinary_exponents_and_timestamps_are_accepted_in_every_direction(shape, direction):
  value = _ORDINARY_EXPONENTS_AND_TIMESTAMPS[shape]
  raw = _compact(value)
  assert len(raw) < 8 * MIB
  with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
    if direction == "materialized":
      await admission.retain({"body": value})
    else:
      admission.admit_received(len(raw))
      decoded = await (admission.decode(raw) if direction == "request" else admission.decode_response(raw))
      assert decoded == value
  assert app_services._admitted_exchange_cost == 0


def _token_work(token: str) -> float:
  shape = app_services._json_number_shape(token, 0, len(token))
  exponent = app_services._json_exponent_state(token, 0, len(token), app_services._JSON_NO_EXPONENT)
  return app_services._json_number_work(len(token), shape, token.startswith("-"), exponent)


@pytest.mark.parametrize("token,tier", [
  ("1.5e-05", "low"), ("-1.5E+05", "low"), ("1e0", "low"), ("1e9", "low"), ("1e22", "low"),
  ("1e23", "high"), ("-1e+99", "high"), ("1.23456789012345678e-91", "high"),
  ("1e-099", "full"), ("1e-0000009", "full"), ("1e100", "full"), ("1e-0100", "full"),
  ("1.2345678901234567e-9", "low"), ("1.2345678901234567890e-1", "low"),
  ("1.23456789012345678901e-1", "full"), ("1e", "full"),
], ids=lambda value: value)
def test_exponent_tier_follows_the_exponent_field_and_token_length(token, tier):
  # A one- or two-character exponent field (after its sign) in a token of at
  # most 24 characters is charged by its value; anything else, a padded
  # field included, keeps the costliest charge.
  expected = {
    "low": 3 + len(token) // 2, "high": 8 + len(token) // 2,
    "full": len(token) + 12 + max(0, len(token) - 24),
  }[tier]
  assert _token_work(token) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
  "1.5e-05", "-1.5E+05", "1e-099", "-9.999999999999999e+0099", "1e100",
  "1.2345678901234567e-9", "1.23456789012345678901e-1", "-1234567890123456789", "9" * 83,
])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-32-be"])
async def test_every_chunk_cut_of_a_number_gets_the_whole_tokens_verdict(monkeypatch, token, encoding):
  # A small scan chunk puts every cut position of the token on a boundary.
  scan_bytes = 8 * len("x".encode(encoding))
  monkeypatch.setattr(app_services, "_JSON_SCAN_BYTES", scan_bytes)
  for pad in range(8):
    text = "[" + " " * pad + token + "]"
    raw = text.encode(encoding)
    units = 1 + len(text) // app_services._JSON_BARE_CHARS_PER_UNIT + _token_work(token)
    monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units)
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      assert await admission.decode(raw) == [json.loads(token)]
    monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", units - 1)
    with pytest.raises(HTTPException):
      with app_services.ServiceExchangeAdmission(MIB) as admission:
        await admission.decode(raw)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
  1.5e-05, -1.5e05, 1e-99, -9.999999999999999e99, 1e100, 1.2345678901234567e-9,
  1697040000123456789, -1697040000123456789,
])
async def test_compact_number_charge_is_the_same_over_http_and_materialized(monkeypatch, value):
  items = [value] * 1000
  raw = _compact(items)
  units = len(items) + len(raw) // app_services._JSON_BARE_CHARS_PER_UNIT + 1000 * _token_work(json.dumps(value))
  for cap, expected in ((units, "accepted"), (units - 1, "App service request structural resource limit exceeded.")):
    monkeypatch.setattr(app_services, "MAX_JSON_STRUCTURE", cap)
    assert await _number_work_verdicts(items) == [expected, expected]


def test_number_classifier_matches_stdlib_for_fuzzed_tokens_and_every_split():
  rng = random.Random(53)
  for _ in range(3000):
    sign = rng.choice(["", "-"])
    mantissa = str(rng.randrange(1, 10 ** rng.randint(1, 20)))
    if rng.random() < 0.5 and len(mantissa) > 1:
      cut = rng.randrange(1, len(mantissa))
      mantissa = mantissa[:cut] + "." + mantissa[cut:]
    exponent = ""
    if rng.random() < 0.7:
      digits = str(rng.randrange(0, 10 ** rng.randint(1, 4)))
      exponent = rng.choice("eE") + rng.choice(["", "+", "-"]) + "0" * rng.randint(0, 3) + digits
    token = sign + mantissa + exponent
    value = json.loads(token)  # stdlib ground truth: valid, and its type
    shape = app_services._json_number_shape(token, 0, len(token))
    whole = app_services._json_exponent_state(token, 0, len(token), app_services._JSON_NO_EXPONENT)
    assert (shape == app_services._JSON_INT) == isinstance(value, int)
    if exponent:
      field = exponent[1:].lstrip("+-")
      assert app_services._json_exponent_value(whole) == (int(field) if len(field) <= 2 else None)
    else:
      assert whole == app_services._JSON_NO_EXPONENT
    for split in range(1, len(token)):
      head = app_services._json_exponent_state(token, 0, split, app_services._JSON_NO_EXPONENT)
      assert app_services._json_exponent_state(token, split, len(token), head) == whole
      assert max(
        app_services._json_number_shape(token, 0, split), app_services._json_number_shape(token, split, len(token)),
      ) == shape


def _resource_corpus():
  n = 40_000
  return {
    "small-ints": b"[" + b"0," * n + b"0]",
    "large-ints": _compact([2 ** 62 + i for i in range(n)]),
    "long-int": _compact(int("9" * 4000)),
    "floats": _compact([i + 0.5 for i in range(n // 4)]),
    "float-render-expansion": b"[" + b"1e15," * (n // 4) + b"1e15]",
    "literals": _compact([True, False, None] * (n // 3)),
    "empty-lists": b"[" + b"[]," * n + b"[]]",
    "empty-objects": b"[" + b"{}," * n + b"{}]",
    "one-member-objects": _compact([{"k": i} for i in range(n)]),
    "wide-object-distinct-keys": _compact({f"key{i}": i for i in range(n)}),
    "duplicate-keys": b"{" + b'"x":0,' * n + b'"x":0}',
    "deep-at-bound": _compact(_nested(app_services.MAX_JSON_DEPTH)),
    "nested-lists": _compact([[i] for i in range(n)]),
    "ascii-strings": _compact([f"s{i}" for i in range(n)]),
    "escapes": _compact(['a"b\\c\n\t'] * n),
    "unicode-escapes": _compact(["é雪😀"] * n, ensure_ascii=True),
    "mixed-unicode": _compact(["é雪😀"] * n),
    "astral-then-escape": _compact("😀" * 100_000 + "\n"),
    "ascii-then-astral-escape": _compact("x" * 400_000 + "\n😀"),
    "bmp-then-astral": _compact("x" * 400_000 + "雪😀"),
    "astral-only": _compact("x" * 400_000 + "😀"),
    "emoji-every-chunk": _compact(["x" * 4000 + "雪😀"] * 100),
    "latin1": _compact("é" * 200_000),
    "utf-8-bom": b"\xef\xbb\xbf" + _compact({"items": ["QUJD" * 1000], "caption": "雪😀"}),
    "utf-16": _compact(["雪😀", "x"] * n, "utf-16"),
    "utf-16-le-no-bom": _compact(["雪😀", "x"] * n, "utf-16-le"),
    "utf-32": _compact(["é", 1.5] * (n // 4), "utf-32"),
    "base64-media-mixed-caption": _compact({"items": ["QUJD" * 250_000] * 2, "caption": "雪😀"}),
    "binary-with-unicode-metadata": _compact({
      "body_base64": "QUJD" * 250_000, "media_type": "image/gif", "headers": {"x-note": "雪😀"},
    }),
    "pretty-printed": json.dumps({"rows": [{"id": i, "v": [i]} for i in range(n // 4)]}, indent=4).encode(),
  }


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_resource_corpus()))
async def test_decode_estimates_cover_measured_allocation_peaks(shape):
  """Each phase's reservation bounds what it actually allocates.

  Raw bytes are excluded (transfer owns them). The measured decode is the
  real scan, Unicode copy and stdlib parse; retain() must cover the surviving
  tree; a response's estimate plus three stdout copies must cover the tree
  while it renders; rendering is byte-identical to Starlette's JSONResponse.
  """
  import gc
  import tracemalloc
  from starlette.responses import JSONResponse

  raw = _resource_corpus()[shape]
  # Exclude one-time codec module imports from the first measurement.
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    await admission.decode(raw)
  for direction in ("request", "response"):
    gc.collect()
    with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
      tracemalloc.start()
      try:
        value = await (admission.decode(raw) if direction == "request" else admission.decode_response(raw))
        decode_peak = tracemalloc.get_traced_memory()[1]
        # Scan chunks, the Unicode copy and parsing, never released early.
        estimate = admission.request_cost if direction == "request" else admission.response_cost
        assert decode_peak <= estimate, direction
        if direction == "request":
          await admission.retain(value)
          retained = admission.request_cost
        else:
          tracemalloc.reset_peak()
          rendered = await app_services.render_json(value)
          _current, render_peak = tracemalloc.get_traced_memory()
          assert render_peak <= estimate + 3 * len(raw)
      finally:
        tracemalloc.stop()
    print(f"{shape} {direction}: {len(raw)} bytes, decode peak {decode_peak} <= {estimate}")
    del value
  # The parsed tree alone, measured away from the event loop's own allocations.
  gc.collect()
  tracemalloc.start()
  try:
    value = json.loads(raw)
    tree = tracemalloc.get_traced_memory()[0]
  finally:
    tracemalloc.stop()
  assert tree <= retained
  assert rendered == JSONResponse(value).body
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("value", [
  {"a": [1, -0.0, 1e15, 5e-324, 1.7976931348623157e308, True, None], "é": "雪😀 \x00\"\\/"},
  "x" * 70_000 + "😀", [[]] * 20_000, int("9" * 4300), _nested(app_services.MAX_JSON_DEPTH),
], ids=["scalars", "wide-string", "dense", "long-int", "deep"])
def test_part_rendering_is_byte_identical_to_json_response(value):
  from starlette.responses import JSONResponse

  assert asyncio.run(app_services.render_json(value)) == JSONResponse(value).body


@pytest.mark.parametrize("value", [float("inf"), float("nan"), {"k": ["\ud800"]}])
def test_part_rendering_raises_what_json_response_raises(value):
  from starlette.responses import JSONResponse

  with pytest.raises(Exception) as default:
    JSONResponse(value)
  with pytest.raises(default.type):
    asyncio.run(app_services.render_json(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["request", "response"])
async def test_huge_wide_scalar_rejects_before_any_unicode_copy_or_parse(monkeypatch, direction):
  import tracemalloc

  # One astral character makes this string four bytes per character in the
  # text and in the parsed value: more than the decode budget holds.
  raw = _compact("x" * (58 * MIB) + "😀")
  real_loads = json.loads
  monkeypatch.setattr(app_services.json, "loads", lambda *_a, **_k: pytest.fail("parsed"))
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
        await (admission.decode(raw) if direction == "request" else admission.decode_response(raw))
    peak = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
    monkeypatch.setattr(app_services.json, "loads", real_loads)
  assert caught.value.detail.endswith("decoded resource limit exceeded.")
  # Only the reserved scan chunks existed; no wide text was joined.
  assert peak < len(raw) + 4 * MIB
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_parent_mixed_caption_gallery_fits_every_phase_of_both_directions():
  body = {"items": ["QUJD" * (7 * MIB)] * 2, "caption": "雪😀"}
  raw = _compact(body)
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    admission.admit_received(len(raw))
    value = await admission.decode(raw)
    assert value == body
    await admission.retain({"body": value})
    admission.admit_execution(60 * MIB)
    request = await app_services._serialize_request({"body": value}, 60 * MIB)
    assert json.loads(request)["body"] == body
    del request
    admission.admit_output(raw)
    response = await admission.decode_response(raw)
    assert await app_services.render_json(response) == raw
    assert admission.cost <= app_services.MAX_ADMITTED_EXCHANGE_COST
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_wide_request_serialization_is_reserved_before_its_part_is_built(db, auth, monkeypatch):
  import tracemalloc
  from app import service_preload

  app = _service_app(db, max_bytes=60 * MIB)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  envelope = {"actor": {}, "body": ["x" * (10 * MIB) + "😀"]}
  reserved = []
  real_serialize = app_services._serialize_request

  async def serialize(value, max_bytes):
    reserved.append(app_services._admitted_exchange_cost)
    tracemalloc.start()
    try:
      return await real_serialize(value, max_bytes)
    finally:
      reserved.append(tracemalloc.get_traced_memory()[1])
      tracemalloc.stop()

  async def reply(*_args):
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_serialize_request", serialize)
  monkeypatch.setattr(app_services, "_run_spawned", reply)
  await app_services.invoke_service(app, owner, envelope, lane="tools")
  total, peak = reserved
  # The retained envelope is already held; encoding needs its own allowance.
  assert total > 3 * 60 * MIB
  assert peak <= total
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cooperative_retain_runs_pinned_and_attributed(db, auth, monkeypatch):
  app = _service_app(db)
  owner = db.query(models.Owner).first()
  seen = []
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "hold_runtime", lambda _id: seen.append("pinned") or SimpleNamespace(
    close=lambda: seen.append("released"),
  ))
  async def retain(self, envelope):
    seen.append(("retain", asyncio.current_task() in app_services._browser_calls.get("grant", ())))
    raise HTTPException(413, "stop")

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "retain", retain)
  monkeypatch.setattr(app_services, "_actor_browser", lambda _actor: SimpleNamespace(grant_id="grant"))
  with pytest.raises(HTTPException):
    await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
  assert seen == ["pinned", ("retain", True), "released"]
  assert "grant" not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["scan", "retain"])
async def test_cancellation_while_chunks_or_retain_walk_are_held_releases_everything(stage):
  raw = _compact(["é雪😀" * 100] * 4000)
  value = json.loads(raw)
  started = asyncio.Event()

  async def run():
    with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
      admission.admit_received(len(raw))
      if stage == "scan":
        coro = admission.decode(raw)
      else:
        coro = admission.retain({"body": [value] * 60})
      started.set()
      await coro

  task = asyncio.create_task(run())
  await started.wait()
  for _ in range(3):
    await asyncio.sleep(0)
  assert app_services._admitted_exchange_cost > 0
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_http_render_cancellation_releases_the_held_response(db, auth, monkeypatch):
  import httpx
  from app.main import app as application

  app = _service_app(db, access="public")
  entered = asyncio.Event()

  async def blocked(_value):
    entered.set()
    await asyncio.Event().wait()

  monkeypatch.setattr(app_services, "render_json", blocked)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    task = asyncio.create_task(http.post(f"/api/app-services/{app.slug}/echo", json={"value": "ok"}))
    await asyncio.wait_for(entered.wait(), 10)
    assert app_services._admitted_exchange_cost > 0, "rendering ran outside the exchange reservation"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_actual_public_http_dense_payload_at_the_mark_bound_does_not_starve_heartbeat(db, auth):
  import httpx
  import time
  from app.main import app as application

  service = b'''import json, sys
print(json.dumps({"body": json.load(sys.stdin)["body"]}, separators=(",", ":")))
'''
  app = _service_app(db, access="public", service_bytes=service)
  # Each "5," is one mark plus two characters outside strings (1/12 mark);
  # the envelope adds a few dozen marks and the response wrapper three.
  value = [5] * ((app_services.MAX_JSON_STRUCTURE - 64) * 12 // 13)
  raw = _compact(value)
  beats = []
  running = True

  async def heartbeat():
    while running:
      beats.append(time.monotonic())
      await asyncio.sleep(0.01)

  beat = asyncio.create_task(heartbeat())
  try:
    await asyncio.sleep(0.02)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
      response = await http.post(f"/api/app-services/{app.slug}/echo", content=raw)
    await asyncio.sleep(0.02)
  finally:
    running = False
    await beat
  assert response.status_code == 200
  assert response.json() == value
  gap = max(b - a for a, b in zip(beats, beats[1:]))
  print(f"Actual public {len(raw)}-byte {len(value)}-element round trip maximum 10ms heartbeat gap: {gap:.4f}s")
  assert gap < 0.2, "a dense exchange monopolized the event loop"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.parametrize("shape", ["scalar", "original", "gallery"])
@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
def test_http_reviewed_media_allowance_preserves_twenty_mib_gif_and_gallery(
  client, auth, db, shape, public,
):
  import base64

  service = b'''import base64, hashlib, json, sys
body = json.load(sys.stdin)["body"]
items = [body] if isinstance(body, str) else body["items"]
print(json.dumps({"body": [{"size": len(base64.b64decode(item)), "sha256": hashlib.sha256(base64.b64decode(item)).hexdigest()} for item in items]}))
'''
  import hashlib
  app = _service_app(db, access="public", service_bytes=service, max_bytes=60 * 1024 * 1024)
  gif = b"GIF89a" + b"x" * (20 * 1024 * 1024 - 6)
  encoded = base64.b64encode(gif).decode("ascii")
  items = [encoded] * (2 if shape == "gallery" else 1)
  body = encoded if shape == "scalar" else {"items": items, "caption": "雪😀"}
  # ensure_ascii=False exercises the full Unicode text copy alongside base64.
  raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
  path = f"/api/app-services/{app.slug}/media" if public else f"/api/apps/{app.id}/service/media"
  response = client.post(path, headers=auth, content=raw)
  assert response.status_code == 200
  assert response.json() == [{"size": len(gif), "sha256": hashlib.sha256(gif).hexdigest()}] * len(items)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_queued_http_payloads_keep_their_envelope_admission_and_reject_new_growth(
  monkeypatch, body_buffer,
):
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  class Gate(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  gate = Gate()
  reads = []
  monkeypatch.setattr(app_services, "_app_slots", {(1, "private"): gate})
  monkeypatch.setattr(app_services, "service_contract", lambda *_a, **_k: {"max_request_bytes": 1024, "max_response_bytes": 1024})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  body = b'{"payload":"' + b'x' * 256 + b'"}'
  admissions = []

  async def http_call():
    async def receive():
      reads.append(True)
      return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
    with app_services.ServiceExchangeAdmission(1024) as admission:
      admissions.append(admission)
      envelope = await _envelope(request, "echo", public=False, actor={}, max_bytes=1024, admission=admission)
      return await app_services.invoke_service(SimpleNamespace(id=1, slug="service-test"), None, envelope, admission=admission)

  first = asyncio.create_task(http_call())
  try:
    await asyncio.wait_for(gate.waiting.wait(), 1)
    reserved = app_services._admitted_exchange_cost
    assert reserved > len(body), "queued decoded input was no longer counted"
    # Only the retained envelope is held while queued, not a transfer allowance.
    assert reserved == admissions[0].request_cost
    assert admissions[0].transfer_cost == 0
    monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", reserved + 3 * len(body) - 1)
    buffered = list(body_buffer)
    assert buffered[-1][:2] == ("copy", len(body))
    with pytest.raises(HTTPException) as caught:
      await http_call()
    assert caught.value.status_code == 413
    assert reads == [True, True]
    assert body_buffer == buffered, "rejected backlog request buffered its body"
    assert app_services._admitted_exchange_cost == reserved
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cancellation_while_reading_releases_admission():
  started = asyncio.Event()

  async def read():
    with app_services.ServiceExchangeAdmission(1024) as admission:
      admission.admit_received(1024)
      started.set()
      await asyncio.Event().wait()

  task = asyncio.create_task(read())
  await started.wait()
  assert app_services._admitted_exchange_cost == 3072
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
async def test_service_execution_timeout_releases_request_admission(
  db, auth, monkeypatch, backend,
):
  from app import service_preload

  app = _service_app(db, service_bytes=b'import time; time.sleep(60)')
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object() if backend == "preload" else None)

  async def timeout(*_args, **_kwargs):
    raise TimeoutError()

  monkeypatch.setattr(service_preload, "run", timeout)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {}, timeout_seconds=0.05)
  assert caught.value.status_code == 503
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_concurrent_http_backlog_is_bounded_before_execution_and_cleans_up(
  auth, db, monkeypatch,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=4096)
  waiting = asyncio.Event()

  class BusyApp(asyncio.Semaphore):
    async def acquire(self):
      waiting.set()
      return await super().acquire()

  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): BusyApp(0)})
  requests = []
  peaks = []
  real_reserve = app_services.ServiceExchangeAdmission._reserve

  def measured_reserve(self, cost, **kwargs):
    real_reserve(self, cost, **kwargs)
    peaks.append(app_services._admitted_exchange_cost)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "_reserve", measured_reserve)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    async def post():
      return await http.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={"payload": "x" * 256})

    requests.append(asyncio.create_task(post()))
    try:
      await asyncio.wait_for(waiting.wait(), 3)
      budget = 3 * app_services._admitted_exchange_cost
      monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", budget)
      requests.extend(asyncio.create_task(post()) for _ in range(7))
      # Let the real HTTP routes finish admission. Queued requests remain
      # asleep at the per-app execution semaphore, not at a new memory queue.
      for _ in range(100):
        if sum(task.done() for task in requests) >= 5:
          break
        await asyncio.sleep(0.01)
      done = [task for task in requests if task.done()]
      assert len(done) >= 5
      assert all(task.result().status_code == 413 for task in done)
      assert 1 <= sum(not task.done() for task in requests) <= 3
      assert max(peaks) <= budget
      assert app_services._admitted_exchange_cost <= budget
    finally:
      for task in requests:
        task.cancel()
      await asyncio.gather(*requests, return_exceptions=True)
  assert app_services._admitted_exchange_cost == 0


DENSE_RESPONSE_SERVICE = b'''import json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  json.load(sys.stdin)
  sys.stdout.write('{"body":[' + '[],' * 349_999 + '[]]}')
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
@pytest.mark.parametrize("lane", ["private", "public", "tools"])
async def test_dense_service_response_is_rejected_before_json_materialization(
  db, auth, monkeypatch, backend, lane,
):
  import sys
  import tracemalloc
  from app import service_preload

  app = _service_app(db, access="public", service_bytes=DENSE_RESPONSE_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="public")
  entry = app_services.service_entry(app, service)
  envelope = {"actor": {}, "public": lane == "public"}
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable, entry,
      app_services.service_environment(app, owner, service, public=lane == "public"),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  real_loads = json.loads

  def guarded_loads(raw, *args, **kwargs):
    if isinstance(raw, bytes) and raw.startswith(b'{"body":[[],[],[]'):
      pytest.fail("dense stdout materialized before the response resource check")
    return real_loads(raw, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope, lane=lane)
    peak = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
    await service_preload.shutdown()
  assert caught.value.status_code == 502
  assert "structural resource limit" in caught.value.detail
  assert peak < 8 * 1024 * 1024
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_spawned_response_decode_peak_includes_stdout_allocation(db, auth, monkeypatch):
  import tracemalloc
  from app import service_preload

  app = _service_app(db, service_bytes=DENSE_RESPONSE_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  real_decode = app_services.ServiceExchangeAdmission.decode_response
  async def predecessor_decode(_self, raw):
    return json.loads(raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", predecessor_decode)
  tracemalloc.start()
  try:
    result = await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
    before = tracemalloc.get_traced_memory()[1]
    assert len(result[1]) == 350_000
  finally:
    tracemalloc.stop()
  del result
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", real_decode)
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
    after = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert caught.value.status_code == 502
  assert before > 20 * 1024 * 1024
  assert after < 8 * 1024 * 1024
  assert app_services._admitted_exchange_cost == 0
  print(f"Spawned response decode peak including stdout: before={before}, after={after} bytes")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"body":', b'', b'{"body":NaN}', b'"\xff"'])
async def test_malformed_service_response_still_returns_502_and_releases_exchange(
  db, auth, monkeypatch, raw,
):
  from app import service_preload

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    return raw, b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {"actor": {}})
  assert caught.value.status_code == 502
  assert caught.value.detail == "App service returned invalid JSON."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_response_budget_adds_retained_request_before_parsing(db, auth, monkeypatch):
  from app import service_preload

  app = _service_app(db, max_bytes=4096)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  raw = b'{"body":"' + b'x' * 1000 + b'"}'
  envelope = {"actor": {}, "body": "x" * 1000}
  with app_services.ServiceExchangeAdmission(4096) as alone:
    alone.admit_output(raw)
    await alone.decode_response(raw)
    response_alone = alone.cost
  assert app_services._admitted_exchange_cost == 0

  async def reply(*_args):
    request_reserved = app_services._admitted_exchange_cost
    assert request_reserved > _execution_transfer(4096)
    retained_request = request_reserved - _execution_transfer(4096)
    # Either direction fits alone; their simultaneously retained costs do not.
    monkeypatch.setattr(
      app_services, "MAX_ADMITTED_EXCHANGE_COST", retained_request + response_alone - 1,
    )
    return raw, b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  real_loads = json.loads

  def guarded_loads(value, *args, **kwargs):
    if value is raw:
      pytest.fail("response parsed before joint admission")
    return real_loads(value, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, envelope)
  assert caught.value.status_code == 502
  assert "admission budget" in caught.value.detail
  assert app_services._admitted_exchange_cost == 0


def test_http_exchange_reservation_covers_simultaneous_envelopes_and_json_response(
  client, auth, db, monkeypatch,
):
  from app.routes import app_services as routes

  app = _service_app(db, max_bytes=4096)
  real_decode = app_services.ServiceExchangeAdmission.decode_response
  real_response = routes._response
  seen = []

  async def decode(self, raw):
    value = await real_decode(self, raw)
    seen.append((self.cost, self.request_cost, self.response_cost))
    # Three copies of the actual stdout replace the execution allowance.
    assert self.transfer_cost == 3 * len(raw) < 3 * self.max_bytes
    assert self.cost == self.transfer_cost + self.request_cost + self.response_cost
    assert self.request_cost > 0 and self.response_cost > 0
    return value

  def response(*args):
    assert app_services._admitted_exchange_cost == seen[0][0]
    result = real_response(*args)  # JSONResponse eagerly constructs its bytes.
    assert app_services._admitted_exchange_cost == seen[0][0]
    return result

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", decode)
  monkeypatch.setattr(routes, "_response", response)
  reply = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={"payload": "雪😀\\\"" * 10})
  assert reply.status_code == 201
  assert len(seen) == 1
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_tool_exchange_reservation_is_held_through_decoded_result_handoff(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    return b'{"body":{"ok":true}}', b"", 0

  real_exit = app_services.ServiceExchangeAdmission.__exit__
  seen = []

  def release(self, *args):
    assert self.response_cost > 0 and self.request_cost > 0
    assert app_services._admitted_exchange_cost == self.cost
    seen.append(self.cost)
    return real_exit(self, *args)

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "__exit__", release)
  result = await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
  assert result[:2] == (200, {"ok": True})
  assert len(seen) == 1
  assert app_services._admitted_exchange_cost == 0


DOWNLOAD_SERVICE = '''import base64, json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  mode = json.load(sys.stdin)["body"]["mode"]
  size = 20 * 1024 * 1024
  if mode == "binary":
    body = b"GIF89a" + b"x" * (size - 6)
    print(json.dumps({"body_base64": base64.b64encode(body).decode(), "media_type": "image/gif"}))
  else:
    suffix = '\\\\"{},[]:雪😀'
    body = "x" * (size - len(suffix)) + suffix
    print(json.dumps({"body": body}, ensure_ascii=mode == "escaped"))
'''.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,lane", [("spawn", "public"), ("preload", "tools")])
@pytest.mark.parametrize("shape", ["binary", "scalar", "escaped"])
async def test_twenty_mib_download_response_preserves_binary_unicode_and_escape_allowance(
  db, auth, monkeypatch, backend, lane, shape,
):
  import httpx
  import sys
  from app import service_preload
  from app.main import app as application

  app = _service_app(db, access="public", service_bytes=DOWNLOAD_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="public")
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable,
      app_services.service_entry(app, service),
      app_services.service_environment(app, owner, service, public=False),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  try:
    if lane == "public":
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
        result = await http.post(f"/api/app-services/{app.slug}/download", json={"mode": shape})
      assert result.status_code == 200
      body = result.content if shape == "binary" else result.json()
      assert result.headers["content-type"].startswith("image/gif" if shape == "binary" else "application/json")
    else:
      status, body, _headers, media = await app_services.invoke_service(
        app, owner, {"body": {"mode": shape}, "actor": {}}, lane="tools",
      )
      assert status == 200
      assert media == ("image/gif" if shape == "binary" else None)
    assert len(body) == 20 * 1024 * 1024
    if shape == "binary":
      assert body[:6] == b"GIF89a"
      assert body[6:] == b"x" * (len(body) - 6)
    else:
      assert body.startswith("x" * 1024)
      assert body.endswith('\\"{},[]:雪😀')
  finally:
    await service_preload.shutdown()
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cancel_during_partial_response_read_reaps_child_and_releases_exchange(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db, max_bytes=4096, service_bytes=b'''import json, sys, time
json.load(sys.stdin)
sys.stdout.write('{"body":[')
sys.stdout.flush()
time.sleep(60)
''')
  owner = db.query(models.Owner).first()
  started = asyncio.Event()
  processes = []
  real_spawn = asyncio.create_subprocess_exec
  real_read = app_services._read_bounded

  async def spawn(*args, **kwargs):
    process = await real_spawn(*args, **kwargs)
    processes.append(process)
    return process

  async def read(reader, limit):
    if limit == 4096:
      prefix = await reader.readexactly(9)
      started.set()
      return prefix + await real_read(reader, limit - len(prefix))
    return await real_read(reader, limit)

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
  monkeypatch.setattr(app_services, "_read_bounded", read)
  task = asyncio.create_task(app_services.invoke_service(app, owner, {"actor": {}}, lane="tools"))
  try:
    await asyncio.wait_for(started.wait(), 3)
    assert app_services._admitted_exchange_cost > 3 * 4096
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, 3)
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert processes and all(process.returncode is not None for process in processes)
  assert app_services._admitted_exchange_cost == 0


def test_http_response_construction_failure_releases_exchange(client, auth, db, monkeypatch):
  from app.routes import app_services as routes

  app = _service_app(db)

  def fail(*_args):
    assert app_services._admitted_exchange_cost > 0
    raise RuntimeError("response construction failed")

  monkeypatch.setattr(routes, "_response", fail)
  with pytest.raises(RuntimeError, match="response construction failed"):
    client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "shared", "public"])
@pytest.mark.parametrize("trickle", [False, True], ids=["stalled", "trickling"])
async def test_actual_http_body_deadline_is_total_and_releases_reservations(
  db, auth, monkeypatch, route, trickle,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * 1024 * 1024)
  assert routes.SERVICE_BODY_TIMEOUT_SECONDS == 120
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 0.08)
  started = asyncio.Event()
  cancelled = asyncio.Event()
  received = []

  async def body():
    try:
      started.set()
      received.append(True)
      yield b'"'
      while True:
        if trickle:
          await asyncio.sleep(0.01)
          received.append(True)
          yield b'x'
        else:
          await asyncio.Event().wait()
    finally:
      cancelled.set()

  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    response = await asyncio.wait_for(http.post(path, headers=auth, content=body()), 2)
  assert started.is_set() and cancelled.is_set()
  assert len(received) >= (2 if trickle else 1)
  assert response.status_code == 408
  assert response.json()["detail"] == "App service request body timed out."
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_stalled_public_http_cannot_monopolize_private_or_tool_admission(
  db, auth, monkeypatch, body_buffer,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 5)
  assert app_services.MAX_PUBLIC_INGRESS_COST == 192 * 1024 * 1024
  assert app_services.MAX_ADMITTED_EXCHANGE_COST == 512 * 1024 * 1024
  # A public read holds what it actually received, so saturate a scaled
  # sub-budget with real bytes rather than buffering 64 MiB in a test.
  received = 64 * 1024
  held = 3 * received
  monkeypatch.setattr(app_services, "MAX_PUBLIC_INGRESS_COST", held)
  started = asyncio.Event()
  cancelled = asyncio.Event()

  async def stalled():
    try:
      yield b'"' + b"x" * (received - 1)
      started.set()
      await asyncio.Event().wait()
    finally:
      cancelled.set()

  async def denied():
    yield b'{}'

  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    first = asyncio.create_task(http.post(f"/api/app-services/{app.slug}/echo", content=stalled()))
    try:
      await asyncio.wait_for(started.wait(), 2)
      for _ in range(100):
        if app_services._public_ingress_cost == held:
          break
        await asyncio.sleep(0.01)
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
      buffered = list(body_buffer)
      second = await http.post(f"/api/app-services/{app.slug}/echo", content=denied())
      assert second.status_code == 413
      assert "public ingress budget" in second.json()["detail"]
      assert body_buffer == buffered, "a denied public body was buffered"
      # Both real HTTP private execution and direct tool execution retain the
      # same 60 MiB grant while the anonymous read remains stalled.
      private = await asyncio.wait_for(http.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={}), 3)
      assert private.status_code == 201
      result = await asyncio.wait_for(app_services.invoke_service(
        app, owner, {"actor": {"scope": "owner"}, "body": {}, "path": "echo",
                     "method": "POST", "query": {}, "public": False}, lane="tools",
      ), 3)
      assert result[0] == 201
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
    finally:
      first.cancel()
      await asyncio.gather(first, return_exceptions=True)
  assert cancelled.is_set()
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_public_read_transfer_keeps_total_body_ownership(monkeypatch):
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  max_bytes = 4096
  body = b'{"value":"accepted"}'
  received = 3 * len(body)
  observed = []
  real_decode = app_services.ServiceExchangeAdmission.decode

  async def decode(self, raw):
    assert app_services._public_ingress_cost == 0
    assert app_services._admitted_exchange_cost == self.cost == received
    observed.append(raw)
    return await real_decode(self, raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", decode)

  async def receive():
    # Nothing is charged before a byte arrives: the header is not trusted.
    assert app_services._public_ingress_cost == app_services._admitted_exchange_cost == 0
    return {"type": "http.request", "body": body, "more_body": False}

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with app_services.ServiceExchangeAdmission(max_bytes, public_ingress=True) as admission:
    envelope = await _envelope(request, "echo", public=True, actor={}, max_bytes=max_bytes, admission=admission)
    assert envelope["body"] == {"value": "accepted"}
    assert app_services._admitted_exchange_cost == admission.cost > received
    assert app_services._public_ingress_cost == 0
    admission.finish_ingress()  # phase completion is idempotent
    assert app_services._admitted_exchange_cost == admission.cost
  assert observed == [b'{"value":"accepted"}']
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_public_disconnect_during_body_read_releases_both_budgets():
  from starlette.requests import ClientDisconnect, Request
  from app.routes.app_services import _envelope

  messages = iter([
    {"type": "http.request", "body": b'"', "more_body": True},
    {"type": "http.disconnect"},
  ])

  async def receive():
    return next(messages)

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with pytest.raises(ClientDisconnect):
    with app_services.ServiceExchangeAdmission(4096, public_ingress=True) as admission:
      await _envelope(request, "echo", public=True, actor={}, max_bytes=4096, admission=admission)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", [b'\\n', b'\\"', b'\\\\', b'\\u1234'])
async def test_actual_public_http_escape_heavy_scalar_does_not_starve_heartbeat(
  db, auth, escape,
):
  import httpx
  import time
  from app.main import app as application

  service = b'''import json, sys
value = json.load(sys.stdin)["body"]
print(json.dumps({"body": {"length": len(value)}}))
'''
  app = _service_app(db, access="public", service_bytes=service, max_bytes=60 * 1024 * 1024)
  raw = b'"' + escape * ((4 * 1024 * 1024) // len(escape)) + b'"'
  expected = len(json.loads(raw))
  beats = []
  running = True

  async def heartbeat():
    while running:
      beats.append(time.monotonic())
      await asyncio.sleep(0.01)

  beat = asyncio.create_task(heartbeat())
  try:
    await asyncio.sleep(0.02)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
      response = await http.post(f"/api/app-services/{app.slug}/echo", content=raw)
    await asyncio.sleep(0.02)
  finally:
    running = False
    await beat
  assert response.status_code == 200
  assert response.json() == {"length": expected}
  gap = max(b - a for a, b in zip(beats, beats[1:]))
  print(f"Actual public 4 MiB {escape!r} scalar maximum 10ms heartbeat gap: {gap:.4f}s")
  assert gap < 0.2, "bounded scanner monopolized the event loop for an escaped scalar"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["request", "response"])
async def test_cooperative_resource_scan_cancel_releases_owned_bytes(monkeypatch, direction):
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT

  class ObservedMatcher:
    def match(self, *args):
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  raw = b'"' + b'\\n' * 100_000 + b'"'

  async def decode():
    with app_services.ServiceExchangeAdmission(1024 * 1024) as admission:
      if direction == "request":
        admission.admit_received(len(raw))
        return await admission.decode(raw)
      admission.admit_output(raw)
      return await admission.decode_response(raw)

  task = asyncio.create_task(decode())
  try:
    await asyncio.wait_for(scanned.wait(), 1)
    assert not task.done()
    # Three transfer copies plus the scan's own reservation for the chunks
    # it holds and the next one it decodes.
    assert app_services._admitted_exchange_cost > 3 * len(raw)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_revocation_cancels_attributed_response_scan_before_handoff(db, auth, monkeypatch):
  from app import service_preload

  service = b'''import json, sys
json.load(sys.stdin)
sys.stdout.write('{"body":"' + '\\\\n' * 100_000 + '"}')
'''
  app = _service_app(db, service_bytes=service)
  owner = db.query(models.Owner).first()
  grant_id = "a" * 32
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT

  class ObservedMatcher:
    def match(self, *args):
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  task = asyncio.create_task(app_services.invoke_service(
    app, owner, {"actor": {"browser_grant_id": grant_id}}, lane="tools",
  ))
  try:
    await asyncio.wait_for(scanned.wait(), 3)
    assert app_services.browser_grant_has_active_calls(grant_id)
    assert not task.done()
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


MIB = 1024 * 1024


def _execution_transfer(max_bytes):
  return 3 * max_bytes + app_services._stdio_overhead(max_bytes)


def _route_limit(method, path, max_bytes):
  return {
    "method": method, "path": path,
    "max_request_bytes": max_bytes, "max_response_bytes": max_bytes,
  }


def test_route_ceiling_matches_one_canonical_once_decoded_path():
  service = {
    "max_request_bytes": 60 * MIB, "max_response_bytes": 60 * MIB,
    "route_limits": [
      _route_limit("GET", "/actor", MIB),
      _route_limit("POST", "/tools/log", 2048),
      _route_limit("POST", "/reviewer/manual-plan", 4096),
    ],
  }
  # HTTP delivers no leading slash, tools a leading slash, policies none.
  for path in ("actor", "/actor", "actor/", "//actor"):
    assert app_services.exchange_max_bytes(service, "GET", path) == MIB
  assert app_services.exchange_max_bytes(service, "POST", "/tools/log") == 2048
  assert app_services.exchange_max_bytes(service, "POST", "reviewer/manual-plan") == 4096
  # Wrong method, case, prefix, extension, an undecoded form, or a missing
  # selector all keep the whole reviewed grant: no broad or repeated matching.
  for method, path in (
    ("POST", "actor"), ("get", "actor"), ("GET", "Actor"), ("GET", "actor/x"),
    ("GET", "act"), ("GET", "a%63tor"), ("GET", "actor?x=1"),
    ("GET", "x/../actor"), (None, "actor"), ("GET", None),
  ):
    assert app_services.exchange_max_bytes(service, method, path) == 60 * MIB
  assert app_services.exchange_max_bytes({}, None, None) == 8 * MIB


def test_route_ceilings_are_bounded_by_the_exact_accepted_whole_grant():
  def whole(max_bytes, *limits):
    return {"max_request_bytes": max_bytes, "max_response_bytes": max_bytes,
            "route_limits": list(limits)}

  # A ceiling equal to its grant is valid, including at the platform maximum
  # and under the 8 MiB default of a service accepted without whole fields.
  exact = whole(1024, _route_limit("GET", "/actor", 1024))
  assert app_services.exchange_max_bytes(exact, "GET", "actor") == 1024
  top = whole(60 * MIB, _route_limit("GET", "/actor", 60 * MIB))
  assert app_services.exchange_max_bytes(top, "GET", "actor") == 60 * MIB
  legacy = {"route_limits": [_route_limit("GET", "/actor", 8 * MIB)]}
  assert app_services.exchange_max_bytes(legacy, "GET", "actor") == 8 * MIB

  for service in (
    whole(1024, _route_limit("GET", "/actor", 2048)),
    {"route_limits": [_route_limit("GET", "/actor", 8 * MIB + 1)]},
  ):
    with pytest.raises(HTTPException) as refused:
      app_services.exchange_max_bytes(service, "POST", "unrelated")
    assert (refused.value.status_code, refused.value.detail) == (
      503, "Accepted app service route limits are invalid.",
    )


@pytest.mark.parametrize(("request_bytes", "response_bytes"), [
  (1024.0, 1024.0), (True, True), (1024, 1024.0), (1024.0, 1024), (1024, True),
  (1024, 2048), (0, 0), (60 * MIB + 1, 60 * MIB + 1), ("1024", "1024"), (None, None),
  (1024, "omitted"), ("omitted", 1024),
])
@pytest.mark.parametrize("route_limits", ["omitted", [], [_route_limit("GET", "/actor", 1)]],
                         ids=["no-routes", "empty-routes", "routes"])
def test_malformed_accepted_whole_grant_disables_the_service(
  request_bytes, response_bytes, route_limits,
):
  from app.manifest_contract import accepted_service_max_bytes

  service = {}
  for key, value in (
    ("max_request_bytes", request_bytes), ("max_response_bytes", response_bytes),
    ("route_limits", route_limits),
  ):
    if value != "omitted":
      service[key] = value
  assert accepted_service_max_bytes(service) is None
  with pytest.raises(HTTPException) as refused:
    app_services.exchange_max_bytes(service, "GET", "actor")
  assert (refused.value.status_code, refused.value.detail) == (
    503, "Accepted app service transfer limit is invalid.",
  )


@pytest.mark.parametrize("route_limits", [
  {"GET /actor": 1024},
  [["GET", "/actor", 1024]],
  [{"method": "GET", "path": "/actor", "max_request_bytes": 1024}],
  [{**_route_limit("GET", "/actor", 1024), "extra": True}],
  [{**_route_limit("GET", "/actor", 1024), "max_response_bytes": 2048}],
  [_route_limit("GET", "/actor", 0)],
  [_route_limit("GET", "/actor", True)],
  [{**_route_limit("GET", "/actor", 1), "max_response_bytes": True}],
  [{**_route_limit("GET", "/actor", 1), "max_response_bytes": 1.0}],
  [_route_limit({}, [], 1024)],
  [_route_limit(None, "/actor", 1024)],
  [_route_limit("GET", "/actor", 1024.0)],
  [_route_limit("GET", "/actor", 8 * MIB + 1)],
  [_route_limit("get", "/actor", 1024)],
  [_route_limit("HEAD", "/actor", 1024)],
  [_route_limit("GET", "actor", 1024)],
  [_route_limit("GET", "/actor/", 1024)],
  [_route_limit("GET", "/a%2Fb", 1024)],
  [_route_limit("GET", "/", 1024)],
  [_route_limit("GET", "/actor", 1024), _route_limit("GET", "/actor", 512)],
  [_route_limit("GET", f"/r{index}", 1024) for index in range(17)],
], ids=lambda value: json.dumps(value)[:40])
def test_malformed_accepted_route_limits_disable_the_service_instead_of_widening(
  client, auth, db, monkeypatch, route_limits,
):
  from app.routes import app_services as routes

  app = _service_app(db, slug="malformed-routes", route_limits=route_limits)

  async def forbidden_read(*_args, **_kwargs):
    pytest.fail("a malformed accepted contract reached the body read")

  monkeypatch.setattr(routes, "read_capped_body", forbidden_read)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert response.status_code == 503
  assert response.json()["detail"] == "Accepted app service route limits are invalid."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "shared", "public"])
@pytest.mark.parametrize("framing", ["chunked", "understated-length"])
async def test_http_route_ceiling_is_reserved_and_enforced_before_body_decode(
  db, auth, monkeypatch, route, framing,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  cap = 4096
  app = _service_app(
    db, access="public", max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/activity", cap)],
  )
  reads = []
  decoded = []
  grown = []
  executions = []
  real_read = routes.read_capped_body
  real_decode = app_services.ServiceExchangeAdmission.decode
  real_grow = app_services.ServiceExchangeAdmission.admit_received
  real_execute = app_services.ServiceExchangeAdmission.admit_execution

  async def tracked_read(request, limit, **kwargs):
    reads.append((
      limit, app_services._admitted_exchange_cost,
      app_services._public_ingress_cost, request.headers.get("content-length"),
    ))
    return await real_read(request, limit, **kwargs)

  async def tracked_decode(self, raw):
    decoded.append(len(raw))
    return await real_decode(self, raw)

  def tracked_grow(self, size):
    real_grow(self, size)
    grown.append((size, app_services._public_ingress_cost))

  def tracked_execute(self, max_bytes):
    real_execute(self, max_bytes)
    executions.append((max_bytes, self.transfer_cost))

  monkeypatch.setattr(routes, "read_capped_body", tracked_read)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", tracked_decode)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_received", tracked_grow)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  url = {
    "self": f"/api/apps/{app.id}/service/activity",
    "shared": f"/api/services/{app.slug}/activity",
    "public": f"/api/app-services/{app.slug}/activity",
  }[route]
  headers = dict(auth)
  if framing == "understated-length":
    headers["content-length"] = "2"

  def body(size):
    payload = b'"' + b"x" * (size - 2) + b'"'

    async def chunks():
      for start in range(0, len(payload), 1000):
        yield payload[start:start + 1000]

    return chunks()

  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    over = await http.post(url, headers=headers, content=body(cap + 1))
    exact = await http.post(url, headers=headers, content=body(cap))
    small = await http.post(url, headers=auth, json={})
  assert over.status_code == 413
  assert over.json()["detail"] == "App service request is too large."
  # cap+1 bytes never reach the decoder; exactly cap bytes pass the read (its
  # envelope then exceeds the same ceiling at serialization, also 413).
  assert decoded == [cap, 2]
  assert exact.status_code == 413
  assert small.status_code == 201
  assert [read[0] for read in reads] == [cap, cap, cap]
  # Nothing is reserved before bytes arrive; growth charges only received
  # bytes, never beyond the route ceiling, and execution reserves that ceiling.
  assert all(read[1] == read[2] == 0 for read in reads)
  assert max(size for size, _public in grown) == cap
  assert all(public == (3 * size if route == "public" else 0) for size, public in grown)
  # Stdio headroom dominates encoding these small envelopes, but the route's
  # ceiling, not the service's 60 MiB maximum, must still govern both calls.
  assert executions == [(cap, _execution_transfer(cap))] * 2
  if framing == "understated-length":
    assert [read[3] for read in reads[:2]] == ["2", "2"]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_tool_and_policy_requests_use_their_route_ceiling_before_execution(
  db, auth, monkeypatch,
):
  from app import service_preload

  cap = 2048
  app = _service_app(db, max_bytes=60 * MIB, route_limits=[
    _route_limit("POST", "/tools/log", cap),
    _route_limit("POST", "/reviewer/validate-comment", cap),
  ])
  owner = db.query(models.Owner).first()
  executed = []

  async def reply(_python, _entry, _environment, request, _timeout, max_stdout):
    executed.append((json.loads(request)["path"], max_stdout, app_services._admitted_exchange_cost))
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "_run_spawned", reply)

  def tool(path, text):
    return {
      "schema": 1, "method": "POST", "path": path, "query": {}, "headers": {},
      "body": {"arguments": {"text": text}, "call": {}}, "public": False, "actor": {},
    }

  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, tool("/tools/log", "x" * cap), lane="tools")
  assert caught.value.status_code == 413
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_policy(app, owner, "reviewer/validate-comment", {"body": "x" * cap})
  assert caught.value.status_code == 413
  assert executed == []

  await app_services.invoke_service(app, owner, tool("/tools/log", "ok"), lane="tools")
  await app_services.invoke_policy(app, owner, "reviewer/validate-comment", {"body": "ok"})
  await app_services.invoke_service(app, owner, tool("/tools/other", "x" * cap), lane="tools")
  assert [(path, limit) for path, limit, _cost in executed] == [
    ("/tools/log", cap), ("reviewer/validate-comment", cap), ("/tools/other", 60 * MIB),
  ]
  for _path, limit, cost in executed:
    # Three serialized copies of the selected ceiling, the running process's
    # stdio overhead and the small envelope.
    assert _execution_transfer(limit) <= cost < _execution_transfer(limit) + 64 * 1024
  assert app_services._admitted_exchange_cost == 0


ROUTE_OUTPUT_SERVICE = b'''import base64, json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  body = json.load(sys.stdin)["body"]
  if body["binary"]:
    print(json.dumps({"body_base64": base64.b64encode(b"x" * body["size"]).decode(), "media_type": "image/gif"}))
  else:
    print(json.dumps({"body": "x" * body["size"]}))
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
@pytest.mark.parametrize("binary", [False, True], ids=["json", "binary"])
async def test_route_ceiling_bounds_actual_service_output_on_both_backends(
  db, auth, monkeypatch, backend, binary,
):
  import sys
  from app import service_preload

  cap = 64 * 1024
  app = _service_app(
    db, service_bytes=ROUTE_OUTPUT_SERVICE, max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/tools/thumbnail", cap)],
  )
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="self")
  limits = []
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable,
      app_services.service_entry(app, service),
      app_services.service_environment(app, owner, service, public=False),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
    real_run = service_preload.run

    async def run(*args, **kwargs):
      limits.append(kwargs["max_stdout"])
      return await real_run(*args, **kwargs)

    monkeypatch.setattr(service_preload, "run", run)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
    real_spawned = app_services._run_spawned

    async def spawned(*args):
      limits.append(args[5])
      return await real_spawned(*args)

    monkeypatch.setattr(app_services, "_run_spawned", spawned)

  def envelope(path):
    return {
      "schema": 1, "method": "POST", "path": path, "query": {}, "headers": {},
      "body": {"size": cap, "binary": binary}, "public": False, "actor": {},
    }

  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope("/tools/thumbnail"), lane="tools")
    # The existing serialized-output limit outcome, now at the route ceiling.
    assert caught.value.status_code == 503
    assert caught.value.detail == "App service exceeded its execution limits."
    status, body, _headers, media = await app_services.invoke_service(
      app, owner, envelope("/tools/original"), lane="tools",
    )
  finally:
    await service_preload.shutdown()
  assert status == 200
  assert len(body) == cap
  assert media == ("image/gif" if binary else None)
  assert limits == [cap, 60 * MIB]
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_route_ceiling_bounds_decoded_binary_response(db, auth, monkeypatch):
  import base64
  from app import service_preload

  cap = 1024
  app = _service_app(db, max_bytes=60 * MIB, route_limits=[_route_limit("GET", "/thumb", cap)])
  owner = db.query(models.Owner).first()
  # A compact envelope with a decoded body one byte over the route ceiling.
  raw = json.dumps({"body_base64": base64.b64encode(b"x" * (cap + 1)).decode(), "media_type": "image/gif"}).encode()

  async def reply(*_args):
    return raw, b"", 0

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "_run_spawned", reply)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {"method": "GET", "path": "thumb", "actor": {}})
  assert caught.value.status_code == 502
  assert caught.value.detail == "App service returned too much binary data."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_declared_control_routes_coexist_with_a_stalled_undeclared_public_read(
  db, auth, monkeypatch,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * MIB, route_limits=[
    _route_limit("GET", "/actor", MIB),
    _route_limit("POST", "/activity", MIB),
  ])
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 5)
  started = asyncio.Event()
  cancelled = asyncio.Event()
  executions = []
  started_services = []
  real_execute = app_services.ServiceExchangeAdmission.admit_execution
  real_entry = app_services.service_entry

  def tracked_execute(self, max_bytes):
    executions.append(max_bytes)
    real_execute(self, max_bytes)

  def tracked_entry(*args):
    started_services.append(True)
    return real_entry(*args)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  monkeypatch.setattr(app_services, "service_entry", tracked_entry)
  received = 64 * 1024

  async def stalled():
    try:
      yield b'"' + b"x" * (received - 1)
      started.set()
      await asyncio.Event().wait()
    finally:
      cancelled.set()

  held = 3 * received
  # Leave room for a control route's whole allowance, not a media grant's.
  monkeypatch.setattr(
    app_services, "MAX_ADMITTED_EXCHANGE_COST", held + _execution_transfer(MIB) + MIB,
  )
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    first = asyncio.create_task(http.post(f"/api/app-services/{app.slug}/echo", content=stalled()))
    try:
      await asyncio.wait_for(started.wait(), 2)
      for _ in range(100):
        if app_services._public_ingress_cost == held:
          break
        await asyncio.sleep(0.01)
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
      actor = await asyncio.wait_for(http.get(f"/api/app-services/{app.slug}/actor"), 3)
      activity = await asyncio.wait_for(
        http.post(f"/api/app-services/{app.slug}/activity", json={"type": "Like"}), 3,
      )
      assert (actor.status_code, actor.json()["method"], actor.json()["path"]) == (201, "GET", "actor")
      assert (activity.status_code, activity.json()["body"]) == (201, {"type": "Like"})
      assert executions == [MIB, MIB], "a declared control route reserved more than its ceiling"
      # The route ceiling is enforced, not just reserved.
      oversize = await http.post(
        f"/api/app-services/{app.slug}/activity", content=b'"' + b"x" * MIB + b'"',
      )
      assert oversize.status_code == 413
      assert oversize.json()["detail"] == "App service request is too large."
      # Wrong method and unmatched routes keep the whole 60 MiB grant, which
      # the remaining budget cannot hold: a retryable busy answer, no process.
      for method, path in (("GET", "activity"), ("POST", "actor"), ("POST", "inbox")):
        denied = await http.request(method, f"/api/app-services/{app.slug}/{path}", content=b"{}")
        assert denied.status_code == 503
        assert denied.headers["retry-after"] == "1"
      assert executions == [MIB, MIB, 60 * MIB, 60 * MIB, 60 * MIB]
      assert len(started_services) == 2
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
    finally:
      first.cancel()
      await asyncio.gather(first, return_exceptions=True)
  assert cancelled.is_set()
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_revocation_cancels_a_route_capped_exchange_and_releases_its_reservation(
  db, auth, monkeypatch,
):
  from app import service_preload

  service = b'''import json, sys
json.load(sys.stdin)
sys.stdout.write('{"body":"' + '\\\\n' * 100_000 + '"}')
'''
  cap = 512 * 1024
  app = _service_app(
    db, service_bytes=service, max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/tools/feed", cap)],
  )
  owner = db.query(models.Owner).first()
  grant_id = "b" * 32
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT
  reserved = []
  executed = []
  real_execute = app_services.ServiceExchangeAdmission.admit_execution

  def tracked_execute(self, max_bytes):
    real_execute(self, max_bytes)
    executed.append(app_services._admitted_exchange_cost)

  class ObservedMatcher:
    def match(self, *args):
      reserved.append(app_services._admitted_exchange_cost)
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  task = asyncio.create_task(app_services.invoke_service(app, owner, {
    "method": "POST", "path": "/tools/feed", "actor": {"browser_grant_id": grant_id},
  }, lane="tools"))
  try:
    await asyncio.wait_for(scanned.wait(), 3)
    # Execution reserved the route ceiling; the scan holds the actual output.
    assert 3 * cap <= executed[0] < 3 * MIB
    assert 3 * 200_000 < reserved[0] < 3 * cap
    assert app_services.browser_grant_has_active_calls(grant_id)
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


# Phase-aware exchange admission: a queued exchange holds only its retained
# envelope, a body read is charged for bytes actually received before each
# buffer copy, and the whole transfer allowance is reserved inside the
# execution slot before serialization or any process starts.


class _ParkedLane(asyncio.Semaphore):
  """An execution slot that counts the exchanges queued on it."""

  def __init__(self, value=0):
    super().__init__(value)
    self.queued = 0
    self.changed = asyncio.Event()

  async def acquire(self):
    self.queued += 1
    self.changed.set()
    return await super().acquire()

  async def wait_queued(self, count):
    while self.queued < count:
      self.changed.clear()
      await asyncio.wait_for(self.changed.wait(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway", ["shared", "same-app"])
async def test_queued_bodyless_gets_hold_only_their_envelopes_and_all_complete(
  auth, db, monkeypatch, gateway,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=60 * MIB, service_id="queued-social", route_limits=[
    _route_limit("GET", "/actor", MIB), _route_limit("POST", "/activity", MIB),
  ])
  lane = _ParkedLane()
  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  route = "/api/services/queued-social" if gateway == "shared" else f"/api/apps/{app.id}/service"
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    calls = [
      asyncio.create_task(http.get(f"{route}/board-media/post-{n}/0?thumbnail=true", headers=auth))
      for n in range(4)
    ]
    try:
      while lane.queued < 4:
        refused = [task.result().status_code for task in calls if task.done()]
        assert refused == [], f"a queued read was refused instead of waiting: {refused}"
        await asyncio.sleep(0.01)
      held = app_services._admitted_exchange_cost
      # Four undeclared 60 MiB-grant reads wait on one serialized lane while
      # holding only their small envelopes, not 4 x 180 MiB of allowance.
      assert 0 < held < 64 * 1024
      assert app_services._public_ingress_cost == 0
      lane.release()
      replies = await asyncio.wait_for(asyncio.gather(*calls), 30)
    finally:
      for task in calls:
        task.cancel()
      await asyncio.gather(*calls, return_exceptions=True)
  assert [reply.status_code for reply in replies] == [201] * 4
  assert [reply.json()["path"] for reply in replies] == [
    f"board-media/post-{n}/0" for n in range(4)
  ]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_burst_of_tiny_responses_never_needs_a_second_transfer_allowance(
  auth, db, monkeypatch,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=60 * MIB)
  # One execution allowance plus room for small envelopes and responses: a
  # finished or queued exchange must not keep a media-sized reservation.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", _execution_transfer(60 * MIB) + MIB)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    replies = await asyncio.wait_for(asyncio.gather(*(
      http.get(f"/api/apps/{app.id}/service/thumb/{n}", headers=auth) for n in range(5)
    )), 30)
  assert [reply.status_code for reply in replies] == [201] * 5
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_finished_tiny_responses_release_their_transfer_allowance_before_handoff(
  db, auth, monkeypatch,
):
  from app import service_preload

  cap = 60 * MIB
  app = _service_app(db, max_bytes=cap)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", _execution_transfer(cap) + MIB)
  executed = []

  async def reply(*_args):
    executed.append(app_services._admitted_exchange_cost)
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  release = asyncio.Event()
  handed_off = []

  async def exchange(n):
    # The HTTP routes' shape: the reservation is held through response handoff.
    with app_services.ServiceExchangeAdmission(cap) as admission:
      result = await app_services.invoke_service(app, owner, {
        "method": "GET", "path": f"thumb/{n}", "actor": {}, "public": False,
      }, admission=admission)
      handed_off.append(admission.cost)
      await release.wait()
      return result

  parked = []
  try:
    for n in range(4):
      parked.append(asyncio.create_task(exchange(n)))
      while len(handed_off) <= n:
        assert not parked[-1].done(), parked[-1].result()
        await asyncio.sleep(0)
    # Each later call ran while every earlier response was still awaiting
    # handoff, because those hold only their envelope and tiny response.
    assert len(handed_off) == 4 and all(cost < 64 * 1024 for cost in handed_off)
    assert all(_execution_transfer(cap) <= cost < _execution_transfer(cap) + MIB for cost in executed)
    release.set()
    results = await asyncio.gather(*parked)
  finally:
    release.set()
    for task in parked:
      task.cancel()
    await asyncio.gather(*parked, return_exceptions=True)
  assert [result[:2] for result in results] == [(200, {"ok": True})] * 4
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "public"])
@pytest.mark.parametrize("framing", ["unframed-chunked", "understated-length", "overstated-length"])
async def test_http_body_growth_charges_only_received_bytes_whatever_the_length_header(
  db, auth, route, framing,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, access="public", max_bytes=60 * MIB)
  payload = json.dumps({"payload": "x" * 3000}).encode()
  headers = dict(auth)
  if framing == "understated-length":
    headers["content-length"] = "2"
  elif framing == "overstated-length":
    headers["content-length"] = str(len(payload) + 4096)
  charged = []

  def observe():
    charged.append((app_services._admitted_exchange_cost, app_services._public_ingress_cost))

  async def chunks():
    # Each observation runs when the reader asks for more, after it has
    # admitted and buffered everything sent before.
    for start in range(0, len(payload), 1000):
      observe()
      yield payload[start:start + 1000]
    observe()

  url = f"/api/apps/{app.id}/service/echo" if route == "self" else f"/api/app-services/{app.slug}/echo"
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    response = await http.post(url, headers=headers, content=chunks())
  assert response.status_code == 201
  assert response.json()["body"] == {"payload": "x" * 3000}
  received = [0, 1000, 2000, 3000, len(payload)]
  assert charged == [
    (3 * size, 3 * size if route == "public" else 0) for size in received
  ], "body admission did not follow the bytes actually received"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_missing_length_http2_body_is_read_and_charged_not_treated_as_bodyless():
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  sent = []

  async def receive():
    if not sent:
      sent.append(True)
      return {"type": "http.request", "body": b'{"x":1}', "more_body": False}
    return {"type": "http.disconnect"}

  request = Request({
    "type": "http", "method": "POST", "path": "/echo", "headers": [],
    "query_string": b"", "http_version": "2",
  }, receive)
  with app_services.ServiceExchangeAdmission(1024) as admission:
    envelope = await _envelope(request, "echo", public=False, actor={}, max_bytes=1024, admission=admission)
    assert envelope["body"] == {"x": 1}
    assert admission.transfer_cost == 3 * len(b'{"x":1}')
  assert app_services._admitted_exchange_cost == 0


def _busy_lane_envelope(lane):
  return {
    "schema": 1, "method": "POST", "path": "/tools/echo" if lane == "tools" else "echo",
    "query": {}, "headers": {}, "body": {"value": "ok"}, "public": lane == "public",
    "actor": {},
  }


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["private", "public", "tools", "policy"])
async def test_exhausted_execution_admission_is_retryable_busy_without_side_effects(
  db, auth, monkeypatch, lane,
):
  from app import service_preload

  cap = 4096
  service = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({"body": {"path": request["path"]}}))
'''
  app = _service_app(db, access="public", service_bytes=service, max_bytes=cap)
  owner = db.query(models.Owner).first()
  real = {
    "serialize": getattr(app_services, "_serialize_request", None),
    "entry": app_services.service_entry,
    "spawn": asyncio.create_subprocess_exec,
  }
  side_effects = []
  monkeypatch.setattr(
    app_services, "_serialize_request",
    lambda *a: side_effects.append("serialize") or real["serialize"](*a), raising=False,
  )
  monkeypatch.setattr(app_services, "service_entry", lambda *a: side_effects.append("entry") or real["entry"](*a))
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: side_effects.append("preload") or None)

  async def spawn(*args, **kwargs):
    side_effects.append("process")
    return await real["spawn"](*args, **kwargs)

  monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
  # The retained envelope fits; the whole three-copy allowance does not.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * cap - 1)

  async def call():
    if lane == "policy":
      return await app_services.invoke_policy(app, owner, "echo", {"value": "ok"})
    return await app_services.invoke_service(
      app, owner, _busy_lane_envelope(lane), lane="tools" if lane == "tools" else None,
    )

  with pytest.raises(HTTPException) as caught:
    await call()
  assert caught.value.status_code == 503
  assert caught.value.detail == "App service is busy; retry shortly."
  assert caught.value.headers == {"Retry-After": "1"}
  assert side_effects == [], "a busy exchange serialized or started its service"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0
  # Busy is truthful: the identical request runs once the allowance is free.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 512 * MIB)
  result = await call()
  assert side_effects == ["serialize", "entry", "preload", "process"]
  assert (result if lane == "policy" else result[1]) == {"path": _busy_lane_envelope(lane)["path"]}
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("route", ["self", "shared", "public"])
def test_http_busy_execution_answers_retry_after_and_starts_no_service(
  client, auth, db, monkeypatch, route,
):
  # A ceiling whose three copies exceed the fixed decode reservation, so the
  # body decodes and its envelope is retained before execution is refused.
  cap = 64 * 1024
  app = _service_app(db, access="public", max_bytes=cap)
  started = []
  monkeypatch.setattr(app_services, "service_entry", lambda *a: started.append(True))
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * cap - 1)
  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  response = client.post(path, headers=auth, json={"value": "ok"})
  assert response.status_code == 503
  assert response.headers["retry-after"] == "1"
  assert response.json()["detail"] == "App service is busy; retry shortly."
  assert started == []
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
async def test_queued_tool_and_policy_requests_serialize_only_after_execution_admission(
  db, auth, monkeypatch, caller,
):
  from app import service_preload

  cap = 64 * 1024
  app = _service_app(db, max_bytes=cap)
  owner = db.query(models.Owner).first()
  lane = _ParkedLane()
  if caller == "tool":
    monkeypatch.setattr(app_services, "_global_slots", {**app_services._global_slots, "tools": lane})
  else:
    monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  serialized = []
  real_serialize = getattr(app_services, "_serialize_request", None)

  def serialize(envelope, max_bytes):
    serialized.append((max_bytes, app_services._admitted_exchange_cost))
    return real_serialize(envelope, max_bytes)

  async def reply(_python, _entry, _environment, request, _timeout, _max_stdout):
    assert json.loads(request)["body"] == {"text": "x" * 1000}
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_serialize_request", serialize, raising=False)
  monkeypatch.setattr(app_services, "_run_spawned", reply)
  body = {"text": "x" * 1000}
  task = asyncio.create_task(
    app_services.invoke_service(app, owner, {
      "schema": 1, "method": "POST", "path": "/tools/log", "query": {}, "headers": {},
      "body": body, "public": False, "actor": {},
    }, lane="tools") if caller == "tool"
    else app_services.invoke_policy(app, owner, "reviewer/validate", body)
  )
  try:
    await lane.wait_queued(1)
    retained = app_services._admitted_exchange_cost
    assert 1000 < retained < 3 * cap, "a queued call held a transfer allowance"
    assert serialized == [], "a queued call serialized its request"
    lane.release()
    result = await asyncio.wait_for(task, 5)
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert (result if caller == "policy" else result[1]) == {"ok": True}
  assert serialized == [(cap, _execution_transfer(cap) + retained)]
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_revoking_a_queued_browser_call_releases_its_envelope_and_starts_nothing(
  db, auth, monkeypatch,
):
  app = _service_app(db, max_bytes=60 * MIB)
  owner = db.query(models.Owner).first()
  grant_id = "c" * 32
  lane = _ParkedLane()
  started = []
  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "service_entry", lambda *a: started.append(True))
  task = asyncio.create_task(app_services.invoke_service(app, owner, {
    "method": "GET", "path": "media/1", "actor": {"browser_grant_id": grant_id},
  }))
  try:
    await lane.wait_queued(1)
    assert 0 < app_services._admitted_exchange_cost < 64 * 1024
    assert app_services.browser_grant_has_active_calls(grant_id)
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert started == []
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
async def test_disconnect_after_body_growth_releases_the_received_charge(public):
  from starlette.requests import ClientDisconnect, Request
  from app.routes.app_services import _envelope

  observed = []
  messages = iter([
    {"type": "http.request", "body": b'"' + b"x" * 999, "more_body": True},
    {"type": "http.disconnect"},
  ])

  async def receive():
    observed.append((app_services._admitted_exchange_cost, app_services._public_ingress_cost))
    return next(messages)

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with pytest.raises(ClientDisconnect):
    with app_services.ServiceExchangeAdmission(4096, public_ingress=public) as admission:
      await _envelope(request, "echo", public=public, actor={}, max_bytes=4096, admission=admission)
  assert observed == [(0, 0), (3000, 3000 if public else 0)]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


# Ordinary numeric payloads an 8 MiB service accepted before number work was
# charged: each measured well inside the unit budget's parse time.
_rng = random.Random(7)
_ORDINARY_NUMERIC = {
  "geojson-20k": {"type": "LineString", "coordinates": [
    [round(_rng.uniform(-180, 180), 6), round(_rng.uniform(-90, 90), 6)] for _ in range(20_000)
  ]},
  "embeddings-16x1536": [[_rng.random() for _ in range(1536)] for _ in range(16)],
  "sensor-100k": [round(_rng.uniform(0, 100), 2) for _ in range(100_000)],
  "timestamps-50k": [1697040000.123 + i for i in range(50_000)],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_ORDINARY_NUMERIC))
async def test_ordinary_numeric_payloads_have_one_accepting_verdict(shape):
  value = _ORDINARY_NUMERIC[shape]
  assert len(_compact(value)) < 8 * MIB
  assert await _number_work_verdicts(value) == ["accepted", "accepted"]
  raw = _compact(value)
  with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
    admission.admit_output(raw)
    assert await admission.decode_response(raw) == value
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
# Counts at which each spelling's measured parse time alone (thread CPU, in
# small-int elements: 14.6, 12.5, 17 and 10 per element) exceeds the budget by
# at least 1.3x, so a relaxed charge can never admit them.
@pytest.mark.parametrize("token,count", [
  (b"2.2e-308", 50_000), (b"5e-324", 60_000), (b"2.2250738585072014e-300", 40_000),
  (b"0.9539150178908785693288", 70_000),
], ids=["short-near-min-normal", "subnormal", "halfway-exponent", "long-decimal"])
async def test_costly_float_spellings_still_exhaust_the_unit_budget(token, count):
  raw = b"[" + b",".join([token] * count) + b"]"
  with pytest.raises(HTTPException) as refused:
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      await admission.decode(raw)
  assert refused.value.detail == "App service request structural resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
  b"[" + b"1 " * (2 * MIB) + b"]",
  b"[" + b"- " * (2 * MIB) + b"]",
  b"[" + b"]" * (4 * MIB) + b"]",
  b"[" + b"\\" * (4 * MIB) + b"]",
], ids=["spaced-digits", "lone-signs", "closers", "backslashes"])
async def test_invalid_token_runs_take_no_python_step_each_beyond_the_unit_budget(monkeypatch, raw):
  searches = []
  real = app_services._JSON_RESOURCE_MARKS

  class Counting:
    def search(self, *args):
      searches.append(1)
      return real.search(*args)

  monkeypatch.setattr(app_services, "_JSON_RESOURCE_MARKS", Counting())
  with pytest.raises((HTTPException, ValueError)):
    with app_services.ServiceExchangeAdmission(8 * MIB) as admission:
      await admission.decode(raw)
  chunks = len(raw) // app_services._JSON_SCAN_BYTES + 1
  assert len(searches) <= 2 * chunks + app_services.MAX_JSON_STRUCTURE + 1
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
  {"images": ["A" * (14 * MIB)] * 4, "caption": "雪😀"},
  ["x" * (20 * 1024) for _ in range(3000)],
  "x" * (55 * MIB),
], ids=["gallery", "many-medium-strings", "one-long-string"])
async def test_retained_string_sizing_yields_by_bytes_sized(body):
  beats = []
  running = True

  async def heartbeat():
    while running:
      beats.append(time.perf_counter())
      await asyncio.sleep(0)

  task = asyncio.create_task(heartbeat())
  await asyncio.sleep(0)
  try:
    with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
      await admission.retain({"body": body})
  finally:
    beats.append(time.perf_counter())
    running = False
    await task
  assert max(b - a for a, b in zip(beats, beats[1:])) < 0.05
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_wide_list_item_rendering_fits_a_reservation_without_unheld_copies():
  raw = _compact({"body": ["x" * (20 * MIB) + "😀"]})
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    admission.admit_output(raw)
    gc.collect()
    tracemalloc.start()
    try:
      decoded = await admission.decode_response(raw)
      await app_services.render_json(decoded["body"])
      peak = tracemalloc.get_traced_memory()[1]
    finally:
      tracemalloc.stop()
    held = admission.response_cost + admission.transfer_cost
    assert peak <= held
    # Separator concatenation and UTF-8 encoding never coexist.
    assert held < peak + 80 * MIB
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("source, status, category", [
  ('print("not JSON private")', 502, "invalid_json"),
  ('print("[]")', 502, "invalid_envelope"),
  ('print(\'{"status":true}\')', 502, "invalid_status"),
  # A boundary failure outranks the app's own 5xx status.
  ('print(\'{"status":503,"headers":[]}\')', 502, "invalid_headers"),
  ('print(\'{"body_base64":123}\')', 502, "invalid_binary_response"),
  ('print(\'{"media_type":"text/plain"}\')', 502, "invalid_media_type"),
  ('print(\'{"body_base64":"!","media_type":"text/plain"}\')', 502, "invalid_binary_data"),
  ('import sys\nprint("private", file=sys.stderr)\nsys.exit(3)', 502, "process_failed"),
  ('print(\'{"status":503,"body":{"detail":"private"}}\')', 503, "app_http_error"),
  ('print(\'{"body":{"ok":"private"}}\')', 200, None),
])
def test_service_traces_record_app_and_platform_category_never_request_data(
  client, auth, db, monkeypatch, source, status, category,
):
  recorded = []
  monkeypatch.setattr(app_services.tracing, "annotate", lambda _h, attrs: recorded.append(attrs))
  app = _service_app(db)
  (runtime_parent(app.id) / app.runtime_revision / "service.py").write_text(source)
  response = client.get(
    f"/api/apps/{app.id}/service/private-id?token=private-query", headers=auth,
  )
  assert response.status_code == status
  assert recorded == [
    {"mobius.app.slug": "service-test"},
    *([{"mobius.service.error_type": category}] if category else []),
  ]


def test_tracing_wrapper_preserves_http_admission_through_response_render(
  client, auth, db, monkeypatch,
):
  created, rendered = [], []
  real_init = app_services.ServiceExchangeAdmission.__init__
  real_render = app_services.render_json

  def capture_admission(admission, *args, **kwargs):
    created.append(admission)
    real_init(admission, *args, **kwargs)

  async def capture_render(value):
    rendered.append((len(created), created[0].response_cost, app_services._admitted_exchange_cost))
    return await real_render(value)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "__init__", capture_admission)
  monkeypatch.setattr(app_services, "render_json", capture_render)
  app = _service_app(db)
  response = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth, json={"values": [1.5] * 1000},
  )
  assert response.status_code == 201
  [(count, response_cost, held)] = rendered
  assert count == 1, "tracing replaced the route's admission instead of forwarding it"
  assert response_cost > 0 and held >= response_cost, "rendering lost its response reservation"
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status,category", [
  ("busy", 503, "exchange_busy"),
  ("response", 502, "response_limit"),
  ("transfer-declaration", 503, "invalid_declaration"),
  ("route-declaration", 503, "invalid_declaration"),
])
async def test_service_traces_categorize_exchange_failures_without_changing_responses(
  db, auth, monkeypatch, failure, status, category,
):
  from app import service_preload

  cap = 64 * 1024
  app = _service_app(db, max_bytes=cap)
  owner = db.query(models.Owner).first()
  recorded = []
  monkeypatch.setattr(app_services.tracing, "annotate", lambda _h, attrs: recorded.append(attrs))
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    assert failure == "response", "a refused exchange started a service"
    monkeypatch.setattr(app_services, "MAX_JSON_DECODE_COST", 1)
    return b'{"body":{"value":"private"}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  if failure == "busy":
    monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * cap - 1)
  elif failure == "transfer-declaration":
    app.capability_contract["service"]["max_request_bytes"] = 0
  elif failure == "route-declaration":
    app.capability_contract["service"]["route_limits"] = [_route_limit("GET", "/echo", cap + 1)]

  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {"actor": {}, "body": "private"})
  assert caught.value.status_code == status
  assert caught.value.headers == ({"Retry-After": "1"} if failure == "busy" else None)
  assert recorded == [
    {"mobius.app.slug": "service-test"}, {"mobius.service.error_type": category},
  ]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answers_first", [False, True])
@pytest.mark.parametrize("lane", ["spawn", "preload"])
@pytest.mark.parametrize("stderr_bytes", [0, app_services.MAX_ERROR_BYTES])
@pytest.mark.parametrize("cap", [4096, 64 * 1024, MIB, 8 * MIB])
async def test_stdio_fits_its_execution_reservation_in_either_order(
  tmp_path, monkeypatch, answers_first, lane, stderr_bytes, cap,
):
  # A service may answer and close stdout before reading stdin; the unsent
  # request then waits in the pipe transport while stdout is collected.
  collected = tmp_path / "collected"
  entry = tmp_path / "service.py"
  answer = (
    f"view = memoryview(b'{{\"body\":\"' + b'x' * ({cap} - 11) + b'\"}}')\n"
    "while view: view = view[os.write(1, view):]\n"
  )
  # uvloop subprocess stdio and preload stdio use sockets; shutdown signals
  # EOF even when the child has another descriptor for that socket.
  close_stdout = (
    "if stat.S_ISSOCK(os.fstat(1).st_mode):\n"
    "  socket.socket(fileno=os.dup(1)).shutdown(socket.SHUT_WR)\n"
    "else: os.close(1)\n"
  )
  action = (
    answer + close_stdout
    + f"while not os.path.exists({str(collected)!r}): time.sleep(0.001)\n"
    + "sys.stdin.buffer.read()\n"
    if answers_first else "sys.stdin.buffer.read()\n" + answer
  )
  entry.write_text(
    "import os, sys, time, socket, stat\nMOBIUS_PRELOAD = True\n"
    + 'if __name__ == "__main__":\n'
    + f"  os.write(2, b\"e\" * {stderr_bytes})\n"
    + "".join("  " + line + "\n" for line in action.splitlines())
  )
  preload = app_services.service_preload
  environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
  host = None
  if lane == "preload":
    host = await preload.start((990, "stdio-budget"), "service-test", sys.executable, entry, environment)
    assert host is not None
  real_read = preload.read_bounded

  async def read_then_release(reader, limit):
    data = await real_read(reader, limit)
    if limit == cap:
      collected.touch()
    return data

  monkeypatch.setattr(app_services, "_read_bounded", read_then_release)
  monkeypatch.setattr(preload, "read_bounded", read_then_release)
  tracemalloc.start()
  try:
    with app_services.ServiceExchangeAdmission(cap) as admission:
      part = min(16384, cap // 8)
      envelope = {"body": [str(i).ljust(part, "x") for i in range(cap // part - 2)], "actor": {}}
      await admission.retain(envelope)
      admission.admit_execution(cap)
      request = await app_services._serialize_request(envelope, cap)
      tracemalloc.reset_peak()
      if lane == "spawn":
        stdout, _stderr, code = await app_services._run_spawned(
          sys.executable, entry, environment, request, 30, cap,
        )
      else:
        stdout, _stderr, code = await preload.run(
          host, environment, request, timeout_seconds=30,
          max_stdout=cap, max_stderr=app_services.MAX_ERROR_BYTES,
        )
      peak = tracemalloc.get_traced_memory()[1]
      assert code == 0 and len(stdout) == cap
      assert _stderr == b"e" * stderr_bytes
      assert peak <= admission.cost, (peak, admission.cost)
  finally:
    tracemalloc.stop()
    if host is not None:
      await preload.shutdown()
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_bounded_output_reads_keep_whole_chunks_however_the_writer_splits_it():
  size = 16 * 1024
  reader = asyncio.StreamReader()

  async def drip():
    for _ in range(size):
      reader.feed_data(b"x")
      await asyncio.sleep(0)
    reader.feed_eof()

  tracemalloc.start()
  try:
    producer = asyncio.create_task(drip())
    data = await app_services._read_bounded(reader, size)
    await producer
    peak = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert data == b"x" * size
  # One object per write would hold dozens of bytes of headers per byte.
  assert peak < 4 * size, peak


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537])
async def test_bounded_output_preserves_bytes_through_chunk_and_eof_boundaries(size):
  reader = asyncio.StreamReader()
  expected = b"x" * size
  reader.feed_data(expected)
  reader.feed_eof()
  assert await app_services._read_bounded(reader, size) == expected


@pytest.mark.asyncio
async def test_bounded_output_rejects_the_first_excess_byte_without_waiting_for_eof():
  reader = asyncio.StreamReader()
  reader.feed_data(b"x" * 65537)
  with pytest.raises(ValueError, match="output limit exceeded"):
    await asyncio.wait_for(app_services._read_bounded(reader, 65536), timeout=1)


@pytest.mark.asyncio
async def test_full_sized_output_still_waits_for_eof_and_is_cancellable():
  reader = asyncio.StreamReader()
  reader.feed_data(b"x" * 65536)
  task = asyncio.create_task(app_services._read_bounded(reader, 65536))
  await asyncio.sleep(0)
  assert not task.done()
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task


@pytest.mark.asyncio
async def test_stdio_headroom_keeps_two_full_media_calls_and_a_control_admissible():
  cap = 60 * MIB
  envelope = {"body": [str(i).ljust(16384, "x") for i in range(cap // 16384 - 2)], "actor": {}}
  with (
    app_services.ServiceExchangeAdmission(cap) as first,
    app_services.ServiceExchangeAdmission(cap) as second,
    app_services.ServiceExchangeAdmission(8 * MIB) as control,
  ):
    for admission in (first, second):
      await admission.retain(envelope)
      admission.admit_execution(cap)
    await control.retain({"body": {"ok": 1}, "actor": {}})
    control.admit_execution(8 * MIB)
    assert app_services._admitted_exchange_cost <= app_services.MAX_ADMITTED_EXCHANGE_COST
    assert all(a.transfer_cost >= 3 * a.max_bytes + 256 * 1024 for a in (first, second, control))
  assert app_services._admitted_exchange_cost == 0


def test_stdio_execution_overhead_includes_fixed_and_size_scaled_costs():
  # Independent pins complement peak-allocation tests and callers that derive
  # their expected execution charge from the production formula.
  assert app_services._stdio_overhead(4096) == 1085752
  assert app_services._stdio_overhead(60 * MIB) == 1281848


@pytest.mark.asyncio
async def test_execution_reserves_the_larger_encoding_peak_instead_of_stdio():
  cap = 2 * MIB
  with app_services.ServiceExchangeAdmission(cap) as admission:
    await admission.retain({"body": "x" * (cap - 1024)})
    encoding = app_services._json_encoding_peak(
      min(cap, admission.encoded_bytes), admission.largest_part, admission.encoded_depth,
    )
    assert encoding > _execution_transfer(cap)
    admission.admit_execution(cap)
    assert admission.transfer_cost == encoding
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_stopping_a_child_drains_paused_output_before_wait_can_finish(monkeypatch):
  drained = asyncio.Event()
  events = []

  class Reader:
    async def read(self, size):
      assert size == app_services._STDIO_SLICE
      events.append("drained")
      drained.set()
      return b""

  class Process:
    pid = 42
    stdout = Reader()
    stderr = Reader()

    async def wait(self):
      # A real subprocess transport with paused unread pipes behaves this
      # way even after SIGKILL: reaping alone cannot complete its waiters.
      await drained.wait()
      events.append("reaped")

  monkeypatch.setattr(app_services.os, "killpg", lambda *_: events.append("killed"))
  pending_read = asyncio.create_task(asyncio.Event().wait())
  try:
    await asyncio.wait_for(app_services._stop_process(Process(), pending_read), timeout=1)
  finally:
    pending_read.cancel()
    await asyncio.gather(pending_read, return_exceptions=True)
  assert events[0] == "killed" and "reaped" in events


@pytest.mark.asyncio
async def test_bounded_diagnostics_stay_reserved_through_response_decode():
  raw = b'{"status":500,"body":{}}'
  diagnostic = "x" * 999 + "😀"
  with app_services.ServiceExchangeAdmission(4096) as admission:
    admission.admit_output(raw, diagnostic)
    expected = 3 * len(raw) + app_services._JSON_STRING_COST + 4 * len(diagnostic)
    assert admission.transfer_cost == expected
    await admission.decode_response(raw)
    assert admission.transfer_cost == expected
  assert app_services._admitted_exchange_cost == 0
