"""Accepted app services own policy; the platform owns their hard boundary."""

from pathlib import Path

from app import auth as auth_tokens, models
from app.applied_app_runtime import runtime_parent
from app.config import get_settings


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
