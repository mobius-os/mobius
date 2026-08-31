from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_mobius_has_no_recovery_runtime_or_boot_mode():
  assert not (ROOT / "backend/recovery_target").exists()
  assert not (ROOT / "scripts/mobiusctl").exists()
  assert not (ROOT / "scripts/external_recovery_release.py").exists()
  assert not (ROOT / "scripts/core_digest_release.py").exists()
  assert not (ROOT / "backend/app/release_channel.py").exists()
  assert not (ROOT / ".github/workflows/external-recovery-image.yml").exists()
  assert not (ROOT / ".github/workflows/core-digest-image.yml").exists()

  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  runtime = dockerfile + "\n" + entrypoint
  for retired in (
    "MOBIUS_BOOT_MODE",
    "MOBIUS_RECOVERY_CAPABILITY_PUBLIC_KEY",
    "MOBIUS_RECOVERY_TARGET_TOKEN",
    "MOBIUS_PLATFORM_RELEASE_REF",
    "managed_release",
    "targetd.py",
    "recovery-target",
  ):
    assert retired not in runtime

  deployment = (ROOT / "scripts/deploy-prod.sh").read_text(encoding="utf-8")
  test_sync = (ROOT / "scripts/sync-test-backend.sh").read_text(
    encoding="utf-8",
  )
  architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
  assert "recoveryd" not in deployment.lower()
  assert "recovery island" not in test_sync.lower()
  assert "recovery profile" not in architecture.lower()


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


def test_legacy_recovery_skill_is_not_seeded_or_referenced():
  seed = ROOT / "backend/scripts/seed-skills"
  assert not (seed / "recovery.md").exists()
  assert (seed / "platform-maintenance.md").is_file()
  assert (seed / "undo-and-restore.md").is_file()
  for path in seed.glob("*.md"):
    assert "`recovery.md`" not in path.read_text(encoding="utf-8")


def test_boot_fallback_never_moves_or_prunes_owner_platform_source():
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  assert "_platform_import_probe" in entrypoint
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
