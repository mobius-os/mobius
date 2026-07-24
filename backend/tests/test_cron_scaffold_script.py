"""init-cron-scaffold.sh regressions."""

import os
import subprocess
from pathlib import Path


def test_init_cron_scaffold_does_not_splice_existing_crontab_into_comments(
  tmp_path,
):
  """A generated init script must not execute command substitutions in
  comments while it is being authored.

  Regression: a comment containing backticked `crontab -l` lived inside an
  unquoted heredoc, so the live crontab was inserted into init-cron.sh and
  later replayed as shell code.
  """
  app_base = tmp_path / "apps"
  app_dir = app_base / "reflection"
  app_dir.mkdir(parents=True)

  state = tmp_path / "crontab.txt"
  existing = "0 10 * * * /data/apps/news/fetch.sh 12\n"
  state.write_text(existing)

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    "state=\"$CRONTAB_STATE\"\n"
    "if [ \"$1\" = \"-u\" ]; then shift 2; fi\n"
    "case \"$1\" in\n"
    "  -l) [ -f \"$state\" ] && cat \"$state\" || exit 1 ;;\n"
    "  -) cat > \"$state\" ;;\n"
    "  *) echo \"bad crontab args: $*\" >&2; exit 2 ;;\n"
    "esac\n"
  )
  crontab.chmod(0o755)

  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "CRONTAB_STATE": str(state),
    "MOBIUS_APP_BASE": str(app_base),
    "MOBIUS_ALLOW_TEST_CRON": "1",
    "DATA_DIR": str(tmp_path / "data"),
  }
  script = Path(__file__).parents[1] / "scripts" / "init-cron-scaffold.sh"

  result = subprocess.run(
    [str(script), "reflection", "0 6 * * *", "fetch.sh", "46"],
    text=True,
    capture_output=True,
    env=env,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  init_text = (app_dir / "init-cron.sh").read_text()
  assert existing.strip() not in init_text
  assert "ENTRY=\"0 6 * * *" in init_text
  live_crontab = state.read_text()
  assert existing.strip() in live_crontab
  assert "0 6 * * *" in live_crontab


def test_init_cron_scaffold_refuses_test_runtime_before_any_write(tmp_path):
  """A production-container pytest must not reach durable or live cron state."""
  app_base = tmp_path / "apps"
  app_dir = app_base / "memory"
  app_dir.mkdir(parents=True)
  sentinel = tmp_path / "crontab-was-called"
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    f"touch {sentinel}\n"
  )
  crontab.chmod(0o755)

  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "MOBIUS_TEST_RUNTIME": "1",
    "MOBIUS_APP_BASE": str(app_base),
  }
  env.pop("MOBIUS_ALLOW_TEST_CRON", None)
  script = Path(__file__).parents[1] / "scripts" / "init-cron-scaffold.sh"

  result = subprocess.run(
    [str(script), "memory", "30 5 * * *", "fetch.sh", "57"],
    text=True,
    capture_output=True,
    env=env,
    check=False,
  )

  assert result.returncode == 78
  assert "disabled in the test runtime" in result.stderr
  assert not sentinel.exists()
  assert not (app_dir / "fetch.sh").exists()
  assert not (app_dir / "init-cron.sh").exists()
