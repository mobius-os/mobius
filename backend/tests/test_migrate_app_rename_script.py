"""migrate-app-rename.sh regressions."""

import os
import sqlite3
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate-app-rename.sh"


def _init_db(data_dir, rows=()):
  db_dir = data_dir / "db"
  db_dir.mkdir(parents=True)
  db = db_dir / "ultimate.db"
  con = sqlite3.connect(db)
  con.execute(
    "create table apps (id integer primary key, slug text, name text, source_dir text)"
  )
  con.executemany(
    "insert into apps (id, slug, name, source_dir) values (?, ?, ?, ?)",
    rows,
  )
  con.commit()
  con.close()
  return db


def _rows(data_dir):
  con = sqlite3.connect(data_dir / "db" / "ultimate.db")
  rows = {
    slug: {"id": app_id, "name": name, "source_dir": source_dir}
    for app_id, slug, name, source_dir in con.execute(
      "select id, slug, name, source_dir from apps order by id"
    )
  }
  con.close()
  return rows


def _run_migration(tmp_path, data_dir, crontab_text=""):
  state = tmp_path / "crontab.txt"
  state.write_text(crontab_text)

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir(exist_ok=True)
  crontab = fake_bin / "crontab"
  crontab.write_text(
    "#!/bin/sh\n"
    "state=\"$CRONTAB_STATE\"\n"
    "case \"${1:-}\" in\n"
    "  -l) [ -f \"$state\" ] && cat \"$state\" || exit 1 ;;\n"
    "  -) cat > \"$state\" ;;\n"
    "  *) echo \"bad crontab args: $*\" >&2; exit 2 ;;\n"
    "esac\n"
  )
  crontab.chmod(0o755)

  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "CRONTAB_STATE": str(state),
    "DATA_DIR": str(data_dir),
  }
  result = subprocess.run(
    ["bash", str(SCRIPT)],
    text=True,
    capture_output=True,
    env=env,
    check=False,
  )
  return result, state.read_text()


def test_old_only_state_migrates_in_place_and_preserves_app_ids(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(
    data_dir,
    [
      (11, "mind", "Mind", str(data_dir / "apps" / "mind")),
      (12, "dreaming", "Dreaming", str(data_dir / "apps" / "dreaming")),
    ],
  )
  for slug in ("mind", "dreaming"):
    app_dir = data_dir / "apps" / slug
    app_dir.mkdir(parents=True)
    (app_dir / "index.jsx").write_text(f"{slug} source")
  skills = data_dir / "shared" / "skills"
  skills.mkdir(parents=True)
  (skills / "mind.md").write_text("old memory skill")
  (skills / "dreaming.md").write_text("old reflection skill")
  logs = data_dir / "cron-logs"
  logs.mkdir(parents=True)
  (logs / "mind.heartbeat").write_text("mind beat")
  (logs / "dreaming.log").write_text("dream log")
  crontab = (
    "PATH=/usr/local/bin:/usr/bin:/bin\n"
    "# keep /data/apps/dreaming/fetch.sh in this comment unchanged\n"
    "0 6 * * * /data/apps/dreaming/fetch.sh 12\n"
  )

  result, live_crontab = _run_migration(tmp_path, data_dir, crontab)

  assert result.returncode == 0, result.stderr
  rows = _rows(data_dir)
  assert rows["memory"] == {
    "id": 11,
    "name": "Memory",
    "source_dir": str(data_dir / "apps" / "memory"),
  }
  assert rows["reflection"] == {
    "id": 12,
    "name": "Reflection",
    "source_dir": str(data_dir / "apps" / "reflection"),
  }
  assert not (data_dir / "apps" / "mind").exists()
  assert not (data_dir / "apps" / "dreaming").exists()
  assert (data_dir / "apps" / "memory" / "index.jsx").read_text() == "mind source"
  assert (data_dir / "apps" / "reflection" / "index.jsx").read_text() == "dreaming source"
  assert (skills / "memory.md").read_text() == "old memory skill"
  assert (skills / "reflection.md").read_text() == "old reflection skill"
  assert (logs / "memory.heartbeat").read_text() == "mind beat"
  assert (logs / "reflection.log").read_text() == "dream log"
  assert "/data/apps/reflection/fetch.sh 12" in live_crontab
  assert "# keep /data/apps/dreaming/fetch.sh in this comment unchanged" in live_crontab


def test_new_only_state_is_a_clean_noop(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(
    data_dir,
    [
      (21, "memory", "Memory", str(data_dir / "apps" / "memory")),
      (22, "reflection", "Reflection", str(data_dir / "apps" / "reflection")),
    ],
  )
  for slug in ("memory", "reflection"):
    app_dir = data_dir / "apps" / slug
    app_dir.mkdir(parents=True)
    (app_dir / "index.jsx").write_text(f"{slug} source")
  skills = data_dir / "shared" / "skills"
  skills.mkdir(parents=True)
  (skills / "memory.md").write_text("new memory skill")
  (skills / "reflection.md").write_text("new reflection skill")
  logs = data_dir / "cron-logs"
  logs.mkdir(parents=True)
  (logs / "reflection.log").write_text("new log")
  crontab = "0 6 * * * /data/apps/reflection/fetch.sh 22\n"

  result, live_crontab = _run_migration(tmp_path, data_dir, crontab)

  assert result.returncode == 0, result.stderr
  rows = _rows(data_dir)
  assert set(rows) == {"memory", "reflection"}
  assert (data_dir / "apps" / "memory" / "index.jsx").read_text() == "memory source"
  assert (data_dir / "apps" / "reflection" / "index.jsx").read_text() == "reflection source"
  assert (skills / "memory.md").read_text() == "new memory skill"
  assert (skills / "reflection.md").read_text() == "new reflection skill"
  assert (logs / "reflection.log").read_text() == "new log"
  assert live_crontab == crontab


def test_pre_schema_database_skips_db_step_without_traceback(tmp_path):
  data_dir = tmp_path / "data"
  db_dir = data_dir / "db"
  db_dir.mkdir(parents=True)
  sqlite3.connect(db_dir / "ultimate.db").close()
  old_dir = data_dir / "apps" / "mind"
  old_dir.mkdir(parents=True)
  (old_dir / "index.jsx").write_text("legacy source")

  result, _ = _run_migration(tmp_path, data_dir)

  assert result.returncode == 0
  assert "Traceback" not in result.stdout
  assert "no such table" not in result.stdout
  assert not old_dir.exists()
  assert (data_dir / "apps" / "memory" / "index.jsx").read_text() == "legacy source"


def test_half_migrated_source_dir_is_repaired(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(
    data_dir,
    [(31, "reflection", "Reflection", str(data_dir / "apps" / "dreaming"))],
  )
  old_dir = data_dir / "apps" / "dreaming"
  old_dir.mkdir(parents=True)
  (old_dir / "prompt.md").write_text("prompt")

  result, _ = _run_migration(tmp_path, data_dir)

  assert result.returncode == 0, result.stderr
  assert _rows(data_dir)["reflection"]["source_dir"] == str(data_dir / "apps" / "reflection")
  assert not old_dir.exists()
  assert (data_dir / "apps" / "reflection" / "prompt.md").read_text() == "prompt"


def test_both_old_and_new_rows_are_preserved_with_warning(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(
    data_dir,
    [
      (41, "mind", "Mind", str(data_dir / "apps" / "mind")),
      (42, "memory", "Memory", str(data_dir / "apps" / "memory")),
    ],
  )
  for slug, text in (("mind", "old"), ("memory", "new")):
    app_dir = data_dir / "apps" / slug
    app_dir.mkdir(parents=True)
    (app_dir / "index.jsx").write_text(text)

  result, _ = _run_migration(tmp_path, data_dir)

  assert result.returncode == 0
  rows = _rows(data_dir)
  assert rows["mind"]["id"] == 41
  assert rows["memory"]["id"] == 42
  assert (data_dir / "apps" / "mind" / "index.jsx").read_text() == "old"
  assert (data_dir / "apps" / "memory" / "index.jsx").read_text() == "new"
  assert "WARN db conflict for mind -> memory" in result.stdout
  assert "WARN source dir conflict for mind -> memory" in result.stderr


def test_cron_logs_never_overwrite_existing_new_logs(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(data_dir)
  logs = data_dir / "cron-logs"
  logs.mkdir(parents=True)
  (logs / "dreaming.log").write_text("old log")
  (logs / "reflection.log").write_text("new log")
  (logs / "dreaming.lock").write_text("old lock")
  (logs / "reflection.lock").write_text("new lock")
  (logs / "reflection.pre-rename.lock").write_text("prior old lock")

  result, _ = _run_migration(tmp_path, data_dir)

  assert result.returncode == 0, result.stderr
  assert (logs / "reflection.log").read_text() == "new log"
  assert (logs / "reflection.pre-rename.log").read_text() == "old log"
  assert (logs / "reflection.lock").read_text() == "new lock"
  assert (logs / "reflection.pre-rename.lock").read_text() == "prior old lock"
  assert (logs / "reflection.pre-rename.1.lock").read_text() == "old lock"
  assert not (logs / "dreaming.log").exists()
  assert not (logs / "dreaming.lock").exists()


def test_skill_conflict_archives_old_file_out_of_root_namespace(tmp_path):
  data_dir = tmp_path / "data"
  _init_db(data_dir)
  skills = data_dir / "shared" / "skills"
  archive_dir = skills / ".rename-conflicts"
  archive_dir.mkdir(parents=True)
  (skills / "mind.md").write_text("old skill")
  (skills / "memory.md").write_text("new skill")
  (archive_dir / "mind.pre-rename.md").write_text("prior archive")

  result, _ = _run_migration(tmp_path, data_dir)

  assert result.returncode == 0, result.stderr
  assert not (skills / "mind.md").exists()
  assert (skills / "memory.md").read_text() == "new skill"
  assert (archive_dir / "mind.pre-rename.md").read_text() == "prior archive"
  assert (archive_dir / "mind.pre-rename.1.md").read_text() == "old skill"
  assert "archived conflicting skill mind.md" in result.stderr
