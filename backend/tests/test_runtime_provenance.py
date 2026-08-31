"""Protected image-runtime parity is explicit and fail closed."""

from pathlib import Path

from app import runtime_provenance as provenance


def _write(root: Path, relative: str, content: bytes = b"same") -> None:
  path = root / relative
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(content)


def test_matching_trees_are_current_and_ignore_python_bytecode(tmp_path):
  source = tmp_path / "source"
  deployed = tmp_path / "deployed"
  _write(source, "identity_broker.py")
  _write(deployed, "identity_broker.py")
  _write(deployed, "__pycache__/identity_broker.cpython-312.pyc", b"cache")

  status = provenance.protected_runtime_status(source, deployed)

  assert status["state"] == "current"
  assert status["source_sha256"] == status["deployed_sha256"]
  assert status["mismatched_paths"] == []


def test_changed_missing_and_extra_files_are_stale(tmp_path):
  source = tmp_path / "source"
  deployed = tmp_path / "deployed"
  _write(source, "changed.py", b"wanted")
  _write(deployed, "changed.py", b"old")
  _write(source, "missing.py")
  _write(deployed, "extra.py")

  status = provenance.protected_runtime_status(source, deployed)

  assert status["state"] == "stale"
  assert status["mismatched_paths"] == ["changed.py", "extra.py", "missing.py"]
  assert provenance.activation_paths(status) == [
    "backend/runtime/changed.py",
    "backend/runtime/extra.py",
    "backend/runtime/missing.py",
  ]


def test_missing_source_is_unavailable_but_missing_deployed_tree_is_stale(
  tmp_path,
):
  source = tmp_path / "source"
  deployed = tmp_path / "deployed"

  unavailable = provenance.protected_runtime_status(source, deployed)
  assert unavailable == {
    "state": "unavailable",
    "source_sha256": None,
    "deployed_sha256": None,
    "mismatched_paths": [],
  }

  _write(source, "restart_ledger.py")
  stale = provenance.protected_runtime_status(source, deployed)
  assert stale["state"] == "stale"
  assert stale["deployed_sha256"] is None
  assert stale["mismatched_paths"] == ["restart_ledger.py"]


def test_symlinks_are_reported_without_following_them(tmp_path):
  source = tmp_path / "source"
  deployed = tmp_path / "deployed"
  source.mkdir()
  deployed.mkdir()
  outside = tmp_path / "outside"
  outside.write_text("secret", encoding="utf-8")
  (source / "linked.py").symlink_to(outside)
  _write(deployed, "linked.py", b"secret")

  status = provenance.protected_runtime_status(source, deployed)

  assert status["state"] == "stale"
  assert status["mismatched_paths"] == ["linked.py"]


def test_symlinked_root_is_unavailable_without_traversing_it(tmp_path):
  outside = tmp_path / "outside"
  _write(outside, "private.py", b"do not traverse")
  linked_source = tmp_path / "source"
  linked_source.symlink_to(outside, target_is_directory=True)

  status = provenance.protected_runtime_status(
    linked_source, tmp_path / "deployed",
  )

  assert status == {
    "state": "unavailable",
    "source_sha256": None,
    "deployed_sha256": None,
    "mismatched_paths": [],
  }
