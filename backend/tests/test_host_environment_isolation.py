"""The suite cannot inherit the deployment identity of the host it runs on."""

import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent


def test_suite_preamble_drops_host_railway_identity(tmp_path):
  # Möbius instances hosted on Railway carry RAILWAY_* variables. Load the
  # shared test preamble in a fresh interpreter that starts with one, the way
  # a run launched inside such an instance would, and check the app still
  # sees the self-hosted default.
  probe = (
    "import os, conftest\n"
    "from app import platform_activation\n"
    "print([n for n in os.environ if n.startswith('RAILWAY_')],"
    " platform_activation.deployment_kind())\n"
  )
  env = {
    **os.environ,
    "RAILWAY_PROJECT_ID": "host-project",
    "PYTHONPATH": os.pathsep.join(
      filter(None, [str(_TESTS_DIR.parent), os.environ.get("PYTHONPATH")])
    ),
    "TMPDIR": str(tmp_path),
  }
  result = subprocess.run(
    [sys.executable, "-c", probe],
    cwd=_TESTS_DIR,
    env=env,
    capture_output=True,
    text=True,
    timeout=120,
  )
  assert result.returncode == 0, result.stderr
  assert result.stdout.strip().splitlines()[-1] == "[] self_hosted"
