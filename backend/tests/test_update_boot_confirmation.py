"""Boot readiness must not wait for work only a replacement controller owns."""

from contextlib import contextmanager

import pytest

from app import platform_update as pu


@pytest.mark.parametrize("controller", ["host", "railway"])
def test_controller_owned_confirmation_does_not_wait_for_late_edit_resolver(
  monkeypatch, tmp_path, controller,
):
  record = {"state": "swapped", "operation": {"controller": controller, "id": "cutover"}}
  monkeypatch.setattr(pu, "read_prepared_update", lambda: record)

  @contextmanager
  def resolver_owns_lock():
    pytest.fail("Startup waited for the resolver although only the controller can confirm")
    yield

  monkeypatch.setattr(pu, "_reconcile_flock", resolver_owns_lock)
  monkeypatch.setattr(pu, "_retire_swap", lambda *_: pytest.fail("Boot retired controller rollback"))

  assert pu.confirm_platform_swap_loaded(tmp_path) is False
  assert record["operation"]["id"] == "cutover"


def test_server_confirmation_rechecks_controller_ownership_under_lock(monkeypatch, tmp_path):
  record = {"state": "swapped", "operation": None}
  monkeypatch.setattr(pu, "read_prepared_update", lambda: dict(record))

  @contextmanager
  def controller_binds_before_lock():
    record["operation"] = {"controller": "host", "id": "new-cutover"}
    yield

  monkeypatch.setattr(pu, "_reconcile_flock", controller_binds_before_lock)
  monkeypatch.setattr(pu, "_retire_swap", lambda *_: pytest.fail("Concurrent controller lost rollback"))

  assert pu.confirm_platform_swap_loaded(tmp_path) is False


def test_unbound_server_confirmation_still_retires_only_under_lock(monkeypatch, tmp_path):
  record = {"state": "swapped", "operation": None}
  held = False
  retired = []
  monkeypatch.setattr(pu, "read_prepared_update", lambda: record)
  monkeypatch.setattr(pu, "_serves_replayed", lambda *_: True)

  @contextmanager
  def lock():
    nonlocal held
    held = True
    try:
      yield
    finally:
      held = False

  def retire(repo, value):
    assert held
    retired.append((repo, value))

  monkeypatch.setattr(pu, "_reconcile_flock", lock)
  monkeypatch.setattr(pu, "_retire_swap", retire)

  assert pu.confirm_platform_swap_loaded(tmp_path) is True
  assert retired == [(tmp_path, record)]
