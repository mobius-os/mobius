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
