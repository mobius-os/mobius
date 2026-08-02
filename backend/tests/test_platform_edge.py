"""The updater's public app-frame policy migration contract."""

import os
import re
import stat
import subprocess
from pathlib import Path

from app.platform_edge import (
  APP_FRAME_EDGE_PROBE_PATH,
  app_frame_edge_preflight_passes,
  csp_allows_blob_modules,
)


# Exact resource policy served to every frame by the bundled Caddyfile before
# the brokered blob loader landed. The old matcher used this ordinary shell CSP
# for `/api/apps/*/frame`, producing the real version-skew outage from #114.
OLD_BUNDLED_APP_FRAME_CSP = (
  "default-src 'self'; script-src 'self' 'unsafe-inline' https://esm.sh; "
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
  "font-src 'self' https://fonts.gstatic.com https://cdn.openai.com; "
  "connect-src 'self'; img-src 'self' data: blob:; frame-src 'self' "
  "{$MOBIUS_SERVICE_GATEWAY_ORIGIN}; frame-ancestors 'self'"
)


def _current_bundled_app_frame_csp() -> str:
  source = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text()
  line = next(
    line.strip() for line in source.splitlines()
    if line.strip().startswith(
      "header @appFrame >Content-Security-Policy "
    )
  )
  match = re.search(r'Content-Security-Policy "(.*)"$', line)
  assert match is not None
  return match.group(1)


def test_real_old_bundled_policy_is_rejected_before_new_frame_activation():
  assert csp_allows_blob_modules(OLD_BUNDLED_APP_FRAME_CSP) is False
  assert csp_allows_blob_modules(_current_bundled_app_frame_csp()) is True


def test_every_enforced_policy_must_allow_blob_modules():
  assert csp_allows_blob_modules(None) is True
  assert csp_allows_blob_modules("sandbox allow-scripts") is True
  assert csp_allows_blob_modules(
    "default-src 'self'; script-src 'self' blob:, sandbox allow-scripts"
  ) is True
  assert csp_allows_blob_modules(
    "script-src 'self' blob:, default-src 'self'"
  ) is False
  assert csp_allows_blob_modules(
    "script-src 'self' blob:; script-src-elem 'self'"
  ) is False
  assert csp_allows_blob_modules(
    "script-src; default-src 'self' blob:"
  ) is False


def test_preflight_evidence_is_bound_to_the_frame_policy_path():
  csp = "default-src 'self'; script-src 'self' blob:"
  assert app_frame_edge_preflight_passes(
    path=APP_FRAME_EDGE_PROBE_PATH,
    content_security_policy=csp,
  ) is True
  assert app_frame_edge_preflight_passes(
    path="/shell/",
    content_security_policy=csp,
  ) is False


def test_self_hosted_update_validates_then_reloads_the_running_edge(tmp_path):
  root = Path(__file__).resolve().parents[2]
  script_path = root / "scripts" / "reload-caddy.sh"
  readme = (root / "README.md").read_text()

  binary_dir = tmp_path / "bin"
  binary_dir.mkdir()
  call_log = tmp_path / "docker-calls"
  docker = binary_dir / "docker"
  docker.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >> \"$MOBIUS_DOCKER_CALL_LOG\"\n"
    "if [ \"$*\" = 'compose ps --status running --services' ]; then\n"
    "  printf '%s\\n' caddy\n"
    "fi\n"
  )
  docker.chmod(0o755)
  env = dict(os.environ)
  env["PATH"] = f"{binary_dir}:{env['PATH']}"
  env["MOBIUS_DOCKER_CALL_LOG"] = str(call_log)

  result = subprocess.run(
    [str(script_path)],
    cwd=root,
    env=env,
    capture_output=True,
    text=True,
    check=False,
  )

  assert script_path.stat().st_mode & stat.S_IXUSR
  assert result.returncode == 0, result.stderr
  calls = call_log.read_text().splitlines()
  assert calls == [
    "compose version",
    "compose ps --status running --services",
    (
      "compose exec -T caddy caddy validate "
      "--config /etc/caddy/Caddyfile --adapter caddyfile"
    ),
    (
      "compose exec -T caddy caddy reload "
      "--config /etc/caddy/Caddyfile --adapter caddyfile"
    ),
  ]
  assert result.stdout.strip() == "reload-caddy: active edge policy reloaded."
  assert "scripts/reload-caddy.sh" in readme
  update_section = readme.index("Update a self-hosted instance inside Möbius:")
  reload_instruction = readme.index("scripts/reload-caddy.sh", update_section)
  retry_instruction = readme.index("before retrying Apply", update_section)
  assert reload_instruction < retry_instruction
