import subprocess
from pathlib import Path

from app import app_git, models
from app.config import get_settings
from scripts import repair_app_git_repos


def _seed_installed_app(db, source_dir: Path) -> models.App:
  app = models.App(
    name="News",
    description="Installed news app",
    jsx_source="export default function News() { return <h1>News</h1>; }\n",
    slug="news",
    source_dir=str(source_dir),
    manifest_url="https://example.test/news/mobius.json",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _write_source(source_dir: Path) -> None:
  source_dir.mkdir(parents=True)
  (source_dir / "index.jsx").write_text(
    "export default function News() { return <h1>News</h1>; }\n",
    encoding="utf-8",
  )
  (source_dir / "fetch.sh").write_text(
    "#!/usr/bin/env bash\nprintf 'ok\\n'\n",
    encoding="utf-8",
  )


def test_repair_app_git_repos_dry_run_does_not_create_repo(db):
  data_dir = Path(get_settings().data_dir)
  subprocess.run(["git", "-C", str(data_dir), "init", "-q"], check=True)
  source_dir = data_dir / "apps" / "news"
  _write_source(source_dir)
  _seed_installed_app(db, source_dir)

  rows = repair_app_git_repos.run(
    data_dir=data_dir,
    source_dirs={str(source_dir)},
    apply=False,
  )

  assert [(row.status, row.source_dir) for row in rows] == [
    ("would-repair", str(source_dir.resolve())),
  ]
  assert not (source_dir / ".git").exists()
  app = db.query(models.App).filter(models.App.slug == "news").one()
  assert app.upstream_commit is None
  assert app.upstream_jsx_sha is None


def test_repair_app_git_repos_apply_seeds_nested_repo_and_preserves_files(db):
  data_dir = Path(get_settings().data_dir)
  subprocess.run(["git", "-C", str(data_dir), "init", "-q"], check=True)
  source_dir = data_dir / "apps" / "news"
  _write_source(source_dir)
  _seed_installed_app(db, source_dir)
  index_before = (source_dir / "index.jsx").read_text(encoding="utf-8")
  job_before = (source_dir / "fetch.sh").read_text(encoding="utf-8")

  rows = repair_app_git_repos.run(
    data_dir=data_dir,
    source_dirs={str(source_dir)},
    apply=True,
  )

  assert rows[0].status == "repaired"
  assert app_git.is_repo(source_dir)
  assert (source_dir / "index.jsx").read_text(encoding="utf-8") == index_before
  assert (source_dir / "fetch.sh").read_text(encoding="utf-8") == job_before
  assert app_git._run(
    source_dir, "rev-parse", "--show-toplevel",
  ).stdout.strip() == str(source_dir.resolve())
  assert app_git._run(source_dir, "status", "--porcelain").stdout == ""

  db.expire_all()
  app = db.query(models.App).filter(models.App.slug == "news").one()
  assert app.upstream_commit == app_git.head_sha(
    source_dir, app_git.UPSTREAM_BRANCH,
  )
  assert app.upstream_jsx_sha


def test_repair_app_git_repos_reseeds_when_db_has_commit_but_repo_missing(db):
  data_dir = Path(get_settings().data_dir)
  subprocess.run(["git", "-C", str(data_dir), "init", "-q"], check=True)
  source_dir = data_dir / "apps" / "news"
  _write_source(source_dir)
  app = _seed_installed_app(db, source_dir)
  app.upstream_commit = "deadbeef"
  db.add(app)
  db.commit()

  dry_run = repair_app_git_repos.run(
    data_dir=data_dir,
    source_dirs={str(source_dir)},
    apply=False,
  )
  assert dry_run[0].status == "would-repair"
  assert "despite DB upstream_commit" in dry_run[0].detail

  repaired = repair_app_git_repos.run(
    data_dir=data_dir,
    source_dirs={str(source_dir)},
    apply=True,
  )

  assert repaired[0].status == "repaired"
  assert app_git.is_repo(source_dir)
  db.expire_all()
  app = db.query(models.App).filter(models.App.slug == "news").one()
  assert app.upstream_commit != "deadbeef"
  assert app.upstream_commit == app_git.head_sha(
    source_dir, app_git.UPSTREAM_BRANCH,
  )
