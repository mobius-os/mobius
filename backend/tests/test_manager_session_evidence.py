import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = (
  Path(__file__).resolve().parents[1]
  / "scripts"
  / "seed-skills"
  / "manager-session-evidence.py"
)
SPEC = importlib.util.spec_from_file_location("manager_session_evidence", SCRIPT)
manager_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager_evidence)


class ManagerSessionEvidenceTests(unittest.TestCase):
  def test_reflection_section_marks_metric_backed_run_without_interview(self):
    with tempfile.TemporaryDirectory() as raw:
      root = Path(raw)
      metrics = root / "reflection-run-metrics.jsonl"
      metrics.write_text(json.dumps({
        "started_at": "2026-07-28T02:00:00+00:00",
        "exit_code": 0,
        "duration_seconds": 42,
        "brief_written": True,
      }) + "\n")
      output = io.StringIO()
      with (
        mock.patch.object(manager_evidence, "REFLECTION_METRICS", str(metrics)),
        mock.patch.object(
          manager_evidence,
          "REFLECTION_RUNS",
          str(root / "missing-runs"),
        ),
        contextlib.redirect_stdout(output),
      ):
        manager_evidence.section_reflection(5)

    self.assertIn("2026-07-28", output.getvalue())
    self.assertIn("[no interview capture]", output.getvalue())

  def test_writer_diff_is_limited_to_reported_memory_paths(self):
    with tempfile.TemporaryDirectory() as raw:
      repo = Path(raw)
      subprocess.run(["git", "init", "-q", repo], check=True)
      subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Test"],
        check=True,
      )
      subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"],
        check=True,
      )
      notes = repo / "notes"
      notes.mkdir()
      selected = notes / "selected.md"
      unrelated = notes / "unrelated.md"
      selected.write_text("before\n")
      unrelated.write_text("before\n")
      subprocess.run(["git", "-C", repo, "add", "notes"], check=True)
      subprocess.run(
        ["git", "-C", repo, "commit", "-qm", "base"],
        check=True,
      )
      before = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        text=True,
      ).strip()

      selected.write_text("selected update\n")
      unrelated.write_text("unrelated update\n")
      subprocess.run(["git", "-C", repo, "add", "notes"], check=True)
      subprocess.run(
        ["git", "-C", repo, "commit", "-qm", "update"],
        check=True,
      )
      after = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        text=True,
      ).strip()

      with mock.patch.object(
        manager_evidence,
        "MEM_REPOSITORY",
        str(repo),
      ):
        diff = manager_evidence._applied_memory_diff({
          "previous_commit": before,
          "commit": after,
          "changed_paths": ["notes/selected.md"],
        })

    self.assertIn("selected update", diff)
    self.assertNotIn("unrelated update", diff)
    self.assertNotIn("notes/unrelated.md", diff)


if __name__ == "__main__":
  unittest.main()
