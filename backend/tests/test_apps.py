"""App registry lifecycle tests."""

from pathlib import Path

from app.config import get_settings


def test_delete_app_removes_non_slug_source_dir(client, auth):
  """Delete uses source_dir rather than the display-name slug."""
  source_dir = Path(get_settings().data_dir) / "apps" / "My App (draft)"
  source_dir.mkdir(parents=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )

  r = client.post("/api/apps/", json={
    "name": "My App (draft)",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201
  app_id = r.json()["id"]

  r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204
  assert not source_dir.exists()
