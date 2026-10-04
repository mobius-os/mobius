"""An app's declared Python lock becomes its own environment on /data."""

import base64
import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from app import app_git, app_python_env, app_services, applied_app_runtime, models
from app.config import get_settings
from app.manifest_contract import (
  ManifestContractError,
  python_job_arguments,
  validate_manifest_contract,
  validate_schedule_job,
)


@pytest.fixture(autouse=True)
def _fresh_key_cache():
  app_python_env.declared_key.cache_clear()
  yield
  app_python_env.declared_key.cache_clear()


def _data_dir() -> Path:
  return Path(get_settings().data_dir)


def _manifest(**extra) -> dict:
  return {
    "id": "deps-demo", "name": "Deps", "version": "0.1.0",
    "description": "Declares its Python.", "entry": "index.jsx",
    "permissions": {}, **extra,
  }




def _tiny_wheel(directory: Path, name: str = "tinypkg", version: str = "1.0") -> Path:
  """A pure-Python wheel with a console script, installable from a local dir."""
  directory.mkdir(parents=True, exist_ok=True)
  info = f"{name}-{version}.dist-info"
  files = {
    f"{name}/__init__.py": b"VALUE = 42\n\ndef main():\n  print('tiny', VALUE)\n",
    f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
    f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    f"{info}/entry_points.txt": f"[console_scripts]\n{name}-cli = {name}:main\n".encode(),
  }
  record = []
  for path, content in files.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    record.append(f"{path},sha256={digest},{len(content)}")
  record.append(f"{info}/RECORD,,")
  files[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
  wheel = directory / f"{name}-{version}-py3-none-any.whl"
  with zipfile.ZipFile(wheel, "w") as archive:
    for path, content in files.items():
      archive.writestr(path, content)
  return wheel


def _lock_for(wheel: Path, name: str = "tinypkg", version: str = "1.0") -> str:
  digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
  return f"{name}=={version} \\\n    --hash=sha256:{digest}\n"


def _offline_index(monkeypatch, wheels: Path) -> None:
  wheels.mkdir(parents=True, exist_ok=True)
  monkeypatch.setenv("PIP_NO_INDEX", "1")
  monkeypatch.setenv("PIP_FIND_LINKS", str(wheels))


SERVICE = b'''import json, subprocess, sys
import tinypkg

if __name__ == "__main__":
  json.load(sys.stdin)
  try:
    import fastapi  # the platform's own library
    borrowed = True
  except ImportError:
    borrowed = False
  child = subprocess.run(
    ["python3", "-c", "import sys, tinypkg; print(sys.prefix)"],
    capture_output=True, text=True,
  )
  print(json.dumps({"status": 200, "body": {
    "value": tinypkg.VALUE, "prefix": sys.prefix, "borrowed": borrowed,
    "subprocess_prefix": child.stdout.strip(), "subprocess_error": child.stderr[-500:],
  }}))
'''


def _runtime_tree(root: Path, *, lock: str | None, service: bytes | None = SERVICE,
                  job: bytes | None = None) -> Path:
  root.mkdir(parents=True)
  extra = {"source_files": []}
  if service is not None:
    (root / "service.py").write_bytes(service)
    extra["service"] = {"entry": "service.py"}
    extra["source_files"].append("service.py")
  if job is not None:
    (root / "job.py").write_bytes(job)
    extra["schedule"] = {"job": "job.py"}
  if lock is not None:
    (root / "requirements.lock").write_text(lock)
    extra["python"] = {"lock": "requirements.lock"}
    extra["source_files"].append("requirements.lock")
  (root / "mobius.json").write_text(json.dumps(_manifest(**extra)))
  return root


def _fake_env(env: Path) -> None:
  """What a venv looks like to the resolver, without building one."""
  (env / "bin").mkdir(parents=True)
  (env / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
  (env / "bin" / "python").symlink_to(sys.executable)


def _keyed(data_dir, app_id: int, lock: str) -> Path:
  return app_python_env.envs_parent(data_dir, app_id) / app_python_env.env_key(lock.encode())


def _fake_builds(monkeypatch) -> list[str]:
  """Replace the pip build with a fake venv; returns the built lock names."""
  builds = []

  def fake_build(build, runtime_root, manifest, lock, relative):
    builds.append(lock.read_text())
    _fake_env(build)

  monkeypatch.setattr(app_python_env, "_build", fake_build)
  return builds


# --- Manifest contract -------------------------------------------------------

def test_manifest_accepts_a_python_lock_listed_as_a_source_file():
  validate_manifest_contract(_manifest(
    python={"lock": "requirements.lock"}, source_files=["requirements.lock"],
  ))


@pytest.mark.parametrize(("python", "sources", "message"), [
  ({"lock": "requirements.lock"}, [], "listed in `source_files`"),
  ({"lock": "requirements.lock"}, None, "listed in `source_files`"),
  ({"lock": "../outside.lock"}, ["../outside.lock"], "'..'"),
  ({"lock": "/etc/requirements.lock"}, ["/etc/requirements.lock"], "relative path"),
  ({"lock": "requirements.lock", "index": "x"}, ["requirements.lock"], "only `lock`"),
  ("requirements.lock", ["requirements.lock"], "only `lock`"),
])
def test_manifest_rejects_a_python_lock_outside_the_reviewed_source(python, sources, message):
  extra = {"python": python}
  if sources is not None:
    extra["source_files"] = sources
  with pytest.raises(ManifestContractError, match=message):
    validate_manifest_contract(_manifest(**extra))


@pytest.mark.parametrize("shebang", [
  b"#!/usr/bin/env -i python3\n", b"#!/usr/bin/env PYTHONPATH=x python3\n",
  b"#!/opt/python3-wrapper\n",
])
def test_declaring_app_rejects_python_shebangs_it_could_not_redirect(shebang):
  declaring = _manifest(python={"lock": "r.lock"}, source_files=["r.lock"])
  with pytest.raises(ManifestContractError, match="unsupported form"):
    validate_schedule_job(declaring, shebang)
  # Without a declaration the platform runs the shebang as written.
  assert validate_schedule_job(_manifest(), shebang)


@pytest.mark.parametrize(("shebang", "arguments"), [
  (("/usr/bin/env", "python3"), ()),
  (("/usr/bin/env", "-S", "python3", "-u"), ("-u",)),
  (("/usr/local/bin/python3.12",), ()),
  (("/usr/bin/python", "-X", "utf8"), ("-X", "utf8")),
  (("/usr/bin/env", "bash"), None),
  (("/bin/sh",), None),
])
def test_python_job_grammar(shebang, arguments):
  assert python_job_arguments(shebang) == arguments


# --- Environment key and resolution ------------------------------------------

def test_env_key_changes_with_the_lock_and_with_the_interpreter(monkeypatch):
  first = app_python_env.env_key(b"a==1\n")
  assert first == app_python_env.env_key(b"a==1\n")
  assert first != app_python_env.env_key(b"a==2\n")
  assert first.startswith(sys.implementation.cache_tag)
  assert hashlib.sha256(b"a==1\n").hexdigest() in first
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  assert app_python_env.env_key(b"a==1\n") != first


def test_env_key_changes_with_the_python_patch_version_only(tmp_path, monkeypatch):
  """A new image that keeps the interpreter reuses the env in /data."""
  with monkeypatch.context() as patch:
    patch.setattr(app_python_env.platform, "python_version", lambda: "3.13.1")
    app_python_env.compatibility_tag.cache_clear()
    first = app_python_env.env_key(b"a==1\n")
    patch.setattr(app_python_env.platform, "python_version", lambda: "3.13.2")
    app_python_env.compatibility_tag.cache_clear()
    assert app_python_env.env_key(b"a==1\n") != first
    build_info = tmp_path / "build-info.json"
    build_info.write_bytes(b'{"sha":"another-image"}')
    patch.setenv("MOBIUS_BUILD_INFO_PATH", str(build_info))
    patch.setattr(app_python_env.platform, "python_version", lambda: "3.13.1")
    app_python_env.compatibility_tag.cache_clear()
    assert app_python_env.env_key(b"a==1\n") == first
  app_python_env.compatibility_tag.cache_clear()


def test_undeclared_app_keeps_the_platform_interpreter_and_environment(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock=None)
  assert app_python_env.resolve_env(tmp_path, 7, root) is None
  assert app_python_env.python_for(None) == sys.executable
  environment = {"PATH": "/usr/bin"}
  assert app_python_env.activated_environment(environment, None) is environment
  assert app_python_env.prepare_env(tmp_path, 7, root) is None
  # A revision accepted without any manifest is undeclared too.
  (root / "mobius.json").unlink()
  app_python_env.declared_key.cache_clear()
  assert app_python_env.resolve_env(tmp_path, 7, root) is None


@pytest.mark.parametrize("manifest", [b"{not json", b"[]", None])
def test_unreadable_accepted_manifest_fails_closed(tmp_path, manifest):
  root = _runtime_tree(tmp_path / "rev", lock=None)
  target = root / "mobius.json"
  target.unlink()
  if manifest is None:
    target.symlink_to(tmp_path / "elsewhere.json")
  else:
    target.write_bytes(manifest)
  with pytest.raises(app_python_env.PythonEnvUnavailable, match="cannot be read"):
    app_python_env.resolve_env(tmp_path, 7, root)
  with pytest.raises(HTTPException) as caught:
    app_services.service_python_env(models.App(id=7, slug="x"), root / "service.py")
  assert caught.value.status_code == 503


def test_malformed_accepted_python_declaration_fails_closed(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock=None)
  manifest = json.loads((root / "mobius.json").read_text())
  manifest["python"] = {"lock": "../escape.lock"}
  (root / "mobius.json").write_text(json.dumps(manifest))
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.resolve_env(tmp_path, 7, root)


def test_declared_app_never_falls_back_to_the_platform_interpreter(tmp_path, monkeypatch):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  with pytest.raises(app_python_env.PythonEnvUnavailable, match="Automatic restoration"):
    app_python_env.resolve_env(tmp_path, 7, root)

  env = _keyed(tmp_path, 7, "a==1\n")
  _fake_env(env)
  assert app_python_env.resolve_env(tmp_path, 7, root) == env

  # A replaced image with another interpreter selects another key: the old
  # env is not used and nothing substitutes the platform interpreter.
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  app_python_env.declared_key.cache_clear()
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.resolve_env(tmp_path, 7, root)


def test_env_whose_base_interpreter_vanished_is_unavailable(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  env = _keyed(tmp_path, 7, "a==1\n")
  (env / "bin").mkdir(parents=True)
  (env / "pyvenv.cfg").write_text("home = /gone\n")
  (env / "bin" / "python").symlink_to(tmp_path / "gone" / "python3")
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.resolve_env(tmp_path, 7, root)


def test_activated_environment_puts_the_env_first_on_path(tmp_path):
  env = tmp_path / "env"
  activated = app_python_env.activated_environment({"PATH": "/usr/bin", "A": "1"}, env)
  assert activated == {"PATH": f"{env}/bin:/usr/bin", "A": "1", "VIRTUAL_ENV": str(env)}


def test_job_command_uses_the_env_only_for_python_shebangs(tmp_path):
  env = tmp_path / "env"
  assert app_python_env.job_command_interpreter(("/usr/bin/env", "python3", "-u"), env) == (
    str(env / "bin" / "python"), "-u",
  )
  assert app_python_env.job_command_interpreter(("/bin/sh",), env) == ("/bin/sh",)
  assert app_python_env.job_command_interpreter(("/usr/bin/env", "python3"), None) == (
    "/usr/bin/env", "python3",
  )


def _load_runner():
  path = Path(__file__).resolve().parent.parent / "scripts" / "app-job-runner.py"
  spec = importlib.util.spec_from_file_location("app_job_runner_env", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_job_runner_runs_every_job_of_a_declaring_app_with_its_env(tmp_path, monkeypatch):
  runner = _load_runner()
  monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
  root = _runtime_tree(
    tmp_path / "rev", lock="a==1\n", job=b"#!/usr/bin/env python3\nprint(1)\n",
  )
  (root / "job.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
  env = _keyed(tmp_path, 7, "a==1\n")
  _fake_env(env)
  assert runner._job_command(root / "job.py", 7, env) == [
    str(env / "bin" / "python"), str(root / "job.py"), "7",
  ]
  assert runner._job_command(root / "job.sh", 7, env) == [
    "/usr/bin/env", "bash", str(root / "job.sh"), "7",
  ]
  (root / "job.py").write_bytes(b"#!/usr/bin/env -i python3\n")
  with pytest.raises(ManifestContractError):
    runner._job_command(root / "job.py", 7, env)


def test_job_runner_fails_a_declaring_apps_job_without_its_env(tmp_path, monkeypatch):
  runner = _load_runner()
  monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
  monkeypatch.setattr(runner, "SUPERVISOR_LOG", tmp_path / "app-jobs.log")
  root = _runtime_tree(tmp_path / "app-runtime" / "7" / ("c" * 64), lock="a==1\n")
  (root / "job.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
  launched = []
  monkeypatch.setattr(runner, "_mint_app_token", lambda app_id: "token")
  monkeypatch.setattr(runner, "_app_is_live", lambda app_id, token: True)
  monkeypatch.setattr(runner, "_job_context", lambda app_id, token: {})
  monkeypatch.setattr(runner, "_runtime_job", lambda app_id, locator, context: root / "job.sh")
  monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: launched.append(a))
  monkeypatch.setattr(runner.os, "setsid", lambda: None)
  code = runner._execute_job(
    7, tmp_path / "apps" / "x" / "job.sh", wait_for_ready=False, scheduled=False,
    run_lock_fd=0,
  )
  assert code == 4 and launched == []
  assert "Automatic restoration" in (tmp_path / "app-jobs.log").read_text()


# --- Services and preload hosts ----------------------------------------------

def test_service_without_its_env_fails_visibly_instead_of_borrowing(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  with pytest.raises(HTTPException) as caught:
    app_services.service_python_env(models.App(id=7, slug="deps-demo"), root / "service.py")
  assert caught.value.status_code == 503
  assert "Automatic restoration" in caught.value.detail


@pytest.mark.asyncio
async def test_spawned_request_and_preload_host_both_use_the_resolved_interpreter(
  monkeypatch, tmp_path,
):
  from app import service_preload
  launched = []

  async def record(program, *args, **kwargs):
    launched.append((program, kwargs["env"].get("PATH")))
    raise OSError("recorded")

  monkeypatch.setattr(app_services.asyncio, "create_subprocess_exec", record)
  environment = app_python_env.activated_environment({"PATH": "/usr/bin"}, Path("/envs/7"))
  with pytest.raises(HTTPException):
    await app_services._run_spawned("/envs/7/bin/python", tmp_path / "s.py", environment, b"{}", 1)
  with pytest.raises(OSError):
    await service_preload.start((7, "rev"), "demo", "/envs/7/bin/python", tmp_path / "s.py", environment)
  assert launched == [("/envs/7/bin/python", "/envs/7/bin:/usr/bin")] * 2


# --- Real builds ---------------------------------------------------------------

def _declaring_app(db, lock: str) -> tuple[models.App, Path]:
  source = _data_dir() / "apps" / "deps-demo"
  source.mkdir(parents=True)
  app = models.App(
    name="Deps", slug="deps-demo", description="", source_dir=str(source),
    jsx_source="export default () => null",
    capability_contract={"schema": 6, "service": {
      "entry": "service.py", "access": "self", "protocol": "json-v1",
    }},
  )
  db.add(app)
  db.commit()
  revision = "b" * 64
  root = _runtime_tree(applied_app_runtime.runtime_parent(app.id) / revision, lock=lock)
  return app, root


def test_real_build_serves_from_an_isolated_env_whose_scripts_survive_publication(
  client, auth, db, monkeypatch, tmp_path,
):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  app, root = _declaring_app(db, _lock_for(_tiny_wheel(wheels)))

  staged = app_python_env.prepare_env(_data_dir(), app.id, root)
  assert staged is not None and not staged.reused
  published = app_python_env.publish_env(_data_dir(), app.id, staged)
  env = app_python_env.envs_parent(_data_dir(), app.id) / staged.key
  assert published.link == env and env.resolve() == staged.root.resolve()
  reused = app_python_env.prepare_env(_data_dir(), app.id, root)
  assert reused.reused and reused.root == env

  # A console script pip generated keeps a valid interpreter after publication.
  import subprocess
  script = subprocess.run([str(env / "bin" / "tinypkg-cli")], capture_output=True, text=True)
  assert script.returncode == 0, script.stderr
  assert script.stdout.strip() == "tiny 42"

  app.runtime_revision = root.name
  db.commit()
  response = client.post(f"/api/apps/{app.id}/service/probe", headers=auth, json={})
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["value"] == 42
  assert Path(body["prefix"]) == env
  # Without system site-packages the app cannot silently borrow platform libraries,
  # and a `python3` it spawns is its own env's.
  assert body["borrowed"] is False
  assert Path(body["subprocess_prefix"]).resolve() == env.resolve(), body["subprocess_error"]


def test_build_ignores_hostile_pip_configuration(monkeypatch, tmp_path):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  lock = _lock_for(_tiny_wheel(wheels))
  config = tmp_path / "pip.conf"
  config.write_text(f"[install]\ntarget = {tmp_path / 'hijack-config'}\n")
  monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
  monkeypatch.setenv("PIP_TARGET", str(tmp_path / "hijack-env"))
  monkeypatch.setenv("PIP_REQUIRE_HASHES", "0")
  monkeypatch.setenv("PIP_USER", "1")
  root = _runtime_tree(tmp_path / "rev", lock=lock)
  staged = app_python_env.prepare_env(tmp_path, 7, root)
  assert (staged.root / "bin" / "tinypkg-cli").is_file()
  assert not (tmp_path / "hijack-config").exists()
  assert not (tmp_path / "hijack-env").exists()


def test_build_diagnostics_never_return_index_credentials():
  text = app_python_env._tail(
    "", "ERROR: Could not fetch https://owner:s3cret@index.example/simple/tinypkg/\n",
  )
  assert "s3cret" not in text and "https://****@index.example" in text


def test_build_failure_names_the_package_and_leaves_nothing_behind(monkeypatch, tmp_path):
  _offline_index(monkeypatch, tmp_path / "wheels")
  wheel = _tiny_wheel(tmp_path / "elsewhere", name="missingpkg")
  root = _runtime_tree(tmp_path / "rev", lock=_lock_for(wheel, name="missingpkg"))
  with pytest.raises(app_python_env.PythonEnvBuildError, match="missingpkg"):
    app_python_env.prepare_env(tmp_path, 7, root)
  assert list((tmp_path / "app-envs" / "builds").iterdir()) == []
  assert not app_python_env.envs_parent(tmp_path, 7).exists()


def test_build_rejects_a_lock_whose_hash_does_not_match(monkeypatch, tmp_path):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  _tiny_wheel(wheels)
  lock = "tinypkg==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
  root = _runtime_tree(tmp_path / "rev", lock=lock)
  with pytest.raises(app_python_env.PythonEnvBuildError, match="HASH|hash"):
    app_python_env.prepare_env(tmp_path, 7, root)


def test_build_smoke_runs_the_service_setup_and_names_a_missing_import(
  monkeypatch, tmp_path,
):
  wheels = tmp_path / "wheels"
  _offline_index(monkeypatch, wheels)
  lock = _lock_for(_tiny_wheel(wheels))
  root = _runtime_tree(tmp_path / "rev", lock=lock, service=b"import tinypkg\nimport notlocked\n")
  with pytest.raises(app_python_env.PythonEnvBuildError, match="notlocked"):
    app_python_env.prepare_env(tmp_path, 7, root)


@pytest.mark.parametrize("reused", [False, True], ids=["new-env", "reused-env"])
def test_candidate_smokes_both_service_and_python_job(monkeypatch, tmp_path, reused):
  lock = "a==1\n"
  root = _runtime_tree(
    tmp_path / "rev", lock=lock, service=b"import json\n",
    job=b"#!/usr/bin/env python3\nimport pathlib\n",
  )
  builds = _fake_builds(monkeypatch)
  if reused:
    _fake_env(_keyed(tmp_path, 7, lock))
  actual_run = app_python_env._run
  checked = []

  def observe(command, **kwargs):
    if kwargs["step"].startswith("Starting the app's"):
      checked.append((command[-2], Path(command[-1]).name))
    return actual_run(command, **kwargs)

  monkeypatch.setattr(app_python_env, "_run", observe)
  staged = app_python_env.prepare_env(tmp_path, 7, root)
  assert staged.reused is reused
  assert builds == ([] if reused else [lock])
  assert checked == [("service", "service.py"), ("job", "job.py")]
  app_python_env.discard_env(staged)


@pytest.mark.parametrize("entry", ["service", "job"])
@pytest.mark.parametrize("reused", [False, True], ids=["new-env", "reused-env"])
def test_candidate_rejects_changed_python_entry_even_with_same_lock(
  monkeypatch, tmp_path, entry, reused,
):
  lock = "a==1\n"
  root = _runtime_tree(
    tmp_path / "rev", lock=lock, service=b"import json\n",
    job=b"#!/usr/bin/env python3\nimport pathlib\n",
  )
  _fake_builds(monkeypatch)
  if reused:
    _fake_env(_keyed(tmp_path, 7, lock))
  missing = f"not_locked_for_{entry}"
  shebang = "#!/usr/bin/env python3\n" if entry == "job" else ""
  (root / f"{entry}.py").write_text(f"{shebang}import {missing}\n")

  with pytest.raises(app_python_env.PythonEnvBuildError, match=missing):
    app_python_env.prepare_env(tmp_path, 7, root)

  if reused:
    assert _keyed(tmp_path, 7, lock).exists()
  else:
    assert list((tmp_path / "app-envs" / "builds").iterdir()) == []


def test_build_step_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
  marker = tmp_path / "survivor"
  child = f"import time; time.sleep(2); open({str(marker)!r}, 'w')"
  program = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
    "time.sleep(30)\n"
  )
  with pytest.raises(app_python_env.PythonEnvBuildError, match="exceeded 1 seconds"):
    app_python_env._run([sys.executable, "-c", program], step="Smoke", timeout=1, env=dict(os.environ))
  import time
  time.sleep(2.5)
  assert not marker.exists()


def test_missing_declared_lock_fails_the_build_clearly(tmp_path):
  root = _runtime_tree(tmp_path / "rev", lock="a==1\n")
  (root / "requirements.lock").unlink()
  with pytest.raises(app_python_env.PythonEnvBuildError, match="missing from the app source"):
    app_python_env.prepare_env(tmp_path, 7, root)


def test_boot_discards_unlinked_builds_only(tmp_path):
  builds = tmp_path / "app-envs" / "builds"
  kept, orphan = builds / "kept", builds / "orphan"
  _fake_env(kept)
  _fake_env(orphan)
  parent = app_python_env.envs_parent(tmp_path, 7)
  parent.mkdir(parents=True)
  (parent / "key").symlink_to(os.path.relpath(kept, parent))
  app_python_env.discard_interrupted_builds(tmp_path)
  assert kept.is_dir() and not orphan.exists()
  assert [path.name for path in parent.iterdir()] == ["key"]


def test_build_step_wait_is_bounded_when_an_escaped_child_holds_the_pipes(tmp_path):
  import signal
  import time
  pid_file = tmp_path / "escaped.pid"
  child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
  program = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
    "time.sleep(60)\n"
  )
  started = time.monotonic()
  try:
    with pytest.raises(app_python_env.PythonEnvBuildError, match="exceeded 1 seconds"):
      app_python_env._run([sys.executable, "-c", program], step="Smoke", timeout=1, env=dict(os.environ))
    assert time.monotonic() - started < 1 + app_python_env._REAP_SECONDS + 5
  finally:
    for _ in range(50):
      if pid_file.exists() and pid_file.read_text():
        break
      time.sleep(0.1)
    if pid_file.exists() and pid_file.read_text():
      try:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
      except ProcessLookupError:
        pass


def _staged(tmp_path, name: str, key: str = "k") -> app_python_env.StagedEnv:
  build = tmp_path / "app-envs" / "builds" / name
  _fake_env(build)
  return app_python_env.StagedEnv(key=key, root=build, reused=False)


def test_competing_publishers_of_one_key_never_replace_the_winner(tmp_path):
  from concurrent.futures import ThreadPoolExecutor
  staged = [_staged(tmp_path, f"b{index}") for index in range(8)]
  with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(lambda item: app_python_env.publish_env(tmp_path, 7, item), staged))
  winners = [result for result in results if result is not None]
  assert len(winners) == 1
  link = app_python_env.envs_parent(tmp_path, 7) / "k"
  assert link.resolve() == winners[0].build.resolve()
  assert [path.name for path in (tmp_path / "app-envs" / "builds").iterdir()] == [winners[0].build.name]


def test_rollback_of_a_publication_that_lost_the_race_keeps_the_winner(tmp_path):
  winner = app_python_env.publish_env(tmp_path, 7, _staged(tmp_path, "winner"))
  late = _staged(tmp_path, "late")
  # Rolling back a publication whose link is not its own removes only its build.
  app_python_env.unpublish_env(app_python_env.PublishedEnv(link=winner.link, build=late.root))
  assert winner.link.resolve() == winner.build.resolve() and winner.build.is_dir()
  assert not late.root.exists()
  assert app_python_env.publish_env(tmp_path, 7, _staged(tmp_path, "loser")) is None
  app_python_env.unpublish_env(None)
  assert winner.link.resolve() == winner.build.resolve()
  app_python_env.unpublish_env(winner)
  assert not os.path.lexists(winner.link) and not winner.build.exists()


def test_publication_replaces_only_a_link_whose_build_is_gone(tmp_path):
  parent = app_python_env.envs_parent(tmp_path, 7)
  parent.mkdir(parents=True)
  (parent / "k").symlink_to("../builds/deleted")
  published = app_python_env.publish_env(tmp_path, 7, _staged(tmp_path, "fresh"))
  assert published is not None and (parent / "k").resolve() == published.build.resolve()


# --- Apply -----------------------------------------------------------------

def _source(lock: str | None, job: bytes | None = None, service: bytes | None = None) -> Path:
  root = _data_dir() / "apps" / "deps-demo"
  root.mkdir(parents=True, exist_ok=True)
  extra = {"source_files": []}
  if lock is not None:
    (root / "requirements.lock").write_text(lock)
    extra = {"python": {"lock": "requirements.lock"}, "source_files": ["requirements.lock"]}
  if job is not None:
    (root / "job.py").write_bytes(job)
    extra["schedule"] = {"job": "job.py"}
  if service is not None:
    (root / "service.py").write_bytes(service)
    extra["service"] = {"entry": "service.py"}
    extra["source_files"].append("service.py")
  (root / "mobius.json").write_text(json.dumps(_manifest(**extra)))
  (root / "index.jsx").write_text("export default function App() { return <div>x</div> }\n")
  return root


def _apply(client, auth, source):
  return client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)


def test_apply_build_failure_keeps_the_previous_revision_live(
  client, auth, db, monkeypatch, tmp_path,
):
  _offline_index(monkeypatch, tmp_path / "wheels")
  source = _source(lock=None)
  first = _apply(client, auth, source)
  assert first.status_code == 200, first.text
  app_id = first.json()["app"]["id"]
  previous = db.get(models.App, app_id).runtime_revision

  wheel = _tiny_wheel(tmp_path / "elsewhere", name="missingpkg")
  _source(lock=_lock_for(wheel, name="missingpkg"))
  failed = _apply(client, auth, source)

  assert failed.status_code == 422, failed.text
  detail = failed.json()["detail"]
  assert detail["code"] == "python_env_failed"
  assert "missingpkg" in detail["message"]
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == previous
  assert not app_python_env.envs_parent(_data_dir(), app_id).exists()


def test_apply_rejects_changed_service_with_reused_lock_and_keeps_previous_revision(
  client, auth, db, monkeypatch,
):
  builds = _fake_builds(monkeypatch)
  source = _source(lock="a==1\n", service=b"import json\n")
  first = _apply(client, auth, source)
  assert first.status_code == 200, first.text
  app_id = first.json()["app"]["id"]
  previous = db.get(models.App, app_id).runtime_revision

  (source / "service.py").write_bytes(b"import missing_from_same_lock\n")
  failed = _apply(client, auth, source)
  assert failed.status_code == 422, failed.text
  assert failed.json()["detail"]["code"] == "python_env_failed"
  assert "missing_from_same_lock" in failed.json()["detail"]["message"]
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == previous
  assert builds == ["a==1\n"]
  assert _keyed(_data_dir(), app_id, "a==1\n").exists()


def test_apply_rejects_an_unredirectable_python_job_in_a_declaring_app(
  client, auth, monkeypatch,
):
  builds = _fake_builds(monkeypatch)
  source = _source(lock="a==1\n", job=b"#!/usr/bin/env -i python3\nprint(1)\n")
  response = _apply(client, auth, source)
  assert response.status_code == 422, response.text
  assert response.json()["detail"]["code"] == "invalid_schedule_job"
  assert builds == []


def test_apply_publishes_the_env_before_the_runtime_and_rebuilds_after_image_change(
  client, auth, db, monkeypatch,
):
  builds = _fake_builds(monkeypatch)
  source = _source(lock="a==1\n")
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  row = db.get(models.App, created.json()["app"]["id"])
  root = applied_app_runtime.runtime_root(row)
  first_env = _keyed(_data_dir(), row.id, "a==1\n")
  assert app_python_env.resolve_env(_data_dir(), row.id, root) == first_env
  assert builds == ["a==1\n"]

  again = _apply(client, auth, source)
  assert again.status_code == 200, again.text
  assert builds == ["a==1\n"]  # a matching env is reused

  # A new image's interpreter: the env is unavailable until restoration rebuilds it.
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "cpython-399-other")
  app_python_env.declared_key.cache_clear()
  with pytest.raises(app_python_env.PythonEnvUnavailable):
    app_python_env.resolve_env(_data_dir(), row.id, root)
  rebuilt = _apply(client, auth, source)
  assert rebuilt.status_code == 200, rebuilt.text
  assert len(builds) == 2
  assert app_python_env.resolve_env(_data_dir(), row.id, root).name.startswith("cpython-399-other")


def test_rebuild_accepted_env_uses_frozen_lock_after_image_change(
  client, auth, db, monkeypatch,
):
  builds = _fake_builds(monkeypatch)
  source = _source(lock="accepted==1\n")
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  row = db.get(models.App, created.json()["app"]["id"])
  accepted_root = applied_app_runtime.runtime_root(row)
  assert builds == ["accepted==1\n"]

  # The editable tree is not accepted input, even if it declares another lock.
  (source / "requirements.lock").write_text("dirty==2\n")
  monkeypatch.setattr(app_python_env, "compatibility_tag", lambda: "new-image")
  app_python_env.declared_key.cache_clear()
  rebuilt = app_python_env.rebuild_accepted_env(_data_dir(), row.id, applied_app_runtime.runtime_root(row))
  assert rebuilt == app_python_env.resolve_env(_data_dir(), row.id, accepted_root)
  assert rebuilt.name.startswith("new-image-")
  assert builds == ["accepted==1\n", "accepted==1\n"]
  assert app_python_env.rebuild_accepted_env(_data_dir(), row.id, applied_app_runtime.runtime_root(row)) == rebuilt
  assert len(builds) == 2


def test_rebuild_accepted_env_does_not_rerun_code_for_a_usable_env(
  client, auth, db, monkeypatch,
):
  _fake_builds(monkeypatch)
  source = _source(lock="accepted==1\n", service=b"import json\n")
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  row = db.get(models.App, created.json()["app"]["id"])
  root = applied_app_runtime.runtime_root(row)
  existing = app_python_env.resolve_env(_data_dir(), row.id, root)

  def no_smoke(*_args, **_kwargs):
    raise AssertionError("restoring a usable env must not run app code")

  monkeypatch.setattr(app_python_env, "_smoke", no_smoke)
  assert app_python_env.rebuild_accepted_env(_data_dir(), row.id, root) == existing


def test_apply_failure_after_env_publication_removes_the_new_env(
  client, auth, db, monkeypatch,
):
  from app import app_apply
  _fake_builds(monkeypatch)

  def fail(*args, **kwargs):
    raise OSError("bundle publication failed")

  monkeypatch.setattr(app_apply, "publish_staged_bundle", fail)
  source = _source(lock="a==1\n")
  with pytest.raises(OSError):
    _apply(client, auth, source)
  assert db.query(models.App).count() == 0
  envs = _data_dir() / "app-envs"
  assert list((envs / "builds").iterdir()) == []
  assert all(not any(parent.iterdir()) for parent in envs.iterdir() if parent.name.isdigit())


# --- Store install and update ---------------------------------------------------

@pytest.fixture
def store(monkeypatch):
  from tests.test_apps_install import _fake_async_client
  monkeypatch.setattr(
    "app.install._validate_url_safe",
    lambda url: (url, urlparse(url).netloc, urlparse(url).hostname),
  )
  monkeypatch.setattr("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh"))
  base = "https://deps.test/repo/"

  def install(client, auth, *, lock: str, version: str,
              service: bytes = b"import json, sys\n",
              files: dict[str, bytes] | None = None):
    files = files or {}
    manifest = _manifest(
      version=version, python={"lock": "requirements.lock"},
      service={"entry": "service.py"},
      source_files=["service.py", "requirements.lock", *files],
    )
    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, b"export default function App() { return <div>x</div> }"),
      base + "service.py": (200, service),
      base + "requirements.lock": (200, lock.encode()),
      **{base + name: (200, content) for name, content in files.items()},
    }
    with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
      return client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })

  return install


def test_store_install_builds_before_the_row_and_publishes_with_the_runtime(
  client, auth, db, monkeypatch, store,
):
  from app.database import SessionLocal
  rows_during_build = []
  builds = _fake_builds(monkeypatch)
  fake = app_python_env._build

  def observe(build, runtime_root, manifest, lock, relative):
    with SessionLocal() as other:
      rows_during_build.append(other.query(models.App).count())
    fake(build, runtime_root, manifest, lock, relative)

  monkeypatch.setattr(app_python_env, "_build", observe)
  response = store(client, auth, lock="a==1\n", version="1.0.0")
  assert response.status_code == 201, response.text
  row = db.get(models.App, response.json()["id"])
  root = applied_app_runtime.runtime_root(row)
  assert app_python_env.resolve_env(_data_dir(), row.id, root) == _keyed(_data_dir(), row.id, "a==1\n")
  assert builds == ["a==1\n"] and rows_during_build == [0]


def test_store_update_rebuilds_for_a_new_lock_and_a_failed_build_keeps_the_old_one(
  client, auth, db, monkeypatch, store,
):
  builds = _fake_builds(monkeypatch)
  first = store(client, auth, lock="a==1\n", version="1.0.0")
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]

  updated = store(client, auth, lock="a==2\n", version="1.1.0")
  assert updated.status_code == 201, updated.text
  db.expire_all()
  row = db.get(models.App, app_id)
  assert app_python_env.resolve_env(
    _data_dir(), app_id, applied_app_runtime.runtime_root(row),
  ) == _keyed(_data_dir(), app_id, "a==2\n")
  assert builds == ["a==1\n", "a==2\n"]
  live = row.runtime_revision

  def broken(build, runtime_root, manifest, lock, relative):
    raise app_python_env.PythonEnvBuildError("Installing failed:\nERROR: No matching distribution for brokenpkg")

  monkeypatch.setattr(app_python_env, "_build", broken)
  failed = store(client, auth, lock="brokenpkg==1\n", version="1.2.0")
  assert failed.status_code == 422, failed.text
  assert failed.json()["detail"]["code"] == "python_env_failed"
  assert "brokenpkg" in failed.json()["detail"]["message"]
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == live
  assert _keyed(_data_dir(), app_id, "a==2\n").exists()
  assert not _keyed(_data_dir(), app_id, "brokenpkg==1\n").exists()


def test_store_update_rejects_changed_service_with_reused_lock(
  client, auth, db, monkeypatch, store,
):
  builds = _fake_builds(monkeypatch)
  first = store(client, auth, lock="a==1\n", version="1.0.0")
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  previous = db.get(models.App, app_id).runtime_revision

  failed = store(
    client, auth, lock="a==1\n", version="1.1.0",
    service=b"import missing_from_store_update\n",
  )
  assert failed.status_code == 422, failed.text
  assert failed.json()["detail"]["code"] == "python_env_failed"
  assert "missing_from_store_update" in failed.json()["detail"]["message"]
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == previous
  assert builds == ["a==1\n"]
  assert _keyed(_data_dir(), app_id, "a==1\n").exists()


def test_store_update_whose_merged_lock_diverges_is_refused_and_leaves_no_build(
  client, auth, db, monkeypatch, store,
):
  builds = _fake_builds(monkeypatch)
  padding = "".join(f"# {index}\n" for index in range(8))
  first = store(client, auth, lock=f"a==1\n{padding}b==1\n", version="1.0.0")
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  source = Path(db.get(models.App, app_id).source_dir)
  (source / "requirements.lock").write_text(f"a==9\n{padding}b==1\n")
  applied = _apply(client, auth, source)
  assert applied.status_code == 200, applied.text
  db.expire_all()
  live = db.get(models.App, app_id).runtime_revision
  built = list(builds)

  diverged = store(client, auth, lock=f"a==1\n{padding}b==2\n", version="1.1.0")

  assert diverged.status_code == 409, diverged.text
  assert diverged.json()["detail"]["code"] == "python_lock_diverged"
  assert builds == built
  db.expire_all()
  assert db.get(models.App, app_id).runtime_revision == live
  linked = {
    link.resolve() for link in app_python_env.envs_parent(_data_dir(), app_id).iterdir()
  }
  assert {path.resolve() for path in (_data_dir() / "app-envs" / "builds").iterdir()} == linked


def _assert_no_unlinked_env_or_runtime(app_id: int) -> None:
  linked = {
    link.resolve() for link in app_python_env.envs_parent(_data_dir(), app_id).iterdir()
  }
  assert {path.resolve() for path in (_data_dir() / "app-envs" / "builds").iterdir()} <= linked
  assert not list((_data_dir() / "app-runtime").glob(".staged-*"))


def _locally_edited_store_app(client, auth, db, store) -> tuple[int, Path, str]:
  """A Store app whose applied local service uses a helper the package ships."""
  first = store(
    client, auth, lock="a==1\n", version="1.0.0", files={"lib.py": b"VALUE = 1\n"},
  )
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  source = Path(db.get(models.App, app_id).source_dir)
  (source / "service.py").write_bytes(b"import json, sys\nfrom lib import VALUE\n")
  applied = _apply(client, auth, source)
  assert applied.status_code == 200, applied.text
  db.expire_all()
  return app_id, source, db.get(models.App, app_id).runtime_revision


def test_store_update_smokes_the_merged_tree_and_publishes_exactly_it(
  client, auth, db, monkeypatch, store,
):
  builds = _fake_builds(monkeypatch)
  app_id, _, _ = _locally_edited_store_app(client, auth, db, store)
  actual_smoke = app_python_env._smoke
  smoked = []

  def observe(env, runtime_root, manifest, relative):
    smoked.append((
      (runtime_root / "service.py").read_bytes(),
      (runtime_root / "lib.py").read_bytes(),
      applied_app_runtime._prepared(runtime_root).revision,
    ))
    actual_smoke(env, runtime_root, manifest, relative)

  monkeypatch.setattr(app_python_env, "_smoke", observe)
  updated = store(
    client, auth, lock="a==1\n", version="1.1.0",
    files={"lib.py": b"VALUE = 2\n"},
  )

  assert updated.status_code == 201, updated.text
  db.expire_all()
  row = db.get(models.App, app_id)
  assert smoked == [(
    b"import json, sys\nfrom lib import VALUE\n", b"VALUE = 2\n", row.runtime_revision,
  )]
  assert builds == ["a==1\n"]
  _assert_no_unlinked_env_or_runtime(app_id)


def test_store_update_whose_merged_service_fails_keeps_the_old_revision_live(
  client, auth, db, monkeypatch, store,
):
  builds = _fake_builds(monkeypatch)
  app_id, source, live = _locally_edited_store_app(client, auth, db, store)

  # The package alone passes: its own service never imports VALUE. Only the
  # merged tree, which keeps the local service, cannot start.
  failed = store(
    client, auth, lock="a==1\n", version="1.1.0",
    files={"lib.py": b"RENAMED = 1\n"},
  )

  assert failed.status_code == 422, failed.text
  assert failed.json()["detail"]["code"] == "python_env_failed"
  assert "VALUE" in failed.json()["detail"]["message"]
  db.expire_all()
  row = db.get(models.App, app_id)
  assert row.runtime_revision == live
  root = applied_app_runtime.runtime_root(row)
  assert (root / "lib.py").read_bytes() == b"VALUE = 1\n"
  assert app_python_env.resolve_env(_data_dir(), app_id, root) == _keyed(_data_dir(), app_id, "a==1\n")
  assert (source / "lib.py").read_bytes() == b"VALUE = 1\n"
  assert app_git.head_sha(source, app_git.UPSTREAM_BRANCH) == row.upstream_commit
  assert builds == ["a==1\n"]
  _assert_no_unlinked_env_or_runtime(app_id)


def test_store_update_refuses_a_tree_that_changed_while_it_was_checked(
  client, auth, db, monkeypatch, store,
):
  from sqlalchemy import text
  from app import fs_locks
  from app.database import SessionLocal
  _fake_builds(monkeypatch)
  app_id, source, live = _locally_edited_store_app(client, auth, db, store)
  draft = b"import json, sys\nfrom lib import VALUE\nDRAFT = True\n"
  actual_smoke = app_python_env._smoke

  def edit_while_checking(env, runtime_root, manifest, relative):
    # The check holds neither the source lock nor a write transaction, so an
    # agent edit can land here.
    assert not fs_locks.source_dir_lock(str(source)).locked()
    with SessionLocal() as other:
      other.execute(text("UPDATE apps SET description = description WHERE id = :id"), {"id": app_id})
      other.commit()
    actual_smoke(env, runtime_root, manifest, relative)
    (source / "service.py").write_bytes(draft)

  monkeypatch.setattr(app_python_env, "_smoke", edit_while_checking)
  stale = store(
    client, auth, lock="a==1\n", version="1.1.0", files={"lib.py": b"VALUE = 2\n"},
  )

  assert stale.status_code == 409, stale.text
  assert stale.json()["detail"]["code"] == "python_check_stale"
  db.expire_all()
  row = db.get(models.App, app_id)
  assert row.runtime_revision == live
  assert (applied_app_runtime.runtime_root(row) / "lib.py").read_bytes() == b"VALUE = 1\n"
  assert (source / "service.py").read_bytes() == draft
  assert (source / "lib.py").read_bytes() == b"VALUE = 1\n"
  _assert_no_unlinked_env_or_runtime(app_id)


@pytest.mark.usefixtures("store")
def test_git_origin_install_smokes_modules_its_manifest_does_not_list(
  client, auth, db, monkeypatch, tmp_path,
):
  import subprocess
  from tests.test_apps_install import _fake_async_client, _fixture_commit
  _fake_builds(monkeypatch)
  base = "https://deps.test/origin/"
  manifest = _manifest(
    python={"lock": "requirements.lock"}, service={"entry": "service.py"},
    source_files=["service.py", "requirements.lock"],
  )
  files = {
    "mobius.json": json.dumps(manifest).encode(),
    "index.jsx": b"export default function App() { return <div>x</div> }",
    "service.py": b"import json, sys\nimport helper\n",
    "requirements.lock": b"a==1\n",
  }
  work = tmp_path / "origin-work"
  bare = tmp_path / "origin.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  for name, content in {**files, "helper.py": b"READY = True\n"}.items():
    (work / name).write_bytes(content)
  commit = _fixture_commit(work, "v1")
  subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)

  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), commit),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({base + name: (200, content) for name, content in files.items()}),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert response.status_code == 201, response.text
  row = db.get(models.App, response.json()["id"])
  root = applied_app_runtime.runtime_root(row)
  assert (root / "helper.py").read_bytes() == b"READY = True\n"
  assert app_python_env.resolve_env(_data_dir(), row.id, root) == _keyed(_data_dir(), row.id, "a==1\n")


# --- GC --------------------------------------------------------------------------

def test_env_gc_follows_kept_runtimes_and_waits_for_pins(db):
  source = _data_dir() / "apps" / "gc-demo"
  source.mkdir(parents=True)
  row = models.App(name="GC", slug="gc-demo", description="", source_dir=str(source),
                   jsx_source="export default () => null")
  db.add(row)
  db.commit()
  revisions = [str(index) * 64 for index in range(1, 4)]
  builds = _data_dir() / "app-envs" / "builds"
  envs = {}
  for index, revision in enumerate(revisions):
    lock = f"a=={index}\n"
    _runtime_tree(applied_app_runtime.runtime_parent(row.id) / revision, lock=lock)
    _fake_env(builds / revision[:8])
    envs[revision] = _keyed(_data_dir(), row.id, lock)
    envs[revision].parent.mkdir(parents=True, exist_ok=True)
    envs[revision].symlink_to(os.path.relpath(builds / revision[:8], envs[revision].parent))
  stale_interpreter = app_python_env.envs_parent(_data_dir(), row.id) / "cpython-311-old-abc"
  _fake_env(stale_interpreter)
  row.runtime_revision = revisions[-1]
  db.commit()

  pin = applied_app_runtime.hold_runtime(row.id)
  try:
    applied_app_runtime.prune_runtime(row, previous_revision=revisions[-2])
    assert all(env.is_dir() for env in envs.values()) and stale_interpreter.is_dir()
  finally:
    pin.close()

  assert applied_app_runtime.prune_runtime(row, previous_revision=revisions[-2]) == 1
  assert {path.name for path in app_python_env.envs_parent(_data_dir(), row.id).iterdir()} == {
    envs[revisions[-2]].name, envs[revisions[-1]].name,
  }
  assert sorted(path.name for path in builds.iterdir()) == [revisions[1][:8], revisions[2][:8]]

  app_python_env.remove_app_envs(_data_dir(), row.id)
  assert not app_python_env.envs_parent(_data_dir(), row.id).exists()
  assert list(builds.iterdir()) == []
