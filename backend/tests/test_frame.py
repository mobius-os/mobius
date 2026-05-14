"""Tests for the mini-app frame endpoint."""


def test_frame_injects_app_id(client, owner_token):
  """GET /api/apps/{id}/frame replaces the placeholder app ID."""
  r = client.post("/api/apps/", json={
    "name": "frame-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200
  html = r.text
  assert f"var _FRAME_APP_ID = \"{app_id}\"" in html
  assert "var _FRAME_APP_ID = 'unknown'" not in html


def test_frame_has_no_unset_origin(client, owner_token):
  """The frame must not contain _FRAME_PARENT_ORIGIN = 'UNSET'.

  Regression test for 2d17109: the frame-origin refactor switched the
  module script to window.location.origin but left the error panel's
  reportError() using the old _FRAME_PARENT_ORIGIN variable, which the
  server no longer substitutes. postMessage(..., 'UNSET') silently fails,
  so "Tell agent to fix" was broken for crashed apps.
  """
  r = client.post("/api/apps/", json={
    "name": "origin-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "UNSET" not in html
  assert "_FRAME_PARENT_ORIGIN" not in html


def test_frame_report_error_uses_location_origin(client, owner_token):
  """reportError() must postMessage with window.location.origin."""
  r = client.post("/api/apps/", json={
    "name": "report-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "window.location.origin)" in html


def test_frame_is_public(client, owner_token):
  """Frame endpoint does not require authentication."""
  r = client.post("/api/apps/", json={
    "name": "public-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  # No auth header — should still work.
  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200
