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
  stub but `recover_router` (and other healthy modules) still load
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

  # recover_router is a real router (not a stub): it has at least
  # one route registered, and that route is NOT the catch-all
  # `{rest_of_path:path}` the stub registers.
  recover_paths = [
    getattr(r, "path", "") for r in routes_pkg.recover_router.routes
  ]
  assert recover_paths, "recover_router should have real routes"
  assert not any(
    "{rest_of_path:path}" in p for p in recover_paths
  ), f"recover_router looks like a stub: {recover_paths}"


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
    assert resp.json() == {"status": "ok"}
