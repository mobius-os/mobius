"""deploy-prod.sh must broadcast `shell_rebuilt` after a successful deploy.

An already-open PWA never learns a fresh bundle exists — it keeps running the
old shell until reopened. The Shell already handles a `shell_rebuilt` system
event (fade + reload), and POST /api/notify already broadcasts that type to
every open Shell's /api/events/system stream. The deploy script fires it after
verification, authenticated with the entrypoint's owner service token.

These tests guard that wiring against a future edit silently dropping it,
without needing Docker: they read the script text and assert the call is
present and lands AFTER the final readiness gate (so the reload always points
at a verified-healthy bundle), plus that the script still parses under
`bash -n`.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Repo-root scripts/, not backend/scripts/ — deploy-prod.sh lives at the top.
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"


def _read() -> str:
  return SCRIPT.read_text()


def test_deploy_script_exists():
  assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_broadcast_helper_posts_shell_rebuilt_to_notify():
  """The helper authenticates with the service token and POSTs the
  shell_rebuilt event to /api/notify."""
  text = _read()
  assert "broadcast_shell_rebuilt()" in text
  helper = text.split("broadcast_shell_rebuilt()", 1)[1]
  helper = helper.split("\n}\n", 1)[0]
  assert "/data/service-token.txt" in helper, "must use the owner service token"
  assert "/api/notify" in helper
  assert "shell_rebuilt" in helper
  assert "Authorization: Bearer" in helper


def test_broadcast_call_is_present_and_invokes_helper():
  text = _read()
  # The step is invoked (not just defined).
  assert re.search(r"\bif broadcast_shell_rebuilt;", text), \
    "deploy must actually call broadcast_shell_rebuilt"


def test_broadcast_runs_after_final_readiness_gate():
  """The reload must only fire on a verified-healthy deploy, so the call has
  to come AFTER the final readiness wait and the verify block."""
  text = _read()
  ready_gate = text.rindex("rcode=$(ready_code)")
  call_site = text.index("if broadcast_shell_rebuilt;")
  assert call_site > ready_gate, \
    "broadcast must run after the final readiness gate"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_deploy_script_still_parses():
  """A bash syntax error in the script would break every deploy — guard it."""
  result = subprocess.run(
    ["bash", "-n", str(SCRIPT)],
    capture_output=True, text=True,
  )
  assert result.returncode == 0, result.stderr
