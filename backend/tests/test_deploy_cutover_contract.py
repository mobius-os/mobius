"""Build-time contract for the shell-owned production cutover loop."""

from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "deploy-prod.sh"


def test_cutover_calls_named_probe_functions_without_eval():
  source = SCRIPT.read_text(encoding="utf-8")

  assert 'eval "$probe"' not in source
  assert 'eval "$diagnostic"' not in source
  assert 'code=$("$probe")' in source
  assert 'wait_for_cutover \\\n  "health_code"' in source
  assert 'wait_for_cutover "ready_code"' in source
