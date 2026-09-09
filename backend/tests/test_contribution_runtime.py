"""Server-private contribution recovery storage boundaries."""

import json
import stat
from types import SimpleNamespace

import pytest

from app import contribution_runtime
from app.storage_io import atomic_write
from test_app_fixtures import create_local_app


def _make_app_and_token(client, owner_token) -> tuple[int, str]:
  owner_headers = {"Authorization": f"Bearer {owner_token}"}
  app = create_local_app(
    client,
    owner_headers,
    name="contribution-runtime-boundary",
  )
  response = client.post(
    "/api/auth/app-token",
    headers=owner_headers,
    json={"app_id": app["id"]},
  )
  assert response.status_code == 200, response.text
  return app["id"], response.json()["token"]


def test_private_artifact_reads_do_not_create_and_writes_use_private_dirs(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(
    contribution_runtime,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(tmp_path)),
  )

  read_path = contribution_runtime.personal_attempt_path(7, "review-one")
  assert not read_path.exists()
  assert not (tmp_path / ".contribution-runtime").exists()

  write_path = contribution_runtime.personal_attempt_path(
    7, "review-one", create_parent=True,
  )
  atomic_write(write_path, b"private receipt")

  assert write_path.read_bytes() == b"private receipt"
  assert write_path.is_relative_to(tmp_path / ".contribution-runtime")
  assert not write_path.is_relative_to(tmp_path / "apps")
  assert not write_path.is_relative_to(tmp_path / "shared")
  for directory in (
    tmp_path / ".contribution-runtime",
    tmp_path / ".contribution-runtime" / "7",
    tmp_path / ".contribution-runtime" / "7" / "review-one",
  ):
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("artifact", [
  "personal_attempt", "relay_claim", "relay_request",
])
def test_private_artifact_helpers_reject_symlink_traversal(
  artifact, tmp_path, monkeypatch,
):
  monkeypatch.setattr(
    contribution_runtime,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(tmp_path)),
  )
  outside = tmp_path / "outside"
  outside.mkdir()
  runtime_root = tmp_path / ".contribution-runtime"
  runtime_root.symlink_to(outside, target_is_directory=True)

  with pytest.raises(OSError, match="symlink"):
    contribution_runtime.contribution_artifact_path(
      7, "review-one", artifact, create_parent=True,
    )
  assert list(outside.iterdir()) == []


@pytest.mark.parametrize("component", ["app", "record", "artifact"])
def test_private_artifact_helper_rejects_nested_symlink_traversal(
  component, tmp_path, monkeypatch,
):
  monkeypatch.setattr(
    contribution_runtime,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(tmp_path)),
  )
  outside = tmp_path / "outside"
  outside.mkdir()
  runtime_root = tmp_path / ".contribution-runtime"
  runtime_root.mkdir()
  app_root = runtime_root / "7"
  record_root = app_root / "review-one"
  if component == "app":
    app_root.symlink_to(outside, target_is_directory=True)
  elif component == "record":
    app_root.mkdir()
    record_root.symlink_to(outside, target_is_directory=True)
  else:
    app_root.mkdir()
    record_root.mkdir()
    (record_root / "personal-submit.json").symlink_to(outside / "receipt")

  with pytest.raises(OSError, match="symlink"):
    contribution_runtime.personal_attempt_path(
      7, "review-one", create_parent=True,
    )
  assert list(outside.iterdir()) == []


@pytest.mark.parametrize("app_id", [0, -1, True, "7"])
def test_private_artifact_helper_rejects_invalid_app_ids(app_id):
  with pytest.raises(ValueError, match="app_id"):
    contribution_runtime.personal_attempt_path(app_id, "review-one")


@pytest.mark.parametrize("record_id", [
  "", ".hidden", "../escape", "nested/escape", "x" * 129,
])
def test_private_artifact_helper_rejects_invalid_record_ids(record_id):
  with pytest.raises(ValueError, match="record_id"):
    contribution_runtime.relay_claim_path(7, record_id)


def test_private_artifact_helper_rejects_unknown_artifact():
  with pytest.raises(ValueError, match="artifact"):
    contribution_runtime.contribution_artifact_path(
      7, "review-one", "../../shared",
    )


def test_storage_routes_cannot_reach_private_recovery_artifacts(
  client, owner_token,
):
  app_id, app_token = _make_app_and_token(client, owner_token)
  record_id = "private-recovery"
  artifacts = {
    "personal-submit": contribution_runtime.personal_attempt_path(
      app_id, record_id, create_parent=True,
    ),
    "relay-claim": contribution_runtime.relay_claim_path(
      app_id, record_id, create_parent=True,
    ),
    "relay-request": contribution_runtime.relay_request_path(
      app_id, record_id, create_parent=True,
    ),
  }
  for kind, path in artifacts.items():
    atomic_write(path, json.dumps({"kind": kind, "secret": "server-owned"}))

  owner_headers = {"Authorization": f"Bearer {owner_token}"}
  app_headers = {"Authorization": f"Bearer {app_token}"}
  for headers in (owner_headers, app_headers):
    listing = client.get(
      f"/api/storage/apps-list/{app_id}/contributions?include_content=true",
      headers=headers,
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["entries"] == []

    for kind, private_path in artifacts.items():
      public_path = f"contributions/.{record_id}.{kind}.json"
      direct = f"/api/storage/apps/{app_id}/{public_path}"
      assert client.get(direct, headers=headers).status_code == 404
      assert client.delete(direct, headers=headers).status_code == 404

      before = private_path.read_bytes()
      shadow = client.put(
        direct,
        headers=headers,
        json={"kind": kind, "secret": "app-writable-shadow"},
      )
      assert shadow.status_code == 204, shadow.text
      assert private_path.read_bytes() == before

      moved_path = f"contributions/moved-{kind}.json"
      moved = client.post(
        f"/api/storage/apps/{app_id}/move",
        headers=headers,
        json={"from": public_path, "to": moved_path},
      )
      assert moved.status_code == 204, moved.text
      assert private_path.read_bytes() == before
      assert client.delete(
        f"/api/storage/apps/{app_id}/{moved_path}", headers=headers,
      ).status_code == 204
      assert private_path.read_bytes() == before

    # Removing the app-writable shadow directory cannot touch the private root.
    client.put(
      f"/api/storage/apps/{app_id}/contributions/shadow.json",
      headers=headers,
      json={"shadow": True},
    )
    removed = client.delete(
      f"/api/storage/apps/{app_id}/folder/contributions", headers=headers,
    )
    assert removed.status_code == 204, removed.text
    assert all(path.is_file() for path in artifacts.values())


def test_cleanup_removes_only_empty_private_directories(tmp_path, monkeypatch):
  monkeypatch.setattr(
    contribution_runtime,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(tmp_path)),
  )
  claim = contribution_runtime.relay_claim_path(
    7, "review-one", create_parent=True,
  )
  request = contribution_runtime.relay_request_path(
    7, "review-one", create_parent=True,
  )
  atomic_write(claim, b"claim")
  atomic_write(request, b"request")

  claim.unlink()
  contribution_runtime.cleanup_empty_runtime_dirs(7, "review-one")
  assert request.is_file()

  request.unlink()
  contribution_runtime.cleanup_empty_runtime_dirs(7, "review-one")
  assert not (tmp_path / ".contribution-runtime").exists()


def test_cleanup_ignores_an_unsafe_private_root(tmp_path, monkeypatch):
  monkeypatch.setattr(
    contribution_runtime,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(tmp_path)),
  )
  outside = tmp_path / "outside"
  outside.mkdir()
  (tmp_path / ".contribution-runtime").symlink_to(
    outside, target_is_directory=True,
  )

  contribution_runtime.cleanup_empty_runtime_dirs(7, "review-one")

  assert list(outside.iterdir()) == []
