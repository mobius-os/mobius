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

import asyncio
import importlib
import sys
import threading

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
  from app.routes import apps as apps_module
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
  from app import chat as chat_mod
  from app.main import app as main_app

  sweep_entered = threading.Event()
  release_sweep = threading.Event()
  lifespan_ready = threading.Event()
  boot_errors = []

  async def held_sweep(db):
    del db
    sweep_entered.set()
    await asyncio.to_thread(release_sweep.wait)
    return []

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
