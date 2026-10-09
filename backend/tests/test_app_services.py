"""Accepted app services own policy; the platform owns their hard boundary."""

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
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
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
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
    app_services.invoke_service(SimpleNamespace(id=1), None, {"public": False}),
  )
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="public callback admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=1), None, {"public": True}),
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
  "headers": {"Content-Language": "en"},
}))
'''


def _service_app(
  db, *, access="self", slug="service-test", service_id=None, aliases=(),
  service_bytes=SERVICE, max_bytes=8 * 1024 * 1024,
):
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
        "max_request_bytes": max_bytes,
        "max_response_bytes": max_bytes,
      },
    },
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
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", lambda _self, raw: json.loads(raw))
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


@pytest.mark.parametrize("route", ["self", "shared", "public"])
def test_http_resource_admission_precedes_body_read_for_every_route(
  client, auth, db, monkeypatch, route,
):
  from app.routes import app_services as routes

  app = _service_app(db, access="public")
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 1)

  async def forbidden_read(*_args, **_kwargs):
    pytest.fail("request body was read before admission")

  monkeypatch.setattr(routes, "read_capped_body", forbidden_read)
  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  response = client.post(path, headers=auth, content=b"{}")
  assert response.status_code == 413
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("raw", [
  b"[" + b"{}," * 150_000 + b"{}]",
  b"{" + b'"x":0,' * 100_000 + b'"x":0}',
  b"[" * 65 + b"0" + b"]" * 65,
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


@pytest.mark.parametrize("shape", ["scalar", "original", "gallery"])
def test_http_reviewed_media_allowance_preserves_twenty_mib_gif_and_gallery(
  client, auth, db, shape,
):
  import base64

  service = b'''import base64, hashlib, json, sys
body = json.load(sys.stdin)["body"]
items = [body] if isinstance(body, str) else body["items"]
print(json.dumps({"body": [{"size": len(base64.b64decode(item)), "sha256": hashlib.sha256(base64.b64decode(item)).hexdigest()} for item in items]}))
'''
  import hashlib
  app = _service_app(db, service_bytes=service, max_bytes=60 * 1024 * 1024)
  gif = b"GIF89a" + b"x" * (20 * 1024 * 1024 - 6)
  encoded = base64.b64encode(gif).decode("ascii")
  items = [encoded] * (2 if shape == "gallery" else 1)
  body = encoded if shape == "scalar" else {"items": items, "caption": "雪😀"}
  # ensure_ascii=False exercises the full Unicode text copy alongside base64.
  raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
  response = client.post(f"/api/apps/{app.id}/service/media", headers=auth, content=raw)
  assert response.status_code == 200
  assert response.json() == [{"size": len(gif), "sha256": hashlib.sha256(gif).hexdigest()}] * len(items)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_queued_http_payloads_keep_admission_and_reject_new_reads(monkeypatch):
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

  async def http_call():
    async def receive():
      reads.append(True)
      return {"type": "http.request", "body": b'{"payload":"' + b'x' * 256 + b'"}', "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
    with app_services.ServiceExchangeAdmission(1024) as admission:
      envelope = await _envelope(request, "echo", public=False, actor={}, max_bytes=1024, admission=admission)
      return await app_services.invoke_service(SimpleNamespace(id=1), None, envelope, admission=admission)

  first = asyncio.create_task(http_call())
  try:
    await asyncio.wait_for(gate.waiting.wait(), 1)
    reserved = app_services._admitted_exchange_cost
    assert reserved > 3 * 1024, "queued decoded input was no longer counted"
    monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", reserved + 3 * 1024 - 1)
    with pytest.raises(HTTPException) as caught:
      await http_call()
    assert caught.value.status_code == 413
    assert reads == [True], "rejected backlog request buffered its body"
    assert app_services._admitted_exchange_cost == reserved
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cancellation_while_reading_releases_admission():
  started = asyncio.Event()

  async def read():
    with app_services.ServiceExchangeAdmission(1024):
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
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", lambda _self, raw: json.loads(raw))
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

  async def reply(*_args):
    request_reserved = app_services._admitted_exchange_cost
    assert request_reserved > 3 * 4096
    # Either direction fits alone; their simultaneously retained costs do not.
    monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", request_reserved + 1000)
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

  def decode(self, raw):
    value = real_decode(self, raw)
    seen.append((self.cost, self.request_cost, self.response_cost))
    assert self.cost == 3 * self.max_bytes + self.request_cost + self.response_cost
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
