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
