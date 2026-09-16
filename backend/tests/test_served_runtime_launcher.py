"""The frozen loader admits only valid served privileged source."""

import importlib.util
from pathlib import Path


LAUNCHER_PATH = (
  Path(__file__).parents[1] / "runtime" / "served_runtime_launcher.py"
)
SPEC = importlib.util.spec_from_file_location(
  "mobius_served_runtime_launcher", LAUNCHER_PATH,
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def _served_tree(tmp_path, monkeypatch, source: str | None):
  platform = tmp_path / "platform"
  runtime = platform / "backend" / "runtime"
  runtime.mkdir(parents=True)
  if source is not None:
    (runtime / "identity_broker.py").write_text(source, encoding="utf-8")
  monkeypatch.setattr(launcher, "PLATFORM_DIR", platform)
  return runtime / "identity_broker.py"


def _image_tree(tmp_path, monkeypatch, source: str | None):
  """Point the launcher at an image runtime copy with the given broker source."""
  image = tmp_path / "image-runtime"
  image.mkdir()
  if source is not None:
    (image / "identity_broker.py").write_text(source, encoding="utf-8")
  monkeypatch.setenv("MOBIUS_PROTECTED_RUNTIME_DIR", str(image))
  return image


def _epoch_source(epoch: object) -> str:
  return f"BROKER_ROUTE_EPOCH = {epoch}\nVALUE = 'served'\n"


def test_check_accepts_a_valid_served_module(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 0


def test_missing_served_module_is_refused(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, None)

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_symlinked_served_module_is_refused(tmp_path, monkeypatch):
  target = _served_tree(tmp_path, monkeypatch, None)
  outside = tmp_path / "outside.py"
  outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
  target.symlink_to(outside)

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_group_writable_served_module_is_refused(tmp_path, monkeypatch):
  target = _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")
  target.chmod(0o664)

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_uncompilable_served_module_is_refused(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, "def broken(:\n")

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_an_unlisted_module_is_refused():
  assert launcher.main(["launcher.py", "restart_ledger"]) == 2
  assert launcher.main(["launcher.py", "--check", "restart_ledger"]) == 2
  assert launcher.main(["launcher.py"]) == 2


def test_served_epoch_behind_image_is_refused(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, _epoch_source(1))
  _image_tree(tmp_path, monkeypatch, _epoch_source(2))

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_served_epoch_equal_to_image_is_allowed(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, _epoch_source(2))
  _image_tree(tmp_path, monkeypatch, _epoch_source(2))

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 0


def test_served_epoch_ahead_of_image_is_allowed(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, _epoch_source(3))
  _image_tree(tmp_path, monkeypatch, _epoch_source(2))

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 0


def test_served_missing_marker_with_image_marker_is_refused(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")
  _image_tree(tmp_path, monkeypatch, _epoch_source(2))

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_non_numeric_served_marker_with_image_marker_is_refused(
  tmp_path, monkeypatch
):
  _served_tree(tmp_path, monkeypatch, _epoch_source("'two'"))
  _image_tree(tmp_path, monkeypatch, _epoch_source(2))

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 1


def test_image_without_marker_fails_open(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")
  _image_tree(tmp_path, monkeypatch, "VALUE = 'image'\n")

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 0


def test_unreadable_image_copy_fails_open(tmp_path, monkeypatch):
  _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")
  # No image identity_broker.py at the configured directory: cannot prove behind.
  _image_tree(tmp_path, monkeypatch, None)

  assert launcher.main(["launcher.py", "--check", "identity_broker"]) == 0


def test_exec_uses_the_same_validated_served_path(tmp_path, monkeypatch):
  target = _served_tree(tmp_path, monkeypatch, "VALUE = 'served'\n")
  invoked = {}

  def execv(executable, argv):
    invoked.update(executable=executable, argv=argv)
    raise OSError("stop after capture")

  monkeypatch.setattr(launcher.os, "execv", execv)

  assert launcher.main(["launcher.py", "identity_broker"]) == 1
  assert invoked["argv"][-1] == str(target)
