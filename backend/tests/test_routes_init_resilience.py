"""Boot import chain must tolerate broken route modules.

`main.py` does `from app.routes import (...)` with ~15 names. Any one of those
modules raising on import would otherwise kill uvicorn at boot.
`app/routes/__init__.py` defends against this by wrapping each import in
`_load(name)`. A failure is recorded and replaced by an empty router so
`main.py` can finish importing; the entrypoint consumes the explicit failure
verdict and serves the baked platform floor.

These tests lock in the explicit boot verdict and verify the scaffold doesn't
collapse when a real route module is forced to fail.
"""

import asyncio
import importlib
import sys
import threading

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _restore_app_routes_modules():
  """Restore the canonical app.routes.* modules after each test here.

  These tests pop app.routes.* from sys.modules and re-import them under a
  monkeypatched importer (some as 503 stubs). Left in place, a later test that
  does `import app.routes.<x>` would get a freshly re-imported — and now
  DISTINCT — module object, while the app router built at conftest import still
  holds the original. A monkeypatch on that fresh module then never reaches the
  running handler. (This silently broke a storage size-cap test.) Snapshot the
  originals up front and restore them on teardown so the rest of the suite sees
  the same module objects the app router uses.
  """
  def _route_mod_names():
    return [
      k for k in sys.modules
      if k == "app.routes" or k.startswith("app.routes.")
    ]

  saved = {k: sys.modules[k] for k in _route_mod_names()}
  saved_failures = set(getattr(
    saved.get("app.routes"), "_ROUTER_IMPORT_FAILURES", set(),
  ))
  try:
    yield
  finally:
    for k in _route_mod_names():
      sys.modules.pop(k, None)
    sys.modules.update(saved)
    # Restoring sys.modules entries is not enough: the re-import also rebinds
    # the `app.routes` attribute on the `app` package and each `<sub>`
    # attribute on the `app.routes` package to the freshly imported objects.
    # pytest's `monkeypatch.setattr("app.routes.<sub>.<attr>", ...)` resolves
    # its target via the parent getattr chain (__import__ + getattr), NOT via
    # sys.modules[name] — so a left-over rebind makes a string-target patch
    # land on a different module object than the one the app's bound route
    # handler closed over, and the patch silently misses. Re-point the chain at
    # the canonical (restored) objects so getattr and sys.modules agree again.
    routes_pkg = saved.get("app.routes")
    if routes_pkg is not None:
      sys.modules["app"].routes = routes_pkg
      routes_pkg._ROUTER_IMPORT_FAILURES.clear()
      routes_pkg._ROUTER_IMPORT_FAILURES.update(saved_failures)
      for k, mod in saved.items():
        if k.startswith("app.routes."):
          setattr(routes_pkg, k.rsplit(".", 1)[1], mod)


def _mount(router: APIRouter, prefix: str = "/x") -> TestClient:
  """Mounts a router under `prefix` on a throwaway FastAPI app and
  returns a TestClient for it."""
  app = FastAPI()
  app.include_router(router, prefix=prefix)
  return TestClient(app)


def test_canonical_registry_has_no_import_failures():
  from app.routes import require_all_routers_loaded, router_import_failures

  assert router_import_failures() == ()
  require_all_routers_loaded()


def test_load_records_failure_without_installing_a_global_catch_all():
  """A race after preflight may lose one router, never the healthy API."""
  from app.routes import (
    _load,
    require_all_routers_loaded,
    router_import_failures,
  )

  name = "definitely_not_a_real_module_xyz"
  fallback = _load(name)
  assert isinstance(fallback, APIRouter)
  assert fallback.routes == []
  assert router_import_failures() == (name,)
  with pytest.raises(RuntimeError, match=name):
    require_all_routers_loaded()

  # An isolated empty router is a normal miss, not an unscoped route that
  # captures every path registered after it.
  app = FastAPI()
  app.include_router(fallback)

  @app.get("/api/health")
  def healthy_route():
    return {"status": "ok"}

  client = TestClient(app)
  assert client.get("/api/health").json() == {"status": "ok"}
  assert client.get("/api/anything-else").status_code == 404


def test_broken_route_module_is_isolated_and_real_routers_remain(
  monkeypatch,
):
  """If `app.routes.apps` raises on import, `apps_router` becomes a
  stub but `auth_router` (and other healthy modules) still load
  as real routers."""
  # Drop any cached `app.routes` so `_load` re-executes against the
  # monkeypatched importer state.
  for mod in list(sys.modules):
    if mod == "app.routes" or mod.startswith("app.routes."):
      sys.modules.pop(mod, None)

  real_import = __import__

  def fake_import(name, *args, **kwargs):
    if name == "app.routes.apps":
      raise SyntaxError("simulated broken apps.py")
    return real_import(name, *args, **kwargs)

  monkeypatch.setattr("builtins.__import__", fake_import)

  routes_pkg = importlib.import_module("app.routes")

  # Every expected name still exists — `main.py`'s import won't
  # crash even though apps.py was broken.
  for name in routes_pkg.__all__:
    assert hasattr(routes_pkg, name), f"missing attribute: {name}"

  # The failed router is empty and the explicit boot verdict fails. It cannot
  # shadow healthy routers while the entrypoint changes over to baked source.
  assert routes_pkg.apps_router.routes == []
  assert routes_pkg.router_import_failures() == ("apps",)
  with pytest.raises(RuntimeError, match="apps"):
    routes_pkg.require_all_routers_loaded()

  # auth_router is a real router (not a stub): it has at least
  # one route registered, and that route is NOT the catch-all
  # `{rest_of_path:path}` the stub registers.
  auth_paths = [
    getattr(r, "path", "") for r in routes_pkg.auth_router.routes
  ]
  assert auth_paths, "auth_router should have real routes"
  assert not any(
    "{rest_of_path:path}" in p for p in auth_paths
  ), f"auth_router looks like a stub: {auth_paths}"


def test_router_failure_degrades_probes_without_hiding_them():
  """Defense in depth for source changing after the successful preflight."""
  from app import main as main_module
  from app.routes import _ROUTER_IMPORT_FAILURES

  _ROUTER_IMPORT_FAILURES.add("apps")

  health_response = Response()
  health = main_module.health(health_response)
  assert health_response.status_code == 200
  assert health["mode"] == "degraded"
  assert health["failed_routers"] == ["apps"]

  strict_response = Response()
  strict = main_module.health_strict(strict_response)
  assert strict_response.status_code == 503
  assert strict["status"] == "router_import_failure"

  ready_response = Response()
  ready = main_module.ready(ready_response)
  assert ready_response.status_code == 503
  assert ready["reason"] == "router_import_failure"

  with pytest.raises(HTTPException) as exc_info:
    main_module.unknown_api("apps")
  assert exc_info.value.status_code == 503
  assert "apps" in exc_info.value.detail
  assert "external Recovery" in exc_info.value.detail


def test_lifespan_cannot_mutate_cron_for_a_low_id_test_app(
  monkeypatch, db, tmp_path,
):
  """Reproduce the production-container leak and prove it fails closed.

  Entering the real lifespan with a scheduled row used to invoke the baked
  scaffold. Since the isolated test DB assigns low IDs, that could replace a
  production Memory/Reflection crontab entry when pytest ran in the live
  container.
  """
  from pathlib import Path

  from app import install, models
  from app.config import get_settings
  from app.routes import app_schedules as apps_module
  from app.main import app as main_app

  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "mobius.json").write_text(
    '{"schedule":{"default":"30 5 * * *","job":"fetch.sh"}}',
    encoding="utf-8",
  )
  app_row = models.App(
    name="Memory",
    slug="memory",
    description="scheduled test app",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
  )
  db.add(app_row)
  db.commit()
  assert app_row.id < 10  # Preserve the low-id shape that caused the incident.

  sentinel = tmp_path / "scaffold-was-called"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text(
    f"#!/bin/sh\ntouch {sentinel}\n",
    encoding="utf-8",
  )
  fake_scaffold.chmod(0o755)
  monkeypatch.setattr(install, "CRON_SCAFFOLD", fake_scaffold)
  monkeypatch.setattr(apps_module, "_read_live_crontab", lambda: "")
  monkeypatch.delenv("MOBIUS_ALLOW_TEST_CRON", raising=False)

  with TestClient(main_app) as client:
    assert client.get("/api/health").status_code == 200

  assert not sentinel.exists()


def test_lifespan_waits_for_initial_restart_resume_sweep(monkeypatch):
  """The server must not accept a manual send before restart recovery claims."""
  from app import bootstrap as bootstrap_mod
  from app import chat as chat_mod
  from app.main import app as main_app

  sweep_entered = threading.Event()
  release_sweep = threading.Event()
  lifespan_ready = threading.Event()
  boot_errors = []

  async def held_sweep(db, **kwargs):
    del db, kwargs
    sweep_entered.set()
    await asyncio.to_thread(release_sweep.wait)
    return []

  async def skip_external_bootstrap(db):
    # This test owns startup ordering at the restart-resume seam. A first-boot
    # Store clone/fallback can legitimately take longer than its 20-second
    # assertion budget and must not turn the ordering contract into a network
    # timing test.
    del db

  monkeypatch.setattr(
    bootstrap_mod,
    "ensure_bootstrap_apps_installed",
    skip_external_bootstrap,
  )
  monkeypatch.setattr(chat_mod, "sweep_reset_parks", held_sweep)

  def boot_app():
    try:
      with TestClient(main_app):
        lifespan_ready.set()
    except BaseException as exc:  # surface a lifespan-thread failure below
      boot_errors.append(exc)

  thread = threading.Thread(target=boot_app, daemon=True)
  thread.start()
  try:
    assert sweep_entered.wait(timeout=20)
    # The old fire-and-forget startup reached the usable server while this
    # sweep was still blocked. The fixed lifecycle awaits it before yielding.
    assert not lifespan_ready.wait(timeout=1.0)
  finally:
    release_sweep.set()

  thread.join(timeout=30)
  assert not thread.is_alive()
  assert boot_errors == []
  assert lifespan_ready.is_set()


def test_lifespan_does_not_shadow_module_session_factory():
  """A late local import must not break earlier startup migrations.

  ``SessionLocal`` is imported at module scope and used near the start of the
  lifespan. Assigning or importing that name anywhere inside the function
  makes it local for the whole function, raising ``UnboundLocalError`` before
  the later statement runs.
  """
  from app.main import lifespan

  assert "SessionLocal" not in lifespan.__code__.co_varnames
