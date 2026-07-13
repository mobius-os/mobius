"""Installed-app system-prompt composition and uninstall boundary."""

from datetime import datetime, UTC
import json
from unittest.mock import patch

from fastapi import HTTPException
import pytest

from app import models
from app import chat
from app.install import _validate_manifest
from app.system_prompts import compose_system_prompt
from tests.test_apps_install import (  # noqa: F401
  JSX,
  _bypass_cron_scaffold,
  _fake_async_client,
  _stub_resolver_run_chat,
  bypass_url_validation,
)


def _manifest(**over):
  value = {
    "id": "memory",
    "name": "Memory",
    "version": "2.0.0",
    "description": "System memory",
    "entry": "index.jsx",
    "source_files": ["memory-core.md"],
    "system_prompt": "memory-core.md",
  }
  value.update(over)
  return value


@pytest.mark.parametrize("value", ["../x.md", "dir/x.md", ".hidden.md", "x.txt", 7])
def test_system_prompt_manifest_requires_bare_markdown(value):
  with pytest.raises(HTTPException, match="system_prompt"):
    _validate_manifest(_manifest(system_prompt=value))


def test_system_prompt_must_be_a_declared_source_file():
  with pytest.raises(HTTPException, match="source_files"):
    _validate_manifest(_manifest(source_files=[]))


def test_no_fragments_returns_base_bytes_verbatim(db):
  base = "base prompt\n"
  assert compose_system_prompt(base, db) == base


def test_only_live_app_fragments_are_composed_in_stable_order(db, tmp_path):
  first = tmp_path / "one"
  second = tmp_path / "two"
  gone = tmp_path / "gone"
  for path, text in ((first, "FIRST"), (second, "SECOND"), (gone, "GONE")):
    path.mkdir()
    (path / "fragment.md").write_text(text, encoding="utf-8")
  db.add_all([
    models.App(
      id=20, name="two", slug="two", source_dir=str(second),
      system_prompt_file="fragment.md",
    ),
    models.App(
      id=10, name="one", slug="one", source_dir=str(first),
      system_prompt_file="fragment.md",
    ),
    models.App(
      id=5, name="gone", slug="gone", source_dir=str(gone),
      system_prompt_file="fragment.md", deleted_at=datetime.now(UTC),
    ),
  ])
  db.commit()

  prompt = compose_system_prompt("BASE\n", db)
  assert prompt.startswith("BASE\n")
  assert prompt.index("FIRST") < prompt.index("SECOND")
  assert "GONE" not in prompt


def test_lingering_fragment_is_inert_after_soft_uninstall(db, tmp_path):
  source = tmp_path / "memory"
  source.mkdir()
  (source / "memory-core.md").write_text("GRAPH INSTRUCTIONS", encoding="utf-8")
  app = models.App(
    name="Memory", slug="memory", source_dir=str(source),
    system_prompt_file="memory-core.md",
  )
  db.add(app)
  db.commit()
  assert "GRAPH INSTRUCTIONS" in compose_system_prompt("BASE", db)

  app.deleted_at = datetime.now(UTC)
  db.commit()
  assert compose_system_prompt("BASE", db) == "BASE"
  assert (source / "memory-core.md").is_file()


def test_install_persists_system_prompt_capability(
  client, auth, db, bypass_url_validation,
):
  base = "https://system-prompt.test/memory/"
  manifest = _manifest()
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "memory-core.md": (200, b"RETRIEVE ON DEMAND"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert response.status_code == 201, response.text
  app = db.query(models.App).filter(models.App.slug == "memory").one()
  assert app.system_prompt_file == "memory-core.md"
  assert response.json()["system_prompt_file"] == "memory-core.md"
  assert "RETRIEVE ON DEMAND" in compose_system_prompt("BASE", db)


def test_system_app_suffix_also_applies_to_custom_chat_prompts(monkeypatch):
  monkeypatch.setattr(
    chat, "_SYSTEM_APP_PROMPT_SUFFIX_CACHE",
    "\n\n<!-- installed system app: memory -->\nRECALL\n",
  )
  assert chat._with_system_app_prompts("CUSTOM") == (
    "CUSTOM\n\n<!-- installed system app: memory -->\nRECALL\n"
  )
