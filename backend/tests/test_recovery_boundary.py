from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_normal_agent_owns_full_root_without_a_recovery_co_process():
  compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  sudo_config = (ROOT / "backend/scripts/agent_sudo.sh").read_text(
    encoding="utf-8",
  )
  protected = (ROOT / "protected-files.txt").read_text(encoding="utf-8")

  assert "MOBIUS_AGENT_SUDO=${MOBIUS_AGENT_SUDO:-1}" in compose
  assert 'configure_agent_sudo "${MOBIUS_AGENT_SUDO:-1}"' in entrypoint
  assert "mobius ALL=(root) NOPASSWD: ALL" in sudo_config
  assert "/app/scripts/agent_sudo.sh" in protected
  assert "recovery" not in sudo_config.lower()


def test_platform_maintenance_and_restore_skills_are_seeded():
  seed = ROOT / "backend/scripts/seed-skills"
  assert (seed / "platform-maintenance.md").is_file()
  assert (seed / "undo-and-restore.md").is_file()


def test_boot_fallback_never_moves_or_prunes_owner_platform_source():
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  assert "_platform_import_probe" in entrypoint
  assert "require_all_routers_loaded" in entrypoint
  assert "_platform_use_baked" in entrypoint
  assert "serving baked floor" in entrypoint
  assert "platform.crashloop-prev" not in entrypoint
  assert "boot_attempt_counter" not in entrypoint
  assert "app.data_volume" not in entrypoint


def test_service_token_refresh_uses_selected_backend_and_retries_db_lookup():
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  refresh = entrypoint.split(
    "# Generate (or refresh) a service token", 1,
  )[1].split("# The python block above", 1)[0]

  assert 'cd "$_serve_workdir"' in refresh
  assert "timeout 15 python3" in refresh
  assert "for attempt in range(3)" in refresh
  assert "service token refresh skipped after database errors" in refresh
  assert "service token refresh command failed" in refresh
