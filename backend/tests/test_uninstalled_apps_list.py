"""The Apps screen's "Recently uninstalled" list: apps in their recovery window."""

from datetime import datetime, timedelta

from app import models
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc
from test_app_fixtures import create_local_app


def _listed(client, auth):
  response = client.get("/api/apps/uninstalled", headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def test_an_uninstalled_app_is_listed_with_its_expiry_until_recovered(client, auth):
  app = create_local_app(client, auth, name="Doomed")
  assert _listed(client, auth) == []

  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  [row] = _listed(client, auth)
  assert (row["id"], row["name"]) == (app["id"], "Doomed")
  # Timestamps carry a timezone so the browser does not read them as local time.
  assert row["deleted_at"].endswith(("Z", "+00:00"))
  stamp = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
  assert stamp(row["expires_at"]) - stamp(row["deleted_at"]) == SOFT_DELETE_TTL

  assert client.post(f"/api/apps/{app['id']}/recover", headers=auth).status_code == 200
  assert _listed(client, auth) == []
  assert [a["id"] for a in client.get("/api/apps/", headers=auth).json()] == [app["id"]]


def test_an_app_past_its_window_and_live_apps_are_not_listed(client, auth, db):
  live = create_local_app(client, auth, name="Live")
  gone = create_local_app(client, auth, name="Expired")
  assert client.delete(f"/api/apps/{gone['id']}", headers=auth).status_code == 204
  row = db.get(models.App, gone["id"])
  row.deleted_at = now_naive_utc() - SOFT_DELETE_TTL - timedelta(hours=1)
  db.commit()
  assert live["id"] not in {r["id"] for r in _listed(client, auth)}
  assert _listed(client, auth) == []


def test_each_row_says_how_long_is_left_by_the_server_clock(client, auth, db):
  # The browser counts down from this, not from expires_at, so a device clock
  # that runs fast cannot hide an app the server would still recover.
  app = create_local_app(client, auth, name="Nearly gone")
  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  row = db.get(models.App, app["id"])
  row.deleted_at = now_naive_utc() - SOFT_DELETE_TTL + timedelta(hours=2)
  db.commit()
  [listed] = _listed(client, auth)
  assert 2 * 3600 - 60 < listed["seconds_left"] <= 2 * 3600


def test_the_list_needs_a_signed_in_owner_or_an_app_that_manages_apps(client):
  assert client.get("/api/apps/uninstalled").status_code in (401, 403)


def test_the_list_carries_a_small_icon_and_colours_for_the_row(client, auth, db):
  import base64
  import io

  from PIL import Image

  plain = create_local_app(client, auth, name="Plain")
  iconic = create_local_app(client, auth, name="Iconic")
  buffer = io.BytesIO()
  Image.new("RGBA", (256, 256), (200, 40, 40, 255)).save(buffer, format="PNG")
  row = db.get(models.App, iconic["id"])
  row.icon_png = buffer.getvalue()
  row.background_color = "#112233"
  db.commit()
  for app in (plain, iconic):
    assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204

  listed = {r["name"]: r for r in _listed(client, auth)}
  assert listed["Plain"]["icon_data_url"] is None
  assert listed["Iconic"]["background_color"] == "#112233"
  prefix = "data:image/png;base64,"
  assert listed["Iconic"]["icon_data_url"].startswith(prefix)
  icon = Image.open(io.BytesIO(base64.b64decode(listed["Iconic"]["icon_data_url"][len(prefix):])))
  assert icon.size == (64, 64)  # a thumbnail, not the stored 256px artwork


def test_the_list_runs_from_the_oldest_removal_to_the_newest(client, auth):
  names = ["First", "Second", "Third"]
  ids = [create_local_app(client, auth, name=name)["id"] for name in names]
  # Removed in a different order from how they were installed.
  for app_id in (ids[1], ids[2], ids[0]):
    assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert [row["name"] for row in _listed(client, auth)] == ["Second", "Third", "First"]


def test_the_list_says_where_each_app_came_from_so_the_store_can_match_its_pages(client, auth, db):
  app = create_local_app(client, auth, name="From the store")
  row = db.get(models.App, app["id"])
  row.manifest_url = "https://raw.githubusercontent.com/mobius-os/app-demo/main#manifest-id=demo"
  row.package_id = "app.example.demo"
  db.commit()
  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  [listed] = _listed(client, auth)
  assert listed["manifest_url"].endswith("#manifest-id=demo")
  assert listed["package_id"] == "app.example.demo"
  assert listed["source_manifest"] == {
    "id": "demo", "url": "https://raw.githubusercontent.com/mobius-os/app-demo/main/mobius.json",
  }


def _purge(client, auth, app_id):
  return client.delete(f"/api/apps/{app_id}/permanent", headers=auth)


def test_an_uninstalled_app_can_be_erased_before_its_window_ends(client, auth, db):
  keep = create_local_app(client, auth, name="Keep")
  gone = create_local_app(client, auth, name="Erase me")
  assert client.delete(f"/api/apps/{gone['id']}", headers=auth).status_code == 204

  assert _purge(client, auth, gone["id"]).status_code == 204

  db.expire_all()
  assert db.get(models.App, gone["id"]) is None
  assert _listed(client, auth) == []
  # Recovering it is no longer possible, and the other app is untouched.
  assert client.post(f"/api/apps/{gone['id']}/recover", headers=auth).status_code == 404
  assert [a["id"] for a in client.get("/api/apps/", headers=auth).json()] == [keep["id"]]


def test_a_live_or_unknown_app_cannot_be_erased_this_way(client, auth, db):
  live = create_local_app(client, auth, name="Still installed")
  assert _purge(client, auth, live["id"]).status_code == 404
  assert _purge(client, auth, 987654).status_code == 404
  db.expire_all()
  assert db.get(models.App, live["id"]) is not None


def test_erasing_needs_the_owner_and_is_refused_while_agent_work_is_running(client, auth, db, monkeypatch):
  app = create_local_app(client, auth, name="Busy")
  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  assert client.delete(f"/api/apps/{app['id']}/permanent").status_code in (401, 403)

  import app.delegations as delegations
  monkeypatch.setattr(delegations, "active_delegation_ids_for_app", lambda _db, _id: [1])
  refused = _purge(client, auth, app["id"])
  assert refused.status_code == 409
  assert refused.json()["detail"] == "Agents are still working for this app. Try again when they finish."
  db.expire_all()
  # Nothing was lost: it is still in the recovery window.
  assert [row["id"] for row in _listed(client, auth)] == [app["id"]]
