"""Route-level wiring for the platform updater endpoints.

The reconcile plumbing is covered exhaustively in ``test_platform_update.py``
against throwaway clones; these assert owner-gating, immutable plan forwarding,
and truthful failures at the HTTP boundary. A failed read must never become
an apparently successful empty review or up-to-date status.
"""

from app import deployment_control
from app.platform_activation import classify_activation


def test_update_preview_requires_owner(client):
  assert client.get("/api/platform/update-preview").status_code == 401


def test_update_preview_failure_is_not_an_empty_success(client, auth, monkeypatch):
  def fail_preview():
    raise RuntimeError("platform clone unavailable")

  monkeypatch.setattr(
    "app.routes.platform.platform_update.platform_update_preview", fail_preview,
  )
  response = client.get("/api/platform/update-preview", headers=auth)

  assert response.status_code == 503
  assert response.json()["detail"] == {
    "code": "platform_preview_unavailable",
    "message": "Could not prepare the update review. Try again.",
  }


def test_update_progress_requires_owner(client):
  assert client.get("/api/platform/update-progress").status_code == 401


def test_update_progress_returns_observable_phase(client, auth, monkeypatch):
  monkeypatch.setattr(
    "app.routes.platform.platform_update.platform_update_progress",
    lambda: {
      "plan_id": "a" * 64,
      "target_sha": "2" * 40,
      "phase": "building",
      "active": True,
      "error": None,
      "updated_at": 123.0,
    },
  )

  res = client.get("/api/platform/update-progress", headers=auth)

  assert res.status_code == 200
  assert res.json()["phase"] == "building"
  assert res.json()["active"] is True


def test_apply_forwards_exact_reviewed_plan(client, auth, monkeypatch):
  captured = {}

  async def fake_apply(db, **plan):
    captured.update(plan)
    return {
      "state": "restart_needed",
      "needs_restart": True,
      "activation": classify_activation(
        ["backend/app/main.py"], deployment="self_hosted"
      ),
      "upstream_commit": plan["target_sha"],
      "merge_commit": "3" * 40,
      "conflict_paths": [],
      "chat_id": None,
      "phase": "complete",
      "error": None,
      "reconciliation": {
        "proven_present": [],
        "local_only_paths": [],
        "new_upstream_paths": [],
        "compatible_paths": [],
        "unresolved_conflict_paths": [],
        "provenance_refs_used": [],
      },
    }

  monkeypatch.setattr(
    "app.routes.platform.platform_update.apply_platform_update",
    fake_apply,
  )
  body = {
    "plan_id": "a" * 64,
    "current_sha": "1" * 40,
    "target_sha": "2" * 40,
  }

  res = client.post("/api/platform/apply", headers=auth, json=body)

  assert res.status_code == 200
  assert captured == {**body, "image_digest": None}
  assert res.json()["upstream_commit"] == body["target_sha"]


def test_reviewed_rebuild_forwards_exact_sha_and_digest(
  client, auth, monkeypatch,
):
  captured = {}

  async def fake_rebuild(**plan):
    # The route also threads a DB session for the self-hosted apply step; the
    # forwarded update-plan identity is what this test asserts.
    captured.update({k: v for k, v in plan.items() if k != "db"})
    return {
      "supported": True,
      "bootstrap_available": False,
      "deployment": "railway",
      "operation_id": "replace_123",
      "state": "queued",
      "expected_sha": plan["target_sha"],
      "code": None,
      "message": "Replacement queued.",
      "error": None,
      "image_digest": plan["image_digest"],
      "release_source": "latest_ghcr",
      "updated_at": None,
    }

  monkeypatch.setattr(
    "app.routes.platform.deployment_control.request_reviewed_rebuild",
    fake_rebuild,
  )
  body = {
    "plan_id": "a" * 64,
    "current_sha": "1" * 40,
    "target_sha": "2" * 40,
    "image_digest": "sha256:" + "d" * 64,
  }

  response = client.post("/api/platform/rebuild", headers=auth, json=body)

  assert response.status_code == 202
  assert captured == body
  assert response.json()["release_source"] == "latest_ghcr"


def test_reviewed_rebuild_railway_requires_digest(client, auth, monkeypatch):
  # On Railway the image is pinned by GHCR digest, so a review with no digest is
  # invalid. (Self-hosted has no GHCR digest and anchors on the sha-<target>
  # tag, so it does not require one — see the deployment_control tests.)
  monkeypatch.setattr(
    "app.platform_activation.deployment_kind",
    lambda *a, **k: "railway",
  )
  response = client.post("/api/platform/rebuild", headers=auth, json={
    "plan_id": "a" * 64,
    "current_sha": "1" * 40,
    "target_sha": "2" * 40,
  })

  assert response.status_code == 409
  assert response.json()["detail"] == {
    "code": "update_plan_invalid",
    "message": "This update review is no longer valid. Refresh it and try again.",
  }


def test_railway_preview_uses_latest_verified_ghcr_release(
  client, auth, monkeypatch,
):
  digest = "sha256:" + "e" * 64
  target = "2" * 40
  captured = {}

  async def latest():
    return {
      "build_sha": target,
      "image_digest": digest,
      "image_ref": "immutable",
    }

  def preview(**kwargs):
    captured.update(kwargs)
    return {
      "state": "available", "available": True,
      "actionable": True, "operation": "update",
      "current_sha": "1" * 40, "target_sha": target,
      "plan_id": "a" * 64, "image_digest": digest,
      "activation": classify_activation(
        ["Dockerfile"], deployment="railway",
      ),
      "total_commits": 1, "commits_truncated": False,
      "commits": [], "files": [], "diff": None,
      "diff_truncated": False, "conflict_paths": [], "blocking_paths": [],
    }

  monkeypatch.setattr(
    "app.routes.platform.platform_activation.deployment_kind",
    lambda: "railway",
  )
  monkeypatch.setattr(
    "app.routes.platform.deployment_control.latest_official_release",
    latest,
  )
  monkeypatch.setattr(
    "app.routes.platform.platform_update.platform_update_preview",
    preview,
  )

  response = client.get("/api/platform/update-preview", headers=auth)

  assert response.status_code == 200
  assert captured == {"target_sha": target, "image_digest": digest}
  assert response.json()["image_digest"] == digest


def test_railway_status_and_check_use_latest_verified_ghcr_target(
  client, auth, monkeypatch,
):
  target = "2" * 40
  calls = []

  async def latest():
    return {
      "build_sha": target,
      "image_digest": "sha256:" + "e" * 64,
      "image_ref": "immutable",
    }

  def status(*, target_sha):
    calls.append(("status", target_sha))
    return {
      "state": "available", "available": True,
      "needs_restart": False,
      "activation": classify_activation([]),
      "current_build_sha": None,
      "recorded_upstream_sha": None,
      "contained_upstream_sha": None,
      "contained_upstream_committed_at": None,
      "upstream_checked_at": None,
      "seed_required": False,
      "conflict_paths": [], "conflict_chat_id": None,
      "newer_updates_available": False,
      "rollback_target_sha": None, "rollback_error": None,
      "overlay": None,
    }

  def check(*, target_sha):
    calls.append(("check", target_sha))
    return status(target_sha=target_sha)

  monkeypatch.setattr(
    "app.routes.platform.platform_activation.deployment_kind",
    lambda: "railway",
  )
  monkeypatch.setattr(
    "app.routes.platform.deployment_control.latest_official_release",
    latest,
  )
  monkeypatch.setattr(
    "app.routes.platform.platform_update.platform_status",
    status,
  )
  monkeypatch.setattr(
    "app.routes.platform.platform_update.check_for_updates",
    check,
  )

  status_response = client.get("/api/platform/status", headers=auth)
  check_response = client.post("/api/platform/check", headers=auth)

  assert status_response.status_code == 200
  assert check_response.status_code == 200
  assert calls == [
    ("status", target),
    ("check", target),
    ("status", target),
  ]


def test_railway_status_does_not_call_unknown_release_current(
  client, auth, monkeypatch,
):
  monkeypatch.setattr(
    "app.routes.platform.platform_activation.deployment_kind",
    lambda: "railway",
  )

  async def unavailable():
    raise deployment_control.DeploymentControlError(
      "controller_unavailable",
      "The official image could not be checked.",
    )

  monkeypatch.setattr(
    "app.routes.platform.deployment_control.latest_official_release",
    unavailable,
  )

  response = client.get("/api/platform/status", headers=auth)

  assert response.status_code == 503
  assert response.json()["detail"] == {
    "code": "controller_unavailable",
    "message": "The official image could not be checked.",
  }


def test_update_check_reports_fetch_failure(client, auth, monkeypatch):
  """Settings must not translate an unreachable origin into "up to date"."""
  def fail_check():
    from app.platform_update import PlatformUpdateError
    raise PlatformUpdateError("platform_fetch_failed")

  monkeypatch.setattr("app.routes.platform.platform_update.check_for_updates",
                      fail_check)
  res = client.post("/api/platform/check", headers=auth)
  assert res.status_code == 503
  assert res.json()["detail"] == {
    "code": "platform_fetch_failed",
    "message": "This update could not be verified. Refresh it and try again.",
  }


def test_status_failure_does_not_claim_up_to_date(
  client, auth, monkeypatch,
):
  def fail_status():
    raise RuntimeError("platform clone unavailable")

  monkeypatch.setattr(
    "app.routes.platform.platform_update.platform_status", fail_status,
  )

  response = client.get("/api/platform/status", headers=auth)

  assert response.status_code == 503
  assert response.json()["detail"] == {
    "code": "platform_status_unavailable",
    "message": "Could not check update status. Try again.",
  }


def test_finish_preview_selects_applied_target_not_latest(client, auth, monkeypatch):
  from app import platform_update
  captured = {}
  target = "1" * 40
  monkeypatch.setattr(deployment_control, "applied_release_sha", lambda: target)
  monkeypatch.setattr("app.platform_activation.deployment_kind", lambda: "self_hosted")

  def preview(**kwargs):
    captured.update(kwargs)
    return platform_update.empty_platform_update_preview()

  monkeypatch.setattr(platform_update, "platform_update_preview", preview)
  response = client.get("/api/platform/update-preview?intent=finish", headers=auth)
  assert response.status_code == 200
  assert captured == {"target_sha": target, "image_digest": None}


def test_finish_preview_verifies_exact_managed_image(client, auth, monkeypatch):
  from app import platform_update
  target = "1" * 40
  digest = "sha256:" + "2" * 64
  captured = {}
  monkeypatch.setattr(deployment_control, "applied_release_sha", lambda: target)
  monkeypatch.setattr("app.platform_activation.deployment_kind", lambda: "railway")

  async def applied_digest(sha):
    assert sha == target
    return digest

  def preview(**kwargs):
    captured.update(kwargs)
    return platform_update.empty_platform_update_preview()

  monkeypatch.setattr(deployment_control, "applied_release_digest", applied_digest)
  monkeypatch.setattr(platform_update, "platform_update_preview", preview)
  response = client.get("/api/platform/update-preview?intent=finish", headers=auth)
  assert response.status_code == 200
  assert captured == {"target_sha": target, "image_digest": digest}


def test_finish_rejects_source_that_moved_behind_selected_release(client, auth, monkeypatch):
  from app import platform_update
  target = "1" * 40
  monkeypatch.setattr(deployment_control, "applied_release_sha", lambda: target)
  monkeypatch.setattr("app.platform_activation.deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(platform_update, "platform_update_preview", lambda **kwargs: {
    # A reset between target selection and snapshot means this is now an
    # incoming update, not a legitimate Finish operation.
    "available": True, "operation": "update", "target_sha": target,
  })
  response = client.get("/api/platform/update-preview?intent=finish", headers=auth)
  assert response.status_code == 503
  assert response.json()["detail"]["code"] == "update_plan_stale"
