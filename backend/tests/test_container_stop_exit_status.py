"""A planned container stop must exit 0; genuine failures must not.

Railway reports any non-zero container exit as "Deployment crashed" and emails
the owner. Every planned Möbius stop ends in SIGTERM, which `su` and uvicorn
re-raise after shutting down, so the image's init must map that one status to
a clean exit while leaving real failures (and SIGKILL/OOM) visible.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _image_entrypoint() -> list[str]:
  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  match = re.search(r"^ENTRYPOINT\s+(\[.*\])\s*$", dockerfile, re.MULTILINE)
  assert match, "Dockerfile must declare an exec-form ENTRYPOINT"
  return json.loads(match.group(1))


def _exit_status_under_entrypoint(child_script: str) -> int:
  entrypoint = _image_entrypoint()
  if not Path(entrypoint[0]).exists() and not shutil.which(entrypoint[0]):
    pytest.skip(f"{entrypoint[0]} is not installed in this environment")
  return subprocess.run(
    [*entrypoint, "sh", "-c", child_script], timeout=30, check=False,
  ).returncode


def test_planned_sigterm_stop_exits_clean_so_railway_does_not_report_a_crash():
  assert _exit_status_under_entrypoint("kill -TERM $$") == 0


def test_genuine_failure_keeps_its_nonzero_exit_status():
  assert _exit_status_under_entrypoint("exit 3") == 3


def test_sigkill_and_oom_kill_still_report_a_crash():
  assert _exit_status_under_entrypoint("kill -KILL $$") == 137
