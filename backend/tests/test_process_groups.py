import logging
import signal

from app import process_groups


def test_isolated_process_group_id_refuses_shared_group(monkeypatch):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 4000)

  assert process_groups.isolated_process_group_id(4321) is None


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
