"""Server-only secrets never reach anything the server starts."""
import os
import subprocess
import sys

from app import config


def test_children_started_after_startup_do_not_inherit_secret_key(monkeypatch):
  monkeypatch.setenv("SECRET_KEY", os.environ["SECRET_KEY"])
  expected = config.get_settings().secret_key

  config.withhold_server_secrets_from_children()

  child = subprocess.run(
    [sys.executable, "-c", "import os; print('SECRET_KEY' in os.environ)"],
    capture_output=True, text=True, check=True,
  )
  assert child.stdout.strip() == "False"
  # The running server still holds the key in memory.
  assert config.get_settings().secret_key == expected


def test_server_start_withholds_secrets_before_anything_is_spawned():
  """The lifespan withholds them first, before startup work can spawn."""
  import inspect
  from app import main
  source = inspect.getsource(main.lifespan)
  assert source.index("withhold_server_secrets_from_children()") < source.index(
    "run_startup_plan"
  )


def test_a_child_without_the_key_reads_the_persisted_key(monkeypatch, tmp_path):
  """Children never inherit SECRET_KEY, so settings fall back to the key the
  entrypoint persisted, as the entrypoint itself does."""
  (tmp_path / ".secret-key").write_text("k" * 40 + "\n")
  monkeypatch.delenv("SECRET_KEY", raising=False)
  monkeypatch.setenv("DATA_DIR", str(tmp_path))

  assert config.Settings().secret_key == "k" * 40


def test_restart_startup_check_needs_no_real_key(monkeypatch, tmp_path):
  """The startup check only proves the source imports; it passes with the key
  withheld and no persisted key file."""
  from app import restart_util
  seen = {}

  def fake_run(command, **kwargs):
    seen.update(kwargs["env"])
    return subprocess.CompletedProcess(command, 0, "", "")

  monkeypatch.delenv("SECRET_KEY", raising=False)
  monkeypatch.setattr(restart_util.subprocess, "run", fake_run)
  root = tmp_path / "platform"
  (root / "backend" / "app").mkdir(parents=True)
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(root))

  restart_util.validate_restart_source()
  assert len(seen.get("SECRET_KEY", "")) >= 32


def test_update_import_probe_needs_no_real_key(monkeypatch, tmp_path):
  """The post-merge import probe runs after the server withheld its key, on
  installs that pin SECRET_KEY in the environment and persist no key file."""
  from app import platform_update
  seen = {}

  def fake_run(command, **kwargs):
    seen.update(kwargs["env"])
    return subprocess.CompletedProcess(command, 0, "", "")

  monkeypatch.delenv("SECRET_KEY", raising=False)
  monkeypatch.setattr(platform_update.subprocess, "run", fake_run)

  assert platform_update._import_probe(tmp_path) == (True, "")
  assert len(seen.get("SECRET_KEY", "")) >= 32
