"""POST /api/apps/{app_id}/run-job — manual cron trigger.

Mini-apps can't shell out, so the Reports tab's "Generate now" button
posts here to spawn the same job the scheduled cron entry would run.
The endpoint is owner-only, non-blocking (subprocess.Popen, no wait),
and returns 202 with a started_at timestamp.

We patch `subprocess.Popen` in `app.routes.apps` so tests don't
actually spawn anything — they verify the right argv is built from
the right (source_dir, job_name) pair.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from app.config import get_settings


JSX = "export default function App() { return <div>ok</div> }"


def _app_source(name):
  """A production-valid source dir (immediate child of /data/apps) — the
  shape create_app's source_dir validation requires. The fresh_db fixture
  wipes /data/apps between tests, so reusing a name across tests is safe."""
  d = Path(get_settings().data_dir) / "apps" / name
  d.mkdir(parents=True, exist_ok=True)
  return d


def _make_app(client, auth, source_dir):
  """Creates an app row with source_dir set to the given path."""
  r = client.post("/api/apps/", json={
    "name": "News",
    "description": "test",
    "jsx_source": JSX,
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text
  return r.json()["id"]


def test_run_job_spawns_fetch_sh(client, auth, tmp_path):
  """Happy path enters the supervised wrapper with app id + fetch.sh."""
  source_dir = _app_source("news")
  fetch = source_dir / "fetch.sh"
  fetch.write_text("#!/bin/bash\necho ok\n")
  fetch.chmod(0o755)

  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    mock_popen.return_value = MagicMock()
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)
    assert r.status_code == 202, r.text
    body = r.json()
    assert "started_at" in body
    # Exactly one spawn, with the expected argv.
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    argv = args[0]
    assert argv[-2:] == [str(app_id), str(fetch)]
    assert argv[1].endswith("app-job-runner.py")
    assert kwargs.get("cwd") == str(source_dir)


def test_run_job_falls_back_to_job_sh(client, auth, tmp_path):
  """If fetch.sh is missing, job.sh (install-default) is used."""
  source_dir = _app_source("other")
  job = source_dir / "job.sh"
  job.write_text("#!/bin/bash\necho ok\n")
  job.chmod(0o755)

  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    mock_popen.return_value = MagicMock()
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)
    assert r.status_code == 202
    argv = mock_popen.call_args[0][0]
    assert argv[-2:] == [str(app_id), str(job)]


def test_run_job_404_for_unknown_app(client, auth):
  """An app id that doesn't exist returns 404, no spawn."""
  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    r = client.post("/api/apps/99999/run-job", headers=auth)
    assert r.status_code == 404
    mock_popen.assert_not_called()


def test_run_job_400_when_no_script(client, auth, tmp_path):
  """Source dir exists but contains neither fetch.sh nor job.sh → 400."""
  source_dir = _app_source("empty")
  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)
    assert r.status_code == 400
    mock_popen.assert_not_called()


def test_run_job_requires_owner_token(client, tmp_path):
  """No token → 401."""
  r = client.post("/api/apps/1/run-job")
  assert r.status_code == 401


def test_run_job_rejects_cross_site(client, auth, tmp_path):
  """Sec-Fetch-Site: cross-site → 403, no spawn."""
  source_dir = _app_source("news")
  (source_dir / "fetch.sh").write_text("#!/bin/bash\n")
  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    r = client.post(
      f"/api/apps/{app_id}/run-job",
      headers={**auth, "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    mock_popen.assert_not_called()
