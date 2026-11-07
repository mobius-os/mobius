"""Unexpected Send failures are mapped by signed phase, with one outcome event."""
import json

import pytest

from app.routes import github as routes
from tests.test_github_routes import (
  _app_token,
  _prepared_new_stack,
  _prepared_real_review,
  _write_contribution,
  _write_token,
)


@pytest.fixture
def events(monkeypatch):
  seen = []
  monkeypatch.setattr(routes.activity, "log_event", lambda ev, **fields: seen.append({"ev": ev, **fields}))
  return seen


def _quiet_source(monkeypatch):
  monkeypatch.setattr(routes, "_assert_personal_publication_source", lambda *_a, **_k: "exact")
  monkeypatch.setattr(routes, "_equivalence_source_repo", lambda _r: None)
  async def no_witness(*_a, **_k): return None
  monkeypatch.setattr(routes, "_record_pending_equivalence_locked", no_witness)


def _crash_after(phase):
  def publish(record, _diff, *, attempt_event, **_kwargs):
    if phase:
      attempt_event(phase, {"action": "create_pr", "head_sha": record["plan"]["head_sha"]}, {})
    raise RuntimeError("connection reset after GitHub accepted the request")
  return publish


def _assert_event(event, *, action, phase, status, code, certainty):
  assert event["ev"] == "contribution_publication_outcome"
  assert {k: event[k] for k in ("action", "phase", "status", "code", "certainty")} == {
    "action": action, "phase": phase, "status": status, "code": code, "certainty": certainty,
  }
  assert len(event["attempt_id"]) == 16
  # Content-free: no public text, patch, path, credential or Git output.
  assert set(event) == {"ev", "app_id", "attempt_id", "action", "phase", "code", "status", "certainty"}


@pytest.mark.parametrize("phase,status,code,certainty,retained", [
  (None, 500, "publication_failed", "none", False),
  ("branch_published", 503, "publication_unconfirmed", "unknown", True),
  ("pr_ambiguous", 503, "publication_unconfirmed", "unknown", True),
])
def test_submit_unexpected_failure_is_definite_only_before_a_public_effect(
  client, owner_token, monkeypatch, events, phase, status, code, certainty, retained,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _repo, record, _diff = _prepared_real_review(app_id, f"generic-{phase}")
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_submit_prepared_pr", _crash_after(phase))
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == status, response.text
  detail = response.json()["detail"]
  assert detail["code"] == code and detail["record"]["last_submit_error_code"] == code
  if retained:
    assert detail["message"] == routes._PUBLICATION_UNCONFIRMED
  path, _ = routes._record_paths(app_id, record["id"])
  receipt = routes._read_personal_attempt(path, app_id=app_id, record_id=record["id"])
  assert (receipt is not None) is retained
  [event] = events
  _assert_event(event, action="submit", phase=phase or "armed", status=status, code=code,
                certainty=certainty)


def test_successful_submit_logs_one_settled_effect(client, owner_token, monkeypatch, events):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _repo, record, _diff = _prepared_real_review(app_id, "generic-success")
  _quiet_source(monkeypatch)
  url = "https://github.com/mobius-os/app-demo/pull/5"
  monkeypatch.setattr(routes, "_submit_prepared_pr", lambda *_a, **_k: (url, 5, {}))
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  [event] = events
  _assert_event(event, action="submit", phase="complete", status=200, code="", certainty="effect")


@pytest.mark.parametrize("phase,status,code", [
  (None, 500, "publication_failed"),
  ("push_pending", 503, "publication_unconfirmed"),
])
def test_update_existing_has_submits_uncertainty_branch(
  client, owner_token, monkeypatch, events, phase, status, code,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _repo, record, diff = _prepared_real_review(app_id, f"update-{phase}")
  record.update(number=9, url="https://github.com/mobius-os/app-demo/pull/9",
                head_repository="octocat/app-demo")
  record["plan"].update(action="pr_update",
                        pr_metadata={"old_title": record["plan"]["title"],
                                     "old_body": record["plan"]["body_draft"]})
  _write_contribution(app_id, record["id"], record, diff)
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_autopilot_live_target", lambda *_a: {
    "error": None, "head_sha": record["plan"]["base_sha"], "base_branch": "main",
    "title": record["plan"]["title"], "body": record["plan"]["body_draft"],
  })
  monkeypatch.setattr(routes, "_assert_reviewed_update_contains_live_head", lambda *_a: None)
  monkeypatch.setattr(routes, "_submit_prepared_pr", _crash_after(phase))
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/update-existing",
    headers={"Authorization": f"Bearer {owner_token}"}, json={},
  )
  assert response.status_code == status, response.text
  assert response.json()["detail"]["code"] == code
  if status == 503:
    assert response.json()["detail"]["message"] == routes._PUBLICATION_UNCONFIRMED
  [event] = events
  assert event["action"] == "update_existing" and event["code"] == code


def test_partial_stack_success_is_kept_and_only_the_unsent_layer_is_uncertain(
  client, owner_token, monkeypatch, events,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_ids, _records = _prepared_new_stack(app_id, "partial-generic")
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_preflight_prepared_stack", lambda *_a, **_k: None)
  calls = []
  def publish(record, _diff, *, attempt_event, **_kwargs):
    calls.append(record["id"])
    if record["id"] == record_ids[0]:
      return "https://github.com/mobius-os/mobius/pull/31", 31, {}
    return _crash_after("branch_published")(record, _diff, attempt_event=attempt_event)
  monkeypatch.setattr(routes, "_submit_prepared_pr", publish)
  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": record_ids, "publication_stage": "draft"},
  )
  assert response.status_code == 503, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "publication_unconfirmed"
  statuses = {row["id"]: row["status"] for row in detail["records"]}
  assert statuses[record_ids[0]] in {"draft", "open"}
  assert calls == record_ids
  assert [(e["action"], e["certainty"]) for e in events] == [
    ("submit_stack", "effect"), ("submit_stack", "unknown"),
  ]
  # The settled parent's receipt is gone; only the uncertain child keeps one.
  kept = [routes._read_personal_attempt(routes._record_paths(app_id, rid)[0], app_id=app_id, record_id=rid)
          for rid in record_ids]
  assert kept[0] is None and kept[1]["phase"] == "branch_published"


def test_armed_screenshot_send_failure_is_unconfirmed_and_keeps_its_receipt(
  client, owner_token, monkeypatch, events,
):
  """Approved images may already be public before the first code-push phase."""
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _repo, record, diff = _prepared_real_review(app_id, "armed-screenshots")
  record["plan"]["screenshots"] = [{"name": "after.png", "sha256": "f" * 64, "alt": "After"}]
  record["plan"]["body_draft"] += "\n\n![After](screenshot:after.png)"
  _write_contribution(app_id, record["id"], record, diff)
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_submit_prepared_pr", _crash_after(None))
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 503, response.text
  assert response.json()["detail"]["code"] == "publication_unconfirmed"
  path, _ = routes._record_paths(app_id, record["id"])
  receipt = routes._read_personal_attempt(path, app_id=app_id, record_id=record["id"])
  assert receipt is not None and receipt["phase"] == "armed"
  [event] = events
  assert event["certainty"] == "unknown"


def _accepted(phase="complete"):
  url = "https://github.com/mobius-os/app-demo/pull/5"
  def publish(record, _diff, *, attempt_event, **_kwargs):
    attempt_event(phase, {"action": "create_pr", "url": url, "number": 5,
                          "head_sha": record["plan"]["head_sha"]}, {})
    return url, 5, {}
  return publish


@pytest.mark.parametrize("route,writer", [
  ("submit", "_mark_submit_success"), ("update-existing", "_mark_existing_pr_update_success"),
])
def test_failed_final_ledger_write_is_unconfirmed_and_keeps_the_receipt(
  client, owner_token, monkeypatch, events, route, writer,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = f"ledger-fail-{route}"
  _repo, record, diff = _prepared_real_review(app_id, record_id)
  if route == "update-existing":
    _as_update(app_id, record, diff, monkeypatch)
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_submit_prepared_pr", _accepted())
  def full_disk(**_kwargs): raise OSError("no space left on device")
  monkeypatch.setattr(routes, writer, full_disk)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/{route}",
    headers={"Authorization": f"Bearer {owner_token}"}, json={},
  )
  assert response.status_code == 503, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "publication_unconfirmed" and detail["number"] == 5
  path, _ = routes._record_paths(app_id, record_id)
  receipt = routes._read_personal_attempt(path, app_id=app_id, record_id=record_id)
  assert receipt is not None and receipt["phase"] == "complete"
  assert json.loads(path.read_text())["status"] == "submitting"  # nothing rolled back
  [event] = events
  assert event["certainty"] == "unknown" and event["status"] == 503


@pytest.mark.parametrize("route", ["submit", "update-existing"])
def test_failed_receipt_retirement_never_reports_a_durable_success_as_failed(
  client, owner_token, monkeypatch, events, route,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = f"retire-fail-{route}"
  _repo, record, diff = _prepared_real_review(app_id, record_id)
  if route == "update-existing":
    _as_update(app_id, record, diff, monkeypatch)
  _quiet_source(monkeypatch)
  monkeypatch.setattr(routes, "_submit_prepared_pr", _accepted())
  def stuck(_self): raise OSError("read-only file system")
  monkeypatch.setattr(routes._PersonalAttemptOwner, "settle", stuck)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/{route}",
    headers={"Authorization": f"Bearer {owner_token}"}, json={},
  )
  assert response.status_code == 200, response.text
  assert response.json()["number"] == 5
  [event] = events
  assert event["certainty"] == "effect"


def _as_update(app_id, record, diff, monkeypatch):
  record.update(number=5, url="https://github.com/mobius-os/app-demo/pull/5",
                head_repository="octocat/app-demo")
  record["plan"].update(action="pr_update",
                        pr_metadata={"old_title": record["plan"]["title"],
                                     "old_body": record["plan"]["body_draft"]})
  _write_contribution(app_id, record["id"], record, diff)
  monkeypatch.setattr(routes, "_autopilot_live_target", lambda *_a: {
    "error": None, "head_sha": record["plan"]["base_sha"], "base_branch": "main",
    "title": record["plan"]["title"], "body": record["plan"]["body_draft"],
  })
  monkeypatch.setattr(routes, "_assert_reviewed_update_contains_live_head", lambda *_a: None)
