"""The app validator uses the same compile contract as installation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.app_capabilities import contract_from_manifest
from app.install import _validate_manifest
from app.manifest_contract import ManifestContractError, validate_manifest_contract


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate-app.py"


def _write_app(tmp_path: Path, source: str) -> None:
  (tmp_path / "mobius.json").write_text(json.dumps({
    "id": "validator-test",
    "name": "Validator test",
    "version": "1",
    "description": "test",
    "entry": "index.jsx",
  }))
  (tmp_path / "index.jsx").write_text(source)


def _run(tmp_path: Path):
  return subprocess.run(
    [sys.executable, str(SCRIPT), str(tmp_path)],
    capture_output=True,
    text=True,
    check=False,
  )


def test_validator_accepts_an_installable_app(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  result = _run(tmp_path)
  assert result.returncode == 0, result.stderr
  assert "OK" in result.stdout


def test_manifest_accepts_one_path_prefix_credential_placement(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["permissions"] = {
    "credentialed_fetch": {
      "telegram": {
        "secret": "bot-token",
        "origin": "https://api.telegram.org",
        "paths": ["/bot/"],
        "path_prefix": "/bot",
      },
    },
  }
  validate_manifest_contract(manifest)
  _validate_manifest(manifest)
  assert contract_from_manifest(manifest)["data"]["credentialed_fetch"] == {
    "telegram": manifest["permissions"]["credentialed_fetch"]["telegram"]
  }


def test_manifest_rejects_two_credential_placements(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["permissions"] = {
    "credentialed_fetch": {
      "telegram": {
        "secret": "bot-token",
        "origin": "https://api.telegram.org",
        "paths": ["/bot/"],
        "path_prefix": "/bot",
        "query_parameter": "token",
      },
    },
  }
  with pytest.raises(ManifestContractError, match="exactly one"):
    validate_manifest_contract(manifest)


def test_validator_stays_zero_configuration_outside_a_runtime(tmp_path):
  """A developer checkout has no settings; the build lease must not need any."""
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  env = os.environ.copy()
  for key in ("DATA_DIR", "SECRET_KEY"):
    env.pop(key, None)

  result = subprocess.run(
    [sys.executable, str(SCRIPT), str(tmp_path)],
    capture_output=True,
    text=True,
    check=False,
    env=env,
  )

  assert result.returncode == 0, result.stderr
  assert "OK" in result.stdout


def test_validator_rejects_a_bundle_compile_failure(tmp_path):
  _write_app(
    tmp_path,
    "import './missing.js'; export default function App(){ return <div /> }",
  )
  result = _run(tmp_path)
  assert result.returncode == 1
  # The source-closure check catches this before Rolldown, which is still part of
  # the same preflight contract and avoids reporting a duplicate compile error.
  assert "resolves to no file" in result.stderr


def test_validator_rejects_a_compilable_module_without_default_export(tmp_path):
  _write_app(tmp_path, "export const value = 1")
  result = _run(tmp_path)
  assert result.returncode == 1
  assert "no default export" in result.stderr


def test_validator_rejects_manifest_type_holes_and_missing_package_files(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["offline_capable"] = "false"
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))
  result = _run(tmp_path)
  assert result.returncode == 1
  assert "offline_capable" in result.stderr

  manifest["offline_capable"] = False
  manifest["static_assets"] = ["static/missing.png"]
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))
  result = _run(tmp_path)
  assert result.returncode == 1
  assert "static asset source" in result.stderr


@pytest.mark.parametrize("update", [
  {"id": "Bad/Slug"},
  {"permissions": {"cross_app_access": "admin"}},
  {"permissions": {"shared_memory": "all"}},
  {"permissions": {"job_authority": "root"}},
  {"offline": {"writes": "eventually"}},
  {"schedule": {"default": "@daily"}},
  {"schedule": {"default": "0 0 * * * *"}},
  {"schedule": {"initialize_on_install": True}},
  {"schedule": {"job": "job.sh", "user_configurable": "yes"}},
  {"system_app": "yes"},
  {"system_prompt": "prompt.md"},
  {"entry": "src/index.jsx"},
  {"entry": "main.jsx"},
])
def test_shared_contract_and_installer_reject_the_same_manifest(update, tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest.update(update)
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(manifest)
  with pytest.raises(HTTPException) as exc:
    _validate_manifest(manifest)
  assert exc.value.status_code == 400


def test_system_prompt_requires_explicit_system_app_identity(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest.update({
    "source_files": ["prompt.md"],
    "system_prompt": "prompt.md",
  })
  (tmp_path / "prompt.md").write_text("System contribution")
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "system_app: true" in result.stderr
  with pytest.raises(HTTPException, match="system_app: true"):
    _validate_manifest(manifest)


@pytest.mark.parametrize(("removed_permission", "value"), [
  ("background_agent", True),
  ("job_authority", "scoped"),
])
def test_removed_job_authority_permissions_fail_clearly(
  tmp_path, removed_permission, value,
):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["permissions"] = {removed_permission: value}
  manifest["schedule"] = {"job": "job.sh"}
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "has been removed" in result.stderr
  with pytest.raises(HTTPException, match="has been removed"):
    _validate_manifest(manifest)


def test_requires_accepts_recognized_capabilities(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["requires"] = ["identity_manage", "railway_manage"]
  validate_manifest_contract(manifest)  # does not raise on a build that has them


def test_requires_unrecognized_capability_fails_loudly(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["requires"] = ["identity_manage", "does_not_exist"]
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "does not provide" in result.stderr
  with pytest.raises(ManifestContractError, match="Update Möbius"):
    validate_manifest_contract(manifest)
  with pytest.raises(HTTPException, match="does not provide"):
    _validate_manifest(manifest)


def test_requires_must_be_a_list_of_capability_names(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["requires"] = "identity_manage"
  with pytest.raises(ManifestContractError, match="must be a list"):
    validate_manifest_contract(manifest)


def test_validator_materializes_declared_static_asset_destinations(tmp_path):
  _write_app(
    tmp_path,
    "import logo from './static/logo.js';\n"
    "export default function App(){ return <img src={logo} /> }",
  )
  (tmp_path / "assets").mkdir()
  (tmp_path / "assets" / "logo.js").write_text("export default 'logo'")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["static_assets"] = {"logo.js": "assets/logo.js"}
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 0, result.stderr


def test_validator_does_not_double_prefix_static_destinations(tmp_path):
  _write_app(
    tmp_path,
    "import logo from './static/logo.js';\n"
    "export default function App(){ return <img src={logo} /> }",
  )
  (tmp_path / "assets").mkdir()
  (tmp_path / "assets" / "logo.js").write_text("export default 'logo'")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["static_assets"] = {"static/logo.js": "assets/logo.js"}
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "resolves to no file" in result.stderr


def test_validator_accepts_default_reexport(tmp_path):
  _write_app(
    tmp_path,
    "const App = () => null;\nexport { App as default };",
  )
  result = _run(tmp_path)
  assert result.returncode == 0, result.stderr


def test_validator_rejects_comment_that_only_mentions_default_export(tmp_path):
  _write_app(
    tmp_path,
    "// export default function Fake() {}\nexport const value = 1;",
  )
  result = _run(tmp_path)
  assert result.returncode == 1
  assert "no default export" in result.stderr


def test_validator_rejects_css_imports(tmp_path):
  _write_app(
    tmp_path,
    "import './theme.css';\nexport default function App(){ return <div /> }",
  )
  (tmp_path / "theme.css").write_text("body { color: red; }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["source_files"] = ["theme.css"]
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "CSS imports are not supported" in result.stderr


def test_validator_rejects_css_before_following_nested_imports(tmp_path):
  _write_app(
    tmp_path,
    "import './a.css';\nexport default function App(){ return <div /> }",
  )
  (tmp_path / "a.css").write_text("@import './b.css';")
  (tmp_path / "b.css").write_text("body { color: red; }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["source_files"] = ["a.css"]
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "CSS imports are not supported" in result.stderr
  assert "b.css" not in result.stderr


def test_validator_rejects_symlinked_package_files(tmp_path):
  outside = tmp_path.parent / f"{tmp_path.name}-outside.jsx"
  outside.write_text("export default function App(){ return null }")
  (tmp_path / "mobius.json").write_text(json.dumps({
    "id": "validator-test",
    "name": "Validator test",
    "version": "1",
    "description": "test",
    "entry": "index.jsx",
  }))
  (tmp_path / "index.jsx").symlink_to(outside)

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "symlink" in result.stderr


def test_validator_rejects_symlinked_manifest(tmp_path):
  _write_app(tmp_path, "export default function App(){ return null }")
  actual = tmp_path / "actual-manifest.json"
  (tmp_path / "mobius.json").replace(actual)
  (tmp_path / "mobius.json").symlink_to(actual.name)

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "manifest must not be a symlink" in result.stderr


def test_validator_rejects_oversized_local_icon(tmp_path):
  _write_app(tmp_path, "export default function App(){ return null }")
  icon = tmp_path / "icon.png"
  icon.write_bytes(b"x" * (12 * 1024 * 1024 + 1))
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["icon"] = "icon.png"
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 1
  assert "local apply rejects the revision" in result.stderr


def test_validator_rejects_missing_declared_local_icon(tmp_path):
  _write_app(tmp_path, "export default function App(){ return null }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["icon"] = "missing.png"
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)

  assert result.returncode == 1
  assert "local apply rejects the revision" in result.stderr


def test_validator_does_not_require_informational_offline_precache_files(tmp_path):
  _write_app(tmp_path, "export default function App(){ return <div /> }")
  manifest = json.loads((tmp_path / "mobius.json").read_text())
  manifest["offline"] = {"precache": ["future-cache-entry.js"]}
  (tmp_path / "mobius.json").write_text(json.dumps(manifest))

  result = _run(tmp_path)
  assert result.returncode == 0, result.stderr
