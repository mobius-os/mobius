"""POST /api/apps/{app_id}/run-job — build.sh candidate.

Invariant: when a source_dir contains only build.sh (no fetch.sh, no
job.sh), run-job resolves build.sh as the job script and spawns it.

Mirrors test_cron_run.py's structure — patch subprocess.Popen in
app.routes.apps so no process is actually spawned.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import get_settings


JSX = "export default function App() { return <div>ok</div> }"


def _app_source(name):
  """A production-valid source dir under /data/apps/<name>.
  fresh_db wipes /data/apps between tests so reusing a name is safe."""
  d = Path(get_settings().data_dir) / "apps" / name
  d.mkdir(parents=True, exist_ok=True)
  return d


def _make_app(client, auth, source_dir):
  """Creates an app row with source_dir set to the given path."""
  r = client.post("/api/apps/", json={
    "name": "LaTeX",
    "description": "test",
    "jsx_source": JSX,
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text
  return r.json()["id"]


def test_run_job_resolves_build_sh(client, auth):
  """build.sh is the only script present → run-job uses it, returns 202."""
  source_dir = _app_source("latex")
  build = source_dir / "build.sh"
  build.write_text("#!/bin/bash\necho building\n")
  build.chmod(0o755)

  # Confirm no fetch.sh / job.sh present — ensures build.sh wins by being
  # the sole candidate, not by priority position.
  assert not (source_dir / "fetch.sh").exists()
  assert not (source_dir / "job.sh").exists()

  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    mock_popen.return_value = MagicMock()
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)

  assert r.status_code == 202, r.text
  assert "started_at" in r.json()

  mock_popen.assert_called_once()
  args, kwargs = mock_popen.call_args
  argv = args[0]
  # Exactly bash + build.sh path + app_id as positional arg.
  assert argv == ["bash", str(build), str(app_id)]
  assert kwargs.get("cwd") == str(source_dir)
