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
    "reflection.md": {hashlib.sha256(old.encode()).hexdigest()},
  })

  module.init()
  assert live.read_text(encoding="utf-8") == "new optional-app gate"

  live.write_text("owner edit", encoding="utf-8")
  module.init()
  assert live.read_text(encoding="utf-8") == "owner edit"


def test_controlled_skills_have_fix_forward_migrations():
  module = _load("init_skills")

  assert module.SEED_VERSION == "18"
  assert module._UNMODIFIED_MIGRATIONS["images.md"] == {
    "248ea31e13d2d2d84a5acfca13526aa8ebfa3d90e9ee4bf55cfb72d47937f7d1",
  }
  assert module._UNMODIFIED_MIGRATIONS["building-apps.md"] == {
    "4126b40d209c422184e0135f611bb9f4197ea280fa27e63cd71c806f8b5ebd79",
    "91b655952d55b37fda0be82e3914c3b09e67ca7c5f5a575d315fb2ca75ef08f1",
    "563dcd7bfa1ff7cbad074d98462eb9755a010a15bf340c7f594fc7f6825a6a86",
    "a8591f03bd5fb6eb0cfcd811d6d6d4309657f2f4e9e8e11ded4cbefbd77facfd",
  }
  assert module._UNMODIFIED_MIGRATIONS["building-apps-quickstart.md"] == {
    "7d8af2664b37a69b88e48c2a28140c15556202c3c7ce30d77816c203d1959fcb",
    "4c2b080bcc91626f761c5823ea00d324667b9710f6757931823e22e9c8b5c2b1",
    "85a4b5ce5b47c81fa53bec90d530adfe433c0d2f7f31363427b6c792bd332e05",
  }
  assert module._UNMODIFIED_MIGRATIONS["resolving-app-git.md"] == {
    "6d462f1711891a182c26e212a1ec8fc922eeb02faee45e70ab9b2becfba24f5a",
  }
  assert module._UNMODIFIED_MIGRATIONS["app-component-shapes.md"] == {
    "0320609ff924a0954c20d5e5db91ed3681d421d76f6804b24552eb6e8fa5eb31",
    "91243377242700acb5093165af58c372bed0005f358d3a4b26774aeb2ef8a365",
  }
  assert module._UNMODIFIED_MIGRATIONS["visual-testing.md"] == {
    "9525b36b945c2a0b4cb02806081bb674f38e865b6e1c3961226112e1dbbc16ec",
    "a0648921b9c9ea2423e8abd52aa57e71e7bebfa1736073fcf3bfcaec3749ad19",
  }
  assert (
    "6e6e82e02287e8bb38195fb021ea25cee2dc4e27da1a6ce1e2a0143fb1d82d87"
    in module._UNMODIFIED_MIGRATIONS["recovery.md"]
  )


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
  text = ENTRYPOINT.read_text(encoding="utf-8")

  assert "shared/memory/repository/" in text
  assert "! -regex '/data/shared/memory/repository/\\.git'" in text
  assert "Memory's optional graph repo" in text


def test_install_rollback_never_executes_app_owned_cron_declarations():
  text = INSTALL.read_text(encoding="utf-8")
  assert '["bash", str(Path(o) / "init-cron.sh")]' not in text
  assert "rollback_actions.append(_reconcile_cron_after_install_rollback)" in text
