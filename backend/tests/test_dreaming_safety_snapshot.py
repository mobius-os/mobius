"""Feature 112: Dreaming takes a guaranteed pre-run git snapshot of /data.

The nightly run consolidates the memory graph, rewrites skills, and fixes apps
— destructive overwrites of agent-owned files under /data/shared. The
"git is the undo" promise (mind.md) previously rested only on the agent's own
mid-run pm-commit discipline, so a consolidation that overwrote a note before
the first commit had no pre-state restore point beyond last night's. The runner
now commits the current tree as the very first thing the run does.
"""

from unittest.mock import patch, MagicMock

import scripts.dreaming_runner as dr


def test_safety_snapshot_commits_with_allow_broad():
  calls = []

  def fake_run(cmd, **kwargs):
    calls.append((cmd, kwargs))
    return MagicMock(returncode=0, stderr="", stdout="")

  with patch.object(dr.subprocess, "run", fake_run):
    dr._safety_snapshot("dreaming: pre-run snapshot 2026-06-08")

  assert len(calls) == 1
  cmd, kwargs = calls[0]
  assert cmd[0] == dr.PM_COMMIT
  # --allow-broad: a full day's accumulated changes must not trip pm-commit's
  # 50-file refusal, or the safety snapshot silently doesn't happen.
  assert "--allow-broad" in cmd
  assert cmd[-1] == "dreaming: pre-run snapshot 2026-06-08"
  assert str(kwargs.get("cwd")) == str(dr.DATA_DIR)


def test_safety_snapshot_swallows_failure():
  """A snapshot failure must NEVER abort the night's run."""
  def boom(cmd, **kwargs):
    raise OSError("git exploded")

  with patch.object(dr.subprocess, "run", boom):
    dr._safety_snapshot("x")  # must not raise
