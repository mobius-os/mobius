"""Focused contracts for the install transaction's explicit phase boundary."""

from app.install import InstallJournal


def test_install_journal_compensates_precommit_work_in_reverse(tmp_path):
  created = tmp_path / "new-source"
  created.mkdir()
  observed = []
  journal = InstallJournal(created_paths=[created])
  journal.rollback_actions.extend([
    lambda: observed.append("first"),
    lambda: observed.append("second"),
  ])

  journal.rollback_materialization()

  assert observed == ["second", "first"]
  assert not created.exists()


def test_durable_install_can_never_run_failure_compensation(tmp_path):
  selected = tmp_path / "selected-bundle"
  selected.write_text("durable")
  events = []
  journal = InstallJournal(created_paths=[selected])
  journal.rollback_actions.append(lambda: events.append("rollback"))
  journal.commit_actions.append(lambda: events.append("cleanup"))

  journal.mark_durable()
  journal.rollback_materialization()
  journal.cleanup_superseded()

  assert selected.read_text() == "durable"
  assert events == ["cleanup"]
