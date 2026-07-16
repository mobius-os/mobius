"""The shared-edge deploy must preserve one gateway value end to end."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"
BLOCK_START = "# ── prod environment resolution"
BLOCK_END = "# ── end prod environment resolution"


def _resolution_source() -> str:
  text = SCRIPT.read_text()
  start = text.index(BLOCK_START)
  end = text.index(BLOCK_END, start)
  return text[start:end]


def _git(repo: Path, *args: str) -> str:
  env = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
  }
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    check=True, capture_output=True, text=True, env=env,
  ).stdout.strip()


@pytest.fixture
def linked_worktree(tmp_path):
  canonical = tmp_path / "canonical"
  linked = tmp_path / "linked"
  canonical.mkdir()
  _git(canonical, "init", "-q", "-b", "main")
  (canonical / "tracked").write_text("base")
  _git(canonical, "add", "tracked")
  _git(canonical, "commit", "-q", "-m", "base")
  _git(canonical, "worktree", "add", "-q", "-b", "linked", str(linked))
  return canonical, linked


def _run_resolution(
  repo_root: Path, *, gateway: str | None = None,
) -> subprocess.CompletedProcess:
  gateway_setup = (
    "unset MOBIUS_SERVICE_GATEWAY_ORIGIN"
    if gateway is None
    else f"MOBIUS_SERVICE_GATEWAY_ORIGIN={gateway!r}"
  )
  harness = textwrap.dedent(f"""\
    set -euo pipefail
    info() {{ printf 'INFO %s\\n' "$1" >&2; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    TARGET=prod
    DOMAIN=mobius.example.com
    REPO_ROOT={str(repo_root)!r}
    {gateway_setup}
    {_resolution_source()}
    resolve_prod_service_gateway_origin
    printf '%s' "$MOBIUS_SERVICE_GATEWAY_ORIGIN"
  """)
  return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


def test_preexported_domain_still_loads_gateway_from_canonical_checkout(
  linked_worktree,
):
  canonical, linked = linked_worktree
  (canonical / ".env").write_text(
    "MOBIUS_SERVICE_GATEWAY_ORIGIN=https://services.mobius.example.com/\n"
  )

  result = _run_resolution(linked)

  assert result.returncode == 0, result.stderr
  assert result.stdout == "https://services.mobius.example.com"
  assert f"loaded service gateway origin from {canonical / '.env'}" in result.stderr


def test_explicit_gateway_wins_over_canonical_checkout(linked_worktree):
  canonical, linked = linked_worktree
  (canonical / ".env").write_text(
    "MOBIUS_SERVICE_GATEWAY_ORIGIN=https://canonical.example.com\n"
  )

  result = _run_resolution(linked, gateway="https://explicit.example.com:443/")

  assert result.returncode == 0, result.stderr
  assert result.stdout == "https://explicit.example.com"
  assert "loaded service gateway origin from" not in result.stderr


@pytest.mark.parametrize("gateway", [
  "http://services.mobius.example.com",
  "https://mobius.example.com",
  "https://services.mobius.example.com/path",
  "https://services.mobius.example.com|bad",
])
def test_invalid_or_shell_gateway_is_rejected(tmp_path, gateway):
  result = _run_resolution(tmp_path, gateway=gateway)

  assert result.returncode != 0
  assert "must be a separate HTTPS origin" in result.stderr


def test_render_and_public_verification_share_the_resolved_variable():
  text = SCRIPT.read_text()
  assert 'local gw="$MOBIUS_SERVICE_GATEWAY_ORIGIN"' in text
  assert 'gw_origin="${MOBIUS_SERVICE_GATEWAY_ORIGIN:-}"' in text
  assert text.index("resolve_prod_service_gateway_origin") < text.index(
    "# ── proxy topology"
  )
