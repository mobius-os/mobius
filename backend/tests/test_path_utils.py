"""Unit tests for `app.path_utils.validate_path_within_base`.

Covers the three escape vectors the helper exists to block: symlink
escape, `..` traversal, and absolute-path injection. Also verifies
the happy path returns the resolved Path object.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.path_utils import validate_path_within_base


def test_returns_resolved_path_for_relative_input():
  """A normal relative path resolves under base and is returned."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    result = validate_path_within_base("file.txt", base)
    assert result == (base / "file.txt").resolve()


def test_returns_resolved_path_for_nested_relative_input():
  """Nested relative paths resolve under base and are returned."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    result = validate_path_within_base("sub/dir/file.txt", base)
    assert result == (base / "sub" / "dir" / "file.txt").resolve()


def test_rejects_dot_dot_traversal():
  """`..` segments that escape the base raise HTTPException(400)."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    with pytest.raises(HTTPException) as exc:
      validate_path_within_base("../escape.txt", base)
    assert exc.value.status_code == 400


def test_rejects_deep_dot_dot_traversal():
  """Repeated `..` segments that escape are also rejected."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    with pytest.raises(HTTPException) as exc:
      validate_path_within_base("sub/../../escape.txt", base)
    assert exc.value.status_code == 400


def test_rejects_absolute_path_injection():
  """An absolute path joined onto base resolves to the absolute and
  must be rejected (this is the key behavior `str().startswith()` got
  right but string concat missed in earlier implementations)."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    with pytest.raises(HTTPException) as exc:
      validate_path_within_base("/etc/passwd", base)
    assert exc.value.status_code == 400


def test_rejects_symlink_escape():
  """A symlink whose target is outside the base is rejected — this is
  the edge that prefix-string checks miss because they compare the
  literal joined path, not the resolved real path."""
  with tempfile.TemporaryDirectory() as td_base, \
      tempfile.TemporaryDirectory() as td_outside:
    base = Path(td_base)
    outside = Path(td_outside)
    secret = outside / "secret.txt"
    secret.write_text("nope")
    # Place a symlink inside the base that points outside.
    link = base / "link"
    os.symlink(str(outside), str(link))
    with pytest.raises(HTTPException) as exc:
      validate_path_within_base("link/secret.txt", base)
    assert exc.value.status_code == 400


def test_accepts_path_input_not_just_string():
  """The helper also accepts a `Path` instance (callers vary)."""
  with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    result = validate_path_within_base(Path("nested/file.txt"), base)
    assert result == (base / "nested" / "file.txt").resolve()
