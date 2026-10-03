"""Owner timezone and schedule provenance across app updates."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app import app_cron, app_git, models
from app.app_cron import ScheduleChoice
from app.config import get_settings


def test_owner_timezone_is_recorded_validated_and_readable(client, auth):
  assert client.get("/api/owner/timezone", headers=auth).json() == {
    "timezone": None,
  }

  saved = client.put(
    "/api/owner/timezone", json={"timezone": "Asia/Tokyo"}, headers=auth,
  )
  rejected = client.put(
    "/api/owner/timezone", json={"timezone": "Not/AZone"}, headers=auth,
  )

  assert saved.status_code == 200, saved.text
  assert rejected.status_code == 400
  assert client.get("/api/owner/timezone", headers=auth).json() == {
    "timezone": "Asia/Tokyo",
  }


def test_owner_timezone_requires_the_owner(client):
  response = client.put("/api/owner/timezone", json={"timezone": "UTC"})
  assert response.status_code == 401


def _owner_daily(app_id: int, default: str = "30 5 * * *") -> None:
  app_cron.record_schedule_choice(app_id, ScheduleChoice(
    source="owner", cron="15 7 * * *", job="fetch.sh",
    timezone="Asia/Tokyo", manifest_default=default,
  ))


def test_owner_daily_time_survives_an_app_retiming_its_daily_default():
  _owner_daily(9101)

  kept = app_cron.owner_schedule_to_keep(9101, "0 6 * * *", "fetch.sh")

  assert kept == ScheduleChoice(
    source="owner", cron="15 7 * * *", job="fetch.sh",
    timezone="Asia/Tokyo", manifest_default="30 5 * * *",
  )


def test_changed_schedule_contract_releases_the_owner_choice():
  _owner_daily(9102)

  assert app_cron.owner_schedule_to_keep(
    9102, "*/10 * * * *", "fetch.sh",
  ) is None
  assert app_cron.owner_schedule_to_keep(
    9102, "30 5 * * *", "refresh.sh",
  ) is None


def test_non_daily_owner_choice_survives_only_an_unchanged_default():
  app_cron.record_schedule_choice(9103, ScheduleChoice(
    source="owner", cron="0 */2 * * *", job="job.sh",
    manifest_default="0 * * * *",
  ))

  assert app_cron.owner_schedule_to_keep(9103, "0 * * * *", "job.sh")
  assert app_cron.owner_schedule_to_keep(9103, "*/30 * * * *", "job.sh") is None


def test_manifest_default_is_never_mistaken_for_an_owner_choice():
  app_cron.record_schedule_choice(9104, ScheduleChoice(
    source="manifest", cron="30 5 * * *", job="fetch.sh",
    timezone="Asia/Tokyo", manifest_default="30 5 * * *",
  ))
  _write_zone_declaration(9104, "memory", "Asia/Tokyo", "30 5 * * *")

  assert app_cron.owner_schedule_to_keep(9104, "30 5 * * *", "fetch.sh") is None


def _write_zone_declaration(app_id, slug, zone, zone_cron):
  state = app_cron.schedule_state_dir(app_id)
  state.mkdir(parents=True, exist_ok=True)
  escaped = zone_cron.replace(" ", "\\ ").replace("*", "\\*")
  (state / "init-cron.sh").write_text(
    "#!/bin/sh\n"
    f'ENTRY="* * * * * API_BASE_URL=http://localhost:8000 python3 '
    f"/app/scripts/app-job-runner.py --scheduled --wall-clock {zone} "
    f'{escaped} {app_id} /data/apps/{slug}/fetch.sh"\n'
    "exit 0\n"
    f'\nSCHEDULE_TZ="{zone}"\nSCHEDULE_SOURCE="{zone_cron}"\n'
  )


def test_owner_weekday_time_survives_only_a_same_weekdays_default():
  app_cron.record_schedule_choice(9107, ScheduleChoice(
    source="owner", cron="30 8 * * 1-5", job="fetch.sh",
    timezone="Asia/Tokyo", manifest_default="0 9 * * 1-5",
  ))

  assert app_cron.owner_schedule_to_keep(9107, "0 10 * * 1-5", "fetch.sh")
  assert app_cron.owner_schedule_to_keep(9107, "0 10 * * *", "fetch.sh") is None


def _write_server_declaration(app_id, slug, cron):
  state = app_cron.schedule_state_dir(app_id)
  state.mkdir(parents=True, exist_ok=True)
  (state / "init-cron.sh").write_text(
    f'#!/bin/sh\nENTRY="{cron} API_BASE_URL=http://localhost:8000 '
    f'python3 /app/scripts/app-job-runner.py --scheduled {app_id} '
    f'/data/apps/{slug}/fetch.sh"\n'
  )


def test_provenance_migration_classifies_declarations_made_before_it(tmp_path):
  """Before provenance, updates always reset defaults to server time. So a
  zone-owned declaration, or a server-time one off the default, was set
  through the schedule route; one equal to the default is the manifest's."""
  import json

  from sqlalchemy import create_engine

  from app import models
  from app.config import get_settings
  import app.schema_migrations as migrations

  data_dir = Path(get_settings().data_dir)
  revision = "a" * 40
  eng = create_engine(f"sqlite:///{tmp_path / 'provenance.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    for app_id in (9111, 9112, 9113, 9114):
      db.add(models.App(
        id=app_id, name=f"a{app_id}", slug=f"a{app_id}",
        source_dir=str(data_dir / "apps" / f"a{app_id}"),
        runtime_revision=revision,
      ))
      runtime = data_dir / "app-runtime" / str(app_id) / revision
      runtime.mkdir(parents=True, exist_ok=True)
      (runtime / "mobius.json").write_text(json.dumps({
        "schedule": {"default": "0 6 * * *", "job": "fetch.sh"},
      }))
    db.commit()
  _write_zone_declaration(9111, "a9111", "Asia/Tokyo", "30 5 * * 1-5")
  _write_server_declaration(9112, "a9112", "0 6 * * *")
  _write_server_declaration(9113, "a9113", "15 4 * * *")
  _write_server_declaration(9114, "a9114", "0 6 * * *")
  already = ScheduleChoice(
    source="owner", cron="0 6 * * *", job="fetch.sh",
    manifest_default="0 6 * * *",
  )
  app_cron.record_schedule_choice(9114, already)

  migrations._record_schedule_provenance(eng)
  migrations._record_schedule_provenance(eng)

  assert app_cron.read_schedule_choice(9111) == ScheduleChoice(
    source="owner", cron="30 5 * * 1-5", job="fetch.sh",
    timezone="Asia/Tokyo", manifest_default="0 6 * * *",
  )
  assert app_cron.read_schedule_choice(9112) == ScheduleChoice(
    source="manifest", cron="0 6 * * *", job="fetch.sh",
    manifest_default="0 6 * * *",
  )
  assert app_cron.read_schedule_choice(9113) == ScheduleChoice(
    source="owner", cron="15 4 * * *", job="fetch.sh",
    manifest_default="0 6 * * *",
  )
  assert app_cron.read_schedule_choice(9114) == already


def test_declaration_without_provenance_is_never_adopted_as_the_owners():
  """Registration follows the recorded choice, so an unrecorded zone
  declaration can only be an interrupted default: an update replaces it."""
  _write_zone_declaration(9115, "memory", "Asia/Tokyo", "30 5 * * *")

  assert app_cron.owner_schedule_to_keep(9115, "30 5 * * *", "fetch.sh") is None


def test_rollback_restores_the_prior_choice_when_registration_fails():
  _owner_daily(9116)
  prior = app_cron.read_schedule_choice(9116)

  try:
    with app_cron.schedule_choice_rollback(9116):
      app_cron.record_schedule_choice(9116, ScheduleChoice(
        source="owner", cron="0 3 * * *", job="fetch.sh",
      ))
      raise RuntimeError("registration failed")
  except RuntimeError:
    pass

  assert app_cron.read_schedule_choice(9116) == prior


def test_rollback_leaves_no_provenance_where_there_was_none():
  with app_cron.schedule_choice_rollback(9117):
    app_cron.record_schedule_choice(9117, ScheduleChoice(
      source="owner", cron="0 3 * * *", job="fetch.sh",
    ))
  assert app_cron.read_schedule_choice(9117) is not None

  try:
    with app_cron.schedule_choice_rollback(9118):
      app_cron.record_schedule_choice(9118, ScheduleChoice(
        source="owner", cron="0 3 * * *", job="fetch.sh",
      ))
      raise RuntimeError("registration failed")
  except RuntimeError:
    pass

  assert app_cron.read_schedule_choice(9118) is None


@pytest.fixture
def scheduled_app(db):
  """One installed app whose accepted revision carries a job script."""
  source_dir = Path(get_settings().data_dir) / "apps" / "schedule-rollback"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  app = models.App(
    name="Schedule rollback", description="", slug="schedule-rollback",
    source_dir=str(source_dir), jsx_source="export default () => null",
    token_nonce="schedule-rollback-nonce",
  )
  db.add(app)
  db.flush()
  app_git.ensure_repo(source_dir)
  app_git.commit_local(source_dir, "Accept schedule fixture")
  app.source_commit = app_git.head_sha(source_dir, app_git.LOCAL_BRANCH)
  from app.applied_app_runtime import prepare_runtime, publish_runtime
  publish_runtime(app, prepare_runtime(source_dir, app.source_commit))
  db.commit()
  return app


def test_a_schedule_save_that_failed_is_never_applied_by_a_later_update(
  client, auth, scheduled_app, monkeypatch,
):
  """The owner was told the save failed, so no update may adopt that time."""
  def refuse(*_args, **_kwargs):
    raise app_cron.CronInfrastructureError(500, "Could not save schedule.")

  monkeypatch.setattr(app_cron, "register_cron", refuse)

  response = client.post(
    f"/api/apps/{scheduled_app.id}/schedule",
    json={"cron": "15 7 * * *", "job": "fetch.sh"}, headers=auth,
  )

  assert response.status_code == 500, response.text
  assert app_cron.read_schedule_choice(scheduled_app.id) is None
  assert app_cron.owner_schedule_to_keep(
    scheduled_app.id, "0 6 * * *", "fetch.sh",
  ) is None
