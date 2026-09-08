"""The Host installs a frozen image source using the same owner Apply path."""
import importlib.util
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app import platform_update, database

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend/scripts/install_platform_release.py"


def load_installer():
  spec = importlib.util.spec_from_file_location("install_platform_release", SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.mark.parametrize("state,code", [("activation_needed", 0), ("up_to_date", 0), ("conflict", 1), ("rolled_back", 1)])
def test_installer_applies_only_bundle_target_through_shared_owner(monkeypatch, state, code):
  target = "a" * 40
  calls = []
  monkeypatch.setattr("sys.argv", [str(SCRIPT), "--bundle", "/tmp/image.bundle", "--target", target])
  monkeypatch.setattr(platform_update, "_reconcile_flock", nullcontext)
  monkeypatch.setattr(platform_update, "_git", lambda *args, **kwargs: calls.append(("git", args)))
  def preview(repo, *, target_sha):
    calls.append(("preview", target_sha))
    return {"plan_id": "review", "current_sha": "b" * 40}
  monkeypatch.setattr(platform_update, "platform_update_preview", preview)
  async def apply(db, **plan):
    calls.append(("apply", plan))
    return {"state": state}
  monkeypatch.setattr(platform_update, "apply_platform_update", apply)
  monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(SimpleNamespace()))
  assert load_installer().main() == code
  assert calls[0] == ("git", ("fetch", "--no-tags", "/tmp/image.bundle", target))
  assert calls[1] == ("preview", target)
  assert calls[2][1]["target_sha"] == target
  assert calls[2][1]["current_sha"] == "b" * 40
  assert calls[2][1]["plan_id"] == "review"


def test_host_installs_preflighted_image_source_before_cutover_without_moving_fetch():
  source = (ROOT / "scripts/deploy-prod.sh").read_text()
  install = source.index('step "install the image\'s reviewed source')
  cutover = source.index('# ── step 2: recreate container')
  assert source.index('ok "preflight protected runtime: current"') < install < cutover
  assert 'git -C /app/platform-baked rev-parse HEAD' in source[:install]
  assert 'git -C "$REPO_ROOT" bundle create "$source_bundle" HEAD' in source[install:cutover]
  assert 'rev-parse HEAD)" != "$INSTALLED_SOURCE_SHA"' in source[install:cutover]
  assert 'rev-parse --is-shallow-repository)" != "false"' in source[install:cutover]
  assert '--target "$INSTALLED_SOURCE_SHA"' in source[install:cutover]
  assert 'install_platform_release.py' in source[install:cutover]
  verification = source[source.index('# Verify the frozen source release'):]
  assert 'merge-base --is-ancestor "$INSTALLED_SOURCE_SHA" HEAD' in verification
  assert 'git fetch' not in verification
  # Skip-build reuses installed source; rollback never invokes the installer.
  assert source.index('if [ "$BUILT_THIS_RUN" = "1" ] && [ -n "$IMAGE_TAG" ]; then') < install
  assert install < source.index('elif [ "$SKIP_BUILD" = "1" ]; then', install)
  assert 'install_platform_release.py' not in source[:source.index('# ── preflight:')]


def test_first_boot_never_substitutes_moving_network_source():
  source = (ROOT / "backend/scripts/entrypoint.sh").read_text()
  bootstrap = source[source.index('_platform_bootstrap() {'):source.index('_platform_seed_test_checkout() {')]
  assert 'git clone' not in '\n'.join(line for line in bootstrap.splitlines() if not line.lstrip().startswith('#'))
  assert 'git fetch' not in bootstrap
  assert 'no valid image source seed' in bootstrap
  assert 'cp -a /app/platform-baked/.' in bootstrap
