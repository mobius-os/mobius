"""Owner timezone and schedule provenance across app updates."""

import json
import os
from dataclasses import asdict
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


def _write_zone_declaration(app_id, slug, zone, zone_cron, state=None):
  state = state or app_cron.schedule_state_dir(app_id)
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


def _write_server_declaration(app_id, slug, cron, state=None):
  state = state or app_cron.schedule_state_dir(app_id)
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

  def legacy(app_id):
    return data_dir / "apps" / str(app_id) / "schedule"

  _write_zone_declaration(
    9111, "a9111", "Asia/Tokyo", "30 5 * * 1-5", legacy(9111),
  )
  _write_server_declaration(9112, "a9112", "0 6 * * *", legacy(9112))
  _write_server_declaration(9113, "a9113", "15 4 * * *", legacy(9113))
  _write_server_declaration(9114, "a9114", "0 6 * * *", legacy(9114))
  already = ScheduleChoice(
    source="owner", cron="0 6 * * *", job="fetch.sh",
    manifest_default="0 6 * * *",
  )
  (legacy(9114) / "choice.json").write_text(json.dumps(asdict(already)))

  migrations._record_schedule_provenance(eng)
  migrations._record_schedule_provenance(eng)
  migrations._move_schedule_state_out_of_app_data(eng)

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

  with pytest.raises(RuntimeError, match="registration failed"):
    with app_cron.schedule_choice_rollback(9116):
      app_cron.record_schedule_choice(9116, ScheduleChoice(
        source="owner", cron="0 3 * * *", job="fetch.sh",
      ))
      raise RuntimeError("registration failed")

  assert app_cron.read_schedule_choice(9116) == prior


def test_rollback_leaves_no_provenance_where_there_was_none():
  with app_cron.schedule_choice_rollback(9117):
    app_cron.record_schedule_choice(9117, ScheduleChoice(
      source="owner", cron="0 3 * * *", job="fetch.sh",
    ))
  assert app_cron.read_schedule_choice(9117) is not None

  with pytest.raises(RuntimeError, match="registration failed"):
    with app_cron.schedule_choice_rollback(9118):
      app_cron.record_schedule_choice(9118, ScheduleChoice(
        source="owner", cron="0 3 * * *", job="fetch.sh",
      ))
      raise RuntimeError("registration failed")

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


def _fake_crontab(tmp_path: Path, monkeypatch) -> Path:
  """A crontab whose reads work and whose writes fail while ``fail`` exists."""
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  state = tmp_path / "crontab.txt"
  state.write_text("")
  fail = tmp_path / "crontab-fail"
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    f"state={state}\n"
    "if [ \"$1\" = \"-u\" ]; then shift 2; fi\n"
    "case \"$1\" in\n"
    "  -l) [ -f \"$state\" ] && cat \"$state\" || exit 1 ;;\n"
    f"  -) [ -e {fail} ] && exit 1; cat > \"$state\" ;;\n"
    "  *) exit 2 ;;\n"
    "esac\n",
  )
  crontab.chmod(0o755)
  monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
  monkeypatch.setenv("MOBIUS_ALLOW_TEST_CRON", "1")
  app_base = tmp_path / "apps"
  for slug in ("rollback-real", "rollback-fresh"):
    (app_base / slug).mkdir(parents=True)
  monkeypatch.setenv("MOBIUS_APP_BASE", str(app_base))
  monkeypatch.delenv("MOBIUS_TEST_RUNTIME", raising=False)
  return fail


def test_rollback_restores_the_durable_declaration_when_only_the_live_write_fails(
  tmp_path, monkeypatch,
):
  """The real scaffold writes init-cron.sh before the live crontab, so a
  failed live write leaves the new declaration unless the rollback undoes it."""
  fail = _fake_crontab(tmp_path, monkeypatch)
  app_id = 9119
  job = tmp_path / "fetch.sh"
  job.write_text("#!/bin/sh\n", encoding="utf-8")
  init_path = app_cron.schedule_state_dir(app_id) / "init-cron.sh"

  def save(cron: str) -> None:
    with app_cron.schedule_choice_rollback(app_id):
      app_cron.record_schedule_choice(app_id, ScheduleChoice(
        source="owner", cron=cron, job="fetch.sh",
      ))
      app_cron.register_cron("rollback-real", cron, job, app_id)

  save("0 6 * * *")
  kept_choice = app_cron.read_schedule_choice(app_id)
  kept_init = init_path.read_bytes()
  assert "0 6 * * *" in kept_init.decode()

  fail.touch()
  with pytest.raises(app_cron.CronInfrastructureError):
    save("7 15 * * *")

  assert app_cron.read_schedule_choice(app_id) == kept_choice
  assert init_path.read_bytes() == kept_init

  # First-ever save that fails leaves neither file behind.
  with pytest.raises(app_cron.CronInfrastructureError):
    with app_cron.schedule_choice_rollback(9120):
      app_cron.record_schedule_choice(9120, ScheduleChoice(
        source="owner", cron="7 15 * * *", job="fetch.sh",
      ))
      app_cron.register_cron("rollback-fresh", "7 15 * * *", job, 9120)
  assert app_cron.read_schedule_choice(9120) is None
  assert not (app_cron.schedule_state_dir(9120) / "init-cron.sh").exists()


def test_a_failing_rollback_never_replaces_the_original_error(monkeypatch):
  def broken(*_args, **_kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(app_cron, "_restore_schedule_file", broken)

  with pytest.raises(RuntimeError, match="registration failed"):
    with app_cron.schedule_choice_rollback(9121):
      app_cron.record_schedule_choice(9121, ScheduleChoice(
        source="owner", cron="0 3 * * *", job="fetch.sh",
      ))
      raise RuntimeError("registration failed")


def test_clearing_app_data_keeps_its_wall_clock_schedule(
  client, auth, db, scheduled_app, tmp_path, monkeypatch,
):
  """Clear data keeps cron, so a reconcile afterwards must not downgrade the
  every-minute wall-clock gate into a real every-minute job."""
  _fake_crontab(tmp_path, monkeypatch)
  monkeypatch.setenv(
    "MOBIUS_APP_BASE", str(Path(get_settings().data_dir) / "apps"),
  )
  saved = client.post(
    f"/api/apps/{scheduled_app.id}/schedule",
    json={"cron": "0 5 * * *", "job": "fetch.sh",
          "timezone": "Europe/Belgrade"},
    headers=auth,
  )
  assert saved.status_code == 200, saved.text
  choice = app_cron.read_schedule_choice(scheduled_app.id)
  storage = Path(get_settings().data_dir) / "apps" / str(scheduled_app.id)
  (storage / "schedule").mkdir(parents=True, exist_ok=True)
  (storage / "schedule" / "notes.txt").write_text("app data")

  wiped = client.delete(f"/api/apps/{scheduled_app.id}/data", headers=auth)
  assert wiped.status_code == 204, wiped.text
  assert not storage.exists()

  from app.routes.app_schedules import reconcile_app_cron_supervision
  _count, warnings, _ready = reconcile_app_cron_supervision(db)
  live = (tmp_path / "crontab.txt").read_text()
  assert warnings == []
  assert "--wall-clock Europe/Belgrade" in live, live
  assert app_cron.read_schedule_choice(scheduled_app.id) == choice


def test_an_app_cannot_rewrite_its_owner_schedule_from_its_data_folder(
  client, auth, db, scheduled_app, tmp_path, monkeypatch,
):
  """The schedule is outside everything the app can write: its storage token,
  and the data folder its Edit-role agents and jobs write on disk. Replaying
  the old attack (rewrite the declaration, then redeploy with an empty
  crontab) leaves the owner's time in place."""
  from app import auth as auth_mod
  from app.routes.app_schedules import reconcile_app_cron_supervision
  crontab = _fake_crontab(tmp_path, monkeypatch).with_name("crontab.txt")
  monkeypatch.setenv(
    "MOBIUS_APP_BASE", str(Path(get_settings().data_dir) / "apps"),
  )
  saved = client.post(
    f"/api/apps/{scheduled_app.id}/schedule",
    json={"cron": "0 5 * * *", "job": "fetch.sh",
          "timezone": "Europe/Belgrade"},
    headers=auth,
  )
  assert saved.status_code == 200, saved.text
  state = app_cron.schedule_state_dir(scheduled_app.id)
  declared = (state / "init-cron.sh").read_bytes()
  choice = app_cron.read_schedule_choice(scheduled_app.id)
  storage = Path(get_settings().data_dir) / "apps" / str(scheduled_app.id)
  assert not state.resolve().is_relative_to(storage.resolve())

  owner = db.query(models.Owner).one()
  app_auth = {"Authorization": "Bearer " + auth_mod.create_app_token(
    scheduled_app.id, owner.username, owner.token_epoch,
    scheduled_app.token_nonce,
  )}
  written = client.put(
    f"/api/storage/apps/{scheduled_app.id}/schedule/init-cron.sh",
    content=b"ENTRY='* * * * * fetch.sh'\n",
    headers={**app_auth, "Content-Type": "text/plain"},
  )
  assert written.status_code == 204, written.text
  (storage / "schedule" / "choice.json").write_text(
    '{"source": "owner", "cron": "* * * * *", "job": "fetch.sh"}',
  )

  crontab.write_text("")
  _count, warnings, _ready = reconcile_app_cron_supervision(db)

  assert warnings == []
  assert "--wall-clock Europe/Belgrade" in crontab.read_text()
  assert (state / "init-cron.sh").read_bytes() == declared
  assert app_cron.read_schedule_choice(scheduled_app.id) == choice


def test_purging_an_uninstalled_app_removes_its_schedule(
  client, auth, db, scheduled_app,
):
  """Uninstall keeps the schedule for recovery; the purge removes it before
  the app id can be reused, so a later app never inherits it."""
  from datetime import UTC, datetime, timedelta
  app_id = scheduled_app.id
  _owner_daily(app_id)
  state = app_cron.schedule_state_dir(app_id)

  removed = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert removed.status_code == 204, removed.text
  assert app_cron.read_schedule_choice(app_id) is not None

  scheduled_app.deleted_at = (
    datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  )
  db.commit()
  client.get("/api/apps/", headers=auth)

  db.expire_all()
  assert db.get(models.App, app_id) is None
  assert not state.exists()


def test_schedule_migration_moves_declarations_out_of_app_data(tmp_path):
  """The one-shot move takes only the two declaration files, lets a restored
  backup's copy replace one already moved, follows no symlink, and is safe
  to run again."""
  from sqlalchemy import create_engine

  import app.schema_migrations as migrations

  data_dir = Path(get_settings().data_dir)
  eng = create_engine(f"sqlite:///{tmp_path / 'move.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    for app_id in (9141, 9142, 9143):
      db.add(models.App(
        id=app_id, name=f"m{app_id}", slug=f"m{app_id}",
        source_dir=str(data_dir / "apps" / f"m{app_id}"),
      ))
    db.commit()

  def legacy(app_id):
    folder = data_dir / "apps" / str(app_id) / "schedule"
    folder.mkdir(parents=True, exist_ok=True)
    return folder

  _write_zone_declaration(
    9141, "m9141", "Europe/Belgrade", "0 5 * * *", legacy(9141),
  )
  declared = (legacy(9141) / "init-cron.sh").read_bytes()
  (legacy(9141) / "init-cron.sh").chmod(0o755)
  choice = json.dumps(asdict(ScheduleChoice(
    source="owner", cron="0 5 * * *", job="fetch.sh",
    timezone="Europe/Belgrade",
  ))).encode()
  (legacy(9141) / "choice.json").write_bytes(choice)
  (legacy(9141) / "notes.txt").write_text("app data")

  _write_server_declaration(9142, "m9142", "0 7 * * *")
  _write_server_declaration(9142, "m9142", "0 6 * * *", legacy(9142))
  restored = (legacy(9142) / "init-cron.sh").read_bytes()

  elsewhere = tmp_path / "elsewhere.sh"
  elsewhere.write_text("ENTRY='* * * * * x'\n")
  (legacy(9143) / "init-cron.sh").symlink_to(elsewhere)

  migrations._move_schedule_state_out_of_app_data(eng)
  migrations._move_schedule_state_out_of_app_data(eng)

  moved = app_cron.schedule_state_dir(9141)
  assert (moved / "init-cron.sh").read_bytes() == declared
  assert (moved / "init-cron.sh").stat().st_mode & 0o777 == 0o755
  assert (moved / "choice.json").read_bytes() == choice
  assert sorted(p.name for p in legacy(9141).iterdir()) == ["notes.txt"]

  assert (
    app_cron.schedule_state_dir(9142) / "init-cron.sh"
  ).read_bytes() == restored
  assert not (legacy(9142) / "init-cron.sh").exists()

  assert not app_cron.schedule_state_dir(9143).exists()
  assert (legacy(9143) / "init-cron.sh").is_symlink()


def test_wall_clock_gate_is_read_the_way_cron_runs_the_line():
  """Only the runner's own --wall-clock flag makes a gate; a comment or an
  unsupervised command naming it never does."""
  runner = "python3 /app/scripts/app-job-runner.py --scheduled"
  gate = (
    f"* * * * * {runner} --wall-clock Europe/Belgrade "
    "0\\ 5\\ \\*\\ \\*\\ \\* 7 /data/apps/memory/fetch.sh"
  )
  assert app_cron.is_wall_clock_gate(gate)
  assert not app_cron.is_wall_clock_gate(
    f"* * * * * {runner} 7 /data/apps/memory/fetch.sh # --wall-clock",
  )
  assert not app_cron.is_wall_clock_gate(
    "* * * * * /data/apps/memory/fetch.sh --wall-clock Europe/Belgrade",
  )
