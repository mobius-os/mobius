"""Base boot creates chat continuity only; graph memory belongs to its app."""

import importlib.util
import hashlib
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ENTRYPOINT = SCRIPTS / "entrypoint.sh"
INSTALL = SCRIPTS.parent / "app" / "install.py"


def _load(name: str):
  spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


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
  monkeypatch.setattr(module, "VERSION_FILE", skills / ".seed-version")
  monkeypatch.setattr(module, "_chown_mobius", lambda _path: None)

  module.init()

  assert (skills / "files.md").read_text(encoding="utf-8") == "base owned"
  assert not (skills / "memory.md").exists()

  baked_seed = SCRIPTS / "seed-skills"
  assert not (baked_seed / "memory.md").exists()


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
  monkeypatch.setattr(module, "VERSION_FILE", skills / ".seed-version")
  monkeypatch.setattr(module, "_chown_mobius", lambda _path: None)

  module.init()

  assert (skills / "memory.md").read_text(encoding="utf-8") == "installed app copy"


def test_later_boot_migrates_only_unmodified_graph_aware_base_skill(
  tmp_path, monkeypatch,
):
  module = _load("init_skills")
  seed = tmp_path / "seed"
  skills = tmp_path / "skills"
  seed.mkdir()
  skills.mkdir()
  old = "old unconditional graph instructions"
  (seed / "reflection.md").write_text("new optional-app gate", encoding="utf-8")
  live = skills / "reflection.md"
  live.write_text(old, encoding="utf-8")
  monkeypatch.setattr(module, "_SEED_CANDIDATES", [seed])
  monkeypatch.setattr(module, "SKILLS", skills)
  monkeypatch.setattr(module, "VERSION_FILE", skills / ".seed-version")
  monkeypatch.setattr(module, "_chown_mobius", lambda _path: None)
  monkeypatch.setattr(module, "_UNMODIFIED_MIGRATIONS", {
    "reflection.md": hashlib.sha256(old.encode()).hexdigest(),
  })

  module.init()
  assert live.read_text(encoding="utf-8") == "new optional-app gate"

  live.write_text("owner edit", encoding="utf-8")
  module.init()
  assert live.read_text(encoding="utf-8") == "owner edit"


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


def test_install_rollback_never_executes_app_owned_cron_declarations():
  text = INSTALL.read_text(encoding="utf-8")
  assert '["bash", str(Path(o) / "init-cron.sh")]' not in text
  assert "rollback_actions.append(_reconcile_cron_after_install_rollback)" in text
