"""Route-level wiring for the platform updater endpoints.

The reconcile plumbing is covered exhaustively in ``test_platform_update.py``
against throwaway clones; these assert the HTTP surface: owner-gating and the
degrade-to-empty contract the Settings review step relies on. There is no real
``/data/platform`` clone in the test env, so ``GET /update-preview`` returns the
empty preview — which is exactly the "nothing to review" shape the sheet reads.
"""


def test_update_preview_requires_owner(client):
  assert client.get("/api/platform/update-preview").status_code == 401


def test_update_preview_returns_empty_shape_without_a_clone(client, auth):
  res = client.get("/api/platform/update-preview", headers=auth)
  assert res.status_code == 200
  body = res.json()
  # The keys the review sheet + its summarizer read must always be present.
  for key in (
    "available", "state", "commits", "files", "diff", "diff_truncated",
    "conflict_paths", "plan_id", "total_commits", "commits_truncated",
  ):
    assert key in body
  assert body["available"] is False
  assert body["commits"] == []
  assert body["files"] == []
  assert body["diff"] is None
  assert body["diff_truncated"] is False
  assert body["plan_id"] is None
  assert body["total_commits"] == 0
  assert body["commits_truncated"] is False


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
      "upstream_commit": plan["target_sha"],
      "merge_commit": "3" * 40,
      "conflict_paths": [],
      "chat_id": None,
      "phase": "complete",
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
  assert captured == body
  assert res.json()["upstream_commit"] == body["target_sha"]


def test_update_check_reports_fetch_failure(client, auth, monkeypatch):
  """Settings must not translate an unreachable origin into "up to date"."""
  def fail_check():
    from app.platform_update import PlatformUpdateError
    raise PlatformUpdateError("platform_fetch_failed")

  monkeypatch.setattr("app.routes.platform.platform_update.check_for_updates",
                      fail_check)
  res = client.post("/api/platform/check", headers=auth)
  assert res.status_code == 503
  assert res.json()["detail"] == "Could not reach the platform update source."
