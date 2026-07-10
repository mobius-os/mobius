"""Boot import chain must tolerate broken route modules.

`main.py` is frozen and does `from app.routes import (...)` with ~15
names. Any one of those modules raising on import would otherwise
kill uvicorn at boot and take the always-reachable `/recover/chat`
endpoint down with it. `app/routes/__init__.py` defends against
this by wrapping each import in `_load(name)` — on failure, a 503
stub with the right name is exposed so `main.py` still finds
every expected attribute.

These tests lock in the stub contract + verify the scaffold doesn't
collapse when a real route module is forced to fail.
"""

import importlib
import sys

import pytest
from fastapi import APIRouter, FastAPI
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
      for k, mod in saved.items():
        if k.startswith("app.routes."):
          setattr(routes_pkg, k.rsplit(".", 1)[1], mod)


def _mount(router: APIRouter, prefix: str = "/x") -> TestClient:
  """Mounts a router under `prefix` on a throwaway FastAPI app and
  returns a TestClient for it."""
  app = FastAPI()
  app.include_router(router, prefix=prefix)
  return TestClient(app)


def test_load_returns_stub_for_nonexistent_module():
  """`_load` on a missing module returns a router whose paths 503."""
  from app.routes import _load
  stub = _load("definitely_not_a_real_module_xyz")
  assert isinstance(stub, APIRouter)
  client = _mount(stub, prefix="/x")
  for method in ("get", "post", "put", "delete", "patch"):
    resp = getattr(client, method)("/x/anything/here")
    assert resp.status_code == 503, (
      f"{method.upper()} expected 503, got {resp.status_code}"
    )
    assert "definitely_not_a_real_module_xyz" in resp.json()["detail"]
    assert "/recover/chat" in resp.json()["detail"]


def test_broken_route_module_yields_stub_real_routers_unaffected(
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

  # apps_router is a stub: any path returns 503 with apps-named
  # detail.
  apps_client = _mount(routes_pkg.apps_router, prefix="/api/apps")
  resp = apps_client.get("/api/apps/")
  assert resp.status_code == 503
  assert "apps" in resp.json()["detail"]

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


def test_main_boots_when_app_watcher_start_raises(monkeypatch):
  """A failure inside lifespan's `start_watcher` call must NOT crash
  uvicorn boot — it should be logged and the app should still serve
  /api/health (and therefore /recover/chat) normally."""
  # Re-import main with a patched start_watcher that raises.
  import app.app_watcher as watcher_mod

  def boom(loop):
    raise RuntimeError("simulated watcher crash")

  monkeypatch.setattr(watcher_mod, "start_watcher", boom)

  from app.main import app as main_app
  client = TestClient(main_app)
  with client:
    # The `with` block enters lifespan; if start_watcher's failure
    # weren't caught, this would raise before yielding a usable
    # client.
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["boot_id"]
