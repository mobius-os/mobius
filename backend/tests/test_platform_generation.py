from __future__ import annotations

import subprocess
import json
from types import SimpleNamespace

import pytest

from app import platform_generation as generation


def _git(repo, *args):
  return subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
     "user.email=test@example.invalid", *args],
    check=True, capture_output=True, text=True,
  )


def _repo(tmp_path):
  repo = tmp_path / "platform"
  (repo / "backend" / "app").mkdir(parents=True)
  (repo / "frontend").mkdir()
  (repo / "backend" / "requirements.txt").write_text("example==1\n")
  (repo / "backend" / "requirements.lock").write_text("example==1\n")
  (repo / "backend" / "app" / "main.py").write_text("VALUE = 1\n")
  (repo / "frontend" / "package.json").write_text("{}\n")
  (repo / "frontend" / "package-lock.json").write_text("{}\n")
  (repo / "frontend" / ".source-build-signature").write_text("front-a\n")
  dist = repo / "frontend" / "dist"
  (dist / "assets").mkdir(parents=True)
  (dist / "assets" / "app.js").write_text("console.log('ok')\n")
  (dist / "index.html").write_text("<main>ok</main>\n")
  (dist / "sw.js").write_text("// sw\n")
  (dist / "manifest.webmanifest").write_text("{}\n")
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "add", ".")
  _git(repo, "commit", "-qm", "initial")
  return repo


def test_generation_identity_changes_for_uncommitted_authored_bytes(tmp_path, monkeypatch):
  repo = _repo(tmp_path)
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)

  clean = generation.checkout_generation(repo, required_actions=["server_restart"])
  assert clean["worktree_dirty"] is False

  (repo / "backend" / "app" / "main.py").write_text("VALUE = 2\n")
  dirty = generation.checkout_generation(repo, required_actions=["server_restart"])

  assert dirty["source_sha"] == clean["source_sha"]
  assert dirty["worktree_dirty"] is True
  assert dirty["worktree_sha256"] != clean["worktree_sha256"]
  assert dirty["generation_id"] != clean["generation_id"]


def test_generation_identity_ignores_gitignored_runtime_output(tmp_path, monkeypatch):
  repo = _repo(tmp_path)
  (repo / ".gitignore").write_text("runtime-output/\n")
  _git(repo, "add", ".gitignore")
  _git(repo, "commit", "-qm", "ignore runtime")
  monkeypatch.setattr(generation, "_image_sha", lambda: None)
  before = generation.checkout_generation(repo)

  (repo / "runtime-output").mkdir()
  (repo / "runtime-output" / "result.txt").write_text("generated\n")
  after = generation.checkout_generation(repo)

  assert after == before


def test_root_bootstrap_gives_a_new_generation_lock_to_runtime_user(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  state_path = tmp_path / "state.json"
  ownership = []
  monkeypatch.setattr(platform_artifacts.os, "geteuid", lambda: 0)
  monkeypatch.setattr(
    platform_artifacts.pwd, "getpwnam",
    lambda _name: SimpleNamespace(pw_uid=1234, pw_gid=5678),
  )
  monkeypatch.setattr(
    platform_artifacts.os, "fchown",
    lambda _fd, uid, gid: ownership.append((uid, gid)),
  )

  with platform_artifacts._state_lock(state_path):
    pass

  assert ownership == [(1234, 5678)]


def test_prepare_ready_and_one_shot_rollback_are_crash_durable(tmp_path, monkeypatch):
  repo = _repo(tmp_path)
  state_path = tmp_path / "generation.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: "b" * 40)
  candidate = generation.checkout_generation(repo, required_actions=["server_restart"])

  prepared = generation.prepare_generation(
    candidate, operation_id="update-1", repo=repo, path=state_path,
  )
  assert prepared["pending"]["generation_id"] == candidate["generation_id"]
  assert prepared["active"] is None

  # There is no rollback target until some generation has actually reached
  # readiness. Spending still persists, so a process crash cannot retry it.
  assert generation.spend_rollback(
    candidate["generation_id"], owner="source", path=state_path,
  ) is None
  assert generation.spend_rollback(
    candidate["generation_id"], owner="source", path=state_path,
  ) is None

  generation.prepare_generation(
    candidate, operation_id="update-2", repo=repo, path=state_path,
  )
  ready = generation.record_ready_generation(
    candidate, boot_id="boot-1", path=state_path,
  )
  assert ready["pending"] is None
  assert ready["active"] == candidate
  assert ready["last_ready"] == candidate
  assert ready["activation"]["status"] == "ready"


def test_different_ready_generation_retires_pending_as_superseded(
  tmp_path, monkeypatch,
):
  repo = _repo(tmp_path)
  state_path = tmp_path / "generation.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: None)
  pending = generation.checkout_generation(
    repo, required_actions=["server_restart"],
  )
  generation.prepare_generation(
    pending, operation_id="restart-1", repo=repo, path=state_path,
  )

  (repo / "backend" / "app" / "main.py").write_text("VALUE = 4\n")
  observed = generation.checkout_generation(
    repo, required_actions=["server_restart"],
  )
  ready = generation.record_ready_generation(
    observed, boot_id="boot-newer", path=state_path,
  )

  assert ready["pending"] is None
  assert ready["active"] == observed
  assert ready["last_ready"] == observed
  assert ready["activation"] == {
    "generation_id": pending["generation_id"],
    "operation_id": "restart-1",
    "status": "superseded_on_boot",
    "rollback_attempted": False,
    "boot_id": "boot-newer",
    "observed_generation_id": observed["generation_id"],
    "manifest_sha256": ready["activation"]["manifest_sha256"],
  }


def test_restart_refuses_generation_changed_after_review(tmp_path, monkeypatch):
  repo = _repo(tmp_path)
  monkeypatch.setattr(generation, "_image_sha", lambda: None)
  reviewed = generation.checkout_generation(repo, required_actions=["server_restart"])

  (repo / "backend" / "app" / "main.py").write_text("VALUE = 3\n")

  with pytest.raises(generation.GenerationChanged, match="changed after"):
    generation.require_generation(
      reviewed["generation_id"], repo=repo,
      required_actions=["server_restart"],
    )


def test_generation_state_rejects_a_tampered_identity(tmp_path, monkeypatch):
  repo = _repo(tmp_path)
  state_path = tmp_path / "generation.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: None)
  candidate = generation.checkout_generation(repo)
  generation.prepare_generation(candidate, repo=repo, path=state_path)

  payload = json.loads(state_path.read_text())
  payload["pending"]["backend_tree"] = "tampered"
  state_path.write_text(json.dumps(payload))

  assert generation.generation_state(path=state_path)["pending"] is None


def test_materialized_generation_detects_changed_artifact(tmp_path, monkeypatch):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  artifact_root = tmp_path / "artifacts"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  candidate = generation.checkout_generation(repo)
  directory, digest = platform_artifacts.materialize(
    repo, candidate, root=artifact_root,
  )

  assert platform_artifacts.verify(directory, candidate["generation_id"])[
    "manifest_sha256"
  ] == digest
  (directory / "backend" / "app" / "main.py").write_text("changed\n")
  with pytest.raises(platform_artifacts.ArtifactError, match="fingerprint"):
    platform_artifacts.verify(directory, candidate["generation_id"])


def test_selector_prefers_verified_active_generation(tmp_path, monkeypatch):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  artifact_root = tmp_path / "artifacts"
  state_path = tmp_path / "state.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  active = generation.checkout_generation(repo)
  platform_artifacts.materialize(repo, active, root=artifact_root)
  state_path.write_text(json.dumps({
    "pending": None, "active": active, "last_ready": active,
  }))

  selected = platform_artifacts.select(state_path, artifact_root, "a" * 40)
  assert selected["role"] == "active"
  assert selected["generation_id"] == active["generation_id"]


def test_selector_can_skip_a_failed_pending_generation(tmp_path, monkeypatch):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  artifact_root = tmp_path / "artifacts"
  state_path = tmp_path / "state.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  active = generation.checkout_generation(repo)
  platform_artifacts.materialize(repo, active, root=artifact_root)
  (repo / "backend" / "app" / "main.py").write_text("VALUE = 5\n")
  pending = generation.checkout_generation(repo)
  platform_artifacts.materialize(repo, pending, root=artifact_root)
  state_path.write_text(json.dumps({
    "pending": pending, "active": active, "last_ready": active,
  }))

  selected = platform_artifacts.select(
    state_path, artifact_root, "a" * 40, skip_pending=True,
  )
  assert selected["role"] == "active"
  assert selected["generation_id"] == active["generation_id"]


def test_replacement_image_boot_does_not_spend_candidate_rollback(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  state_path = tmp_path / "state.json"
  artifact_root = tmp_path / "platform-generations"
  old_image = "a" * 40
  monkeypatch.setattr(generation, "_image_sha", lambda: old_image)
  active = generation.checkout_generation(repo)
  generation.prepare_generation(active, repo=repo, path=state_path)
  generation.record_ready_generation(active, boot_id="boot-ready", path=state_path)

  (repo / "backend" / "app" / "main.py").write_text("VALUE = 12\n")
  _git(repo, "add", ".")
  _git(repo, "commit", "-qm", "replacement image")
  pending = generation.checkout_generation(
    repo, required_actions=["image_rebuild", "server_restart"],
  )
  generation.prepare_generation(pending, repo=repo, path=state_path)

  with pytest.raises(platform_artifacts.ImageBootstrapRequired):
    platform_artifacts.select_for_boot(
      state_path, artifact_root, pending["source_sha"], "boot-new-image",
    )

  waiting = json.loads(state_path.read_text())
  assert waiting["pending"]["generation_id"] == pending["generation_id"]
  assert waiting["activation"]["rollback_attempted"] is False
  monkeypatch.setattr(
    generation, "_image_sha", lambda: pending["source_sha"],
  )
  baked = generation.baked_generation(pending["source_sha"])
  ready = generation.record_ready_generation(
    baked, boot_id="boot-new-image", path=state_path,
  )
  assert ready["pending"] is None
  assert ready["activation"]["status"] == "image_bootstrap_ready"
  assert ready["activation"]["rollback_attempted"] is False


def test_selector_rejects_pending_artifact_outside_its_activation_receipt(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  artifact_root = tmp_path / "artifacts"
  state_path = tmp_path / "state.json"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  pending = generation.checkout_generation(repo)
  platform_artifacts.materialize(repo, pending, root=artifact_root)
  state_path.write_text(json.dumps({
    "pending": pending,
    "active": None,
    "last_ready": None,
    "activation": {
      "generation_id": pending["generation_id"],
      "manifest_sha256": "0" * 64,
    },
  }))

  with pytest.raises(platform_artifacts.ArtifactError, match="activation receipt"):
    platform_artifacts.select(state_path, artifact_root, "a" * 40)


def test_artifact_pruning_keeps_all_recorded_generations(tmp_path):
  from app import platform_artifacts

  root = tmp_path / "artifacts"
  identifiers = [f"{index:064x}" for index in range(6)]
  for identifier in identifiers:
    (root / identifier).mkdir(parents=True)
  state = {
    "pending": {"generation_id": identifiers[0]},
    "active": {"generation_id": identifiers[1]},
    "last_ready": {"generation_id": identifiers[2]},
  }

  platform_artifacts.prune(root, state, retain_unreferenced=1)

  remaining = {path.name for path in root.iterdir()}
  assert set(identifiers[:3]).issubset(remaining)
  assert len(remaining) == 4


def test_materialization_rejects_source_symlink_outside_generation(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  outside = tmp_path / "outside.txt"
  outside.write_text("mutable\n")
  (repo / "outside-link").symlink_to(outside)
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  candidate = generation.checkout_generation(repo)

  with pytest.raises(platform_artifacts.ArtifactError, match="absolute"):
    platform_artifacts.materialize(
      repo, candidate, root=tmp_path / "artifacts",
    )


def test_failed_pending_boot_spends_one_rollback_and_restores_last_ready(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  state_path = tmp_path / "state.json"
  artifact_root = tmp_path / "platform-generations"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  active = generation.checkout_generation(repo)
  generation.prepare_generation(active, repo=repo, path=state_path)
  generation.record_ready_generation(active, boot_id="boot-ready", path=state_path)
  (repo / "backend" / "app" / "main.py").write_text("VALUE = 9\n")
  pending = generation.checkout_generation(repo)
  generation.prepare_generation(pending, repo=repo, path=state_path)

  selected = platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "boot-candidate",
  )
  assert selected["generation_id"] == pending["generation_id"]
  assert json.loads(state_path.read_text())["activation"]["status"] == "booting"

  assert platform_artifacts.fail_boot(
    state_path,
    generation_id=pending["generation_id"],
    boot_id="boot-candidate",
    stage="health_timeout",
    message="health failed",
  ) is True
  rolled_back = platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "boot-rollback",
  )
  assert rolled_back["generation_id"] == active["generation_id"]
  state = json.loads(state_path.read_text())
  assert state["pending"] is None
  assert state["activation"]["rollback_attempted"] is True
  assert state["activation"]["status"] == "rollback_booting"

  ready = generation.record_ready_generation(
    active, boot_id="boot-rollback", path=state_path,
  )
  assert ready["activation"]["status"] == "rolled_back_ready"


def test_failed_rollback_is_not_retried_or_reselected(tmp_path, monkeypatch):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  state_path = tmp_path / "state.json"
  artifact_root = tmp_path / "platform-generations"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  active = generation.checkout_generation(repo)
  generation.prepare_generation(active, repo=repo, path=state_path)
  generation.record_ready_generation(active, boot_id="boot-ready", path=state_path)
  (repo / "backend" / "app" / "main.py").write_text("VALUE = 10\n")
  pending = generation.checkout_generation(repo)
  generation.prepare_generation(pending, repo=repo, path=state_path)
  platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "boot-candidate",
  )
  platform_artifacts.fail_boot(
    state_path,
    generation_id=pending["generation_id"],
    boot_id="boot-candidate",
    stage="health_timeout",
    message="candidate failed",
  )
  platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "boot-rollback",
  )

  assert platform_artifacts.fail_boot(
    state_path,
    generation_id=active["generation_id"],
    boot_id="boot-rollback",
    stage="health_timeout",
    message="rollback failed",
  ) is True
  with pytest.raises(
    platform_artifacts.PendingArtifactError, match="rollback also failed",
  ):
    platform_artifacts.select_for_boot(
      state_path, artifact_root, "a" * 40, "boot-after-failure",
    )
  state = json.loads(state_path.read_text())
  assert state["activation"]["status"] == "rollback_failed"
  assert state["activation"]["rollback_attempted"] is True


def test_next_boot_detects_candidate_that_exited_before_readiness(
  tmp_path, monkeypatch,
):
  from app import platform_artifacts

  repo = _repo(tmp_path)
  state_path = tmp_path / "state.json"
  artifact_root = tmp_path / "platform-generations"
  monkeypatch.setattr(generation, "_image_sha", lambda: "a" * 40)
  active = generation.checkout_generation(repo)
  generation.prepare_generation(active, repo=repo, path=state_path)
  generation.record_ready_generation(active, boot_id="boot-ready", path=state_path)
  (repo / "backend" / "app" / "main.py").write_text("VALUE = 11\n")
  pending = generation.checkout_generation(repo)
  generation.prepare_generation(pending, repo=repo, path=state_path)
  platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "boot-that-exits",
  )

  selected = platform_artifacts.select_for_boot(
    state_path, artifact_root, "a" * 40, "next-boot",
  )

  assert selected["generation_id"] == active["generation_id"]
  assert selected["rollback_from_generation_id"] == pending["generation_id"]
  state = json.loads(state_path.read_text())
  assert state["activation"]["status"] == "rollback_booting"
  assert state["activation"]["last_failure"]["stage"] == "early_boot_exit"
