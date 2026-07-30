import logging
import signal

from app import process_groups


def test_isolated_process_group_id_refuses_shared_group(monkeypatch):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 4000)

  assert process_groups.isolated_process_group_id(4321) is None


def test_lower_process_group_priority_targets_proven_private_group(monkeypatch):
  calls = []
  monkeypatch.setattr(process_groups.os, "getpgid", lambda pid: pid)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda which, who, priority: calls.append((which, who, priority)),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is True
  assert calls == [
    (
      process_groups.os.PRIO_PGRP,
      4321,
      process_groups.BACKGROUND_PROCESS_NICE,
    ),
  ]


def test_lower_process_group_priority_refuses_unverified_group(monkeypatch):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(
      AssertionError("unverified group must not be adjusted"),
    ),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False


def test_lower_process_group_priority_refuses_missing_group(monkeypatch):
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(
      AssertionError("missing group must not be adjusted"),
    ),
  )

  assert process_groups.lower_process_group_priority(
    None,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False


def test_lower_process_group_priority_is_nonfatal(monkeypatch, caplog):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda pid: pid)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False
  assert "test priority adjustment failed pgid=4321: denied" in caplog.text


def test_terminate_process_group_has_sigkill_backstop(monkeypatch):
  calls = []
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "killpg",
    lambda pgid, sig: calls.append((pgid, sig)),
  )

  assert process_groups.terminate_process_group(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
    grace_seconds=0,
  ) is True
  assert calls == [
    (4321, signal.SIGTERM),
    (4321, signal.SIGKILL),
  ]
