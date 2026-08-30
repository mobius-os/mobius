import pytest

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
