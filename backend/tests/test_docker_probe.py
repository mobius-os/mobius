from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "docker-probe.sh"


def test_docker_probe_scripts_parse():
  for script in (HELPER, ROOT / "scripts" / "test-docker-probe.sh"):
    result = subprocess.run(
      ["bash", "-n", str(script)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_direct_docker_runs_use_the_lifecycle_helper():
  test_wrapper = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
  preship = (ROOT / "scripts" / "preship-gate.sh").read_text(encoding="utf-8")
  workflow = (
    ROOT / ".github" / "workflows" / "test.yml"
  ).read_text(encoding="utf-8")

  assert "docker-probe.sh" in test_wrapper
  assert "docker-probe.sh" in preship
  assert "scripts/docker-probe.sh" in workflow
  assert "scripts/test-docker-probe.sh" in workflow


def test_helper_owns_exact_container_identity_and_cleanup():
  helper = HELPER.read_text(encoding="utf-8")

  assert '--cidfile "$CID_FILE"' in helper
  assert '--name "$PROBE_NAME"' in helper
  assert 'docker rm -f "$ref"' in helper
  assert 'docker inspect "$ref"' in helper
  assert "trap cleanup EXIT" in helper
  assert "trap 'exit 143' TERM" in helper
  assert "io.mobius.probe.started_at" in helper
  assert "docker stats --no-stream" in helper
