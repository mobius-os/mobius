"""A session never carries over a switch between production and staging.

Each token names the launcher it was issued under and is refused under any
other; the first boot on a new launcher also signs everyone out. Health tells
anyone which launcher the instance signs people in with.
"""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

import jwt
import pytest

from app import auth as auth_module
from app import launcher_side, models
from app.config import get_settings

PRODUCTION = "https://www.mobius.example"
STAGING = "https://staging.mobius.example"


@pytest.fixture
def instance(monkeypatch, tmp_path):
  """A developer's instance on production, with its own data directory."""
  settings = get_settings()
  monkeypatch.setattr(settings, "data_dir", str(tmp_path))
  monkeypatch.setattr(settings, "mobius_sso_instance_id", "mob_developer")

  class Instance:
    data_dir = tmp_path

    def run_on(self, issuer: str, *, side: str = "production", available: bool = True):
      monkeypatch.setattr(settings, "mobius_sso_issuer", issuer)
      monkeypatch.setattr(
        launcher_side, "_boot_side",
        launcher_side.LauncherSide(available=available, side=side),
      )

  running = Instance()
  running.run_on(PRODUCTION)
  return running


def _owner_session(db) -> str:
  owner = db.query(models.Owner).one()
  return auth_module.create_access_token(
    {"sub": owner.username}, token_epoch=owner.token_epoch,
  )


def _signed_in(client, token: str) -> bool:
  response = client.get(
    "/api/platform/launcher-side", headers={"Authorization": f"Bearer {token}"},
  )
  assert response.status_code in {200, 401}, response.text
  return response.status_code == 200


def test_a_staging_session_is_refused_once_the_instance_is_on_production(
  client, owner_token, db, instance,
):
  instance.run_on(STAGING, side="staging")
  staging_session = _owner_session(db)
  assert _signed_in(client, staging_session)

  instance.run_on(PRODUCTION)

  assert not _signed_in(client, staging_session)
  assert _signed_in(client, _owner_session(db))


def test_a_production_session_is_refused_once_the_instance_is_on_staging(
  client, owner_token, db, instance,
):
  production_session = _owner_session(db)

  instance.run_on(STAGING, side="staging")

  assert not _signed_in(client, production_session)


def test_removing_the_staging_variables_ends_staging_sessions(
  client, owner_token, db, instance,
):
  instance.run_on(STAGING, side="staging")
  staging_session = _owner_session(db)

  instance.run_on(PRODUCTION, available=False)

  assert not _signed_in(client, staging_session)


def test_every_token_names_the_launcher_it_was_issued_under(db, owner_token, instance):
  token = _owner_session(db)

  claims = jwt.decode(token, options={"verify_signature": False})

  assert claims[auth_module.ISSUER_CLAIM] == PRODUCTION


def _legacy_token(db) -> str:
  owner = db.query(models.Owner).one()
  return jwt.encode(
    {"sub": owner.username, "epoch": owner.token_epoch, "exp": 2**40},
    get_settings().secret_key, algorithm="HS256",
  )


def test_a_token_from_before_issuers_still_works_on_an_ordinary_instance(
  client, owner_token, db, instance,
):
  instance.run_on(PRODUCTION, available=False)

  assert _signed_in(client, _legacy_token(db))


def test_a_token_from_before_issuers_is_refused_on_a_developer_instance(
  client, owner_token, db, instance,
):
  assert not _signed_in(client, _legacy_token(db))


class _Blocked(Exception):
  """Not an OSError, so a reader that swallows those cannot hide the stall."""


@contextlib.contextmanager
def _fails_instead_of_hanging(seconds: int = 5):
  """Turns a read that blocks on a FIFO into a failure, not a stuck suite."""
  def stop(_signum, _frame):
    raise _Blocked("blocked reading a control file")

  previous = signal.signal(signal.SIGALRM, stop)
  signal.alarm(seconds)
  try:
    yield
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def _epoch(db) -> int:
  db.expire_all()
  return db.query(models.Owner).one().token_epoch


def _record(instance) -> str:
  return (instance.data_dir / launcher_side.SESSION_ISSUER_FILE).read_text(
    encoding="utf-8",
  ).strip()


def test_the_first_boot_on_another_launcher_signs_everyone_out(
  client, owner_token, db, instance,
):
  assert launcher_side.settle_sessions(db, instance.data_dir) is True
  session = _owner_session(db)
  before = _epoch(db)

  instance.run_on(STAGING, side="staging")
  assert launcher_side.settle_sessions(db, instance.data_dir) is True

  assert _epoch(db) == before + 1
  assert _record(instance) == STAGING
  # A token minted now under the old epoch would still be refused by it.
  instance.run_on(PRODUCTION)
  assert not _signed_in(client, session)
  service = (instance.data_dir / "service-token.txt").read_text(encoding="utf-8")
  assert auth_module.decode_access_token(service) is None  # staging-issued


def test_a_boot_on_the_same_launcher_keeps_everyone_signed_in(
  client, owner_token, db, instance,
):
  launcher_side.settle_sessions(db, instance.data_dir)
  before = _epoch(db)

  assert launcher_side.settle_sessions(db, instance.data_dir) is False

  assert _epoch(db) == before
  assert _signed_in(client, _owner_session(db))


def test_an_ordinary_instance_keeps_its_sessions_on_its_first_boot(
  client, owner_token, db, instance,
):
  instance.run_on(PRODUCTION, available=False)
  session = _owner_session(db)
  before = _epoch(db)

  assert launcher_side.settle_sessions(db, instance.data_dir) is False

  assert _epoch(db) == before
  assert _record(instance) == PRODUCTION
  assert _signed_in(client, session)


def test_a_developer_instance_signs_everyone_out_on_its_first_boot(
  client, owner_token, db, instance,
):
  before = _epoch(db)

  assert launcher_side.settle_sessions(db, instance.data_dir) is True

  assert _epoch(db) == before + 1


@pytest.mark.parametrize("planted", ["fifo", "oversized", "link"])
def test_an_issuer_record_that_is_not_a_small_file_counts_as_a_switch(
  owner_token, db, instance, planted,
):
  record = instance.data_dir / launcher_side.SESSION_ISSUER_FILE
  if planted == "fifo":
    os.mkfifo(record)
  elif planted == "oversized":
    record.write_text(PRODUCTION + "\n" + " " * 4096, encoding="utf-8")
  else:
    target = instance.data_dir / "elsewhere"
    target.write_text(PRODUCTION + "\n", encoding="utf-8")
    record.symlink_to(target)
  before = _epoch(db)

  with _fails_instead_of_hanging():
    assert launcher_side.settle_sessions(db, instance.data_dir) is True

  assert _epoch(db) == before + 1
  assert not record.is_symlink()
  assert _record(instance) == PRODUCTION


def _health_issuer(client):
  response = client.get("/api/health")
  assert response.status_code == 200
  return response.json()["issuer"]


def test_health_reports_the_launcher_it_signs_people_in_with(client, instance):
  assert _health_issuer(client) == PRODUCTION

  instance.run_on(STAGING, side="staging")
  (instance.data_dir / launcher_side.SIDE_FILE).write_text("staging\n", encoding="utf-8")

  assert _health_issuer(client) == STAGING


def test_health_reports_no_issuer_while_a_switch_waits_for_its_restart(client, instance):
  launcher_side.choose_side("staging", instance.data_dir)
  assert _health_issuer(client) is None

  instance.run_on(STAGING, side="staging")
  launcher_side.choose_side("production", instance.data_dir)
  assert _health_issuer(client) is None


def test_health_reports_no_issuer_with_local_sign_in(client, monkeypatch, instance):
  monkeypatch.setattr(get_settings(), "mobius_sso_issuer", "")

  assert _health_issuer(client) is None


def test_a_staging_choice_without_staging_variables_is_not_a_pending_switch(
  client, instance,
):
  instance.run_on(PRODUCTION, available=False)
  (instance.data_dir / launcher_side.SIDE_FILE).write_text("staging\n", encoding="utf-8")

  assert _health_issuer(client) == PRODUCTION


def test_an_oversized_choice_file_is_not_a_pending_switch(client, instance):
  # The broker ignores it and boots production, so health must not wait on it.
  (instance.data_dir / launcher_side.SIDE_FILE).write_text(
    "staging\n" + " " * 4096, encoding="utf-8",
  )

  assert _health_issuer(client) == PRODUCTION


def test_a_fifo_in_place_of_the_choice_file_cannot_hang_health(instance):
  os.mkfifo(instance.data_dir / launcher_side.SIDE_FILE)

  with _fails_instead_of_hanging():
    assert launcher_side.published_issuer() == PRODUCTION


def test_short_lived_tokens_follow_the_same_rule(db, owner_token, instance):
  owner = db.query(models.Owner).one()
  media = auth_module.create_media_token("chat", owner.username, owner.token_epoch)

  instance.run_on(STAGING, side="staging")

  assert auth_module.decode_access_token(media) is None


def test_settling_needs_no_owner(db, instance):
  assert launcher_side.settle_sessions(db, Path(instance.data_dir)) is True
  assert _record(instance) == PRODUCTION
