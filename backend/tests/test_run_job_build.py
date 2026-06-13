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


def test_run_job_honors_manifest_over_stale_sibling(client, auth):
  """The manifest's schedule.job wins even when it names a script the legacy
  probe never looks for, AND a stale probe-named sibling is present.

  This is the tandem regression: a renamed job (generate.sh) shipped in
  mobius.json while a stale job.sh lingered in the tree — the old probe-only
  resolver ran job.sh, shadowing the script the app actually ships."""
  source_dir = _app_source("tandem")
  generate = source_dir / "generate.sh"
  generate.write_text("#!/bin/bash\necho generating\n")
  generate.chmod(0o755)
  # A stale sibling the legacy probe WOULD have picked first.
  stale = source_dir / "job.sh"
  stale.write_text("#!/bin/bash\necho stale\n")
  stale.chmod(0o755)
  (source_dir / "mobius.json").write_text(
    '{"schedule": {"default": "0 * * * *", "job": "generate.sh"}}'
  )

  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    mock_popen.return_value = MagicMock()
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)

  assert r.status_code == 202, r.text
  args, _ = mock_popen.call_args
  # generate.sh wins, NOT the stale job.sh the probe would have grabbed.
  assert args[0] == ["bash", str(generate), str(app_id)]


def test_run_job_falls_back_to_probe_for_manifestless_app(client, auth):
  """Legacy apps with no mobius.json still resolve via the probe → fetch.sh
  wins over job.sh by priority order, unchanged behavior."""
  source_dir = _app_source("legacy")
  fetch = source_dir / "fetch.sh"
  fetch.write_text("#!/bin/bash\necho fetching\n")
  fetch.chmod(0o755)
  (source_dir / "job.sh").write_text("#!/bin/bash\necho job\n")
  assert not (source_dir / "mobius.json").exists()

  app_id = _make_app(client, auth, source_dir)

  with patch("app.routes.apps.subprocess.Popen") as mock_popen:
    mock_popen.return_value = MagicMock()
    r = client.post(f"/api/apps/{app_id}/run-job", headers=auth)

  assert r.status_code == 202, r.text
  args, _ = mock_popen.call_args
  assert args[0] == ["bash", str(fetch), str(app_id)]
