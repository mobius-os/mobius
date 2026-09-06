from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import project_git, workspace_files


def test_directory_listing_has_an_inspection_ceiling(tmp_path, monkeypatch):
  for index in range(8):
    (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
  monkeypatch.setattr(workspace_files, "LIST_SCAN_LIMIT", 3)

  result = workspace_files.list_entries(tmp_path, tmp_path)

  assert result["truncated"] is True
  assert len(result["entries"]) <= 3


def test_text_read_stops_at_the_workspace_byte_ceiling(tmp_path, monkeypatch):
  target = tmp_path / "large.txt"
  target.write_text("abcdefgh", encoding="utf-8")
  monkeypatch.setattr(workspace_files, "READ_MAX", 4)

  with pytest.raises(OverflowError, match="too large"):
    workspace_files.read_file(target, "large.txt")


def test_untracked_git_preview_reads_only_one_byte_past_its_ceiling(
  tmp_path, monkeypatch,
):
  target = tmp_path / "new.txt"
  target.write_text("one\ntwo\nthree\n", encoding="utf-8")
  monkeypatch.setattr(project_git, "_DIFF_OUTPUT_MAX", 5)

  result = project_git._untracked_diff(target, "untracked")

  assert result["truncated"] is True
  assert result["additions"] <= 2


def test_revisioned_write_is_compare_and_set(tmp_path):
  target = tmp_path / "notes" / "todo.md"

  created = workspace_files.write_file(tmp_path, target, b"one", None)
  assert created["path"] == "notes/todo.md"
  assert created["revision"] == workspace_files.file_revision(target)

  with pytest.raises(HTTPException) as stale:
    workspace_files.write_file(tmp_path, target, b"two", None)
  assert stale.value.status_code == 409
  assert stale.value.detail["code"] == "file_revision_conflict"
  assert stale.value.detail["current_revision"] == created["revision"]
  assert target.read_bytes() == b"one"

  replaced = workspace_files.write_file(
    tmp_path, target, b"two", created["revision"],
  )
  assert target.read_bytes() == b"two"
  forced = workspace_files.write_file(
    tmp_path, target, b"three", "0" * 64, force=True,
  )
  assert target.read_bytes() == b"three"
  assert replaced["revision"] != forced["revision"]
  assert [p.name for p in target.parent.iterdir()] == ["todo.md"]


def test_revision_precondition_decodes_save_headers():
  def request(**headers):
    return SimpleNamespace(headers=headers)

  assert workspace_files.revision_precondition(request()) == (False, None)
  assert workspace_files.revision_precondition(
    request(**{"if-none-match": "*"}),
  ) == (True, None)
  assert workspace_files.revision_precondition(
    request(**{"if-match": f'"{"A" * 64}"'}),
  ) == (True, "a" * 64)
  with pytest.raises(HTTPException) as malformed:
    workspace_files.revision_precondition(request(**{"if-match": "W/xyz"}))
  assert malformed.value.status_code == 400
  assert workspace_files.revision_required("a.txt").status_code == 428
