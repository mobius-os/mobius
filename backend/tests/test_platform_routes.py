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
    "conflict_paths",
  ):
    assert key in body
  assert body["available"] is False
  assert body["commits"] == []
  assert body["files"] == []
  assert body["diff"] is None
  assert body["diff_truncated"] is False
