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
  def test_skill_section_labels_old_aggregate_counts_as_legacy_unknown(self):
    output = io.StringIO()
    with (
      mock.patch.object(
        manager_evidence,
        "api_json",
        return_value={"skills": [{"skill": "memory", "count": 7}]},
      ),
      contextlib.redirect_stdout(output),
    ):
      manager_evidence.section_skill_loads(24)

    self.assertIn("complete=0", output.getvalue())
    self.assertIn("legacy=7", output.getvalue())

  def test_memory_section_keeps_unmatched_scheduler_receipt_separate(self):
    with tempfile.TemporaryDirectory() as raw:
      root = Path(raw)
      status = root / "run-status.json"
      status.write_text(json.dumps({
        "status": "published",
        "started_at": "2026-07-28T05:30:00+00:00",
        "finished_at": "2026-07-28T05:35:00+00:00",
      }))
      output = io.StringIO()
      with (
        mock.patch.object(manager_evidence, "MEM_RUN_STATUS", str(status)),
        mock.patch.object(
          manager_evidence, "MEM_UPDATE_LOG", str(root / "missing-updates"),
        ),
        mock.patch.object(
          manager_evidence, "installed_app", return_value={"id": 57},
        ),
        mock.patch.object(
          manager_evidence,
          "latest_cron_outcome",
          return_value={
            "ev": "cron_outcome",
            "app_id": 57,
            "job": "fetch.sh",
            "exit_code": 4,
            "ts": "2026-07-29T05:30:00+00:00",
          },
        ),
        contextlib.redirect_stdout(output),
      ):
        manager_evidence.section_memory(3)

    rendered = output.getvalue()
    self.assertIn("outer scheduler receipt (not run-id linked): exit=4", rendered)
    self.assertNotIn("do not report Memory healthy", rendered)

  def test_memory_run_lookup_returns_last_terminal_receipt_for_exact_run(self):
    with tempfile.TemporaryDirectory() as raw:
      run_log = Path(raw)
      (run_log / "2026-07-20.jsonl").write_text("\n".join([
        "not-json",
        json.dumps({"run_id": "target", "status": "running"}),
        json.dumps({"run_id": "other", "status": "failed"}),
        json.dumps({"run_id": "target", "status": "published", "seq": 1}),
        json.dumps({"run_id": "target", "status": "abandoned", "seq": 2}),
      ]) + "\n")
      with mock.patch.object(manager_evidence, "MEM_RUN_LOG", str(run_log)):
        receipt = manager_evidence._memory_run_for_id("target")
        missing = manager_evidence._memory_run_for_id("missing")

    self.assertEqual(receipt["status"], "abandoned")
    self.assertEqual(receipt["seq"], 2)
    self.assertIsNone(missing)

  def test_memory_section_prints_complete_update_run_id(self):
    run_id = "12345678-1234-5678-1234-567812345678"
    output = io.StringIO()
    with (
      mock.patch.object(manager_evidence, "MEM_RUN_STATUS", "/missing/status"),
      mock.patch.object(manager_evidence, "installed_app", return_value=None),
      mock.patch.object(
        manager_evidence,
        "_read_update_log",
        return_value=[{"run_id": run_id, "status": "published"}],
      ),
      contextlib.redirect_stdout(output),
    ):
      manager_evidence.section_memory(3)

    self.assertIn(f"run={run_id}", output.getvalue())

  def test_reflection_section_lists_artifacts_without_inventing_interviews(self):
    with tempfile.TemporaryDirectory() as raw:
      root = Path(raw)
      metrics = root / "reflection-run-metrics.jsonl"
      metrics.write_text(json.dumps({
        "started_at": "2026-07-28T02:00:00+00:00",
        "exit_code": 0,
        "duration_seconds": 42,
        "brief_written": True,
      }) + "\n")
      run_dir = root / "runs" / "2026-07-28"
      run_dir.mkdir(parents=True)
      (run_dir / "failed-interview.txt").write_text("provider error\n")
      (run_dir / "memory-writer-review.md").write_text("review\n")
      output = io.StringIO()
      with (
        mock.patch.object(manager_evidence, "REFLECTION_METRICS", str(metrics)),
        mock.patch.object(
          manager_evidence,
          "REFLECTION_RUNS",
          str(root / "runs"),
        ),
        contextlib.redirect_stdout(output),
      ):
        manager_evidence.section_reflection(5)

    rendered = output.getvalue()
    self.assertIn("2026-07-28", rendered)
    self.assertIn("failed-interview.txt", rendered)
    self.assertIn("memory-writer-review.md", rendered)
    self.assertNotIn("[interview:", rendered)
    self.assertNotIn("[no interview capture]", rendered)

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

  def test_writer_packet_prefers_native_run_testimony(self):
    output = io.StringIO()
    outcome = {
      "run_id": "run-1",
      "status": "published",
      "writer_self_reviews": [{
        "hardest_decision": "Which route to shorten.",
        "possibly_missed": "none",
        "prompt_change": "none",
      }],
    }
    with (
      mock.patch.object(manager_evidence, "_read_update_log", return_value=[outcome]),
      mock.patch.object(manager_evidence, "_recall_audits_for_run", return_value=[]),
      mock.patch.object(manager_evidence, "_read_json", return_value={"run_id": "run-1"}),
      mock.patch.object(manager_evidence, "_read_text", return_value="skill"),
      mock.patch.object(manager_evidence, "_function_source", return_value="prompt"),
      mock.patch.object(manager_evidence, "_applied_memory_diff", return_value="diff"),
      contextlib.redirect_stdout(output),
    ):
      manager_evidence.section_memory_writer_packet()

    self.assertIn("testimony=native writer self-review", output.getvalue())
    self.assertIn("Which route to shorten", output.getvalue())

  def test_writer_packet_never_mixes_current_status_into_prior_outcome(self):
    output = io.StringIO()
    outcome = {"run_id": "published-run", "status": "published"}
    historical = {
      "run_id": "published-run",
      "provider_summary": [{
        "provider": "codex", "model": "historical",
        "accepted": 5, "considered": 5, "failures": {},
      }],
      "pending_chat_count": 12,
      "queued_chat_count": 40,
      "deferred_chat_count": 2,
    }
    current = {
      "run_id": "current-run",
      "provider_summary": [{
        "provider": "claude", "model": "current",
        "accepted": 1, "considered": 9, "failures": {"invalid": 8},
      }],
      "pending_chat_count": 999,
      "queued_chat_count": 100,
      "deferred_chat_count": 90,
    }
    with (
      mock.patch.object(manager_evidence, "_read_update_log", return_value=[outcome]),
      mock.patch.object(manager_evidence, "_recall_audits_for_run", return_value=[]),
      mock.patch.object(manager_evidence, "_read_json", return_value=current),
      mock.patch.object(manager_evidence, "_memory_run_for_id", return_value=historical),
      mock.patch.object(manager_evidence, "_read_text", return_value="skill"),
      mock.patch.object(manager_evidence, "_function_source", return_value="prompt"),
      contextlib.redirect_stdout(output),
    ):
      manager_evidence.section_memory_writer_packet()

    rendered = output.getvalue()
    self.assertIn('"provider": "codex"', rendered)
    self.assertIn('"model": "historical"', rendered)
    self.assertIn('"pending_chat_count": 12', rendered)
    self.assertNotIn('"provider": "claude"', rendered)
    self.assertNotIn('"pending_chat_count": 999', rendered)

  def test_writer_packet_never_matches_missing_run_ids(self):
    output = io.StringIO()
    current = {
      "provider_summary": [{"provider": "current-without-id"}],
      "pending_chat_count": 999,
    }
    with (
      mock.patch.object(
        manager_evidence,
        "_read_update_log",
        return_value=[{"status": "published"}],
      ),
      mock.patch.object(manager_evidence, "_recall_audits_for_run", return_value=[]),
      mock.patch.object(manager_evidence, "_read_json", return_value=current),
      mock.patch.object(manager_evidence, "_memory_run_for_id", return_value=None),
      mock.patch.object(manager_evidence, "_read_text", return_value="skill"),
      mock.patch.object(manager_evidence, "_function_source", return_value="prompt"),
      mock.patch.object(manager_evidence, "_applied_memory_diff", return_value="diff"),
      contextlib.redirect_stdout(output),
    ):
      manager_evidence.section_memory_writer_packet()

    rendered = output.getvalue()
    self.assertIn("writer outcome has no run_id", rendered)
    self.assertNotIn("current-without-id", rendered)
    self.assertNotIn('"pending_chat_count": 999', rendered)


if __name__ == "__main__":
  unittest.main()
