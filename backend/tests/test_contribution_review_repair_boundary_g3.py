"""The server may publish only a re-proved, ordinary fast-forward successor."""
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_review_repairs as repairs


def git(path, *args):
  return subprocess.run(["git", "-C", str(path), *args], check=True,
                        text=True, capture_output=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
  root = tmp_path
  path = root / "repair-one"
  path.mkdir()
  git(path, "init", "-q")
  git(path, "config", "user.name", "Test")
  git(path, "config", "user.email", "test@example.com")
  (path / "owned.py").write_text("before\n")
  git(path, "add", ".")
  git(path, "commit", "-qm", "initial")
  initial = git(path, "rev-parse", "HEAD")
  monkeypatch.setattr(repairs, "_checkout_root", lambda: root)
  return path, initial


def test_alternates_and_nested_redirects_are_rejected(checkout):
  path, _ = checkout
  info = path / ".git/objects/info"
  info.mkdir(exist_ok=True)
  for name in ("alternates", "http-alternates"):
    marker = info / name
    marker.write_text("/tmp/elsewhere\n")
    with pytest.raises(HTTPException):
      repairs.owned_checkout(path)
    marker.unlink()
  marker = info / "redirect"
  marker.symlink_to("/tmp/elsewhere")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(path)


def test_config_allowlist_rejects_include_and_textconv(checkout):
  path, _ = checkout
  for key in ("include.path", "diff.foo.textconv", "core.hooksPath"):
    git(path, "config", key, "/tmp/unsafe")
    with pytest.raises(HTTPException):
      repairs.owned_checkout(path)
    git(path, "config", "--unset", key)
  (path / ".git/info/attributes").write_text("*.py diff=foo\n")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(path)


def test_receipt_aware_sweep_preserves_unknown_and_active(checkout):
  path, _ = checkout
  old = path / ".git/index"
  os.utime(old, (1, 1))
  for marker in (path, path / ".git", path / ".git/HEAD"):
    os.utime(marker, (1, 1))
  repairs.sweep_stale_checkouts(protected_paths={str(path)}, terminal_paths=set(), dormant_paths=set(), now=10**12)
  assert path.exists()
  repairs.sweep_stale_checkouts(protected_paths=set(), terminal_paths={str(path)}, dormant_paths=set(), now=10**12)
  assert not path.exists()


def test_base_drift_is_not_dispatched(checkout, monkeypatch):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **k: validation)
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: initial)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "d" * 40)
  monkeypatch.setattr(repairs, "_git", lambda *a, **k: pytest.fail("must not dispatch"))
  result = repairs.push_repair(None, path, None, target, validation)
  assert result.state == "not_dispatched"


def test_push_is_plain_fast_forward_and_confirmed_only_by_ref(checkout, monkeypatch):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **k: validation)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "a" * 40)
  pushed = []
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: "b" * 40 if pushed else initial)
  monkeypatch.setattr(repairs, "owned_checkout", lambda *a: path)
  def push(_path, *args, **kwargs):
    assert args == ("push", "--porcelain", "https://github.com/owner/fork.git",
                    "b" * 40 + ":refs/heads/topic")
    assert "force" not in " ".join(args)
    pushed.append(1)
    return type("Result", (), {"returncode": 0})()
  monkeypatch.setattr(repairs, "_git", push)
  result = repairs.push_repair(None, path, None, target, validation)
  assert result.state == "confirmed" and result.receipt["head_sha"] == "b" * 40


def test_stop_guard_has_proved_not_dispatched_result(checkout, monkeypatch):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **k: validation)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "a" * 40)
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: initial)
  monkeypatch.setattr(repairs, "_git", lambda *a, **k: pytest.fail("must not push"))
  def stopped():
    raise repairs.NotDispatched("Stopped")
  assert repairs.push_repair(None, path, None, target, validation,
                             before_push=stopped).state == "not_dispatched"


def test_canonical_diff_never_runs_textconv(checkout, tmp_path):
  from app import app_git
  path, initial = checkout
  (path / "owned.py").write_text("after\n")
  git(path, "add", ".")
  git(path, "commit", "-qm", "after")
  head = git(path, "rev-parse", "HEAD")
  probe = tmp_path / "ran"
  (path / ".git/info/attributes").write_text("owned.py diff=probe\n")
  git(path, "config", "diff.probe.textconv", f"touch {probe}")
  diff = app_git._canonical_diff(path, initial, head, read_only=True)
  assert diff and not probe.exists()


@pytest.mark.parametrize("drift", ["head", "stop"])
def test_repeated_proof_drift_never_dispatches_push(checkout, monkeypatch, drift):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  proof_count = []
  def proof(*_args, **_kwargs):
    proof_count.append(1)
    return validation
  monkeypatch.setattr(repairs, "validate_repair", proof)
  monkeypatch.setattr(repairs, "owned_checkout", lambda *_args: path)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *_args: "a" * 40)
  monkeypatch.setattr(repairs, "_branch_head", lambda *_args: "d" * 40 if drift == "head" and len(proof_count) > 1 else initial)
  monkeypatch.setattr(repairs, "_git", lambda *_args, **_kwargs: pytest.fail("push after final drift"))
  guards = []
  def guard():
    guards.append(1)
    if drift == "stop" and len(proof_count) > 1:
      raise repairs.NotDispatched("Stopped during repeated proof")
  result = repairs.push_repair(None, path, None, target, validation, before_push=guard)
  assert result.state == "not_dispatched"
  assert len(proof_count) == 2
  if drift == "stop":
    assert len(guards) == 2


@pytest.mark.parametrize("porcelain,remote,returncode,expected", [
  ("!\t{head}:refs/heads/topic\t[rejected] (non-fast-forward)\n", "initial", 1, "not_dispatched"),
  ("!\t{head}:refs/heads/topic\t[remote rejected] (protected branch)\n", "initial", 1, "not_dispatched"),
  ("!\t{head}:refs/heads/other\t[rejected] (non-fast-forward)\n", "initial", 1, "unknown"),
  ("!\t{head}:refs/heads/topic\t[remote failure] (EOF)\n", "initial", 1, "unknown"),
  ("!\t{head}:refs/heads/topic\t[rejected] (non-fast-forward)\n", "moved", 1, "unknown"),
  ("!\t{head}:refs/heads/topic\t[rejected] (non-fast-forward)\n=\tother:refs/heads/other\t[up to date]\n", "initial", 1, "unknown"),
  ("!\t{head}:refs/heads/topic\t[rejected] (non-fast-forward)\n", "initial", -9, "unknown"),
])
def test_exact_porcelain_rejection_releases_only_when_remote_unchanged(checkout, monkeypatch, porcelain, remote, returncode, expected):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: validation)
  monkeypatch.setattr(repairs, "owned_checkout", lambda *a: path)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "a" * 40)
  pushed = []
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: ("d" * 40 if remote == "moved" else initial) if pushed else initial)
  def rejected(*args, **kwargs):
    pushed.append(1)
    return type("Result", (), {"returncode": returncode, "stdout": porcelain.format(head="b" * 40)})()
  monkeypatch.setattr(repairs, "_git", rejected)
  result = repairs.push_repair(None, path, None, target, validation)
  assert result.state == expected
  if expected == "not_dispatched":
    assert result.receipt == {"kind": "remote_ref_rejection", "head_sha": "b" * 40}
  assert pushed == [1]


def test_final_base_drift_after_repeated_proof_never_pushes(checkout, monkeypatch):
  path, initial = checkout
  validation = {"checkout": str(path), "initial_head_sha": initial,
                "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
                "allowed_files": ["owned.py"], "head_sha": "b" * 40,
                "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {"repo": "owner/base", "base_ref": "main", "head_sha": initial}
  proofs = []
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: proofs.append(1) or validation)
  monkeypatch.setattr(repairs, "owned_checkout", lambda *a: path)
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: initial)
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "d" * 40 if len(proofs) == 2 else "a" * 40)
  monkeypatch.setattr(repairs, "_git", lambda *a, **kw: pytest.fail("base moved before push"))
  assert repairs.push_repair(None, path, None, target, validation).state == "not_dispatched"
  assert len(proofs) == 2


@pytest.mark.parametrize("marker", ["info/grafts", "commondir", "objects/info/commit-graph"])
def test_forged_ancestry_metadata_is_rejected(checkout, marker):
  path, initial = checkout
  altered = path / ".git" / marker
  altered.parent.mkdir(exist_ok=True)
  altered.write_text(initial + "\n")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(path)


def test_valid_but_forbidden_commondir_redirect_is_rejected(checkout):
  path, _ = checkout
  # A self-pointing common dir is valid Git metadata; the explicit guard
  # must reject it rather than a later Git parse failure doing so by accident.
  (path / ".git/commondir").write_text(".\n")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(path)
