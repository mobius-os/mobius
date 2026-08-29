"""Trust and failure-boundary tests for the installed host worker."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "mobius-rebuild-host.py"
INSTALLER = Path(__file__).parents[2] / "scripts" / "install-rebuild-helper.sh"
ENTRYPOINT = Path(__file__).parents[1] / "scripts" / "entrypoint.sh"
SPEC = importlib.util.spec_from_file_location("mobius_rebuild_host", SCRIPT)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


def test_entrypoint_restores_host_control_after_compatibility_chown():
  source = ENTRYPOINT.read_text(encoding="utf-8")

  broad_chown = source.index("chown -R mobius:mobius /data")
  control_hardening = source.index("chown -R root:root /data/mobius-rebuild")
  inbox_grant = source.index(
    "chown -R mobius:mobius /data/mobius-rebuild/inbox",
  )

  assert broad_chown < control_hardening < inbox_grant


def test_installer_enables_boot_time_reconciliation():
  source = INSTALLER.read_text(encoding="utf-8")

  assert "mobius-rebuild-reconcile.service" in source
  assert "ExecStart=/usr/local/libexec/mobius-rebuild-host reconcile" in source
  assert "Before=mobius-rebuild.path" in source
  assert "WantedBy=multi-user.target" in source
  assert "systemctl enable mobius-rebuild-reconcile.service" in source
  assert 'bootstrap-runtime "$CID"' in source
  assert "MOBIUS_RUNTIME_OVERLAY" in source
  assert "target: /app/runtime" in source
  assert "FROZEN_SOURCE=/etc/mobius-rebuild/compose.yml" in source
  assert '"com.docker.compose.project.environment_file"' in source
  assert 'ARGS+=(--env-file "$file")' in source
  assert "CURRENT_IMAGE=$(docker inspect" in source
  assert 'MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose' in source


def _frozen(tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
  etc = tmp_path / "etc"
  data = tmp_path / "data"
  control = data / "mobius-rebuild"
  inbox = control / "inbox"
  etc.mkdir()
  inbox.mkdir(parents=True)
  config_path = etc / "config.json"
  compose = etc / "compose.yml"
  override = etc / "image.override.yml"
  for path in (config_path, compose, override):
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)
  control.chmod(0o755)
  monkeypatch.setattr(host, "CONFIG", config_path)
  monkeypatch.setattr(host, "COMPOSE", compose)
  monkeypatch.setattr(host, "OVERRIDE", override)
  value = {"version": 3, "project": "mobius", "data_dir": str(data)}
  return value, control


def test_frozen_config_accepts_minimal_root_owned_boundary(tmp_path, monkeypatch):
  value, control = _frozen(tmp_path, monkeypatch)

  result = host.validate_config(value, trusted_uid=os.getuid())

  assert result["project"] == "mobius"
  assert result["control_dir"] == control
  assert set(value) == {"version", "project", "data_dir"}


def test_frozen_config_rejects_group_writable_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  host.COMPOSE.chmod(0o620)

  with pytest.raises(ValueError, match="not root-controlled"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_frozen_config_rejects_symlinked_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  target = host.COMPOSE.with_name("mutable.yml")
  target.write_text("{}\n", encoding="utf-8")
  host.COMPOSE.unlink()
  host.COMPOSE.symlink_to(target)

  with pytest.raises(ValueError, match="may not use symlinks"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_served_generation_accepts_pending_source_but_requires_exact_overlay(
  monkeypatch,
):
  def version(payload):
    return subprocess.CompletedProcess(
      [], 0, stdout=payload, stderr="",
    )

  monkeypatch.setattr(
    host.subprocess, "run",
    lambda *_args, **_kwargs: version(
      '{"sha":"' + "a" * 40 + '","protected_runtime_state":"stale",'
      '"protected_runtime":{"deployed_sha256":"' + "b" * 64 + '"}}',
    ),
  )
  host.verify_served_generation("cid", "a" * 40, "b" * 64)

  monkeypatch.setattr(
    host.subprocess, "run",
    lambda *_args, **_kwargs: version(
      '{"sha":"' + "a" * 40 + '","protected_runtime":'
      '{"deployed_sha256":"' + "c" * 64 + '"}}',
    ),
  )
  with pytest.raises(RuntimeError, match="prepared generation"):
    host.verify_served_generation("cid", "a" * 40, "b" * 64)


def test_replacement_verifies_provenance_before_retiring_chat_handoff():
  source = SCRIPT.read_text(encoding="utf-8")
  run = source[source.index("def run()") : source.index("def reconcile()")]
  healthy = run.index("if not wait_healthy(config_value)")
  provenance = run.index("verify_served_generation(", healthy)
  finalize = run.index('"finalize-cutover"', provenance)

  assert healthy < provenance < finalize


def _worker_paths(tmp_path: Path, monkeypatch):
  state = tmp_path / "state"
  inbox = tmp_path / "control" / "inbox"
  state.mkdir()
  inbox.mkdir(parents=True)
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "LOCK", state / "replace.lock")
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "IMAGES", state / "images.json")
  monkeypatch.setattr(host, "RUNTIME_GENERATIONS", state / "runtime-generations")
  monkeypatch.setattr(host, "RUNTIME_STATE", state / "runtime.json")
  monkeypatch.setattr(host, "RUNTIME_RESOLUTIONS", state / "runtime-resolutions")
  monkeypatch.setattr(host, "RUNTIME_RESOLUTION", state / "runtime-resolution.json")
  runtime = host.RUNTIME_GENERATIONS / f"runtime-{'a' * 16}-{'b' * 8}"
  runtime.mkdir(parents=True)
  (runtime / "identity_broker.py").write_text("active\n", encoding="utf-8")
  digest, _files = host._runtime_snapshot(runtime)
  host._write_runtime_state(
    host.RuntimeGeneration(runtime.name, runtime, digest), None,
  )
  data = tmp_path / "data"
  data.mkdir()
  config = {
    "project": "mobius", "control_dir": inbox.parent, "data_dir": data,
  }
  monkeypatch.setattr(host, "config", lambda: config)
  return config, inbox


def _prepared_runtime() -> host.PreparedRuntime:
  prior_active, prior_rollback = host._read_runtime_state()
  previous = prior_active
  candidate_path = host.RUNTIME_GENERATIONS / f"runtime-{'c' * 16}-{'d' * 8}"
  candidate_path.mkdir(exist_ok=True)
  (candidate_path / "identity_broker.py").write_text(
    "merged\n", encoding="utf-8",
  )
  digest, _files = host._runtime_snapshot(candidate_path)
  candidate = host.RuntimeGeneration(candidate_path.name, candidate_path, digest)
  return host.PreparedRuntime(
    previous, candidate, ("identity_broker.py",), prior_active, prior_rollback,
  )


def _write_runtime_tree(root: Path, value: str) -> None:
  root.mkdir(parents=True)
  (root / "identity_broker.py").write_text(value, encoding="utf-8")


def test_runtime_merge_carries_active_edits_onto_incoming_official_tree(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  result = tmp_path / "result"
  _write_runtime_tree(base, "alpha=old\nshared=keep\nomega=old\n")
  _write_runtime_tree(active, "alpha=local\nshared=keep\nomega=old\n")
  _write_runtime_tree(incoming, "alpha=old\nshared=keep\nomega=official\n")

  digest, carried = host._merge_runtime_trees(base, active, incoming, result)

  assert (result / "identity_broker.py").read_text(encoding="utf-8") == (
    "alpha=local\nshared=keep\nomega=official\n"
  )
  assert carried == ("identity_broker.py",)
  assert digest == host._runtime_snapshot(result)[0]


def test_runtime_merge_blocks_a_real_active_vs_official_conflict(tmp_path):
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  _write_runtime_tree(base, "route=old\n")
  _write_runtime_tree(active, "route=local\n")
  _write_runtime_tree(incoming, "route=official\n")

  with pytest.raises(host.RuntimeOverlayConflict) as exc:
    host._merge_runtime_trees(base, active, incoming, tmp_path / "result")

  assert exc.value.paths == ("identity_broker.py",)


def test_runtime_merge_applies_an_exact_target_bound_reviewed_resolution(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  reviewed = tmp_path / "reviewed"
  result = tmp_path / "result"
  _write_runtime_tree(base, "route=old\nkeep=base\n")
  _write_runtime_tree(active, "route=local\nkeep=base\n")
  _write_runtime_tree(incoming, "route=official\nkeep=official\n")
  _write_runtime_tree(reviewed, "route=combined\nkeep=official\n")
  digest = host._runtime_snapshot(reviewed)[0]
  resolution = host.RuntimeResolution(
    "a" * 40, "b" * 64, "c" * 40, ("identity_broker.py",),
    (), digest, reviewed,
  )

  merged_digest, carried = host._merge_runtime_trees(
    base, active, incoming, result, resolution,
  )

  assert (result / "identity_broker.py").read_text(encoding="utf-8") == (
    "route=combined\nkeep=official\n"
  )
  assert carried == ("identity_broker.py",)
  assert merged_digest == host._runtime_snapshot(result)[0]


def test_runtime_merge_applies_a_reviewed_followup_after_a_clean_merge(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  reviewed = tmp_path / "reviewed"
  result = tmp_path / "result"
  _write_runtime_tree(base, "route=official\n")
  _write_runtime_tree(active, "route=local-v1\n")
  _write_runtime_tree(incoming, "route=official\n")
  _write_runtime_tree(reviewed, "route=local-v2\n")
  resolution = host.RuntimeResolution(
    "a" * 40, "b" * 64, "c" * 40, ("identity_broker.py",),
    (), host._runtime_snapshot(reviewed)[0], reviewed,
  )

  merged_digest, carried = host._merge_runtime_trees(
    base, active, incoming, result, resolution,
  )

  assert (result / "identity_broker.py").read_text(encoding="utf-8") == (
    "route=local-v2\n"
  )
  assert carried == ("identity_broker.py",)
  assert merged_digest == host._runtime_snapshot(result)[0]


def test_runtime_merge_rejects_a_resolution_for_different_conflict_paths(
  tmp_path,
):
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  reviewed = tmp_path / "reviewed"
  _write_runtime_tree(base, "route=old\n")
  _write_runtime_tree(active, "route=local\n")
  _write_runtime_tree(incoming, "route=official\n")
  reviewed.mkdir()
  (reviewed / "other.py").write_text("reviewed\n", encoding="utf-8")
  resolution = host.RuntimeResolution(
    "a" * 40, "b" * 64, "c" * 40, ("other.py",),
    (), host._runtime_snapshot(reviewed)[0], reviewed,
  )

  with pytest.raises(host.RuntimeOverlayConflict) as exc:
    host._merge_runtime_trees(
      base, active, incoming, tmp_path / "result", resolution,
    )

  assert exc.value.paths == ("identity_broker.py",)


def test_runtime_merge_applies_a_reviewed_conflict_deletion(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  base = tmp_path / "base"
  active = tmp_path / "active"
  incoming = tmp_path / "incoming"
  reviewed = tmp_path / "reviewed"
  result = tmp_path / "result"
  _write_runtime_tree(base, "base\n")
  _write_runtime_tree(active, "local edit\n")
  incoming.mkdir()
  reviewed.mkdir()
  deleted = ("identity_broker.py",)
  digest = host._runtime_resolution_digest(reviewed, deleted)[0]
  resolution = host.RuntimeResolution(
    "a" * 40, "b" * 64, "c" * 40, deleted, deleted, digest, reviewed,
  )

  merged_digest, carried = host._merge_runtime_trees(
    base, active, incoming, result, resolution,
  )

  assert not (result / "identity_broker.py").exists()
  assert carried == deleted
  assert merged_digest == host._runtime_snapshot(result)[0]


def test_prepare_runtime_uses_commit_merge_base_for_a_local_image(
  tmp_path, monkeypatch,
):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  repo = tmp_path / "platform"
  repo.mkdir()

  def git(*args):
    return subprocess.run(
      ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
    )

  git("init", "-q")
  git("config", "user.name", "Test")
  git("config", "user.email", "test@example.com")
  runtime = repo / "backend" / "runtime"
  _write_runtime_tree(
    runtime, "alpha=old\nkeep=one\nkeep=two\nkeep=three\nomega=old\n",
  )
  git("add", "-A")
  git("commit", "-q", "-m", "base")
  base = git("rev-parse", "HEAD").stdout.strip()

  git("checkout", "-q", "-b", "local")
  (runtime / "identity_broker.py").write_text(
    "alpha=local\nkeep=one\nkeep=two\nkeep=three\nomega=old\n",
    encoding="utf-8",
  )
  git("commit", "-qam", "local runtime")
  current = git("rev-parse", "HEAD").stdout.strip()
  local_tree = tmp_path / "local-tree"
  shutil.copytree(runtime, local_tree)

  git("checkout", "-q", "-b", "official", base)
  (runtime / "identity_broker.py").write_text(
    "alpha=old\nkeep=one\nkeep=two\nkeep=three\nomega=official\n",
    encoding="utf-8",
  )
  git("commit", "-qam", "official runtime")
  incoming = git("rev-parse", "HEAD").stdout.strip()
  incoming_tree = tmp_path / "incoming-tree"
  shutil.copytree(runtime, incoming_tree)

  def copy_active(_cid, destination):
    shutil.copytree(local_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination)

  def copy_image(image, destination):
    source = local_tree if image == "current-image" else incoming_tree
    shutil.copytree(source, destination, dirs_exist_ok=True)
    digest = host._normalize_runtime_tree(destination)
    return digest, {"sha": current if image == "current-image" else incoming}

  monkeypatch.setattr(host, "_copy_container_runtime", copy_active)
  monkeypatch.setattr(host, "_copy_image_runtime", copy_image)

  prepared = host.prepare_runtime_overlay(
    "container", "current-image", "incoming-image", repo, incoming,
  )

  assert prepared.carried_paths == ("identity_broker.py",)
  assert (prepared.candidate.path / "identity_broker.py").read_text(
    encoding="utf-8",
  ) == "alpha=local\nkeep=one\nkeep=two\nkeep=three\nomega=official\n"


def test_stage_runtime_resolution_binds_reviewed_file_to_active_tree_and_target(
  tmp_path, monkeypatch,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  repo = config["data_dir"] / "platform"
  repo.mkdir()

  def git(*args):
    return subprocess.run(
      ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
    )

  git("init", "-q")
  git("config", "user.name", "Test")
  git("config", "user.email", "test@example.com")
  runtime = repo / "backend" / "runtime"
  runtime.mkdir(parents=True)
  (runtime / "restart_ledger.py").write_text("base\n", encoding="utf-8")
  git("add", "-A")
  git("commit", "-q", "-m", "base")
  base = git("rev-parse", "HEAD").stdout.strip()

  git("checkout", "-q", "-b", "local")
  (runtime / "identity_broker.py").write_text("local\n", encoding="utf-8")
  git("add", "-A")
  git("commit", "-q", "-m", "local")
  current = git("rev-parse", "HEAD").stdout.strip()
  active_tree = tmp_path / "active-tree"
  shutil.copytree(runtime, active_tree)

  git("checkout", "-q", "-b", "official", base)
  (runtime / "identity_broker.py").write_text("official\n", encoding="utf-8")
  git("add", "-A")
  git("commit", "-q", "-m", "official")
  expected = git("rev-parse", "HEAD").stdout.strip()
  git("checkout", "-q", "-b", "reviewed")
  (runtime / "identity_broker.py").write_text("combined\n", encoding="utf-8")
  git("commit", "-qam", "reviewed resolution")
  source_commit = git("rev-parse", "HEAD").stdout.strip()

  def copy_active(_cid, destination):
    shutil.copytree(active_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination)

  def copy_image(_image, destination):
    shutil.copytree(active_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination), {"sha": current}

  monkeypatch.setattr(host, "app_container", lambda _config: ("a" * 12, "image"))
  monkeypatch.setattr(host, "_copy_container_runtime", copy_active)
  monkeypatch.setattr(host, "_copy_image_runtime", copy_image)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  resolution = host.stage_runtime_resolution(
    config, "a" * 12, expected, source_commit,
  )

  assert resolution.paths == ("identity_broker.py",)
  assert (resolution.directory / "identity_broker.py").read_text(
    encoding="utf-8",
  ) == "combined\n"
  assert resolution.active_digest == host._runtime_snapshot(active_tree)[0]
  assert statuses[-1]["code"] == "runtime_overlay_resolved"


def test_staged_runtime_resolution_can_delete_a_conflicted_file(
  tmp_path, monkeypatch,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  repo = config["data_dir"] / "platform"
  repo.mkdir()

  def git(*args):
    return subprocess.run(
      ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
    )

  git("init", "-q")
  git("config", "user.name", "Test")
  git("config", "user.email", "test@example.com")
  runtime = repo / "backend" / "runtime"
  _write_runtime_tree(runtime, "base\n")
  (runtime / "keep.py").write_text("keep\n", encoding="utf-8")
  git("add", "-A")
  git("commit", "-q", "-m", "base")
  base = git("rev-parse", "HEAD").stdout.strip()

  git("checkout", "-q", "-b", "local")
  (runtime / "identity_broker.py").write_text("local edit\n", encoding="utf-8")
  git("commit", "-qam", "local runtime")
  current = git("rev-parse", "HEAD").stdout.strip()
  active_tree = tmp_path / "active-tree-delete"
  shutil.copytree(runtime, active_tree)

  git("checkout", "-q", "-b", "official", base)
  (runtime / "identity_broker.py").unlink()
  git("commit", "-qam", "official deletion")
  expected = git("rev-parse", "HEAD").stdout.strip()
  official_tree = tmp_path / "official-tree-delete"
  shutil.copytree(runtime, official_tree)

  def copy_active(_cid, destination):
    shutil.copytree(active_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination)

  def copy_image(image, destination):
    source = active_tree if image == "current-image" else official_tree
    shutil.copytree(source, destination, dirs_exist_ok=True)
    digest = host._normalize_runtime_tree(destination)
    return digest, {"sha": current if image == "current-image" else expected}

  monkeypatch.setattr(host, "app_container", lambda _config: ("a" * 12, "current-image"))
  monkeypatch.setattr(host, "_copy_container_runtime", copy_active)
  monkeypatch.setattr(host, "_copy_image_runtime", copy_image)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  resolution = host.stage_runtime_resolution(
    config, "a" * 12, expected, expected,
  )
  prepared = host.prepare_runtime_overlay(
    "a" * 12, "current-image", "incoming-image", repo, expected,
  )

  assert resolution.paths == ("identity_broker.py",)
  assert resolution.deleted_paths == ("identity_broker.py",)
  assert not (resolution.directory / "identity_broker.py").exists()
  assert not (prepared.candidate.path / "identity_broker.py").exists()
  assert (prepared.candidate.path / "keep.py").read_text(encoding="utf-8") == "keep\n"


def test_stage_runtime_resolution_accepts_a_reviewed_clean_followup(
  tmp_path, monkeypatch,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  repo = config["data_dir"] / "platform"
  repo.mkdir()

  def git(*args):
    return subprocess.run(
      ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
    )

  git("init", "-q")
  git("config", "user.name", "Test")
  git("config", "user.email", "test@example.com")
  runtime = repo / "backend" / "runtime"
  _write_runtime_tree(runtime, "official\n")
  git("add", "-A")
  git("commit", "-q", "-m", "official")
  expected = git("rev-parse", "HEAD").stdout.strip()
  official_tree = tmp_path / "official-tree"
  shutil.copytree(runtime, official_tree)

  (runtime / "identity_broker.py").write_text("reviewed-v2\n", encoding="utf-8")
  git("commit", "-qam", "reviewed followup")
  source_commit = git("rev-parse", "HEAD").stdout.strip()
  active_tree = tmp_path / "active-tree"
  _write_runtime_tree(active_tree, "active-v1\n")

  def copy_active(_cid, destination):
    shutil.copytree(active_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination)

  def copy_image(_image, destination):
    shutil.copytree(official_tree, destination, dirs_exist_ok=True)
    return host._normalize_runtime_tree(destination), {"sha": expected}

  monkeypatch.setattr(host, "app_container", lambda _config: ("a" * 12, "image"))
  monkeypatch.setattr(host, "_copy_container_runtime", copy_active)
  monkeypatch.setattr(host, "_copy_image_runtime", copy_image)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  resolution = host.stage_runtime_resolution(
    config, "a" * 12, expected, source_commit,
  )

  assert resolution.paths == ("identity_broker.py",)
  assert (resolution.directory / "identity_broker.py").read_text(
    encoding="utf-8",
  ) == "reviewed-v2\n"


def test_controller_status_advertises_active_runtime_support(
  tmp_path, monkeypatch,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)

  status = host.write_status(config, state="idle")

  assert status["handoff"] == "external-cutover-v1"
  assert status["runtime_overlay"] == "active-runtime-v1"


def test_runtime_resolution_receipt_is_exact_target_and_content_bound(
  tmp_path, monkeypatch,
):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host.os, "chown", lambda *_args: None)
  directory = host.RUNTIME_RESOLUTIONS / f"resolution-{'a' * 32}"
  _write_runtime_tree(directory, "combined\n")
  digest = host._normalize_runtime_tree(directory)
  host._atomic_json(host.RUNTIME_RESOLUTION, {
    "version": 1,
    "expected_sha": "b" * 40,
    "active_digest": "c" * 64,
    "source_commit": "d" * 40,
    "paths": ["identity_broker.py"],
    "digest": digest,
    "directory": directory.name,
  })

  resolution = host._read_runtime_resolution("b" * 40, "c" * 64)

  assert resolution is not None
  assert resolution.paths == ("identity_broker.py",)
  assert host._read_runtime_resolution("e" * 40, "c" * 64) is None
  resolved_file = directory / "identity_broker.py"
  resolved_file.chmod(0o644)
  resolved_file.write_text("changed\n", encoding="utf-8")
  with pytest.raises(host.RuntimeOverlayError, match="invalid"):
    host._read_runtime_resolution("b" * 40, "c" * 64)


def test_compose_mounts_the_selected_runtime_generation(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  selected = host.active_runtime_generation()
  calls = []

  def execute(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  monkeypatch.setattr(host.subprocess, "run", execute)

  host.compose(config, "ps", runtime=selected)

  _args, kwargs = calls[0]
  assert kwargs["env"]["MOBIUS_RUNTIME_OVERLAY"] == str(selected.path)


def test_no_change_does_not_drain_active_chats(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "c" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "request_drain",
    lambda *_args: (_ for _ in ()).throw(AssertionError("no-change must not drain")),
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 0
  assert statuses[-1]["state"] == "no_change"


def test_same_image_applies_a_staged_runtime_followup(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "c" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "_read_runtime_resolution", lambda *_args: object())
  monkeypatch.setattr(host, "_consume_runtime_resolution", lambda *_args: None)
  prepared = _prepared_runtime()
  prepared_calls = []
  monkeypatch.setattr(
    host, "prepare_runtime_overlay",
    lambda *_args: prepared_calls.append(True) or prepared,
  )
  monkeypatch.setattr(host, "request_drain", lambda *_args: None)
  compose_calls = []
  monkeypatch.setattr(
    host, "compose",
    lambda *_args, **kwargs: compose_calls.append(kwargs.get("runtime")),
  )
  monkeypatch.setattr(host, "wait_healthy", lambda *_args: True)
  monkeypatch.setattr(host, "restart_ledger", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 0
  assert prepared_calls == [True]
  assert compose_calls == [prepared.candidate]
  assert statuses[-1]["state"] == "succeeded"


def test_request_is_claimed_on_the_control_filesystem(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "e" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  real_replace = host.os.replace
  claims = []

  def same_filesystem_replace(source, target):
    if Path(source) == request:
      claims.append(Path(target))
      assert Path(target).parent == config["control_dir"]
    return real_replace(source, target)

  monkeypatch.setattr(host.os, "replace", same_filesystem_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert len(claims) == 1
  assert not claims[0].exists()


def test_worker_locks_before_exposing_claim_to_reconcile(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "1" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  order = []
  real_replace = host.os.replace
  real_flock = host.fcntl.flock

  def record_flock(fd, operation):
    order.append("lock")
    return real_flock(fd, operation)

  def record_replace(source, target):
    if Path(source) == request:
      order.append("claim")
    return real_replace(source, target)

  monkeypatch.setattr(host.fcntl, "flock", record_flock)
  monkeypatch.setattr(host.os, "replace", record_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert order[:2] == ["lock", "claim"]


def test_worker_waits_for_boot_reconcile_before_claiming(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "2" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  attempts = []

  def reconcile_then_release(_fd, _operation):
    attempts.append("lock")
    if len(attempts) == 1:
      raise BlockingIOError

  monkeypatch.setattr(host.fcntl, "flock", reconcile_then_release)
  monkeypatch.setattr(host.time, "monotonic", lambda: 0)
  monkeypatch.setattr(host.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert attempts == ["lock", "lock"]
  assert not request.exists()


def test_failed_request_claim_is_terminal_and_retryable(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{"f" * 40}"}}', encoding="utf-8",
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  monkeypatch.setattr(
    host.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("claim failed")),
  )

  assert host.run() == 1
  assert statuses[-1]["state"] == "failed"
  assert not request.exists()


def test_runtime_overlay_conflict_stops_before_chat_drain(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "8" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
  monkeypatch.setattr(
    host,
    "prepare_runtime_overlay",
    lambda *_args: (_ for _ in ()).throw(
      host.RuntimeOverlayConflict(["identity_broker.py"]),
    ),
  )
  monkeypatch.setattr(
    host, "request_drain",
    lambda *_args: (_ for _ in ()).throw(
      AssertionError("a runtime conflict must stop before chat drain"),
    ),
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 1
  assert statuses[-1]["code"] == "runtime_overlay_conflict"
  assert "identity_broker.py" in statuses[-1]["message"]


def test_replacement_drains_then_rolls_back_after_cutover_error(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "d" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
  prepared = _prepared_runtime()
  monkeypatch.setattr(host, "prepare_runtime_overlay", lambda *_args: prepared)
  order = []
  ready = inbox / "ready"
  monkeypatch.setattr(
    host, "request_drain", lambda *_args: order.append("drain") or ready,
  )
  monkeypatch.setattr(host, "restart_ledger", lambda *_args, **_kwargs: True)

  def compose(_config, *args, image=None, **_kwargs):
    order.append(f"compose:{image}")
    if image == f"{host.IMAGE}:sha-{expected}":
      raise RuntimeError("cutover failed")

  monkeypatch.setattr(host, "compose", compose)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 1
  assert order == [
    "drain",
    f"compose:{host.IMAGE}:sha-{expected}",
    f"compose:{host.ROLLBACK_TAG}",
  ]
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "replacement_failed"


def test_success_reports_when_chat_handoff_receipt_cannot_be_retired(
  tmp_path, monkeypatch,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "9" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
  prepared = _prepared_runtime()
  monkeypatch.setattr(host, "prepare_runtime_overlay", lambda *_args: prepared)
  monkeypatch.setattr(host, "request_drain", lambda *_args: None)
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "restart_ledger",
    lambda _config, _cid, command, _operation, **_kwargs:
      command != "finalize-cutover",
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 0
  assert statuses[-1]["state"] == "succeeded"
  assert statuses[-1]["code"] == "handoff_finalize_failed"
  assert "could not verify and retire" in statuses[-1]["message"]


@pytest.mark.parametrize(
  ("rearmed", "finalized", "expected_code", "message_fragment"),
  [
    (False, False, "handoff_rearm_failed", "may need manual Resume"),
    (True, False, "handoff_finalize_failed", "could not verify and retire"),
  ],
)
def test_healthy_rollback_reports_degraded_chat_handoff(
  tmp_path, monkeypatch, rearmed, finalized, expected_code, message_fragment,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  operation = "a" * 32
  expected = "b" * 40
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)

  def ledger(_config, _cid, command, _operation, **_kwargs):
    assert _operation == operation
    return rearmed if command == "rearm-cutover" else finalized

  monkeypatch.setattr(host, "restart_ledger", ledger)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.rollback(
    config, operation, expected, "health_check_failed", "new image unhealthy",
  ) == 1
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == expected_code
  assert message_fragment in statuses[-1]["message"]


def test_reconcile_marks_interrupted_active_worker_failed(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text('{"state":"verifying"}', encoding="utf-8")
  abandoned = inbox.parent / f'.request-{"a" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written[-1]["code"] == "worker_interrupted"
  assert not abandoned.exists()


def test_reconcile_cleans_claim_abandoned_before_first_status(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  abandoned = inbox.parent / f'.request-{"b" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]
  assert not abandoned.exists()


def test_reconcile_refreshes_an_idle_controller_capability_receipt(
  tmp_path, monkeypatch,
):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text(
    '{"state":"idle","handoff":"external-cutover-v1"}', encoding="utf-8",
  )
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]


def test_drain_requires_root_open_prepare_accept_order(tmp_path, monkeypatch):
  data = tmp_path / "data"
  data.mkdir()
  operation = "a" * 32
  order = []

  def ledger(_config, _cid, command, value, **_kwargs):
    order.append(command)
    assert value == operation
    return True

  def execute(args, **_kwargs):
    order.append("prepare")
    assert args[-1] == operation
    return subprocess.CompletedProcess([], 0)

  monkeypatch.setattr(host, "restart_ledger", ledger)
  monkeypatch.setattr(host.subprocess, "run", execute)

  result = host.request_drain(
    {"data_dir": data, "control_dir": data / "mobius-rebuild"},
    operation,
    "container",
  )

  assert result is None
  assert order == ["open-cutover", "prepare", "accept-cutover"]
