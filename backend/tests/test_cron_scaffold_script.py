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
    "API_BASE_URL": "http://jobs.example.test:8123",
    "MOBIUS_APP_JOB_RUNNER": "/live/scripts/app-job-runner.py",
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
  assert "API_BASE_URL=http://jobs.example.test:8123" in init_text
  assert "/live/scripts/app-job-runner.py 46" in init_text
  live_crontab = state.read_text()
  assert existing.strip() in live_crontab
  assert "0 6 * * *" in live_crontab
  assert "API_BASE_URL=http://jobs.example.test:8123" in live_crontab


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


def test_zone_declaration_is_durable_before_live_crontab_changes(tmp_path):
  """The live writer must observe the complete durable IANA identity."""
  app_base = tmp_path / "apps"
  app_dir = app_base / "memory"
  app_dir.mkdir(parents=True)
  (app_dir / "fetch.sh").write_text("#!/bin/sh\n")
  init_path = app_dir / "init-cron.sh"
  state = tmp_path / "crontab.txt"

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    "state=\"$CRONTAB_STATE\"\n"
    "if [ \"$1\" = \"-u\" ]; then shift 2; fi\n"
    "case \"$1\" in\n"
    "  -l) echo 'no crontab for mobius' >&2; exit 1 ;;\n"
    "  -)\n"
    "    grep -q '^SCHEDULE_TZ=\"Europe/Belgrade\"$' \"$EXPECTED_INIT\" || exit 41\n"
    "    grep -q '^SCHEDULE_SOURCE=\"30 2 \\* \\* \\*\"$' \"$EXPECTED_INIT\" || exit 42\n"
    "    cat > \"$state\" ;;\n"
    "  *) exit 2 ;;\n"
    "esac\n"
  )
  crontab.chmod(0o755)
  argv_state = tmp_path / "runner-argv.txt"
  python = fake_bin / "python3"
  python.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$@\" > \"$RUNNER_ARGV_STATE\"\n"
  )
  python.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "CRONTAB_STATE": str(state),
    "EXPECTED_INIT": str(init_path),
    "RUNNER_ARGV_STATE": str(argv_state),
    "MOBIUS_APP_BASE": str(app_base),
    "MOBIUS_ALLOW_TEST_CRON": "1",
    "DATA_DIR": str(tmp_path / "data"),
    "MOBIUS_APP_JOB_RUNNER": "/live/scripts/app-job-runner.py",
  }
  script = Path(__file__).parents[1] / "scripts" / "init-cron-scaffold.sh"

  result = subprocess.run(
    [
      str(script), "memory", "* * * * *", "fetch.sh", "57",
      "Europe/Belgrade", "30 2 * * *",
    ],
    text=True, capture_output=True, env=env, check=False,
  )

  assert result.returncode == 0, result.stderr
  assert "--wall-clock Europe/Belgrade" in init_path.read_text()
  live_entry = state.read_text().strip()
  assert live_entry.startswith("* * * * *")
  command = live_entry.split(maxsplit=5)[5]
  subprocess.run(["bash", "-c", command], env=env, check=True)
  assert argv_state.read_text().splitlines() == [
    "/live/scripts/app-job-runner.py",
    "--wall-clock",
    "Europe/Belgrade",
    "30 2 * * *",
    "57",
    str(app_dir / "fetch.sh"),
  ]


def test_durable_replace_failure_cannot_change_live_crontab(tmp_path):
  """A failed atomic declaration write stops before any live side effect."""
  app_base = tmp_path / "apps"
  app_dir = app_base / "memory"
  app_dir.mkdir(parents=True)
  (app_dir / "fetch.sh").write_text("#!/bin/sh\n")
  init_path = app_dir / "init-cron.sh"
  init_path.write_text("old durable declaration\n")
  touched = tmp_path / "crontab-called"

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  mv = fake_bin / "mv"
  mv.write_text("#!/bin/sh\nexit 23\n")
  mv.chmod(0o755)
  crontab = fake_bin / "crontab"
  crontab.write_text(f"#!/bin/sh\ntouch {touched}\nexit 0\n")
  crontab.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "MOBIUS_APP_BASE": str(app_base),
    "MOBIUS_ALLOW_TEST_CRON": "1",
    "DATA_DIR": str(tmp_path / "data"),
  }
  script = Path(__file__).parents[1] / "scripts" / "init-cron-scaffold.sh"

  result = subprocess.run(
    [
      str(script), "memory", "* * * * *", "fetch.sh", "57",
      "Europe/Belgrade", "30 2 * * *",
    ],
    text=True, capture_output=True, env=env, check=False,
  )

  assert result.returncode == 23
  assert init_path.read_text() == "old durable declaration\n"
  assert not touched.exists()


def test_live_write_failure_leaves_complete_durable_retry_point(tmp_path):
  """After the durable commit, a live failure is honest and retryable."""
  app_base = tmp_path / "apps"
  app_dir = app_base / "memory"
  app_dir.mkdir(parents=True)
  (app_dir / "fetch.sh").write_text("#!/bin/sh\n")
  init_path = app_dir / "init-cron.sh"
  state = tmp_path / "crontab.txt"
  state.write_text("0 9 * * * /data/apps/news/fetch.sh 12\n")

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    "state=\"$CRONTAB_STATE\"\n"
    "if [ \"$1\" = \"-u\" ]; then shift 2; fi\n"
    "case \"$1\" in\n"
    "  -l) cat \"$state\" ;;\n"
    "  -) cat >/dev/null; exit 17 ;;\n"
    "  *) exit 2 ;;\n"
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
    [
      str(script), "memory", "* * * * *", "fetch.sh", "57",
      "Europe/Belgrade", "30 2 * * *",
    ],
    text=True, capture_output=True, env=env, check=False,
  )

  assert result.returncode == 17
  assert state.read_text() == "0 9 * * * /data/apps/news/fetch.sh 12\n"
  init_text = init_path.read_text()
  assert 'SCHEDULE_TZ="Europe/Belgrade"' in init_text
  assert 'SCHEDULE_SOURCE="30 2 * * *"' in init_text
