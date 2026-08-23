"""Owner boundary and HTTP behavior for container rebuilding."""

from __future__ import annotations

from app import deployment_control as dc
from app.routes import admin as admin_routes


def _status(**overrides):
  value = {
    "supported": True,
    "deployment": "self_hosted",
    "operation_id": "job-1",
    "state": "queued",
    "expected_sha": "a" * 40,
    "code": None,
    "message": None,
    "updated_at": None,
  }
  value.update(overrides)
  return value


def test_rebuild_routes_require_owner(client):
  assert client.get("/api/admin/rebuild").status_code == 401
  assert client.post("/api/admin/rebuild").status_code == 401
  assert client.post("/api/admin/rebuild/prepare").status_code == 401


def test_rebuild_post_rejects_cross_site(client, auth):
  response = client.post(
    "/api/admin/rebuild",
    headers={**auth, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
  )
  assert response.status_code == 403


def test_rebuild_status_is_read_only(client, auth, monkeypatch):
  async def read():
    return _status(state="verifying")

  monkeypatch.setattr(dc, "read_rebuild_status", read)

  response = client.get("/api/admin/rebuild", headers=auth)

  assert response.status_code == 200
  assert response.json()["state"] == "verifying"


def test_rebuild_post_accepts_empty_owner_action(client, auth, monkeypatch):
  async def request():
    return _status()

  monkeypatch.setattr(dc, "request_rebuild", request)

  response = client.post(
    "/api/admin/rebuild",
    # Caller values are deliberately ignored: the route has no body model and
    # deployment_control derives the target/provider server-side.
    json={"image": "attacker/image", "service": "other"},
    headers=auth,
  )

  assert response.status_code == 202
  assert response.json() == _status()


def test_rebuild_error_preserves_stable_code(client, auth, monkeypatch):
  async def request():
    raise dc.DeploymentControlError(
      "image_not_ready",
      "The new container image is still publishing.",
      status_code=409,
    )

  monkeypatch.setattr(dc, "request_rebuild", request)

  response = client.post("/api/admin/rebuild", headers=auth)

  assert response.status_code == 409
  assert response.json()["detail"] == {
    "code": "image_not_ready",
    "message": "The new container image is still publishing.",
  }


def test_host_prepare_drains_only_the_matching_operation(
  client, auth, tmp_path, monkeypatch,
):
  operation = "a" * 32
  ready = tmp_path / "ready"
  calls = []
  monkeypatch.setattr(dc, "replacement_ready_path", lambda value: (
    ready if value == operation else (_ for _ in ()).throw(
      dc.DeploymentControlError("operation_mismatch", "wrong", status_code=409)
    )
  ))

  async def drain(path=None):
    calls.append(path)

  monkeypatch.setattr(admin_routes, "restart_this_worker", drain)

  response = client.post(
    "/api/admin/rebuild/prepare",
    json={"operation_id": operation},
    headers=auth,
  )

  assert response.status_code == 202
  assert calls == [ready]
