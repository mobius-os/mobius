"""Base boot creates chat continuity only; graph memory belongs to its app."""

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ENTRYPOINT = SCRIPTS / "entrypoint.sh"
INSTALL = SCRIPTS.parent / "app" / "install.py"
CORE = SCRIPTS.parents[1] / "skill" / "core.md"


def _load(name: str):
  spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


def test_core_quotes_mapi_targets_with_query_strings():
  core = CORE.read_text(encoding="utf-8")

  assert 'mapi "/api/chats/<id>?limit=500"' in core
  assert "mapi /api/chats/<id>?limit=500" not in core


def test_chat_summary_boot_does_not_create_graph_scaffolding(tmp_path, monkeypatch):
  module = _load("init_chat_summaries")
  memory_root = tmp_path / "shared" / "memory"
  monkeypatch.setattr(module, "CHATS", memory_root / "chats")
  monkeypatch.setattr(module.pwd, "getpwnam", lambda _name: (_ for _ in ()).throw(KeyError()))

  module.init()

  assert (memory_root / "chats").is_dir()
  assert sorted(path.name for path in memory_root.iterdir()) == ["chats"]
  assert not (memory_root / ".ready").exists()
  assert not (memory_root / "index.md").exists()


def test_base_skill_boot_never_seeds_app_owned_memory_skill(tmp_path, monkeypatch):
  module = _load("init_skills")
  seed = tmp_path / "seed"
  skills = tmp_path / "skills"
  seed.mkdir()
  (seed / "files.md").write_text("base owned", encoding="utf-8")
  monkeypatch.setattr(module, "_SEED_CANDIDATES", [seed])
  monkeypatch.setattr(module, "SKILLS", skills)
  monkeypatch.setattr(module, "_writable", lambda _path: None)

  module.init()

  assert (skills / "files.md").read_text(encoding="utf-8") == "base owned"
  assert not (skills / ".seed-version").exists()
  assert not (skills / "memory.md").exists()

  baked_seed = SCRIPTS / "seed-skills"
  assert not (baked_seed / "memory.md").exists()


def test_init_skills_absolute_entrypoint_imports_sibling_app(tmp_path):
  """Warm boot imports app.skills when invoked outside the backend cwd."""
  skills = tmp_path / "data" / "shared" / "skills"
  skills.mkdir(parents=True)
  (skills / "owner.md").write_text("owner skill", encoding="utf-8")
  env = os.environ.copy()
  env.pop("PYTHONPATH", None)
  env["DATA_DIR"] = str(tmp_path / "data")

  result = subprocess.run(
    [sys.executable, str(SCRIPTS / "init_skills.py")],
    cwd=tmp_path,
    env=env,
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  assert "init_skills: reconcile skipped" not in result.stdout
  assert (skills / "skills-index.md").is_file()


def test_later_boot_preserves_existing_memory_skill_but_does_not_reseed_it(
  tmp_path, monkeypatch,
):
  module = _load("init_skills")
  seed = tmp_path / "seed"
  skills = tmp_path / "skills"
  seed.mkdir()
  skills.mkdir()
  (seed / "memory.md").write_text("baked", encoding="utf-8")
  (skills / "memory.md").write_text("installed app copy", encoding="utf-8")
  monkeypatch.setattr(module, "_SEED_CANDIDATES", [seed])
  monkeypatch.setattr(module, "SKILLS", skills)
  monkeypatch.setattr(module, "_writable", lambda _path: None)

  module.init()

  assert (skills / "memory.md").read_text(encoding="utf-8") == "installed app copy"


def test_seeded_cron_jobs_use_only_app_scoped_credentials():
  text = (SCRIPTS / "seed-skills" / "cron.md").read_text(encoding="utf-8")

  assert 'Authorization: Bearer $APP_TOKEN' in text
  assert "Never read `/data/service-token.txt` from an app job" in text
  assert "SERVICE_TOKEN=$(cat /data/service-token.txt)" not in text
  assert "using bearer token $SERVICE_TOKEN" not in text


def test_cron_starts_only_after_per_boot_supervision_proof():
  text = ENTRYPOINT.read_text(encoding="utf-8")
  remove = text.index("rm -f /data/run/app-cron-supervision-ready")
  guard = text.index("if [ -f /data/run/app-cron-supervision-ready ]")
  start = text.index("        cron", guard)

  assert remove < guard < start
  assert "cron remains disabled (fail closed)" in text


def test_boot_never_executes_app_owned_cron_declarations():
  text = ENTRYPOINT.read_text(encoding="utf-8")
  assert "for init_script in /data/apps/*/init-cron.sh" not in text
  assert 'su -s /bin/sh mobius -c "bash $init_script"' not in text
  assert "Never execute app-owned init-cron.sh at boot" in text


def test_boot_preserves_the_optional_memory_apps_git_repository():
  entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
  data_repo_helper = (SCRIPTS / "init_data_repo.py").read_text(
    encoding="utf-8",
  )

  assert "init_data_repo.py write-ignore /data" in entrypoint
  assert "init_data_repo.py reconcile /data" in entrypoint
  assert "shared/memory/repository/" in data_repo_helper
  assert "Memory owns this optional repository directly" in data_repo_helper


def test_install_rollback_never_executes_app_owned_cron_declarations():
  text = INSTALL.read_text(encoding="utf-8")
  module = ast.parse(text)
  subprocess_runs = [
    node for node in ast.walk(module)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and isinstance(node.func.value, ast.Name)
    and node.func.value.id == "subprocess"
    and node.func.attr == "run"
  ]
  assert all(
    "init-cron.sh" not in (ast.get_source_segment(text, call) or "")
    for call in subprocess_runs
  )
  assert any(
    isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "append"
    and isinstance(node.func.value, ast.Attribute)
    and isinstance(node.func.value.value, ast.Name)
    and node.func.value.value.id == "journal"
    and node.func.value.attr == "rollback_actions"
    and len(node.args) == 1
    and isinstance(node.args[0], ast.Name)
    and node.args[0].id == "_reconcile_cron_after_install_rollback"
    for node in ast.walk(module)
  )
