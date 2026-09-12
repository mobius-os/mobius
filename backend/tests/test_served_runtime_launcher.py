"""The frozen loader starts served privileged source behind a safe image floor."""

import importlib.util
import json
from pathlib import Path

LAUNCHER_PATH = (
  Path(__file__).parents[1] / "runtime" / "served_runtime_launcher.py"
)
SPEC = importlib.util.spec_from_file_location(
  "mobius_served_runtime_launcher", LAUNCHER_PATH,
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def _trees(tmp_path, monkeypatch, *, served: str | None, frozen: str | None):
  platform = tmp_path / "platform"
  frozen_dir = tmp_path / "frozen"
  (platform / "backend" / "runtime").mkdir(parents=True)
  frozen_dir.mkdir()
  if served is not None:
    (platform / "backend" / "runtime" / "identity_broker.py").write_text(
      served, encoding="utf-8",
    )
  if frozen is not None:
    (frozen_dir / "identity_broker.py").write_text(frozen, encoding="utf-8")
  monkeypatch.setattr(launcher, "PLATFORM_DIR", platform)
  monkeypatch.setattr(launcher, "FROZEN_DIR", frozen_dir)
  monkeypatch.setattr(launcher, "RECEIPT_PATH", tmp_path / "run" / "runtime.json")
  return platform, frozen_dir


def test_a_valid_served_module_is_started(tmp_path, monkeypatch):
  platform, frozen_dir = _trees(
    tmp_path, monkeypatch, served="VALUE = 'served'\n", frozen="VALUE = 'image'\n",
  )

  target, source, reason = launcher._choose("identity_broker")

  assert target == platform / "backend" / "runtime" / "identity_broker.py"
  assert source == "served"
  assert reason is None


def test_a_missing_served_module_falls_back_to_the_image(tmp_path, monkeypatch):
  _, frozen_dir = _trees(
    tmp_path, monkeypatch, served=None, frozen="VALUE = 'image'\n",
  )

  target, source, reason = launcher._choose("identity_broker")

  assert target == frozen_dir / "identity_broker.py"
  assert source == "frozen"
  assert "missing" in reason


def test_a_symlinked_served_module_falls_back_to_the_image(tmp_path, monkeypatch):
  platform, frozen_dir = _trees(
    tmp_path, monkeypatch, served=None, frozen="VALUE = 'image'\n",
  )
  outside = tmp_path / "outside.py"
  outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
  (platform / "backend" / "runtime" / "identity_broker.py").symlink_to(outside)

  target, source, reason = launcher._choose("identity_broker")

  assert (target, source) == (frozen_dir / "identity_broker.py", "frozen")
  assert "symlink" in reason


def test_a_group_writable_served_module_falls_back_to_the_image(
  tmp_path, monkeypatch,
):
  platform, frozen_dir = _trees(
    tmp_path, monkeypatch, served="VALUE = 'served'\n", frozen="VALUE = 'image'\n",
  )
  (platform / "backend" / "runtime" / "identity_broker.py").chmod(0o664)

  target, source, reason = launcher._choose("identity_broker")

  assert (target, source) == (frozen_dir / "identity_broker.py", "frozen")
  assert "group or other" in reason


def test_an_uncompilable_served_module_falls_back_to_the_image(
  tmp_path, monkeypatch,
):
  _, frozen_dir = _trees(
    tmp_path, monkeypatch, served="def broken(:\n", frozen="VALUE = 'image'\n",
  )

  target, source, reason = launcher._choose("identity_broker")

  assert (target, source) == (frozen_dir / "identity_broker.py", "frozen")
  assert "does not compile" in reason


def test_an_unlisted_module_is_refused():
  assert launcher.main(["launcher.py", "restart_ledger"]) == 2
  assert launcher.main(["launcher.py"]) == 2


def test_the_receipt_records_which_copy_started(tmp_path, monkeypatch):
  platform, _ = _trees(
    tmp_path, monkeypatch, served="VALUE = 'served'\n", frozen="VALUE = 'image'\n",
  )
  target, source, reason = launcher._choose("identity_broker")

  launcher._record("identity_broker", target, source, reason)

  payload = json.loads(launcher.RECEIPT_PATH.read_text(encoding="utf-8"))
  assert payload["module"] == "identity_broker"
  assert payload["started"] == "served"
  assert payload["path"] == str(platform / "backend" / "runtime" / "identity_broker.py")
  assert payload["reason"] is None
