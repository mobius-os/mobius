"""Owner app opens drive durable mixed-drawer recency."""

from datetime import timedelta

from app import models


def _app(db):
  app = models.App(
    slug="test-app-recency-9",
    source_dir="/tmp/mobius-tests/test-app-recency-9",
    name="Atlas",
    description="",
    jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _listed(client, auth, app_id):
  response = client.get("/api/apps/", headers=auth)
  assert response.status_code == 200, response.text
  return next(row for row in response.json() if row["id"] == app_id)


def test_open_records_durable_recency_without_rotating_bundle_version(
  client, auth, db,
):
  app = _app(db)
  original_updated_at = app.updated_at
  assert _listed(client, auth, app.id)["last_opened_at"] is None

  opened = client.post(f"/api/apps/{app.id}/opened", headers=auth)
  assert opened.status_code == 204, opened.text

  row = _listed(client, auth, app.id)
  assert row["last_opened_at"] is not None
  db.refresh(app)
  assert app.updated_at == original_updated_at


def test_reopening_advances_the_existing_recency_row(client, auth, db):
  app = _app(db)
  assert client.post(f"/api/apps/{app.id}/opened", headers=auth).status_code == 204
  state = db.get(models.AppRecencyState, app.id)
  first = state.last_opened_at

  state.last_opened_at = first.replace(year=first.year - 1)
  db.commit()
  assert client.post(f"/api/apps/{app.id}/opened", headers=auth).status_code == 204
  db.refresh(state)
  assert state.last_opened_at > first
  assert db.query(models.AppRecencyState).count() == 1


def test_delayed_open_never_moves_a_newer_recency_marker_backward(
  client, auth, db,
):
  app = _app(db)
  future = app.updated_at + timedelta(days=1)
  db.add(models.AppRecencyState(
    app_id=app.id,
    last_opened_at=future,
  ))
  db.commit()

  assert client.post(f"/api/apps/{app.id}/opened", headers=auth).status_code == 204
  state = db.get(models.AppRecencyState, app.id)
  assert state.last_opened_at == future


def test_open_requires_owner_and_rejects_cross_site(client, auth, db):
  app = _app(db)
  assert client.post(f"/api/apps/{app.id}/opened").status_code == 401
  cross_site = client.post(
    f"/api/apps/{app.id}/opened",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross_site.status_code == 403
  assert db.get(models.AppRecencyState, app.id) is None


def test_open_rejects_unknown_app(client, auth, db):
  response = client.post("/api/apps/999999/opened", headers=auth)
  assert response.status_code == 404
  assert db.query(models.AppRecencyState).count() == 0
