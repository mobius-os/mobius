"""Dreaming last-run marker — a simple bookmark, NOT an exactly-once engine.

The agent is the dedupe layer (it skips its own commits + unchanged chats); this just
records the last successfully-reviewed HEAD and tolerates a bad/absent marker by
bootstrapping. See app/dreaming_checkpoint.py for the philosophy.
"""
import pytest

from app import dreaming_checkpoint as dc


def test_marker_write_then_read_roundtrips(tmp_path):
    p = tmp_path / "last-run.json"
    m = {"repos": {"data": "abc123"}, "ts": "2026-06-15T06:00:00+00:00"}
    dc.write_marker(p, m)
    assert dc.read_marker(p) == m


def test_read_absent_returns_none(tmp_path):
    assert dc.read_marker(tmp_path / "nope.json") is None


def test_read_corrupt_bootstraps_not_fail_closed(tmp_path):
    p = tmp_path / "last-run.json"
    p.write_text("{ not valid json")
    assert dc.read_marker(p) is None   # tolerant: bootstrap, never raise


def test_write_is_atomic_prior_survives_failed_write(tmp_path, monkeypatch):
    p = tmp_path / "last-run.json"
    dc.write_marker(p, {"ts": "good"})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(dc.os, "replace", boom)
    with pytest.raises(OSError):
        dc.write_marker(p, {"ts": "bad"})
    monkeypatch.undo()
    assert dc.read_marker(p)["ts"] == "good"
    assert [f.name for f in tmp_path.iterdir()] == ["last-run.json"]


def test_exclude_pathspecs_cover_the_bloat_sources():
    joined = " ".join(dc.EXCLUDE_PATHSPECS)
    for needle in ("*.db", "logs/", "agent-browser-profiles", "Cache"):
        assert needle in joined
